"""画布就地改图（canvas_edit_image / canvas_edit_image_annotated）的入口测试。

覆盖三段契约：
1) 入口同步校验：确认位、画布 ID、空指令、源图存在性 / 体积 / 格式、并发闸门；
2) 线程体行为：调工具（参数正确）、产物路径与预览 URL 回填、事件字段、
   插件缺失 / 工具报错 / 异常都必须转成 error 事件，且闸门一定被清掉；
3) 画笔标注：透明标注层与源图合成（尺寸兜底 / 像素正确）后复用同一条改图
   链路，坏 base64、非 PNG、损坏 PNG 都在起线程之前拒绝且不留中间文件。

线程是异步的，所有等待都用事件 + 轮询，绝不用固定 sleep 死等。
"""

import base64
import io
import os
import threading
import time

import pytest

import gui_canvas_mixin
from core.tool_protocol import ToolResult
from gui_canvas_mixin import CanvasBridgeMixin

# 1x1 RGBA PNG：产物文件内容无所谓真假（归档模块在测试里被替换），
# 但写成真 PNG 便于人工排查失败现场
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_WAIT_TIMEOUT = 5.0


class _FakeBridge(CanvasBridgeMixin):
    """最小假桥：只补齐画布混入依赖的两件事——工作目录与事件出口。"""

    def __init__(self, workdir):
        self._workdir = str(workdir)
        self.events = []
        self._event = threading.Event()

    def _canvas_workdir(self):
        # 画布所有落盘位置都从这里推导，指到 tmp_path 就与真实项目完全隔离
        return self._workdir

    def _emit_raw(self, event_type, data):
        self.events.append((event_type, data))
        self._event.set()

    def wait_event(self, event_type="canvas_edit_result", job_id=None, timeout=_WAIT_TIMEOUT):
        """等第一条目标事件；超时返回 None（让断言给出可读失败，而不是卡死）。

        传 job_id 时按任务号过滤：同一用例里提交多次时，不能拿上一次的旧事件
        冒充本次结果。
        """
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

    def find_event(self, event_type="canvas_edit_result"):
        for kind, data in self.events:
            if kind == event_type:
                return data
        return None


class _FakeRegistry:
    """假装成 core.llm.tool_registry：只实现 execute_strict。"""

    def __init__(self):
        self.calls = []
        # 注入项：卡住任务（复现"同一画布还在改"）、抛 KeyError、指定返回
        self.gate = None
        self.raise_key_error = False
        self.result = None
        self.raise_exc = None

    def execute_strict(self, tool_name, **kwargs):
        self.calls.append({"tool": tool_name, **kwargs})
        if self.gate is not None:
            # 卡到测试主动放行，模拟 180s 级的真实耗时
            assert self.gate.wait(_WAIT_TIMEOUT)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.raise_key_error:
            raise KeyError(f"unknown tool: {tool_name}")
        if self.result is not None:
            return self.result
        # 模拟插件行为：产物写进调用方给的 save_dir
        target = os.path.join(kwargs["save_dir"], "edit-0001.png")
        with open(target, "wb") as handle:
            handle.write(_PNG_BYTES)
        return ToolResult("图片已改图并保存", changed_files=[target])


