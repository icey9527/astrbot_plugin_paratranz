"""权限与群绑定。

四道闸门：
1. 群白名单：allowed_groups 非空时，只在这些群里响应。
2. 管理员校验：管理类指令（绑定、授权、撤销）只认 AstrBot 管理员或 admin_ids。
3. 授权等级：viewer 可读词条正文，translator 可改词条译文，两者都由管理员用 /pt grant 赋权。
4. 二次确认：写操作生成一次性确认码，超时作废；确认必须由有写权限的人完成。
"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent

ROLE_ADMIN = "admin"
ROLE_TRANSLATOR = "translator"
ROLE_VIEWER = "viewer"


class Permission:
    def __init__(self, config: dict, store=None):
        self.admin_ids = {
            str(i).strip() for i in (config.get("admin_ids") or []) if str(i).strip()
        }
        self.allowed_groups = {
            str(i).strip()
            for i in (config.get("allowed_groups") or [])
            if str(i).strip()
        }
        self.admin_only_write = bool(config.get("admin_only_write", True))
        # 读词条正文是否需要授权：关掉之后谁都能看，写操作照旧按等级校验
        self.read_requires_grant = bool(config.get("read_requires_grant", True))
        self.store = store

    # ---------------------------------------------------------------- 群

    def group_allowed(self, event: AstrMessageEvent) -> bool:
        if not self.allowed_groups:
            return True
        gid = str(event.get_group_id() or "").strip()
        if not gid:  # 私聊放行，群聊严格按白名单
            return True
        return gid in self.allowed_groups

    # ---------------------------------------------------------------- 人

    def is_admin(self, event: AstrMessageEvent) -> bool:
        if str(event.get_sender_id()) in self.admin_ids:
            return True
        try:
            return bool(event.is_admin())
        except Exception:  # noqa: BLE001
            return False

    def role(self, event: AstrMessageEvent) -> str | None:
        if self.is_admin(event):
            return ROLE_ADMIN
        if self.store is not None:
            return self.store.role_of(str(event.get_sender_id()))
        return None

    def can_read_strings(self, event: AstrMessageEvent) -> bool:
        """词条正文：管理员 / 已授权的人 / 允许公开阅读时所有人。"""
        if not self.read_requires_grant:
            return True
        if not self.admin_only_write:
            return True
        return self.role(event) in (ROLE_ADMIN, ROLE_TRANSLATOR, ROLE_VIEWER)

    def can_write(self, event: AstrMessageEvent) -> bool:
        if not self.admin_only_write:
            return True
        return self.role(event) in (ROLE_ADMIN, ROLE_TRANSLATOR)

    # ---------------------------------------------------------------- 报错

    def deny_reason(self, event: AstrMessageEvent, need: str = "write") -> str:
        if not self.group_allowed(event):
            return "这个群不在白名单里，不接活。"
        if need == "write" and not self.can_write(event):
            return "改词条要管理员授权，你这号还没被赋权，别急。"
        if need == "read" and not self.can_read_strings(event):
            return "词条正文只给已授权的号看，先让管理员 /pt grant 你一下。"
        if need == "admin" and not self.is_admin(event):
            return "这属于管理操作，只有管理员能动。"
        return ""
