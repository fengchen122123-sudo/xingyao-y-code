"""E1 元素收集脚本的真实浏览器行为测试（设计 9：T-E1-1~6）。

用 Playwright 直连系统 Edge（headless）执行 ELEMENT_COLLECT_SCRIPT 的真实
JS 语义：kind/name 推导、可见性与视口过滤、200/80 上限、data-ycode-ref
打标、确定性。环境不可用（无 playwright 或无可用 Edge）时整组 skip——
不伪造通过；fake 层集成语义由 test_computer_browser_elements.py 覆盖。
"""

from __future__ import annotations

import json

import pytest

from core.computer.browser_backend import ELEMENT_COLLECT_SCRIPT


@pytest.fixture(scope="module")
def edge_page():
    """module 级 headless Edge 页面；启动失败则整组 skip（环境问题，不算通过）。"""
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


# ---------------- 基本收集与 kind 推导 ----------------


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
    # ref 按文档顺序从 el-1 递增
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


# ---------------- 可见性与视口过滤 ----------------


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


# ---------------- name 推导优先级 ----------------


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


# ---------------- 确定性 + 打标 ----------------


def test_collect_is_deterministic_and_marks_elements(edge_page):
    html = '<button id="b1">按我</button><input placeholder="输入">'
    edge_page.set_content(html)
    first = edge_page.evaluate(ELEMENT_COLLECT_SCRIPT)
    second = edge_page.evaluate(ELEMENT_COLLECT_SCRIPT)
    assert json.dumps(first, ensure_ascii=False) == json.dumps(second, ensure_ascii=False)
    # 打标：E2 的 Playwright locator 依赖 data-ycode-ref 属性
    marked = edge_page.query_selector('[data-ycode-ref="el-1"]')
    assert marked is not None
    assert marked.inner_text() == "按我"


# ---------------- 上限与截断 ----------------


def test_max_elements_and_name_truncation(edge_page):
    buttons = "".join(f"<button>{i}</button>" for i in range(210))
    result = _collect(edge_page, buttons)
    assert len(result) == 200
    assert result[-1]["ref"] == "el-200"

    long_name = "长" * 100
    truncated = _collect(edge_page, f"<button>{long_name}</button>")
    assert len(truncated[0]["name"]) == 80


# ---------------- 隐私 ----------------


def test_collect_never_reads_form_values(edge_page):
    edge_page.set_content(
        '<input id="u" type="text" value="SECRET_VALUE" aria-label="用户名">'
        '<input id="p" type="password" value="SECRET_PW">'
    )
    result = edge_page.evaluate(ELEMENT_COLLECT_SCRIPT)
    dumped = json.dumps(result, ensure_ascii=False)
    assert "SECRET_VALUE" not in dumped
    assert "SECRET_PW" not in dumped
