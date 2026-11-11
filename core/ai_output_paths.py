"""AI 生成内容目录：AI 产物（生图 / 下载图 / 临时脚本）的统一落点。

设计见 docs/superpowers/specs/2026-09-20-ai-output-dir-design.md。
本模块是落点解析的唯一入口——各调用点不得自行读 config 拼路径，
否则行为会分叉（历史上生图与 fetch_image 直接用进程 cwd，导致
打包版把文件写进安装目录）。
"""

from __future__ import annotations

import os
from pathlib import Path

from core.workspace_context import get_workdir

AI_OUTPUT_DIR_KEY = "ai_output_dir"


class AiOutputDirError(Exception):
    """设置 AI 生成内容目录失败（路径不可用等）。"""


def _read_setting() -> str:
    """读取配置项；读取异常一律按未配置处理（不让配置故障阻断生图）。"""
    try:
        import config

        return str(config._load().get(AI_OUTPUT_DIR_KEY, "") or "").strip()
    except Exception:
        return ""


def _write_setting(value: str) -> bool:
    """持久化配置项；返回是否写盘成功。"""
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


def _expand(value: str) -> Path:
    """展开 ~ 与 %VAR%/$VAR 后返回绝对路径；相对路径按线程工作目录解析。"""
    expanded = os.path.expandvars(os.path.expanduser(value))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = get_workdir() / candidate
    return candidate.resolve()


def resolve_ai_output_dir() -> Path:
    """当前生效的 AI 生成内容目录（绝对路径，保证已存在）。

    规则（规格 4）：
    1. 读配置；未配置 → default_ai_output_dir()
    2. 展开 ~ / 环境变量 → 规范化绝对路径
    3. 当前线程 workdir 命中该目录（相等或子目录）→ 直接返回 workdir
    4. 确保目录存在；配置目录不可用 → 回退默认目录（保证已创建）
    """
    configured = _read_setting()
    fallback = default_ai_output_dir()
    if not configured:
        target = fallback
    else:
        try:
            target = _expand(configured)
        except Exception:
            target = fallback

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
    text = str(value or "").strip()
    if not text:
        # 留空 = 恢复默认：先落盘再按默认规则解析
        if not _write_setting(text):
            raise AiOutputDirError("配置写入失败（~/.ycode/config.json）")
        return resolve_ai_output_dir()
    try:
        target = _expand(text)
    except Exception as error:
        raise AiOutputDirError(f"路径无法解析：{text}") from error
    if not _is_writable(target):
        raise AiOutputDirError(f"目录不可写或无法创建：{target}")
    if not _write_setting(text):
        raise AiOutputDirError("配置写入失败（~/.ycode/config.json）")
    # 直接返回刚校验通过的目录，不回读配置：同一次调用内配置缓存可能尚未刷新，
    # 回读会把这个刚设置好的目录丢掉（设置页会显示成默认值）。
    return target


def is_ai_output_path(path) -> bool:
    """路径是否位于 AI 生成内容目录内（沙盒豁免判定，规格 6.1）。"""
    if not path:
        return False
    try:
        target = Path(os.path.expanduser(str(path))).resolve()
        root = resolve_ai_output_dir()
    except Exception:
        return False
    return target == root or root in target.parents
