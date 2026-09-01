# -*- coding: utf-8 -*-
"""GUI 预览 / 打开路径的统一访问策略（read_file_preview / open_path 共用）。

历史行为：两个接口都硬性要求"路径必须落在当前会话工作区内"，GLOBAL 自由
对话会话一律拒绝，且不跟随沙盒开关——与 Agent 工具层（core.tools /
core.harness.tool_adapters）"沙盒开启即放行工作区外"的语义不一致，用户在
对话里点击模型输出的工作区外文件链接必现"文件不在当前工作区内"。

统一策略（凭据安全边界不变）：
1. 凭据文件永远拒绝：~/.ycode 数据目录顶层的 config.json / mcp.json（含
   .bak）含 API Key 等敏感配置，预览内容会直接泄露明文，无论沙盒状态。
2. GLOBAL 自由对话会话：无工作区概念，放行（仅受凭据规则约束）。
3. 路径在会话工作区内：放行。
4. 工作区外：跟随沙盒开关 allow_outside_workspace（/sandbox on 或 GUI
   "沙盒: 解除"时放行，与 Agent read_file 行为一致）。

本模块只依赖标准库与 config，不引入 PIL / webview / gui_bridge 等重组件，
便于独立测试（gui_bridge 模块顶层 import PIL，缺依赖环境下无法导入）。
"""
import os
from pathlib import Path

from config import YCODE_DIR as CONFIG_YCODE_DIR

# 与 core.harness.tool_adapters 的凭据口径一致：数据目录顶层的配置文件
# （含 .bak 备份）含 API Key / MCP server 配置，GUI 预览/打开一律拒绝
_CREDENTIAL_BASENAMES = {"config.json", "config.json.bak", "mcp.json", "mcp.json.bak"}

# 数据目录注入口：测试可通过设置该变量指向临时目录，避免触碰用户真实 ~/.ycode
_ycode_dir_override: str = ""


def _ycode_data_dir() -> str:
    """返回 ycode 数据目录（默认 ~/.ycode，YCODE_DATA_DIR 环境变量可覆盖）。"""
    if _ycode_dir_override:
        return _ycode_dir_override
    try:
        return str(CONFIG_YCODE_DIR)
    except Exception:
        return str(Path.home() / ".ycode")


def _allow_outside_workspace() -> bool:
    """读取沙盒开关（CLI /sandbox 与 GUI 沙盒解除共享同一配置键）。"""
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
    """commonpath 前缀判断（与 gui_bridge._path_is_within 同语义，避免反向导入）。"""
    try:
        candidate = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        base = os.path.normcase(os.path.realpath(os.path.abspath(root)))
        return os.path.commonpath([candidate, base]) == base
    except (OSError, ValueError, TypeError):
        return False


def gui_path_access_allowed(real_path: str, workspace: str, *, action: str = "preview") -> "tuple[bool, str]":
    """判断 GUI 预览 / 打开路径是否放行。

    参数:
        real_path: 已解析的绝对路径（调用方先过 _resolve_real_path）
        workspace: 当前会话工作目录；GLOBAL 自由对话会话传 "GLOBAL"
        action: "preview" 或 "open"，仅影响拒绝文案
    返回:
        (是否放行, 拒绝原因)；放行时第二项为空字符串。
    """
    noun = "打开" if action == "open" else "预览"
    # 1) 凭据文件永远拒绝（安全边界，不受沙盒开关影响）
    if is_credential_config_path(real_path):
        return False, f"安全策略禁止{noun}应用配置文件（含 API Key 等凭据）"
    # 2) GLOBAL 自由对话会话：无工作区约束，放行
    if workspace == "GLOBAL":
        return True, ""
    # 3) 会话工作区内：放行
    if _path_within(real_path, workspace):
        return True, ""
    # 4) 工作区外：跟随沙盒开关
    if _allow_outside_workspace():
        return True, ""
    return False, f"文件不在当前工作区内（沙盒开启后可{noun}工作区外文件）"
