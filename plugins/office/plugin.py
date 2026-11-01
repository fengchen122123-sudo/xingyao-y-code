"""office：办公文档读取插件。

提供 office_read 工具：把 xlsx / docx / pptx / pdf 转成结构化 Markdown，
让模型先"读懂"办公文档再做后续处理（整理、汇总、生成报告、改写）。

设计取舍：
- docx / pptx 不引入 python-docx / python-pptx。这两个库会带进 lxml（C 扩展），
  打包体积会明显变大；而这两类文件的本质就是 zip 包里的 OOXML，读文本、表格、
  标题层级用标准库 zipfile + xml.etree 足够，且零新增依赖。
- xlsx 走已在依赖中的 openpyxl（能正确取公式的计算结果缓存、处理只读大表）。
- pdf 走已在依赖中的 pypdf。

边界：本工具只做「读」。写入能力（office_write）另做，且必须先定产物规范。
"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

from core.tool_protocol import ToolResult, ToolSpec

SUPPORTED_EXTS = (".xlsx", ".xlsm", ".docx", ".pptx", ".pdf")
# 老格式（.doc/.xls/.ppt）是二进制 OLE 结构，标准库读不了。
LEGACY_HINT = {
    ".doc": "老版 Word（.doc）是二进制格式，请另存为 .docx 后再读",
    ".xls": "老版 Excel（.xls）是二进制格式，请另存为 .xlsx 后再读",
    ".ppt": "老版 PowerPoint（.ppt）是二进制格式，请另存为 .pptx 后再读",
}

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

DEFAULT_MAX_ROWS = 200
DEFAULT_MAX_CHARS = 24000
DEFAULT_MAX_PAGES = 50


def _w(tag: str) -> str:
    return "{%s}%s" % (_W_NS, tag)


def _a(tag: str) -> str:
    return "{%s}%s" % (_A_NS, tag)


def _md_cell(value) -> str:
    """单元格值 → Markdown 表格安全文本（竖线转义、换行压平）。"""
    if value is None:
        return ""
    text = str(value)
    return (
        text.replace("|", "\\|")
        .replace("\r\n", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


def _md_table(rows: list) -> str:
    """二维列表 → Markdown 表格；第一行当表头，列数按最宽行补齐。"""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    if width == 0:
        return ""

    def line(cells):
        padded = list(cells) + [None] * (width - len(cells))
        return "| " + " | ".join(_md_cell(c) for c in padded) + " |"

    out = [line(rows[0]), "| " + " | ".join(["---"] * width) + " |"]
    out.extend(line(r) for r in rows[1:])
    return "\n".join(out)


# ─── xlsx ────────────────────────────────────────────────────────────


def _read_xlsx(path: str, sheet: str = "", max_rows: int = DEFAULT_MAX_ROWS) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        names = list(wb.sheetnames)
        targets = [n for n in names if not sheet or n == sheet]
        if sheet and not targets:
            return "工作簿里没有名为「%s」的工作表。可用工作表：%s" % (sheet, "、".join(names))

        blocks = []
        for name in targets:
            ws = wb[name]
            rows = []
            truncated = False
            for idx, row in enumerate(ws.iter_rows(values_only=True)):
                if idx >= max_rows:
                    truncated = True
                    break
                # 整行全空直接跳过：Excel 里大量格式化产生的空行对模型没有信息量
                if any(c is not None and str(c).strip() != "" for c in row):
                    rows.append(row)
            title = "## 工作表：%s" % name
            if not rows:
                blocks.append(title + "\n\n（无数据）")
                continue
            block = title + "\n\n" + _md_table(rows)
            if truncated:
                block += "\n\n（已截断：每个工作表只显示前 %d 个非空行，可用 max_rows 调大）" % max_rows
            blocks.append(block)
        return "\n\n".join(blocks)
    finally:
        wb.close()


# ─── docx ────────────────────────────────────────────────────────────


def _docx_paragraph_text(p) -> str:
    return "".join(t.text or "" for t in p.iter(_w("t")))


def _docx_heading_level(p) -> int:
    """段落样式 → 标题层级；非标题返回 0。

    兼容英文版（Heading1/Heading 1）与数字样式名（部分模板直接叫 "1"）。
    不解析自定义样式的实际字号——按语义层级读已经够模型理解结构。
    """
    ppr = p.find(_w("pPr"))
    if ppr is None:
        return 0
    style = ppr.find(_w("pStyle"))
    if style is None:
        return 0
    val = (style.get(_w("val")) or "").strip()
    if not val:
        return 0
    low = val.lower().replace(" ", "")
    if low == "title":
        return 1
    if low.startswith("heading"):
        digits = "".join(ch for ch in low if ch.isdigit())
        return int(digits) if digits else 1
    if low.isdigit():
        return min(int(low), 6)
    return 0


def _read_docx(path: str) -> str:
    with zipfile.ZipFile(path) as zf:
        try:
            xml_bytes = zf.read("word/document.xml")
        except KeyError:
            return "（这不是有效的 Word 文档：缺少 word/document.xml）"

    root = ET.fromstring(xml_bytes)
    body = root.find(_w("body"))
    if body is None:
        return "（文档正文为空）"

    blocks = []
    for child in body:
        if child.tag == _w("p"):
            text = _docx_paragraph_text(child).strip()
            if not text:
                continue
            level = _docx_heading_level(child)
            blocks.append("#" * min(level, 6) + " " + text if level else text)
        elif child.tag == _w("tbl"):
            rows = []
            for tr in child.findall(_w("tr")):
                cells = []
                for tc in tr.findall(_w("tc")):
                    cell_text = " ".join(
                        _docx_paragraph_text(p).strip() for p in tc.findall(_w("p"))
                    ).strip()
                    cells.append(cell_text)
                if cells:
                    rows.append(cells)
            table = _md_table(rows)
            if table:
                blocks.append(table)
    return "\n\n".join(blocks)


# ─── pptx ────────────────────────────────────────────────────────────


def _read_pptx(path: str) -> str:
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)]
        if not names:
            return "（演示文稿里没有幻灯片）"
        names.sort(key=lambda n: int(re.search(r"slide(\d+)\.xml$", n).group(1)))

        blocks = []
        for idx, name in enumerate(names, 1):
            root = ET.fromstring(zf.read(name))
            texts = [(t.text or "").strip() for t in root.iter(_a("t"))]
            texts = [t for t in texts if t]
            if texts:
                blocks.append("## 第 %d 页\n\n%s" % (idx, "\n".join(texts)))
            else:
                blocks.append("## 第 %d 页\n\n（无文本，可能是纯图片页）" % idx)
        return "\n\n".join(blocks)


# ─── pdf ─────────────────────────────────────────────────────────────


def _read_pdf(path: str, max_pages: int = DEFAULT_MAX_PAGES) -> str:
    from pypdf import PdfReader

    reader = PdfReader(path)
    total = len(reader.pages)
    if total == 0:
        return "（PDF 没有页面）"

    blocks = []
    for idx in range(min(total, max_pages)):
        try:
            text = (reader.pages[idx].extract_text() or "").strip()
        except Exception as exc:  # 单页解析失败不影响其余页
            text = "（本页解析失败：%s: %s）" % (type(exc).__name__, exc)
        if not text:
            text = "（无文本，可能是扫描件或纯图片）"
        blocks.append("## 第 %d 页\n\n%s" % (idx + 1, text))
    if total > max_pages:
        blocks.append("（已截断：共 %d 页，只读了前 %d 页）" % (total, max_pages))
    return "\n\n".join(blocks)


# ─── 工具入口 ────────────────────────────────────────────────────────


_MISSING_DEP_HINTS = {
    "openpyxl": ("openpyxl", "xlsx 读写"),
    "et_xmlfile": ("openpyxl", "xlsx 读写"),
    "pypdf": ("pypdf", "pdf 文本提取"),
}


def _missing_dependency_hint(exc: BaseException) -> str:
    """把 ModuleNotFoundError 翻成「缺什么、怎么装」的可执行提示。

    正常安装包已通过 ycode.spec 的 collect_all 内置这两个包；走到这里通常是
    源码运行环境没装依赖，或拿到的是旧版安装包。裸抛 ModuleNotFoundError
    对用户毫无可操作性，所以统一翻译。
    """
    name = str(getattr(exc, "name", "") or "")
    package, capability = _MISSING_DEP_HINTS.get(name, (name, "该格式"))
    if not package:
        return "缺少依赖：%s" % exc
    return (
        "缺少依赖 %s（%s所需）。\n"
        "  源码/开发环境：pip install %s\n"
        "  安装包版本：请升级到内置该依赖的新版本（旧包未随包携带）"
        % (package, capability, package)
    )


def office_read(path, sheet="", max_rows=DEFAULT_MAX_ROWS, max_chars=DEFAULT_MAX_CHARS):
    """读取办公文档并转成结构化 Markdown。"""
    if not isinstance(path, str) or not path.strip():
        return ToolResult(content="读取失败：path 不能为空", is_error=True)

    try:
        from core.tools import _resolve_workspace_path

        resolved = _resolve_workspace_path(path, allow_trusted_read=True)
    except Exception as exc:
        return ToolResult(content="读取失败：%s" % exc, is_error=True)

    if not resolved.exists():
        return ToolResult(content="文件不存在：%s" % resolved, is_error=True)
    if resolved.is_dir():
        return ToolResult(content="这是一个目录，不是文档：%s" % resolved, is_error=True)

    ext = resolved.suffix.lower()
    if ext in LEGACY_HINT:
        return ToolResult(content="读取失败：%s" % LEGACY_HINT[ext], is_error=True)
    if ext not in SUPPORTED_EXTS:
        return ToolResult(
            content="读取失败：不支持 %s 格式。office_read 支持：%s"
            % (ext or "无扩展名", "、".join(SUPPORTED_EXTS)),
            is_error=True,
        )

    try:
        if ext in (".xlsx", ".xlsm"):
            body = _read_xlsx(str(resolved), sheet=sheet, max_rows=max_rows)
        elif ext == ".docx":
            body = _read_docx(str(resolved))
        elif ext == ".pptx":
            body = _read_pptx(str(resolved))
        else:
            body = _read_pdf(str(resolved))
    except ImportError as exc:
        # openpyxl / pypdf 缺失：给可执行提示，别把 ModuleNotFoundError 甩给用户
        return ToolResult(content=_missing_dependency_hint(exc), is_error=True)
    except Exception as exc:
        return ToolResult(
            content="解析 %s 失败：%s: %s" % (resolved.name, type(exc).__name__, exc),
            is_error=True,
        )

    if not body.strip():
        return ToolResult(content="（%s 里没有可提取的文本内容）" % resolved.name)

    text = "# 文件：%s\n\n%s" % (resolved.name, body)
    limit = max(1000, int(max_chars or DEFAULT_MAX_CHARS))
    if len(text) > limit:
        text = text[:limit] + (
            "\n\n（内容超长已截断：上限 %d 字符。可调大 max_chars，"
            "xlsx 也可用 sheet / max_rows 缩小范围）" % limit
        )
    return ToolResult(content=text)


# ─── 写入：xlsx ──────────────────────────────────────────────────────

# 默认样式规范。集中在一处是为了让「产物长什么样」只有唯一事实源：
# 改样式只改这里，所有走 office_write 的表格一起变。
STYLE = {
    "font_name": "微软雅黑",
    "font_size": 10.5,
    "header_fill": "2563EB",
    "header_color": "FFFFFF",
    "stripe_fill": "F8FAFC",
    "border_color": "D1D5DB",
    "min_col_width": 8,
    "max_col_width": 40,
    "header_row_height": 22,
}


def _display_width(value) -> int:
    """按显示宽度估算列宽：中文/全角按 2，其余按 1。"""
    width = 0
    for ch in str(value):
        width += 2 if ord(ch) > 0x7F else 1
    return width


def _cell_value(value):
    """限制单元格值类型：openpyxl 只接受标量，容器统一转文本。"""
    if value is None or isinstance(value, (str, int, float, bool, date, datetime)):
        return value
    return str(value)


def _write_xlsx(path: Path, sheets, overwrite: bool = False):
    """按办公室默认规范生成 xlsx 字节；返回 (字节, 数据行数, 工作表名列表)。

    只在内存里生成产物、不落盘——落盘由调用方走 _save_with_turn_undo，
    这样按回合撤销能拿到完整的计划 after。非法输入抛 ValueError。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.properties import PageSetupProperties

    if path.exists() and not overwrite:
        raise ValueError(
            "文件已存在：%s。确认要整体覆盖时传 overwrite=true（会丢掉原有内容）；"
            "如果只是想改旧表，应先 office_read 读出内容、改完再整体重写" % path
        )
    if not isinstance(sheets, list) or not sheets:
        raise ValueError("sheets 不能为空，至少给一个工作表")

    thin = Side(style="thin", color=STYLE["border_color"])
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill = PatternFill("solid", fgColor=STYLE["header_fill"])
    stripe_fill = PatternFill("solid", fgColor=STYLE["stripe_fill"])
    header_font = Font(
        name=STYLE["font_name"],
        size=STYLE["font_size"],
        bold=True,
        color=STYLE["header_color"],
    )
    body_font = Font(name=STYLE["font_name"], size=STYLE["font_size"])

    wb = Workbook()
    wb.remove(wb.active)  # 只保留传入的工作表，避免多出一个空的 Sheet

    written = 0
    names = []
    for spec in sheets:
        if not isinstance(spec, dict):
            raise ValueError("sheets 的每一项必须是 {name, rows} 对象")
        rows = spec.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValueError("工作表「%s」的 rows 不能为空" % (spec.get("name") or "Sheet1"))
        name = str(spec.get("name") or ("Sheet%d" % (len(names) + 1)))[:31]
        ws = wb.create_sheet(title=name)
        names.append(name)

        width = 0
        for r_idx, row in enumerate(rows, 1):
            if not isinstance(row, list):
                raise ValueError("工作表「%s」第 %d 行不是数组" % (name, r_idx))
            width = max(width, len(row))
            for c_idx, raw in enumerate(row, 1):
                cell = ws.cell(row=r_idx, column=c_idx, value=_cell_value(raw))
                cell.border = border
                if r_idx == 1:
                    cell.fill = header_fill
                    cell.font = header_font
                    cell.alignment = Alignment(
                        horizontal="center", vertical="center", wrap_text=True
                    )
                else:
                    cell.font = body_font
                    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                        cell.alignment = Alignment(horizontal="right", vertical="center")
                    else:
                        cell.alignment = Alignment(vertical="center")
                    if r_idx % 2 == 0:
                        cell.fill = stripe_fill
        written += len(rows)

        # 列宽按内容自适应：太窄中文挤成一团，太宽打印时会摊成好几页
        for c_idx in range(1, width + 1):
            longest = 0
            for row in rows:
                if c_idx <= len(row):
                    cell_text = row[c_idx - 1] if row[c_idx - 1] is not None else ""
                    longest = max(longest, _display_width(cell_text))
            letter = get_column_letter(c_idx)
            ws.column_dimensions[letter].width = max(
                STYLE["min_col_width"], min(STYLE["max_col_width"], longest + 2)
            )

        ws.row_dimensions[1].height = STYLE["header_row_height"]
        ws.freeze_panes = "A2"  # 首行是表头，往下滚时不跟着走
        if width:
            ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(width), len(rows))
            ws.print_title_rows = "1:1"  # 跨页打印时每页都重复表头
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue(), written, names


