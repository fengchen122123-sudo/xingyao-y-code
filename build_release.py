import argparse
import datetime
import glob
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

sys.stdout.reconfigure(encoding='utf-8')


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
        help="强制重新执行 npm run build（主前端），即使 dist/index.html 已经存在。",
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
        "--component-only", action="store_true",
        help="只产出按需组件包（playwright + ffmpeg，即 dist/playwright-component-*.zip "
             "与 dist/ffmpeg-component-*.zip），跳过其它所有构建"
             "（前端 / 核心载荷 / 安装向导 / 分卷）。",
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


def _strip_python_comment(line):
    in_str = None
    for i, ch in enumerate(line):
        if in_str:
            if ch == in_str:
                in_str = None
        elif ch in "'\"":
            in_str = ch
        elif ch == "#":
            return line[:i]
    return line


def collect_all_targets_from_spec(spec_path):
    """从 PyInstaller spec 提取全部 collect_all('包名') 目标（去重保序）。

    以 spec 文件为唯一事实源解析，而不是在本脚本里另抄一份包名清单——
    以后往 spec 加 collect_all 时校验自动跟上，不会出现两处清单漂移。
    解析前剥离注释（见 _strip_python_comment），防止注释里的 collect_all
    字样解析出假目标。
    """
    pattern = re.compile(r"collect_all\(\s*['\"]([A-Za-z0-9_.]+)['\"]\s*\)")
    targets = []
    with open(spec_path, "r", encoding="utf-8") as f:
        for line in f:
            code = _strip_python_comment(line)
            for match in pattern.finditer(code):
                if match.group(1) not in targets:
                    targets.append(match.group(1))
    return targets


def find_missing_collect_all_targets(targets):
    missing = []
    for name in targets:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            missing.append(name)
    return missing


def verify_spec_collect_all_targets(project_dir):
    return find_missing_collect_all_targets(
        collect_all_targets_from_spec(os.path.join(project_dir, "ycode.spec"))
    )


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


def read_playwright_version(project_dir):
    version_file = os.path.join(
        project_dir, "venv", "Lib", "site-packages", "playwright", "_repo_version.py"
    )
    try:
        with open(version_file, "r", encoding="utf-8") as f:
            content = f.read()
        match = re.search(
            r"__version__\s*=\s*(?:version\s*=\s*)?[\"']([^\"']+)[\"']", content
        )
        if match:
            return match.group(1)
    except OSError:
        pass
    print("⚠️ 未能解析 playwright 版本号，组件包文件名将回退为 0.0.0。")
    return "0.0.0"


PLAYWRIGHT_COMPONENT_SOURCES = ("playwright", "greenlet", "pyee")


def step0b_build_playwright_component(project_dir):
    print("\n[步骤 0b/5] 正在产出 playwright 组件包 (dist/playwright-component-*.zip)...")
    site_packages = os.path.join(project_dir, "venv", "Lib", "site-packages")
    missing = [
        name for name in PLAYWRIGHT_COMPONENT_SOURCES
        if not os.path.isdir(os.path.join(site_packages, name))
    ]
    if missing:
        print("❌ 组件源目录缺失: " + ", ".join(missing))
        sys.exit(1)

    version = read_playwright_version(project_dir)
    dist_dir = os.path.join(project_dir, "dist")
    os.makedirs(dist_dir, exist_ok=True)
    zip_path = os.path.join(dist_dir, f"playwright-component-{version}.zip")
    if os.path.exists(zip_path):
        print(f"✅ 已存在同版本组件包，跳过压缩: {zip_path} ({format_size_mb(zip_path)})")
        return zip_path

    tmp_dir = os.path.join(project_dir, "build", "_playwright_component_tmp")
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "tests")
        for name in PLAYWRIGHT_COMPONENT_SOURCES:
            shutil.copytree(
                os.path.join(site_packages, name),
                os.path.join(tmp_dir, name),
                ignore=ignore,
            )

        manifest = {
            "playwright_version": version,
            "python_version": "3.14",
            "abi": "cp314",
            "platform": "win-amd64",
            "built_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with open(os.path.join(tmp_dir, "COMPONENT.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        for old in glob.glob(os.path.join(dist_dir, "playwright-component-*.zip")):
            os.remove(old)

        print("📦 正在压缩 playwright 组件包 (LZMA)...")
        count = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_LZMA, allowZip64=True) as zf:
            for root, _dirs, files in os.walk(tmp_dir):
                for name in files:
                    src = os.path.join(root, name)
                    arcname = os.path.relpath(src, tmp_dir).replace(os.sep, "/")
                    zf.write(src, arcname)
                    count += 1

        digest = sha256_file(zip_path)
        print(f"✔ 组件包产出完成: {count} 个文件 -> {format_size_mb(zip_path)}")
        print(f"   - 文件  : {zip_path}")
        print(f"   - SHA256: {digest}")
        return zip_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


