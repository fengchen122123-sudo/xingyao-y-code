import datetime
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.harness.models import JournalEventDraft


import pathlib


def _canonicalize_api_base(value):
    """规范 endpoint 身份并移除 userinfo/query/fragment，避免分桶漂移或泄密。"""
    text = str(value or "").strip()
    if not text:
        return ""
    digest = lambda data: hashlib.sha256(
        str(data).encode("utf-8")
    ).hexdigest()[:16]
    try:
        parsed = urlsplit(text)
        scheme = str(parsed.scheme or "").lower()
        host = str(parsed.hostname or "").lower()
        if not scheme or not host:
            return f"invalid-endpoint#{digest(text)}"
        port = parsed.port
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        default_port = (scheme == "https" and port == 443) or (
            scheme == "http" and port == 80
        )
        netloc = host if port is None or default_port else f"{host}:{port}"
        path = str(parsed.path or "").rstrip("/")
        identity = urlunsplit((scheme, netloc, path, "", ""))
        if parsed.username is not None or parsed.password is not None:
            identity += "|auth~" + digest(
                f"{parsed.username or ''}:{parsed.password or ''}"
            )
        if parsed.query:
            normalized_query = urlencode(
                sorted(parse_qsl(parsed.query, keep_blank_values=True)),
                doseq=True,
            )
            identity += "|query~" + digest(normalized_query)
        return identity
    except Exception:
        return f"invalid-endpoint#{digest(text)}"

def _normalize_workdir(path):
    """规范化工作目录路径（真实磁盘物理大小写），用于会话归属目录比较。"""
    try:
        p = pathlib.Path(path)
        if p.exists():
            return str(p.resolve())
        return os.path.abspath(str(path))
    except Exception:
        return str(path or "")


def _try_parse_json(text):
    """仅对形如 JSON 对象/数组的字符串尝试解析，避免把 '123'、'true' 等纯文本曲解为标量。"""
    if not isinstance(text, str):
        return None, False
    stripped = text.lstrip()
    if not stripped or stripped[0] not in "{[":
        return None, False
    try:
        return json.loads(text), True
    except (ValueError, TypeError):
        return None, False


_FOLDED_MEDIA_TEXT = "[图片/媒体内容已折叠以节省 Token；如需再次查看请重新提供]"


def _content_has_media(content):
    return (
        isinstance(content, list)
        and any(
            isinstance(b, dict) and b.get("type") in ("image", "image_url")
            for b in content
        )
    )


def message_content_to_text(raw_content):
    """把入库的消息 content 提取为纯文本（多模态数组只拼接 text 块）。

    供记忆压缩归纳等只需要文本的场景复用，避免各处重复 JSON 解析逻辑。
    """
    parsed, ok = _try_parse_json(raw_content)
    if not ok:
        return str(raw_content)
    if isinstance(parsed, list):
        text_parts = []
        for item in parsed:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))
        return " ".join(text_parts)
    return str(parsed)


def _sanitize_tool_call_pairing(history):
    """修复 tool_call 配对，避免坏历史导致 API 报错后整个会话无法继续使用。

    两种损坏形态（典型成因：记忆压缩边界切在 assistant(tool_calls) 与其 tool 结果之间）：
    1. 孤儿 tool 消息：tool_call_id 未在前面任何 assistant.tool_calls 中声明，
       API 会报 "tool_call_id is not found" → 直接丢弃该 tool 消息。
    2. assistant 声明了 tool_calls 但历史中缺少对应 tool 结果，
       API 会报 "assistant message with tool_calls must be followed by tool messages"
       → 移除该 assistant 的 tool_calls 字段（保留文本内容）。
    """
    announced = set()
    for msg in history:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict) and tc.get("id"):
                announced.add(tc["id"])

    sanitized = []
    answered = set()
    for msg in history:
        if msg.get("role") == "tool":
            tcid = msg.get("tool_call_id")
            if tcid not in announced or tcid in answered:
                continue
            answered.add(tcid)
        sanitized.append(msg)

    for msg in sanitized:
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            continue
        unresolved = [
            tc for tc in tool_calls
            if isinstance(tc, dict) and tc.get("id") and tc["id"] not in answered
        ]
        if unresolved:
            resolved = [tc for tc in tool_calls if tc not in unresolved]
            if resolved:
                msg["tool_calls"] = resolved
            else:
                msg.pop("tool_calls", None)
                if not msg.get("content"):
                    msg["content"] = "[工具调用记录缺失]"

    return sanitized

# 按会话隔离的撤销状态。
# _undo_state_lock 保护并发写：多 worker 线程（含并行工具批次）同时写文件时
# 防止 LAST_UNDO_STATE 被竞态覆盖导致 /undo 回滚到错误文件/错误内容。
# 每个会话独立维护自己的最后一次 undo 记录，避免切会话后误删/误改其他会话的文件。
_undo_state_lock = threading.Lock()
_undo_tls = threading.local()
LAST_UNDO_STATE: dict[str, dict] = {}


def set_undo_session(session_name: str | None) -> None:
    """标记当前线程的 undo 会话，供 set_undo_state / get_undo_state 隐式使用。"""
    _undo_tls.session_name = session_name


def clear_undo_session() -> None:
    _undo_tls.session_name = None


def _current_undo_session(session_name: str | None = None) -> str:
    explicit = session_name or getattr(_undo_tls, "session_name", None)
    if explicit:
        return explicit
    # GUI 回合与子代理线程的权威会话路由在 app_context TLS 中；它不能
    # 继承 UI 线程调用 set_undo_session 写入的 memory TLS。
    from core import app_context

    return app_context.get_thread_session() or "_global_"

# 会话级检查点缓存（按 session_name → list of checkpoint dict）
# 每轮对话开始前自动打一个 checkpoint，记录本轮所有被修改文件的旧内容
# /checkpoint list 查看，/checkpoint restore <id> 回滚到该检查点
# 设计参考 Grok Build 的 workspace 级 checkpoint
_session_checkpoints_lock = threading.Lock()
_session_checkpoints: dict[str, list[dict]] = {}


def set_undo_state(
    file_path,
    old_content,
    session_name=None,
    *,
    existed_before=None,
    old_bytes=None,
):
    session = _current_undo_session(session_name)
    if existed_before is None:
        # 兼容旧调用方：历史约定 old_content="" 表示新建文件。
        existed_before = bool(file_path) and old_content != ""
    with _undo_state_lock:
        LAST_UNDO_STATE[session] = {
            "file_path": file_path,
            "old_content": old_content,
            "existed_before": bool(existed_before),
            "old_bytes": old_bytes,
        }
    # 同步追加到当前会话的检查点文件快照
    # （由 worker 线程在 begin_turn 时已建好当前 turn 的 checkpoint 上下文）
    if file_path and old_content is not None:
        record_file_snapshot_for_current_turn(
            file_path,
            old_content,
            existed_before=bool(existed_before),
            old_bytes=old_bytes,
        )


def get_undo_state(session_name=None):
    session = _current_undo_session(session_name)
    with _undo_state_lock:
        # 返回副本，避免调用方在无锁状态下读写共享 dict 产生竞态
        return dict(LAST_UNDO_STATE.get(session, {
            "file_path": None,
            "old_content": None,
            "existed_before": False,
            "old_bytes": None,
        }))


# ---------------------------------------------------------------------------
# 会话级检查点 API
# ---------------------------------------------------------------------------
# 设计：每轮对话开始前由 main._background_worker 调用 begin_turn_checkpoint，
# 把 turn_id + last_msg_id 记下来；之后 write_file/edit_file 调用 set_undo_state
# 时同步把旧内容追加到该 turn 的快照列表里。
# 用户 /checkpoint restore <turn_id> 时，把所有快照按反序回写文件。

# 当前 worker 线程的活动 turn 上下文（thread-local，避免多线程串扰）
_checkpoint_tls = threading.local()
# 父回合和子代理分属不同线程：用 (session, turn) 注册活动 checkpoint，
# 子代理只借用记录权，不拥有提交权。每个 ctx 的锁保护 completed 与快照列表。
_active_turn_checkpoints_lock = threading.RLock()
_active_turn_checkpoints: dict[tuple[str, str], tuple[dict, threading.RLock]] = {}


def begin_turn_checkpoint(session_name, turn_id, last_msg_id, description=""):
    """每轮对话开始前调用：建立当前 turn 的检查点上下文。

    此后该线程内的 write_file/edit_file 调用会通过 set_undo_state 自动把旧内容
    追加到本 turn 的快照列表。turn 结束后调用 end_turn_checkpoint 提交到全局缓存。
    """
    ctx = {
        "session_name": session_name,
        "turn_id": turn_id,
        "last_msg_id": last_msg_id,
        "description": description,
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file_snapshots": [],  # list of {file_path, old_content}
        "completed": False,
    }
    key = (str(session_name), str(turn_id))
    ctx_lock = threading.RLock()
    with _active_turn_checkpoints_lock:
        existing = _active_turn_checkpoints.get(key)
        if existing is not None and not existing[0].get("completed"):
            raise RuntimeError(f"checkpoint already active: {key!r}")
        _active_turn_checkpoints[key] = (ctx, ctx_lock)
    _checkpoint_tls.current = ctx
    _checkpoint_tls.current_lock = ctx_lock
    _checkpoint_tls.owner = True
    return ctx


def bind_turn_checkpoint(session_name, turn_id) -> bool:
    """把子代理线程绑定到父回合的活动 checkpoint；不转移提交所有权。"""
    key = (str(session_name), str(turn_id))
    with _active_turn_checkpoints_lock:
        entry = _active_turn_checkpoints.get(key)
    if entry is None:
        _checkpoint_tls.current = None
        _checkpoint_tls.current_lock = None
        _checkpoint_tls.owner = False
        return False
    ctx, ctx_lock = entry
    with ctx_lock:
        if ctx.get("completed"):
            _checkpoint_tls.current = None
            _checkpoint_tls.current_lock = None
            _checkpoint_tls.owner = False
            return False
        _checkpoint_tls.current = ctx
        _checkpoint_tls.current_lock = ctx_lock
        _checkpoint_tls.owner = False
    return True


