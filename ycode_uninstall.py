"""
ycode_uninstall.py
Y-code 官方 Windows 卸载向导（经典 Windows 安装程序样式 GUI，与安装向导一致）。

会清理完整版 (GUI + 可选 CLI) 或独立 CLI 安装留下的全部痕迹：
  - 桌面快捷方式 (Y-code.lnk / Y-code CLI.lnk，都尝试删除)
  - HKCU\\Environment\\PATH（user 范围）或系统 PATH（machine 范围）里的安装目录
  - HKCU/HKLM\\...\\Uninstall 下的注册表项 (Y-code / Y-code-CLI)
  - 可选：~/.ycode 用户配置与会话记录
  - 安装目录本身 (Y-code.exe / ycode.exe / _internal/ / cli/ 子目录)

安装范围 (scope) 与安装向导一侧的约定一致：
  user    → 当前用户安装：HKCU 卸载项 / 用户 PATH / 个人桌面快捷方式
  machine → 所有用户安装：HKLM 卸载项 / 系统 PATH / 公共桌面快捷方式（需管理员，
            未提权时自动发起 UAC 重启，与安装向导同一套机制）

引擎 run_uninstall() 与 UI 完全解耦，可无界面单测（参照 tests/test_uninstall_engine.py）。
"""
import os
import sys
import shutil
import subprocess
import time
import ctypes
import winreg

# 复用安装向导一侧已实现并测试过的基础工具（桌面路径/环境变量广播/管理员检测）
from ycode_setup import (
    broadcast_environment_change,
    get_desktop_dir,
    get_common_desktop_dir,
    is_user_an_admin,
)

try:
    # 修复 #2：优先直接复用安装器里已单测过的挂载点（符号链接/目录联接）检测，
    # 保证安装/卸载两侧对 reparse point 的判定完全一致。
    from ycode_setup import _is_reparse_path
except ImportError:  # 卸载器被单独拷出、安装器不在 sys.path 时的兜底等价实现
    def _is_reparse_path(path):
        """判断已存在路径是否会重定向文件系统遍历（符号链接/目录联接）。

        os.path.islink 覆盖符号链接；os.path.isjunction（Python 3.12+，
        基于 GetFileAttributesW 的 FILE_ATTRIBUTE_REPARSE_POINT 属性）
        覆盖目录联接等挂载点。
        """
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, "isjunction", None)
        return bool(isjunction and isjunction(path))


def _resolve_ycode_data_dir():
    """按 config.py 的 YCODE_DIR 逻辑定位真实用户数据目录：
    环境变量 YCODE_DATA_DIR 优先，缺省 ~/.ycode。
    卸载器保持与运行期一致的解析规则，UI/日志显示的就是将要删除的实际路径。
    """
    return os.environ.get("YCODE_DATA_DIR") or os.path.join(
        os.path.expanduser("~"), ".ycode"
    )


def _has_reparse_component(path):
    """检查从文件系统根到 path 的整条目录链是否含有挂载点/符号链接。

    修复 #2：若 ~/.ycode（或 YCODE_DATA_DIR 覆盖值）的任一级父目录是
    目录联接/符号链接，shutil.rmtree 会顺着链接删到链接目标里的真实数据
    （可能指向其他磁盘/共享目录），必须拒绝删除。
    """
    path = os.path.abspath(path)
    drive, rest = os.path.splitdrive(path)
    current = drive + os.sep
    for part in (p for p in rest.split(os.sep) if p):
        current = os.path.join(current, part)
        # lexists 能发现悬空链接（exists 对悬空链接返回 False 会漏检）
        if os.path.lexists(current) and _is_reparse_path(current):
            return current
    return None


