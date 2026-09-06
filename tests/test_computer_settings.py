"""电脑操控设置（core/computer/settings.py）的严格行为测试。"""

import json
import sys
from pathlib import Path

import pytest

import config
from core.computer.models import ComputerControlError
from core.computer.settings import (
    BROWSER_CONTROL_CONFIG_KEY,
    COMPUTER_BROWSER_CONFIG_KEY,
    COMPUTER_CONTROL_CONFIG_KEY,
    DEFAULT_COMPUTER_BROWSER,
    get_browser_profile_dir,
    get_settings_generation,
    list_browser_options,
    load_computer_settings,
    set_browser_control_enabled,
    set_computer_browser,
    set_computer_control_enabled,
)
from tests.computer_fakes import use_temp_config


# ---------------- brief 样例（逐字） ----------------


def test_missing_computer_settings_are_safe_defaults(tmp_path, monkeypatch):
    use_temp_config(tmp_path, monkeypatch, {})
    snapshot = load_computer_settings()
    assert snapshot.browser_control_enabled is False
    assert snapshot.computer_control_enabled is False
    assert snapshot.computer_browser == "edge"


@pytest.mark.parametrize("key,value", [
    ("browser_control_enabled", 1),
    ("computer_control_enabled", "false"),
    ("computer_browser", "chromium"),
])
def test_explicit_invalid_config_is_rejected(tmp_path, monkeypatch, key, value):
    use_temp_config(tmp_path, monkeypatch, {key: value})
    with pytest.raises(ComputerControlError):
        load_computer_settings()


def test_enable_does_not_persist_when_preflight_fails(tmp_path, monkeypatch):
    path = use_temp_config(tmp_path, monkeypatch, {})
    with pytest.raises(ComputerControlError, match="热键"):
        set_browser_control_enabled(True, preflight=lambda: (_ for _ in ()).throw(RuntimeError("热键不可用")))
    assert "browser_control_enabled" not in json.loads(path.read_text(encoding="utf-8"))


def test_disable_persists_and_increments_generation_before_stop(tmp_path, monkeypatch):
    use_temp_config(tmp_path, monkeypatch, {"computer_control_enabled": True})
    before = get_settings_generation()
    snapshot = set_computer_control_enabled(False, preflight=lambda: None)
    assert snapshot.computer_control_enabled is False
    assert get_settings_generation() == before + 1


# ---------------- 补充：默认值与损坏配置 ----------------


def test_invalid_config_error_code_is_stable(tmp_path, monkeypatch):
    use_temp_config(tmp_path, monkeypatch, {"browser_control_enabled": 1})
    with pytest.raises(ComputerControlError) as exc_info:
        load_computer_settings()
    assert exc_info.value.code == "computer_invalid_config"


@pytest.mark.parametrize("value", [0, 1, "true", "false", None, [], {}])
def test_non_bool_values_are_never_truthy_coerced(tmp_path, monkeypatch, value):
    use_temp_config(tmp_path, monkeypatch, {"computer_control_enabled": value})
    with pytest.raises(ComputerControlError) as exc_info:
        load_computer_settings()
    assert exc_info.value.code == "computer_invalid_config"


@pytest.mark.parametrize("value", ["chromium", "firefox", "EDGE", "", 1, None])
def test_invalid_browser_value_is_rejected(tmp_path, monkeypatch, value):
    use_temp_config(tmp_path, monkeypatch, {"computer_browser": value})
    with pytest.raises(ComputerControlError) as exc_info:
        load_computer_settings()
    assert exc_info.value.code == "computer_invalid_config"


def test_default_browser_constant_is_edge():
    assert DEFAULT_COMPUTER_BROWSER == "edge"
    assert BROWSER_CONTROL_CONFIG_KEY == "browser_control_enabled"
    assert COMPUTER_CONTROL_CONFIG_KEY == "computer_control_enabled"
    assert COMPUTER_BROWSER_CONFIG_KEY == "computer_browser"


def test_snapshot_exposes_static_fields_without_runtime_state(tmp_path, monkeypatch):
    use_temp_config(tmp_path, monkeypatch, {})
    snapshot = load_computer_settings()
    assert isinstance(snapshot.browser_options, tuple)
    assert {option.browser for option in snapshot.browser_options} == {"edge", "chrome"}
    assert isinstance(snapshot.profile_display_path, str)
    assert snapshot.platform_supported is (sys.platform == "win32")
    assert not hasattr(snapshot, "hotkey_available")
    assert not hasattr(snapshot, "run_summary")


def test_non_win32_platform_is_not_supported(tmp_path, monkeypatch):
    use_temp_config(tmp_path, monkeypatch, {})
    snapshot = load_computer_settings(platform="linux")
    assert snapshot.platform_supported is False


# ---------------- 补充：setter 语义 ----------------


def test_setter_rejects_non_bool_value(tmp_path, monkeypatch):
    use_temp_config(tmp_path, monkeypatch, {})
    with pytest.raises(ComputerControlError) as exc_info:
        set_browser_control_enabled(1, preflight=lambda: None)
    assert exc_info.value.code == "computer_invalid_config"


def test_enable_persists_after_successful_preflight(tmp_path, monkeypatch):
    path = use_temp_config(tmp_path, monkeypatch, {})
    calls = []
    before = get_settings_generation()
    snapshot = set_browser_control_enabled(True, preflight=lambda: calls.append(1))
    assert calls == [1]
    assert snapshot.browser_control_enabled is True
    assert json.loads(path.read_text(encoding="utf-8"))["browser_control_enabled"] is True
    assert get_settings_generation() == before + 1


