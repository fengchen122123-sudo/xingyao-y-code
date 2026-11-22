from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

STATUS_RUNNING = "running"
STATUS_AWAITING_CONFIRM = "awaiting_confirm"
STATUS_ACHIEVED = "achieved"
STATUS_BLOCKED = "blocked"
STATUS_STOPPED = "stopped"
STATUS_INTERRUPTED = "interrupted"

VALID_STATUSES = frozenset(
    {
        STATUS_RUNNING,
        STATUS_AWAITING_CONFIRM,
        STATUS_ACHIEVED,
        STATUS_BLOCKED,
        STATUS_STOPPED,
        STATUS_INTERRUPTED,
    }
)

# 文件名里必须替换掉的字符：Windows 保留字符 + 路径分隔符。中文本身是合法的
# 文件名字符，因此保留原样——用户能在 goals 目录里一眼认出是哪个会话。
_ILLEGAL_CHARS = '<>:"/\\|?*'
_MAX_SLUG_LEN = 80

# Windows 保留设备名：即使带扩展名也不能作为文件名，会话刚好叫 "con" 时会写失败。
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


@dataclass
class GoalState:

    session: str
    goal: str
    check: str = ""
    max_rounds: int = 10
    round: int = 0
    status: str = STATUS_RUNNING
    # [{round: int, summary: str}]：只留摘要不留全文，避免历史把 JSON 撑大
    history: list = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    owner_pid: int = 0

    def is_terminal(self) -> bool:
        """是否已到终态（不再自动续跑）。"""
        return self.status in {
            STATUS_ACHIEVED,
            STATUS_BLOCKED,
            STATUS_STOPPED,
            STATUS_INTERRUPTED,
        }


def _digest(text: str) -> str:
    value = 0x811C9DC5
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 0x01000193) & 0xFFFFFFFF
    return f"{value:08x}"


def slugify_session(session: str) -> str:
    raw = "" if session is None else str(session)
    chars = []
    for ch in raw:
        if ch in _ILLEGAL_CHARS or ord(ch) < 32 or ord(ch) == 127:
            chars.append("_")
        elif ch.isspace():
            chars.append("_")
        else:
            chars.append(ch)
    slug = "".join(chars)
    while "__" in slug:
        slug = slug.replace("__", "_")
    slug = slug.strip(" .")
    if not slug:
        # 会话名全是非法字符时也得有文件，否则每次保存都覆盖 goals/.json
        slug = "session"
    if slug.upper() in _RESERVED_NAMES:
        slug = f"_{slug}"
    if len(slug) > _MAX_SLUG_LEN:
        # 截断后追加原文摘要，保证长会话名之间不会因为共同前缀互相覆盖
        slug = f"{slug[: _MAX_SLUG_LEN - 9]}-{_digest(raw)}"
    return slug


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class GoalStore:

    def __init__(self, root: Path):
        self.root = Path(root)

    def path_for(self, session: str) -> Path:
        """返回会话对应的状态文件路径（slug 化后）。"""
        return self.root / f"{slugify_session(session)}.json"

    def load(self, session: str) -> Optional[GoalState]:
        path = self.path_for(session)
        try:
            raw_text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            return None
        try:
            data = json.loads(raw_text)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None

        state = self._from_dict(data, session)
        if state.status == STATUS_RUNNING and state.owner_pid != os.getpid():
            state.status = STATUS_INTERRUPTED
            self.save(state)
        return state

    def save(self, state: GoalState) -> None:
        """原子写入状态文件，并打上当前进程号与时间戳。"""
        now = time.time()
        if not state.created_at:
            state.created_at = now
        state.updated_at = now
        # owner_pid 在写入时统一刷新：调用方（GUI/CLI）不必关心自己是不是新进程，
        # 也避免复用旧对象续跑时把上一个进程的 pid 写回去。
        state.owner_pid = os.getpid()

        path = self.path_for(state.session)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            state.__dict__,
            ensure_ascii=False,
            indent=2,
        )
        # 临时文件与目标同目录：跨盘/跨设备时 os.replace 会失败，同目录才能保证原子
        tmp_path = path.parent / f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                # fsync 再替换：否则断电时可能留下长度为 0 的正式文件
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

    def clear(self, session: str) -> bool:
        """删除会话状态文件；返回是否真的删掉了文件。"""
        try:
            self.path_for(session).unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    @staticmethod
    def _from_dict(data: dict, session: str) -> GoalState:
        allowed = {item.name for item in fields(GoalState)}
        known = {key: value for key, value in data.items() if key in allowed}

        history = []
        raw_history = known.get("history")
        if isinstance(raw_history, list):
            for entry in raw_history:
                if not isinstance(entry, dict):
                    continue
                history.append(
                    {
                        "round": _as_int(entry.get("round"), 0),
                        "summary": str(entry.get("summary") or ""),
                    }
                )

        status = str(known.get("status") or STATUS_INTERRUPTED).strip()
        if status not in VALID_STATUSES:
            # 未知状态不当作 running：宁可让用户手动 resume，也不要自动烧 token
            status = STATUS_INTERRUPTED

        return GoalState(
            # 以调用方传入的会话名为准：文件名是 slug 化结果，不能反过来覆盖会话名
            session=str(known.get("session") or session),
            goal=str(known.get("goal") or ""),
            check=str(known.get("check") or ""),
            # 非法轮次上限回落 10；下限 1，否则循环一次都不会跑却立刻 blocked
            max_rounds=max(1, _as_int(known.get("max_rounds"), 10)),
            round=max(0, _as_int(known.get("round"), 0)),
            status=status,
            history=history,
            created_at=_as_float(known.get("created_at"), 0.0),
            updated_at=_as_float(known.get("updated_at"), 0.0),
            owner_pid=_as_int(known.get("owner_pid"), 0),
        )
