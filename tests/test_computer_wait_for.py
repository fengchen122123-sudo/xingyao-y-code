"""wait_for 条件等待（Windows-MCP 对齐，2026-11-09）测试。

Task 1 覆盖协议层：core.computer.models / core.computer.actions 的严格解析与
schema 声明，不触碰真实 UIA 遍历、截图或轮询。

Task 2 覆盖执行层：backend 的 wait_for 分片轮询与超时错误码、只读 probe
（绝不调用 collect / 绝不替换 ref 全集）、window_active 走顶层窗口标题枚举。
全部用注入 fake：绝不真实截屏、绝不真实遍历 UIA 树、绝不真实注入输入。
"""

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
    """condition 缺省 text_exists，timeout_ms/interval_ms 取默认值；schema 同步声明。"""
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

    # 显式给出的合法值原样保留
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

    # 新增字段对既有动作保持默认（None），不改变现有语义
    observe = parse_computer_action({"kind": "observe", "target": "desktop"})
    assert observe.condition is None
    assert observe.timeout_ms is None
    assert observe.interval_ms is None

    # schema 同步暴露 wait_for 与四个新字段（condition 为枚举，数值带边界）
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
    """text_exists 缺 text；window_active 缺 window_name；text 超长拒绝。"""
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


def test_parse_wait_for_rejects_browser_target():
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action({
            "kind": "wait_for",
            "target": "browser",
            "text": "加载完成",
        })
    assert raised.value.code == "computer_invalid_action"
    assert "desktop" in raised.value.message


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


# ---------------- Task 2：执行层（只读 probe 与分片轮询） ----------------


class _WaitAdapter(FakeWin32Adapter):
    """FakeWin32Adapter + list_top_window_titles（wait_for window_active 用）。"""

    def __init__(self, top_titles=(), **kwargs):
        super().__init__(**kwargs)
        self.top_titles = tuple(top_titles)
        self.top_title_calls = 0

    def list_top_window_titles(self):
        self.top_title_calls += 1
        return self.top_titles


class _FakeElementLocator:
    """元素定位器 fake：probe 按脚本命中，collect/invalidate 一旦被调用即留痕。

    ``elements`` 模拟 locator 层的 ref 全集；``elements_id`` 记录字典对象身份，
    用于断言等待期间 ref 表既没有被替换、也没有被改写（el-N 仍然可用）。
    """

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
        self.elements = {}  # 真实 collect 会整体替换 ref 全集
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
    """构造合法 wait_for 动作；payload 按 condition 只带该条件的字段。"""
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
    """条件永不满足：等满 timeout_ms 后抛 computer_wait_timeout，提示改用 observe。"""
    locator = _FakeElementLocator()  # probe 永远不命中
    backend = _wait_backend(element_locator=locator)
    started = time.monotonic()
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-wait", _wait_action())
    elapsed = time.monotonic() - started
    assert raised.value.code == "computer_wait_timeout"
    assert "observe" in raised.value.message
    assert 0.9 <= elapsed < 5  # 真等满 1000ms，不是立刻失败
    assert len(locator.probe_calls) >= 2  # 按 interval_ms 分片轮询多次
    # 只读：轮询期间绝不 collect（会整体替换 ref 全集），也不 invalidate。
    assert locator.collect_calls == 0
    assert locator.invalidate_calls == 0


def test_wait_for_probe_hit_keeps_element_refs_untouched():
    """probe 命中即返回；ref 表的对象身份与内容都不变（el-N 不失效）。"""
    locator = _FakeElementLocator([{"condition": "text_exists", "name": "保存成功"}])
    backend = _wait_backend(element_locator=locator)
    snapshot = dict(locator.elements)
    started = time.monotonic()
    backend.execute("run-wait", _wait_action())
    assert time.monotonic() - started < 0.9  # 首轮命中，不空等
    assert locator.collect_calls == 0
    assert locator.invalidate_calls == 0
    assert id(locator.elements) == locator.elements_id
    assert locator.elements == snapshot == {"el-1": None, "el-2": None}
    assert locator.probe_calls == [
        (1001, {"condition": "text_exists", "text": "保存成功", "element_ref": None})
    ]


