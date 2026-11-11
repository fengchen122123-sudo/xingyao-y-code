import base64
import copy
import ctypes
import json
import shutil
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import config
import core.model_manager as model_manager
import gui_bridge
import gui_main
from core.gui_theme import BUILTIN_THEMES, ThemeError, ThemeRepository
from gui_bridge import ApiBridge


def _use_tmp_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(model_manager, "_CONFIG_FILE", str(cfg))
    return cfg


def _bare_bridge():
    """不经 __init__ 构造裸实例，避免启动期的重初始化副作用。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._hwnd = 0
    bridge._titlebar_dark = True
    return bridge


def complete_light_theme():
    return copy.deepcopy(BUILTIN_THEMES["builtin.light"])


def editable_light_theme(theme_id="rose"):
    theme = complete_light_theme()
    theme.update({"id": theme_id, "name": "Rose", "extends": "builtin.light", "builtin": False})
    return theme


class BrokenRepo:
    def list_themes(self):
        raise ThemeError("theme_not_found", "gui_theme_id", "missing")


class RecordingRepo:
    def __init__(self, theme):
        self.theme = theme
        self.calls = []

    def get_theme(self, theme_id):
        self.calls.append(f"get:{theme_id}")
        return self.theme

    def set_active(self, theme_id):
        self.calls.append(f"set_active:{theme_id}")
        return self.theme

    def get_active(self):
        self.calls.append("get_active")
        return self.theme


class RepoWithActive:
    def __init__(self, theme):
        self.theme = theme

    def get_active(self):
        return self.theme


class RepoWithBrokenList(RepoWithActive):
    def list_themes(self):
        raise ThemeError("invalid_manifest", "broken/theme.json", "broken")


class FailingSetActiveRepo(RecordingRepo):
    def set_active(self, theme_id):
        self.calls.append(f"set_active:{theme_id}")
        raise OSError("disk full")


class ExplodingRepo:
    def __init__(self, method_name, error):
        self.method_name = method_name
        self.error = error

    def __getattr__(self, name):
        if name != self.method_name:
            raise AttributeError(name)

        def explode(*_args, **_kwargs):
            raise self.error

        return explode


def test_list_gui_themes_returns_structured_error(monkeypatch):
    bridge = _bare_bridge()
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: BrokenRepo())
    result = bridge.list_gui_themes()
    assert result == {
        "ok": False,
        "error": {"code": "theme_not_found", "path": "gui_theme_id", "message": "missing"},
    }


def test_set_active_gui_theme_applies_dwm_before_persisting(monkeypatch):
    bridge = _bare_bridge()
    repo = RecordingRepo(theme=complete_light_theme())
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: repo)
    monkeypatch.setattr(bridge, "_apply_titlebar_colors", Mock(return_value=True))
    result = bridge.set_active_gui_theme("rose")
    assert result["ok"] is True
    assert repo.calls == ["get:rose", "set_active:rose"]


def test_dwm_failure_does_not_change_active_id(monkeypatch):
    bridge = _bare_bridge()
    repo = RecordingRepo(theme=complete_light_theme())
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: repo)
    monkeypatch.setattr(bridge, "_apply_titlebar_colors", Mock(return_value=False))
    result = bridge.set_active_gui_theme("rose")
    assert result["ok"] is False
    assert result["error"]["code"] == "native_titlebar_failed"
    assert repo.calls == ["get:rose"]


def test_preview_and_restore_titlebar_do_not_change_active_id(monkeypatch):
    bridge = _bare_bridge()
    repo = RecordingRepo(theme=complete_light_theme())
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: repo)
    apply = Mock(return_value=True)
    monkeypatch.setattr(bridge, "_apply_titlebar_colors", apply)
    assert bridge.preview_native_titlebar(complete_light_theme()["native_titlebar"])["ok"] is True
    restored = bridge.restore_native_titlebar()
    assert restored["ok"] is True
    assert restored["theme"] == complete_light_theme()
    assert repo.calls == ["get_active"]
    assert not any(call.startswith("set_active:") for call in repo.calls)


def test_get_active_gui_theme_returns_authoritative_theme_without_writing_dwm(monkeypatch):
    bridge = _bare_bridge()
    theme = complete_light_theme()
    repo = RecordingRepo(theme=theme)
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: repo)
    apply = Mock(return_value=True)
    monkeypatch.setattr(bridge, "_apply_titlebar_colors", apply)

    result = bridge.get_active_gui_theme()

    assert result["ok"] is True
    assert result["theme"] == theme
    assert repo.calls == ["get_active"]
    apply.assert_not_called()


@pytest.mark.parametrize("color,expected", [
    ("#20201e", 0x001E2020),
    ("#ffffff", 0x00FFFFFF),
    ("#6a92d8", 0x00D8926A),
])
def test_rgb_hex_to_colorref(color, expected):
    assert gui_bridge._rgb_hex_to_colorref(color) == expected


def test_initial_theme_metadata_comes_from_repository(monkeypatch):
    monkeypatch.setattr(gui_main, "get_gui_theme_repository", lambda: RepoWithActive(complete_light_theme()))
    meta = gui_main._initial_gui_theme()
    assert meta == {"appearance": "light", "background": "#f4f4f1"}


def test_read_edit_save_complete_gui_theme_dto(tmp_path, monkeypatch):
    repo = ThemeRepository(tmp_path / "themes", tmp_path / "config.json")
    repo.copy_theme("builtin.light", "rose", "Rose")
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: repo)
    bridge = _bare_bridge()

    dto = bridge.get_gui_theme("rose")["theme"]
    assert dto["builtin"] is False
    dto["name"] = "Rose Edited"
    result = bridge.save_gui_theme(dto)

    assert result["ok"] is True
    assert result["theme"]["name"] == "Rose Edited"
    assert repo.get_theme("rose")["name"] == "Rose Edited"


def test_read_gui_theme_asset_returns_exact_base64_envelope(monkeypatch):
    repo = SimpleNamespace(read_asset=lambda _theme_id, _asset_path: ("image/png", b"\x00\x01"))
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: repo)

    result = _bare_bridge().read_gui_theme_asset("rose", "assets/a.png")

    assert result == {
        "ok": True,
        "mime": "image/png",
        "base64": base64.b64encode(b"\x00\x01").decode("ascii"),
    }


def test_broken_non_active_theme_list_is_non_blocking(monkeypatch):
    monkeypatch.setattr(
        gui_bridge,
        "get_gui_theme_repository",
        lambda: RepoWithBrokenList(complete_light_theme()),
    )

    state = gui_bridge._load_initial_gui_theme_state()

    assert state["gui_theme"]["id"] == "builtin.light"
    assert state["gui_themes"] == []
    assert state["gui_theme_error"] is None
    assert state["gui_theme_list_error"]["code"] == "invalid_manifest"


def test_initial_theme_repository_failure_is_structured(monkeypatch):
    def fail_repository():
        raise OSError("unavailable")

    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", fail_repository)

    state = gui_bridge._load_initial_gui_theme_state()

    assert state["gui_theme"] is None
    assert state["gui_themes"] == []
    assert state["gui_theme_error"]["code"] == "theme_io_failed"


def test_initial_gui_theme_io_failure_returns_startup_marker(monkeypatch):
    def fail_repository():
        raise OSError("unavailable")

    monkeypatch.setattr(gui_main, "get_gui_theme_repository", fail_repository)

    startup = gui_main._initial_gui_theme()

    assert startup["appearance"] == "dark"
    assert startup["error"]["code"] == "theme_io_failed"


def test_invalid_startup_theme_keeps_config_bytes(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text('{"gui_theme_id":"missing-theme","other":1}', encoding="utf-8")
    repo = ThemeRepository(tmp_path / "themes", config_file)
    before = config_file.read_bytes()
    monkeypatch.setattr(gui_main, "get_gui_theme_repository", lambda: repo)

    startup = gui_main._initial_gui_theme()

    assert startup["error"]["code"] == "theme_not_found"
    assert config_file.read_bytes() == before


def test_startup_fragment_marks_only_active_theme_error(monkeypatch):
    # fragment 是否带 os=win10 取决于系统版本，固定 Win11 保证跨 OS 可重复
    monkeypatch.setattr(
        gui_main.sys,
        "getwindowsversion",
        lambda: SimpleNamespace(build=22631),
        raising=False,
    )
    assert gui_main._build_gui_theme_fragment({
        "appearance": "light", "background": "#f4f4f1",
    }) == "theme=light&bg=f4f4f1"
    assert gui_main._build_gui_theme_fragment({
        "appearance": "dark",
        "background": "#1a1a18",
        "error": {"code": "theme_not_found", "path": "gui_theme_id", "message": "missing"},
    }) == "theme=dark&bg=1a1a18&gui_theme_error=1"


def test_startup_fragment_marks_win10_for_frontend_border(monkeypatch):
    """Win10 无 DWMWA_BORDER_COLOR 可隐藏外框，前端据 os=win10 补 1px CSS 边框。"""
    monkeypatch.setattr(
        gui_main.sys,
        "getwindowsversion",
        lambda: SimpleNamespace(build=19045),
        raising=False,
    )
    assert gui_main._build_gui_theme_fragment({
        "appearance": "light", "background": "#f4f4f1",
    }) == "theme=light&bg=f4f4f1&os=win10"


def test_startup_fragment_has_no_os_marker_on_win11(monkeypatch):
    monkeypatch.setattr(
        gui_main.sys,
        "getwindowsversion",
        lambda: SimpleNamespace(build=22631),
        raising=False,
    )
    assert gui_main._build_gui_theme_fragment({
        "appearance": "light", "background": "#f4f4f1",
    }) == "theme=light&bg=f4f4f1"


def test_set_active_persist_failure_restores_native_titlebar(monkeypatch):
    old_titlebar = copy.deepcopy(BUILTIN_THEMES["builtin.dark"]["native_titlebar"])
    new_theme = editable_light_theme()
    repo = FailingSetActiveRepo(new_theme)
    bridge = _bare_bridge()
    bridge._native_titlebar = old_titlebar
    bridge._titlebar_dark = True
    writes = []

    def write_dwm(native_titlebar):
        writes.append(copy.deepcopy(native_titlebar))
        return True

    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: repo)
    monkeypatch.setattr(bridge, "_write_native_titlebar_dwm", write_dwm, raising=False)

    result = bridge.set_active_gui_theme("rose")

    assert result["ok"] is False
    assert result["error"]["code"] == "theme_io_failed"
    assert repo.calls == ["get:rose", "set_active:rose"]
    assert writes == [new_theme["native_titlebar"], old_titlebar]
    assert bridge._native_titlebar == old_titlebar
    assert bridge._titlebar_dark is True


def test_set_active_rollback_write_failure_still_restores_memory(monkeypatch):
    old_titlebar = copy.deepcopy(BUILTIN_THEMES["builtin.dark"]["native_titlebar"])
    repo = FailingSetActiveRepo(editable_light_theme())
    bridge = _bare_bridge()
    bridge._native_titlebar = old_titlebar
    bridge._titlebar_dark = True
    writes = 0

    def write_dwm(_native_titlebar):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("DWM unavailable")
        return True

    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: repo)
    monkeypatch.setattr(bridge, "_write_native_titlebar_dwm", write_dwm)

    result = bridge.set_active_gui_theme("rose")

    assert result["ok"] is False
    assert result["error"]["code"] == "native_titlebar_rollback_failed"
    assert bridge._native_titlebar == old_titlebar
    assert bridge._titlebar_dark is True


def test_native_titlebar_failed_apply_restores_old_dwm_and_memory(monkeypatch):
    old_titlebar = copy.deepcopy(BUILTIN_THEMES["builtin.dark"]["native_titlebar"])
    new_titlebar = copy.deepcopy(BUILTIN_THEMES["builtin.light"]["native_titlebar"])
    bridge = _bare_bridge()
    bridge._native_titlebar = old_titlebar
    bridge._titlebar_dark = True
    writes = []

    def write_dwm(native_titlebar):
        writes.append(copy.deepcopy(native_titlebar))
        return native_titlebar == old_titlebar

    monkeypatch.setattr(bridge, "_write_native_titlebar_dwm", write_dwm, raising=False)

    assert bridge._apply_native_titlebar_theme(new_titlebar) is False
    assert writes == [new_titlebar, old_titlebar]
    assert bridge._native_titlebar == old_titlebar
    assert bridge._titlebar_dark is True


def test_native_titlebar_required_attribute_failure_is_reported(monkeypatch):
    bridge = _bare_bridge()
    bridge._hwnd = 123
    fake_dwm = Mock()

    def set_attribute(_hwnd, attr, _value, _size):
        return 1 if attr == 36 else 0

    fake_dwm.DwmSetWindowAttribute.side_effect = set_attribute
    monkeypatch.setattr(gui_bridge.sys, "platform", "win32")
    monkeypatch.setattr(gui_bridge, "_windows_build_number", lambda: 19045, raising=False)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(dwmapi=fake_dwm), raising=False)
    monkeypatch.setattr(gui_bridge, "ctypes", ctypes, raising=False)

    result = bridge._write_native_titlebar_dwm(complete_light_theme()["native_titlebar"])
    assert result["ok"] is False
    assert [call.args[1] for call in fake_dwm.DwmSetWindowAttribute.call_args_list] == [20, 35, 36]


def test_win10_unsupported_custom_colors_still_activates_theme(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch)
    bridge = _bare_bridge()
    bridge._hwnd = 123
    bridge._native_titlebar = copy.deepcopy(BUILTIN_THEMES["builtin.dark"]["native_titlebar"])
    bridge._native_titlebar_color_capability = "supported"
    fake_dwm = Mock()
    unsupported = ctypes.c_int32(0x80070057).value  # E_INVALIDARG on pre-Win11 color attrs

    def set_attribute(_hwnd, attr, _value, _size):
        return 0 if attr in (20, 19) else unsupported

    fake_dwm.DwmSetWindowAttribute.side_effect = set_attribute
    monkeypatch.setattr(gui_bridge.sys, "platform", "win32")
    monkeypatch.setattr(gui_bridge, "_windows_build_number", lambda: 19045, raising=False)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(dwmapi=fake_dwm), raising=False)
    monkeypatch.setattr(gui_bridge, "ctypes", ctypes, raising=False)

    result = bridge.set_active_gui_theme("builtin.light")

    assert result["ok"] is True
    assert result["native_titlebar_color_capability"] == "unsupported"
    assert bridge._native_titlebar_color_capability == "unsupported"
    assert model_manager.load_config()["gui_theme_id"] == "builtin.light"
    assert [call.args[1] for call in fake_dwm.DwmSetWindowAttribute.call_args_list] == [
        20, 35, 36, 34,
    ]


def test_known_unsupported_mixed_with_real_failure_is_not_downgraded(monkeypatch):
    bridge = _bare_bridge()
    bridge._hwnd = 123
    bridge._native_titlebar = copy.deepcopy(BUILTIN_THEMES["builtin.dark"]["native_titlebar"])
    bridge._native_titlebar_color_capability = "supported"
    fake_dwm = Mock()
    unsupported = ctypes.c_int32(0x80070057).value

    def set_attribute(_hwnd, attr, _value, _size):
        return {20: 0, 35: unsupported, 36: unsupported, 34: 5}[attr]

    fake_dwm.DwmSetWindowAttribute.side_effect = set_attribute
    monkeypatch.setattr(gui_bridge.sys, "platform", "win32")
    monkeypatch.setattr(gui_bridge, "_windows_build_number", lambda: 19045, raising=False)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(dwmapi=fake_dwm), raising=False)
    monkeypatch.setattr(gui_bridge, "ctypes", ctypes, raising=False)

    assert bridge._apply_native_titlebar_theme(
        complete_light_theme()["native_titlebar"],
    ) is False
    assert bridge._native_titlebar["mode"] == "dark"
    assert bridge._native_titlebar_color_capability == "supported"
    assert [call.args[1] for call in fake_dwm.DwmSetWindowAttribute.call_args_list] == [
        20, 35, 36, 34, 20, 35, 36, 34,
    ]


def test_win11_known_unsupported_hresult_is_treated_as_real_failure(monkeypatch):
    bridge = _bare_bridge()
    bridge._hwnd = 123
    bridge._native_titlebar = copy.deepcopy(BUILTIN_THEMES["builtin.dark"]["native_titlebar"])
    bridge._native_titlebar_color_capability = "supported"
    fake_dwm = Mock()
    unsupported = ctypes.c_int32(0x80070057).value

    def set_attribute(_hwnd, attr, _value, _size):
        return 0 if attr == 20 else unsupported

    fake_dwm.DwmSetWindowAttribute.side_effect = set_attribute
    monkeypatch.setattr(gui_bridge.sys, "platform", "win32")
    monkeypatch.setattr(gui_bridge, "_windows_build_number", lambda: 22631, raising=False)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(dwmapi=fake_dwm), raising=False)
    monkeypatch.setattr(gui_bridge, "ctypes", ctypes, raising=False)

    assert bridge._apply_native_titlebar_theme(
        complete_light_theme()["native_titlebar"],
    ) is False
    assert bridge._native_titlebar["mode"] == "dark"
    assert bridge._native_titlebar_color_capability == "supported"
    assert [call.args[1] for call in fake_dwm.DwmSetWindowAttribute.call_args_list] == [
        20, 35, 36, 34, 20, 35, 36, 34,
    ]


def test_native_titlebar_rejects_alpha_while_web_tokens_allow_it(tmp_path):
    repo = ThemeRepository(tmp_path / "themes", tmp_path / "config.json")
    valid = {
        key: value for key, value in editable_light_theme().items() if key != "builtin"
    }
    valid["tokens"]["accent_soft"] = "#11223344"
    assert repo.save_theme(valid)["tokens"]["accent_soft"] == "#11223344"

    invalid = copy.deepcopy(valid)
    invalid["native_titlebar"]["background"] = "#11223344"
    with pytest.raises(ThemeError) as exc:
        repo.save_theme(invalid)
    assert exc.value.code == "invalid_color"
    assert exc.value.path == "native_titlebar.background"


@pytest.mark.parametrize("bridge_method,repo_method,args", [
    ("list_gui_themes", "list_themes", ()),
    ("get_gui_theme", "get_theme", ("rose",)),
    ("save_gui_theme", "save_theme", (editable_light_theme(),)),
    ("copy_gui_theme", "copy_theme", ("builtin.light", "rose", "Rose")),
    ("rename_gui_theme", "rename_theme", ("rose", "rose-new", "Rose New")),
    ("delete_gui_theme", "delete_theme", ("rose",)),
    ("remove_gui_theme_image", "remove_image", ("rose", "workspace")),
    ("read_gui_theme_asset", "read_asset", ("rose", "assets/a.png")),
])
def test_gui_theme_bridge_unexpected_errors_are_structured(
    monkeypatch, bridge_method, repo_method, args,
):
    monkeypatch.setattr(
        gui_bridge,
        "get_gui_theme_repository",
        lambda: ExplodingRepo(repo_method, RuntimeError("boom")),
    )

    result = getattr(_bare_bridge(), bridge_method)(*args)

    assert result["ok"] is False
    assert result["error"]["code"] == "theme_operation_failed"
    assert result["error"]["message"]


def test_active_theme_rename_updates_authoritative_id(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")
    repo = ThemeRepository(tmp_path / "themes", config_file)
    repo.copy_theme("builtin.light", "rose", "Rose")
    repo.set_active("rose")

    repo.rename_theme("rose", "rose-new", "Rose New")

    assert json.loads(config_file.read_text(encoding="utf-8"))["gui_theme_id"] == "rose-new"
    assert repo.get_active()["id"] == "rose-new"


def test_rename_theme_with_unchanged_id_only_updates_name(tmp_path):
    """重命名对话框默认沿用当前主题 ID：只改名称时不能报“主题已存在”。"""
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")
    repo = ThemeRepository(tmp_path / "themes", config_file)
    repo.copy_theme("builtin.light", "rose", "Rose")
    repo.set_active("rose")
    asset = repo.root / "rose" / "assets" / "keep.png"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"asset")

    renamed = repo.rename_theme("rose", "rose", "Rose New")

    assert renamed["id"] == "rose"
    assert renamed["name"] == "Rose New"
    assert repo.get_theme("rose")["name"] == "Rose New"
    assert repo.get_active()["id"] == "rose"
    assert asset.read_bytes() == b"asset"
    assert json.loads(config_file.read_text(encoding="utf-8"))["gui_theme_id"] == "rose"
    assert sorted(item.name for item in repo.root.iterdir()) == ["rose"]


def test_rename_theme_to_another_existing_id_still_rejected(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")
    repo = ThemeRepository(tmp_path / "themes", config_file)
    repo.copy_theme("builtin.light", "rose", "Rose")
    repo.copy_theme("builtin.light", "sakura", "Sakura")

    with pytest.raises(ThemeError) as exc:
        repo.rename_theme("rose", "sakura", "Sakura")

    assert exc.value.code == "theme_exists"
    assert repo.get_theme("rose")["name"] == "Rose"
    assert repo.get_theme("sakura")["name"] == "Sakura"


def test_active_rename_partial_old_cleanup_keeps_complete_new_active(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")
    repo = ThemeRepository(tmp_path / "themes", config_file)
    repo.copy_theme("builtin.light", "rose", "Rose")
    repo.set_active("rose")
    original_rmtree = shutil.rmtree

    def partial_cleanup(path, *args, **kwargs):
        path = type(repo.root)(path)
        if path.name == "rose" or path.name.startswith(".gui-theme-retired-"):
            (path / "theme.json").unlink(missing_ok=True)
            raise OSError("partial cleanup")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", partial_cleanup)

    with pytest.raises(ThemeError) as exc:
        repo.rename_theme("rose", "rose-new", "Rose New")

    assert exc.value.code == "theme_cleanup_failed"
    assert json.loads(config_file.read_text(encoding="utf-8"))["gui_theme_id"] == "rose-new"
    assert repo.get_active()["id"] == "rose-new"
    assert (repo.root / "rose-new" / "theme.json").is_file()


def test_active_rename_cleanup_failure_never_attempts_unsafe_config_rollback(
    tmp_path, monkeypatch,
):
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")
    repo = ThemeRepository(tmp_path / "themes", config_file)
    repo.copy_theme("builtin.light", "rose", "Rose")
    repo.set_active("rose")
    original_write = config._atomic_write_json
    original_rmtree = shutil.rmtree
    config_writes = 0

    def reject_rollback(path, data):
        nonlocal config_writes
        config_writes += 1
        if config_writes > 1:
            raise OSError("rollback write failed")
        return original_write(path, data)

    def fail_retired_cleanup(path, *args, **kwargs):
        path = type(repo.root)(path)
        if path.name == "rose" or path.name.startswith(".gui-theme-retired-"):
            raise OSError("cleanup failed")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(config, "_atomic_write_json", reject_rollback)
    monkeypatch.setattr(shutil, "rmtree", fail_retired_cleanup)

    with pytest.raises(ThemeError) as exc:
        repo.rename_theme("rose", "rose-new", "Rose New")

    assert exc.value.code == "theme_cleanup_failed"
    assert config_writes == 1
    assert repo.get_active()["id"] == "rose-new"
    assert (repo.root / "rose-new" / "theme.json").is_file()


def test_gui_theme_crud_success_envelopes(tmp_path, monkeypatch):
    repo = ThemeRepository(tmp_path / "themes", tmp_path / "config.json")
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: repo)
    bridge = _bare_bridge()

    copied = bridge.copy_gui_theme("builtin.light", "rose", "Rose")
    fetched = bridge.get_gui_theme("rose")
    renamed = bridge.rename_gui_theme("rose", "rose-new", "Rose New")
    listed = bridge.list_gui_themes()
    deleted = bridge.delete_gui_theme("rose-new")

    assert copied["ok"] is True and copied["theme"]["id"] == "rose"
    assert fetched["ok"] is True and fetched["theme"]["builtin"] is False
    assert renamed["ok"] is True and renamed["theme"]["id"] == "rose-new"
    assert any(item["id"] == "rose-new" for item in listed["themes"])
    assert deleted == {"ok": True}


def test_set_gui_theme_persists_light(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch)
    bridge = _bare_bridge()
    res = bridge.set_gui_theme(True)
    assert res["ok"] is True
    assert model_manager.load_config().get("gui_theme_light") is True
    assert model_manager.load_config().get("gui_theme_id") == "builtin.light"
    # 标题栏同步为浅色（dark=False）
    assert bridge._titlebar_dark is False


def test_set_gui_theme_persists_dark(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch)
    bridge = _bare_bridge()
    bridge.set_gui_theme(True)
    bridge.set_gui_theme(False)
    assert model_manager.load_config().get("gui_theme_light") is False
    assert model_manager.load_config().get("gui_theme_id") == "builtin.dark"
    assert bridge._titlebar_dark is True


def test_theme_light_defaults_to_false_in_config(tmp_path, monkeypatch):
    """老配置没有 gui_theme_light 键时按深色处理。"""
    _use_tmp_config(tmp_path, monkeypatch)
    assert bool(model_manager.load_config().get("gui_theme_light", False)) is False
