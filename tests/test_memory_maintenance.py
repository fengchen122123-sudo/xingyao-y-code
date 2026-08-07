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
