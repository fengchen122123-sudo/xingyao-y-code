import core.model_manager
from core.llm import _truncate_tool_result_for_context


def test_read_file_unlimited_by_default(monkeypatch):
    monkeypatch.setattr(core.model_manager, "load_config", lambda: {})
    big = "x" * 100000
    assert _truncate_tool_result_for_context("read_file", big) == big


def test_read_file_truncated_when_configured(monkeypatch):
    monkeypatch.setattr(
        core.model_manager, "load_config",
        lambda: {"read_file_result_max_chars": 20000},
    )
    big = "h" * 15000 + "m" * 80000 + "t" * 15000
    out = _truncate_tool_result_for_context("read_file", big)
    assert len(out) < len(big)
    # 上限 20000：保头 10000 + 保尾 10000，中间省略
    assert out.startswith("h" * 10000)
    assert out.endswith("t" * 10000)
    assert "中间省略" in out
    # 未超限时原样返回
    small = "s" * 100
    assert _truncate_tool_result_for_context("read_file", small) == small


def test_read_file_config_error_falls_back_unlimited(monkeypatch):
    def _boom():
        raise RuntimeError("config corrupted")

    monkeypatch.setattr(core.model_manager, "load_config", _boom)
    big = "x" * 100000
    assert _truncate_tool_result_for_context("read_file", big) == big


def test_other_tools_truncation_unchanged(monkeypatch):
    monkeypatch.setattr(core.model_manager, "load_config", lambda: {})
    big = "y" * 30000
    out = _truncate_tool_result_for_context("fetch_url", big)
    # fetch_url 上限 16000，超出部分截断
    assert len(out) < len(big)
    assert "已截断" in out
