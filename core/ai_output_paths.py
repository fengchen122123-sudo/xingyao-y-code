from __future__ import annotations

import os
from pathlib import Path

from core.workspace_context import get_workdir

AI_OUTPUT_DIR_KEY = "ai_output_dir"


class AiOutputDirError(Exception):
    pass


def _read_setting() -> str:
    try:
        import config

        return str(config._load().get(AI_OUTPUT_DIR_KEY, "") or "").strip()
    except Exception:
        return ""


def _write_setting(value: str) -> bool:
    try:
        import config

        return bool(config.save_config_value(AI_OUTPUT_DIR_KEY, value))
    except Exception:
        return False


def default_ai_output_dir() -> Path:
    """默认目录：<YCODE_DIR>/tmp（尊重 YCODE_DATA_DIR 覆盖）。"""
    import config

    return Path(config.YCODE_DIR) / "tmp"


def _is_writable(path: Path) -> bool:
    """目录可写探测：不可写目录会让生图静默失败，必须在解析阶段拦下。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(str(path), os.W_OK)


def _strip_outer_quotes(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        text = text[1:-1].strip()
    return text


def _expand(value: str, *, base_dir: Path | None = None) -> Path:
    text = _strip_outer_quotes(value)
    expanded = os.path.expandvars(os.path.expanduser(text))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = (base_dir if base_dir is not None else get_workdir()) / candidate
    return candidate.resolve()


def resolve_ai_output_dir(*, respect_workdir: bool = True) -> Path:
    configured = _read_setting()
    fallback = default_ai_output_dir()
    base_dir = None if respect_workdir else fallback.parent
    if not configured:
        target = fallback
    else:
        try:
            target = _expand(configured, base_dir=base_dir)
        except Exception:
            target = fallback

    if respect_workdir:
        workdir = get_workdir()
        if workdir == target or target in workdir.parents:
            return workdir

    if _is_writable(target):
        return target
    if target != fallback:
        # 配置目录不可用 → 回退默认目录。这里只借 _is_writable 保证默认目录已创建，
        # 不用它的返回值做判定：默认目录由本模块自建，宁可在默认目录暴露写盘错误，
        # 也不能把文件写进用户配置的那个不可用路径。
        _is_writable(fallback)
        return fallback
    # 默认目录自身不可用（极端情况）：返回原值，由调用方在写盘时报出真实错误
    return target


def get_ai_output_dir_setting() -> str:
    """原始配置值（未配置返回空串），供设置页回显。"""
    return _read_setting()


def set_ai_output_dir_setting(value: str) -> Path:
    """保存配置并返回生效目录；目录不可用时抛 AiOutputDirError 且不写盘。"""
    text = _strip_outer_quotes(value)
    if not text:
        if not _write_setting(text):
            raise AiOutputDirError("配置写入失败（~/.ycode/config.json）")
        return resolve_ai_output_dir()
    try:
        expanded = os.path.expandvars(os.path.expanduser(text))
        is_abs = Path(expanded).is_absolute()
        target = _expand(text)
    except Exception as error:
        raise AiOutputDirError(f"路径无法解析：{text}") from error
    if not _is_writable(target):
        raise AiOutputDirError(f"目录不可写或无法创建：{target}")
    stored_value = text if is_abs else str(target)
    if not _write_setting(stored_value):
        raise AiOutputDirError("配置写入失败（~/.ycode/config.json）")
    return target


def is_ai_output_path(path, *, respect_workdir: bool = True) -> bool:
    if not path or not _strip_outer_quotes(str(path)):
        return False
    try:
        base_dir = None if respect_workdir else default_ai_output_dir().parent
        target = _expand(str(path), base_dir=base_dir)
        root = resolve_ai_output_dir(respect_workdir=respect_workdir)
    except Exception:
        return False
    return target == root or root in target.parents


def resolve_ai_output_save_dir(save_dir: str | None = None) -> str:
    """解析媒体/文件生成工具的保存目录。

    未传或传空串时落入 AI 生成内容目录；传入相对路径时基于当前工作区解析。
    """
    text = _strip_outer_quotes(save_dir or "") if save_dir is not None else ""
    if not text:
        return str(resolve_ai_output_dir())
    return str(_expand(text))


def is_ai_output_write_target(path) -> bool:
    """判断路径是否属于临时 AI 输出目录的安全免审批写目标。

    若当前工作区位于配置输出目录的真子目录下（用户显式打开的子项目），
    该子项目内的文件仍按普通项目文件处理，避免静默绕过确认门禁。
    """
    if not path or not _strip_outer_quotes(str(path)):
        return False
    try:
        target = _expand(str(path))
        root = resolve_ai_output_dir()
        if not (target == root or root in target.parents):
            return False
        workdir = get_workdir()
        base_ai_dir = resolve_ai_output_dir(respect_workdir=False)
        if base_ai_dir in workdir.parents and (
            target == workdir or workdir in target.parents
        ):
            return False
    except Exception:
        return False
    return True
