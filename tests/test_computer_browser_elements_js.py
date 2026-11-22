from __future__ import annotations

import json

import pytest

from core.computer.browser_backend import ELEMENT_COLLECT_SCRIPT


@pytest.fixture(scope="module")
def edge_page():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:  # pragma: no cover - 环境分支
        pytest.skip(f"playwright 不可用：{error}")
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
    except Exception as error:  # pragma: no cover - 环境分支
        playwright.stop()
        pytest.skip(f"headless Edge 不可用：{type(error).__name__}: {error}")
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        yield page
    finally:
        browser.close()
        playwright.stop()


def _collect(page, html: str):
    page.set_content(html)
    return page.evaluate(ELEMENT_COLLECT_SCRIPT)


def test_collects_common_html_with_kinds(edge_page):
    result = _collect(
        edge_page,
        """
        <a href="#target" id="a1">链接一</a>
        <button id="b1">按钮一</button>
        <input id="i1" type="text" placeholder="文本输入">
        <input id="i2" type="checkbox" aria-label="勾选框">
        <input id="i3" type="submit" value="提交">
        <select id="s1" aria-label="选择器"><option>选项</option></select>
        <textarea id="t1" aria-label="备注"></textarea>
        <div role="searchbox" id="r1" aria-label="搜索"
             style="width:120px;height:20px"></div>
        """,
    )
    assert [item["kind"] for item in result] == [
        "link", "button", "textbox", "checkbox", "button",
        "combobox", "textbox", "searchbox",
    ]
    assert [item["ref"] for item in result] == [f"el-{i}" for i in range(1, 9)]
    assert result[0]["name"] == "链接一"
    assert result[1]["name"] == "按钮一"
    assert result[3]["name"] == "勾选框"
    # input[type=submit] 的 name 不取 value（隐私铁律）：为空是预期行为
    assert result[4]["name"] == ""


def test_disabled_state_is_reported(edge_page):
    result = _collect(
        edge_page,
        """
        <button disabled>禁用按钮</button>
        <button aria-disabled="true">ARIA 禁用</button>
        <button>可用按钮</button>
        """,
    )
    assert [item["enabled"] for item in result] == [False, False, True]


def test_visibility_filters_hidden_elements(edge_page):
    result = _collect(
        edge_page,
        """
        <button style="display:none">不可见1</button>
        <button style="visibility:hidden">不可见2</button>
        <button style="opacity:0">不可见3</button>
        <button id="ok">可见按钮</button>
        """,
    )
    assert [item["name"] for item in result] == ["可见按钮"]


def test_viewport_filter_excludes_far_elements(edge_page):
    result = _collect(
        edge_page,
        """
        <button id="ok">在视口内</button>
        <button style="position:absolute;top:9000px;left:0">视口外</button>
        """,
    )
    assert [item["name"] for item in result] == ["在视口内"]


def test_name_derivation_priority(edge_page):
    result = _collect(
        edge_page,
        """
        <button aria-label="别名按钮">文本按钮</button>
        <label for="lf">标签文本</label><input id="lf" type="text">
        <label>包装文本<input id="wl" type="text"></label>
        <input id="ph" type="text" placeholder="占位提示">
        <button id="tx">可见文本</button>
        """,
    )
    assert [item["name"] for item in result] == [
        "别名按钮", "标签文本", "包装文本", "占位提示", "可见文本",
    ]


def test_collect_is_deterministic_without_dom_mutation(edge_page):
    html = '<button id="b1">按我</button><input placeholder="输入">'
    edge_page.set_content(html)
    first = edge_page.evaluate(ELEMENT_COLLECT_SCRIPT)
    second = edge_page.evaluate(ELEMENT_COLLECT_SCRIPT)
    assert json.dumps(first, ensure_ascii=False) == json.dumps(second, ensure_ascii=False)
    assert edge_page.query_selector('[data-ycode-ref]') is None


