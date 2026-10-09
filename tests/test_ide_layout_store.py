# -*- coding: utf-8 -*-
"""IDE 布局状态后端持久化：按会话隔离、脏数据清洗、原子写。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.ide.layout_store as store


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    # 计划原文用 monkeypatch.setenv("YCODE_DATA_DIR", ...) 重定向，但 config 在
    # conftest 阶段已被导入，YCODE_DIR 是导入期常量——事后改环境变量不会生效
    # （探针：改 env 后 _store_path() 仍指向真实 YCODE_DIR），且有跨用例串写风险。
    # 等价修正为直接打桩 config.YCODE_DIR，与 tests/test_ui_layout.py 的既有写法一致。
    import config

    monkeypatch.setattr(config, "YCODE_DIR", tmp_path, raising=False)
    return tmp_path


def test_default_layout_when_file_absent(data_dir):
    layout = store.get_ide_layout("s1")
    assert layout == {"version": 1, "tabs": [], "active": "", "breakpoints": []}


def test_save_then_get_roundtrip(data_dir):
    saved = store.save_ide_layout("s1", {
        "tabs": [{"path": "E:\\p\\a.py", "line": 12, "dirty": False}],
        "active": "E:\\p\\a.py",
        "breakpoints": [{"path": "E:\\p\\a.py", "line": 3}],
    })
    assert saved["tabs"][0]["path"] == "E:\\p\\a.py"
    again = store.get_ide_layout("s1")
    assert again["active"] == "E:\\p\\a.py"
    assert again["breakpoints"] == [{"path": "E:\\p\\a.py", "line": 3}]


def test_sessions_are_isolated(data_dir):
    store.save_ide_layout("s1", {"tabs": [{"path": "a.py", "line": 1}], "active": "a.py", "breakpoints": []})
    assert store.get_ide_layout("s2")["tabs"] == []


def test_dirty_payload_is_sanitized(data_dir):
    saved = store.save_ide_layout("s1", {
        "tabs": [
            {"path": "ok.py", "line": 1},
            {"path": "", "line": 1},          # 空路径丢弃
            {"path": "bad.py", "line": "x"},  # 行号非整数：整条保留，行号归一为 1
            "not-a-dict",                      # 结构错误丢弃
        ],
        "active": 123,
        "breakpoints": [{"path": "b.py", "line": -1}],  # 行号必须为正，非法丢弃
    })
    assert [t["path"] for t in saved["tabs"]] == ["ok.py", "bad.py"]
    assert saved["tabs"][1]["line"] == 1
    assert saved["active"] == "ok.py"  # 非字符串 active 被约束回 tabs 内首个路径
    assert saved["breakpoints"] == []


def test_tabs_are_capped(data_dir):
    tabs = [{"path": f"f{i}.py", "line": 1} for i in range(store.MAX_TABS + 20)]
    saved = store.save_ide_layout("s1", {"tabs": tabs, "active": "", "breakpoints": []})
    assert len(saved["tabs"]) == store.MAX_TABS


def test_corrupt_file_falls_back_to_default(data_dir):
    (data_dir / "ide_layout.json").write_text("{ not json", encoding="utf-8")
    assert store.get_ide_layout("s1")["tabs"] == []


def test_saved_file_is_valid_json(data_dir):
    store.save_ide_layout("s1", {"tabs": [{"path": "a.py", "line": 1}], "active": "a.py", "breakpoints": []})
    raw = (data_dir / "ide_layout.json").read_text(encoding="utf-8")
    assert json.loads(raw)["sessions"]["s1"]["active"] == "a.py"


# ---------------------------------------------------------------------------
# 保存拒绝「存在但读不出」的布局文件（来自 Task 6 评审的 Important 发现）
#
# save_ide_layout 是全量覆盖语义：先读全量快照、改当前会话、再整体回写。若读取
# 失败被回落成空快照，回写内容只含当前会话，磁盘上其它会话的布局会被**静默抹掉**。
# 因此必须区分「文件不存在」（合法，空快照）与「存在但读不出来」（异常，拒绝写入）。
# ---------------------------------------------------------------------------
def test_save_refuses_when_store_unreadable_and_keeps_disk(data_dir):
    path = data_dir / "ide_layout.json"
    original = b"{ not json at all"
    path.write_bytes(original)

    with pytest.raises(store.IdeLayoutError):
        store.save_ide_layout("s1", {
            "tabs": [{"path": "a.py", "line": 1}], "active": "a.py", "breakpoints": [],
        })

    # 拒绝写入：磁盘内容一字不变
    assert path.read_bytes() == original


def test_save_works_when_file_absent(data_dir):
    """文件不存在是合法状态（等价空快照），保存必须正常写入。"""
    path = data_dir / "ide_layout.json"
    assert not path.exists()

    saved = store.save_ide_layout("s1", {
        "tabs": [{"path": "a.py", "line": 1}], "active": "a.py", "breakpoints": [],
    })

    assert saved["active"] == "a.py"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["sessions"]["s1"]["active"] == "a.py"
