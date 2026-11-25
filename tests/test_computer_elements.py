from __future__ import annotations

import logging

import pytest

from core.computer.models import ComputerAction, ComputerControlError
from core.computer.platforms.windows_elements import WindowsElementLocator


class FakeUIAElement:
    def __init__(self, name):
        self.name = name
        self.invoked = False
        self.value_set = None


class FakeComHost:

    def __init__(self):
        self._elements = {}
        self.click_calls = []
        self.value_calls = []
        self.collect_calls = 0

    def collect_impl(self, hwnd):
        self.collect_calls += 1
        top = 1 if self.collect_calls >= 2 else 3
        self._elements = {f"el-{n}": FakeUIAElement(f"控件{n}") for n in range(1, top + 1)}
        listing = [
            {"ref": f"el-{n}", "kind": "ButtonControl", "name": f"控件{n}",
             "bounds": [0, 0, 10, 10], "enabled": True}
            for n in range(1, top + 1)
        ]
        return listing, dict(self._elements)

    def click_impl(self, ref):
        self.click_calls.append(ref)
        self._require(ref).invoked = True

    def set_value_impl(self, ref, text):
        self.value_calls.append((ref, text))
        self._require(ref).value_set = text

    def _require(self, ref):
        if ref not in self._elements:
            raise ComputerControlError(
                "computer_element_stale", "元素引用已失效，请重新 list_elements"
            )
        return self._elements[ref]


def _locator_with_fake():
    host = FakeComHost()
    locator = WindowsElementLocator(
        com_host=host,
        collect_fn=host.collect_impl,
        click_fn=host.click_impl,
        set_value_fn=host.set_value_impl,
    )
    return locator, host


def test_collect_and_click_roundtrip():
    locator, host = _locator_with_fake()
    elements = locator.collect(hwnd=0)
    assert elements[0]["ref"] == "el-1"
    locator.click("el-2")
    assert host._elements["el-2"].invoked


def test_set_value_roundtrip():
    locator, host = _locator_with_fake()
    locator.collect(hwnd=0)
    locator.set_value("el-3", "中文内容")
    assert host._elements["el-3"].value_set == "中文内容"


def test_stale_ref_after_recollect():
    locator, _host = _locator_with_fake()
    locator.collect(hwnd=0)
    locator.collect(hwnd=0)
    with pytest.raises(ComputerControlError) as exc:
        locator.click("el-2")
    assert exc.value.code == "computer_element_stale"


def test_parse_element_actions():
    from core.computer.actions import COMPUTER_ACTION_TOOL_SCHEMA, parse_computer_action

    a = parse_computer_action(
        {"kind": "click_element", "target": "desktop", "element_ref": "el-3"}
    )
    assert a.element_ref == "el-3"
    b = parse_computer_action(
        {
            "kind": "set_value_element",
            "target": "desktop",
            "element_ref": "el-1",
            "text": "中文内容",
        }
    )
    assert b.text == "中文内容"
    assert "list_elements" in (
        COMPUTER_ACTION_TOOL_SCHEMA["function"]["parameters"]["properties"]["kind"]["enum"]
    )


def test_element_actions_allow_browser_except_list_elements():
    from core.computer.actions import parse_computer_action

    click = parse_computer_action(
        {"kind": "click_element", "target": "browser", "element_ref": "el-1"}
    )
    assert click.element_ref == "el-1"
    set_value = parse_computer_action(
        {"kind": "set_value_element", "target": "browser",
         "element_ref": "el-2", "text": "中文内容"}
    )
    assert set_value.element_ref == "el-2"
    assert set_value.text == "中文内容"
    with pytest.raises(ComputerControlError) as exc:
        parse_computer_action({"kind": "list_elements", "target": "browser"})
    assert exc.value.code == "computer_invalid_action"


def test_desktop_only_kinds_reflect_e2_protocol_change():
    from core.computer.actions import _DESKTOP_ONLY_KINDS

    assert "click_element" not in _DESKTOP_ONLY_KINDS
    assert "set_value_element" not in _DESKTOP_ONLY_KINDS
    assert "list_elements" in _DESKTOP_ONLY_KINDS


def test_summary_includes_element_ref():
    from core.computer.actions import parse_computer_action, sanitized_action_summary

    a = parse_computer_action(
        {"kind": "click_element", "target": "desktop", "element_ref": "el-3"}
    )
    assert sanitized_action_summary(a)["element_ref"] == "el-3"


