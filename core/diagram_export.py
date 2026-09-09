"""Mermaid 图表本地导出（CLI 端）。

CLI 终端无法渲染图形；模型输出中的 ```mermaid 代码块由本模块提取后生成
自包含本地 HTML（引用随包分发的 vendor/mermaid/mermaid-*.min.js，strict
安全模式），写入 ~/.ycode/exports/diagrams/，由用户通过 /diagram open 主动
在浏览器打开。浏览器即渲染器，终端不承担图形绘制。

安全约定（与 GUI 前端渲染同一威胁模型：mermaid 源码来自模型输出，按不可信
输入处理）：
- 源码写入 HTML 前先经 html.escape 转义（先转义后展示，正文与属性双份）；
- mermaid 以 securityLevel: 'strict' 初始化（净化 SVG 标签、禁用交互回调）；
- 页面带 CSP，禁止一切远程资源，脚本只允许本地 file: 与内联初始化段。
"""

from __future__ import annotations

import datetime
import html
import re
import sys
import uuid
import webbrowser
from pathlib import Path

_MERMAID_JS_NAME = "mermaid-11.17.2.min.js"

# 只提取显式标注 mermaid 语言的闭合代码块；未闭合栅栏（流式中间态）与
# 其他语言块不命中。DOTALL 允许源码跨行，非贪婪在第一个闭合栅栏处截断。
_MERMAID_BLOCK_RE = re.compile(
    r"```mermaid[ \t]*\r?\n(.*?)\r?\n?[ \t]*```",
    re.DOTALL | re.IGNORECASE,
)

# 本进程最近一次导出（/diagram open 的首选目标）；重启后回落目录扫描。
_LAST_EXPORT: Path | None = None


def get_diagrams_dir() -> Path:
    """图表 HTML 的落盘目录：与 /share 导出共用 ~/.ycode/exports 约定。"""
    return Path.home() / ".ycode" / "exports" / "diagrams"


def get_mermaid_js_path() -> Path | None:
    """定位随包分发的 mermaid UMD 渲染器：打包版走 _MEIPASS，开发版走仓库根。"""
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "vendor" / "mermaid" / _MERMAID_JS_NAME)
    candidates.append(
        Path(__file__).resolve().parent.parent / "vendor" / "mermaid" / _MERMAID_JS_NAME
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def extract_mermaid_blocks(text: str) -> list[str]:
    """提取文本内全部闭合 ```mermaid 块的源码（按出现顺序，去首尾空白）。"""
    if not isinstance(text, str) or "```" not in text:
        return []
    blocks = []
    for match in _MERMAID_BLOCK_RE.finditer(text):
        source = match.group(1).strip("\r\n")
        if source.strip():
            blocks.append(source)
    return blocks


def _escape_body(source: str) -> str:
    return html.escape(source, quote=False)


def _escape_attr(source: str) -> str:
    return html.escape(source, quote=True)


def build_diagram_html(sources: list[str]) -> str:
    """把若干 mermaid 源码渲染为一个独立本地 HTML（含 CSP 与降级提示）。"""
    js_path = get_mermaid_js_path()
    js_uri = js_path.as_uri() if js_path is not None else ""
    sections = []
    for index, source in enumerate(sources, 1):
        sections.append(
            '<section class="diag">'
            '<div class="diag-head">'
            f'<h2>图 {index} <span class="tag">mermaid</span></h2>'
            '<button class="copy-src" type="button">复制源码</button>'
            "</div>"
            f'<div class="mermaid" data-source="{_escape_attr(source)}">'
            f"{_escape_body(source)}"
            "</div>"
            "</section>"
        )
    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = f"Y-code 流程图 · {generated_at}"
    if js_uri:
        mermaid_script = (
            f'<script src="{html.escape(js_uri, quote=True)}" '
            'onerror="document.body.classList.add(\'no-mermaid\')"></script>'
        )
    else:
        # 渲染器未随包（异常安装）：不输出空 src 的 script 标签——空 src 会
        # 回读页面自身当 JS 解析。直接置 no-mermaid 态，页面只显示源码。
        mermaid_script = "<script>document.body.classList.add('no-mermaid');</script>"
    page = _HTML_TEMPLATE
    page = page.replace("__MERMAID_SCRIPT__", mermaid_script)
    page = page.replace("__TITLE__", html.escape(title))
    page = page.replace("__GENERATED_AT__", html.escape(generated_at))
    page = page.replace("__SECTIONS__", "\n".join(sections))
    return page


def export_diagram_html(text: str) -> tuple[Path, int] | None:
    """检测并导出 text 内的 mermaid 块；无命中返回 None。

    返回 (文件路径, 图表数)。同一块只会被 flush 渲染一次（CLI 的段落切分
    不会拆开代码围栏），因此每次导出对应一个新文件，不做去重。
    """
    global _LAST_EXPORT
    sources = extract_mermaid_blocks(text)
    if not sources:
        return None
    diagrams_dir = get_diagrams_dir()
    diagrams_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:6]
    filename = f"diagram_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{token}.html"
    output_path = diagrams_dir / filename
    output_path.write_text(build_diagram_html(sources), encoding="utf-8")
    _LAST_EXPORT = output_path
    return output_path, len(sources)


def get_recent_diagrams(limit: int = 20) -> list[Path]:
    """按修改时间倒序列出已生成的图表文件（/diagram list 与 open 用）。"""
    diagrams_dir = get_diagrams_dir()
    if not diagrams_dir.is_dir():
        return []
    files = [
        path
        for path in diagrams_dir.glob("diagram_*.html")
        if path.is_file()
    ]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return files[: max(1, int(limit))]


