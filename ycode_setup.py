"""
ycode_setup.py
Y-code 官方 Windows 安装向导（可视化 GUI 版，单一源码，靠内嵌的 setup_mode.txt 区分两种模式）：

  full 模式（默认）：完整版安装包。默认安装图形界面版 (Y-code.exe)，
                     安装选项页可勾选是否附带安装命令行版 (ycode.exe / ycode 命令)、
                     是否创建桌面快捷方式。
                     若用户当时选择不装命令行版，会把内嵌的 TUI 独立安装向导
                     释放到安装目录的 tui\\ 子目录下，方便以后随时补装。

  tui 模式：TUI 独立安装包。只安装命令行版，用于用户后续补装，或单独分发
            给只想用命令行的用户。

界面：tkinter（标准库，零新增依赖）+ 内嵌虚化背景图 assets/setup_bg.png。
安装引擎与界面解耦：run_install() 在后台线程执行，经队列回调刷新 UI，
可直接被 pytest 无界面测试。

模式来源：build_release.py 用 --onefile 分别打包本脚本两次，每次都通过
--add-data 内嵌一个不同内容的 setup_mode.txt（'full' 或 'tui'）。如果直接用
项目根目录下的 ycode_setup.spec 手动构建（不经过 build_release.py），因为
没有内嵌 setup_mode.txt，会自动按 'full' 模式运行。
"""
import os
import queue
import shutil
import subprocess
import sys
import threading
import winreg


# ============================================================================
# 打包资源读取
# ============================================================================

