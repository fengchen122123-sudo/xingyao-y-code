"""E1 浏览器元素收集（fake 层）测试（设计规格 2、9；验收清单阶段 2 · E1）。

真浏览器 JS 行为（checkVisibility 过滤、DOM 打标、label/innerText 解析、
200/80 上限等）由 tests/test_computer_browser_elements_js.py 覆盖；本文件只
使用 tests.computer_fakes 的纯 fake，绝不启动真实浏览器。
"""

import json
import logging
from pathlib import Path

import pytest

from core.computer.browser_backend import (
    BrowserComputerBackend,
    ELEMENT_COLLECT_SCRIPT,
)
from core.computer.browser_worker_protocol import (
    normalize_observation_payload,
    observation_payload,
)
from core.computer.models import ComputerObservation
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