def clear_turn_checkpoint_binding() -> None:
    """清除当前线程借用的 checkpoint 绑定。"""
    _checkpoint_tls.current = None
    _checkpoint_tls.current_lock = None
    _checkpoint_tls.owner = False


def record_file_snapshot_for_current_turn(
    file_path,
    old_content,
    *,
    existed_before=None,
    old_bytes=None,
):
    """把当前线程内被修改文件的旧内容追加到当前 turn 的快照列表。"""
    ctx = getattr(_checkpoint_tls, "current", None)
    ctx_lock = getattr(_checkpoint_tls, "current_lock", None)
    if ctx is None or ctx_lock is None:
        return
    with ctx_lock:
        if ctx.get("completed"):
            return
        # 同一文件在一轮内可能被多次修改，只保留最早的旧内容（最原始状态）
        path_key = os.path.normcase(os.path.realpath(os.path.abspath(file_path)))
        existing = next((
            snapshot
            for snapshot in ctx["file_snapshots"]
            if os.path.normcase(
                os.path.realpath(os.path.abspath(snapshot["file_path"]))
            ) == path_key
        ), None)
        if existing is None:
            ctx["file_snapshots"].append({
                "file_path": file_path,
                "old_content": old_content,
                "existed_before": (
                    old_content != "" if existed_before is None else bool(existed_before)
                ),
                "old_bytes": old_bytes,
            })


def end_turn_checkpoint():
    """turn 结束时把当前上下文提交到全局缓存，便于后续 /checkpoint 查询和回滚。"""
    ctx = getattr(_checkpoint_tls, "current", None)
    if ctx is None:
        return None
    if not getattr(_checkpoint_tls, "owner", False):
        raise RuntimeError("borrowed checkpoint binding cannot be committed")
    ctx_lock = getattr(_checkpoint_tls, "current_lock", None)
    if ctx_lock is None:
        raise RuntimeError("checkpoint lock missing")
    session_name = ctx["session_name"]
    key = (str(session_name), str(ctx["turn_id"]))
    with ctx_lock:
        ctx["completed"] = True
        with _active_turn_checkpoints_lock:
            active = _active_turn_checkpoints.get(key)
            if active is not None and active[0] is ctx:
                _active_turn_checkpoints.pop(key, None)
        with _session_checkpoints_lock:
            bucket = _session_checkpoints.setdefault(session_name, [])
            bucket.append(ctx)
            # 限制每个会话最多保留 50 个检查点，防止内存无限增长
            if len(bucket) > 50:
                del bucket[: len(bucket) - 50]
    clear_turn_checkpoint_binding()
    return ctx


def list_checkpoints(session_name):
    """返回指定会话的所有检查点（按时间正序）。"""
    with _session_checkpoints_lock:
        return list(_session_checkpoints.get(session_name, []))


def get_checkpoint(session_name, turn_id):
    """按 turn_id 查找单个检查点。"""
    with _session_checkpoints_lock:
        for cp in _session_checkpoints.get(session_name, []):
            if cp["turn_id"] == turn_id:
                return cp
    return None


def restore_checkpoint(session_name, turn_id):
    """回滚到指定检查点：返回该检查点记录的所有文件快照（按反序，便于逐个回写）。

    调用方负责把 old_content 写回文件。本函数只返回快照列表，不直接写文件，
    避免在 memory 模块引入文件写入副作用。
    """
    cp = get_checkpoint(session_name, turn_id)
    if cp is None:
        return None
    # 反序返回：最后修改的文件先回写，最早修改的文件最后回写
    # 这样即使有依赖关系也能正确恢复
    return list(reversed(cp["file_snapshots"]))


def clear_checkpoints(session_name):
    """清空指定会话的所有检查点（切换会话或主动清理时调用）。"""
    with _session_checkpoints_lock:
        _session_checkpoints.pop(session_name, None)

