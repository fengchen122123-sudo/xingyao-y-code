"""core.diagram_export 的提取 / 转义 / HTML 生成 / 命令处理单测。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import diagram_export as de


@pytest.fixture()
def diagrams_dir(tmp_path, monkeypatch):
    target = tmp_path / "diagrams"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(de, "get_diagrams_dir", lambda: target)
    return target


FLOWCHART = "flowchart TD\n    A[开始] --> B{判断}\n    B -->|是| C[结束]"


def test_extract_single_block():
    text = f"前置说明\n\n```mermaid\n{FLOWCHART}\n```\n\n后置说明"
    assert de.extract_mermaid_blocks(text) == [FLOWCHART]


def test_extract_multiple_blocks_keeps_order():
    text = (
        "```mermaid\ngraph TD\nA-->B\n```\n"
        "中间文字\n"
        "```mermaid\nsequenceDiagram\nA->>B: hi\n```"
    )
    blocks = de.extract_mermaid_blocks(text)
    assert len(blocks) == 2
    assert blocks[0].startswith("graph TD")
    assert blocks[1].startswith("sequenceDiagram")


def test_extract_ignores_non_mermaid_and_unclosed():
    text = "```python\nprint('hi')\n```\n\n```mermaid\ngraph TD\nA-->B\n"
    assert de.extract_mermaid_blocks(text) == []


def test_extract_crlf_and_case_insensitive_lang():
    text = "```MERMAID\r\ngraph TD\r\nA-->B\r\n```"
    assert de.extract_mermaid_blocks(text) == ["graph TD\r\nA-->B"]


def test_extract_empty_block_skipped():
    text = "```mermaid\n   \n```"
    assert de.extract_mermaid_blocks(text) == []


def test_extract_requires_exact_lang():
    # mermaidish / mermaid-2 等变体语言名不命中
    text = "```mermaidish\ngraph TD\n```\n\n```mermaid-x\nA\n```"
    assert de.extract_mermaid_blocks(text) == []


def test_export_returns_none_without_blocks(diagrams_dir):
    assert de.export_diagram_html("没有图表的普通输出") is None


def test_export_writes_html_with_escaped_source(diagrams_dir):
    malicious = "flowchart TD\n    A[<img src=x onerror=alert(1)>] --> B"
    text = f"```mermaid\n{malicious}\n```"
    result = de.export_diagram_html(text)
    assert result is not None
    path, count = result
    assert count == 1
    assert path.parent == diagrams_dir
    assert path.is_file()
    html_text = path.read_text(encoding="utf-8")
    # 渲染器 script 必须是顶级的真实标签：模板不得把含 <script> 的 token
    # 再包进一层 <script>（嵌套会被 HTML 解析器当 JS 文本，渲染器永不加载）
    assert "<script><script" not in html_text
    # 源码必须以转义后的形式出现：<img 被转为 &lt;img，事件属性不再可执行
    assert "<img" not in html_text
    assert "&lt;img" in html_text
    # strict 安全模式 + CSP + 本地渲染器引用
    assert "securityLevel: \"strict\"" in html_text
    assert "Content-Security-Policy" in html_text
    assert "vendor/mermaid/mermaid-11.17.2.min.js" in html_text


def test_export_multiple_blocks_numbered(diagrams_dir):
    text = "```mermaid\ngraph TD\nA-->B\n```\n\n```mermaid\ngraph LR\nC-->D\n```"
    _, count = de.export_diagram_html(text)
    assert count == 2


def test_realistic_assistant_block_triggers_export(diagrams_dir):
    # 模拟 CLI 实际会收到的完整正文块：散文 + 带 <br/> 换行与 style 填色的
    # mermaid 块 + 收尾散文（正是 _render_public_answer_block 收到的形态），
    # 回归用户实测"没有提示"的场景
    text = (
        "流程核心要点：\n\n"
        "```mermaid\n"
        "flowchart TD\n"
        "    S[收到任务] --> R{需要查资料?}\n"
        "    R -- 是 --> T[systematic-debugging<br/>定位根因后修复]\n"
        "    T --> N[apply 最小改动]\n"
        "    R -- 否 --> N\n"
        "    N --> U[汇报:改了哪些文件<br/>核心变化 / 验证结果]\n"
        "    style S fill:#4a9eff,color:#fff\n"
        "    style U fill:#2ecc71,color:#fff\n"
        "```\n\n"
        "1. 技能优先：任何工作开始前先扫描技能目录\n"
    )
    result = de.export_diagram_html(text)
    assert result is not None
    path, count = result
    assert count == 1
    html_text = path.read_text(encoding="utf-8")
    assert "systematic-debugging" in html_text
    assert "style S fill" in html_text


def test_export_unique_filenames(diagrams_dir):
    text = f"```mermaid\n{FLOWCHART}\n```"
    path_a, _ = de.export_diagram_html(text)
    path_b, _ = de.export_diagram_html(text)
    assert path_a != path_b


def test_render_fallback_when_js_missing(monkeypatch, diagrams_dir):
    # 渲染器文件缺失时：不得输出空 src 的 script 标签（会回读页面自身当 JS），
    # 且页面自带 no-mermaid 置位脚本保证源码兜底展示
    monkeypatch.setattr(de, "get_mermaid_js_path", lambda: None)
    text = f"```mermaid\n{FLOWCHART}\n```"
    path, _ = de.export_diagram_html(text)
    html_text = path.read_text(encoding="utf-8")
    assert '<script src=""' not in html_text
    assert "no-mermaid" in html_text


def test_get_recent_diagrams_sorted_newest_first(diagrams_dir):
    import time

    for name in ("diagram_1.html", "diagram_2.html", "diagram_3.html"):
        (diagrams_dir / name).write_text("x", encoding="utf-8")
        time.sleep(0.01)
    recent = de.get_recent_diagrams()
    assert [p.name for p in recent] == ["diagram_3.html", "diagram_2.html", "diagram_1.html"]


def test_handle_command_open_no_files(diagrams_dir):
    assert "还没有生成过流程图" in de.handle_diagram_command("/diagram open")


def test_handle_command_open_invalid_index(diagrams_dir):
    (diagrams_dir / "diagram_a.html").write_text("x", encoding="utf-8")
    assert "用法" in de.handle_diagram_command("/diagram open abc")
    assert "超出范围" in de.handle_diagram_command("/diagram open 5")


def test_handle_command_list(diagrams_dir):
    (diagrams_dir / "diagram_a.html").write_text("x", encoding="utf-8")
    output = de.handle_diagram_command("/diagram list")
    assert "diagram_a.html" in output


def test_handle_command_open_invokes_browser(diagrams_dir, monkeypatch):
    opened = {}
    monkeypatch.setattr(
        de.webbrowser, "open", lambda uri: opened.setdefault("uri", uri) or True
    )
    (diagrams_dir / "diagram_a.html").write_text("x", encoding="utf-8")
    output = de.handle_diagram_command("/diagram open")
    assert "已在浏览器打开" in output
    assert opened["uri"].startswith("file:///")


def test_mermaid_js_path_resolves_dev_repo():
    path = de.get_mermaid_js_path()
    assert path is not None
    assert path.is_file()
    assert path.name == "mermaid-11.17.2.min.js"
