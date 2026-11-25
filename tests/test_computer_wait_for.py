from __future__ import annotations

import time

import pytest

from core.computer.actions import COMPUTER_ACTION_TOOL_SCHEMA, parse_computer_action
from core.computer.models import ComputerAction, ComputerControlError
from core.computer.win32_adapter import Win32InputAdapter
from core.computer.windows_backend import WindowsComputerBackend
from tests.computer_fakes import FakeImage, FakeWin32Adapter


def _properties() -> dict:
    return COMPUTER_ACTION_TOOL_SCHEMA["function"]["parameters"]["properties"]


def test_parse_wait_for_defaults():
    from core.computer.actions import WAIT_FOR_CONDITIONS
    action = parse_computer_action({
        "kind": "wait_for",
        "target": "desktop",
        "text": "保存成功",
    })
    assert isinstance(action, ComputerAction)
    assert action.kind == "wait_for"
    assert action.condition == "text_exists"
    assert action.timeout_ms == 8000
    assert action.interval_ms == 300
    assert action.text == "保存成功"
    assert action.window_name is None and action.element_ref is None

    explicit = parse_computer_action({
        "kind": "wait_for",
        "target": "desktop",
        "text": "登录",
        "window_name": "登录窗口",
        "timeout_ms": 1000,
        "interval_ms": 2000,
    })
    assert (explicit.timeout_ms, explicit.interval_ms) == (1000, 2000)
    assert explicit.window_name == "登录窗口"

    observe = parse_computer_action({"kind": "observe", "target": "desktop"})
    assert observe.condition is None
    assert observe.timeout_ms is None
    assert observe.interval_ms is None

    properties = _properties()
    assert "wait_for" in properties["kind"]["enum"]
    assert properties["condition"]["enum"] == list(WAIT_FOR_CONDITIONS) == [
        "text_exists",
        "element_exists",
        "element_enabled",
        "window_active",
    ]
    assert properties["timeout_ms"]["minimum"] == 1000
    assert properties["timeout_ms"]["maximum"] == 30000
    assert properties["interval_ms"]["minimum"] == 100
    assert properties["interval_ms"]["maximum"] == 2000
    assert properties["window_name"]["maxLength"] == 200


def test_parse_wait_for_rejects_unknown_condition():
    for payload in (
        {"kind": "wait_for", "target": "desktop", "condition": "network_idle", "text": "x"},
        {"kind": "wait_for", "target": "desktop", "condition": "", "text": "x"},
        {"kind": "wait_for", "target": "desktop", "condition": True, "text": "x"},
        {"kind": "wait_for", "target": "desktop", "condition": ["text_exists"], "text": "x"},
    ):
        with pytest.raises(ComputerControlError) as raised:
            parse_computer_action(payload)
        assert raised.value.code == "computer_invalid_action"
        assert "condition" in raised.value.message


def test_parse_wait_for_rejects_missing_required_field():
    for payload in (
        {"kind": "wait_for", "target": "desktop"},
        {"kind": "wait_for", "target": "desktop", "condition": "text_exists"},
        {"kind": "wait_for", "target": "desktop", "condition": "text_exists", "text": ""},
        {"kind": "wait_for", "target": "desktop", "condition": "text_exists", "text": "长" * 201},
        {"kind": "wait_for", "target": "desktop", "condition": "window_active"},
        {"kind": "wait_for", "target": "desktop", "condition": "window_active", "window_name": ""},
    ):
        with pytest.raises(ComputerControlError) as raised:
            parse_computer_action(payload)
        assert raised.value.code == "computer_invalid_action"


def test_parse_wait_for_rejects_element_condition_without_element_ref():
    for condition in ("element_exists", "element_enabled"):
        for payload in (
            {"kind": "wait_for", "target": "desktop", "condition": condition},
            {"kind": "wait_for", "target": "desktop", "condition": condition, "element_ref": ""},
            {"kind": "wait_for", "target": "desktop", "condition": condition, "element_ref": 7},
        ):
            with pytest.raises(ComputerControlError) as raised:
                parse_computer_action(payload)
            assert raised.value.code == "computer_invalid_action"
            assert "element_ref" in raised.value.message

    ok = parse_computer_action({
        "kind": "wait_for",
        "target": "desktop",
        "condition": "element_enabled",
        "element_ref": "el-3",
    })
    assert ok.element_ref == "el-3" and ok.condition == "element_enabled"


