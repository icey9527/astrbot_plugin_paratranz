"""ParaTranz OpenAPI 的极简异步客户端。

设计原则：
1. 默认只读。写操作必须显式传入 allow_write=True，否则直接抛异常。
2. 全局串行 + 最小间隔，避免触发 480 次/分钟的限流。
3. Token 可选：不带 token 也能查公开数据（项目列表/项目信息/文件进度/术语/成员）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp


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
    ):
        self.base_url = (base_url or "https://paratranz.cn").rstrip("/")
        self.token = (token or "").strip()
        self.timeout = float(timeout or 15)
        self.min_interval = max(0.0, float(min_interval or 0.0))
        self._sem = asyncio.Semaphore(max(1, int(max_concurrency or 1)))
        self._last_call = 0.0
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------ 基础

    @property
    def has_token(self) -> bool:
        return bool(self.token)

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

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

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

        async with self._sem:
            for attempt in range(retry + 1):
                await self._throttle()
                session = await self._get_session()
                try:
                    async with session.request(
                        method.upper(), url, params=clean_params, json=json_body
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
                            raise ParaTranzError(
                                f"没有权限（HTTP {resp.status}）：词条正文与导出数据需要 Token，且必须是项目成员",
                                resp.status,
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
                except asyncio.TimeoutError as exc:
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

    async def list_members(self, project_id: int) -> list[dict]:
        return await self._request("GET", f"/projects/{project_id}/members")

    async def list_strings(
        self,
        project_id: int,
        file_id: int | None = None,
        stage: int | None = None,
        page: int = 1,
        page_size: int = 20,
        query: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if file_id is not None:
            params["file"] = file_id
        if stage is not None:
            params["stage"] = stage
        if query:
            params["query"] = query
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

    # ------------------------------------------------------------------ 汇总

    @staticmethod
    def summarize_files(files: list[dict]) -> dict[str, int]:
        keys = ("total", "translated", "disputed", "checked", "reviewed", "locked")
        total = dict.fromkeys(keys, 0)
        for f in files:
            for k in keys:
                total[k] += int(f.get(k) or 0)
        return total