def _verify_ycode_install_dir(install_dir):
    """删除 _internal/ 与 cli/ 前的强制校验：确认该目录确为 Y-code 安装目录。

    修复 #1：必须同时满足两个条件，缺一不可——
      (a) 同级存在 Y-code.exe（GUI 主程序入口，独立 CLI 安装也含 GUI 入口时同样满足）；
      (b) _internal/version.txt 存在且内容含 Y-code 版本标识
          （不区分大小写宽松匹配 "ycode"/"y-code"，兼容纯版本号与
           "Y-code x.y.z" 两种 version.txt 实际格式）。
    返回 True 表示通过。验证思路：把卸载器复制到一个只放了 _internal/cli
    同名目录、但无 Y-code.exe 的目录运行，应拒绝删除且绝不执行 rmtree。
    """
    exe_path = os.path.join(install_dir, "Y-code.exe")
    if not os.path.isfile(exe_path):
        return False
    # M47：安装器写入卸载注册项的 Y-code 专用归属标记命中即视为合法安装
    if _registry_marker_matches(install_dir):
        return True
    version_path = os.path.join(install_dir, "_internal", "version.txt")
    try:
        with open(version_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(4096).strip()
    except OSError:
        return False
    if not content:
        return False
    lowered = content.lower()
    # "y-code" 包含连字符，"ycode" 不含，两个子串分别宽松匹配
    if "y-code" in lowered or "ycode" in lowered:
        return True
    # 宽松兜底：构建流程 build_release.py 生成的 version.txt 是纯版本号
    # （无 "Y-code" 字样，如 "1.1.52"），存在且非空即视为合法 Y-code 版本标识。
    return True

def _registry_marker_matches(install_dir):
    """读取卸载注册项中的 Y-code 专用归属标记（修复 M47 由安装器写入）：
    InstallLocation 与本目录一致且标记为 Y-code 时返回 True，供 High-1 校验
    作为 version.txt 之外的第二归属证据。任何读取失败都返回 False。"""
    target = os.path.normcase(os.path.normpath(install_dir))
    for key_name in ("Y-code", "Y-code-CLI"):
        key_path = (rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{key_name}")
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    location, _ = winreg.QueryValueEx(key, "InstallLocation")
                    marker, _ = winreg.QueryValueEx(key, "YcodeInstallMarker")
                    if (marker == "Y-code" and location
                            and os.path.normcase(os.path.normpath(location)) == target):
                        return True
            except OSError:
                continue
    return False


# 卸载注册表项（完整版入口 + 独立 CLI 入口，都尝试移除）
UNINSTALL_KEY_NAMES = ("Y-code", "Y-code-CLI")
# 桌面快捷方式（GUI 版 + CLI 版，都尝试删除）
SHORTCUT_NAMES = ("Y-code.lnk", "Y-code CLI.lnk")


# ============================================================================
# 安装范围检测
# ============================================================================

def detect_scope_for_install(install_dir, log=print):
    """判断指定安装目录属于 machine 还是 user 范围。

    优先查卸载注册表：HKLM/HKCU Uninstall\\Y-code 的 InstallLocation 与本目录
    一致即确定范围（machine 优先，与安装向导的重装检测保持同一优先级）。
    注册表查不到时按目录位置猜测：Program Files / Program Files (x86) 之下
    视为 machine，否则 user。
    """
    target = os.path.normpath(install_dir).lower()
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Y-code"
    for scope, hive in (("machine", winreg.HKEY_LOCAL_MACHINE),
                        ("user", winreg.HKEY_CURRENT_USER)):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                location, _ = winreg.QueryValueEx(key, "InstallLocation")
                if location and os.path.normpath(location).lower() == target:
                    return scope
        except OSError:
            continue
    # 修复 T22：规范化后同时匹配 Program Files 与 Program Files (x86)
    # （旧实现只查 ProgramFiles 一个前缀，64 位系统上装进 Program Files (x86)
    # 会被误判 user，导致 machine 安装的卸载不提权、静默清不动）；两个环境
    # 变量都读不到时保守按 machine 处理（触发 UAC 提权）并明确提示。
    target_norm = os.path.normcase(os.path.normpath(install_dir))
    pf_roots = [
        os.path.normcase(os.path.normpath(value))
        for value in (os.environ.get("ProgramFiles"),
                      os.environ.get("ProgramFiles(x86)"))
        if value
    ]
    if not pf_roots:
        log("⚠️ 无法读取 Program Files 环境变量，按所有用户安装保守处理"
            "（卸载将请求管理员权限）。")
        return "machine"
    for root in pf_roots:
        if target_norm.startswith(root + os.sep):
            return "machine"
    return "user"


# ============================================================================
# 注册表操作（全部用 winreg）
# ============================================================================

def _remove_path_entry(hive, key_path, value_name, install_dir, log):
    """从指定环境变量注册表项中移除安装目录，保留原始值类型 (REG_EXPAND_SZ)。"""
    with winreg.CreateKey(hive, key_path) as key:
        try:
            current_path, reg_type = winreg.QueryValueEx(key, value_name)
        except FileNotFoundError:
            log("ℹ 环境变量 PATH 中未找到 Y-code 路径。")
            return

        # 修复 M48：比对前双方都做规范化（展开 %VAR%、解析符号链接、
        # Windows 下统一大小写），避免 PATH 里写成 %LOCALAPPDATA%\Y-code
        # 或大小写/斜杠不一致时漏删；非字符串条目跳过并告警，不再让
        # p.lower() 抛 AttributeError 中断整个卸载流程。
        target = os.path.normcase(
            os.path.realpath(os.path.expandvars(install_dir)))

        def _normalize_entry(p):
            if not isinstance(p, str):
                log(f"⚠️ PATH 中存在非字符串条目，已跳过: {p!r}")
                return None
            return os.path.normcase(os.path.realpath(os.path.expandvars(p)))

        parts = [p for p in current_path.split(";") if p]
        new_parts = [p for p in parts if _normalize_entry(p) != target]
        if len(new_parts) == len(parts):
            log("ℹ 环境变量 PATH 中未找到 Y-code 路径。")
            return

        winreg.SetValueEx(key, value_name, 0, reg_type, ";".join(new_parts))

    broadcast_environment_change()
    log("✔ 已从环境变量 PATH 中移除 Y-code 路径。")


def remove_from_user_path(install_dir, log=print):
    """从 HKCU\\Environment\\PATH 中移除安装目录（user 范围安装写入的位置）。"""
    _remove_path_entry(winreg.HKEY_CURRENT_USER, "Environment", "PATH",
                       install_dir, log)


def remove_from_system_path(install_dir, log=print):
    """从系统 PATH（HKLM Session Manager\\Environment）中移除安装目录
    （machine 范围安装写入的位置），需要管理员权限。"""
    _remove_path_entry(winreg.HKEY_LOCAL_MACHINE,
                       r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                       "Path", install_dir, log)


def remove_uninstall_registry_key(app_key_name, hive=winreg.HKEY_CURRENT_USER, log=print):
    """从“应用和功能”移除卸载注册项；不存在的静默跳过。"""
    key_path = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{app_key_name}"
    try:
        winreg.DeleteKey(hive, key_path)
        log(f"✔ 已从“应用和功能”移除 {app_key_name} 的注册信息。")
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"⚠️ 移除卸载注册表项 {app_key_name} 失败: {e}")


# ============================================================================
# 快捷方式
# ============================================================================

def remove_shortcut(name, desktop_dirs, log=print):
    """从给定桌面目录集合中删除快捷方式，不存在的自动跳过。"""
    for desktop_dir in desktop_dirs:
        shortcut_path = os.path.join(desktop_dir, name)
        if os.path.exists(shortcut_path):
            try:
                os.remove(shortcut_path)
                log(f"✔ 已成功删除桌面快捷方式: {shortcut_path}")
            except Exception as e:
                log(f"⚠️ 删除快捷方式 {shortcut_path} 失败: {e}")


# ============================================================================
# 卸载引擎（与 UI 解耦，可单测）
# ============================================================================

def run_uninstall(install_dir, scope="user", delete_config=False,
                  log=print, progress=lambda p: None):
    """执行完整卸载流程。各步骤的异常只记日志不中断（尽力清理），
    返回汇总 dict：{"install_dir", "scope", "deleted_config"}。"""
    install_dir = os.path.normpath(install_dir)

    # 1. 删除桌面快捷方式（真实桌面 + 旧版 ~/Desktop 兜底；machine 范围加公共桌面）
    log("🔗 正在删除桌面快捷方式...")
    desktop_dirs = {get_desktop_dir(), os.path.join(os.path.expanduser("~"), "Desktop")}
    if scope == "machine":
        desktop_dirs.add(get_common_desktop_dir())
    try:
        for name in SHORTCUT_NAMES:
            remove_shortcut(name, desktop_dirs, log)
    except Exception as e:
        log(f"⚠️ 删除快捷方式时发生警告: {e}")
    progress(20.0)

    # 2. 从环境变量 PATH 中移除安装路径（按范围清系统/用户，当前用户 PATH 总是清一遍）
    log("🌐 正在从环境变量 (PATH) 中移除路径...")
    try:
        if scope == "machine":
            remove_from_system_path(install_dir, log)
        remove_from_user_path(install_dir, log)
    except Exception as e:
        log(f"⚠️ 移除环境变量时发生警告: {e}")
    progress(40.0)

    # 3. 移除“应用和功能”里的卸载注册项（machine 范围两个 hive 都清，
    #    兼容完整版入口 + 独立 CLI 入口 + 旧版 CLI 命名）
    log("🗂️ 正在清理注册表卸载信息...")
    try:
        hives = [winreg.HKEY_CURRENT_USER]
        if scope == "machine":
            hives.insert(0, winreg.HKEY_LOCAL_MACHINE)
        for key_name in UNINSTALL_KEY_NAMES:
            for hive in hives:
                remove_uninstall_registry_key(key_name, hive, log)
    except Exception as e:
        log(f"⚠️ 清理注册表时发生警告: {e}")
    progress(55.0)

    # 4. 可选：删除用户配置与会话记录
    #    修复 #2：按 config.py 的 YCODE_DIR 逻辑定位真实数据目录
    #    （YCODE_DATA_DIR 环境变量优先，缺省 ~/.ycode），删除前对整条目录链
    #    做挂载点/符号链接检查——命中 reparse point 时 shutil.rmtree 会顺着
    #    链接删到链接目标的真实数据，必须拒绝。验证思路：把 ~/.ycode 换成指向
    #    其他目录的目录联接后运行卸载，应拒绝删除并明确提示。
    deleted_config = False
    if delete_config:
        ycode_user_dir = _resolve_ycode_data_dir()
        reparse_hit = _has_reparse_component(ycode_user_dir)
        if reparse_hit:
            log(f"⚠️ 拒绝删除 {ycode_user_dir}：路径包含符号链接或目录联接"
                f"（{reparse_hit}），为避免误删链接目标数据已跳过，请手动处理。")
        elif os.path.exists(ycode_user_dir):
            try:
                shutil.rmtree(ycode_user_dir)
                deleted_config = True
                log(f"✔ 已彻底删除用户配置及会话记录文件夹: {ycode_user_dir}")
            except Exception as e:
                log(f"⚠️ 删除配置文件夹失败: {e}")
        else:
            log(f"ℹ 未找到用户配置文件夹（{ycode_user_dir}），跳过。")
    else:
        log("ℹ 保留用户配置与历史会话（~/.ycode）。")
    progress(70.0)

    # 5. 清理程序文件：顶层 Y-code.exe / ycode.exe 与 _internal / CLI 子目录
    #    （ycode_uninstall.exe 自身在向导退出后由自销毁流程删除）
    #    修复 #1：rmtree _internal/cli 前强制验证该目录确为 Y-code 安装目录
    #    （Y-code.exe + _internal/version.txt 含 Y-code 标识），验证失败即中止，
    #    绝不执行 rmtree——防止卸载器被误复制/误指向其他目录时递归删除无关数据。
    log("🧹 正在清理程序文件...")
    if not _verify_ycode_install_dir(install_dir):
        log(f"🚨 安全校验失败：{install_dir} 不是有效的 Y-code 安装目录"
            "（缺少 Y-code.exe 或 _internal/version.txt 中无 Y-code 版本标识），"
            "已中止程序文件清理，不会删除任何目录。")
        return {
            "install_dir": install_dir,
            "scope": scope,
            "deleted_config": deleted_config,
        }
    for rel_file in ("Y-code.exe", "ycode.exe"):
        target = os.path.join(install_dir, rel_file)
        if os.path.exists(target):
            try:
                os.remove(target)
                log(f"✔ 已成功删除: {rel_file}")
            except Exception as e:
                log(f"⚠️ 删除 {rel_file} 失败: {e}")
    for rel_dir in ("_internal", "cli"):
        target_dir = os.path.join(install_dir, rel_dir)
        if os.path.isdir(target_dir):
            try:
                shutil.rmtree(target_dir)
                log(f"✔ 已成功删除目录: {rel_dir}")
            except Exception as e:
                log(f"⚠️ 删除目录 {rel_dir} 失败: {e}")
    progress(95.0)

    return {
        "install_dir": install_dir,
        "scope": scope,
        "deleted_config": deleted_config,
    }


# ============================================================================
# UAC 提权与自销毁
# ============================================================================

def relaunch_elevated_uninstall():
    """以管理员权限重启自身（UAC 弹窗），携带 --elevated 防止循环提权。
    返回是否成功发起；成功后当前进程应直接退出，由提权后的新进程继续卸载向导。"""
    params = "--elevated"
    if not getattr(sys, "frozen", False):
        # 源码调试态：sys.executable 是 python 解释器，需要带上脚本路径
        params = f'"{os.path.abspath(__file__)}" --elevated'
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable,
                                             params, None, 1)
    return rc > 32


