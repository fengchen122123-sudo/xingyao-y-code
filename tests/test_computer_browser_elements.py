import json
import logging
from pathlib import Path

import pytest

from core.computer.actions import parse_computer_action
from core.computer.browser_backend import (
    BrowserComputerBackend,
    ELEMENT_COLLECT_SCRIPT,
    PAGE_TEXT_SCRIPT,
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


def test_observe_without_elements_falls_back_to_screenshot(backend):
    observation = backend.observe("run-a")
    assert observation.elements == ()
    assert backend._observed_element_refs == frozenset()
    assert observation.png_path
    assert Path(observation.png_path).read_bytes() == fake_png_bytes(7)


def test_observe_without_screenshot_keeps_elements_and_page_text(backend):
    page = backend.active_page
    page.element_collect_result = [dict(_ELEMENT)]
    page.page_text = "页面正文：提交表单"
    page.screenshot_error = RuntimeError("frame not produced while occluded")
    observation = backend.observe("run-a")
    assert observation.ok is True
    assert observation.png_path == ""
    assert observation.screenshot_error
    assert [item["ref"] for item in observation.elements] == ["el-1"]
    assert backend._observed_element_refs == frozenset({"el-1"})
    assert observation.page_text == "页面正文：提交表单"


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
    page = backend.active_page
    page.url = "https://example.com/step-1"
    page.element_collect_result = [dict(_ELEMENT)]

    def _navigate_during_collect(fake_page):
        fake_page.url = "https://example.com/step-2"

    page.element_collect_hook = _navigate_during_collect
    observation = backend.observe("run-a")
    assert observation.elements == ()
    assert backend._observed_element_refs == frozenset()
    assert observation.png_path
    assert observation.page_origin == "https://example.com"


def test_elements_serialization_is_deterministic(backend):
    backend.active_page.element_collect_result = [dict(_ELEMENT)]
    first = backend.observe("run-a")
    second = backend.observe("run-a")
    assert json.dumps(first.elements, ensure_ascii=False) == json.dumps(
        second.elements, ensure_ascii=False
    )


@pytest.mark.parametrize(
    "forbidden", [".value", "cookie", "localStorage", "sessionStorage"]
)
def test_collect_script_never_touches_private_values(forbidden):
    assert forbidden not in ELEMENT_COLLECT_SCRIPT


@pytest.mark.parametrize(
    "forbidden", [".value", "cookie", "localStorage", "sessionStorage"]
)
def test_page_text_script_never_touches_private_values(forbidden):
    assert forbidden not in PAGE_TEXT_SCRIPT


def test_elements_observation_projects_as_text_only():
    from core.computer.provider_generic import _observation_followup_message

    observation = ComputerObservation(
        run_id="run-a",
        target="browser",
        elements=({"ref": "el-1", "kind": "button", "name": "提交",
                   "bounds": [1, 2, 3, 4], "enabled": True},),
    )
    message = _observation_followup_message(observation, run_id="run-a", ordinal=1)
    assert all(block["type"] == "text" for block in message["content"])
    assert not any(block["type"] == "image_url" for block in message["content"])
    assert "el-1" in message["content"][-1]["text"]


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
    assert isinstance(payload["elements"], list)
    restored = ComputerObservation(**normalize_observation_payload(payload))
    assert isinstance(restored.elements, tuple)
    assert restored.elements[0]["ref"] == "el-1"
    assert restored.elements[0]["bounds"] == [1, 2, 3, 4]
    assert isinstance(restored.elements[0]["bounds"], list)
    assert restored.page_origin == observation.page_origin
    assert restored.surface_generation == observation.surface_generation


def test_observation_payload_carries_page_text_and_screenshot_error():
    observation = ComputerObservation(
        run_id="run-a",
        target="browser",
        page_text="页面正文",
        screenshot_error="浏览器窗口不可见或被遮挡，本次未取到截图：TimeoutError",
    )
    payload = json.loads(
        json.dumps(observation_payload(observation), ensure_ascii=False)
    )
    restored = ComputerObservation(**normalize_observation_payload(payload))
    assert restored.page_text == "页面正文"
    assert "TimeoutError" in restored.screenshot_error


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
    page = backend.active_page
    page.element_collect_result = [dict(_ELEMENT)]
    backend.observe("run-a")
    assert backend._observed_element_refs == frozenset({"el-1"})
    page.element_collect_result = []
    backend.observe("run-a")
    assert backend._observed_element_refs == frozenset()


def _prepare_element(backend, ref="el-1", *, kind="button", name="提交"):
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


def test_click_element_resolves_without_dom_attribute(backend):
    _prepare_element(backend)
    backend.execute("run-a", parse_computer_action(_click_payload()))
    page = backend.active_page
    assert page.locator_calls == []
    assert len(page.last_element_handle.click_calls) == 1
    assert page.last_element_handle.click_calls[0]["timeout"] == 10_000
    assert page.last_element_handle.disposed is True


def test_click_element_uses_visual_hit_point(backend):
    _prepare_element(backend)
    page = backend.active_page
    page.element_hit_result = {
        "x": 712, "y": 469, "contains": True, "inViewport": True,
    }
    backend.execute("run-a", parse_computer_action(_click_payload()))
    assert ("click", 712, 469, "left") in page.mouse.calls
    assert page.last_element_handle.click_calls == []


def test_click_element_falls_back_when_visual_point_missed(backend):
    _prepare_element(backend)
    page = backend.active_page
    page.element_hit_result = {
        "x": 10, "y": 10, "contains": False, "inViewport": True,
    }
    backend.execute("run-a", parse_computer_action(_click_payload()))
    assert page.mouse.calls == []
    assert len(page.last_element_handle.click_calls) == 1


def test_set_value_element_fill_success(backend):
    _prepare_element(backend, kind="textbox", name="用户名")
    backend.execute("run-a", parse_computer_action(_set_value_payload()))
    page = backend.active_page
    assert page.locator_calls == []
    assert page.last_element_handle.fill_calls[0][0] == "中文测试"
    assert page.last_element_handle.fill_calls[0][1]["timeout"] == 10_000
    assert page.last_element_handle.click_calls == []
    assert page.keyboard.calls == []


def test_set_value_element_timeout_never_runs_ctrl_a_or_keyboard_fallback(backend):
    _prepare_element(backend, kind="textbox", name="正文")
    page = backend.active_page
    page.locator_fill_error = TimeoutError("fill timed out")
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-a", parse_computer_action(_set_value_payload()))
    assert exc.value.code == "computer_action_timeout"
    assert page.last_element_handle.fill_calls[0][0] == "中文测试"
    assert page.last_element_handle.click_calls == []
    assert page.keyboard.calls == []


def test_click_element_failure_is_element_unsupported(backend):
    _prepare_element(backend)
    page = backend.active_page
    page.locator_click_error = RuntimeError("injected click failure")
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-a", parse_computer_action(_click_payload()))
    assert exc.value.code == "computer_element_unsupported"
    assert "点击失败" in exc.value.message


def test_click_element_missing_after_reload_is_stale(backend):
    _prepare_element(backend)
    page = backend.active_page
    page.element_attached = False
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-a", parse_computer_action(_click_payload()))
    assert exc.value.code == "computer_element_stale"
    assert "失效" in exc.value.message or "不存在" in exc.value.message


def test_set_value_element_fill_failure_never_uses_keyboard_fallback(backend):
    _prepare_element(backend, kind="textbox", name="正文")
    page = backend.active_page
    page.locator_fill_error = RuntimeError("injected fill failure")
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-a", parse_computer_action(_set_value_payload()))
    assert exc.value.code == "computer_element_unsupported"
    assert "写入失败" in exc.value.message
    assert page.last_element_handle.click_calls == []
    assert page.keyboard.calls == []


def test_live_refs_replace_across_navigation_shadow_iframe_tabs_and_viewport(
    tmp_path, monkeypatch
):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:  # pragma: no cover
        pytest.skip(f"playwright 不可用：{error}")

    class _HeadlessRuntimeFactory:
        def __call__(self):
            return sync_playwright()

    from core.computer import browser_backend as bb

    monkeypatch.setattr(bb, "resolve_headless", lambda: True)
    instance = bb.BrowserComputerBackend(
        "edge", tmp_path / "edge-live", playwright_factory=_HeadlessRuntimeFactory()
    )
    try:
        instance.start()
    except ComputerControlError as error:
        pytest.skip(f"headless Edge 不可用：{error}")
    try:
        # Run all checks via instance.observe / instance.execute on real pages
        def _eval_page(fn):
            return instance._call("test-eval", lambda: fn(instance.active_page))

        # 1) Button moved out of viewport: stale ref & NO renumbering from 1
        _eval_page(
            lambda p: p.set_content(
                '<button id="b1">第一个</button><button id="b2">第二个</button>'
            )
        )
        obs1 = instance.observe("run-live")
        assert [(e["ref"], e["name"]) for e in obs1.elements] == [
            ("el-1", "第一个"),
            ("el-2", "第二个"),
        ]
        assert _eval_page(lambda p: p.query_selector("[data-ycode-ref]")) is None
        _eval_page(
            lambda p: p.eval_on_selector(
                "#b1",
                "el => { el.style.position='absolute'; el.style.top='9000px'; }",
            )
        )
        obs2 = instance.observe("run-live")
        assert [(e["ref"], e["name"]) for e in obs2.elements] == [("el-2", "第二个")]
        with pytest.raises(ComputerControlError) as stale_offscreen:
            instance.execute("run-live", parse_computer_action(_click_payload("el-1")))
        assert stale_offscreen.value.code == "computer_element_stale"

        # 2) Same-URL reload: old ref stale BEFORE observe, fresh ref AFTER observe
        same_url = "data:text/html,<button id='btn'>提交</button>"
        _eval_page(lambda p: p.goto(same_url))
        obs_reload_1 = instance.observe("run-live")
        ref_before_reload = obs_reload_1.elements[0]["ref"]
        instance.execute("run-live", parse_computer_action({"kind": "reload", "target": "browser"}))
        with pytest.raises(ComputerControlError) as stale_reload:
            instance.execute("run-live", parse_computer_action(_click_payload(ref_before_reload)))
        assert stale_reload.value.code == "computer_element_stale"
        obs_reload_2 = instance.observe("run-live")
        assert obs_reload_2.elements[0]["ref"] != ref_before_reload

        # 3) Duplicate-title tabs at identical URL: switching tab invalidates old ref & mints new
        def _open_dup_tab(p):
            p2 = p.context.new_page()
            p2.set_content("<title>Same Title</title><button id='tabbtn'>页签按钮</button>")
            return p2

        _eval_page(
            lambda p: p.set_content("<title>Same Title</title><button id='tabbtn'>页签按钮</button>")
        )
        obs_tab1 = instance.observe("run-live")
        tab1_ref = obs_tab1.elements[0]["ref"]
        _eval_page(_open_dup_tab)

        def _switch_tab(p):
            pages = list(p.context.pages)
            pages[0].evaluate(
                "() => { Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true }); document.hasFocus = () => false; }"
            )
            pages[-1].bring_to_front()

        _eval_page(_switch_tab)
        with pytest.raises(ComputerControlError) as stale_tab:
            instance.execute("run-live", parse_computer_action(_click_payload(tab1_ref)))
        assert stale_tab.value.code == "computer_element_stale"
        obs_tab2 = instance.observe("run-live")
        assert obs_tab2.elements[0]["ref"] != tab1_ref

        # 4) Iframe navigation without top-URL change: iframe ref becomes stale immediately
        _eval_page(
            lambda p: p.set_content(
                "<button id='topbtn'>顶层</button>"
                "<iframe id='f' srcdoc=\"<button id='ifbtn'>框内旧按钮</button>\"></iframe>"
            )
        )
        obs_iframe = instance.observe("run-live")
        iframe_ref = next(e["ref"] for e in obs_iframe.elements if e["name"] == "框内旧按钮")
        _eval_page(
            lambda p: p.eval_on_selector(
                "#f", "f => { f.srcdoc = '<button id=ifbtn2>框内新按钮</button>'; }"
            )
        )
        with pytest.raises(ComputerControlError) as stale_iframe:
            instance.execute("run-live", parse_computer_action(_click_payload(iframe_ref)))
        assert stale_iframe.value.code == "computer_element_stale"

        # 5) Open Shadow DOM & contenteditable fill without data-ycode-ref
        _eval_page(
            lambda p: (
                p.set_content(
                    "<div id='host'></div>"
                    "<div id='ce' contenteditable='true' aria-label='富文本'>初始</div>"
                ),
                p.evaluate(
                    "() => { const r = document.getElementById('host').attachShadow({mode:'open'});"
                    " r.innerHTML = '<button id=sb onclick=\"window.__sb=(window.__sb||0)+1\">影子按钮</button>'; }"
                ),
            )
        )
        obs_shadow = instance.observe("run-live")
        sb_ref = next(e["ref"] for e in obs_shadow.elements if e["name"] == "影子按钮")
        ce_ref = next(e["ref"] for e in obs_shadow.elements if e["name"] == "富文本")
        instance.execute("run-live", parse_computer_action(_click_payload(sb_ref)))
        instance.execute("run-live", parse_computer_action(_set_value_payload(ce_ref, "新富文本")))
        assert _eval_page(lambda p: p.evaluate("() => window.__sb")) == 1
        assert _eval_page(lambda p: p.inner_text("#ce")) == "新富文本"
        assert _eval_page(lambda p: p.query_selector("[data-ycode-ref]")) is None

        # 6) Viewport changes invalidate coordinate/element baseline
        obs_vp = instance.observe("run-live")
        vp_ref = obs_vp.elements[0]["ref"]
        _eval_page(lambda p: p.set_viewport_size({"width": 800, "height": 500}))
        with pytest.raises(ComputerControlError) as vp_err:
            instance.execute(
                "run-live",
                parse_computer_action({"kind": "click", "target": "browser", "x": 5, "y": 5}),
            )
        assert vp_err.value.code == "computer_surface_changed"
        with pytest.raises(ComputerControlError) as vp_el_err:
            instance.execute("run-live", parse_computer_action(_click_payload(vp_ref)))
        assert vp_el_err.value.code in ("computer_surface_changed", "computer_element_stale")
    finally:
        instance.close()


def test_stale_ref_not_in_observed_set(backend):
    _prepare_element(backend)
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-a", parse_computer_action(_click_payload("el-9")))
    assert exc.value.code == "computer_element_stale"
    assert "已失效" in exc.value.message


def test_stale_ref_after_url_change(backend):
    _prepare_element(backend)
    backend.active_page.url = "https://other.example/"
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-a", parse_computer_action(_click_payload()))
    assert exc.value.code == "computer_element_stale"
    assert "地址" in exc.value.message


def test_stale_ref_after_page_switch(backend):
    _prepare_element(backend)
    backend._observed_page_token = -1
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-a", parse_computer_action(_click_payload()))
    assert exc.value.code == "computer_element_stale"
    assert "页面已切换" in exc.value.message


def test_element_action_without_observe_is_stale(backend):
    with pytest.raises(ComputerControlError) as exc:
        backend.execute("run-a", parse_computer_action(_click_payload()))
    assert exc.value.code == "computer_element_stale"


def test_coordinate_click_still_uses_surface_check(backend):
    _prepare_element(backend)
    backend.active_page.url = "https://other.example/"
    backend.execute(
        "run-a",
        parse_computer_action({"kind": "click", "target": "browser", "x": 5, "y": 6}),
    )
    assert backend.active_page.mouse.calls == [("click", 5, 6, "left")]