def test_parse_wait_for_rejects_timeout_out_of_range():
    base = {"kind": "wait_for", "target": "desktop", "text": "x"}
    for extra in (
        {"timeout_ms": 999},
        {"timeout_ms": 30001},
        {"timeout_ms": True},
        {"timeout_ms": "8000"},
        {"timeout_ms": 8000.0},
        {"interval_ms": 99},
        {"interval_ms": 2001},
        {"interval_ms": False},
        {"interval_ms": None},
    ):
        with pytest.raises(ComputerControlError) as raised:
            parse_computer_action({**base, **extra})
        assert raised.value.code == "computer_invalid_action"


def test_parse_wait_for_browser_allows_same_doc_and_rejects_desktop_or_deferred():
    ok_text = parse_computer_action({
        "kind": "wait_for",
        "target": "browser",
        "condition": "text_exists",
        "text": "加载完成",
    })
    assert ok_text.target == "browser" and ok_text.condition == "text_exists"

    ok_el = parse_computer_action({
        "kind": "wait_for",
        "target": "browser",
        "condition": "element_enabled",
        "element_ref": "el-2",
    })
    assert ok_el.element_ref == "el-2" and ok_el.condition == "element_enabled"

    for bad_payload in (
        {"kind": "wait_for", "target": "browser", "condition": "window_active", "window_name": "Edge"},
        {"kind": "wait_for", "target": "browser", "condition": "text_exists", "text": "x", "window_name": "Edge"},
        {"kind": "wait_for", "target": "browser", "condition": "url_contains", "text": "x"},
        {"kind": "wait_for", "target": "browser", "condition": "network_idle", "text": "x"},
    ):
        with pytest.raises(ComputerControlError) as raised:
            parse_computer_action(bad_payload)
        assert raised.value.code == "computer_invalid_action"


def test_parse_wait_for_rejects_unknown_field():
    for extra in (
        {"duration_ms": 3000},
        {"steps": []},
        {"url": "https://example.com"},
        {"retry": 3},
    ):
        with pytest.raises(ComputerControlError) as raised:
            parse_computer_action({
                "kind": "wait_for",
                "target": "desktop",
                "text": "x",
                **extra,
            })
        assert raised.value.code == "computer_invalid_action"
        assert "wait_for" in raised.value.message


class _WaitAdapter(FakeWin32Adapter):

    def __init__(self, top_titles=(), **kwargs):
        super().__init__(**kwargs)
        self.top_titles = tuple(top_titles)
        self.top_title_calls = 0

    def list_top_window_titles(self):
        self.top_title_calls += 1
        return self.top_titles


class _FakeElementLocator:

    def __init__(self, probe_results=()):
        self.probe_results = list(probe_results)
        self.probe_calls = []
        self.collect_calls = 0
        self.invalidate_calls = 0
        self.elements = {"el-1": None, "el-2": None}
        self.elements_id = id(self.elements)

    def probe(self, hwnd, matcher):
        self.probe_calls.append((hwnd, dict(matcher)))
        if not self.probe_results:
            return None
        result = self.probe_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def collect(self, hwnd):
        self.collect_calls += 1
        self.elements = {}
        return []

    def invalidate(self):
        self.invalidate_calls += 1
        self.elements = {}


def _wait_backend(*, adapter=None, element_locator=None):
    return WindowsComputerBackend(
        image_grab=lambda: FakeImage(width=1920, height=1080),
        win32_adapter=adapter or _WaitAdapter(virtual_rect=(0, 0, 1920, 1080)),
        element_locator=element_locator,
    )


def _wait_action(**overrides):
    payload = {
        "kind": "wait_for",
        "target": "desktop",
        "timeout_ms": 1000,
        "interval_ms": 100,
    }
    payload.update(overrides)
    if payload.get("condition", "text_exists") == "text_exists":
        payload.setdefault("text", "保存成功")
    return parse_computer_action(payload)


