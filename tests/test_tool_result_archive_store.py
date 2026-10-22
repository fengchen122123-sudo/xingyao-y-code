"""归档存储层回归：内容寻址句柄 / 压缩编码 / 容量与 TTL 淘汰。

覆盖不变量：
1. 小文本按 utf-8 明文落库，读回逐字节一致；
2. 300KB 结果编码为 gzip+base64，读回逐字节一致；
3. 9MB 结果按头尾各 4MB 截断归档，bytes 仍是真实原始长度；
4. 同一段原文重复归档（两个会话）只存一行，两条 marker 句柄相同；
5. 句柄真碰撞（同 handle、sha256 不同）时回退随机句柄；
6. TTL 过期由 sweep_tool_result_archive 删除；
7. 预算淘汰按 accessed_at 升序，最近访问过的记录受保护；
8. 旧库（缺 sha256/stored_bytes/encoding/accessed_at 列）升级后旧记录仍可读；
9. reducer 分支的句柄同样由原文内容派生，归档的也是原文。
"""

import base64
import datetime
import gzip
import hashlib
import re
import sqlite3

import pytest

from core.memory import (
    ARCHIVE_TTL_DAYS,
    COMPRESS_THRESHOLD_BYTES,
    SINGLE_RECORD_LIMIT_BYTES,
    MemoryManager,
    _TOOL_RESULT_ARCHIVE_INSERT_SQL,
    _try_parse_json,
    build_archive_row,
    content_handle,
    decode_payload,
)
from core.tool_output_reducer import REDUCED_HINT_MARK
from core.tool_result_maintenance import extract_handle

TIME_FMT = "%Y-%m-%d %H:%M:%S"


@pytest.fixture
def manager():
    m = MemoryManager(":memory:")
    yield m
    m.close()


def _now_text(delta_seconds=0):
    moment = datetime.datetime.now() + datetime.timedelta(seconds=delta_seconds)
    return moment.strftime(TIME_FMT)


def _archive(
    manager,
    text,
    *,
    session_id=1,
    msg_id=1,
    tool_name="read_file",
    created_at=None,
    accessed_at=None,
):
    """走生产同一条落库路径写一条归档，返回句柄。

    句柄用 content_handle（内容寻址），落库行用 build_archive_row 加工，
    最后执行归档表唯一写入口的 INSERT 语句——与 maintain_tool_results 一致。
    """
    created = created_at or _now_text()
    accessed = accessed_at or created
    with manager._lock:
        cursor = manager.conn.cursor()
        handle = content_handle(cursor, text)
        row = build_archive_row(
            (handle, session_id, msg_id, tool_name, len(text.encode("utf-8")), text, created)
        )
        # accessed_at 由调用方指定（build_archive_row 默认与 created_at 对齐）
        cursor.execute(_TOOL_RESULT_ARCHIVE_INSERT_SQL, tuple(row[:10]) + (accessed,))
        manager.conn.commit()
    return handle


def _stored_row(manager, handle):
    """按句柄取原始落库行（dict），用于断言编码/容量等存储细节。"""
    with manager._lock:
        cursor = manager.conn.execute(
            "SELECT * FROM tool_result_archive WHERE handle = ?", (handle,)
        )
        fetched = cursor.fetchone()
        columns = [col[0] for col in (cursor.description or ())]
    return dict(zip(columns, fetched)) if fetched else None


def _archive_count(manager):
    with manager._lock:
        row = manager.conn.execute("SELECT COUNT(*) FROM tool_result_archive").fetchone()
    return int(row[0] or 0)


def _big_text(tag, lines=300):
    """300 行长文本：任何 snip 策略都一定会命中。"""
    return "\n".join(f"{tag}-line-{i} " + "x" * 40 for i in range(lines))