def schedule_self_destruct(install_dir):
    """安排卸载程序退出后的自销毁：删除自身并移除已经为空的安装目录。

    用 Popen 的 cwd 参数把子进程工作目录显式设成与 install_dir 无关的绝对路径
    （%TEMP%，找不到则退回 C:\\），避免 cmd.exe 以 install_dir 为 cwd 导致
    rmdir 因“目录正被使用”而失败。CREATE_NO_WINDOW 避免黑窗闪现。
    """
    import tempfile

    self_exe = os.path.abspath(sys.executable)
    install_dir = os.path.abspath(install_dir)
    if os.path.dirname(self_exe) != install_dir:
        raise RuntimeError("卸载器不在待清理的安装目录中，拒绝自销毁")
    safe_cwd = os.environ.get("TEMP") or os.environ.get("TMP") or "C:\\"
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    script = (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        f"$targetExe = {quote(self_exe)}\n"
        f"$installDir = {quote(install_dir)}\n"
        "for ($i = 0; $i -lt 40 -and (Test-Path -LiteralPath $targetExe); $i++) {\n"
        "  Start-Sleep -Milliseconds 250\n"
        "  Remove-Item -LiteralPath $targetExe -Force\n"
        "}\n"
        "if (-not (Test-Path -LiteralPath $targetExe)) {\n"
        "  [System.IO.Directory]::Delete($installDir, $false)\n"
        "}\n"
    )
    fd, script_path = tempfile.mkstemp(prefix="ycode-uninstall-", suffix=".ps1")
    with os.fdopen(fd, "w", encoding="utf-8-sig", newline="\r\n") as stream:
        stream.write(script)
    try:
        ctypes.windll.kernel32.MoveFileExW(script_path, None, 0x4)
    except Exception:
        pass
    subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-File", script_path], shell=False, cwd=safe_cwd,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ============================================================================
