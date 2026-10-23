"""QQ JSON 卡片（OneBot v11 的 json 段）。

AstrBot 4.x 的 aiocqhttp 适配层里没有专门的卡片组件，Comp.Json 序列化出来是
{"type":"json","data":{"data":{...字典...}}}，而 OneBot 要求 data.data 是一段
JSON 字符串，直接发大概率不认。所以这里绕过组件，走 bot.call_action 手搓
json 段，失败就回退纯文本。
"""

from __future__ import annotations

import json
import random
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

NEWS_VIEW_TEMPLATE = {
    "app": "com.tencent.structmsg",
    "desc": "0",
    "ver": "0.0.0.1",
    "view": "news",
    "prompt": "[ParaTranz]",
}


def build_news_card(
    title: str,
    desc: str,
    tag: str = "ParaTranz",
    jump_url: str = "https://paratranz.cn",
    preview: str = "https://paratranz.cn/favicon.png",
    prompt: str = "[ParaTranz]",
) -> dict[str, Any]:
    card = dict(NEWS_VIEW_TEMPLATE)
    card["prompt"] = prompt
    card["meta"] = {
        "news": {
            "title": title,
            "desc": desc,
            "tag": tag,
            "jumpUrl": jump_url,
            "preview": preview,
        }
    }
    return card


def build_progress_card(
    project: dict, summary: dict[str, int], files_count: int, percent: float
) -> dict[str, Any]:
    name = project.get("name") or f"项目 {project.get('id')}"
    total = summary.get("total", 0)
    translated = summary.get("translated", 0)
    checked = summary.get("checked", 0)
    reviewed = summary.get("reviewed", 0) + summary.get("locked", 0)
    disputed = summary.get("disputed", 0)
    desc = (
        f"翻译 {translated}/{total}（{percent:.1f}%）\n"
        f"已检查 {checked}　已审核 {reviewed}　有疑问 {disputed}\n"
        f"文件数 {files_count}"
    )
    return build_news_card(
        title=name,
        desc=desc,
        tag="翻译进度",
        jump_url=f"https://paratranz.cn/projects/{project.get('id')}",
        prompt=f"[ParaTranz] {name} 当前进度 {percent:.1f}%",
    )


async def try_send_card(event: AstrMessageEvent, card: dict[str, Any]) -> bool:
    """能发卡片就发，发不了返回 False 让上层走文本。"""
    platform = ""
    try:
        platform = event.get_platform_name() or ""
    except Exception:  # noqa: BLE001
        platform = ""

    if platform != "aiocqhttp":
        return False

    bot = getattr(event, "bot", None)
    if bot is None:
        return False

    payload = [{"type": "json", "data": {"data": json.dumps(card, ensure_ascii=False)}}]
    group_id = str(event.get_group_id() or "").strip()
    sender_id = str(event.get_sender_id() or "").strip()
    try:
        if group_id.isdigit():
            await bot.call_action(
                "send_group_msg", group_id=int(group_id), message=payload
            )
            return True
        if sender_id.isdigit():
            await bot.call_action(
                "send_private_msg", user_id=int(sender_id), message=payload
            )
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ParaTranz] 卡片发送失败，回退文本：{exc}")
    return False


# ---------------------------------------------------------------- 官方机器人按钮
#
# 真按钮是 QQ 开放平台（qq_official 适配器）的能力，走 /v2/groups/{openid}/messages
# 的 markdown + keyboard 组合。普通协议端（NapCat、LLOneBot 等）没有这个接口，
# 客户端也渲染不出来，只能发图片菜单。


def button(
    label: str,
    data: str,
    style: int = 1,
    btn_id: str | None = None,
    enter: bool = True,
) -> dict:
    """一个指令按钮：点一下客户端自动 @bot + 把 data 发出去。

    注意 action.type 的官方语义：
      0 = 跳转，1 = 回调（要公网后端，内网必报「请求第三方失败」），2 = 指令。
    这里一律用 2。enter=True 点一下就直接发；False 只把指令填进输入框，
    留给用户补参数再用（术语、搜索这类需要关键词的按钮就靠这个）。
    """
    return {
        "id": btn_id or f"pt_{abs(hash((label, data))) % 10**10}",
        "render_data": {"label": label, "visited_label": label, "style": style},
        "action": {
            "type": 2,
            "permission": {"type": 2},
            "data": data,
            "enter": enter,
            "reply": False,
            "unsupport_tips": "QQ 版本太低，请升级后再试",
        },
    }


def build_menu_keyboard() -> dict:
    """菜单按钮：一行三个，只放常用入口。

    文件进度 / 术语查词 / 词条列表 这三块从菜单里撤了（指令和 LLM 工具还在，
    用得着的时候直接发 /pt 文件、/pt 术语、/pt 词条）。
    """
    rows = [
        [
            button("项目进度", "/pt 项目"),
            button("任务进度", "/pt 任务"),
            button("成员贡献", "/pt 成员"),
        ],
        [
            button("提交记录", "/pt 记录"),
            button("我的权限", "/pt 我", style=0),
            button("搜索项目", "/pt 搜索 ", style=0, enter=False),
        ],
        [
            button("查看绑定", "/pt 绑定列表", style=0),
            button("授权名单", "/pt 名单", style=0),
            button("完整指令", "/pt 指令", style=0),
        ],
    ]
    return {"rows": [{"buttons": row} for row in rows]}


