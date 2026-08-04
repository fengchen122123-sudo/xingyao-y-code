"""
ycode_setup.py
Y-code 官方 Windows 安装向导（单一源码，靠内嵌的 setup_mode.txt 区分两种模式）：

  full 模式（默认）：完整版安装包。默认安装图形界面版 (Y-code.exe)，
                     并询问是否附带安装命令行版 (ycode.exe / ycode 命令)。
                     若用户当时选择不装命令行版，会把内嵌的 TUI 独立安装向导
                     释放到安装目录的 tui\\ 子目录下，方便以后随时补装。

  tui 模式：TUI 独立安装包。只安装命令行版，用于用户后续补装，或单独分发
            给只想用命令行的用户。

模式来源：build_release.py 用 --onefile 分别打包本脚本两次，每次都通过
--add-data 内嵌一个不同内容的 setup_mode.txt（'full' 或 'tui'）。如果直接用
项目根目录下的 ycode_setup.spec 手动构建（不经过 build_release.py），因为
没有内嵌 setup_mode.txt，会自动按 'full' 模式运行。
"""
import os
import sys
import shutil
import subprocess
import winreg


# ============================================================================
# 展示 / 文案
# ============================================================================

def print_logo():
    print(r"""
============================================================
 __  __      __________  ____  ______
\ \/ /     / ____/ __ \/ __ \/ ____/
 \  /_____/ /   / / / / / / / __/
 / /_____/ /___/ /_/ / /_/ / /___
/_/      \____/\____/_____/_____/

                 Y-code 官方 Windows 安装向导
============================================================
    """)


def print_finish_banner_full(install_dir, installed_tui):
    ycode_user_dir = os.path.join(os.path.expanduser("~"), ".ycode")
    print("\n============================================================")
    print("🎉 Y-code 安装成功！")
    print(f"安装路径: {install_dir}")
    print(f"配置文件夹: {ycode_user_dir}")
    print("------------------------------------------------------------")
    print("🚀 启动方法：")
    print("1. 双击桌面上的 [Y-code] 快捷方式，打开图形界面版。")
    if installed_tui:
        print("2. 打开一个新的命令行窗口 (CMD/PowerShell)，输入 ycode 敲回车，使用命令行版。")
    else:
        print("2. 本次未安装命令行版；如需要，可随时双击安装目录下的 tui\\ycode_tui_setup.exe 补装。")
    print("============================================================")
    input("\n按任意键退出安装向导...")


def print_finish_banner_tui(install_dir):
    print("\n============================================================")
    print("🎉 Y-code 命令行版 (CLI) 安装成功！")
    print(f"安装路径: {install_dir}")
    print("------------------------------------------------------------")
    print("🚀 启动方法：")
    print("1. 打开任意全新的命令行窗口 (CMD/PowerShell)，输入 ycode 敲回车。")
    print("============================================================")
    input("\n按任意键退出安装向导...")


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

def prompt_install_dir():
    user_home = os.path.expanduser("~")
    local_appdata = os.environ.get("LOCALAPPDATA") or os.path.join(user_home, "AppData", "Local")
    default_install_dir = os.path.join(local_appdata, "Programs", "Y-code")

    print(f"📁 默认安装路径: {default_install_dir}")
    choice = input("是否同意安装到此路径？ (Y/n): ").strip().lower()
    if choice and choice != "y":
        custom_dir = input("请输入您自定义的安装绝对路径: ").strip()
        install_dir = custom_dir if custom_dir else default_install_dir
    else:
        install_dir = default_install_dir

    os.makedirs(install_dir, exist_ok=True)
    return install_dir


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


def add_to_user_path(install_dir):
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
            print("ℹ Y-code 路径已存在于环境变量中，跳过配置。")
            return

        parts.append(install_dir)
        new_path = ";".join(parts)
        winreg.SetValueEx(key, "PATH", 0, reg_type, new_path)

    broadcast_environment_change()
    print("✔ 成功将 Y-code 路径加入环境变量 PATH！")


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