def open_diagram(path: Path) -> bool:
    """用系统默认浏览器打开本地图表文件（file:// URI，正确编码非 ASCII 路径）。"""
    try:
        return webbrowser.open(path.resolve().as_uri())
    except Exception:
        return False


def handle_diagram_command(command_text: str) -> str:
    """处理 /diagram 斜杠命令，返回可直接打印给用户的文本。

    用法: /diagram open [序号]（默认 open 最近一张）| /diagram list
    """
    parts = command_text.strip().split()
    sub = parts[1].lower() if len(parts) > 1 else "open"

    if sub == "open":
        files = get_recent_diagrams()
        if not files:
            try:
                from core.persona_settings import get_agent_display_name
                _agent = get_agent_display_name()
            except Exception:
                _agent = "星瑶"
            return f"🚨 还没有生成过流程图。{_agent}输出 Mermaid 代码块后会自动生成网页文件。"
        index = 1
        if len(parts) > 2:
            if not parts[2].isdigit() or int(parts[2]) < 1:
                return "⚠️ 用法: /diagram open [序号]，序号为 /diagram list 中的编号。"
            index = int(parts[2])
        if index > len(files):
            return f"🚨 序号超出范围：当前共 {len(files)} 个图表文件，请先 /diagram list 查看。"
        target = files[index - 1]
        if open_diagram(target):
            return f"🌐 已在浏览器打开: {target}"
        return f"🚨 无法打开浏览器。请手动访问: {target}"

    if sub == "list":
        files = get_recent_diagrams()
        if not files:
            return "暂无已生成的流程图文件。"
        lines = ["已生成的流程图（新 → 旧）:"]
        for position, path in enumerate(files, 1):
            size_kb = max(1, path.stat().st_size // 1024)
            modified = datetime.datetime.fromtimestamp(path.stat().st_mtime)
            lines.append(
                f"  {position}. {path.name}  ({size_kb} KB, {modified:%m-%d %H:%M})"
            )
        return "\n".join(lines)

    return "⚠️ 用法: /diagram [open [序号] | list]"


# 模板用 token 替换而非 f-string：内联 JS/CSS 含大量 {} 与反引号，
# f-string 转义会把这些片段全部破坏。
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- CSP：禁止一切远程资源；脚本仅限本地 file: 与本文件内联初始化段 -->
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; script-src file: 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: file:; font-src data: file:;">
<title>__TITLE__</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px;
    font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
    background: #f6f7f9; color: #1f2328;
  }
  header { margin-bottom: 18px; }
  header h1 { font-size: 17px; margin: 0 0 4px; }
  header .meta { font-size: 12px; color: #656d76; }
  section.diag {
    background: #fff; border: 1px solid #d7dce1; border-radius: 10px;
    padding: 14px 16px; margin-bottom: 18px;
  }
  .diag-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .diag-head h2 { font-size: 14px; margin: 0; color: #4b5563; }
  .diag-head h2 .tag {
    font-size: 11px; font-weight: normal; color: #0969da;
    background: #ddf4ff; border-radius: 5px; padding: 1px 7px; vertical-align: 1px;
  }
  .copy-src {
    margin-left: auto; font-size: 12px; padding: 2px 10px; cursor: pointer;
    border: 1px solid #d0d7de; border-radius: 6px; background: #f6f8fa; color: #24292f;
  }
  .copy-src:hover { background: #eef1f4; }
  .mermaid { text-align: center; overflow-x: auto; }
  .mermaid svg { max-width: 100%; height: auto; }
  /* 渲染器缺失/渲染失败：源码以等宽文本兜底展示，绝不静默空白 */
  pre.mermaid-fallback, body.no-mermaid .mermaid {
    text-align: left; white-space: pre-wrap; word-break: break-word;
    font-family: Consolas, "Courier New", monospace; font-size: 12.5px;
    background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 8px;
    padding: 10px 12px; margin: 0;
  }
  #no-mermaid-banner { display: none; }
  body.no-mermaid #no-mermaid-banner {
    display: block; margin-bottom: 16px; padding: 10px 14px;
    background: #fff8c5; border: 1px solid #d4a72c66; border-radius: 8px;
    color: #4d2d00; font-size: 13px;
  }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <div class="meta">由 Y-code 生成 · 在浏览器本地渲染，不经过任何远程服务</div>
</header>
<div id="no-mermaid-banner">
  ⚠️ 本地渲染器缺失（vendor/mermaid/mermaid-*.min.js），当前仅显示图表源码。
  源码可粘贴到任何 Mermaid 渲染工具查看，或重新安装 Y-code 修复。
</div>
__SECTIONS__
__MERMAID_SCRIPT__
<script>
(function () {
  var containers = document.querySelectorAll(".mermaid");
  function showFallback() { document.body.classList.add("no-mermaid"); }
  // 复制源码：从 data-source 属性取原始文本（浏览器已自动反转义）
  document.querySelectorAll(".copy-src").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var section = btn.closest(".diag");
      var container = section ? section.querySelector(".mermaid") : null;
      var source = container ? (container.getAttribute("data-source") || "") : "";
      var done = function () {
        var original = btn.textContent;
        btn.textContent = "已复制";
        setTimeout(function () { btn.textContent = original; }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(source).then(done, function () {});
      } else {
        var helper = document.createElement("textarea");
        helper.value = source;
        document.body.appendChild(helper);
        helper.select();
        try { document.execCommand("copy"); done(); } catch (e) {}
        document.body.removeChild(helper);
      }
    });
  });
  if (!window.mermaid) { showFallback(); return; }
  try {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "neutral"
    });
    mermaid.run({ nodes: Array.prototype.slice.call(containers) });
  } catch (err) {
    showFallback();
  }
})();
</script>
</body>
</html>
"""
