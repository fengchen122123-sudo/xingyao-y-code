"""R10：语义定位（SurfaceLocator）、局部观察与协议扩展测试。

所有测试只使用 fake host / FakeSurfaceLocator / FakeImage / FakeWin32Adapter，
绝不触碰真实窗口、UI Automation、鼠标键盘或截屏。
"""

import sys
import threading
import time
import types

import pytest

from core.computer.actions import (
    COMPUTER_ACTION_TOOL_SCHEMA,
    observation_provider_projection,
    parse_computer_action,
)
from core.computer.controller import ComputerControlService
from core.computer.models import (
    ComputerAction,
    ComputerControlError,
    ComputerOwner,
)
from core.computer.surfaces import ObservationRegion, region_to_physical
from core.computer.windows_backend import WindowsComputerBackend
from tests.computer_fakes import (
    GENERIC_CAPABILITY,
    FakeComputerBackend,
    FakeImage,
    FakeSurfaceLocator,
    FakeWin32Adapter,
)


# ---------------- region_to_physical 纯函数 ----------------


def test_region_coordinate_round_trip_with_negative_origin():
    region = ObservationRegion(
        origin_x=-1000, origin_y=200, original_width=400, original_height=200,
        model_width=200, model_height=100,
    )
    assert region_to_physical(0, 0, region) == (-1000, 200)
    assert region_to_physical(199, 99, region) == (-602, 398)
    assert region_to_physical(100, 50, region) == (-800, 300)


def test_region_rejects_out_of_bounds_and_bool_without_clamping():
    region = ObservationRegion(
        origin_x=0, origin_y=0, original_width=100, original_height=100,
        model_width=100, model_height=100,
    )
    for x, y in ((100, 0), (-1, 0), (0, 100), (True, 1)):
        with pytest.raises(ComputerControlError):
            region_to_physical(x, y, region)


# ---------------- WindowsSurfaceLocator（fake host） ----------------


class _FakeHost:
    """_DefaultSurfaceHost 的纯 fake：返回配置的窗口/托盘元组。"""

    def __init__(self, windows=(), tray=()):
        self.windows = list(windows)
        self.tray = list(tray)
        self.activated = []
        self.invoked = []
        self.activate_error = None
        self.moved_bounds = {}
        self.closed = 0

    def enum_windows(self):
        return list(self.windows)

    def window_bounds(self, hwnd):
        if hwnd in self.moved_bounds:
            return self.moved_bounds[hwnd]
        for row in self.windows:
            if row[0] == hwnd:
                return row[3]
        return None

    def activate_window(self, hwnd):
        self.activated.append(hwnd)
        if self.activate_error is not None:
            raise self.activate_error

    def current_foreground(self):
        return self.windows[0][0] if self.windows else 0

    def list_tray_buttons(self):
        return list(self.tray)

    def invoke_button(self, serial):
        self.invoked.append(serial)
        if self.invoke_error is not None:
            raise self.invoke_error

    def element_bounds(self, serial):
        if serial in self.moved_bounds:
            return self.moved_bounds[serial]
        for row in self.tray:
            if row[0] == serial:
                return row[2]
        return None

    def process_name(self, pid):
        return {42: "Weixin.exe"}.get(pid, "app.exe")

    def close(self):
        self.closed += 1


def _locator(host):
    from core.computer.platforms.windows_surfaces import WindowsSurfaceLocator

    return WindowsSurfaceLocator(host=host)


def test_list_windows_registers_candidates_with_refs_and_bounds():
    host = _FakeHost(
        windows=[(101, "微信", 42, (100, 100, 800, 600), False)],
        tray=[(9001, "微信", (1800, 1040, 24, 24), True)],
    )
    locator = _locator(host)
    windows = locator.list_windows()
    tray = locator.list_tray_icons()
    assert len(windows) == 1 and len(tray) == 1
    window = windows[0]
    assert window.kind == "window" and "微信" in window.label and "Weixin.exe" in window.label
    assert window.bounds == (100, 100, 800, 600)
    assert window.supported_operations == ("activate", "click_point")
    icon = tray[0]
    assert icon.kind == "tray_icon" and "invoke" in icon.supported_operations
    assert icon.ref != window.ref  # 同名称候选身份也不同