def test_wait_for_timeout_raises_wait_timeout_code():
    locator = _FakeElementLocator()
    backend = _wait_backend(element_locator=locator)
    started = time.monotonic()
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-wait", _wait_action())
    elapsed = time.monotonic() - started
    assert raised.value.code == "computer_wait_timeout"
    assert "observe" in raised.value.message
    assert 0.9 <= elapsed < 5
    assert len(locator.probe_calls) >= 2
    # 只读：轮询期间绝不 collect（会整体替换 ref 全集），也不 invalidate。
    assert locator.collect_calls == 0
    assert locator.invalidate_calls == 0


def test_wait_for_probe_hit_keeps_element_refs_untouched():
    locator = _FakeElementLocator([{"condition": "text_exists", "name": "保存成功"}])
    backend = _wait_backend(element_locator=locator)
    snapshot = dict(locator.elements)
    started = time.monotonic()
    backend.execute("run-wait", _wait_action())
    assert time.monotonic() - started < 0.9
    assert locator.collect_calls == 0
    assert locator.invalidate_calls == 0
    assert id(locator.elements) == locator.elements_id
    assert locator.elements == snapshot == {"el-1": None, "el-2": None}
    assert locator.probe_calls == [
        (1001, {"condition": "text_exists", "text": "保存成功", "element_ref": None})
    ]


def test_wait_for_window_active_checks_actual_foreground_hwnd_title():
    # 后台同名窗口不得命中；只有实际前台 HWND 的标题包含目标子串才命中。
    bg_only = _WaitAdapter(
        foreground_id=1001,
        foreground_title="设置",
        top_titles=("设置", "WeiXin 微信"),
        virtual_rect=(0, 0, 1920, 1080),
    )
    locator = _FakeElementLocator()
    backend_bg = _wait_backend(adapter=bg_only, element_locator=locator)
    with pytest.raises(ComputerControlError) as raised:
        backend_bg.execute(
            "run-wait",
            _wait_action(condition="window_active", window_name="微信", timeout_ms=1000, interval_ms=100),
        )
    assert raised.value.code == "computer_wait_timeout"
    assert bg_only.top_title_calls == 0

    import threading

    switching = _WaitAdapter(
        foreground_id=1001,
        foreground_title="记事本",
        top_titles=("记事本", "WeiXin 微信"),
        virtual_rect=(0, 0, 1920, 1080),
    )
    backend_switch = _wait_backend(adapter=switching, element_locator=locator)

    def switch_foreground_later():
        time.sleep(0.15)
        switching.foreground_id = 2002
        switching.foreground_title = "WeiXin 微信"

    worker = threading.Thread(target=switch_foreground_later, daemon=True)
    worker.start()
    backend_switch.execute(
        "run-wait",
        _wait_action(condition="window_active", window_name="微信", timeout_ms=2000, interval_ms=100),
    )
    worker.join(timeout=2)
    assert switching.top_title_calls == 0
    assert locator.probe_calls == []


def test_com_thread_deadline_and_cancel_reject_queued_jobs_before_execution():
    import queue as _queue
    import threading
    from core.computer.platforms.windows_surfaces import _ComThread

    com = _ComThread.__new__(_ComThread)
    com._queue = _queue.Queue()
    com._ready = threading.Event()
    com._ready.set()
    com._init_error = None
    com._uia = object()
    ran = []

    def fake_worker():
        while True:
            job = com._queue.get()
            if job is None:
                return
            job()

    com._thread = threading.Thread(target=fake_worker, daemon=True)
    com._thread.start()
    try:
        with pytest.raises(ComputerControlError) as raised:
            com.call(
                lambda _uia: ran.append("expired"),
                deadline=time.monotonic() - 0.1,
                timeout_code="computer_wait_timeout",
            )
        assert raised.value.code == "computer_wait_timeout"
        assert ran == []

        cancel = threading.Event()
        cancel.set()
        with pytest.raises(ComputerControlError) as raised_cancel:
            com.call(lambda _uia: ran.append("cancelled"), cancel_event=cancel)
        assert raised_cancel.value.code == "computer_cancelled"
        assert ran == []
    finally:
        com.close()


