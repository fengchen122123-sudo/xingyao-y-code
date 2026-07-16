import os
import subprocess
import shutil
import sys

sys.stdout.reconfigure(encoding='utf-8')

def build():
    print("=========================================")
    # 确保当前目录是工程根目录
    project_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_dir)
    
    # 虚拟环境的 PyInstaller 路径
    pyinstaller_bin = os.path.join(project_dir, "venv", "Scripts", "pyinstaller.exe")
    if not os.path.exists(pyinstaller_bin):
        pyinstaller_bin = "pyinstaller" # 备用全局命令
        
    print(f"1. 正在编译核心主程序 ycode.exe...")
    # --clean: 清除缓存
    # --onefile: 生成单个独立 .exe
    # --console: 保持控制台窗口（TUI 所需）
    cmd_core = [
        pyinstaller_bin,
        "--clean",
        "--onefile",
        "--console",
        "--name", "ycode",
        "main.py"
    ]
    print("运行命令:", " ".join(cmd_core))
    subprocess.run(cmd_core, check=True)
    
    print("\n2. 正在验证核心程序是否编译成功...")
    ycode_exe = os.path.join(project_dir, "dist", "ycode.exe")
    if os.path.exists(ycode_exe):
        print(f"✔ 核心程序成功生成: {ycode_exe}")
    else:
        print("🚨 核心程序编译失败！")
        sys.exit(1)
        
    print("\n3. 正在编译安装向导程序 ycode_setup.exe...")
    # 将 dist/ycode.exe 打包进 ycode_setup.exe
    # 在 Windows 下，数据文件打包语法为 "源路径;目标包内相对目录"，用半角分号分割
    cmd_setup = [
        pyinstaller_bin,
        "--clean",
        "--onefile",
        "--console",
        "--add-data", f"dist/ycode.exe;.",
        "--name", "ycode_setup",
        "ycode_setup.py"
    ]
    print("运行命令:", " ".join(cmd_setup))
    subprocess.run(cmd_setup, check=True)
    
    print("\n4. 正在验证安装程序是否编译成功...")
    setup_exe = os.path.join(project_dir, "dist", "ycode_setup.exe")
    if os.path.exists(setup_exe):
        print(f"✔ 安装程序成功生成: {setup_exe}")
        print("\n=========================================")
        print("🎉 恭喜！发布包编译圆满成功！")
        print(f"👉 您的 Windows 双击安装包位于: {setup_exe}")
        print("=========================================")
    else:
        print("🚨 安装程序编译失败！")
        sys.exit(1)

if __name__ == "__main__":
    build()
