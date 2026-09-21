"""E1 浏览器元素收集（fake 层）测试（设计规格 2、9；验收清单阶段 2 · E1）。

真浏览器 JS 行为（checkVisibility 过滤、DOM 打标、label/innerText 解析、
200/80 上限等）由 tests/test_computer_browser_elements_js.py 覆盖；本文件只
使用 tests.computer_fakes 的纯 fake，绝不启动真实浏览器。
"""

import json
import logging
from pathlib import Path

import pytest

from core.computer.actions import parse_computer_action
from core.computer.browser_backend import (
    BrowserComputerBackend,
    ELEMENT_COLLECT_SCRIPT,
)
from core.computer.browser_worker_protocol import (
    normalize_observation_payload,
    observation_payload,
)
from core.computer.models import ComputerControlError, ComputerObservation
from tests.computer_fakes import FakePlaywrightRuntime, fake_png_bytes

_ELEMENT = {
    "ref": "el-1",
    "kind": "button",
    "name": "提交",
    "bounds": [10, 20, 30, 40],
    "enabled": True,
}


@pytest.fixture
def runtime():
    return FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(7))


@pytest.fixture
def backend(tmp_path, runtime):
    instance = BrowserComputerBackend(
        "edge", tmp_path / "edge", playwright_factory=lambda: runtime
    )
    instance.start()
    yield instance
    instance.close()


# ---------------- ① observe 填充 elements 与 ref 集合 ----------------


def test_observe_fills_elements_and_refs(backend):
    backend.active_page.element_collect_result = [
        dict(_ELEMENT),
        {"ref": "el-2", "kind": "textbox", "name": "用户名",
         "bounds": [10, 50, 200, 70], "enabled": False},
    ]
    observation = backend.observe("run-a")
    assert [item["ref"] for item in observation.elements] == ["el-1", "el-2"]
    # bounds 必须是 list 形态（对齐桌面 windows_elements_uia 脱敏 dict 形状）
    assert observation.elements[0]["bounds"] == [10, 20, 30, 40]
    assert isinstance(observation.elements[0]["bounds"], list)
    assert observation.elements[1]["enabled"] is False
    assert backend._observed_element_refs == frozenset({"el-1", "el-2"})


def test_observe_invokes_collect_script(backend):
    backend.observe("run-a")
    assert ELEMENT_COLLECT_SCRIPT in backend.active_page.evaluate_calls


def test_collect_normalizes_field_shapes(backend):
    """字段防御：非 dict 项/坏 ref 丢弃，str/bool/int 归一，name 截断 80。"""
    backend.active_page.element_collect_result = [
        "not-a-dict",
        {"ref": 5, "kind": "button"},
        {"ref": "el-1", "kind": 7, "name": None,
         "bounds": (1.2, 2.3, 3.4, 4.5), "enabled": "true"},
        {"ref": "el-2", "kind": "link", "name": "长" * 100,
         "bounds": [1, 2, 3], "enabled": True},
    ]
    observation = backend.observe("run-a")
    assert [item["ref"] for item in observation.elements] == ["el-1", "el-2"]
    first, second = observation.elements
    assert first == {"ref": "el-1", "kind": "", "name": "",
                     "bounds": [1, 2, 3, 4], "enabled": True}
    assert len(second["name"]) == 80
    assert second["bounds"] == [0, 0, 0, 0]


# ---------------- ② 无元素回落截图不报错（T-E1-9） ----------------


def test_observe_without_elements_falls_back_to_screenshot(backend):
    observation = backend.observe("run-a")
    assert observation.elements == ()
    assert backend._observed_element_refs == frozenset()
    # 截图路径仍然完整（canvas 页面回落语义，产品决定）
    assert observation.png_path
    assert Path(observation.png_path).read_bytes() == fake_png_bytes(7)


# ---------------- ③ 收集失败不中断（元素不可用回落是产品决定） ----------------


def test_collect_failure_does_not_break_observe(backend, caplog):
    backend.active_page.element_collect_error = RuntimeError("injected js failure")
    with caplog.at_level(logging.WARNING, logger="core.computer.browser_backend"):
        observation = backend.observe("run-a")
    assert observation.ok is True
    assert observation.elements == ()
    assert backend._observed_element_refs == frozenset()
    assert any("元素收集失败" in record.message for record in caplog.records)