def _save_with_turn_undo(path: Path, payload: bytes, tool_name: str) -> None:
    """落盘并接入按回合撤销（与 write_file、图片落盘同一套记录器流程）。

    检查点已封闭（迟到写入）时直接抛错、不触盘；没有活动检查点（CLI、测试或
    未开启撤销的嵌入方）降级为不可撤销裸写，与 write_file 的降级语义一致。
    工作区正被其他活跃回合写入时 prepare_mutation 抛 workspace_busy，同样不触盘。
    """
    from core.turn_undo import coordinator as _tu_coordinator
    from core.turn_undo import recorder as _tu_recorder
    from core.turn_undo.models import NodeKind, TurnUndoError, UndoErrorCode

    path.parent.mkdir(parents=True, exist_ok=True)
    with _tu_coordinator.get_coordinator().workspace_lock(
        _tu_coordinator.thread_workspace_root()
    ):
        handle = None
        try:
            handle = _tu_recorder.prepare_mutation(
                display_path=str(path),
                after_kind=NodeKind.REGULAR_FILE,
                after_content=payload,
                tool_name=tool_name,
                tool_call_id="",
            )
        except TurnUndoError as error:
            if error.code != UndoErrorCode.NO_ACTIVE_CHECKPOINT:
                raise
            handle = None
        with open(path, "wb") as f:
            f.write(payload)
        if handle is not None:
            _tu_recorder.verify_after_mutation(handle)