def test_wait_for_discards_late_probe_hit_after_deadline():
    class _SlowHitLocator(_FakeElementLocator):
        def probe(self, hwnd, matcher, deadline=None, cancel_event=None):
            assert deadline is not None
            time.sleep(0.15)
            return {"condition": "text_exists", "name": "保存成功"}

    backend = _wait_backend(element_locator=_SlowHitLocator())
    action = ComputerAction(
        kind="wait_for",
        target="desktop",
        condition="text_exists",
        text="保存成功",
        timeout_ms=50,
        interval_ms=50,
    )
    with pytest.raises(ComputerControlError) as raised:
        backend._wait_for_interruptible(1001, action)
    assert raised.value.code == "computer_wait_timeout"


def test_wait_for_element_condition_goes_through_probe_with_stale_ref():
    locator = _FakeElementLocator(
        [
            ComputerControlError(
                "computer_element_stale", "元素引用已失效，请重新 list_elements"
            )
        ]
    )
    backend = _wait_backend(element_locator=locator)
    with pytest.raises(ComputerControlError) as raised:
        backend.execute(
            "run-wait",
            _wait_action(condition="element_enabled", element_ref="el-1"),
        )
    assert raised.value.code == "computer_element_stale"
    assert locator.probe_calls[0][1] == {
        "condition": "element_enabled",
        "text": None,
        "element_ref": "el-1",
    }
    assert locator.collect_calls == 0


def test_wait_for_stop_event_cancels_condition_wait():
    locator = _FakeElementLocator()
    backend = _wait_backend(element_locator=locator)
    backend._stop_event.set()
    started = time.monotonic()
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-wait", _wait_action())
    assert raised.value.code == "computer_cancelled"
    assert time.monotonic() - started < 0.5
    assert locator.probe_calls == []


class _FakeUIAElement:

    def __init__(self, name="保存", enabled=True):
        self.CurrentName = name
        self.CurrentIsEnabled = enabled

    def GetCurrentPattern(self, pattern_id):
        raise RuntimeError("fake 元素不支持 Value pattern")


def test_real_uia_probe_element_conditions_are_read_only():
    from core.computer.platforms.windows_elements_uia import RealUIAHost

    host = RealUIAHost.__new__(RealUIAHost)
    host._elements = {"el-1": _FakeUIAElement(name="保存", enabled=False)}
    snapshot = dict(host._elements)
    snapshot_id = id(host._elements)

    assert host._probe_job(
        None, 1001, {"condition": "element_exists", "element_ref": "el-1"}
    ) == {"condition": "element_exists", "ref": "el-1", "name": "保存"}
    assert (
        host._probe_job(
            None, 1001, {"condition": "element_enabled", "element_ref": "el-1"}
        )
        is None
    )
    assert id(host._elements) == snapshot_id
    assert host._elements == snapshot

    with pytest.raises(ComputerControlError) as raised:
        host._probe_job(
            None, 1001, {"condition": "element_exists", "element_ref": "el-9"}
        )
    assert raised.value.code == "computer_element_stale"
    assert id(host._elements) == snapshot_id
    assert host._elements == snapshot


class _FakeUIANode:

    def __init__(self, name="", children=()):
        self.CurrentName = name
        self.CurrentControlType = 50020
        self.parent = None
        self.children = []
        for child in children:
            child.parent = self
            self.children.append(child)

    def GetCurrentPattern(self, pattern_id):
        raise RuntimeError("fake 节点不支持 Value pattern")

    def next_sibling(self):
        if self.parent is None:
            return None
        index = self.parent.children.index(self) + 1
        siblings = self.parent.children
        return siblings[index] if index < len(siblings) else None


class _FakeUIAWalker:
    def GetFirstChildElement(self, node):
        return node.children[0] if node.children else None

    def GetNextSiblingElement(self, node):
        return node.next_sibling()


class _FakeUIA:

    def __init__(self, root):
        self._root = root
        self.ControlViewWalker = _FakeUIAWalker()

    def ElementFromHandle(self, hwnd):
        return self._root

    def GetProgrammaticName(self, type_id):
        return "ControlType.Text"


