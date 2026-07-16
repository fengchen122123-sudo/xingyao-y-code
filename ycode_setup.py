import os
import sys
import shutil
import subprocess
import time
import ctypes

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def print_logo():
    print("""
============================================================
              __   __                     _      
              \\ \\ / /                    | |     
               \\ V / ___  ___  ___   __| | ___ 
                \\ / / __|/ _ \\/ _ \\ / _` |/ _ \\
                | | \\__ \\  __/ (_) | (_| |  __/
                \\_/ |___/\\___|\\___/ \\__,_|\\___|
                                                 
                 ✨ Y-code TUI 终端 AI 助手 ✨
============================================================
    """)

def install():
    print_logo()
    print("👋 欢迎使用 Y-code 官方 Windows 安装向导！")
    print("本向导将为您安装星瑶 (Y-code TUI) 到您的电脑上。\n")
    
    # 1. 确定安装路径 (默认为用户 AppData 目录，避免需要管理员权限)
    user_home = os.path.expanduser("~")
    default_install_dir = os.path.join(user_home, "AppData", "Local", "Programs", "Y-code")
    
    print(f"📁 默认安装路径: {default_install_dir}")
    choice = input("是否同意安装到此路径？ (Y/n): ").strip().lower()
    if choice and choice != 'y':
        custom_dir = input("请输入您自定义的安装绝对路径: ").strip()
        if custom_dir:
            install_dir = custom_dir
        else:
            install_dir = default_install_dir
    else:
        install_dir = default_install_dir
        
    os.makedirs(install_dir, exist_ok=True)
    
    # 2. 确定打包文件的源路径
    if getattr(sys, 'frozen', False):
        # 运行在 PyInstaller 打包后的临时目录
        bundle_dir = sys._MEIPASS
    else:
        # 本地开发测试环境
        bundle_dir = os.path.dirname(os.path.abspath(__file__))
        
    src_exe = os.path.join(bundle_dir, "ycode.exe")
    dest_exe = os.path.join(install_dir, "ycode.exe")
    
    if not os.path.exists(src_exe):
        # 兼容性开发模式测试：如果 dist/ycode.exe 存在
        dev_exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "ycode.exe")
        if os.path.exists(dev_exe):
            src_exe = dev_exe
            
    if not os.path.exists(src_exe):
        print(f"🚨 错误：未能在安装包中找到核心文件 ycode.exe！(检测路径: {src_exe})")
        print("请检查安装包完整性，或联系开发者。")
        input("按任意键退出安装...")
        sys.exit(1)
        
    # 3. 复制可执行文件
    print("\n🚀 正在复制核心文件...")
    try:
        shutil.copy2(src_exe, dest_exe)
        print(f"✔ 核心文件成功复制到: {dest_exe}")
    except Exception as e:
        print(f"🚨 复制文件失败: {str(e)}")
        print("请确保目标目录可写，且没有其他 ycode 进程在运行。")
        input("按任意键退出安装...")
        sys.exit(1)
        
    # 4. 创建 ~/.ycode 文件夹，并写入初始配置文件
    print("\n⚙️ 正在初始化用户配置环境...")
    ycode_user_dir = os.path.join(user_home, ".ycode")
    os.makedirs(ycode_user_dir, exist_ok=True)
    
    # 初始化 config.json
    config_path = os.path.join(ycode_user_dir, "config.json")
    if not os.path.exists(config_path):
        src_config = os.path.join(bundle_dir, "config.json")
        if not os.path.exists(src_config):
            src_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
            
        if os.path.exists(src_config):
            try:
                shutil.copy2(src_config, config_path)
                print(f"✔ 成功创建配置模板: {config_path}")
            except:
                pass
        else:
            with open(config_path, "w", encoding="utf-8") as f:
                f.write('{"active_model_id": null, "providers": []}')
                print(f"✔ 成功创建默认配置: {config_path}")
                
    # 初始化 system_prompt.md
    prompt_path = os.path.join(ycode_user_dir, "system_prompt.md")
    if not os.path.exists(prompt_path):
        src_prompt = os.path.join(bundle_dir, "system_prompt.md")
        if not os.path.exists(src_prompt):
            src_prompt = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.md")
            
        if os.path.exists(src_prompt):
            try:
                shutil.copy2(src_prompt, prompt_path)
                print(f"✔ 成功创建人设模板: {prompt_path}")
            except:
                pass
                
    # 5. 将安装路径添加到系统 User PATH 环境变量中
    print("\n🌐 正在配置环境变量 (PATH)...")
    try:
        # 获取当前的 User PATH
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
                        
        if install_dir.lower() not in current_path.lower():
            # 拼接并更新
            new_path = current_path + ";" + install_dir if current_path else install_dir
            # 使用 reg 命令更新 HKCU 环境变量 (避开对机器环境变量的修改，无需特权)
            subprocess.run(
                ['reg', 'add', 'HKCU\\Environment', '/v', 'PATH', '/t', 'REG_EXPAND_SZ', '/d', new_path, '/f'],
                startupinfo=startupinfo
            )
            # 广播 WM_SETTINGCHANGE 消息，使环境变量无需重启直接生效 (使用 SendMessageTimeoutW 避免卡死在挂起窗口)
            import ctypes.wintypes
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
            print("✔ 成功将 Y-code 路径加入环境变量 PATH！")
        else:
            print("ℹ Y-code 路径已存在于环境变量中，跳过配置。")
    except Exception as e:
        print(f"⚠️ 配置环境变量时发生警告: {str(e)}")
        print("请稍后手动将安装目录添加到您的系统环境变量中。")

    # 6. 创建桌面快捷方式
    print("\n🖥️ 正在创建桌面快捷方式...")
    try:
        desktop_dir = os.path.join(user_home, "Desktop")
        shortcut_path = os.path.join(desktop_dir, "Y-code TUI.lnk")
        
        # 使用 PowerShell 动态生成 LNK 快捷方式
        ps_cmd = (
            f"$WshShell = New-Object -ComObject WScript.Shell; "
            f"$Shortcut = $WshShell.CreateShortcut('{shortcut_path}'); "
            f"$Shortcut.TargetPath = '{dest_exe}'; "
            f"$Shortcut.WorkingDirectory = '{install_dir}'; "
            f"$Shortcut.Save();"
        )
        subprocess.run(['powershell', '-Command', ps_cmd], startupinfo=startupinfo)
        print(f"✔ 桌面快捷方式创建成功: {shortcut_path}")
    except Exception as e:
        print(f"⚠️ 创建桌面快捷方式失败: {str(e)}")
        
    print("\n============================================================")
    print("🎉 Y-code TUI 安装成功！")
    print(f"安装路径: {install_dir}")
    print(f"配置文件夹: {ycode_user_dir}")
    print("------------------------------------------------------------")
    print("🚀 启动方法：")
    print("1. 双击桌面上的 [Y-code TUI] 快捷方式即可开启对话。")
    print("2. 打开任意全新的命令行窗口 (CMD/PowerShell)，输入 ycode 敲回车。")
    print("============================================================")
    input("\n按任意键退出安装向导...")

if __name__ == "__main__":
    install()
