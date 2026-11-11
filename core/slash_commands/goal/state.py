"""`/goal` 的状态定义与持久化：纯文件逻辑，不依赖 GUI / CLI。

为什么单独成模块：GUI 与 CLI 要读写同一份目标状态，且都要求"进程退出后状态可恢复、
下次启动能看到上一轮进展"。把数据格式与落盘集中在一处，两端适配层只负责渲染与
发起回合，避免各自维护一份 JSON 结构而漂移。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

# 状态取值（设计文档 6.1）。集中定义成常量集合，是为了让适配层（GoalBar / CLI
# status）与加载校验共用同一份枚举，避免某处把 "awaiting_confirm" 拼错后静默
# 落到"未知状态"分支。
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
    """一个会话的目标模式状态。

    字段严格按设计文档 6.2 定义：适配层（GUI 桥、CLI 命令）直接按字段名读写，
    新增字段必须同步两端，所以这里不加"仅内部使用"的隐藏字段。
    """

    session: str
    goal: str
    check: str = ""  # 可执行验证命令；空串表示改为自评（见 runner.judge）
    max_rounds: int = 10
    round: int = 0  # 字段名按设计文档保留 round，仅在类作用域内遮蔽内置名
    status: str = STATUS_RUNNING
    # [{round: int, summary: str}]：只留摘要不留全文，避免历史把 JSON 撑大
    history: list = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    owner_pid: int = 0  # 写入进程号，用于识别"上一个进程没正常收尾"

    def is_terminal(self) -> bool:
        """是否已到终态（不再自动续跑）。"""
        return self.status in {
            STATUS_ACHIEVED,
            STATUS_BLOCKED,
            STATUS_STOPPED,
            STATUS_INTERRUPTED,
        }


def _digest(text: str) -> str:
    """对文本做 32 位 FNV-1a 摘要，返回 8 位十六进制。

    为什么不用 hashlib：会话名过长需要截断，截断后必须仍能区分不同会话，而
    内置 hash() 受 PYTHONHASHSEED 影响、跨进程不稳定，会破坏"同一会话同一文件"。
    """
    value = 0x811C9DC5
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 0x01000193) & 0xFFFFFFFF
    return f"{value:08x}"


def slugify_session(session: str) -> str:
    """把会话名转成安全的文件名主干（不含扩展名）。

    处理三类情况：非法字符（`<>:"/\\|?*`、控制字符）替换为下划线；空格变成下划线，
    使"新会话 1"读起来仍像原名；中文原样保留，因为它是合法文件名字符，改写成拼音
    或哈希反而让人无法确认是哪个会话。
    """
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
    # 连续下划线压成一个：会话名里的多个空格或换行不必产生 "___"
    while "__" in slug:
        slug = slug.replace("__", "_")
    # Windows 不允许文件名以点或空格结尾；前导点会在类 Unix 上变隐藏文件
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
    """把 JSON 里的值收敛成 int；坏数据回落到默认值，不让加载整体失败。"""
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
    """目标状态的读写入口，一个会话一个 JSON 文件。

    为什么不在这里做跨进程锁：锁的粒度与阻塞时长取决于适配层（GUI 后台线程、
    CLI daemon 线程）的线程模型，核心层贸然加锁只会让 stop/resume 也排队。
    核心层只承担可见的弱约束：写入时记录 owner_pid，加载时发现"别的进程还标着
    running"就降级为 interrupted，避免两个进程同时续跑同一个目标。
    """

    def __init__(self, root: Path):
        # 不在构造时创建目录：查询/加载路径（GUI 打开面板、CLI /goal status）
        # 不该产生任何写盘副作用，目录留到 save 时按需创建。
        self.root = Path(root)

    def path_for(self, session: str) -> Path:
        """返回会话对应的状态文件路径（slug 化后）。"""
        return self.root / f"{slugify_session(session)}.json"

    def load(self, session: str) -> Optional[GoalState]:
        """读取会话状态；文件不存在或内容损坏时返回 None。

        损坏文件（半截 JSON、被手工编辑坏）按"没有状态"处理而不是抛异常：目标模式
        是辅助功能，不能因为一个坏文件让 GUI 启动失败。
        """
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
            # 上一个进程没走到收尾（崩溃/被杀）：标成 interrupted 并立刻落盘，
            # 让用户看到"被打断、可恢复"，而不是以为还在跑。
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
            ensure_ascii=False,  # 中文目标原文直接可读，便于用户排查
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
            # 写失败时别把临时文件留在 goals 目录里积累垃圾
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
        """从 JSON 字典重建状态，未知字段忽略、缺失字段取默认值。"""
        allowed = {item.name for item in fields(GoalState)}
        known = {key: value for key, value in data.items() if key in allowed}

        history = []
        raw_history = known.get("history")
        if isinstance(raw_history, list):
            for entry in raw_history:
                if not isinstance(entry, dict):
                    continue
                # 只保留约定字段：旧版本多写的键不该带进内存后被再次写回文件
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