def seed_user_config(bundle_dir):
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
            print(f"✔ 成功创建配置模板: {config_path}")
        else:
            with open(config_path, "w", encoding="utf-8") as f:
                f.write('{"active_model_id": null, "providers": []}')
            print(f"✔ 成功创建默认配置: {config_path}")

    prompt_path = os.path.join(ycode_user_dir, "system_prompt.md")
    if not os.path.exists(prompt_path):
        src_prompt = os.path.join(bundle_dir, "system_prompt.md")
        if os.path.exists(src_prompt):
            shutil.copy2(src_prompt, prompt_path)
            print(f"✔ 成功创建人设模板: {prompt_path}")


# ============================================================================
# TUI-only 载荷选择性复制（跳过 GUI 专属文件）
# ============================================================================

def copy_tui_payload(src_pkg_dir, install_dir):
    """TUI 独立安装模式：只复制 ycode.exe + _internal，且跳过 _internal/frontend
    （GUI 专属的前端静态资源）——这样独立安装命令行版时体积更小，也不会让人
    误以为装了图形界面。"""
    os.makedirs(install_dir, exist_ok=True)

    src_exe = os.path.join(src_pkg_dir, "ycode.exe")
    if not os.path.exists(src_exe):
        raise FileNotFoundError(f"安装包中缺少核心文件: {src_exe}")
    shutil.copy2(src_exe, os.path.join(install_dir, "ycode.exe"))

    src_internal = os.path.join(src_pkg_dir, "_internal")
    dest_internal = os.path.join(install_dir, "_internal")
    if os.path.isdir(src_internal):
        def _skip_gui_only_assets(dir_path, names):
            if os.path.normpath(dir_path) == os.path.normpath(src_internal):
                return [n for n in names if n == "frontend"]
            return []

        shutil.copytree(
            src_internal, dest_internal, dirs_exist_ok=True, ignore=_skip_gui_only_assets,
        )


# ============================================================================
# full 模式
# ============================================================================