FFMPEG_COMPONENT_FILES = ("ffmpeg.exe", "NOTICE.txt")


def read_ffmpeg_version(project_dir):
    notice_path = os.path.join(project_dir, "vendor", "ffmpeg", "NOTICE.txt")
    try:
        with open(notice_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        print("⚠️ 未能读取 vendor/ffmpeg/NOTICE.txt，版本号回退为 bundled。")
        return "bundled"
    match = re.search(r"ffmpeg\s+version\s+(\d+(?:\.\d+)*)", content, re.IGNORECASE)
    if match:
        return match.group(1)
    print("⚠️ 未能从 NOTICE.txt 解析 ffmpeg 版本号，回退为 bundled。")
    return "bundled"


def step0d_build_ffmpeg_component(project_dir):
    print("\n[步骤 0d/5] 正在产出 ffmpeg 组件包 (dist/ffmpeg-component-*.zip)...")
    ffmpeg_dir = os.path.join(project_dir, "vendor", "ffmpeg")
    sources = [os.path.join(ffmpeg_dir, name) for name in FFMPEG_COMPONENT_FILES]
    missing = [path for path in sources if not os.path.isfile(path)]
    if missing:
        print("⚠️ ffmpeg 组件源文件缺失，本次不产出组件包: "
              + ", ".join(os.path.relpath(path, project_dir) for path in missing))
        print("   影响：设置页「下载视频转换组件」会报『未找到组件下载源』，"
              "背景图转换回退系统 PATH 的 ffmpeg。")
        return None

    version = read_ffmpeg_version(project_dir)
    dist_dir = os.path.join(project_dir, "dist")
    os.makedirs(dist_dir, exist_ok=True)
    zip_path = os.path.join(dist_dir, f"ffmpeg-component-{version}.zip")
    if os.path.exists(zip_path):
        print(f"✅ 已存在同版本组件包，跳过压缩: {zip_path} ({format_size_mb(zip_path)})")
        return zip_path

    manifest = {
        "ffmpeg_version": version,
        "platform": "win-amd64",
        "built_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    for old in glob.glob(os.path.join(dist_dir, "ffmpeg-component-*.zip")):
        os.remove(old)

    print("📦 正在压缩 ffmpeg 组件包 (LZMA，97MB 需要几分钟)...")
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_LZMA, allowZip64=True) as zf:
        for src in sources:
            zf.write(src, os.path.basename(src))
            count += 1
        zf.writestr("COMPONENT.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        count += 1

    digest = sha256_file(zip_path)
    print(f"✔ 组件包产出完成: {count} 个文件 -> {format_size_mb(zip_path)}")
    print(f"   - 文件  : {zip_path}")
    print(f"   - SHA256: {digest}")
    return zip_path


SEARCH_COMPONENT_SOURCES = ("ddgs", "primp", "lxml", "fake_useragent")


def read_search_version(project_dir):
    init_file = os.path.join(
        project_dir, "venv", "Lib", "site-packages", "ddgs", "__init__.py"
    )
    try:
        with open(init_file, "r", encoding="utf-8") as f:
            content = f.read()
        match = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", content)
        if match:
            return match.group(1)
    except OSError:
        pass
    print("⚠️ 未能解析 ddgs 版本号，组件包文件名将回退为 bundled。")
    return "bundled"


def step0e_build_search_component(project_dir):
    print("\n[步骤 0e/5] 正在产出网页搜索组件包 (dist/search-component-*.zip)...")
    site_packages = os.path.join(project_dir, "venv", "Lib", "site-packages")
    missing = [
        name for name in SEARCH_COMPONENT_SOURCES
        if not os.path.isdir(os.path.join(site_packages, name))
    ]
    if missing:
        print("⚠️ 网页搜索组件源目录缺失，本次不产出组件包: " + ", ".join(missing))
        print("   影响：设置页「网页搜索组件」点下载会报『未找到组件下载源』，"
              "web_search 工具在运行时不可用。")
        print("   补齐方式：venv\\Scripts\\python -m pip install -r requirements.txt")
        return None

    version = read_search_version(project_dir)
    dist_dir = os.path.join(project_dir, "dist")
    os.makedirs(dist_dir, exist_ok=True)
    zip_path = os.path.join(dist_dir, f"search-component-{version}.zip")
    if os.path.exists(zip_path):
        print(f"✅ 已存在同版本组件包，跳过压缩: {zip_path} ({format_size_mb(zip_path)})")
        return zip_path

    tmp_dir = os.path.join(project_dir, "build", "_search_component_tmp")
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "tests")
        for name in SEARCH_COMPONENT_SOURCES:
            shutil.copytree(
                os.path.join(site_packages, name),
                os.path.join(tmp_dir, name),
                ignore=ignore,
            )

        manifest = {
            "search_version": version,
            "platform": "win-amd64",
            "built_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with open(os.path.join(tmp_dir, "COMPONENT.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        # 清掉其它版本的旧包，避免 dist/ 里堆着多个 search-component-*.zip
        # （find_component_archive 取最新一个，混着容易传错版本）
        for old in glob.glob(os.path.join(dist_dir, "search-component-*.zip")):
            os.remove(old)

        print("📦 正在压缩网页搜索组件包 (LZMA)...")
        count = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_LZMA, allowZip64=True) as zf:
            for root, _dirs, files in os.walk(tmp_dir):
                for name in files:
                    src = os.path.join(root, name)
                    arcname = os.path.relpath(src, tmp_dir).replace(os.sep, "/")
                    zf.write(src, arcname)
                    count += 1

        digest = sha256_file(zip_path)
        print(f"✔ 组件包产出完成: {count} 个文件 -> {format_size_mb(zip_path)}")
        print(f"   - 文件  : {zip_path}")
        print(f"   - SHA256: {digest}")
        return zip_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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

    dist_assets = os.path.join(project_dir, "frontend", "dist", "assets")
    MAX_ENTRY_BYTES = 1_800_000
    if os.path.isdir(dist_assets):
        entry_files = [
            f for f in os.listdir(dist_assets)
            if f.startswith("index-") and f.endswith(".js")
        ]
        for name in entry_files:
            size = os.path.getsize(os.path.join(dist_assets, name))
            if size > MAX_ENTRY_BYTES:
                raise SystemExit(
                    f"前端首屏包过大：{name} {size / 1024 / 1024:.2f} MB，"
                    f"上限 {MAX_ENTRY_BYTES / 1024 / 1024:.2f} MB。"
                    "请检查是否有语言包被静态引入。"
                )

    if not os.path.exists(index_html):
        print("❌ npm run build 已执行完成，但未生成 frontend/dist/index.html，请检查 vite 构建配置。")
        sys.exit(1)

    print(f"✅ 前端构建成功: {index_html}")

    print("\n[步骤 0.5/5] 构建 / 复用移动端子应用产物 (frontend-mobile/dist)...")
    mobile_dir = os.path.join(project_dir, "frontend-mobile")
    mobile_dist = os.path.join(mobile_dir, "dist")
    mobile_index = os.path.join(mobile_dist, "index.html")

    if args.skip_frontend:
        print("ℹ 已指定 --skip-frontend，同步跳过移动端构建。")
        os.makedirs(mobile_dist, exist_ok=True)
        if not os.path.exists(mobile_index):
            print("⚠️ frontend-mobile/dist/index.html 不存在：打包后的 /m/ 新版移动端将 404（经典界面不受影响）。")
        return

    if os.path.exists(mobile_index) and not args.rebuild_frontend:
        print(f"✅ 已检测到移动端产物: {mobile_index}（如需强制重新构建，请加上 --rebuild-frontend）。")
        return

    if not os.path.isdir(mobile_dir):
        print(f"❌ 未找到移动端项目目录: {mobile_dir}")
        sys.exit(1)

    npm_bin = shutil.which("npm")
    if npm_bin is None:
        print("❌ PATH 环境变量中未找到 npm，无法构建移动端。")
        sys.exit(1)
    if not os.path.isdir(os.path.join(mobile_dir, "node_modules")):
        print("🚀 移动端依赖缺失，正在执行 npm install ...")
        install = subprocess.run([npm_bin, "install", "--no-audit", "--no-fund"], cwd=mobile_dir)
        if install.returncode != 0:
            print("❌ 移动端 npm install 失败。")
            sys.exit(1)

    print("🚀 正在执行移动端 npm run build ...")
    result = subprocess.run([npm_bin, "run", "build"], cwd=mobile_dir)
    if result.returncode != 0:
        print(f"❌ 移动端构建失败（npm run build 退出码 {result.returncode}）。")
        sys.exit(1)
    if not os.path.exists(mobile_index):
        print("❌ 移动端构建完成但未生成 frontend-mobile/dist/index.html，请检查 vite 配置。")
        sys.exit(1)
    print(f"✅ 移动端构建成功: {mobile_index}")


def find_csc():
    """定位 .NET Framework 自带的 csc.exe（Windows 10/11 默认存在，无需装 VS）。

    优先 v4.x（launcher 只用到 kernel32 P/Invoke 与最基础的 BCL，v3.5 也能编，
    但 v4 运行时在所有受支持 Windows 上都预装）。找不到返回空串，由调用方回退。
    """
    windir = os.environ.get("WINDIR") or r"C:\Windows"
    found = []
    for flavor in ("Framework64", "Framework"):
        root = os.path.join(windir, "Microsoft.NET", flavor)
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root), reverse=True):
            exe = os.path.join(root, name, "csc.exe")
            if os.path.isfile(exe):
                found.append(exe)
    for exe in found:
        if os.sep + "v4." in exe:
            return exe
    return found[0] if found else ""