def office_write(path, sheets=None, overwrite=False):
    """按默认规范写 xlsx。"""
    if not isinstance(path, str) or not path.strip():
        return ToolResult(content="写入失败：path 不能为空", is_error=True)

    resolved = Path(path)
    if resolved.suffix.lower() not in (".xlsx", ".xlsm"):
        return ToolResult(
            content=(
                "写入失败：office_write 目前只写 .xlsx。Word 报告/纪要建议先用 "
                "Markdown 交付（星瑶擅长的就是这一层），需要 docx 直出时再单独提需求。"
                "收到：%s" % (resolved.suffix or "无扩展名")
            ),
            is_error=True,
        )

    try:
        from core.tools import _resolve_workspace_path

        resolved = _resolve_workspace_path(path)
    except Exception as exc:
        return ToolResult(content="写入失败：%s" % exc, is_error=True)

    try:
        payload, written, names = _write_xlsx(resolved, sheets, overwrite=overwrite)
    except ValueError as exc:
        return ToolResult(content="写入失败：%s" % exc, is_error=True)
    except ImportError as exc:
        return ToolResult(content=_missing_dependency_hint(exc), is_error=True)
    except Exception as exc:
        return ToolResult(
            content="写入 %s 失败：%s: %s" % (resolved.name, type(exc).__name__, exc),
            is_error=True,
        )

    try:
        _save_with_turn_undo(resolved, payload, "office_write")
    except Exception as exc:
        # 撤销记录失败（检查点已封闭 / 工作区被占）一律不触盘，如实报错
        return ToolResult(
            content="写入 %s 失败：%s: %s" % (resolved.name, type(exc).__name__, exc),
            is_error=True,
        )

    return ToolResult(
        content=(
            "已写入 %s\n工作表：%s\n数据行：%d（含表头）\n"
            "已应用默认规范：表头蓝底白字加粗、冻结首行、自动筛选、斑马纹、"
            "列宽自适应、横向打印并缩放到一页宽、跨页重复表头"
            % (resolved, "、".join(names), written)
        )
    )


