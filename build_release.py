"""
build_release.py
Y-code Windows 发布打包流水线：一条命令生成完整的 Windows 安装包体系。

产出物（均在 dist/ 下）：
  dist/ycode/                  onedir 核心程序包，同时包含：
      Y-code.exe                 GUI 窗口版 (pywebview, console=False)
      ycode.exe                  TUI 控制台版 (console=True)
      _internal/                 两者共享的运行时依赖与数据文件
  dist/ycode_uninstall.exe     独立卸载向导
  dist/ycode_tui_setup_v{版本}.exe  独立 TUI 安装向导（供主安装包内嵌，也可单独分发）
  dist/ycode_setup_v{版本}.exe      主安装向导（默认装 GUI，可选附带装 TUI）
  （两个安装向导文件名带版本号，便于多渠道分发时区分新旧；内嵌进主安装包的
    tui/ycode_tui_setup.exe 仍用固定名，安装引擎按固定名查找。）

用法：
  venv\\Scripts\\python build_release.py                   完整构建
  venv\\Scripts\\python build_release.py --tui-only         只构建 TUI 相关产物，跳过主安装包
  venv\\Scripts\\python build_release.py --skip-frontend    跳过前端构建，直接用现有 frontend/dist
  venv\\Scripts\\python build_release.py --rebuild-frontend 强制重新执行 npm run build
  venv\\Scripts\\python build_release.py --setup-only       只重建两个安装向导（复用现有载荷）
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import zipfile

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
        help="只构建 TUI 相关产物 (dist/ycode、ycode_uninstall.exe、ycode_tui_setup_v*.exe)，"
             "跳过体积较大的主安装包 ycode_setup_v*.exe。",
    )
    parser.add_argument(
        "--setup-only", action="store_true",
        help="只重建两个安装向导 (ycode_setup_v*.exe / ycode_tui_setup_v*.exe)：复用现有 dist/ycode "
             "载荷与 dist/ycode_uninstall.exe，适用于仅改动 ycode_setup.py 后的快速重建。",
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
        # Windows 上 npm 实际可执行文件是 npm.cmd：用 shutil.which 解析完整路径后
        # 以列表参数直接调用（已验证无需 shell=True），消除 PATH 劫持/命令注入面
        npm_bin = shutil.which("npm")
        if npm_bin is None:
            raise FileNotFoundError("PATH 环境变量中未找到 npm")
        result = subprocess.run([npm_bin, "run", "build"], cwd=frontend_dir)
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
            # --windowed：安装向导是 tkinter GUI，不能弹出黑色控制台窗口
            pyinstaller_bin, "--clean", "--noconfirm", "--onefile", "--windowed", "--noupx",
            "--name", output_name,
            "--icon", os.path.join(project_dir, "assets", "ycode.ico"),
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


def make_payload_archive(project_dir, ycode_dir):
    """把 dist/ycode 整个目录压缩成单个 ycode_pkg.zip 作为安装向导的载荷。

    安装向导是 PyInstaller onefile exe：启动时要先把内嵌数据全部解压到临时
    目录才能显示界面。内嵌 4000+ 散文件时，解压 + 杀毒软件逐个扫描会让双击后
    超过一分钟毫无反应（用户以为安装包坏了）。压缩成单文件后，启动解压量从
    4000+ 个文件降到十几个，界面几秒内出现；真正的文件释放挪到安装步骤里做
    （有进度条，符合正常安装体验）。
    """
    zip_path = os.path.join(project_dir, "build", "ycode_pkg.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    print("📦 正在压缩核心程序包为单文件载荷 (ycode_pkg.zip)...")
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
        for root, _dirs, files in os.walk(ycode_dir):
            for name in files:
                src = os.path.join(root, name)
                zf.write(src, os.path.relpath(src, ycode_dir))
                count += 1
    size_mb = os.path.getsize(zip_path) / 1024 / 1024
    print(f"✔ 载荷压缩完成: {count} 个文件 -> {size_mb:.1f} MB")
    return zip_path


def step3_build_tui_setup(pyinstaller_bin, project_dir, payload_zip, uninstall_exe):
    print("\n[步骤 3/5] 正在编译 TUI 独立安装向导 ycode_tui_setup.exe...")
    # 同时内嵌卸载程序：这样即便用户脱离主安装包、单独运行这个 CLI 安装向导
    # （即"安装目录下还没有 Y-code.exe"的独立安装场景），也能正常复制出
    # 卸载程序并注册 Y-code-CLI 卸载入口，而不是只能在合并进完整版安装后
    # 才具备可卸载性。
    return build_setup_variant(
        pyinstaller_bin, project_dir, mode="tui", output_name="ycode_tui_setup",
        add_data_pairs=[
            (payload_zip, "."),
            (uninstall_exe, "."),
            ("config.example.json", "."),
            ("system_prompt.md", "."),
            # GUI 向导资源：横幅/窗口图标（经典样式已弃用 setup_bg.png 背景图）
            (os.path.join("assets", "ycode.png"), "assets"),
            (os.path.join("assets", "ycode.ico"), "assets"),
        ],
    )


def step4_build_full_setup(pyinstaller_bin, project_dir, payload_zip, uninstall_exe, tui_setup_exe):
    print("\n[步骤 4/5] 正在编译主安装向导 ycode_setup.exe...")
    return build_setup_variant(
        pyinstaller_bin, project_dir, mode="full", output_name="ycode_setup",
        add_data_pairs=[
            (payload_zip, "."),
            (uninstall_exe, "."),
            (tui_setup_exe, "tui"),
            ("config.example.json", "."),
            ("system_prompt.md", "."),
            # GUI 向导资源：横幅/窗口图标（经典样式已弃用 setup_bg.png 背景图）
            (os.path.join("assets", "ycode.png"), "assets"),
            (os.path.join("assets", "ycode.ico"), "assets"),
        ],
    )


# ============================================================================
# 步骤 4.5：安装包重命名为带版本号的最终分发名
# ============================================================================

def versioned_setup_name(base, version):
    """安装包最终分发文件名（不含扩展名）：ycode_setup_v1.1.48 样式。"""
    return f"{base}_v{version}"


def step4b_rename_setups(project_dir, version, tui_only):
    """把固定名构建产物重命名为带版本号的分发名，并清理上一版本的同名旧产物。

    构建期仍用固定名（ycode_setup / ycode_tui_setup）：主安装包内嵌的
    tui/ycode_tui_setup.exe 由安装引擎按固定名查找，不能带版本号；最终落到
    dist/ 供分发的文件才改成带版本号的名字。"""
    print("\n[步骤 4.5/5] 安装包重命名为带版本号的分发名...")
    bases = ["ycode_tui_setup"] if tui_only else ["ycode_tui_setup", "ycode_setup"]
    renamed = {}
    for base in bases:
        src = os.path.join(project_dir, "dist", f"{base}.exe")
        if not os.path.exists(src):
            print(f"❌ 未找到待重命名产物: {src}")
            sys.exit(1)
        # 清理历史版本留下的同名旧产物，避免 dist/ 里堆叠多个版本难以分辨
        for old in glob.glob(os.path.join(project_dir, "dist", f"{base}_v*.exe")):
            os.remove(old)
        dst = os.path.join(project_dir, "dist", f"{versioned_setup_name(base, version)}.exe")
        os.replace(src, dst)
        renamed[base] = dst
        print(f"✔ {base}.exe -> {os.path.basename(dst)}")
    return renamed


# ============================================================================
# 步骤 5：汇总校验
# ============================================================================

def step5_summary(project_dir, tui_only, version):
    print("\n[步骤 5/5] 校验构建产物...")
    rows = [
        (os.path.join("dist", "ycode", "Y-code.exe"), "GUI 主程序 (窗口版)"),
        (os.path.join("dist", "ycode", "ycode.exe"), "TUI 主程序 (控制台版)"),
        (os.path.join("dist", "ycode_uninstall.exe"), "卸载程序"),
        (os.path.join("dist", f"{versioned_setup_name('ycode_tui_setup', version)}.exe"), "TUI 独立安装包"),
    ]
    if not tui_only:
        rows.append((os.path.join("dist", f"{versioned_setup_name('ycode_setup', version)}.exe"),
                     "主安装包 (GUI + 可选 TUI)"))

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
        print("ℹ 已指定 --tui-only，主安装包未构建（如需要请去掉该参数重新运行）。")
    else:
        print(f"👉 您的 Windows 双击主安装包位于: "
              f"{os.path.join(project_dir, 'dist', versioned_setup_name('ycode_setup', version) + '.exe')}")
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

    if args.setup_only:
        # 快速通道：核心载荷与卸载程序未变，直接复用现有产物，只重新编译安装向导。
        ycode_dir = os.path.join(project_dir, "dist", "ycode")
        uninstall_exe = os.path.join(project_dir, "dist", "ycode_uninstall.exe")
        if not os.path.isdir(ycode_dir):
            print(f"❌ --setup-only 需要现有核心载荷目录: {ycode_dir}（请先完整构建一次）。")
            sys.exit(1)
        if not os.path.exists(uninstall_exe):
            print(f"❌ --setup-only 需要现有卸载程序: {uninstall_exe}（请先完整构建一次）。")
            sys.exit(1)
        print("ℹ 已指定 --setup-only，跳过前端 / 核心载荷 / 卸载程序构建，直接复用现有产物。")
        payload_zip = make_payload_archive(project_dir, ycode_dir)
    else:
        step0_build_frontend(project_dir, args)
        ycode_dir = step1_build_core(pyinstaller_bin, project_dir)
        payload_zip = make_payload_archive(project_dir, ycode_dir)
        uninstall_exe = step2_build_uninstaller(pyinstaller_bin, project_dir)

    tui_setup_exe = step3_build_tui_setup(pyinstaller_bin, project_dir, payload_zip, uninstall_exe)

    if not args.tui_only:
        step4_build_full_setup(pyinstaller_bin, project_dir, payload_zip, uninstall_exe, tui_setup_exe)

    version = read_project_version(project_dir)
    step4b_rename_setups(project_dir, version, args.tui_only)
    step5_summary(project_dir, args.tui_only, version)


if __name__ == "__main__":
    build()
