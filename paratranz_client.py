"""ParaTranz OpenAPI 的极简异步客户端。

设计原则：
1. 默认只读。写操作必须显式传入 allow_write=True，否则直接抛异常。
2. 全局串行 + 最小间隔，避免触发 480 次/分钟的限流。
3. Token 可选：不带 token 也能查公开数据（项目列表/项目信息/文件进度/术语/成员）。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

import aiohttp
from astrbot.api import logger


class ParaTranzError(Exception):
    """带上状态码与业务提示的异常，方便上层给出人话报错。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


class ParaTranzClient:
    def __init__(
        self,
        base_url: str = "https://paratranz.cn",
        token: str = "",
        timeout: float = 15.0,
        min_interval: float = 0.2,
        max_concurrency: int = 1,
        session_path: str | None = None,
        write_with_session: bool = True,
    ):
        self.base_url = (base_url or "https://paratranz.cn").rstrip("/")
        self.token = (token or "").strip()
        self.timeout = float(timeout or 15)
        self.min_interval = max(0.0, float(min_interval or 0.0))
        self._sem = asyncio.Semaphore(max(1, int(max_concurrency or 1)))
        self._last_call = 0.0
        self._session: aiohttp.ClientSession | None = None
        self._write_session: aiohttp.ClientSession | None = None

        # 会话（sid）：带它写入才会记到 history 里并署名，纯 token 只会记成匿名导入
        self.session_path = session_path
        self.write_with_session = bool(write_with_session)
        self._sid: str = ""
        self._sid_expires: float = 0.0
        self.session_uid: int = 0
        self.session_name: str = ""
        self._load_session()

    # ------------------------------------------------------------------ 基础

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    # ------------------------------------------------------------------ 会话

    def _load_session(self) -> None:
        """从磁盘读上次换到的 sid（跟 token 放一个目录）。"""
        if not self.session_path:
            return
        try:
            import json as _json

            with open(self.session_path, encoding="utf-8") as f:
                data = _json.load(f)
        except (OSError, ValueError):
            return
        if str(data.get("token_tail", "")) != self.token[-6:]:
            return  # token 换过，旧会话作废
        self._sid = str(data.get("sid") or "")
        self._sid_expires = float(data.get("expires_at") or 0)
        try:
            self.session_uid = int(data.get("uid") or 0)
        except (TypeError, ValueError):
            self.session_uid = 0
        self.session_name = str(data.get("username") or "")

    def _save_session(self) -> None:
        if not self.session_path:
            return
        try:
            import json as _json
            import os as _os

            _os.makedirs(_os.path.dirname(self.session_path) or ".", exist_ok=True)
            with open(self.session_path, "w", encoding="utf-8") as f:
                _json.dump(
                    {
                        "sid": self._sid,
                        "expires_at": self._sid_expires,
                        "uid": self.session_uid,
                        "username": self.session_name,
                        "token_tail": self.token[-6:],
                        "at": datetime.now().isoformat(timespec="seconds"),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            try:
                _os.chmod(self.session_path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            logger.warning(f"[ParaTranz] 保存会话失败：{exc}")

    @property
    def session_valid(self) -> bool:
        return bool(self._sid) and time.time() < self._sid_expires - 60

    async def ensure_session(self, force: bool = False) -> bool:
        """用 token 换一次 sid；已在有效期内就直接用。"""
        if not self.has_token or not self.write_with_session:
            return False
        if not force and self.session_valid:
            return True
        url = f"{self.base_url}/oauth/token"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
            ) as sess, sess.post(
                url, data={"token": self.token}, allow_redirects=False
            ) as resp:
                if resp.status not in (200, 301, 302, 303):
                    logger.info(f"[ParaTranz] 换会话失败：HTTP {resp.status}")
                    return False
                sid = ""
                expires = 0.0
                raw = ""
                for cookie in resp.headers.getall("Set-Cookie", []):
                    if cookie.startswith("sid="):
                        raw = cookie
                        sid = cookie.split(";", 1)[0][4:]
                    elif cookie.startswith("user="):
                        try:
                            import urllib.parse as _up

                            payload = _up.unquote(cookie.split(";", 1)[0][5:])
                            import json as _json

                            info = _json.loads(payload)
                            self.session_uid = int(info.get("id") or 0)
                            self.session_name = str(info.get("username") or "")
                        except (ValueError, TypeError):
                            pass
                if not sid:
                    logger.info("[ParaTranz] 换会话没拿到 sid")
                    return False
                import re as _re

                m = _re.search(r"Max-Age=(\d+)", raw) or _re.search(
                    r"Expires=([^;]+)", raw
                )
                if m and m.group(1).isdigit():
                    expires = time.time() + int(m.group(1))
                else:
                    expires = time.time() + 7 * 86400
                self._sid = sid
                self._sid_expires = expires
                self._save_session()
                logger.info(
                    f"[ParaTranz] 会话已更新：{self.session_name or '?'}"
                    f"（uid {self.session_uid or '?'}）"
                )
                return True
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[ParaTranz] 换会话异常：{type(exc).__name__}: {exc}")
            return False

    def _cookie_header(self) -> dict[str, str]:
        if not self._sid:
            return {}
        return {"Cookie": f"sid={self._sid}"}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # 注意：ParaTranz 的 nginx 会拦 User-Agent 里带 "bot" 的请求（直接 403），
            # 所以这里千万别写成 AstrBot-Bot 之类，实测 AstrBot 也会被拦。
            headers = {
                "Accept": "application/json",
                "User-Agent": "ParaTranz-Client/0.1",
            }
            if self.has_token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers=headers,
            )
        return self._session

    async def _get_write_session(self) -> aiohttp.ClientSession:
        """写操作用的会话：只带 Cookie，绝不能带 Authorization。

        token 和 sid 同时出现时服务端优先认 token，就会退回匿名导入，
        所以这两个会话必须分开。
        """
        if self._write_session is None or self._write_session.closed:
            self._write_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
                },
            )
        return self._write_session

    async def close(self) -> None:
        for sess in (self._session, self._write_session):
            if sess and not sess.closed:
                await sess.close()
        self._session = None
        self._write_session = None

    async def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        allow_write: bool = False,
        retry: int = 2,
    ) -> Any:
        if method.upper() != "GET" and not allow_write:
            raise ParaTranzError("写操作已被插件配置禁止（enable_write=false）")

        url = f"{self.base_url}/api{path}"
        clean_params = {
            k: v for k, v in (params or {}).items() if v is not None and v != ""
        }
        is_write = method.upper() != "GET"
        relogged = False
        if is_write and self.write_with_session:
            await self.ensure_session()

        async with self._sem:
            for attempt in range(retry + 1):
                await self._throttle()
                use_cookie = is_write and self.write_with_session and bool(self._sid)
                session = (
                    await self._get_write_session()
                    if use_cookie
                    else await self._get_session()
                )
                extra = self._cookie_header() if use_cookie else {}
                try:
                    async with session.request(
                        method.upper(),
                        url,
                        params=clean_params,
                        json=json_body,
                        headers=extra or None,
                    ) as resp:
                        if resp.status == 429 and attempt < retry:
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        if resp.status in (200, 201):
                            ctype = resp.headers.get("Content-Type", "")
                            if "json" in ctype:
                                return await resp.json()
                            return await resp.text()
                        if resp.status in (401, 403):
                            # 会话过期/失效时重登一次再试，别直接判死
                            if (
                                is_write
                                and self.write_with_session
                                and not relogged
                                and await self.ensure_session(force=True)
                            ):
                                relogged = True
                                continue
                            hint = (
                                "会话可能过期，或缺少权限（已审核的词条、标为已审核都需要校对以上权限）"
                                if is_write and self.write_with_session
                                else "词条正文与导出数据需要 Token，且必须是项目成员"
                            )
                            raise ParaTranzError(
                                f"没有权限（HTTP {resp.status}）：{hint}", resp.status
                            )
                        if resp.status == 404:
                            raise ParaTranzError(
                                "资源不存在（项目 ID / 文件 ID 可能不对）", 404
                            )
                        if resp.status == 423:
                            raise ParaTranzError(
                                "目标资源正被其他用户更新或回滚中（HTTP 423），稍后再试",
                                423,
                            )
                        detail = ""
                        try:
                            payload = await resp.json()
                            detail = payload.get("message") or payload.get("code") or ""
                        except Exception:
                            detail = (await resp.text())[:120]
                        raise ParaTranzError(
                            f"请求失败（HTTP {resp.status}）{detail}", resp.status
                        )
                except TimeoutError as exc:
                    if attempt < retry:
                        continue
                    raise ParaTranzError("请求超时，稍后再试") from exc
                except aiohttp.ClientError as exc:
                    if attempt < retry:
                        await asyncio.sleep(1.0)
                        continue
                    raise ParaTranzError(f"网络错误：{exc}") from exc

        raise ParaTranzError("请求失败")

    # ------------------------------------------------------------------ 读接口

    async def list_projects(self, page: int = 1, page_size: int = 20) -> dict:
        return await self._request(
            "GET", "/projects", params={"page": page, "pageSize": page_size}
        )

    async def get_project(self, project_id: int) -> dict:
        return await self._request("GET", f"/projects/{project_id}")

    async def list_files(self, project_id: int) -> list[dict]:
        return await self._request("GET", f"/projects/{project_id}/files")

    async def list_terms(
        self, project_id: int, page: int = 1, page_size: int = 50
    ) -> dict:
        return await self._request(
            "GET",
            f"/projects/{project_id}/terms",
            params={"page": page, "pageSize": page_size},
        )

    async def list_tasks(
        self, project_id: int, page: int = 1, page_size: int = 50
    ) -> dict:
        """任务列表。返回 {results, page, pageCount, rowCount}。"""
        return await self._request(
            "GET",
            f"/projects/{project_id}/tasks",
            params={"page": page, "pageSize": page_size},
        )

    async def set_stages_batch(
        self,
        project_id: int,
        string_ids: list[int],
        stage: int,
        allow_write: bool = False,
    ) -> dict:
        """批量改状态，一次 API 调用搞定。"""
        items = [{"id": int(sid), "stage": int(stage)} for sid in string_ids]
        return await self._request(
            "PUT",
            f"/projects/{project_id}/strings",
            json_body={"op": "edit", "items": items},
            allow_write=allow_write,
        )

    async def list_scores(
        self, project_id: int, page: int = 1, page_size: int = 20
    ) -> dict:
        return await self._request(
            "GET",
            f"/projects/{project_id}/scores",
            params={"page": page, "pageSize": page_size},
        )

    async def list_issues(
        self,
        project_id: int,
        status: int | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> dict:
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if status is not None:
            params["status"] = status
        return await self._request(
            "GET", f"/projects/{project_id}/issues", params=params
        )

    async def get_issue(self, project_id: int, issue_id: int) -> dict:
        return await self._request("GET", f"/projects/{project_id}/issues/{issue_id}")

    async def list_history(
        self,
        project_id: int,
        uid: int | None = None,
        tid: int | None = None,
        kind: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> dict:
        """项目历史。kind 可选 translate/edit/check/review/term/import/comment。"""
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if uid is not None:
            params["uid"] = uid
        if tid is not None:
            params["tid"] = tid
        if kind:
            params["type"] = kind
        return await self._request(
            "GET", f"/projects/{project_id}/history", params=params
        )

    async def request_raw(
        self, method: str, path: str, json_body: dict[str, Any] | None = None
    ) -> Any:
        """插件内部使用：不受 enable_write 限制的裸请求，只在明确知情时用。"""
        return await self._request(method, path, json_body=json_body, allow_write=True)

    async def batch_edit_strings(
        self,
        project_id: int,
        items: list[dict],
        allow_write: bool = False,
    ) -> list[int]:
        """批量改词条。items 里每项至少要有 id，可带 translation/stage/key/original/context。

        服务端一次最多处理 100 条，返回成功更新的 id 列表。
        """
        return await self._request(
            "PUT",
            f"/projects/{project_id}/strings",
            json_body={"op": "edit", "items": items},
            allow_write=allow_write,
        )

    async def create_issue(
        self,
        project_id: int,
        title: str,
        content: str,
        allow_write: bool = False,
    ) -> dict:
        return await self._request(
            "POST",
            f"/projects/{project_id}/issues",
            json_body={"title": title, "content": content},
            allow_write=allow_write,
        )

    async def reply_issue(
        self,
        project_id: int,
        issue_id: int,
        content: str,
        allow_write: bool = False,
    ) -> dict:
        # 服务端只认 op=reply，少了就 400 "op not valid"
        return await self._request(
            "POST",
            f"/projects/{project_id}/issues/{issue_id}",
            json_body={"op": "reply", "content": content},
            allow_write=allow_write,
        )

    async def delete_issue(
        self, project_id: int, issue_id: int, allow_write: bool = False
    ) -> str:
        return await self._request(
            "DELETE",
            f"/projects/{project_id}/issues/{issue_id}",
            allow_write=allow_write,
        )

    async def list_messages(self, page: int = 1, page_size: int = 20) -> dict:
        """站内通知/消息（含 @、回复、成员变动等）。"""
        return await self._request(
            "GET", "/messages", params={"page": page, "pageSize": page_size}
        )

    async def list_comments(
        self,
        tid: int,
        ctype: str = "text",
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """读评论。注意 type 必须和 tid 一起传，只给 tid 服务端会忽略筛选。"""
        return await self._request(
            "GET",
            "/comments",
            params={
                "type": ctype,
                "tid": int(tid),
                "page": page,
                "pageSize": page_size,
            },
        )

    async def list_all_comments(
        self, tid: int, ctype: str = "text", cap: int = 300
    ) -> list[dict]:
        """把一条词条下的评论全部拉下来（按页翻到底），老到新排序。"""
        out: list[dict] = []
        page = 1
        page_size = 100
        while len(out) < cap:
            data = await self.list_comments(tid, ctype, page, page_size)
            rows = data.get("results", []) if isinstance(data, dict) else (data or [])
            rows = [r for r in rows if int(r.get("tid") or tid) == int(tid)]
            out.extend(rows)
            total = (
                int((data or {}).get("rowCount") or 0) if isinstance(data, dict) else 0
            )
            if not rows or len(rows) < page_size or (total and len(out) >= total):
                break
            page += 1
        out.sort(key=lambda r: int(r.get("id") or 0))
        return out[:cap]

    async def add_comment(
        self,
        tid: int,
        content: str,
        ctype: str = "text",
        reply_to: int | None = None,
        allow_write: bool = False,
    ) -> dict:
        body: dict[str, Any] = {
            "type": ctype,
            "tid": int(tid),
            "content": content,
            "images": [],
        }
        if reply_to:
            body["replyTo"] = int(reply_to)
        return await self._request(
            "POST", "/comments", json_body=body, allow_write=allow_write
        )

    async def delete_comment(self, comment_id: int, allow_write: bool = False) -> Any:
        return await self._request(
            "DELETE", f"/comments/{int(comment_id)}", allow_write=allow_write
        )

    async def get_suggestions(
        self,
        project_id: int,
        string_id: int,
        rate: int = 70,
        scope: str = "self",
    ) -> list[dict]:
        """翻译建议（翻译记忆）。高开销接口，调用方需自行串行。"""
        return await self._request(
            "GET",
            f"/projects/{project_id}/strings/{string_id}/suggestions",
            params={"rate": rate, "scope": scope},
        )

    async def list_strings(
        self,
        project_id: int,
        file_id: int | None = None,
        stage: int | None = None,
        page: int = 1,
        page_size: int = 20,
        query: str | None = None,
        text: str | None = None,
        translation: str | None = None,
        context: str | None = None,
        key: str | None = None,
        regex: str | None = None,
        regex_on: str = "original",
        regex_negate: bool = False,
    ) -> dict:
        """读词条列表。

        高级搜索的过滤参数都是服务端 AND 组合：
        text 搜原文、translation 搜译文、context 搜上下文备注、key 搜键值。
        regex 走服务端正则（`original~=` + `advanced=1`），regex_on 可选 original / translation。
        query 只在没给 text 时当 text 用。
        """
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if file_id is not None:
            params["file"] = file_id
        if stage is not None:
            params["stage"] = stage
        if text:
            params["text"] = text
        elif query:
            params["text"] = query
        if translation:
            params["translation"] = translation
        if context:
            params["context"] = context
        if key:
            params["key"] = key
        if regex:
            # 服务端正则：field~=pattern（匹配）或 field!~=pattern（不匹配），都加 advanced=1
            # 注意：参数名里的 ~ 会被 aiohttp 编码成 %7E 导致服务端 500，必须手动拼 URL
            field = "translation" if regex_on == "translation" else "original"
            op = "!~" if regex_negate else "~"
            from urllib.parse import quote, urlencode

            params["advanced"] = 1
            qs = urlencode(
                {k: v for k, v in params.items() if v is not None and v != ""}
            )
            qs += f"&{field}{op}={quote(regex)}"
            return await self._request("GET", f"/projects/{project_id}/strings?{qs}")
        return await self._request(
            "GET", f"/projects/{project_id}/strings", params=params
        )

    # ------------------------------------------------------------------ 写接口

    async def bulk_get_strings(
        self, project_id: int, string_ids: list[int], concurrency: int = 6
    ) -> dict[int, dict]:
        """批量取词条（用于发起提交时抓旧译文），临时放宽串行限制，一次并发几个。"""
        sem_old, interval_old = self._sem, self.min_interval
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self.min_interval = 0.0
        out: dict[int, dict] = {}
        try:

            async def one(sid: int) -> None:
                try:
                    out[int(sid)] = await self.get_string(project_id, int(sid))
                except Exception:  # noqa: BLE001
                    return

            await asyncio.gather(*(one(sid) for sid in string_ids))
        finally:
            self._sem, self.min_interval = sem_old, interval_old
        return out

    async def get_string(self, project_id: int, string_id: int) -> dict:
        return await self._request("GET", f"/projects/{project_id}/strings/{string_id}")

    async def update_string(
        self,
        project_id: int,
        string_id: int,
        translation: str,
        stage: int | None = None,
        allow_write: bool = False,
    ) -> dict:
        body: dict[str, Any] = {"translation": translation}
        if stage is not None:
            body["stage"] = stage
        return await self._request(
            "PUT",
            f"/projects/{project_id}/strings/{string_id}",
            json_body=body,
            allow_write=allow_write,
        )

    async def set_string_stage(
        self,
        project_id: int,
        string_id: int,
        stage: int,
        allow_write: bool = False,
    ) -> dict:
        """只改状态，不动译文，也不碰原文。"""
        return await self._request(
            "PUT",
            # 注意：必须是集合路径 /strings，写到 /strings/{sid} 服务端也回 200，
            # 但那 200 是假的，状态压根没变（实测过，别踩这个坑）。
            f"/projects/{project_id}/strings",
            json_body={
                "op": "edit",
                "items": [{"id": int(string_id), "stage": int(stage)}],
            },
            allow_write=allow_write,
        )

    async def list_members(self, project_id: int) -> list[dict]:
        data = await self._request("GET", f"/projects/{project_id}/members")
        return data if isinstance(data, list) else (data or {}).get("results", [])

    # ------------------------------------------------------------------ 汇总

    @staticmethod
    def summarize_files(files: list[dict]) -> dict[str, int]:
        keys = ("total", "translated", "disputed", "checked", "reviewed", "locked")
        total = dict.fromkeys(keys, 0)
        for f in files:
            for k in keys:
                total[k] += int(f.get(k) or 0)
        return total