_PERMISSION_KWARGS = dict(
    read_only=True,
    mutates_files=False,
    requires_permission=True,
    parallel_safe=True,
    # 纯本地只读：按声明契约（tests/test_tool_declarations.py）只读工具的
    # side_effect_scope 只能是 none / network，不得标 workspace 这类写类范围
    side_effect_scope="none",
)

_WRITE_KWARGS = dict(
    read_only=False,
    mutates_files=True,
    requires_permission=True,
    parallel_safe=False,
    side_effect_scope="workspace",
)


def register(registry):
    registry.register(ToolSpec(
        name="office_read",
        description=(
            "读取办公文档内容并转成结构化 Markdown。支持 xlsx / xlsm（每个工作表一个"
            "Markdown 表格）、docx（标题层级 + 段落 + 表格）、pptx（每页文本）、"
            "pdf（每页文本）。"
            "【何时使用】用户提到 .xlsx/.docx/.pptx/.pdf 文件，或要求「整理这个表格」"
            "「总结这份文档」「提取会议纪要」「看看这份报告」时，先用本工具读取内容，"
            "再进行后续处理——不要凭文件名猜内容。"
            "【读取前不必先用 read_file】二进制办公文档用 read_file 只会得到乱码。"
            "【大文件】xlsx 用 sheet 指定工作表、max_rows 限制行数；其余格式按页/段落"
            "自动截断，可用 max_chars 调整上限。"
            "【只读】本工具不修改文件，也不做格式转换。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文档路径：相对当前工作目录，或绝对路径",
                },
                "sheet": {
                    "type": "string",
                    "description": "仅 xlsx 有效：指定工作表名，不传则读全部工作表",
                },
                "max_rows": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2000,
                    "description": "仅 xlsx 有效：每个工作表最多读取的非空行数，默认 200",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 200000,
                    "description": "返回内容的字符上限，默认 24000",
                },
            },
            "required": ["path"],
        },
        execute=office_read,
        activity_description="读取办公文档中",
        **_PERMISSION_KWARGS,
    ))

    registry.register(ToolSpec(
        name="office_write",
        description=(
            "按办公室默认规范生成 Excel 文件（.xlsx）。传入二维数据即自动套用：表头蓝底"
            "白字加粗居中、正文微软雅黑 10.5、细边框、斑马纹、列宽按内容自适应、冻结首行、"
            "自动筛选、横向打印且缩放到一页宽、跨页重复表头。"
            "【何时使用】用户要求「做个表格」「导成 Excel」「整理成表」「生成报表」"
            "或需要把整理好的数据交付成可直接发出去的文件时。"
            "【数据结构】sheets 是工作表数组，每个工作表 {name, rows}；rows 是二维数组，"
            "第一行自动当表头。支持多工作表。"
            "【覆盖保护】目标文件已存在时默认拒绝写入，需显式传 overwrite=true；"
            "要改旧表应先 office_read 读出内容再整体重写，不要盲目覆盖。"
            "【不写 Word】.docx 直出暂不支持，报告/纪要类先输出 Markdown。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目标文件路径，必须以 .xlsx 或 .xlsm 结尾",
                },
                "sheets": {
                    "type": "array",
                    "description": "工作表列表，每个工作表形如 {name: 表名, rows: 二维数组}",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "工作表名，最长 31 字符；不传自动命名",
                            },
                            "rows": {
                                "type": "array",
                                "description": "二维数组，第一行作表头",
                                "items": {
                                    "type": "array",
                                    "items": {},
                                },
                            },
                        },
                        "required": ["rows"],
                    },
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "目标文件已存在时是否覆盖，默认 false（拒绝写入）",
                },
            },
            "required": ["path", "sheets"],
        },
        execute=office_write,
        activity_description="生成表格中",
        **_WRITE_KWARGS,
    ))
