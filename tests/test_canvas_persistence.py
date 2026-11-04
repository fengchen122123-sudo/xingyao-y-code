"""画布项目持久化四方法（M2-T02）的入口测试。

覆盖五段契约：
1) project_id 白名单：``../x``、``a/b``、空串、65 字符超长等一律拒绝，
   且拒绝路径上绝不创建任何目录 / 文件（路径穿越防线）；
2) 原子写：mkstemp + fsync + os.replace；注入 os.replace 失败后原文件内容不变、
   目录里不留 ``.tmp`` 残留；
3) schema_version 2 口径：v1 拒存 / 拒载（msg="旧版数据，不支持"），
   而列表只把它标 ``supported: false``，不报错；
4) 结构兜底：nodes / connections 非 list 时加载置空数组；坏 JSON、坏文件名
   都不让列表整体失败；
5) delete 幂等（文件不存在也 ok）与目录按需自动创建。

宿主构造照抄已删除的 tests/test_canvas_inline_edit.py：假桥只补齐画布混入依赖的
一件事——宿主工作目录（``_canvas_workdir``），其余全部走真实实现。
"""

import copy
import json
from pathlib import Path

import pytest

import gui_canvas_mixin
from gui_canvas_mixin import CANVAS_SCHEMA_VERSION, CanvasBridgeMixin

# 白名单应当拒绝的一组 ID：路径穿越、分隔符、空值、超长、非法字符
_BAD_IDS = [
    "../x",
    "a/b",
    "",
    "   ",
    None,
    "x" * 65,
    "a.b",
    "a b",
    "画布",
    "..",
    "a\\b",
]


class _FakeBridge(CanvasBridgeMixin):
    """最小假桥：只补齐画布混入依赖的宿主工作目录。

    画布所有落盘位置都从 ``_canvas_workdir`` 推导，指到 tmp_path 就与真实项目
    完全隔离（与旧 test_canvas_inline_edit 的宿主构造同口径）。
    """

    def __init__(self, workdir):
        self._workdir = str(workdir)

    def _canvas_workdir(self):
        return self._workdir


class _HostWithHookRoot(CanvasBridgeMixin):
    """不覆写 ``_canvas_workdir``：验证混入真的按宿主既有属性取工作目录。"""

    def __init__(self, root, session_root=None):
        self.root = root
        self.session_root = session_root

    def _hook_workspace_root(self):
        return self.root

    def _session_workdir(self, session_name):
        return self.session_root

    def _current_session(self):
        return "session_x"


class _BareHost(CanvasBridgeMixin):
    """宿主什么解析器都没提供：工作目录应回落进程 cwd。"""


def _project(pid="p1", *, title="画布", schema_version=CANVAS_SCHEMA_VERSION,
             nodes=None, connections=None, **extra):
    """构造一份完整 CanvasProject dict（字段口径对齐计划 M2-T01）。"""
    data = {
        "id": pid,
        "title": title,
        "createdAt": 1_700_000_000_000,
        "updatedAt": 1_700_000_000_000,
        "nodes": [] if nodes is None else nodes,
        "connections": [] if connections is None else connections,
        "viewport": {"x": 0, "y": 0, "zoom": 1},
        "backgroundMode": "grid",
        "sidePanel": {"open": False, "width": 320, "nodeId": None},
        "schema_version": schema_version,
    }
    data.update(extra)
    return data


@pytest.fixture()
def bridge(tmp_path):
    return _FakeBridge(tmp_path)


# ----------------------------------------------------------------------
# 宿主工作目录取法
# ----------------------------------------------------------------------
def test_canvas_dir_follows_host_workspace_root(tmp_path):
    host = _HostWithHookRoot(str(tmp_path))
    assert host._canvas_dir() == Path(str(tmp_path)) / ".ycode" / "canvas"
    assert not host._canvas_dir().exists(), "推导路径本身不应创建目录"


