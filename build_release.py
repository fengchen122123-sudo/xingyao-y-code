"""
build_release.py
Y-code Windows 发布打包流水线：一条命令生成完整的 Windows 安装包体系。

产出物（均在 dist/ 下）：
  dist/ycode/                  onedir 核心程序包，同时包含：
      Y-code.exe                 GUI 窗口版 (pywebview, console=False)
      ycode.exe                  CLI 控制台版 (console=True)
      _internal/                 两者共享的运行时依赖与数据文件
  dist/ycode_uninstall.exe     独立卸载向导
  dist/ycode_cli_setup_v{版本}.exe  独立 CLI 安装向导（供主安装包内嵌，也可单独分发）
  dist/ycode_setup_v{版本}.exe      主安装向导（默认装 GUI，可选附带装 CLI）
  dist/release_parts/          Gitee Release 附件（超 100MB 的安装包按 ≤95MB
                               二进制分卷 .part1/.part2/...，附 RELEASE_NOTES.txt
                               上传指引，内含 SHA256 校验行）
  （两个安装向导文件名带版本号，便于多渠道分发时区分新旧；内嵌进主安装包的
    cli/ycode_cli_setup.exe 仍用固定名，安装引擎按固定名查找。）

用法：
  venv\\Scripts\\python build_release.py                   完整构建
  venv\\Scripts\\python build_release.py --cli-only         只构建 CLI 相关产物，跳过主安装包
  venv\\Scripts\\python build_release.py --skip-frontend    跳过前端构建，直接用现有 frontend/dist
  venv\\Scripts\\python build_release.py --rebuild-frontend 强制重新执行 npm run build
  venv\\Scripts\\python build_release.py --setup-only       只重建两个安装向导（复用现有载荷）
  venv\\Scripts\\python build_release.py --no-split-parts   跳过分卷切割（产物不分发到 Gitee 时用）
"""
import argparse
import glob
import hashlib
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
        description="Y-code Windows 发布打包流水线（GUI 主安装包 + 可选 CLI 组件）。"
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
        "--cli-only", action="store_true",
        help="只构建 CLI 相关产物 (dist/ycode、ycode_uninstall.exe、ycode_cli_setup_v*.exe)，"
             "跳过体积较大的主安装包 ycode_setup_v*.exe。",
    )
    parser.add_argument(
        "--setup-only", action="store_true",
        help="只重建两个安装向导 (ycode_setup_v*.exe / ycode_cli_setup_v*.exe)：复用现有 dist/ycode "
             "载荷与 dist/ycode_uninstall.exe，适用于仅改动 ycode_setup.py 后的快速重建。",
    )
    parser.add_argument(
        "--no-split-parts", action="store_true",
        help="跳过步骤 4.7 的 Gitee Release 分卷切割（产物不经 Gitee 分发时用）。",
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
    print("\n[步骤 1/5] 正在编译核心程序包 (Y-code.exe GUI + ycode.exe CLI)...")
    # 用 ycode.spec 而不是一堆 CLI flag：spec 里配置了两套 Analysis（分别对应
    # GUI/CLI 两个入口）、collect_all 依赖收集与 upx=False（避免启动时内存
    # 解压 + Defender 扫描拖慢启动速度），这些精细控制没法完全用 CLI 表达。
    cmd = [pyinstaller_bin, "--clean", "--noconfirm", "ycode.spec"]
    run_checked(cmd, "核心程序包编译")

    ycode_dir = os.path.join(project_dir, "dist", "ycode")
    cli_exe = os.path.join(ycode_dir, "ycode.exe")
    gui_exe = os.path.join(ycode_dir, "Y-code.exe")
    missing = [p for p in (cli_exe, gui_exe) if not os.path.exists(p)]
    if missing:
        print("❌ 核心程序包生成不完整，缺少以下文件：")
        for p in missing:
            print(f"   - {p}")
        sys.exit(1)
    print(f"✅ 核心程序包 (onedir) 生成成功: {ycode_dir}")
    print(f"   - GUI 窗口版  : {gui_exe}")
    print(f"   - CLI 控制台版: {cli_exe}")
    return ycode_dir


# ============================================================================
# 步骤 2：卸载程序
# ============================================================================

def step2_build_uninstaller(pyinstaller_bin, project_dir):
    print("\n[步骤 2/5] 正在编译卸载程序 Y-code-uninstall.exe...")
    cmd = [pyinstaller_bin, "--clean", "--noconfirm", "ycode_uninstall.spec"]
    run_checked(cmd, "卸载程序编译")

    uninstall_exe = os.path.join(project_dir, "dist", "Y-code-uninstall.exe")
    if not os.path.exists(uninstall_exe):
        print(f"❌ 未找到预期产物: {uninstall_exe}")
        sys.exit(1)
    print(f"✅ 卸载程序生成成功: {uninstall_exe}")
    return uninstall_exe


# ============================================================================
# 步骤 3 / 4：安装向导
# (ycode_setup.py 是同一份源码；靠内嵌的 setup_mode.txt marker 文件区分
#  'cli' / 'full' 两种运行模式，因此这里构建两次、每次内嵌不同的数据。)
# ============================================================================

def build_setup_variant(pyinstaller_bin, project_dir, mode, output_name, add_data_pairs):
    """通用安装向导构建：把 mode（'cli'/'full'）和版本号写入临时 marker 文件，随构建一并打包进 exe。

    这里没有用静态 .spec 文件表达 cli/full 两个变体，是因为它们依赖的内嵌数据
    ——尤其是 dist/ycode_cli_setup.exe 这种“上一步骤才产出”的文件——在两次
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


def step3_build_cli_setup(pyinstaller_bin, project_dir, payload_zip, uninstall_exe):
    print("\n[步骤 3/5] 正在编译 CLI 独立安装向导 ycode_cli_setup.exe...")
    # 同时内嵌卸载程序：这样即便用户脱离主安装包、单独运行这个 CLI 安装向导
    # （即"安装目录下还没有 Y-code.exe"的独立安装场景），也能正常复制出
    # 卸载程序并注册 Y-code-CLI 卸载入口，而不是只能在合并进完整版安装后
    # 才具备可卸载性。
    return build_setup_variant(
        pyinstaller_bin, project_dir, mode="cli", output_name="ycode_cli_setup",
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


def step4_build_full_setup(pyinstaller_bin, project_dir, payload_zip, uninstall_exe, cli_setup_exe):
    print("\n[步骤 4/5] 正在编译主安装向导 ycode_setup.exe...")
    return build_setup_variant(
        pyinstaller_bin, project_dir, mode="full", output_name="ycode_setup",
        add_data_pairs=[
            (payload_zip, "."),
            (uninstall_exe, "."),
            (cli_setup_exe, "cli"),
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
    """安装包最终分发文件名（不含扩展名）。

    主安装包用 Y-code-{version}（如 Y-code-1.2.2），CLI 独立安装包用
    Y-code-cli-{version}。
    """
    if base == "ycode_setup":
        return f"Y-code-{version}"
    if base == "ycode_cli_setup":
        return f"Y-code-cli-{version}"
    return f"{base}_v{version}"


def step4b_rename_setups(project_dir, version, cli_only):
    """把固定名构建产物重命名为带版本号的分发名，并清理上一版本的同名旧产物。

    构建期仍用固定名（ycode_setup / ycode_cli_setup）：主安装包内嵌的
    cli/ycode_cli_setup.exe 由安装引擎按固定名查找，不能带版本号；最终落到
    dist/ 供分发的文件才改成带版本号的名字。"""
    print("\n[步骤 4.5/5] 安装包重命名为带版本号的分发名...")
    bases = ["ycode_cli_setup"] if cli_only else ["ycode_cli_setup", "ycode_setup"]
    renamed = {}
    for base in bases:
        src = os.path.join(project_dir, "dist", f"{base}.exe")
        if not os.path.exists(src):
            print(f"❌ 未找到待重命名产物: {src}")
            sys.exit(1)
        # 清理历史版本留下的同名旧产物，避免 dist/ 里堆叠多个版本难以分辨
        for old in glob.glob(os.path.join(project_dir, "dist", f"{base}_v*.exe")):
            os.remove(old)
        if base == "ycode_setup":
            # 主安装包新命名 Y-code-{版本}.exe 也清理旧版本残留；
            # 用 [0-9] 前缀精确匹配，避免误删 Y-code-cli-*.exe（CLI 安装包）
            for old in glob.glob(os.path.join(project_dir, "dist", "Y-code-[0-9]*.exe")):
                os.remove(old)
        if base == "ycode_cli_setup":
            # CLI 安装包新命名 Y-code-cli-*.exe 也清理旧版本残留
            for old in glob.glob(os.path.join(project_dir, "dist", "Y-code-cli-*.exe")):
                os.remove(old)
        dst = os.path.join(project_dir, "dist", f"{versioned_setup_name(base, version)}.exe")
        os.replace(src, dst)
        renamed[base] = dst
        print(f"✔ {base}.exe -> {os.path.basename(dst)}")
    return renamed


# ============================================================================
# 步骤 4.7：Gitee Release 分卷附件（绕过单附件 100MB 上限）
# ============================================================================

# Gitee Release 单附件上限 100MB；留 5MB 余量切割
SPLIT_PART_BYTES = 95 * 1024 * 1024


def sha256_file(path):
    """分块计算文件 SHA256（安装包几百 MB，不能一次读入内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# 首 512 字节内连续可解码为 UTF-16 非控制字符的码元对数阈值：
# 达到即判定"看起来像 UTF-16 文本"，边界必须避开（v1.2.16 事故片的
# 危险头部实测可连续解码 36 对；正常二进制分卷实测 ≤2 对即遇裸代理项）
_UTF16_RUN_THRESHOLD = 16


def _looks_like_utf16_text(head: bytes) -> bool:
    """首 512 字节在 UTF-16LE 或 BE 下是否像文本（连续可解码的非控制码元
    达到阈值）。二进制数据通常几对之内就会撞上裸代理项（U+D800-DFFF）
    或控制字符，连续 16 对都能解码的极少见——那种数据会被 CDN 嗅探成
    text/plain; charset=utf-16le 并注入 BOM（v1.2.16 part4 事故）。
    """
    for little in (True, False):
        run = 0
        for i in range(0, min(len(head), 512) - 1, 2):
            unit = int.from_bytes(head[i:i + 2], "little" if little else "big")
            if 0xD800 <= unit <= 0xDFFF:
                break  # 裸代理项：非合法 UTF-16
            if unit < 0x20 and unit not in (0x09, 0x0A, 0x0D):
                break  # C0 控制字符
            if 0x7F <= unit <= 0x9F:
                break  # DEL / C1 控制字符
            run += 1
            if run >= _UTF16_RUN_THRESHOLD:
                return True
    return False


def _safe_part_boundary(f, off, file_size):
    """分卷边界微调：避免分卷起始字节被 CDN 内容嗅探误判为文本。

    v1.2.16 事故：part4 起始字节 FE 91 FF 9D... 前 72 字节可连续按
    UTF-16LE 解码成 CJK 字符，Gitee/foruda CDN 将其误判为 UTF-16 文本，
    注入 0xFF 拼成 FF FE BOM（体积 +1 字节）并以
    text/plain; charset=utf-16le 返回，老版本 core/updater.py 的
    Content-Type 防线把它当成风控页面拒绝下载（自动更新确定性失败）。

    边界整体向后挪到首个"非 BOM、前 512 字节含 0x00、且看起来不像
    UTF-16 文本"的位置，只改变切片点，不影响拼接还原结果。
    注意本规则只是降低命中概率的启发式，真正的兜底是发布侧的
    上传后回读校验（publish_release.verify_remote_attachment）。
    """
    while off < file_size:
        f.seek(off)
        head = f.read(512)
        if head[:3] == b"\xef\xbb\xbf" or head[:2] in (b"\xff\xfe", b"\xfe\xff"):
            off += 1
            continue
        if b"\x00" not in head:
            off += 1
            continue
        if _looks_like_utf16_text(head):
            off += 1
            continue
        return off
    return off


def split_file_for_release(src_path, out_dir, part_bytes=SPLIT_PART_BYTES):
    """把超限文件切成 .partN 二进制分卷并移入 out_dir。

    分卷是纯字节切割（无压缩/加密）：按序号顺序拼接即还原原文件。
    文件本身 ≤ 100MB 时直接原样拷贝（单片分卷没有拼接意义）。
    采用均衡切分：先定片数再均摊片长，避免固定片长切出极小的尾卷
    （曾出现 78KB 尾卷被 core/updater.py 的 >1MB 完整性防线误判为
    HTML 错误页而拒绝下载，见 v1.2.16 发布事故）。
    返回 (分卷文件名列表, 原文件 SHA256)。
    """
    size = os.path.getsize(src_path)
    base = os.path.basename(src_path)
    if size <= part_bytes:
        shutil.copy2(src_path, os.path.join(out_dir, base))
        return [base], sha256_file(src_path)

    # 片数按上限取整，再把总长均摊到各片：每片大小相近且都不超上限
    part_count = -(-size // part_bytes)  # ceil(size / part_bytes)
    balanced_part_bytes = -(-size // part_count)  # ceil(size / part_count)

    # 计算各片边界（首个边界为 0，末边界为文件末尾），并对中间边界做
    # 内容嗅探安全微调
    names = []
    with open(src_path, "rb") as f:
        boundaries = [0]
        for k in range(1, part_count):
            boundaries.append(
                _safe_part_boundary(f, balanced_part_bytes * k, size)
            )
        boundaries.append(size)

        for idx, (start, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
            part_name = f"{base}.part{idx}"
            with open(os.path.join(out_dir, part_name), "wb") as pf:
                f.seek(start)
                remaining = end - start
                while remaining > 0:
                    chunk = f.read(min(4 * 1024 * 1024, remaining))
                    if not chunk:
                        break
                    pf.write(chunk)
                    remaining -= len(chunk)
            names.append(part_name)
    return names, sha256_file(src_path)


def step4c_make_release_parts(project_dir, version, cli_only):
    """生成 Gitee Release 上传用的分卷附件目录 dist/release_parts/。

    主安装包（约 290MB）与 CLI 安装包（约 145MB）都超过 Gitee 单附件
    100MB 上限，统一切成 ≤95MB 分卷；自动更新端（core/updater.py）识别
    .partN 序列后顺序下载拼接，并用 RELEASE_NOTES.txt 中发布的 SHA256
    校验拼接结果，校验不过即拒绝安装。
    """
    print("\n[步骤 4.7/5] 生成 Gitee Release 分卷附件 (dist/release_parts/)...")
    out_dir = os.path.join(project_dir, "dist", "release_parts")
    # 整目录重建：清掉上一版本残留的分卷与指引，避免多版本文件混传
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    targets = [
        (os.path.join("dist", f"{versioned_setup_name('ycode_cli_setup', version)}.exe"),
         "CLI 独立安装包"),
    ]
    if not cli_only:
        targets.append(
            (os.path.join("dist", f"{versioned_setup_name('ycode_setup', version)}.exe"),
             "主安装包 (GUI + 可选 CLI)")
        )

    entries = []  # (描述, 文件名, 分卷列表, sha256)
    for rel_path, desc in targets:
        abs_path = os.path.join(project_dir, rel_path)
        if not os.path.exists(abs_path):
            print(f"⚠️ 跳过（产物缺失）: {rel_path}")
            continue
        names, digest = split_file_for_release(abs_path, out_dir)
        entries.append((desc, os.path.basename(abs_path), names, digest))
        parts_desc = f"{len(names)} 片" if len(names) > 1 else "无需分卷（≤100MB 直接传）"
        print(f"✔ {os.path.basename(abs_path)} -> {parts_desc}  SHA256: {digest[:16]}...")

    if not entries:
        print("⚠️ 没有可切分的安装包产物，跳过 RELEASE_NOTES 生成。")
        return

    # 生成上传指引 + 发布说明模板（SHA256 行供 updater 解析，勿改动格式）
    main_entry = next((e for e in entries if "主安装包" in e[0]), entries[0])
    lines = []
    lines.append(f"Y-CODE v{version} Gitee Release 附件上传指引")
    lines.append("=" * 52)
    lines.append("")
    lines.append("1. Gitee 仓库 -> 发布 -> 新建发布，Tag 填 v" + version)
    lines.append("2. 描述粘贴下方『发布说明模板』（SHA256 校验行格式勿改动，")
    lines.append("   自动更新依赖它做安装包完整性校验）")
    lines.append("3. 附件上传本目录 (dist/release_parts/) 下的全部分卷文件")
    lines.append("4. 勾选『设为最新发布版本』后发布")
    lines.append("")
    lines.append("发布说明模板")
    lines.append("-" * 52)
    lines.append("## 更新内容")
    lines.append("（从 assets/CHANGELOG.md 复制本版本条目）")
    lines.append("")
    lines.append(f"SHA256: {main_entry[3]}")
    lines.append("")
    lines.append("手动合并安装（未走应用内自动更新的用户）")
    lines.append("-" * 52)
    for _desc, fname, names, _digest in entries:
        if len(names) > 1:
            joined = "+".join(names)
            lines.append(f"下载全部分卷后在同一目录执行：")
            lines.append(f"  copy /b {joined} {fname}")
    lines.append("")
    lines.append("产物 SHA256")
    lines.append("-" * 52)
    for desc, fname, names, digest in entries:
        parts_desc = f"（{len(names)} 分卷）" if len(names) > 1 else ""
        lines.append(f"{fname:<40} {parts_desc} {digest}")

    notes_path = os.path.join(out_dir, "RELEASE_NOTES.txt")
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"✔ 上传指引已生成: {notes_path}")


# ============================================================================
# 步骤 5：汇总校验
# ============================================================================

def step5_summary(project_dir, cli_only, version):
    print("\n[步骤 5/5] 校验构建产物...")
    rows = [
        (os.path.join("dist", "ycode", "Y-code.exe"), "GUI 主程序 (窗口版)"),
        (os.path.join("dist", "ycode", "ycode.exe"), "CLI 主程序 (控制台版)"),
        (os.path.join("dist", "Y-code-uninstall.exe"), "卸载程序"),
        (os.path.join("dist", f"{versioned_setup_name('ycode_cli_setup', version)}.exe"), "CLI 独立安装包"),
    ]
    if not cli_only:
        rows.append((os.path.join("dist", f"{versioned_setup_name('ycode_setup', version)}.exe"),
                     "主安装包 (GUI + 可选 CLI)"))

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
    if cli_only:
        print("ℹ 已指定 --cli-only，主安装包未构建（如需要请去掉该参数重新运行）。")
    else:
        print(f"👉 您的 Windows 双击主安装包位于: "
              f"{os.path.join(project_dir, 'dist', versioned_setup_name('ycode_setup', version) + '.exe')}")
    print(f"👉 Gitee Release 附件（分卷 + 上传指引）位于: "
          f"{os.path.join(project_dir, 'dist', 'release_parts')}")
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
        uninstall_exe = os.path.join(project_dir, "dist", "Y-code-uninstall.exe")
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

    cli_setup_exe = step3_build_cli_setup(pyinstaller_bin, project_dir, payload_zip, uninstall_exe)

    if not args.cli_only:
        step4_build_full_setup(pyinstaller_bin, project_dir, payload_zip, uninstall_exe, cli_setup_exe)

    version = read_project_version(project_dir)
    step4b_rename_setups(project_dir, version, args.cli_only)
    if not args.no_split_parts:
        step4c_make_release_parts(project_dir, version, args.cli_only)
    step5_summary(project_dir, args.cli_only, version)


if __name__ == "__main__":
    build()