def test_stale_ref_or_generation_is_rejected():
    host = _FakeHost(windows=[(101, "A", 1, (0, 0, 10, 10), False)])
    locator = _locator(host)
    candidate = locator.list_windows()[0]
    with pytest.raises(ComputerControlError) as raised:
        locator.activate_window(candidate.ref, candidate.surface_generation + 99)
    assert raised.value.code == "computer_surface_unknown"
    # 重新列表后旧 ref 失效。
    fresh = locator.list_windows()[0]
    with pytest.raises(ComputerControlError) as raised2:
        locator.activate_window(candidate.ref, candidate.surface_generation)
    assert raised2.value.code == "computer_surface_unknown"
    locator.activate_window(fresh.ref, fresh.surface_generation)
    assert host.activated == [101]


def test_activate_window_verification_failure_propagates():
    host = _FakeHost(windows=[(101, "A", 1, (0, 0, 10, 10), False)])
    host.activate_error = ComputerControlError("computer_action_failed", "系统拒绝置前台")
    locator = _locator(host)
    candidate = locator.list_windows()[0]
    with pytest.raises(ComputerControlError) as raised:
        locator.activate_window(candidate.ref, candidate.surface_generation)
    assert raised.value.code == "computer_action_failed"


def test_invoke_requires_invoke_support():
    host = _FakeHost(tray=[(9001, "图标", (0, 0, 24, 24), False)])  # can_invoke=False
    locator = _locator(host)
    candidate = locator.list_tray_icons()[0]
    assert candidate.supported_operations == ("click_point",)
    with pytest.raises(ComputerControlError) as raised:
        locator.invoke_element(candidate.ref, candidate.surface_generation)
    assert raised.value.code == "computer_invalid_action"


def test_candidate_current_bounds_detects_move_and_disappear():
    host = _FakeHost(windows=[(101, "A", 1, (0, 0, 10, 10), False)])
    locator = _locator(host)
    candidate = locator.list_windows()[0]
    assert locator.current_candidate_bounds(candidate.ref, candidate.surface_generation) == (0, 0, 10, 10)
    host.moved_bounds[101] = (50, 50, 10, 10)
    assert locator.current_candidate_bounds(candidate.ref, candidate.surface_generation) == (50, 50, 10, 10)
    host.moved_bounds[101] = None
    assert locator.current_candidate_bounds(candidate.ref, candidate.surface_generation) is None


def test_closed_locator_rejects_everything():
    locator = _locator(_FakeHost())
    locator.close()
    with pytest.raises(ComputerControlError) as raised:
        locator.list_windows()
    assert raised.value.code == "computer_surfaces_unavailable"


# ---------------- 动作解析 ----------------


def test_parse_accepts_semantic_desktop_actions():
    action = parse_computer_action({"kind": "list_surfaces", "target": "desktop"})
    assert action.kind == "list_surfaces"
    activate = parse_computer_action(
        {"kind": "activate_surface", "target": "desktop",
         "surface_ref": "surface-1-window-1", "surface_generation": 3}
    )
    assert activate.surface_ref == "surface-1-window-1"
    assert activate.surface_generation == 3
    region = parse_computer_action(
        {"kind": "observe_region", "target": "desktop",
         "surface_ref": "r", "surface_generation": 0}
    )
    assert region.surface_ref == "r"


@pytest.mark.parametrize("payload", [
    {"kind": "list_surfaces", "target": "browser"},          # desktop only
    {"kind": "activate_surface", "target": "browser",
     "surface_ref": "r", "surface_generation": 0},
    {"kind": "activate_surface", "target": "desktop"},       # 缺 ref/generation
    {"kind": "activate_surface", "target": "desktop", "surface_ref": "r"},
    {"kind": "activate_surface", "target": "desktop",
     "surface_ref": "", "surface_generation": 0},
    {"kind": "observe_region", "target": "desktop",
     "surface_ref": "r", "surface_generation": True},
    {"kind": "observe_region", "target": "desktop",
     "surface_ref": "r", "surface_generation": -1},
    {"kind": "list_surfaces", "target": "desktop", "x": 1},  # 未知字段
])
def test_parse_rejects_invalid_semantic_actions(payload):
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action(payload)
    assert raised.value.code == "computer_invalid_action"