def test_canvas_dir_skips_global_root(tmp_path):
    # 自由对话会话的 workdir 是 "GLOBAL"，不能把画布写到进程 cwd 之外的全局位置
    host = _HostWithHookRoot("GLOBAL", session_root=str(tmp_path / "proj"))
    assert host._canvas_dir() == Path(str(tmp_path / "proj")) / ".ycode" / "canvas"


def test_canvas_dir_falls_back_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expected = (Path.cwd() / ".ycode" / "canvas").resolve()
    assert Path(_BareHost()._canvas_dir()).resolve() == expected


# ----------------------------------------------------------------------
# project_id 白名单（路径穿越防线）
# ----------------------------------------------------------------------
@pytest.mark.parametrize("bad", _BAD_IDS)
def test_save_rejects_illegal_project_id(bridge, bad):
    result = bridge.canvas_save_project(_project(bad))
    assert result["ok"] is False
    assert result["msg"] == "非法画布 ID"
    # 拒绝路径上不得留下任何痕迹（尤其不能在工作目录外建目录）
    assert not bridge._canvas_dir().exists()
    assert not (bridge._canvas_dir().parent).exists()


@pytest.mark.parametrize("bad", _BAD_IDS)
def test_load_and_delete_reject_illegal_project_id(bridge, bad):
    load = bridge.canvas_load_project(bad)
    assert load["ok"] is False and load["msg"] == "非法画布 ID"
    delete = bridge.canvas_delete_project(bad)
    assert delete["ok"] is False and delete["msg"] == "非法画布 ID"
    assert not bridge._canvas_dir().exists()


def test_canvas_path_never_escapes_canvas_dir(bridge):
    ok_path = bridge._canvas_path("fine-id_1")
    assert ok_path is not None
    ok_path.resolve().relative_to(bridge._canvas_dir().resolve())
    for bad in ("../x", "a/b", "..", "x" * 65):
        assert bridge._canvas_path(bad) is None


def test_valid_id_boundaries_accepted(bridge):
    # 1 位与 64 位都在白名单内
    for pid in ("a", "y" * 64):
        assert bridge.canvas_save_project(_project(pid))["ok"] is True
        assert (bridge._canvas_dir() / f"{pid}.json").is_file()


# ----------------------------------------------------------------------
# 保存 / 读取往返
# ----------------------------------------------------------------------
def test_save_creates_dir_and_roundtrips(bridge, tmp_path):
    nodes = [
        {
            "id": "n1",
            "type": "text",
            "title": "标题",
            "position": {"x": 1, "y": 2},
            "width": 100,
            "height": 80,
        }
    ]
    connections = [{"id": "c1", "fromNodeId": "n1", "toNodeId": "n1"}]
    assert not bridge._canvas_dir().exists(), "前置：画布目录还不存在"

    result = bridge.canvas_save_project(_project("proj-1", title="第一张",
                                                nodes=nodes, connections=connections))

    assert result["ok"] is True
    updated_at = result["updatedAt"]
    assert isinstance(updated_at, int) and updated_at > 0
    path = tmp_path / ".ycode" / "canvas" / "proj-1.json"
    assert path.is_file(), "目录应按需自动创建"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["id"] == "proj-1"
    assert on_disk["schema_version"] == CANVAS_SCHEMA_VERSION
    assert on_disk["updatedAt"] == updated_at, "updatedAt 以桥侧写回值为准"
    assert on_disk["nodes"][0]["id"] == "n1"

    loaded = bridge.canvas_load_project("proj-1")
    assert loaded["ok"] is True
    assert loaded["project"]["title"] == "第一张"
    assert loaded["project"]["nodes"] == nodes
    assert loaded["project"]["connections"] == connections
    assert loaded["project"]["viewport"] == {"x": 0, "y": 0, "zoom": 1}