def test_wait_for_window_active_uses_top_window_titles():
    """window_active 走 adapter.list_top_window_titles（大小写不敏感子串），不碰 UIA。"""
    adapter = _WaitAdapter(
        top_titles=("设置", "WeiXin 微信"), virtual_rect=(0, 0, 1920, 1080)
    )
    locator = _FakeElementLocator()
    backend = _wait_backend(adapter=adapter, element_locator=locator)
    backend.execute(
        "run-wait", _wait_action(condition="window_active", window_name="微信")
    )
    assert adapter.top_title_calls >= 1
    assert locator.probe_calls == []  # 窗口条件不走元素探测
    # 没有匹配窗口时不会误判成功：等满超时抛 computer_wait_timeout。
    other = _WaitAdapter(top_titles=("设置",), virtual_rect=(0, 0, 1920, 1080))
    backend_other = _wait_backend(adapter=other, element_locator=locator)
    with pytest.raises(ComputerControlError) as raised:
        backend_other.execute(
            "run-wait", _wait_action(condition="window_active", window_name="微信")
        )
    assert raised.value.code == "computer_wait_timeout"


def test_wait_for_element_condition_goes_through_probe_with_stale_ref():
    """element_enabled 走 probe 转发；stale ref 直接上抛，不静默空等。"""
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
    """停止事件已置位时立刻 computer_cancelled，绝不空转到超时。"""
    locator = _FakeElementLocator()
    backend = _wait_backend(element_locator=locator)
    backend._stop_event.set()
    started = time.monotonic()
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-wait", _wait_action())
    assert raised.value.code == "computer_cancelled"
    assert time.monotonic() - started < 0.5
    assert locator.probe_calls == []


# ---------------- Task 2：RealUIAHost.probe 只读性（硬约束，绝不启动 COM） ----------------


class _FakeUIAElement:
    """UIA 元素的最小 fake：只暴露 probe 需要的两个 Current 属性。"""

    def __init__(self, name="保存", enabled=True):
        self.CurrentName = name
        self.CurrentIsEnabled = enabled

    def GetCurrentPattern(self, pattern_id):
        raise RuntimeError("fake 元素不支持 Value pattern")


def test_real_uia_probe_element_conditions_are_read_only():
    """probe 只从 self._elements 读取：映射对象身份不变、内容不变、编号不重排。"""
    from core.computer.platforms.windows_elements_uia import RealUIAHost

    host = RealUIAHost.__new__(RealUIAHost)  # 不启动 COM 线程，不触碰真实 UIA
    host._elements = {"el-1": _FakeUIAElement(name="保存", enabled=False)}
    snapshot = dict(host._elements)
    snapshot_id = id(host._elements)

    assert host._probe_job(
        None, 1001, {"condition": "element_exists", "element_ref": "el-1"}
    ) == {"condition": "element_exists", "ref": "el-1", "name": "保存"}
    # enabled=False 未命中，但同样不改映射
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
    """UIA 树节点 fake：名称/类型 + 父子关系，Value pattern 一律不支持。"""

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
    """IUIAutomation 的最小 fake：ElementFromHandle + ControlViewWalker。"""

    def __init__(self, root):
        self._root = root
        self.ControlViewWalker = _FakeUIAWalker()

    def ElementFromHandle(self, hwnd):
        return self._root

    def GetProgrammaticName(self, type_id):
        return "ControlType.Text"


def test_real_uia_probe_text_exists_matches_child_name_case_insensitively():
    """text_exists 用 ControlViewWalker BFS 匹配 CurrentName；摘要不带 ref。"""
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
    assert "ref" not in hit  # 摘要不携带可操作引用，与 ref 生命周期解耦
    assert id(host._elements) == snapshot_id and host._elements == snapshot
    assert (
        host._probe_job(
            _FakeUIA(root), 1001, {"condition": "text_exists", "text": "不存在的文本"}
        )
        is None
    )


def test_real_uia_probe_text_exists_also_matches_value_pattern(monkeypatch):
    """ValuePattern 当前值也参与匹配（COM 读值在 pytest 内用 seam 替身注入）。"""
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


# ---------------- Task 2：locator 转发与 Win32 标题枚举 ----------------


def test_locator_probe_gates_stale_ref_and_keeps_ref_table():
    """locator.probe 先做 ref 成员门禁再转发；全程不替换 locator 的 ref 表。"""
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
    assert len(forwarded) == 1  # stale 在转发前就被拦下


def test_win32_adapter_list_top_window_titles_only_returns_non_empty_titles():
    adapter = Win32InputAdapter(
        list_top_window_titles=lambda: ("记事本", "", None, "微信")
    )
    assert adapter.list_top_window_titles() == ("记事本", "微信")

    def _boom():
        raise RuntimeError("EnumWindows 失败")

    # 枚举失败 best-effort 归一为空元组（等待会继续轮询到超时），绝不抛出。
    assert Win32InputAdapter(list_top_window_titles=_boom).list_top_window_titles() == ()