def test_disable_never_runs_preflight(tmp_path, monkeypatch):
    use_temp_config(tmp_path, monkeypatch, {"browser_control_enabled": True})

    def _boom():
        raise AssertionError("preflight must not run when disabling")

    snapshot = set_browser_control_enabled(False, preflight=_boom)
    assert snapshot.browser_control_enabled is False


def test_save_failure_raises_invalid_config(tmp_path, monkeypatch):
    use_temp_config(tmp_path, monkeypatch, {})
    monkeypatch.setattr(config, "save_config_value", lambda _key, _value: False)
    with pytest.raises(ComputerControlError) as exc_info:
        set_computer_control_enabled(False, preflight=lambda: None)
    assert exc_info.value.code == "computer_invalid_config"


def test_browser_change_rejected_while_run_active(tmp_path, monkeypatch):
    path = use_temp_config(tmp_path, monkeypatch, {"computer_browser": "edge"})
    with pytest.raises(ComputerControlError) as exc_info:
        set_computer_browser("chrome", active_run=True)
    assert exc_info.value.code == "computer_session_busy"
    assert json.loads(path.read_text(encoding="utf-8"))["computer_browser"] == "edge"


@pytest.mark.parametrize("value", ["chromium", "EDGE", "", 1, None])
def test_browser_setter_rejects_invalid_value(tmp_path, monkeypatch, value):
    use_temp_config(tmp_path, monkeypatch, {})
    with pytest.raises(ComputerControlError) as exc_info:
        set_computer_browser(value, active_run=False)
    assert exc_info.value.code == "computer_invalid_config"


def test_browser_change_persists_and_bumps_generation(tmp_path, monkeypatch):
    path = use_temp_config(tmp_path, monkeypatch, {})
    before = get_settings_generation()
    snapshot = set_computer_browser("chrome", active_run=False)
    assert snapshot.computer_browser == "chrome"
    assert json.loads(path.read_text(encoding="utf-8"))["computer_browser"] == "chrome"
    assert get_settings_generation() == before + 1


# ---------------- 补充：profile 路径隔离与逃逸防护 ----------------


def test_profile_dirs_are_isolated_per_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "YCODE_DIR", str(tmp_path))
    edge_dir = get_browser_profile_dir("edge")
    chrome_dir = get_browser_profile_dir("chrome")
    root = (tmp_path / "browser-profiles").resolve()
    assert edge_dir != chrome_dir
    assert edge_dir == root / "edge"
    assert chrome_dir == root / "chrome"
    assert edge_dir.is_relative_to(root)
    assert chrome_dir.is_relative_to(root)


@pytest.mark.parametrize("browser", ["..", "../evil", "edge/../chrome", "", "chromium", 1, None])
def test_profile_dir_rejects_escape_or_unknown_browser(tmp_path, monkeypatch, browser):
    monkeypatch.setattr(config, "YCODE_DIR", str(tmp_path))
    with pytest.raises(ComputerControlError) as exc_info:
        get_browser_profile_dir(browser)
    assert exc_info.value.code == "computer_invalid_config"


def test_snapshot_profile_path_uses_ycode_dir_string_safely(tmp_path, monkeypatch):
    use_temp_config(tmp_path, monkeypatch, {"computer_browser": "chrome"})
    monkeypatch.setattr(config, "YCODE_DIR", str(tmp_path))
    snapshot = load_computer_settings()
    expected = (Path(str(tmp_path)) / "browser-profiles" / "chrome").resolve()
    assert snapshot.profile_display_path == str(expected)


# ---------------- 补充：浏览器探测不 fallback ----------------


def _patch_no_explicit_installs(monkeypatch):
    monkeypatch.setattr("core.computer.platforms.windows.os.path.isfile", lambda _p: False)


def test_browser_detection_uses_which_and_explicit_paths(monkeypatch):
    edge_exe = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    chrome_exe = r"C:\tools\chrome\chrome.exe"
    monkeypatch.setattr(
        "core.computer.platforms.windows.shutil.which",
        lambda name: chrome_exe if name == "chrome.exe" else None,
    )
    monkeypatch.setattr(
        "core.computer.platforms.windows.os.path.isfile",
        lambda p: p == edge_exe,
    )
    options = {option.browser: option for option in list_browser_options(platform="win32")}
    assert options["edge"].available is True
    assert options["edge"].executable == edge_exe
    assert options["chrome"].available is True
    assert options["chrome"].executable == chrome_exe
    assert options["edge"].unavailable_reason == ""


def test_browser_options_are_independent_and_never_fallback(tmp_path, monkeypatch):
    use_temp_config(tmp_path, monkeypatch, {"computer_browser": "edge"})
    chrome_exe = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    monkeypatch.setattr(
        "core.computer.platforms.windows.shutil.which",
        lambda name: chrome_exe if name == "chrome.exe" else None,
    )
    _patch_no_explicit_installs(monkeypatch)

    options = {option.browser: option for option in list_browser_options(platform="win32")}
    # 所选 edge 不可用时绝不改用 chrome：edge 保持不可用且带原因
    assert options["edge"].available is False
    assert options["edge"].executable == ""
    assert options["edge"].unavailable_reason != ""
    assert options["chrome"].available is True

    snapshot = load_computer_settings(platform="win32")
    assert snapshot.computer_browser == "edge"
    snap_options = {option.browser: option for option in snapshot.browser_options}
    assert snap_options["edge"].available is False
    assert snap_options["chrome"].available is True


def test_browser_option_to_dict_shape(monkeypatch):
    _patch_no_explicit_installs(monkeypatch)
    monkeypatch.setattr("core.computer.platforms.windows.shutil.which", lambda _name: None)
    option = list_browser_options(platform="win32")[0].to_dict()
    assert set(option) == {"browser", "label", "available", "executable", "unavailable_reason"}
    assert option["available"] is False
