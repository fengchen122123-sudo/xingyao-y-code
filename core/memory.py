import datetime
import json
import logging
import os
import re
import sqlite3
import threading


import pathlib

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
    return session_name or getattr(_undo_tls, "session_name", None) or "_global_"

# 会话级检查点缓存（按 session_name → list of checkpoint dict）
# 每轮对话开始前自动打一个 checkpoint，记录本轮所有被修改文件的旧内容
# /checkpoint list 查看，/checkpoint restore <id> 回滚到该检查点
# 设计参考 Grok Build 的 workspace 级 checkpoint
_session_checkpoints_lock = threading.Lock()
_session_checkpoints: dict[str, list[dict]] = {}


def set_undo_state(file_path, old_content, session_name=None):
    session = _current_undo_session(session_name)
    with _undo_state_lock:
        LAST_UNDO_STATE[session] = {"file_path": file_path, "old_content": old_content}
    # 同步追加到当前会话的检查点文件快照
    # （由 worker 线程在 begin_turn 时已建好当前 turn 的 checkpoint 上下文）
    if file_path and old_content is not None:
        record_file_snapshot_for_current_turn(file_path, old_content)


def get_undo_state(session_name=None):
    session = _current_undo_session(session_name)
    with _undo_state_lock:
        # 返回副本，避免调用方在无锁状态下读写共享 dict 产生竞态
        return dict(LAST_UNDO_STATE.get(session, {"file_path": None, "old_content": None}))


# ---------------------------------------------------------------------------
# 会话级检查点 API
# ---------------------------------------------------------------------------
# 设计：每轮对话开始前由 main._background_worker 调用 begin_turn_checkpoint，
# 把 turn_id + last_msg_id 记下来；之后 write_file/edit_file 调用 set_undo_state
# 时同步把旧内容追加到该 turn 的快照列表里。
# 用户 /checkpoint restore <turn_id> 时，把所有快照按反序回写文件。

# 当前 worker 线程的活动 turn 上下文（thread-local，避免多线程串扰）
_checkpoint_tls = threading.local()


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
    _checkpoint_tls.current = ctx
    return ctx


def record_file_snapshot_for_current_turn(file_path, old_content):
    """把当前线程内被修改文件的旧内容追加到当前 turn 的快照列表。"""
    ctx = getattr(_checkpoint_tls, "current", None)
    if ctx is None or ctx.get("completed"):
        return
    # 同一文件在一轮内可能被多次修改，只保留最早的旧内容（最原始状态）
    existing = next(
        (s for s in ctx["file_snapshots"] if s["file_path"] == file_path),
        None,
    )
    if existing is None:
        ctx["file_snapshots"].append({
            "file_path": file_path,
            "old_content": old_content,
        })


