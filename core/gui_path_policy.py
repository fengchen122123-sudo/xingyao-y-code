# -*- coding: utf-8 -*-
import logging
import os
from pathlib import Path

from config import YCODE_DIR as CONFIG_YCODE_DIR

_LOGGER = logging.getLogger(__name__)

_CREDENTIAL_BASENAMES = {"config.json", "config.json.bak", "mcp.json", "mcp.json.bak"}

_ACTION_NOUN = {"preview": "预览", "open": "打开", "edit": "编辑"}

# 数据目录注入口：测试可通过设置该变量指向临时目录，避免触碰用户真实 ~/.ycode
_ycode_dir_override: str = ""


def _ycode_data_dir() -> str:
    if _ycode_dir_override:
        return _ycode_dir_override
    try:
        return str(CONFIG_YCODE_DIR)
    except Exception:
        return str(Path.home() / ".ycode")


def _allow_outside_workspace() -> bool:
    try:
        from core.model_manager import load_config

        return bool(load_config().get("allow_outside_workspace", False))
    except Exception:
        return False


def is_credential_config_path(real_path: str) -> bool:
    """路径是否指向数据目录顶层的凭据配置文件（预览/打开一律拒绝）。"""
    if not real_path:
        return False
    try:
        # realpath + normcase 归一化：8.3 短名、大小写、分隔符变体统一收敛
        normalized = os.path.normcase(os.path.realpath(os.path.abspath(real_path)))
        # Win32 文件名会吞掉尾部的点与空格（config.json. -> config.json），
        # 比对前同样剥掉，避免变体绕过
        normalized = normalized.rstrip(". ")
        parent = os.path.normcase(os.path.realpath(os.path.abspath(_ycode_data_dir())))
        if os.path.dirname(normalized) != parent:
            return False
        return os.path.basename(normalized) in _CREDENTIAL_BASENAMES
    except (OSError, ValueError, TypeError):
        return False


def _path_within(path: str, root: str) -> bool:
    try:
        candidate = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        base = os.path.normcase(os.path.realpath(os.path.abspath(root)))
        return os.path.commonpath([candidate, base]) == base
    except (OSError, ValueError, TypeError):
        return False


def gui_path_access_allowed(real_path: str, workspace: str, *, action: str = "preview", session_id: str = None) -> "tuple[bool, str]":
    noun = _ACTION_NOUN.get(action, _ACTION_NOUN["preview"])
    # 1) 凭据文件永远拒绝（安全边界，不受沙盒开关影响）
    if is_credential_config_path(real_path):
        return False, f"安全策略禁止{noun}应用配置文件（含 API Key 等凭据）"
    if workspace == "GLOBAL":
        return True, ""
    if _path_within(real_path, workspace):
        return True, ""
    try:
        from core.ai_output_paths import is_ai_output_path
        if is_ai_output_path(real_path, respect_workdir=False):
            return True, ""
    except Exception:
        _LOGGER.warning("AI 生成内容目录校验不可用，继续按工作区规则处理", exc_info=True)
    try:
        from core.tools import is_trusted_paste_path, is_session_user_attached_path
        if is_trusted_paste_path(real_path) or is_session_user_attached_path(real_path, session_id):
            return True, ""
    except Exception:
        # 导入失败不能静默跳过：受信粘贴/附件不放行会被误报"不在工作区内"，
        # 留痕便于发现 core.tools 装配异常。
        _LOGGER.warning("受信粘贴/会话附件校验不可用，按工作区规则处理", exc_info=True)
    if _allow_outside_workspace():
        return True, ""
    return False, f"文件不在当前工作区内（沙盒开启后可{noun}工作区外文件）"
