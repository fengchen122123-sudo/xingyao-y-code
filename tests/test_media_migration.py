import json
import os

import pytest

from core.media_migration import run_media_migration_once
from core.memory import MemoryManager

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture(autouse=True)
def _isolate_migration_gate(monkeypatch):
    import core.media_migration as mm

    store: dict = {}
    monkeypatch.setattr(mm, "load_config", lambda: dict(store))
    monkeypatch.setattr(
        mm, "save_config_value", lambda k, v: store.__setitem__(k, v)
    )
    yield store


@pytest.fixture()
def manager(tmp_path):
    return MemoryManager(str(tmp_path / "media-migration.db"))


@pytest.fixture()
def png_file(tmp_path):
    path = os.path.join(str(tmp_path), "pic.png")
    with open(path, "wb") as f:
        f.write(PNG_BYTES)
    return path


def _add_assistant(manager, session, workdir, content, display=None):
    manager.ensure_session(session, workdir=workdir)
    msg = {"role": "assistant", "content": content}
    if display is not None:
        msg["_display"] = display
    manager.add_messages(session, [("assistant", json.dumps(msg, ensure_ascii=False))])


def _get_content(manager, session):
    return [m for m in manager.get_history(session) if m.get("role") == "assistant"]


def test_migration_adds_manifest_and_keeps_body(manager, png_file, tmp_path):
    body = f"![图]({png_file})"
    _add_assistant(manager, "s1", str(tmp_path), body)

    stats = run_media_migration_once(manager)
    assert stats["updated"] == 1
    msgs = _get_content(manager, "s1")
    items = msgs[0]["_display"]["media_v1"]["items"]
    assert items[0]["kind"] == "image"
    assert msgs[0]["content"] == body


def test_migration_idempotent_and_gate(manager, png_file, tmp_path, _isolate_migration_gate):
    body = f"![图]({png_file})"
    _add_assistant(manager, "s1", str(tmp_path), body)

    first = run_media_migration_once(manager)
    assert first["updated"] == 1
    _isolate_migration_gate.clear()
    second = run_media_migration_once(manager)
    assert second["updated"] == 0
    third = run_media_migration_once(manager)
    assert third.get("skipped") is True


def test_migration_skips_plain_text_and_tool_messages(manager, tmp_path, _isolate_migration_gate):
    _add_assistant(manager, "s1", str(tmp_path), "普通回复，没有媒体")
    plain_stats = run_media_migration_once(manager)
    assert plain_stats["updated"] == 0
    manager.add_messages("s1", [("assistant", "纯文本旧消息 ![图](x.png)")])
    _isolate_migration_gate.clear()
    stats = run_media_migration_once(manager)
    assert stats["updated"] == 0


def test_migration_uses_session_workdir_for_relative_paths(manager, tmp_path):
    subdir = tmp_path / "assets"
    subdir.mkdir()
    png = subdir / "rel.png"
    png.write_bytes(PNG_BYTES)

    _add_assistant(manager, "s1", str(tmp_path), "![图](assets/rel.png)")
    stats = run_media_migration_once(manager)
    assert stats["updated"] == 1
    items = _get_content(manager, "s1")[0]["_display"]["media_v1"]["items"]
    assert items[0]["kind"] == "image"


def test_migration_not_found_becomes_error_item(manager, tmp_path):
    _add_assistant(manager, "s1", str(tmp_path), "![图](missing.png)")
    run_media_migration_once(manager)
    items = _get_content(manager, "s1")[0]["_display"]["media_v1"]["items"]
    assert items[0]["kind"] == "error"
    assert items[0]["error_code"] == "not_found"
