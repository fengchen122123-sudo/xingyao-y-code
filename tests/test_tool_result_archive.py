"""工具结果归档与取回回归（snip/elide → tool_result_archive → tool_result_read）。

覆盖不变量：
1. snip/elide 的 marker 里带 8 位小写十六进制句柄，模型可据此取回原文；
2. 归档内容是改写前的逐字节原文，bytes 与原文 utf-8 字节数一致；
3. snip → elide 升级复用同一句柄，原文只存一次（INSERT OR IGNORE）；
4. Tier 1 幂等：第二次维护 (0, 0)，归档行数不变；
5. 错误结果（🚨 前缀）与短结果（< 1024 字符）不改写、不入档；
6. tool_result_read 的 head/tail/grep/full 四种 mode 与非法/不存在句柄的友好提示。
"""

import re
import threading

import pytest

import core.tools as tools
from core.memory import MemoryManager, _try_parse_json
from core.tool_result_maintenance import (
    ELIDED_PREFIX,
    SNIPPED_PREFIX,
    extract_handle,
    is_valid_handle,
)

SNIP_HANDLE_RE = re.compile(r"^\[snipped tool result #([0-9a-f]{8})\b")
ELIDE_HANDLE_RE = re.compile(r"^\[elided tool result #([0-9a-f]{8})\b")


@pytest.fixture
def manager():
    m = MemoryManager(":memory:")
    yield m
    m.close()


def _big_text(tag, lines=300):
    """300 行长文本：行数截断策略（read_file: 200 行上限）一定能命中。"""
    return "\n".join(f"{tag}-line-{i} " + "x" * 40 for i in range(lines))


def _fill_tool_session(manager, session, tool_texts):
    """构造 1 条 user + N 组 (assistant tool_call, tool 结果)。

    tool_call_id 必须全局唯一，否则 get_history_for_llm 的配对清洗会合并/丢弃消息。
    """
    manager.add_message(session, "user", "开始")
    for idx, text in enumerate(tool_texts):
        manager.add_messages(session, [
            ("assistant", {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"{session}-call-{idx}", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]}),
            ("tool", {"role": "tool", "tool_call_id": f"{session}-call-{idx}",
                      "name": "read_file", "content": text}),
        ])


def _tool_contents(manager, session):
    """按 id 正序返回 [(msg_id, 工具结果正文)]，正文从入库的 JSON 里解出。"""
    with manager._lock:
        rows = manager.conn.execute(
            "SELECT id, content FROM messages WHERE role = 'tool' AND session_id = "
            "(SELECT id FROM sessions WHERE session_name = ?) ORDER BY id ASC",
            (session,),
        ).fetchall()
    out = []
    for msg_id, raw in rows:
        parsed, ok = _try_parse_json(raw)
        text = parsed.get("content") if ok and isinstance(parsed, dict) else raw
        out.append((int(msg_id), str(text)))
    return out


def _archive_rows(manager):
    """返回归档表的 (handle, msg_id) 列表，用于断言"不增加"。"""
    with manager._lock:
        rows = manager.conn.execute(
            "SELECT handle, msg_id FROM tool_result_archive ORDER BY msg_id ASC"
        ).fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


def _first_snip(manager, session):
    """取第一条被 snip 的工具消息：(msg_id, marker 文本, 句柄)。"""
    for msg_id, text in _tool_contents(manager, session):
        if text.startswith(SNIPPED_PREFIX):
            return msg_id, text, extract_handle(text)
    raise AssertionError("没有找到被 snip 的工具结果")


# ---------------------------------------------------------------------------
# 1. marker 与句柄
# ---------------------------------------------------------------------------


def test_snip_marker_carries_8_hex_handle(manager):
    session = "archive-snip"
    _fill_tool_session(manager, session, [_big_text(f"m{i}") for i in range(6)])

    snipped, elided = manager.maintain_tool_results(session, tier=1, protection_tokens=200)

    # 7 条记录（1 user + 6 tool），最后一条工具结果落在 200 token 保护区
    assert (snipped, elided) == (5, 0)
    markers = [t for _mid, t in _tool_contents(manager, session) if t.startswith(SNIPPED_PREFIX)]
    assert len(markers) == 5
    for marker in markers:
        match = SNIP_HANDLE_RE.match(marker)
        assert match is not None, f"marker 头格式不符: {marker[:80]!r}"
        assert is_valid_handle(match.group(1))
        assert extract_handle(marker) == match.group(1)
    # 句柄互不相同（每条结果独立归档）
    assert len({extract_handle(m) for m in markers}) == 5