def _backend_with_locator(locator):
    from core.computer.windows_backend import WindowsComputerBackend
    from tests.computer_fakes import FakeImage, FakeWin32Adapter

    return WindowsComputerBackend(
        image_grab=lambda: FakeImage(width=3840, height=2160, origin_x=-1920, origin_y=0),
        win32_adapter=FakeWin32Adapter(),
        element_locator=locator,
    )


def test_backend_list_elements_attaches_to_observation():
    locator, _host = _locator_with_fake()
    backend = _backend_with_locator(locator)
    backend.start()
    run_id = "run-el"
    backend.execute(run_id, ComputerAction(kind="list_elements", target="desktop"))
    obs = backend.observe(run_id)
    assert obs.elements and obs.elements[0]["ref"] == "el-1"
    backend.close()


def test_backend_click_element_dispatches_to_locator():
    locator, host = _locator_with_fake()
    backend = _backend_with_locator(locator)
    backend.start()
    run_id = "run-el2"
    backend.execute(run_id, ComputerAction(kind="list_elements", target="desktop"))
    backend.observe(run_id)
    backend.execute(
        run_id, ComputerAction(kind="click_element", target="desktop", element_ref="el-2")
    )
    assert host.click_calls == ["el-2"]
    backend.close()


def test_backend_set_value_element_dispatches_text():
    locator, host = _locator_with_fake()
    backend = _backend_with_locator(locator)
    backend.start()
    run_id = "run-el3"
    backend.execute(run_id, ComputerAction(kind="list_elements", target="desktop"))
    backend.observe(run_id)
    backend.execute(
        run_id,
        ComputerAction(
            kind="set_value_element", target="desktop", element_ref="el-1", text="你好"
        ),
    )
    assert host.value_calls == [("el-1", "你好")]
    backend.close()


def test_backend_element_actions_without_locator_is_stable_error():
    backend = _backend_with_locator(None)
    backend.start()
    run_id = "run-el4"
    backend.observe(run_id)
    with pytest.raises(ComputerControlError) as exc:
        backend.execute(
            run_id,
            ComputerAction(kind="click_element", target="desktop", element_ref="el-1"),
        )
    assert exc.value.code == "computer_elements_unavailable"
    backend.close()


def test_backend_list_elements_without_locator_is_stable_error():
    backend = _backend_with_locator(None)
    backend.start()
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-el5", ComputerAction(kind="list_elements", target="desktop"))
    assert exc.value.code == "computer_elements_unavailable"
    backend.close()


def test_followup_text_observation_when_elements_present():
    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import _observation_followup_message

    obs = ComputerObservation(
        run_id="r",
        target="desktop",
        elements=(
            {
                "ref": "el-1",
                "kind": "EditControl",
                "name": "输入",
                "bounds": [0, 0, 5, 5],
                "enabled": True,
            },
        ),
    )
    msg = _observation_followup_message(obs, run_id="r", ordinal=1)
    assert msg["_computer_v1"]["ordinal"] == 1
    assert all(block["type"] == "text" for block in msg["content"])
    assert "el-1" in msg["content"][-1]["text"]


def test_followup_image_path_unchanged_when_no_elements(tmp_path):
    from io import BytesIO

    from PIL import Image

    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import _observation_followup_message

    buffer = BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
    png_path = tmp_path / "shot.png"
    png_path.write_bytes(buffer.getvalue())
    obs = ComputerObservation(run_id="r", target="desktop", png_path=str(png_path))
    msg = _observation_followup_message(obs, run_id="r", ordinal=1)
    image_blocks = [b for b in msg["content"] if b.get("type") == "image_url"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_repeat_guard_exempts_list_elements():
    from core.harness.tool_pipeline import repeat_guard_exempt

    assert repeat_guard_exempt(
        "computer_action", {"kind": "list_elements", "target": "desktop"}
    )


def test_generic_prompt_mentions_elements():
    from core.computer.models import ComputerCapability
    from core.computer.provider_generic import build_generic_dynamic_prompt

    cap = ComputerCapability(enabled=True, protocol="generic", targets=("desktop",))
    prompt = build_generic_dynamic_prompt(cap)
    assert "list_elements" in prompt
    assert "set_value_element" in prompt


def test_locator_lazy_start_defers_com_thread(monkeypatch):
    import core.computer.platforms.windows_elements_uia as uia_module

    def _boom(*args, **kwargs):
        raise AssertionError("RealUIAHost 在惰性启动前被构造")

    monkeypatch.setattr(uia_module, "RealUIAHost", _boom)
    locator = WindowsElementLocator()
    locator.close()


def test_locator_lazy_start_constructs_on_first_collect(monkeypatch):
    import core.computer.platforms.windows_elements_uia as uia_module

    constructed = []

    class _StubRealUIAHost:
        def __init__(self):
            constructed.append(1)

        def collect(self, hwnd):
            return [], {}

        def click(self, ref):
            raise AssertionError("本测试不点击")

        def set_value(self, ref, text):
            raise AssertionError("本测试不写值")

    monkeypatch.setattr(uia_module, "RealUIAHost", _StubRealUIAHost)
    locator = WindowsElementLocator()
    assert constructed == []
    locator.collect(1001)
    assert constructed == [1]
    locator.close()


def test_create_default_wires_element_locator():
    from core.computer.windows_backend import WindowsComputerBackend

    backend = WindowsComputerBackend.create_default()
    try:
        assert backend._element_locator is not None
    finally:
        backend.close()
    assert backend._element_locator._closed


def test_backend_close_releases_element_locator():
    from core.computer.windows_backend import WindowsComputerBackend
    from tests.computer_fakes import FakeImage, FakeWin32Adapter

    class _RecordingLocator:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    locator = _RecordingLocator()
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(width=3840, height=2160, origin_x=-1920, origin_y=0),
        win32_adapter=FakeWin32Adapter(),
        element_locator=locator,
    )
    backend.close()
    assert locator.close_calls == 1
    backend.close()
    assert locator.close_calls == 1