def build_cli_launcher(project_dir, ycode_dir):
    print("\n[步骤 1.5/5] 正在编译 CLI 控制台启动器 ycode.exe...")
    source = os.path.join(project_dir, "packaging", "ycode_launcher.cs")
    target = os.path.join(ycode_dir, "ycode.exe")

    def _fallback(reason):
        gui_exe = os.path.join(ycode_dir, "Y-code.exe")
        shutil.copy2(gui_exe, target)
        print(f"⚠️ {reason}")
        print("⚠️ 已回退为“复制 Y-code.exe 作为 ycode.exe”：CLI 将以 GUI 模式启动，")
        print("   控制台 CLI 模式不可用（安装包体积也会多出约 31MB）。")

    if not os.path.isfile(source):
        _fallback(f"未找到启动器源码: {source}")
        return target

    csc = find_csc()
    if not csc:
        _fallback("未找到 csc.exe（.NET Framework 编译器）")
        return target

    # 先编到临时名，成功后再原子替换：编译失败时不会把可用的旧产物弄坏
    tmp_target = target + ".building"
    cmd = [
        csc, "/nologo", "/target:exe", "/platform:anycpu", "/optimize+",
        f"/out:{tmp_target}", source,
    ]
    try:
        result = subprocess.run(cmd)
    except OSError as exc:
        _fallback(f"启动 csc.exe 失败: {exc}")
        return target
    if result.returncode != 0 or not os.path.isfile(tmp_target):
        if os.path.exists(tmp_target):
            os.remove(tmp_target)
        _fallback(f"csc 编译失败（退出码 {result.returncode}）")
        return target

    os.replace(tmp_target, target)
    size_kb = os.path.getsize(target) / 1024
    print(f"✅ CLI 启动器编译成功: {target} ({size_kb:.0f} KB)")
    if size_kb > 1024:
        print("⚠️ 启动器体积异常（> 1MB），请检查是否误用了回退产物。")
    return target


