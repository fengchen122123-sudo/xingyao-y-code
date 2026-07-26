"""
ycode_uninstall.py
Y-code 官方 Windows 卸载向导。

会清理完整版 (GUI + 可选 TUI) 或独立 TUI 安装留下的全部痕迹：
  - 桌面快捷方式 (Y-code.lnk / Y-code TUI.lnk，两个都尝试删除，不存在的自动跳过)
  - HKCU\\Environment\\PATH 里的安装目录
  - HKCU\\...\\Uninstall 下的注册表项 (Y-code / Y-code-TUI，两个都尝试移除)
  - 可选：~/.ycode 用户配置与会话记录
  - 安装目录本身 (Y-code.exe / ycode.exe / _internal/ / tui/ 子目录)
"""
import os
import sys
import shutil
import subprocess
import time
import ctypes
import winreg


def print_logo():
    print("""
============================================================
              __   __                     _
              \\ \\ / /                    | |
               \\ V / ___  ___  ___   __| | ___
                \\ / / __|/ _ \\/ _ \\ / _` |/ _ \\
                | | \\__ \\  __/ (_) | (_| |  __/
                \\_/ |___/\\___|\\___/ \\__,_|\\___|

                 Y-code 卸载向导
============================================================
    """)


# ============================================================================
# 注册表操作（全部用 winreg）
# ============================================================================

def broadcast_environment_change():
    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    result_val = ctypes.c_long()
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
        SMTO_ABORTIFHUNG, 1000, ctypes.byref(result_val),
    )


def remove_from_user_path(install_dir):
    """从 HKCU\\Environment\\PATH 中移除安装目录，直接用 winreg 读写并保留原始值类型。"""
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            current_path, reg_type = winreg.QueryValueEx(key, "PATH")
        except FileNotFoundError:
            print("ℹ 环境变量 PATH 中未找到 Y-code 路径。")
            return

        parts = [p for p in current_path.split(";") if p]
        new_parts = [p for p in parts if p.lower() != install_dir.lower()]
        if len(new_parts) == len(parts):
            print("ℹ 环境变量 PATH 中未找到 Y-code 路径。")
            return

        new_path = ";".join(new_parts)
        winreg.SetValueEx(key, "PATH", 0, reg_type, new_path)

    broadcast_environment_change()
    print("✔ 成功从环境变量 PATH 中移除 Y-code 路径！")


def remove_uninstall_registry_key(app_key_name):
    key_path = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{app_key_name}"
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        print(f"✔ 已从“应用和功能”移除 {app_key_name} 的注册信息。")
    except FileNotFoundError:
        pass  # 本来就没有注册这一项，静默跳过
    except OSError as e:
        print(f"⚠️ 移除卸载注册表项 {app_key_name} 失败: {e}")


# ============================================================================
# 快捷方式
# ============================================================================

def remove_shortcut(name):
    shortcut_path = os.path.join(os.path.expanduser("~"), "Desktop", name)
    if os.path.exists(shortcut_path):
        try:
            os.remove(shortcut_path)
            print(f"✔ 已成功删除桌面快捷方式: {name}")
        except Exception as e:
            print(f"⚠️ 删除快捷方式 {name} 失败: {e}")


# ============================================================================
# 主流程
# ============================================================================

