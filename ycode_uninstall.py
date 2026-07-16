import os
import sys
import shutil
import subprocess
import time
import ctypes

def print_logo():
    print("""
============================================================
              __   __                     _      
              \\ \\ / /                    | |     
               \\ V / ___  ___  ___   __| | ___ 
                \\ / / __|/ _ \\/ _ \\ / _` |/ _ \\
                | | \\__ \\  __/ (_) | (_| |  __/
                \\_/ |___/\\___|\\___/ \\__,_|\\___|
                                                 
                 ❌ Y-code TUI 卸载向导 ❌
============================================================
    """)

def uninstall():
    print_logo()
    print("👋 欢迎使用 Y-code 官方 Windows 卸载向导！")
    print("本向导将从您的电脑上安全卸载星瑶 (Y-code TUI)。\n")
    
    # 确定安装路径 (当前可执行文件所在的目录)
    if getattr(sys, 'frozen', False):
        install_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        install_dir = os.path.dirname(os.path.abspath(__file__))
        
    print(f"📁 确定的安装路径: {install_dir}")
    confirm = input("确定要卸载 Y-code TUI 吗？ (y/N): ").strip().lower()
    if confirm != 'y':
        print("卸载已取消。")
        time.sleep(2)
        sys.exit(0)
        
    # 问询是否删除配置和历史记录
    delete_config = input("是否同时删除您的历史会话与模型配置？ (.ycode 文件夹) (y/N): ").strip().lower()
    
    print("\n🚀 正在执行卸载步骤...")
    
    # 1. 删除桌面快捷方式
    user_home = os.path.expanduser("~")
    desktop_dir = os.path.join(user_home, "Desktop")
    shortcut_path = os.path.join(desktop_dir, "Y-code TUI.lnk")
    if os.path.exists(shortcut_path):
        try:
            os.remove(shortcut_path)
            print("✔ 已成功删除桌面快捷方式。")
        except Exception as e:
            print(f"⚠️ 删除快捷方式失败: {str(e)}")
            
    # 2. 从环境变量 PATH 中删除安装路径
    print("🌐 正在从环境变量 (PATH) 中移除路径...")
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
        reg_query = subprocess.run(
            ['reg', 'query', 'HKCU\\Environment', '/v', 'PATH'],
            capture_output=True, text=True, startupinfo=startupinfo
        )
        current_path = ""
        if reg_query.returncode == 0:
            lines = reg_query.stdout.splitlines()
            for line in lines:
                if "PATH" in line:
                    parts = line.split(None, 2)
                    if len(parts) >= 3:
                        current_path = parts[2]
                        
        if install_dir.lower() in current_path.lower():
            # 过滤掉当前安装目录
            paths = current_path.split(';')
            new_paths = [p for p in paths if p and p.lower() != install_dir.lower()]
            new_path = ';'.join(new_paths)
            
            # 写回环境变量
            subprocess.run(
                ['reg', 'add', 'HKCU\\Environment', '/v', 'PATH', '/t', 'REG_EXPAND_SZ', '/d', new_path, '/f'],
                startupinfo=startupinfo
            )
            # 广播变量更改
            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            SMTO_ABORTIFHUNG = 0x0002
            result_val = ctypes.c_long()
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST,
                WM_SETTINGCHANGE,
                0,
                "Environment",
                SMTO_ABORTIFHUNG,
                1000,
                ctypes.byref(result_val)
            )
            print("✔ 成功从环境变量 PATH 中移除 Y-code 路径！")
        else:
            print("ℹ 环境变量 PATH 中未找到 Y-code 路径。")
    except Exception as e:
        print(f"⚠️ 移除环境变量时发生警告: {str(e)}")
        
    # 3. 如果用户同意，删除 ~/.ycode 文件夹
    if delete_config == 'y':
        ycode_user_dir = os.path.join(user_home, ".ycode")
        if os.path.exists(ycode_user_dir):
            try:
                shutil.rmtree(ycode_user_dir)
                print(f"✔ 已彻底删除用户配置及会话记录文件夹: {ycode_user_dir}")
            except Exception as e:
                print(f"⚠️ 删除配置文件夹失败: {str(e)}")
                
    # 4. 删除主程序 ycode.exe 并准备自我销毁
    ycode_exe = os.path.join(install_dir, "ycode.exe")
    if os.path.exists(ycode_exe):
        try:
            os.remove(ycode_exe)
            print("✔ 已成功卸载核心主程序。")
        except Exception as e:
            print(f"⚠️ 删除核心主程序失败: {str(e)}")
            
    print("\n============================================================")
    print("🎉 Y-code TUI 卸载完成！")
    print("============================================================")
    
    # 5. 经典自销毁逻辑：在程序退出前，开启一个 cmd 后台进程，在 1 秒后自动删除卸载程序和安装目录本身
    if getattr(sys, 'frozen', False):
        self_exe = sys.executable
        # 用 choice 等待并强行静默删除当前 exe，然后删除当前文件夹
        cmd_self_delete = f'choice /t 1 /d y /n > nul & del /f /q "{self_exe}" & cd .. & rmdir /q /s "{install_dir}"'
        subprocess.Popen(cmd_self_delete, shell=True)
        print("💡 卸载向导正在进行自销毁清理，控制台将在退出后自动清除残留文件。")
        time.sleep(1)
        sys.exit(0)
    else:
        print("本地测试环境下跳过自销毁步骤。")
        input("按任意键退出...")

if __name__ == "__main__":
    uninstall()
