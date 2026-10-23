from __future__ import annotations

import asyncio
import hashlib
import json
import os
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
from .store import ROLES, StateStore

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
STRINGS_LIMIT_MAX = 50
BATCH_MAX = 100
TEMP_IMAGE_TTL = 60
PENDING_TTL = 3 * 86400
PLUGIN_VERSION = "0.18.1"
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
    "0.18.1",
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
        )

    async def initialize(self):
        mode = "Token 已配置" if self.client.has_token else "公开只读模式（无 Token）"
        write_mode = "允许写" if self.enable_write else "禁止写"
        binds = len(self.store.all_binds())
        grants = len(self.store.all_grants())
        logger.info(
            f"[ParaTranz] v{PLUGIN_VERSION} 就绪：{mode} / {write_mode} / "
            f"已绑定 {binds} 个群 / 已授权 {grants} 人"
        )

    async def terminate(self):
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
                "· /pt 授权 <QQ号> [viewer|translator]",
                "· /pt 撤销 <QQ号>",
                "· /pt 授权列表",
                "",
                "【提交与确认】",
                "· /pt 修改 <词条ID> <译文>　发起一条修改（谁都能发起）",
                "· /pt 确认 <确认码>　有写权限的人点/发这个才真写入",
                "· /pt cancel　放弃自己发起的待确认",
                "· /pt 记录 [页码|哈希ID]　提交记录（每页 10 条，点哈希看详情图）",
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
        if pids:
            lines.append(
                "· 本群绑定：" + "、".join(self._project_label(p) for p in pids)
            )
            if len(pids) > 1:
                lines.append("· 多个项目，查询时用 @项目ID 指定")
        else:
            lines.append("· 本群绑定：未绑定（管理员先 /pt 绑定 <项目ID>）")
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

    async def _cmd_binds(self, event: AstrMessageEvent, rest: list[str]):
        binds = self.store.all_binds()
        binds = {gid: pids for gid, pids in binds.items() if pids}
        if not binds:
            return "text", "还没有任何绑定。用 /pt 绑定 <项目ID> 绑一个。"
        lines = [f"共 {len(binds)} 个群、{sum(len(p) for p in binds.values())} 条绑定"]
        for gid, pids in sorted(binds.items()):
            lines.append(
                f"· 群 {gid} → " + "、".join(self._project_label(pid) for pid in pids)
            )
        return "text", "\n".join(lines)

    async def _cmd_grant(self, event: AstrMessageEvent, rest: list[str]):
        reason = self.permission.deny_reason(event, need="admin")
        if reason:
            return "text", reason
        if not rest or not rest[0].isdigit():
            return "text", "用法：/pt grant <QQ号> [viewer|translator]，默认 translator"
        target = rest[0]
        role = rest[1].lower() if len(rest) > 1 else ROLE_TRANSLATOR
        if role not in (ROLE_VIEWER, ROLE_TRANSLATOR):
            return (
                "text",
                f"角色只有 viewer（{ROLES['viewer']}）和 translator（{ROLES['translator']}）。",
            )
        await self.store.grant(target, role, event.get_sender_id())
        return (
            "text",
            f"已给 {target} 授权：{ROLE_CN[role]}\n要用写操作还得插件配置里 enable_write 打开。",
        )

    async def _cmd_revoke(self, event: AstrMessageEvent, rest: list[str]):
        reason = self.permission.deny_reason(event, need="admin")
        if reason:
            return "text", reason
        if not rest or not rest[0].isdigit():
            return "text", "用法：/pt revoke <QQ号>"
        ok = await self.store.revoke(rest[0])
        return (
            "text",
            f"已撤销 {rest[0]} 的授权。" if ok else f"{rest[0]} 本来就没授权。",
        )

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
                f"{r.get('by_name') or r.get('by')} → "
                f"{r.get('approver_name') or r.get('approver') or '未确认'}　#{r.get('hash')}"
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
