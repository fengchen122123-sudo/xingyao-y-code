import json

import pytest

from core import ui_layout


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "YCODE_DIR", tmp_path, raising=False)
    return tmp_path / "ui_layout.json"


def test_missing_file_falls_back_to_empty_snapshot(isolated_store):
    snap = ui_layout.get_ui_layout()

    assert snap["group_order"] == []
    assert snap["session_order"] == {}
    assert not isolated_store.exists()


def test_save_then_load_roundtrip(isolated_store):
    saved = ui_layout.save_ui_layout(
        {
            "group_order": ["E:\\proj\\a", "E:\\proj\\b"],
            "session_order": {"E:\\proj\\a": ["s1", "s2"]},
        }
    )
    assert saved["group_order"] == ["E:\\proj\\a", "E:\\proj\\b"]

    loaded = ui_layout.get_ui_layout()
    assert loaded["group_order"] == ["E:\\proj\\a", "E:\\proj\\b"]
    assert loaded["session_order"] == {"E:\\proj\\a": ["s1", "s2"]}

    on_disk = json.loads(isolated_store.read_text(encoding="utf-8"))
    assert on_disk["group_order"] == ["E:\\proj\\a", "E:\\proj\\b"]


def test_save_is_full_overwrite(isolated_store):
    ui_layout.save_ui_layout({"group_order": ["a", "b"], "session_order": {"a": ["s1"]}})
    ui_layout.save_ui_layout({"group_order": ["b"], "session_order": {}})

    loaded = ui_layout.get_ui_layout()
    assert loaded["group_order"] == ["b"]
    assert loaded["session_order"] == {}


def test_dirty_values_are_dropped(isolated_store):
    saved = ui_layout.save_ui_layout(
        {
            "group_order": ["a", "", None, "a", "   ", "b"],
            "session_order": {"a": ["s1", 42, "", "s1"], "": ["x"], "b": "not-a-list"},
        }
    )

    assert saved["group_order"] == ["a", "b"]
    assert saved["session_order"] == {"a": ["s1"]}


def test_group_order_is_trimmed_at_limit(isolated_store):
    many = [f"g{i}" for i in range(ui_layout.MAX_GROUPS + 20)]

    saved = ui_layout.save_ui_layout({"group_order": many})

    assert len(saved["group_order"]) == ui_layout.MAX_GROUPS


def test_corrupted_file_falls_back_to_empty(isolated_store):
    isolated_store.write_text("{不是合法 JSON", encoding="utf-8")

    snap = ui_layout.get_ui_layout()

    assert snap["group_order"] == []
    assert snap["session_order"] == {}


def test_wrong_types_in_file_fall_back_to_empty(isolated_store):
    isolated_store.write_text(
        json.dumps({"group_order": "oops", "session_order": [1, 2]}), encoding="utf-8"
    )

    snap = ui_layout.get_ui_layout()

    assert snap["group_order"] == []
    assert snap["session_order"] == {}


def test_non_dict_payload_raises(isolated_store):
    with pytest.raises(ui_layout.UiLayoutError):
        ui_layout.save_ui_layout(["not", "a", "dict"])


def test_save_survives_config_failure(monkeypatch, isolated_store):

    def boom(*args, **kwargs):
        raise OSError("disk on fire")

    import config

    monkeypatch.setattr(config, "_atomic_write_json", boom, raising=False)

    with pytest.raises(ui_layout.UiLayoutError):
        ui_layout.save_ui_layout({"group_order": ["a"]})