class MemoryManager:
    def __init__(self, db_path="ycode.db"):
        # 1. 连接数据库（check_same_thread=False 允许多线程访问，timeout=30.0 防止多窗口并发挂起）
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        self.conn.execute("PRAGMA foreign_keys=ON")
        # 2. 启用 WAL 模式与 busy_timeout，提升多进程多窗口并发读写性能，彻底解决锁死卡顿
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        # 3. 读写序列化锁（可重入，允许方法间互调），防止多线程共享单连接游标并发竞态
        self._lock = threading.RLock()
        # 4. 新建会话的归属工作目录（默认取启动时的 cwd，可用 set_workdir 修改）
        self.workdir = _normalize_workdir(os.getcwd())
        # 5. 立刻建表
        self.create_tables()
        self.interrupt_stale_harness_runs(time.time_ns() // 1_000_000 - 10_000)

    def set_workdir(self, path):
        """设置后续新建会话的归属工作目录。"""
        self.workdir = _normalize_workdir(path)

    def acquire_lock(self):
        """公开的加锁接口：返回内部可重入锁，供外部模块（如 memory_compressor）在多步读写间保持原子性。"""
        return self._lock

    def create_tables(self):
        with self._lock:
            cursor = self.conn.cursor()

            try:
                # 兼容性恢复：检测并丢弃与当前版本不兼容的遗留 sessions 表。
                # 当 ~/.ycode/ycode.db 由更旧的星瑶版本创建、缺少核心 session_name
                # 列时，CREATE TABLE IF NOT EXISTS 不会重建它，SQLite 也无法通过
                # ALTER ADD COLUMN 添加带 UNIQUE 约束的列，从而导致首次运行就崩在
                # get_history 的 "no such column: session_name"。该遗留库无
                # session_name 标识符、其消息均为孤儿，本身已无法使用，因此直接清表
                # 重建是最稳的恢复（同时记一条 runtime-warnings 日志便于事后追溯）。
                cursor.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
                )
                if cursor.fetchone() is not None:
                    cursor.execute("PRAGMA table_info(sessions)")
                    existing_columns = {col[1] for col in cursor.fetchall()}
                    if "session_name" not in existing_columns:
                        try:
                            logging.getLogger("py.warnings").warning(
                                "Detected legacy ycode.db with incompatible sessions schema "
                                "(missing session_name). Recreating tables; legacy data is unrecoverable."
                            )
                        except Exception:
                            pass
                        cursor.execute("DROP TABLE IF EXISTS sessions")
                        cursor.execute("DROP TABLE IF EXISTS messages")

                # 历史遗留清理：session_diffs 表来自 1.2.5 开发期的 diff 持久化
                # 方案（后按用户决定移除，仅保留实时 diff_ready 显示）。现行代码
                # 无任何读写，这里幂等清表，避免旧构建留下的表与数据长期残留；
                # 若将来恢复持久化，CREATE TABLE 会按新 schema 重建。
                cursor.execute("DROP TABLE IF EXISTS session_diffs")

                # 创建一个叫 sessions 的表，加入摘要字段
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_name TEXT UNIQUE,
                        memory_summary TEXT,
                        summarized_until_msg_id INTEGER
                    )
                ''')

                # 兼容性升级逻辑：如果旧表已经存在但缺少字段，动态添加
                cursor.execute("PRAGMA table_info(sessions)")
                columns = [col[1] for col in cursor.fetchall()]
                if "memory_summary" not in columns:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN memory_summary TEXT")
                if "summarized_until_msg_id" not in columns:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN summarized_until_msg_id INTEGER")
                # 会话归属工作目录与时间戳（按目录隔离会话列表，NULL 表示历史遗留的全局会话）
                if "workdir" not in columns:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN workdir TEXT")
                if "created_at" not in columns:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN created_at TEXT")
                if "updated_at" not in columns:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN updated_at TEXT")
                if "source" not in columns:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN source TEXT DEFAULT 'GUI'")
                # 会话级主模型绑定：不继承全局 active_model_id，不同会话/不同界面互不干扰
                if "active_model_id" not in columns:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN active_model_id TEXT")

                # 创建一个叫 messages 的表
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER,
                        role TEXT,
                        content TEXT
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS harness_owners (
                        owner_id TEXT PRIMARY KEY,
                        heartbeat_at_ms INTEGER NOT NULL
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS harness_turns (
                        turn_id TEXT PRIMARY KEY,
                        session_id INTEGER NOT NULL,
                        owner_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at_ms INTEGER NOT NULL,
                        finished_at_ms INTEGER,
                        last_sequence INTEGER NOT NULL DEFAULT 0,
                        error_code TEXT,
                        FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                        FOREIGN KEY(owner_id) REFERENCES harness_owners(owner_id) ON DELETE RESTRICT
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS harness_steps (
                        step_id TEXT PRIMARY KEY,
                        turn_id TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        protocol TEXT,
                        model TEXT,
                        api_base TEXT,
                        cache_scope TEXT,
                        messages_sha256 TEXT,
                        system_sha256 TEXT,
                        tools_sha256 TEXT,
                        options_sha256 TEXT,
                        cache_lane_sha256 TEXT,
                        message_count INTEGER,
                        system_message_count INTEGER,
                        request_bytes INTEGER,
                        system_bytes INTEGER,
                        tools_bytes INTEGER,
                        options_bytes INTEGER,
                        request_reason TEXT,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        deepseek_generation_id INTEGER,
                        started_at_ms INTEGER NOT NULL,
                        finished_at_ms INTEGER,
                        duration_ms INTEGER,
                        finish_reason TEXT,
                        error_code TEXT,
                        UNIQUE(turn_id, ordinal),
                        FOREIGN KEY(turn_id) REFERENCES harness_turns(turn_id) ON DELETE CASCADE
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS harness_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        turn_id TEXT NOT NULL,
                        step_id TEXT,
                        sequence INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        message_id INTEGER,
                        tool_name TEXT,
                        call_ordinal INTEGER,
                        arguments_sha256 TEXT,
                        result_sha256 TEXT,
                        duration_ms INTEGER,
                        result_status TEXT,
                        error_code TEXT,
                        UNIQUE(turn_id, sequence),
                        FOREIGN KEY(turn_id) REFERENCES harness_turns(turn_id) ON DELETE CASCADE,
                        FOREIGN KEY(step_id) REFERENCES harness_steps(step_id) ON DELETE CASCADE,
                        FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE SET NULL
                    )
                ''')
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_harness_turns_session_status "
                    "ON harness_turns(session_id, status)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_harness_steps_turn_ordinal "
                    "ON harness_steps(turn_id, ordinal)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_harness_events_turn_sequence "
                    "ON harness_events(turn_id, sequence)"
                )
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS deepseek_prompt_generations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_name TEXT NOT NULL,
                        model TEXT NOT NULL,
                        api_base TEXT NOT NULL,
                        cache_scope TEXT NOT NULL,
                        tools_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS deepseek_prompt_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        generation_id INTEGER NOT NULL,
                        ordinal INTEGER NOT NULL,
                        message_json TEXT NOT NULL,
                        UNIQUE(generation_id, ordinal)
                    )
                ''')
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_deepseek_prompt_messages_generation "
                    "ON deepseek_prompt_messages(generation_id, ordinal)"
                )
                # 创建一个叫 workspaces 的表持久化记录引入过的项目文件夹
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS workspaces (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        workdir TEXT UNIQUE,
                        created_at TEXT
                    )
                ''')
                # token 用量统计表：每次流式响应结束后追加一条真实用量记录，
                # 供 /usage 按模型与日期区间聚合（今日/近7天/近30天）
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS token_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts INTEGER,
                        date TEXT,
                        provider TEXT,
                        model TEXT,
                        prompt_tokens INTEGER,
                        completion_tokens INTEGER,
                        cached_tokens INTEGER,
                        total_tokens INTEGER,
                        session_id TEXT,
                        api_base TEXT NOT NULL DEFAULT '',
                        cache_scope TEXT NOT NULL DEFAULT 'legacy'
                    )
                ''')
                cursor.execute("PRAGMA table_info(token_usage)")
                token_usage_columns = {col[1] for col in cursor.fetchall()}
                if "api_base" not in token_usage_columns:
                    cursor.execute(
                        "ALTER TABLE token_usage "
                        "ADD COLUMN api_base TEXT NOT NULL DEFAULT ''"
                    )
                if "cache_scope" not in token_usage_columns:
                    cursor.execute(
                        "ALTER TABLE token_usage "
                        "ADD COLUMN cache_scope TEXT NOT NULL DEFAULT 'legacy'"
                    )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_token_usage_date ON token_usage(date)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_token_usage_route "
                    "ON token_usage(model, api_base, cache_scope, date)"
                )
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS cache_request_diagnostics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts INTEGER NOT NULL,
                        session_id TEXT NOT NULL,
                        model TEXT NOT NULL,
                        api_base TEXT NOT NULL,
                        cache_scope TEXT NOT NULL,
                        request_reason TEXT NOT NULL,
                        messages_sha256 TEXT NOT NULL,
                        system_sha256 TEXT NOT NULL,
                        tools_sha256 TEXT NOT NULL,
                        message_count INTEGER NOT NULL,
                        system_message_count INTEGER NOT NULL,
                        common_prefix_messages INTEGER NOT NULL,
                        request_bytes INTEGER NOT NULL,
                        system_bytes INTEGER NOT NULL,
                        tools_bytes INTEGER NOT NULL,
                        prompt_tokens INTEGER,
                        cached_tokens INTEGER,
                        cache_miss_tokens INTEGER,
                        cache_reported INTEGER NOT NULL,
                        usage_valid INTEGER NOT NULL,
                        usage_errors TEXT NOT NULL
                    )
                ''')
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cache_diag_identity "
                    "ON cache_request_diagnostics"
                    "(session_id, model, api_base, cache_scope, id)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cache_diag_ts "
                    "ON cache_request_diagnostics(ts)"
                )
                # 保存修改
                self.conn.commit()
            except Exception:
                # DDL 失败时回滚，避免遗留未提交事务阻塞后续写操作
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise

    def create_harness_turn(self, turn_id: str, session_name: str, owner_id: str,
                            started_at_ms: int) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "SELECT id FROM sessions WHERE session_name = ?",
                    (session_name,),
                )
                session = cursor.fetchone()
                if session is None:
                    raise RuntimeError(f"missing harness session: {session_name}")
                cursor.execute(
                    "INSERT INTO harness_turns "
                    "(turn_id, session_id, owner_id, status, started_at_ms) "
                    "VALUES (?, ?, ?, 'created', ?)",
                    (turn_id, session[0], owner_id, int(started_at_ms)),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def create_harness_step(self, step_id: str, turn_id: str, ordinal: int,
                            started_at_ms: int) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO harness_steps "
                    "(step_id, turn_id, ordinal, status, started_at_ms) "
                    "VALUES (?, ?, ?, 'created', ?)",
                    (step_id, turn_id, int(ordinal), int(started_at_ms)),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def transition_harness_turn(self, turn_id: str, expected_status: str,
                                target_status: str, now_ms: int, *,
                                error_code: str | None = None) -> None:
        terminal = {"completed", "cancelled", "failed", "interrupted"}
        finished_at_ms = int(now_ms) if target_status in terminal else None
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "UPDATE harness_turns "
                    "SET status = ?, finished_at_ms = ?, error_code = ? "
                    "WHERE turn_id = ? AND status = ?",
                    (target_status, finished_at_ms, error_code, turn_id, expected_status),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"stale harness turn transition: {turn_id} expected {expected_status}"
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def transition_harness_step(self, step_id: str, expected_status: str,
                                target_status: str, now_ms: int, *,
                                duration_ms: int | None = None,
                                finish_reason: str | None = None,
                                error_code: str | None = None) -> None:
        terminal = {"completed", "cancelled", "failed", "interrupted"}
        finished_at_ms = int(now_ms) if target_status in terminal else None
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "UPDATE harness_steps "
                    "SET status = ?, finished_at_ms = ?, duration_ms = ?, "
                    "finish_reason = ?, error_code = ? "
                    "WHERE step_id = ? AND status = ?",
                    (
                        target_status,
                        finished_at_ms,
                        duration_ms,
                        finish_reason,
                        error_code,
                        step_id,
                        expected_status,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"stale harness step transition: {step_id} expected {expected_status}"
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def append_harness_event(self, turn_id: str, draft: JournalEventDraft,
                             created_at_ms: int) -> int:
        with self._lock:
            cursor = self.conn.cursor()
            try:
                if draft.message_index is not None:
                    raise ValueError(
                        "message_index requires atomic message-id resolution"
                    )
                cursor.execute(
                    "UPDATE harness_turns "
                    "SET last_sequence = last_sequence + 1 WHERE turn_id = ?",
                    (turn_id,),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"missing harness turn: {turn_id}")
                cursor.execute(
                    "SELECT last_sequence FROM harness_turns WHERE turn_id = ?",
                    (turn_id,),
                )
                sequence = int(cursor.fetchone()[0])
                cursor.execute(
                    "INSERT INTO harness_events "
                    "(turn_id, step_id, sequence, kind, created_at_ms, message_id, "
                    "tool_name, call_ordinal, arguments_sha256, result_sha256, "
                    "duration_ms, result_status, error_code) "
                    "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        turn_id,
                        draft.step_id,
                        sequence,
                        draft.kind.value,
                        int(created_at_ms),
                        draft.tool_name,
                        draft.call_ordinal,
                        draft.arguments_sha256,
                        draft.result_sha256,
                        draft.duration_ms,
                        draft.result_status,
                        draft.error_code,
                    ),
                )
                self.conn.commit()
                return sequence
            except Exception:
                self.conn.rollback()
                raise

    def heartbeat_harness_owner(self, owner_id, now_ms):
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO harness_owners (owner_id, heartbeat_at_ms) "
                    "VALUES (?, ?) "
                    "ON CONFLICT(owner_id) DO UPDATE SET heartbeat_at_ms = excluded.heartbeat_at_ms",
                    (owner_id, int(now_ms)),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def interrupt_stale_harness_runs(self, stale_before_ms) -> int:
        turn_nonterminal = ("created", "preparing", "running", "cancelling")
        step_nonterminal = (
            "created",
            "requesting",
            "streaming",
            "assistant_ready",
            "tools_running",
            "tools_committed",
        )
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "UPDATE harness_steps SET status = 'interrupted' "
                    "WHERE status IN (?, ?, ?, ?, ?, ?) AND turn_id IN ("
                    "SELECT harness_turns.turn_id FROM harness_turns "
                    "JOIN harness_owners "
                    "ON harness_owners.owner_id = harness_turns.owner_id "
                    "WHERE harness_owners.heartbeat_at_ms < ?"
                    ")",
                    (*step_nonterminal, int(stale_before_ms)),
                )
                interrupted_steps = cursor.rowcount
                cursor.execute(
                    "UPDATE harness_turns SET status = 'interrupted' "
                    "WHERE status IN (?, ?, ?, ?) AND owner_id IN ("
                    "SELECT owner_id FROM harness_owners WHERE heartbeat_at_ms < ?"
                    ")",
                    (*turn_nonterminal, int(stale_before_ms)),
                )
                interrupted_turns = cursor.rowcount
                self.conn.commit()
                return interrupted_steps + interrupted_turns
            except Exception:
                self.conn.rollback()
                raise

    def get_harness_turn(self, turn_id):
        columns = (
            "turn_id", "session_id", "owner_id", "status", "started_at_ms",
            "finished_at_ms", "last_sequence", "error_code",
        )
        with self._lock:
            cursor = self.conn.execute(
                "SELECT turn_id, session_id, owner_id, status, started_at_ms, "
                "finished_at_ms, last_sequence, error_code "
                "FROM harness_turns WHERE turn_id = ?",
                (turn_id,),
            )
            row = cursor.fetchone()
            return None if row is None else dict(zip(columns, row))

    def get_harness_step(self, step_id):
        columns = (
            "step_id", "turn_id", "ordinal", "status", "started_at_ms",
            "finished_at_ms", "duration_ms", "finish_reason", "error_code",
        )
        with self._lock:
            cursor = self.conn.execute(
                "SELECT step_id, turn_id, ordinal, status, started_at_ms, "
                "finished_at_ms, duration_ms, finish_reason, error_code "
                "FROM harness_steps WHERE step_id = ?",
                (step_id,),
            )
            row = cursor.fetchone()
            return None if row is None else dict(zip(columns, row))

    def checkpoint_wal(self):
        """主动执行 WAL checkpoint，整理被写的预写日志，防止 ycode.db-wal 文件体积无限膨胀。"""
        with self._lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            except Exception as e:
                try:
                    logging.getLogger("py.warnings").warning("WAL checkpoint 执行失败: %s", e)
                except Exception:
                    pass

    def add_token_usage(self, provider, model, prompt_tokens, completion_tokens,
                        cached_tokens, total_tokens, session_id=None, ts=None,
                        api_base="", cache_scope="legacy"):
        """追加一条真实 token 用量记录（由 core/llm.py 流结束记账点调用）。"""
        import time
        if ts is None:
            ts = int(time.time())
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT INTO token_usage "
                    "(ts, date, provider, model, prompt_tokens, completion_tokens, "
                    "cached_tokens, total_tokens, session_id, api_base, cache_scope) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        int(ts), day, str(provider or ""), str(model or ""),
                        int(prompt_tokens or 0), int(completion_tokens or 0),
                        None if cached_tokens is None else int(cached_tokens),
                        int(total_tokens or 0),
                        str(session_id) if session_id is not None else None,
                        _canonicalize_api_base(api_base),
                        str(cache_scope or "legacy"),
                    ),
                )
                self.conn.commit()
            except Exception:
                # 与同模块其它写方法一致：失败必须回滚，避免连接遗留活动事务
                self.conn.rollback()
                raise

    @staticmethod
    def _serialize_prompt_value(value):
        """Serialize a provider request value once, preserving insertion order."""
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def get_deepseek_prompt_generation(self, generation_id):
        """Return one immutable DeepSeek prompt generation and its ordered messages."""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT id, session_name, model, api_base, cache_scope, tools_json "
                "FROM deepseek_prompt_generations WHERE id = ?",
                (int(generation_id),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                "SELECT message_json FROM deepseek_prompt_messages "
                "WHERE generation_id = ? ORDER BY ordinal ASC",
                (int(generation_id),),
            )
            return {
                "id": row[0],
                "session_name": row[1],
                "model": row[2],
                "api_base": row[3],
                "cache_scope": row[4],
                "tools": json.loads(row[5]),
                "messages": [json.loads(item[0]) for item in cursor.fetchall()],
            }

    def get_or_create_deepseek_prompt_generation(
        self, session_name, model, api_base, cache_scope, seed_messages, tools,
        *, force_new=False
    ):
        """Return the route generation, seeding it exactly once.

        The route identity deliberately excludes request-header fields. Callers compare
        system/tools and create a fresh generation when that stable header changes.
        """
        identity = (
            str(session_name), str(model).lower(), str(api_base).rstrip("/").lower(),
            str(cache_scope),
        )
        seed_json = [self._serialize_prompt_value(item) for item in seed_messages]
        tools_json = self._serialize_prompt_value(tools)
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "SELECT 1 FROM sessions WHERE session_name = ?",
                    (identity[0],),
                )
                if cursor.fetchone() is None:
                    raise RuntimeError(
                        f"DeepSeek prompt 账本所属会话不存在: {identity[0]!r}"
                    )
                row = None
                if not force_new:
                    cursor.execute(
                        "SELECT id FROM deepseek_prompt_generations "
                        "WHERE session_name = ? AND model = ? AND api_base = ? AND cache_scope = ? "
                        "ORDER BY id DESC LIMIT 1",
                        identity,
                    )
                    row = cursor.fetchone()
                if row is None:
                    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute(
                        "INSERT INTO deepseek_prompt_generations "
                        "(session_name, model, api_base, cache_scope, tools_json, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (*identity, tools_json, now),
                    )
                    generation_id = cursor.lastrowid
                    cursor.executemany(
                        "INSERT INTO deepseek_prompt_messages "
                        "(generation_id, ordinal, message_json) VALUES (?, ?, ?)",
                        [
                            (generation_id, ordinal, message_json)
                            for ordinal, message_json in enumerate(seed_json)
                        ],
                    )
                    self.conn.commit()
                else:
                    generation_id = row[0]
                result = self.get_deepseek_prompt_generation(generation_id)
                result["created"] = row is None
                return result
            except Exception:
                self.conn.rollback()
                raise

    def append_deepseek_prompt_messages(
        self, generation_id, messages, *, expected_count
    ):
        """CAS-append ordered messages; stale concurrent append attempts fail loudly."""
        serialized = [self._serialize_prompt_value(item) for item in messages]
        if not serialized:
            return
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "SELECT COUNT(*), MAX(g.session_name) "
                    "FROM deepseek_prompt_messages AS m "
                    "JOIN deepseek_prompt_generations AS g ON g.id = m.generation_id "
                    "WHERE m.generation_id = ?",
                    (int(generation_id),),
                )
                count_row = cursor.fetchone()
                actual_count = int(count_row[0])
                generation_session = count_row[1]
                if generation_session is None:
                    raise RuntimeError("DeepSeek prompt 账本 generation 不存在")
                cursor.execute(
                    "SELECT 1 FROM sessions WHERE session_name = ?",
                    (generation_session,),
                )
                if cursor.fetchone() is None:
                    raise RuntimeError(
                        f"DeepSeek prompt 账本所属会话不存在: {generation_session!r}"
                    )
                if actual_count != int(expected_count):
                    raise RuntimeError(
                        "DeepSeek prompt 账本消息数量已变化："
                        f"expected={expected_count}, actual={actual_count}"
                    )
                cursor.executemany(
                    "INSERT INTO deepseek_prompt_messages "
                    "(generation_id, ordinal, message_json) VALUES (?, ?, ?)",
                    [
                        (int(generation_id), actual_count + offset, message_json)
                        for offset, message_json in enumerate(serialized)
                    ],
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def _delete_deepseek_prompt_generations_locked(self, cursor, session_names):
        """Delete ledger messages before their generations inside caller transaction."""
        names = [str(name) for name in session_names if str(name)]
        if not names:
            return
        placeholders = ",".join("?" for _ in names)
        cursor.execute(
            "SELECT id FROM deepseek_prompt_generations "
            f"WHERE session_name IN ({placeholders})",
            names,
        )
        generation_ids = [row[0] for row in cursor.fetchall()]
        if generation_ids:
            generation_placeholders = ",".join("?" for _ in generation_ids)
            cursor.execute(
                "DELETE FROM deepseek_prompt_messages "
                f"WHERE generation_id IN ({generation_placeholders})",
                generation_ids,
            )
        cursor.execute(
            "DELETE FROM deepseek_prompt_generations "
            f"WHERE session_name IN ({placeholders})",
            names,
        )

    def get_last_token_usage(self, session_id):
        """查询指定会话最近一次真实 token 用量，无记录返回 None。

        供窗口占用显示与压缩触发判断使用主链路真实基线（下一轮请求的
        prompt 必然包含上一轮的 prompt + completion），避免保活/子代理的
        独立上下文覆盖主会话基线。legacy 用于兼容迁移前记录。
        """
        with self._lock:
            cursor = self.conn.execute(
                "SELECT prompt_tokens, completion_tokens, total_tokens, cached_tokens, "
                "provider, model "
                "FROM token_usage WHERE session_id = ? "
                "AND cache_scope IN ('main', 'legacy') "
                "ORDER BY id DESC LIMIT 1",
                (str(session_id),),
            )
            row = cursor.fetchone()
        if not row:
            return None
        provider = str(row[4] or "").strip()
        model = str(row[5] or "").strip()
        return {
            "prompt_tokens": int(row[0] or 0),
            "completion_tokens": int(row[1] or 0),
            "total_tokens": int(row[2] or 0),
            "cached_tokens": None if row[3] is None else int(row[3]),
            "model_name": f"{provider}/{model}" if provider else model,
        }

    def query_token_usage_by_model(self, start_date, end_date):
        """按模型聚合指定日期区间（含端点，格式 YYYY-MM-DD）的 token 用量。

        返回按合计 token 降序排列的字典列表:
        [{"provider", "model", "prompt", "completion", "cached",
          "cache_prompt", "total"}, ...]
        """
        with self._lock:
            cursor = self.conn.execute(
                """
                SELECT provider, model, api_base, cache_scope,
                       SUM(prompt_tokens), SUM(completion_tokens),
                       SUM(cached_tokens),
                       SUM(CASE WHEN cached_tokens IS NOT NULL THEN prompt_tokens ELSE 0 END),
                       SUM(total_tokens)
                FROM token_usage
                WHERE date >= ? AND date <= ?
                GROUP BY provider, model, api_base, cache_scope
                ORDER BY SUM(total_tokens) DESC
                """,
                (str(start_date), str(end_date)),
            )
            return [
                {
                    "provider": row[0] or "",
                    "model": row[1] or "",
                    "api_base": row[2] or "",
                    "cache_scope": row[3] or "legacy",
                    "prompt": row[4] or 0,
                    "completion": row[5] or 0,
                    "cached": row[6] or 0,
                    "cache_prompt": row[7] or 0,
                    "total": row[8] or 0,
                }
                for row in cursor.fetchall()
            ]

    def query_token_usage_by_day_and_model(self, start_date, end_date):
        """按日期与模型聚合真实 token 用量，供 GUI 时间序列图使用。"""
        with self._lock:
            cursor = self.conn.execute(
                """
                SELECT date, provider, model,
                       SUM(prompt_tokens), SUM(completion_tokens),
                       SUM(cached_tokens),
                       SUM(CASE WHEN cached_tokens IS NOT NULL THEN prompt_tokens ELSE 0 END),
                       SUM(total_tokens), COUNT(*)
                FROM token_usage
                WHERE date >= ? AND date <= ?
                GROUP BY date, provider, model
                ORDER BY date ASC, SUM(total_tokens) DESC, provider ASC, model ASC
                """,
                (str(start_date), str(end_date)),
            )
            return [
                {
                    "date": row[0] or "",
                    "provider": row[1] or "",
                    "model": row[2] or "",
                    "prompt": row[3] or 0,
                    "completion": row[4] or 0,
                    "cached": row[5] or 0,
                    "cache_prompt": row[6] or 0,
                    "total": row[7] or 0,
                    "requests": row[8] or 0,
                }
                for row in cursor.fetchall()
            ]

    def add_cache_request_diagnostic(self, diagnostic):
        """追加一条无正文的请求级缓存诊断记录。"""
        import time
        ts = diagnostic.get("ts")
        if ts is None:
            ts = int(time.time())
        fields = (
            "session_id", "model", "api_base", "cache_scope", "request_reason",
            "messages_sha256", "system_sha256", "tools_sha256", "message_count",
            "system_message_count", "common_prefix_messages", "request_bytes",
            "system_bytes", "tools_bytes", "prompt_tokens", "cached_tokens",
            "cache_miss_tokens", "cache_reported", "usage_valid", "usage_errors",
        )
        values = [
            str(diagnostic.get("session_id") or ""),
            str(diagnostic.get("model") or ""),
            str(diagnostic.get("api_base") or ""),
            str(diagnostic.get("cache_scope") or "main"),
            str(diagnostic.get("request_reason") or ""),
            str(diagnostic.get("messages_sha256") or ""),
            str(diagnostic.get("system_sha256") or ""),
            str(diagnostic.get("tools_sha256") or ""),
            int(diagnostic.get("message_count") or 0),
            int(diagnostic.get("system_message_count") or 0),
            int(diagnostic.get("common_prefix_messages") or 0),
            int(diagnostic.get("request_bytes") or 0),
            int(diagnostic.get("system_bytes") or 0),
            int(diagnostic.get("tools_bytes") or 0),
            None if diagnostic.get("prompt_tokens") is None else int(diagnostic["prompt_tokens"]),
            None if diagnostic.get("cached_tokens") is None else int(diagnostic["cached_tokens"]),
            None if diagnostic.get("cache_miss_tokens") is None else int(diagnostic["cache_miss_tokens"]),
            1 if diagnostic.get("cache_reported") else 0,
            1 if diagnostic.get("usage_valid") else 0,
            json.dumps(list(diagnostic.get("usage_errors") or ()), ensure_ascii=False),
        ]
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT INTO cache_request_diagnostics (ts," + ",".join(fields) + ") "
                    "VALUES (" + ",".join("?" for _ in range(len(fields) + 1)) + ")",
                    [int(ts), *values],
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def query_cache_request_diagnostics(
        self, start_ts=None, end_ts=None, session_id=None, cache_scope=None
    ):
        """按时间/会话读取诊断行；NULL 与真实 0 保持原样。"""
        clauses = []
        params = []
        if start_ts is not None:
            clauses.append("ts >= ?")
            params.append(int(start_ts))
        if end_ts is not None:
            clauses.append("ts <= ?")
            params.append(int(end_ts))
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(str(session_id))
        if cache_scope is not None:
            clauses.append("cache_scope = ?")
            params.append(str(cache_scope))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        columns = (
            "id", "ts", "session_id", "model", "api_base", "cache_scope",
            "request_reason", "messages_sha256", "system_sha256", "tools_sha256",
            "message_count", "system_message_count", "common_prefix_messages",
            "request_bytes", "system_bytes", "tools_bytes", "prompt_tokens",
            "cached_tokens", "cache_miss_tokens", "cache_reported", "usage_valid",
            "usage_errors",
        )
        with self._lock:
            rows = self.conn.execute(
                "SELECT " + ",".join(columns)
                + " FROM cache_request_diagnostics" + where + " ORDER BY ts, id",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(zip(columns, row))
            item["cache_reported"] = bool(item["cache_reported"])
            item["usage_valid"] = bool(item["usage_valid"])
            item["usage_errors"] = json.loads(item["usage_errors"] or "[]")
            result.append(item)
        return result

    def query_cache_diagnostic_summary(
        self,
        start_date,
        end_date,
        min_prompt_tokens=20000,
        max_hit_ratio=0.5,
        idle_seconds=120,
        cache_scope=None,
    ):
        """基于已持久化证据分类低命中事件，不猜测未证明的 provider 原因。"""
        import time
        start_ts = int(time.mktime(time.strptime(
            str(start_date) + " 00:00:00", "%Y-%m-%d %H:%M:%S"
        )))
        end_ts = int(time.mktime(time.strptime(
            str(end_date) + " 23:59:59", "%Y-%m-%d %H:%M:%S"
        )))
        rows = self.query_cache_request_diagnostics(
            start_ts, end_ts, cache_scope=cache_scope
        )
        category_names = (
            "cold_start", "identity_change", "tool_contract_change",
            "client_history_rewrite", "prompt_drop", "idle_gap",
            "provider_or_unknown",
        )
        categories = {name: 0 for name in category_names}
        previous_by_identity = {}
        count = 0
        lost_tokens = 0
        unreported = 0
        explicit_categories = {
            "冷启动": "cold_start",
            "模型或接口变化": "identity_change",
            "工具契约变化": "tool_contract_change",
            "上下文压缩或历史重写": "client_history_rewrite",
        }
        for row in rows:
            identity = (
                row["session_id"], row["model"], row["api_base"], row["cache_scope"]
            )
            previous = previous_by_identity.get(identity)
            prompt = row["prompt_tokens"]
            cached = row["cached_tokens"]
            if not row["cache_reported"]:
                unreported += 1
            is_low = (
                row["usage_valid"]
                and row["cache_reported"]
                and prompt is not None
                and cached is not None
                and prompt >= int(min_prompt_tokens)
                and cached < prompt * float(max_hit_ratio)
            )
            if is_low:
                category = explicit_categories.get(row["request_reason"])
                if category is None and previous is not None:
                    previous_prompt = previous["prompt_tokens"]
                    if (
                        previous_prompt is not None
                        and prompt < previous_prompt * 0.98
                    ):
                        category = "prompt_drop"
                    elif row["ts"] - previous["ts"] > int(idle_seconds):
                        category = "idle_gap"
                if category is None:
                    category = "provider_or_unknown"
                categories[category] += 1
                count += 1
                lost_tokens += prompt - cached
            if row["usage_valid"] and prompt is not None:
                previous_by_identity[identity] = row
        return {
            "count": count,
            "lost_tokens": lost_tokens,
            "unreported": unreported,
            "categories": categories,
        }

    def query_cache_invalidation_events(
        self, start_date, end_date, min_prompt_tokens=20000, max_hit_ratio=0.5,
        cache_scope=None,
    ):
        """统计指定日期区间内的“缓存全失效”事件：大输入但命中率极低。

        典型特征：上一轮还 95%+ 命中，本轮 cached 骤降到接近 0，随后一轮又恢复——
        这是 provider/网关侧前缀缓存条目被驱逐的标志，客户端无法避免。
        返回 {"count": 事件数, "lost_tokens": 这些事件多消耗的输入 token}。
        """
        scope_clause = "" if cache_scope is None else " AND cache_scope = ?"
        params = [
            str(start_date), str(end_date),
            int(min_prompt_tokens), float(max_hit_ratio),
        ]
        if cache_scope is not None:
            params.append(str(cache_scope))
        with self._lock:
            cursor = self.conn.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(prompt_tokens - cached_tokens), 0)
                FROM token_usage
                WHERE date >= ? AND date <= ?
                  AND prompt_tokens >= ?
                  AND cached_tokens < prompt_tokens * ?
                """ + scope_clause,
                params,
            )
            row = cursor.fetchone()
        return {"count": int(row[0] or 0), "lost_tokens": int(row[1] or 0)}

    def close(self):
        """关闭数据库连接前自动刷盘 WAL 日志。"""
        with self._lock:
            if hasattr(self, "conn") and self.conn:
                try:
                    self.checkpoint_wal()
                    self.conn.close()
                except Exception:
                    pass

    def add_message(self, session_name, role, content):
        """记笔记技能：把聊天内容存进数据库"""
        self.add_messages(session_name, [(role, content)])
        if str(role) == "user":
            try:
                self.prune_history_files(
                    session_name, max_turns=16, retain_recent=4
                )
            except Exception:
                # 裁剪发生在消息入库提交之后（非原子）：裁剪失败时消息已安全落库，
                # 不能让异常冒泡把“已成功的写入”误报为失败；裁剪是幂等的，
                # 下一条 user 消息入库时会自动重试。
                logging.getLogger("py.warnings").warning(
                    "add_message: prune_history_files 失败，已延后到下次重试 (session=%s)",
                    session_name, exc_info=True,
                )

    @staticmethod
    def _serialize_message_content(content):
        if isinstance(content, (list, dict)):
            return json.dumps(content, ensure_ascii=False)
        return str(content)

    def replace_latest_user_message_content(
        self, session_name, expected_content, canonical_content
    ):
        """CAS-replace the latest user message without touching newer history."""
        expected_raw = self._serialize_message_content(expected_content)
        canonical_raw = self._serialize_message_content(canonical_content)
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT messages.id, messages.content
                    FROM messages
                    JOIN sessions ON sessions.id = messages.session_id
                    WHERE sessions.session_name = ? AND messages.role = 'user'
                    ORDER BY messages.id DESC
                    LIMIT 1
                    """,
                    (session_name,),
                )
                row = cursor.fetchone()
                if row is None or row[1] != expected_raw:
                    return False
                cursor.execute(
                    "UPDATE messages SET content = ? WHERE id = ?",
                    (canonical_raw, row[0]),
                )
                self.conn.commit()
                return True
            except Exception:
                self.conn.rollback()
                raise

    def _serialize_message_batch(self, messages):
        serialized = []
        has_new_media = False
        for role, content in messages:
            # 注意：媒体判断必须用原始 list（json.dumps 之前就判定）
            if str(role) == "user" and _content_has_media(content):
                has_new_media = True
            content = self._serialize_message_content(content)
            serialized.append((str(role), content))
        return serialized, has_new_media

    @staticmethod
    def _validate_harness_message_events(events, message_count, step_id):
        for draft in events:
            message_index = draft.message_index
            if type(message_index) is not int:
                raise ValueError("message_index must be a zero-based integer")
            if message_index < 0 or message_index >= message_count:
                raise ValueError("message_index is outside the submitted message batch")
            if draft.step_id != step_id:
                raise ValueError("event step_id must match the committed harness step")

    @staticmethod
    def _get_harness_commit_session_id_locked(
        cursor,
        session_name,
        turn_id,
        step_id,
    ):
        cursor.execute(
            "SELECT sessions.id FROM sessions "
            "JOIN harness_turns ON harness_turns.session_id = sessions.id "
            "JOIN harness_steps ON harness_steps.turn_id = harness_turns.turn_id "
            "WHERE sessions.session_name = ? AND harness_turns.turn_id = ? "
            "AND harness_steps.step_id = ?",
            (session_name, turn_id, step_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(
                "missing or mismatched harness session, turn, or step"
            )
        return row[0]

    def _validate_harness_commit_ownership(
        self,
        session_name,
        turn_id,
        step_id,
    ):
        with self._lock:
            cursor = self.conn.cursor()
            self._get_harness_commit_session_id_locked(
                cursor,
                session_name,
                turn_id,
                step_id,
            )

    def _insert_message_batch_locked(self, cursor, session_name, serialized):
        cursor.execute(
            "SELECT id FROM sessions WHERE session_name = ?",
            (session_name,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"missing message session: {session_name}")
        session_id = row[0]
        message_ids = []
        for role, content in serialized:
            cursor.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                (session_id, role, content),
            )
            message_ids.append(int(cursor.lastrowid))
        return tuple(message_ids)
    def add_messages(self, session_name, messages):
        """Atomically append one ordered batch of conversation messages."""
        serialized, has_new_media = self._serialize_message_batch(messages)
        if not serialized:
            return

        with self._lock:
            cursor = self.conn.cursor()
            try:
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute('SELECT id, workdir FROM sessions WHERE session_name = ?', (session_name,))
                row = cursor.fetchone()
                if row:
                    session_id = row[0]
                else:
                    target_wdir = self.workdir
                    # 并发首写防御：两个连接同时 SELECT 均未命中时会双双 INSERT，
                    # session_name UNIQUE 约束让后提交者整批消息 IntegrityError 丢失。
                    # INSERT OR IGNORE + 重新 SELECT：冲突时采用已存在的会话行。
                    cursor.execute('''
                        INSERT OR IGNORE INTO sessions (session_name, workdir, created_at, updated_at, source)
                        VALUES (?, ?, ?, ?, 'GUI')
                    ''', (session_name, target_wdir, now, now))
                    cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError(f"会话档案创建失败: {session_name!r}")
                    session_id = row[0]
                self._insert_message_batch_locked(cursor, session_name, serialized)
                # 新媒体入库的同一事务内折叠旧媒体：请求路径不再改写历史中间字节
                if has_new_media:
                    self._fold_stale_media_locked(cursor, session_id)
                # 更新最近活跃时间，供会话列表按时间展示
                cursor.execute('UPDATE sessions SET updated_at = ? WHERE id = ?', (now, session_id))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def add_messages_with_harness_events(
        self,
        session_name,
        messages,
        *,
        turn_id,
        step_id,
        events,
        _created_at_ms=None,
    ) -> tuple[int, ...]:
        """Commit messages and their Harness event references atomically."""
        serialized, has_new_media = self._serialize_message_batch(messages)
        events = list(events)
        self._validate_harness_message_events(events, len(serialized), step_id)
        if not serialized:
            return ()

        with self._lock:
            cursor = self.conn.cursor()
            try:
                session_id = self._get_harness_commit_session_id_locked(
                    cursor,
                    session_name,
                    turn_id,
                    step_id,
                )
                message_ids = self._insert_message_batch_locked(
                    cursor,
                    session_name,
                    serialized,
                )
                if has_new_media:
                    self._fold_stale_media_locked(cursor, session_id)

                created_at_ms = (
                    time.time_ns() // 1_000_000
                    if _created_at_ms is None
                    else int(_created_at_ms)
                )
                for draft in events:
                    cursor.execute(
                        "UPDATE harness_turns SET last_sequence = last_sequence + 1 "
                        "WHERE turn_id = ?",
                        (turn_id,),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(f"missing harness turn: {turn_id}")
                    cursor.execute(
                        "SELECT last_sequence FROM harness_turns WHERE turn_id = ?",
                        (turn_id,),
                    )
                    sequence = int(cursor.fetchone()[0])
                    cursor.execute(
                        "INSERT INTO harness_events "
                        "(turn_id, step_id, sequence, kind, created_at_ms, message_id, "
                        "tool_name, call_ordinal, arguments_sha256, result_sha256, "
                        "duration_ms, result_status, error_code) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            turn_id,
                            draft.step_id,
                            sequence,
                            draft.kind.value,
                            int(created_at_ms),
                            message_ids[draft.message_index],
                            draft.tool_name,
                            draft.call_ordinal,
                            draft.arguments_sha256,
                            draft.result_sha256,
                            draft.duration_ms,
                            draft.result_status,
                            draft.error_code,
                        ),
                    )

                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                self.conn.commit()
                return message_ids
            except Exception:
                self.conn.rollback()
                raise

    def _fold_stale_media_locked(self, cursor, session_id):
        """把会话中除最新一条外的含媒体 user 消息折叠为文本占位符（入库时一次性）。

        在含媒体的新 user 消息写入的同一事务内调用，替代请求时改写历史的旧路径；
        折叠后字节永久冻结（占位符是 text block，不再匹配媒体折叠条件）。
        """
        cursor.execute(
            "SELECT id, content FROM messages WHERE session_id = ? AND role = 'user' "
            "ORDER BY id ASC",
            (session_id,),
        )
        media_rows = []
        for msg_id, raw in cursor.fetchall():
            parsed, ok = _try_parse_json(raw)
            if ok and _content_has_media(parsed):
                media_rows.append((msg_id, parsed))
        folded = 0
        for msg_id, blocks in media_rows[:-1]:  # 最新一条保留原样
            new_blocks = [
                {"type": "text", "text": _FOLDED_MEDIA_TEXT}
                if isinstance(b, dict) and b.get("type") in ("image", "image_url")
                else b
                for b in blocks
            ]
            cursor.execute(
                'UPDATE messages SET content = ? WHERE id = ?',
                (json.dumps(new_blocks, ensure_ascii=False), msg_id),
            )
            folded += 1
        return folded

    def ensure_session(self, session_name, workdir=None, source="GUI"):
        """确保会话记录存在（不存在则创建），返回是否为本次新建。

        与 create_session_explicit 的区别：本方法会明确告知调用方会话是否已存在，
        供 GUI 启动时"准备当前会话"这类幂等场景使用。
        """
        wdir = workdir if workdir == "GLOBAL" else (_normalize_workdir(workdir) if workdir else self.workdir)
        with self._lock:
            cursor = self.conn.cursor()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
                if cursor.fetchone() is not None:
                    return False
                cursor.execute('''
                    INSERT OR IGNORE INTO sessions (session_name, workdir, created_at, updated_at, source)
                    VALUES (?, ?, ?, ?, ?)
                ''', (session_name, wdir, now, now, source))
                self.conn.commit()
                return True
            except Exception:
                self.conn.rollback()
                raise

    def update_session_workdir(self, session_name, workdir):
        """把已有会话重新绑定到指定工作目录（或 'GLOBAL' 自由对话分组）。

        供 GUI 在会话进行中切换项目工作区时迁移当前会话的归属分组，
        否则启动时挂在 GLOBAL 下的会话即使已进入项目文件夹，
        侧栏仍会一直显示在"对话"分组里。
        """
        wdir = workdir if workdir == "GLOBAL" else _normalize_workdir(workdir)
        with self._lock:
            cursor = self.conn.cursor()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                cursor.execute('''
                    UPDATE sessions SET workdir = ?, updated_at = ?
                    WHERE session_name = ?
                ''', (wdir, now, session_name))
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception:
                self.conn.rollback()
                raise

    def set_session_model_id(self, session_name: str, model_id: str | None) -> bool:
        """绑定会话级主模型。传 None/空字符串则清除绑定，回退到全局默认模型。"""
        mid = str(model_id or "").strip() or None
        with self._lock:
            cursor = self.conn.cursor()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                cursor.execute('''
                    UPDATE sessions SET active_model_id = ?, updated_at = ?
                    WHERE session_name = ?
                ''', (mid, now, session_name))
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception:
                self.conn.rollback()
                raise

    def get_session_model_id(self, session_name: str) -> str | None:
        """取会话级主模型 ID；未设置返回 None，调用方应回退到全局默认。"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT active_model_id FROM sessions WHERE session_name = ?
            ''', (session_name,))
            row = cursor.fetchone()
            return str(row[0]).strip() if row and row[0] else None

    def clear_session_messages(self, session_name):
        """清空指定会话的全部消息，但保留会话本身（供 GUI "清空历史" 使用）。

        同时重置记忆摘要与压缩游标，避免清空后仍向大模型注入旧摘要。
        返回被删除的消息条数；会话不存在时返回 0。
        """
        deleted = 0
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
                row = cursor.fetchone()
                if row is None:
                    return 0
                session_id = row[0]
                cursor.execute('SELECT COUNT(*) FROM messages WHERE session_id = ?', (session_id,))
                deleted = cursor.fetchone()[0] or 0
                cursor.execute('DELETE FROM harness_turns WHERE session_id = ?', (session_id,))
                cursor.execute('DELETE FROM messages WHERE session_id = ?', (session_id,))
                self._delete_deepseek_prompt_generations_locked(cursor, [session_name])
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute('''
                    UPDATE sessions
                    SET memory_summary = NULL, summarized_until_msg_id = 0, updated_at = ?
                    WHERE id = ?
                ''', (now, session_id))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        from core.llm import clear_cache_usage_session
        clear_cache_usage_session(session_name)
        return deleted

    def create_session_explicit(self, session_name, workdir=None, source="GUI"):
        """明确指定 workdir（如 'GLOBAL' 或具体路径）创建一个会话记录。"""
        wdir = workdir if workdir == "GLOBAL" else (_normalize_workdir(workdir) if workdir else self.workdir)
        with self._lock:
            cursor = self.conn.cursor()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                cursor.execute('''
                    INSERT OR IGNORE INTO sessions (session_name, workdir, created_at, updated_at, source)
                    VALUES (?, ?, ?, ?, ?)
                ''', (session_name, wdir, now, now, source))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise


    def get_history(self, session_name):
        """翻旧账技能：根据会话名字，把历史记录全拿出来"""
        with self._lock:
            cursor = self.conn.cursor()

            # 1. 先去查查有没有这个档案袋
            cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
            row = cursor.fetchone()

            # 如果连档案袋都找不到，说明是新聊天，直接返回空列表 []
            if row is None:
                return []

            session_id = row[0]

            # 2. 根据档案袋 ID，把所有的聊天记录按时间顺序（id 从小到大）查出来
            cursor.execute('''
                SELECT role, content FROM messages
                WHERE session_id = ?
                ORDER BY id ASC
            ''', (session_id,))

            # 3. 把查出来的数据，打包成 Python 认识的格式（字典列表）
            history = []
            for record in cursor.fetchall():
                role = record[0]
                raw_content = record[1]
                parsed, ok = _try_parse_json(raw_content)
                if not ok:
                    history.append({"role": role, "content": raw_content})
                elif isinstance(parsed, dict):
                    msg = {"role": role}
                    msg.update(parsed)
                    # 数据库列中的 role 是权威值：防止消息正文恰好是含 "role" 键的
                    # JSON 时覆盖真实角色（历史损坏 + 注入向量）
                    msg["role"] = role
                    history.append(msg)
                else:
                    history.append({"role": role, "content": parsed})


            return history
            return history

    def get_history_for_llm(self, session_name):
        """获取专门供给大模型的历史，剔除已被总结的段落，并在最前面附带记忆摘要"""
        with self._lock:
            cursor = self.conn.cursor()

            cursor.execute('SELECT id, memory_summary, summarized_until_msg_id FROM sessions WHERE session_name = ?', (session_name,))
            row = cursor.fetchone()
            if row is None:
                return []

            session_id = row[0]
            memory_summary = row[1]
            summarized_until_msg_id = row[2] or 0

            # 仅获取 id > summarized_until_msg_id 的消息
            cursor.execute('''
                SELECT role, content FROM messages
                WHERE session_id = ? AND id > ?
                ORDER BY id ASC
            ''', (session_id, summarized_until_msg_id))

            history = []
            for record in cursor.fetchall():
                role = record[0]
                raw_content = record[1]
                parsed, ok = _try_parse_json(raw_content)
                if not ok:
                    history.append({"role": role, "content": raw_content})
                    continue

                if isinstance(parsed, dict):
                    normalized = {"role": role}
                    normalized.update(parsed)
                    normalized["role"] = role
                    # 剔除模型显示元数据（_display 仅供 UI 展示），
                    # 避免混入发给 LLM 的提示词
                    normalized.pop("_display", None)
                    history.append(normalized)
                else:
                    history.append({"role": role, "content": parsed})

            # 修复 tool_call 配对（如压缩边界切断了工具调用组），
            # 防止坏历史让 API 报 "tool_call_id is not found" 后会话彻底不可用
            history = _sanitize_tool_call_pairing(history)

            # 如果存在背景记忆摘要，将其包装为参考上下文并插在首部。
            # 注意 role 必须是 user 而非 system：持久化的上下文块（动态运行上下文/
            # 时间注记等）使用 system 角色，摘要用 user 角色可以与之明确区分，
            # 且位置固定在 history[0]，在两次压缩之间保持稳定，
            # 不破坏 provider 侧 prefix caching。
            if memory_summary:
                history.insert(0, {
                    "role": "user",
                    "content": f"[历史对话记忆摘要 (以下是前段长对话的归纳记忆，请作为背景上下文参考，无需直接回复这段摘要)]\n{memory_summary}"
                })

            return history

    def undo_last_interaction(self, session_name):
        """抹除大模型最近一次回答的记忆（防止大模型误以为文件没被回退）"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
            row = cursor.fetchone()
            if not row: return
            session_id = row[0]

            # 找到最后一条 user 记录的 ID
            cursor.execute('''
                SELECT id FROM messages 
                WHERE session_id = ? AND role = 'user' 
                ORDER BY id DESC LIMIT 1
            ''', (session_id,))
            last_user = cursor.fetchone()

            if last_user:
                user_msg_id = last_user[0]
                # 删除这条 user 提问【之后】产生的所有 assistant 和 tool 记录
                try:
                    cursor.execute('''
                        DELETE FROM messages
                        WHERE session_id = ? AND id > ?
                    ''', (session_id, user_msg_id))
                    # 同步失效压缩产物：撤销后旧摘要内容已失真（可能仍描述被撤销的
                    # 修改），继续注入会误导模型；回退水位让被摘要排除的历史重新可见，
                    # 超限由水位线阶梯在下轮重新压缩。与删除同事务，避免半完成状态。
                    cursor.execute('''
                        UPDATE sessions
                        SET memory_summary = NULL, summarized_until_msg_id = 0
                        WHERE id = ? AND (memory_summary IS NOT NULL OR summarized_until_msg_id)
                    ''', (session_id,))
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise

    def get_all_sessions(self, workdir=None, source=None, include_empty=False):
        """返回会话列表（名称/消息数/归属目录/时间/来源）。

        workdir 为 None 时返回全部会话；指定路径时只返回该目录下创建的会话。
        source 参数用于过滤 GUI 或 CLI 专属会话。
        include_empty=False 时过滤掉无任何消息的空会话（参考 kimicode 的 is_empty 策略），
        避免会话列表被空会话污染；磁盘记录仍保留，不删除。
        """
        with self._lock:
            cursor = self.conn.cursor()
            if source:
                cursor.execute('''
                    SELECT s.session_name, COUNT(m.id) as msg_count,
                           s.workdir, s.updated_at, s.created_at, s.source
                    FROM sessions s
                    LEFT JOIN messages m ON s.id = m.session_id
                    WHERE s.source = ? OR s.source IS NULL
                    GROUP BY s.id
                    ORDER BY s.id DESC
                ''', (source,))
            else:
                cursor.execute('''
                    SELECT s.session_name, COUNT(m.id) as msg_count,
                           s.workdir, s.updated_at, s.created_at, s.source
                    FROM sessions s
                    LEFT JOIN messages m ON s.id = m.session_id
                    GROUP BY s.id
                    ORDER BY s.id DESC
                ''')
            rows = cursor.fetchall()
        if workdir is None:
            filtered = rows
        else:
            target = _normalize_workdir(workdir)
            filtered = [
                row for row in rows
                if not row[2] or _normalize_workdir(row[2]) == target
            ]
        # 参考 kimicode：列表过滤空会话（msg_count=0），磁盘保留不删除
        if not include_empty:
            filtered = [row for row in filtered if row[1] > 0]
        return [
            {
                "name": row[0],
                "count": row[1],
                "workdir": row[2],
                "updated_at": row[3],
                "created_at": row[4],
                "source": row[5] or "GUI"
            }
            for row in filtered
        ]

    def get_memory_summary(self, session_name):
        """获取当前会话的背景记忆摘要"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('SELECT memory_summary FROM sessions WHERE session_name = ?', (session_name,))
            row = cursor.fetchone()
            return row[0] if row else None

    def delete_session(self, session_name):
        """删除指定会话及其所有消息"""
        deleted = False
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
            row = cursor.fetchone()
            if row:
                session_id = row[0]
                try:
                    cursor.execute('DELETE FROM messages WHERE session_id = ?', (session_id,))
                    self._delete_deepseek_prompt_generations_locked(cursor, [session_name])
                    cursor.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
                    self.conn.commit()
                    deleted = True
                except Exception:
                    self.conn.rollback()
                    raise
        if deleted:
            from core.llm import clear_cache_usage_session
            clear_cache_usage_session(session_name)

    def delete_sessions_by_workdir(self, workdir):
        """删除指定工作目录下的所有历史会话及其消息记录（仅删除 DB 记录）"""
        workdir_norm = _normalize_workdir(workdir)
        deleted_names = []
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    'SELECT id, session_name FROM sessions WHERE workdir = ?',
                    (workdir_norm,),
                )
                rows = cursor.fetchall()
                for r in rows:
                    sid = r[0]
                    deleted_names.append(r[1])
                    cursor.execute('DELETE FROM messages WHERE session_id = ?', (sid,))
                self._delete_deepseek_prompt_generations_locked(cursor, deleted_names)
                cursor.execute('DELETE FROM sessions WHERE workdir = ?', (workdir_norm,))
                cursor.execute('DELETE FROM workspaces WHERE workdir = ?', (workdir_norm,))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        if deleted_names:
            from core.llm import clear_cache_usage_session
            for session_name in deleted_names:
                clear_cache_usage_session(session_name)

    def add_workspace(self, workdir):
        if not workdir or workdir == "GLOBAL":
            return
        wdir = _normalize_workdir(workdir)
        with self._lock:
            cursor = self.conn.cursor()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                cursor.execute('''
                    INSERT OR IGNORE INTO workspaces (workdir, created_at)
                    VALUES (?, ?)
                ''', (wdir, now))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def get_all_workspaces(self):
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('SELECT workdir FROM workspaces')
            return [_normalize_workdir(row[0]) for row in cursor.fetchall()]

    def delete_workspace(self, workdir):
        if not workdir or workdir == "GLOBAL":
            return
        wdir = _normalize_workdir(workdir)
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute('DELETE FROM workspaces WHERE workdir = ?', (wdir,))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def rename_session(self, old_name, new_name):
        """重命名指定会话，确保新名称在数据库中唯一。如果名字冲突则自动加数字后缀。"""
        # 空名字/纯空白名字无意义，直接拒绝，避免产生空 session_name
        if not isinstance(new_name, str) or not new_name.strip():
            return False, old_name
        result = (False, old_name)
        with self._lock:
            cursor = self.conn.cursor()

            # 1. 查找旧会话是否存在
            cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (old_name,))
            row = cursor.fetchone()
            if not row:
                return False, old_name

            session_id = row[0]

            # 2. 检查新名字是否冲突并生成唯一名字（统一以 strip 后的基准名加后缀）
            base_name = new_name.strip()
            final_name = base_name
            suffix = 1
            while True:
                cursor.execute('SELECT id FROM sessions WHERE session_name = ? AND id != ?', (final_name, session_id))
                if not cursor.fetchone():
                    break
                final_name = f"{base_name}_{suffix}"
                suffix += 1

            # 3. 更新会话名称
            try:
                cursor.execute('UPDATE sessions SET session_name = ? WHERE id = ?', (final_name, session_id))
                cursor.execute(
                    'UPDATE deepseek_prompt_generations SET session_name = ? WHERE session_name = ?',
                    (final_name, old_name),
                )
                self.conn.commit()
                result = (True, final_name)
            except Exception:
                # 回滚半残更新，避免被下一次 commit 幽灵提交
                self.conn.rollback()
                return False, old_name
        from core.llm import rename_cache_usage_session
        rename_cache_usage_session(old_name, final_name)
        return result

    def prune_history_files(self, session_name, max_turns=16, retain_recent=4):
        """物理级折叠历史挂载的大文件，以节省 token。"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
            row = cursor.fetchone()
            if not row: return
            session_id = row[0]

            cursor.execute('SELECT id, role, content FROM messages WHERE session_id = ? ORDER BY id ASC', (session_id,))
            records = cursor.fetchall()

            if len(records) <= max_turns:
                return

            updates = []
            error_count = 0
            for i in range(len(records) - retain_recent):
                msg_id, role, raw_content = records[i]
                if role != "user":
                    continue
                try:
                    content_obj, ok = _try_parse_json(raw_content)
                    if not ok:
                        # 纯文本消息：直接按字符串处理，回写时保持原始纯文本格式
                        content_obj = raw_content
                    modified = False

                    def prune_text(text):
                        if "[附带上下文:" in text:
                            return re.sub(
                                r"(\[附带上下文: .*?\])\n```.*?\n```\n?", 
                                r"\1 (⚠️ 内容已折叠以节省 Token。若需再次查看请使用 read_file 工具)\n\n", 
                                text, 
                                flags=re.DOTALL
                            )
                        return text

                    if isinstance(content_obj, list):
                        for block in content_obj:
                            if block.get("type") == "text":
                                old_text = block.get("text", "")
                                new_text = prune_text(old_text)
                                if new_text != old_text:
                                    block["text"] = new_text
                                    modified = True
                    elif isinstance(content_obj, str):
                        new_text = prune_text(content_obj)
                        if new_text != content_obj:
                            content_obj = new_text
                            modified = True

                    if modified:
                        # 只有原本是 JSON 结构的消息才以 JSON 回写，纯文本保持原样
                        new_raw = json.dumps(content_obj, ensure_ascii=False) if ok else content_obj
                        updates.append((new_raw, msg_id))
                except Exception:
                    error_count += 1

            if error_count:
                # 诊断日志：折叠失败的条目不阻断整体流程，但要留下痕迹
                import logging
                logging.getLogger("py.warnings").warning(
                    "prune_history_files: %d 条消息折叠失败 (session=%s)", error_count, session_name
                )

            if updates:
                try:
                    cursor.executemany('UPDATE messages SET content = ? WHERE id = ?', updates)
                    # 同步更新会话时间戳，避免外部基于 updated_at 的缓存/排序与
                    # 实际内容变化脱节。
                    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute(
                        'UPDATE sessions SET updated_at = ? WHERE session_name = ?',
                        (now, session_name)
                    )
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise

    def evict_oldest_turn_persistent(self, session_name):
        """DB 层滑窗淘汰：推进 summarized_until_msg_id，把最早一个完整轮次
        （首个非 system 消息到下一个 user 之前）一次性排除出活跃历史。

        只推进水位、不改写任何消息字节（字节稳定性铁律允许的一次性 DB 层水位
        推进）。纯内存淘汰会在下一轮 prepare 时被 get_history_for_llm 全量读回
        而失效，淘汰必须持久化才真正生效。
        返回 True 表示确实淘汰了一个轮次，False 表示没有可安全淘汰的内容。
        """
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    'SELECT id, summarized_until_msg_id FROM sessions WHERE session_name = ?',
                    (session_name,),
                )
                row = cursor.fetchone()
                if not row:
                    return False
                session_id = row[0]
                boundary = row[1] or 0
                cursor.execute(
                    'SELECT id, role FROM messages WHERE session_id = ? AND id > ? ORDER BY id ASC',
                    (session_id, boundary),
                )
                rows = cursor.fetchall()
                start = next(
                    (i for i, r in enumerate(rows) if r[1] != "system"),
                    None,
                )
                if start is None:
                    return False
                end = next(
                    (j for j in range(start + 1, len(rows)) if rows[j][1] == "user"),
                    len(rows),
                )
                if end >= len(rows):
                    # 最早轮次已是最后轮次：再淘汰会清空活跃历史，拒绝
                    return False
                new_boundary = rows[end - 1][0]
                if new_boundary <= boundary:
                    return False
                cursor.execute(
                    'UPDATE sessions SET summarized_until_msg_id = ? WHERE id = ?',
                    (new_boundary, session_id),
                )
                self.conn.commit()
                return True
            except Exception:
                self.conn.rollback()
                raise

    def maintain_tool_results(self, session_name, tier=1, protection_tokens=8000):
        """分层本地维护旧工具结果（Snip/Elide），DB 层一次性改写并永久冻结。

        tier=1 截断（snip）；tier=2 占位符化（elide，snipped 可升级一次）。
        尾部 protection_tokens 估算 token 为保护区内消息，永不改写。
        只处理 id > summarized_until_msg_id 的活跃消息（已被摘要排除的消息
        不在请求前缀里，无需维护）。
        返回 (snipped_count, elided_count)。
        """
        from core.tool_result_maintenance import (
            elide_tool_result,
            estimate_text_tokens,
            snip_tool_result,
        )
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                'SELECT id, summarized_until_msg_id FROM sessions WHERE session_name = ?',
                (session_name,),
            )
            row = cursor.fetchone()
            if not row:
                return (0, 0)
            session_id, summarized_until = row[0], row[1] or 0
            cursor.execute(
                'SELECT id, role, content FROM messages '
                'WHERE session_id = ? AND id > ? ORDER BY id ASC',
                (session_id, summarized_until),
            )
            records = cursor.fetchall()

            # 保护区：从尾部向前累加估算 token
            protection_start = 0
            total = 0
            for i in range(len(records) - 1, -1, -1):
                total += estimate_text_tokens(str(records[i][2] or ""))
                if total >= protection_tokens:
                    protection_start = i
                    break

            updates = []
            snipped = elided = 0
            for i in range(protection_start):
                msg_id, role, raw = records[i]
                if role != "tool":
                    continue
                parsed, ok = _try_parse_json(raw)
                if ok and isinstance(parsed, dict):
                    name = str(parsed.get("name") or "")
                    text = str(parsed.get("content") or "")
                else:
                    name, text = "", str(raw or "")

                new_text = None
                if tier >= 2:
                    new_text = elide_tool_result(name, text)
                    if new_text is not None:
                        elided += 1
                if new_text is None and tier >= 1:
                    new_text = snip_tool_result(name, text)
                    if new_text is not None:
                        snipped += 1
                if new_text is None:
                    continue
                if ok and isinstance(parsed, dict):
                    parsed["content"] = new_text
                    new_raw = json.dumps(parsed, ensure_ascii=False)
                else:
                    new_raw = new_text
                updates.append((new_raw, msg_id))

            if updates:
                try:
                    cursor.executemany('UPDATE messages SET content = ? WHERE id = ?', updates)
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise
            return (snipped, elided)


# Backward-compatible public name used by the original test and extension API.
SessionManager = MemoryManager