def test_invalidate_clears_ref_mapping():
    locator, _host = _locator_with_fake()
    locator.collect(hwnd=0)
    locator.click("el-2")
    locator.invalidate()
    with pytest.raises(ComputerControlError) as exc:
        locator.click("el-2")
    assert exc.value.code == "computer_element_stale"


def _pil_backend(locator, adapter=None):
    from PIL import Image as PILImage

    from core.computer.windows_backend import WindowsComputerBackend
    from tests.computer_fakes import FakeWin32Adapter

    def grab():
        return PILImage.new("RGB", (3840, 2160), "white")

    return WindowsComputerBackend(
        image_grab=grab,
        win32_adapter=adapter or FakeWin32Adapter(),
        element_locator=locator,
    )


def test_list_elements_observe_refreshes_table_and_annotation():
    from pathlib import Path

    locator, _host = _locator_with_fake()
    backend = _pil_backend(locator)
    backend.start()
    run_id = "run-som"
    try:
        auto = backend.observe(run_id)
        auto_bytes = Path(auto.png_path).read_bytes()
        backend.execute(run_id, ComputerAction(kind="list_elements", target="desktop"))
        annotated = backend.observe(run_id)
        annotated_bytes = Path(annotated.png_path).read_bytes()
        assert [item["ref"] for item in auto.elements] == ["el-1", "el-2", "el-3"]
        assert annotated.elements and annotated.elements[0]["ref"] == "el-1"
        assert annotated.screen_sha256 != auto.screen_sha256
        assert annotated_bytes != auto_bytes
    finally:
        backend.close()


def test_elements_reused_while_generation_stable():
    from pathlib import Path

    locator, _host = _locator_with_fake()
    backend = _pil_backend(locator)
    backend.start()
    run_id = "run-som2"
    try:
        backend.execute(run_id, ComputerAction(kind="list_elements", target="desktop"))
        first = backend.observe(run_id)
        assert first.elements
        first_bytes = Path(first.png_path).read_bytes()
        second = backend.observe(run_id)
        assert second.elements and second.elements[0]["ref"] == "el-1"
        assert Path(second.png_path).read_bytes() == first_bytes
    finally:
        backend.close()


def test_generation_change_invalidates_old_table_and_recollects():
    from pathlib import Path

    from tests.computer_fakes import FakeWin32Adapter

    locator, _host = _locator_with_fake()

    class _RecordingLocator:

        def __init__(self, inner):
            self.inner = inner
            self.invalidate_calls = 0

        def collect(self, hwnd):
            return self.inner.collect(hwnd)

        def invalidate(self):
            self.invalidate_calls += 1
            return self.inner.invalidate()

        def click(self, ref):
            return self.inner.click(ref)

        def close(self):
            return self.inner.close()

    recording = _RecordingLocator(locator)
    adapter = FakeWin32Adapter()
    backend = _pil_backend(recording, adapter=adapter)
    backend.start()
    run_id = "run-som3"
    try:
        first = backend.observe(run_id)
        assert [item["ref"] for item in first.elements] == ["el-1", "el-2", "el-3"]
        first_bytes = Path(first.png_path).read_bytes()
        adapter.foreground_id = 2002
        switched = backend.observe(run_id)
        assert switched.surface_generation == first.surface_generation + 1
        assert recording.invalidate_calls == 1
        assert [item["ref"] for item in switched.elements] == ["el-1"]
        assert Path(switched.png_path).read_bytes() != first_bytes
        with pytest.raises(ComputerControlError) as exc:
            locator.click("el-2")
        assert exc.value.code == "computer_element_stale"
        locator.click("el-1")
    finally:
        backend.close()