def test_save_overwrites_existing_project(bridge):
    assert bridge.canvas_save_project(_project("p1", title="旧标题"))["ok"] is True
    assert bridge.canvas_save_project(_project("p1", title="新标题"))["ok"] is True
    loaded = bridge.canvas_load_project("p1")
    assert loaded["project"]["title"] == "新标题"


def test_save_does_not_mutate_caller_object(bridge):
    project = _project("p1")
    snapshot = copy.deepcopy(project)
    bridge.canvas_save_project(project)
    assert project == snapshot


def test_save_rejects_non_dict(bridge):
    result = bridge.canvas_save_project(["not", "a", "dict"])
    assert result["ok"] is False
    assert result["msg"] == "画布数据必须是对象"
    assert not bridge._canvas_dir().exists()


# ----------------------------------------------------------------------
# 原子写失败注入
# ----------------------------------------------------------------------
def test_atomic_save_failure_keeps_original_and_no_tmp(bridge, monkeypatch):
    canvas_dir = bridge._canvas_dir()
    assert bridge.canvas_save_project(_project("p1", title="原稿"))["ok"] is True
    original_text = (canvas_dir / "p1.json").read_text(encoding="utf-8")

    def _boom(*args, **kwargs):
        raise OSError("磁盘已满（测试注入）")

    monkeypatch.setattr(gui_canvas_mixin.os, "replace", _boom)

    result = bridge.canvas_save_project(_project("p1", title="坏稿"))

    assert result["ok"] is False
    assert "保存画布失败" in result["msg"]
    # 1) 原文件内容一字未变，仍是可解析的完整 JSON
    assert (canvas_dir / "p1.json").read_text(encoding="utf-8") == original_text
    assert json.loads(original_text)["title"] == "原稿"
    # 2) 失败路径清掉了临时文件：目录里只剩那一个正式文件
    assert [p.name for p in canvas_dir.iterdir()] == ["p1.json"]


# ----------------------------------------------------------------------
# schema_version 口径
# ----------------------------------------------------------------------
@pytest.mark.parametrize("version", [1, 0, 3, None, "abc", ""])
def test_save_rejects_unsupported_schema(bridge, version):
    project = _project("p1")
    if version is None:
        project.pop("schema_version")
    else:
        project["schema_version"] = version
    result = bridge.canvas_save_project(project)
    assert result["ok"] is False
    assert result["msg"] == "旧版数据，不支持"
    assert not (bridge._canvas_dir() / "p1.json").exists()


def test_save_rejects_unsupported_schema_keeps_existing_file(bridge):
    assert bridge.canvas_save_project(_project("p1", title="原稿"))["ok"] is True
    before = (bridge._canvas_dir() / "p1.json").read_text(encoding="utf-8")
    result = bridge.canvas_save_project(_project("p1", title="坏稿", schema_version=1))
    assert result["ok"] is False and result["msg"] == "旧版数据，不支持"
    assert (bridge._canvas_dir() / "p1.json").read_text(encoding="utf-8") == before


@pytest.mark.parametrize("field", ["nodes", "connections"])
def test_save_rejects_non_list_containers(bridge, field):
    project = _project("p1", **{field: "oops"})
    result = bridge.canvas_save_project(project)
    assert result["ok"] is False
    assert field in result["msg"]
    assert not (bridge._canvas_dir() / "p1.json").exists()


