"""
ycode_setup.py
Y-code 官方 Windows 安装向导（可视化 GUI 版，单一源码，靠内嵌的 setup_mode.txt 区分两种模式）：

  full 模式（默认）：完整版安装包。默认安装图形界面版 (Y-code.exe)，
                     安装选项页可勾选是否附带安装命令行版 (ycode.exe / ycode 命令)、
                     是否创建桌面快捷方式。
                     若用户当时选择不装命令行版，会把内嵌的 CLI 独立安装向导
                     释放到安装目录的 cli\\ 子目录下，方便以后随时补装。

  cli 模式：CLI 独立安装包。只安装命令行版，用于用户后续补装，或单独分发
            给只想用命令行的用户。

界面：tkinter（标准库，零新增依赖）+ 内嵌虚化背景图 assets/setup_bg.png。
安装引擎与界面解耦：run_install() 在后台线程执行，经队列回调刷新 UI，
可直接被 pytest 无界面测试。

模式来源：build_release.py 用 --onefile 分别打包本脚本两次，每次都通过
--add-data 内嵌一个不同内容的 setup_mode.txt（'full' 或 'cli'）。如果直接用
项目根目录下的 ycode_setup.spec 手动构建（不经过 build_release.py），因为
没有内嵌 setup_mode.txt，会自动按 'full' 模式运行。
"""
import os
import queue
import re
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
            if mode in ("full", "cli"):
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

def default_install_dir(scope="user"):
    """按安装范围返回默认安装目录：
    user    → %LOCALAPPDATA%\\Programs\\Y-code（无需管理员权限）
    machine → %ProgramFiles%\\Y-code（所有用户共享，需要管理员权限）
    """
    if scope == "machine":
        program_files = os.environ.get("ProgramFiles") or r"C:\Program Files"
        return os.path.join(program_files, "Y-code")
    user_home = os.path.expanduser("~")
    local_appdata = os.environ.get("LOCALAPPDATA") or os.path.join(user_home, "AppData", "Local")
    return os.path.join(local_appdata, "Programs", "Y-code")


def normalize_browsed_install_dir(chosen):
    """浏览选择的目录统一在其下追加 Y-code 子目录（所选目录本身已叫
    Y-code 时不重复追加）。

    避免用户选择盘符根目录（如 D:\\）或普通目录时，把程序文件直接
    平铺进所选目录。仅作用于向导「浏览...」选择的结果；路径输入框
    手动输入的路径不受影响（仍由 _validate_install_target 兜底校验）。
    覆盖安装（更新）时路径锁定为原安装位置，不走本函数。
    """
    chosen = os.path.normpath(str(chosen))
    if os.path.basename(chosen).lower() == "y-code":
        return chosen
    # 裸盘符（如 "D:"，normpath 不会补斜杠）拼 join 会得到相对当前目录的
    # "D:Y-code"，必须先补根斜杠
    if len(chosen) == 2 and chosen[1] == ":":
        chosen += os.sep
    return os.path.join(chosen, "Y-code")


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


def get_common_desktop_dir():
    """获取公共桌面路径（所有用户共享），machine 范围安装时放置快捷方式。

    优先读注册表 Shell Folders 的 Common Desktop 值，失败回退默认路径。
    """
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Common Desktop")
            return os.path.normpath(os.path.expandvars(value))
    except OSError:
        return r"C:\Users\Public\Desktop"


def create_shortcut(shortcut_path, target_path, working_dir, startupinfo):
    """用 PowerShell 的 WScript.Shell COM 对象动态生成 .lnk 快捷方式。"""
    def _ps_single_quote(value):
        return str(value).replace("'", "''")

    ps_cmd = (
        f"$WshShell = New-Object -ComObject WScript.Shell; "
        f"$Shortcut = $WshShell.CreateShortcut('{_ps_single_quote(shortcut_path)}'); "
        f"$Shortcut.TargetPath = '{_ps_single_quote(target_path)}'; "
        f"$Shortcut.WorkingDirectory = '{_ps_single_quote(working_dir)}'; "
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


def add_to_system_path(install_dir, log=print):
    """把安装目录加入系统 PATH（HKLM），machine 范围安装时使用，需要管理员权限。"""
    _validate_path_entry(install_dir)
    key_path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
        try:
            current_path, reg_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current_path, reg_type = "", winreg.REG_EXPAND_SZ

        parts = [p for p in current_path.split(";") if p]
        if any(p.lower() == install_dir.lower() for p in parts):
            log("ℹ Y-code 路径已存在于系统环境变量中，跳过配置。")
            return

        parts.append(install_dir)
        winreg.SetValueEx(key, "Path", 0, reg_type, ";".join(parts))

    broadcast_environment_change()
    log("✔ 成功将 Y-code 路径加入系统环境变量 PATH！")


def add_to_user_path(install_dir, log=print):
    """把安装目录加入 HKCU\\Environment\\PATH，直接用 winreg 读写，保留原始值类型
    (REG_EXPAND_SZ)，不再依赖 `reg query` 的文本输出解析（旧实现在 PATH 值里
    出现多个空格、或碰巧包含子串 "PATH" 的路径分量时会解析错位）。"""
    _validate_path_entry(install_dir)
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
                                   uninstall_string, display_icon,
                                   hive=winreg.HKEY_CURRENT_USER):
    """在卸载注册表下写入一个标准的“应用和功能”卸载入口。
    user 范围写 HKCU，machine 范围写 HKLM（需管理员权限）。"""
    key_path = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{app_key_name}"
    with winreg.CreateKey(hive, key_path) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, version)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, install_dir)
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, uninstall_string)
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, display_icon)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "Y-code")
        # 修复 M47：记录 Y-code 专用归属标记，供卸载侧校验安装目录归属（配合 High-1）
        winreg.SetValueEx(key, "YcodeInstallMarker", 0, winreg.REG_SZ, "Y-code")
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def detect_previous_install():
    """读取卸载注册表中的历史安装信息，用于重装时沿用原安装范围与路径。

    返回 (scope, install_dir)；未安装过返回 (None, None)。machine 优先：
    机器级安装存在时以它为准（覆盖安装必须回到同一范围才能被“应用和功能”正确接管）。
    """
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Y-code"
    for scope, hive in (("machine", winreg.HKEY_LOCAL_MACHINE),
                        ("user", winreg.HKEY_CURRENT_USER)):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                location, _ = winreg.QueryValueEx(key, "InstallLocation")
                if location and os.path.isdir(location):
                    return scope, os.path.normpath(location)
        except OSError:
            continue

    # 回退：注册表读不到（如残留清理、手动删除、跨版本迁移）时，扫描常见安装
    # 目录。目录存在且含 Y-code.exe 即视为已安装，更新时自动填充路径，无需重选。
    candidates = [
        ("user", default_install_dir("user")),
        ("machine", default_install_dir("machine")),
    ]
    if os.environ.get("ProgramFiles(x86)"):
        candidates.append(("machine", os.path.join(os.environ["ProgramFiles(x86)"], "Y-code")))
    for scope, dir_path in candidates:
        if os.path.isdir(dir_path) and os.path.isfile(os.path.join(dir_path, "Y-code.exe")):
            return scope, os.path.normpath(dir_path)
    return None, None


