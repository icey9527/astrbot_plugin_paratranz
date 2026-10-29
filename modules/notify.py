"""站内通知轮询。

ParaTranz 不会主动推送给我，只能定时拉 /api/messages。
这一层干三件事：
1. 轮询拉取 + 本地过滤（类型、是否绑定的项目、见过的 id、id 水位线）；
2. 新通知交给模型，由它自己判断回不回、要不要标状态；
3. 动作直接在云平台上落地（词条评论 / 讨论回复），不往群里播报。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from astrbot.api.all import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.agent.tool import FunctionTool, ToolSet

STAGE_NAMES = {
    0: "未翻译",
    1: "已翻译",
    2: "有疑问",
    3: "已检查",
    5: "已审核",
    9: "已锁定",
}
STAGE_ALIASES = {"已翻译": 1, "有疑问": 2, "已检查": 3}

# 值得推给群里的类型
INTERESTING = {
    "string.mention": "@ 了",
    "string.comment": "评论了词条",
    "issue.reply": "回复了讨论",
    "issue.comment": "回复了讨论",
    "issue.mention": "在讨论里 @ 了",
}


@dataclass
class PendingNotice:
    id: int
    type: str
    text: str
    project: int | None = None
    string_id: int | None = None
    username: str = ""
    created_at: str = ""
    session: str | None = None
    is_owner: bool = False
    user_id: int | None = None
    issue_id: int | None = None
    mention: str = ""
    at: float = field(default_factory=time.time)

    def brief(self) -> str:
        where = []
        if self.project:
            where.append(f"项目 {self.project}")
        if self.string_id:
            where.append(f"词条 {self.string_id}")
        who = f"{self.username}（主人）" if self.is_owner else self.username
        head = f"{who} {INTERESTING.get(self.type, self.type)}"
        tail = "　".join(where)
        body = self.text.strip()[:80]
        line = f"· {head}" + (f"（{tail}）" if tail else "")
        return f"{line}\n  {body}" if body else line


class Notifier:
    """通知轮询器。一个插件实例一个，跟着 initialize / terminate 起停。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        cfg = getattr(plugin, "config", {}) or {}
        self.enabled = bool(cfg.get("notify_enable", False))
        self.interval = max(
            15,
            int(cfg.get("notify_interval_minutes") or cfg.get("notify_interval") or 60),
        )
        self.mode = str(cfg.get("notify_mode", "batch") or "batch").lower()
        self.batch_size = max(1, int(cfg.get("notify_batch_size", 3) or 3))
        self.batch_minutes = max(1, int(cfg.get("notify_batch_minutes", 30) or 30))
        self.max_per_round = max(1, int(cfg.get("notify_max_per_round", 20) or 20))

        self._task: asyncio.Task[None] | None = None
        self._seen: set[int] = set()
        self._max_seen: int = 0  # 水位线：只处理比它新的，别信服务端已读状态
        self._pending: list[PendingNotice] = []
        self._max_seen = self._load_watermark()
        self._first_pending_at: float = 0.0
        self._acked: dict[str, float] = {}
        self._held: list[PendingNotice] = []
        self._held_since: float = 0.0
        self.min_count = max(1, min(10, int(cfg.get("notify_min_count", 1) or 1)))
        self._warned_unsupported = False

    # ------------------------------------------------------------------ 调试

    def _dbg(self, msg: str) -> None:
        """往 plugin_data 里记一行排查日志，出问题直接看文件。"""
        try:
            path = os.path.join(self.plugin.store.dir, "notify_debug.log")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path) and os.path.getsize(path) > 200_000:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    tail = f.readlines()[-300:]
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(tail)
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
        except Exception:  # noqa: BLE001
            pass

    # 水位线单独存一个小文件，别跟 state.json 混，插件写状态时容易把它冲掉
    def _watermark_path(self) -> str:
        return os.path.join(self.plugin.store.dir, "notify_seen.json")

    def _load_watermark(self) -> int:
        try:
            with open(self._watermark_path(), encoding="utf-8") as f:
                return int(json.load(f).get("seen") or 0)
        except Exception:  # noqa: BLE001
            return 0

    async def _save_watermark(self) -> None:
        try:
            path = self._watermark_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"seen": int(self._max_seen or 0)}, f)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if not self.enabled:
            logger.info("[ParaTranz] 通知轮询未启用")
            return
        if not getattr(self.plugin.client, "has_token", False):
            logger.info("[ParaTranz] 没有 Token，通知轮询不启动")
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="paratranz-notify")
        self._log_group_sendable()
        logger.info(
            f"[ParaTranz] 通知轮询已启动：每 {self.interval}s 一轮，模式 {self.mode}"
        )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if not task:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def _loop(self) -> None:
        # 第一轮稍等一下，别跟插件初始化抢资源
        await asyncio.sleep(10)
        while True:
            try:
                fresh = await self.poll_once()
                if fresh:
                    if not self._held:
                        self._held_since = time.time()
                    self._held.extend(fresh)
                    self._dbg(f"攒下 {len(self._held)}/{self.min_count} 条")
                self.drop_pending()
                if self._held and (
                    len(self._held) >= self.min_count
                    or time.time() - self._held_since >= 300
                ):
                    batch, self._held = self._held, []
                    self._held_since = 0.0
                    # 不再往群里播报，抓到就在云平台上直接回
                    await self._auto_ack(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ParaTranz] 通知轮询出错：{exc}")
                try:
                    await self.plugin.report_error_once(
                        f"notify:{type(exc).__name__}",
                        f"站内通知轮询出错了：{type(exc).__name__}: {exc}",
                    )
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(self.interval)

    # ------------------------------------------------------------------ 拉取与过滤

    async def poll_once(self, baseline: bool = False) -> list[PendingNotice]:
        data = await self.plugin.client.list_messages(1, 50)
        rows = data.get("results", []) if isinstance(data, dict) else (data or [])
        # 通知里的 to 就是自己的 uid，面板没填时顺手学下来
        for row in rows:
            to = row.get("to")
            if (
                isinstance(to, int)
                and to > 0
                and not getattr(self.plugin, "_self_uid", 0)
            ):
                self.plugin._self_uid = to
                break
        ids = [int(r.get("id") or 0) for r in rows if str(r.get("id") or "").isdigit()]
        top = max(ids) if ids else 0
        if self._max_seen == 0 and not baseline:
            # 头一回跑：只记水位，别把历史通知全当新的回一遍
            self._max_seen = top
            await self._save_watermark()
            self._dbg(f"首轮建水位 {top}，不回历史通知")
            return []
        fresh = [r for r in rows if self._accept(r)]
        for r in fresh[: self.max_per_round]:
            notice = self._to_notice(r)
            if not notice:
                continue
            self._seen.add(notice.id)
            self._pending.append(notice)
        if top > self._max_seen:
            self._max_seen = top
            await self._save_watermark()
        if fresh:
            await self._mark_all_read()
            logger.info(f"[ParaTranz] 收到 {len(fresh)} 条新通知")
            self._dbg(
                f"拉到 {len(fresh)} 条新通知（水位 {self._max_seen}）："
                + ", ".join(str(r.get("type")) for r in fresh[:5])
            )
        return self._pending

    def _accept(self, row: dict) -> bool:
        try:
            mid = int(row.get("id") or 0)
        except (TypeError, ValueError):
            return False
        if mid <= 0 or mid in self._seen:
            return False
        if self._max_seen and mid <= self._max_seen:
            return False
        if str(row.get("type")) not in INTERESTING:
            return False
        # /api/messages 只返回发给自己的通知，不必再按收件人筛
        project = ((row.get("link") or {}).get("project")) or None
        if project:
            try:
                if int(project) not in self._bound_projects():
                    return False
            except (TypeError, ValueError):
                pass
        return True

    def _self_uid(self) -> int:
        """自己的 ParaTranz 用户 ID。API 用不到它，只拿来给本地记录署名。"""
        uid = getattr(self.plugin, "_self_uid", 0)
        try:
            return int(uid or 0)
        except (TypeError, ValueError):
            return 0

    def self_uid_text(self) -> str:
        uid = self._self_uid()
        return str(uid) if uid else ""

    def _bound_projects(self) -> set[int]:
        binds = self.plugin.store.all_binds() or {}
        out: set[int] = set()
        for pids in binds.values():
            for pid in pids if isinstance(pids, list) else [pids]:
                try:
                    out.add(int(pid))
                except (TypeError, ValueError):
                    continue
        return out

    def _to_notice(self, row: dict) -> PendingNotice | None:
        link = row.get("link") or {}
        user = row.get("fromUser") or {}
        content = row.get("content")
        if isinstance(content, dict):
            content = content.get("text") or ""
        project = link.get("project")
        string_id = link.get("id")
        if str(row.get("target")) == "comment" and string_id:
            # 评论通知里 link.id 是词条 id，评论 id 在 tid
            pass
        is_issue = str(row.get("type") or "").startswith("issue.")
        return PendingNotice(
            id=int(row.get("id") or 0),
            type=str(row.get("type") or ""),
            text=str(content or ""),
            project=int(project) if project else None,
            string_id=None
            if is_issue
            else (int(string_id) if isinstance(string_id, int) else None),
            issue_id=int(string_id)
            if is_issue and isinstance(string_id, int)
            else None,
            username=str(
                user.get("nickname") or user.get("username") or row.get("from") or ""
            ),
            mention=str(user.get("username") or ""),
            created_at=str(row.get("createdAt") or ""),
            session=self._session_for(project),
            is_owner=bool(getattr(self.plugin, "is_owner", lambda _u: False)(user)),
            user_id=(
                int(user.get("id"))
                if str(user.get("id") or "").isdigit()
                else (
                    int(row["from"]) if str(row.get("from") or "").isdigit() else None
                )
            ),
        )

    def _session_for(self, project: Any) -> str | None:
        try:
            pid = int(project)
        except (TypeError, ValueError):
            return None
        for group_id, pids in (self.plugin.store.all_binds() or {}).items():
            seq = pids if isinstance(pids, list) else [pids]
            if pid in [int(p) for p in seq if str(p).isdigit()]:
                # 会话字符串格式：platform_id:GroupMessage:group_id
                cands = self.plugin.session_candidates(str(group_id))
                if cands:
                    return cands[0]
        return None

    async def _mark_all_read(self) -> None:
        """官方只给了全量已读，没有单条接口。"""
        if not getattr(getattr(self.plugin, "client", None), "has_token", False):
            return
        try:
            await self.plugin.client.request_raw("PUT", "/messages", {"op": "readAll"})
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[ParaTranz] 标记已读失败：{exc}")

    # ------------------------------------------------------------------ 出口

    def ready_batch(self) -> list[PendingNotice]:
        """按模式判断现在该不该把攒的东西吐出来。"""
        if not self._pending:
            return []
        if self.mode == "one":
            out, self._pending = self._pending, []
            self._first_pending_at = 0.0
            return out
        waited = time.time() - (self._first_pending_at or time.time())
        if len(self._pending) >= self.batch_size or waited >= self.batch_minutes * 60:
            out, self._pending = self._pending, []
            self._first_pending_at = 0.0
            return out
        if not self._first_pending_at:
            self._first_pending_at = time.time()
        return []

    def take_all(self) -> list[PendingNotice]:
        """把攒着的全拿出来（工具用，不管模式）。"""
        out, self._pending = self._pending, []
        self._first_pending_at = 0.0
        return out

    def pending_count(self) -> int:
        return len(self._pending)

    def render_batch(self, notices: list[PendingNotice]) -> str:
        if not notices:
            return ""
        others = [n for n in notices if not n.is_owner]
        tail = (
            f"\n（其中 {len(others)} 条不是主人发的，动手前先看一眼是谁）"
            if others
            else ""
        )
        if len(notices) == 1:
            return "翻译那边有人找你：\n" + notices[0].brief() + tail
        lines = [f"翻译那边攒了 {len(notices)} 条："]
        lines.extend(n.brief() for n in notices)
        if tail:
            lines.append(tail.strip())
        return "\n".join(lines)

    async def _auto_ack(self, notices: list[PendingNotice]) -> None:
        """把新通知交给模型，由它自己决定回不回、要不要动状态。

        只处理主人和项目内成员，非成员不理。同一个词条/讨论 10 分钟只处理一次。
        """
        cfg = getattr(self.plugin, "config", {}) or {}
        allow_owner = bool(cfg.get("auto_reply_owner", True))
        allow_member = bool(cfg.get("auto_reply_member", True))
        allow_stage = bool(cfg.get("auto_stage", True))
        if not (allow_owner or allow_member):
            return
        now = time.time()
        for notice in notices:
            if notice.type not in INTERESTING:
                continue
            if not (notice.string_id or notice.issue_id):
                continue
            if not notice.is_owner:
                if not allow_member:
                    continue
                if not await self.plugin.is_project_member(
                    notice.project, notice.user_id
                ):
                    logger.info(
                        f"[ParaTranz] 跳过：uid {notice.user_id} 不是项目 "
                        f"{notice.project} 的成员"
                    )
                    continue
            key = f"{notice.issue_id or 0}:{notice.string_id or 0}"
            last = self._acked.get(key, 0.0)
            if last and now - last < 600:
                self._dbg(f"  冷却中，跳过 {key}")
                continue
            self._acked[key] = now
            self._dbg(
                f"处理 {notice.type} 项目{notice.project} "
                f"词条{notice.string_id or '-'} 讨论{notice.issue_id or '-'} "
                f"来自{notice.username}({notice.user_id})：{notice.text[:60]}"
            )
            result = await self._handle(notice, allow_stage)
            self._dbg(f"  → 结果：{result}")

    async def _build_context(self, notice: PendingNotice) -> str:
        lines: list[str] = []
        if notice.issue_id and notice.project:
            issue = await self.plugin.client.get_issue(notice.project, notice.issue_id)
            lines.append(f"【讨论 #{notice.issue_id}】{issue.get('title') or ''}")
            body = issue.get("content") or ""
            if body:
                lines.append("正文：" + str(body)[:400])
            acts = issue.get("activities") or []
            for r in acts[-6:]:
                who = (r.get("user") or {}).get("nickname") or ""
                body = str(r.get("content") or "").strip()
                if body:
                    lines.append(f"· {who}：{body[:200]}")
        elif notice.string_id and notice.project:
            row = await self.plugin.client.get_string(notice.project, notice.string_id)
            stage = STAGE_NAMES.get(int(row.get("stage") or 0), row.get("stage"))
            lines.append(f"【词条 {notice.string_id}】状态：{stage}")
            lines.append("原文：" + str(row.get("original") or "")[:300])
            lines.append(
                "现译文：" + (str(row.get("translation") or "")[:300] or "（空）")
            )
            if row.get("context"):
                lines.append("上下文备注：" + str(row.get("context"))[:200])
            try:
                cm = await self.plugin.client.list_comments(
                    int(notice.string_id), page_size=10
                )
                rows = cm.get("results", []) if isinstance(cm, dict) else (cm or [])
                for c in rows[-5:]:
                    who = (c.get("user") or {}).get("nickname") or ""
                    lines.append(f"· {who}：{str(c.get('content') or '')[:200]}")
            except Exception:  # noqa: BLE001
                pass
        lines.append("")
        lines.append(f"【这次的新消息】{notice.username}：{notice.text[:400]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 干活
    #
    # 这不是一问一答的 JSON 判断题，是个小 agent：给它工具（读评论 / 改状态 / 回话），
    # 让它自己转几轮，看完再决定干什么。判断权在模型手上，代码只负责边界（能改什么、
    # 不能改什么）和防重复。

    def _toolset(self, notice: PendingNotice, allow_stage: bool) -> ToolSet:
        tools = [
            FunctionTool(
                name="read_comments",
                description="把这个词条（讨论）下的评论、上下文再读一遍，看清楚再来。",
                parameters={"type": "object", "properties": {}},
            ),
            FunctionTool(
                name="reply",
                description="在平台上回一句话给对方。会自动 @ 那个人，不用自己写 @。",
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "要说的话，20 到 60 字，口语，别客服腔",
                        }
                    },
                    "required": ["text"],
                },
            ),
        ]
        if allow_stage and notice.string_id:
            tools.append(
                FunctionTool(
                    name="set_stage",
                    description="改这个词条的状态。只能改状态，改不了译文。",
                    parameters={
                        "type": "object",
                        "properties": {
                            "stage": {
                                "type": "string",
                                "enum": ["已翻译", "有疑问"],
                                "description": "已翻译=确认没问题；有疑问=还得再看",
                            },
                            "why": {"type": "string", "description": "一句话理由"},
                        },
                        "required": ["stage"],
                    },
                )
            )
        tools.extend(
            [
                FunctionTool(
                    name="search_text",
                    description="按原文搜索项目里的词条，看同一句话别处怎么翻的，保持统一。",
                    parameters={
                        "type": "object",
                        "properties": {
                            "keyword": {"type": "string", "description": "原文里的片段"}
                        },
                        "required": ["keyword"],
                    },
                ),
                FunctionTool(
                    name="search_terms",
                    description="查项目术语表，确认专有名词、人名该译成什么。",
                    parameters={
                        "type": "object",
                        "properties": {
                            "keyword": {
                                "type": "string",
                                "description": "原文或译文关键词",
                            }
                        },
                        "required": ["keyword"],
                    },
                ),
                FunctionTool(
                    name="read_context",
                    description="看当前词条在同一文件里的前后几条，判断语气、人称。",
                    parameters={"type": "object", "properties": {}},
                ),
                FunctionTool(
                    name="project_progress",
                    description="看项目整体进度，判断这条急不急。",
                    parameters={"type": "object", "properties": {}},
                ),
            ]
        )
        if bool(getattr(self.plugin, "config", {}).get("notify_to_qq", False)):
            tools.append(
                FunctionTool(
                    name="tell_qq",
                    description="在 QQ 群里/私聊里说一句，把处理结果顺带告诉那边的人。只在值得说一声时用。",
                    parameters={
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "要发到 QQ 的话，一句话，带上是哪个词条/讨论",
                            }
                        },
                        "required": ["text"],
                    },
                )
            )
        tools.append(
            FunctionTool(
                name="do_nothing",
                description="这条不用管，什么都不做。纯告知类消息才用。",
                parameters={
                    "type": "object",
                    "properties": {
                        "why": {"type": "string", "description": "为什么不理"}
                    },
                    "required": ["why"],
                },
            )
        )
        return ToolSet(tools=tools)

    async def dry_run(self, notice: PendingNotice) -> str:
        """只读模拟：模型照常看上下文、调工具，但回话和改状态都不落库。"""
        return await self._handle(notice, allow_stage=True, dry_run=True)

    async def _handle(
        self, notice: PendingNotice, allow_stage: bool, dry_run: bool = False
    ) -> str:
        """让模型自己看、自己动手，最多转 6 轮（出错还要一轮去跟人解释）。"""
        try:
            provider = self.plugin.context.get_using_provider()
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[ParaTranz] 取模型失败：{type(exc).__name__}: {exc}")
            return "取不到模型"
        if not provider:
            logger.info("[ParaTranz] 没有可用的对话模型，跳过")
            return "没有可用模型"
        try:
            ctx = await self._build_context(notice)
        except Exception as exc:  # noqa: BLE001
            self._dbg(f"  取上下文失败：{type(exc).__name__}: {exc}")
            return "取上下文失败"
        style = str(self.plugin.config.get("auto_reply_text") or "").strip()
        system = (
            "你是翻译协作项目里的一员，替项目里的机器人干活。说话像真人，简短、口语，"
            "不要客服腔，别说自己是 AI。你有工具可以动手：读评论、回话、改词条状态。"
            + (f"语气参考：{style}" if style else "")
        )
        prompt = (
            f"{ctx}\n\n"
            f"最后一行是对方刚说的话。你自己判断要不要动手，能用工具就调工具，"
            f"别只在脑子里想。规矩：\n"
            f"1. 改译文你没权限，也别说能改。\n"
            f"2. 对方点你名、问你在不在、催你、问词条的事，都要 reply，别装死。\n"
            f"3. 状态照对方说的办：说「标回已翻译」就改已翻译，说「这里不对」就改有疑问，"
            f"只是闲聊就别动状态。\n"
            f"4. 纯告知、不需要你干什么的，调 do_nothing 收尾。\n"
            f"5. 拿不准译文对不对、专有名词怎么翻，先用 search_text / search_terms /"
            f" read_context 查一查，别瞎猜。\n"
            f"6. 词条状态是「已审核」「已锁定」的，你改了也会 403；这种情况别硬试，"
            f"直接回话跟对方说「这条已审核/锁定，我改不了，得你或有校对权限的人来改」。\n"
            f"7. QQ 那边的人在等你消息的话，可以用 tell_qq 顺带说一声；没这个工具就别提。\n"
            f"8. 一句话说完就行，别长篇大论。"
        )
        target = (
            f"讨论 #{notice.issue_id}"
            if notice.issue_id
            else f"词条 {notice.string_id}"
        )
        toolset = self._toolset(notice, allow_stage)
        ctxs: list[dict] = [{"role": "user", "content": prompt}]
        notes: list[str] = []

        async def run_tool(name: str, args: dict) -> str:
            client = self.plugin.client
            if name == "read_comments":
                if notice.issue_id:
                    issue = await client.get_issue(notice.project, notice.issue_id)
                    acts = issue.get("activities") or []
                    lines = [f"【{target}】{issue.get('title') or ''}"]
                    for r in acts:
                        who = (r.get("user") or {}).get("nickname") or ""
                        lines.append(f"· {who}：{str(r.get('content') or '')[:200]}")
                    return "\n".join(lines[-30:])
                rows = await client.list_all_comments(
                    int(notice.string_id), "text", 300
                )
                if not rows:
                    return "还没有评论"
                lines = []
                for r in rows:
                    who = (r.get("user") or {}).get("nickname") or ""
                    lines.append(
                        f"· #{r.get('id')} {who}：{str(r.get('content') or '')[:200]}"
                    )
                return f"词条 {notice.string_id} 共 {len(rows)} 条评论：\n" + "\n".join(
                    lines
                )
            if name == "set_stage":
                if not (allow_stage and notice.string_id):
                    return "这个场景不能改状态"
                stage = STAGE_ALIASES.get(str(args.get("stage") or ""))
                if stage not in (1, 2):
                    return "状态只能是 已翻译 或 有疑问"
                want = STAGE_NAMES.get(stage, str(stage))
                if dry_run:
                    notes.append(f"会改状态 {want}")
                    return f"（预览）会把词条 {notice.string_id} 改成「{want}」，实际没有修改"
                try:
                    await client.set_string_stage(
                        notice.project, int(notice.string_id), stage, allow_write=True
                    )
                except Exception as exc:  # noqa: BLE001
                    msg = self._stage_error_hint(exc)
                    notes.append(f"改状态失败（{msg}）")
                    self._dbg(f"    set_stage 失败：{type(exc).__name__}: {exc}")
                    return f"没改成，{msg}。这个你要自己告诉对方，别当成功。"
                # 服务端可能 200 但没生效（老版本单条路径踩过），写后必须回读
                orig = ""
                try:
                    await asyncio.sleep(0.8)
                    row = await client.get_string(notice.project, int(notice.string_id))
                    orig = STAGE_NAMES.get(
                        int(row.get("stage") or 0), str(row.get("stage"))
                    )
                    if int(row.get("stage") or -1) == stage:
                        notes.append(f"改状态 {want}")
                        return (
                            f"已把词条 {notice.string_id} 改成「{want}」（已回读确认）"
                        )
                except Exception as exc:  # noqa: BLE001
                    self._dbg(f"    回读失败：{type(exc).__name__}: {exc}")
                notes.append(f"改状态没生效（还是{orig or '原状态'}）")
                return (
                    f"接口说成功，但回读发现还是「{orig}」，其实没改成。"
                    f"很可能是没权限（已审核/已锁定的词条要校对权限），你要跟对方讲清楚。"
                )
            if name == "reply":
                text = str(args.get("text") or "").strip()
                if not text:
                    return "空话不用发"
                if any(n.startswith("回话") for n in notes):
                    return "已经回过一条了，别刷屏"
                if dry_run:
                    notes.append(f"会回话：{text}")
                    return "（预览）不实际发送"
                content = f"@{notice.mention or notice.username} {text}"
                if notice.issue_id:
                    await client.reply_issue(
                        notice.project, notice.issue_id, content, allow_write=True
                    )
                else:
                    await client.add_comment(
                        int(notice.string_id), content, allow_write=True
                    )
                notes.append("回话")
                return "已发出"
            if name == "search_text":
                kw = str(args.get("keyword") or "").strip()
                if not kw:
                    return "关键词空着没法搜"
                data = await client.list_strings(notice.project, text=kw, page_size=8)
                rows = data.get("results", []) if isinstance(data, dict) else []
                if not rows:
                    return f"原文里没搜到「{kw}」"
                lines = [
                    f"共 {data.get('rowCount', len(rows))} 条，前 {len(rows)} 条："
                ]
                for r in rows:
                    lines.append(
                        f"· #{r.get('id')} 「{str(r.get('original') or '')[:60]}」"
                        f" → 「{str(r.get('translation') or '')[:60] or '未翻译'}」"
                    )
                return "\n".join(lines)
            if name == "search_terms":
                kw = str(args.get("keyword") or "").strip()
                data = await client.list_terms(notice.project, page_size=200)
                rows = (
                    data.get("results", []) if isinstance(data, dict) else (data or [])
                )
                hits = [
                    t
                    for t in rows
                    if kw
                    and (
                        kw in str(t.get("original") or "")
                        or kw in str(t.get("translation") or "")
                    )
                ]
                if not hits:
                    return f"术语表里没有和「{kw}」相关的条目"
                return "\n".join(
                    f"· {t.get('original')} → {t.get('translation')}" for t in hits[:15]
                )
            if name == "read_context":
                if not notice.string_id:
                    return "讨论没有前后文可看"
                data = await client.list_strings(notice.project, page_size=200)
                rows = data.get("results", []) if isinstance(data, dict) else []
                idx = next(
                    (
                        i
                        for i, r in enumerate(rows)
                        if int(r.get("id") or 0) == int(notice.string_id)
                    ),
                    -1,
                )
                if idx < 0:
                    return "当前文件里没定位到这条"
                seg = rows[max(0, idx - 3) : idx + 4]
                return "\n".join(
                    ("→ " if int(r.get("id") or 0) == int(notice.string_id) else "· ")
                    + f"#{r.get('id')} 「{str(r.get('original') or '')[:50]}」→「{str(r.get('translation') or '')[:50] or '未翻译'}」"
                    for r in seg
                )
            if name == "project_progress":
                proj = await client.get_project(notice.project)
                files = await client.list_files(notice.project)
                total = client.summarize_files(files)
                return (
                    f"项目「{proj.get('name') or notice.project}」共 {total.get('total')} 条："
                    f"已翻译 {total.get('translated')}，有疑问 {total.get('disputed')}，"
                    f"已检查 {total.get('checked')}，已审核 {total.get('reviewed')}"
                )
            if name == "tell_qq":
                text = str(args.get("text") or "").strip()
                if not text:
                    return "空话不用发"
                if dry_run:
                    notes.append(f"会发到 QQ：{text}")
                    return "（预览）不实际发送"
                ok = await self._send_qq(notice, text)
                notes.append("QQ 说了一声" if ok else "QQ 没发出去")
                return "已发到 QQ" if ok else "没找到能发消息的会话，算了"
            if name == "do_nothing":
                notes.append(f"不理：{args.get('why') or ''}")
                return "好"
            return f"没有这个工具：{name}"

        for round_no in range(6):
            try:
                resp = await provider.text_chat(
                    contexts=ctxs,
                    system_prompt=system,
                    func_tool=toolset,
                )
            except Exception as exc:  # noqa: BLE001
                self._dbg(f"  模型调用失败：{type(exc).__name__}: {exc}")
                break
            text = (getattr(resp, "completion_text", "") or "").strip()
            names = list(getattr(resp, "tools_call_name", []) or [])
            args_list = list(getattr(resp, "tools_call_args", []) or [])
            ids = list(getattr(resp, "tools_call_ids", []) or [])
            if text:
                self._dbg(f"  第{round_no + 1}轮 模型说：{text[:100]}")
            if not names:
                break
            ctxs.append(
                {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [
                        {
                            "id": ids[i] if i < len(ids) else f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(
                                    args_list[i] if i < len(args_list) else {},
                                    ensure_ascii=False,
                                ),
                            },
                        }
                        for i, name in enumerate(names)
                    ],
                }
            )
            for i, name in enumerate(names):
                args = args_list[i] if i < len(args_list) else {}
                cid = ids[i] if i < len(ids) else f"call_{i}"
                self._dbg(f"  第{round_no + 1}轮 调工具 {name} {str(args)[:120]}")
                try:
                    result = await run_tool(
                        name, args if isinstance(args, dict) else {}
                    )
                except Exception as exc:  # noqa: BLE001
                    result = f"出错：{type(exc).__name__}: {exc}"
                    self._dbg(f"    工具出错：{result}")
                ctxs.append(
                    {"role": "tool", "tool_call_id": cid, "content": str(result)}
                )
        return "，".join(notes) or "没动手"

    # ------------------------------------------------------------------ 往 QQ 说一声

    def _pick_platform_id(self) -> str:
        """找一个能主动发消息的平台实例。"""
        ctx = getattr(self.plugin, "context", None)
        if not ctx:
            return ""
        try:
            insts = ctx.platform_manager.get_insts()
        except Exception:  # noqa: BLE001
            return ""
        for inst in insts:
            try:
                meta = inst.meta()
                if getattr(meta, "support_proactive_message", False):
                    return str(meta.id)
            except Exception:  # noqa: BLE001
                continue
        return ""

    def _group_for_project(self, project_id: int) -> str:
        try:
            binds = self.plugin.store.all_binds()
        except Exception as exc:  # noqa: BLE001
            self._dbg(f"  查群绑定失败：{type(exc).__name__}: {exc}")
            return ""
        for gid, pids in (binds or {}).items():
            if int(project_id or 0) in [int(p) for p in pids]:
                return str(gid)
        return ""

    @staticmethod
    def _stage_error_hint(exc: Exception) -> str:
        text = f"{type(exc).__name__}: {exc}"
        if "403" in text or "权限" in text:
            return "没权限，这条大概是已审核或已锁定，得校对以上权限的人来改"
        if "401" in text:
            return "Token 失效了，得让管理员去面板重填"
        return text[:120]

    def _qq_inst(self, platform_id: str):
        try:
            for inst in self.plugin.context.platform_manager.get_insts():
                try:
                    if str(inst.meta().id) == str(platform_id):
                        return inst
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            return None
        return None

    def _qq_sendable(self, platform_id: str, session: str) -> tuple[bool, str]:
        """发之前先看看平台让不让发。

        QQ 官方机器人的群主动推送有硬限制：群里最近没说过话、平台没开主动推送权限时，
        `send_by_session` 会打印一行 warning 然后**静默 return**，不抛异常也不告诉你没发出去。
        所以得自己先探一眼缓存里的 msg_id，别把「没发」当「发好了」。
        """
        try:
            insts = self.plugin.context.platform_manager.get_insts()
        except Exception:  # noqa: BLE001
            return True, ""
        for inst in insts:
            try:
                if str(inst.meta().id) != str(platform_id):
                    continue
            except Exception:  # noqa: BLE001
                continue
            target = session.rsplit(":", 1)[-1]
            if "GroupMessage" not in session:
                return True, ""
            cache = getattr(inst, "_session_last_message_id", None)
            if isinstance(cache, dict) and target in cache:
                return True, ""
            # 官方适配器里 allow_group_proactive_send 还要求这个群被识别成 group 场景，
            # 而场景是它收到群消息时才记的。只有 flag 不够，flag 是构造时就 True 的。
            scene = getattr(inst, "_session_scene", None)
            if isinstance(scene, dict) and scene.get(target) == "group":
                return True, ""
            return (
                False,
                "这个群机器人还没收到过消息，平台不让主动推（群里随便说一句就能发）",
            )
        return True, ""

    def _log_group_sendable(self) -> None:
        """把每个绑定群的主动推送可用性打一行到日志，省得一个个试。"""
        try:
            platform_id = self.plugin.store.platform_id() or self._pick_platform_id()
            binds = self.plugin.store.all_binds() or {}
            if not platform_id or not binds:
                return
            ok, blocked = [], []
            for gid in binds:
                session = f"{platform_id}:GroupMessage:{gid}"
                sendable, _why = self._qq_sendable(platform_id, session)
                (ok if sendable else blocked).append(gid[-8:])
            self._dbg(
                f"群主动推送：共 {len(binds)} 个绑定群，可发 {len(ok)} 个"
                f"{('（' + ', '.join(ok) + '）') if ok else ''}，"
                f"暂时发不了 {len(blocked)} 个"
                f"{('（' + ', '.join(blocked) + '）') if blocked else ''}"
            )
        except Exception as exc:  # noqa: BLE001
            self._dbg(f" 群主动推送自检失败：{type(exc).__name__}: {exc}")

    async def _send_qq(self, notice: PendingNotice, text: str) -> bool:
        """按官方 session 往 QQ 发一句。群发不出去就退到私聊，还是不行就算了。"""
        try:
            platform_id = self.plugin.store.platform_id() or self._pick_platform_id()
            if not platform_id:
                self._dbg("  QQ 发不出去：没有支持主动消息的平台实例")
                return False
            target = str(self.plugin.config.get("notify_qq_target") or "").strip()
            sessions: list[str] = []
            if target:
                kind = "GroupMessage" if len(target) > 6 else "FriendMessage"
                sessions.append(f"{platform_id}:{kind}:{target}")
            gid = self._group_for_project(notice.project)
            if gid:
                sessions.append(f"{platform_id}:GroupMessage:{gid}")
            if not sessions:
                # 注意：通知里的 user_id 是 ParaTranz 的 uid，不是 QQ 号，
                # 拿它当 QQ 好友发是发不出去的（实测会报「用户/群已注销」）。
                self._dbg("  QQ 没地方发：项目没绑群，也没配 notify_qq_target")
                return False
            self._dbg(f"  QQ 候选会话：{sessions}")
            reasons = []
            for session in sessions:
                ok, why = self._qq_sendable(platform_id, session)
                if not ok:
                    # 兜底：绑表里的群本来就有群号，补记一次 group 场景再试。
                    # remember_session_scene 是适配器的公开方法，不是硬改内部状态。
                    inst = self._qq_inst(platform_id)
                    rec = getattr(inst, "remember_session_scene", None)
                    if callable(rec):
                        try:
                            rec(session.rsplit(":", 1)[-1], "group")
                            ok, why = self._qq_sendable(platform_id, session)
                            self._dbg(
                                f"  补记群场景后{'可以发' if ok else '还是不行'}：{session}"
                            )
                        except Exception as exc:  # noqa: BLE001
                            self._dbg(f"  补记群场景失败：{type(exc).__name__}: {exc}")
                if not ok:
                    reasons.append(why)
                    self._dbg(f"  QQ 不发 {session}：{why}")
                    continue
                try:
                    await self.plugin.context.send_message(
                        session, MessageChain([Plain(text)])
                    )
                    self._dbg(f"  QQ 已发 {session}：{text[:60]}")
                    return True
                except Exception as exc:  # noqa: BLE001
                    self._dbg(f"  QQ 发 {session} 失败：{type(exc).__name__}: {exc}")
                    reasons.append(f"{type(exc).__name__}: {exc}")
            self._dbg(f"  QQ 一条都没发出去：{reasons}")
            return False
        except Exception as exc:  # noqa: BLE001
            self._dbg(f"  QQ 发送失败：{type(exc).__name__}: {exc}")
            return False

    def drop_pending(self) -> None:
        """不播报了，攒着的东西直接倒掉，免得越堆越多。"""
        self._pending.clear()
        self._first_pending_at = 0.0

    @staticmethod
    def digest(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
