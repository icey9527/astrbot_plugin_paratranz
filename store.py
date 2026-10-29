"""群绑定与授权信息的本地持久化（JSON）。

存放位置：<data>/plugin_data/astrbot_plugin_paratranz/state.json
结构：
{
  "binds":    {"<群号>": [12352, 20374]},
  "grants":   {"<QQ号>": {"role": "translator", "by": "<管理员QQ>", "at": "2026-10-23T17:20:00"}},
  "projects": {"20374": {"name": "女皇之刃＆女皇之门", "at": "2026-10-23T19:56:00"}}
}

一个群可以绑多个项目（列表），老版本的单个整数会自动升级成列表。
projects 只是缓存项目名，省得菜单每次都要请求一遍接口。

role 只有两种：
  viewer     —— 能读词条正文（还得插件本身配了 Token）
  translator —— 在上面基础上能改词条译文
管理员不写进 grants，直接走 AstrBot 管理员 / admin_ids。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except Exception:  # noqa: BLE001
    get_astrbot_plugin_data_path = None

ROLES = {"viewer": "只读词条", "translator": "可改词条"}


def _as_pid_list(value) -> list[int]:
    """把老的 {"群": 12352} 和新的 {"群": [12352]} 统一成列表。"""
    if value is None:
        return []
    if isinstance(value, (int, str)):
        text = str(value)
        return [int(text)] if text.isdigit() else []
    if isinstance(value, list):
        out: list[int] = []
        for item in value:
            if isinstance(item, int):
                out.append(item)
            elif isinstance(item, str) and item.isdigit():
                out.append(int(item))
            elif isinstance(item, dict) and str(item.get("id", "")).isdigit():
                out.append(int(item["id"]))
        return out
    return []


class StateStore:
    def __init__(self, filename: str = "state.json"):
        base = self._resolve_base()
        self.dir = os.path.join(base, "astrbot_plugin_paratranz")
        self.path = os.path.join(self.dir, filename)
        self._lock = asyncio.Lock()
        self._data: dict = {"binds": {}, "grants": {}, "projects": {}}
        self.sub_dir = os.path.join(self.dir, "submissions")
        self._loaded = False

    @staticmethod
    def _resolve_base() -> str:
        """定位 <data>/plugin_data。

        优先 ASTRBOT_ROOT，其次由本文件路径反推
        （本文件在 <data>/plugins/<插件名>/ 下），最后才用 AstrBot 的辅助函数。
        反推这一层是为了不受启动时 cwd 影响。
        """
        env = os.environ.get("ASTRBOT_ROOT", "").strip()
        if env:
            return os.path.join(os.path.realpath(env), "data", "plugin_data")
        here = os.path.realpath(__file__)
        plugins_dir = os.path.dirname(os.path.dirname(here))
        if os.path.basename(plugins_dir) == "plugins":
            return os.path.join(os.path.dirname(plugins_dir), "plugin_data")
        if get_astrbot_plugin_data_path:
            try:
                return get_astrbot_plugin_data_path()
            except Exception:  # noqa: BLE001
                pass
        return os.path.join(os.getcwd(), "data", "plugin_data")

    # ---------------------------------------------------------------- 读写

    def _load_sync(self) -> None:
        if self._loaded:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self._data["binds"] = raw.get("binds") or {}
                self._data["grants"] = raw.get("grants") or {}
                self._data["projects"] = raw.get("projects") or {}
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            # 文件坏了就当空的，别把插件搞崩
            self._data = {"binds": {}, "grants": {}, "projects": {}}
        self._loaded = True

    def _save_sync(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    async def _mutate(self, fn) -> None:
        async with self._lock:
            self._load_sync()
            fn()
            await asyncio.to_thread(self._save_sync)

    # ---------------------------------------------------------------- 群绑定

    def binds_of(self, group_id: str) -> list[int]:
        """这个群绑定的所有项目 ID。"""
        self._load_sync()
        gid = str(group_id or "").strip()
        if not gid:
            return []
        return _as_pid_list(self._data["binds"].get(gid))

    def bind_of(self, group_id: str) -> int | None:
        """兼容老代码：取第一个绑定的项目。"""
        pids = self.binds_of(group_id)
        return pids[0] if pids else None

    async def bind(self, group_id: str, project_id: int) -> bool:
        """追加绑定，返回是否新加的。"""
        added = {"v": False}

        def _do() -> None:
            gid = str(group_id)
            pids = _as_pid_list(self._data["binds"].get(gid))
            if int(project_id) not in pids:
                pids.append(int(project_id))
                added["v"] = True
            self._data["binds"][gid] = pids

        await self._mutate(_do)
        return added["v"]

    async def unbind(self, group_id: str, project_id: int | None = None) -> int:
        """解绑。给了项目 ID 就只删这个，没给就把整个群清空。返回解绑数量。"""
        removed = {"v": 0}

        def _do() -> None:
            gid = str(group_id)
            pids = _as_pid_list(self._data["binds"].get(gid))
            if project_id is None:
                removed["v"] = len(pids)
                self._data["binds"].pop(gid, None)
                return
            pid = int(project_id)
            if pid in pids:
                pids.remove(pid)
                removed["v"] = 1
            if pids:
                self._data["binds"][gid] = pids
            else:
                self._data["binds"].pop(gid, None)

        await self._mutate(_do)
        return removed["v"]

    def all_binds(self) -> dict[str, list[int]]:
        self._load_sync()
        return {str(gid): _as_pid_list(v) for gid, v in self._data["binds"].items()}

    # ---------------------------------------------------------------- 项目名缓存

    # ---------------------------------------------------------------- 通知水位线
    def notify_watermark(self) -> int:
        """上次处理到的通知ID。重载插件不能丢，不然期间来的通知会被当成旧消息吞掉。"""
        self._load_sync()
        try:
            return int(self._data.get("notify_seen") or 0)
        except (TypeError, ValueError):
            return 0

    async def set_notify_watermark(self, value: int) -> None:
        def _do() -> None:
            self._data["notify_seen"] = int(value or 0)

        await self._mutate(_do)

    def platform_id(self) -> str:
        self._load_sync()
        return str(self._data.get("platform_id") or "")

    async def set_platform_id(self, platform_id: str) -> None:
        pid = str(platform_id or "").strip()
        if not pid:
            return

        def _do() -> None:
            self._data["platform_id"] = pid

        await self._mutate(_do)

    def group_name(self, group_id: str) -> str:
        self._load_sync()
        info = (self._data.get("group_names") or {}).get(str(group_id)) or {}
        return str(info.get("name") or "")

    async def set_group_name(self, group_id: str, name: str) -> None:
        gid, name = str(group_id), str(name or "").strip()
        if not gid or not name:
            return

        def _do() -> None:
            self._data.setdefault("group_names", {})[gid] = {
                "name": name,
                "at": datetime.now().isoformat(timespec="seconds"),
            }

        await self._mutate(_do)

    def all_group_names(self) -> dict[str, str]:
        self._load_sync()
        return {
            gid: str(info.get("name") or "")
            for gid, info in (self._data.get("group_names") or {}).items()
        }

    def project_name(self, project_id: int) -> str:
        self._load_sync()
        info = self._data["projects"].get(str(project_id)) or {}
        return str(info.get("name") or "")

    def all_project_names(self) -> dict[str, str]:
        self._load_sync()
        return {
            pid: str(info.get("name") or "")
            for pid, info in (self._data.get("projects") or {}).items()
        }

    async def set_project_name(self, project_id: int, name: str) -> None:
        if not name:
            return

        def _do() -> None:
            self._data["projects"][str(project_id)] = {
                "name": str(name),
                "at": datetime.now().isoformat(timespec="seconds"),
            }

        await self._mutate(_do)

    # ---------------------------------------------------------------- 授权

    def role_of(self, user_id: str) -> str | None:
        self._load_sync()
        info = self._data["grants"].get(str(user_id or "").strip())
        return (info or {}).get("role")

    async def grant(self, user_id: str, role: str, by: str) -> None:
        def _do() -> None:
            self._data["grants"][str(user_id)] = {
                "role": role,
                "by": str(by),
                "at": datetime.now().isoformat(timespec="seconds"),
            }

        await self._mutate(_do)

    async def revoke(self, user_id: str) -> bool:
        existed = {"v": False}

        def _do() -> None:
            existed["v"] = str(user_id) in self._data["grants"]
            self._data["grants"].pop(str(user_id), None)

        await self._mutate(_do)
        return existed["v"]

    def all_grants(self) -> dict[str, dict]:
        self._load_sync()
        return dict(self._data["grants"])

    # ---------------------------------------------------------------- 提交记录
    # 一条提交存到 submissions/<项目ID>.json，各项目互不干扰、删项目就删文件。

    def _sub_path(self, project_id: int) -> str:
        return os.path.join(self.sub_dir, f"{int(project_id)}.json")

    def _load_subs(self, project_id: int) -> dict:
        path = self._sub_path(project_id)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:  # noqa: BLE001
            return {}

    def _save_subs(self, project_id: int, data: dict) -> None:
        os.makedirs(self.sub_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.sub_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._sub_path(project_id))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _all_sub_files(self) -> list[int]:
        if not os.path.isdir(self.sub_dir):
            return []
        out: list[int] = []
        for name in os.listdir(self.sub_dir):
            if name.endswith(".json") and name[:-5].isdigit():
                out.append(int(name[:-5]))
        return sorted(out)

    def add_submission(self, record: dict) -> None:
        pid = int(record["project"])
        data = self._load_subs(pid)
        data[str(record["hash"])] = record
        self._save_subs(pid, data)

    def update_submission(self, sub_hash: str, **fields) -> dict | None:
        for pid in self._all_sub_files():
            data = self._load_subs(pid)
            rec = data.get(str(sub_hash))
            if rec is not None:
                rec.update(fields)
                self._save_subs(pid, data)
                return rec
        return None

    def get_submission(self, sub_hash: str, project: int | None = None) -> dict | None:
        pids = [int(project)] if project is not None else self._all_sub_files()
        for pid in pids:
            rec = self._load_subs(pid).get(str(sub_hash))
            if rec is not None:
                return rec
        return None

    def find_submission_by_code(
        self, code: str, projects: list[int] | None = None
    ) -> dict | None:
        code = str(code or "").strip()
        pids = [int(p) for p in projects] if projects else self._all_sub_files()
        for pid in pids:
            for rec in self._load_subs(pid).values():
                if str(rec.get("code")) == code and rec.get("status") == "pending":
                    return rec
        return None

    def list_submissions(
        self,
        limit: int = 20,
        status: str | None = None,
        projects: list[int] | None = None,
        page: int = 1,
    ) -> tuple[list[dict], int]:
        """返回 (本页记录, 总数)。projects 为空表示所有项目。"""
        pids = [int(p) for p in projects] if projects else self._all_sub_files()
        items: list[dict] = []
        for pid in pids:
            for rec in self._load_subs(pid).values():
                if status and rec.get("status") != status:
                    continue
                items.append(rec)
        items.sort(key=lambda r: float(r.get("created_at") or 0), reverse=True)
        total = len(items)
        page = max(1, int(page or 1))
        start = (page - 1) * max(1, int(limit or 20))
        return items[start : start + max(1, int(limit or 20))], total

    def purge_submissions(
        self,
        pending_ttl: float,
        projects: list[int] | None = None,
        now: float | None = None,
    ) -> int:
        """清掉过期的待确认记录（已提交的记录永久保留）。"""
        import time as _time

        now = now or _time.time()
        pids = [int(p) for p in projects] if projects else self._all_sub_files()
        removed = 0
        for pid in pids:
            data = self._load_subs(pid)
            dead = [
                h
                for h, r in data.items()
                if r.get("status") == "pending"
                and now - float(r.get("created_at") or 0) > pending_ttl
            ]
            if dead:
                for h in dead:
                    data.pop(h, None)
                self._save_subs(pid, data)
                removed += len(dead)
        return removed
