import base64
import datetime
import gzip
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.cache_health import classify_lanes_health
from core.harness.models import JournalEventDraft
from core.tool_result_maintenance import PROTECTION_ZONE_TOKENS, new_handle


import pathlib


# token 流水按 session_name 记账，而会话名可被新会话复用（旧会话被删除后
# sessions 行消失、名字随之释放）。因此按名字聚合必须叠加「会话创建时间
# 下界」，否则新会话会把同名旧会话的历史流水算进自己的累计（2026-09-29
# 状态行显示 3200 万输入即此因）。created_at 是本地时间文本，'utc' 修饰符
# 按系统本地时区把它换算成 epoch，与写入侧 int(time.time()) 口径一致。
_SESSION_TOKEN_TIME_BOUND_SQL = (
    "(s.created_at IS NULL OR s.created_at = '' "
    "OR t.ts >= CAST(strftime('%s', s.created_at, 'utc') AS INTEGER))"
)


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


def canonicalize_api_base(value):
    """公开别名：规范化 endpoint 身份，剥离 userinfo/query/fragment。

    规范化把凭据片段折算成不可逆摘要（最多保留 16 位哈希，绝不留明文），
    因此 api_base 写进 harness_steps 不会落密码或 token；断点续跑的路由
    快照可安全读取比对。
    """
    return _canonicalize_api_base(value)


def _normalize_workdir(path):
    """规范化工作目录路径（真实磁盘物理大小写），用于会话归属目录比较。"""
    try:
        p = pathlib.Path(path)
        if p.exists():
            return str(p.resolve())
        return os.path.abspath(str(path))
    except Exception:
        return str(path or "")


# 首条用户消息尾部常被追加运行时上下文/附件清单（【当前运行上下文】、
# [附带文件] 等）。这些系统块会把「模型」「当前时间」这类词带进检索文本，
# 让 @ 补全命中一堆无关会话，所以生成会话检索文本时要在第一个块之前截断。
_SESSION_SEARCH_CUT_MARKERS = (
    "\n\n【\u200b当前运行上下文】",  # 带零宽空格的版本（GUI 注入）
    "\n\n【当前运行上下文】",
    "\n\n[附带",
)


def _session_search_text(content, limit: int = 2000) -> str:
    """把首条用户消息压成 @ 提及检索用的文本（去系统注入块 + 限制长度）。"""
    text = str(content or "")
    cut = len(text)
    for marker in _SESSION_SEARCH_CUT_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut].strip()[:limit]


# 消息全文检索的归一化取「字符 bigram」而非 FTS5 内置 trigram/unicode61：
# - trigram 分词器要求查询 ≥3 字符，中文两字词（"缓存""路由"）会搜不到；
# - unicode61 会把一整串连续汉字当成单个 token，同样搜不到子串。
# 因此索引与查询两侧都走同一套自建归一化（零第三方依赖）：CJK 段切成相邻
# 两字的 bigram（单字段保留单字），非 CJK 按非字母数字切分、转小写、长度
# ≥2 保留。索引侧把结果存成空格连接的 token 串，查询侧把每个 token 加双引号
# 后用 AND 连接（双引号既做短语化也防 FTS5 语法注入）。
_CJK_CHAR_CLASS = (
    "\u3040-\u30ff"  # 日文假名
    "\u3400-\u4dbf"  # CJK 扩展 A
    "\u4e00-\u9fff"  # CJK 基本区
    "\uac00-\ud7af"  # 谚文音节
    "\uf900-\ufaff"  # CJK 兼容表意文字
)
_CJK_RUN_RE = re.compile(f"[{_CJK_CHAR_CLASS}]+")
_NON_ALNUM_RE = re.compile(r"[^0-9A-Za-z]+")


def _fts_normalize(text) -> str:
    """把任意文本归一化成 FTS 索引/查询共用的 token 串（空格连接）。"""
    raw = str(text or "")
    if not raw:
        return ""
    tokens = []
    for match in _CJK_RUN_RE.finditer(raw):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    for chunk in _NON_ALNUM_RE.split(_CJK_RUN_RE.sub(" ", raw)):
        token = chunk.lower()
        if len(token) >= 2:
            tokens.append(token)
    return " ".join(tokens)


def _fts_match_expression(normalized: str) -> str:
    """把归一化 token 串转成 FTS5 MATCH 表达式（每个 token 短语化后 AND）。"""
    tokens = [tok for tok in str(normalized or "").split() if tok]
    return " AND ".join(f'"{tok}"' for tok in tokens)


def _fts_snippet(text, query, radius=45):
    """从消息原文里截取命中位置附近的可读片段。

    索引列存的是 bigram token 串，FTS5 的 snippet() 只会在该列上取片段，
    结果会变成 `stable 《prefix》` 这类 token 串而不通读。因此索引只用于
    命中定位，片段一律回到 messages.content 原文上截取。

    定位策略：按查询里的字符逐个在原文中查找，取第一个命中的位置（中文
    不依赖分词边界，英文同样适用）；找不到则退化为原文开头。
    """
    body = str(text or "")
    if not body:
        return ""
    needle_pos = -1
    for ch in str(query or ""):
        if not ch.strip():
            continue
        needle_pos = body.find(ch)
        if needle_pos >= 0:
            break
    if needle_pos < 0:
        head = body[: radius * 2]
        return head + ("…" if len(body) > len(head) else "")
    start = max(0, needle_pos - radius)
    end = min(len(body), needle_pos + radius)
    fragment = body[start:end].replace("\r", " ").replace("\n", " ")
    result = ("…" if start > 0 else "") + fragment + ("…" if end < len(body) else "")
    # 与 search_sessions_and_messages 的片段脱敏口径保持一致：检索结果会
    # 直接进入模型上下文，历史消息里残留的密钥不能经由片段再次外泄。
    return re.sub(r"sk-[a-zA-Z0-9]{16,}", "sk-***", result)


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


def _folded_media_text(block):
    """折叠占位文本：带来源路径（如有），便于用户按路径找回原图。"""
    src = block.get("source_path") if isinstance(block, dict) else None
    if src:
        return f"[图片已折叠以节省 Token；原图路径: {src}]"
    return _FOLDED_MEDIA_TEXT


def _content_has_media(content):
    """判断消息内容是否含图片/视频块；同时识别两种持久化形态（Task 10）。

    1. content 列表（普通多模态消息）；
    2. 含 content 列表的完整消息 dict（带 _computer_v1 私有元数据的
       follow-up 观察消息，以完整 dict 入库）。
    """
    if isinstance(content, dict):
        content = content.get("content")
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


def _tool_call_is_replayable(tc) -> bool:
    """判定历史里的 assistant tool call 是否可原样回放给 provider。

    严格网关（如 Agnes AI）对 name 为空或 arguments 不是合法 JSON 的
    tool call 直接 400 整个请求，坏记录落库后该会话每轮复现。此判定
    只读不改：不可回放的调用连同其 tool 结果在回放历史中剔除，
    DB 原文不动，会话即可自愈。
    """
    if not isinstance(tc, dict):
        return False
    function = tc.get("function") or {}
    if not str(function.get("name") or "").strip():
        return False
    # 缺 arguments 键按无参调用处理（与执行层 _tool_call_arguments 的
    # 默认值一致）；键存在但为空串/非字符串/非法 JSON 才判不可回放
    raw = function.get("arguments", "{}")
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        json.loads(raw)
    except ValueError:
        return False
    return True


def _sanitize_tool_call_pairing(history):
    """修复 tool_call 配对，避免坏历史导致 API 报错后整个会话无法继续使用。

    四种损坏形态：
    1. 孤儿 tool 消息：tool_call_id 未在前面任何 assistant.tool_calls 中声明，
       API 会报 "tool_call_id is not found" → 直接丢弃该 tool 消息。
    2. assistant 声明了 tool_calls 但历史中缺少对应 tool 结果，
       API 会报 "assistant message with tool_calls must be followed by tool messages"
       → 移除该 assistant 的 tool_calls 字段（保留文本内容）。
    3. tool call 本身不可回放（函数名为空 / arguments 非法 JSON，
       典型成因：上游网关流式吐出残缺调用被原样落库）→ 从回放历史
       中剔除该调用，其 tool 结果按形态 1 连带丢弃。
    4. tool 结果与应答它的 assistant 之间被注入了别的消息（典型成因：
       编排提醒等 notice 消息在旧版落库顺序里插到了工具结果之前），严格
       网关同样报 "must be followed by tool messages" → 按 tool_call_id 把
       结果搬回对应 assistant 之后；正文一字不动，只调整位置。
    """
    announced = set()
    for msg in history:
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            continue
        replayable = [tc for tc in tool_calls if _tool_call_is_replayable(tc)]
        if len(replayable) != len(tool_calls):
            if replayable:
                msg["tool_calls"] = replayable
            else:
                msg.pop("tool_calls", None)
        for tc in replayable:
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

    # 形态 4：把被注入消息隔开的 tool 结果搬回所属 assistant 之后，
    # 让回放顺序满足 provider 的「tool_calls 必须被 tool 消息应答」约束。
    # 已合法相邻的调用组顺序保持不变（幂等，不扰动 provider 侧前缀缓存）。
    tool_message_by_id = {}
    for msg in sanitized:
        if msg.get("role") != "tool":
            continue
        tcid = msg.get("tool_call_id")
        if tcid and tcid not in tool_message_by_id:
            tool_message_by_id[tcid] = msg

    if tool_message_by_id:
        reordered = []
        for msg in sanitized:
            if msg.get("role") == "tool":
                # 结果统一由所属 assistant 就地安置，此处不再原样输出
                continue
            reordered.append(msg)
            for tool_call in msg.get("tool_calls") or []:
                tcid = tool_call.get("id") if isinstance(tool_call, dict) else None
                tool_message = tool_message_by_id.pop(tcid, None) if tcid else None
                if tool_message is not None:
                    reordered.append(tool_message)
        sanitized = reordered

    return sanitized


def _position_kind_for_role(role: str) -> str:
    """消息角色 → message_turn_links 位置种类的显式映射（规格 7.7）。

    回合内持久化的 user 角色消息（压缩摘要请求、接管通知等系统注入内容）
    归类为 notice；回合的发起用户消息由调用方显式传 'user'。
    未知角色显式失败，不塞默认值。
    """
    mapping = {
        "assistant": "assistant_intermediate",
        "tool": "tool",
        "user": "notice",
    }
    value = mapping.get(str(role))
    if value is None:
        from core.turn_undo.models import TurnUndoError, UndoErrorCode

        raise TurnUndoError(
            UndoErrorCode.INVALID_INPUT,
            f"回合内持久化了无法分类的消息角色: {role!r}，"
            "必须先扩展 _position_kind_for_role 映射",
        )
    return value


# ── 工具结果归档层：编码策略 / 内容寻址句柄 / 落库行加工 ──────────────────────
# 超过该字节数的原文改用 gzip+base64 落库（文本类工具结果通常能压到 1/5 以下），
# 省下的是磁盘与 WAL 体积；解码在读取路径完成，调用方无感知。
COMPRESS_THRESHOLD_BYTES = 256 * 1024
# 单条归档上限：超过则只保留头尾各 4MB 并在中间插省略提示；bytes 仍记真实长度
SINGLE_RECORD_LIMIT_BYTES = 8 * 1024 * 1024
# 归档层总预算与 TTL：供闲时维护的 sweep_tool_result_archive 淘汰
ARCHIVE_BUDGET_BYTES = 256 * 1024 * 1024
ARCHIVE_TTL_DAYS = 7

# 单条归档保留了头尾各这么多字节（= SINGLE_RECORD_LIMIT_BYTES 的一半）
_TRUNCATE_HEAD_BYTES = SINGLE_RECORD_LIMIT_BYTES // 2
_TRUNCATE_TAIL_BYTES = SINGLE_RECORD_LIMIT_BYTES // 2