# ---------------------------------------------------------------------------
# 2. 归档原文逐字节相等
# ---------------------------------------------------------------------------


def test_archive_content_matches_original_bytes(manager):
    session = "archive-bytes"
    texts = [_big_text(f"m{i}") for i in range(6)]
    _fill_tool_session(manager, session, texts)

    manager.maintain_tool_results(session, tier=1, protection_tokens=200)

    tool_rows = _tool_contents(manager, session)
    assert len(tool_rows) == 6  # 顺序与写入顺序一致，可按位置配回原文
    checked = 0
    for (msg_id, marker), original in zip(tool_rows, texts):
        if not marker.startswith(SNIPPED_PREFIX):
            assert marker == original  # 保护区内原样保留
            continue
        handle = extract_handle(marker)
        archive = manager.get_tool_result_archive(handle)
        assert archive is not None
        assert archive["handle"] == handle
        assert archive["msg_id"] == msg_id
        assert archive["tool_name"] == "read_file"
        assert archive["content"] == original  # 逐字节相等
        assert archive["bytes"] == len(original.encode("utf-8"))
        assert archive["created_at"]
        checked += 1
    assert checked == 5


# ---------------------------------------------------------------------------
# 3. elide 升级：同句柄 + 归档行数不变（INSERT OR IGNORE）
# ---------------------------------------------------------------------------


def test_elide_upgrade_reuses_handle_and_keeps_archive_rows(manager):
    session = "archive-elide"
    _fill_tool_session(manager, session, [_big_text(f"m{i}") for i in range(6)])

    manager.maintain_tool_results(session, tier=1, protection_tokens=200)
    snip_handles = {
        msg_id: extract_handle(text)
        for msg_id, text in _tool_contents(manager, session)
        if text.startswith(SNIPPED_PREFIX)
    }
    rows_after_snip = _archive_rows(manager)
    assert len(rows_after_snip) == 5

    snipped2, elided2 = manager.maintain_tool_results(session, tier=2, protection_tokens=200)
    assert (snipped2, elided2) == (0, 5)

    elide_markers = {
        msg_id: text
        for msg_id, text in _tool_contents(manager, session)
        if text.startswith(ELIDED_PREFIX)
    }
    assert set(elide_markers) == set(snip_handles)
    for msg_id, marker in elide_markers.items():
        match = ELIDE_HANDLE_RE.match(marker)
        assert match is not None, f"elided marker 头格式不符: {marker[:80]!r}"
        # snip → elide 升级必须是同一句柄，否则原文会被重复归档
        assert match.group(1) == snip_handles[msg_id]

    # INSERT OR IGNORE：升级不新增归档行
    assert _archive_rows(manager) == rows_after_snip


# ---------------------------------------------------------------------------
# 4. Tier 1 幂等
# ---------------------------------------------------------------------------


def test_tier1_maintenance_is_idempotent(manager):
    session = "archive-idempotent"
    _fill_tool_session(manager, session, [_big_text(f"m{i}") for i in range(6)])

    first = manager.maintain_tool_results(session, tier=1, protection_tokens=200)
    assert first == (5, 0)
    rows_first = _archive_rows(manager)
    snapshot = [text for _mid, text in _tool_contents(manager, session)]

    second = manager.maintain_tool_results(session, tier=1, protection_tokens=200)
    assert second == (0, 0)
    assert _archive_rows(manager) == rows_first
    assert [text for _mid, text in _tool_contents(manager, session)] == snapshot


# ---------------------------------------------------------------------------
# 5. 错误结果与短结果不改写、不入档
# ---------------------------------------------------------------------------