def step1_build_core(pyinstaller_bin, project_dir):
    print("\n[步骤 1/5] 正在编译核心程序包 (Y-code.exe GUI，CLI 共用同一份载荷)...")
    cmd = [pyinstaller_bin, "--clean", "--noconfirm", "ycode.spec"]
    run_checked(cmd, "核心程序包编译")

    ycode_dir = os.path.join(project_dir, "dist", "ycode")
    gui_exe = os.path.join(ycode_dir, "Y-code.exe")
    if not os.path.exists(gui_exe):
        print("❌ 核心程序包生成不完整，缺少 GUI 主程序：")
        print(f"   - {gui_exe}")
        sys.exit(1)
    print(f"✅ 核心程序包 (onedir) 生成成功: {ycode_dir}")
    print(f"   - GUI 窗口版  : {gui_exe} ({os.path.getsize(gui_exe) / 1024 / 1024:.1f} MB)")

    cli_exe = build_cli_launcher(project_dir, ycode_dir)
    print(f"   - CLI 控制台版: {cli_exe}")
    return ycode_dir


def step2_build_uninstaller(pyinstaller_bin, project_dir):
    """确认卸载向导已由 ycode.spec 产出（与主程序共享 _internal 的 onedir 入口）。

    历史上卸载器是单独的 PyInstaller onefile（13.4MB），需要这一步单独编译、
    再作为 add-data 内嵌进两个安装器。现在它由 spec 的第三套 Analysis 产出到
    dist/ycode/ 下、随载荷一起释放，安装包因此省掉整整一份重复的运行时。
    """
    print("\n[步骤 2/5] 正在检查卸载向导 Y-code-uninstall.exe...")
    uninstall_exe = os.path.join(project_dir, "dist", "ycode", "Y-code-uninstall.exe")
    if not os.path.exists(uninstall_exe):
        print(f"❌ 未找到预期产物: {uninstall_exe}（应由 ycode.spec 的 a_uninst 产出）")
        sys.exit(1)
    size_kb = os.path.getsize(uninstall_exe) / 1024
    print(f"✅ 卸载向导就绪: {uninstall_exe} ({size_kb:.0f} KB)")
    if size_kb > 5120:
        print("⚠️ 卸载向导体积异常（> 5MB），请检查是否仍在用旧的独立 onefile。")
    return uninstall_exe


