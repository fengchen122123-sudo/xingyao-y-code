"""回合来源白名单一致性：入口层与低层必须同步。

背景（真实缺陷）：飞书通道新增来源 `feishu` 时只改了入口层
`gui_bridge.send_user_message` 的白名单，低层 `core/llm.chat_with_model_events`
的 `computer_turn_source` 校验仍是 `("local", "lan")`——于是飞书回合一发起模型请求
就抛 `ValueError: computer_turn_source 只允许受控值 local 或 lan`，表现为
「飞书里发消息，收到执行出错卡」。

本文件把这类「漏改一处枚举」的缺陷钉死：新增来源必须两处同时登记。
"""

import pytest

from core.llm import COMPUTER_TURN_SOURCES, chat_with_model_events
from gui_bridge import TURN_SOURCES


def test_entry_and_low_level_whitelists_match():
    """入口层受控来源与低层校验必须完全一致（不一致就是漏改）。"""
    assert set(TURN_SOURCES) == set(COMPUTER_TURN_SOURCES)


def test_expected_sources_present():
    for source in ("local", "lan", "feishu"):
        assert source in TURN_SOURCES
        assert source in COMPUTER_TURN_SOURCES


def test_every_entry_source_passes_low_level_validation(monkeypatch):
    """逐个来源走一遍低层校验：合法来源不得被「只允许受控值」拒绝。

    校验通过后用哨兵异常中断，避免真正发起模型请求（本用例只验证校验边界）。
    """
    import core.llm as llm

    sentinel = RuntimeError("passed-source-validation")
    monkeypatch.setattr(llm, "take_turn_computer_capability", lambda: None)

    def _boom(*args, **kwargs):
        raise sentinel

    monkeypatch.setattr(llm, "resolve_turn_computer_capability", _boom)

    for source in llm.COMPUTER_TURN_SOURCES:
        gen = chat_with_model_events(
            "nonexistent-model", "sk-test", [], computer_turn_source=source, persist=False
        )
        with pytest.raises(RuntimeError) as exc:
            next(gen)
        assert exc.value is sentinel, f"{source} 未通过低层来源校验"


def test_low_level_still_rejects_unknown_source():
    """护栏本身还在：非法来源必须被拒（不能因为放宽而失效）。"""
    gen = chat_with_model_events(
        "nonexistent-model", "sk-test", [], computer_turn_source="git_panel", persist=False
    )
    with pytest.raises(ValueError) as exc:
        next(gen)
    assert "只允许受控值" in str(exc.value)
    # 文案要列全受控值，避免报错时让人以为只有 local/lan
    for source in COMPUTER_TURN_SOURCES:
        assert source in str(exc.value)