def build_project_keyboard(action: str, projects: list[tuple[int, str]]) -> dict:
    """多项目时让用户点一个：按钮直接发「/pt <action> @项目ID」。

    按钮上只写项目名（不带 #ID），名字太长就从尾部截断加省略号；
    ID 藏在按钮数据里，点下去就是 /pt <action> @ID。
    """
    # 一行一个按钮，名字能写全；项目太多（>5）时退成一行两个，免得行数超限
    per_row = 1 if len(projects) <= 5 else 2
    max_len = 20 if per_row == 1 else 12
    rows: list[list[dict]] = []
    row: list[dict] = []
    for pid, name in projects:
        label = str(name or "").strip() or f"项目{pid}"
        if len(label) > max_len:
            label = label[: max_len - 1] + "…"
        row.append(
            button(label, f"/pt {action} @{pid}", style=0, btn_id=f"pt_pick_{pid}")
        )
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"rows": [{"buttons": r} for r in rows]}


def build_records_keyboard(
    page: int, pages: int, hashes: list[str], project: int | None = None
) -> dict:
    """提交记录页的键盘：每条哈希一颗按钮跳到详情，最后一行是翻页。

    给了 project 就带上 @项目ID，翻页 / 详情都锁在同一个项目里。
    """
    prefix = f"@{project} " if project else ""
    rows: list[list[dict]] = []
    row: list[dict] = []
    for h in hashes:
        row.append(
            button(f"#{h}", f"/pt 记录 {prefix}{h}", style=0, btn_id=f"pt_rec_{h}")
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav: list[dict] = []
    if page > 1:
        nav.append(button("上一页", f"/pt 记录 {prefix}{page - 1}", style=0))
    if page < pages:
        nav.append(button("下一页", f"/pt 记录 {prefix}{page + 1}", style=0))
    if nav:
        rows.append(nav)
    return {"rows": [{"buttons": r} for r in rows]}


def build_confirm_keyboard(code: str, ok_label: str = "确认写入") -> dict:
    """写操作确认按钮：一行两颗，点「确认」等于发 /pt 确认 <码>。

    群聊里 action.enter 不生效（官方限制，仅单聊自动发），所以点了会把指令
    填进输入框，用户再点一次发送；单聊则会直接发出。
    """
    return {
        "rows": [
            {
                "buttons": [
                    button(ok_label, f"/pt 确认 {code}", btn_id=f"pt_ok_{code}"),
                    button("取消", "/pt 取消", style=0, btn_id=f"pt_no_{code}"),
                ]
            }
        ]
    }


def build_revert_keyboard(sub_hash: str) -> dict:
    """写入成功后给的撤回按钮（管理员才点得动）。"""
    return {
        "rows": [
            {
                "buttons": [
                    button(
                        "撤回这次提交",
                        f"/pt 撤回 {sub_hash}",
                        style=0,
                        btn_id=f"pt_rev_{sub_hash}",
                    )
                ]
            }
        ]
    }


async def try_send_keyboard_menu(
    event: AstrMessageEvent, markdown: str, keyboard: dict
) -> bool:
    ok, detail = await send_keyboard(event, markdown, keyboard)
    if not ok and detail:
        logger.warning(f"[ParaTranz] 按钮菜单发送失败，回退图片/文本：{detail}")
    return ok


def _seq() -> int:
    global _MSG_SEQ
    _MSG_SEQ += 1
    return _MSG_SEQ


_MSG_SEQ = random.randint(1, 1000)


def _group_openid(event: AstrMessageEvent) -> str:
    """官方适配器的群 openid 优先从 raw_message 拿，get_group_id 有时是空。"""
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    for attr in ("group_openid", "group_id"):
        val = getattr(raw, attr, None)
        if val:
            return str(val)
    return str(event.get_group_id() or "").strip()


async def send_keyboard(
    event: AstrMessageEvent, markdown: str, keyboard: dict
) -> tuple[bool, str]:
    """真正去调官方接口，返回 (是否成功, 失败原因)。"""
    try:
        platform = event.get_platform_name() or ""
    except Exception:  # noqa: BLE001
        platform = ""
    if platform != "qq_official":
        return False, f"平台是 {platform}，不是 qq_official"

    bot = getattr(event, "bot", None)
    api = getattr(bot, "api", None)
    if api is None:
        return False, "拿不到 bot.api"

    group_id = _group_openid(event)
    sender_id = str(event.get_sender_id() or "").strip()
    msg_id = getattr(getattr(event, "message_obj", None), "message_id", None)

    # QQ 开放平台要求 keyboard 外面套一层 content
    kb = keyboard if "content" in keyboard else {"content": keyboard}
    kwargs = {
        "msg_type": 2,
        "markdown": {"content": markdown},
        "keyboard": kb,
        "msg_id": msg_id,
        "msg_seq": _seq(),
    }
    try:
        if group_id:
            await api.post_group_message(group_openid=group_id, **kwargs)
            return True, ""
        if sender_id:
            await api.post_c2c_message(openid=sender_id, **kwargs)
            return True, ""
        return False, "既没有 group_id 也没有 sender_id"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
