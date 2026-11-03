"""角色库桥（canvas_character_* / canvas_generate_video 角色注入）的测试。

覆盖：
1) 角色档案 CRUD 桥入口：建/列/删 + 重名与未初始化降级；
2) 存为角色参考：归档拿 digest → 项目 refs 副本 → 主参考替换提示；
3) canvas_character_generate_image：主参考作垫图、prompt 拼装、事件带
   character_ref_snapshot、生成台账落库；
4) canvas_generate_video 角色注入：单角色拼装、多角色硬拒绝、快照回传、
   台账落库；
5) P1.5 OSS 直传：新鲜 URL 直传插件（oss_direct=True）、过期/缺失回落
   本地图重绘路径；
6) 普通改图/视频路径回归：不传新参数时行为与 P1 一致（prompt 原样、
   无角色字段、无台账副作用之外的差异）。

与 test_canvas_video_bridge.py 同一套假桥模式；等待一律用事件 + 轮询。
"""

import os
import time

import pytest

import gui_canvas_mixin
import core.character_store as character_store_module
from core.character_store import init_character_storage
from core.memory import MemoryManager
from core.tool_protocol import ToolResult
from gui_canvas_mixin import CanvasBridgeMixin

_PNG_BYTES = (
    b"\x89PNG\r\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
_MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 32
_FAKE_DIGEST = "a" * 64

_WAIT_TIMEOUT = 5.0


def _wait_until(predicate, timeout=_WAIT_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _FakeBridge(CanvasBridgeMixin):
    """最小假桥：工作目录 + 事件出口（与 test_canvas_video_bridge 同款）。"""

    def __init__(self, workdir):
        self._workdir = str(workdir)
        self.events = []

    def _canvas_workdir(self):
        return self._workdir

    def _emit_raw(self, event_type, data):
        self.events.append((event_type, data))


@pytest.fixture(autouse=True)
def _reset_gates():
    gui_canvas_mixin._CANVAS_VIDEO_JOBS.clear()
    gui_canvas_mixin._CANVAS_EDIT_JOBS.clear()
    # 角色库句柄是进程级：用例间必须归零，避免上一例的库漏到下一例
    character_store_module._storage = None
    yield
    gui_canvas_mixin._CANVAS_VIDEO_JOBS.clear()
    gui_canvas_mixin._CANVAS_EDIT_JOBS.clear()
    character_store_module._storage = None


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    fake = _FakeBridge(tmp_path)
    # 归档走桩：digest 固定、不写全局内容寻址仓库
    monkeypatch.setattr(
        "core.media_store.archive_media_file",
        lambda path, workdir=None: {
            "kind": "image", "digest": _FAKE_DIGEST, "mime": "image/png",
            "ext": "png", "bytes": len(_PNG_BYTES), "width": 1, "height": 1,
            "duration_ms": None, "source_name": os.path.basename(str(path)),
        },
        raising=False,
    )
    from core.media_server import media_server

    monkeypatch.setattr(media_server, "public_url", lambda digest, ext: "http://127.0.0.1/media/a.png")
    return fake


@pytest.fixture
def storage(tmp_path):
    manager = MemoryManager(str(tmp_path / "chars.db"))
    return init_character_storage(manager)


@pytest.fixture
def registry(monkeypatch):
    class _FakeRegistry:
        def __init__(self):
            self.calls = []

        def execute_strict(self, tool_name, **kwargs):
            self.calls.append({"tool": tool_name, **kwargs})
            if tool_name == "tokenrhythm_generate_video":
                target = os.path.join(kwargs["save_dir"], "video-0001.mp4")
                payload = _MP4_BYTES
            else:
                target = os.path.join(kwargs["save_dir"], "edit-0001.png")
                payload = _PNG_BYTES
            with open(target, "wb") as handle:
                handle.write(payload)
            return ToolResult(
                "已生成", changed_files=[target],
                metadata={"oss_url": "https://oss.example/x.png", "cost_cny": 0.6},
            )

    fake = _FakeRegistry()
    monkeypatch.setattr("core.llm.tool_registry", fake, raising=False)
    return fake


@pytest.fixture
def source_png(tmp_path):
    path = tmp_path / "portrait.png"
    path.write_bytes(_PNG_BYTES)
    return "portrait.png"


def _make_character(storage, name="小满"):
    return storage.create_character(
        name, prompt_fragment="黑色齐肩短发，琥珀色眼睛", style_tags="写实摄影",
        workdir=None or os.getcwd(),
    )


class TestCharacterCrudBridge:
    def test_list_create_delete_roundtrip(self, bridge, storage):
        res = bridge.canvas_character_create(
            "小满", "短发少女", "黑色齐肩短发", "写实摄影", 42
        )
        assert res["ok"] is True
        cid = res["character"]["id"]
        listed = bridge.canvas_character_list()
        assert listed["ok"] is True
        assert [c["name"] for c in listed["characters"]] == ["小满"]
        assert listed["characters"][0]["seed"] == 42
        assert bridge.canvas_character_delete(cid)["ok"] is True
        assert bridge.canvas_character_list()["characters"] == []

    def test_duplicate_name_friendly_error(self, bridge, storage):
        bridge.canvas_character_create("小满")
        res = bridge.canvas_character_create("小满")
        assert res["ok"] is False and "已存在" in res["msg"]

    def test_storage_unavailable_degrades(self, bridge):
        res = bridge.canvas_character_list()
        assert res["ok"] is False and "未初始化" in res["msg"]
        assert bridge.canvas_character_create("x")["ok"] is False
        assert bridge.canvas_character_delete(1)["ok"] is False


class TestAddRefBridge:
    def test_add_ref_writes_project_copy(self, bridge, storage, source_png, tmp_path):
        character = _make_character(storage)
        res = bridge.canvas_character_add_ref(
            "c1", source_png, character["id"], "front",
            is_primary=True, source_node_id="node-1",
        )
        assert res["ok"] is True
        assert res["replaced_primary"] is False
        assert res["character_id"] == character["id"]
        ref = res["ref"]
        assert ref["media_digest"] == _FAKE_DIGEST
        assert ref["ref_type"] == "front"
        # 项目内副本真实落盘，路径是项目相对 posix 路径
        assert ref["rel_path"].startswith(".ycode/canvas/media/refs/")
        assert os.path.isfile(os.path.join(str(tmp_path), ref["rel_path"]))

    def test_primary_replacement_flag(self, bridge, storage, source_png):
        character = _make_character(storage)
        first = bridge.canvas_character_add_ref(
            "c1", source_png, character["id"], "front", is_primary=True
        )
        assert first["replaced_primary"] is False
        second = bridge.canvas_character_add_ref(
            "c1", source_png, character["id"], "front", is_primary=True
        )
        assert second["replaced_primary"] is True
        primaries = [r for r in storage.list_refs(character["id"]) if r["is_primary"]]
        assert len(primaries) == 1

    def test_bad_ref_type_and_bad_source(self, bridge, storage, source_png):
        character = _make_character(storage)
        res = bridge.canvas_character_add_ref("c1", source_png, character["id"], "back")
        assert res["ok"] is False and "参考类型" in res["msg"]
        res = bridge.canvas_character_add_ref("c1", "missing.png", character["id"], "front")
        assert res["ok"] is False
        res = bridge.canvas_character_add_ref("../evil", source_png, character["id"], "front")
        assert res["ok"] is False


class TestCharacterImageGeneration:
    def test_compose_prompt_and_primary_ref_as_base(self, bridge, storage, registry, source_png):
        character = _make_character(storage)
        bridge.canvas_character_add_ref(
            "c1", source_png, character["id"], "front", is_primary=True
        )
        res = bridge.canvas_character_generate_image(
            "c1", character["id"], "正面半身定妆照", "node-1", confirmed=True
        )
        assert res["ok"] is True
        _wait_until(lambda: any(k == "canvas_edit_result" for k, _ in bridge.events))
        kind, event = next((k, d) for k, d in bridge.events if k == "canvas_edit_result")
        assert kind == "canvas_edit_result"
        assert event["ok"] is True
        # 垫图 = 主参考图的项目内副本（不是源节点图）
        call = registry.calls[0]
        assert call["tool"] == "tokenrhythm_edit_image"
        assert "refs" in call["image"]
        # prompt 已拼装：镜头描述 + 角色 + 画风
        assert call["prompt"].startswith("正面半身定妆照")
        assert "【角色】小满：黑色齐肩短发，琥珀色眼睛" in call["prompt"]
        assert "【画风】写实摄影" in call["prompt"]
        # 事件带角色三件套（前端写节点 meta 用）
        assert event["character_ids"] == [character["id"]]
        assert event["character_ref_snapshot"] == [_FAKE_DIGEST]
        assert event["final_prompt"] == call["prompt"]
        # P1.5：OSS 直链透出（时间戳非零）
        assert event["oss_url"] == "https://oss.example/x.png"
        assert event["oss_url_saved_at"] > 0

    def test_generation_record_landed(self, bridge, storage, registry, source_png):
        character = _make_character(storage)
        bridge.canvas_character_add_ref(
            "c1", source_png, character["id"], "front", is_primary=True
        )
        bridge.canvas_character_generate_image(
            "c1", character["id"], "定妆照", "node-1", confirmed=True
        )
        assert _wait_until(lambda: any(k == "canvas_edit_result" for k, _ in bridge.events))
        records = storage.list_generation_records()
        assert len(records) == 1
        record = records[0]
        assert record["kind"] == "image" and record["status"] == "success"
        assert record["character_ids"] == [character["id"]]
        assert record["cost_cny"] == pytest.approx(0.2)
        assert record["ref_snapshot"] == [_FAKE_DIGEST]
        assert record["final_prompt"].startswith("定妆照")

    def test_requires_confirmed_and_primary_ref(self, bridge, storage, source_png):
        character = _make_character(storage)
        res = bridge.canvas_character_generate_image(
            "c1", character["id"], "定妆照", "node-1", confirmed=False
        )
        assert res["ok"] is False and "确认" in res["msg"]
        res = bridge.canvas_character_generate_image(
            "c1", character["id"], "定妆照", "node-1", confirmed=True
        )
        assert res["ok"] is False and "主参考" in res["msg"]

    def test_empty_prompt_rejected(self, bridge, storage, source_png):
        character = _make_character(storage)
        bridge.canvas_character_add_ref("c1", source_png, character["id"], "front", is_primary=True)
        res = bridge.canvas_character_generate_image(
            "c1", character["id"], "  ", "node-1", confirmed=True
        )
        assert res["ok"] is False and "画面描述" in res["msg"]


class TestVideoCharacterInjection:
    def test_single_character_compose_and_snapshot(self, bridge, storage, registry, source_png):
        character = _make_character(storage)
        bridge.canvas_character_add_ref("c1", source_png, character["id"], "front", is_primary=True)
        res = bridge.canvas_generate_video(
            "c1", "node-1", source_png, "镜头缓缓推近", 5, "", True,
            character_ids=[character["id"]],
        )
        assert res["ok"] is True
        event = None
        assert _wait_until(lambda: any(k == "canvas_video_result" for k, _ in bridge.events))
        event = next(d for k, d in bridge.events if k == "canvas_video_result")
        assert event["ok"] is True
        call = registry.calls[0]
        assert call["prompt"] == (
            "镜头缓缓推近\n【角色】小满：黑色齐肩短发，琥珀色眼睛\n【画风】写实摄影"
        )
        assert event["final_prompt"] == call["prompt"]
        assert event["prompt"] == "镜头缓缓推近"
        assert event["character_ids"] == [character["id"]]
        assert event["character_ref_snapshot"] == [_FAKE_DIGEST]
        assert event["cost_cny"] == pytest.approx(0.6)
        # 台账落库
        records = storage.list_generation_records()
        assert len(records) == 1 and records[0]["kind"] == "video"
        assert records[0]["status"] == "success"

    def test_multi_character_hard_reject(self, bridge, storage, registry, source_png):
        c1 = _make_character(storage, "小满")
        c2 = _make_character(storage, "阿七")
        res = bridge.canvas_generate_video(
            "c1", "node-1", source_png, "推近", 5, "", True,
            character_ids=[c1["id"], c2["id"]],
        )
        assert res["ok"] is False
        assert "一个角色" in res["msg"]
        assert registry.calls == []

    def test_missing_character_rejected(self, bridge, storage, registry, source_png):
        res = bridge.canvas_generate_video(
            "c1", "node-1", source_png, "推近", 5, "", True, character_ids=[999]
        )
        assert res["ok"] is False and "角色不存在" in res["msg"]
        assert registry.calls == []

    def test_no_characters_keeps_plain_prompt(self, bridge, storage, registry, source_png):
        """回归：不带角色时 prompt 原样透传、无角色字段（P1 行为不变）。"""
        bridge.canvas_generate_video("c1", "node-1", source_png, "推近", 5, "", True)
        assert _wait_until(lambda: any(k == "canvas_video_result" for k, _ in bridge.events))
        event = next(d for k, d in bridge.events if k == "canvas_video_result")
        assert event["prompt"] == "推近"
        assert event["final_prompt"] == "推近"
        assert event["character_ids"] == []
        assert event["character_ref_snapshot"] == []
        assert event["oss_direct"] is False
        assert registry.calls[0]["prompt"] == "推近"


class TestOssDirectPassthrough:
    def test_fresh_url_passthrough_skips_local_redraw(self, bridge, storage, registry, source_png):
        fresh_ms = time.time() * 1000
        bridge.canvas_generate_video(
            "c1", "node-1", source_png, "推近", 5, "", True,
            image_url="https://oss.example/signed.png", image_url_saved_at=fresh_ms,
        )
        assert _wait_until(lambda: any(k == "canvas_video_result" for k, _ in bridge.events))
        event = next(d for k, d in bridge.events if k == "canvas_video_result")
        assert event["ok"] is True
        assert event["oss_direct"] is True
        call = registry.calls[0]
        assert call["image"] == "https://oss.example/signed.png"  # URL 直传，不重绘

    def test_stale_url_falls_back_to_local(self, bridge, storage, registry, source_png):
        stale_ms = (time.time() - 51 * 60) * 1000
        bridge.canvas_generate_video(
            "c1", "node-1", source_png, "推近", 5, "", True,
            image_url="https://oss.example/signed.png", image_url_saved_at=stale_ms,
        )
        assert _wait_until(lambda: any(k == "canvas_video_result" for k, _ in bridge.events))
        event = next(d for k, d in bridge.events if k == "canvas_video_result")
        assert event["oss_direct"] is False
        assert registry.calls[0]["image"] != "https://oss.example/signed.png"
        assert os.path.isabs(registry.calls[0]["image"])

    def test_missing_timestamp_falls_back(self, bridge, storage, registry, source_png):
        bridge.canvas_generate_video(
            "c1", "node-1", source_png, "推近", 5, "", True,
            image_url="https://oss.example/signed.png",
        )
        assert _wait_until(lambda: any(k == "canvas_video_result" for k, _ in bridge.events))
        event = next(d for k, d in bridge.events if k == "canvas_video_result")
        assert event["oss_direct"] is False


class TestEditOssUrlPassthrough:
    def test_plain_edit_event_carries_oss_url(self, bridge, storage, registry, source_png):
        """普通改图（canvas_edit_image 原入口）也带出 OSS 直链：P1.5。"""
        res = bridge.canvas_edit_image("c1", source_png, "改成黄昏", "node-1", confirmed=True)
        assert res["ok"] is True
        assert _wait_until(lambda: any(k == "canvas_edit_result" for k, _ in bridge.events))
        event = next(d for k, d in bridge.events if k == "canvas_edit_result")
        assert event["ok"] is True
        assert event["oss_url"] == "https://oss.example/x.png"
        assert event["oss_url_saved_at"] > 0
        # 无角色字段：普通改图不带角色上下文
        assert "character_ids" not in event
