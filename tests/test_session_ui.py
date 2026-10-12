# -*- coding: utf-8 -*-
"""会话级 UI 状态后端持久化：按会话隔离、脏数据清洗、原子写、拒写保护。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.session_ui as store


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    # 计划原文用 monkeypatch.setenv("YCODE_DATA_DIR", ...) 重定向，但 config 在
    # conftest 阶段已被导入，YCODE_DIR 是导入期常量——事后改环境变量不会生效，
    # 会写进用户真实的 ~/.ycode 并跨用例污染。等价修正为直接打桩 config.YCODE_DIR，
    # 与 tests/test_ui_layout.py / tests/test_ide_layout_store.py 的既有写法一致。
    import config

    monkeypatch.setattr(config, "YCODE_DIR", tmp_path, raising=False)
    return tmp_path


def test_default_when_file_absent(data_dir):
    ui = store.get_session_ui("s1")
    assert ui == {"version": 1, "ide_mode": False}
    # 只读不写：没保存过就不该凭空造出文件
    assert not (data_dir / "session_ui.json").exists()


def test_save_then_get_roundtrip(data_dir):
    saved = store.save_session_ui("s1", {"ide_mode": True})
    assert saved == {"version": 1, "ide_mode": True}

    again = store.get_session_ui("s1")
    assert again["ide_mode"] is True

    on_disk = json.loads((data_dir / "session_ui.json").read_text(encoding="utf-8"))
    assert on_disk["version"] == 1
    assert on_disk["sessions"]["s1"]["ide_mode"] is True


def test_sessions_are_isolated(data_dir):
    store.save_session_ui("s1", {"ide_mode": True})

    assert store.get_session_ui("s2")["ide_mode"] is False
    # 保存 s2 不得改动 s1
    store.save_session_ui("s2", {"ide_mode": False})
    assert store.get_session_ui("s1")["ide_mode"] is True


def test_unknown_fields_are_dropped(data_dir):
    saved = store.save_session_ui("s1", {"ide_mode": True, "theme": "dark", "__proto__": {}})
    assert saved == {"version": 1, "ide_mode": True}

    on_disk = json.loads((data_dir / "session_ui.json").read_text(encoding="utf-8"))
    assert set(on_disk["sessions"]["s1"].keys()) == {"ide_mode"}


@pytest.mark.parametrize("bad", ["true", "false", 1, 0, None, [], {}, "yes"])
def test_non_bool_ide_mode_falls_back_to_false(data_dir, bad):
    """非法类型回落 False：绝不做弱转换把 "false"/1 当成 True 静默开 IDE。"""
    saved = store.save_session_ui("s1", {"ide_mode": bad})
    assert saved["ide_mode"] is False
    assert store.get_session_ui("s1")["ide_mode"] is False


def test_non_dict_payload_falls_back_to_default(data_dir):
    saved = store.save_session_ui("s1", ["not", "a", "dict"])
    assert saved == {"version": 1, "ide_mode": False}


def test_empty_session_rejected(data_dir):
    with pytest.raises(store.SessionUiError):
        store.save_session_ui("", {"ide_mode": True})


def test_corrupt_file_falls_back_to_default_on_read(data_dir):
    (data_dir / "session_ui.json").write_text("{ not json", encoding="utf-8")
    assert store.get_session_ui("s1")["ide_mode"] is False


# ---------------------------------------------------------------------------
# 保存拒绝「存在但读不出」的状态文件
#
# save_session_ui 是全量覆盖语义：先读全量快照、改当前会话、再整体回写。若读取
# 失败被回落成空快照，回写内容只含当前会话，磁盘上其它会话的状态会被**静默抹掉**。
# 因此必须区分「文件不存在」（合法，空快照）与「存在但读不出来」（异常，拒绝写入）。
# ---------------------------------------------------------------------------
def test_save_refuses_when_store_unreadable_and_keeps_disk(data_dir):
    path = data_dir / "session_ui.json"
    original = b"{ not json at all"
    path.write_bytes(original)

    with pytest.raises(store.SessionUiError):
        store.save_session_ui("s1", {"ide_mode": True})

    # 拒绝写入：磁盘内容一字不变（其它会话因此得以保留）
    assert path.read_bytes() == original


def test_save_refuses_when_existing_snapshot_has_bad_shape(data_dir):
    """文件是合法 JSON 但结构非法（sessions 不是对象）：同样按损坏处理，拒写。"""
    path = data_dir / "session_ui.json"
    original = json.dumps({"version": 1, "sessions": ["oops"]}).encode("utf-8")
    path.write_bytes(original)

    with pytest.raises(store.SessionUiError):
        store.save_session_ui("s1", {"ide_mode": True})

    assert path.read_bytes() == original


def test_save_works_when_file_absent(data_dir):
    """文件不存在是合法状态（等价空快照），保存必须正常写入。"""
    path = data_dir / "session_ui.json"
    assert not path.exists()

    saved = store.save_session_ui("s1", {"ide_mode": True})

    assert saved["ide_mode"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["sessions"]["s1"]["ide_mode"] is True


# ---------------------------------------------------------------------------
# 会话数上限
# ---------------------------------------------------------------------------
def test_new_session_rejected_at_limit(data_dir):
    for i in range(store.MAX_SESSIONS):
        store.save_session_ui(f"s{i}", {"ide_mode": True})

    with pytest.raises(store.SessionUiError):
        store.save_session_ui("overflow", {"ide_mode": True})

    # 既有条目必须原样保留，且新会话没有混进去
    assert store.get_session_ui("s0")["ide_mode"] is True
    assert store.get_session_ui("overflow")["ide_mode"] is False
    on_disk = json.loads((data_dir / "session_ui.json").read_text(encoding="utf-8"))
    assert len(on_disk["sessions"]) == store.MAX_SESSIONS


def test_existing_session_still_updatable_at_limit(data_dir):
    """超限时已存在会话仍可更新——否则用户连当前会话都改不了。"""
    for i in range(store.MAX_SESSIONS):
        store.save_session_ui(f"s{i}", {"ide_mode": False})

    saved = store.save_session_ui("s0", {"ide_mode": True})

    assert saved["ide_mode"] is True
    assert store.get_session_ui("s0")["ide_mode"] is True


def test_disk_overflow_still_rejects_new_session(data_dir):
    """磁盘上已被外部写入超量条目时，新增同样被拒（上限判定看的是快照总条数）。"""
    sessions = {f"s{i}": {"ide_mode": True} for i in range(store.MAX_SESSIONS + 5)}
    path = data_dir / "session_ui.json"
    path.write_text(json.dumps({"version": 1, "sessions": sessions}), encoding="utf-8")

    with pytest.raises(store.SessionUiError):
        store.save_session_ui("another", {"ide_mode": True})

    # 既有条目一条不少
    assert store.get_session_ui("s0")["ide_mode"] is True
    assert len(json.loads(path.read_text(encoding="utf-8"))["sessions"]) == store.MAX_SESSIONS + 5
