# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import hashlib
import locale
import logging
import os
import subprocess
import sys
from pathlib import Path

from core.web_preview import get_preview_dir

_LOGGER = logging.getLogger(__name__)

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

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)

_ERROR_SUMMARY_LIMIT = 200

# 打包版（PyInstaller）的子进程分派标志。冻结后 sys.executable 是 exe 本身，
# `-m core.office_preview` 无法成立（解释器不知道这个模块），只能让 exe 自己
# 重新起来再靠 argv 标志分流；gui_main.py 与 main.py 顶部都按本字面量预检。
WORKER_ARGV_FLAG = "--ycode-office-convert"


class OfficeConvertError(RuntimeError):
    """Office 转 PDF 失败。消息面向用户可读，调用方可直接展示。"""


def _engine_for(path: str) -> "str | None":
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
        real = str(source_path or "").strip()
    key = hashlib.sha1(real.lower().encode("utf-8", "replace")).hexdigest()[:12]
    return get_preview_dir() / f"office_{key}.pdf"


def _short_error(exc: BaseException) -> str:
    parts: "list[str]" = []
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
    candidates: list[str] = ["utf-8"]
    for getter in (
        getattr(locale, "getencoding", None),
        lambda: locale.getpreferredencoding(False),
    ):
        if callable(getter):
            enc = getter() or ""
            if enc and enc.lower() not in {c.lower() for c in candidates}:
                candidates.append(enc)
    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _convert_argv(src: Path, dst: Path) -> "list[str]":
    if getattr(sys, "frozen", False):
        return [sys.executable, WORKER_ARGV_FLAG, str(src), str(dst)]
    return [sys.executable, "-m", "core.office_preview", "--convert", str(src), str(dst)]


def _run_child_conversion(src: Path, dst: Path, timeout: float) -> None:
    command = _convert_argv(src, dst)
    run_kwargs: dict = {
        # 收字节再自己解码：子进程用系统 locale（中文 Windows 是 GBK）写管道，
        # 而 GUI 侧可能继承 UTF-8 环境，两侧编码不一定一致，交给 _decode_output 兜
        "capture_output": True,
        "timeout": timeout,
        "cwd": _PROJECT_ROOT,
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
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
        if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
            return dst
    except OSError:
        # 拿不到 mtime（文件被独占/权限问题）就当作未命中，走一次真实转换
        pass

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
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


def _looks_like_missing_app(exc: BaseException) -> bool:
    text = _short_error(exc).lower()
    markers = (
        "invalid class string",
        "class not registered",
        "800401f3",
        "80040154",
        "无效的类字符串",
        "类没有注册",
    )
    return any(marker in text for marker in markers)


def _dispatch(prog_id: str):
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
    from pywintypes import com_error

    app = None
    doc = None
    try:
        app = _dispatch("Word.Application")
        app.Visible = False
        app.DisplayAlerts = 0
        app.AutomationSecurity = 3
        doc = app.Documents.Open(src, ReadOnly=True, AddToRecentFiles=False)
        doc.ExportAsFixedFormat(dst, 17)
    except OfficeConvertError:
        raise
    except com_error as exc:
        raise OfficeConvertError(f"Word 转换失败：{_short_error(exc)}") from exc
    except Exception as exc:
        raise OfficeConvertError(f"Word 转换失败：{_short_error(exc)}") from exc
    finally:
        _quiet_close(doc, False)
        _quiet_quit(app)


def _convert_with_excel(src: str, dst: str) -> None:
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
    except Exception as exc:
        _write_worker_stderr(f"Office 转换失败：{_short_error(exc)}")
        return 1


def _main(argv: "list[str] | None" = None) -> int:
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