def build_setup_variant(pyinstaller_bin, project_dir, mode, output_name, add_data_pairs):
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
            pyinstaller_bin, "--clean", "--noconfirm", "--onefile", "--windowed", "--noupx",
            "--exclude-module", "numpy",
            "--exclude-module", "PIL",
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
    zip_path = os.path.join(project_dir, "build", "ycode_pkg.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    print("📦 正在压缩核心程序包为单文件载荷 (ycode_pkg.zip, LZMA)...")
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_LZMA, allowZip64=True) as zf:
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
    return build_setup_variant(
        pyinstaller_bin, project_dir, mode="cli", output_name="ycode_cli_setup",
        add_data_pairs=[
            (payload_zip, "."),
            ("config.example.json", "."),
            ("system_prompt.md", "."),
            (os.path.join("assets", "ycode-logo-168.png"), "assets"),
            (os.path.join("assets", "ycode.ico"), "assets"),
        ],
    )


def step4_build_full_setup(pyinstaller_bin, project_dir, payload_zip, uninstall_exe):
    print("\n[步骤 4/5] 正在编译主安装向导 ycode_setup.exe...")
    return build_setup_variant(
        pyinstaller_bin, project_dir, mode="full", output_name="ycode_setup",
        add_data_pairs=[
            (payload_zip, "."),
            ("config.example.json", "."),
            ("system_prompt.md", "."),
            (os.path.join("assets", "ycode-logo-168.png"), "assets"),
            (os.path.join("assets", "ycode.ico"), "assets"),
        ],
    )


