import datetime
import json
import logging
import os
import re
import sqlite3
import threading


def _normalize_workdir(path):
    """规范化工作目录路径（绝对路径 + 平台大小写归一），用于会话归属目录比较。"""
    try:
        return os.path.normcase(os.path.abspath(str(path)))
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

# 全局撤销状态（单文件 undo 栈，向后兼容保留）
LAST_UNDO_STATE = {
    "file_path": None,
    "old_content": None
}

# 会话级检查点缓存（按 session_name → list of checkpoint dict）
# 每轮对话开始前自动打一个 checkpoint，记录本轮所有被修改文件的旧内容
# /checkpoint list 查看，/checkpoint restore <id> 回滚到该检查点
# 设计参考 Grok Build 的 workspace 级 checkpoint
_session_checkpoints_lock = threading.Lock()
_session_checkpoints: dict[str, list[dict]] = {}


def set_undo_state(file_path, old_content):
    LAST_UNDO_STATE["file_path"] = file_path
    LAST_UNDO_STATE["old_content"] = old_content
    # 同步追加到当前会话的检查点文件快照
    # （由 worker 线程在 begin_turn 时已建好当前 turn 的 checkpoint 上下文）
    if file_path and old_content is not None:
        record_file_snapshot_for_current_turn(file_path, old_content)


def get_undo_state():
    return LAST_UNDO_STATE


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
        # 1. 连接数据库（check_same_thread=False 允许多线程访问）
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        # 2. 启用 WAL 模式，提升多线程并发读写性能，防止数据库锁死
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
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

            # 创建一个叫 messages 的表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER,
                    role TEXT,
                    content TEXT
                )
            ''')
            # 保存修改
            self.conn.commit()

    def checkpoint_wal(self):
        """主动执行 WAL checkpoint，整理被写的预写日志，防止 ycode.db-wal 文件体积无限膨胀。"""
        with self.lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            except Exception as e:
                try:
                    logging.getLogger("py.warnings").warning("WAL checkpoint 执行失败: %s", e)
                except Exception:
                    pass

    def close(self):
        """关闭数据库连接前自动刷盘 WAL 日志。"""
        with self.lock:
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
        for role, content in messages:
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
                cursor.execute('''
                    INSERT OR IGNORE INTO sessions (session_name, workdir, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                ''', (session_name, self.workdir, now, now))
                cursor.execute(
                    'SELECT id FROM sessions WHERE session_name = ?',
                    (session_name,),
                )
                session_id = cursor.fetchone()[0]
                cursor.executemany('''
                    INSERT INTO messages (session_id, role, content)
                    VALUES (?, ?, ?)
                ''', [
                    (session_id, role, content)
                    for role, content in serialized
                ])
                # 更新最近活跃时间，供会话列表按时间展示
                cursor.execute('UPDATE sessions SET updated_at = ? WHERE id = ?', (now, session_id))
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
                    normalized.pop("reasoning_content", None)
                    history.append(normalized)
                else:
                    history.append({"role": role, "content": parsed})

            # 如果存在背景记忆摘要，将其包装为 system 提示参考并插在首部
            if memory_summary:
                history.insert(0, {
                    "role": "system", 
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

    def get_all_sessions(self, workdir=None):
        """返回会话列表（名称/消息数/归属目录/时间）。

        workdir 为 None 时返回全部会话；指定路径时只返回该目录下创建的会话，
        以及历史遗留的无目录记录（workdir 为 NULL 的旧会话，避免升级后历史“消失”）。
        """
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT s.session_name, COUNT(m.id) as msg_count,
                       s.workdir, s.updated_at, s.created_at
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
        return [
            {
                "name": row[0],
                "count": row[1],
                "workdir": row[2],
                "updated_at": row[3],
                "created_at": row[4],
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
                cursor.executemany('UPDATE messages SET content = ? WHERE id = ?', updates)
            self.conn.commit()


# Backward-compatible public name used by the original test and extension API.
SessionManager = MemoryManager
