"""ParaTranz 翻译助手插件主文件。

【关于拆分：先记在这里，别忘】
这个文件太大了，命令、渲染编排、LLM 工具全堆在一起。为了安全起见现在不动，
等有把握时按下面的方式拆，一批搬完编译+冒烟一次，别一口气全搬：

    main.py         只留注册、配置读取、指令分发、生命周期
    modules/tools.py    全部 @filter.llm_tool 方法（约 900 行，整块搬，用 mixin）
    modules/commands.py 全部 _cmd_* 子命令（约 1100 行，按主题分几批搬）
    modules/notify.py   通知轮询（已拆出去，可以当范本）
    card.py / render.py / permission.py / store.py / paratranz_client.py  保持不动

拆的时候注意：方法里大量 self.xxx，直接搬会断，得用 mixin 类继承进主类，
或者抽成显式传参的函数。共享常量（STAGE_NAMES、ROLE_CN、BATCH_MAX 等）
要一起挪到 modules/common.py，让主文件反过来 import，避免循环依赖。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .card import (
    build_confirm_keyboard,
    build_menu_keyboard,
    build_progress_card,
    build_project_keyboard,
    build_records_keyboard,
    build_revert_keyboard,
    button,
    send_keyboard,
    try_send_card,
    try_send_keyboard_menu,
)
from .modules.notify import Notifier, PendingNotice
from .paratranz_client import ParaTranzClient, ParaTranzError
from .permission import (
    ROLE_ADMIN,
    ROLE_TRANSLATOR,
    ROLE_VIEWER,
    Permission,
)
from .render import (
    _fmt_time,
    render_diff_card,
    render_file_card,
    render_member_card,
    render_multi_project_card,
    render_project_card,
    render_records_card,
    render_strings_card,
    render_table_card,
    render_task_card,
)
from .store import StateStore

STAGE_NAMES = {
    0: "未翻译",
    1: "已翻译",
    2: "有疑问",
    3: "已检查",
    5: "已审核",
    9: "已锁定",
    -1: "已隐藏",
}


class AmbiguousProject(ParaTranzError):
    """本群绑定多个项目，需要用户点一个。"""

    def __init__(self, message: str, projects: list[tuple[int, str]]):
        super().__init__(message)
        self.projects = projects


STAGE_ALIASES = {
    "未翻译": 0,
    "已翻译": 1,
    "有疑问": 2,
    "已检查": 3,
    "已审核": 5,
    "锁定": 9,
    "已锁定": 9,
    "隐藏": -1,
}

CMD_ALIASES = {"pt", "paratranz", "汉化", "翻译项目"}

# ParaTranz 官方 logo。官方 markdown 只能引用公网 URL；
# 本地图片渲染则换成 base64 data URI，免得内网拉不到图。
ICON_URL = "https://paratranz.cn/assets/logo-small-wej5B4rx.png"
MEDIA_CACHE = "media_logo.json"
AVATAR_TTL = 7 * 86400
MEMBER_LIMIT_DEFAULT = 20
MEMBER_LIMIT_MAX = 100
STRINGS_LIMIT_DEFAULT = 20
STRINGS_LIMIT_MAX = 800
BATCH_MAX = 800
TEMP_IMAGE_TTL = 60
PENDING_TTL = 3 * 86400
PLUGIN_VERSION = "0.29.4"
CONFIRM_TTL_DEFAULT = 600
CACHE_TTL = 3 * 86400

# 中文指令别名，懒得记英文就用这些
CN_ACTIONS = {
    "项目": "project",
    "进度": "project",
    "文件": "files",
    "术语": "terms",
    "词条": "strings",
    "成员": "members",
    "任务": "tasks",
    "搜索": "search",
    "查项目": "search",
    "我": "me",
    "权限": "me",
    "绑定": "bind",
    "解绑": "unbind",
    "绑定列表": "binds",
    "状态": "stage",
    "标记": "stage",
    "群名": "groupname",
    "群名称": "groupname",
    "授权": "grant",
    "撤销": "revoke",
    "名单": "grants",
    "授权列表": "grants",
    "改": "set",
    "修改": "set",
    "设置": "set",
    "确认": "ok",
    "取消": "cancel",
    "撤回": "revert",
    "回滚": "revert",
    "记录": "records",
    "历史": "records",
    "帮助": "help",
    "菜单": "menu",
    "指令清单": "cmds",
}

ROLE_CN = {
    ROLE_ADMIN: "管理员",
    ROLE_TRANSLATOR: "可改词条",
    ROLE_VIEWER: "只读词条",
    None: "未授权",
}


def _bar(percent: float, width: int = 12) -> str:
    filled = int(round(percent / 100 * width))
    return "█" * filled + "░" * (width - filled)


def _n(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _pct(part: int, whole: int) -> float:
    return (part / whole * 100) if whole else 0.0


def _parse_args(event: AstrMessageEvent) -> list[str]:
    text = (event.message_str or "").strip()
    parts = text.split()
    # 指令按钮点下去会带一个 @bot，唤醒前缀也可能留下 @ 段，先剥掉
    while parts and parts[0].startswith("@"):
        parts = parts[1:]
    if parts and parts[0].lstrip("/!／").lower() in CMD_ALIASES:
        parts = parts[1:]
    return parts


def _fmt_summary(total: dict, files_count: int) -> str:
    n = total["total"] or 0
    if not n:
        return "这个项目还没有词条。"
    reviewed = total["reviewed"] + total["locked"]
    words = total.get("words") or 0
    pct = _pct(total["translated"], n)
    lines = []
    if words:
        lines.append(f"总字数 {words:,}　总条数 {n:,}")
    else:
        lines.append(f"总条数 {n:,}")
    lines.append(
        f"已翻译 {total['translated']:,}（{pct:.2f}%）{_bar(pct)}\n"
        f"已检查 {total['checked']:,}（{_pct(total['checked'], n):.2f}%）　"
        f"已审核 {reviewed:,}　有疑问 {total['disputed']:,}"
    )
    lines.append(f"文件数 {files_count}　进度条 █ 一格 10%")
    return "\n".join(lines)


def _fmt_progress(files: list[dict]) -> str:
    total = ParaTranzClient.summarize_files(files)
    n = total["total"] or 0
    if not n:
        return "这个项目还没有文件，或者文件全被隐藏了。"
    reviewed = total["reviewed"] + total["locked"]
    lines = [
        f"进度 {total['translated']}/{n}（{_pct(total['translated'], n):.1f}%） {_bar(_pct(total['translated'], n))}",
        f"已检查 {total['checked']}　已审核 {reviewed}　有疑问 {total['disputed']}　文件数 {len(files)}",
    ]
    return "\n".join(lines)


@register(
    "astrbot_plugin_paratranz",
    "DeepSeek",
    "ParaTranz 翻译项目查询插件",
    "0.29.4",
)
class ParaTranzPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.list_limit = int(self.config.get("list_limit", 10) or 10)
        self.member_limit = max(
            1,
            min(
                int(
                    self.config.get("member_limit", MEMBER_LIMIT_DEFAULT)
                    or MEMBER_LIMIT_DEFAULT
                ),
                MEMBER_LIMIT_MAX,
            ),
        )
        self.enable_write = bool(self.config.get("enable_write", False))
        self._error_seen: dict[str, float] = {}
        self._group_name_try: dict[str, float] = {}
        self._members_cache: dict[int, tuple[float, set[int]]] = {}
        # 自己的 ParaTranz uid：不让人填，从登录会话或站内通知里自己学
        self._self_uid: int = 0
        self.use_card = bool(self.config.get("use_card", True))
        self.menu_image = bool(self.config.get("menu_image", True))
        self.task_limit = max(1, min(int(self.config.get("task_limit", 10) or 10), 20))
        self.records_per_page = max(
            1, min(int(self.config.get("records_per_page", 10) or 10), 20)
        )
        self.diff_preview_max = max(
            0, min(int(self.config.get("diff_preview_max", 12) or 0), 30)
        )
        self.strings_limit = max(
            1,
            min(
                int(
                    self.config.get("strings_limit", STRINGS_LIMIT_DEFAULT)
                    or STRINGS_LIMIT_DEFAULT
                ),
                STRINGS_LIMIT_MAX,
            ),
        )
        self.progress_image = bool(self.config.get("progress_image", True))
        self.list_image = bool(self.config.get("list_image", True))
        self._last_sweep = 0.0
        self.menu_buttons = bool(self.config.get("menu_buttons", True))
        self._menu_cache: dict[str, str] = {}
        self._menu_cache_at: dict[str, float] = {}
        self.store = StateStore()
        self.permission = Permission(self.config, self.store)
        self.notify_platform = self._clean_platform(self.store.platform_id())
        self.notifier = Notifier(self)
        self.confirm_ttl = max(
            15,
            int(
                self.config.get("confirm_ttl", CONFIRM_TTL_DEFAULT)
                or CONFIRM_TTL_DEFAULT
            ),
        )

        token = (self.config.get("token") or "").strip() or os.environ.get(
            "PARATRANZ_TOKEN", ""
        ).strip()
        self.client = ParaTranzClient(
            base_url=self.config.get("base_url", "https://paratranz.cn"),
            token=token,
            timeout=float(self.config.get("timeout", 15) or 15),
            min_interval=float(self.config.get("min_interval", 0.2) or 0.0),
            max_concurrency=int(self.config.get("max_concurrency", 1) or 1),
            session_path=os.path.join(self.store.dir, "session.json"),
            write_with_session=bool(self.config.get("write_with_session", True)),
        )

    async def initialize(self):
        await self.notifier.start()
        if self.client.write_with_session:
            await self.client.ensure_session()
            if self.client.session_uid and not self._self_uid:
                self._self_uid = self.client.session_uid
        if self.client.has_token and not self._self_uid:
            # 顺手学一下自己的 uid：通知里的 to 就是它，不额外花什么
            try:
                data = await self.client.list_messages(1, 1)
                rows = data.get("results", []) if isinstance(data, dict) else []
                to = rows[0].get("to") if rows else None
                if isinstance(to, int) and to > 0:
                    self._self_uid = to
                    logger.info(f"[ParaTranz] 自己的用户ID：{to}")
            except Exception as exc:  # noqa: BLE001
                logger.info(f"[ParaTranz] 取自己的用户ID失败：{exc}")
        mode = "Token 已配置" if self.client.has_token else "公开只读模式（无 Token）"
        write_mode = "允许写" if self.enable_write else "禁止写"
        binds = len(self.store.all_binds())
        grants = len(self.store.all_grants())
        logger.info(
            f"[ParaTranz] v{PLUGIN_VERSION} 就绪：{mode} / {write_mode} / "
            f"已绑定 {binds} 个群 / 已授权 {grants} 人"
        )

    async def project_members(self, pid: int) -> set[int]:
        """项目成员 uid 集合，缓存 5 分钟，省得每条通知都拉一次。"""
        now = time.time()
        hit = self._members_cache.get(int(pid))
        if hit and now - hit[0] < 300:
            return hit[1]
        try:
            rows = await self.client.list_members(int(pid))
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[ParaTranz] 拉成员失败：{type(exc).__name__}: {exc}")
            return hit[1] if hit else set()
        uids = {int(r.get("uid")) for r in rows if str(r.get("uid") or "").isdigit()}
        self._members_cache[int(pid)] = (now, uids)
        return uids

    async def is_project_member(self, pid: int | None, uid: int | None) -> bool:
        if not pid or not uid:
            return False
        return int(uid) in await self.project_members(int(pid))

    def is_owner(self, user: dict | None) -> bool:
        """判断这个 ParaTranz 用户是不是主人。只认 uid 和用户名，昵称会变还会重。"""
        keys = str(self.config.get("owner_ids") or "")
        wanted = {
            k.strip().lower() for k in keys.replace("，", ",").split(",") if k.strip()
        }
        if not wanted or not isinstance(user, dict):
            return False
        candidates = {str(user.get("id") or ""), str(user.get("username") or "")}
        return any(c and c.lower() in wanted for c in candidates)

    @staticmethod
    def _clean_platform(value: str) -> str:
        """平台ID必须长得像 default_xxx / qq_official 这种，纯数字是被填错的 uid。"""
        text = str(value or "").strip()
        if not text or text.isdigit():
            return ""
        return text

    def pick_session(self, group_id: str | None = None) -> str | None:
        """拼一个可主动发送的会话串。优先指定群，否则用第一个绑定群。"""
        sessions = self.session_candidates(group_id)
        return sessions[0] if sessions else None

    def _platform_from_manager(self) -> list[str]:
        """问 AstrBot 要一份当前活着的平台实例 ID，别自己瞎猜。"""
        try:
            insts = self.context.platform_manager.get_insts()
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[ParaTranz] 取平台实例失败：{type(exc).__name__}: {exc}")
            return []
        out: list[str] = []
        for inst in insts or []:
            try:
                meta = inst.meta()
                pid = str(meta.id or "")
                if not bool(getattr(meta, "support_proactive_message", True)):
                    continue
            except Exception:  # noqa: BLE001
                continue
            if pid and pid not in ("webchat", "astrbot_webchat"):
                out.append(pid)
        return out

    def platform_candidates(self) -> list[str]:
        """可用的平台 ID：先用手上记住的，再补上 AstrBot 的实例列表。"""
        out: list[str] = []
        for pid in [self.notify_platform, *self._platform_from_manager()]:
            pid = self._clean_platform(str(pid or ""))
            if pid and pid not in out:
                out.append(pid)
        return out

    def session_candidates(self, group_id: str | None = None) -> list[str]:
        """拼一批可尝试的会话串，推送时按顺序试，成了就记住那个平台。"""
        binds = self.store.all_binds() or {}
        # 不知道具体群时只试第一个绑定群，别把通知撒到所有群里去
        groups = [group_id] if group_id else list(binds.keys())[:1]
        out: list[str] = []
        for plat in self.platform_candidates():
            for gid in groups:
                out.append(f"{plat}:GroupMessage:{gid}")
        return out

    async def report_error_once(self, signature: str, text: str) -> bool:
        """同样的错误 10 分钟内只播一次，避免刷屏。"""
        now = time.time()
        last = self._error_seen.get(signature, 0.0)
        if last and now - last < 600:
            return False
        self._error_seen[signature] = now
        session = self.pick_session()
        if not session:
            return False
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Plain

            await self.context.send_message(session, MessageChain([Plain(text)]))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[ParaTranz] 主动报错发送失败：{type(exc).__name__}: {exc}")
            return False

    async def terminate(self):
        await self.notifier.stop()
        await self.client.close()

    # ------------------------------------------------------------------ 指令入口

    @filter.command("pt")
    async def pt(self, event: AstrMessageEvent):
        # 指令归指令，别让 LLM 再插一嘴
        try:
            event.stop_event()
        except Exception:  # noqa: BLE001
            pass
        if not self.permission.group_allowed(event):
            yield event.plain_result("这个群不在白名单里，不接活。")
            return

        try:
            plat = self._clean_platform(str(event.unified_msg_origin).split(":", 1)[0])
            if plat and plat != self.notify_platform:
                self.notify_platform = plat
                await self.store.set_platform_id(plat)
        except Exception:  # noqa: BLE001
            pass

        await self._learn_group_name(event)

        args = _parse_args(event)
        if not args:
            # 没参数就当看菜单
            args = ["菜单"]

        action, rest = args[0].lower(), args[1:]
        action = CN_ACTIONS.get(args[0], action)
        handlers = {
            "menu": self._cmd_menu,
            "菜单": self._cmd_menu,
            "help": self._cmd_menu,
            "-h": self._cmd_menu,
            "cmd": self._cmd_cmds,
            "cmds": self._cmd_cmds,
            "指令": self._cmd_cmds,
            "project": self._cmd_project,
            "p": self._cmd_project,
            "files": self._cmd_files,
            "f": self._cmd_files,
            "terms": self._cmd_terms,
            "t": self._cmd_terms,
            "strings": self._cmd_strings,
            "s": self._cmd_strings,
            "tasks": self._cmd_tasks,
            "任务": self._cmd_tasks,
            "members": self._cmd_members,
            "m": self._cmd_members,
            "search": self._cmd_search,
            "debug": self._cmd_debug,
            "调试": self._cmd_debug,
            "me": self._cmd_me,
            "bind": self._cmd_bind,
            "unbind": self._cmd_unbind,
            "binds": self._cmd_binds,
            "groupname": self._cmd_groupname,
            "stage": self._cmd_stage,
            "grant": self._cmd_grant,
            "revoke": self._cmd_revoke,
            "grants": self._cmd_grants,
            "set": self._cmd_set,
            "ok": self._cmd_ok,
            "cancel": self._cmd_cancel,
            "revert": self._cmd_revert,
            "撤回": self._cmd_revert,
            "rollback": self._cmd_revert,
            "records": self._cmd_records,
            "记录": self._cmd_records,
            "历史": self._cmd_records,
        }
        handler = handlers.get(action)
        if handler is None:
            yield event.plain_result(
                f"没有这个子命令：{action}\n{self._help_text(event)}"
            )
            return

        try:
            kind, payload = await handler(event, rest)
        except AmbiguousProject as exc:
            hint = await self._send_project_picker(event, action, exc)
            if hint:
                yield event.plain_result(hint)
            return
        except ParaTranzError as exc:
            yield event.plain_result(f"查询失败：{exc.message}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("[ParaTranz] 未预期错误")
            yield event.plain_result(f"出错了：{exc}")
            return

        if kind == "card":
            card, fallback = payload
            if self.use_card and await try_send_card(event, card):
                return
            yield event.plain_result(fallback)
            return
        if kind == "sent":
            return
        if kind == "image":
            path, fallback = payload
            try:
                if path:
                    yield event.image_result(path)
                    return
            except Exception:  # noqa: BLE001
                logger.warning("[ParaTranz] 发送菜单图片失败，回退文本")
            yield event.plain_result(fallback)
            return
        yield event.plain_result(payload)

    # ------------------------------------------------------------------ 菜单

    async def _send_project_picker(
        self, event: AstrMessageEvent, action: str, exc: AmbiguousProject
    ) -> str:
        """本群绑了多个项目：优先发一排按钮，点哪个就带 @项目ID 重发同一条指令。

        返回需要以纯文本发出去的提示（按钮发成功就返回空串）。
        """
        await self._warm_project_names([pid for pid, _ in exc.projects])
        projects = [(pid, self.store.project_name(pid)) for pid, _ in exc.projects]
        kb = build_project_keyboard(action, projects)
        kb_ok, _kb_detail = await send_keyboard(event, f"**{exc.message}**", kb)
        if kb_ok:
            return ""
        lines = [exc.message]
        for pid, name in projects:
            lines.append(f"· #{pid} {name or ''}".rstrip())
        lines.append(f"用法：/pt {action} @项目ID")
        return "\n".join(lines)

    async def _multi_progress(self, event: AstrMessageEvent, pids: list[int]) -> tuple:
        """多项目总览图：每个绑定项目一张卡片。"""
        shown = pids[:8]
        items: list[dict] = []
        lines = []
        for pid in shown:
            try:
                project = await self.client.get_project(pid)
            except ParaTranzError as exc:
                lines.append(f"· #{pid} 查询失败：{exc.message}")
                continue
            name = str(project.get("name") or "")
            if name:
                await self.store.set_project_name(pid, name)
            stats = project.get("stats") or {}
            summary = {
                "total": _n(stats.get("total")),
                "translated": _n(stats.get("translated")),
                "checked": _n(stats.get("checked")),
                "reviewed": _n(stats.get("reviewed")),
                "locked": _n(stats.get("locked")),
                "disputed": _n(stats.get("disputed")),
                "words": _n(stats.get("words")),
            }
            items.append(
                {
                    "project": project,
                    "summary": summary,
                    "files_count": _n(project.get("filesCount")),
                }
            )
            lines.append(
                f"· #{pid} {name}　翻译 {summary['translated']:,}/{summary['total']:,}"
                f"（{_pct(summary['translated'], summary['total']):.1f}%）"
            )
        text = "本群绑定项目：\n" + "\n".join(lines)
        if not items:
            return "text", text
        if self.progress_image:
            out = os.path.join(self.store.dir, "cache", "multi_progress.png")
            try:
                path = await asyncio.to_thread(
                    render_multi_project_card,
                    items,
                    out,
                    self._wide_logo_path(),
                    f"共 {len(pids)} 个项目"
                    + ("" if len(pids) <= 8 else "，只显示前 8 个"),
                )
            except Exception:  # noqa: BLE001
                logger.warning("[ParaTranz] 多项目总览渲染失败", exc_info=True)
                path = None
            if path:
                return "image", (path, text)
        return "text", text

    def _project_label(self, project_id: int) -> str:
        """#20374 女皇之刃＆女皇之门（名字有缓存就用缓存）。"""
        name = self.store.project_name(project_id)
        return f"#{project_id} {name}" if name else f"#{project_id}"

    def _menu_markdown(
        self, event: AstrMessageEvent, logo_url: str | None = None
    ) -> str:
        write = "允许" if self.enable_write else "禁止"
        role = ROLE_CN.get(self.permission.role(event), "未授权")
        pids = self.store.binds_of(event.get_group_id())
        bound = (
            "　".join(self._project_label(pid) for pid in pids) if pids else "未绑定"
        )
        token = "有" if self.client.has_token else "无"
        # QQ 官方 markdown 的图片必须带尺寸：#宽px #高px，不写客户端渲染不出来
        if logo_url:
            head = [f"![ParaTranz #240px #54px]({logo_url})", ""]
        else:
            head = [f"![ParaTranz #96px #96px]({ICON_URL})", ""]
        lines = [
            *head,
            "### ParaTranz 翻译助手",
            "",
            f"- **项目**：{bound}",
            f"- **权限**：{role}",
            f"- **写操作**：{write}",
            f"- **Token**：{token}",
            "",
            "点下面按钮就行，带「…」的点完先补参数再发。",
            "查别的项目就在指令里加 **@项目ID**，完整清单发 **/pt 指令**。",
        ]
        return "\n".join(lines)

    @staticmethod
    def _valid_image(path: str | None) -> bool:
        if not path or not os.path.exists(path) or os.path.getsize(path) < 512:
            return False
        try:
            from PIL import Image

            with Image.open(path) as im:
                im.verify()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _media_url(self, event: AstrMessageEvent, path: str | None) -> str | None:
        """把本地图片传进 QQ 素材库，拿 qqbot-media:// 引用。按文件 md5 缓存。"""
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                raw = f.read()
            if not raw:
                return None
            md5 = hashlib.md5(raw).hexdigest()
            cache_path = os.path.join(self.store.dir, MEDIA_CACHE)
            cache: dict = {}
            if os.path.exists(cache_path):
                try:
                    with open(cache_path, encoding="utf-8") as f:
                        cache = json.load(f) or {}
                except Exception:  # noqa: BLE001
                    cache = {}
            hit = (cache.get("items") or {}).get(md5)
            if hit and hit.get("url"):
                return hit["url"]

            http = getattr(
                getattr(getattr(event, "bot", None), "api", None), "_http", None
            )
            if http is None:
                return None
            from botpy.http import Route

            prepared = await http.request(
                Route("POST", "/v2/media-images/prepare"),
                json={
                    "file_name": os.path.basename(path) or "image.png",
                    "file_size": len(raw),
                    "content_type": "image/png",
                    "md5": md5,
                    "sha1": hashlib.sha1(raw).hexdigest(),
                },
            )
            if not isinstance(prepared, dict) or not prepared.get("presigned_url"):
                return None
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.put(
                    prepared["presigned_url"],
                    data=raw,
                    headers={"Content-Type": "image/png"},
                ) as resp:
                    if resp.status not in (200, 201):
                        logger.warning(f"[ParaTranz] 素材上传失败：HTTP {resp.status}")
                        return None
                    etag = (resp.headers.get("ETag") or "").strip('"') or md5
            done = await http.request(
                Route("POST", "/v2/media-images/complete"),
                json={"upload_id": prepared["upload_id"], "md5": etag},
            )
            url = done.get("url") if isinstance(done, dict) else None
            if not url:
                return None
            items = cache.get("items") or {}
            items[md5] = {
                "url": url,
                "image_id": done.get("image_id"),
                "at": int(time.time()),
                "name": os.path.basename(path),
            }
            cache["items"] = items
            os.makedirs(self.store.dir, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
            logger.info(f"[ParaTranz] 素材已上传：{os.path.basename(path)}")
            return url
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[ParaTranz] 素材库上传不可用：{exc}")
            return None

    async def _official_logo_url(self, event: AstrMessageEvent) -> str | None:
        return await self._media_url(event, self._wide_logo_path())

    @staticmethod
    def _wide_logo_path() -> str | None:
        """插件自带的 logo（官方 SVG 转出来的横向 PNG），没有就退正方形那张。"""
        here = os.path.dirname(os.path.realpath(__file__))
        for name in ("logo_wide.png", "logo.png"):
            path = os.path.join(here, name)
            if os.path.exists(path) and os.path.getsize(path) > 200:
                return path
        return None

    def _decorate_logo(self, path: str, logo_path: str, crop_top: int = 0) -> str:
        """去掉本地渲染器自带的 AstrBot 抬头，再把 logo 贴到顶部。"""
        try:
            from PIL import Image

            with Image.open(path) as base_img, Image.open(logo_path) as logo_img:
                base = base_img.convert("RGBA")
                if crop_top and base.height > crop_top + 120:
                    base = base.crop((0, crop_top, base.width, base.height))
                logo = logo_img.convert("RGBA")
                if logo.width >= logo.height:
                    # 横向 wordmark：按宽度缩放，别拉成方块
                    width = min(280, base.width - 48)
                    height = max(1, round(logo.height * width / logo.width))
                else:
                    width = height = 72
                logo = logo.resize((width, height), Image.LANCZOS)
                pad_x, pad_y = 24, 16
                canvas = Image.new(
                    "RGBA",
                    (base.width, base.height + height + pad_y * 2),
                    (255, 255, 255, 255),
                )
                canvas.paste(logo, (pad_x, pad_y), logo)
                canvas.paste(base, (0, height + pad_y * 2))
            out = os.path.splitext(path)[0] + "_logo.png"
            canvas.convert("RGB").save(out, "PNG")
            return out
        except Exception:  # noqa: BLE001
            logger.warning("[ParaTranz] 贴 logo 失败，用原图", exc_info=True)
            return path

    def _cleanup_later(self, path: str) -> None:
        """临时图用完就删，延后一点等平台读完。"""

        async def _rm() -> None:
            await asyncio.sleep(TEMP_IMAGE_TTL)
            try:
                os.remove(path)
            except OSError:
                pass

        asyncio.create_task(_rm())

    async def _send_once_image(self, event: AstrMessageEvent, path: str) -> bool:
        """发一张临时图，发完 delay 秒自己删掉。"""
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Image as AstrImage

            await event.send(MessageChain([AstrImage.fromFileSystem(path)]))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ParaTranz] 临时图发送失败：{exc}")
            return False

        async def _cleanup() -> None:
            await asyncio.sleep(TEMP_IMAGE_TTL)
            try:
                os.remove(path)
            except OSError:
                pass

        asyncio.create_task(_cleanup())
        return True

    async def _render_diff(
        self, pid: int, enriched: list[dict], author: str = ""
    ) -> str | None:
        """把带 old/new 的条目渲染成红绿差分图（超长只画前 diff_preview_max 条）。"""
        if not self.diff_preview_max or not enriched:
            return None
        preview = enriched[: self.diff_preview_max]
        rows: list[dict] = []
        for it in preview:
            sid = int(it["string_id"])
            original = str(it.get("original") or "").strip()
            rows.append(
                {
                    "id": sid,
                    "original": original or "（原文取不到）",
                    "old": it.get("old") or "",
                    "new": it.get("new") or "",
                }
            )
        out = os.path.join(
            self.store.dir,
            "cache",
            f"diff_{pid}_{rows[0]['id']}_{int(time.time())}.png",
        )
        more = (
            f"…共 {len(enriched)} 条，图上只放了前 {len(preview)} 条"
            if len(enriched) > len(preview)
            else f"共 {len(enriched)} 条"
        )
        try:
            return await asyncio.to_thread(
                render_diff_card,
                "待确认的修改",
                f"#{pid}　确认后才写入 ParaTranz",
                rows,
                out,
                self._wide_logo_path(),
                more,
                author,
            )
        except Exception:  # noqa: BLE001
            logger.warning("[ParaTranz] 改动汇报图渲染失败", exc_info=True)
            return None

    async def _warm_project_names(self, pids: list[int]) -> None:
        """菜单要显示项目名，缓存里没有的顺手拉一次。"""
        for pid in pids:
            if self.store.project_name(pid):
                continue
            try:
                project = await self.client.get_project(pid)
            except Exception:  # noqa: BLE001
                continue
            name = str(project.get("name") or "")
            if name:
                await self.store.set_project_name(pid, name)

    async def _render_menu(self, event: AstrMessageEvent, md: str) -> str | None:
        if not self.menu_image:
            return None
        key = hashlib.md5(md.encode("utf-8")).hexdigest()
        cached = self._menu_cache.get(key)
        if (
            cached
            and self._valid_image(cached)
            and time.time() - self._menu_cache_at.get(key, 0) < 600
        ):
            return cached

        # 图片渲染里不塞外链图（本地渲染器只认 http，内网还可能拉不到），
        # 渲染完自己用 PIL 贴 logo
        md_img = "\n".join(
            line for line in md.splitlines() if not line.startswith("![ParaTranz")
        )
        logo_path = self._wide_logo_path()

        path = None
        local_used = False
        # 1) 优先本地离线渲染：远端 t2i 接口不稳定时不会拖慢
        try:
            renderer = getattr(self.context, "html_renderer", None)
            local = getattr(renderer, "local_strategy", None)
            if local is not None:
                path = await local.render(md_img)
                local_used = True
        except Exception:  # noqa: BLE001
            logger.warning("[ParaTranz] 本地渲染菜单失败", exc_info=True)
            path = None

        # 2) 本地不行再走 AstrBot 的 t2i 服务（可能是远端接口）
        if not self._valid_image(path):
            try:
                path = await self.text_to_image(
                    md_img, return_url=False, umo=event.unified_msg_origin
                )
            except Exception:  # noqa: BLE001
                logger.warning("[ParaTranz] t2i 服务渲染菜单失败", exc_info=True)
                path = None

        if not self._valid_image(path):
            logger.warning("[ParaTranz] 菜单图片不可用，回退为文本")
            return None

        if logo_path:
            # 本地渲染器顶上固定有个 91px 的 AstrBot 抬头，砍掉再贴自己的 logo
            path = self._decorate_logo(path, logo_path, 91 if local_used else 0)

        self._menu_cache[key] = path
        self._menu_cache_at[key] = time.time()
        return path

    async def _cmd_menu(self, event: AstrMessageEvent, rest: list[str]):
        await self._warm_project_names(self.store.binds_of(event.get_group_id()))
        md = self._menu_markdown(event, await self._official_logo_url(event))
        # 官方机器人：markdown + 真按钮，点了等于发指令
        if self.menu_buttons and await try_send_keyboard_menu(
            event, md, build_menu_keyboard()
        ):
            return "sent", ""
        path = await self._render_menu(event, md)
        if not path:
            return "text", self._plain_menu(md)
        return "image", (path, md)

    @staticmethod
    def _plain_menu(md: str) -> str:
        """退化成纯文本时把图片行和 markdown 标记去掉。"""
        lines: list[str] = []
        for raw in md.splitlines():
            if raw.startswith("![ParaTranz"):
                continue
            line = raw.lstrip("#").strip()
            if line.startswith(("- ", "* ")):
                line = "· " + line[2:].strip()
            line = line.replace("**", "").replace("`", "")
            lines.append(line)
        while lines and not lines[0]:
            lines.pop(0)
        return "\n".join(lines).strip()

    async def _cmd_debug(self, event: AstrMessageEvent, rest: list[str]):
        """自检：菜单图片到底卡在哪一步。仅管理员可用，避免把本地路径发到群里。"""
        reason = self.permission.deny_reason(event, need="admin")
        if reason:
            return "text", reason
        plat = getattr(getattr(event, "platform_meta", None), "name", "?")
        lines = [
            f"平台：{plat}",
            f"group_id={event.get_group_id()!r} sender={event.get_sender_id()!r} msg_id={getattr(getattr(event, 'message_obj', None), 'message_id', None)!r}",
            f"menu_image={self.menu_image} menu_buttons={self.menu_buttons}",
            f"local_strategy={getattr(getattr(self.context, 'html_renderer', None), 'local_strategy', None) is not None}",
        ]
        md = self._menu_markdown(event, await self._official_logo_url(event))
        try:
            local = getattr(
                getattr(self.context, "html_renderer", None), "local_strategy", None
            )
            p = await local.render(md) if local is not None else None
            ok = self._valid_image(p)
            size = os.path.getsize(p) if ok else 0
            lines.append(f"本地渲染：{'OK' if ok else '失败'} {size}B {p}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"本地渲染异常：{type(exc).__name__}: {exc}")
        try:
            p2 = await asyncio.wait_for(
                self.text_to_image(md, return_url=False, umo=event.unified_msg_origin),
                timeout=20,
            )
            ok2 = self._valid_image(p2)
            lines.append(f"t2i 服务：{'OK' if ok2 else '返回假图'} {p2}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"t2i 异常：{type(exc).__name__}: {exc}")
        if rest and rest[0] in ("按钮", "button", "kb"):
            ok, detail = await send_keyboard(
                event,
                "**按钮自检**\n点下面这颗看看回不回来",
                {
                    "rows": [
                        {"buttons": [button("回菜单", "/pt 菜单", btn_id="pt_probe")]}
                    ]
                },
            )
            lines.append(
                f"按钮探测：{'成功' if ok else '失败'}{'' if ok else ' -> ' + detail}"
            )
        return "text", "\n".join(lines)

    async def _cmd_cmds(self, event: AstrMessageEvent, rest: list[str]):
        """完整指令清单，纯文本，给不想看按钮的人。"""
        return "text", self._help_text(event)

    def _help_text(self, event: AstrMessageEvent | None = None) -> str:
        mode = (
            "可读词条正文"
            if self.client.has_token
            else "仅公开数据（无 Token，词条会 401）"
        )
        write = "允许" if self.enable_write else "禁止"
        role = ROLE_CN.get(self.permission.role(event), "未授权") if event else "未知"
        bound = ""
        if event is not None:
            pid = self.store.bind_of(event.get_group_id())
            if pid:
                bound = f"· 本群绑定 #{pid}，命令里省略项目ID就用它"
        return "\n".join(
            [
                "ParaTranz 查询指令",
                "",
                "【查询】",
                "· /pt 项目 [@项目ID]　项目概览 + 进度图",
                "· /pt 文件 [数量]　文件进度排行",
                "· /pt 术语 [关键词]　术语表",
                "· /pt 词条 [文件ID] [状态] [数量]　词条列表（状态可写名字，默认 20 条）",
                "· /pt 成员 [数量]　贡献排行榜（默认 20 人，最多 100）",
                "· /pt 任务 [数量]　任务进度（默认 10 个）",
                "· /pt 搜索 <关键词>　在本群绑定的项目里搜",
                "· /pt me　自己的权限和 Token 状态",
                "",
                "【管理｜仅管理员】",
                "· /pt 绑定 <项目ID> [群号]　给群加一个项目（可绑多个）",
                "· /pt 解绑 [项目ID] [群号]　不给项目ID就清空该群",
                "· /pt 绑定列表　查看所有绑定",
                "· /pt 授权 <@某人 或 QQ号/openid> [viewer|translator]",
                "· /pt 撤销 <@某人 或 QQ号/openid>",
                "· /pt 授权列表",
                "· /pt 群名 <名字>　给本群起个名，绑定列表里显示",
                "",
                "【提交与确认】",
                "· /pt 修改 <词条ID> <译文>　发起一条修改（谁都能发起）",
                "· /pt 确认 <确认码>　有写权限的人点/发这个才真写入",
                "· /pt cancel　放弃自己发起的待确认",
                "· /pt 记录 [页码|哈希ID]　提交记录（每页 10 条，点哈希看详情图）",
                "· /pt 状态 <词条ID> <状态>　标有疑问/已检查，只改状态，不碰原文和译文",
                "· /pt 撤回 <哈希ID>　把已写入的改动回滚（管理员）",
                "",
                "【说明】",
                "· 只能查本群绑定的项目，绑了多个就用 @项目ID 指定，比如 /pt 文件 @12352",
                "· 管理员不受限制，可以用 @项目ID 查任意项目",
                "· 英文子命令也能用，中文只是别名",
                "",
                f"当前：{mode}　写操作 {write}　你的权限 {role}",
                *([bound] if bound else []),
            ]
        )

    # ------------------------------------------------------------------ 项目解析

    def _pid_or_bound(
        self, event: AstrMessageEvent, rest: list[str]
    ) -> tuple[int, list[str]]:
        """解析项目 ID，只允许查询绑定在本群的项目。

        规则：
          1. 本群绑了多个项目时，必须写成 @12352 指定；
          2. 本群只绑一个 -> 直接用它，剩下的参数按各命令自己的含义解析
             （所以 /pt files 5 里的 5 是条数，不会被当项目）；
          3. 本群没绑定 -> 直接拒绝，让管理员先绑。
        """
        pids = self.store.binds_of(event.get_group_id())
        admin = self.permission.is_admin(event)
        has_at = bool(rest) and rest[0].startswith("@") and rest[0][1:].isdigit()
        if has_at:
            pid = int(rest[0][1:])
            if pid not in pids and not admin:
                raise ParaTranzError(
                    f"#{pid} 没绑到本群，查不了。本群绑的是："
                    + ("、".join(self._project_label(p) for p in pids) or "（空）")
                )
            return pid, rest[1:]
        if not pids:
            if admin:
                raise ParaTranzError(
                    "本群还没绑定项目。管理员可以临时用 @项目ID 跨项目查，比如 /pt 项目 @20374"
                )
            raise ParaTranzError(
                "本群还没绑定项目，管理员先发 /pt 绑定 <项目ID>，绑完才能查"
            )
        if len(pids) == 1:
            return pids[0], rest
        raise AmbiguousProject(
            "本群绑了多个项目，点一个继续（也可以直接发 @项目ID）：",
            [(p, self.store.project_name(p)) for p in pids],
        )

    async def _resolve_project(self, key: str) -> dict:
        if key.isdigit():
            return await self.client.get_project(int(key))
        data = await self.client.list_projects(page=1, page_size=50)
        kw = key.lower()
        for p in data.get("results", []):
            if kw in (p.get("name") or "").lower():
                return p
        raise ParaTranzError("第一页 50 个项目里没找到，直接给项目 ID 更稳")

    # ------------------------------------------------------------------ 只读

    # ---------------------------------------------------------------- 列表图缓存

    def _sweep_cache(self) -> None:
        try:
            purged = self.store.purge_submissions(PENDING_TTL)
            if purged:
                logger.info(f"[ParaTranz] 清掉 {purged} 条过期待确认提交")
        except Exception:  # noqa: BLE001
            pass
        """清掉过期缓存：列表图 3 天、头像 7 天。一小时最多扫一次。"""
        now = time.time()
        if now - self._last_sweep < 3600:
            return
        self._last_sweep = now
        root = os.path.join(self.store.dir, "cache")
        if not os.path.isdir(root):
            return
        removed = 0
        for dirpath, _dirnames, filenames in os.walk(root):
            ttl = AVATAR_TTL if os.path.basename(dirpath) == "avatars" else CACHE_TTL
            for fn in filenames:
                path = os.path.join(dirpath, fn)
                try:
                    if now - os.path.getmtime(path) > ttl:
                        os.remove(path)
                        removed += 1
                except OSError:
                    continue
        media = os.path.join(self.store.dir, MEDIA_CACHE)
        try:
            if os.path.exists(media):
                with open(media, encoding="utf-8") as f:
                    data = json.load(f) or {}
                items = data.get("items") or {}
                alive = {
                    k: v
                    for k, v in items.items()
                    if now - float(v.get("at") or 0) < 30 * 86400
                }
                if len(alive) != len(items):
                    data["items"] = alive
                    with open(media, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
        if removed:
            logger.info(f"[ParaTranz] 清掉 {removed} 个过期缓存文件")

    async def _render_list(self, name: str, *args, **kwargs) -> str | None:
        """在 cache 里生成一张图，失败返回 None。"""
        self._sweep_cache()
        out = os.path.join(self.store.dir, "cache", f"{name}.png")
        try:
            return await asyncio.to_thread(*args, out, **kwargs)
        except Exception:  # noqa: BLE001
            logger.warning("[ParaTranz] 列表图渲染失败，退文本", exc_info=True)
            return None

    async def _fetch_avatars(self, members: list[dict]) -> dict:
        """按 uid 下载头像到 cache/avatars，7 天内复用。"""
        items: list[tuple[object, str]] = []
        for m in members:
            user = m.get("user") or {}
            url = str(user.get("avatar") or "")
            uid = user.get("id") or m.get("uid")
            if not url or not uid:
                continue
            if url.startswith("/"):
                url = "https://paratranz.cn" + url
            items.append((uid, url))
        if not items:
            return {}
        import aiohttp

        dirpath = os.path.join(self.store.dir, "cache", "avatars")
        os.makedirs(dirpath, exist_ok=True)
        found: dict = {}
        timeout = aiohttp.ClientTimeout(total=8)
        sem = asyncio.Semaphore(6)
        headers = {"User-Agent": "ParaTranz-Client/0.1"}

        async with aiohttp.ClientSession(timeout=timeout) as sess:

            async def one(uid, url: str) -> None:
                key = hashlib.md5(url.encode("utf-8")).hexdigest()
                path = os.path.join(dirpath, key + ".img")
                try:
                    if (
                        os.path.exists(path)
                        and time.time() - os.path.getmtime(path) < AVATAR_TTL
                    ):
                        found[uid] = path
                        return
                except OSError:
                    pass
                async with sem:
                    try:
                        async with sess.get(url, headers=headers) as resp:
                            if resp.status != 200:
                                return
                            data = await resp.read()
                        if len(data) < 200:
                            return
                        tmp = path + ".tmp"
                        with open(tmp, "wb") as f:
                            f.write(data)
                        os.replace(tmp, path)
                        found[uid] = path
                    except Exception:  # noqa: BLE001
                        return

            await asyncio.gather(*(one(uid, url) for uid, url in items))
        return found

    # ---------------------------------------------------------------- 只读

    async def _cmd_project(self, event: AstrMessageEvent, rest: list[str]):
        pids = self.store.binds_of(event.get_group_id())
        if len(pids) > 1 and not (rest and rest[0].startswith("@")):
            return await self._multi_progress(event, pids)
        pid, _ = self._pid_or_bound(event, rest)
        project = await self.client.get_project(pid)
        name = str(project.get("name") or "")
        if name:
            await self.store.set_project_name(pid, name)
        stats = project.get("stats") or {}
        files: list[dict] = []
        if stats.get("total"):
            summary = {
                "total": int(stats.get("total") or 0),
                "translated": int(stats.get("translated") or 0),
                "checked": int(stats.get("checked") or 0),
                "reviewed": int(stats.get("reviewed") or 0),
                "locked": int(stats.get("locked") or 0),
                "disputed": int(stats.get("disputed") or 0),
                "words": int(stats.get("words") or 0),
            }
            files_count = int(project.get("filesCount") or 0)
        else:
            files = await self.client.list_files(pid)
            summary = ParaTranzClient.summarize_files(files)
            files_count = len(files)
        percent = _pct(summary["translated"], summary["total"])

        text_parts = [
            f"#{pid} {project.get('name')}",
            "",
            _fmt_summary(summary, files_count),
        ]
        if not self.client.has_token:
            text_parts.append("（未配置 Token，词条正文查不了）")
        fallback = "\n".join(text_parts)

        if self.progress_image:
            path = await self._render_progress(project, summary, files_count)
            if path:
                return "image", (path, fallback)
        if not self.use_card:
            return "text", fallback
        card = build_progress_card(project, summary, files_count, percent)
        return "card", (card, fallback)

    async def _render_progress(
        self, project: dict, summary: dict, files_count: int
    ) -> str | None:
        out = os.path.join(self.store.dir, "cache", f"progress_{project.get('id')}.png")
        subtitle = ""
        modified = (project.get("stats") or {}).get("modifiedAt") or ""
        if len(modified) >= 10:
            subtitle = f"更新 {modified[5:7]}-{modified[8:10]}"
        try:
            return await asyncio.to_thread(
                render_project_card,
                project,
                summary,
                files_count,
                out,
                self._wide_logo_path(),
                subtitle,
            )
        except Exception:  # noqa: BLE001
            logger.warning("[ParaTranz] 进度图渲染失败，退文本", exc_info=True)
            return None

    async def _cmd_files(self, event: AstrMessageEvent, rest: list[str]):
        pid, rest = self._pid_or_bound(event, rest)
        limit = self.list_limit
        if rest and rest[0].isdigit():
            limit = min(int(rest[0]), 30)
        files = await self.client.list_files(pid)
        if not files:
            return "text", "这个项目没有文件。"
        files.sort(key=lambda f: int(f.get("total") or 0), reverse=True)
        lines = [_fmt_progress(files), ""]
        for f in files[:limit]:
            total = int(f.get("total") or 0)
            done = int(f.get("translated") or 0)
            lines.append(
                f"· {f.get('name')}　{done}/{total}（{_pct(done, total):.0f}%）"
            )
        if len(files) > limit:
            lines.append(f"…共 {len(files)} 个文件")
        fallback = "\n".join(lines)

        if self.list_image:
            project = await self.client.get_project(pid)
            name = str(project.get("name") or "")
            if name:
                await self.store.set_project_name(pid, name)
            summary = ParaTranzClient.summarize_files(files)
            path = await self._render_list(
                f"files_{pid}",
                render_file_card,
                project,
                summary,
                files,
                logo_path=self._wide_logo_path(),
                limit=limit,
            )
            if path:
                return "image", (path, fallback)
        return "text", fallback

    async def _cmd_terms(self, event: AstrMessageEvent, rest: list[str]):
        pid, rest = self._pid_or_bound(event, rest)
        kw = " ".join(rest).lower()
        data = await self.client.list_terms(pid, page=1, page_size=200)
        terms = data.get("results", [])
        if kw:
            terms = [
                t
                for t in terms
                if kw in (t.get("term") or "").lower()
                or kw in (t.get("translation") or "").lower()
            ]
        if not terms:
            return "text", "术语表里没找到匹配项。"
        lines = [f"术语命中 {len(terms)} 条（取前 {self.list_limit}）"]
        for t in terms[: self.list_limit]:
            note = (t.get("note") or "").strip()
            line = f"· {t.get('term')} → {t.get('translation')}"
            if note:
                line += f"（{note[:40]}）"
            lines.append(line)
        fallback = "\n".join(lines)

        if self.list_image:
            rows = []
            for t in terms[: self.list_limit]:
                note = (t.get("note") or "").strip()
                right = str(t.get("translation") or "")
                if note:
                    right += f"（{note}）"
                rows.append((str(t.get("term") or ""), right))
            path = await self._render_list(
                f"terms_{pid}",
                render_table_card,
                "术语表",
                f"#{pid}　命中 {len(terms)} 条，显示前 {len(rows)} 条",
                rows,
                logo_path=self._wide_logo_path(),
                columns=("原文", "译文"),
                footer="",
            )
            if path:
                return "image", (path, fallback)
        return "text", fallback

    async def _cmd_me(self, event: AstrMessageEvent, rest: list[str]):
        role = ROLE_CN.get(self.permission.role(event), "未授权")
        pids = self.store.binds_of(event.get_group_id())
        if pids:
            await self._warm_project_names(pids)
        lines = [f"· 你的权限：{role}"]
        try:
            lines.append(f"· 你的ID：{event.get_sender_id()}")
        except Exception:  # noqa: BLE001
            pass
        if pids:
            lines.append(
                "· 本群绑定：" + "、".join(self._project_label(p) for p in pids)
            )
            if len(pids) > 1:
                lines.append("· 多个项目，查询时用 @项目ID 指定")
        else:
            lines.append("· 本群绑定：未绑定（管理员先 /pt 绑定 <项目ID>）")
        if self.client.write_with_session:
            if self.client.session_valid:
                lines.append(
                    f"· 写入会话：有效（{self.client.session_name or '?'}"
                    f" / uid {self.client.session_uid or '?'}）"
                )
            else:
                lines.append("· 写入会话：无效，写入会退回匿名导入")
        else:
            lines.append("· 写入会话：已关闭（写入会记为匿名导入）")
        if not self.client.has_token:
            lines.append("· Token：未配置，只能查公开数据")
        else:
            try:
                await self.client.list_projects(page=1, page_size=1)
                lines.append("· Token：有效")
            except ParaTranzError as exc:
                lines.append(f"· Token：有问题（{exc.message}）")
        return "text", "\n".join(lines)

    async def _cmd_search(self, event: AstrMessageEvent, rest: list[str]):
        if not rest:
            return "text", "给个关键词。"
        kw = " ".join(rest).lower()
        pids = self.store.binds_of(event.get_group_id())
        admin = self.permission.is_admin(event)
        if not pids and not admin:
            return (
                "text",
                "本群还没绑定项目。搜索只在绑定的项目里找，管理员先 /pt 绑定 <项目ID>",
            )

        hits: list[dict] = []
        if admin and not pids:
            data = await self.client.list_projects(page=1, page_size=50)
            for p in data.get("results", []):
                if kw in str(p.get("name") or "").lower() or kw in str(p.get("id")):
                    hits.append({"id": int(p["id"]), "name": str(p.get("name") or "")})
        else:
            await self._warm_project_names(pids)
            for pid in pids:
                name = self.store.project_name(pid)
                if kw in name.lower() or kw in str(pid):
                    hits.append({"id": pid, "name": name or f"#{pid}"})
        if not hits:
            where = "全站" if (admin and not pids) else "本群绑定的项目里"
            bound = "、".join(self._project_label(p) for p in pids)
            return "text", f"{where}没搜到。当前绑定：{bound or '（空）'}"

        fallback = "\n".join(
            f"· {self._project_label(int(h['id']))}" for h in hits[: self.list_limit]
        )
        if self.list_image:
            rows = []
            for h in hits[: self.list_limit]:
                desc = ""
                try:
                    project = await self.client.get_project(int(h["id"]))
                    desc = " ".join((project.get("desc") or "").split())
                except Exception:  # noqa: BLE001
                    desc = ""
                name = h.get("name") or self.store.project_name(int(h["id"]))
                rows.append((f"#{h['id']} {name}", desc or "—"))
            path = await self._render_list(
                "search",
                render_table_card,
                "搜索项目",
                f"关键词「{kw}」　只在绑定项目里找，命中 {len(hits)} 个",
                rows,
                logo_path=self._wide_logo_path(),
                columns=("项目", "简介"),
                footer="",
            )
            if path:
                return "image", (path, fallback)
        return "text", fallback

    async def _cmd_members(self, event: AstrMessageEvent, rest: list[str]):
        pid, rest = self._pid_or_bound(event, rest)
        limit = self.member_limit
        if rest and rest[0].isdigit():
            limit = max(1, min(int(rest[0]), MEMBER_LIMIT_MAX))
        project = await self.client.get_project(pid)
        members = await self.client.list_members(pid)
        if not members:
            return "text", "没有成员数据。"
        members.sort(
            key=lambda m: (
                float(m.get("totalPoints") or 0),
                int(m.get("translated") or 0),
            ),
            reverse=True,
        )
        shown = members[:limit]
        lines = [f"成员 {len(members)} 人（按 PP 排序）"]
        for idx, m in enumerate(shown, start=1):
            user = m.get("user") or {}
            name = user.get("nickname") or user.get("username") or m.get("uid")
            lines.append(
                f"{idx}. {name}　PP {float(m.get('totalPoints') or 0):.2f}　"
                f"译 {m.get('translated', 0)}　编 {m.get('edited', 0)}　审 {m.get('reviewed', 0)}"
            )
        if len(members) > len(shown):
            lines.append(
                f"…共 {len(members)} 人，只列前 {len(shown)} 名（/pt 成员 <数量> 可调）"
            )
        fallback = "\n".join(lines)

        if self.list_image:
            avatars = await self._fetch_avatars(shown)
            path = await self._render_list(
                f"members_{pid}_{len(shown)}",
                render_member_card,
                project,
                shown,
                logo_path=self._wide_logo_path(),
                avatars=avatars,
                limit=len(shown),
                total_members=len(members),
            )
            if path:
                return "image", (path, fallback)
        return "text", fallback

    async def _cmd_strings(self, event: AstrMessageEvent, rest: list[str]):
        if not self.client.has_token:
            return "text", "词条正文需要 Token，当前是公开只读模式。"
        reason = self.permission.deny_reason(event, need="read")
        if reason:
            return "text", reason

        pid, rest = self._pid_or_bound(event, rest)
        # 位置参数：文件ID 状态 数量，状态可以写名字（已翻译/有疑问/...）也可以写数字
        stage: int | None = None
        file_id = None
        limit = self.strings_limit
        after_named_stage = False
        for tok in rest:
            low = tok.strip().lower()
            if low in ("全部", "all", "*"):
                continue
            if low in STAGE_ALIASES:
                stage = STAGE_ALIASES[low]
                after_named_stage = True
                continue
            if not tok.lstrip("-").isdigit():
                continue
            val = int(tok)
            if file_id is None and not after_named_stage:
                file_id = val
            elif stage is None and -1 <= val <= 9:
                stage = val
            else:
                limit = max(1, min(val, STRINGS_LIMIT_MAX))
        data = await self.client.list_strings(
            pid, file_id=file_id, stage=stage, page=1, page_size=limit
        )
        rows = data.get("results", []) if isinstance(data, dict) else data
        if not rows:
            return "text", "没有匹配的词条。"
        lines = []
        for s in rows:
            stage_name = STAGE_NAMES.get(s.get("stage"), str(s.get("stage")))
            lines.append(f"[{s.get('id')}] {stage_name}")
            lines.append(f"原：{(s.get('original') or '').strip()[:120]}")
            lines.append(f"译：{(s.get('translation') or '').strip()[:120]}")
        fallback = "\n".join(lines)

        if self.list_image:
            items = [self._string_item(s) for s in rows]
            stage_txt = (
                STAGE_NAMES.get(stage, stage) if stage is not None else "全部状态"
            )
            path = await self._render_list(
                f"strings_{pid}_{file_id or 0}_{stage if stage is not None else -99}_{limit}",
                render_strings_card,
                "词条",
                f"#{pid}　文件 {file_id or '全部'}　{stage_txt}　共 {len(rows)} 条",
                items,
                logo_path=self._wide_logo_path(),
                footer="",
            )
            if path:
                return "image", (path, fallback)
        return "text", fallback

    # ------------------------------------------------------------------ 管理

    async def _cmd_bind(self, event: AstrMessageEvent, rest: list[str]):
        reason = self.permission.deny_reason(event, need="admin")
        if reason:
            return "text", reason

        group_id = str(event.get_group_id() or "").strip()
        pid_arg = None
        if rest and rest[0].isdigit():
            pid_arg = int(rest[0])
            if len(rest) > 1 and rest[1].isdigit():
                group_id = rest[1]

        if pid_arg is None:
            return "text", "用法：/pt 绑定 <项目ID> [群号]（一个群可以绑多个项目）"
        if not group_id:
            return "text", "私聊里要显式给群号：/pt 绑定 <项目ID> <群号>"

        project = await self.client.get_project(pid_arg)
        name = str(project.get("name") or "")
        added = await self.store.bind(group_id, pid_arg)
        if name:
            await self.store.set_project_name(pid_arg, name)
        label = f"#{pid_arg} {name}" if name else f"#{pid_arg}"
        pids = self.store.binds_of(group_id)
        head = (
            f"已把 {label} 绑到群 {group_id}"
            if added
            else f"{label} 本来就在群 {group_id} 的绑定里"
        )
        joined = "、".join(self._project_label(pid) for pid in pids)
        tail = f"本群现有 {len(pids)} 个项目：{joined}"
        hint = (
            "多个项目时指令里要写 @项目ID 指定"
            if len(pids) > 1
            else "之后直接用 /pt 项目 就行"
        )
        return "text", f"{head}\n· {tail}\n· {hint}"

    async def _cmd_unbind(self, event: AstrMessageEvent, rest: list[str]):
        reason = self.permission.deny_reason(event, need="admin")
        if reason:
            return "text", reason
        group_id = str(event.get_group_id() or "").strip()
        pid = None
        args = list(rest)
        if args and args[0].isdigit():
            pid = int(args[0])
            args = args[1:]
        if args and args[0].isdigit():
            group_id = args[0]
        if not group_id:
            return "text", "用法：/pt 解绑 [项目ID] [群号]　不给项目ID就清空这个群"

        removed = await self.store.unbind(group_id, pid)
        if not removed:
            what = f"#{pid}" if pid else "绑定"
            return "text", f"群 {group_id} 本来就没有 {what} 这条绑定。"
        left = self.store.binds_of(group_id)
        if pid:
            msg = f"已把 #{pid} 从群 {group_id} 解绑。"
        else:
            msg = f"已清空群 {group_id} 的所有项目绑定（{removed} 个）。"
        if left:
            msg += "\n· 还剩：" + "、".join(self._project_label(p) for p in left)
        else:
            msg += "\n· 这个群现在没绑项目了，查询会被拒绝"
        return "text", msg

    async def _fetch_group_name(self, event: AstrMessageEvent, gid: str) -> str:
        """先试事件自带的群对象，再退回适配器的 botpy 客户端问群信息接口。"""
        try:
            group = await asyncio.wait_for(event.get_group(gid), timeout=6)
            name = str(getattr(group, "group_name", "") or "").strip()
            if name:
                return name
        except Exception as exc:  # noqa: BLE001
            logger.info(
                f"[ParaTranz] event.get_group 没拿到群名：{type(exc).__name__}: {exc}"
            )

        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            plat_id = umo.split(":", 1)[0]
            inst = self.context.get_platform_inst(plat_id) if plat_id else None
            bot = getattr(inst, "client", None) if inst else None
            api = getattr(bot, "api", None)
            if api is None or not hasattr(api, "_http"):
                return ""
            from botpy.http import Route

            route = Route("GET", "/v2/groups/{group_openid}/info", group_openid=gid)
            payload = await api._http.request(route)
            if isinstance(payload, dict):
                return str(payload.get("group_name") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[ParaTranz] 群信息接口失败：{type(exc).__name__}: {exc}")
        return ""

    async def _learn_group_name(self, event: AstrMessageEvent) -> None:
        """群里说过话就顺手把群名补上，同一个群 10 分钟最多问一次。"""
        gid = str(event.get_group_id() or "")
        if not gid or self.store.group_name(gid):
            return
        now = time.time()
        if now - self._group_name_try.get(gid, 0.0) < 600:
            return
        self._group_name_try[gid] = now
        name = await self._fetch_group_name(event, gid)
        if name:
            await self.store.set_group_name(gid, name)
            logger.info(f"[ParaTranz] 记下群名：{gid[-6:]} = {name}")

    async def _cmd_groupname(self, event: AstrMessageEvent, rest: list[str]):
        gid = str(event.get_group_id() or "")
        if not gid:
            return "text", "私聊里认不了群，去群里用。"
        want = " ".join(rest).strip()
        if want:
            reason = self.permission.deny_reason(event, need="admin")
            if reason:
                return "text", reason
            await self.store.set_group_name(gid, want)
            return "text", f"这个群以后就叫「{want}」了。"
        cur = self.store.group_name(gid)
        if cur:
            return "text", f"这个群：{cur}（{gid}）"
        return "text", f"这个群还没名字，群ID {gid}。用 /pt 群名 <名字> 起一个。"

    async def _cmd_binds(self, event: AstrMessageEvent, rest: list[str]):
        binds = self.store.all_binds()
        binds = {gid: pids for gid, pids in binds.items() if pids}
        if not binds:
            return "text", "还没有任何绑定。用 /pt 绑定 <项目ID> 绑一个。"
        lines = [f"共 {len(binds)} 个群、{sum(len(p) for p in binds.values())} 条绑定"]
        for gid, pids in sorted(binds.items()):
            gname = self.store.group_name(gid)
            head = f"· {gname}（…{gid[-6:]}）" if gname else f"· 群 {gid}"
            lines.append(
                head + " → " + "、".join(self._project_label(pid) for pid in pids)
            )
        return "text", "\n".join(lines)

    _AT_MARKUP = r'<qqbot-at-user\s+id="([^"]+)"\s*/>|<@!?([A-Za-z0-9_\-]{6,})>'

    @classmethod
    def _at_targets(cls, event: AstrMessageEvent) -> list[str]:
        """把消息里的 @ 取成 id 列表。

        官方机器人适配器不会把"@别人"转成带 openid 的 At 组件：正文里的标记会被
        抹掉，At 组件里只塞一个 qq_official 占位，所以三条路都要扫一遍。
        """
        out: list[str] = []

        def _add(value) -> None:
            text = str(value or "").strip()
            if not text:
                return
            if text.lower() in (
                "all",
                "-1",
                "everyone",
                "qq_official",
                "unknown_selfid",
            ):
                return
            if text not in out:
                out.append(text)

        try:
            chain = event.get_messages()
        except Exception:  # noqa: BLE001
            chain = []
        for seg in chain or []:
            name = getattr(seg, "type", "") or type(seg).__name__
            if str(name).lower() != "at":
                continue
            for attr in ("qq", "user_id", "uid", "target"):
                val = getattr(seg, attr, None)
                if val is None:
                    continue
                _add(val)
                break

        try:
            raw_text = str(event.message_str or "")
        except Exception:  # noqa: BLE001
            raw_text = ""
        if raw_text:
            for m in re.finditer(cls._AT_MARKUP, raw_text):
                _add(m.group(1) or m.group(2))

        # 官方机器人：@ 的信息只在原始消息对象里，正文已经被清干净了
        try:
            message_obj = getattr(event, "message_obj", None)
            raw_message = getattr(message_obj, "raw_message", None)
        except Exception:  # noqa: BLE001
            raw_message = None
        for mention in getattr(raw_message, "mentions", None) or []:
            if isinstance(mention, dict):
                if mention.get("is_you") is True:
                    continue
                _add(mention.get("id"))
            else:
                if getattr(mention, "is_you", False) is True:
                    continue
                _add(getattr(mention, "id", None))

        # 兜底：msg_elements 里偶尔还留着原始片段
        for elem in getattr(raw_message, "msg_elements", None) or []:
            if not isinstance(elem, dict):
                continue
            content = elem.get("content")
            if not isinstance(content, str):
                continue
            for m in re.finditer(cls._AT_MARKUP, content):
                _add(m.group(1) or m.group(2))
        return out

    @staticmethod
    def _looks_like_id(text: str) -> bool:
        """QQ 号是纯数字，官方机器人的 openid 是十六进制/带下划线的长串。"""
        t = str(text or "").strip()
        if not t:
            return False
        if t.isdigit() and len(t) >= 5:
            return True
        return bool(re.fullmatch(r"[A-Za-z0-9_\-]{8,}", t))

    async def _cmd_grant(self, event: AstrMessageEvent, rest: list[str]):
        reason = self.permission.deny_reason(event, need="admin")
        if reason:
            return "text", reason

        role = ROLE_TRANSLATOR
        args = list(rest or [])
        if args and args[-1].lower() in (ROLE_VIEWER, ROLE_TRANSLATOR):
            role = args.pop().lower()

        targets = self._at_targets(event)
        for arg in args:
            if self._looks_like_id(arg) and arg not in targets:
                targets.append(arg)

        if not targets:
            return (
                "text",
                "用法：/pt grant <@某人 或 QQ号/openid> [viewer|translator]，默认 translator。"
                "官方机器人 @ 人拿到的是 openid，填 QQ 号对不上；"
                "也可以让对方发 /pt 我，把「你的ID」那行复制过来。",
            )
        by = str(event.get_sender_id())
        for target in targets:
            await self.store.grant(target, role, by)
        return (
            "text",
            f"已给 {'、'.join(targets)} 授权：{ROLE_CN[role]}\n"
            f"要用写操作还得插件配置里 enable_write 打开。",
        )

    async def _cmd_revoke(self, event: AstrMessageEvent, rest: list[str]):
        reason = self.permission.deny_reason(event, need="admin")
        if reason:
            return "text", reason
        targets = self._at_targets(event) or [
            a for a in (rest or []) if self._looks_like_id(a)
        ]
        if not targets:
            return "text", "用法：/pt revoke <@某人 或 QQ号/openid>"
        done = [t for t in targets if await self.store.revoke(t)]
        missing = [t for t in targets if t not in done]
        lines = []
        if done:
            lines.append(f"已撤销 {'、'.join(done)} 的授权。")
        if missing:
            lines.append(f"{'、'.join(missing)} 本来就没授权。")
        return "text", "\n".join(lines)

    async def _cmd_grants(self, event: AstrMessageEvent, rest: list[str]):
        grants = self.store.all_grants()
        if not grants:
            return "text", "授权名单是空的。"
        lines = [f"已授权 {len(grants)} 人"]
        for uid, info in sorted(grants.items()):
            role = ROLE_CN.get(info.get("role"), info.get("role"))
            lines.append(
                f"· {uid}　{role}　{info.get('by', '?')} 于 {info.get('at', '?')} 授予"
            )
        return "text", "\n".join(lines)

    # ------------------------------------------------------------------ 写

    # ---------------------------------------------------------------- 提交与确认

    @staticmethod
    def _string_item(row: dict) -> dict:
        """把 API 词条行整理成词条卡片需要的字段。"""
        fobj = row.get("file") or {}
        fname = fobj.get("name") if isinstance(fobj, dict) else str(fobj or "")
        return {
            "id": row.get("id"),
            "stage": row.get("stage"),
            "stage_name": STAGE_NAMES.get(row.get("stage"), str(row.get("stage"))),
            "file_name": fname or "",
            "original": (row.get("original") or "").strip(),
            "translation": (row.get("translation") or "").strip(),
        }

    def _sender_name(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_name() or event.get_sender_id())
        except Exception:  # noqa: BLE001
            return str(event.get_sender_id())

    async def _propose(
        self,
        event: AstrMessageEvent,
        pid: int,
        items: list[tuple[int, str]],
        note: str = "",
    ) -> str:
        """发起一次待确认提交：抓旧译文、落盘、发「红绿差分图 + 按钮」一条消息。"""
        if not items:
            return "没有要提交的内容。"
        bulk = await self.client.bulk_get_strings(pid, [int(sid) for sid, _ in items])
        enriched: list[dict] = []
        for sid, new_text in items:
            info = bulk.get(int(sid)) or {}
            enriched.append(
                {
                    "string_id": int(sid),
                    "original": str(info.get("original") or ""),
                    "old": str(info.get("translation") or ""),
                    "new": str(new_text),
                }
            )

        now = time.time()
        sub_hash = hashlib.sha1(
            f"{pid}|{now}|{event.get_sender_id()}|{len(items)}|{note}".encode()
        ).hexdigest()[:8]
        if self.store.get_submission(sub_hash, project=int(pid)):
            sub_hash = hashlib.sha1(f"{sub_hash}{now}".encode()).hexdigest()[:8]
        code = f"{secrets.randbelow(900000) + 100000}"
        rec = {
            "hash": sub_hash,
            "code": code,
            "project": int(pid),
            "items": enriched,
            "by": str(event.get_sender_id()),
            "by_name": self._sender_name(event),
            "note": note,
            **(
                {"submitter": self.notifier.self_uid_text()}
                if self.notifier.self_uid_text()
                else {}
            ),
            "status": "pending",
            "created_at": now,
            "at": datetime.now().isoformat(timespec="seconds"),
            "expires_at": now + float(self.confirm_ttl),
        }
        self.store.add_submission(rec)

        path = await self._render_diff(pid, enriched, self._sender_name(event))
        sent_keyboard = False
        if path:
            url = await self._media_url(event, path)
            if url:
                try:
                    from PIL import Image

                    with Image.open(path) as im:
                        height = im.height
                except Exception:  # noqa: BLE001
                    height = 400
                md = f"![待确认 #{720}px #{height}px]({url})"
                sent_keyboard, _kb_detail = await send_keyboard(
                    event,
                    md,
                    build_confirm_keyboard(code, f"确认写入 {len(items)} 条"),
                )
            if not sent_keyboard:
                await self._send_once_image(event, path)
            else:
                self._cleanup_later(path)
        if sent_keyboard:
            return (
                f"已发起提交 {sub_hash}：{len(items)} 条，确认码 {code}，"
                f"{self.confirm_ttl} 秒内有效（有写权限的人确认后才写入）。"
            )
        return (
            f"已发起提交 {sub_hash}：{len(items)} 条，确认码 {code}，"
            f"{self.confirm_ttl} 秒内有效。\n"
            f"（按钮/图发不出去，直接发 /pt 确认 {code} 也行）"
        )

    async def _apply_items(
        self, pid: int, items: list[dict]
    ) -> tuple[list[int], list[str]]:
        done: list[int] = []
        failed: list[str] = []
        payload = [
            {
                "id": int(it["string_id"]),
                "translation": str(it.get("new") or ""),
                "stage": 1,
            }
            for it in items
        ]

        # 多条走批量接口，省请求；一条就老实单发
        if len(payload) > 1:
            try:
                got = await self.client.batch_edit_strings(
                    pid, payload, allow_write=self.enable_write
                )
                ok_ids = {
                    int(x)
                    for x in (got or [])
                    if isinstance(x, (int, str)) and str(x).isdigit()
                }
                done = [i for i in ok_ids]
                failed = [str(p["id"]) for p in payload if int(p["id"]) not in ok_ids]
                if done:
                    return done, failed
                logger.warning("[ParaTranz] 批量写入没返回 id，改用逐条")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ParaTranz] 批量写入失败，改用逐条：{exc}")

        for it in items:
            try:
                await self.client.update_string(
                    pid,
                    int(it["string_id"]),
                    str(it.get("new") or ""),
                    stage=1,
                    allow_write=self.enable_write,
                )
                done.append(int(it["string_id"]))
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{it['string_id']}（{exc}）")
        return done, failed

    async def _cmd_set(self, event: AstrMessageEvent, rest: list[str]):
        """手动发起一条修改（不直接写，等有权限的人确认）。"""
        if not self.client.has_token:
            return "text", "改词条要 Token，先配一个项目成员的 Token。"
        if len(rest) < 2:
            return (
                "text",
                "用法：/pt 修改 <词条ID> <译文>，或 /pt 修改 @项目ID <词条ID> <译文>",
            )

        pids = self.store.binds_of(event.get_group_id())
        if rest[0].startswith("@") and rest[0][1:].isdigit():
            pid = int(rest[0][1:])
            if pid not in pids and not self.permission.is_admin(event):
                return "text", f"#{pid} 没绑到本群，改不了。先用 /pt 绑定 绑上"
            rest = rest[1:]
        elif len(pids) == 1:
            pid = pids[0]
        elif not pids:
            return "text", "本群没绑定项目，管理员先 /pt 绑定 <项目ID>"
        else:
            joined = "、".join(self._project_label(p) for p in pids)
            return "text", f"本群绑了多个项目，用 @项目ID 指定改哪个：{joined}"

        sid, translation = int(rest[0]), " ".join(rest[1:])
        await self._propose(event, pid, [(sid, translation)])
        return "sent", ""

    async def _cmd_stage(self, event: AstrMessageEvent, rest: list[str]):
        """改词条状态（标有疑问、标已检查等），只动状态，不碰原文和译文。"""
        if not self.client.has_token:
            return "text", "改状态要 Token。"
        reason = self.permission.deny_reason(event, need="write")
        if reason:
            return "text", reason
        pids = self.store.binds_of(event.get_group_id())
        if rest and rest[0].startswith("@") and rest[0][1:].isdigit():
            pid = int(rest[0][1:])
            rest = rest[1:]
        elif len(pids) == 1:
            pid = pids[0]
        elif not pids:
            return "text", "本群没绑定项目，管理员先 /pt 绑定 <项目ID>"
        else:
            return "text", "本群绑了多个项目，用 /pt 状态 @项目ID <词条ID> <状态> 指定"
        if len(rest) < 2:
            return (
                "text",
                "用法：/pt 状态 <词条ID> <状态>，状态可写 有疑问 / 已翻译 / 已检查",
            )
        sid_text, stage_text = rest[0], rest[-1].strip()
        if not sid_text.isdigit():
            return "text", "词条ID得是数字。"
        stage = (
            int(stage_text)
            if stage_text.lstrip("-").isdigit()
            else STAGE_ALIASES.get(stage_text)
        )
        if stage is None:
            return "text", f"状态不认识：{stage_text}。可用：" + "、".join(
                STAGE_ALIASES
            )
        if stage == 5:
            return "text", "标「已审核」要校对权限，这个自己去网页点，别让我代劳。"
        try:
            await self.client.set_string_stage(
                pid, int(sid_text), int(stage), allow_write=self.enable_write
            )
        except ParaTranzError as exc:
            return "text", f"改状态失败：{exc.message}"
        return "text", f"词条 {sid_text} 已标成「{STAGE_NAMES.get(stage, stage)}」。"

    async def _cmd_ok(self, event: AstrMessageEvent, rest: list[str]):
        if not rest or not rest[0].isdigit():
            return "text", "用法：/pt 确认 <确认码>"
        code = rest[0].strip()
        rec = self.store.find_submission_by_code(
            code, projects=self.store.binds_of(event.get_group_id()) or None
        )
        if rec is None:
            return "text", "确认码无效、已经被确认过，或者已过期。"
        if time.time() > float(rec.get("expires_at") or 0):
            self.store.update_submission(rec["hash"], status="expired")
            return (
                "text",
                f"提交 {rec['hash']} 已过期（超过 {self.confirm_ttl} 秒），重新发起一次吧。",
            )
        reason = self.permission.deny_reason(event, need="write")
        if reason:
            return "text", f"你没权限确认，得等有写权限的人来点。{reason}"
        if not self.enable_write:
            return "text", "插件里 enable_write 是关的，谁点都写不进去。"

        done, failed = await self._apply_items(
            int(rec["project"]), rec.get("items") or []
        )
        status = "committed" if done else "failed"
        self.store.update_submission(
            rec["hash"],
            status=status,
            approver=str(event.get_sender_id()),
            approver_name=self._sender_name(event),
            approved_at=datetime.now().isoformat(timespec="seconds"),
            result={"done": done, "failed": failed},
        )
        lines = [
            f"提交 {rec['hash']} 已写入：成功 {len(done)} 条，失败 {len(failed)} 条",
            f"· 发起人：{rec.get('by_name') or rec.get('by')}　确认人：{self._sender_name(event)}",
        ]
        if failed:
            lines.append("· 失败：" + "；".join(failed[:8]))
        lines.append(f"· 反悔就用 /pt 撤回 {rec['hash']}（管理员）")
        report = "\n".join(lines)
        if self.permission.is_admin(event):
            md = (
                f"**提交 {rec['hash']} 已写入**　{len(done)} 条\n\n"
                f"发起人 {rec.get('by_name') or rec.get('by')}　确认人 {self._sender_name(event)}"
            )
            revert_ok, _detail = await send_keyboard(
                event, md, build_revert_keyboard(rec["hash"])
            )
            if revert_ok:
                return "sent", ""
        return "text", report

    async def _cmd_cancel(self, event: AstrMessageEvent, rest: list[str]):
        n = 0
        pending, _total = self.store.list_submissions(
            limit=500,
            status="pending",
            projects=self.store.binds_of(event.get_group_id()) or None,
        )
        for rec in pending:
            if str(rec.get("by")) == str(event.get_sender_id()):
                self.store.update_submission(rec["hash"], status="cancelled")
                n += 1
        return "text", f"取消 {n} 个待确认提交。" if n else "没有待确认提交。"

    async def _cmd_revert(self, event: AstrMessageEvent, rest: list[str]):
        """管理员撤回一次已提交的改动：把旧译文写回去。"""
        reason = self.permission.deny_reason(event, need="admin")
        if reason:
            return "text", reason
        if not rest:
            items, _total = self.store.list_submissions(
                limit=10,
                status="committed",
                projects=self.store.binds_of(event.get_group_id()) or None,
            )
            if not items:
                return "text", "没有已提交的记录。"
            return "text", "要给哪个撤回？\n" + "\n".join(
                f"· {r['hash']}　{len(r.get('items') or [])} 条　"
                f"{r.get('by_name') or r.get('by')} 发起，"
                f"{(r.get('approved_at') or r.get('at') or '')[:16]}"
                for r in items
            )
        sub_hash = rest[0].strip().lstrip("#")
        rec = self.store.get_submission(sub_hash, project=None) or None
        if rec is None:
            return "text", f"没找到提交 {sub_hash}。"
        if rec.get("status") != "committed":
            return (
                "text",
                f"提交 {sub_hash} 状态是 {rec.get('status')}，只能撤回已写入的。",
            )
        if not self.client.has_token:
            return "text", "撤回要 Token。"

        pid = int(rec["project"])
        done, failed = [], []
        for it in rec.get("items") or []:
            try:
                await self.client.update_string(
                    pid,
                    int(it["string_id"]),
                    str(it.get("old") or ""),
                    stage=1,
                    allow_write=self.enable_write,
                )
                done.append(int(it["string_id"]))
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{it['string_id']}（{exc}）")
        self.store.update_submission(
            sub_hash,
            status="reverted" if done else "revert_failed",
            reverted_by=str(event.get_sender_id()),
            reverted_at=datetime.now().isoformat(timespec="seconds"),
        )
        lines = [f"提交 {sub_hash} 已撤回：回滚 {len(done)} 条，失败 {len(failed)} 条"]
        if failed:
            lines.append("· 失败：" + "；".join(failed[:8]))
        return "text", "\n".join(lines)

    async def _cmd_tasks(self, event: AstrMessageEvent, rest: list[str]):
        pid, rest = self._pid_or_bound(event, rest)
        limit = self.task_limit
        if rest and rest[0].isdigit():
            limit = max(1, min(int(rest[0]), 20))
        project = await self.client.get_project(pid)
        data = await self.client.list_tasks(pid)
        tasks = data.get("results") if isinstance(data, dict) else data
        tasks = tasks or []
        if not tasks:
            return "text", "这个项目没有任务。"
        shown = tasks[:limit]
        fallback = [f"#{pid} {project.get('name')}　共 {len(tasks)} 个任务"]
        for t in shown:
            files = t.get("taskFiles") or []
            total = sum(_n(f.get("total")) for f in files) or 1
            done = sum(_n(f.get("translated")) for f in files)
            fallback.append(
                f"· {t.get('title')}　翻译 {done / total * 100:.2f}%　"
                f"{len(files)} 文件 / {total} 词条"
            )
        if len(tasks) > limit:
            fallback.append(f"…共 {len(tasks)} 个任务，只显示前 {limit} 个")
        text = "\n".join(fallback)

        if self.list_image:
            path = await self._render_list(
                f"tasks_{pid}",
                render_task_card,
                project,
                shown,
                logo_path=self._wide_logo_path(),
                footer=(
                    f"共 {len(tasks)} 个任务，只显示前 {limit} 个"
                    if len(tasks) > limit
                    else f"共 {len(tasks)} 个任务"
                ),
            )
            if path:
                return "image", (path, text)
        return "text", text

    async def _record_detail(
        self, event: AstrMessageEvent, sub_hash: str
    ) -> tuple[str, object]:
        pids = self.store.binds_of(event.get_group_id())
        rec = None
        for pid in pids or []:
            rec = self.store.get_submission(sub_hash, project=pid)
            if rec:
                break
        if rec is None and self.permission.is_admin(event):
            rec = self.store.get_submission(sub_hash)
        if rec is None:
            return "text", f"本群绑定的项目里没有提交 {sub_hash}。"

        items = rec.get("items") or []
        rows = [
            {
                "id": it.get("string_id"),
                "original": it.get("original") or "（未记录原文）",
                "old": it.get("old") or "",
                "new": it.get("new") or "",
            }
            for it in items
        ]
        status_cn = {
            "pending": "待确认",
            "committed": "已写入",
            "reverted": "已撤回",
            "cancelled": "已取消",
            "expired": "已过期",
            "failed": "部分失败",
            "revert_failed": "撤回失败",
        }.get(str(rec.get("status")), str(rec.get("status")))
        who = (
            f"发起 {rec.get('by_name') or rec.get('by')}　"
            f"确认 {rec.get('approver_name') or rec.get('approver') or '未确认'}"
        )
        when = _fmt_time(rec.get("approved_at") or rec.get("at") or "")
        subtitle = f"#{rec.get('project')}　{status_cn}　{len(items)} 条　{when}"
        out = os.path.join(self.store.dir, "cache", f"rec_{sub_hash}.png")
        try:
            path = await asyncio.to_thread(
                render_diff_card,
                f"提交 {sub_hash}",
                subtitle,
                rows[: self.diff_preview_max or 10],
                out,
                self._wide_logo_path(),
                f"{who}　共 {len(items)} 条",
                rec.get("by_name") or rec.get("by") or "",
            )
        except Exception:  # noqa: BLE001
            logger.warning("[ParaTranz] 提交详情图渲染失败", exc_info=True)
            return "text", f"提交 {sub_hash}：{status_cn}　{len(items)} 条　{who}"
        if not await self._send_once_image(event, path):
            return "text", f"提交 {sub_hash}：{status_cn}　{len(items)} 条　{who}"
        return "sent", ""

    async def _cmd_records(self, event: AstrMessageEvent, rest: list[str]):
        pids = self.store.binds_of(event.get_group_id())
        if not pids:
            return (
                "text",
                "本群还没绑定项目，提交记录也按项目分开存，先 /pt 绑定 一个。",
            )

        pid: int | None = None
        page = 1
        for tok in rest:
            if tok.startswith("@") and tok[1:].isdigit():
                pid = int(tok[1:])
                if pid not in pids and not self.permission.is_admin(event):
                    return "text", f"#{pid} 没绑到本群，看不了它的提交记录。"
            elif tok.isdigit():
                page = max(1, int(tok))
            else:
                return await self._record_detail(event, tok.strip().lstrip("#").lower())

        if pid is None:
            if len(pids) > 1:
                raise AmbiguousProject(
                    "本群绑了多个项目，要看哪个的提交记录？",
                    [(p, self.store.project_name(p)) for p in pids],
                )
            pid = pids[0]

        per_page = self.records_per_page
        recs, total = self.store.list_submissions(
            limit=per_page, projects=[pid], page=page
        )
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        label = self._project_label(pid)

        fallback = [f"提交记录 {label}　共 {total} 条　第 {page}/{pages} 页"]
        for r in recs:
            state = {
                "pending": "待确认",
                "committed": "已写入",
                "reverted": "已撤回",
                "cancelled": "已取消",
                "expired": "已过期",
                "failed": "失败",
                "revert_failed": "撤回失败",
            }.get(str(r.get("status")), str(r.get("status")))
            fallback.append(
                f"· {_fmt_time(r.get('approved_at') or r.get('at') or '')}　"
                f"{len(r.get('items') or [])} 条　{state}　"
                f"{r.get('by_name') or r.get('by')}"
                + (f"（提交者 {r['submitter']}）" if r.get("submitter") else "")
                + f" → {r.get('approver_name') or r.get('approver') or '未确认'}　#{r.get('hash')}"
            )
        fallback.append("看详细图：/pt 记录 <哈希ID>　翻页：/pt 记录 @项目ID <页码>")
        text = "\n".join(fallback)

        if self.list_image:
            path = await self._render_list(
                f"records_{pid}",
                render_records_card,
                recs,
                logo_path=self._wide_logo_path(),
                page=page,
                pages=pages,
                total=total,
                title=f"提交记录　{label}",
            )
            if path:
                hashes = [str(r.get("hash")) for r in recs]
                kb = build_records_keyboard(page, pages, hashes, pid)
                url = await self._media_url(event, path)
                if url:
                    try:
                        from PIL import Image

                        with Image.open(path) as im:
                            height = im.height
                    except Exception:  # noqa: BLE001
                        height = 400
                    md = f"![提交记录 #{720}px #{height}px]({url})"
                    rec_ok, _detail = await send_keyboard(event, md, kb)
                    if rec_ok:
                        self._cleanup_later(path)
                        return "sent", ""
                if await self._send_once_image(event, path):
                    return "sent", ""
        return "text", text

    # ------------------------------------------------------------------ LLM 工具

    async def _hint_pid(self, event: AstrMessageEvent, hint: str) -> int:
        """把「项目ID / @项目ID / 项目名 / 空」解析成项目 ID，且必须绑在本群。"""
        hint = (hint or "").strip().lstrip("@#")
        pids = self.store.binds_of(event.get_group_id())
        admin = self.permission.is_admin(event)
        if not pids and not admin:
            raise ParaTranzError("当前会话还没绑定项目，管理员先发 /pt 绑定 <项目ID>")
        if hint.isdigit():
            pid = int(hint)
            if pid not in pids and not admin:
                raise ParaTranzError(
                    f"#{pid} 没绑到本群。本群绑的是："
                    + "、".join(self._project_label(p) for p in pids)
                )
            return pid
        if not hint:
            if len(pids) == 1:
                return pids[0]
            raise ParaTranzError(
                "本群绑了多个项目，请在参数里用 @项目ID 指定："
                + ("、".join(self._project_label(p) for p in pids) or "（本群没绑定）")
            )
        await self._warm_project_names(pids)
        kw = hint.lower()
        for pid in pids:
            if kw in self.store.project_name(pid).lower():
                return pid
        if admin:
            project = await self._resolve_project(hint)
            pid = int(project.get("id"))
            await self.store.set_project_name(pid, str(project.get("name") or ""))
            return pid
        raise ParaTranzError(
            "本群绑定的项目里没这个名字，直接用 @项目ID 更稳。当前绑定："
            + "、".join(self._project_label(p) for p in pids)
        )

    @filter.llm_tool(name="paratranz_project_progress")
    async def tool_progress(self, event: AstrMessageEvent, project: str = ""):
        """查询 ParaTranz 翻译项目的总体进度，包括已翻译、已检查、已审核、有疑问的条数和百分比。

        Args:
            project(string): 项目ID、@项目ID 或项目名称；留空表示当前群已绑定的项目
        """
        if not self.permission.group_allowed(event):
            return "当前会话不在白名单，无法查询。"
        try:
            pid = await self._hint_pid(event, project)
            info = await self.client.get_project(pid)
            files = await self.client.list_files(pid)
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        summary = ParaTranzClient.summarize_files(files)
        total = summary["total"] or 1
        return (
            f"项目 #{pid} {info.get('name')}\n"
            f"文件 {len(files)} 个，词条 {summary['total']} 条，"
            f"已翻译 {summary['translated']}（{_pct(summary['translated'], total):.1f}%），"
            f"已检查 {summary['checked']}，已审核 {summary['reviewed'] + summary['locked']}，"
            f"有疑问 {summary['disputed']}"
        )

    @filter.llm_tool(name="paratranz_search_terms")
    async def tool_terms(
        self, event: AstrMessageEvent, keyword: str, project: str = ""
    ):
        """在翻译项目的术语表里查词，确认某个词统一译成了什么。专有名词、人名、技能名都该先查这里。

        Args:
            keyword(string): 要查的词，原文或译文都行
            project(string): 项目ID、@项目ID 或项目名称；留空表示当前群已绑定的项目
        """
        if not self.permission.group_allowed(event):
            return "当前会话不在白名单，无法查询。"
        try:
            pid = await self._hint_pid(event, project)
            data = await self.client.list_terms(pid, page=1, page_size=200)
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        kw = (keyword or "").lower()
        hits = [
            t
            for t in data.get("results", [])
            if kw in (t.get("term") or "").lower()
            or kw in (t.get("translation") or "").lower()
        ]
        if not hits:
            return f"术语表里没找到「{keyword}」。"
        return "\n".join(
            f"{t.get('term')} → {t.get('translation')}"
            + (
                f"（{(t.get('note') or '').strip()[:40]}）"
                if (t.get("note") or "").strip()
                else ""
            )
            for t in hits[:10]
        )

    @filter.llm_tool(name="paratranz_get_strings")
    async def tool_strings(
        self,
        event: AstrMessageEvent,
        project: str = "",
        file_id: int = 0,
        stage: int = -99,
    ):
        """读取词条原文和现有译文，用来校对或找问题条目。需要 Token 和授权。

        Args:
            project(string): 项目ID、@项目ID 或项目名称；留空表示当前群已绑定的项目
            file_id(number): 只看某个文件的词条，0 表示不限
            stage(number): 按状态过滤，0未翻译 1已翻译 2有疑问 3已检查 5已审核 9已锁定；-99 表示不限
        """
        if not self.client.has_token:
            return "未配置 Token，读不了词条正文。"
        reason = self.permission.deny_reason(event, need="read")
        if reason:
            return reason
        try:
            pid = await self._hint_pid(event, project)
            data = await self.client.list_strings(
                pid,
                file_id=int(file_id) or None,
                stage=None if int(stage) == -99 else int(stage),
                page=1,
                page_size=self.strings_limit,
            )
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        rows = data.get("results", []) if isinstance(data, dict) else data
        if not rows:
            return "没有匹配的词条。"
        out = []
        for s in rows:
            out.append(
                f"[{s.get('id')}] 状态 {STAGE_NAMES.get(s.get('stage'), s.get('stage'))}\n"
                f"原文：{(s.get('original') or '').strip()[:200]}\n"
                f"译文：{(s.get('translation') or '').strip()[:200]}"
            )
        return "\n\n".join(out)

    @filter.llm_tool(name="paratranz_notifications")
    async def tool_notifications(
        self, event: AstrMessageEvent, only_unread: bool = True, limit: int = 10
    ):
        """看自己的站内通知：谁 @ 了你、谁回复了你的评论、谁把你加进了项目。只读，不会标记已读。

        Args:
            only_unread(bool): 只看未读（status=0），默认 true
            limit(number): 最多几条，默认 10，最多 30
        """
        if not self.client.has_token:
            return "未配置 Token，读不了通知。"
        try:
            data = await self.client.list_messages(1, max(1, min(int(limit) * 3, 90)))
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        rows = data.get("results", []) if isinstance(data, dict) else (data or [])
        if only_unread:
            rows = [r for r in rows if int(r.get("status") or 0) == 0]
        rows = rows[: max(1, min(int(limit), 30))]
        if not rows:
            return "没有" + ("未读" if only_unread else "") + "通知。"
        names = {
            "string.mention": "@了你",
            "string.comment": "评论了词条",
            "member.add": "把你加进了项目",
            "issue.comment": "回复了讨论",
        }
        out = [f"通知 {len(rows)} 条："]
        for r in rows:
            who = (
                (r.get("fromUser") or {}).get("nickname")
                or (r.get("fromUser") or {}).get("username")
                or r.get("from")
            )
            kind = names.get(str(r.get("type")), str(r.get("type")))
            link = r.get("link") or {}
            where = ""
            if link.get("project"):
                where = f"　项目 {link.get('project')}"
            if link.get("id"):
                where += f"　词条 {link.get('id')}"
            content = r.get("content")
            if isinstance(content, dict):
                content = content.get("text") or ""
            out.append(
                f"· {who} {kind}{where}　{r.get('createdAt')}\n"
                f"  {str(content or '').strip()[:80]}"
            )
        return "\n".join(out)

    @filter.llm_tool(name="paratranz_comments")
    async def tool_comments(
        self, event: AstrMessageEvent, string_id: str, limit: int = 0
    ):
        """读词条下的全部评论（注释），校对时看别人留的意见用。默认整条词条的评论一次全拿出来。

        Args:
            string_id(string): 一个词条ID，或者多个用逗号/空格分开，比如 "1080209941,1080209940"
            limit(number): 最多每条显示几条，0 表示全部（默认 0，上限 300）
        """
        if not self.client.has_token:
            return "未配置 Token，读不了评论。"
        ids = [int(x) for x in str(string_id).replace(",", " ").split() if x.isdigit()]
        if not ids:
            return "词条ID得是数字，多个用逗号分开。"
        ids = ids[:10]
        cap = 300 if not limit or int(limit) <= 0 else max(1, min(int(limit), 300))

        async def one(sid: int):
            try:
                rows = await self.client.list_all_comments(sid, "text", cap)
            except ParaTranzError as exc:
                return sid, None, exc.message
            return sid, rows, ""

        results = await asyncio.gather(*[one(i) for i in ids])
        blocks: list[str] = []
        for sid, rows, err in results:
            if err:
                blocks.append(f"词条 {sid}：读取失败（{err}）")
                continue
            if not rows:
                blocks.append(f"词条 {sid}：还没有评论")
                continue
            blocks.append(self._format_comments(sid, rows))
        return "\n\n".join(blocks)

    @staticmethod
    def _format_comments(sid: int, rows: list[dict]) -> str:
        """评论块：按时间老到新，带 #ID 和回复关系，方便顺着对话看。"""
        by_id = {int(r.get("id") or 0): r for r in rows}
        lines = [f"词条 {sid} 的评论共 {len(rows)} 条（从早到晚）："]
        for r in rows:
            user = r.get("user") or {}
            name = user.get("nickname") or user.get("username") or r.get("uid")
            uname = user.get("username") or ""
            who = f"{name}(@{uname})" if uname and uname != name else str(name)
            quote = ""
            parent = by_id.get(int(r.get("replyTo") or 0))
            if parent:
                p_user = (parent.get("user") or {}).get("nickname") or ""
                p_text = (
                    str(parent.get("content") or "").strip().replace("\n", " ")[:24]
                )
                quote = f"（回复 {p_user}「{p_text}」）"
            elif r.get("replyTo"):
                quote = f"（回复 #{r['replyTo']}）"
            body = str(r.get("content") or "").strip().replace("\n", "  ")
            lines.append(
                f"· #{r.get('id')} {who} {r.get('createdAt')}{quote}\n  {body[:400]}"
            )
        return "\n".join(lines)

    @filter.llm_tool(name="paratranz_add_comment")
    async def tool_add_comment(
        self,
        event: AstrMessageEvent,
        string_id: int,
        content: str,
        reply_to: int = 0,
        mention: str = "",
    ):
        """在某条词条下发评论。会以 Token 所属账号的身份发出，需写权限。@人直接传用户名，服务端会渲染成链接。

        Args:
            string_id(number): 词条ID
            content(string): 评论内容（markdown）
            reply_to(number): 回复某条评论时填它的 ID，0 表示新开一条
            mention(string): 要 @ 的用户名，多个用英文逗号分隔，留空不 @
        """
        if not self.client.has_token:
            return "未配置 Token，发不了评论。"
        text = str(content or "").strip()
        names = [
            n.strip().lstrip("@")
            for n in str(mention or "").replace("，", ",").split(",")
            if n.strip().lstrip("@")
        ]
        if names:
            text = (text + " " if text else "") + " ".join(f"@{n}" for n in names) + " "
        if not text.strip():
            return "评论内容是空的。"
        reason = self.permission.deny_reason(event, need="write")
        if reason:
            return reason
        try:
            data = await self.client.add_comment(
                int(string_id),
                text.strip(),
                "text",
                int(reply_to) or None,
                allow_write=self.enable_write,
            )
        except ParaTranzError as exc:
            return f"发评论失败：{exc.message}"
        if isinstance(data, dict) and data.get("id"):
            tail = f"，已 @ {'、'.join(names)}" if names else ""
            return f"已发评论 #{data['id']}（词条 {string_id}）{tail}。"
        return "发出去了，但没拿到评论 ID。"

    @filter.llm_tool(name="paratranz_delete_comment")
    async def tool_delete_comment(self, event: AstrMessageEvent, comment_id: int):
        """删除一条评论。仅管理员可用，删了就没了。

        Args:
            comment_id(number): 评论ID
        """
        if not self.client.has_token:
            return "未配置 Token。"
        reason = self.permission.deny_reason(event, need="admin")
        if reason:
            return reason
        try:
            await self.client.delete_comment(
                int(comment_id), allow_write=self.enable_write
            )
        except ParaTranzError as exc:
            return f"删除失败：{exc.message}"
        return f"已删除评论 #{comment_id}。"

    @filter.llm_tool(name="paratranz_suggestions")
    async def tool_suggestions(
        self,
        event: AstrMessageEvent,
        string_id: int,
        project: str = "",
        rate: int = 70,
    ):
        """取某条词条的翻译建议（翻译记忆），返回历史项目里相似原文的译文和匹配率，校对时用来参考。高开销接口，别连续狂调。

        Args:
            string_id(number): 词条ID
            project(string): 项目ID或@项目ID；留空表示当前群已绑定的项目
            rate(number): 最低匹配率百分比，默认 70，传 0 表示不过滤
        """
        if not self.client.has_token:
            return "未配置 Token，查不了翻译建议。"
        reason = self.permission.deny_reason(event, need="read")
        if reason:
            return reason
        try:
            pid = await self._hint_pid(event, project)
            rows = await self.client.get_suggestions(
                pid, int(string_id), rate=max(0, int(rate))
            )
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        items = rows.get("results", []) if isinstance(rows, dict) else (rows or [])
        if not items:
            return "没有匹配到翻译建议。"
        out = [f"词条 {string_id} 的建议 {len(items)} 条："]
        for s in items[:8]:
            match = s.get("matching")
            match_txt = (
                f"匹配 {round(match * 100, 1)}%"
                if isinstance(match, (int, float))
                else ""
            )
            out.append(
                f"· {match_txt}\n"
                f"  原文：{(s.get('original') or '').strip()[:120]}\n"
                f"  译文：{(s.get('translation') or '').strip()[:120]}"
            )
        return "\n".join(out)

    @filter.llm_tool(name="paratranz_issues")
    async def tool_issues(
        self,
        event: AstrMessageEvent,
        project: str = "",
        issue_id: int = 0,
        status: int = -1,
        page: int = 1,
    ):
        """看项目讨论：不给 issue_id 就是列表（含主讨论和回复），给了就看那条讨论的详细内容和回复。

        Args:
            project(string): 项目ID或@项目ID；留空表示当前群已绑定的项目
            issue_id(number): 讨论ID，0 表示列列表
            status(number): 按状态筛选，0 讨论中，1 已关闭，-1 不限
            page(number): 页码，从 1 开始
        """
        if not self.client.has_token:
            return "未配置 Token，看不了讨论。"
        try:
            pid = await self._hint_pid(event, project)
            if int(issue_id) > 0:
                data = await self.client.get_issue(pid, int(issue_id))
                return self._fmt_issue_detail(data)
            data = await self.client.list_issues(
                pid,
                status=None if int(status) < 0 else int(status),
                page=max(1, int(page)),
                page_size=10,
            )
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        rows = data.get("results", []) if isinstance(data, dict) else (data or [])
        if not rows:
            return "这个项目没有讨论。"
        total = data.get("rowCount") if isinstance(data, dict) else None
        out = [f"讨论 {total if total is not None else len(rows)} 条："]
        for it in rows:
            parent = it.get("parent")
            tag = "回复" if parent else "讨论"
            who = self._tiny_user(it)
            state = "已关闭" if it.get("status") == 1 else "讨论中"
            head = (it.get("title") or "").strip() or "（无标题）"
            body = (it.get("content") or "").strip().replace("\n", " ")[:80]
            out.append(
                f"· [{it.get('id')}] {tag} {head}　{who}　{state if not parent else ''}\n  {body}"
            )
        return "\n".join(out)

    @staticmethod
    def _tiny_user(item: dict) -> str:
        user = item.get("user") or {}
        if isinstance(user, dict):
            return str(user.get("username") or item.get("uid") or "?")
        return str(item.get("uid") or "?")

    def _fmt_issue_detail(self, data: dict) -> str:
        if not isinstance(data, dict):
            return "没拿到这条讨论。"
        lines = [
            f"[{data.get('id')}] {(data.get('title') or '').strip() or '（无标题）'}"
            f"　{self._tiny_user(data)}"
            f"　{'已关闭' if data.get('status') == 1 else '讨论中'}",
            (data.get("content") or "").strip()[:800] or "（空）",
        ]
        replies = data.get("replies") or data.get("children") or []
        if isinstance(replies, list) and replies:
            lines.append(f"\n回复 {len(replies)} 条：")
            for r in replies[:10]:
                lines.append(
                    f"· {self._tiny_user(r)}：{(r.get('content') or '').strip()[:200]}"
                )
        return "\n".join(lines)

    @filter.llm_tool(name="paratranz_create_issue")
    async def tool_create_issue(
        self,
        event: AstrMessageEvent,
        title: str,
        content: str,
        project: str = "",
    ):
        """在项目里发起一条讨论。需要写权限。发出去就公开可见，别乱发。

        Args:
            title(string): 讨论标题
            content(string): 讨论内容（markdown），@人直接写 @用户名
            project(string): 项目ID或@项目ID；留空表示当前群已绑定的项目
        """
        if not self.client.has_token:
            return "未配置 Token，发不了讨论。"
        if not str(title or "").strip() or not str(content or "").strip():
            return "标题和内容都要有。"
        reason = self.permission.deny_reason(event, need="write")
        if reason:
            return reason
        try:
            pid = await self._hint_pid(event, project)
            data = await self.client.create_issue(
                pid,
                str(title).strip(),
                str(content).strip(),
                allow_write=self.enable_write,
            )
        except ParaTranzError as exc:
            return f"发起讨论失败：{exc.message}"
        if isinstance(data, dict) and data.get("id"):
            return f"已发起讨论 #{data['id']}：{title}"
        return "发出去了，但没拿到讨论 ID。"

    @filter.llm_tool(name="paratranz_dry_run")
    async def tool_dry_run(
        self,
        event: AstrMessageEvent,
        target_id: int,
        kind: str = "string",
        text: str = "",
        project: str = "",
    ):
        """预览：拿一句话（或讨论里的一条回复）模拟一遍，看看模型会不会回、会不会改状态。
        只读不写，用来排查「通知到了但机器人没动作」。

        Args:
            target_id(number): 词条ID，kind=issue 时是讨论ID
            kind(string): string 或 issue，默认 string
            text(string): 假设对方说的话，留空就用「在吗」
            project(string): 项目ID或@项目ID；留空表示当前群已绑定的项目
        """
        if not self.client.has_token:
            return "未配置 Token。"
        try:
            pid = await self._hint_pid(event, project)
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        is_issue = str(kind).lower().startswith("issue")
        notice = PendingNotice(
            id=0,
            type="issue.reply" if is_issue else "string.comment",
            text=str(text or "在吗"),
            project=int(pid),
            string_id=None if is_issue else int(target_id),
            issue_id=int(target_id) if is_issue else None,
            username=self._sender_name(event) or "测试",
            mention="",
            user_id=int(event.get_sender_id())
            if str(event.get_sender_id()).isdigit()
            else None,
        )
        try:
            ctx = await self.notifier._build_context(notice)
        except Exception as exc:  # noqa: BLE001
            return f"取上下文失败：{type(exc).__name__}: {exc}"
        preview = await self.notifier.dry_run(notice)
        return f"模拟结果：{preview}\n\n上下文：\n{ctx[:800]}"

    @filter.llm_tool(name="paratranz_reply_issue")
    async def tool_reply_issue(
        self,
        event: AstrMessageEvent,
        issue_id: int,
        content: str,
        project: str = "",
    ):
        """回复某条讨论。需要写权限。

        Args:
            issue_id(number): 讨论ID
            content(string): 回复内容（markdown），@人直接写 @用户名
            project(string): 项目ID或@项目ID；留空表示当前群已绑定的项目
        """
        if not self.client.has_token:
            return "未配置 Token，回不了讨论。"
        if not str(content or "").strip():
            return "回复内容是空的。"
        reason = self.permission.deny_reason(event, need="write")
        if reason:
            return reason
        try:
            pid = await self._hint_pid(event, project)
            data = await self.client.reply_issue(
                pid, int(issue_id), str(content).strip(), allow_write=self.enable_write
            )
        except ParaTranzError as exc:
            return f"回复失败：{exc.message}"
        if isinstance(data, dict) and data.get("id"):
            return f"已回复讨论 #{issue_id}（回复 {data['id']}）。"
        return "回上去了，但没拿到回复 ID。"

    @filter.llm_tool(name="paratranz_check_notifications")
    async def tool_check_notifications(
        self, event: AstrMessageEvent, force: bool = False
    ):
        """主动拉一轮站内通知（等于定时任务跑一次），把攒到的提醒内容返回；不会自己发消息，也不会主动推送。

        Args:
            force(bool): 忽略 batch 模式的时间/条数阈值，把攒着的全拿出来
        """
        if not self.client.has_token:
            return "未配置 Token，读不了通知。"
        try:
            await self.notifier.poll_once()
        except ParaTranzError as exc:
            return f"拉取失败：{exc.message}"
        batch = self.notifier.take_all() if force else self.notifier.ready_batch()
        if not batch:
            left = self.notifier.pending_count()
            if left:
                return f"有 {left} 条提醒攒着，还没到批量阈值（模式 {self.notifier.mode}）。"
            return "没有新的提醒。"
        return self.notifier.render_batch(batch)

    @filter.llm_tool(name="paratranz_list_groups")
    async def tool_list_groups(self, event: AstrMessageEvent):
        """列出本机器人绑定了项目的群：群ID尾号、平台、各自绑定的项目。用来搞清楚"哪个群是哪个群"。"""
        binds = self.store.all_binds() or {}
        if not binds:
            return "还没有任何群绑定项目。"
        names = dict(self.store.all_project_names() or {})
        out = [f"共 {len(binds)} 个群："]
        for gid, pids in binds.items():
            seq = pids if isinstance(pids, list) else [pids]
            labels = []
            for pid in seq:
                name = names.get(str(pid)) or names.get(pid)
                labels.append(f"#{pid} {name}" if name else f"#{pid}")
            gname = self.store.group_name(gid)
            head = (
                f"· {gname}（…{str(gid)[-6:]}）" if gname else f"· 群 …{str(gid)[-6:]}"
            )
            out.append(f"{head} [{gid}] → " + ("、".join(labels) or "（空）"))
        out.append("群名来自官方群信息接口，取不到就用 /pt 群名 <名字> 手动起。")
        return "\n".join(out)

    @filter.llm_tool(name="paratranz_read_group_chat")
    async def tool_read_group_chat(
        self,
        event: AstrMessageEvent,
        project: int = 0,
        group_id: str = "",
        limit: int = 20,
    ):
        """读某个群最近的聊天记录（就是机器人平时看到的那份），用来"水群"前先看上下文。

        Args:
            project(number): 按项目找群，比如 20916
            group_id(string): 直接指定群ID
            limit(number): 取最近几条，默认 20，最多 50
        """
        gid = str(group_id or "").strip()
        if not gid and int(project):
            for g, pids in (self.store.all_binds() or {}).items():
                seq = pids if isinstance(pids, list) else [pids]
                if int(project) in [int(p) for p in seq if str(p).isdigit()]:
                    gid = g
                    break
            if not gid:
                return f"项目 {project} 没绑定在任何群里。"
        if not gid:
            return "得给 project 或 group_id。"
        sess = self.pick_session(gid)
        if not sess:
            return "拼不出会话串（平台还没记住）。"
        plat_id = sess.split(":", 1)[0]
        n = max(1, min(int(limit or 20), 50))
        try:
            rows = await self.context.get_db().get_platform_message_history(
                plat_id, sess, page=1, page_size=n
            )
        except Exception as exc:  # noqa: BLE001
            return f"读聊天记录失败：{type(exc).__name__}: {exc}"

        lines: list[str] = []
        for row in reversed(list(rows or [])):
            text = self._history_text(getattr(row, "content", "") or "")
            if not text:
                continue
            who = getattr(row, "sender_name", "") or "?"
            when = str(getattr(row, "created_at", ""))[11:16]
            lines.append(f"[{when}] {who}: {text}")
        if not lines:
            return (
                "没读到聊天记录。可能这个群还没说过话，或者没开群消息记录"
                "（provider_ltm_settings.group_message_history_enable）。"
            )
        name = self.store.group_name(gid) or f"…{gid[-6:]}"
        return f"「{name}」最近 {len(lines)} 条：\n" + "\n".join(lines)

    @staticmethod
    def _history_text(content: str) -> str:
        """把数据库里存的消息 JSON 拆成一行纯文本。"""
        try:
            data = json.loads(content) if isinstance(content, str) else content
        except Exception:  # noqa: BLE001
            return str(content)[:120]
        if not isinstance(data, dict):
            return ""
        parts: list[str] = []
        for comp in data.get("message") or []:
            if not isinstance(comp, dict):
                continue
            ctype = str(comp.get("type") or "")
            if ctype == "plain":
                parts.append(str(comp.get("text") or ""))
            elif ctype == "at":
                parts.append(f"@{comp.get('name') or comp.get('user_id')}")
            elif ctype == "image":
                parts.append("[图片]")
            elif ctype in ("record", "video", "file"):
                parts.append(f"[{ctype}]")
            elif ctype == "face" or ctype == "mface":
                parts.append("[表情]")
        return " ".join(p.strip() for p in parts if p and p.strip())[:300]

    @filter.llm_tool(name="paratranz_speak_group")
    async def tool_speak_group(
        self,
        event: AstrMessageEvent,
        text: str,
        project: int = 0,
        group_id: str = "",
    ):
        """往指定绑定群主动发一条消息。需要管理员权限。按项目找群，或直接给群ID。

        Args:
            text(string): 要说的话
            project(number): 按项目找群，比如 20916；绑了多个群时发第一个
            group_id(string): 直接指定群ID（绑定列表里的那串）
        """
        reason = self.permission.deny_reason(event, need="admin")
        if reason:
            return reason
        body = str(text or "").strip()
        if not body:
            return "要说什么总得给吧。"
        gid = str(group_id or "").strip()
        if not gid and int(project):
            target = None
            for g, pids in (self.store.all_binds() or {}).items():
                seq = pids if isinstance(pids, list) else [pids]
                if int(project) in [int(p) for p in seq if str(p).isdigit()]:
                    target = g
                    break
            if not target:
                return f"项目 {project} 没有绑定在任何群里。"
            gid = target
        if not gid:
            return "得告诉我是哪个群：给 project 或 group_id。"
        session = self.pick_session(gid)
        if not session:
            return "拿不到平台会话（还没在群里说过话？先发条指令让我记住平台）。"
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Plain

            await self.context.send_message(session, MessageChain([Plain(body)]))
        except Exception as exc:  # noqa: BLE001
            return f"发送失败：{type(exc).__name__}: {exc}"
        return f"已发到群 …{gid[-6:]}。"

    @filter.llm_tool(name="paratranz_scores")
    async def tool_scores(
        self, event: AstrMessageEvent, project: str = "", page: int = 1
    ):
        """看项目贡献榜（按翻译/编辑/审核累计的贡献分），用来评价某个译者干了多少活。

        Args:
            project(string): 项目ID或@项目ID；留空表示当前群已绑定的项目
            page(number): 页码，从 1 开始
        """
        if not self.client.has_token:
            return "未配置 Token，看不了贡献。"
        try:
            pid = await self._hint_pid(event, project)
            data = await self.client.list_scores(
                pid, page=max(1, int(page)), page_size=20
            )
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        rows = data.get("results", []) if isinstance(data, dict) else (data or [])
        if not rows:
            return "还没有贡献记录（整包导入的项目通常一条都没有）。"
        lines = []
        for i, s in enumerate(rows, 1):
            lines.append(
                f"{i}. {self._tiny_user(s)}　贡献 {round(float(s.get('value') or 0), 2)}"
                f"　基准 {s.get('base')} × 乘数 {s.get('multiplier')}"
            )
        return "\n".join(lines)

    @filter.llm_tool(name="paratranz_history")
    async def tool_history(
        self,
        event: AstrMessageEvent,
        project: str = "",
        uid: int = 0,
        tid: int = 0,
        kind: str = "",
        page: int = 1,
    ):
        """查项目历史记录：可按成员、词条、操作类型筛选。kind 传 comment 就是看评论记录。

        Args:
            project(string): 项目ID或@项目ID；留空表示当前群已绑定的项目
            uid(number): 只看这个用户的操作，0 表示不限
            tid(number): 只看这条词条的操作，0 表示不限
            kind(string): 操作类型，translate/edit/check/review/term/import/comment，空表示默认词条历史
            page(number): 页码，从 1 开始
        """
        if not self.client.has_token:
            return "未配置 Token，看不了历史。"
        try:
            pid = await self._hint_pid(event, project)
            data = await self.client.list_history(
                pid,
                uid=int(uid) or None,
                tid=int(tid) or None,
                kind=str(kind or "").strip() or None,
                page=max(1, int(page)),
                page_size=10,
            )
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        rows = data.get("results", []) if isinstance(data, dict) else (data or [])
        if not rows:
            return "没有匹配的历史记录。"
        total = data.get("rowCount") if isinstance(data, dict) else None
        out = [f"历史 {total if total is not None else len(rows)} 条："]
        for r in rows:
            field = r.get("field") or r.get("type") or ""
            out.append(
                f"· [{r.get('tid')}] {self._tiny_user(r)} {field}"
                f"　{str(r.get('from') or '')[:40]} → {str(r.get('to') or '')[:40]}"
                f"　{r.get('createdAt') or ''}"
            )
        return "\n".join(out)

    @filter.llm_tool(name="paratranz_search_strings")
    async def tool_search_strings(
        self,
        event: AstrMessageEvent,
        text: str = "",
        translation: str = "",
        context: str = "",
        key: str = "",
        project: str = "",
        file_id: int = 0,
        stage: int = -99,
        page: int = 1,
        regex: str = "",
        regex_negate: bool = False,
        format: str = "",
    ):
        """高级搜索词条：原文、译文、上下文、键值可同时组合（服务端 AND），用来找某句台词出现在哪、谁把某个词译成了什么。需要 Token 和授权。

        Args:
            text(string): 在原文里搜的关键词，留空不限制
            translation(string): 在译文里搜的关键词，留空不限制
            context(string): 在上下文备注里搜的关键词，留空不限制
            key(string): 按键值搜，比如 000A 这种 uid，留空不限制
            project(string): 项目ID、@项目ID 或项目名称；留空表示当前群已绑定的项目
            file_id(number): 只看某个文件的词条，0 表示不限
            stage(number): 按状态过滤，0未翻译 1已翻译 2有疑问 3已检查 5已审核 9已锁定；-99 表示不限
            page(number): 页码，从 1 开始
            regex(string): 正则表达式，在原文里服务端正则匹配。走 ParaTranz 原生 advanced 搜索，不需要本地拉页。
            regex_negate=true 时不匹配（!~= 操作符）。
            format(string): 传 ids 时只返回逗号分隔的ID列表，不带原文和状态，省大量token。
        """
        if not self.client.has_token:
            return "未配置 Token，读不了词条正文。"
        regex_pat = str(regex or "").strip()
        if not any(
            str(x or "").strip() for x in (text, translation, context, key, regex_pat)
        ):
            return "至少给一个搜索条件：text / translation / context / key / regex。"
        reason = self.permission.deny_reason(event, need="read")
        if reason:
            return reason
        try:
            pid = await self._hint_pid(event, project)
            data = await self.client.list_strings(
                pid,
                file_id=int(file_id) or None,
                stage=None if int(stage) == -99 else int(stage),
                page=max(1, int(page)),
                page_size=self.strings_limit,
                text=str(text or "").strip() or None,
                translation=str(translation or "").strip() or None,
                context=str(context or "").strip() or None,
                key=str(key or "").strip() or None,
                regex=regex_pat or None,
                regex_negate=str(regex_negate).lower() in ("true", "1", "yes"),
            )
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        rows = data.get("results", []) if isinstance(data, dict) else data

        if not rows:
            return "没有匹配的词条。"

        # format=ids：只返回ID列表，省token
        if str(format or "").strip().lower() == "ids":
            ids = [str(r.get("id")) for r in rows if r.get("id")]
            total = data.get("rowCount") if isinstance(data, dict) else None
            head = f"共 {total if total is not None else len(ids)} 条，本页 {len(ids)} 个ID："
            return head + ",".join(ids)
        total = data.get("rowCount") if isinstance(data, dict) else None
        cond = "、".join(
            f"{name}={value}"
            for name, value in (
                ("原文", text),
                ("译文", translation),
                ("上下文", context),
                ("键值", key),
            )
            if str(value or "").strip()
        )
        head = f"命中 {total if total is not None else len(rows)} 条（{cond}），第 {max(1, int(page))} 页："
        out = [head]
        for s in rows:
            out.append(
                f"[{s.get('id')}] {STAGE_NAMES.get(s.get('stage'), s.get('stage'))}\n"
                f"原文：{(s.get('original') or '').strip()[:200]}\n"
                f"译文：{(s.get('translation') or '').strip()[:200] or '（空）'}"
            )
        return "\n\n".join(out)

    @filter.llm_tool(name="paratranz_get_string")
    async def tool_get_string(
        self, event: AstrMessageEvent, string_id: int, project: str = ""
    ):
        """按词条 ID 读单条词条（原文、现有译文、状态），改完或校对时用来复查那一条。需要 Token 和授权。

        Args:
            string_id(number): 词条ID
            project(string): 项目ID或@项目ID；留空表示当前群已绑定的项目
        """
        if not self.client.has_token:
            return "未配置 Token，读不了词条正文。"
        reason = self.permission.deny_reason(event, need="read")
        if reason:
            return reason
        try:
            pid = await self._hint_pid(event, project)
            item = await self.client.get_string(pid, int(string_id))
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        if not isinstance(item, dict):
            return "没拿到这条词条。"
        stage = STAGE_NAMES.get(item.get("stage"), item.get("stage"))
        uid = item.get("uid")
        return (
            f"[{item.get('id')}] 状态 {stage}　文件 {item.get('file') or '?'}　提交者 {uid or '?'}\n"
            f"原文：{(item.get('original') or '').strip()}\n"
            f"译文：{(item.get('translation') or '').strip() or '（空）'}"
            + (
                f"\n备注：{(item.get('context') or '').strip()[:200]}"
                if (item.get("context") or "").strip()
                else ""
            )
        )

    @filter.llm_tool(name="paratranz_render_strings")
    async def tool_render_strings(
        self,
        event: AstrMessageEvent,
        project: str = "",
        stage: int = -99,
        file_id: int = 0,
        limit: int = 10,
    ):
        """把一组词条渲染成卡片图发到群里：每条一张白卡，状态徽标按官网配色，
        原文灰色（只读）、译文正文色。适合一次看多条、跟人讨论用。

        Args:
            project(string): 项目ID或@项目ID；留空表示当前群已绑定的项目
            stage(number): 状态过滤，0未翻译 1已翻译 2有疑问 3已检查 5已审核 9已锁定；-99 表示不限
            file_id(number): 只看某个文件的词条，0 表示不限
            limit(number): 取几条，默认 10，上限 30
        """
        if not self.client.has_token:
            return "未配置 Token，读不了词条。"
        reason = self.permission.deny_reason(event, need="read")
        if reason:
            return reason
        try:
            pid = await self._hint_pid(event, project)
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        limit = max(1, min(int(limit or 10), 30))
        stage_arg = None if int(stage) == -99 else int(stage)
        try:
            data = await self.client.list_strings(
                pid,
                file_id=int(file_id) or None,
                stage=stage_arg,
                page=1,
                page_size=limit,
            )
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        rows = data.get("results", []) if isinstance(data, dict) else data
        if not rows:
            return "没有匹配的词条。"

        items = [self._string_item(r) for r in rows]
        stage_txt = (
            STAGE_NAMES.get(stage_arg, stage_arg)
            if stage_arg is not None
            else "全部状态"
        )
        out = os.path.join(self.store.dir, "cache", f"strings_{pid}_{limit}.png")
        try:
            path = await asyncio.to_thread(
                render_strings_card,
                "词条",
                f"#{pid}　{stage_txt}　文件 {file_id or '全部'}　共 {len(items)} 条",
                items,
                out,
                self._wide_logo_path(),
                "",
            )
        except Exception:  # noqa: BLE001
            logger.warning("[ParaTranz] 词条卡片渲染失败", exc_info=True)
            path = None
        sent = bool(path) and await self._send_once_image(event, path)
        brief = "\n".join(
            f"[{r.get('id')}] {STAGE_NAMES.get(r.get('stage'), r.get('stage'))}"
            for r in rows[:limit]
        )
        return (f"已发词条卡片图（{len(items)} 条）。\n" if sent else "") + brief

    @filter.llm_tool(name="paratranz_disputed")
    async def tool_disputed(
        self,
        event: AstrMessageEvent,
        project: str = "",
        limit: int = 10,
        with_image: bool = True,
    ):
        """列出标了「有疑问」（stage 2）的词条，用来跟用户讨论怎么改。可以顺带发一张清单图。

        Args:
            project(string): 项目ID或@项目ID；留空表示当前群已绑定的项目
            limit(number): 取几条，默认 10，上限 30
            with_image(boolean): 是否顺便发一张清单图，默认 true
        """
        if not self.client.has_token:
            return "未配置 Token，读不了有疑问词条。"
        reason = self.permission.deny_reason(event, need="read")
        if reason:
            return reason
        try:
            pid = await self._hint_pid(event, project)
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        limit = max(1, min(int(limit or 10), 30))
        try:
            data = await self.client.list_strings(pid, stage=2, page=1, page_size=limit)
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        rows = data.get("results", []) if isinstance(data, dict) else data
        if not rows:
            return "这个项目当前没有标为「有疑问」的词条。"

        lines = [f"有疑问词条 {len(rows)} 条："]
        for s in rows:
            fobj = s.get("file") or {}
            fname = fobj.get("name") if isinstance(fobj, dict) else str(fobj or "")
            lines.append(
                f"[{s.get('id')}] {fname}\n"
                f"  原文：{(s.get('original') or '').strip()[:160]}\n"
                f"  现译：{(s.get('translation') or '').strip()[:160] or '（空）'}"
            )
        text = "\n".join(lines)

        if with_image:
            out = os.path.join(self.store.dir, "cache", f"disputed_{pid}.png")
            items = [self._string_item(s) for s in rows]
            try:
                path = await asyncio.to_thread(
                    render_strings_card,
                    "有疑问词条",
                    f"#{pid}　共 {len(rows)} 条（最多取 {limit} 条）",
                    items,
                    out,
                    self._wide_logo_path(),
                    "",
                )
            except Exception:  # noqa: BLE001
                logger.warning("[ParaTranz] 有疑问清单图渲染失败", exc_info=True)
                path = None
            if path and await self._send_once_image(event, path):
                text += "\n（清单图已发到群里）"
        return text

    @filter.llm_tool(name="paratranz_string_context")
    async def tool_context(
        self,
        event: AstrMessageEvent,
        string_id: int,
        project: str = "",
        span: int = 3,
    ):
        """看某条词条在同一文件里的上下文（前后各几条），讨论剧情、人称、前后文语气时用。

        Args:
            string_id(number): 词条ID
            project(string): 项目ID或@项目ID；留空表示当前群已绑定的项目
            span(number): 前后各取几条，默认 3，上限 10
        """
        if not self.client.has_token:
            return "未配置 Token，读不了词条。"
        reason = self.permission.deny_reason(event, need="read")
        if reason:
            return reason
        try:
            pid = await self._hint_pid(event, project)
            info = await self.client.get_string(pid, int(string_id))
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        if not isinstance(info, dict):
            return "没拿到这条词条。"
        fobj = info.get("file") or {}
        file_id = fobj.get("id") if isinstance(fobj, dict) else fobj
        fname = fobj.get("name") if isinstance(fobj, dict) else ""
        span = max(1, min(int(span or 3), 10))

        rows: list[dict] = []
        if file_id:
            try:
                data = await self.client.list_strings(
                    pid, file_id=int(file_id), page=1, page_size=800
                )
                rows = data.get("results", []) if isinstance(data, dict) else data
            except ParaTranzError:
                rows = []
        ids = [int(r.get("id") or 0) for r in rows]
        target = int(string_id)
        if target in ids:
            i = ids.index(target)
            window = rows[max(0, i - span) : i + span + 1]
        else:
            window = [info]

        out_lines = [f"词条 {target}　文件 {fname or file_id or '?'}　上下文："]
        for r in window:
            mark = "▶" if int(r.get("id") or 0) == target else " "
            stage = STAGE_NAMES.get(r.get("stage"), r.get("stage"))
            out_lines.append(
                f"{mark} [{r.get('id')}] {stage}\n"
                f"   原：{(r.get('original') or '').strip()[:160]}\n"
                f"   译：{(r.get('translation') or '').strip()[:160] or '（空）'}"
            )
        return "\n".join(out_lines)

    @filter.llm_tool(name="paratranz_propose_translation")
    async def tool_propose_translation(
        self,
        event: AstrMessageEvent,
        string_id: int,
        translation: str,
        project: str = "",
    ):
        """提交一条词条译文修改。不会立刻写入 ParaTranz：先生成一条待确认记录（带哈希ID），
        由有写权限的人在群里点确认按钮，或回复「/pt 确认 验证码」才真正写入。

        Args:
            string_id(number): 要修改的词条ID
            translation(string): 新的译文
            project(string): 项目ID或@项目ID；留空表示当前群已绑定的项目
        """
        if not self.client.has_token:
            return "未配置 Token，读不了原译文也改不了。"
        try:
            pid = await self._hint_pid(event, project)
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        return await self._propose(event, pid, [(int(string_id), str(translation))])

    @filter.llm_tool(name="paratranz_set_stage")
    async def tool_set_stage(
        self,
        event: AstrMessageEvent,
        string_id: str = "",
        stage: str = "",
        project: str = "",
        regex: str = "",
    ):
        """改词条状态（标有疑问、标已检查、退回已翻译等）。支持三种方式：
        1. 单条：传 string_id=12345
        2. 批量：传 string_id=12345,67890,11111（逗号分隔）
        3. 按正则批量：传 regex=正则表达式，自动搜索匹配的词条并全部改掉，不用回传ID
        只改状态，不动原文，也不动译文。

        Args:
            string_id(string): 词条ID，单个如 12345，或多个用逗号分隔如 12345,67890,11111。用 regex 时可留空。
            stage(string): 目标状态，可写 未翻译/已翻译/有疑问/已检查/已审核/锁定 或数字
            project(string): 项目ID或@项目ID；留空表示当前群已绑定的项目
            regex(string): 正则表达式，在原文里服务端正则匹配。传了这个就自动搜索并批量改，不需要 string_id。
        """
        if not self.client.has_token:
            return "未配置 Token，改不了。"
        reason = self.permission.deny_reason(event, need="write")
        if reason:
            return reason
        text = str(stage).strip()
        value = int(text) if text.lstrip("-").isdigit() else STAGE_ALIASES.get(text)
        if value is None:
            return "状态不认识，可用：" + "、".join(STAGE_ALIASES)
        name = STAGE_NAMES.get(value, str(value))
        try:
            pid = await self._hint_pid(event, project)
            # 按正则批量：先搜再改，ID 不经过模型
            regex_pat = str(regex or "").strip()
            if regex_pat:
                ids: list[int] = []
                for pg in range(1, 20):
                    data = await self.client.list_strings(
                        pid,
                        page=pg,
                        page_size=800,
                        regex=regex_pat,
                        regex_negate=False,
                    )
                    rows = (
                        data.get("results", [])
                        if isinstance(data, dict)
                        else (data or [])
                    )
                    ids.extend(int(r["id"]) for r in rows if r.get("id"))
                    if len(rows) < 800:
                        break
                if not ids:
                    return f"正则 `{regex_pat}` 没有匹配到任何词条，什么都没改。"
                await self.client.set_stages_batch(
                    pid, ids, int(value), allow_write=self.enable_write
                )
                return (
                    f"正则 `{regex_pat}` 匹配到 {len(ids)} 条，已全部标成「{name}」。"
                )
            # 按 ID 改：单条或逗号分隔
            raw_ids = [
                x.strip()
                for x in str(string_id).replace("，", ",").split(",")
                if x.strip()
            ]
            ids = [int(x) for x in raw_ids if x.lstrip("-").isdigit()]
            if not ids:
                return "没解析到有效词条ID，也没给 regex。"
            if len(ids) == 1:
                await self.client.set_string_stage(
                    pid, ids[0], int(value), allow_write=self.enable_write
                )
                return f"词条 {ids[0]} 已标成「{name}」。"
            await self.client.set_stages_batch(
                pid, ids, int(value), allow_write=self.enable_write
            )
            return f"{len(ids)} 条词条已标成「{name}」。"
        except ParaTranzError as exc:
            return f"改状态失败：{exc.message}"

    @filter.llm_tool(name="paratranz_replace_translations")
    async def tool_replace_translations(
        self,
        event: AstrMessageEvent,
        find: str,
        replace: str = "",
        project: str = "",
        use_regex: bool = False,
        stage: int = -99,
    ):
        """在译文里批量查找替换（类似网页的「替换」功能）。先搜索译文里含 find 的词条，
        把每条译文里的 find 换成 replace，然后生成差分图加确认按钮；有写权限的人确认后才批量写入。
        不会立刻写入，走确认流程，安全。

        Args:
            find(string): 要查找的文本，译文里包含它的词条会被处理
            replace(string): 替换成什么，留空表示删掉 find
            project(string): 项目ID或@项目ID；留空表示当前群已绑定的项目
            use_regex(bool): true 时 find 按正则处理，replace 里可以用 \1 \2 引用捕获组
            stage(number): 按状态过滤，只处理该状态的词条；-99 表示不限
        """
        if not self.client.has_token:
            return "未配置 Token，改不了。"
        reason = self.permission.deny_reason(event, need="admin")
        if reason:
            return f"{reason}（批量替换只认管理员）"
        find = str(find or "").strip()
        if not find:
            return "find 不能为空，要找什么？"
        try:
            pid = await self._hint_pid(event, project)
            # 找出译文里含 find 的词条
            if use_regex:
                data = await self.client.list_strings(
                    pid,
                    stage=None if int(stage) == -99 else int(stage),
                    page=1,
                    page_size=800,
                    regex=find,
                    regex_on="translation",
                    regex_negate=False,
                )
            else:
                data = await self.client.list_strings(
                    pid,
                    stage=None if int(stage) == -99 else int(stage),
                    page=1,
                    page_size=800,
                    translation=find,
                )
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        rows = data.get("results", []) if isinstance(data, dict) else (data or [])
        if not rows:
            return "没找到译文里含这个的词条，什么都没改。"
        # 逐条算替换结果
        items: list[tuple[int, str]] = []
        if use_regex:
            try:
                pat = re.compile(find)
            except re.error as exc:
                return f"正则写错了：{exc}"
            for row in rows:
                tr = str(row.get("translation") or "")
                if not tr:
                    continue
                new = pat.sub(replace, tr)
                if new != tr:
                    items.append((int(row["id"]), new))
        else:
            for row in rows:
                tr = str(row.get("translation") or "")
                if not tr or find not in tr:
                    continue
                new = tr.replace(find, replace)
                if new != tr:
                    items.append((int(row["id"]), new))
        if not items:
            return "搜到了词条，但替换后没有变化，可能译文已经是目标内容了。"
        if len(items) > BATCH_MAX:
            return f"一次最多替换 {BATCH_MAX} 条，这次匹配到 {len(items)} 条，条件收窄点再试。"
        note = f"替换译文：{find} → {replace or '（删除）'}"
        return await self._propose(event, pid, items, note=note)

    @filter.llm_tool(name="paratranz_propose_batch")
    async def tool_propose_batch(
        self, event: AstrMessageEvent, edits: str, project: str = ""
    ):
        """批量提交多条词条译文修改。不会立刻写入，只生成一条待确认记录（带哈希ID）+ 一张红绿差分图，
        有写权限的人点确认按钮或回复「/pt 确认 验证码」后才批量写入；单条失败不影响其他。

        Args:
            edits(string): 每行「词条ID|新译文」，也接受 JSON 数组 [{"id":1,"translation":"..."}]
            project(string): 项目ID或@项目ID；留空表示当前群已绑定的项目
        """
        if not self.client.has_token:
            return "未配置 Token，读不了原译文也改不了。"
        reason = self.permission.deny_reason(event, need="admin")
        if reason:
            return f"{reason}（批量改动只认管理员）"
        items: list[tuple[int, str]] = []
        text = (edits or "").strip()
        if text.startswith("["):
            try:
                data = json.loads(text)
            except Exception:  # noqa: BLE001
                return "edits 的 JSON 解析失败，检查一下引号和逗号。"
            for row in data if isinstance(data, list) else []:
                if not isinstance(row, dict):
                    continue
                sid = row.get("id") or row.get("string_id")
                tr = row.get("translation")
                if str(sid).isdigit() and tr is not None:
                    items.append((int(sid), str(tr)))
        else:
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                sep = "|" if "|" in line else ("=>" if "=>" in line else None)
                if not sep:
                    return "每行要写成「词条ID|新译文」，比如 1048017907|因为是老身叫来的啊！"
                left, right = line.split(sep, 1)
                left = left.strip()
                if not left.isdigit() or not right.strip():
                    return f"这一行看不懂：{line[:40]}"
                items.append((int(left), right.strip()))
        if not items:
            return "没有解析出任何修改。"
        if len(items) > BATCH_MAX:
            return f"一次最多提交 {BATCH_MAX} 条，你给了 {len(items)} 条，分两批吧。"
        try:
            pid = await self._hint_pid(event, project)
        except ParaTranzError as exc:
            return f"查询失败：{exc.message}"
        return await self._propose(event, pid, items)