def test_real_uia_probe_text_exists_matches_child_name_case_insensitively():
    from core.computer.platforms.windows_elements_uia import RealUIAHost

    root = _FakeUIANode(
        "主窗口", [_FakeUIANode("工具栏"), _FakeUIANode("Save Success 已完成")]
    )
    host = RealUIAHost.__new__(RealUIAHost)
    host._elements = {"el-1": _FakeUIAElement()}
    snapshot = dict(host._elements)
    snapshot_id = id(host._elements)

    hit = host._probe_job(
        _FakeUIA(root), 1001, {"condition": "text_exists", "text": "save success"}
    )
    assert hit == {
        "condition": "text_exists",
        "kind": "TextControl",
        "name": "Save Success 已完成",
        "value": "",
    }
    assert "ref" not in hit
    assert id(host._elements) == snapshot_id and host._elements == snapshot
    assert (
        host._probe_job(
            _FakeUIA(root), 1001, {"condition": "text_exists", "text": "不存在的文本"}
        )
        is None
    )


def test_real_uia_probe_text_exists_also_matches_value_pattern(monkeypatch):
    from core.computer.platforms import windows_elements_uia as uia_module

    monkeypatch.setattr(
        uia_module, "_probe_node_texts", lambda node: ("", "进度 100%，请稍候")
    )
    host = uia_module.RealUIAHost.__new__(uia_module.RealUIAHost)
    host._elements = {}
    hit = host._probe_job(
        _FakeUIA(_FakeUIANode("窗口", [_FakeUIANode("空标签")])),
        1001,
        {"condition": "text_exists", "text": "100%"},
    )
    assert hit is not None
    assert hit["value"] == "进度 100%，请稍候"
    assert host._elements == {}


def test_locator_probe_gates_stale_ref_and_keeps_ref_table():
    from core.computer.platforms.windows_elements import WindowsElementLocator

    forwarded = []

    def probe_fn(hwnd, matcher):
        forwarded.append((hwnd, dict(matcher)))
        return {"condition": matcher["condition"], "ref": matcher["element_ref"]}

    locator = WindowsElementLocator(
        collect_fn=lambda hwnd: ([{"ref": "el-1"}], {"el-1": None}),
        probe_fn=probe_fn,
    )
    locator.collect(1001)
    ref_table = locator._elements
    snapshot = dict(ref_table)
    assert locator.probe(
        1001, {"condition": "element_exists", "element_ref": "el-1"}
    ) == {"condition": "element_exists", "ref": "el-1"}
    assert id(locator._elements) == id(ref_table)
    assert locator._elements == snapshot
    with pytest.raises(ComputerControlError) as raised:
        locator.probe(1001, {"condition": "element_exists", "element_ref": "el-2"})
    assert raised.value.code == "computer_element_stale"
    assert len(forwarded) == 1


