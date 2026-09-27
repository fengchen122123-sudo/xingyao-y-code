"""browser_set_viewport 工具包装函数（core/tool_registry.py）的行为测试。"""

from core.tool_registry import _browser_set_viewport_wrapper


def test_wrapper_rejects_when_session_has_no_project(monkeypatch):
    import core.workspace_context as wc

    monkeypatch.setattr(wc, "get_workdir", lambda: None)
    result = _browser_set_viewport_wrapper(800, 600)
    assert result.is_error is True
    assert "computer_invalid_config" in result.content


def test_wrapper_writes_project_setting(tmp_path, monkeypatch):
    import core.workspace_context as wc
    from core.computer.settings import get_project_browser_viewport

    workdir = tmp_path / "proj"
    workdir.mkdir()
    monkeypatch.setattr(wc, "get_workdir", lambda: str(workdir))

    result = _browser_set_viewport_wrapper(1024, 768)

    assert result.is_error is False
    assert get_project_browser_viewport(str(workdir)) == (1024, 768)
    assert "1024×768" in result.content


def test_wrapper_clamps_out_of_range_dimensions(tmp_path, monkeypatch):
    import core.workspace_context as wc
    from core.computer.settings import get_project_browser_viewport

    workdir = tmp_path / "proj"
    workdir.mkdir()
    monkeypatch.setattr(wc, "get_workdir", lambda: str(workdir))

    result = _browser_set_viewport_wrapper(50, 9000)

    assert result.is_error is False
    assert get_project_browser_viewport(str(workdir)) == (120, 8192)