def test_error_and_short_results_are_never_rewritten_or_archived(manager):
    session = "archive-skip"
    error_text = "🚨 命令执行失败（退出码 1）。\n" + "traceback line\n" * 400
    short_text = "短结果 " + "y" * 100  # 明确 < MIN_SNIP_CHARS(1024)
    assert len(short_text) < 1024
    texts = [error_text, short_text, error_text, short_text, error_text, short_text]
    _fill_tool_session(manager, session, texts)

    snipped, elided = manager.maintain_tool_results(session, tier=2, protection_tokens=100)

    assert (snipped, elided) == (0, 0)
    assert [text for _mid, text in _tool_contents(manager, session)] == texts
    assert _archive_rows(manager) == []


# ---------------------------------------------------------------------------
# 6. tool_result_read 四种 mode + 句柄校验
# ---------------------------------------------------------------------------


@pytest.fixture
def archived(manager, monkeypatch):
    """产出 (manager, monkeypatched 上下文, 句柄, 原文)。"""
    session = "archive-read"
    big = _big_text("row")
    # 两条工具结果 + protection_tokens=1：第一条进可改写区，第二条留保护区
    _fill_tool_session(manager, session, [big, big])
    manager.maintain_tool_results(session, tier=1, protection_tokens=1)
    _msg_id, _marker, handle = _first_snip(manager, session)
    monkeypatch.setattr("core.app_context.get_manager", lambda: manager)
    return manager, handle, big


def test_tool_result_read_head_mode(archived):
    _manager, handle, big = archived
    output = tools.tool_result_read(handle, mode="head", lines=5)

    header, rest = output.split("\n\n", 1)
    assert header.startswith(f"归档工具结果 #{handle}")
    assert "来源工具: read_file" in header
    assert f"原始 {len(big.encode('utf-8'))} 字节 / 300 行" in header
    body = rest.split("\n")
    assert body[0] == "开头 5 行（全文共 300 行）:"
    assert body[1:6] == big.split("\n")[:5]
    assert len(body) == 6
    assert "row-line-5 " not in rest


def test_tool_result_read_tail_mode(archived):
    _manager, handle, big = archived
    output = tools.tool_result_read(handle, mode="tail", lines=5)

    header, rest = output.split("\n\n", 1)
    assert header.startswith(f"归档工具结果 #{handle}")
    body = rest.split("\n")
    assert body[0] == "末尾 5 行（从第 296 行开始）（全文共 300 行）:"
    assert body[1:6] == big.split("\n")[-5:]
    assert "row-line-0 " not in rest


def test_tool_result_read_grep_mode_returns_line_number_and_content(archived):
    _manager, handle, big = archived
    output = tools.tool_result_read(handle, mode="grep", grep="row-line-42 ")

    assert "命中 1 行" in output
    segment = output.split("（最多 100 行，格式 行号:内容）:\n", 1)[1]
    hit = segment.split("\n")[0]
    assert hit.startswith("43:")  # 行号 1-based，"row-line-42" 是第 43 行
    assert hit == "43:" + big.split("\n")[42]

    # 无命中也必须是明确提示，而不是空串
    miss = tools.tool_result_read(handle, mode="grep", grep="肯定不存在的串")
    assert "命中 0 行" in miss


def test_tool_result_read_full_mode_returns_whole_text(archived):
    _manager, handle, big = archived
    output = tools.tool_result_read(handle, mode="full")

    assert f"全文（{len(big)} 字符）:" in output
    assert output.endswith(big)


def test_tool_result_read_rejects_invalid_and_unknown_handle(archived):
    _manager, _handle, _big = archived

    for bad in ("ZZZZ", "DEADBEEF", "", "abc", "abcd_efg"):
        assert tools.tool_result_read(bad).startswith("🚨 无效的句柄")

    missing = tools.tool_result_read("deadbeef")
    assert missing.startswith("归档中没有句柄 #deadbeef 的记录")
    assert "tool_result_read" not in missing  # 提示里不误导模型重试取回


def test_tool_result_read_respects_cancel_event(archived):
    _manager, handle, _big = archived
    cancel_event = threading.Event()
    cancel_event.set()

    assert tools.tool_result_read(handle, cancel_event=cancel_event) == "🛑 操作已被用户取消。"
    # grep 分支不应在取消后继续扫描
    assert tools.tool_result_read(
        handle, mode="grep", grep="row-line-1", cancel_event=cancel_event
    ) == "🛑 操作已被用户取消。"