def test_collect_non_list_result_does_not_break_observe(backend, caplog):
    backend.active_page.element_collect_result = "not-a-list"
    with caplog.at_level(logging.WARNING, logger="core.computer.browser_backend"):
        observation = backend.observe("run-a")
    assert observation.ok is True
    assert observation.elements == ()


def test_observe_discards_elements_when_page_changes_during_collect(backend):
    """收集期间页面变化（同页导航）：元素与页面身份快照不同源 → 清空回落截图。

    对齐「截图后复核」语义（E1 评审修复）：收集完成后复核页面 token 与 URL，
    不一致时元素整体作废，但截图与 URL 快照仍按既有语义返回。
    """
    page = backend.active_page
    page.url = "https://example.com/step-1"
    page.element_collect_result = [dict(_ELEMENT)]

    def _navigate_during_collect(fake_page):
        fake_page.url = "https://example.com/step-2"

    page.element_collect_hook = _navigate_during_collect
    observation = backend.observe("run-a")
    assert observation.elements == ()
    assert backend._observed_element_refs == frozenset()
    # 元素不可用回落截图路径（产品决定）：截图与 URL 快照仍完整
    assert observation.png_path
    assert observation.page_origin == "https://example.com"


# ---------------- ④ 确定性（T-E1-6） ----------------


def test_elements_serialization_is_deterministic(backend):
    backend.active_page.element_collect_result = [dict(_ELEMENT)]
    first = backend.observe("run-a")
    second = backend.observe("run-a")
    assert json.dumps(first.elements, ensure_ascii=False) == json.dumps(
        second.elements, ensure_ascii=False
    )


# ---------------- ⑤ 隐私静态检查（T-E1-7） ----------------


@pytest.mark.parametrize(
    "forbidden", [".value", "cookie", "localStorage", "sessionStorage"]
)
def test_collect_script_never_touches_private_values(forbidden):
    assert forbidden not in ELEMENT_COLLECT_SCRIPT


# ---------------- ⑥ 投影契约：有 elements 走纯文本路径 ----------------


def test_elements_observation_projects_as_text_only():
    from core.computer.provider_generic import _observation_followup_message

    observation = ComputerObservation(
        run_id="run-a",
        target="browser",
        elements=({"ref": "el-1", "kind": "button", "name": "提交",
                   "bounds": [1, 2, 3, 4], "enabled": True},),
    )
    # png_path 为空：若误走截图路径会因读文件失败而抛错，本测试即回归保护。
    message = _observation_followup_message(observation, run_id="run-a", ordinal=1)
    assert all(block["type"] == "text" for block in message["content"])
    assert not any(block["type"] == "image_url" for block in message["content"])
    assert "el-1" in message["content"][-1]["text"]


# ---------------- ⑦ worker 协议归一化（T-E1-8） ----------------


def test_observation_payload_roundtrip_restores_elements_tuple():
    observation = ComputerObservation(
        run_id="run-a",
        target="browser",
        elements=({"ref": "el-1", "kind": "button", "name": "提交",
                   "bounds": [1, 2, 3, 4], "enabled": True},),
    )
    payload = json.loads(
        json.dumps(observation_payload(observation), ensure_ascii=False)
    )
    assert isinstance(payload["elements"], list)  # JSON 往返：tuple → list
    restored = ComputerObservation(**normalize_observation_payload(payload))
    assert isinstance(restored.elements, tuple)
    assert restored.elements[0]["ref"] == "el-1"
    assert restored.elements[0]["bounds"] == [1, 2, 3, 4]
    assert isinstance(restored.elements[0]["bounds"], list)
    assert restored.page_origin == observation.page_origin
    assert restored.surface_generation == observation.surface_generation


