"""core/gui_runtime_check.py
GUI 运行环境自检：WebView2 Runtime 探测 + 不依赖 WebView 的原生告警。

背景：Windows 上 GUI 固定走 pywebview 的 edgechromium 后端，强依赖系统
Microsoft Edge WebView2 Runtime。Win11 随系统内置，Win10 默认没有（除非装过
新版 Edge/Office 之类捎带装上）。

该运行时缺失或损坏时，pywebview 的 `edgechromium.on_webview_ready` 在
`IsSuccess=False` 时只写一条日志就 `return`——不导航、不弹窗、不回退；而窗口
本身已创建并显示，用户看到的是 WebView2 控件的 `DefaultBackgroundColor`
（即窗口 background_color，本项目为 #1a1a18）铺满客户区：一块纯色、无内容、
无提示的"黑屏"，且没有任何线索指向原因。

本模块把这种"哑巴黑屏"变成可读结论：
  - 启动前探测 Runtime 版本，缺失时弹原生 MessageBox（附官方下载页，可由用户
    选择暂不安装继续启动，避免误判直接阻断）；
  - 页面始终没加载起来时，同样用原生弹窗给出日志路径。
原生弹窗不依赖 WebView，因此黑屏状态下用户依然看得见。

探测与告警一律 best-effort：任何异常都不得影响启动或退出路径。
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional, Union

logger = logging.getLogger("gui_runtime_check")

# WebView2 Evergreen Runtime 在 EdgeUpdate 客户端项下的 GUID（微软文档固定值）。
WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_DOWNLOAD_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

# 候选注册表位置：机器级的两个视图（64 位进程读第一个，32 位进程只能看到
# WOW6432Node）与用户级（WebView2 允许按用户安装）。任一命中即视为已安装。
WEBVIEW2_REGISTRY_LOCATIONS = (
    ("HKLM", rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
    ("HKLM", rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
    ("HKCU", rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
)

_MB_YESNO = 0x04
_MB_ICONWARNING = 0x30
_MB_ICONINFORMATION = 0x40
# 置顶：这两种告警都发生在"窗口一片空白"的场景下，弹窗若被空白主窗口盖住就
# 等于没提示，必须浮在最上层。
_MB_TOPMOST = 0x00040000
_IDYES = 6


def read_registry_pv(hive_name: str, subkey: str) -> Optional[str]:
    """读取注册表某个子键下的 `pv` 值（WebView2 用它承载版本号）。

    键不存在、无权限、值非字符串等情况一律返回 None：探测属于 best-effort，
    绝不能因为环境差异抛异常打断启动。
    """
    try:
        import winreg

        hive = {
            "HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKCU": winreg.HKEY_CURRENT_USER,
        }.get(hive_name)
        if hive is None:
            return None
        with winreg.OpenKey(hive, subkey) as key:
            value, _ = winreg.QueryValueEx(key, "pv")
    except Exception:
        return None
    return str(value) if value else None


def detect_webview2_runtime() -> Optional[str]:
    """探测系统 WebView2 Runtime 版本号；未安装或非 Windows 返回 None。"""
    if sys.platform != "win32":
        return None
    for hive_name, subkey in WEBVIEW2_REGISTRY_LOCATIONS:
        version = read_registry_pv(hive_name, subkey)
        if version:
            return version
    return None


def _show_message_box(text: str, title: str, flags: int) -> int:
    """原生 MessageBoxW 封装；非 Windows 返回 0（等价"用户未确认"）。"""
    if sys.platform != "win32":
        return 0
    import ctypes

    return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))


def _open_download_page() -> None:
    os.startfile(WEBVIEW2_DOWNLOAD_URL)  # type: ignore[attr-defined]


def warn_missing_webview2_runtime() -> None:
    """未检测到 WebView2 Runtime 时的原生告警（含一键打开官方下载页）。"""
    logger.error(
        "未检测到 Microsoft Edge WebView2 Runtime —— 界面黑屏/空白的最常见原因。"
        "下载：%s",
        WEBVIEW2_DOWNLOAD_URL,
    )
    try:
        result = _show_message_box(
            "未检测到 Microsoft Edge WebView2 Runtime。\n\n"
            "星瑶Code 的图形界面依赖它渲染页面，缺少时窗口会是一片空白（黑屏）。\n\n"
            "是否现在打开官方下载页面？安装后重新启动本程序即可。\n"
            "（选择“否”仍会尝试启动界面。）",
            "星瑶Code · 缺少 WebView2 Runtime",
            _MB_YESNO | _MB_ICONWARNING | _MB_TOPMOST,
        )
        if result == _IDYES:
            _open_download_page()
    except Exception:
        logger.exception("WebView2 Runtime 缺失告警弹窗失败")


def warn_render_failure(log_path: Union[str, "os.PathLike[str]"]) -> None:
    """界面页面始终没渲染出来时的兜底告警：给出日志路径，把黑屏变成线索。"""
    logger.error("界面页面未能加载（窗口可能是一片空白）；运行日志: %s", log_path)
    try:
        _show_message_box(
            "星瑶Code 的界面页面未能加载，窗口可能是一片空白。\n\n"
            "常见原因：\n"
            "  1. Microsoft Edge WebView2 Runtime 缺失或损坏；\n"
            "  2. 安全软件拦截了界面进程。\n\n"
            f"运行日志：{log_path}\n"
            "把这个日志文件发给开发者可快速定位问题。",
            "星瑶Code · 界面加载失败",
            _MB_ICONINFORMATION | _MB_TOPMOST,
        )
    except Exception:
        logger.exception("界面加载失败告警弹窗失败")


def check_webview2_runtime() -> Optional[str]:
    """启动前体检：返回 Runtime 版本；未检测到时告警并返回 None。

    不阻断启动——用户可以选择先不装，程序照常尝试拉起界面（也为未来可能出现的
    非注册表式 WebView2 部署留出余地，避免误判把能用的机器拦在门外）。
    """
    version = detect_webview2_runtime()
    if version:
        logger.info("WebView2 Runtime 已就绪: %s", version)
        return version
    if sys.platform != "win32":
        return None
    warn_missing_webview2_runtime()
    return None