def test_schema_declares_semantic_actions():
    properties = COMPUTER_ACTION_TOOL_SCHEMA["function"]["parameters"]["properties"]
    kinds = properties["kind"]["enum"]
    for kind in ("list_surfaces", "activate_surface", "observe_region"):
        assert kind in kinds
    assert "surface_ref" in properties and "surface_generation" in properties


# ---------------- 桌面 backend 语义动作 ----------------


def _desktop_backend(locator=None, adapter=None):
    adapter = adapter or FakeWin32Adapter(virtual_rect=(0, 0, 1280, 720))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1280, 720),
        win32_adapter=adapter,
        surface_locator=locator,
    )
    backend.observe("run-a")
    return backend, adapter


def test_list_surfaces_candidates_ride_on_next_observation():
    locator = FakeSurfaceLocator(
        windows=[{"label": "微信（Weixin.exe）", "bounds": (100, 100, 800, 600)}],
        tray_icons=[{"label": "微信", "bounds": (1180, 680, 24, 24),
                     "operations": ("invoke", "click_point")}],
    )
    backend, _adapter = _desktop_backend(locator)
    backend.execute("run-a", ComputerAction(kind="list_surfaces", target="desktop"))
    observation = backend.observe("run-a")
    labels = [candidate["label"] for candidate in observation.surface_candidates]
    assert any("微信" in label for label in labels)
    kinds = {candidate["kind"] for candidate in observation.surface_candidates}
    assert kinds == {"window", "tray_icon"}
    # 普通观察不再携带候选。
    assert backend.observe("run-a").surface_candidates == ()


def test_activate_surface_routes_to_locator_with_generation():
    locator = FakeSurfaceLocator(windows=[{"label": "微信", "bounds": (0, 0, 100, 100)}])
    backend, _adapter = _desktop_backend(locator)
    backend.execute("run-a", ComputerAction(kind="list_surfaces", target="desktop"))
    observation = backend.observe("run-a")
    candidate = observation.surface_candidates[0]
    action = parse_computer_action(
        {"kind": "activate_surface", "target": "desktop",
         "surface_ref": candidate["ref"],
         "surface_generation": candidate["surface_generation"]}
    )
    backend.execute("run-a", action)
    assert locator.activate_calls == [(candidate["ref"], candidate["surface_generation"])]


def test_observe_region_crops_original_resolution_and_maps_coordinates():
    locator = FakeSurfaceLocator(
        tray_icons=[{"label": "微信", "bounds": (1000, 600, 48, 48),
                     "operations": ("invoke", "click_point")}],
    )
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1280, 720))
    backend, _ = _desktop_backend(locator, adapter)
    backend.execute("run-a", ComputerAction(kind="list_surfaces", target="desktop"))
    observation = backend.observe("run-a")
    candidate = observation.surface_candidates[0]
    backend.execute(
        "run-a",
        parse_computer_action(
            {"kind": "observe_region", "target": "desktop",
             "surface_ref": candidate["ref"],
             "surface_generation": candidate["surface_generation"]}
        ),
    )
    region_observation = backend.observe("run-a")
    # 48x48 区域保持原分辨率（不再缩小到 24x24）。
    assert (region_observation.width, region_observation.height) == (48, 48)
    assert (region_observation.origin_x, region_observation.origin_y) == (1000, 600)
    assert region_observation.scale_x == 1.0
    # 区域中心点击反算回物理坐标。
    backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=24, y=24))
    assert adapter.clicks == [(1024, 624, "left")]