def _fill_tool_session(manager, session, texts):
    """构造 1 条 user + N 组 (assistant tool_call, tool 结果) + 尾部保护消息。"""
    manager.add_message(session, "user", "开始")
    for idx, text in enumerate(texts):
        manager.add_messages(session, [
            ("assistant", {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"{session}-call-{idx}", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]}),
            ("tool", {"role": "tool", "tool_call_id": f"{session}-call-{idx}",
                      "name": "read_file", "content": text}),
        ])
    # 尾部再垫一条消息：让所有工具结果都落在可改写区（保护 tokens 只圈最后一条）
    manager.add_message(session, "user", "继续")


def _tool_texts(manager, session):
    """按 id 正序返回该会话的工具结果正文（从入库 JSON 里解出）。"""
    with manager._lock:
        rows = manager.conn.execute(
            "SELECT id, content FROM messages WHERE role = 'tool' AND session_id = "
            "(SELECT id FROM sessions WHERE session_name = ?) ORDER BY id ASC",
            (session,),
        ).fetchall()
    out = []
    for _msg_id, raw in rows:
        parsed, ok = _try_parse_json(raw)
        out.append(str(parsed.get("content") if ok and isinstance(parsed, dict) else raw))
    return out


# ---------------------------------------------------------------------------
# 1. 小文本：utf-8 明文
# ---------------------------------------------------------------------------