def test_normalize_observation_payload_defends_bad_shapes():
    assert normalize_observation_payload({"run_id": "r"})["elements"] == ()
    normalized = normalize_observation_payload({
        "run_id": "r",
        "elements": [
            "not-a-dict",
            {"ref": 5},
            {"ref": "el-1", "kind": 7, "name": "长" * 100,
             "bounds": "bad", "enabled": "yes"},
        ],
    })
    assert normalized["elements"] == ({
        "ref": "el-1", "kind": "", "name": "长" * 80,
        "bounds": [0, 0, 0, 0], "enabled": True,
    },)


# ---------------- ⑧ 两次不同注入时 refs 正确更新 ----------------


def test_refs_replace_on_each_observation(backend):
    page = backend.active_page
    page.element_collect_result = [
        dict(_ELEMENT),
        {"ref": "el-2", "kind": "link", "name": "下一页",
         "bounds": [0, 0, 1, 1], "enabled": True},
    ]
    backend.observe("run-a")
    assert backend._observed_element_refs == frozenset({"el-1", "el-2"})
    page.element_collect_result = [
        {"ref": "el-3", "kind": "button", "name": "新按钮",
         "bounds": [0, 0, 1, 1], "enabled": True},
    ]
    backend.observe("run-a")
    assert backend._observed_element_refs == frozenset({"el-3"})


def test_refs_clear_when_collection_returns_empty(backend):
    """页面变化后元素消失：旧 ref 全部失效，不保留幽灵引用。"""
    page = backend.active_page
    page.element_collect_result = [dict(_ELEMENT)]
    backend.observe("run-a")
    assert backend._observed_element_refs == frozenset({"el-1"})
    page.element_collect_result = []
    backend.observe("run-a")
    assert backend._observed_element_refs == frozenset()


# ---------------- ⑨ E2 元素引用动作（设计 3；验收清单阶段 3 · E2） ----------------


def _prepare_element(backend, ref="el-1", *, kind="button", name="提交"):
    """observe 注入一个元素，建立 E2 动作前置状态（ref 集合 + token + URL）。"""
    backend.active_page.element_collect_result = [
        {"ref": ref, "kind": kind, "name": name,
         "bounds": [10, 20, 30, 40], "enabled": True},
    ]
    backend.observe("run-a")
    return ref


def _click_payload(ref="el-1"):
    return {"kind": "click_element", "target": "browser", "element_ref": ref}


def _set_value_payload(ref="el-1", text="中文测试"):
    return {"kind": "set_value_element", "target": "browser",
            "element_ref": ref, "text": text}


def test_click_element_hits_locator_selector(backend):
    """click_element 命中：observe 后按 ref 构造 locator 并点击（含 timeout）。

    动作经 parse_computer_action 构造，确保「白名单 → 解析 → backend 分派」
    全链路连通（E2 协议变更的回归保护）。
    """
    _prepare_element(backend)
    backend.execute("run-a", parse_computer_action(_click_payload()))
    page = backend.active_page
    assert page.locator_calls == ['[data-ycode-ref="el-1"]']
    assert len(page.last_locator.click_calls) == 1
    # 元素动作超时取 _ELEMENT_ACTION_TIMEOUT_MS（明显小于默认 30s 命令预算）
    assert page.last_locator.click_calls[0]["timeout"] == 10_000


def test_set_value_element_fill_success(backend):
    """set_value_element 主路径：fill 直接设值（中文不过输入法）。"""
    _prepare_element(backend, kind="textbox", name="用户名")
    backend.execute("run-a", parse_computer_action(_set_value_payload()))
    page = backend.active_page
    assert page.locator_calls == ['[data-ycode-ref="el-1"]']
    assert page.last_locator.fill_calls[0][0] == "中文测试"
    assert page.last_locator.fill_calls[0][1]["timeout"] == 10_000
    # fill 主路径成功即返回：不触发键盘回落
    assert page.last_locator.click_calls == []
    assert page.keyboard.calls == []


def test_set_value_element_falls_back_to_keyboard(backend):
    """fill 不支持的控件（contenteditable/富文本）：点击聚焦 + 键盘输入。"""
    _prepare_element(backend, kind="textbox", name="正文")
    page = backend.active_page
    page.locator_fill_error = RuntimeError("injected fill failure")
    backend.execute("run-a", parse_computer_action(_set_value_payload()))
    assert page.last_locator.fill_calls[0][0] == "中文测试"  # fill 先被尝试
    assert len(page.last_locator.click_calls) == 1  # 回落：点击聚焦
    assert page.last_locator.click_calls[0]["timeout"] == 10_000
    assert ("type", "中文测试") in page.keyboard.calls  # 回落：键盘逐字符输入


