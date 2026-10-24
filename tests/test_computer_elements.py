"""UIA 元素定位与元素引用动作（提速计划阶段二 Task 9-12）。"""

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
    """绕过真实 COM：直接以 dict 模拟元素表（ref → FakeUIAElement）。"""

    def __init__(self):
        self._elements = {}
        self.click_calls = []
        self.value_calls = []
        self.collect_calls = 0

    def collect_impl(self, hwnd):
        self.collect_calls += 1
        # 第 2 代起只返回 el-1：模拟窗口变化后 el-2/el-3 过期。
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


# ---------------- Task 9：locator ----------------


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
    locator.collect(hwnd=0)  # 重新收集：fake 第 2 代起只返回 el-1，el-2 过期
    with pytest.raises(ComputerControlError) as exc:
        locator.click("el-2")
    assert exc.value.code == "computer_element_stale"


# ---------------- Task 10：解析与 schema ----------------


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
    """E2 有意协议变更：click_element / set_value_element 支持 browser target。

    变更前这两个动作是 desktop 专属；E2 后浏览器 observe 携带元素列表（el-N），
    元素动作按 ref 走 Playwright locator 通路。list_elements 仍为桌面专属：
    浏览器版职责由 observe 直接承担（设计 3.1），不提供独立动作。
    """
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
    """白名单断言：click/set_value 已移除（双 target），list_elements 仍桌面专属。"""
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


# ---------------- Task 11：backend 接线 ----------------


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
    backend.observe(run_id)  # 清空 pending elements，建立画面基线
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


# ---------------- Task 12：文本观察 followup ----------------


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
    """无元素时维持既有截图路径字节（回归保护）。"""
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


# ---------------- Task A：元素能力生产接线（惰性启动） ----------------


def test_locator_lazy_start_defers_com_thread(monkeypatch):
    """生产路径构造绝不创建 RealUIAHost（不起 COM 线程），close 前也未构造。"""
    import core.computer.platforms.windows_elements_uia as uia_module

    def _boom(*args, **kwargs):
        raise AssertionError("RealUIAHost 在惰性启动前被构造")

    monkeypatch.setattr(uia_module, "RealUIAHost", _boom)
    locator = WindowsElementLocator()
    locator.close()  # 未使用即关闭：全程未触碰 COM


def test_locator_lazy_start_constructs_on_first_collect(monkeypatch):
    """首次 collect 才惰性构造 RealUIAHost（专用 COM 线程）。"""
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
    """create_default 生产构造挂接元素定位器；backend 关闭时释放定位器。"""
    from core.computer.windows_backend import WindowsComputerBackend

    backend = WindowsComputerBackend.create_default()
    try:
        assert backend._element_locator is not None
    finally:
        backend.close()
    assert backend._element_locator._closed


def test_backend_close_releases_element_locator():
    """backend.close() 在 release_inputs 之后调用元素定位器 close（幂等留痕）。"""
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
    backend.close()  # backend 关闭幂等：不重复释放定位器
    assert locator.close_calls == 1


# ---------------- Task B：SoM 编号标注截图与元素表代数 ----------------


def test_invalidate_clears_ref_mapping():
    locator, _host = _locator_with_fake()
    locator.collect(hwnd=0)
    locator.click("el-2")
    locator.invalidate()
    with pytest.raises(ComputerControlError) as exc:
        locator.click("el-2")
    assert exc.value.code == "computer_element_stale"


def _pil_backend(locator, adapter=None):
    """真实 PIL 白屏 grab：让 SoM 标注真实执行（FakeImage 字节无法被 PIL 打开）。"""
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
    """显式 list_elements 后紧邻 observe：元素表刷新为新一代并重新标注。

    观察本身已自动携带元素（见自动补表用例），这里验证 list_elements 的显式
    结果优先——新表只含 el-1，与自动补的首个观察（3 个控件）不同，PNG 随之变化。
    """
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
    """后续 observe（无新 list_elements）：代数未变时元素表仍附加且继续标注。"""
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
    """画面代数变化：旧元素表作废（locator ref 失效）、观察重新收集当前表。

    旧契约是"代数变化即清空元素表"，自动补表后观察始终携带当前窗口元素：
    旧表绝不沿用（invalidate + 缓存清零），改为重新收集一次。
    """
    from pathlib import Path

    from tests.computer_fakes import FakeWin32Adapter

    locator, _host = _locator_with_fake()

    class _RecordingLocator:
        """包一层记录 invalidate 调用（旧 ref 失效的唯一入口）。"""

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
        # 用户切走窗口：前台身份变化 → 代数递增 → 旧表作废并重新收集
        adapter.foreground_id = 2002
        switched = backend.observe(run_id)
        assert switched.surface_generation == first.surface_generation + 1
        assert recording.invalidate_calls == 1
        # 第 2 代收集只剩 el-1：旧表不沿用，PNG 按新表重新标注
        assert [item["ref"] for item in switched.elements] == ["el-1"]
        assert Path(switched.png_path).read_bytes() != first_bytes
        with pytest.raises(ComputerControlError) as exc:
            locator.click("el-2")
        assert exc.value.code == "computer_element_stale"
        locator.click("el-1")  # 新表 ref 立即可用
    finally:
        backend.close()


# ---------------- 桌面 observe 自动携带 UIA 元素表 ----------------


def test_observe_auto_collects_elements_without_list_elements():
    """首次 observe 无需 list_elements 即携带元素表，且 ref 可直接点击。"""
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
    """元素提取失败：吞掉异常、留日志，观察照常成功且退回普通截图。"""

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
    """自动补表按上限截断，避免超大树把一次观察拖成数百条控件。"""
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
    """未挂元素定位器：观察元素表仍为空（保持既有契约）。"""
    backend = _backend_with_locator(None)
    backend.start()
    try:
        observation = backend.observe("run-auto4")
        assert observation.elements == ()
    finally:
        backend.close()