def test_collect_does_not_renumber_after_element_out_of_viewport(edge_page):
    edge_page.set_content(
        '<button id="b1">第一个</button><button id="b2">第二个</button>'
    )
    first = edge_page.evaluate(ELEMENT_COLLECT_SCRIPT)
    assert [(item["ref"], item["name"]) for item in first] == [
        ("el-1", "第一个"),
        ("el-2", "第二个"),
    ]
    edge_page.eval_on_selector(
        "#b1",
        "el => { el.style.position = 'absolute'; el.style.top = '9000px'; }",
    )
    second = edge_page.evaluate(ELEMENT_COLLECT_SCRIPT)
    assert [(item["ref"], item["name"]) for item in second] == [("el-2", "第二个")]
    assert edge_page.query_selector('[data-ycode-ref]') is None


def test_collect_pierces_open_shadow_dom(edge_page):
    edge_page.set_content('<div id="host"></div>')
    edge_page.evaluate(
        """() => {
            const root = document.getElementById('host').attachShadow({ mode: 'open' });
            root.innerHTML = '<label for="si">影子输入</label>'
                + '<input id="si" type="text">'
                + '<button id="sb">影子按钮</button>';
        }"""
    )
    result = edge_page.evaluate(ELEMENT_COLLECT_SCRIPT)
    assert [(item["kind"], item["name"]) for item in result] == [
        ("textbox", "影子输入"),
        ("button", "影子按钮"),
    ]
    assert all(
        item["bounds"][2] > item["bounds"][0] and item["bounds"][3] > item["bounds"][1]
        for item in result
    )


def test_collect_cursor_pointer_supplements_without_duplicating_children(edge_page):
    result = _collect(
        edge_page,
        """
        <div id="card" style="cursor:pointer;width:160px;height:40px">
          <span>卡片标题</span>
          <span>副标题</span>
        </div>
        <button id="real-btn">真实按钮</button>
        """,
    )
    names = [item["name"] for item in result]
    assert "真实按钮" in names
    assert result[0]["name"] == "真实按钮"
    assert any("卡片标题" in name for name in names)
    assert names.count("副标题") == 0


def test_max_elements_and_name_truncation(edge_page):
    buttons = "".join(f"<button>{i}</button>" for i in range(210))
    result = _collect(edge_page, buttons)
    assert len(result) == 210

    many = "".join(f"<button>{i}</button>" for i in range(520))
    truncated = _collect(edge_page, many)
    assert len(truncated) == 500
    assert truncated[-1]["ref"] == "el-500"

    long_name = "长" * 100
    truncated_name = _collect(edge_page, f"<button>{long_name}</button>")
    assert len(truncated_name[0]["name"]) == 80


def test_inline_anchor_bounds_cover_wrapped_icon(edge_page):
    result = _collect(
        edge_page,
        """
        <style>a.grid { display:inline; font-size:1px; line-height:0; }</style>
        <a class="grid" href="#x" onclick="return false">
          <img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
               width="16" height="16" alt="">
        </a>
        """,
    )
    assert len(result) == 1
    left, top, right, bottom = result[0]["bounds"]
    assert (right - left, bottom - top) == (16, 16)


def test_name_truncation_counts_code_points_not_utf16_units(edge_page):
    truncated = _collect(edge_page, f"<button>{'😀' * 100}</button>")
    assert truncated[0]["name"] == "😀" * 80
    mixed = _collect(edge_page, f"<button>{'a' + '😀' * 100}</button>")
    assert mixed[0]["name"] == "a" + "😀" * 79


def test_collect_never_reads_form_values(edge_page):
    edge_page.set_content(
        '<input id="u" type="text" value="SECRET_VALUE" aria-label="用户名">'
        '<input id="p" type="password" value="SECRET_PW">'
    )
    result = edge_page.evaluate(ELEMENT_COLLECT_SCRIPT)
    dumped = json.dumps(result, ensure_ascii=False)
    assert "SECRET_VALUE" not in dumped
    assert "SECRET_PW" not in dumped