def _wait_until(predicate, timeout=_WAIT_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(autouse=True)
def _reset_gate():
    """闸门是模块级 dict：每个用例前后都清空，避免顺序依赖。"""
    gui_canvas_mixin._CANVAS_EDIT_JOBS.clear()
    yield
    gui_canvas_mixin._CANVAS_EDIT_JOBS.clear()


@pytest.fixture
def bridge(tmp_path):
    return _FakeBridge(tmp_path)


@pytest.fixture
def registry(monkeypatch):
    fake = _FakeRegistry()
    monkeypatch.setattr("core.llm.tool_registry", fake, raising=False)
    # 归档 + 媒体服务是外部基础设施：换成确定返回值，流程与断言都可控
    monkeypatch.setattr(
        "core.media_store.archive_media_file",
        lambda path, workdir=None: {
            "digest": "a" * 64,
            "ext": "png",
            "width": 1,
            "height": 1,
        },
        raising=False,
    )
    from core.media_server import media_server

    monkeypatch.setattr(
        media_server, "public_url", lambda digest, ext: "http://127.0.0.1/media/a.png"
    )
    return fake


@pytest.fixture
def source_png(tmp_path):
    path = tmp_path / "source.png"
    path.write_bytes(_PNG_BYTES)
    return path


def _submit(bridge, tmp_path, **overrides):
    payload = {
        "canvas_id": "default",
        "node_file": str(tmp_path / "source.png"),
        "prompt": "把月亮换成一轮落日",
        "source_node_id": "node-1",
        "confirmed": True,
    }
    payload.update(overrides)
    return bridge.canvas_edit_image(**payload)


def _submit_annotated(bridge, tmp_path, **overrides):
    payload = {
        "canvas_id": "default",
        "node_file": str(tmp_path / "source.png"),
        "prompt": "把月亮换成一轮落日",
        "source_node_id": "node-1",
        "annotation_png_b64": "",
        "confirmed": True,
    }
    payload.update(overrides)
    return bridge.canvas_edit_image_annotated(**payload)


def _png_bytes(image):
    """把 PIL 图对象压成 PNG 字节：标注层按前端口径真的走 base64。"""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def pillow():
    """真实 Pillow：合成链路要解码真图，桩不出来；venv 里没装就跳过标注用例。"""
    return pytest.importorskip("PIL.Image")


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
def test_happy_path_passes_arguments_and_emits_result(bridge, registry, tmp_path, source_png):
    reply = _submit(bridge, tmp_path)

    assert reply["ok"] is True
    assert reply["job_id"]
    data = bridge.wait_event()
    assert data is not None

    # 工具入参：源图的绝对路径、去掉首尾空白的指令、画布媒体目录
    assert len(registry.calls) == 1
    call = registry.calls[0]
    assert call["tool"] == "tokenrhythm_edit_image"
    assert call["prompt"] == "把月亮换成一轮落日"
    assert call["image"] == str(source_png)
    expected_dir = str(tmp_path / ".ycode" / "canvas" / "media")
    assert call["save_dir"] == expected_dir
    assert os.path.isdir(expected_dir)
    # 空 model 不下传，交给工具默认值
    assert "model" not in call

    # 事件字段：ok / job_id / 画布与节点溯源 / 相对路径 + 绝对路径 / 预览 URL
    assert data["ok"] is True
    assert data["error"] == ""
    assert data["job_id"] == reply["job_id"]
    assert data["canvas_id"] == "default"
    assert data["source_node_id"] == "node-1"
    assert data["path"] == ".ycode/canvas/media/edit-0001.png"
    assert data["abs_path"] == os.path.join(expected_dir, "edit-0001.png")
    assert data["url"] == "http://127.0.0.1/media/a.png"
    assert (data["width"], data["height"]) == (1, 1)
    # 任务结束后闸门必须释放，否则这张画布再也改不了图
    assert _wait_until(lambda: not gui_canvas_mixin._CANVAS_EDIT_JOBS)


def test_model_is_forwarded_when_given(bridge, registry, tmp_path, source_png):
    reply = _submit(bridge, tmp_path, model="wan2.7-image")
    assert reply["ok"] is True
    assert bridge.wait_event() is not None
    assert registry.calls[0]["model"] == "wan2.7-image"


def test_relative_node_file_resolves_against_workdir(bridge, registry, tmp_path):
    """节点存的是相对项目根的路径，必须按画布工作目录解析。"""
    nested = tmp_path / ".ycode" / "canvas" / "media"
    nested.mkdir(parents=True)
    (nested / "prev.png").write_bytes(_PNG_BYTES)

    reply = _submit(bridge, tmp_path, node_file=".ycode/canvas/media/prev.png")

    assert reply["ok"] is True
    assert bridge.wait_event() is not None
    assert registry.calls[0]["image"] == str(nested / "prev.png")


# ---------------------------------------------------------------------------
# 同步拒绝路径
# ---------------------------------------------------------------------------
def test_missing_confirmed_is_rejected(bridge, registry, tmp_path, source_png):
    reply = _submit(bridge, tmp_path, confirmed=False)

    assert reply == {"ok": False, "msg": "改图需要用户确认"}
    # 没确认就不能起任务，也不该碰工具
    assert registry.calls == []
    assert bridge.find_event() is None


def test_empty_prompt_is_rejected(bridge, registry, tmp_path, source_png):
    reply = _submit(bridge, tmp_path, prompt="   ")

    assert reply["ok"] is False
    assert "改图要求" in reply["msg"]
    assert registry.calls == []


def test_missing_source_file_is_rejected(bridge, registry, tmp_path):
    reply = _submit(bridge, tmp_path, node_file=str(tmp_path / "不存在.png"))

    assert reply["ok"] is False
    assert "源图" in reply["msg"]
    assert registry.calls == []


def test_invalid_canvas_id_is_rejected(bridge, registry, tmp_path, source_png):
    reply = _submit(bridge, tmp_path, canvas_id="../escape")

    assert reply == {"ok": False, "msg": "非法画布 ID"}
    assert registry.calls == []


def test_oversize_and_unsupported_ext_are_rejected(bridge, registry, tmp_path, source_png):
    big = tmp_path / "big.png"
    with open(big, "wb") as handle:
        handle.truncate(gui_canvas_mixin._CANVAS_EDIT_MAX_BYTES + 1)
    reply = _submit(bridge, tmp_path, node_file=str(big))
    assert reply["ok"] is False
    assert "10MB" in reply["msg"]

    gif = tmp_path / "anim.gif"
    gif.write_bytes(b"GIF89a")
    reply = _submit(bridge, tmp_path, node_file=str(gif))
    assert reply["ok"] is False
    assert "格式不支持" in reply["msg"]

    assert registry.calls == []


def test_second_submit_on_same_canvas_is_rejected(bridge, registry, tmp_path, source_png):
    """单画布同时只允许 1 个改图任务：连点会同时烧钱又互相覆盖节点。"""
    registry.gate = threading.Event()

    first = _submit(bridge, tmp_path)
    assert first["ok"] is True
    second = _submit(bridge, tmp_path, source_node_id="node-2")
    assert second == {"ok": False, "msg": "这张图正在改，等它回来"}
    assert len(registry.calls) == 1

    registry.gate.set()
    assert bridge.wait_event(job_id=first["job_id"]) is not None
    # 放行并收尾后闸门必须回到可用状态
    assert _wait_until(lambda: not gui_canvas_mixin._CANVAS_EDIT_JOBS)
    third = _submit(bridge, tmp_path, source_node_id="node-3")
    assert third["ok"] is True
    assert bridge.wait_event(job_id=third["job_id"]) is not None
    assert _wait_until(lambda: not gui_canvas_mixin._CANVAS_EDIT_JOBS)


# ---------------------------------------------------------------------------
# 线程体内的失败：一律转成 error 事件，且闸门必被清
# ---------------------------------------------------------------------------
def test_missing_tool_becomes_error_event(bridge, registry, tmp_path, source_png):
    registry.raise_key_error = True

    reply = _submit(bridge, tmp_path)
    assert reply["ok"] is True
    data = bridge.wait_event()

    assert data is not None
    assert data["ok"] is False
    assert data["error"] == "改图插件未启用，请在设置页启用 tokenrhythm_image 并配置 API Key"
    assert data["job_id"] == reply["job_id"]
    assert _wait_until(lambda: not gui_canvas_mixin._CANVAS_EDIT_JOBS)


def test_tool_error_result_becomes_error_event(bridge, registry, tmp_path, source_png):
    registry.result = ToolResult("垫图超过 10MB 限制", is_error=True)

    _submit(bridge, tmp_path)
    data = bridge.wait_event()

    assert data is not None
    assert data["ok"] is False
    assert data["error"] == "垫图超过 10MB 限制"
    assert data["path"] == ""
    assert _wait_until(lambda: not gui_canvas_mixin._CANVAS_EDIT_JOBS)


def test_unexpected_exception_becomes_error_event(bridge, registry, tmp_path, source_png):
    """工具抛异常（而不是返回 is_error）也必须本地消化，绝不能漏出线程。"""
    registry.raise_exc = RuntimeError("网络炸了")

    reply = _submit(bridge, tmp_path)
    assert reply["ok"] is True
    data = bridge.wait_event()

    assert data is not None
    assert data["ok"] is False
    assert "网络炸了" in data["error"]
    assert _wait_until(lambda: not gui_canvas_mixin._CANVAS_EDIT_JOBS)


# ---------------------------------------------------------------------------
# 画笔标注：合成后走同一条改图链路
# ---------------------------------------------------------------------------
def test_annotated_composes_layer_and_reuses_edit_pipeline(bridge, registry, tmp_path, pillow):
    """前端只送透明笔迹层，后端补原图：合成图才是喂给模型的垫图。"""
    source = tmp_path / "source.png"
    pillow.new("RGBA", (4, 4), (10, 20, 30, 255)).save(source)

    ann = pillow.new("RGBA", (4, 4), (0, 0, 0, 0))
    for x in (1, 2):
        for y in (1, 2):
            ann.putpixel((x, y), (255, 0, 0, 255))
    payload = base64.b64encode(_png_bytes(ann)).decode("ascii")

    reply = _submit_annotated(
        bridge,
        tmp_path,
        # 首尾空白必须被 strip：进 prompt 的是干净的指令
        prompt="  把方块换成蓝色  ",
        annotation_png_b64=payload,
    )
    assert reply["ok"] is True
    data = bridge.wait_event(job_id=reply["job_id"])
    assert data is not None
    assert data["ok"] is True

    # 合成图落在画布媒体目录，文件名前缀固定，且不覆盖源图
    media_dir = tmp_path / ".ycode" / "canvas" / "media"
    composed = data["annotation_abs_path"]
    assert os.path.dirname(composed) == str(media_dir)
    assert os.path.basename(composed).startswith("annotation-")
    assert data["annotation_path"] == f".ycode/canvas/media/{os.path.basename(composed)}"

    # 尺寸与源图一致；笔迹像素是红的，透明处仍是源图底色（没被整片糊住）
    with pillow.open(composed) as img:
        assert img.size == (4, 4)
        rgba = img.convert("RGBA")
        assert rgba.getpixel((1, 1)) == (255, 0, 0, 255)
        assert rgba.getpixel((2, 2)) == (255, 0, 0, 255)
        assert rgba.getpixel((0, 0)) == (10, 20, 30, 255)

    # 同一条改图链路：工具拿到的是合成图，prompt 追加了固定的"只改标注处"说明
    assert len(registry.calls) == 1
    call = registry.calls[0]
    assert call["tool"] == "tokenrhythm_edit_image"
    assert call["image"] == composed
    assert call["prompt"] == (
        "把方块换成蓝色\n图中红色标注处需要按上述要求修改，画面其他部分保持原样。"
    )
    assert _wait_until(lambda: not gui_canvas_mixin._CANVAS_EDIT_JOBS)


def test_annotated_resizes_layer_when_size_differs(bridge, registry, tmp_path, pillow):
    """标注层尺寸和源图不一致时按源图缩放兜底，不能整条链路失败。"""
    source = tmp_path / "source.png"
    pillow.new("RGBA", (4, 4), (0, 0, 0, 255)).save(source)

    ann = pillow.new("RGBA", (8, 8), (255, 0, 0, 255))
    # 顺带覆盖 data URL 前缀容错：前端 canvas.toDataURL 天然带这段
    payload = "data:image/png;base64," + base64.b64encode(_png_bytes(ann)).decode("ascii")

    reply = _submit_annotated(bridge, tmp_path, annotation_png_b64=payload)
    assert reply["ok"] is True
    data = bridge.wait_event(job_id=reply["job_id"])
    assert data is not None
    assert data["ok"] is True

    with pillow.open(data["annotation_abs_path"]) as img:
        assert img.size == (4, 4)
        # 整层纯红缩放到 4×4 后仍然通红：缩放用的是源图尺寸，不是反的
        assert img.convert("RGBA").getpixel((2, 2)) == (255, 0, 0, 255)
    assert registry.calls[0]["image"] == data["annotation_abs_path"]
    assert _wait_until(lambda: not gui_canvas_mixin._CANVAS_EDIT_JOBS)


def test_annotated_rejects_bad_payload_without_leaving_files(
    bridge, registry, tmp_path, source_png
):
    """坏数据必须在起线程之前拒绝：不占闸门、不调工具、不留半截中间文件。"""
    reply = _submit_annotated(bridge, tmp_path, annotation_png_b64="@@@not-base64@@@")
    assert reply["ok"] is False
    assert "base64" in reply["msg"]

    # 合法 base64 但不是 PNG：靠魔数就能拒
    reply = _submit_annotated(
        bridge, tmp_path, annotation_png_b64=base64.b64encode(b"GIF89a\x01\x00").decode()
    )
    assert reply == {"ok": False, "msg": "标注层必须是 PNG 图片"}

    # 有 PNG 魔数但内容坏了：Pillow 解码失败也要给出明确原因
    broken = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"broken-body").decode()
    reply = _submit_annotated(bridge, tmp_path, annotation_png_b64=broken)
    assert reply["ok"] is False
    assert "解析" in reply["msg"]

    assert registry.calls == []
    assert bridge.find_event() is None
    assert not gui_canvas_mixin._CANVAS_EDIT_JOBS
    media_dir = tmp_path / ".ycode" / "canvas" / "media"
    assert not list(media_dir.glob("annotation-*.png"))


def test_annotated_requires_confirmed_and_valid_canvas_id(bridge, registry, tmp_path, source_png):
    """授权与画布白名单口径必须和普通改图完全一致。"""
    reply = _submit_annotated(bridge, tmp_path, annotation_png_b64="AAAA", confirmed=False)
    assert reply == {"ok": False, "msg": "改图需要用户确认"}

    reply = _submit_annotated(bridge, tmp_path, annotation_png_b64="AAAA", canvas_id="../escape")
    assert reply == {"ok": False, "msg": "非法画布 ID"}

    reply = _submit_annotated(bridge, tmp_path, annotation_png_b64="AAAA", prompt="  ")
    assert reply["ok"] is False
    assert "改图要求" in reply["msg"]

    assert registry.calls == []
    assert bridge.find_event() is None
    assert not gui_canvas_mixin._CANVAS_EDIT_JOBS