def versioned_setup_name(base, version):
    if base == "ycode_setup":
        return f"XingYaoCode-{version}"
    if base == "ycode_cli_setup":
        return f"Y-code-cli-{version}"
    return f"{base}_v{version}"


def step4b_rename_setups(project_dir, version, cli_only):
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
            for old in glob.glob(os.path.join(project_dir, "dist", "Y-code-[0-9]*.exe")):
                os.remove(old)
            for old in glob.glob(os.path.join(project_dir, "dist", "XingYaoCode-[0-9]*.exe")):
                os.remove(old)
        if base == "ycode_cli_setup":
            for old in glob.glob(os.path.join(project_dir, "dist", "Y-code-cli-*.exe")):
                os.remove(old)
        dst = os.path.join(project_dir, "dist", f"{versioned_setup_name(base, version)}.exe")
        os.replace(src, dst)
        renamed[base] = dst
        print(f"✔ {base}.exe -> {os.path.basename(dst)}")
    return renamed


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
                break
            if unit < 0x20 and unit not in (0x09, 0x0A, 0x0D):
                break
            if 0x7F <= unit <= 0x9F:
                break
            run += 1
            if run >= _UTF16_RUN_THRESHOLD:
                return True
    return False


def _safe_part_boundary(f, off, file_size):
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
    size = os.path.getsize(src_path)
    base = os.path.basename(src_path)
    if size <= part_bytes:
        shutil.copy2(src_path, os.path.join(out_dir, base))
        return [base], sha256_file(src_path)

    part_count = -(-size // part_bytes)
    balanced_part_bytes = -(-size // part_count)

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


# 发布说明里组件包校验行的固定格式：``<文件名>  SHA256=<64 位十六进制>``。
# 刻意用 "=" 而不是 ":"：core.updater.parse_release_sha256 只认 "SHA256: <hex>"，
# 用冒号会让主安装包的官方校验和解析错取组件包 digest；客户端解析见
# core/components.py::_parse_component_digest（两处格式必须同步修改）。
COMPONENT_DIGEST_LINE = "{name}  SHA256={digest}"


def component_digest_line(archive_name, digest):
    return COMPONENT_DIGEST_LINE.format(name=archive_name, digest=digest)


RELEASE_COMPONENTS = (
    ("playwright-component", "浏览器操控组件包", 25),
    ("ffmpeg-component", "视频转换组件包", 97),
    ("search-component", "网页搜索组件包", 18),
)


def find_component_archive(project_dir, prefix):
    """返回 dist/ 下某个组件包路径（多个时取最新；没有返回空串）。

    prefix 形如 "playwright-component" / "ffmpeg-component"，与 core/components.py
    里各 ComponentSpec 的 asset_hint 保持一致（客户端按 asset_hint 在 Release
    资产里找下载源，两边必须同名，否则用户点下载直接报"未找到组件下载源"）。
    """
    candidates = sorted(
        glob.glob(os.path.join(project_dir, "dist", f"{prefix}-*.zip"))
    )
    return candidates[-1] if candidates else ""


def find_playwright_component_archive(project_dir):
    """playwright 组件包的便捷封装（保留旧名字，调用方无需关心泛化细节）。"""
    return find_component_archive(project_dir, "playwright-component")


def step4c_make_release_parts(project_dir, version, cli_only):
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

    entries = []
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

    # 组件包 SHA256 必须写进发布说明：客户端下载组件前会解析对应那一行做强制校验
    # （core/components.py::_parse_component_digest，按资产名精确匹配），缺失时
    # 客户端只能告警放行。每个组件各占一行，互不影响——多个组件行共存时客户端
    # 靠资产名区分，不会串号（见 tests/test_components.py 的 digest 用例）。
    component_entries = []
    for prefix, title, _size_mb in RELEASE_COMPONENTS:
        archive = find_component_archive(project_dir, prefix)
        if not archive:
            print(f"⚠️ 未找到 dist/{prefix}-*.zip：发布说明里不会有「{title}」"
                  "校验行，客户端下载该组件时无法校验完整性。")
            continue
        component_digest = sha256_file(archive)
        component_entries.append(
            (prefix, title, os.path.basename(archive), component_digest)
        )
        print(f"✔ 组件包 {os.path.basename(archive)}  SHA256: {component_digest}")

    main_entry = next((e for e in entries if "主安装包" in e[0]), entries[0])
    lines = []
    lines.append(f"Y-CODE v{version} Gitee Release 附件上传指引")
    lines.append("=" * 52)
    lines.append("")
    lines.append("1. Gitee 仓库 -> 发布 -> 新建发布，Tag 填 v" + version)
    lines.append("2. 描述粘贴下方『发布说明模板』（SHA256 校验行格式勿改动，")
    lines.append("   自动更新依赖它做安装包完整性校验）")
    lines.append("3. 附件上传本目录 (dist/release_parts/) 下的全部分卷文件")
    # 组件包必须与主安装包同 Release 发布：core/components.py 只在该 Release 的
    # 资产里按「名字含 spec.asset_hint 且以 .zip 结尾」查找下载源（见
    # _resolve_component_source_impl），漏传时用户在设置页点「下载组件」会直接报
    # "未找到组件下载源"。逐个组件单独列一行，漏传哪项一眼可见。
    for prefix, title, size_mb in RELEASE_COMPONENTS:
        lines.append(f"   + dist/{prefix}-*.zip（{title}，约 {size_mb}MB；"
                     "漏传则用户点下载会报「未找到组件下载源」）")
    lines.append("   （上述附件必须分别出现在下方对应的『…组件包 SHA256』行里："
                 "客户端靠它校验组件包完整性，格式勿改动）")
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

    for prefix, title, _size_mb in RELEASE_COMPONENTS:
        found = next((e for e in component_entries if e[0] == prefix), None)
        lines.append("")
        lines.append(f"{title} SHA256（core/components.py 下载校验，格式勿改动）")
        lines.append("-" * 52)
        lines.append(
            component_digest_line(found[2], found[3]) if found
            else f"（本次未产出 dist/{prefix}-*.zip：客户端下载该组件时无法校验，"
                 "请补传组件包后补充此行）"
        )

    notes_path = os.path.join(out_dir, "RELEASE_NOTES.txt")
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"✔ 上传指引已生成: {notes_path}")


def step5_summary(project_dir, cli_only, version):
    print("\n[步骤 5/5] 校验构建产物...")
    rows = [
        (os.path.join("dist", "ycode", "Y-code.exe"), "GUI 主程序 (窗口版)"),
        (os.path.join("dist", "ycode", "ycode.exe"), "CLI 主程序 (控制台版)"),
        (os.path.join("dist", "ycode", "Y-code-uninstall.exe"), "卸载向导"),
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
    for prefix, title, _size_mb in RELEASE_COMPONENTS:
        found_zips = sorted(
            glob.glob(os.path.join(project_dir, "dist", f"{prefix}-*.zip"))
        )
        if not found_zips:
            print(f"  ⚠️ {f'dist/{prefix}-*.zip':<34} {'未产出':>9}   {title} (Gitee Release)")
            continue
        for zip_path in found_zips:
            rel = os.path.relpath(zip_path, project_dir)
            print(f"  ✅ {rel:<34} {format_size_mb(zip_path):>9}   {title} (Gitee Release)")
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


def build():
    args = parse_args()

    project_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_dir)

    if args.component_only:
        # 快速通道：只打组件包，不需要 PyInstaller / 前端 / 安装向导环境
        step0b_build_playwright_component(project_dir)
        step0d_build_ffmpeg_component(project_dir)
        step0e_build_search_component(project_dir)
        print("ℹ 已指定 --component-only，跳过其它所有构建步骤。")
        return

    pyinstaller_bin = resolve_pyinstaller(project_dir)

    if args.setup_only:
        ycode_dir = os.path.join(project_dir, "dist", "ycode")
        uninstall_exe = os.path.join(project_dir, "dist", "ycode", "Y-code-uninstall.exe")
        if not os.path.isdir(ycode_dir):
            print(f"❌ --setup-only 需要现有核心载荷目录: {ycode_dir}（请先完整构建一次）。")
            sys.exit(1)
        if not os.path.exists(uninstall_exe):
            print(f"❌ --setup-only 需要现有卸载程序: {uninstall_exe}（请先完整构建一次）。")
            sys.exit(1)
        print("ℹ 已指定 --setup-only，跳过前端 / 核心载荷 / 卸载程序构建，直接复用现有产物。")
        payload_zip = make_payload_archive(project_dir, ycode_dir)
    else:
        missing_collect_all = verify_spec_collect_all_targets(project_dir)
        if missing_collect_all:
            print("❌ 构建环境缺少 ycode.spec collect_all 依赖的包: "
                  + ", ".join(missing_collect_all))
            print("   collect_all 对构建环境缺失的包会静默返回空而不报错，继续构建")
            print("   会产出丢失该依赖的安装包（典型症状：打包后 GUI 内嵌终端启动")
            print("   即报 No module named 'winpty'）。")
            print("   请先补齐依赖：venv\\Scripts\\python -m pip install -r requirements.txt")
            sys.exit(1)
        # 供应链把关（审查 B8-03）：vendor/ 随包资源（内置 ripgrep / mermaid）
        # 是安装包的组成输入，其来源与哈希必须与 vendor/VENDOR_MANIFEST.json
        # 记录一致；校验失败即中止构建，防止被篡改或漂移的资源打进产物。
        run_checked(
            [sys.executable, os.path.join(project_dir, "scripts", "verify_vendor.py")],
            "vendor 随包资源校验",
        )
        step0_build_frontend(project_dir, args)
        # playwright / ffmpeg / 网页搜索与安装包分离发布：组件包必须与同版本主安装包
        # 一起上传 Gitee Release（ffmpeg 缺失时 step0d 只告警，不阻断构建）
        step0b_build_playwright_component(project_dir)
        step0d_build_ffmpeg_component(project_dir)
        step0e_build_search_component(project_dir)
        ycode_dir = step1_build_core(pyinstaller_bin, project_dir)
        payload_zip = make_payload_archive(project_dir, ycode_dir)
        uninstall_exe = step2_build_uninstaller(pyinstaller_bin, project_dir)

    step3_build_cli_setup(pyinstaller_bin, project_dir, payload_zip, uninstall_exe)

    if not args.cli_only:
        step4_build_full_setup(pyinstaller_bin, project_dir, payload_zip, uninstall_exe)

    version = read_project_version(project_dir)
    step4b_rename_setups(project_dir, version, args.cli_only)
    if not args.no_split_parts:
        step4c_make_release_parts(project_dir, version, args.cli_only)
    step5_summary(project_dir, args.cli_only, version)


if __name__ == "__main__":
    build()