def test_observe_auto_collects_elements_without_list_elements():
    locator, host = _locator_with_fake()
    backend = _pil_backend(locator)
    backend.start()
    run_id = "run-auto1"
    try:
        observation = backend.observe(run_id)
        assert [item["ref"] for item in observation.elements] == [
            "el-1",
            "el-2",
            "el-3",
        ]
        assert host.collect_calls == 1
        backend.execute(
            run_id,
            ComputerAction(kind="click_element", target="desktop", element_ref="el-2"),
        )
        assert host.click_calls == ["el-2"]
    finally:
        backend.close()


def test_observe_auto_collect_failure_does_not_block_observation(caplog):

    class _BrokenLocator:
        def collect(self, hwnd):
            raise RuntimeError("UIA 树读取失败")

        def invalidate(self):
            raise AssertionError("首次观察不应调用 invalidate")

        def close(self):
            pass

    backend = _backend_with_locator(_BrokenLocator())
    backend.start()
    try:
        with caplog.at_level(logging.ERROR):
            observation = backend.observe("run-auto2")
        assert observation.ok is True
        assert observation.elements == ()
        assert str(observation.png_path)
        assert "自动提取前台窗口元素失败" in caplog.text
    finally:
        backend.close()


def test_observe_auto_collect_caps_element_count():
    from core.computer.windows_backend import _MAX_OBSERVE_ELEMENTS

    listing = [
        {
            "ref": f"el-{n}",
            "kind": "ButtonControl",
            "name": f"控件{n}",
            "bounds": [0, 0, 10, 10],
            "enabled": True,
        }
        for n in range(1, 251)
    ]

    class _BigLocator:
        def collect(self, hwnd):
            return list(listing)

        def close(self):
            pass

    backend = _pil_backend(_BigLocator())
    backend.start()
    try:
        observation = backend.observe("run-auto3")
        assert len(observation.elements) == _MAX_OBSERVE_ELEMENTS == 200
        assert observation.elements[-1]["ref"] == "el-200"
    finally:
        backend.close()


def test_observe_without_locator_has_empty_elements():
    backend = _backend_with_locator(None)
    backend.start()
    try:
        observation = backend.observe("run-auto4")
        assert observation.elements == ()
    finally:
        backend.close()


def test_real_uia_collect_filters_offscreen_and_zero_area_and_reverifies_identity():
    import time
    from core.computer.platforms import windows_elements_uia as uia_module

    class _Rect:
        def __init__(self, left, top, right, bottom):
            self.left, self.top, self.right, self.bottom = left, top, right, bottom

    class _Node:
        def __init__(
            self,
            name,
            *,
            control_type=50000,
            bounds=(10, 10, 100, 40),
            offscreen=False,
            enabled=True,
            runtime_id=(42, 1),
            children=(),
        ):
            self.CurrentName = name
            self.CurrentControlType = control_type
            self.CurrentBoundingRectangle = _Rect(*bounds)
            self.CurrentIsOffscreen = offscreen
            self.CurrentIsEnabled = enabled
            self._runtime_id = runtime_id
            self.parent = None
            self.children = []
            for ch in children:
                ch.parent = self
                self.children.append(ch)

        def GetRuntimeId(self):
            return self._runtime_id

        def GetCurrentPattern(self, pattern_id):
            return None

        def next_sibling(self):
            if self.parent is None:
                return None
            idx = self.parent.children.index(self) + 1
            return self.parent.children[idx] if idx < len(self.parent.children) else None

    hidden = _Node("隐藏按钮", offscreen=True, bounds=(10, 10, 80, 30), runtime_id=(42, 2))
    zero_area = _Node("零面积按钮", offscreen=False, bounds=(10, 10, 10, 40), runtime_id=(42, 3))
    visible_btn = _Node("确认", offscreen=False, bounds=(20, 50, 120, 90), runtime_id=(42, 4))
    root = _Node("根窗", control_type=50032, children=[hidden, zero_area, visible_btn])

    class _Walker:
        def GetFirstChildElement(self, node):
            return node.children[0] if node.children else None

        def GetNextSiblingElement(self, node):
            return node.next_sibling()

    class _FakeUIA:
        def __init__(self):
            self.ControlViewWalker = _Walker()

        def ElementFromHandle(self, hwnd):
            return root

        def GetProgrammaticName(self, type_id):
            return "ControlType.Button"

    class _InlineCom:
        def call(self, fn, *, deadline=None, cancel_event=None, timeout_code=None):
            if deadline is not None and time.monotonic() >= deadline:
                raise ComputerControlError(timeout_code or "computer_wait_timeout", "已到期")
            return fn(_FakeUIA())

    host = uia_module.RealUIAHost.__new__(uia_module.RealUIAHost)
    host._com = _InlineCom()
    host._elements = {}
    host._element_meta = {}

    listing, mapping = host.collect(1001)
    assert [item["name"] for item in listing] == ["确认"]
    assert list(mapping) == ["el-1"]

    # 控件位置发生迁移时必须判为 stale，绝不继续执行 Pattern 或坐标点击
    visible_btn.CurrentBoundingRectangle = _Rect(220, 250, 320, 290)
    with pytest.raises(ComputerControlError) as raised_moved:
        host.click("el-1")
    assert raised_moved.value.code == "computer_element_stale"

    # 恢复位置但控件被禁用 -> 报 computer_element_unsupported（不支持 Invoke/disabled），不回退其它操作
    visible_btn.CurrentBoundingRectangle = _Rect(20, 50, 120, 90)
    visible_btn.CurrentIsEnabled = False
    with pytest.raises(ComputerControlError) as raised_disabled:
        host.click("el-1")
    assert raised_disabled.value.code == "computer_element_unsupported"

    # 到期 deadline 直接拒绝 collect
    with pytest.raises(ComputerControlError) as raised_dl:
        host.collect(1001, deadline=time.monotonic() - 0.1)
    assert raised_dl.value.code in ("computer_wait_timeout", "computer_surfaces_unavailable")