def test_region_baseline_invalidated_when_candidate_moves():
    locator = FakeSurfaceLocator(
        tray_icons=[{"label": "微信", "bounds": (1000, 600, 48, 48),
                     "operations": ("invoke", "click_point")}],
    )
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1280, 720))
    backend, _ = _desktop_backend(locator, adapter)
    backend.execute("run-a", ComputerAction(kind="list_surfaces", target="desktop"))
    candidate = backend.observe("run-a").surface_candidates[0]
    backend.execute(
        "run-a",
        parse_computer_action(
            {"kind": "observe_region", "target": "desktop",
             "surface_ref": candidate["ref"],
             "surface_generation": candidate["surface_generation"]}
        ),
    )
    backend.observe("run-a")
    # 候选移动：旧区域坐标作废，零输入。
    locator.bounds_override[candidate["ref"]] = (500, 300, 48, 48)
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=24, y=24))
    assert raised.value.code == "computer_surface_changed"
    assert adapter.clicks == []


def test_invoke_surface_routes_to_locator():
    locator = FakeSurfaceLocator(
        tray_icons=[{"label": "微信", "bounds": (1180, 680, 24, 24),
                     "operations": ("invoke", "click_point")}],
    )
    backend, _adapter = _desktop_backend(locator)
    backend.execute("run-a", ComputerAction(kind="list_surfaces", target="desktop"))
    candidate = backend.observe("run-a").surface_candidates[0]
    backend.execute(
        "run-a",
        parse_computer_action(
            {"kind": "invoke_surface", "target": "desktop",
             "surface_ref": candidate["ref"],
             "surface_generation": candidate["surface_generation"]}
        ),
    )
    assert locator.invoke_calls == [(candidate["ref"], candidate["surface_generation"])]


def test_semantic_actions_require_locator():
    backend, _adapter = _desktop_backend(locator=None)
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", ComputerAction(kind="list_surfaces", target="desktop"))
    assert raised.value.code == "computer_surfaces_unavailable"


def test_semantic_candidates_in_provider_projection():
    locator = FakeSurfaceLocator(
        windows=[{"label": "微信", "bounds": (1, 2, 300, 200)}],
    )
    backend, _adapter = _desktop_backend(locator)
    backend.execute("run-a", ComputerAction(kind="list_surfaces", target="desktop"))
    observation = backend.observe("run-a")
    projection = observation_provider_projection(observation)
    candidates = projection["surface_candidates"]
    assert candidates[0]["label"] == "微信"
    assert candidates[0]["bounds"] == [1, 2, 300, 200]
    assert "surface_generation" in candidates[0]
    # 无候选的普通观察投影字节形状不变。
    plain = observation_provider_projection(backend.observe("run-a"))
    assert "surface_candidates" not in plain


# ---------------- Controller 集成 ----------------


def _owner():
    return ComputerOwner(
        session_id="s", turn_id="t", session_label="测试",
        cancel_event=threading.Event(),
    )


def test_semantic_actions_flow_through_controller_and_are_no_progress_exempt():
    """list_surfaces/observe_region 经固定适配链执行，且不计入无进展。"""
    backend = FakeComputerBackend(target="desktop")
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        generation_provider=lambda: 1,
        abort_streams=lambda event: None,
    )
    owner = _owner()
    run = service.start_run(owner, GENERIC_CAPABILITY, target="desktop")
    try:
        service.execute(run.run_id, owner, ComputerAction(kind="observe", target="desktop"))
        for _ in range(4):
            service.execute(
                run.run_id, owner, ComputerAction(kind="list_surfaces", target="desktop")
            )
        assert service.snapshot().status == "running"
        assert [entry[1] for entry in backend.execute_calls] == ["list_surfaces"] * 4
    finally:
        service.stop()


# ---------------- _ComThread：初始化失败必须立即显式报错 ----------------
#
# 缺陷背景：初始化异常逸出 COM 线程后被静默吞掉，start() 白等 10 秒仍不抛错，
# call() 再把任务投进无人消费的队列、再白等 10 秒，最终对外谎报「调用超时」。
# 以下测试只注入 sys.modules 里的纯 fake，绝不触碰真实 comtypes / UIA。

# 初始化失败时的最长可接受耗时：远小于旧的 10 秒空等。
_PROMPT_FAILURE_BUDGET_SECONDS = 2.0