def test_load_rejects_legacy_schema(bridge):
    canvas_dir = bridge._canvas_dir()
    canvas_dir.mkdir(parents=True)
    legacy = _project("old", schema_version=1)
    (canvas_dir / "old.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )
    result = bridge.canvas_load_project("old")
    assert result["ok"] is False
    assert result["msg"] == "旧版数据，不支持"


def test_load_missing_project(bridge):
    result = bridge.canvas_load_project("nope")
    assert result["ok"] is False
    assert result["msg"] == "画布不存在"


def test_load_corrupt_json(bridge):
    canvas_dir = bridge._canvas_dir()
    canvas_dir.mkdir(parents=True)
    (canvas_dir / "broken.json").write_text("{ not json", encoding="utf-8")
    result = bridge.canvas_load_project("broken")
    assert result["ok"] is False
    assert result["msg"] == "画布文件格式非法"


def test_load_non_list_containers_fall_back_to_empty(bridge):
    canvas_dir = bridge._canvas_dir()
    canvas_dir.mkdir(parents=True)
    doc = _project("p1")
    doc["nodes"] = "oops"
    doc["connections"] = {"a": 1}
    (canvas_dir / "p1.json").write_text(json.dumps(doc, ensure_ascii=False),
                                        encoding="utf-8")
    result = bridge.canvas_load_project("p1")
    assert result["ok"] is True
    assert result["project"]["nodes"] == []
    assert result["project"]["connections"] == []


# ----------------------------------------------------------------------
# 列表
# ----------------------------------------------------------------------
def test_list_marks_legacy_file_unsupported(bridge):
    canvas_dir = bridge._canvas_dir()
    canvas_dir.mkdir(parents=True)
    legacy = _project("old", title="旧画布", schema_version=1,
                      updatedAt=1_000)
    (canvas_dir / "old.json").write_text(json.dumps(legacy, ensure_ascii=False),
                                         encoding="utf-8")
    assert bridge.canvas_save_project(_project("new", title="新画布"))["ok"] is True

    result = bridge.canvas_list_projects()

    assert result["ok"] is True, "v1 文件不该让整个列表报错"
    by_id = {item["id"]: item for item in result["projects"]}
    assert set(by_id) == {"old", "new"}
    assert by_id["old"]["supported"] is False
    assert by_id["old"]["schema_version"] == 1
    assert by_id["old"]["title"] == "旧画布"
    assert by_id["old"]["updatedAt"] == 1_000
    assert by_id["new"]["supported"] is True
    assert by_id["new"]["schema_version"] == CANVAS_SCHEMA_VERSION
    assert by_id["new"]["title"] == "新画布"
    # 最近更新的排前面
    assert [item["id"] for item in result["projects"]] == ["new", "old"]


def test_list_missing_dir_returns_empty(bridge):
    result = bridge.canvas_list_projects()
    assert result == {"ok": True, "projects": []}


def test_list_skips_illegal_filenames_and_broken_files(bridge):
    canvas_dir = bridge._canvas_dir()
    canvas_dir.mkdir(parents=True)
    (canvas_dir / "ok.json").write_text(
        json.dumps(_project("ok", title="好的"), ensure_ascii=False), encoding="utf-8"
    )
    (canvas_dir / "bad name.json").write_text("{}", encoding="utf-8")
    (canvas_dir / "broken.json").write_text("{ not json", encoding="utf-8")

    result = bridge.canvas_list_projects()

    assert result["ok"] is True
    by_id = {item["id"]: item for item in result["projects"]}
    assert set(by_id) == {"ok", "broken"}, "非法文件名直接跳过，坏文件降级而不是报错"
    assert by_id["broken"]["supported"] is False
    assert by_id["broken"]["title"] == "broken"
    assert isinstance(by_id["broken"]["updatedAt"], int)


# ----------------------------------------------------------------------
# 删除
# ----------------------------------------------------------------------
def test_delete_is_idempotent(bridge):
    canvas_dir = bridge._canvas_dir()
    assert bridge.canvas_save_project(_project("p1"))["ok"] is True
    assert (canvas_dir / "p1.json").is_file()

    assert bridge.canvas_delete_project("p1") == {"ok": True}
    assert not (canvas_dir / "p1.json").exists()
    # 第二次删除：文件已不在，仍返回 ok（前端重试 / 双端删同一张画布不报错）
    assert bridge.canvas_delete_project("p1") == {"ok": True}
    assert bridge.canvas_list_projects() == {"ok": True, "projects": []}