def test_real_uia_click_falls_back_to_selection_and_toggle_patterns():
    import sys
    import types
    from core.computer.platforms import windows_elements_uia as uia_module

    class _Rect:
        left, top, right, bottom = 10, 10, 80, 40

    class _Pattern:
        def __init__(self, log, label):
            self._log = log
            self._label = label

        def QueryInterface(self, _iface):
            return self

        def Select(self):
            self._log.append(("select", self._label))

        def Toggle(self):
            self._log.append(("toggle", self._label))

    class _Element:
        CurrentControlType = 50019
        CurrentBoundingRectangle = _Rect()
        CurrentIsEnabled = True
        CurrentIsOffscreen = False

        def __init__(self, supported_pattern_id, log, label):
            self._supported = supported_pattern_id
            self._log = log
            self._label = label

        def GetRuntimeId(self):
            return (1, 2)

        def GetCurrentPattern(self, pid):
            if pid == self._supported:
                return _Pattern(self._log, self._label)
            return None

    class _InlineCom:
        def call(self, fn, **kwargs):
            return fn(None)

    calls = []
    host = uia_module.RealUIAHost.__new__(uia_module.RealUIAHost)
    host._com = _InlineCom()
    host._elements = {
        "el-tab": _Element(uia_module._UIA_SELECTION_ITEM_PATTERN_ID, calls, "tab"),
        "el-chk": _Element(uia_module._UIA_TOGGLE_PATTERN_ID, calls, "chk"),
    }
    host._element_meta = {
        "el-tab": {"hwnd": 1, "control_type": 50019, "bounds": (10, 10, 80, 40), "runtime_id": (1, 2)},
        "el-chk": {"hwnd": 1, "control_type": 50019, "bounds": (10, 10, 80, 40), "runtime_id": (1, 2)},
    }

    comtypes = types.ModuleType("comtypes")
    client = types.ModuleType("comtypes.client")
    gen = types.ModuleType("comtypes.gen")
    uia_client = types.ModuleType("comtypes.gen.UIAutomationClient")
    uia_client.IUIAutomationInvokePattern = object
    uia_client.IUIAutomationSelectionItemPattern = object
    uia_client.IUIAutomationTogglePattern = object
    uia_client.IUIAutomationExpandCollapsePattern = object
    gen.UIAutomationClient = uia_client
    comtypes.client = client
    comtypes.gen = gen
    old = {k: sys.modules.get(k) for k in ("comtypes", "comtypes.client", "comtypes.gen", "comtypes.gen.UIAutomationClient")}
    sys.modules.update({
        "comtypes": comtypes,
        "comtypes.client": client,
        "comtypes.gen": gen,
        "comtypes.gen.UIAutomationClient": uia_client,
    })
    try:
        host.click("el-tab")
        host.click("el-chk")
        assert calls == [("select", "tab"), ("toggle", "chk")]
    finally:
        for k, v in old.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