def get_bundle_dir():
    """打包后 (frozen) 返回 PyInstaller 单文件运行时的解压临时目录，本地调试返回脚本所在目录。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


def read_setup_mode(bundle_dir):
    marker_path = os.path.join(bundle_dir, "setup_mode.txt")
    if os.path.exists(marker_path):
        try:
            with open(marker_path, "r", encoding="utf-8") as f:
                mode = f.read().strip().lower()
            if mode in ("full", "tui"):
                return mode
        except Exception:
            pass
    # 未内嵌 marker 文件时（例如直接手动 `pyinstaller ycode_setup.spec`），
    # 默认按完整版处理。
    return "full"


def read_bundled_version(bundle_dir):
    version_path = os.path.join(bundle_dir, "version.txt")
    try:
        with open(version_path, "r", encoding="utf-8") as f:
            version = f.read().strip()
            if version:
                return version
    except Exception:
        pass
    return "0.0.0"


# ============================================================================
# 安装路径 / 桌面快捷方式
# ============================================================================

def default_install_dir():
    user_home = os.path.expanduser("~")
    local_appdata = os.environ.get("LOCALAPPDATA") or os.path.join(user_home, "AppData", "Local")
    return os.path.join(local_appdata, "Programs", "Y-code")


def get_desktop_dir():
    """获取用户真实桌面路径。

    桌面文件夹可能被用户或 OneDrive 重定向（例如 D:\\Users\\xxx\\Desktop），
    此时 ~/Desktop 并不是用户实际看到的桌面。优先读注册表
    User Shell Folders 的 Desktop 值，读取失败再回退 ~/Desktop。
    """
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Desktop")
            return os.path.normpath(os.path.expandvars(value))
    except OSError:
        return os.path.join(os.path.expanduser("~"), "Desktop")


def create_shortcut(shortcut_path, target_path, working_dir, startupinfo):
    """用 PowerShell 的 WScript.Shell COM 对象动态生成 .lnk 快捷方式。"""
    ps_cmd = (
        f"$WshShell = New-Object -ComObject WScript.Shell; "
        f"$Shortcut = $WshShell.CreateShortcut('{shortcut_path}'); "
        f"$Shortcut.TargetPath = '{target_path}'; "
        f"$Shortcut.WorkingDirectory = '{working_dir}'; "
        f"$Shortcut.Save();"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_cmd],
        startupinfo=startupinfo, check=True,
    )


# ============================================================================
# 注册表操作（全部用 winreg，不再用 reg.exe + 文本解析）
# ============================================================================

def broadcast_environment_change():
    """广播 WM_SETTINGCHANGE，使新写入的环境变量无需重启/重新登录即可对新开的进程生效。"""
    import ctypes
    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    result_val = ctypes.c_long()
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
        SMTO_ABORTIFHUNG, 1000, ctypes.byref(result_val),
    )


def add_to_user_path(install_dir, log=print):
    """把安装目录加入 HKCU\\Environment\\PATH，直接用 winreg 读写，保留原始值类型
    (REG_EXPAND_SZ)，不再依赖 `reg query` 的文本输出解析（旧实现在 PATH 值里
    出现多个空格、或碰巧包含子串 "PATH" 的路径分量时会解析错位）。"""
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            current_path, reg_type = winreg.QueryValueEx(key, "PATH")
        except FileNotFoundError:
            current_path, reg_type = "", winreg.REG_EXPAND_SZ

        parts = [p for p in current_path.split(";") if p]
        if any(p.lower() == install_dir.lower() for p in parts):
            log("ℹ Y-code 路径已存在于环境变量中，跳过配置。")
            return

        parts.append(install_dir)
        new_path = ";".join(parts)
        winreg.SetValueEx(key, "PATH", 0, reg_type, new_path)

    broadcast_environment_change()
    log("✔ 成功将 Y-code 路径加入环境变量 PATH！")


def write_uninstall_registry_key(app_key_name, display_name, version, install_dir,
                                   uninstall_string, display_icon):
    """在 HKCU\\...\\Uninstall 下写入一个标准的“应用和功能”卸载入口。"""
    key_path = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{app_key_name}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, version)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, install_dir)
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, uninstall_string)
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, display_icon)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "Y-code")
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


# ============================================================================
# 用户配置播种
# ============================================================================

def seed_user_config(bundle_dir, log=print):
    """从安装包内置的 config.example.json / system_prompt.md 初始化 ~/.ycode 用户配置
    (仅在用户目录下尚不存在对应文件时才写入，不会覆盖用户已有的配置)。"""
    user_home = os.path.expanduser("~")
    ycode_user_dir = os.path.join(user_home, ".ycode")
    os.makedirs(ycode_user_dir, exist_ok=True)

    config_path = os.path.join(ycode_user_dir, "config.json")
    if not os.path.exists(config_path):
        src_config = os.path.join(bundle_dir, "config.example.json")
        if os.path.exists(src_config):
            shutil.copy2(src_config, config_path)
            log(f"✔ 成功创建配置模板: {config_path}")
        else:
            with open(config_path, "w", encoding="utf-8") as f:
                f.write('{"active_model_id": null, "providers": []}')
            log(f"✔ 成功创建默认配置: {config_path}")

    prompt_path = os.path.join(ycode_user_dir, "system_prompt.md")
    if not os.path.exists(prompt_path):
        src_prompt = os.path.join(bundle_dir, "system_prompt.md")
        if os.path.exists(src_prompt):
            shutil.copy2(src_prompt, prompt_path)
            log(f"✔ 成功创建人设模板: {prompt_path}")


# ============================================================================
# 安装引擎（与界面解耦：log/progress 经回调上报，可在后台线程中运行）
# ============================================================================

class InstallError(Exception):
    """致命安装错误：向导会中断并展示该消息。"""


class InstallOptions:
    """安装选项页收集到的用户选择。"""

    def __init__(self, install_dir, install_cli=True, create_desktop_shortcut=True):
        self.install_dir = install_dir
        self.install_cli = install_cli
        self.create_desktop_shortcut = create_desktop_shortcut


def _open_payload(bundle_dir):
    """定位核心程序载荷：优先单文件压缩包 ycode_pkg.zip（onefile 启动快的关键，
    内嵌 4000+ 散文件会让启动解压耗时超过一分钟），
    回退到目录形式 ycode_pkg/（兼容旧的手动 spec 构建与本地调试）。"""
    zip_path = os.path.join(bundle_dir, "ycode_pkg.zip")
    if os.path.exists(zip_path):
        return ("zip", zip_path)
    pkg_dir = os.path.join(bundle_dir, "ycode_pkg")
    if os.path.isdir(pkg_dir):
        return ("dir", pkg_dir)
    raise InstallError("安装包中未找到核心程序包 (ycode_pkg)，安装终止。")


def _payload_names(kind, src, tui_only):
    """枚举待释放的载荷文件（相对路径列表）。

    tui_only=True 时只取 ycode.exe + _internal，且跳过 _internal/frontend
    （GUI 专属的前端静态资源）——独立安装命令行版体积更小。
    """
    if kind == "zip":
        import zipfile
        with zipfile.ZipFile(src) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
    else:
        names = []
        for root, dirs, files in os.walk(src):
            if tui_only and os.path.normpath(root) == os.path.normpath(os.path.join(src, "_internal")):
                dirs[:] = [d for d in dirs if d != "frontend"]
            for name in files:
                names.append(os.path.relpath(os.path.join(root, name), src))
    if tui_only:
        names = [
            n for n in names
            if n.replace("\\", "/") == "ycode.exe"
            or (n.replace("\\", "/").startswith("_internal/")
                and not n.replace("\\", "/").startswith("_internal/frontend/"))
        ]
    if not names:
        raise InstallError(f"安装包中缺少核心文件: {src}")
    return names


def _copy_payload(kind, src, install_dir, tui_only, log, progress):
    """带进度回调的载荷释放（占整体进度的 0~85%）。"""
    names = _payload_names(kind, src, tui_only)
    total = max(1, len(names))
    os.makedirs(install_dir, exist_ok=True)
    if kind == "zip":
        import zipfile
        with zipfile.ZipFile(src) as zf:
            for idx, rel in enumerate(names, 1):
                dest = os.path.join(install_dir, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(zf.read(rel))
                progress(idx / total * 85.0)
    else:
        for idx, rel in enumerate(names, 1):
            dest = os.path.join(install_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(os.path.join(src, rel), dest)
            progress(idx / total * 85.0)
    log(f"✔ 核心程序包及依赖成功释放到: {install_dir}")


def run_install(mode, bundle_dir, options, startupinfo, log=print, progress=lambda p: None):
    """执行完整安装流程。任何致命错误抛 InstallError；非致命问题只记日志。

    返回汇总 dict：{"install_dir", "installed_cli", "desktop_shortcut", "merging_into_full"}。
    """
    install_dir = options.install_dir
    payload_kind, payload_src = _open_payload(bundle_dir)

    tui_exe_path = os.path.join(install_dir, "ycode.exe")
    gui_exe_path = os.path.join(install_dir, "Y-code.exe")
    merging_into_full = False

    # 1. 复制核心程序包
    if mode == "tui":
        # 目标目录已是完整版安装时只需合并进去，不重复写卸载注册表项
        merging_into_full = os.path.exists(gui_exe_path)
        if merging_into_full:
            log("ℹ 检测到该目录已安装图形界面版 (Y-code.exe)，将把命令行版合并安装进同一目录。")
        log("🚀 正在安装命令行版核心依赖与组件...")
    else:
        log("🚀 正在安装程序核心依赖与组件...")
    try:
        _copy_payload(payload_kind, payload_src, install_dir,
                      tui_only=(mode == "tui"), log=log, progress=progress)
    except InstallError:
        raise
    except Exception as e:
        raise InstallError(
            f"复制核心程序失败: {e}\n请确保目标目录可写，且没有其他 Y-code 进程正在运行。"
        ) from e

    installed_cli = False
    desktop_shortcut = False

    if mode == "full":
        # 2. 命令行版选项
        installed_cli = bool(options.install_cli)
        if installed_cli:
            try:
                add_to_user_path(install_dir, log=log)
            except Exception as e:
                log(f"⚠️ 配置环境变量失败: {e}，请稍后手动将安装目录添加到系统环境变量中。")
            # 顺手清理旧版安装留下的 "Y-code CLI" 快捷方式
            try:
                legacy_lnk = os.path.join(get_desktop_dir(), "Y-code CLI.lnk")
                if os.path.exists(legacy_lnk):
                    os.remove(legacy_lnk)
                    log("ℹ 已移除旧版遗留的 [Y-code CLI] 桌面快捷方式。")
            except Exception as e:
                log(f"⚠️ 清理旧版 CLI 快捷方式失败: {e}")
        else:
            try:
                if os.path.exists(tui_exe_path):
                    os.remove(tui_exe_path)
                    log("ℹ 已跳过命令行版 (ycode.exe)，仅保留图形界面版。")
            except Exception as e:
                log(f"⚠️ 移除命令行版组件失败: {e}")
            try:
                bundled_tui_setup = os.path.join(bundle_dir, "tui", "ycode_tui_setup.exe")
                if os.path.exists(bundled_tui_setup):
                    tui_subdir = os.path.join(install_dir, "tui")
                    os.makedirs(tui_subdir, exist_ok=True)
                    dest = os.path.join(tui_subdir, "ycode_tui_setup.exe")
                    shutil.copy2(bundled_tui_setup, dest)
                    log(f"✔ 已附带命令行版补装程序: {dest}，以后可双击它补装命令行版")
                else:
                    log("⚠️ 安装包中未找到命令行版补装程序，跳过。")
            except Exception as e:
                log(f"⚠️ 复制命令行版补装程序失败: {e}")
        progress(90.0)

        # 3. 桌面快捷方式 "Y-code" → Y-code.exe（按用户勾选）
        if options.create_desktop_shortcut:
            try:
                shortcut_path = os.path.join(get_desktop_dir(), "Y-code.lnk")
                create_shortcut(shortcut_path, gui_exe_path, install_dir, startupinfo)
                desktop_shortcut = True
                log(f"✔ 桌面快捷方式创建成功: {shortcut_path}")
            except Exception as e:
                log(f"⚠️ 创建桌面快捷方式失败: {e}")
        else:
            log("ℹ 按您的选择，未创建桌面快捷方式。")
        progress(93.0)

        # 4. 复制卸载程序
        dest_uninst = os.path.join(install_dir, "ycode_uninstall.exe")
        try:
            src_uninst = os.path.join(bundle_dir, "ycode_uninstall.exe")
            if os.path.exists(src_uninst):
                shutil.copy2(src_uninst, dest_uninst)
                log(f"✔ 卸载程序成功复制到: {dest_uninst}")
            else:
                log("⚠️ 安装包中未找到卸载程序，跳过复制（“应用和功能”卸载入口将不可用）。")
        except Exception as e:
            log(f"⚠️ 复制卸载程序失败: {e}")
        progress(96.0)

        # 5. 初始化 ~/.ycode 用户配置
        try:
            seed_user_config(bundle_dir, log=log)
        except Exception as e:
            log(f"⚠️ 初始化用户配置失败: {e}")

        # 6. 写入卸载注册表项（“应用和功能”）
        try:
            write_uninstall_registry_key(
                app_key_name="Y-code",
                display_name="Y-code",
                version=read_bundled_version(bundle_dir),
                install_dir=install_dir,
                uninstall_string=f'"{dest_uninst}"',
                display_icon=f'"{gui_exe_path}"',
            )
            log("✔ 已在“应用和功能”中注册 Y-code。")
        except Exception as e:
            log(f"⚠️ 写入卸载注册表项失败: {e}")
        progress(100.0)

    else:  # tui 模式
        try:
            add_to_user_path(install_dir, log=log)
        except Exception as e:
            log(f"⚠️ 配置环境变量失败: {e}，请稍后手动将安装目录添加到系统环境变量中。")
        try:
            legacy_lnk = os.path.join(get_desktop_dir(), "Y-code CLI.lnk")
            if os.path.exists(legacy_lnk):
                os.remove(legacy_lnk)
                log("ℹ 已移除旧版遗留的 [Y-code CLI] 桌面快捷方式。")
        except Exception as e:
            log(f"⚠️ 清理旧版 CLI 快捷方式失败: {e}")
        progress(92.0)

        try:
            seed_user_config(bundle_dir, log=log)
        except Exception as e:
            log(f"⚠️ 初始化用户配置失败: {e}")

        installed_cli = True
        if merging_into_full:
            log("ℹ 已合并进现有的完整版安装，沿用原有卸载入口，不再重复注册。")
        else:
            try:
                dest_uninst = os.path.join(install_dir, "ycode_uninstall.exe")
                src_uninst = os.path.join(bundle_dir, "ycode_uninstall.exe")
                if os.path.exists(src_uninst):
                    shutil.copy2(src_uninst, dest_uninst)
                    log(f"✔ 卸载程序成功复制到: {dest_uninst}")
                    write_uninstall_registry_key(
                        app_key_name="Y-code-CLI",
                        display_name="Y-code CLI",
                        version=read_bundled_version(bundle_dir),
                        install_dir=install_dir,
                        uninstall_string=f'"{dest_uninst}"',
                        display_icon=f'"{tui_exe_path}"',
                    )
                    log("✔ 已在“应用和功能”中注册 Y-code CLI。")
                else:
                    log("⚠️ 安装包中未找到卸载程序，跳过卸载注册。")
            except Exception as e:
                log(f"⚠️ 注册卸载信息失败: {e}")
        progress(100.0)

    return {
        "install_dir": install_dir,
        "installed_cli": installed_cli,
        "desktop_shortcut": desktop_shortcut,
        "merging_into_full": merging_into_full,
    }


# ============================================================================
# 可视化安装向导（tkinter）
# ============================================================================

import tkinter as tk
from tkinter import filedialog, ttk

WINDOW_W, WINDOW_H = 760, 520
COLOR_BG_PANEL = "#0e1c26"      # 内容面板底色（压暗保证文字可读）
COLOR_BORDER = "#2b4a5a"
COLOR_TEXT = "#e8f2f5"
COLOR_TEXT_DIM = "#9fb8c2"
COLOR_ACCENT = "#2ec5e0"        # 星瑶青
COLOR_BTN = "#1b6d80"
COLOR_BTN_HOVER = "#23899f"
COLOR_PANEL_ENTRY = "#132833"

FONT_TITLE = ("Microsoft YaHei UI", 22, "bold")
FONT_SUB = ("Microsoft YaHei UI", 11)
FONT_TEXT = ("Microsoft YaHei UI", 10)
FONT_BTN = ("Microsoft YaHei UI", 10, "bold")


class SetupWizard(tk.Tk):
    """Y-code 安装向导主窗口：欢迎页 → 选项页 → 安装页 → 完成页。"""

    def __init__(self, mode, bundle_dir, startupinfo):
        super().__init__()
        self.mode = mode
        self.bundle_dir = bundle_dir
        self.startupinfo = startupinfo
        self.version = read_bundled_version(bundle_dir)
        self._ui_queue = queue.Queue()
        self._install_result = None

        self.title("Y-code 安装向导")
        self.resizable(False, False)
        self._center_window()
        self._set_icon()
        self._paint_background()
        # Windows 上最小化再恢复后，Tk 会把窗口收缩成只剩标题栏（实测可复现，
        # <Map> 事件无法捕获该时序），用轻量看门狗定期把固定几何声明回去。
        self.after(500, self._geometry_watchdog)

        # 选项页状态
        self.var_dir = tk.StringVar(value=default_install_dir())
        self.var_cli = tk.BooleanVar(value=True)
        self.var_shortcut = tk.BooleanVar(value=True)

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

    def _geometry_watchdog(self):
        """看门狗：窗口处于正常状态但尺寸被压缩时，恢复为固定尺寸。"""
        try:
            if self.wm_state() == "normal" and self.winfo_viewable():
                if self.winfo_width() < WINDOW_W or self.winfo_height() < WINDOW_H:
                    self.geometry(f"{WINDOW_W}x{WINDOW_H}")
        except tk.TclError:
            pass
        self.after(500, self._geometry_watchdog)

    def _set_icon(self):
        ico = os.path.join(self.bundle_dir, "assets", "ycode.ico")
        if not os.path.exists(ico):
            return
        try:
            self.iconbitmap(ico)
        except Exception:
            pass
        # tk 的 iconbitmap 不一定覆盖任务栏用的 ICON_BIG，且 Tk 窗口类的类图标
        # 始终是默认羽毛（任务栏/Alt-Tab 可能回退到它），这里用 Win32 显式把
        # 窗口大/小图标与类图标全部换成我们的图标。
        try:
            import ctypes
            u32 = ctypes.windll.user32
            hwnd = int(self.wm_frame(), 16)  # 顶层窗口 HWND
            LR_LOADFROMFILE = 0x10
            IMAGE_ICON = 1
            WM_SETICON = 0x0080
            h_big = u32.LoadImageW(None, ico, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
            h_small = u32.LoadImageW(None, ico, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            if h_big:
                u32.SendMessageW(hwnd, WM_SETICON, 1, h_big)      # ICON_BIG
                u32.SetClassLongPtrW(hwnd, -14, h_big)            # GCLP_HICON
            if h_small:
                u32.SendMessageW(hwnd, WM_SETICON, 0, h_small)    # ICON_SMALL
                u32.SetClassLongPtrW(hwnd, -34, h_small)          # GCLP_HICONSM
        except Exception:
            pass

    def _paint_background(self):
        bg_path = os.path.join(self.bundle_dir, "assets", "setup_bg.png")
        if os.path.exists(bg_path):
            try:
                self._bg_image = tk.PhotoImage(file=bg_path)
                tk.Label(self, image=self._bg_image, bd=0).place(x=0, y=0)
                return
            except Exception:
                pass
        self.configure(bg="#0a141c")

    def _panel(self):
        """居中内容面板：所有页面控件都放在里面，与背景图拉开层次。"""
        panel = tk.Frame(
            self, bg=COLOR_BG_PANEL,
            highlightbackground=COLOR_BORDER, highlightthickness=1,
        )
        panel.place(relx=0.5, rely=0.5, anchor="center", width=600, height=420)
        return panel

    def _label(self, parent, text, font=FONT_TEXT, fg=COLOR_TEXT, **kw):
        return tk.Label(parent, text=text, font=font, fg=fg, bg=COLOR_BG_PANEL, **kw)

    def _button(self, parent, text, command, primary=False):
        btn = tk.Button(
            parent, text=text, command=command,
            font=FONT_BTN, fg="#ffffff",
            bg=COLOR_BTN if primary else "#26404e",
            activebackground=COLOR_BTN_HOVER, activeforeground="#ffffff",
            bd=0, padx=22, pady=7, cursor="hand2",
        )
        return btn

    # ------------------------------------------------------------------
    # 页面构建
    # ------------------------------------------------------------------
    def _build_pages(self):
        self._pages["welcome"] = self._build_welcome_page()
        self._pages["options"] = self._build_options_page()
        self._pages["installing"] = self._build_installing_page()
        self._pages["finish"] = self._build_finish_page()

    def show_page(self, name):
        for page in self._pages.values():
            page.place_forget()
        self._pages[name].place(relx=0.5, rely=0.5, anchor="center", width=600, height=420)

    def _build_welcome_page(self):
        page = self._panel()
        is_full = self.mode == "full"
        self._label(page, "Y-code 星瑶", FONT_TITLE, COLOR_ACCENT).place(relx=0.5, y=52, anchor="center")
        self._label(page, f"AI 编程助手 · 官方安装向导   v{self.version}", FONT_SUB, COLOR_TEXT_DIM).place(
            relx=0.5, y=88, anchor="center")
        desc = (
            "本向导将在您的电脑上安装 Y-code 图形界面版 (GUI)，\n"
            "您可以在下一步选择是否附带安装命令行版 (CLI)。"
            if is_full else
            "本向导将在您的电脑上安装 Y-code 命令行版 (CLI)，\n"
            "安装完成后可在终端中直接使用 ycode 命令。"
        )
        self._label(page, desc, FONT_TEXT, COLOR_TEXT, justify="center").place(relx=0.5, y=170, anchor="center")
        self._label(page, "单击“下一步”继续，或单击“取消”退出安装向导。", FONT_TEXT, COLOR_TEXT_DIM).place(
            relx=0.5, y=225, anchor="center")
        self._button(page, "取  消", self.destroy).place(relx=0.5, y=330, anchor="e", x=-14)
        self._button(page, "下一步 >", lambda: self.show_page("options"), primary=True).place(
            relx=0.5, y=330, anchor="w", x=14)
        return page

    def _build_options_page(self):
        page = self._panel()
        self._label(page, "安装选项", ("Microsoft YaHei UI", 15, "bold")).place(x=40, y=34)

        self._label(page, "安装位置:", FONT_TEXT).place(x=40, y=88)
        entry = tk.Entry(
            page, textvariable=self.var_dir, font=FONT_TEXT,
            bg=COLOR_PANEL_ENTRY, fg=COLOR_TEXT, insertbackground=COLOR_TEXT,
            bd=0, highlightthickness=1, highlightbackground=COLOR_BORDER,
        )
        entry.place(x=40, y=112, width=420, height=30)
        self._button(page, "浏览...", self._browse_dir).place(x=470, y=112, width=90, height=30)

        if self.mode == "full":
            self._checkbox(page, "同时安装命令行版 CLI（终端中使用 ycode 命令）",
                           self.var_cli).place(x=40, y=180)
            self._checkbox(page, "创建桌面快捷方式", self.var_shortcut).place(x=40, y=216)
        else:
            self._label(
                page,
                "将把命令行版 (ycode.exe) 安装到所选目录并加入 PATH。\n"
                "若所选目录已安装图形界面版，则会自动合并安装。",
                FONT_TEXT, COLOR_TEXT_DIM, justify="left",
            ).place(x=40, y=180)

        self._button(page, "< 上一步", lambda: self.show_page("welcome")).place(x=40, y=350)
        self._button(page, "取  消", self.destroy).place(x=400, y=350, width=80)
        self._button(page, "安  装", self._start_install, primary=True).place(x=500, y=350, width=90)
        return page

    def _checkbox(self, parent, text, var):
        return tk.Checkbutton(
            parent, text=text, variable=var,
            font=FONT_TEXT, fg=COLOR_TEXT, bg=COLOR_BG_PANEL,
            activebackground=COLOR_BG_PANEL, activeforeground=COLOR_TEXT,
            selectcolor=COLOR_PANEL_ENTRY, cursor="hand2",
        )

    def _build_installing_page(self):
        page = self._panel()
        self._label(page, "正在安装", ("Microsoft YaHei UI", 15, "bold")).place(x=40, y=30)
        # 进度条主题化为星瑶青（clam 主题才支持自定义颜色）
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "cyan.Horizontal.TProgressbar",
            background=COLOR_ACCENT, troughcolor=COLOR_PANEL_ENTRY,
            bordercolor=COLOR_BORDER, lightcolor=COLOR_ACCENT, darkcolor=COLOR_ACCENT,
        )
        self._progress = ttk.Progressbar(
            page, mode="determinate", maximum=100.0, style="cyan.Horizontal.TProgressbar"
        )
        self._progress.place(x=40, y=72, width=520, height=16)
        self._pct_label = self._label(page, "0%", FONT_TEXT, COLOR_TEXT_DIM)
        self._pct_label.place(x=470, y=94)
        self._log_text = tk.Text(
            page, font=("Consolas", 9), bg=COLOR_PANEL_ENTRY, fg=COLOR_TEXT_DIM,
            bd=0, highlightthickness=1, highlightbackground=COLOR_BORDER,
            state="disabled", wrap="word",
        )
        self._log_text.place(x=40, y=122, width=520, height=250)
        return page

    def _build_finish_page(self):
        page = self._panel()
        self._finish_icon = self._label(page, "🎉", ("Microsoft YaHei UI", 26))
        self._finish_icon.place(relx=0.5, y=52, anchor="center")
        self._finish_title = self._label(page, "安装成功", ("Microsoft YaHei UI", 16, "bold"), COLOR_ACCENT)
        self._finish_title.place(relx=0.5, y=100, anchor="center")
        self._finish_detail = self._label(page, "", FONT_TEXT, COLOR_TEXT, justify="center")
        self._finish_detail.place(relx=0.5, y=185, anchor="center")
        self._finish_hint = self._label(page, "", FONT_TEXT, COLOR_TEXT_DIM, justify="center")
        self._finish_hint.place(relx=0.5, y=265, anchor="center")
        self._button(page, "完  成", self.destroy, primary=True).place(relx=0.5, y=350, anchor="center")
        return page

    # ------------------------------------------------------------------
    # 交互逻辑
    # ------------------------------------------------------------------
    def _browse_dir(self):
        chosen = filedialog.askdirectory(parent=self, title="选择安装位置",
                                         initialdir=self.var_dir.get() or None)
        if chosen:
            self.var_dir.set(os.path.normpath(chosen))

    def _start_install(self):
        install_dir = self.var_dir.get().strip()
        if not install_dir:
            self._append_log("⚠️ 请先选择有效的安装位置。")
            return
        options = InstallOptions(
            install_dir=install_dir,
            install_cli=self.var_cli.get(),
            create_desktop_shortcut=self.var_shortcut.get(),
        )
        self.show_page("installing")
        threading.Thread(
            target=self._install_worker, args=(options,), daemon=True,
            name="setup-install-worker",
        ).start()

    def _install_worker(self, options):
        """后台线程：执行安装引擎，所有 UI 刷新经队列回主线程（Tk 非线程安全）。"""
        try:
            result = run_install(
                self.mode, self.bundle_dir, options, self.startupinfo,
                log=lambda msg: self._ui_queue.put(("log", msg)),
                progress=lambda p: self._ui_queue.put(("progress", p)),
            )
            self._ui_queue.put(("done", result))
        except InstallError as e:
            self._ui_queue.put(("error", str(e)))
        except Exception as e:  # 兜底：引擎外的意外异常也不能让向导卡死
            self._ui_queue.put(("error", f"安装过程出现意外错误: {e}"))

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
        install_dir = result["install_dir"]
        ycode_user_dir = os.path.join(os.path.expanduser("~"), ".ycode")
        if self.mode == "full":
            hints = ["双击桌面上的 [Y-code] 快捷方式，或到安装目录启动 Y-code.exe。"]
            if result["installed_cli"]:
                hints.append("打开新的命令行窗口 (CMD/PowerShell)，输入 ycode 使用命令行版。")
            else:
                hints.append("本次未安装命令行版；如需要，可随时双击安装目录下 tui\\ycode_tui_setup.exe 补装。")
        else:
            hints = ["打开任意全新的命令行窗口 (CMD/PowerShell)，输入 ycode 敲回车即可开始使用。"]
        self._finish_detail.configure(
            text=f"安装路径: {install_dir}\n配置文件夹: {ycode_user_dir}"
        )
        self._finish_hint.configure(text="\n".join(hints))
        self.show_page("finish")

    def _show_error(self, message):
        self._finish_icon.configure(text="🚨")
        self._finish_title.configure(text="安装失败", fg="#e06c75")
        self._finish_detail.configure(text=message)
        self._finish_hint.configure(text="您可以关闭向导后重试，或更换安装位置。")
        self.show_page("finish")


# ============================================================================
# 入口
# ============================================================================

def main():
    # windowed 打包（--noconsole）时 sys.stdout 为 None，任何 print 都会炸，
    # 统一重定向到 devnull，保留 print 作为日志手段而不影响 GUI。
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="ignore")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="ignore")

    # 在最外层统一定义一次 startupinfo，供本次运行内所有 subprocess 调用共用，
    # 避免它像旧版本那样定义在某一步的 try 块里——一旦那一步提前失败/跳过，
    # 后面的步骤引用同名变量就会 NameError。
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    bundle_dir = get_bundle_dir()
    mode = read_setup_mode(bundle_dir)

    wizard = SetupWizard(mode=mode, bundle_dir=bundle_dir, startupinfo=startupinfo)
    wizard.mainloop()


if __name__ == "__main__":
    main()