def is_user_an_admin():
    """当前进程是否已拥有管理员权限。"""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _parse_elevated_args(argv):
    """解析提权重启时携带的命令行参数（纯函数，可单测）。无提权参数返回 None。

    形式：--elevated [--machine] --dir=<路径> --cli=0/1 --shortcut=0/1
    """
    if "--elevated" not in argv:
        return None
    opts = {"scope": "user", "dir": "", "cli": True, "shortcut": True}
    for arg in argv:
        if arg == "--machine":
            opts["scope"] = "machine"
        elif arg.startswith("--dir="):
            opts["dir"] = arg[len("--dir="):].strip('"')
        elif arg.startswith("--cli="):
            # 修复 T21：精确解析 = 后的值，避免 --cli=10 被 endswith("1") 误判为 True
            opts["cli"] = arg.split("=", 1)[1] == "1"
        elif arg.startswith("--shortcut="):
            opts["shortcut"] = arg.split("=", 1)[1] == "1"
    if not opts["dir"]:
        return None
    return opts


def relaunch_elevated(options):
    """以管理员权限重启自身并携带安装选项（UAC 弹窗）。
    返回是否成功发起；成功后当前进程应直接退出，由提权后的新进程接着执行安装。
    """
    import ctypes
    args = ["--elevated"]
    if options.scope == "machine":
        args.append("--machine")
    args.append(f"--dir={options.install_dir}")
    args.append(f"--cli={1 if options.install_cli else 0}")
    args.append(f"--shortcut={1 if options.create_desktop_shortcut else 0}")
    exe = sys.executable
    params = subprocess.list2cmdline(args)
    if not getattr(sys, "frozen", False):
        # 源码调试态：sys.executable 是 python 解释器，需要带上脚本路径
        params = subprocess.list2cmdline([os.path.abspath(__file__), *args])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    return rc > 32


# ============================================================================
# 用户配置播种
# ============================================================================

def seed_user_config(bundle_dir, log=print):
    """初始化 ~/.ycode 用户配置为干净的空配置（无预置厂商）。

    仅在用户目录下尚不存在对应文件时才写入，不会覆盖用户已有的配置。
    不再复制安装包内的 config.example.json：那是带占位符 API key 的示例
    模板，直接播种会把「Agnes AI / 深度求索」等假厂商塞给用户，看似已
    配置、实际 key 无效无法调用。安装后用户自行在模型页添加厂商并填写
    真实 key（bundle_dir 参数仅为兼容调用签名保留，不再使用）。

    注意：系统提示词不再初始化到用户目录——它随安装包分发、只读跟随系统，
    用户目录只放数据与记忆（见 core/agent_service.load_system_prompt）。
    """
    user_home = os.path.expanduser("~")
    ycode_user_dir = os.path.join(user_home, ".ycode")
    os.makedirs(ycode_user_dir, exist_ok=True)

    config_path = os.path.join(ycode_user_dir, "config.json")
    if _seed_file_exclusive(
        config_path,
        source_path=None,
        default_text='{"active_model_id": null, "providers": []}',
    ):
        log(f"✔ 成功创建干净配置: {config_path}")


