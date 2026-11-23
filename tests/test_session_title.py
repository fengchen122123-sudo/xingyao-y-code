"""会话 AI 总结命名的纯逻辑测试。"""

import core.session_title as st
from core.session_title import (
    build_title_prompt,
    is_placeholder_session_name,
    sanitize_generated_title,
)


def test_sanitize_generated_title_strips_prefix_quotes_and_punctuation():
    raw = '标题：“冒泡排序，实现。”'
    assert sanitize_generated_title(raw) == "冒泡排序实现"


def test_sanitize_generated_title_caps_at_12_chars():
    raw = "一二三四五六七八九十十一十二十三"
    assert sanitize_generated_title(raw) == "一二三四五六七八九十十一"


def test_sanitize_generated_title_rejects_empty_after_cleaning():
    assert sanitize_generated_title("“”。，") is None
    assert sanitize_generated_title("") is None
    assert sanitize_generated_title(None) is None


def test_sanitize_generated_title_rejects_placeholder_like_result():
    assert sanitize_generated_title("session_abc123") is None


def test_is_placeholder_session_name():
    assert is_placeholder_session_name("session_abc123") is True
    assert is_placeholder_session_name("修复登录页") is False
    assert is_placeholder_session_name("session_ABC123") is False


def test_build_title_prompt_contains_both_sides_and_length_rule():
    prompt = build_title_prompt("帮我写个冒泡排序", "好的，这是实现……")
    assert "帮我写个冒泡排序" in prompt
    assert "好的，这是实现" in prompt
    assert "4" in prompt and "12" in prompt


def test_generate_session_title_returns_cleaned_title(monkeypatch):
    class FakeMessage:
        content = "“冒泡排序”"

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    monkeypatch.setattr(st, "native_completion", lambda **kwargs: FakeResponse())

    assert st.generate_session_title("m", "k", "u", "q", "a") == "冒泡排序"


def test_generate_session_title_returns_none_on_error(monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(st, "native_completion", _boom)

    assert st.generate_session_title("m", "k", "u", "q", "a") is None


def test_generate_session_title_skips_request_without_model_or_input():
    calls = []

    def _record(**kwargs):
        calls.append(kwargs)
        raise AssertionError("缺少模型或材料时不该发起请求")

    import core.session_title as module

    original = module.native_completion
    module.native_completion = _record
    try:
        assert module.generate_session_title("", "k", "u", "q", "a") is None
        assert module.generate_session_title("m", "k", "u", "", "") is None
    finally:
        module.native_completion = original
    assert calls == []