def end_turn_checkpoint():
    """turn 结束时把当前上下文提交到全局缓存，便于后续 /checkpoint 查询和回滚。"""
    ctx = getattr(_checkpoint_tls, "current", None)
    if ctx is None:
        return None
    ctx["completed"] = True
    session_name = ctx["session_name"]
    with _session_checkpoints_lock:
        bucket = _session_checkpoints.setdefault(session_name, [])
        bucket.append(ctx)
        # 限制每个会话最多保留 50 个检查点，防止内存无限增长
        if len(bucket) > 50:
            del bucket[: len(bucket) - 50]
    _checkpoint_tls.current = None
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

                # 创建一个叫 messages 的表
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER,
                        role TEXT,
                        content TEXT
                    )
                ''')
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
                        session_id TEXT
                    )
                ''')
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_token_usage_date ON token_usage(date)"
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
                        cached_tokens, total_tokens, session_id=None, ts=None):
        """追加一条真实 token 用量记录（由 core/llm.py 流结束记账点调用）。"""
        import time
        if ts is None:
            ts = int(time.time())
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        with self._lock:
            self.conn.execute(
                "INSERT INTO token_usage "
                "(ts, date, provider, model, prompt_tokens, completion_tokens, "
                "cached_tokens, total_tokens, session_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    int(ts), day, str(provider or ""), str(model or ""),
                    int(prompt_tokens or 0), int(completion_tokens or 0),
                    None if cached_tokens is None else int(cached_tokens),
                    int(total_tokens or 0),
                    str(session_id) if session_id is not None else None,
                ),
            )
            self.conn.commit()

    def get_last_token_usage(self, session_id):
        """查询指定会话最近一次真实 token 用量，无记录返回 None。

        供窗口占用显示与压缩触发判断使用真实基线（下一轮请求的 prompt
        必然包含上一轮的 prompt + completion），避免本地估算。
        """
        with self._lock:
            cursor = self.conn.execute(
                "SELECT prompt_tokens, completion_tokens, total_tokens, cached_tokens "
                "FROM token_usage WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (str(session_id),),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return {
            "prompt_tokens": int(row[0] or 0),
            "completion_tokens": int(row[1] or 0),
            "total_tokens": int(row[2] or 0),
            "cached_tokens": None if row[3] is None else int(row[3]),
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
                SELECT provider, model,
                       SUM(prompt_tokens), SUM(completion_tokens),
                       SUM(cached_tokens),
                       SUM(CASE WHEN cached_tokens IS NOT NULL THEN prompt_tokens ELSE 0 END),
                       SUM(total_tokens)
                FROM token_usage
                WHERE date >= ? AND date <= ?
                GROUP BY provider, model
                ORDER BY SUM(total_tokens) DESC
                """,
                (str(start_date), str(end_date)),
            )
            return [
                {
                    "provider": row[0] or "",
                    "model": row[1] or "",
                    "prompt": row[2] or 0,
                    "completion": row[3] or 0,
                    "cached": row[4] or 0,
                    "cache_prompt": row[5] or 0,
                    "total": row[6] or 0,
                }
                for row in cursor.fetchall()
            ]

    def query_cache_invalidation_events(
        self, start_date, end_date, min_prompt_tokens=20000, max_hit_ratio=0.5
    ):
        """统计指定日期区间内的“缓存全失效”事件：大输入但命中率极低。

        典型特征：上一轮还 95%+ 命中，本轮 cached 骤降到接近 0，随后一轮又恢复——
        这是 provider/网关侧前缀缓存条目被驱逐的标志，客户端无法避免。
        返回 {"count": 事件数, "lost_tokens": 这些事件多消耗的输入 token}。
        """
        with self._lock:
            cursor = self.conn.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(prompt_tokens - cached_tokens), 0)
                FROM token_usage
                WHERE date >= ? AND date <= ?
                  AND prompt_tokens >= ?
                  AND cached_tokens < prompt_tokens * ?
                """,
                (str(start_date), str(end_date),
                 int(min_prompt_tokens), float(max_hit_ratio)),
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

    def add_messages(self, session_name, messages):
        """Atomically append one ordered batch of conversation messages."""
        serialized = []
        has_new_media = False
        for role, content in messages:
            # 注意：媒体判断必须用原始 list（json.dumps 之前就判定）
            if str(role) == "user" and _content_has_media(content):
                has_new_media = True
            if isinstance(content, (list, dict)):
                content = json.dumps(content, ensure_ascii=False)
            else:
                content = str(content)
            serialized.append((str(role), content))
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
                    cursor.execute('''
                        INSERT INTO sessions (session_name, workdir, created_at, updated_at, source)
                        VALUES (?, ?, ?, ?, 'GUI')
                    ''', (session_name, target_wdir, now, now))
                    session_id = cursor.lastrowid
                cursor.executemany('''
                    INSERT INTO messages (session_id, role, content)
                    VALUES (?, ?, ?)
                ''', [
                    (session_id, role, content)
                    for role, content in serialized
                ])
                # 新媒体入库的同一事务内折叠旧媒体：请求路径不再改写历史中间字节
                if has_new_media:
                    self._fold_stale_media_locked(cursor, session_id)
                # 更新最近活跃时间，供会话列表按时间展示
                cursor.execute('UPDATE sessions SET updated_at = ? WHERE id = ?', (now, session_id))
                self.conn.commit()
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

    def clear_session_messages(self, session_name):
        """清空指定会话的全部消息，但保留会话本身（供 GUI "清空历史" 使用）。

        同时重置记忆摘要与压缩游标，避免清空后仍向大模型注入旧摘要。
        返回被删除的消息条数；会话不存在时返回 0。
        """
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
                cursor.execute('DELETE FROM messages WHERE session_id = ?', (session_id,))
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute('''
                    UPDATE sessions
                    SET memory_summary = NULL, summarized_until_msg_id = 0, updated_at = ?
                    WHERE id = ?
                ''', (now, session_id))
                self.conn.commit()
                return deleted
            except Exception:
                self.conn.rollback()
                raise

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
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise

    def get_all_sessions(self, workdir=None, source=None, include_empty=False):
        """返回会话列表（名称/消息数/归属目录/时间/来源）。

        workdir 为 None 时返回全部会话；指定路径时只返回该目录下创建的会话。
        source 参数用于过滤 GUI 或 TUI 专属会话。
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
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
            row = cursor.fetchone()
            if row:
                session_id = row[0]
                try:
                    cursor.execute('DELETE FROM messages WHERE session_id = ?', (session_id,))
                    cursor.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise

    def delete_sessions_by_workdir(self, workdir):
        """删除指定工作目录下的所有历史会话及其消息记录（仅删除 DB 记录）"""
        workdir_norm = _normalize_workdir(workdir)
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute('SELECT id FROM sessions WHERE workdir = ?', (workdir_norm,))
                rows = cursor.fetchall()
                for r in rows:
                    sid = r[0]
                    cursor.execute('DELETE FROM messages WHERE session_id = ?', (sid,))
                cursor.execute('DELETE FROM sessions WHERE workdir = ?', (workdir_norm,))
                cursor.execute('DELETE FROM workspaces WHERE workdir = ?', (workdir_norm,))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

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
                self.conn.commit()
                return True, final_name
            except Exception:
                # 回滚半残更新，避免被下一次 commit 幽灵提交
                self.conn.rollback()
                return False, old_name

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
