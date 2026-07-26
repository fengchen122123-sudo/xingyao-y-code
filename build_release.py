"""
build_release.py
Y-code Windows 发布打包流水线：一条命令生成完整的 Windows 安装包体系。

产出物（均在 dist/ 下）：
  dist/ycode/                  onedir 核心程序包，同时包含：
      Y-code.exe                 GUI 窗口版 (pywebview, console=False)
      ycode.exe                  TUI 控制台版 (console=True)
      _internal/                 两者共享的运行时依赖与数据文件
  dist/ycode_uninstall.exe     独立卸载向导
  dist/ycode_tui_setup.exe     独立 TUI 安装向导（供主安装包内嵌，也可单独分发）
  dist/ycode_setup.exe         主安装向导（默认装 GUI，可选附带装 TUI）

用法：
  venv\\Scripts\\python build_release.py                   完整构建
  venv\\Scripts\\python build_release.py --tui-only         只构建 TUI 相关产物，跳过主安装包
  venv\\Scripts\\python build_release.py --skip-frontend    跳过前端构建，直接用现有 frontend/dist
  venv\\Scripts\\python build_release.py --rebuild-frontend 强制重新执行 npm run build
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8')


# ============================================================================
# 通用工具函数
# ============================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Y-code Windows 发布打包流水线（GUI 主安装包 + 可选 TUI 组件）。"
    )
    parser.add_argument(
        "--skip-frontend", action="store_true",
        help="跳过前端构建步骤，直接复用现有的 frontend/dist 产物"
             "（不存在也不会报错，但生成的 GUI 版启动后将无法加载界面）。",
    )
    parser.add_argument(
        "--rebuild-frontend", action="store_true",
        help="强制重新执行 npm run build，即使 frontend/dist/index.html 已经存在。",
    )
    parser.add_argument(
        "--tui-only", action="store_true",
        help="只构建 TUI 相关产物 (dist/ycode、ycode_uninstall.exe、ycode_tui_setup.exe)，"
             "跳过体积较大的主安装包 ycode_setup.exe。",
    )
    return parser.parse_args()


def resolve_pyinstaller(project_dir):
    """优先使用虚拟环境自带的 pyinstaller.exe，找不到再退回全局命令；两者都没有就直接报错退出。"""
    venv_bin = os.path.join(project_dir, "venv", "Scripts", "pyinstaller.exe")
    if os.path.exists(venv_bin):
        return venv_bin
    global_bin = shutil.which("pyinstaller")
    if global_bin:
        return global_bin
    print("❌ 未找到 PyInstaller，无法继续构建。")
    print("   请先创建/激活虚拟环境并安装依赖，例如：")
    print("   venv\\Scripts\\activate")
    print("   venv\\Scripts\\pip install pyinstaller")
    sys.exit(1)


def read_project_version(project_dir):
    """从 core/version.py 用正则提取版本号；不 import core 包，避免不必要的依赖耦合。"""
    version_file = os.path.join(project_dir, "core", "version.py")
    try:
        with open(version_file, "r", encoding="utf-8") as f:
            content = f.read()
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
        if match:
            return match.group(1)
    except OSError:
        pass
    print("⚠️ 未能从 core/version.py 解析出版本号，安装包 DisplayVersion 将回退为 0.0.0。")
    return "0.0.0"


def run_checked(cmd, step_desc):
    """运行一个构建子命令；失败时打印清晰的中文错误并终止流水线（而不是抛出 Python 堆栈）。"""
    print("运行命令:", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"❌ {step_desc}失败（退出码 {result.returncode}），已终止后续构建步骤。")
        sys.exit(1)


def format_size_mb(path):
    size_bytes = os.path.getsize(path)
    return f"{size_bytes / (1024 * 1024):.1f} MB"


# ============================================================================
# 步骤 0：前端构建
# ============================================================================

def step0_build_frontend(project_dir, args):
    print("\n[步骤 0/5] 检查 / 构建前端产物 (frontend/dist)...")
    frontend_dir = os.path.join(project_dir, "frontend")
    dist_dir = os.path.join(frontend_dir, "dist")
    index_html = os.path.join(dist_dir, "index.html")

    if args.skip_frontend:
        print("ℹ 已指定 --skip-frontend，跳过前端构建。")
        # ycode.spec 的 GUI Analysis 会把 frontend/dist 整个目录当作 datas 源，
        # 目录本身必须存在，否则 PyInstaller 会在 Analysis 阶段直接报错退出。
        os.makedirs(dist_dir, exist_ok=True)
        if not os.path.exists(index_html):
            print("⚠️ frontend/dist/index.html 不存在，构建出的 GUI 版 (Y-code.exe) 启动后将无法加载界面。")
        return

    if os.path.exists(index_html) and not args.rebuild_frontend:
        print(f"✅ 已检测到前端产物: {index_html}（如需强制重新构建，请加上 --rebuild-frontend）。")
        return

    if not os.path.isdir(frontend_dir):
        print(f"❌ 未找到前端项目目录: {frontend_dir}")
        sys.exit(1)

    print("🚀 正在执行 npm run build ...")
    try:
        # Windows 上 npm 实际可执行文件是 npm.cmd，必须 shell=True 才能被正确解析到。
        result = subprocess.run(["npm", "run", "build"], cwd=frontend_dir, shell=True)
    except OSError as exc:
        print("❌ 前端构建失败：无法启动 npm 命令。")
        print(f"   错误详情: {exc}")
        print("   请确认已安装 Node.js（建议 20 及以上版本），且 npm 已加入 PATH 环境变量。")
        sys.exit(1)

    if result.returncode != 0:
        print(f"❌ 前端构建失败（npm run build 退出码 {result.returncode}）。")
        print("   请检查上方 npm 输出的报错信息，常见原因：")
        print("   1) 未安装 Node.js / npm，或版本过低（建议 Node 20+）")
        print("   2) 尚未执行过 npm install 安装前端依赖")
        print("   3) frontend/src 下的 TypeScript / Vue 代码存在编译错误")
        sys.exit(1)

    if not os.path.exists(index_html):
        print("❌ npm run build 已执行完成，但未生成 frontend/dist/index.html，请检查 vite 构建配置。")
        sys.exit(1)

    print(f"✅ 前端构建成功: {index_html}")


# ============================================================================
# 步骤 1：核心双主程序 (Y-code.exe + ycode.exe)
# ============================================================================

def step1_build_core(pyinstaller_bin, project_dir):
    print("\n[步骤 1/5] 正在编译核心程序包 (Y-code.exe GUI + ycode.exe TUI)...")
    # 用 ycode.spec 而不是一堆 CLI flag：spec 里配置了两套 Analysis（分别对应
    # GUI/TUI 两个入口）、collect_all 依赖收集与 upx=False（避免启动时内存
    # 解压 + Defender 扫描拖慢启动速度），这些精细控制没法完全用 CLI 表达。
    cmd = [pyinstaller_bin, "--clean", "--noconfirm", "ycode.spec"]
    run_checked(cmd, "核心程序包编译")

    ycode_dir = os.path.join(project_dir, "dist", "ycode")
    tui_exe = os.path.join(ycode_dir, "ycode.exe")
    gui_exe = os.path.join(ycode_dir, "Y-code.exe")
    missing = [p for p in (tui_exe, gui_exe) if not os.path.exists(p)]
    if missing:
        print("❌ 核心程序包生成不完整，缺少以下文件：")
        for p in missing:
            print(f"   - {p}")
        sys.exit(1)
    print(f"✅ 核心程序包 (onedir) 生成成功: {ycode_dir}")
    print(f"   - GUI 窗口版  : {gui_exe}")
    print(f"   - TUI 控制台版: {tui_exe}")
    return ycode_dir


# ============================================================================
# 步骤 2：卸载程序
# ============================================================================

def step2_build_uninstaller(pyinstaller_bin, project_dir):
    print("\n[步骤 2/5] 正在编译卸载程序 ycode_uninstall.exe...")
    cmd = [pyinstaller_bin, "--clean", "--noconfirm", "ycode_uninstall.spec"]
    run_checked(cmd, "卸载程序编译")

    uninstall_exe = os.path.join(project_dir, "dist", "ycode_uninstall.exe")
    if not os.path.exists(uninstall_exe):
        print(f"❌ 未找到预期产物: {uninstall_exe}")
        sys.exit(1)
    print(f"✅ 卸载程序生成成功: {uninstall_exe}")
    return uninstall_exe


# ============================================================================
# 步骤 3 / 4：安装向导
# (ycode_setup.py 是同一份源码；靠内嵌的 setup_mode.txt marker 文件区分
#  'tui' / 'full' 两种运行模式，因此这里构建两次、每次内嵌不同的数据。)
# ============================================================================

def build_setup_variant(pyinstaller_bin, project_dir, mode, output_name, add_data_pairs):
    """通用安装向导构建：把 mode（'tui'/'full'）和版本号写入临时 marker 文件，随构建一并打包进 exe。

    这里没有用静态 .spec 文件表达 tui/full 两个变体，是因为它们依赖的内嵌数据
    ——尤其是 dist/ycode_tui_setup.exe 这种“上一步骤才产出”的文件——在两次
    调用之间是不同的。用 CLI --add-data 动态拼装，比维护两份高度相似、还要
    应对“某些源文件此刻可能还不存在”的 spec 文件更清晰，也更不容易出现
    “spec 文件内容和实际构建命令对不上”的配置漂移。
    """
    # 用项目内 build/ 下的临时目录而不是系统 %TEMP%：一是复用已被 .gitignore
    # 忽略的 build/ 目录，二是避免当用户目录名包含中文等非 ASCII 字符时，
    # 个别工具链对系统临时目录路径处理不稳的问题。
    tmp_dir = os.path.join(project_dir, "build", f"_setup_tmp_{mode}")
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        marker_path = os.path.join(tmp_dir, "setup_mode.txt")
        with open(marker_path, "w", encoding="utf-8") as f:
            f.write(mode)

        version_path = os.path.join(tmp_dir, "version.txt")
        with open(version_path, "w", encoding="utf-8") as f:
            f.write(read_project_version(project_dir))

        cmd = [
            pyinstaller_bin, "--clean", "--noconfirm", "--onefile", "--console", "--noupx",
            "--name", output_name,
            # --specpath 指向临时目录：避免 PyInstaller 自动生成/覆盖同名的
            # ycode_setup.spec —— 项目根目录下那份是给开发者单独手动构建用的
            # 参考 spec，绝不能被这里的动态 CLI 构建悄悄覆盖掉。
            "--specpath", tmp_dir,
            "--add-data", f"{marker_path};.",
            "--add-data", f"{version_path};.",
        ]
        for src, dest in add_data_pairs:
            # --specpath 指向临时目录后，PyInstaller 会以 spec 所在目录解析
            # 相对的 add-data 源路径，因此这里统一换算成项目根目录下的绝对路径
            src_abs = src if os.path.isabs(src) else os.path.join(project_dir, src)
            cmd += ["--add-data", f"{src_abs};{dest}"]
        cmd.append(os.path.join(project_dir, "ycode_setup.py"))
        run_checked(cmd, f"安装向导编译 ({output_name}.exe, mode={mode})")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    exe_path = os.path.join(project_dir, "dist", f"{output_name}.exe")
    if not os.path.exists(exe_path):
        print(f"❌ 未找到预期产物: {exe_path}")
        sys.exit(1)
    print(f"✅ {output_name}.exe 生成成功: {exe_path}")
    return exe_path


def step3_build_tui_setup(pyinstaller_bin, project_dir, ycode_dir, uninstall_exe):
    print("\n[步骤 3/5] 正在编译 TUI 独立安装向导 ycode_tui_setup.exe...")
    # 同时内嵌卸载程序：这样即便用户脱离主安装包、单独运行这个 TUI 安装向导
    # （即“安装目录下还没有 Y-code.exe”的独立安装场景），也能正常复制出
    # 卸载程序并注册 Y-code-TUI 卸载入口，而不是只能在合并进完整版安装后
    # 才具备可卸载性。
    return build_setup_variant(
        pyinstaller_bin, project_dir, mode="tui", output_name="ycode_tui_setup",
        add_data_pairs=[
            (ycode_dir, "ycode_pkg"),
            (uninstall_exe, "."),
            ("config.example.json", "."),
            ("system_prompt.md", "."),
        ],
    )


def step4_build_full_setup(pyinstaller_bin, project_dir, ycode_dir, uninstall_exe, tui_setup_exe):
    print("\n[步骤 4/5] 正在编译主安装向导 ycode_setup.exe...")
    return build_setup_variant(
        pyinstaller_bin, project_dir, mode="full", output_name="ycode_setup",
        add_data_pairs=[
            (ycode_dir, "ycode_pkg"),
            (uninstall_exe, "."),
            (tui_setup_exe, "tui"),
            ("config.example.json", "."),
            ("system_prompt.md", "."),
        ],
    )


# ============================================================================
# 步骤 5：汇总校验
# ============================================================================

def step5_summary(project_dir, tui_only):
    print("\n[步骤 5/5] 校验构建产物...")
    rows = [
        (os.path.join("dist", "ycode", "Y-code.exe"), "GUI 主程序 (窗口版)"),
        (os.path.join("dist", "ycode", "ycode.exe"), "TUI 主程序 (控制台版)"),
        (os.path.join("dist", "ycode_uninstall.exe"), "卸载程序"),
        (os.path.join("dist", "ycode_tui_setup.exe"), "TUI 独立安装包"),
    ]
    if not tui_only:
        rows.append((os.path.join("dist", "ycode_setup.exe"), "主安装包 (GUI + 可选 TUI)"))

    print("\n" + "=" * 64)
    print("📦 构建产物一览")
    print("=" * 64)
    all_ok = True
    for rel_path, desc in rows:
        abs_path = os.path.join(project_dir, rel_path)
        if os.path.exists(abs_path):
            print(f"  ✅ {rel_path:<34} {format_size_mb(abs_path):>9}   {desc}")
        else:
            print(f"  ❌ {rel_path:<34} {'缺失':>9}   {desc}")
            all_ok = False
    print("=" * 64)

    if not all_ok:
        print("🚨 部分产物缺失，请检查上方构建日志。")
        sys.exit(1)

    print("🎉 恭喜！发布包编译圆满成功！")
    if tui_only:
        print("ℹ 已指定 --tui-only，主安装包 ycode_setup.exe 未构建（如需要请去掉该参数重新运行）。")
    else:
        print(f"👉 您的 Windows 双击主安装包位于: {os.path.join(project_dir, 'dist', 'ycode_setup.exe')}")
    print("=" * 64)


# ============================================================================
# 主流程
# ============================================================================

def build():
    args = parse_args()

    # 确保当前目录是工程根目录：后面所有相对路径（ycode.spec、frontend/、
    # core/version.py、dist/...）都以此为基准。
    project_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_dir)

    pyinstaller_bin = resolve_pyinstaller(project_dir)

    step0_build_frontend(project_dir, args)
    ycode_dir = step1_build_core(pyinstaller_bin, project_dir)
    uninstall_exe = step2_build_uninstaller(pyinstaller_bin, project_dir)
    tui_setup_exe = step3_build_tui_setup(pyinstaller_bin, project_dir, ycode_dir, uninstall_exe)

    if not args.tui_only:
        step4_build_full_setup(pyinstaller_bin, project_dir, ycode_dir, uninstall_exe, tui_setup_exe)

    step5_summary(project_dir, args.tui_only)


if __name__ == "__main__":
    build()