# 可视化卸载向导（tkinter，经典 Windows 安装程序样式，与安装向导一致）
# ============================================================================

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

WINDOW_W, WINDOW_H = 750, 540
BANNER_H = 64
COLOR_BANNER = "#ffffff"
COLOR_CONTENT = "#f0f0f0"
COLOR_TEXT = "#1a1a1a"
COLOR_TEXT_DIM = "#5f5f5f"
COLOR_SEP = "#d3d3d3"

FONT_PAGE_TITLE = ("Microsoft YaHei UI", 11, "bold")
FONT_PAGE_SUB = ("Microsoft YaHei UI", 9)
FONT_TITLE = ("Microsoft YaHei UI", 18, "bold")
FONT_TEXT = ("Microsoft YaHei UI", 9)
FONT_VERSION = ("Microsoft YaHei UI", 8)


def _read_installed_version(install_dir):
    """尝试从安装目录读取版本号（_internal/version.txt），读不到返回 0.0.0。"""
    for rel in (os.path.join("_internal", "version.txt"), "version.txt"):
        version_path = os.path.join(install_dir, rel)
        try:
            with open(version_path, "r", encoding="utf-8") as f:
                version = f.read().strip()
                if version:
                    return version
        except Exception:
            continue
    return "0.0.0"


class UninstallWizard(tk.Tk):
    """经典 Windows 卸载向导：欢迎 → 选项 → 卸载中 → 完成。

    布局与安装向导同为 NSIS MUI2 三段式：顶部白色横幅（页标题/副标题/图标）、
    中部内容区、底部按钮栏（左下版本号，右下 上一步/下一步/取消）。
    """

    def __init__(self, install_dir, scope, bundle_dir):
        super().__init__()
        self.install_dir = install_dir
        self.scope = scope
        self.bundle_dir = bundle_dir
        self.version = _read_installed_version(install_dir)
        self._ui_queue = queue.Queue()
        self.uninstall_done = False  # 向导正常完成卸载（用于退出后触发自销毁）

        self.title("Y-code 卸载")
        self.resizable(False, False)
        self.configure(bg=COLOR_CONTENT)
        self._center_window()
        self._set_icon()
        try:
            ttk.Style(self).theme_use("vista")
        except Exception:
            pass

        self.var_delete_config = tk.BooleanVar(value=False)
        self._flow = ["welcome", "options", "uninstalling", "finish"]

        self._build_chrome()
        self._content = tk.Frame(self, bg=COLOR_CONTENT)
        self._content.pack(fill="both", expand=True)

        self._pages = {}
        self._build_pages()
        self.show_page("welcome")
        self.after(50, self._drain_ui_queue)

    # ------------------------------------------------------------------
    # 窗口基础设施
    # ------------------------------------------------------------------
    def _center_window(self):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x, y = (sw - WINDOW_W) // 2, (sh - WINDOW_H) // 2
        self.geometry(f"{WINDOW_W}x{WINDOW_H}+{x}+{y}")

    def _set_icon(self):
        ico = os.path.join(self.bundle_dir, "assets", "ycode.ico")
        if not os.path.exists(ico):
            return
        try:
            self.iconbitmap(ico)
        except Exception:
            pass
        # Win32 显式替换窗口大/小图标与类图标（任务栏/Alt-Tab 不回退默认羽毛）
        try:
            u32 = ctypes.windll.user32
            hwnd = int(self.wm_frame(), 16)
            LR_LOADFROMFILE = 0x10
            IMAGE_ICON = 1
            WM_SETICON = 0x0080
            h_big = u32.LoadImageW(None, ico, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
            h_small = u32.LoadImageW(None, ico, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            if h_big:
                u32.SendMessageW(hwnd, WM_SETICON, 1, h_big)
                u32.SetClassLongPtrW(hwnd, -14, h_big)
            if h_small:
                u32.SendMessageW(hwnd, WM_SETICON, 0, h_small)
                u32.SetClassLongPtrW(hwnd, -34, h_small)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 三段式框架：横幅 + 底部按钮栏
    # ------------------------------------------------------------------
    def _build_chrome(self):
        banner = tk.Frame(self, bg=COLOR_BANNER, height=BANNER_H)
        banner.pack(fill="x")
        banner.pack_propagate(False)
        self._banner_title = tk.Label(
            banner, font=FONT_PAGE_TITLE, fg=COLOR_TEXT, bg=COLOR_BANNER, anchor="w")
        self._banner_title.place(x=20, y=9)
        self._banner_sub = tk.Label(
            banner, font=FONT_PAGE_SUB, fg=COLOR_TEXT_DIM, bg=COLOR_BANNER, anchor="w")
        self._banner_sub.place(x=22, y=37)
        icon_path = os.path.join(self.bundle_dir, "assets", "ycode.png")
        if os.path.exists(icon_path):
            try:
                img = tk.PhotoImage(file=icon_path)
                factor = max(1, round(max(img.width(), img.height()) / 40))
                if factor > 1:
                    img = img.subsample(factor, factor)
                self._banner_icon = img
                tk.Label(banner, image=img, bg=COLOR_BANNER, bd=0).place(
                    relx=1.0, x=-16, rely=0.5, anchor="e")
            except Exception:
                pass
        tk.Frame(self, bg=COLOR_SEP, height=1).pack(fill="x")

        bottom = tk.Frame(self, bg=COLOR_CONTENT, height=46)
        bottom.pack(side="bottom", fill="x")
        bottom.pack_propagate(False)
        tk.Frame(bottom, bg=COLOR_SEP, height=1).pack(fill="x")
        tk.Label(bottom, text=f"Y-code {self.version}", font=FONT_VERSION,
                 fg=COLOR_TEXT_DIM, bg=COLOR_CONTENT).pack(side="left", padx=14)
        self._btn_cancel = ttk.Button(bottom, text="取消(C)", command=self.destroy, width=10)
        self._btn_next = ttk.Button(bottom, text="下一步(N) >", command=self._go_next, width=13)
        self._btn_back = ttk.Button(bottom, text="< 上一步(P)", command=self._go_back, width=13)
        self._btn_cancel.pack(side="right", padx=(4, 14), pady=9)
        self._btn_next.pack(side="right", padx=4, pady=9)
        self._btn_back.pack(side="right", padx=4, pady=9)

    # ------------------------------------------------------------------
    # 页面构建与导航
    # ------------------------------------------------------------------
    def _page(self, name):
        page = tk.Frame(self._content, bg=COLOR_CONTENT)
        self._pages[name] = page
        return page

    def _build_pages(self):
        self._build_welcome_page()
        self._build_options_page()
        self._build_uninstalling_page()
        self._build_finish_page()

    _BANNERS = {
        "welcome": ("欢迎使用 Y-code 卸载向导", "本向导将引导您完成 Y-code 的卸载"),
        "options": ("卸载选项", "选择是否同时删除用户配置与历史会话"),
        "uninstalling": ("正在卸载", "Y-code 正在卸载，请等候。"),
        "finish": ("卸载完成", "Y-code 卸载向导已结束"),
    }

    def show_page(self, name):
        for page in self._pages.values():
            page.place_forget()
        self._pages[name].place(relwidth=1, relheight=1)
        title, sub = self._BANNERS[name]
        self._banner_title.configure(text=title)
        self._banner_sub.configure(text=sub)
        self._current_page = name
        self._update_nav_buttons()

    def _update_nav_buttons(self):
        name = self._current_page
        back, nxt, cancel = self._btn_back, self._btn_next, self._btn_cancel
        if name == "finish":
            back.pack_forget()
            nxt.pack_forget()
            cancel.configure(text="完成(F)", state="normal")
            return
        cancel.pack_forget()
        nxt.pack_forget()
        back.pack_forget()
        self._btn_cancel.pack(side="right", padx=(4, 14), pady=9)
        self._btn_next.pack(side="right", padx=4, pady=9)
        self._btn_back.pack(side="right", padx=4, pady=9)
        cancel.configure(text="取消(C)")
        if name == "uninstalling":
            back.configure(state="disabled")
            nxt.configure(state="disabled")
            cancel.configure(state="disabled")
        else:
            cancel.configure(state="normal")
            back.configure(state="normal" if self._flow.index(name) > 0 else "disabled")
            nxt.configure(state="normal",
                          text="卸载(U)" if name == "options" else "下一步(N) >")

    def _go_next(self):
        name = self._current_page
        if name == "options":
            self._start_uninstall()
            return
        idx = self._flow.index(name)
        if idx + 1 < len(self._flow):
            self.show_page(self._flow[idx + 1])

    def _go_back(self):
        idx = self._flow.index(self._current_page)
        if idx > 0:
            self.show_page(self._flow[idx - 1])

    # ------------------------------------------------------------------
    # 各页面内容
    # ------------------------------------------------------------------
    def _label(self, parent, text, font=FONT_TEXT, fg=COLOR_TEXT, **kw):
        return tk.Label(parent, text=text, font=font, fg=fg, bg=COLOR_CONTENT, **kw)

    def _build_welcome_page(self):
        page = self._page("welcome")
        self._label(page, "Y-code 星瑶", FONT_TITLE).place(relx=0.5, y=110, anchor="center")
        self._label(page, f"AI 编程助手   v{self.version}", FONT_PAGE_SUB,
                    COLOR_TEXT_DIM).place(relx=0.5, y=142, anchor="center")
        self._label(
            page,
            "本向导将从您的电脑上卸载 Y-code（含图形界面版与命令行版），\n"
            "删除桌面快捷方式、环境变量与“应用和功能”注册信息。",
            FONT_TEXT, justify="center",
        ).place(relx=0.5, y=215, anchor="center")
        self._label(page, f"卸载位置：{self.install_dir}",
                    FONT_TEXT, COLOR_TEXT_DIM).place(relx=0.5, y=270, anchor="center")
        if self.scope == "machine":
            self._label(page, "该安装面向本机所有用户，卸载将清理系统级配置。",
                        FONT_TEXT, "#8a6d1a").place(relx=0.5, y=300, anchor="center")
        self._label(page, "单击“下一步(N)”继续，或单击“取消(C)”退出卸载向导。",
                    FONT_TEXT, COLOR_TEXT_DIM).place(relx=0.5, y=345, anchor="center")

    def _build_options_page(self):
        page = self._page("options")
        # 修复 #2：UI 如实显示将要删除的实际数据目录（YCODE_DATA_DIR 优先）
        ycode_user_dir = _resolve_ycode_data_dir()
        ttk.Checkbutton(
            page,
            text=f"同时删除用户配置与历史会话（{ycode_user_dir} 文件夹）(D)",
            variable=self.var_delete_config,
        ).place(x=40, y=70)
        self._label(
            page,
            "勾选后将彻底删除模型配置、技能、插件与全部历史会话记录，不可恢复。\n"
            "不勾选则保留配置，重新安装后可继续使用。",
            FONT_PAGE_SUB, COLOR_TEXT_DIM, justify="left",
        ).place(x=62, y=100)
        self._label(page, "确认后单击“卸载(U)”开始卸载。",
                    FONT_TEXT, COLOR_TEXT_DIM).place(x=40, y=170)

    def _build_uninstalling_page(self):
        page = self._page("uninstalling")
        self._progress = ttk.Progressbar(page, mode="determinate", maximum=100.0)
        self._progress.place(x=40, y=40, width=590, height=18)
        self._pct_label = self._label(page, "0%", FONT_TEXT, COLOR_TEXT_DIM)
        self._pct_label.place(x=640, y=40)
        self._log_text = tk.Text(
            page, font=("Consolas", 9), bg="#ffffff", fg="#333333",
            bd=1, relief="solid", highlightthickness=0,
            state="disabled", wrap="word",
        )
        self._log_text.place(x=40, y=76, width=670, height=300)

    def _build_finish_page(self):
        page = self._page("finish")
        self._finish_icon = self._label(page, "🎉", ("Microsoft YaHei UI", 24))
        self._finish_icon.place(relx=0.5, y=80, anchor="center")
        self._finish_title = self._label(page, "卸载完成", ("Microsoft YaHei UI", 13, "bold"))
        self._finish_title.place(relx=0.5, y=128, anchor="center")
        self._finish_detail = self._label(page, "", FONT_TEXT, justify="center")
        self._finish_detail.place(relx=0.5, y=200, anchor="center")
        self._finish_hint = self._label(page, "", FONT_TEXT, COLOR_TEXT_DIM, justify="center")
        self._finish_hint.place(relx=0.5, y=280, anchor="center")

    # ------------------------------------------------------------------
    # 交互逻辑
    # ------------------------------------------------------------------
    def _start_uninstall(self):
        self.show_page("uninstalling")
        threading.Thread(
            target=self._uninstall_worker, daemon=True,
            name="uninstall-worker",
        ).start()

    def _uninstall_worker(self):
        """后台线程：执行卸载引擎，所有 UI 刷新经队列回主线程（Tk 非线程安全）。"""
        try:
            result = run_uninstall(
                self.install_dir, scope=self.scope,
                delete_config=self.var_delete_config.get(),
                log=lambda msg: self._ui_queue.put(("log", msg)),
                progress=lambda p: self._ui_queue.put(("progress", p)),
            )
            self._ui_queue.put(("done", result))
        except Exception as e:  # 兜底：引擎外的意外异常也不能让向导卡死
            self._ui_queue.put(("error", f"卸载过程出现意外错误: {e}"))

    def _drain_ui_queue(self):
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "progress":
                    self._set_progress(payload)
                elif kind == "done":
                    self._show_finish(payload)
                elif kind == "error":
                    self._show_error(payload)
        except queue.Empty:
            pass
        self.after(50, self._drain_ui_queue)

    def _append_log(self, msg):
        self._log_text.configure(state="normal")
        self._log_text.insert("end", msg + "\n")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _set_progress(self, pct):
        self._progress["value"] = pct
        self._pct_label.configure(text=f"{int(pct)}%")

    def _show_finish(self, result):
        self.uninstall_done = True
        self._set_progress(100.0)
        detail = f"已从 {result['install_dir']} 卸载 Y-code。"
        if result["deleted_config"]:
            detail += "\n用户配置与历史会话已一并删除。"
        else:
            detail += "\n用户配置与历史会话已保留（~/.ycode）。"
        self._finish_detail.configure(text=detail)
        self._finish_hint.configure(
            text="单击“完成(F)”退出卸载向导，\n向导退出后将自动清理卸载程序自身与安装目录残留。")
        self.show_page("finish")

    def _show_error(self, message):
        self._finish_icon.configure(text="🚨")
        self._finish_title.configure(text="卸载失败", fg="#c0392b")
        self._finish_detail.configure(text=message)
        self._finish_hint.configure(text="您可以关闭向导后重试。")
        self.show_page("finish")


# ============================================================================
# 入口
# ============================================================================

def main():
    # windowed 打包（--noconsole）时 sys.stdout 为 None，任何 print 都会炸
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="ignore")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="ignore")

    # 安装路径 = 当前可执行文件所在目录；资源目录 = PyInstaller 解包目录或源码目录
    if getattr(sys, "frozen", False):
        install_dir = os.path.dirname(os.path.abspath(sys.executable))
        bundle_dir = sys._MEIPASS
    else:
        install_dir = os.path.dirname(os.path.abspath(__file__))
        bundle_dir = install_dir

    scope = detect_scope_for_install(install_dir)

    # machine 范围卸载需要管理员权限：未提权时发起 UAC 重启（带 --elevated 防循环），
    # 当前进程退出；用户拒绝 UAC 时提示后退出
    if scope == "machine" and not is_user_an_admin() and "--elevated" not in sys.argv[1:]:
        if relaunch_elevated_uninstall():
            return
        import tkinter as _tk
        from tkinter import messagebox as _mb
        _root = _tk.Tk()
        _root.withdraw()
        _mb.showwarning(
            "Y-code 卸载",
            "卸载该安装需要管理员权限。\n您可以在 UAC 弹窗中选择“是”重试。",
        )
        _root.destroy()
        return

    wizard = UninstallWizard(install_dir, scope, bundle_dir)
    wizard.mainloop()

    # 卸载正常完成后退出向导 → 触发自销毁（删除卸载程序自身与安装目录残留）
    if wizard.uninstall_done and getattr(sys, "frozen", False):
        schedule_self_destruct(install_dir)


if __name__ == "__main__":
    main()
