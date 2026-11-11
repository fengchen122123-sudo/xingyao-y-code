# -*- coding: utf-8 -*-
"""Office 文档（Word / Excel / PowerPoint）转 PDF，供右侧面板内嵌预览。

为什么需要这一层：右栏内嵌的 WebView2 只能直接渲染 PDF / HTML / 图片，把
`.docx` / `.xlsx` / `.pptx` 交给它只会显示乱码或空白。要在面板里"看一眼"这些
文件，最省事又保真的公共格式就是 PDF：Office 自己导出的 PDF 版式与打印一致，
而前端预览链路（`file://` + PDF 查看器）已经跑通，不必为每种格式各写一套渲染。

关键设计取舍：
1. 转换跑在**子进程**里。源码运行时命令是
   `sys.executable -m core.office_preview --convert`；打包（PyInstaller）后
   `sys.executable` 是本程序的 exe，`-m` 不成立，改用 `--ycode-office-convert`
   标志回落，由 gui_main.py / main.py 顶部的预检分发到 `run_convert_worker`。
   Office COM 是单线程 STA 组件，遇到异常文档会弹模态窗、等待用户点"确定"，
   在 GUI 主进程里调用等于把整个界面拖死。放子进程后：父进程只等一个有超时的
   `subprocess.run`，超时就 kill，界面永远不卡；子进程即使崩了也只是这一次预览失败。
2. 结果按源文件路径缓存成 PDF（`office_<sha1>.pdf`）。COM 启动 Word 本身就要
   1~2 秒，Office 冷启动更久；同一文件重复点击必须秒开，所以缓存文件落在
   `core.web_preview.get_preview_dir()`（与 markdown 预览页同一目录约定），
   用 mtime 判断新鲜度：PDF 比源文件新即直接复用。
3. 路径安全**不在本模块做**。是否允许预览某个路径由调用方（gui_bridge →
   core.gui_path_policy 凭据/工作区/沙盒规则）判定；本模块只负责"给我一个已
   授权的文件，我把它变成 PDF"，便于单测复用，也避免安全判定散落到两处。
4. 不 import webview / PIL / gui_bridge：本模块会被测试与子进程直接导入，
   引入 GUI 重组件会让子进程启动变慢且容易踩依赖缺失。

依赖：Windows + pywin32（venv 已有，不新增 requirements）。非 Windows 或未装
Office/WPS 时，转换会以可读的 OfficeConvertError 失败，由调用方降级为
"该文件暂不支持预览"。
"""

from __future__ import annotations

import argparse
import hashlib
import locale
import logging
import os
import subprocess
import sys
from pathlib import Path

# 复用预览页目录约定（~/.ycode/exports/preview，跟随 YCODE_DATA_DIR），
# 不复制目录逻辑：缓存 PDF 与 markdown 预览页同生命周期，清理时一起清。
from core.web_preview import get_preview_dir

_LOGGER = logging.getLogger(__name__)

# 扩展名（小写、含点）→ 引擎类型。RTF/ODT/ODS/ODP 也走对应 Office 组件：
# Word 能直接开 RTF/ODT，Excel 能开 ODS，PowerPoint 能开 ODP，失败自然报错。
OFFICE_EXTENSIONS: dict[str, str] = {
    ".doc": "word",
    ".docx": "word",
    ".rtf": "word",
    ".odt": "word",
    ".xls": "excel",
    ".xlsx": "excel",
    ".ods": "excel",
    ".ppt": "powerpoint",
    ".pptx": "powerpoint",
    ".odp": "powerpoint",
}

# 子进程的工作目录：`-m core.office_preview` 需要项目根在 sys.path 上。
# GUI 从任意目录启动时靠这个保证子进程能 import 到本模块。
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)

# 错误摘要截断长度：COM 的 excepinfo 有时带整段堆栈/HTML 错误页，
# 原样塞给用户界面会撑爆气泡，保留前 200 字够定位问题。
_ERROR_SUMMARY_LIMIT = 200

# 打包版（PyInstaller）的子进程分派标志。冻结后 sys.executable 是 exe 本身，
# `-m core.office_preview` 无法成立（解释器不知道这个模块），只能让 exe 自己
# 重新起来再靠 argv 标志分流；gui_main.py 与 main.py 顶部都按本字面量预检。
WORKER_ARGV_FLAG = "--ycode-office-convert"


class OfficeConvertError(RuntimeError):
    """Office 转 PDF 失败。消息面向用户可读，调用方可直接展示。"""


def _engine_for(path: str) -> "str | None":
    """源路径 → 引擎类型；不支持的扩展名返回 None。"""
    if not path:
        return None
    text = str(path).strip()
    if not text:
        return None
    suffix = os.path.splitext(text)[1].lower()
    return OFFICE_EXTENSIONS.get(suffix)