def install_full(bundle_dir, startupinfo):
    print("👋 欢迎使用 Y-code 官方 Windows 安装向导！")
    print("本向导将为您安装 Y-code (星瑶 AI 编程助手)，默认安装图形界面版 (GUI)，")
    print("并可选附带安装命令行版 (CLI)。\n")

    install_dir = prompt_install_dir()

    # 1. 复制核心程序包（完整 onedir：Y-code.exe + ycode.exe + _internal）
    print("\n🚀 正在安装程序核心依赖与组件...")
    src_pkg_dir = os.path.join(bundle_dir, "ycode_pkg")
    if not os.path.isdir(src_pkg_dir):
        print("🚨 安装包中未找到核心程序目录 (ycode_pkg)，安装终止。")
        input("按任意键退出安装...")
        sys.exit(1)
    try:
        shutil.copytree(src_pkg_dir, install_dir, dirs_exist_ok=True)
        print(f"✔ 核心程序包及依赖成功释放到: {install_dir}")
    except Exception as e:
        print(f"🚨 复制核心程序失败: {e}")
        print("请确保目标目录可写，且没有其他 Y-code 进程正在运行。")
        input("按任意键退出安装...")
        sys.exit(1)

    tui_exe_path = os.path.join(install_dir, "ycode.exe")
    gui_exe_path = os.path.join(install_dir, "Y-code.exe")

    # 2. 询问是否附带安装命令行版
    print()
    choice = input("是否同时安装命令行版 CLI (ycode 命令)？[Y/n]: ").strip().lower()
    installed_tui = choice != "n"

    if installed_tui:
        try:
            add_to_user_path(install_dir)
        except Exception as e:
            print(f"⚠️ 配置环境变量失败: {e}")
            print("请稍后手动将安装目录添加到您的系统环境变量中。")
        # 不再创建 CLI 桌面快捷方式（用户直接在终端输入 ycode 即可）；
        # 顺手清理旧版安装留下的 "Y-code CLI" 快捷方式。
        try:
            legacy_lnk = os.path.join(get_desktop_dir(), "Y-code CLI.lnk")
            if os.path.exists(legacy_lnk):
                os.remove(legacy_lnk)
                print("ℹ 已移除旧版遗留的 [Y-code CLI] 桌面快捷方式。")
        except Exception as e:
            print(f"⚠️ 清理旧版 CLI 快捷方式失败: {e}")
    else:
        try:
            if os.path.exists(tui_exe_path):
                os.remove(tui_exe_path)
                print("ℹ 已跳过命令行版 (ycode.exe)，仅保留图形界面版。")
        except Exception as e:
            print(f"⚠️ 移除命令行版组件失败: {e}")
        try:
            bundled_tui_setup = os.path.join(bundle_dir, "tui", "ycode_tui_setup.exe")
            if os.path.exists(bundled_tui_setup):
                tui_subdir = os.path.join(install_dir, "tui")
                os.makedirs(tui_subdir, exist_ok=True)
                dest = os.path.join(tui_subdir, "ycode_tui_setup.exe")
                shutil.copy2(bundled_tui_setup, dest)
                print(f"✔ 已附带命令行版补装程序: {dest}")
                print("💡 以后可双击 tui\\ycode_tui_setup.exe 补装命令行版")
            else:
                print("⚠️ 安装包中未找到命令行版补装程序，跳过。")
        except Exception as e:
            print(f"⚠️ 复制命令行版补装程序失败: {e}")

    # 3. 桌面快捷方式 "Y-code" → Y-code.exe（完整版始终创建）
    try:
        shortcut_path = os.path.join(get_desktop_dir(), "Y-code.lnk")
        create_shortcut(shortcut_path, gui_exe_path, install_dir, startupinfo)
        print(f"✔ 桌面快捷方式创建成功: {shortcut_path}")
    except Exception as e:
        print(f"⚠️ 创建桌面快捷方式失败: {e}")

    # 4. 复制卸载程序
    dest_uninst = os.path.join(install_dir, "ycode_uninstall.exe")
    try:
        src_uninst = os.path.join(bundle_dir, "ycode_uninstall.exe")
        if os.path.exists(src_uninst):
            shutil.copy2(src_uninst, dest_uninst)
            print(f"✔ 卸载程序成功复制到: {dest_uninst}")
        else:
            print("⚠️ 安装包中未找到卸载程序，跳过复制（“应用和功能”卸载入口将不可用）。")
    except Exception as e:
        print(f"⚠️ 复制卸载程序失败: {e}")

    # 5. 初始化 ~/.ycode 用户配置
    try:
        seed_user_config(bundle_dir)
    except Exception as e:
        print(f"⚠️ 初始化用户配置失败: {e}")

    # 6. 写入卸载注册表项（“应用和功能”）
    try:
        version = read_bundled_version(bundle_dir)
        write_uninstall_registry_key(
            app_key_name="Y-code",
            display_name="Y-code",
            version=version,
            install_dir=install_dir,
            uninstall_string=f'"{dest_uninst}"',
            display_icon=f'"{gui_exe_path}"',
        )
        print("✔ 已在“应用和功能”中注册 Y-code。")
    except Exception as e:
        print(f"⚠️ 写入卸载注册表项失败: {e}")

    print_finish_banner_full(install_dir, installed_tui)


# ============================================================================
# tui 模式
# ============================================================================

