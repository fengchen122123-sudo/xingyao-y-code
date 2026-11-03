"""画布生成视频（canvas_generate_video）的入口测试。

与 test_canvas_inline_edit.py 同一套测试模式（照搬改图桥的异步 job 契约）：
1) 入口同步校验：确认位、画布 ID、空指令、时长边界、源图存在性、并发闸门；
2) 线程体行为：调 tokenrhythm_generate_video 工具（参数正确）、产物路径与
   预览 URL 回填、canvas_video_result 事件字段、插件缺失 / 工具报错 / 异常
   都转成 error 事件，且闸门一定被清掉；
3) 单画布并发 1：同一画布第二个视频请求在入口就被拒，不占线程。

线程是异步的，所有等待都用事件 + 轮询，绝不用固定 sleep 死等。
"""

import os
import threading
import time

import pytest

import gui_canvas_mixin
from core.tool_protocol import ToolResult
from gui_canvas_mixin import CanvasBridgeMixin

# 1x1 PNG：源图（首帧）内容真假不影响被 mock 的插件，写成真 PNG 便于排查
_PNG_BYTES = (
    b"\x89PNG\r\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
# 最小 mp4 片段：ftyp 盒在 4-8 字节处
_MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 32

_WAIT_TIMEOUT = 5.0


class _FakeBridge(CanvasBridgeMixin):
    """最小假桥：只补齐画布混入依赖的"工作目录"与"事件出口"。"""

    def __init__(self, workdir):
        self._workdir = str(workdir)
        self.events = []
        self._event = threading.Event()

    def _canvas_workdir(self):
        return self._workdir

    def _emit_raw(self, event_type, data):
        self.events.append((event_type, data))
        self._event.set()

    def wait_event(self, event_type="canvas_video_result", job_id=None, timeout=_WAIT_TIMEOUT):
        """等第一条目标事件；超时返回 None（断言给可读失败，而不是卡死）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for kind, data in list(self.events):
                if kind != event_type:
                    continue
                if job_id is not None and data.get("job_id") != job_id:
                    continue
                return data
            self._event.wait(0.02)
            self._event.clear()
        return None


class _FakeRegistry:
    """假装 core.llm.tool_registry：只实现 execute_strict。"""

    def __init__(self):
        self.calls = []
        self.gate = None
        self.raise_key_error = False
        self.result = None
        self.raise_exc = None

    def execute_strict(self, tool_name, **kwargs):
        self.calls.append({"tool": tool_name, **kwargs})
        if self.gate is not None:
            assert self.gate.wait(_WAIT_TIMEOUT)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.raise_key_error:
            raise KeyError(f"unknown tool: {tool_name}")
        if self.result is not None:
            return self.result
        target = os.path.join(kwargs["save_dir"], "video-0001.mp4")
        with open(target, "wb") as handle:
            handle.write(_MP4_BYTES)
        return ToolResult("视频已生成并保存", changed_files=[target])


def _wait_until(predicate, timeout=_WAIT_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(autouse=True)
def _reset_gate():
    """视频闸门是模块级 dict：每个用例前后都清空，避免顺序依赖。"""
    gui_canvas_mixin._CANVAS_VIDEO_JOBS.clear()
    yield
    gui_canvas_mixin._CANVAS_VIDEO_JOBS.clear()


@pytest.fixture
def bridge(tmp_path):
    return _FakeBridge(tmp_path)


@pytest.fixture
def registry(monkeypatch):
    fake = _FakeRegistry()
    monkeypatch.setattr("core.llm.tool_registry", fake, raising=False)
    monkeypatch.setattr(
        "core.media_store.archive_media_file",
        lambda path, workdir=None: {"digest": "b" * 64, "ext": "mp4"},
        raising=False,
    )
    from core.media_server import media_server

    monkeypatch.setattr(
        media_server, "public_url", lambda digest, ext: "http://127.0.0.1/media/b.mp4"
    )
    return fake


@pytest.fixture
def source_png(tmp_path):
    """源图（首帧）：放在工作目录下，节点里存相对路径。"""
    path = tmp_path / "frame.png"
    path.write_bytes(_PNG_BYTES)
    return "frame.png"


class TestEntranceValidation:
    """同步校验：被拒时不起线程、不占闸门、不碰注册表。"""

    def test_requires_confirmed(self, bridge, registry):
        res = bridge.canvas_generate_video(
            "c1", "node-1", "frame.png", "推近镜头", confirmed=False
        )
        assert res["ok"] is False
        assert "确认" in res["msg"]
        assert registry.calls == []

    def test_rejects_bad_canvas_id(self, bridge, registry):
        res = bridge.canvas_generate_video(
            "../evil", "node-1", "frame.png", "推近镜头", confirmed=True
        )
        assert res["ok"] is False
        assert "画布" in res["msg"]
        assert registry.calls == []

    def test_rejects_empty_prompt(self, bridge, registry, source_png):
        res = bridge.canvas_generate_video(
            "c1", "node-1", source_png, "   ", confirmed=True
        )
        assert res["ok"] is False
        assert "指令" in res["msg"]
        assert registry.calls == []

    def test_rejects_bad_duration(self, bridge, registry, source_png):
        for bad in (1, 31, "abc", True):
            res = bridge.canvas_generate_video(
                "c1", "node-1", source_png, "推近镜头", duration=bad, confirmed=True
            )
            assert res["ok"] is False, "duration=%r 应被拒绝" % (bad,)
        assert registry.calls == []

    def test_accepts_smart_duration(self, bridge, registry, source_png, tmp_path):
        res = bridge.canvas_generate_video(
            "c1", "node-1", source_png, "推近镜头", duration=-1, confirmed=True
        )
        assert res.get("ok") is True
        event = bridge.wait_event()
        assert event is not None
        assert event["ok"] is True
        assert event["duration"] == -1

    def test_rejects_missing_source(self, bridge, registry):
        res = bridge.canvas_generate_video(
            "c1", "node-1", "no_such.png", "推近镜头", confirmed=True
        )
        assert res["ok"] is False
        assert "不存在" in res["msg"]
        assert registry.calls == []


class TestJobFlow:
    """异步线程体：调工具 → 回填产物 → 推事件 → 清闸门。"""

    def test_happy_path(self, bridge, registry, source_png, tmp_path):
        res = bridge.canvas_generate_video(
            "c1", "node-1", source_png, "镜头缓缓推近，橘猫伸懒腰",
            duration=5, confirmed=True,
        )
        assert res.get("ok") is True
        job_id = res["job_id"]

        event = bridge.wait_event(job_id=job_id)
        assert event is not None, "没有等到 canvas_video_result 事件"
        assert event["ok"] is True
        assert event["canvas_id"] == "c1"
        assert event["source_node_id"] == "node-1"
        assert event["job_id"] == job_id
        assert event["error"] == ""
        # 产物落媒体目录，path 是相对工作目录的 posix 路径
        assert event["path"].startswith(".ycode/canvas/media/")
        assert "/" in event["path"] and "\\" not in event["path"]
        assert os.path.isfile(event["abs_path"])
        # 预览 URL 走归档 + 媒体服务（与改图同口径）
        assert event["url"] == "http://127.0.0.1/media/b.mp4"
        assert event["duration"] == 5
        # 闸门已清
        assert _wait_until(lambda: not gui_canvas_mixin._CANVAS_VIDEO_JOBS)

        # 工具调用参数：首帧绝对路径、时长、保存目录
        assert len(registry.calls) == 1
        call = registry.calls[0]
        assert call["tool"] == "tokenrhythm_generate_video"
        assert call["prompt"] == "镜头缓缓推近，橘猫伸懒腰"
        assert os.path.isabs(call["image"]) and call["image"].endswith("frame.png")
        assert call["duration"] == 5
        assert call["save_dir"].startswith(str(tmp_path))

    def test_missing_tool_reports_plugin_guidance(self, bridge, registry, source_png):
        registry.raise_key_error = True
        res = bridge.canvas_generate_video(
            "c1", "node-1", source_png, "推近镜头", confirmed=True
        )
        assert res.get("ok") is True
        event = bridge.wait_event()
        assert event is not None
        assert event["ok"] is False
        assert "tokenrhythm_video" in event["error"]
        assert _wait_until(lambda: not gui_canvas_mixin._CANVAS_VIDEO_JOBS)

    def test_tool_error_becomes_error_event(self, bridge, registry, source_png):
        registry.result = ToolResult("内容审核未通过", is_error=True)
        res = bridge.canvas_generate_video(
            "c1", "node-1", source_png, "推近镜头", confirmed=True
        )
        assert res.get("ok") is True
        event = bridge.wait_event()
        assert event is not None
        assert event["ok"] is False
        assert "内容审核未通过" in event["error"]
        assert _wait_until(lambda: not gui_canvas_mixin._CANVAS_VIDEO_JOBS)

    def test_no_artifact_becomes_error_event(self, bridge, registry, source_png):
        registry.result = ToolResult("视频已生成", changed_files=[])
        bridge.canvas_generate_video("c1", "node-1", source_png, "推近镜头", confirmed=True)
        event = bridge.wait_event()
        assert event is not None
        assert event["ok"] is False
        assert "产物" in event["error"]
        assert _wait_until(lambda: not gui_canvas_mixin._CANVAS_VIDEO_JOBS)

    def test_exception_becomes_error_event_and_clears_gate(self, bridge, registry, source_png):
        registry.raise_exc = RuntimeError("boom")
        bridge.canvas_generate_video("c1", "node-1", source_png, "推近镜头", confirmed=True)
        event = bridge.wait_event()
        assert event is not None
        assert event["ok"] is False
        assert "boom" in event["error"]
        assert _wait_until(lambda: not gui_canvas_mixin._CANVAS_VIDEO_JOBS)

    def test_gate_blocks_second_submission(self, bridge, registry, source_png):
        release = threading.Event()
        registry.gate = release
        res1 = bridge.canvas_generate_video(
            "c1", "node-1", source_png, "推近镜头", confirmed=True
        )
        assert res1.get("ok") is True
        # 第一个任务卡在工具里时，同一画布的第二个请求必须入口即拒
        res2 = bridge.canvas_generate_video(
            "c1", "node-2", source_png, "再生成一个", confirmed=True
        )
        assert res2["ok"] is False
        assert "视频" in res2["msg"]
        assert len(registry.calls) == 1
        release.set()
        event = bridge.wait_event()
        assert event is not None and event["ok"] is True
        assert _wait_until(lambda: not gui_canvas_mixin._CANVAS_VIDEO_JOBS)


class TestBridgeToRealPlugin:
    """桥 → 真实插件入口的端到端（无凭据边界）。

    用真实的 tokenrhythm_video 插件模块 + 真实 ToolRegistry 走一遍：
    桥方法校验通过、线程体把参数送进插件的 generate_video、插件在无 Key
    场景下返回配置指引错误、错误经 canvas_video_result 事件回推、闸门清空。
    不 mock HTTP 层、不发任何真实请求：_resolve_api_key 固定为空串。
    """

    def test_no_credential_returns_config_guidance(self, bridge, monkeypatch, source_png):
        import importlib.util

        from core.tool_registry import ToolRegistry

        entry = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "plugins", "tokenrhythm_video", "plugin.py",
        )
        spec = importlib.util.spec_from_file_location("_tr_video_plugin_real", entry)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # 无凭据边界：密钥解析固定为空串，插件必须停在配置指引、不发网络请求
        monkeypatch.setattr(module, "_resolve_api_key", lambda: "")
        registry = ToolRegistry()
        module.register(registry)
        monkeypatch.setattr("core.llm.tool_registry", registry, raising=False)

        res = bridge.canvas_generate_video(
            "c1", "node-1", source_png, "推近镜头", confirmed=True
        )
        assert res.get("ok") is True
        event = bridge.wait_event()
        assert event is not None
        assert event["ok"] is False
        assert "tokenrhythm.studio" in event["error"]
        assert _wait_until(lambda: not gui_canvas_mixin._CANVAS_VIDEO_JOBS)