def is_office_path(path: str) -> bool:
    """路径是否为本模块能转 PDF 的 Office 文档（只按扩展名判断，不查文件存在性）。"""
    return _engine_for(path) is not None


def cached_pdf_path(source_path: str) -> Path:
    """源文件 → 稳定的缓存 PDF 路径。

    命名取 realpath 的 sha1 前 12 位 + 统一小写：Windows 路径大小写不敏感，
    同一文件用不同大小写打开必须落到同一份缓存，否则磁盘上会堆重复 PDF。
    """
    try:
        real = os.path.realpath(os.path.abspath(str(source_path or "").strip()))
    except (OSError, ValueError):
        # 非法路径（超长、含 NUL 等）退回原文：调用方会先撞上"源文件不存在"
        real = str(source_path or "").strip()
    key = hashlib.sha1(real.lower().encode("utf-8", "replace")).hexdigest()[:12]
    return get_preview_dir() / f"office_{key}.pdf"


def _short_error(exc: BaseException) -> str:
    """把 com_error / OSError 压成一行可读摘要，超长截断。"""
    parts: "list[str]" = []
    # com_error 的 str() 是 (hresult, strerror, excepinfo, argerror) 元组，
    # 真正有用的描述在 excepinfo[2]（应用自己给的说明），其次才是 strerror
    info = getattr(exc, "excepinfo", None)
    if isinstance(info, tuple) and len(info) > 2 and info[2]:
        parts.append(str(info[2]))
    strerror = getattr(exc, "strerror", None)
    if strerror and str(strerror) not in parts:
        parts.append(str(strerror))
    if not parts:
        parts.append(str(exc) or exc.__class__.__name__)
    text = " ".join(" ".join(part.split()) for part in parts if part)
    return text[:_ERROR_SUMMARY_LIMIT]