# 归档表的唯一写入口：11 列与 build_archive_row 的输出一一对应
_TOOL_RESULT_ARCHIVE_INSERT_SQL = (
    "INSERT OR IGNORE INTO tool_result_archive "
    "(handle, session_id, msg_id, tool_name, bytes, content, created_at, "
    "sha256, stored_bytes, encoding, accessed_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def encode_payload(raw: bytes) -> tuple[str, str, int]:
    """把原始工具结果字节编码成 (落库文本, 编码标记, 原始字节数)。"""
    size = len(raw)
    if size > SINGLE_RECORD_LIMIT_BYTES:
        # 超大结果：头尾各留 4MB，中间只留一句省略提示（原始长度照样记全）
        head = raw[:_TRUNCATE_HEAD_BYTES].decode("utf-8", "replace")
        tail = raw[-_TRUNCATE_TAIL_BYTES:].decode("utf-8", "replace")
        hint = f"\n\n[... {size} 字节超过单条归档上限，中间部分未归档 ...]\n\n"
        return head + hint + tail, "truncated", size
    if size > COMPRESS_THRESHOLD_BYTES:
        payload = base64.b64encode(gzip.compress(raw)).decode("ascii")
        return payload, "gzip+base64", size
    return raw.decode("utf-8", "replace"), "utf-8", size


def decode_payload(content: str, encoding: str | None) -> str:
    """把落库文本还原成原文；encoding 为 None/未知时按 utf-8 原文处理。"""
    if content is None:
        return ""
    text = content if isinstance(content, str) else str(content)
    flag = str(encoding or "utf-8").strip().lower()
    if flag == "gzip+base64":
        try:
            return gzip.decompress(
                base64.b64decode(text.encode("ascii"))
            ).decode("utf-8", "replace")
        except Exception:
            # 落库文本损坏或被外部改写时降级返回原文，绝不让读取路径炸掉
            return text
    # truncated / utf-8 / 未知编码：库里存的都是可直接阅读的文本
    return text


def content_handle(cursor, text: str) -> str:
    """由内容 sha256 派生 8 位句柄；同内容复用，碰撞时回退随机句柄。

    同一段原文 → 同一句柄，配合 INSERT OR IGNORE 让重复归档自动去重。
    """
    sha = hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()
    candidate = sha[:8]
    try:
        row = cursor.execute(
            "SELECT sha256 FROM tool_result_archive WHERE handle = ?", (candidate,)
        ).fetchone()
    except Exception:
        # 表/列缺失（极端旧库）：退回一次性随机句柄，不阻断改写流程
        return new_handle()
    # 已有同句柄记录：sha 相同说明同一段内容（复用）；不同（含旧行 sha 为 NULL）
    # 说明是真碰撞，必须换随机句柄，否则 marker 会指向别人的原文
    if row is not None and str(row[0] or "") != sha:
        return new_handle()
    return candidate


def build_archive_row(record) -> tuple:
    """把 7 字段归档元组加工成 11 字段落库行（编码 + 内容寻址 + 容量统计列）。

    record = (handle, session_id, msg_id, tool_name, bytes, content, created_at)。
    bytes 列仍写原始字节数（对外语义不变），stored_bytes 是实际落库文本长度。
    """
    handle, session_id, msg_id, tool_name, _bytes, content, created_at = record
    raw = str(content).encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    stored, encoding, original_bytes = encode_payload(raw)
    return (
        handle,
        session_id,
        msg_id,
        tool_name,
        original_bytes,
        stored,
        created_at,
        sha,
        len(stored),
        encoding,
        created_at,
    )


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
        # 4.1 消息全文检索索引可用性标志：CREATE VIRTUAL TABLE 失败（该 SQLite
        # 未编译 FTS5）时保持 False 并静默降级，检索返回 None 而非抛异常。
        self._fts_available = False
        # 建表失败过一次后不再重试（缺模块是永久性的），也保证 warning 只记一次。
        self._fts_disabled = False
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
                        summarized_until_msg_id INTEGER,
                        archived_at TEXT
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
                # 会话最后一次终态错误（如余额不足/接口超时），供 UI 多端与重载恢复
                if "last_error" not in columns:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN last_error TEXT")
                # 会话级权限档位（confirm/accept_edits/plan/full）：NULL = 跟随全局默认，
                # 权限选择只影响当前会话，不再全局生效
                if "permission_mode" not in columns:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN permission_mode TEXT")
                # 会话归档标记（对话归档功能）：NULL = 未归档，非空 = 归档时刻的
                # ISO8601 文本，与 created_at / updated_at 同风格。只走 ALTER
                # ADD COLUMN 幂等添加，不触发上方「遗留 sessions 表重建」分支。
                if "archived_at" not in columns:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN archived_at TEXT")
                # 秘书身份：会话级秘书名（NULL/空 = 非秘书会话）。起名后该会话的
                # 显示与系统提示词身份全程使用它；名字不参与 session_name 标识，
                # 重名互不影响，改名也不牵动消息/摘要/账本/快照的键。同样只走
                # ALTER ADD COLUMN 幂等添加，不重建表、不动历史行。
                if "secretary_name" not in columns:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN secretary_name TEXT")
                # 秘书人设：会话级自定义人设文本（NULL/空 = 使用内置默认秘书人设）。
                # 每个秘书是独立 agent，人设只属于它自己这个会话，与设置页的
                # 星瑶人设互不影响；同样只走 ALTER ADD COLUMN 幂等添加。
                if "secretary_persona" not in columns:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN secretary_persona TEXT")
                # @ 提及检索用的会话全文：存「首条用户消息」的前 2000 字。
                # 会话名只是首条消息截断到 12 字，用户记住的往往是后面的内容，
                # 只按名字匹配会永远搜不到（例如名字是「我感觉我现在@对话这个功」，
                # 想搜「功能」就命中不了）。新增列时顺带回填存量会话。
                if "search_text" not in columns:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN search_text TEXT")
                    backfill_search_text = True
                else:
                    backfill_search_text = False

                # 创建一个叫 messages 的表
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER,
                        role TEXT,
                        content TEXT
                    )
                ''')
                # 会话维度的消息索引：会话列表/计数（get_all_sessions 的
                # LEFT JOIN + COUNT）、@ 提及对话检索、按会话取历史都按
                # session_id 过滤。缺索引时 SQLite 只能 SCAN messages 全表
                # （真机 900MB 库单次约 95ms），@ 补全每敲一个字都要跑一遍，
                # 叠加 keyup 重复触发后输入明显卡顿。幂等建索引，只在首次
                # 启动时为存量库补齐（53k 行实测数秒内完成）。
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages(session_id)
                ''')
                # 存量库首次补 search_text 列：用每条会话最早的一条 user 消息
                # 回填，这样升级后立刻就能按完整首条消息检索，不用等新对话。
                # 逐条走 Python 而不是纯 SQL：检索文本必须与写入口径一致地
                # 做截断 + 去系统注入块，否则老会话会带着「当前运行上下文」
                # 这类噪音词被搜出来。会话数有限（几百），一次性成本可忽略。
                if backfill_search_text:
                    rows = cursor.execute('''
                        SELECT s.id, (
                            SELECT m.content FROM messages m
                            WHERE m.session_id = s.id AND m.role = 'user'
                            ORDER BY m.id ASC LIMIT 1
                        )
                        FROM sessions s
                        WHERE s.search_text IS NULL
                    ''').fetchall()
                    for sid, content in rows:
                        text = _session_search_text(content or "")
                        if text:
                            cursor.execute(
                                "UPDATE sessions SET search_text = ? WHERE id = ?",
                                (text, sid),
                            )
                # 多代摘要归档：每次压缩成功后追加一代（摘要/topics/边界）。
                # 只存边界元数据与摘要文本，原始消息永久保留在 messages 表——
                # history[0] 摘要块据此注入回溯索引，recall_history 工具据此
                # 按需取回更早被压缩掉的原始消息。
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS session_summary_generations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_name TEXT NOT NULL,
                        gen INTEGER NOT NULL,
                        summary_text TEXT,
                        topics TEXT,
                        until_msg_id INTEGER,
                        created_at TEXT
                    )
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_summary_generations_session
                    ON session_summary_generations(session_name, gen)
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
                        -- 断点续跑（resume）元数据：续跑 turn 指回被中断的父 turn，
                        -- resume_from_step_ordinal 记录从哪一步之后接续（NULL = 非续跑）。
                        parent_turn_id TEXT,
                        turn_kind TEXT,
                        resume_from_step_ordinal INTEGER,
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
                        -- 断点续跑（resume）元数据：replay_of_step_id 指向被回放的
                        -- 旧 step（NULL = 原生 step），tool_call_count 记录该步工具调用数。
                        replay_of_step_id TEXT,
                        tool_call_count INTEGER,
                        UNIQUE(turn_id, ordinal),
                        FOREIGN KEY(turn_id) REFERENCES harness_turns(turn_id) ON DELETE CASCADE
                    )
                ''')
                # 断点续跑（resume）加列：上面两处 CREATE TABLE IF NOT EXISTS 对已存在
                # 的旧库不生效，因此另走 ALTER ADD COLUMN 幂等补列（照 sessions.archived_at
                # 的写法：先读 PRAGMA table_info，缺列才加）。不重建表、不动历史行。
                cursor.execute("PRAGMA table_info(harness_turns)")
                turn_columns = {col[1] for col in cursor.fetchall()}
                if "parent_turn_id" not in turn_columns:
                    try:
                        cursor.execute(
                            "ALTER TABLE harness_turns ADD COLUMN parent_turn_id TEXT"
                        )
                    except Exception:
                        pass
                if "turn_kind" not in turn_columns:
                    try:
                        cursor.execute(
                            "ALTER TABLE harness_turns ADD COLUMN turn_kind TEXT"
                        )
                    except Exception:
                        pass
                if "resume_from_step_ordinal" not in turn_columns:
                    try:
                        cursor.execute(
                            "ALTER TABLE harness_turns "
                            "ADD COLUMN resume_from_step_ordinal INTEGER"
                        )
                    except Exception:
                        pass
                cursor.execute("PRAGMA table_info(harness_steps)")
                step_columns = {col[1] for col in cursor.fetchall()}
                if "replay_of_step_id" not in step_columns:
                    try:
                        cursor.execute(
                            "ALTER TABLE harness_steps ADD COLUMN replay_of_step_id TEXT"
                        )
                    except Exception:
                        pass
                if "tool_call_count" not in step_columns:
                    try:
                        cursor.execute(
                            "ALTER TABLE harness_steps ADD COLUMN tool_call_count INTEGER"
                        )
                    except Exception:
                        pass
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
                # 外键列索引：SQLite 执行 ON DELETE CASCADE / SET NULL 时会对子表按外键列
                # 全表扫描，缺索引时删除会话退化成 O(父行数 × harness_events 行数)。
                # 实测真实库：388 条消息的长会话删除 2153 ms → 80 ms。
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_harness_events_step_id "
                    "ON harness_events(step_id)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_harness_events_message_id "
                    "ON harness_events(message_id)"
                )
                # 工具结果归档（可逆折叠）：snip/elide 改写前把原始文本按句柄落库，
                # 模型用 tool_result_read 按 #xxxxxxxx 句柄取回全文。
                # 故意不加外键级联——消息被 undo/删除时归档原文仍可保留，避免误删。
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS tool_result_archive (
                        handle TEXT PRIMARY KEY,
                        session_id INTEGER NOT NULL,
                        msg_id INTEGER,
                        tool_name TEXT,
                        bytes INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        created_at TEXT,
                        sha256 TEXT,
                        stored_bytes INTEGER,
                        encoding TEXT DEFAULT 'utf-8',
                        accessed_at TEXT
                    )
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_tool_result_archive_session
                    ON tool_result_archive(session_id, created_at)
                ''')
                # 归档层内容寻址/压缩升级：老库没有这四列，照 cache_request_diagnostics
                # 的写法先读 PRAGMA 再 ALTER，缺列才加、失败静默，保证幂等与平滑升级。
                # 旧行 sha256/stored_bytes/accessed_at 保持 NULL，encoding 落默认 utf-8，
                # 读取路径按 utf-8 原文处理（见 decode_payload）。
                cursor.execute("PRAGMA table_info(tool_result_archive)")
                archive_columns = {col[1] for col in cursor.fetchall()}
                for column, alter_sql in (
                    ("sha256", "ALTER TABLE tool_result_archive ADD COLUMN sha256 TEXT"),
                    (
                        "stored_bytes",
                        "ALTER TABLE tool_result_archive ADD COLUMN stored_bytes INTEGER",
                    ),
                    (
                        "encoding",
                        "ALTER TABLE tool_result_archive ADD COLUMN encoding TEXT DEFAULT 'utf-8'",
                    ),
                    ("accessed_at", "ALTER TABLE tool_result_archive ADD COLUMN accessed_at TEXT"),
                ):
                    if column in archive_columns:
                        continue
                    try:
                        cursor.execute(alter_sql)
                    except Exception:
                        pass
                # 去重索引是部分索引：旧行 sha256 为 NULL 天然被排除。若历史库存在
                # 同 (session_id, sha256) 重复数据导致建索引失败，静默忽略即可——
                # 去重只是优化，绝不能因此阻断启动。
                for index_sql in (
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_result_archive_dedup "
                    "ON tool_result_archive(session_id, sha256) WHERE sha256 IS NOT NULL",
                    "CREATE INDEX IF NOT EXISTS idx_tool_result_archive_accessed "
                    "ON tool_result_archive(accessed_at)",
                ):
                    try:
                        cursor.execute(index_sql)
                    except Exception:
                        pass
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
                # 秘书派活台账：一条派活一行，派活时落库、回合收尾回写终态。
                # 读写走 core/secretary_dispatches.py 的 DispatchStore（复用本连接
                # 与 _lock，不开第二个写句柄）。字段名与
                # docs/superpowers/specs/2026-11-04-secretary-upgrade-design.md 3.1 一致。
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS secretary_dispatches (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        secretary_session TEXT NOT NULL,
                        target_session    TEXT NOT NULL,
                        target_display   TEXT NOT NULL DEFAULT '',
                        workdir          TEXT NOT NULL DEFAULT '',
                        title            TEXT NOT NULL DEFAULT '',
                        prompt           TEXT NOT NULL,
                        status           TEXT NOT NULL DEFAULT 'running',
                        depends_on       TEXT NOT NULL DEFAULT '[]',
                        batch_id         TEXT NOT NULL DEFAULT '',
                        started_at       TEXT NOT NULL DEFAULT '',
                        finished_at      TEXT NOT NULL DEFAULT '',
                        result_excerpt   TEXT NOT NULL DEFAULT '',
                        error            TEXT NOT NULL DEFAULT ''
                    )
                ''')
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_secretary_dispatches_target "
                    "ON secretary_dispatches(target_session)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_secretary_dispatches_secretary "
                    "ON secretary_dispatches(secretary_session, status)"
                )
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
                        cache_write_tokens INTEGER,
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
                # 读写分列（Task 7）：为 NULL 表示未报告（未报告 ≠ 0），不加默认值。
                if "cache_write_tokens" not in token_usage_columns:
                    try:
                        cursor.execute(
                            "ALTER TABLE token_usage "
                            "ADD COLUMN cache_write_tokens INTEGER"
                        )
                    except Exception:
                        pass
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_token_usage_date ON token_usage(date)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_token_usage_route "
                    "ON token_usage(model, api_base, cache_scope, date)"
                )
                # 按会话聚合（GUI 状态栏本会话口径 / 会话弹层）的查询走此索引
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_token_usage_session "
                    "ON token_usage(session_id, cache_scope)"
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
                        usage_errors TEXT NOT NULL,
                        route_override_sha256 TEXT NOT NULL DEFAULT '',
                        cache_control_markers INTEGER,
                        request_bytes_final INTEGER,
                        cache_write_tokens INTEGER
                    )
                ''')
                # 老库平滑升级：早期版本建的表没有 route_override_sha256 列，
                # 照 token_usage 加列写法补列；失败静默忽略，保证幂等（列已存在、
                # 并发升级等场景都不该让启动失败）。
                cursor.execute("PRAGMA table_info(cache_request_diagnostics)")
                cache_diag_columns = {col[1] for col in cursor.fetchall()}
                if "route_override_sha256" not in cache_diag_columns:
                    try:
                        cursor.execute(
                            "ALTER TABLE cache_request_diagnostics "
                            "ADD COLUMN route_override_sha256 TEXT NOT NULL DEFAULT ''"
                        )
                    except Exception:
                        pass
                # 线上字节事实两列（Task 2）：为 NULL 表示未观测（老行与未回填行），
                # 绝不把「未观测」写成 0。同样幂等补列。
                if "cache_control_markers" not in cache_diag_columns:
                    try:
                        cursor.execute(
                            "ALTER TABLE cache_request_diagnostics "
                            "ADD COLUMN cache_control_markers INTEGER"
                        )
                    except Exception:
                        pass
                if "request_bytes_final" not in cache_diag_columns:
                    try:
                        cursor.execute(
                            "ALTER TABLE cache_request_diagnostics "
                            "ADD COLUMN request_bytes_final INTEGER"
                        )
                    except Exception:
                        pass
                # 读写分列（Task 7）：写入量单列，NULL 语义与 cached 一致。
                if "cache_write_tokens" not in cache_diag_columns:
                    try:
                        cursor.execute(
                            "ALTER TABLE cache_request_diagnostics "
                            "ADD COLUMN cache_write_tokens INTEGER"
                        )
                    except Exception:
                        pass
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cache_diag_identity "
                    "ON cache_request_diagnostics"
                    "(session_id, model, api_base, cache_scope, id)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cache_diag_ts "
                    "ON cache_request_diagnostics(ts)"
                )
                # 星瑶按回合撤销：持久化检查点/文件/操作/令牌/纠正与消息回合关联。
                # DDL 集中在 core/turn_undo/storage.py，此处保持幂等迁移规范。
                from core.turn_undo.storage import create_turn_undo_tables

                create_turn_undo_tables(cursor)
                # 保存修改
                self.conn.commit()
            except Exception:
                # DDL 失败时回滚，避免遗留未提交事务阻塞后续写操作
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise
            # 消息全文索引（FTS5）：放在主 DDL 事务提交之后单独执行，内部自带
            # try/except —— 未编译 FTS5 的 SQLite 在这里静默降级，绝不冒泡，
            # 不影响上面任何建表与既有功能。
            self._ensure_message_fts(cursor)

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

    def export_harness_journal(self, session_name: str = "", since_ms: int = 0) -> list[dict]:
        """只读导出 harness 三表（turns/steps/events）为逐行 JSON 记录（T5）。

        - 仅含 ID 与元数据（Harness 纪律：journal 不落消息正文，只有 sha256/
          计数/时长/状态等），供诊断包与审计使用；
        - session_name 为空时导出全部会话；since_ms > 0 时只导出
          started_at_ms >= since_ms 的回合及其 steps/events；
        - 不新增写路径、不改 schema：本方法只做 SELECT。
        """
        with self._lock:
            cursor = self.conn.cursor()
            if session_name:
                cursor.execute(
                    "SELECT id, session_name FROM sessions WHERE session_name = ?",
                    (session_name,),
                )
            else:
                cursor.execute("SELECT id, session_name FROM sessions")
            session_names = {int(row[0]): row[1] for row in cursor.fetchall()}
            if not session_names:
                return []

            placeholders = ",".join("?" for _ in session_names)
            turn_filter = f"WHERE session_id IN ({placeholders})"
            turn_params = list(session_names)
            if since_ms:
                turn_filter += " AND started_at_ms >= ?"
                turn_params.append(int(since_ms))
            cursor.execute(
                "SELECT turn_id, session_id, owner_id, status, started_at_ms, "
                "finished_at_ms, last_sequence, error_code, parent_turn_id, "
                "turn_kind, resume_from_step_ordinal FROM harness_turns "
                f"{turn_filter} ORDER BY started_at_ms ASC, turn_id ASC",
                turn_params,
            )
            turn_columns = (
                "turn_id", "session_id", "owner_id", "status", "started_at_ms",
                "finished_at_ms", "last_sequence", "error_code", "parent_turn_id",
                "turn_kind", "resume_from_step_ordinal",
            )
            rows: list[dict] = []
            for raw in cursor.fetchall():
                turn = dict(zip(turn_columns, raw))
                rows.append({
                    "type": "turn",
                    "turn_id": turn["turn_id"],
                    "session_name": session_names.get(int(turn["session_id"]), ""),
                    "owner_id": turn["owner_id"],
                    "status": turn["status"],
                    "started_at_ms": turn["started_at_ms"],
                    "finished_at_ms": turn["finished_at_ms"],
                    "last_sequence": turn["last_sequence"],
                    "error_code": turn["error_code"],
                    "parent_turn_id": turn["parent_turn_id"],
                    "turn_kind": turn["turn_kind"],
                    "resume_from_step_ordinal": turn["resume_from_step_ordinal"],
                })

            joined_filter = (
                "JOIN harness_turns t ON t.turn_id = st.turn_id "
                f"WHERE t.session_id IN ({placeholders})"
            )
            step_params = list(session_names)
            if since_ms:
                joined_filter += " AND t.started_at_ms >= ?"
                step_params.append(int(since_ms))
            cursor.execute(
                "SELECT st.step_id, st.turn_id, st.ordinal, st.status, st.protocol, "
                "st.model, st.api_base, st.cache_scope, st.messages_sha256, "
                "st.system_sha256, st.tools_sha256, st.options_sha256, "
                "st.cache_lane_sha256, st.message_count, st.system_message_count, "
                "st.request_bytes, st.system_bytes, st.tools_bytes, st.options_bytes, "
                "st.request_reason, st.attempt_count, st.deepseek_generation_id, "
                "st.tool_call_count, st.replay_of_step_id, "
                "st.started_at_ms, st.finished_at_ms, st.duration_ms, "
                "st.finish_reason, st.error_code FROM harness_steps st "
                f"{joined_filter} ORDER BY st.turn_id ASC, st.ordinal ASC",
                step_params,
            )
            step_columns = (
                "step_id", "turn_id", "ordinal", "status", "protocol", "model",
                "api_base", "cache_scope", "messages_sha256", "system_sha256",
                "tools_sha256", "options_sha256", "cache_lane_sha256",
                "message_count", "system_message_count", "request_bytes",
                "system_bytes", "tools_bytes", "options_bytes", "request_reason",
                "attempt_count", "deepseek_generation_id", "tool_call_count",
                "replay_of_step_id", "started_at_ms",
                "finished_at_ms", "duration_ms", "finish_reason", "error_code",
            )
            for raw in cursor.fetchall():
                rows.append({"type": "step", **dict(zip(step_columns, raw))})

            event_filter = (
                "JOIN harness_turns t ON t.turn_id = ev.turn_id "
                f"WHERE t.session_id IN ({placeholders})"
            )
            event_params = list(session_names)
            if since_ms:
                event_filter += " AND t.started_at_ms >= ?"
                event_params.append(int(since_ms))
            cursor.execute(
                "SELECT ev.id, ev.turn_id, ev.step_id, ev.sequence, ev.kind, "
                "ev.created_at_ms, ev.message_id, ev.tool_name, ev.call_ordinal, "
                "ev.arguments_sha256, ev.result_sha256, ev.duration_ms, "
                "ev.result_status, ev.error_code FROM harness_events ev "
                f"{event_filter} ORDER BY ev.turn_id ASC, ev.sequence ASC",
                event_params,
            )
            event_columns = (
                "id", "turn_id", "step_id", "sequence", "kind", "created_at_ms",
                "message_id", "tool_name", "call_ordinal", "arguments_sha256",
                "result_sha256", "duration_ms", "result_status", "error_code",
            )
            for raw in cursor.fetchall():
                rows.append({"type": "event", **dict(zip(event_columns, raw))})
            return rows

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
                        api_base="", cache_scope="legacy",
                        cache_write_tokens=None):
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
                    "cached_tokens, cache_write_tokens, total_tokens, session_id, "
                    "api_base, cache_scope) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        int(ts), day, str(provider or ""), str(model or ""),
                        int(prompt_tokens or 0), int(completion_tokens or 0),
                        None if cached_tokens is None else int(cached_tokens),
                        (
                            None
                            if cache_write_tokens is None
                            else int(cache_write_tokens)
                        ),
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
                "SELECT t.prompt_tokens, t.completion_tokens, t.total_tokens, "
                "t.cached_tokens, t.provider, t.model, t.cache_write_tokens "
                "FROM token_usage t "
                "LEFT JOIN sessions s ON s.session_name = t.session_id "
                "WHERE t.session_id = ? "
                "AND t.cache_scope IN ('main', 'legacy') "
                f"AND {_SESSION_TOKEN_TIME_BOUND_SQL} "
                "ORDER BY t.id DESC LIMIT 1",
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
            "cache_write_tokens": None if row[6] is None else int(row[6]),
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

    def get_token_usage_by_session(self, session_name):
        """按会话聚合真实 token 用量：对话口径（main/legacy）为主，保活单列。

        命中率分母 cache_prompt 只统计已报告缓存明细的行（未报告 ≠ 0）。
        返回 {"prompt", "completion", "cached", "cache_prompt", "total",
        "requests", "last_ts", "keepalive_prompt", "keepalive_cached",
        "keepalive_requests"}；无记录时数值项为 0、last_ts 为 None。
        """
        with self._lock:
            cursor = self.conn.execute(
                f"""
                SELECT SUM(t.prompt_tokens), SUM(t.completion_tokens),
                       SUM(t.cached_tokens),
                       SUM(CASE WHEN t.cached_tokens IS NOT NULL
                                THEN t.prompt_tokens ELSE 0 END),
                       SUM(t.total_tokens), COUNT(*), MAX(t.ts)
                FROM token_usage t
                LEFT JOIN sessions s ON s.session_name = t.session_id
                WHERE t.session_id = ? AND t.cache_scope IN ('main', 'legacy')
                  AND {_SESSION_TOKEN_TIME_BOUND_SQL}
                """,
                (str(session_name),),
            )
            row = cursor.fetchone()
            cursor = self.conn.execute(
                f"""
                SELECT SUM(t.prompt_tokens), SUM(t.cached_tokens), COUNT(*)
                FROM token_usage t
                LEFT JOIN sessions s ON s.session_name = t.session_id
                WHERE t.session_id = ? AND t.cache_scope = 'keepalive'
                  AND {_SESSION_TOKEN_TIME_BOUND_SQL}
                """,
                (str(session_name),),
            )
            keepalive_row = cursor.fetchone()
        return {
            "prompt": int(row[0] or 0),
            "completion": int(row[1] or 0),
            "cached": int(row[2] or 0),
            "cache_prompt": int(row[3] or 0),
            "total": int(row[4] or 0),
            "requests": int(row[5] or 0),
            "last_ts": int(row[6]) if row[6] is not None else None,
            "keepalive_prompt": int(keepalive_row[0] or 0),
            "keepalive_cached": int(keepalive_row[1] or 0),
            "keepalive_requests": int(keepalive_row[2] or 0),
        }

    def list_token_usage_by_sessions(self, limit=20):
        """按会话聚合 token 用量并按合计降序排列，供会话弹层选择查看。

        只列 sessions 表中仍存在的会话；已删会话的残留行不进列表，
        但仍计入 get_token_usage_totals() 的全部会话合计。
        返回 [{"session_id", "prompt", "completion", "cached", "cache_prompt",
        "total", "requests", "last_ts"}, ...]
        """
        with self._lock:
            cursor = self.conn.execute(
                f"""
                SELECT t.session_id,
                       SUM(t.prompt_tokens), SUM(t.completion_tokens),
                       SUM(t.cached_tokens),
                       SUM(CASE WHEN t.cached_tokens IS NOT NULL THEN t.prompt_tokens ELSE 0 END),
                       SUM(t.total_tokens), COUNT(*), MAX(t.ts)
                FROM token_usage t
                JOIN sessions s ON s.session_name = t.session_id
                WHERE t.cache_scope IN ('main', 'legacy')
                  AND {_SESSION_TOKEN_TIME_BOUND_SQL}
                GROUP BY t.session_id
                ORDER BY SUM(t.total_tokens) DESC, MAX(t.ts) DESC
                LIMIT ?
                """,
                (int(limit),),
            )
            return [
                {
                    "session_id": row[0] or "",
                    "prompt": int(row[1] or 0),
                    "completion": int(row[2] or 0),
                    "cached": int(row[3] or 0),
                    "cache_prompt": int(row[4] or 0),
                    "total": int(row[5] or 0),
                    "requests": int(row[6] or 0),
                    "last_ts": int(row[7]) if row[7] is not None else None,
                }
                for row in cursor.fetchall()
            ]

    def get_token_usage_totals(self):
        """全部会话的总计（不分 scope，与状态行旧口径同源的落库版）。

        保活/压缩轮次也是真实计费，全部合计必须包含；命中率分母同样
        只取已报告缓存明细的行。返回 {"prompt", "completion", "cached",
        "cache_prompt", "total", "requests"}。
        """
        with self._lock:
            cursor = self.conn.execute(
                """
                SELECT SUM(prompt_tokens), SUM(completion_tokens),
                       SUM(cached_tokens),
                       SUM(CASE WHEN cached_tokens IS NOT NULL THEN prompt_tokens ELSE 0 END),
                       SUM(total_tokens), COUNT(*)
                FROM token_usage
                """
            )
            row = cursor.fetchone()
        return {
            "prompt": int(row[0] or 0),
            "completion": int(row[1] or 0),
            "cached": int(row[2] or 0),
            "cache_prompt": int(row[3] or 0),
            "total": int(row[4] or 0),
            "requests": int(row[5] or 0),
        }

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
            "cache_miss_tokens", "cache_write_tokens",
            "cache_reported", "usage_valid", "usage_errors",
            "route_override_sha256",
            "cache_control_markers", "request_bytes_final",
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
            (
                None
                if diagnostic.get("cache_write_tokens") is None
                else int(diagnostic["cache_write_tokens"])
            ),
            1 if diagnostic.get("cache_reported") else 0,
            1 if diagnostic.get("usage_valid") else 0,
            json.dumps(list(diagnostic.get("usage_errors") or ()), ensure_ascii=False),
            str(diagnostic.get("route_override_sha256", "") or ""),
            (
                None
                if diagnostic.get("cache_control_markers") is None
                else int(diagnostic["cache_control_markers"])
            ),
            (
                None
                if diagnostic.get("request_bytes_final") is None
                else int(diagnostic["request_bytes_final"])
            ),
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
        self, start_ts=None, end_ts=None, session_id=None, cache_scope=None,
        with_prev_prompt=False,
    ):
        """按时间/会话读取诊断行；NULL 与真实 0 保持原样。

        `with_prev_prompt=True` 时每行额外带 `prev_prompt`：同一
        (session_id, model, api_base, cache_scope) 内按 id 排序的上一行 prompt_tokens
        （设计 §Task3 口径）。上一行未报告缓存明细、或本行已是该链首行 → NULL。
        """
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
            "cached_tokens", "cache_miss_tokens", "cache_write_tokens",
            "cache_reported", "usage_valid",
            "usage_errors", "cache_control_markers", "request_bytes_final",
        )
        if not with_prev_prompt:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT " + ",".join(columns)
                    + " FROM cache_request_diagnostics" + where + " ORDER BY ts, id",
                    params,
                ).fetchall()
        else:
            # prev 在**过滤前的全表**上算，再套外层过滤：时间范围的首行照样拿得到
            # 上一行，不会因为「切点正好落在行中间」白丢一个样本。分区含 session_id：
            # 不同会话绝不互借 prev（跨会话的大 prompt 会把新会话首行误判成丢前缀）。
            # CASE 未命中即 NULL：上一行未报告缓存明细时 prev 视为未知（不把未知当 0）。
            selected = columns + ("prev_prompt",)
            with self._lock:
                rows = self.conn.execute(
                    "SELECT * FROM (SELECT "
                    + ",".join(columns)
                    + ", LAG(CASE WHEN cache_reported = 1 AND usage_valid = 1 "
                      "THEN prompt_tokens END) OVER ("
                      "PARTITION BY session_id, model, api_base, cache_scope "
                      "ORDER BY id) AS prev_prompt "
                    + " FROM cache_request_diagnostics)" + where + " ORDER BY ts, id",
                    params,
                ).fetchall()
            columns = selected
        result = []
        for row in rows:
            item = dict(zip(columns, row))
            item["cache_reported"] = bool(item["cache_reported"])
            item["usage_valid"] = bool(item["usage_valid"])
            item["usage_errors"] = json.loads(item["usage_errors"] or "[]")
            result.append(item)
        return result

    def query_keepalive_lane_evidence(self, before_ts=None, since_ts=None):
        """按车道聚合「已落库且报告了缓存明细」的保活轮次，供止损证据跨进程续用。

        只取 cache_scope='keepalive' 且 cached_tokens IS NOT NULL 的行：未报告的保活
        轮次连入分母的资格都没有（未报告 ≠ 0），绝不把 NULL 当 0 参与加权。

        before_ts 把窗口钉在「本进程启动之前」——本进程自己记的轮次已经在内存证据桶里，
        再算一遍会双计（轮次翻倍 → 判定门槛被提前跨过）；since_ts 排除久远到不该再
        压制复活的过去（亏损是稳态特征，但车道好转时不能被陈旧数据永久判死）。

        返回 `{(模型小写全串, 归一化 api_base): (轮次, prompt 合计, cached 合计)}`，
        键口径与 core.llm_diagnostics._cache_usage_key 一致（小写 + 去尾斜杠）。
        """
        clauses = ["cache_scope = 'keepalive'", "cached_tokens IS NOT NULL"]
        params = []
        if before_ts is not None:
            clauses.append("ts < ?")
            params.append(int(before_ts))
        if since_ts is not None:
            clauses.append("ts >= ?")
            params.append(int(since_ts))
        sql = (
            "SELECT provider, model, api_base, COUNT(*), "
            "SUM(prompt_tokens), SUM(cached_tokens) FROM token_usage WHERE "
            + " AND ".join(clauses)
            + " GROUP BY provider, model, api_base"
        )
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        lanes = {}
        for provider, model, api_base, turns, prompt_total, cached_total in rows:
            provider = str(provider or "")
            model_leaf = str(model or "")
            full_model = f"{provider}/{model_leaf}" if provider else model_leaf
            key = (full_model.lower(), str(api_base or "").rstrip("/").lower())
            acc = lanes.setdefault(key, [0, 0, 0])
            acc[0] += int(turns or 0)
            acc[1] += int(prompt_total or 0)
            acc[2] += int(cached_total or 0)
        return {key: tuple(value) for key, value in lanes.items()}

    def query_lane_cache_health(self, start_ts=None, end_ts=None, lane=None, window=200):
        """按时间窗口汇总各 lane 的健康度；lane=(model, api_base, cache_scope) 可选过滤。

        复用 query_cache_request_diagnostics（idx_cache_diag_ts 走时间过滤），按 lane
        分组、组内按 id 升序，再交给 core.cache_health.classify_lanes_health 派生。
        查询层不把 NULL 转成 0——「未报告 ≠ 真实 0」由派生层继续区分。

        prev_prompt 由查询层按 (会话, lane) 分链算好后随行带上（设计 §Task3 口径）：
        时间范围只决定「取哪些行做样本」，不切断 prev 链——否则切点正好落在某个会话
        中间时，区间首行会白白丢掉一个可判定样本。
        """
        rows = self.query_cache_request_diagnostics(
            start_ts, end_ts, with_prev_prompt=True
        )
        grouped = {}
        for row in rows:
            key = (row["model"], row["api_base"], row["cache_scope"])
            grouped.setdefault(key, []).append(row)
        if lane is not None:
            model, api_base, cache_scope = lane
            wanted = (
                str(model or "").lower(),
                str(api_base or "").rstrip("/").lower(),
                str(cache_scope or "main"),
            )
            grouped = {key: value for key, value in grouped.items() if key == wanted}
        for grouped_rows in grouped.values():
            grouped_rows.sort(key=lambda row: row["id"])
        return classify_lanes_health(grouped, window=window)

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
            # 账本换代原因（core/llm_dialects._deepseek_header_rotation_reason 取值域）
            "first_generation": "cold_start",
            "pending_rotation": "client_history_rewrite",
            "caller_force_new": "client_history_rewrite",
            "header_tools_changed": "tool_contract_change",
            "header_system_changed": "client_history_rewrite",
            "boundary_ambiguous": "client_history_rewrite",
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

    def add_message(self, session_name, role, content, *,
                    turn_checkpoint_id=None, turn_position_kind=None):
        """记笔记技能：把聊天内容存进数据库；关联参数透传 add_messages。"""
        position_kinds = (turn_position_kind,) if turn_position_kind else None
        self.add_messages(
            session_name, [(role, content)],
            turn_checkpoint_id=turn_checkpoint_id,
            turn_position_kinds=position_kinds,
        )
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
            "SELECT id, search_text FROM sessions WHERE session_name = ?",
            (session_name,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"missing message session: {session_name}")
        session_id, search_text = row[0], row[1]
        message_ids = []
        first_user_content = ""
        for role, content in serialized:
            cursor.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                (session_id, role, content),
            )
            message_ids.append(int(cursor.lastrowid))
            if not first_user_content and str(role) == "user":
                first_user_content = str(content or "")
        # search_text 是「首条用户消息」的正文（截到 2000 字，并去掉尾部
        # 系统注入的上下文/附件块），供 @ 检索命中被 12 字会话名截断掉的部分。
        # 只在为空时写一次：后续消息不能覆盖，否则检索文本会漂移到会话中途，
        # 失去「标题来源」的语义。
        search_candidate = _session_search_text(first_user_content)
        if not search_text and search_candidate:
            cursor.execute(
                "UPDATE sessions SET search_text = ? WHERE id = ?",
                (search_candidate, session_id),
            )
        return tuple(message_ids)
    def add_messages(self, session_name, messages, *, turn_checkpoint_id=None,
                     turn_position_kinds=None):
        """Atomically append one ordered batch of conversation messages.

        turn_checkpoint_id/turn_position_kinds：按回合撤销的消息关联（规格 7.7）。
        传入时消息正文与 message_turn_links 在同一事务提交或回滚；position_kind
        未知值显式失败。未显式传入时，若当前线程绑定了活动回合检查点上下文
        （回合内非 Harness 持久化路径，如压缩摘要回写），同样自动关联。
        """
        serialized, has_new_media = self._serialize_message_batch(messages)
        if not serialized:
            return

        if turn_checkpoint_id is None:
            try:
                from core.turn_undo import recorder as _tu_recorder

                ctx = _tu_recorder.current_context()
                # 只信任仍处于活动注册中的上下文：陈旧绑定（回合已封闭）
                # 不参与消息关联，避免把消息挂到已封闭检查点上
                if ctx is not None and _tu_recorder.is_context_active(ctx):
                    turn_checkpoint_id = ctx.checkpoint_id
            except Exception:
                turn_checkpoint_id = None
        if turn_position_kinds is None and turn_checkpoint_id is not None:
            turn_position_kinds = tuple(
                _position_kind_for_role(role) for role, _ in serialized
            )

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
                message_ids = self._insert_message_batch_locked(cursor, session_name, serialized)
                if turn_checkpoint_id is not None:
                    from core.turn_undo.storage import insert_message_turn_links_locked

                    links = []
                    for index, message_id in enumerate(message_ids):
                        kind = turn_position_kinds[index] \
                            if turn_position_kinds and index < len(turn_position_kinds) else None
                        if kind:
                            links.append((message_id, turn_checkpoint_id, kind))
                    if links:
                        insert_message_turn_links_locked(cursor, links)
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
        turn_checkpoint_id=None,
        turn_position_kinds=None,
    ) -> tuple[int, ...]:
        """Commit messages and their Harness event references atomically."""
        serialized, has_new_media = self._serialize_message_batch(messages)
        events = list(events)
        self._validate_harness_message_events(events, len(serialized), step_id)
        if not serialized:
            return ()

        # message_turn_links（规格 7.7）：Harness 原子提交路径同事务写入关联。
        # checkpoint 解析顺序：显式参数 → 回合线程 TLS 上下文 → 按 harness turn_id
        # （与回合 turn_id 同值，全局唯一）查库兜底（取消打捞等异线程路径）。
        link_checkpoint_id = turn_checkpoint_id
        if link_checkpoint_id is None:
            try:
                from core.turn_undo import recorder as _tu_recorder

                ctx = _tu_recorder.current_context()
                if ctx is not None:
                    link_checkpoint_id = ctx.checkpoint_id
            except Exception:
                link_checkpoint_id = None
        if link_checkpoint_id is None:
            try:
                from core.turn_undo.storage import _parse_status  # noqa: F401

                with self._lock:
                    cursor = self.conn.cursor()
                    cursor.execute(
                        "SELECT checkpoint_id FROM turn_undo_checkpoints WHERE turn_id = ?",
                        (str(turn_id),),
                    )
                    row = cursor.fetchone()
                link_checkpoint_id = row[0] if row else None
            except Exception:
                link_checkpoint_id = None
        link_kinds = turn_position_kinds or tuple(
            _position_kind_for_role(role) for role, _ in serialized
        )

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
                if link_checkpoint_id is not None:
                    from core.turn_undo.storage import insert_message_turn_links_locked

                    links = []
                    for index, message_id in enumerate(message_ids):
                        kind = link_kinds[index] if index < len(link_kinds) else None
                        if kind:
                            links.append((message_id, link_checkpoint_id, kind))
                    if links:
                        insert_message_turn_links_locked(cursor, links)
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

        含媒体的行有两种持久化形态（Task 10）：content 列表（普通多模态消息）
        与含 content 列表的完整消息 dict（带 _computer_v1 的 follow-up 观察）。
        折叠只把媒体块替换为文本占位符：dict 形态的其余键（_computer_v1 私有
        元数据）原样保留，不删除不改写；普通列表形态的折叠结果字节与历史
        版本完全一致。
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
        for msg_id, payload in media_rows[:-1]:  # 最新一条保留原样
            if isinstance(payload, dict):
                # 完整消息 dict：只折叠 content 内的媒体块，保留 _computer_v1 等键
                blocks = payload.get("content")
                if not isinstance(blocks, list):
                    continue
                payload["content"] = [
                    {"type": "text", "text": _folded_media_text(b)}
                    if isinstance(b, dict) and b.get("type") in ("image", "image_url")
                    else b
                    for b in blocks
                ]
                new_raw = json.dumps(payload, ensure_ascii=False)
            else:
                new_blocks = [
                    {"type": "text", "text": _folded_media_text(b)}
                    if isinstance(b, dict) and b.get("type") in ("image", "image_url")
                    else b
                    for b in payload
                ]
                new_raw = json.dumps(new_blocks, ensure_ascii=False)
            cursor.execute(
                'UPDATE messages SET content = ? WHERE id = ?',
                (new_raw, msg_id),
            )
            folded += 1
        return folded

    def fold_session_computer_media(self, session_id):
        """回合收尾后折叠本会话全部电脑操控观察图（含最新一条）；幂等。

        与 _fold_stale_media_locked 的区别：只处理带 _computer_v1 私有元数据的
        user 消息 dict 形态，折叠其 content 内全部图片块（普通多模态消息与
        _fold_stale_media_locked 的「保留最新一条」语义均不触碰——两者职责
        互斥：前者保运行中滚动上下文，后者清回合结束后的全部截图）。
        铁律：一次性、幂等、DB 层完成，改写后字节永久冻结。
        """
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "SELECT id, content FROM messages WHERE session_id = ? AND role = 'user' "
                    "ORDER BY id ASC",
                    (session_id,),
                )
                folded = 0
                for msg_id, raw in cursor.fetchall():
                    parsed, ok = _try_parse_json(raw)
                    if not ok or not isinstance(parsed, dict) or "_computer_v1" not in parsed:
                        continue
                    blocks = parsed.get("content")
                    if not isinstance(blocks, list) or not any(
                        isinstance(b, dict) and b.get("type") in ("image", "image_url")
                        for b in blocks
                    ):
                        continue  # 已折叠或本就无图：幂等跳过
                    parsed["content"] = [
                        {"type": "text", "text": _folded_media_text(b)}
                        if isinstance(b, dict) and b.get("type") in ("image", "image_url")
                        else b
                        for b in blocks
                    ]
                    cursor.execute(
                        "UPDATE messages SET content = ? WHERE id = ?",
                        (json.dumps(parsed, ensure_ascii=False), msg_id),
                    )
                    folded += 1
                self.conn.commit()
                return folded
            except Exception:
                self.conn.rollback()
                raise

    def iter_assistant_media_migration_rows(self):
        """媒体清单迁移候选行：无 media_v1 的 JSON dict 形态 assistant 消息。

        返回 [(message_id, session_workdir, parsed_content_dict), ...]。
        纯文本旧格式消息不迁移（保持展示层文件链接兜底，避免改写消息结构）；
        已带 media_v1 的消息跳过（迁移幂等的关键）。
        """
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT messages.id, messages.content, sessions.workdir "
                "FROM messages JOIN sessions ON sessions.id = messages.session_id "
                "WHERE messages.role = 'assistant' ORDER BY messages.id ASC"
            )
            rows = []
            for msg_id, raw, workdir in cursor.fetchall():
                parsed, ok = _try_parse_json(raw)
                if not ok or not isinstance(parsed, dict):
                    continue
                display = parsed.get("_display")
                if isinstance(display, dict) and "media_v1" in display:
                    continue
                content = parsed.get("content")
                if not isinstance(content, str) or not content:
                    continue
                rows.append((msg_id, workdir, parsed))
            return rows

    def iter_referenced_media_digests(self):
        """全库媒体清单引用的 Blob 摘要集合（供闲时孤儿 Blob 回收，只读）。

        粗筛 ``content LIKE '%media_v1%'`` 后解析 JSON，提取
        ``_display.media_v1.items[*].digest``；解析失败/结构异常的行跳过
        （回收侧另有宽限期兜底，缺引用最坏只是少回收，不会误删在用 Blob）。
        """
        digests = set()
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT content FROM messages WHERE content LIKE '%media_v1%'"
            )
            for (raw,) in cursor.fetchall():
                parsed, ok = _try_parse_json(raw)
                if not ok or not isinstance(parsed, dict):
                    continue
                display = parsed.get("_display")
                if not isinstance(display, dict):
                    continue
                manifest = display.get("media_v1")
                if not isinstance(manifest, dict):
                    continue
                items = manifest.get("items")
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    digest = item.get("digest")
                    if isinstance(digest, str) and len(digest) == 64:
                        digests.add(digest)
        return digests

    def update_message_display_media(self, message_id, media_v1) -> bool:
        """给单条消息补写 _display.media_v1（正文其余字节不动），幂等。"""
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute("SELECT content FROM messages WHERE id = ?", (message_id,))
                row = cursor.fetchone()
                if row is None:
                    return False
                parsed, ok = _try_parse_json(row[0])
                if not ok or not isinstance(parsed, dict):
                    return False
                display = parsed.get("_display")
                if not isinstance(display, dict):
                    display = {}
                display["media_v1"] = media_v1
                parsed["_display"] = display
                cursor.execute(
                    "UPDATE messages SET content = ? WHERE id = ?",
                    (json.dumps(parsed, ensure_ascii=False), message_id),
                )
                self.conn.commit()
                return True
            except Exception:
                self.conn.rollback()
                raise

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

    def set_session_permission_mode(self, session_name, permission_mode) -> bool:
        """绑定会话级权限档位。传 None/空字符串则清除绑定，回退到全局默认。

        调用方负责传入规范值（confirm/accept_edits/plan/full）。
        """
        mode = str(permission_mode or "").strip() or None
        with self._lock:
            cursor = self.conn.cursor()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                cursor.execute('''
                    UPDATE sessions SET permission_mode = ?, updated_at = ?
                    WHERE session_name = ?
                ''', (mode, now, session_name))
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception:
                self.conn.rollback()
                raise

    def get_session_permission_mode(self, session_name: str) -> str | None:
        """取会话级权限档位；未设置返回 None，调用方应回退到全局默认。"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT permission_mode FROM sessions WHERE session_name = ?
            ''', (session_name,))
            row = cursor.fetchone()
            return str(row[0]).strip() if row and row[0] else None

    def set_session_last_error(self, session_name: str, error_payload: str | dict) -> None:
        """记录会话最后一次终态错误（如余额不足/接口超时），供 UI 多端与重载恢复。"""
        serialized = (
            json.dumps(error_payload, ensure_ascii=False)
            if isinstance(error_payload, dict)
            else str(error_payload)
        )
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE sessions SET last_error = ? WHERE session_name = ?",
                (serialized, session_name),
            )
            self.conn.commit()

    def get_session_last_error(self, session_name: str) -> dict | None:
        """获取会话最后一次终态错误。"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT last_error FROM sessions WHERE session_name = ?",
                (session_name,),
            )
            row = cursor.fetchone()
            if not row or not row[0]:
                return None
            raw = row[0]
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
                return {"message": str(parsed)}
            except Exception:
                return {"message": str(raw)}

    def clear_session_last_error(self, session_name: str) -> None:
        """清除会话的最后一次终态错误。"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE sessions SET last_error = NULL WHERE session_name = ?",
                (session_name,),
            )
            self.conn.commit()

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
                cursor.execute(
                    'DELETE FROM session_summary_generations WHERE session_name = ?',
                    (session_name,),
                )
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute('''
                    UPDATE sessions
                    SET memory_summary = NULL, summarized_until_msg_id = 0, last_error = NULL, updated_at = ?
                    WHERE id = ?
                ''', (now, session_id))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        from core.llm import clear_cache_usage_session
        clear_cache_usage_session(session_name)
        return deleted

    def create_session_explicit(self, session_name, workdir=None, source="GUI", secretary_name=None,
                                secretary_persona=None):
        """明确指定 workdir（如 'GLOBAL' 或具体路径）创建一个会话记录。

        secretary_name 仅秘书会话创建路径传入（NULL/空 = 非秘书会话）。名字合法性
        由调用方（core.secretary.validate_secretary_name）先行校验，本层不再静默
        截断或改写，只把空值统一落 NULL，避免"空串"与"未设置"两套口径并存。

        secretary_persona 同上：仅秘书会话使用，NULL/空 = 走内置默认秘书人设。
        """
        wdir = workdir if workdir == "GLOBAL" else (_normalize_workdir(workdir) if workdir else self.workdir)
        sec_name = str(secretary_name).strip() if secretary_name else None
        sec_persona = str(secretary_persona).strip() if secretary_persona else None
        with self._lock:
            cursor = self.conn.cursor()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                cursor.execute('''
                    INSERT OR IGNORE INTO sessions (session_name, workdir, created_at, updated_at, source, secretary_name, secretary_persona)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (session_name, wdir, now, now, source, sec_name or None, sec_persona or None))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def get_session_secretary_name(self, session_name) -> str:
        """取会话的秘书名；无记录 / 未命名 / 查询异常一律返回空串。

        秘书名是显示与提示词的输入，读取处在每轮请求的高频路径上，不能因查询
        异常打断整轮，故异常回落空串而非向上抛。
        """
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    SELECT secretary_name FROM sessions WHERE session_name = ?
                ''', (session_name,))
                row = cursor.fetchone()
        except Exception:
            return ""
        return str(row[0]).strip() if row and row[0] else ""

    def set_session_secretary_name(self, session_name, name) -> bool:
        """写入会话的秘书名，返回是否有匹配行被更新。

        传 None/空字符串落 NULL（= 非秘书 / 未命名）；合法性由调用方校验，本层
        不做静默截断。异常回滚并返回 False，不向上抛。
        """
        sec_name = str(name).strip() if name else None
        with self._lock:
            cursor = self.conn.cursor()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                cursor.execute('''
                    UPDATE sessions SET secretary_name = ?, updated_at = ?
                    WHERE session_name = ?
                ''', (sec_name or None, now, session_name))
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception:
                self.conn.rollback()
                return False

    def get_session_secretary_persona(self, session_name) -> str:
        """取会话的秘书人设；无记录 / 未设置 / 查询异常一律返回空串。

        空串语义是"使用内置默认秘书人设"，与读取异常共用同一回落值——人设是
        提示词增强项，取不到时宁可退回默认证，也不能打断整轮请求。
        """
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    SELECT secretary_persona FROM sessions WHERE session_name = ?
                ''', (session_name,))
                row = cursor.fetchone()
        except Exception:
            return ""
        return str(row[0]) if row and row[0] else ""

    def set_session_secretary_persona(self, session_name, persona) -> bool:
        """写入会话的秘书人设，返回是否有匹配行被更新。

        传 None/空字符串落 NULL（= 回退内置默认秘书人设）；长度等合法性由调用方
        （core.secretary.validate_secretary_persona）校验。异常回滚返回 False。
        """
        sec_persona = str(persona).strip() if persona else None
        with self._lock:
            cursor = self.conn.cursor()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                cursor.execute('''
                    UPDATE sessions SET secretary_persona = ?, updated_at = ?
                    WHERE session_name = ?
                ''', (sec_persona or None, now, session_name))
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception:
                self.conn.rollback()
                return False

    def get_session_source(self, session_name) -> str:
        """取会话来源（GUI / SECRETARY 等）；无记录 / 查询异常返回空串。"""
        try:
            with self._lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    SELECT source FROM sessions WHERE session_name = ?
                ''', (session_name,))
                row = cursor.fetchone()
        except Exception:
            return ""
        return str(row[0]).strip() if row and row[0] else ""


    @staticmethod
    def _parse_message_rows(rows, with_ids=False, strip_display=False):
        """把数据库行元组解析成消息字典列表（get_history / get_history_with_ids /
        get_history_for_llm 共用）。

        with_ids=False 时 rows 每项为 (role, content)，返回的 dict 不新增任何键；
        with_ids=True 时 rows 每项为 (id, role, content)，返回的 dict 额外带 _msg_id。
        strip_display=True 时剔除模型显示元数据 _display（仅供 UI 展示，不得混入
        发给 LLM 的提示词），供 get_history_for_llm 使用。
        """
        history = []
        for record in rows:
            if with_ids:
                msg_id = record[0]
                role = record[1]
                raw_content = record[2]
            else:
                msg_id = None
                role = record[0]
                raw_content = record[1]
            parsed, ok = _try_parse_json(raw_content)
            if not ok:
                msg = {"role": role, "content": raw_content}
            elif isinstance(parsed, dict):
                msg = {"role": role}
                msg.update(parsed)
                # 数据库列中的 role 是权威值：防止消息正文恰好是含 "role" 键的
                # JSON 时覆盖真实角色（历史损坏 + 注入向量）
                msg["role"] = role
                if strip_display:
                    msg.pop("_display", None)
            else:
                msg = {"role": role, "content": parsed}
            if with_ids:
                msg["_msg_id"] = msg_id
            history.append(msg)
        return history

    def mark_turn_final_assistant(self, checkpoint_id: str) -> None:
        """回合封闭时把该检查点最后一条 assistant 关联更新为 assistant_final。

        显式按 message_id 更新（检查点内最后一条 assistant），不属于按时间
        范围回填；无 assistant 消息（异常回合）时静默跳过。
        """
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "SELECT l.message_id FROM message_turn_links l "
                    "JOIN messages m ON m.id = l.message_id "
                    "WHERE l.checkpoint_id = ? AND m.role = 'assistant' "
                    "ORDER BY l.message_id DESC LIMIT 1",
                    (checkpoint_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return
                from core.turn_undo.storage import update_message_link_position_locked

                update_message_link_position_locked(
                    cursor, message_id=int(row[0]),
                    position_kind="assistant_final",
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def load_session_turn_links(self, session_name: str) -> dict[int, dict]:
        """GUI 历史加载用：message_id -> {turn_id, checkpoint_id, position_kind}。

        仅返回属于当前会话检查点的关联；界面元数据，Provider 请求构造不得读取。
        """
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT l.message_id, l.checkpoint_id, l.position_kind, c.turn_id "
                "FROM message_turn_links l "
                "JOIN turn_undo_checkpoints c ON c.checkpoint_id = l.checkpoint_id "
                "JOIN sessions s ON s.id = c.session_id "
                "WHERE s.session_name = ?",
                (session_name,),
            )
            rows = cursor.fetchall()
        return {
            int(message_id): {
                "checkpoint_id": checkpoint_id,
                "position_kind": position_kind,
                "turn_id": turn_id,
            }
            for message_id, checkpoint_id, position_kind, turn_id in rows
        }

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
            return self._parse_message_rows(cursor.fetchall())

    def get_last_message_id(self, session_name):
        """返回该会话最后一条消息的 id；会话不存在或没有任何消息时返回 0。"""
        with self._lock:
            cursor = self.conn.cursor()

            cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
            row = cursor.fetchone()
            if row is None:
                return 0

            session_id = row[0]
            cursor.execute('SELECT MAX(id) FROM messages WHERE session_id = ?', (session_id,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    def get_history_with_ids(self, session_name):
        """与 get_history 相同，但每条消息额外带 _msg_id，供 GUI 做回合边界过滤。"""
        with self._lock:
            cursor = self.conn.cursor()

            cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
            row = cursor.fetchone()
            if row is None:
                return []

            session_id = row[0]
            cursor.execute('''
                SELECT id, role, content FROM messages
                WHERE session_id = ?
                ORDER BY id ASC
            ''', (session_id,))

            return self._parse_message_rows(cursor.fetchall(), with_ids=True)

    def get_history_paged(self, session_name, before_id=None, limit=50):
        """分页读取会话历史（含 _msg_id），按 id 正序返回本页。

        before_id=None 取最新一页；否则取 id < before_id 的一页。
        分页边界对齐回合起点：本页最旧一条非 user 且仍有更早消息时，
        向前扩展至最近一条 user（上限 limit+30）。
        返回 (messages, has_more)。
        """
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
            row = cursor.fetchone()
            if row is None:
                return [], False
            session_id = row[0]

            limit = max(1, int(limit))          # 防御 limit<=0，避免 page[-1] IndexError
            max_fetch = limit + 30
            if before_id is None:
                cursor.execute(
                    'SELECT id, role, content FROM messages WHERE session_id = ? '
                    'ORDER BY id DESC LIMIT ?', (session_id, max_fetch))
            else:
                cursor.execute(
                    'SELECT id, role, content FROM messages WHERE session_id = ? AND id < ? '
                    'ORDER BY id DESC LIMIT ?', (session_id, int(before_id), max_fetch))
            fetched = cursor.fetchall()          # 倒序：最新在前
            if not fetched:
                return [], False

            page = fetched[:limit]
            extra = list(fetched[limit:])   # 倒序的扩展余量
            # 回合对齐：本页最旧（page[-1]）非 user 时，沿更早方向吞并 extra 直到 user 或耗尽
            while extra and page[-1][1] != 'user':
                page.append(extra.pop(0))

            oldest_id = page[-1][0]
            cursor.execute(
                'SELECT EXISTS(SELECT 1 FROM messages WHERE session_id = ? AND id < ?)',
                (session_id, oldest_id))
            has_more = bool(cursor.fetchone()[0])

            page.reverse()                        # 回到正序
            return self._parse_message_rows(page, with_ids=True), has_more

    def get_session_user_nav_items(self, session_name):
        """红点导航全量索引：该会话全部 user 消息的 [{id, text, is_observation}]（正序）。

        仅提取纯文本（多模态 blocks 只拼 text 块），不做后缀剥离/截断——
        预览清洗在 gui_bridge 层完成（strip_context_suffix 位于高层，
        memory 不反向依赖）。is_observation 标记电脑操控观察跟进协议消息
        （_computer_v1 结构标记），展示层据此过滤，模型上下文不受影响。
        判定助手经惰性导入使用（memory 是底层模块，避免顶层依赖 computer 包）。
        """
        from core.computer.provider_generic import is_observation_protocol_message

        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('SELECT id FROM sessions WHERE session_name = ?', (session_name,))
            row = cursor.fetchone()
            if row is None:
                return []
            session_id = row[0]
            cursor.execute(
                "SELECT id, content FROM messages WHERE session_id = ? "
                "AND role = 'user' ORDER BY id ASC", (session_id,))
            items = []
            for msg_id, raw in cursor.fetchall():
                observation = is_observation_protocol_message(raw)
                parsed, ok = _try_parse_json(raw)
                if ok and isinstance(parsed, list):
                    texts = [
                        str(b.get("text") or "")
                        for b in parsed
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    text = " ".join(t for t in texts if t).strip()
                elif ok and isinstance(parsed, dict):
                    text = str(parsed.get("content") or "")
                else:
                    text = str(raw or "")
                items.append({"id": msg_id, "text": text, "is_observation": observation})
            return items

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

            history = self._parse_message_rows(cursor.fetchall(), strip_display=True)

            # 修复 tool_call 配对（如压缩边界切断了工具调用组），
            # 防止坏历史让 API 报 "tool_call_id is not found" 后会话彻底不可用
            history = _sanitize_tool_call_pairing(history)

            # 如果存在背景记忆摘要，将其包装为参考上下文并插在首部。
            # 注意 role 必须是 user 而非 system：持久化的上下文块（动态运行上下文/
            # 时间注记等）使用 system 角色，摘要用 user 角色可以与之明确区分，
            # 且位置固定在 history[0]，在两次压缩之间保持稳定，
            # 不破坏 provider 侧 prefix caching。
            if memory_summary:
                summary_content = (
                    f"[历史对话记忆摘要 (以下是前段长对话的归纳记忆，请作为背景上下文参考，无需直接回复这段摘要)]\n{memory_summary}"
                )
                index_section = self._format_summary_generation_index(session_name)
                if index_section:
                    summary_content += index_section
                history.insert(0, {
                    "role": "user",
                    "content": summary_content
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
                    # 归档代次一并作废：被撤销区间的原始消息已 DELETE，归档的
                    # 边界与回溯索引随之失真，保守清空（与删除同事务）。
                    cursor.execute(
                        'DELETE FROM session_summary_generations WHERE session_name = ?',
                        (session_name,),
                    )
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise

    def get_message_role(self, session_name, msg_id):
        """取指定消息的角色（供编辑入口做前置校验）；不存在或不属于该会话返回 None。"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                'SELECT m.role FROM messages m '
                'JOIN sessions s ON s.id = m.session_id '
                'WHERE m.id = ? AND s.session_name = ?',
                (int(msg_id), session_name),
            )
            row = cursor.fetchone()
            return str(row[0]) if row else None

    def rewind_to_message(self, session_name, msg_id):
        """把会话截断到指定消息之前：删除该条及之后的全部记录。

        编辑历史用户消息意味着「从这条重新分叉」，因此：
        - messages 表里 id >= msg_id 的行全部删除（含该条自身，新文本由调用方重发）
        - message_turn_links 的孤立关联一并清理（该表无 ON DELETE CASCADE）
        - memory_summary / summarized_until_msg_id 与多代归档作废：被删区间可能
          已被压缩进摘要，继续注入会让模型看到已撤销的旧结论
        - messages_fts 无需处理：全文索引是独立表，查询侧 JOIN messages 自然排除

        返回 {"role": 原消息角色, "removed": 删除条数}；目标消息不属于该会话时
        返回 None（不做任何修改）。
        """
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                'SELECT id FROM sessions WHERE session_name = ?', (session_name,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            session_id = row[0]
            cursor.execute(
                'SELECT role FROM messages WHERE id = ? AND session_id = ?',
                (int(msg_id), session_id),
            )
            target = cursor.fetchone()
            if not target:
                return None
            role = target[0]
            try:
                # turn_undo 未初始化（老库/纯 CLI 环境）时该表可能不存在，跳过即可
                cursor.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'message_turn_links'"
                )
                if cursor.fetchone():
                    cursor.execute(
                        'DELETE FROM message_turn_links WHERE message_id IN ('
                        '  SELECT id FROM messages WHERE session_id = ? AND id >= ?'
                        ')',
                        (session_id, int(msg_id)),
                    )
                cursor.execute(
                    'DELETE FROM messages WHERE session_id = ? AND id >= ?',
                    (session_id, int(msg_id)),
                )
                removed = cursor.rowcount
                cursor.execute(
                    'UPDATE sessions '
                    'SET memory_summary = NULL, summarized_until_msg_id = 0 '
                    'WHERE id = ? AND (memory_summary IS NOT NULL OR summarized_until_msg_id)',
                    (session_id,),
                )
                cursor.execute(
                    'DELETE FROM session_summary_generations WHERE session_name = ?',
                    (session_name,),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            return {"role": str(role or ""), "removed": int(removed or 0)}

    def get_all_sessions(self, workdir=None, source=None, include_empty=False,
                         include_archived=False, archived_only=False,
                         sources: Optional[List[str]] = None):
        """返回会话列表（名称/消息数/归属目录/时间/来源/归档时刻）。

        workdir 为 None 时返回全部会话；指定路径时只返回该目录下创建的会话。
        source 参数用于过滤 GUI 或 CLI 专属会话。
        sources 参数用于一次筛选多个来源（如 ["GUI", "SECRETARY"]），语义为
        `(s.source IN (...) OR s.source IS NULL)`，与 source 的过滤口径一致；
        sources 与 source 同时传入时以 sources 为准（source 被忽略）。
        返回的每个会话都带 source 字段（无值时回退为 "GUI"），供上层按来源分组。
        include_empty=False 时过滤掉无任何消息的空会话（参考 kimicode 的 is_empty 策略），
        避免会话列表被空会话污染；磁盘记录仍保留，不删除。
        include_archived=False（默认）时不返回已归档会话，保证既有调用点行为不变；
        archived_only=True 时只返回已归档会话（供归档区视图）。
        """
        # 归档过滤条件：默认隐藏归档会话，archived_only 只取归档会话
        if archived_only:
            archived_filter = " AND s.archived_at IS NOT NULL"
        elif include_archived:
            archived_filter = ""
        else:
            archived_filter = " AND s.archived_at IS NULL"
        with self._lock:
            cursor = self.conn.cursor()
            if sources:
                # 多来源筛选（如 ["GUI", "SECRETARY"]）：口径与单 source 分支
                # 完全一致，无 source 的历史数据同样视为可见，避免老会话消失
                placeholders = ",".join("?" for _ in sources)
                cursor.execute(f'''
                    SELECT s.session_name, COUNT(m.id) as msg_count,
                           s.workdir, s.updated_at, s.created_at, s.source, s.id,
                           s.archived_at, s.search_text
                    FROM sessions s
                    LEFT JOIN messages m ON s.id = m.session_id
                    WHERE (s.source IN ({placeholders}) OR s.source IS NULL){archived_filter}
                    GROUP BY s.id
                    ORDER BY s.id DESC
                ''', tuple(sources))
            elif source:
                cursor.execute(f'''
                    SELECT s.session_name, COUNT(m.id) as msg_count,
                           s.workdir, s.updated_at, s.created_at, s.source, s.id,
                           s.archived_at, s.search_text
                    FROM sessions s
                    LEFT JOIN messages m ON s.id = m.session_id
                    WHERE (s.source = ? OR s.source IS NULL){archived_filter}
                    GROUP BY s.id
                    ORDER BY s.id DESC
                ''', (source,))
            else:
                cursor.execute(f'''
                    SELECT s.session_name, COUNT(m.id) as msg_count,
                           s.workdir, s.updated_at, s.created_at, s.source, s.id,
                           s.archived_at, s.search_text
                    FROM sessions s
                    LEFT JOIN messages m ON s.id = m.session_id
                    WHERE 1=1{archived_filter}
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
                "source": row[5] or "GUI",
                # 不可变主键 sessions.id（按回合撤销等功能的稳定归属标识）
                "id": row[6],
                # 归档时刻（ISO8601 文本）；None = 未归档
                "archived_at": row[7],
                # 首条用户消息前 2000 字，供 @ 检索匹配被会话名截断的内容
                "search_text": row[8],
            }
            for row in filtered
        ]

    def search_sessions_and_messages(
        self,
        query: str,
        scope: str = "all",
        workdir: Optional[str] = None,
        source: Optional[str] = "GUI",
        limit: int = 30,
        include_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        """全文与标题复合检索会话。

        scope:
          - "all": 既检索标题又检索消息内容
          - "title": 仅检索会话标题/名称
          - "content": 仅检索消息正文 (role IN ('user', 'assistant'))
        workdir:
          - None: 不限工作区
          - "GLOBAL": 自由通用会话
          - path: 指定工作目录
        source:
          - 默认 "GUI"，匹配 s.source = 'GUI' OR s.source IS NULL
        limit:
          - 最大返回条数
        include_archived:
          - 默认 False，排除已归档会话（既有调用点行为不变）
        """
        q = str(query or "").strip()
        if not q:
            return []

        # 转义 SQL LIKE 通配符
        escaped_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped_q}%"

        norm_workdir = _normalize_workdir(workdir) if (workdir and workdir != "GLOBAL") else workdir

        results: List[Dict[str, Any]] = []

        def _make_snippet(text: str) -> str:
            clean = re.sub(r"[\r\n\t]+", " ", str(text or "")).strip()
            lower_clean = clean.lower()
            lower_kw = q.lower()
            idx = lower_clean.find(lower_kw)
            context_len = 35
            if idx == -1:
                sub = clean[: context_len * 2]
                res = sub + ("..." if len(clean) > len(sub) else "")
            else:
                start = max(0, idx - context_len)
                end = min(len(clean), idx + len(q) + context_len)
                res = ("..." if start > 0 else "") + clean[start:end] + ("..." if end < len(clean) else "")
            # 凭证脱敏
            return re.sub(r"sk-[a-zA-Z0-9]{16,}", "sk-***", res)

        with self._lock:
            cursor = self.conn.cursor()

            # 1. 标题搜索 (scope in ("all", "title"))
            if scope in ("all", "title"):
                title_sql = """
                    SELECT s.id, s.session_name, s.workdir, s.updated_at, s.created_at,
                           (SELECT COUNT(m.id) FROM messages m WHERE m.session_id = s.id) as msg_count
                    FROM sessions s
                    WHERE (s.source = ? OR s.source IS NULL)
                      AND s.session_name LIKE ? ESCAPE '\\'
                """
                params = [source, pattern]
                if not include_archived:
                    title_sql += " AND s.archived_at IS NULL"
                if norm_workdir is not None:
                    if norm_workdir == "GLOBAL":
                        title_sql += " AND (s.workdir IS NULL OR s.workdir = 'GLOBAL')"
                    else:
                        title_sql += " AND s.workdir = ?"
                        params.append(norm_workdir)

                title_sql += " ORDER BY s.updated_at DESC LIMIT ?"
                params.append(limit)

                cursor.execute(title_sql, params)
                for row in cursor.fetchall():
                    s_id, s_name, s_workdir, s_updated, s_created, msg_count = row
                    results.append({
                        "type": "title",
                        "session_id": s_id,
                        "session_name": s_name,
                        "workdir": s_workdir or "GLOBAL",
                        "updated_at": s_updated or "",
                        "created_at": s_created or "",
                        "msg_count": msg_count or 0,
                        "msg_id": None,
                        "role": None,
                        "snippet": None,
                    })

            # 2. 内容搜索 (scope in ("all", "content"))
            if scope in ("all", "content"):
                content_sql = """
                    SELECT m.id, m.role, m.content, s.id, s.session_name, s.workdir, s.updated_at, s.created_at,
                           (SELECT COUNT(m2.id) FROM messages m2 WHERE m2.session_id = s.id) as msg_count
                    FROM messages m
                    JOIN sessions s ON m.session_id = s.id
                    WHERE (s.source = ? OR s.source IS NULL)
                      AND m.role IN ('user', 'assistant')
                      AND m.content LIKE ? ESCAPE '\\'
                """
                params = [source, pattern]
                if not include_archived:
                    content_sql += " AND s.archived_at IS NULL"
                if norm_workdir is not None:
                    if norm_workdir == "GLOBAL":
                        content_sql += " AND (s.workdir IS NULL OR s.workdir = 'GLOBAL')"
                    else:
                        content_sql += " AND s.workdir = ?"
                        params.append(norm_workdir)

                content_sql += " ORDER BY s.updated_at DESC, m.id DESC LIMIT ?"
                params.append(limit * 2)

                cursor.execute(content_sql, params)
                session_content_hits: Dict[int, int] = {}
                for row in cursor.fetchall():
                    m_id, m_role, m_content, s_id, s_name, s_workdir, s_updated, s_created, msg_count = row
                    hits = session_content_hits.get(s_id, 0)
                    # 同一会话最多提取 2 条命中消息片段，防止被单个长会话刷屏
                    if hits >= 2:
                        continue
                    session_content_hits[s_id] = hits + 1

                    snippet = _make_snippet(m_content)
                    results.append({
                        "type": "content",
                        "session_id": s_id,
                        "session_name": s_name,
                        "workdir": s_workdir or "GLOBAL",
                        "updated_at": s_updated or "",
                        "created_at": s_created or "",
                        "msg_count": msg_count or 0,
                        "msg_id": m_id,
                        "role": m_role,
                        "snippet": snippet,
                    })
                    if len(results) >= limit:
                        break

        return results[:limit]


    def get_memory_summary(self, session_name):
        """获取当前会话的背景记忆摘要"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('SELECT memory_summary FROM sessions WHERE session_name = ?', (session_name,))
            row = cursor.fetchone()
            return row[0] if row else None

    def get_session_memory_summary(self, session_name):
        """读取会话的记忆压缩摘要；会话不存在或无摘要返回空串。"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                'SELECT memory_summary FROM sessions WHERE session_name = ?',
                (session_name,),
            )
            row = cursor.fetchone()
        if not row:
            return ""
        return str(row[0] or "").strip()

    def archive_summary_generation(self, session_name, summary_text, topics=None, until_msg_id=0):
        """追加一代摘要归档（压缩成功后调用）。失败静默返回 None。

        归档是纯增量元数据：不影响活跃历史，也不参与水位 CAS——任何失败
        （如表尚未建立）只损失回溯索引，绝不影响压缩成果本身。
        """
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    'SELECT COALESCE(MAX(gen), 0) FROM session_summary_generations '
                    'WHERE session_name = ?',
                    (session_name,),
                )
                gen = int(cursor.fetchone()[0] or 0) + 1
                cursor.execute(
                    'INSERT INTO session_summary_generations '
                    '(session_name, gen, summary_text, topics, until_msg_id, created_at) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (
                        session_name,
                        gen,
                        str(summary_text or ""),
                        (str(topics).strip() if topics else None),
                        int(until_msg_id or 0),
                        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                self.conn.commit()
                return gen
            except Exception:
                self.conn.rollback()
                return None

    def get_summary_generation_index(self, session_name, limit=10):
        """读取归档索引。返回 (最新 limit 代 rows 倒序, 总代数)；无归档返回 ([], 0)。"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                'SELECT COUNT(*) FROM session_summary_generations WHERE session_name = ?',
                (session_name,),
            )
            total = int(cursor.fetchone()[0] or 0)
            if total == 0:
                return [], 0
            cursor.execute(
                'SELECT gen, topics, until_msg_id, created_at '
                'FROM session_summary_generations WHERE session_name = ? '
                'ORDER BY gen DESC LIMIT ?',
                (session_name, max(1, int(limit))),
            )
            rows = [
                {"gen": r[0], "topics": r[1], "until_msg_id": r[2], "created_at": r[3]}
                for r in cursor.fetchall()
            ]
            return rows, total

    def _format_summary_generation_index(self, session_name, max_visible=10):
        """生成摘要块尾部的回溯索引段；无归档返回空串（不改动摘要块字节）。

        索引行内容来自归档表，两次压缩之间字节稳定，不破坏前缀缓存。
        """
        try:
            rows, total = self.get_summary_generation_index(session_name, max_visible)
        except Exception:
            return ""
        if not rows:
            return ""
        lines = [
            f"\n\n[更早摘要回溯] 共 {total} 代归档；需要更早的对话细节时可用 "
            "recall_history 工具按 before_msg_id 读取原始消息。最近归档："
        ]
        for row in rows:
            topics = str(row.get("topics") or "").strip()
            created = str(row.get("created_at") or "")[:10]
            line = f"- gen#{row.get('gen')}（{created}，至 msg_id={row.get('until_msg_id')}）"
            if topics:
                line += f" {topics}"
            lines.append(line)
        older = total - len(rows)
        if older > 0:
            lines.append(f"- ...及更早 {older} 代（可用 recall_history 继续向前回溯）")
        return "\n".join(lines)

    def get_compressed_messages(self, session_name, before_msg_id, limit=16):
        """读取已压缩区的原始消息，供 recall_history 工具回溯。

        只服务摘要水位覆盖的范围（id <= summarized_until_msg_id 且
        id <= before_msg_id，含边界 itself——索引里的 until_msg_id 本身就是
        已压缩消息）：活跃区消息模型本就可见，不在此重复。
        返回按时间正序的 [{"id", "role", "content"}]，content 为文本化内容。
        """
        try:
            # 显式判 None：仅缺省用 16；0/负数按下限夹到 1，不再被 `or` 误当缺省
            if limit is None:
                limit = 16
            limit = max(1, min(int(limit), 50))
            before_msg_id = int(before_msg_id)
        except (TypeError, ValueError):
            return []
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                'SELECT id, summarized_until_msg_id FROM sessions WHERE session_name = ?',
                (session_name,),
            )
            row = cursor.fetchone()
            if not row:
                return []
            session_id, watermark = row[0], (row[1] or 0)
            if watermark <= 0:
                return []
            cursor.execute(
                'SELECT id, role, content FROM ('
                '  SELECT id, role, content FROM messages'
                '  WHERE session_id = ? AND id <= ? AND id <= ?'
                '  ORDER BY id DESC LIMIT ?'
                ') ORDER BY id ASC',
                (session_id, before_msg_id, watermark, limit),
            )
            return [
                {
                    "id": r[0],
                    "role": r[1],
                    "content": message_content_to_text(r[2]),
                }
                for r in cursor.fetchall()
            ]

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
                    # 工具结果归档随会话物理删除一并清理（归档句柄只在本会话消息
                    # 里出现，会话没了句柄也永不复活）。只走删除路径，检索/维护
                    # 一律不动归档，避免 marker 句柄失效。
                    cursor.execute(
                        'DELETE FROM tool_result_archive WHERE session_id = ?', (session_id,)
                    )
                    self._delete_deepseek_prompt_generations_locked(cursor, [session_name])
                    cursor.execute(
                        'DELETE FROM session_summary_generations WHERE session_name = ?',
                        (session_name,),
                    )
                    # token 流水按 session_name 记账，会话行删除后流水若留下，
                    # 同名新会话会把它当作自己的历史累计（2026-09-29 状态行
                    # 显示 3200 万输入即此因），故与消息同事务一并清理。
                    cursor.execute(
                        'DELETE FROM token_usage WHERE session_id = ?', (session_name,)
                    )
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
                    # 同 delete_session：被删会话的工具结果归档一并清理
                    cursor.execute(
                        'DELETE FROM tool_result_archive WHERE session_id = ?', (sid,)
                    )
                self._delete_deepseek_prompt_generations_locked(cursor, deleted_names)
                if deleted_names:
                    placeholders = ",".join("?" for _ in deleted_names)
                    cursor.execute(
                        f'DELETE FROM session_summary_generations '
                        f'WHERE session_name IN ({placeholders})',
                        deleted_names,
                    )
                    # 同 delete_session：会话行删除必须连带清理按名字记账的
                    # token 流水，否则同名新会话会继承这批历史累计。
                    cursor.execute(
                        f'DELETE FROM token_usage WHERE session_id IN ({placeholders})',
                        deleted_names,
                    )
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
                # 归档代次同样按 session_name 键控：不随改名迁移会孤儿化，
                # 回溯索引静默消失且旧名行永远清不掉（与上方更新同事务）
                cursor.execute(
                    'UPDATE session_summary_generations SET session_name = ? WHERE session_name = ?',
                    (final_name, old_name),
                )
                # token 用量流水按 session_name 记账：不随迁则按会话统计
                # 在改名后静默丢历史（与上方更新同事务）
                cursor.execute(
                    'UPDATE token_usage SET session_id = ? WHERE session_id = ?',
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

    def archive_session(self, session_name, archived_at=None) -> bool:
        """归档会话：从默认会话列表隐藏，数据零损失、可随时恢复。

        只写 sessions.archived_at 一列（NULL = 未归档，非空 = 归档时刻的
        ISO8601 文本），不触碰 messages / session_summary_generations /
        harness_* / token_usage，也不刷新 updated_at——归档不是「会话有新活动」，
        混用会污染活跃时间排序。
        幂等：已归档会话再次调用不覆盖原 archived_at，仍返回 True；
        会话不存在时返回 False，由调用方提示。
        """
        stamp = (
            str(archived_at).strip()
            if archived_at
            else datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    'SELECT archived_at FROM sessions WHERE session_name = ?',
                    (session_name,),
                )
                row = cursor.fetchone()
                if row is None:
                    return False
                if row[0]:
                    # 已归档：幂等成功，保留原归档时刻
                    return True
                cursor.execute(
                    'UPDATE sessions SET archived_at = ? WHERE session_name = ?',
                    (stamp, session_name),
                )
                self.conn.commit()
                return True
            except Exception:
                self.conn.rollback()
                raise

    def unarchive_session(self, session_name) -> bool:
        """取消归档：把 archived_at 置回 NULL，会话恢复回主列表。

        幂等：对未归档会话调用同样安全返回 True（不产生额外副作用）；
        会话不存在时返回 False。
        """
        with self._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    'SELECT archived_at FROM sessions WHERE session_name = ?',
                    (session_name,),
                )
                if cursor.fetchone() is None:
                    return False
                cursor.execute(
                    'UPDATE sessions SET archived_at = NULL WHERE session_name = ?',
                    (session_name,),
                )
                self.conn.commit()
                return True
            except Exception:
                self.conn.rollback()
                raise

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
                                # T1.2 起标记行与代码块之间多了一行信源声明（不含反引号），
                                # 故容忍这样的一行；无声明行的既有消息仍照常匹配。
                                r"(\[附带上下文: .*?\])(?:\n[^\n`]{1,80})?\n```.*?\n```\n?", 
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
                # CAS：仅当库里水位仍等于本次读到的旧值才推进。无 CAS 时会与
                # 后台压缩的每段提交互相覆盖（谁后写谁赢），令对方进度全废。
                cursor.execute(
                    'UPDATE sessions SET summarized_until_msg_id = ? '
                    'WHERE id = ? AND summarized_until_msg_id IS ?',
                    (new_boundary, session_id, row[1]),
                )
                if cursor.rowcount == 0:
                    self.conn.rollback()
                    return False        # 期间有人推进过水位，本轮放弃，下轮重读
                self.conn.commit()
                return True
            except Exception:
                self.conn.rollback()
                raise

    def maintain_tool_results(self, session_name, tier=1, protection_tokens=PROTECTION_ZONE_TOKENS):
        """分层本地维护旧工具结果（Snip/Elide），DB 层一次性改写并永久冻结。

        tier=1 截断（snip）；tier=2 占位符化（elide，snipped 可升级一次）。
        尾部 protection_tokens 估算 token 为保护区内消息，永不改写。
        只处理 id > summarized_until_msg_id 的活跃消息（已被摘要排除的消息
        不在请求前缀里，无需维护）。
        返回 (snipped_count, elided_count)。
        """
        from core.tool_output_reducer import REDUCED_HINT_MARK, reduce_tool_output
        from core.tool_result_maintenance import (
            ERROR_PREFIXES,
            elide_tool_result,
            estimate_text_tokens,
            extract_handle,
            is_frozen,
            new_handle,
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
            archives = []
            snipped = elided = 0
            now_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
                handle = None
                # 内容寻址句柄：同一段原文 → 同一句柄，配合 INSERT OR IGNORE 让重复
                # 归档自动去重；已 frozen 的 marker 沿用 marker 里的句柄（snip → elide
                # 升级必须是同一句柄，否则同一结果的原文会被重复归档）。
                frozen_handle = extract_handle(text)
                # 命令感知精简：先按输出特征去掉重复行/噪声行。命中时原文必须先
                # 归档（保持可逆），并跳过本条的 snip/elide——已是精简文本，再
                # 截断只会丢信息。reducer 属于 Tier 1 维护，tier=0 时不启用；
                # 已冻结（snip/elide marker）、错误结果、已带精简提示的结果
                # 一律不精简，保持既有幂等与「错误不改写」不变量。
                reduced = None
                if (
                    tier >= 1
                    and not is_frozen(text)
                    and not text.startswith(ERROR_PREFIXES)
                    and REDUCED_HINT_MARK not in text
                ):
                    reduced = reduce_tool_output(name, text)
                if reduced is not None:
                    # 归库存的是 reducer 之前的原文，句柄必须按原文算（不是精简文本）
                    handle = frozen_handle or content_handle(cursor, text)
                    new_text = (
                        reduced.text
                        + f"\n\n[... 原文 {reduced.original_chars} 字符，已按 "
                        f"{reduced.reducer} 规则精简，完整内容见 #{handle} ...]"
                    )
                    # 计入 snipped，与下面的 elide/snip 分支互斥，不会重复累加
                    snipped += 1
                else:
                    # snip/elide 的 marker 必须携带内容寻址句柄，所以先算句柄再传进去
                    if tier >= 1:
                        handle = frozen_handle or content_handle(cursor, text)
                    if tier >= 2:
                        new_text = elide_tool_result(name, text, handle=handle)
                        if new_text is not None:
                            elided += 1
                    if new_text is None and tier >= 1:
                        new_text = snip_tool_result(name, text, handle=handle)
                        if new_text is not None:
                            snipped += 1
                if new_text is None:
                    continue
                # 兜底：正常情况下分支内已经算好句柄，这里只防「改写发生了却没句柄」
                if handle is None:
                    handle = extract_handle(new_text) or new_handle()
                archives.append((
                    handle,
                    session_id,
                    msg_id,
                    name,
                    len(text.encode("utf-8")),
                    text,
                    now_text,
                ))
                if ok and isinstance(parsed, dict):
                    parsed["content"] = new_text
                    new_raw = json.dumps(parsed, ensure_ascii=False)
                else:
                    new_raw = new_text
                updates.append((new_raw, msg_id))

            if updates:
                try:
                    # 归档原文与改写 marker 同一事务提交：要么都成功（可取回），
                    # 要么都回滚（不留指向空归档的 marker）。落库行在此统一加工：
                    # 编码（utf-8 / gzip+base64 / truncated）+ sha256 + 容量统计列；
                    # INSERT OR IGNORE 让同内容重复归档（同句柄）静默跳过。
                    cursor.executemany(
                        _TOOL_RESULT_ARCHIVE_INSERT_SQL,
                        [build_archive_row(record) for record in archives],
                    )
                    cursor.executemany('UPDATE messages SET content = ? WHERE id = ?', updates)
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise
            return (snipped, elided)

    def get_tool_result_archive(self, handle):
        """按归档句柄取回 snip/elide 改写前的原始工具结果；查不到返回 None。

        · 老库可能还没有 sha256/encoding 列，故统一 SELECT * 后按列名取值，
          缺列按 None 处理；
        · content 按 encoding 解码（gzip+base64 反解，truncated/utf-8 原样返回）；
        · 读取成功顺手更新 accessed_at（best effort），供闲时淘汰判断访问热度。
        """
        key = str(handle or "")
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute('SELECT * FROM tool_result_archive WHERE handle = ?', (key,))
            fetched = cursor.fetchone()
            if not fetched:
                return None
            columns = [col[0] for col in (cursor.description or ())]
            record = dict(zip(columns, fetched))
            try:
                cursor.execute(
                    'UPDATE tool_result_archive SET accessed_at = ? WHERE handle = ?',
                    (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), key),
                )
                self.conn.commit()
            except Exception:
                # 只影响淘汰冷热判断，失败静默（旧库无 accessed_at 列也走这里）
                pass
        return {
            "handle": record.get("handle"),
            "session_id": record.get("session_id"),
            "msg_id": record.get("msg_id"),
            "tool_name": record.get("tool_name"),
            "bytes": record.get("bytes"),
            "content": decode_payload(record.get("content"), record.get("encoding")),
            "created_at": record.get("created_at"),
        }

    def prune_tool_result_archive(self, session_name=None, keep_days=None) -> int:
        """清理 tool_result_archive。session_name 给定时只清该会话；keep_days 给定时
        删除 created_at 早于 keep_days 天的行（两种条件可叠加）。返回删除行数。

        安全默认：两个条件都为空时直接返回 0，绝不全表删除。本方法只供会话/
        工作区删除等显式清理路径调用——检索、维护、启动流程都不得自动调用，
        否则仍留在消息 marker 里的 #句柄 会指向已删除的归档而取不回原文。
        """
        if not session_name and keep_days is None:
            return 0
        clauses = []
        params = []
        cutoff_text = None
        if keep_days is not None:
            try:
                days = int(keep_days)
            except (TypeError, ValueError):
                return 0
            if days < 0:
                return 0
            cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
            cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            cursor = self.conn.cursor()
            try:
                if session_name:
                    cursor.execute(
                        'SELECT id FROM sessions WHERE session_name = ?',
                        (session_name,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        return 0
                    clauses.append("session_id = ?")
                    params.append(row[0])
                if cutoff_text is not None:
                    # created_at 是 '%Y-%m-%d %H:%M:%S' 文本，与同格式 cutoff
                    # 直接字符串比较即可（字典序 == 时间序）；NULL 行不会被误删。
                    clauses.append("created_at IS NOT NULL AND created_at < ?")
                    params.append(cutoff_text)
                cursor.execute(
                    'DELETE FROM tool_result_archive WHERE ' + " AND ".join(clauses),
                    params,
                )
                deleted = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                self.conn.commit()
                return deleted
            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                return 0

    def sweep_tool_result_archive(
        self,
        *,
        budget_bytes=ARCHIVE_BUDGET_BYTES,
        keep_days=ARCHIVE_TTL_DAYS,
        protect_seconds=3600,
    ) -> dict:
        """归档层容量治理：先删过期，再按 accessed_at 升序淘汰到预算内。

        返回 {"expired": n, "evicted": n}。protect_seconds 内的记录视为「刚被
        取回」，即使超预算也跳过——它们最可能还被模型引用，删了 marker 就取不回。
        任何异常都静默（回滚 + 返回零统计），这条路径只服务闲时维护。
        """
        stats = {"expired": 0, "evicted": 0}
        with self._lock:
            cursor = self.conn.cursor()
            try:
                now = datetime.datetime.now()
                if keep_days is not None:
                    try:
                        days = max(0, int(keep_days))
                    except (TypeError, ValueError):
                        days = ARCHIVE_TTL_DAYS
                    cutoff_text = (now - datetime.timedelta(days=days)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    # created_at 与 cutoff 同为 '%Y-%m-%d %H:%M:%S' 文本，
                    # 字典序即时间序；NULL 行不被误删（与 prune 保持同口径）。
                    cursor.execute(
                        'DELETE FROM tool_result_archive '
                        'WHERE created_at IS NOT NULL AND created_at < ?',
                        (cutoff_text,),
                    )
                    stats["expired"] = max(cursor.rowcount or 0, 0)
                if budget_bytes is not None:
                    # 旧行没有 stored_bytes，按 LENGTH(content) 估算
                    total = int(
                        cursor.execute(
                            'SELECT COALESCE(SUM(COALESCE(stored_bytes, '
                            'LENGTH(content))), 0) FROM tool_result_archive'
                        ).fetchone()[0]
                        or 0
                    )
                    budget = max(0, int(budget_bytes))
                    if total > budget:
                        try:
                            protect_before = (
                                now - datetime.timedelta(seconds=max(0, int(protect_seconds or 0)))
                            ).strftime("%Y-%m-%d %H:%M:%S")
                        except (TypeError, ValueError):
                            protect_before = now.strftime("%Y-%m-%d %H:%M:%S")
                        # 候选：未访问过（NULL）或最近访问早于保护窗口；按 accessed_at
                        # 升序（冷的最先）淘汰，NULL 视为最冷排最前。
                        cursor.execute(
                            'SELECT handle, COALESCE(stored_bytes, LENGTH(content)) '
                            'FROM tool_result_archive '
                            'WHERE accessed_at IS NULL OR accessed_at < ? '
                            'ORDER BY accessed_at IS NULL DESC, accessed_at ASC, '
                            'created_at ASC',
                            (protect_before,),
                        )
                        for row in cursor.fetchall():
                            if total <= budget:
                                break
                            cursor.execute(
                                'DELETE FROM tool_result_archive WHERE handle = ?',
                                (row[0],),
                            )
                            if cursor.rowcount and cursor.rowcount > 0:
                                total -= int(row[1] or 0)
                                stats["evicted"] += 1
                self.conn.commit()
            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                return {"expired": 0, "evicted": 0}
        return stats

    # ---------- 消息全文检索（FTS5 + bigram 归一化，懒同步） ----------

    # 单条消息参与索引的最大字符数：超大工具结果先截断再归一化，
    # 避免一次检索维护把索引体积撑爆（原文仍在 messages 表中）。
    _FTS_MAX_CONTENT_CHARS = 20000
    # 只索引这三类有检索价值的角色；notice/summary 等注入块不索引。
    _FTS_INDEX_ROLES = ("user", "assistant", "tool")
    _FTS_INSERT_SQL = "INSERT INTO messages_fts(rowid, tokens) VALUES (?, ?)"

    def _ensure_message_fts(self, cursor=None) -> bool:
        """幂等创建消息全文索引表；FTS5 不可用时静默降级并返回 False。

        索引表是独立的 FTS5 表（tokens 列 + unicode61 分词），rowid 对齐
        messages.id。不用 external content 模式：messages 表没有 session_name
        列，无法满足外部内容映射。任何失败（该 SQLite 未编译 FTS5、建表被拒）
        都只记一条 warning，绝不冒泡，不影响其它建表与既有功能。
        """
        if self._fts_available:
            return True
        if self._fts_disabled:
            return False
        with self._lock:
            if self._fts_available:
                return True
            if self._fts_disabled:
                return False
            cur = cursor if cursor is not None else self.conn.cursor()
            try:
                cur.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
                    "USING fts5(tokens, tokenize='unicode61')"
                )
                self.conn.commit()
            except Exception as exc:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                self._fts_available = False
                self._fts_disabled = True
                try:
                    logging.getLogger("py.warnings").warning(
                        "消息全文检索不可用：当前 SQLite 未编译 FTS5（或建表失败: %s）。"
                        "search_sessions 将提示不可用，其它功能不受影响。",
                        exc,
                    )
                except Exception:
                    pass
                return False
            self._fts_available = True
            return True

    @classmethod
    def _fts_tokens_for_content(cls, content) -> str:
        """把一条消息的 content 归一化成索引 token 串（超长先截断）。"""
        text = message_content_to_text(content) if content is not None else ""
        if len(text) > cls._FTS_MAX_CONTENT_CHARS:
            text = text[:cls._FTS_MAX_CONTENT_CHARS]
        return _fts_normalize(text)

    def sync_message_index(self) -> int:
        """把 messages 中尚未入索引的消息增量补进 FTS 索引，返回补入条数。

        懒同步：不在 add_message / add_messages / 删除 / undo / 会话清理等
        任何写入路径挂钩子（避免漏点），检索前调用一次即可。水位取
        messages.MAX(id) 与 messages_fts.MAX(rowid) 的比较结果。已删除消息
        留下的孤儿索引行不主动清理——查询侧的 JOIN messages 会自然排除。
        """
        if not self._ensure_message_fts():
            return 0
        with self._lock:
            reader = self.conn.cursor()
            writer = self.conn.cursor()
            try:
                reader.execute("SELECT MAX(id) FROM messages")
                max_msg_id = int(reader.fetchone()[0] or 0)
                reader.execute("SELECT MAX(rowid) FROM messages_fts")
                max_indexed = int(reader.fetchone()[0] or 0)
                if max_msg_id <= max_indexed:
                    return 0
                reader.execute(
                    "SELECT id, role, content FROM messages "
                    "WHERE id > ? AND role IN ('user', 'assistant', 'tool') "
                    "ORDER BY id ASC",
                    (max_indexed,),
                )
                total = 0
                while True:
                    rows = reader.fetchmany(200)
                    if not rows:
                        break
                    batch = [
                        (int(msg_id), self._fts_tokens_for_content(content))
                        for msg_id, _role, content in rows
                    ]
                    writer.executemany(self._FTS_INSERT_SQL, batch)
                    total += len(batch)
                self.conn.commit()
                return total
            except Exception as exc:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                try:
                    logging.getLogger("py.warnings").warning(
                        "消息全文索引增量同步失败（已回滚，本次检索按现有索引进行）: %s",
                        exc,
                    )
                except Exception:
                    pass
                return 0

    def rebuild_message_index(self) -> int:
        """清空并全量重建消息全文索引，返回重建条数（维护/排障用）。"""
        if not self._ensure_message_fts():
            return 0
        with self._lock:
            reader = self.conn.cursor()
            writer = self.conn.cursor()
            try:
                # FTS5 的 'rebuild' 命令只对 external content 表有效；
                # 本索引是独立表，直接 DELETE 清空即可。
                writer.execute("DELETE FROM messages_fts")
                reader.execute(
                    "SELECT id, role, content FROM messages "
                    "WHERE role IN ('user', 'assistant', 'tool') ORDER BY id ASC"
                )
                total = 0
                while True:
                    rows = reader.fetchmany(200)
                    if not rows:
                        break
                    batch = [
                        (int(msg_id), self._fts_tokens_for_content(content))
                        for msg_id, _role, content in rows
                    ]
                    writer.executemany(self._FTS_INSERT_SQL, batch)
                    total += len(batch)
                self.conn.commit()
                return total
            except Exception as exc:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                try:
                    logging.getLogger("py.warnings").warning(
                        "消息全文索引重建失败（已回滚）: %s", exc
                    )
                except Exception:
                    pass
                return 0

    def search_messages(self, query, limit=10, session_name=None):
        """跨会话按关键词检索历史消息（含已被压缩归档的消息）。

        返回 list[dict]（id/role/session_name/display_name/snippet），按相关度
        排序；FTS5 不可用时返回 None，以区分「不可用」与「无命中」的空列表。
        查询语法问题等 SQLite 层面的错误一律吞掉并记日志，不向调用方抛。
        """
        if not self._ensure_message_fts():
            return None
        match_expr = _fts_match_expression(_fts_normalize(query))
        if not match_expr:
            return []
        try:
            limit_n = int(limit)
        except (TypeError, ValueError):
            limit_n = 10
        limit_n = max(1, min(50, limit_n))
        self.sync_message_index()
        sql = (
            "SELECT m.id, m.role, s.session_name, "
            "COALESCE(NULLIF(s.secretary_name, ''), s.session_name) AS display_name, "
            "m.content AS raw "
            "FROM messages_fts f "
            "JOIN messages m ON m.id = f.rowid "
            "JOIN sessions s ON s.id = m.session_id "
            "WHERE messages_fts MATCH ?"
        )
        params: List[Any] = [match_expr]
        target_session = str(session_name or "").strip()
        if target_session:
            sql += " AND s.session_name = ?"
            params.append(target_session)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit_n)
        with self._lock:
            try:
                rows = self.conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                try:
                    logging.getLogger("py.warnings").warning(
                        "消息全文检索失败（query=%r）: %s", query, exc
                    )
                except Exception:
                    pass
                return []
        return [
            {
                "id": row[0],
                "role": row[1],
                "session_name": row[2],
                "display_name": row[3],
                "snippet": _fts_snippet(message_content_to_text(row[4]), query),
            }
            for row in rows
        ]


# Backward-compatible public name used by the original test and extension API.
SessionManager = MemoryManager