def _seed_file_exclusive(target_path, source_path=None, default_text=""):
    """Create one seed file atomically without following an existing link."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        fd = os.open(target_path, flags, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as destination:
            if source_path:
                with open(source_path, "rb") as source:
                    shutil.copyfileobj(source, destination)
            else:
                destination.write(str(default_text).encode("utf-8"))
    except BaseException:
        try:
            os.unlink(target_path)
        except OSError:
            pass
        raise
    return True


# ============================================================================
# 安装引擎（与界面解耦：log/progress 经回调上报，可在后台线程中运行）
# ============================================================================

class InstallError(Exception):
    """致命安装错误：向导会中断并展示该消息。"""


def _validate_path_entry(path):
    if ";" in str(path or ""):
        raise InstallError("安装路径不能包含分号，避免注入额外 PATH 条目。")


def _is_within_path(path, root):
    try:
        candidate = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        base = os.path.normcase(os.path.realpath(os.path.abspath(root)))
        return os.path.commonpath([candidate, base]) == base
    except (OSError, ValueError, TypeError):
        return False


def _validate_install_target(path, scope):
    install_dir = os.path.abspath(str(path or ""))
    _validate_path_entry(install_dir)
    if '"' in install_dir:
        raise InstallError("安装路径不能包含双引号。")
    drive, tail = os.path.splitdrive(install_dir)
    if not tail.strip("\\/"):
        raise InstallError("不能安装到磁盘根目录。")
    if scope == "machine":
        roots = [
            value for value in (
                os.environ.get("ProgramFiles"),
                os.environ.get("ProgramFiles(x86)"),
            ) if value
        ]
        if not roots or not any(
            _is_within_path(install_dir, root)
            and os.path.normcase(os.path.realpath(install_dir))
            != os.path.normcase(os.path.realpath(root))
            for root in roots
        ):
            raise InstallError("所有用户安装必须位于 Program Files 的子目录中。")
    return install_dir


def _safe_payload_path(install_dir, relative_path):
    """Resolve an archive member and require it to stay below install_dir."""
    rel = str(relative_path or "").replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"^[a-zA-Z]:", rel):
        raise InstallError(f"安装包包含非法路径: {relative_path}")
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise InstallError(f"安装包包含非法路径: {relative_path}")
    root = os.path.realpath(install_dir)
    destination = os.path.realpath(os.path.join(root, *parts))
    try:
        inside = os.path.commonpath([root, destination]) == root
    except ValueError:
        inside = False
    if not inside or destination == root:
        raise InstallError(f"安装包包含非法路径: {relative_path}")
    return destination


def _is_reparse_path(path):
    """Return whether an existing path redirects filesystem traversal."""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def _ensure_no_reparse_components(install_dir, destination):
    """Reject symlink/junction components below the selected install root."""
    root = os.path.abspath(install_dir)
    parent = os.path.abspath(os.path.dirname(destination))
    try:
        if os.path.commonpath([root, parent]) != root:
            raise InstallError(f"安装包目标路径越界: {destination}")
    except ValueError as exc:
        raise InstallError(f"安装包目标路径越界: {destination}") from exc

    current = root
    candidates = [current]
    relative_parent = os.path.relpath(parent, root)
    if relative_parent != ".":
        for part in relative_parent.split(os.sep):
            current = os.path.join(current, part)
            candidates.append(current)
    for candidate in candidates:
        if os.path.lexists(candidate) and _is_reparse_path(candidate):
            raise InstallError(f"安装路径包含符号链接或目录联接: {candidate}")
    # 修复 M46：目标文件自身也可能是预置的符号链接/挂载点（目录链检查覆盖不到），
    # 写入前用 lexists 校验（lexists 能发现悬空链接），命中即拒绝。
    if os.path.lexists(destination) and _is_reparse_path(destination):
        raise InstallError(f"安装目标文件是符号链接或目录联接: {destination}")


def _is_unrecognized_non_empty_dir(install_dir):
    """修复 M47：目标目录已存在、非空、且不含任何可识别的 Y-code 安装痕迹
    （Y-code.exe / ycode.exe / Y-code-uninstall.exe / _internal）时返回 True。
    这类目录多半是用户的既有目录，装入后卸载有误删风险，需显式警告。"""
    if not os.path.isdir(install_dir):
        return False
    try:
        if not os.listdir(install_dir):
            return False
    except OSError:
        return False
    return not any(
        os.path.exists(os.path.join(install_dir, marker))
        for marker in ("Y-code.exe", "ycode.exe", "Y-code-uninstall.exe", "_internal")
    )


class InstallOptions:
    """安装选项页收集到的用户选择。

    scope: 'user'（仅当前用户，默认）或 'machine'（所有用户，需管理员权限）。
    """

    def __init__(self, install_dir, install_cli=True, create_desktop_shortcut=True, scope="user"):
        self.install_dir = install_dir
        self.install_cli = install_cli
        self.create_desktop_shortcut = create_desktop_shortcut
        self.scope = scope if scope in ("user", "machine") else "user"


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


def _payload_names(kind, src, cli_only):
    """枚举待释放的载荷文件（相对路径列表）。

    cli_only=True 时只取 ycode.exe + _internal，且跳过 _internal/frontend
    （GUI 专属的前端静态资源）——独立安装命令行版体积更小。
    """
    if kind == "zip":
        import zipfile
        with zipfile.ZipFile(src) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
    else:
        names = []
        for root, dirs, files in os.walk(src):
            if cli_only and os.path.normpath(root) == os.path.normpath(os.path.join(src, "_internal")):
                dirs[:] = [d for d in dirs if d != "frontend"]
            for name in files:
                names.append(os.path.relpath(os.path.join(root, name), src))
    if cli_only:
        names = [
            n for n in names
            if n.replace("\\", "/") == "ycode.exe"
            or (n.replace("\\", "/").startswith("_internal/")
                and not n.replace("\\", "/").startswith("_internal/frontend/"))
        ]
    if not names:
        raise InstallError(f"安装包中缺少核心文件: {src}")
    return names


# 修复 M46：安装清单（记录本次释放的载荷相对路径），覆盖安装时据此清理
# 旧版已移除的文件；放在 _internal 内，卸载时随 _internal 一起被 rmtree。
INSTALL_MANIFEST_REL = "_internal/ycode_install_manifest.txt"


def _install_manifest_path(install_dir):
    return os.path.join(install_dir, *INSTALL_MANIFEST_REL.split("/"))


def _prune_removed_payload(install_dir, new_names, log):
    """修复 M46：覆盖安装时按上次安装清单清理新版已移除的旧文件，避免旧版
    已删除的文件永久残留。只处理清单中记录、且不在新清单里的文件；清单被
    篡改出越界路径时跳过，绝不出安装目录删文件。"""
    manifest = _install_manifest_path(install_dir)
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            old_names = [line.strip() for line in f if line.strip()]
    except OSError:
        return
    new_set = {name.replace("\\", "/") for name in new_names}
    for rel in old_names:
        if rel.replace("\\", "/") in new_set:
            continue
        try:
            target = _safe_payload_path(install_dir, rel)
        except InstallError:
            continue
        if os.path.isfile(target) or os.path.islink(target):
            try:
                os.remove(target)
                log(f"ℹ 已清理旧版残留文件: {rel}")
            except OSError as e:
                log(f"⚠️ 清理旧版残留文件 {rel} 失败: {e}")


def _write_install_manifest(install_dir, names):
    manifest = _install_manifest_path(install_dir)
    os.makedirs(os.path.dirname(manifest), exist_ok=True)
    with open(manifest, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(name.replace("\\", "/") for name in names) + "\n")


# 文件被占用时的"重启后替换"标记（保证更新时旧版正在运行也能成功替换）
_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_DELAY_UNTIL_REBOOT = 0x4


def _schedule_reboot_replace(tmp_src, dest):
    """用 MoveFileEx 标记临时文件在重启后替换到 dest（目标被占用时）。"""
    import ctypes
    ok = ctypes.windll.kernel32.MoveFileExW(
        tmp_src, dest, _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_DELAY_UNTIL_REBOOT)
    if not ok:
        raise ctypes.WinError()


def _write_bytes_with_reboot_fallback(dest, data, log):
    """写字节到 dest；目标被占用时回退到重启后替换。返回是否需要重启。"""
    try:
        with open(dest, "wb") as f:
            f.write(data)
        return False
    except PermissionError:
        tmp = dest + ".ycode-new"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            _schedule_reboot_replace(tmp, dest)
            log(f"⚠️ 文件被占用，已安排重启后替换: {os.path.basename(dest)}")
            return True
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise


def _copy_file_with_reboot_fallback(src, dest, log):
    """复制文件到 dest；目标被占用时回退到重启后替换。返回是否需要重启。"""
    try:
        shutil.copy2(src, dest)
        return False
    except PermissionError:
        tmp = dest + ".ycode-new"
        try:
            shutil.copy2(src, tmp)
            _schedule_reboot_replace(tmp, dest)
            log(f"⚠️ 文件被占用，已安排重启后替换: {os.path.basename(dest)}")
            return True
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise


def _copy_payload(kind, src, install_dir, cli_only, log, progress, manage_manifest=True):
    """带进度回调的载荷释放（占整体进度的 0~85%）。

    manage_manifest=False（CLI 合并进已有完整版安装）时不清理也不覆盖安装清单，
    避免按 CLI 子集清单误删 GUI 专属文件。
    """
    names = _payload_names(kind, src, cli_only)
    # 在创建任何目录/文件前完成全部成员校验，非法载荷不得留下部分安装。
    destinations = [_safe_payload_path(install_dir, rel) for rel in names]
    total = max(1, len(names))
    os.makedirs(install_dir, exist_ok=True)
    # Existing reparse points are rejected before the first payload byte is written.
    for dest in destinations:
        _ensure_no_reparse_components(install_dir, dest)
    if manage_manifest:
        _prune_removed_payload(install_dir, names, log)
    # 修复 M46：记录本次新创建的文件，中途失败时回滚删除，避免半成品残留
    # （已存在被覆盖的旧文件无法无损恢复，不在回滚范围内）。
    created = []
    needs_reboot = False
    try:
        if kind == "zip":
            import zipfile
            with zipfile.ZipFile(src) as zf:
                for idx, (rel, dest) in enumerate(zip(names, destinations), 1):
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    _ensure_no_reparse_components(install_dir, dest)
                    if not os.path.lexists(dest):
                        created.append(dest)
                    if _write_bytes_with_reboot_fallback(dest, zf.read(rel), log):
                        needs_reboot = True
                    progress(idx / total * 85.0)
        else:
            for idx, (rel, dest) in enumerate(zip(names, destinations), 1):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                _ensure_no_reparse_components(install_dir, dest)
                if not os.path.lexists(dest):
                    created.append(dest)
                if _copy_file_with_reboot_fallback(os.path.join(src, rel), dest, log):
                    needs_reboot = True
                progress(idx / total * 85.0)
    except Exception:
        for path in created:
            try:
                os.remove(path)
            except OSError:
                pass
        raise
    if manage_manifest:
        _write_install_manifest(install_dir, names)
    log(f"✔ 核心程序包及依赖成功释放到: {install_dir}")
    if needs_reboot:
        log("⚠️ 部分文件因正在运行而延迟到系统重启后替换，重启后更新才会完全生效。")


def run_install(mode, bundle_dir, options, startupinfo, log=print, progress=lambda p: None):
    """执行完整安装流程。任何致命错误抛 InstallError；非致命问题只记日志。

    返回汇总 dict：{"install_dir", "installed_cli", "desktop_shortcut", "merging_into_full"}。
    """
    scope = getattr(options, "scope", "user")
    install_dir = _validate_install_target(options.install_dir, scope)
    payload_kind, payload_src = _open_payload(bundle_dir)

    cli_exe_path = os.path.join(install_dir, "ycode.exe")
    gui_exe_path = os.path.join(install_dir, "Y-code.exe")
    merging_into_full = False

    # 1. 复制核心程序包
    if mode == "cli":
        # 目标目录已是完整版安装时只需合并进去，不重复写卸载注册表项
        merging_into_full = os.path.exists(gui_exe_path)
        if merging_into_full:
            log("ℹ 检测到该目录已安装图形界面版 (Y-code.exe)，将把命令行版合并安装进同一目录。")
        log("🚀 正在安装命令行版核心依赖与组件...")
    else:
        log("🚀 正在安装程序核心依赖与组件...")
    try:
        _copy_payload(payload_kind, payload_src, install_dir,
                      cli_only=(mode == "cli"), log=log, progress=progress,
                      manage_manifest=not (mode == "cli" and merging_into_full))
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
                if scope == "machine":
                    add_to_system_path(install_dir, log=log)
                else:
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
                if os.path.exists(cli_exe_path):
                    os.remove(cli_exe_path)
                    log("ℹ 已跳过命令行版 (ycode.exe)，仅保留图形界面版。")
            except Exception as e:
                log(f"⚠️ 移除命令行版组件失败: {e}")
            try:
                bundled_cli_setup = os.path.join(bundle_dir, "cli", "ycode_cli_setup.exe")
                if os.path.exists(bundled_cli_setup):
                    cli_subdir = os.path.join(install_dir, "cli")
                    os.makedirs(cli_subdir, exist_ok=True)
                    dest = os.path.join(cli_subdir, "ycode_cli_setup.exe")
                    shutil.copy2(bundled_cli_setup, dest)
                    log(f"✔ 已附带命令行版补装程序: {dest}，以后可双击它补装命令行版")
                else:
                    log("⚠️ 安装包中未找到命令行版补装程序，跳过。")
            except Exception as e:
                log(f"⚠️ 复制命令行版补装程序失败: {e}")
        progress(90.0)

        # 3. 桌面快捷方式 "Y-code" → Y-code.exe（按用户勾选；machine 范围放公共桌面）
        if options.create_desktop_shortcut:
            try:
                desktop_dir = get_common_desktop_dir() if scope == "machine" else get_desktop_dir()
                shortcut_path = os.path.join(desktop_dir, "Y-code.lnk")
                create_shortcut(shortcut_path, gui_exe_path, install_dir, startupinfo)
                desktop_shortcut = True
                log(f"✔ 桌面快捷方式创建成功: {shortcut_path}")
            except Exception as e:
                log(f"⚠️ 创建桌面快捷方式失败: {e}")
        else:
            log("ℹ 按您的选择，未创建桌面快捷方式。")
        progress(93.0)

        # 4. 复制卸载程序
        dest_uninst = os.path.join(install_dir, "Y-code-uninstall.exe")
        try:
            src_uninst = os.path.join(bundle_dir, "Y-code-uninstall.exe")
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

        # 6. 写入卸载注册表项（“应用和功能”）：machine 范围写 HKLM
        try:
            write_uninstall_registry_key(
                app_key_name="Y-code",
                display_name="Y-code",
                version=read_bundled_version(bundle_dir),
                install_dir=install_dir,
                uninstall_string=f'"{dest_uninst}"',
                display_icon=f'"{gui_exe_path}"',
                hive=winreg.HKEY_LOCAL_MACHINE if scope == "machine" else winreg.HKEY_CURRENT_USER,
            )
            log("✔ 已在“应用和功能”中注册 Y-code。")
        except Exception as e:
            log(f"⚠️ 写入卸载注册表项失败: {e}")
        progress(100.0)

    else:  # cli 模式
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
                dest_uninst = os.path.join(install_dir, "Y-code-uninstall.exe")
                src_uninst = os.path.join(bundle_dir, "Y-code-uninstall.exe")
                if os.path.exists(src_uninst):
                    shutil.copy2(src_uninst, dest_uninst)
                    log(f"✔ 卸载程序成功复制到: {dest_uninst}")
                    write_uninstall_registry_key(
                        app_key_name="Y-code-CLI",
                        display_name="Y-code CLI",
                        version=read_bundled_version(bundle_dir),
                        install_dir=install_dir,
                        uninstall_string=f'"{dest_uninst}"',
                        display_icon=f'"{cli_exe_path}"',
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
# 可视化安装向导（tkinter，经典 Windows 安装程序样式）
# ============================================================================

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# 品牌色与尺寸（现代风格：左侧深色品牌栏 + 右侧白色内容区）
ACCENT = "#4F6EF7"
ACCENT_DARK = "#3A52C4"
ACCENT_DISABLED = "#C7CDE0"
SIDEBAR_BG = "#1F2937"
SIDEBAR_TEXT = "#E5E7EB"
COLOR_CONTENT = "#FFFFFF"
COLOR_TEXT = "#1F2937"
COLOR_TEXT_DIM = "#6B7280"
COLOR_SEP = "#E5E7EB"
WINDOW_W, WINDOW_H = 820, 520
SIDEBAR_W = 200

FONT_PAGE_TITLE = ("Microsoft YaHei UI", 16, "bold")
FONT_PAGE_SUB = ("Microsoft YaHei UI", 10)
FONT_TITLE = ("Microsoft YaHei UI", 15, "bold")
FONT_BRAND = ("Microsoft YaHei UI", 18, "bold")
FONT_TEXT = ("Microsoft YaHei UI", 10)
FONT_TEXT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_DIM = ("Microsoft YaHei UI", 9)
FONT_VERSION = ("Microsoft YaHei UI", 8)


class SetupWizard(tk.Tk):
    """经典 Windows 安装向导：欢迎 → 安装用户 → 路径与选项 → 安装中 → 完成。

    布局为 NSIS MUI2 三段式：顶部白色横幅（页标题/副标题/图标）、中部内容区、
    底部按钮栏（左下版本号，右下 上一步/下一步/取消）。
    """

    def __init__(self, mode, bundle_dir, startupinfo, elevated_options=None):
        super().__init__()
        self.mode = mode
        self.bundle_dir = bundle_dir
        self.startupinfo = startupinfo
        self.version = read_bundled_version(bundle_dir)
        self._ui_queue = queue.Queue()

        # 重装检测：读取卸载注册表中的历史安装范围与路径
        prev_scope, prev_dir = detect_previous_install()
        self.previous_install = prev_dir  # None 或原安装路径
        self.elevated_options = elevated_options

        self.title("Y-code 安装")
        self.resizable(False, False)
        self.configure(bg=COLOR_CONTENT)
        self._center_window()
        self._set_icon()
        # 现代扁平化样式：clam 主题 + 品牌色按钮/进度条
        self._setup_style()
        # Windows 上最小化再恢复后，Tk 会把窗口收缩成只剩标题栏（实测可复现，
        # <Map> 事件无法捕获该时序），用轻量看门狗定期把固定几何声明回去。
        self.after(500, self._geometry_watchdog)

        # 选项状态：提权重启携带的选项 > 重装沿用 > 默认值
        if elevated_options:
            initial_scope = elevated_options["scope"]
            initial_dir = elevated_options["dir"]
            self.var_cli = tk.BooleanVar(value=elevated_options["cli"])
            self.var_shortcut = tk.BooleanVar(value=elevated_options["shortcut"])
        else:
            initial_scope = prev_scope or "user"
            initial_dir = prev_dir or default_install_dir(initial_scope)
            self.var_cli = tk.BooleanVar(value=True)
            self.var_shortcut = tk.BooleanVar(value=True)
        self.var_scope = tk.StringVar(value=initial_scope)
        self.var_dir = tk.StringVar(value=initial_dir)

        # 页面导航流程：cli 模式没有“安装用户”页（CLI 始终按当前用户安装）
        self._flow = (["welcome", "scope", "options", "installing", "finish"]
                      if mode == "full" else
                      ["welcome", "options", "installing", "finish"])

        self._build_chrome()

        self._pages = {}
        self._build_pages()
        if elevated_options:
            # 提权后的新进程：跳过向导页，直接按携带的选项开始安装
            self.show_page("installing")
            self.after(150, self._start_install)
        else:
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
            print(f"⚠️ 图标文件不存在: {ico}")
            return
        try:
            self.iconbitmap(ico)
        except Exception as e:
            print(f"⚠️ iconbitmap 失败: {e}")
        # tk 的 iconbitmap 不一定覆盖任务栏用的 ICON_BIG，且 Tk 窗口类的类图标
        # 始终是默认羽毛（任务栏/Alt-Tab 可能回退到它），这里用 Win32 显式把
        # 窗口大/小图标与类图标全部换成我们的图标。
        try:
            import ctypes
            u32 = ctypes.windll.user32
            # wm_frame() 在 Python 3.13+ 返回十进制字符串（旧版是 0x... 十六进制），
            # 两种格式都兼容；解析失败则整个 Win32 图标替换静默跳过（回退默认羽毛）
            frame = self.wm_frame().strip()
            hwnd = int(frame, 16) if frame.lower().startswith("0x") else int(frame)
            LR_LOADFROMFILE = 0x10
            IMAGE_ICON = 1
            WM_SETICON = 0x0080
            ICON_SMALL = 0
            ICON_BIG = 1
            GCLP_HICON = -14
            GCLP_HICONSM = -13  # 正确值是 -13（此前误写成 -34，小图标始终未替换）
            # 大图标加载失败时尝试多个尺寸（256 → 128 → 64 → 48 → 32）
            h_big = 0
            for size in (256, 128, 64, 48, 32):
                h_big = u32.LoadImageW(None, ico, IMAGE_ICON, size, size, LR_LOADFROMFILE)
                if h_big:
                    print(f"✔ 大图标加载成功 ({size}x{size})")
                    break
            h_small = u32.LoadImageW(None, ico, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
            if h_small:
                print("✔ 小图标加载成功 (32x32)")
            if h_big:
                u32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, h_big)
                u32.SetClassLongPtrW(hwnd, GCLP_HICON, h_big)
            else:
                print("⚠️ 大图标加载失败（所有尺寸都试过），任务栏可能回退默认羽毛")
            if h_small:
                u32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, h_small)
                u32.SetClassLongPtrW(hwnd, GCLP_HICONSM, h_small)
            else:
                print("⚠️ 小图标加载失败")
        except Exception as e:
            print(f"⚠️ Win32 图标替换异常: {e}")

    # ------------------------------------------------------------------
    # 三段式框架：横幅 + 底部按钮栏（内容区由调用方随后 pack）
    def _setup_style(self):
        """现代扁平化样式：clam 主题 + 品牌色按钮/进度条。"""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Brand.TButton", background=ACCENT, foreground="white",
                        font=FONT_TEXT_BOLD, padding=(20, 8), borderwidth=0,
                        focusthickness=0)
        style.map("Brand.TButton",
                  background=[("active", ACCENT_DARK), ("disabled", ACCENT_DISABLED)])
        style.configure("Ghost.TButton", background=COLOR_CONTENT, foreground=COLOR_TEXT_DIM,
                        font=FONT_TEXT, padding=(16, 8), borderwidth=0,
                        focusthickness=0)
        style.map("Ghost.TButton", background=[("active", "#F3F4F6")])
        style.configure("Brand.Horizontal.TProgressbar", background=ACCENT,
                        troughcolor=COLOR_SEP, borderwidth=0, thickness=14)

    # ------------------------------------------------------------------
    # 两栏式框架：左侧深色品牌栏 + 右侧白色内容区 + 底部按钮栏
    # ------------------------------------------------------------------
    def _build_chrome(self):
        # 左侧深色品牌栏：文字在上、Logo 在下
        sidebar = tk.Frame(self, bg=SIDEBAR_BG, width=SIDEBAR_W)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        tk.Label(sidebar, text="Y-code 星瑶", font=FONT_BRAND, fg=SIDEBAR_TEXT,
                 bg=SIDEBAR_BG).pack(pady=(44, 0))
        tk.Label(sidebar, text="AI 编程助手", font=FONT_DIM, fg="#9CA3AF",
                 bg=SIDEBAR_BG).pack()

        # Logo 放大居中（用 PIL 精确缩放到 168x168）
        self._sidebar_logo = None
        logo_path = os.path.join(self.bundle_dir, "assets", "ycode.png")
        if os.path.exists(logo_path):
            try:
                from PIL import Image, ImageTk
                im = Image.open(logo_path)
                im = im.resize((168, 168), Image.LANCZOS)
                self._sidebar_logo = ImageTk.PhotoImage(im)
                tk.Label(sidebar, image=self._sidebar_logo, bg=SIDEBAR_BG).pack(pady=(16, 0))
            except Exception:
                pass

        tk.Label(sidebar, text=f"v{self.version}", font=FONT_VERSION, fg="#6B7280",
                 bg=SIDEBAR_BG).pack(side="bottom", pady=40)

        # 右侧白色内容区
        self._content = tk.Frame(self, bg=COLOR_CONTENT)
        self._content.pack(side="left", fill="both", expand=True)

        # 页面标题区
        title_bar = tk.Frame(self._content, bg=COLOR_CONTENT, height=88)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        self._banner_title = tk.Label(title_bar, text="", font=FONT_PAGE_TITLE,
                                      fg=COLOR_TEXT, bg=COLOR_CONTENT, anchor="w")
        self._banner_title.place(x=40, y=20)
        self._banner_sub = tk.Label(title_bar, text="", font=FONT_PAGE_SUB,
                                    fg=COLOR_TEXT_DIM, bg=COLOR_CONTENT, anchor="w")
        self._banner_sub.place(x=40, y=52)
        tk.Frame(self._content, bg=COLOR_SEP, height=1).pack(fill="x")

        # 页面容器（标题栏和底部按钮之间的中间区域，页面在其中 place 切换）
        self._page_area = tk.Frame(self._content, bg=COLOR_CONTENT)
        self._page_area.pack(fill="both", expand=True)

        # 底部按钮栏
        bottom = tk.Frame(self._content, bg=COLOR_CONTENT, height=64)
        bottom.pack(side="bottom", fill="x")
        bottom.pack_propagate(False)
        tk.Frame(bottom, bg=COLOR_SEP, height=1).pack(fill="x")
        self._btn_cancel = ttk.Button(bottom, text="取消", style="Ghost.TButton",
                                      width=8, command=self.destroy)
        self._btn_next = ttk.Button(bottom, text="下一步", style="Brand.TButton",
                                    width=12, command=self._go_next)
        self._btn_back = ttk.Button(bottom, text="< 上一步", style="Ghost.TButton",
                                    width=10, command=self._go_back)
        self._btn_cancel.pack(side="right", padx=(4, 40), pady=13)
        self._btn_next.pack(side="right", padx=8, pady=13)
        self._btn_back.pack(side="right", padx=8, pady=13)
    # ------------------------------------------------------------------
    # 页面构建与导航
    # ------------------------------------------------------------------
    def _page(self, name):
        page = tk.Frame(self._page_area, bg=COLOR_CONTENT)
        self._pages[name] = page
        return page

    def _build_pages(self):
        self._build_welcome_page()
        if self.mode == "full":
            self._build_scope_page()
        self._build_options_page()
        self._build_installing_page()
        self._build_finish_page()

    _BANNERS = {
        "welcome": ("欢迎使用 Y-code 安装向导", "本向导将引导您完成 Y-code 的安装"),
        "scope": ("选择安装用户", "选择要为哪些用户安装 Y-code"),
        "options": ("选择安装位置与选项", "选择 Y-code 的安装文件夹与附加选项"),
        "installing": ("正在安装", "Y-code 正在安装，请等候。"),
        "finish": ("安装完成", "Y-code 安装向导已结束"),
    }

    def show_page(self, name):
        for page in self._pages.values():
            page.place_forget()
        self._pages[name].place(relwidth=1, relheight=1)
        title, sub = self._BANNERS[name]
        self._banner_title.configure(text=title)
        self._banner_sub.configure(text=sub)
        self._current_page = name
        self._update_nav_buttons()

    def _update_nav_buttons(self):
        name = self._current_page
        back, nxt, cancel = self._btn_back, self._btn_next, self._btn_cancel
        if name == "finish":
            back.pack_forget()
            nxt.pack_forget()
            cancel.pack_forget()
            cancel.configure(text="完成", state="normal")
            cancel.pack(side="right", padx=(4, 40), pady=13)
            return
        # 非完成页：确保三个按钮都在（顺序：back next cancel 从右往左 pack）
        cancel.pack_forget()
        nxt.pack_forget()
        back.pack_forget()
        self._btn_cancel.pack(side="right", padx=(4, 40), pady=13)
        self._btn_next.pack(side="right", padx=8, pady=13)
        self._btn_back.pack(side="right", padx=8, pady=13)
        cancel.configure(text="取消")
        if name == "installing":
            back.configure(state="disabled")
            nxt.configure(state="disabled")
            cancel.configure(state="disabled")
        else:
            cancel.configure(state="normal")
            back.configure(state="normal" if self._flow.index(name) > 0 else "disabled")
            nxt.configure(state="normal",
                          text="安装" if name == "options" else "下一步")

    def _go_next(self):
        name = self._current_page
        if name == "options":
            self._start_install()
            return
        idx = self._flow.index(name)
        if idx + 1 < len(self._flow):
            self.show_page(self._flow[idx + 1])

    def _go_back(self):
        idx = self._flow.index(self._current_page)
        if idx > 0:
            self.show_page(self._flow[idx - 1])

    # ------------------------------------------------------------------
    # 各页面内容
    # ------------------------------------------------------------------
    def _label(self, parent, text, font=FONT_TEXT, fg=COLOR_TEXT, **kw):
        return tk.Label(parent, text=text, font=font, fg=fg, bg=COLOR_CONTENT, **kw)

    def _build_welcome_page(self):
        page = self._page("welcome")
        is_full = self.mode == "full"
        self._label(page, "Y-code 星瑶", FONT_TITLE).place(relx=0.5, y=80, anchor="center")
        self._label(page, f"AI 编程助手   v{self.version}", FONT_PAGE_SUB, COLOR_TEXT_DIM).place(
            relx=0.5, y=112, anchor="center")
        desc = (
            "本向导将在您的电脑上安装 Y-code 图形界面版 (GUI)，\n"
            "接下来您可以选择安装用户、安装位置，\n"
            "以及是否附带安装命令行版 (CLI)。"
            if is_full else
            "本向导将在您的电脑上安装 Y-code 命令行版 (CLI)，\n"
            "安装完成后可在终端中直接使用 ycode 命令。"
        )
        self._label(page, desc, FONT_TEXT, justify="center").place(relx=0.5, y=190, anchor="center")
        self._label(page, "单击“下一步”继续，或单击“取消”退出安装向导。",
                    FONT_TEXT, COLOR_TEXT_DIM).place(relx=0.5, y=265, anchor="center")
        if self.previous_install:
            self._label(page, f"检测到已安装的 Y-code：{self.previous_install}，本次将覆盖安装。",
                        FONT_TEXT, "#8a6d1a").place(relx=0.5, y=305, anchor="center")

    def _build_scope_page(self):
        page = self._page("scope")
        locked = bool(self.previous_install)
        rb1 = tk.Radiobutton(page, text="仅为我安装（推荐）", variable=self.var_scope,
                             value="user", command=self._on_scope_change,
                             bg=COLOR_CONTENT, fg=COLOR_TEXT, font=FONT_TEXT,
                             selectcolor=COLOR_CONTENT, activebackground=COLOR_CONTENT,
                             activeforeground=COLOR_TEXT, cursor="hand2",
                             highlightthickness=0, bd=0)
        rb2 = tk.Radiobutton(page, text="为使用这台电脑的所有人安装", variable=self.var_scope,
                             value="machine", command=self._on_scope_change,
                             bg=COLOR_CONTENT, fg=COLOR_TEXT, font=FONT_TEXT,
                             selectcolor=COLOR_CONTENT, activebackground=COLOR_CONTENT,
                             activeforeground=COLOR_TEXT, cursor="hand2",
                             highlightthickness=0, bd=0)
        rb1.place(x=50, y=70)
        rb2.place(x=50, y=104)
        self._label(page, "为所有用户安装需要管理员权限（会弹出 UAC 确认），\n"
                          "安装位置默认为 Program Files，快捷方式对所有用户可见。",
                    FONT_PAGE_SUB, COLOR_TEXT_DIM, justify="left").place(x=72, y=134)
        if locked:
            rb1.configure(state="disabled")
            rb2.configure(state="disabled")
            self._label(page, f"检测到已安装的 Y-code：{self.previous_install}\n"
                              "重装将沿用原安装范围，如需变更请先卸载。",
                        FONT_TEXT, "#8a6d1a", justify="left").place(x=50, y=200)

    def _on_scope_change(self):
        # 未锁定时，安装路径在用户/机器两个默认值之间跟随切换；
        # 用户手动改过的路径（不在两个默认值里）保持不变
        if self.previous_install:
            return
        cur = self.var_dir.get().strip()
        defaults = {default_install_dir("user"), default_install_dir("machine")}
        if not cur or cur in defaults:
            self.var_dir.set(default_install_dir(self.var_scope.get()))

    def _build_options_page(self):
        page = self._page("options")
        self._label(page, "安装位置:", FONT_TEXT).place(x=40, y=52)
        locked = bool(self.previous_install)
        self._dir_entry = ttk.Entry(page, textvariable=self.var_dir,
                                    state="disabled" if locked else "normal")
        self._dir_entry.place(x=40, y=78, width=430, height=30)
        self._btn_browse = ttk.Button(page, text="浏览...", style="Ghost.TButton",
                                      command=self._browse_dir,
                                      state="disabled" if locked else "normal")
        # Ghost.TButton 样式 padding=(16,8) 会占掉 32px 宽度，110px 按钮实际可用
        # 仅 78px，"浏览..." 显示不下。单独调小 padding 或加宽到 120px
        self._btn_browse.place(x=470, y=78, width=120, height=30)
        if locked:
            self._label(page, "检测到已安装的 Y-code，将覆盖安装到原位置。",
                        FONT_PAGE_SUB, "#8a6d1a").place(x=40, y=108)

        if self.mode == "full":
            tk.Checkbutton(page, text="创建桌面快捷方式",
                           variable=self.var_shortcut,
                           bg=COLOR_CONTENT, fg=COLOR_TEXT, font=FONT_TEXT,
                           selectcolor=COLOR_CONTENT, activebackground=COLOR_CONTENT,
                           activeforeground=COLOR_TEXT, cursor="hand2",
                           highlightthickness=0, bd=0).place(x=40, y=160)
            tk.Checkbutton(page, text="同时安装命令行版 CLI（终端中使用 ycode 命令）",
                           variable=self.var_cli,
                           bg=COLOR_CONTENT, fg=COLOR_TEXT, font=FONT_TEXT,
                           selectcolor=COLOR_CONTENT, activebackground=COLOR_CONTENT,
                           activeforeground=COLOR_TEXT, cursor="hand2",
                           highlightthickness=0, bd=0).place(x=40, y=192)
        else:
            self._label(
                page,
                "将把命令行版 (ycode.exe) 安装到所选目录并加入 PATH。\n"
                "若所选目录已安装图形界面版，则会自动合并安装。",
                FONT_TEXT, COLOR_TEXT_DIM, justify="left",
            ).place(x=40, y=160)

    def _build_installing_page(self):
        page = self._page("installing")
        self._progress = ttk.Progressbar(page, mode="determinate", maximum=100.0,
                                         style="Brand.Horizontal.TProgressbar")
        self._progress.place(x=40, y=40, width=500, height=14)
        self._pct_label = self._label(page, "0%", FONT_TEXT, COLOR_TEXT_DIM)
        self._pct_label.place(x=550, y=38)
        self._log_text = tk.Text(
            page, font=("Consolas", 9), bg="#FFFFFF", fg="#333333",
            bd=1, relief="solid", highlightthickness=0,
            state="disabled", wrap="word",
        )
        self._log_text.place(x=40, y=76, width=540, height=290)

    def _build_finish_page(self):
        page = self._page("finish")
        self._finish_icon = self._label(page, "🎉", ("Microsoft YaHei UI", 24))
        self._finish_icon.place(relx=0.5, y=80, anchor="center")
        self._finish_title = self._label(page, "安装成功", ("Microsoft YaHei UI", 13, "bold"))
        self._finish_title.place(relx=0.5, y=128, anchor="center")
        self._finish_detail = self._label(page, "", FONT_TEXT, justify="center")
        self._finish_detail.place(relx=0.5, y=200, anchor="center")
        self._finish_hint = self._label(page, "", FONT_TEXT, COLOR_TEXT_DIM, justify="center")
        self._finish_hint.place(relx=0.5, y=280, anchor="center")

    # ------------------------------------------------------------------
    # 交互逻辑
    # ------------------------------------------------------------------
    def _browse_dir(self):
        chosen = filedialog.askdirectory(parent=self, title="选择安装位置",
                                         initialdir=self.var_dir.get() or None)
        if chosen:
            # 浏览选择后自动在所选目录下追加 Y-code 子目录（已叫 Y-code 不重复），
            # 避免选盘符根目录时把程序文件直接平铺进 D:\ 之类的位置
            self.var_dir.set(normalize_browsed_install_dir(chosen))

    def _start_install(self):
        install_dir = self.var_dir.get().strip()
        if not install_dir:
            messagebox.showwarning("Y-code 安装", "请先选择有效的安装位置。", parent=self)
            return
        # 修复 M47：user 范围安装目录无位置约束，可装进任意已有非空目录；
        # 目标非空且不像既有 Y-code 安装时先警告（重装/提权重启前已确认过，跳过）。
        if (not self.elevated_options and not self.previous_install
                and _is_unrecognized_non_empty_dir(install_dir)):
            if not messagebox.askyesno(
                "Y-code 安装",
                f"所选目录已存在且不为空：\n{install_dir}\n\n"
                "继续安装会把 Y-code 文件写入该目录。为安全起见，建议改用空目录。\n\n"
                "是否继续安装到该目录？",
                icon="warning",
                parent=self,
            ):
                return
        scope = "user" if self.mode == "cli" else self.var_scope.get()
        options = InstallOptions(
            install_dir=install_dir,
            install_cli=self.var_cli.get(),
            create_desktop_shortcut=self.var_shortcut.get(),
            scope=scope,
        )
        # machine 范围需要管理员权限：未提权时发起 UAC 提权重启（新进程直接开始安装），
        # 当前进程退出；用户拒绝 UAC 时停留在本页
        if scope == "machine" and not is_user_an_admin():
            if relaunch_elevated(options):
                self.destroy()
                return
            messagebox.showwarning(
                "Y-code 安装",
                "为所有用户安装需要管理员权限。\n"
                "您可以在 UAC 弹窗中选择“是”重试，或返回上一步改为“仅为我安装”。",
                parent=self,
            )
            return
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
                hints.append("本次未安装命令行版；如需要，可随时双击安装目录下 cli\\ycode_cli_setup.exe 补装。")
        else:
            hints = ["打开任意全新的命令行窗口 (CMD/PowerShell)，输入 ycode 敲回车即可开始使用。"]
        self._finish_detail.configure(
            text=f"安装路径: {install_dir}\n配置文件夹: {ycode_user_dir}"
        )
        self._finish_hint.configure(text="\n".join(hints))
        self.show_page("finish")

    def _show_error(self, message):
        self._finish_icon.configure(text="🚨")
        self._finish_title.configure(text="安装失败", fg="#c0392b")
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
    # UAC 提权重启时携带的安装选项（无则为正常向导流程）
    elevated_options = _parse_elevated_args(sys.argv[1:])

    wizard = SetupWizard(mode=mode, bundle_dir=bundle_dir, startupinfo=startupinfo,
                         elevated_options=elevated_options)
    wizard.mainloop()


if __name__ == "__main__":
    main()