def _decode_output(data) -> str:
    """子进程输出解码：先按 UTF-8，失败回落系统本地编码（中文 Windows 是 GBK）。

    两侧编码不保证一致（终端/PYTHONUTF8 都会改管道编码），所以不写死编码，
    逐个试到能解为止，最后才用替换字符兜底——报错信息宁可少几个字，不能整段丢失。
    """
    if not data:
        return ""
    if isinstance(data, str):
        return data
    raw = bytes(data)
    for encoding in ("utf-8", locale.getpreferredencoding(False) or "utf-8"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _convert_argv(src: Path, dst: Path) -> "list[str]":
    """构造转换子进程命令（源码运行走 `-m`，打包版走 argv 标志）。

    冻结后 sys.executable 是 GUI/CLI 的 exe，`-m core.office_preview` 会被当成
    exe 自己的参数而无效（PyInstaller 的引导器不转发解释器选项），所以改传专用
    标志，由入口文件顶部的预检分发到 run_convert_worker。两条分支共用同一份
    转换实现，父进程侧只看退出码与 stderr，行为保持一致。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, WORKER_ARGV_FLAG, str(src), str(dst)]
    return [sys.executable, "-m", "core.office_preview", "--convert", str(src), str(dst)]


def _run_child_conversion(src: Path, dst: Path, timeout: float) -> None:
    """父进程侧：起子进程转换，带超时；失败/超时一律抛 OfficeConvertError。"""
    command = _convert_argv(src, dst)
    run_kwargs: dict = {
        # 收字节再自己解码：子进程用系统 locale（中文 Windows 是 GBK）写管道，
        # 而 GUI 侧可能继承 UTF-8 环境，两侧编码不一定一致，交给 _decode_output 兜
        "capture_output": True,
        "timeout": timeout,
        "cwd": _PROJECT_ROOT,
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        # GUI 进程起 python 子进程会闪黑框，隐藏掉；Windows 专属常量按平台判断
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        completed = subprocess.run(command, **run_kwargs)
    except subprocess.TimeoutExpired as exc:
        raise OfficeConvertError(f"Office 转换超时（{timeout:g}s）") from exc
    except OSError as exc:
        raise OfficeConvertError(f"无法启动 Office 转换进程：{_short_error(exc)}") from exc

    if completed.returncode != 0:
        detail = " ".join(_decode_output(completed.stderr or completed.stdout).split())
        reason = detail[:_ERROR_SUMMARY_LIMIT] if detail else f"退出码 {completed.returncode}"
        raise OfficeConvertError(f"Office 转换失败：{reason}")


def convert_to_pdf(source_path: str, timeout: float = 180.0) -> Path:
    """把 Office 文档转成 PDF 并返回 PDF 路径（命中缓存时直接返回，不启动子进程）。

    source_path 必须已由调用方通过路径安全校验（core.gui_path_policy），本模块
    只做"文件在不在"与"扩展名熟不熟"的检查。
    """
    text = str(source_path or "").strip()
    if not text:
        raise OfficeConvertError("源文件不存在（路径为空）")
    src = Path(text)
    try:
        source_exists = src.is_file()
    except OSError:
        source_exists = False
    if not source_exists:
        raise OfficeConvertError(f"源文件不存在：{text}")
    if not is_office_path(text):
        raise OfficeConvertError(f"该文件类型不支持转换为 PDF：{src.suffix or text}")

    dst = cached_pdf_path(text)
    try:
        # 秒开路径：缓存 PDF 比源文件新即认为内容一致，直接用
        if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
            return dst
    except OSError:
        # 拿不到 mtime（文件被独占/权限问题）就当作未命中，走一次真实转换
        pass

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 先删旧缓存：转换失败时如果不删，下面"产出校验"会误把上一次的旧 PDF
        # 当成这次的成功结果返回给用户
        dst.unlink(missing_ok=True)
    except OSError:
        raise OfficeConvertError(f"无法清理旧缓存 PDF：{dst}")

    _run_child_conversion(src, dst, timeout)

    try:
        produced = dst.is_file() and dst.stat().st_size > 0
    except OSError:
        produced = False
    if not produced:
        raise OfficeConvertError("转换结束但没有生成 PDF（Office 可能拒绝了该文件）")
    return dst


# ---------------------------------------------------------------------------
# 以下为子进程内部实现：只在这里 import COM 依赖，父进程无需 pywin32
# ---------------------------------------------------------------------------


def _looks_like_missing_app(exc: BaseException) -> bool:
    """判断是不是"本机没装 Office/WPS"这类注册失败。"""
    text = _short_error(exc).lower()
    markers = (
        "invalid class string",
        "class not registered",
        "800401f3",  # REGDB_E_CLASSNOTREG
        "80040154",  # REGDB_E_CLASSNOTREG 的另一种表现
        "无效的类字符串",
        "类没有注册",
    )
    return any(marker in text for marker in markers)


def _dispatch(prog_id: str):
    """DispatchEx：每次起独立进程实例，不抢用户正在编辑的窗口。"""
    import win32com.client as win32

    try:
        return win32.DispatchEx(prog_id)
    except Exception as exc:
        if _looks_like_missing_app(exc):
            raise OfficeConvertError("本机未安装可供转换的 Office / WPS") from exc
        raise OfficeConvertError(f"无法启动 {prog_id}：{_short_error(exc)}") from exc


def _quiet_close(target, *args) -> None:
    """清理调用的兜底：关窗/退出失败不能盖掉真正的转换错误。"""
    if target is None:
        return
    try:
        target.Close(*args)
    except Exception:
        _LOGGER.warning("关闭 Office 文档失败（忽略）", exc_info=True)


def _quiet_quit(app) -> None:
    """必须退出 COM 实例，否则 WINWORD.EXE / EXCEL.EXE / POWERPNT.EXE 残留。"""
    if app is None:
        return
    try:
        app.Quit()
    except Exception:
        _LOGGER.warning("退出 Office 进程失败（忽略）", exc_info=True)


def _convert_with_word(src: str, dst: str) -> None:
    """Word/RTF/ODT → PDF（wdExportFormatPDF = 17）。"""
    from pywintypes import com_error

    app = None
    doc = None
    try:
        app = _dispatch("Word.Application")
        app.Visible = False
        app.DisplayAlerts = 0  # wdAlertsNone：不弹任何对话框
        app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable：彻底禁宏
        doc = app.Documents.Open(src, ReadOnly=True, AddToRecentFiles=False)
        doc.ExportAsFixedFormat(dst, 17)
    except OfficeConvertError:
        raise
    except com_error as exc:
        raise OfficeConvertError(f"Word 转换失败：{_short_error(exc)}") from exc
    except Exception as exc:  # 子进程边界：任何异常都要收敛成可读消息
        raise OfficeConvertError(f"Word 转换失败：{_short_error(exc)}") from exc
    finally:
        _quiet_close(doc, False)  # False = 不保存修改
        _quiet_quit(app)


def _convert_with_excel(src: str, dst: str) -> None:
    """Excel/ODS → PDF（xlTypePDF = 0）；大表导出多页属正常行为，不额外处理。"""
    from pywintypes import com_error

    app = None
    workbook = None
    try:
        app = _dispatch("Excel.Application")
        app.Visible = False
        app.DisplayAlerts = False
        app.AutomationSecurity = 3
        # UpdateLinks=0：不弹"是否更新外部链接"对话框，否则子进程会静默卡住
        workbook = app.Workbooks.Open(src, ReadOnly=True, UpdateLinks=0)
        workbook.ExportAsFixedFormat(0, dst)
    except OfficeConvertError:
        raise
    except com_error as exc:
        raise OfficeConvertError(f"Excel 转换失败：{_short_error(exc)}") from exc
    except Exception as exc:
        raise OfficeConvertError(f"Excel 转换失败：{_short_error(exc)}") from exc
    finally:
        _quiet_close(workbook, False)
        _quiet_quit(app)


def _convert_with_powerpoint(src: str, dst: str) -> None:
    """PowerPoint/ODP → PDF（ppSaveAsPDF = 32）。"""
    from pywintypes import com_error

    app = None
    presentation = None
    try:
        app = _dispatch("PowerPoint.Application")
        # PowerPoint 的 Application.Visible 在部分版本里不给设 False，
        # 这里不碰它，靠 WithWindow=False 避免弹窗
        try:
            presentation = app.Presentations.Open(src, ReadOnly=True, WithWindow=False)
            presentation.SaveAs(dst, 32)
        except Exception as first_error:
            # Office 各版本对无窗口打开支持不一致，退化为有窗口重试一次
            _LOGGER.warning("无窗口打开失败，改为有窗口重试：%s", _short_error(first_error))
            _quiet_close(presentation)
            presentation = None
            try:
                app.Visible = True
            except Exception:
                _LOGGER.warning("设置 PowerPoint 可见失败（忽略）", exc_info=True)
            presentation = app.Presentations.Open(src, ReadOnly=True)
            presentation.SaveAs(dst, 32)
    except OfficeConvertError:
        raise
    except com_error as exc:
        raise OfficeConvertError(f"PowerPoint 转换失败：{_short_error(exc)}") from exc
    except Exception as exc:
        raise OfficeConvertError(f"PowerPoint 转换失败：{_short_error(exc)}") from exc
    finally:
        _quiet_close(presentation)
        _quiet_quit(app)


def _convert_in_child(src: str, dst: str) -> None:
    """子进程内按扩展名分派到对应 COM 引擎。"""
    engine = _engine_for(src)
    if engine == "word":
        _convert_with_word(src, dst)
    elif engine == "excel":
        _convert_with_excel(src, dst)
    elif engine == "powerpoint":
        _convert_with_powerpoint(src, dst)
    else:
        raise OfficeConvertError(f"该文件类型不支持转换为 PDF：{Path(src).suffix}")


def _write_worker_stderr(message: str) -> None:
    """向 stderr 写一行错误；打包版 console=False 时 stderr 可能是 None 或 runtime
    hook 装上的哑对象，任何写入异常都必须吞掉——报错本身不能反过来把子进程打崩，
    父进程侧只靠退出码兜底。"""
    try:
        print(message, file=sys.stderr)
        sys.stderr.flush()
    except Exception:
        pass


def run_convert_worker(src: str, dst: str) -> int:
    """子进程（或冻结 exe 的同名分派）入口：把 src 转成 dst 处的 PDF。

    成功返回 0；失败把可读错误写到 stderr 并返回 1。父进程只认退出码与产出文件，
    所以这里必须把 COM 异常收敛成消息，绝不向上抛（argparse 版 `_main` 也复用它，
    避免两套转换/校验逻辑）。
    """
    try:
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        _convert_in_child(src, dst)
        # 兜底校验：COM 偶尔"成功"返回却没落盘，父进程只认文件
        target = Path(dst)
        if not target.is_file() or target.stat().st_size <= 0:
            raise OfficeConvertError("转换结束但没有生成 PDF（Office 可能未响应）")
        return 0
    except OfficeConvertError as exc:
        _write_worker_stderr(str(exc))
        return 1
    except Exception as exc:  # 子进程边界：任何异常都要收敛成可读消息
        _write_worker_stderr(f"Office 转换失败：{_short_error(exc)}")
        return 1


def _main(argv: "list[str] | None" = None) -> int:
    """源码运行的子进程入口：成功 0，失败 1（错误信息写 stderr 供父进程摘要展示）。"""
    parser = argparse.ArgumentParser(
        prog="core.office_preview",
        description="Office 文档转 PDF（内部子进程入口，勿直接调用）",
    )
    parser.add_argument(
        "--convert",
        nargs=2,
        metavar=("SRC", "DST"),
        help="把 SRC 转换为 DST（PDF 绝对路径）",
    )
    args = parser.parse_args(argv)
    if not args.convert:
        try:
            parser.print_help(sys.stderr)
        except Exception:
            pass
        return 2

    src, dst = args.convert
    return run_convert_worker(src, dst)


if __name__ == "__main__":
    sys.exit(_main())
