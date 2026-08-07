import pytest

from core.memory import MemoryManager
from core.tool_result_maintenance import ELIDED_PREFIX, SNIPPED_PREFIX


@pytest.fixture
def manager_tmp():
    manager = MemoryManager(":memory:")
    yield manager
    manager.close()


def _fill_tool_session(manager, session, tool_texts):
    manager.add_message(session, "user", "开始")
    for idx, text in enumerate(tool_texts):
        # tool_call_id 必须全局唯一，否则 get_history_for_llm 的配对清洗会合并/丢弃消息
        manager.add_messages(session, [
            ("assistant", {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call-{idx}", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]}),
            ("tool", {"role": "tool", "tool_call_id": f"call-{idx}",
                      "name": "read_file", "content": text}),
        ])


def test_maintain_snips_old_tool_results_but_protects_recent_tail(manager_tmp):
    session = "maintain-snip"
    big = "\n".join(f"line-{i} " + "x" * 40 for i in range(300))
    _fill_tool_session(manager_tmp, session, [big] * 6)
    snipped, elided = manager_tmp.maintain_tool_results(session, tier=1, protection_tokens=200)
    assert snipped > 0
    assert elided == 0
    history = manager_tmp.get_history_for_llm(session)
    tool_texts = [m["content"] for m in history if m.get("role") == "tool"]
    # 旧结果被 snip 且带 marker；尾部保护区内的保持原样
    assert any(t.startswith(SNIPPED_PREFIX) for t in tool_texts)
    assert any(t == big for t in tool_texts)
    # 幂等：再跑一遍不应再有新改写
    snipped2, _ = manager_tmp.maintain_tool_results(session, tier=1, protection_tokens=200)
    assert snipped2 == 0


def test_maintain_tier2_upgrades_snipped_to_elided(manager_tmp):
    session = "maintain-elide"
    big = "\n".join(f"line-{i} " + "x" * 40 for i in range(300))
    _fill_tool_session(manager_tmp, session, [big] * 6)
    manager_tmp.maintain_tool_results(session, tier=1, protection_tokens=200)
    snipped, elided = manager_tmp.maintain_tool_results(session, tier=2, protection_tokens=200)
    assert elided > 0
    history = manager_tmp.get_history_for_llm(session)
    tool_texts = [m["content"] for m in history if m.get("role") == "tool"]
    assert any(t.startswith(ELIDED_PREFIX) for t in tool_texts)
    # 再次 tier=2 全部冻结
    s3, e3 = manager_tmp.maintain_tool_results(session, tier=2, protection_tokens=200)
    assert s3 == 0 and e3 == 0


def test_maintain_never_touches_error_results(manager_tmp):
    session = "maintain-error"
    err = "🚨 命令执行失败（退出码 1）。\n" + "traceback\n" * 500
    _fill_tool_session(manager_tmp, session, [err] * 6)
    s, e = manager_tmp.maintain_tool_results(session, tier=2, protection_tokens=200)
    assert s == 0 and e == 0
    history = manager_tmp.get_history_for_llm(session)
    assert any(m.get("role") == "tool" and m["content"] == err for m in history)


# ---------------------------------------------------------------------------
# 入库时折叠旧媒体（替代请求路径改写历史）
# ---------------------------------------------------------------------------


def test_add_media_message_folds_previous_media_in_db(manager_tmp):
    session = "media-fold"
    img1 = [{"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]
    img2 = [{"type": "text", "text": "再看这张"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBB"}}]
    manager_tmp.add_message(session, "user", img1)
    manager_tmp.add_message(session, "assistant", "看到了")
    manager_tmp.add_message(session, "user", img2)
    history = manager_tmp.get_history_for_llm(session)
    users = [m for m in history if m.get("role") == "user"]
    # 第一张已被折叠为文本占位符；第二张原样保留
    first_content = users[0]["content"]
    assert all(b.get("type") == "text" for b in first_content)
    assert any("已折叠" in b.get("text", "") for b in first_content)
    last_content = users[-1]["content"]
    assert any(b.get("type") == "image_url" for b in last_content)
    # 幂等：再次写入普通消息不影响已折叠内容
    manager_tmp.add_message(session, "assistant", "明白")
    history2 = manager_tmp.get_history_for_llm(session)
    users2 = [m for m in history2 if m.get("role") == "user"]
    assert users2[0]["content"] == first_content


def test_add_media_message_single_media_stays_untouched(manager_tmp):
    session = "media-single"
    img = [{"type": "text", "text": "唯一的图"},
           {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]
    manager_tmp.add_message(session, "user", img)
    manager_tmp.add_message(session, "assistant", "收到")
    manager_tmp.add_message(session, "user", "纯文本追问")
    history = manager_tmp.get_history_for_llm(session)
    users = [m for m in history if m.get("role") == "user"]
    # 只有一张媒体图且最新 user 是纯文本：图片仍保留（折叠只发生在新媒体入库时）
    assert any(b.get("type") == "image_url" for b in users[0]["content"])