def install_tui(bundle_dir, startupinfo):
    print("👋 欢迎使用 Y-code 命令行版 (CLI) 安装向导！")
    print("本向导将为您安装 Y-code 命令行版 (ycode 命令) 到您的电脑上。\n")

    install_dir = prompt_install_dir()

    # 如果目标目录已经是一个完整版安装（存在 Y-code.exe），只需合并进去，
    # 不重复写卸载注册表项——完整版自己的卸载入口已经能清理整个目录。
    merging_into_full = os.path.exists(os.path.join(install_dir, "Y-code.exe"))
    if merging_into_full:
        print("ℹ 检测到该目录已安装图形界面版 (Y-code.exe)，将把命令行版合并安装进同一目录。")

    print("\n🚀 正在安装命令行版核心依赖与组件...")
    src_pkg_dir = os.path.join(bundle_dir, "ycode_pkg")
    if not os.path.isdir(src_pkg_dir):
        print("🚨 安装包中未找到核心程序目录 (ycode_pkg)，安装终止。")
        input("按任意键退出安装...")
        sys.exit(1)
    try:
        copy_tui_payload(src_pkg_dir, install_dir)
        print(f"✔ 命令行版核心程序及依赖成功释放到: {install_dir}")
    except Exception as e:
        print(f"🚨 复制核心程序失败: {e}")
        print("请确保目标目录可写，且没有其他 Y-code 进程正在运行。")
        input("按任意键退出安装...")
        sys.exit(1)

    tui_exe_path = os.path.join(install_dir, "ycode.exe")

    # PATH
    try:
        add_to_user_path(install_dir)
    except Exception as e:
        print(f"⚠️ 配置环境变量失败: {e}")
        print("请稍后手动将安装目录添加到您的系统环境变量中。")

    # 不再创建 CLI 桌面快捷方式（用户直接在终端输入 ycode 即可）；清理旧版遗留
    try:
        legacy_lnk = os.path.join(get_desktop_dir(), "Y-code CLI.lnk")
        if os.path.exists(legacy_lnk):
            os.remove(legacy_lnk)
            print("ℹ 已移除旧版遗留的 [Y-code CLI] 桌面快捷方式。")
    except Exception as e:
        print(f"⚠️ 清理旧版 CLI 快捷方式失败: {e}")

    # 用户配置播种
    try:
        seed_user_config(bundle_dir)
    except Exception as e:
        print(f"⚠️ 初始化用户配置失败: {e}")

    # 卸载程序 + 注册表（仅在不是合并安装时才需要）
    if merging_into_full:
        print("ℹ 已合并进现有的完整版安装，沿用原有卸载入口，不再重复注册。")
    else:
        try:
            dest_uninst = os.path.join(install_dir, "ycode_uninstall.exe")
            src_uninst = os.path.join(bundle_dir, "ycode_uninstall.exe")
            if os.path.exists(src_uninst):
                shutil.copy2(src_uninst, dest_uninst)
                print(f"✔ 卸载程序成功复制到: {dest_uninst}")
                version = read_bundled_version(bundle_dir)
                write_uninstall_registry_key(
                    app_key_name="Y-code-CLI",
                    display_name="Y-code CLI",
                    version=version,
                    install_dir=install_dir,
                    uninstall_string=f'"{dest_uninst}"',
                    display_icon=f'"{tui_exe_path}"',
                )
                print("✔ 已在“应用和功能”中注册 Y-code CLI。")
            else:
                print("⚠️ 安装包中未找到卸载程序，跳过卸载注册。")
        except Exception as e:
            print(f"⚠️ 注册卸载信息失败: {e}")

    print_finish_banner_tui(install_dir)


# ============================================================================
# 入口
# ============================================================================

def main():
    # 在最外层统一定义一次 startupinfo，供本次运行内所有 subprocess 调用共用，
    # 避免它像旧版本那样定义在某一步的 try 块里——一旦那一步提前失败/跳过，
    # 后面的步骤引用同名变量就会 NameError。
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    print_logo()

    bundle_dir = get_bundle_dir()
    mode = read_setup_mode(bundle_dir)

    if mode == "full":
        install_full(bundle_dir, startupinfo)
    else:
        install_tui(bundle_dir, startupinfo)


if __name__ == "__main__":
    main()