def test_click_element_failure_is_element_unsupported(backend):
    """元素存在但不可点击（被遮挡/禁用/超时）：元素语义错误码，非命令超时。"""
    _prepare_element(backend)
    page = backend.active_page
    page.locator_click_error = RuntimeError("injected click failure")
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-a", parse_computer_action(_click_payload()))
    assert exc.value.code == "computer_element_unsupported"
    assert "点击失败" in exc.value.message


def test_click_element_missing_after_reload_is_stale(backend):
    """同 URL 整页重载/DOM 重建：三项校验看不出，locator 命中 0 → stale。

    E2 评审修复：元素不在 DOM 时报 computer_element_stale 引导重新 observe，
    而不是等 Playwright 超时误报 unsupported（白等且误导模型）。
    """
    _prepare_element(backend)
    page = backend.active_page
    page.locator_click_error = RuntimeError("element is not attached to the DOM")
    page.locator_count = 0
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-a", parse_computer_action(_click_payload()))
    assert exc.value.code == "computer_element_stale"
    assert "不存在" in exc.value.message


def test_set_value_element_fallback_failure_is_unsupported(backend):
    """回落（点击 + 键盘）仍失败：与 click 失败语义对齐的元素错误码。"""
    _prepare_element(backend, kind="textbox", name="正文")
    page = backend.active_page
    page.locator_fill_error = RuntimeError("injected fill failure")
    page.locator_click_error = RuntimeError("injected click failure")
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-a", parse_computer_action(_set_value_payload()))
    assert exc.value.code == "computer_element_unsupported"
    assert "写入失败" in exc.value.message
    # 回落序列完整尝试后才报错：click 被调用、键盘未到达
    assert len(page.last_locator.click_calls) == 1
    assert page.keyboard.calls == []


def test_stale_ref_not_in_observed_set(backend):
    """ref 不在最近一次 observe 集合内：computer_element_stale。"""
    _prepare_element(backend)  # 集合 = {el-1}
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-a", parse_computer_action(_click_payload("el-9")))
    assert exc.value.code == "computer_element_stale"
    assert "已失效" in exc.value.message


def test_stale_ref_after_url_change(backend):
    """URL 对比裁决：token 不随同页导航变化，URL 变化必须让旧 ref 失效。"""
    _prepare_element(backend)
    backend.active_page.url = "https://other.example/"
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-a", parse_computer_action(_click_payload()))
    assert exc.value.code == "computer_element_stale"
    assert "地址" in exc.value.message


def test_stale_ref_after_page_switch(backend):
    """页面切换（popup/新前台页）→ 旧 ref 失效。

    白盒注入 token 直接命中 token 检查分支：simulate_popup 会改变可见性解析
    路径，而本用例只关心 _require_ref_valid 的裁决顺序（不必强求 popup）。
    """
    _prepare_element(backend)
    backend._observed_page_token = -1  # 白盒：模拟受控页已切换
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-a", parse_computer_action(_click_payload()))
    assert exc.value.code == "computer_element_stale"
    assert "页面已切换" in exc.value.message


def test_element_action_without_observe_is_stale(backend):
    """从未 observe：token 为 None，ref 校验第一项即拒绝（不静默执行）。"""
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-a", parse_computer_action(_click_payload()))
    assert exc.value.code == "computer_element_stale"


def test_coordinate_click_still_uses_surface_check(backend):
    """坐标动作与元素动作分流：URL 变化不影响坐标动作（仍走 surface 校验）。"""
    _prepare_element(backend)
    backend.active_page.url = "https://other.example/"
    backend.execute(
        "run-a",
        parse_computer_action({"kind": "click", "target": "browser", "x": 5, "y": 6}),
    )
    assert backend.active_page.mouse.calls == [("click", 5, 6, "left")]