def test_browser_wait_for_live_same_doc_conditions(tmp_path, monkeypatch):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:  # pragma: no cover
        pytest.skip(f"playwright 不可用：{error}")

    from core.computer import browser_backend as bb

    class _HeadlessRuntimeFactory:
        def __call__(self):
            return sync_playwright()

    monkeypatch.setattr(bb, "resolve_headless", lambda: True)
    instance = bb.BrowserComputerBackend(
        "edge", tmp_path / "edge-wait", playwright_factory=_HeadlessRuntimeFactory()
    )
    try:
        instance.start()
    except ComputerControlError as error:
        pytest.skip(f"headless Edge 不可用：{error}")
    try:
        def _eval_page(fn):
            return instance._call("test-eval", lambda: fn(instance.active_page))

        _eval_page(
            lambda p: p.set_content(
                "<div id='status'>准备中</div>"
                "<button id='btn' disabled>提交</button>"
                "<script>"
                "setTimeout(() => { document.getElementById('status').textContent = '已完成'; }, 250);"
                "setTimeout(() => { document.getElementById('btn').disabled = false; }, 300);"
                "</script>"
            )
        )
        obs = instance.observe("run-wait-live")
        btn_ref = next(e["ref"] for e in obs.elements if e["name"] == "提交")
        instance.execute(
            "run-wait-live",
            parse_computer_action({
                "kind": "wait_for",
                "target": "browser",
                "condition": "text_exists",
                "text": "已完成",
                "timeout_ms": 3000,
                "interval_ms": 100,
            }),
        )
        instance.execute(
            "run-wait-live",
            parse_computer_action({
                "kind": "wait_for",
                "target": "browser",
                "condition": "element_enabled",
                "element_ref": btn_ref,
                "timeout_ms": 3000,
                "interval_ms": 100,
            }),
        )
        with pytest.raises(ComputerControlError) as timeout_err:
            instance.execute(
                "run-wait-live",
                parse_computer_action({
                    "kind": "wait_for",
                    "target": "browser",
                    "condition": "text_exists",
                    "text": "永不出现",
                    "timeout_ms": 1000,
                    "interval_ms": 100,
                }),
            )
        assert timeout_err.value.code == "computer_wait_timeout"

        _eval_page(lambda p: p.goto("about:blank"))
        with pytest.raises(ComputerControlError) as stale_err:
            instance.execute(
                "run-wait-live",
                parse_computer_action({
                    "kind": "wait_for",
                    "target": "browser",
                    "condition": "element_exists",
                    "element_ref": btn_ref,
                    "timeout_ms": 1000,
                    "interval_ms": 100,
                }),
            )
        assert stale_err.value.code == "computer_element_stale"
    finally:
        instance.close()


def test_browser_wait_for_text_exists_cancels_on_close(tmp_path):
    from tests.computer_fakes import FakePlaywrightRuntime, fake_png_bytes

    runtime = FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(1))
    backend = None
    from core.computer.browser_backend import BrowserComputerBackend
    import threading

    backend = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: runtime)
    backend.start()
    try:
        backend.active_page.page_text = "等待中"
        backend.observe("run-w")
        closer = threading.Timer(0.12, backend.request_stop)
        closer.start()
        try:
            with pytest.raises(ComputerControlError) as raised:
                backend.execute(
                    "run-w",
                    parse_computer_action({
                        "kind": "wait_for",
                        "target": "browser",
                        "condition": "text_exists",
                        "text": "永远不会出现",
                        "timeout_ms": 5000,
                        "interval_ms": 100,
                    }),
                )
            assert raised.value.code == "computer_cancelled"
        finally:
            closer.join(timeout=2)
    finally:
        backend.close()


def test_win32_adapter_list_top_window_titles_only_returns_non_empty_titles():
    adapter = Win32InputAdapter(
        list_top_window_titles=lambda: ("记事本", "", None, "微信")
    )
    assert adapter.list_top_window_titles() == ("记事本", "微信")

    def _boom():
        raise RuntimeError("EnumWindows 失败")

    # 枚举失败 best-effort 归一为空元组（等待会继续轮询到超时），绝不抛出。
    assert Win32InputAdapter(list_top_window_titles=_boom).list_top_window_titles() == ()


def test_probe_node_texts_skips_value_pattern_on_non_value_controls():
    from core.computer.platforms.windows_elements_uia import _probe_node_texts

    class _StrictButton:
        CurrentName = "Build"
        CurrentControlType = 50000  # Button

        def GetCurrentPattern(self, pattern_id):
            raise AssertionError("Button 不应查询 ValuePattern")

    assert _probe_node_texts(_StrictButton()) == ("Build", "")

    class _ValuePattern:
        CurrentValue = "server"

        def QueryInterface(self, iface):
            return self

    class _EditNode:
        CurrentName = ""
        CurrentControlType = 50004  # Edit

        def GetCurrentPattern(self, pattern_id):
            return _ValuePattern()

    import sys
    import types

    comtypes = types.ModuleType("comtypes")
    client = types.ModuleType("comtypes.client")
    gen = types.ModuleType("comtypes.gen")
    uia_client = types.ModuleType("comtypes.gen.UIAutomationClient")
    uia_client.IUIAutomationValuePattern = object
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
        assert _probe_node_texts(_EditNode()) == ("", "server")
    finally:
        for k, v in old.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v