def uninstall():
    print_logo()
    print("👋 欢迎使用 Y-code 官方 Windows 卸载向导！")
    print("本向导将从您的电脑上安全卸载 Y-code（含图形界面版与命令行版）。\n")

    # 确定安装路径（当前可执行文件所在的目录）
    if getattr(sys, "frozen", False):
        install_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        install_dir = os.path.dirname(os.path.abspath(__file__))

    print(f"📁 确定的安装路径: {install_dir}")
    confirm = input("确定要卸载 Y-code 吗？ (y/N): ").strip().lower()
    if confirm != "y":
        print("卸载已取消。")
        time.sleep(2)
        sys.exit(0)

    delete_config = input("是否同时删除您的历史会话与模型配置？ (.ycode 文件夹) (y/N): ").strip().lower()

    print("\n🚀 正在执行卸载步骤...")

    # 1. 删除桌面快捷方式（GUI 版 + TUI 版都尝试删除，不存在的自动跳过）
    try:
        remove_shortcut("Y-code.lnk")
        remove_shortcut("Y-code TUI.lnk")
    except Exception as e:
        print(f"⚠️ 删除快捷方式时发生警告: {e}")

    # 2. 从环境变量 PATH 中移除安装路径
    print("🌐 正在从环境变量 (PATH) 中移除路径...")
    try:
        remove_from_user_path(install_dir)
    except Exception as e:
        print(f"⚠️ 移除环境变量时发生警告: {e}")

    # 3. 移除“应用和功能”里的卸载注册项（完整版入口 + 可能存在的独立 TUI 入口）
    print("🗂️ 正在清理注册表卸载信息...")
    try:
        remove_uninstall_registry_key("Y-code")
        remove_uninstall_registry_key("Y-code-TUI")
    except Exception as e:
        print(f"⚠️ 清理注册表时发生警告: {e}")

    # 4. 如果用户同意，删除 ~/.ycode 文件夹
    if delete_config == "y":
        ycode_user_dir = os.path.join(os.path.expanduser("~"), ".ycode")
        if os.path.exists(ycode_user_dir):
            try:
                shutil.rmtree(ycode_user_dir)
                print(f"✔ 已彻底删除用户配置及会话记录文件夹: {ycode_user_dir}")
            except Exception as e:
                print(f"⚠️ 删除配置文件夹失败: {e}")

    # 5. 清理新版目录结构：顶层的 Y-code.exe / ycode.exe，以及 _internal / tui 子目录
    print("🧹 正在清理程序文件...")
    for rel_file in ("Y-code.exe", "ycode.exe"):
        target = os.path.join(install_dir, rel_file)
        if os.path.exists(target):
            try:
                os.remove(target)
                print(f"✔ 已成功删除: {rel_file}")
            except Exception as e:
                print(f"⚠️ 删除 {rel_file} 失败: {e}")
    for rel_dir in ("_internal", "tui"):
        target_dir = os.path.join(install_dir, rel_dir)
        if os.path.isdir(target_dir):
            try:
                shutil.rmtree(target_dir)
                print(f"✔ 已成功删除目录: {rel_dir}")
            except Exception as e:
                print(f"⚠️ 删除目录 {rel_dir} 失败: {e}")

    print("\n============================================================")
    print("🎉 Y-code 卸载完成！")
    print("============================================================")

    # 6. 自销毁：删除卸载程序自身 (ycode_uninstall.exe) 和安装目录里剩余的一切。
    #    用 Popen 的 cwd 参数把子进程工作目录显式设成一个与 install_dir 无关的
    #    绝对路径（%TEMP%，找不到则退回 C:\），而不是依赖被 Popen 悄悄继承的
    #    当前工作目录——旧实现里 cmd 链里的 `cd ..` 假设卸载程序启动时 cwd
    #    正好在 install_dir 内，一旦不成立（例如从“应用和功能”面板启动，
    #    初始 cwd 可能是 System32），`cd ..` 就可能走到无关目录，最坏情况下
    #    cmd.exe 自身仍以 install_dir（或其子目录）为 cwd，导致后面的
    #    rmdir 因为目录“正被其他进程使用”而失败。显式指定 cwd 从根源上避免
    #    这个不确定性。
    if getattr(sys, "frozen", False):
        self_exe = sys.executable
        safe_cwd = os.environ.get("TEMP") or os.environ.get("TMP") or "C:\\"
        cmd_self_delete = (
            f'choice /t 1 /d y /n > nul & '
            f'del /f /q "{self_exe}" & '
            f'rmdir /q /s "{install_dir}"'
        )
        subprocess.Popen(cmd_self_delete, shell=True, cwd=safe_cwd)
        print("💡 卸载向导正在进行自销毁清理，控制台将在退出后自动清除残留文件。")
        time.sleep(1)
        sys.exit(0)
    else:
        print("本地测试环境下跳过自销毁步骤。")
        input("按任意键退出...")


if __name__ == "__main__":
    uninstall()