def _install_fake_comtypes(monkeypatch):
    """把纯 fake 的 comtypes 家族塞进 sys.modules，返回 (生命周期记录, 假 UIA 对象)。"""
    lifecycle = []
    sentinel_uia = object()

    comtypes = types.ModuleType("comtypes")
    comtypes.CoInitialize = lambda: lifecycle.append("CoInitialize")
    comtypes.CoUninitialize = lambda: lifecycle.append("CoUninitialize")

    client = types.ModuleType("comtypes.client")
    client.GetModule = lambda name: None

    def create_object(clsid, interface=None):
        return sentinel_uia

    client.CreateObject = create_object
    comtypes.client = client

    gen = types.ModuleType("comtypes.gen")
    uia_client = types.ModuleType("comtypes.gen.UIAutomationClient")

    class IUIAutomation:
        """占位接口类型：仅用于满足 `from ... import IUIAutomation`。"""

    uia_client.IUIAutomation = IUIAutomation
    gen.UIAutomationClient = uia_client
    comtypes.gen = gen

    monkeypatch.setitem(sys.modules, "comtypes", comtypes)
    monkeypatch.setitem(sys.modules, "comtypes.client", client)
    monkeypatch.setitem(sys.modules, "comtypes.gen", gen)
    monkeypatch.setitem(sys.modules, "comtypes.gen.UIAutomationClient", uia_client)
    return lifecycle, sentinel_uia


def test_com_thread_start_raises_immediately_when_import_fails(monkeypatch):
    """comtypes 导入失败：start() 必须立即报真实异常类型，而不是等 10 秒。"""
    from core.computer.platforms.windows_surfaces import _ComThread

    # sys.modules[name] = None 会让 `import name` 抛 ImportError（纯桩，无真实依赖）。
    monkeypatch.setitem(sys.modules, "comtypes", None)
    try:
        import comtypes  # noqa: F401  仅为取得真实异常类型名
        expected_type_name = "ImportError"
    except BaseException as error:  # pragma: no cover - 防御
        expected_type_name = type(error).__name__

    com = _ComThread()
    started = time.monotonic()
    try:
        with pytest.raises(ComputerControlError) as raised:
            com.start()
        elapsed = time.monotonic() - started
    finally:
        com.close()

    assert raised.value.code == "computer_surfaces_unavailable"
    message = str(raised.value)
    assert expected_type_name in message  # 带真实异常类型，例如 ModuleNotFoundError
    assert "超时" not in message  # 绝不把初始化失败伪装成调用超时
    assert elapsed < _PROMPT_FAILURE_BUDGET_SECONDS, (
        f"初始化失败必须立即返回，实际等待 {elapsed:.2f} 秒"
    )


def test_com_thread_call_fails_immediately_after_init_failure(monkeypatch):
    """初始化失败后 call() 必须立即失败，不得再空等 10 秒。"""
    from core.computer.platforms.windows_surfaces import _ComThread

    monkeypatch.setitem(sys.modules, "comtypes", None)
    com = _ComThread()
    try:
        with pytest.raises(ComputerControlError):
            com.start()
        executed = []
        started = time.monotonic()
        with pytest.raises(ComputerControlError) as raised:
            com.call(lambda uia: executed.append(uia))
        elapsed = time.monotonic() - started
    finally:
        com.close()

    assert raised.value.code == "computer_surfaces_unavailable"
    assert "超时" not in str(raised.value)
    assert executed == []  # 任务根本不应被投递到无人消费的队列
    assert elapsed < _PROMPT_FAILURE_BUDGET_SECONDS, (
        f"初始化失败后 call() 必须立即失败，实际等待 {elapsed:.2f} 秒"
    )


def test_com_thread_success_path_and_close_semantics_unchanged(monkeypatch):
    """正常初始化路径仍可用；call 正常执行、异常透传，close 释放 COM。"""
    from core.computer.platforms.windows_surfaces import _ComThread

    lifecycle, sentinel_uia = _install_fake_comtypes(monkeypatch)
    com = _ComThread()
    com.start()  # 成功路径不得抛错
    try:
        assert com.call(lambda uia: uia is sentinel_uia) is True
        assert com.call(lambda uia: "ok") == "ok"
        with pytest.raises(ZeroDivisionError):
            com.call(lambda uia: 1 / 0)
    finally:
        com.close()
    assert lifecycle == ["CoInitialize", "CoUninitialize"]