def test_small_text_is_stored_as_utf8(manager):
    text = "小文本工具结果：ok\n" + "行内容 " * 30
    assert len(text.encode("utf-8")) <= COMPRESS_THRESHOLD_BYTES
    handle = _archive(manager, text)

    stored = _stored_row(manager, handle)
    assert stored["encoding"] == "utf-8"
    assert stored["content"] == text  # 明文落库，不压缩
    assert stored["stored_bytes"] == len(text)
    assert stored["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert stored["accessed_at"] == stored["created_at"]

    archive = manager.get_tool_result_archive(handle)
    assert archive["content"] == text
    assert archive["bytes"] == len(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# 2. 300KB：gzip+base64
# ---------------------------------------------------------------------------


def test_large_text_is_gzip_base64_encoded(manager):
    text = "".join(f"line-{i:06d} " + "x" * 30 + "\n" for i in range(9000))
    raw = text.encode("utf-8")
    assert COMPRESS_THRESHOLD_BYTES < len(raw) <= SINGLE_RECORD_LIMIT_BYTES
    handle = _archive(manager, text)

    stored = _stored_row(manager, handle)
    assert stored["encoding"] == "gzip+base64"
    assert stored["content"] != text
    assert stored["stored_bytes"] == len(stored["content"])
    assert len(stored["content"]) < len(raw)  # 压缩确实省了体积
    # 落库文本本身可独立解回原文（编码格式自洽，不依赖读取路径）
    assert gzip.decompress(base64.b64decode(stored["content"])).decode("utf-8") == text

    archive = manager.get_tool_result_archive(handle)
    assert archive["content"] == text  # 逐字节一致
    assert archive["bytes"] == len(raw)


# ---------------------------------------------------------------------------
# 3. 9MB：头尾各 4MB + 省略提示，bytes 仍是真实长度
# ---------------------------------------------------------------------------


def test_oversized_text_is_truncated_while_bytes_keeps_original_length(manager):
    half = SINGLE_RECORD_LIMIT_BYTES // 2
    head = "HEAD-MARKER-" + "h" * half
    middle = "m" * (1024 * 1024)
    tail = "t" * half + "-TAIL-MARKER"
    text = head + middle + tail
    raw_len = len(text.encode("utf-8"))
    assert raw_len > SINGLE_RECORD_LIMIT_BYTES
    handle = _archive(manager, text)

    stored = _stored_row(manager, handle)
    assert stored["encoding"] == "truncated"
    assert stored["bytes"] == raw_len  # 对外语义不变：真实原始长度
    assert stored["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()

    archive = manager.get_tool_result_archive(handle)
    assert archive["bytes"] == raw_len
    content = archive["content"]
    assert content.startswith("HEAD-MARKER-")
    assert content.endswith("-TAIL-MARKER")
    assert f"[... {raw_len} 字节超过单条归档上限，中间部分未归档 ...]" in content
    assert len(content.encode("utf-8")) < raw_len


# ---------------------------------------------------------------------------
# 4. 同内容重复归档：一行 + 同一句柄
# ---------------------------------------------------------------------------


def test_same_content_archived_twice_keeps_one_row_and_same_handle(manager):
    big = _big_text("dup")
    sha = hashlib.sha256(big.encode("utf-8")).hexdigest()
    _fill_tool_session(manager, "dup-a", [big])
    _fill_tool_session(manager, "dup-b", [big])

    assert manager.maintain_tool_results("dup-a", tier=1, protection_tokens=1) == (1, 0)
    assert manager.maintain_tool_results("dup-b", tier=1, protection_tokens=1) == (1, 0)

    handles_a = [extract_handle(t) for t in _tool_texts(manager, "dup-a")]
    handles_b = [extract_handle(t) for t in _tool_texts(manager, "dup-b")]
    assert handles_a == [sha[:8]]
    assert handles_b == handles_a  # 同内容 → 同句柄
    assert _archive_count(manager) == 1  # INSERT OR IGNORE 自动去重
    stored = _stored_row(manager, sha[:8])
    assert stored["sha256"] == sha
    assert manager.get_tool_result_archive(sha[:8])["content"] == big


# ---------------------------------------------------------------------------
# 5. 句柄真碰撞：回退随机句柄
# ---------------------------------------------------------------------------


def test_handle_collision_falls_back_to_random_handle(manager):
    target = "碰撞测试的原始内容 " * 60
    sha = hashlib.sha256(target.encode("utf-8")).hexdigest()
    occupied = sha[:8]
    now = _now_text()
    with manager._lock:
        cursor = manager.conn.cursor()
        # 手工占位：同 handle 但 sha256 完全不同 → 真碰撞
        cursor.execute(
            "INSERT INTO tool_result_archive "
            "(handle, session_id, msg_id, tool_name, bytes, content, created_at, "
            "sha256, stored_bytes, encoding, accessed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (occupied, 9, 9, "read_file", 15, "别的原文", now, "0" * 64, 4, "utf-8", now),
        )
        manager.conn.commit()
        # 碰撞检测本身：候选 handle 已被不同内容占用 → 回退随机句柄
        candidate = content_handle(cursor, target)

    assert candidate != occupied
    assert re.fullmatch(r"[0-9a-f]{8}", candidate)

    # 落库也走回退句柄：新内容能正常归档，旧的占位行不被覆盖
    handle = _archive(manager, target, msg_id=2)
    assert handle != occupied
    assert re.fullmatch(r"[0-9a-f]{8}", handle)
    assert _archive_count(manager) == 2
    assert manager.get_tool_result_archive(handle)["content"] == target
    assert manager.get_tool_result_archive(occupied)["content"] == "别的原文"


# ---------------------------------------------------------------------------
# 6. TTL 过期
# ---------------------------------------------------------------------------


def test_sweep_deletes_expired_records(manager):
    old_text = _now_text(-(ARCHIVE_TTL_DAYS + 23) * 24 * 3600)
    stale = _archive(
        manager, "三十天前的旧归档 " * 40, msg_id=1, created_at=old_text, accessed_at=old_text
    )
    fresh = _archive(manager, "今天的归档 " * 40, msg_id=2)

    stats = manager.sweep_tool_result_archive()

    assert stats["expired"] >= 1
    assert stats["evicted"] == 0  # 两条小记录远不到 256MB 预算
    assert manager.get_tool_result_archive(stale) is None
    assert manager.get_tool_result_archive(fresh) is not None


# ---------------------------------------------------------------------------
# 7. 预算淘汰：冷的最先走，刚访问过的受保护
# ---------------------------------------------------------------------------


def test_sweep_evicts_coldest_and_protects_recently_accessed(manager):
    cold = _archive(manager, "冷记录 " * 200, msg_id=1, accessed_at=_now_text(-5 * 3600))
    hot = _archive(manager, "热记录 " * 200, msg_id=2, accessed_at=_now_text(-5))

    stats = manager.sweep_tool_result_archive(budget_bytes=1, protect_seconds=3600)

    assert stats["expired"] == 0  # 两条都是刚创建，未过期
    assert stats["evicted"] == 1  # 只淘汰冷记录：热记录在保护窗口内被跳过
    assert manager.get_tool_result_archive(cold) is None
    assert manager.get_tool_result_archive(hot) is not None


# ---------------------------------------------------------------------------
# 8. 旧库升级：缺列自动补齐，旧记录仍可读
# ---------------------------------------------------------------------------


def test_legacy_table_without_new_columns_is_upgraded_and_readable(tmp_path):
    db_path = tmp_path / "legacy.db"
    legacy_text = "旧库里的原文"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tool_result_archive ("
        "handle TEXT PRIMARY KEY, session_id INTEGER NOT NULL, msg_id INTEGER, "
        "tool_name TEXT, bytes INTEGER NOT NULL, content TEXT NOT NULL, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO tool_result_archive "
        "(handle, session_id, msg_id, tool_name, bytes, content, created_at) "
        "VALUES ('abcd1234', 1, 1, 'read_file', ?, ?, '2026-01-01 00:00:00')",
        (len(legacy_text.encode("utf-8")), legacy_text),
    )
    conn.commit()
    conn.close()

    manager = MemoryManager(str(db_path))
    try:
        columns = {
            row[1] for row in manager.conn.execute("PRAGMA table_info(tool_result_archive)")
        }
        assert {"sha256", "stored_bytes", "encoding", "accessed_at"} <= columns

        archive = manager.get_tool_result_archive("abcd1234")
        assert archive["content"] == legacy_text
        assert archive["bytes"] == len(legacy_text.encode("utf-8"))
        assert archive["created_at"] == "2026-01-01 00:00:00"
        # 旧行 sha256 仍为 NULL（按 utf-8 原文处理即可），读取后 accessed_at 被补上
        stored = _stored_row(manager, "abcd1234")
        assert stored["sha256"] is None
        assert stored["accessed_at"]
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# 9. reducer 分支：句柄由 reducer 之前的原文派生
# ---------------------------------------------------------------------------


def test_reducer_branch_handle_is_content_addressed(manager):
    noisy = "\n".join(["重复行 same line"] * 400)
    _fill_tool_session(manager, "reduce", [noisy])

    assert manager.maintain_tool_results("reduce", tier=1, protection_tokens=1) == (1, 0)

    marker = _tool_texts(manager, "reduce")[0]
    assert REDUCED_HINT_MARK in marker
    match = re.search(r"完整内容见 #([0-9a-f]{8})", marker)
    assert match is not None, f"精简提示里缺少句柄: {marker[-120:]!r}"
    handle = match.group(1)
    assert handle == hashlib.sha256(noisy.encode("utf-8")).hexdigest()[:8]

    archive = manager.get_tool_result_archive(handle)
    assert archive["content"] == noisy  # 归档的是 reducer 之前的原文
    assert archive["bytes"] == len(noisy.encode("utf-8"))


# ---------------------------------------------------------------------------
# 10. 解码容错：坏数据不能让读取崩
# ---------------------------------------------------------------------------


def test_decode_payload_tolerates_unknown_encoding_and_corrupt_gzip():
    assert decode_payload("原样返回", None) == "原样返回"
    assert decode_payload("原样返回", "unknown-enc") == "原样返回"
    assert decode_payload("not-gzip-base64", "gzip+base64") == "not-gzip-base64"
