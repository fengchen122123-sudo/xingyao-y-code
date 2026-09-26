"""
ycode_setup.py
Y-code 官方 Windows 安装向导（可视化 GUI 版，单一源码，靠内嵌的 setup_mode.txt 区分两种模式）：

  full 模式（默认）：完整版安装包。默认安装图形界面版 (Y-code.exe)，
                     安装选项页可勾选是否附带安装命令行版 (ycode.exe / ycode 命令)、
                     是否创建桌面快捷方式。
                     若用户当时选择不装命令行版，以后想补装时重新运行本安装
                     向导并勾选「命令行版」即可（载荷本身两种 exe 都带）。

  cli 模式：CLI 独立安装包。只安装命令行版，用于用户后续补装，或单独分发
            给只想用命令行的用户。

界面：tkinter（标准库，零新增依赖）。
安装引擎与界面解耦：run_install() 在后台线程执行，经队列回调刷新 UI，
可直接被 pytest 无界面测试。

模式来源：build_release.py 用 --onefile 分别打包本脚本两次，每次都通过
--add-data 内嵌一个不同内容的 setup_mode.txt（'full' 或 'cli'）。如果直接用
项目根目录下的 ycode_setup.spec 手动构建（不经过 build_release.py），因为
没有内嵌 setup_mode.txt，会自动按 'full' 模式运行。
"""
import lzma  # noqa: F401  载荷改用 ZIP_LZMA 压缩后，释放依赖 _lzma 解码器，
             # 显式导入确保 PyInstaller 把 _lzma.pyd 打进安装向导/卸载向导
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import winreg


# 桌面快捷方式显示名（品牌名）
DESKTOP_SHORTCUT_NAME = "星瑶code.lnk"


# 桌面路径/环境变量广播/管理员检测/reparse 判定等基础工具下沉到
# installer_common.py：卸载向导也复用同一实现，且不再链接本模块的
# tkinter/PIL 依赖树（卸载器体积从 32MB 降到 ~25MB）。
from installer_common import (
    _is_reparse_path,
    broadcast_environment_change,
    get_common_desktop_dir,
    get_desktop_dir,
    is_user_an_admin,
)


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


def launch_gui_app(install_dir, startupinfo):
    """安装完成后启动图形界面版 Y-code.exe。返回是否成功发起启动。

    管理员身份（"为所有用户安装"经 UAC 提权）时经 explorer.exe 中转启动，
    使程序回落到普通用户权限，避免继承安装器的提权上下文（拖放文件失效、
    配置写入管理员账户目录等问题）。启动失败静默返回 False，不阻塞向导退出。
    """
    exe_path = os.path.join(install_dir, "Y-code.exe")
    if not os.path.exists(exe_path):
        return False
    try:
        if is_user_an_admin():
            # explorer.exe 以桌面 Shell 的普通用户身份代为启动目标程序
            subprocess.Popen(["explorer.exe", exe_path], startupinfo=startupinfo)
        else:
            subprocess.Popen([exe_path], cwd=install_dir, startupinfo=startupinfo)
    except OSError:
        return False
    return True


# 允许自清理的安装包文件名白名单：
#   * Y-CODE-Setup-v{版本}.exe —— 「关于」页检查更新下载到缓存目录的更新包；
#   * ycode_cli_setup*.exe     —— 独立分发的 CLI 安装向导（补装用）。
# 只有这两种文件才可能是"本向导自身"，其余一律不动，避免误删用户文件。
_SELF_INSTALLER_NAME_RE = re.compile(
    r"^(?:Y-CODE-Setup-v[^\\/]+\.exe|ycode_cli_setup[^\\/]*\.exe)$",
    re.IGNORECASE,
)


def _cleanup_log(msg):
    """自清理相关日志：输出失败（如控制台编码不支持、stdout 已关闭）绝不允许冒泡。"""
    try:
        print(f"[setup] {msg}")
    except Exception:
        pass


def schedule_installer_self_cleanup(installer_path: str, startupinfo=None) -> bool:
    """【仅安装成功时调用】安排延时删除安装包自身，返回是否成功调度。

    设计意图与约束：
      * 为什么必须延时：Windows 上正在运行的 exe 被系统锁定，进程自己删不掉
        自己，只能另起一个脱离本进程的命令解释器，等本向导退出、文件解锁之后
        再删。这里用 `ping -n 3 127.0.0.1` 充当约 2 秒延时（比 `timeout` 可靠：
        timeout 在无控制台的 detached 进程里会以"输入重定向错误"直接失败）。
      * 为什么不能在非 frozen 下调：开发态 `python ycode_setup.py` 时
        sys.executable 是 python.exe，删掉它会破坏用户的 Python 环境。只有
        PyInstaller 打包（frozen）后 sys.executable 才真正指向安装包自身，
        所以本函数只做安全校验，由调用点（SetupWizard）负责判断 frozen。
      * 为什么绝不能无条件删：用户中途关掉向导、或安装失败时，安装包必须保留，
        让用户能直接重跑，而不是重新下载几十 MB。因此清理只在"安装成功 +
        向导退出"这一条路径上由 _maybe_schedule_installer_cleanup 调度。

    安全前置：路径必须非空、必须已是存在的文件，且文件名命中
    _SELF_INSTALLER_NAME_RE。任何不满足前置或启动失败的情况都只打印日志并
    返回 False，绝不抛异常、绝不影响安装流程。
    """
    try:
        if not installer_path or not os.path.isfile(installer_path):
            return False
        if not _SELF_INSTALLER_NAME_RE.match(os.path.basename(installer_path)):
            _cleanup_log(f"安装包文件名不在自清理白名单内，跳过清理: {installer_path}")
            return False
        # 路径要拼进 cmd 字符串，含引号会破坏命令结构，宁可放弃清理
        if '"' in installer_path:
            _cleanup_log(f"安装包路径含非法字符，跳过自清理: {installer_path}")
            return False

        # ping 约 2 秒延时 → 等本向导进程退出、exe 解锁后再删；>nul 屏蔽 ping 输出
        cmd = f'cmd /c ping -n 3 127.0.0.1 >nul & del /f /q "{installer_path}"'
        creationflags = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                         | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        # detached：脱离本进程，本向导退出后它继续存活完成删除；
        # startupinfo 由调用方传入（复用向导里隐藏黑框的那个），避免闪窗
        subprocess.Popen(
            cmd, shell=True, creationflags=creationflags,
            close_fds=True, startupinfo=startupinfo,
        )
        _cleanup_log(f"安装成功，已安排向导退出后清理安装包: {installer_path}")
        return True
    except Exception as e:
        _cleanup_log(f"调度安装包自清理失败（忽略，不影响安装结果）: {e}")
        return False


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


def _is_under_program_files(path):
    """判断 path 是否位于 Program Files（含 x86）的子目录中（不含根目录本身）。

    这是 machine 范围安装位置的唯一判定真值，供向导选项页即时校验与
    _validate_install_target 引擎侧最后防线共用。此前两处各写一份判定，
    一旦漂移就会出现"界面说合法、安装却报错"的错位（用户看到莫名的"安装失败"）。
    判定与旧实现完全一致：realpath + normcase 归一化（消解 8.3 短名、大小写、
    尾随反斜杠、目录联接差异），并排除 Program Files 根目录本身。
    """
    install_dir = os.path.abspath(str(path or ""))
    roots = [
        value for value in (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
        ) if value
    ]
    if not roots:
        return False
    return any(
        _is_within_path(install_dir, root)
        and os.path.normcase(os.path.realpath(install_dir))
        != os.path.normcase(os.path.realpath(root))
        for root in roots
    )


def _validate_install_target(path, scope):
    install_dir = os.path.abspath(str(path or ""))
    _validate_path_entry(install_dir)
    if '"' in install_dir:
        raise InstallError("安装路径不能包含双引号。")
    drive, tail = os.path.splitdrive(install_dir)
    if not tail.strip("\\/"):
        raise InstallError("不能安装到磁盘根目录。")
    if scope == "machine":
        # 复用 _is_under_program_files，避免与向导选项页的即时校验判定漂移。
        # 这条引擎侧校验保留为最后防线，绝不因界面已校验而放宽——防止绕过向导
        # 的路径（提权参数、手改注册表）装到所有人可写目录后又加全局 PATH。
        if not _is_under_program_files(install_dir):
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

# 被占用文件的"改名让位"备份后缀。改名让位不需要管理员权限，对正在运行的 exe
# 也能成功，因此优先于需要管理员权限的 MoveFileEx 重启替换。
_BAK_SUFFIX = ".ycode-bak"

# "重启后替换"用的临时文件后缀。该 tmp 会被登记进 HKLM 的
# PendingFileRenameOperations，下次开机时用它覆盖目标文件——因此它一旦残留到下一次
# 安装，就会在下次重启时把新版本覆盖回旧内容（前端 index.html 与 assets 哈希不配套
# 即白屏），必须在本次成功写入目标后清理掉。
_REBOOT_TMP_SUFFIX = ".ycode-new"


def _schedule_reboot_replace(tmp_src, dest):
    """用 MoveFileEx 标记临时文件在重启后替换到 dest（目标被占用时）。"""
    import ctypes
    ok = ctypes.windll.kernel32.MoveFileExW(
        tmp_src, dest, _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_DELAY_UNTIL_REBOOT)
    if not ok:
        raise ctypes.WinError()


def _is_registered_bak(bak, renamed_aside):
    """该备份路径是否已登记进回滚集合。

    已登记的备份绝不能被删除：删掉它，回滚时再也恢复不出旧字节，还会把一个其实
    从未被修改的文件误报成"未能恢复为旧版本"，把用户引向错误的排查方向。
    """
    if not renamed_aside:
        return False
    return any(registered == bak for registered, _dest in renamed_aside)


def _rename_aside(dest, log, renamed_aside=None):
    """把被占用的 dest 改名让位为 dest + _BAK_SUFFIX，返回备份路径；不可行时返回 None。

    改名让位（os.replace）不需要管理员权限，对正在运行的 exe 也能成功，因此优先
    于 MoveFileEx 重启替换。任何 OSError 都返回 None（不抛异常），交由上层走重启
    替换兜底；上一次安装遗留的备份删不掉时同样放弃让位，避免覆盖那份备份。
    """
    if not os.path.isfile(dest):
        # 只有普通文件才适合让位：目标是目录（损坏的安装）时让位会把目录换成文件，
        # 属于破坏性变更，直接交给重启替换/报错兜底。
        return None
    bak = dest + _BAK_SUFFIX
    if os.path.lexists(bak):
        if _is_registered_bak(bak, renamed_aside):
            # 这份备份已经承担着回滚职责，保留它并放弃让位（放弃让位只影响本文件的
            # 兜底路径选择，回滚能力不受损）。
            log(f"ℹ 备份已在回滚集合中，保留不删: {os.path.basename(bak)}")
            return None
        try:
            os.remove(bak)
        except OSError as exc:
            log(f"⚠️ 上次的备份仍被占用，跳过让位: {os.path.basename(bak)} ({exc})")
            return None
    try:
        os.replace(dest, bak)
        return bak
    except OSError as exc:
        log(f"⚠️ 改名让位失败: {os.path.basename(dest)} ({exc})")
        return None


def _restore_renamed(bak, dest, log):
    """把让位/备份出去的旧文件恢复回原名。失败只记日志，不掩盖原始异常。

    优先用 os.replace（同卷原子改名）。改名失败时退回复制回写：让位失败的常见
    原因正是目标被以无 FILE_SHARE_DELETE 方式打开，此时 os.replace(bak, dest)
    同样会失败，但只要还能写就仍能把备份字节复制回去——否则 _make_way_for_existing
    的复制备份就白做了，失败后目录仍会半新半旧。
    """
    try:
        os.replace(bak, dest)
        return True
    except OSError as exc:
        try:
            shutil.copy2(bak, dest)
        except OSError:
            log(f"⚠️ 恢复原文件失败: {os.path.basename(dest)} ({exc})")
            return False
        try:
            os.remove(bak)
        except OSError:
            pass
        return True


def _cleanup_leftover_baks(install_dir, log):
    """处理安装目录里遗留的 *.ycode-bak：先恢复、再清理。

    上一次安装"改名到一半被强杀/断电"时，bak 是目标文件当时唯一的一份副本：
      * 对应目标**不存在** → 把 bak 改回原名（自愈，绝不丢掉唯一副本）；
      * 对应目标**存在** → 说明目标已就位，bak 只是冗余备份，尽力删除。
    仍用 os.walk(followlinks=False) 且剪掉 reparse point 目录，绝不越出
    install_dir；恢复/删除失败只记日志，绝不中断安装。
    """
    try:
        for root, dirs, files in os.walk(install_dir, followlinks=False):
            dirs[:] = [d for d in dirs if not _is_reparse_path(os.path.join(root, d))]
            for name in files:
                if not name.endswith(_BAK_SUFFIX):
                    continue
                bak = os.path.join(root, name)
                dest = bak[:-len(_BAK_SUFFIX)]
                if not os.path.lexists(dest):
                    # 目标缺失：bak 是上次中断留下的唯一副本，必须恢复
                    try:
                        os.replace(bak, dest)
                        log(f"ℹ 已恢复上次中断遗留的旧文件: {os.path.relpath(dest, install_dir)}")
                    except OSError as exc:
                        log(f"⚠️ 恢复上次中断遗留的旧文件失败: {name} ({exc})")
                    continue
                # 目标已就位：bak 是冗余备份，尽力删除
                try:
                    os.remove(bak)
                    log(f"ℹ 已清理上次遗留的备份文件: {os.path.relpath(bak, install_dir)}")
                except OSError as exc:
                    log(f"⚠️ 清理遗留备份文件失败: {name} ({exc})")
    except OSError as exc:
        log(f"⚠️ 扫描遗留备份文件失败: {exc}")


def _collect_stale_reboot_tmps(install_dir, log):
    """收集上次安装遗留的"待重启替换"临时文件（*.ycode-new）路径集合。

    上次安装对某个文件登记过 MoveFileEx(..., MOVEFILE_DELAY_UNTIL_REBOOT) 时，会在
    下次开机用它覆盖目标。若用户没重启就再次安装：本次把新版本直接写进了目标，而
    残留的 tmp（旧内容）与注册表条目还在 → 下次重启反而把新版本覆盖回旧内容；若该
    文件属于前端（index.html / assets），表现就是"更新后打开白屏"。
    这里只记下这批陈旧 tmp，等本次**成功写入对应目标之后**再删除它们：pending rename
    的源文件不存在时该操作在重启时是空操作，所以删除陈旧 tmp 既不需要管理员权限，
    也不会破坏仍需生效的重启替换。仍不跟随 reparse point、绝不越出 install_dir。
    """
    stale = set()
    try:
        for root, dirs, files in os.walk(install_dir, followlinks=False):
            dirs[:] = [d for d in dirs if not _is_reparse_path(os.path.join(root, d))]
            for name in files:
                if name.endswith(_REBOOT_TMP_SUFFIX):
                    stale.add(os.path.join(root, name))
    except OSError as exc:
        log(f"⚠️ 扫描遗留的待重启替换文件失败: {exc}")
    if stale:
        log(f"ℹ 检测到 {len(stale)} 个上次安装遗留的待重启替换文件，"
            "将在本次成功写入对应文件后清理（避免重启后把新版本覆盖回旧内容）。")
    return stale


def _drop_stale_reboot_tmp(dest, stale_news, log):
    """本次已成功写入 dest：清掉它遗留的陈旧 .ycode-new，避免重启时被覆盖回旧内容。

    只处理"本次安装开始前就存在"的那批（stale_news），绝不动本次自己产生的
    .ycode-new——那个文件正被登记着等重启替换，删了就白等一场。
    """
    if not stale_news:
        return
    tmp = dest + _REBOOT_TMP_SUFFIX
    if tmp not in stale_news:
        return
    stale_news.discard(tmp)
    try:
        os.remove(tmp)
        log(f"ℹ 已清理上次遗留的待重启替换文件: {os.path.basename(tmp)}")
    except OSError as exc:
        log(f"⚠️ 清理遗留的待重启替换文件失败: {os.path.basename(tmp)} ({exc})")


def _make_way_for_existing(dest, log, renamed_aside):
    """先让位、后写入：目标已存在时先把它纯改名让位为 dest + _BAK_SUFFIX。

    改名让位不复制数据（几千个文件也很快），把 (bak, dest) 记入 renamed_aside
    供失败回滚——否则第 N 个文件失败时，前 N-1 个已被新版原地覆盖且无从恢复，
    目录会半新半旧（前端 index.html 与 assets 哈希不配套即白屏）。
    目标不存在、目标是目录、或让位失败（如文件被以无 FILE_SHARE_DELETE 方式
    打开）时什么都不做，交回上层三级兜底（直接写 / 让位后写 / 重启替换），
    绝不因单个文件让位失败而中止安装。
    """
    if not os.path.isfile(dest):
        # 只有普通文件才适合让位：目标不存在无需让位，目标是目录让位会破坏结构。
        return
    renamed = _rename_aside(dest, log, renamed_aside)
    if renamed is not None:
        renamed_aside.append((renamed, dest))
        return
    # 让位失败时"改名"这条路走不通，但"读"通常仍被允许：改为复制一份备份，
    # 并把 (bak, dest) 同样记入 renamed_aside 复用回滚逻辑。否则该文件会被就地
    # 覆盖且无备份，后续任一文件失败就会留下"该文件是新版、其余是旧版"的半新半旧
    # 目录。备份也失败时才退回现状（直接写、无备份），绝不因备份失败中止安装。
    bak = dest + _BAK_SUFFIX
    try:
        if os.path.lexists(bak) and not _is_registered_bak(bak, renamed_aside):
            os.remove(bak)
        shutil.copy2(dest, bak)
    except OSError as exc:
        log(f"⚠️ 复制备份失败: {os.path.basename(dest)} ({exc})")
        return
    renamed_aside.append((bak, dest))


def _payload_file_error(dest, exc):
    """构造带失败文件路径的 InstallError；兜底异常已含路径时不重复出现。"""
    detail = str(exc)
    if dest not in detail:
        detail = f"{detail}\n文件: {dest}"
    return InstallError(
        f"释放文件失败: {detail}\n当前安装目录可能不完整，请重跑安装包修复。"
    )


def _write_bytes_with_reboot_fallback(dest, data, log, renamed_aside=None):
    """写字节到 dest，返回 (status, bak)。

    三级顺序：直接写 → 改名让位后写 → 写临时文件并登记重启后替换。
      ("written", None)       直接写入成功；
      ("written_aside", bak)  原文件被占用，已改名让位并写入新文件（bak 供回滚）；
      ("reboot", None)        已登记重启后替换（需管理员权限，仅在让位失败时尝试）。
    两条兜底路径都失败时抛错，异常信息带上 dest 便于定位失败文件。
    """
    try:
        with open(dest, "wb") as f:
            f.write(data)
        return "written", None
    except PermissionError:
        bak = _rename_aside(dest, log, renamed_aside)
        if bak is not None:
            try:
                with open(dest, "wb") as f:
                    f.write(data)
                log(f"⚠️ 文件被占用，已改名让位并写入新文件: {os.path.basename(dest)}")
                return "written_aside", bak
            except Exception as exc:
                # 让位后仍写不进去：把旧文件改回原名，绝不留下"有备份却没新文件"的状态
                _restore_renamed(bak, dest, log)
                raise OSError(f"文件被占用且让位后仍无法写入: {dest}") from exc
        if os.path.isdir(dest):
            # 目标是目录而非文件：MoveFileEx 无法用文件替换目录，直接报错让上层回滚
            raise OSError(f"目标位置被目录占用，无法替换: {dest}")
        tmp = dest + _REBOOT_TMP_SUFFIX
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            _schedule_reboot_replace(tmp, dest)
            log(f"⚠️ 文件被占用，已安排重启后替换: {os.path.basename(dest)}")
            return "reboot", None
        except Exception as exc:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise OSError(f"文件被占用且无法安排重启替换: {dest}") from exc


def _copy_file_with_reboot_fallback(src, dest, log, renamed_aside=None):
    """复制文件到 dest，返回 (status, bak)；语义同 _write_bytes_with_reboot_fallback。"""
    try:
        shutil.copy2(src, dest)
        return "written", None
    except PermissionError:
        bak = _rename_aside(dest, log, renamed_aside)
        if bak is not None:
            try:
                shutil.copy2(src, dest)
                log(f"⚠️ 文件被占用，已改名让位并写入新文件: {os.path.basename(dest)}")
                return "written_aside", bak
            except Exception as exc:
                # 让位后仍写不进去：把旧文件改回原名，绝不留下"有备份却没新文件"的状态
                _restore_renamed(bak, dest, log)
                raise OSError(f"文件被占用且让位后仍无法写入: {dest}") from exc
        if os.path.isdir(dest):
            # 目标是目录而非文件：MoveFileEx 无法用文件替换目录，直接报错让上层回滚
            raise OSError(f"目标位置被目录占用，无法替换: {dest}")
        tmp = dest + _REBOOT_TMP_SUFFIX
        try:
            shutil.copy2(src, tmp)
            _schedule_reboot_replace(tmp, dest)
            log(f"⚠️ 文件被占用，已安排重启后替换: {os.path.basename(dest)}")
            return "reboot", None
        except Exception as exc:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise OSError(f"文件被占用且无法安排重启替换: {dest}") from exc


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
    # 复制开始前清理上次安装遗留的让位备份（*.ycode-bak）：上次因文件被占用删不掉
    # 的备份在此重试；删不掉只记日志，绝不影响本次安装。
    _cleanup_leftover_baks(install_dir, log)
    # 上次遗留的"待重启替换"临时文件先记下来：本次成功写入对应目标后要删掉它们，
    # 否则下次重启会把新版本覆盖回旧内容（前端资源哈希不配套即白屏）。
    stale_news = _collect_stale_reboot_tmps(install_dir, log)
    # 记录本次新创建的文件、让位备份与重启替换临时文件，中途失败时据此回滚，
    # 避免留下半新半旧、能启动但白屏的坏目录。
    created = []
    renamed_aside = []  # [(bak, dest)]
    reboot_tmps = []
    reboot_dests = []  # 命中重启替换的 dest：成功路径不能删它们的备份
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
                    # 先让位、后写入：已存在的旧文件先纯改名让位（快），记入回滚集合，
                    # 避免第 N 个文件失败时前 N-1 个已被覆盖却无从恢复。
                    _make_way_for_existing(dest, log, renamed_aside)
                    try:
                        status, bak = _write_bytes_with_reboot_fallback(
                            dest, zf.read(rel), log, renamed_aside)
                    except Exception as exc:
                        raise _payload_file_error(dest, exc) from exc
                    if status == "written_aside":
                        renamed_aside.append((bak, dest))
                    elif status == "reboot":
                        needs_reboot = True
                        reboot_tmps.append(dest + _REBOOT_TMP_SUFFIX)
                        reboot_dests.append(dest)
                    else:
                        # 本次已把新版本写进 dest：清掉它的陈旧 .ycode-new，避免重启回滚
                        _drop_stale_reboot_tmp(dest, stale_news, log)
                    progress(idx / total * 85.0)
        else:
            for idx, (rel, dest) in enumerate(zip(names, destinations), 1):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                _ensure_no_reparse_components(install_dir, dest)
                if not os.path.lexists(dest):
                    created.append(dest)
                # 先让位、后写入：已存在的旧文件先纯改名让位（快），记入回滚集合，
                # 避免第 N 个文件失败时前 N-1 个已被覆盖却无从恢复。
                _make_way_for_existing(dest, log, renamed_aside)
                try:
                    status, bak = _copy_file_with_reboot_fallback(
                        os.path.join(src, rel), dest, log, renamed_aside)
                except Exception as exc:
                    raise _payload_file_error(dest, exc) from exc
                if status == "written_aside":
                    renamed_aside.append((bak, dest))
                elif status == "reboot":
                    needs_reboot = True
                    reboot_tmps.append(dest + _REBOOT_TMP_SUFFIX)
                    reboot_dests.append(dest)
                else:
                    # 本次已把新版本写进 dest：清掉它的陈旧 .ycode-new，避免重启回滚
                    _drop_stale_reboot_tmp(dest, stale_news, log)
                progress(idx / total * 85.0)
    except Exception as exc:
        # 回滚必须"尽力全量执行"：删除本次新建的文件 → 把让位的旧文件全部改回原名
        # → 清掉本次产生的 .ycode-new；单步失败只记日志，绝不中断后续恢复，否则会
        # 留下半新半旧的坏目录（前端 index.html 与 assets 哈希不配套即白屏）。
        for path in created:
            try:
                os.remove(path)
            except OSError:
                pass
        unrestored = []
        for bak, dest in renamed_aside:
            if not _restore_renamed(bak, dest, log):
                unrestored.append(dest)
        for tmp in reboot_tmps:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        if unrestored:
            # 把"哪些文件没能恢复"汇总进最终异常文案（保留原始异常对象与堆栈）
            note = ("\n⚠️ 以下文件未能恢复为旧版本，安装目录可能不一致，"
                    "请重跑安装包修复:\n" + "\n".join(unrestored))
            try:
                exc.args = (str(exc) + note,) + tuple(exc.args[1:])
            except Exception:
                pass
        raise
    if manage_manifest:
        # 顺序很重要：先按旧清单清理旧版已移除的文件（prune 依赖旧清单），再写新清单。
        _prune_removed_payload(install_dir, names, log)
        _write_install_manifest(install_dir, names)
    # 全部成功后才处理本次让位产生的备份。命中重启替换的 dest 是例外：它的新内容
    # 要等重启才生效，此时若把备份删掉，目标文件在重启前会整段缺失（Y-code.exe 会
    # 直接消失）。这类 dest 把备份改回原名，让旧版本继续可用，重启时由
    # MOVEFILE_REPLACE_EXISTING 换成新版。
    reboot_set = set(reboot_dests)
    for bak, dest in renamed_aside:
        if dest in reboot_set:
            if _restore_renamed(bak, dest, log):
                log(f"ℹ {os.path.basename(dest)} 将在系统重启后更新，当前保留旧版本。")
            continue
        try:
            os.remove(bak)
        except OSError:
            log(f"⚠️ 备份文件仍被占用，将在下次安装时清理: {os.path.basename(bak)}")
    log(f"✔ 核心程序包及依赖成功释放到: {install_dir}")
    if needs_reboot:
        log("⚠️ 部分文件因正在运行而延迟到系统重启后替换，重启后更新才会完全生效。")


# 安装前占用预检：只关心安装目录里的自有可执行组件（正在运行的进程会锁住它们）。
_OCCUPANCY_EXTS = (".exe", ".dll", ".pyd")
# 可能占用安装目录的自有进程名（安装目录里就这两个可执行文件）。
_OCCUPANCY_PROCESS_NAMES = ("Y-code.exe", "ycode.exe")
# 结束进程后重新检测的最长等待时间（秒）与轮询间隔。
_OCCUPANCY_RETRY_SECONDS = 10
_OCCUPANCY_POLL_INTERVAL = 0.5
# taskkill 子进程超时（秒），避免异常环境下卡死整个安装。
_TASKKILL_TIMEOUT_SECONDS = 10
# 共享冲突类 WinError：只有这两个才代表"文件被别的进程占着"。
# ERROR_ACCESS_DENIED(5) 多半只是没写权限（只读属性、未提权时看 Program Files），
# 不能和真正的占用混为一谈，否则会白弹一次询问、白结束一次进程。
_SHARING_VIOLATION_WINERRORS = (32, 33)
# 占用询问等待用户回答的超时（秒）。主线程若因弹窗异常或窗口被销毁而不再消费队列，
# 没有超时的等待会让安装线程永久假死（进度日志冻结，用户只能杀进程）。
_OCCUPANCY_ANSWER_TIMEOUT_SECONDS = 30


def _probe_locked_payload_files(install_dir):
    """返回安装目录中已存在、且当前被占用（不可读写打开）的 exe/dll/pyd 列表。

    以 os.open(O_RDWR) 尝试打开，句柄立即关闭。只读/读写方式打开不写入任何字节，
    不改变文件内容与 mtime，检测本身无副作用；占用判定细节见 _is_file_occupied。
    失败只返回已收集结果，绝不抛异常，检测不得导致安装失败。
    """
    locked = []
    try:
        for root, dirs, files in os.walk(install_dir, followlinks=False):
            # 剪掉 reparse point 目录，绝不越出 install_dir
            dirs[:] = [d for d in dirs if not _is_reparse_path(os.path.join(root, d))]
            for name in files:
                if not name.lower().endswith(_OCCUPANCY_EXTS):
                    continue
                path = os.path.join(root, name)
                if _is_file_occupied(path):
                    locked.append(path)
    except OSError:
        pass
    return locked


def _is_file_occupied(path):
    """判断 path 是否被其他进程占用（而不是当前用户没有写权限）。

    Windows 上 os.open(path, O_RDWR) 失败时抛的都是 PermissionError，但原因不同：
      * ERROR_SHARING_VIOLATION(32) / ERROR_LOCK_VIOLATION(33)：确实被占用；
      * ERROR_ACCESS_DENIED(5)：多半只是文件带只读属性、或当前用户无写权限（例如
        未提权时看到 Program Files 里的 dll）——把它当成"被占用"会白弹一次询问、
        白结束一次进程，还在重试窗口里空转十秒。
    所以共享冲突直接判占用；ACCESS_DENIED 再用只读方式复探一次，能读出来就说明只是
    没写权限。判定过程中的任何异常都按"未被占用"处理，预检不得影响安装。
    """
    try:
        fd = os.open(path, os.O_RDWR)
    except PermissionError as exc:
        if getattr(exc, "winerror", None) in _SHARING_VIOLATION_WINERRORS:
            return True
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            # 连只读都打不开：保守地当作被占用，把选择权交给用户
            return True
        else:
            os.close(fd)
            return False
    except OSError:
        return False
    else:
        os.close(fd)
        return False


def _run_taskkill(args, startupinfo=None):
    """以列表参数调用 taskkill（不做 shell 字符串插值），带超时；失败返回 False。"""
    try:
        proc = subprocess.run(
            ["taskkill", *args],
            startupinfo=startupinfo,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=_TASKKILL_TIMEOUT_SECONDS,
        )
        return getattr(proc, "returncode", 1) == 0
    except Exception:  # 子进程异常/超时都只记日志，绝不影响安装
        return False


def _taskkill(process_name, log, startupinfo=None, install_dir=None):
    """结束**属于本安装目录**的同名进程；返回是否真的结束了进程。

    为什么不用 `taskkill /F /IM`：它只按镜像名匹配，会把安装目录之外的另一个
    Y-code 实例（便携版、别处的旧版残留）一起结束。这里先用进程镜像全路径筛出
    属于本安装目录的 PID，再按 PID 结束；一个都筛不出来就不动手（宁可少杀，绝不
    误杀）。install_dir 为空时退化为按镜像名结束，保持对旧调用方的兼容。
    """
    if install_dir:
        pids = _iter_own_process_pids(process_name, install_dir)
        if not pids:
            log(f"ℹ 未发现属于本安装目录的 {process_name} 进程")
            return False
        ended = False
        for pid in pids:
            ended = _run_taskkill(["/F", "/PID", str(pid)], startupinfo) or ended
        if ended:
            log(f"✔ 已结束进程: {process_name} "
                f"(PID {', '.join(str(pid) for pid in pids)})")
            return True
        log(f"⚠️ 结束进程失败: {process_name} "
            f"(PID {', '.join(str(pid) for pid in pids)})")
        return False
    if _run_taskkill(["/F", "/IM", process_name], startupinfo):
        log(f"✔ 已结束进程: {process_name}")
        return True
    log(f"ℹ 未发现运行中的进程: {process_name}")
    return False


def _iter_own_process_pids(process_name, install_dir):
    """列出"镜像名匹配、且可执行文件位于 install_dir 内"的进程 PID。

    取不到镜像路径的进程一律不列入（宁可少杀，绝不误杀）。任何异常都只导致少杀，
    绝不影响安装流程。
    """
    try:
        root = os.path.normcase(os.path.realpath(install_dir)) + os.sep
    except OSError:
        return []
    pids = []
    for pid, image in _iter_process_images(process_name):
        if not image:
            continue
        try:
            if os.path.normcase(os.path.realpath(image)).startswith(root):
                pids.append(pid)
        except OSError:
            continue
    return pids


def _iter_process_images(process_name):
    """产出 (pid, 可执行文件全路径)——镜像名等于 process_name 的进程（仅 Windows）。

    用 Toolhelp 快照枚举 PID，再用 QueryFullProcessImageNameW 取全路径：这是为了把
    "结束进程"限定在本安装目录内。非 Windows 或任何异常都返回已收集结果。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    results = []
    try:
        kernel32 = ctypes.windll.kernel32
    except Exception:
        return results
    try:
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    except Exception:
        return results
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return results
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return results
        while True:
            if str(entry.szExeFile).lower() == process_name.lower():
                pid = int(entry.th32ProcessID)
                results.append(
                    (pid, _process_image_path(pid, PROCESS_QUERY_LIMITED_INFORMATION)))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    except Exception:
        pass
    finally:
        try:
            kernel32.CloseHandle(snapshot)
        except Exception:
            pass
    return results


def _process_image_path(pid, access):
    """取进程可执行文件全路径；取不到返回 ""（调用方据此保守跳过该进程）。"""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return ""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(access, False, pid)
    except Exception:
        return ""
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    except Exception:
        return ""
    finally:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass


def _resolve_occupied_files(install_dir, log, ask=None, startupinfo=None):
    """安装前占用预检：返回仍被占用的文件列表（尽力而为，绝不让预检导致安装失败）。

    ask 为询问回调，接收被占用文件列表，返回：
      'kill'     自动结束自有进程并继续（默认）
      'retry'    用户已手动关闭，重新检测
      'continue' 仍要继续（复制阶段的让位+回滚会保证不破坏目录）
    ask 为 None（无 UI / 非交互场景）时绝不阻塞：只记日志并直接返回，保持无人值守
    安装可用。检测无副作用（见 _probe_locked_payload_files）。
    """
    locked = _probe_locked_payload_files(install_dir)
    if not locked:
        return []
    log("⚠️ 检测到以下文件正被占用（可能正在运行旧版本）:\n" + "\n".join(locked))
    if ask is None:
        log("ℹ 非交互模式：跳过占用处理，直接继续安装（复制阶段会自动让位）。")
        return locked
    deadline = time.monotonic() + _OCCUPANCY_RETRY_SECONDS
    while locked:
        try:
            choice = ask(list(locked))
        except Exception as exc:  # 询问回调异常不得阻塞安装
            log(f"⚠️ 占用询问失败，直接继续安装: {exc}")
            return locked
        if choice == "continue":
            log("ℹ 已选择继续安装（不结束进程）。")
            return locked
        if choice == "retry":
            locked = _probe_locked_payload_files(install_dir)
            if not locked:
                log("✔ 占用已解除，继续安装。")
                return []
            log("⚠️ 仍有文件被占用，请关闭相关程序后重试:\n" + "\n".join(locked))
            continue
        # 默认 'kill'：结束自有进程后重新检测，最多重试到 _OCCUPANCY_RETRY_SECONDS
        killed = [name for name in _OCCUPANCY_PROCESS_NAMES
                  if _taskkill(name, log, startupinfo=startupinfo,
                               install_dir=install_dir)]
        while True:
            locked = _probe_locked_payload_files(install_dir)
            if not locked or time.monotonic() >= deadline:
                break
            time.sleep(_OCCUPANCY_POLL_INTERVAL)
        if not locked:
            log(f"✔ 占用已解除，继续安装。（已结束进程: {', '.join(killed) or '无'}）")
            return []
        log(f"⚠️ 结束进程后仍有文件被占用（已结束进程: {', '.join(killed) or '无'}）:\n"
            + "\n".join(locked))
    return locked


def run_install(mode, bundle_dir, options, startupinfo, log=print, progress=lambda p: None,
                ask_occupancy=None, interactive=None):
    """执行完整安装流程。任何致命错误抛 InstallError；非致命问题只记日志。

    ask_occupancy：占用询问回调（见 _resolve_occupied_files）；向导传入，无 UI 时为 None。
    interactive：显式指定是否交互；None 时默认 CLI 模式为非交互（绝不弹窗/阻塞）。
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
    # 安装前占用预检：对安装目录中已存在的 exe/dll/pyd 逐个探测是否被占用（无副作用）。
    # 交互式向导可询问用户自动结束自有进程；非交互（CLI 模式 / 无 UI 回调 / 显式
    # interactive=False）只记日志并继续，绝不弹窗阻塞，保持无人值守安装可用。
    if interactive is None:
        interactive = mode != "cli"
    _resolve_occupied_files(install_dir, log,
                            ask=ask_occupancy if interactive else None,
                            startupinfo=startupinfo)
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
            # 不再内嵌独立 CLI 安装器（曾把 169MB 载荷重复打进主安装包）：
            # 补装 = 重跑本向导勾选「命令行版」，载荷两种 exe 都带，等效且省一半体积。
            log("ℹ 本次未安装命令行版；如需补装，重新运行本安装向导并勾选「命令行版」即可。")
        progress(90.0)

        # 3. 桌面快捷方式 "星瑶code" → Y-code.exe（按用户勾选；machine 范围放公共桌面）
        if options.create_desktop_shortcut:
            try:
                desktop_dir = get_common_desktop_dir() if scope == "machine" else get_desktop_dir()
                # 升级场景：先清掉旧版本遗留的同桌面 [Y-code.lnk]，
                # 避免桌面上同时出现「Y-code」和「星瑶code」两个图标。
                legacy_lnk = os.path.join(desktop_dir, "Y-code.lnk")
                if os.path.exists(legacy_lnk):
                    try:
                        os.remove(legacy_lnk)
                        log("ℹ 已移除旧版遗留的 [Y-code] 桌面快捷方式，改用新的 [星瑶code]。")
                    except Exception as e:
                        log(f"⚠️ 清理旧版桌面快捷方式失败（不影响安装）: {e}")
                shortcut_path = os.path.join(desktop_dir, DESKTOP_SHORTCUT_NAME)
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
# 校验失败提示用的红色（与 _show_error 里的失败色一致，统一"危险/错误"语义）
COLOR_DANGER = "#c0392b"
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

# ---------------------------------------------------------------------------
# 安装向导展示的许可协议（摘要版）
#
# 安装向导每次安装都展示本页并要求主动勾选（默认不勾选），不落盘任何状态；
# 软件内的完整条款（8 章）在「设置 → 法律 → 免责条款」，运行时首次使用由
# 应用内确认弹窗记录同意（~/.ycode/disclaimer_consent.json）。
# 版本号需与 core/disclaimer_consent.py 的 DISCLAIMER_VERSION 保持同步。
# ---------------------------------------------------------------------------
LICENSE_VERSION = "v1.0"
LICENSE_UPDATED_AT = "2026-09-11"
LICENSE_AGREEMENT_TEXT = """\
星瑶Code 许可协议与免责条款（摘要）

一、软件性质与许可
1. 本软件是在你本地设备运行的 AI 编程辅助工具，自身不提供、不运营任何生成式人工智能
   模型服务；模型服务由你自行选择、配置并直接与其通信。
2. 本软件基于 MIT 许可证开源发布，你可依该许可证自由使用、修改与分发。

二、AI 生成内容
3. 大语言模型基于概率机制生成内容，可能不准确、不完整或已过时。本软件不对生成内容的
   真实性、准确性、完整性、合法性或适用性作任何保证。
4. 生成内容不构成法律、医疗、财务、税务等任何专业领域意见，不应作为相关决策的唯一依据。

三、你的责任
5. 在采纳、执行或分发任何生成内容前，你应自行审查、验证与测试，并对最终结果负全部责任。
6. 本软件具备文件读写、命令执行、网页操作与电脑操控能力，此类操作可能修改或删除本机
   文件、改变系统状态或产生外部影响；请在执行前确认操作意图与影响范围，并自行做好
   重要数据的备份。

四、数据与隐私
7. 配置、会话记录与记忆数据默认存储于你的本地设备，本软件不主动向开发者上传上述数据。
8. 使用过程中，会话内容、代码与附件将按你的配置直接发送至你所选的模型服务提供者，
   其收集、存储、使用与跨境传输行为适用该服务提供者的隐私政策；请自行评估是否适宜
   通过所选服务处理敏感信息、商业秘密或个人数据。

五、合规使用
9. 你不得利用本软件从事违反法律法规的活动，不得生成、传播违法有害信息，不得侵害他人
   合法权益，不得规避、攻击或破坏计算机信息系统。

六、责任限制（与你有重大利害关系的条款，请特别注意）
10. 在法律允许的最大范围内，本软件的著作权人及贡献者不对因使用或无法使用本软件而
    造成的任何损失承担责任，包括但不限于数据丢失、设备损坏、业务中断、利润损失及
    第三方索赔等直接或间接损失。
11. 本软件按「现状」提供，不作任何明示或默示的担保，包括但不限于适销性、特定用途
    适用性及不侵权的担保。

七、条款变更
12. 本条款可能随软件版本更新而调整，更新后的条款随新版本一并发布。

完整条款（共 8 章）可在安装完成后于软件内「设置 → 法律 → 免责条款」查看。
"""



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

        self.title("星瑶Code 安装")
        self.resizable(False, False)
        self.configure(bg=COLOR_CONTENT)
        # 右上角 X 关闭也必须走「安装成功才清理安装包」的统一出口，
        # 否则用户用 X 关掉向导就漏掉了清理调度（安装包会一直留在缓存目录）。
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)
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
        # 完成页「立即运行」复选框（仅 full 模式展示）；安装结果状态供
        # 「完成」按钮判断是否需要在退出前启动程序
        self.var_launch = tk.BooleanVar(value=True)
        # 许可协议勾选：默认不勾选——默认勾选的"同意"在司法实践中证明力弱，
        # 也不符合《民法典》第 496 条对显著提示的要求
        self.var_license_agree = tk.BooleanVar(value=False)
        self._install_success = False
        self._install_dir = None
        # 安装包自清理只调度一次（安装成功 + 向导退出时触发，见
        # _maybe_schedule_installer_cleanup）；幂等标志避免用户连点"完成"
        # 或"完成"与窗口关闭协议叠加时启动多个删除进程
        self._installer_cleanup_scheduled = False
        # 选项页安装路径即时校验状态：_dir_invalid 为真时禁用"下一步/安装"，
        # _dir_note 用于展示范围切换自动纠正路径的说明（见 _refresh_dir_validation）
        self._dir_hint = None
        self._dir_invalid = False
        self._dir_note = ""

        # 页面导航流程：cli 模式没有“安装用户”页（CLI 始终按当前用户安装）
        # 许可协议页紧跟欢迎页：每次安装都展示，须主动勾选才能继续
        self._flow = (["welcome", "license", "scope", "options", "installing", "finish"]
                      if mode == "full" else
                      ["welcome", "license", "options", "installing", "finish"])

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

        tk.Label(sidebar, text="星瑶Code", font=FONT_BRAND, fg=SIDEBAR_TEXT,
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
                                      width=8, command=self._on_cancel_or_finish)
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
        self._build_license_page()
        if self.mode == "full":
            self._build_scope_page()
        self._build_options_page()
        self._build_installing_page()
        self._build_finish_page()

    _BANNERS = {
        "welcome": ("欢迎使用 星瑶Code 安装向导", "本向导将引导您完成 星瑶Code 的安装"),
        "license": ("许可协议与免责条款", "请阅读以下条款，勾选同意后继续安装"),
        "scope": ("选择安装用户", "选择要为哪些用户安装 星瑶Code"),
        "options": ("选择安装位置与选项", "选择 星瑶Code 的安装文件夹与附加选项"),
        "installing": ("正在安装", "星瑶Code 正在安装，请等候。"),
        "finish": ("安装完成", "星瑶Code 安装向导已结束"),
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
            # 许可协议页：未勾选同意不得继续（默认不勾选，须用户主动确认）
            if name == "license" and not self.var_license_agree.get():
                nxt.configure(state="disabled")
            # 选项页：machine 范围下路径不在 Program Files 下时不得继续，
            # 避免用户点"安装"后在安装阶段才收到 _validate_install_target 的报错
            if name == "options" and self._dir_invalid:
                nxt.configure(state="disabled")

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
        self._label(page, "星瑶Code", FONT_TITLE).place(relx=0.5, y=80, anchor="center")
        self._label(page, f"AI 编程助手   v{self.version}", FONT_PAGE_SUB, COLOR_TEXT_DIM).place(
            relx=0.5, y=112, anchor="center")
        desc = (
            "本向导将在您的电脑上安装 星瑶Code 图形界面版 (GUI)，\n"
            "接下来您可以选择安装用户、安装位置，\n"
            "以及是否附带安装命令行版 (CLI)。"
            if is_full else
            "本向导将在您的电脑上安装 星瑶Code 命令行版 (CLI)，\n"
            "安装完成后可在终端中直接使用 ycode 命令。"
        )
        self._label(page, desc, FONT_TEXT, justify="center").place(relx=0.5, y=190, anchor="center")
        self._label(page, "单击“下一步”继续，或单击“取消”退出安装向导。",
                    FONT_TEXT, COLOR_TEXT_DIM).place(relx=0.5, y=265, anchor="center")
        if self.previous_install:
            self._label(page, f"检测到已安装的 星瑶Code：{self.previous_install}，本次将覆盖安装。",
                        FONT_TEXT, "#8a6d1a").place(relx=0.5, y=305, anchor="center")

    def _build_license_page(self):
        """许可协议页：每次安装都展示，默认不勾选，未勾选不得进入下一步。

        只做展示与确认，不落盘任何状态——运行时的同意记录由软件内
        「免责条款」确认弹窗写入 ~/.ycode/disclaimer_consent.json。
        """
        page = self._page("license")
        self._label(
            page,
            f"继续安装即表示你已阅读并同意以下条款"
            f"（摘要版 {LICENSE_VERSION}，更新日期 {LICENSE_UPDATED_AT}）。",
            FONT_PAGE_SUB, COLOR_TEXT_DIM,
        ).place(x=40, y=6)

        text = tk.Text(
            page, font=FONT_DIM, bg="#FFFFFF", fg=COLOR_TEXT,
            bd=1, relief="solid", highlightthickness=0,
            wrap="word", padx=10, pady=8, state="normal",
        )
        text.insert("1.0", LICENSE_AGREEMENT_TEXT)
        text.configure(state="disabled")
        text.place(x=40, y=36, width=516, height=196)

        scroll = tk.Scrollbar(page, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.place(x=558, y=36, height=196)

        tk.Checkbutton(
            page, text="我已阅读并同意上述许可协议与免责条款",
            variable=self.var_license_agree, command=self._update_nav_buttons,
            bg=COLOR_CONTENT, fg=COLOR_TEXT, font=FONT_TEXT_BOLD,
            selectcolor=COLOR_CONTENT, activebackground=COLOR_CONTENT,
            activeforeground=COLOR_TEXT, cursor="hand2",
            highlightthickness=0, bd=0,
        ).place(x=40, y=246)

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
            self._label(page, f"检测到已安装的 星瑶Code：{self.previous_install}\n"
                              "重装将沿用原安装范围，如需变更请先卸载。",
                        FONT_TEXT, "#8a6d1a", justify="left").place(x=50, y=200)

    def _on_scope_change(self):
        # 重装锁定路径：行为保持现状不变（沿用原安装位置，不由范围切换改写）
        if self.previous_install:
            return
        scope = self.var_scope.get()
        cur = self.var_dir.get().strip()
        user_default = default_install_dir("user")
        machine_default = default_install_dir("machine")
        defaults = {user_default, machine_default}
        self._dir_note = ""
        if scope == "machine":
            # 切到"所有用户"：路径必须位于 Program Files 子目录下。旧逻辑只在路径为空
            # 或正好等于两个默认值之一时才跟随切换，用户此前浏览/手填过路径时不会纠正，
            # 直到安装阶段 _validate_install_target 才抛错——用户看到的是莫名其妙的
            # "安装失败"。这里当场纠正为 machine 默认路径并提示"已自动改为 …"。
            if not cur or cur in defaults or not _is_under_program_files(cur):
                if cur and cur != machine_default:
                    self._dir_note = (
                        f"已自动改为 {machine_default}（为所有用户安装需位于 Program Files 下）。")
                self.var_dir.set(machine_default)
        elif not cur or cur in defaults:
            # 切回"仅为我安装"：路径若为空或仍停留在 machine 默认值则改回 user 默认
            self.var_dir.set(user_default)
        # var_dir 值未变化时 trace 不会触发，这里显式刷新一次以保证提示/按钮同步
        self._refresh_dir_validation()

    def _refresh_dir_validation(self):
        """按当前范围即时校验安装路径，刷新提示文字与"下一步/安装"按钮状态。

        machine 范围下路径不在 Program Files 子目录时显示红字并置 _dir_invalid，
        由 _update_nav_buttons 禁用继续；路径合法时恢复，并展示 _dir_note（范围
        切换自动纠正路径的说明）。校验判定与引擎共用 _is_under_program_files，
        保证界面结论与安装阶段一致。
        """
        if self._dir_hint is None:
            return
        scope = "user" if self.mode == "cli" else self.var_scope.get()
        cur = self.var_dir.get().strip()
        self._dir_invalid = bool(
            scope == "machine" and cur and not _is_under_program_files(cur))
        if self._dir_invalid:
            self._dir_hint.configure(
                text="⚠️ 为所有用户安装时，路径必须位于 Program Files 的子目录中。",
                fg=COLOR_DANGER)
        else:
            self._dir_hint.configure(text=self._dir_note, fg="#8a6d1a")
        self._update_nav_buttons()

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
            self._label(page, "检测到已安装的 星瑶Code，将覆盖安装到原位置。",
                        FONT_PAGE_SUB, "#8a6d1a").place(x=40, y=108)

        # 路径输入框即时校验：machine 范围下路径不在 Program Files 子目录时红字提示
        # 并禁用"下一步/安装"（见 _refresh_dir_validation），避免用户填了非法路径、
        # 点安装后才在安装阶段收到引擎的"安装失败"。锁定路径时无需校验。
        if not locked:
            self._dir_hint = self._label(page, "", FONT_DIM, COLOR_TEXT_DIM)
            self._dir_hint.place(x=40, y=110)
            self.var_dir.trace_add("write", lambda *_: self._refresh_dir_validation())

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
        # 「立即运行」复选框：先建好不摆放，安装成功且为 full 模式时才显示
        self._launch_check = tk.Checkbutton(
            page, text="立即运行 星瑶Code",
            variable=self.var_launch,
            bg=COLOR_CONTENT, fg=COLOR_TEXT, font=FONT_TEXT,
            selectcolor=COLOR_CONTENT, activebackground=COLOR_CONTENT,
            activeforeground=COLOR_TEXT, cursor="hand2",
            highlightthickness=0, bd=0)

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
            messagebox.showwarning("星瑶Code 安装", "请先选择有效的安装位置。", parent=self)
            return
        # 修复 M47：user 范围安装目录无位置约束，可装进任意已有非空目录；
        # 目标非空且不像既有 Y-code 安装时先警告（重装/提权重启前已确认过，跳过）。
        if (not self.elevated_options and not self.previous_install
                and _is_unrecognized_non_empty_dir(install_dir)):
            if not messagebox.askyesno(
                "星瑶Code 安装",
                f"所选目录已存在且不为空：\n{install_dir}\n\n"
                "继续安装会把 星瑶Code 文件写入该目录。为安全起见，建议改用空目录。\n\n"
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
                "星瑶Code 安装",
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
                ask_occupancy=self._ask_occupancy,
            )
            self._ui_queue.put(("done", result))
        except InstallError as e:
            self._ui_queue.put(("error", str(e)))
        except Exception as e:  # 兜底：引擎外的意外异常也不能让向导卡死
            self._ui_queue.put(("error", f"安装过程出现意外错误: {e}"))

    def _ask_occupancy(self, locked_files):
        """安装线程调用：把占用询问投递到主线程弹窗，阻塞等待用户选择后返回。

        Tk 非线程安全，弹窗必须在主线程执行；这里用 Event 同步等待，返回
        'kill' / 'retry' / 'continue'。回调异常时返回"继续"，绝不阻塞安装。
        """
        done = threading.Event()
        answer = {}

        def _respond(choice):
            answer["choice"] = choice
            done.set()

        self._ui_queue.put(("ask_occupancy", (list(locked_files), _respond)))
        # 等待必须带超时：主线程若因弹窗异常、或窗口被销毁而不再消费队列，无超时的
        # wait 会让安装线程永久假死（进度与日志冻结，用户只能杀进程）。
        if not done.wait(_OCCUPANCY_ANSWER_TIMEOUT_SECONDS):
            print("[setup] 占用询问超时未获答复，按“继续安装”处理")
            return "continue"
        return answer.get("choice", "continue")

    def _prompt_occupancy(self, locked_files):
        """主线程弹出"文件被占用"三选对话框，返回 'kill' / 'retry' / 'continue'。

        默认（关闭窗口 / 回车）为「自动结束进程并继续」，与向导"少打断用户"的取向
        一致；「仍要继续（不推荐）」排在最后。选①后由引擎重新检测并最多重试到 10 秒。
        """
        dialog = tk.Toplevel(self)
        dialog.title("星瑶Code 安装")
        dialog.configure(bg=COLOR_CONTENT)
        dialog.resizable(False, False)
        dialog.transient(self)
        try:
            dialog.grab_set()
        except tk.TclError:
            # 窗口尚未映射时 grab_set 会抛 TclError（提权路径下向导刚起来就开装）。
            # 拿不到模态抓取不影响三选逻辑，但异常冒泡出去会让消息泵停摆、安装线程
            # 只能空等超时，所以必须在这里就地吞掉。
            pass
        result = {"choice": "kill"}
        shown = "\n".join(locked_files[:8]) + ("\n…" if len(locked_files) > 8 else "")
        self._label(
            dialog,
            "检测到以下文件正被占用（可能正在运行旧版本）:\n\n"
            f"{shown}\n\n"
            "可以自动结束 星瑶Code 的进程后继续安装。",
            FONT_TEXT, COLOR_TEXT, justify="left", anchor="w", wraplength=420,
        ).pack(padx=20, pady=(18, 10), anchor="w")

        def _choose(choice):
            result["choice"] = choice
            dialog.destroy()

        btns = tk.Frame(dialog, bg=COLOR_CONTENT)
        btns.pack(fill="x", padx=20, pady=(0, 18))
        ttk.Button(btns, text="自动结束进程并继续", style="Brand.TButton",
                   command=lambda: _choose("kill")).pack(side="left")
        ttk.Button(btns, text="我已手动关闭，重新检测", style="Ghost.TButton",
                   command=lambda: _choose("retry")).pack(side="left", padx=8)
        ttk.Button(btns, text="仍要继续（不推荐）", style="Ghost.TButton",
                   command=lambda: _choose("continue")).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", lambda: _choose("kill"))
        # 居中到向导窗口
        dialog.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        try:
            self.wait_window(dialog)
        except tk.TclError:
            # 弹窗被外部销毁（例如用户关掉向导）：按已记录的选择返回，绝不冒泡异常
            pass
        return result["choice"]

    def _drain_ui_queue(self):
        """主线程消息泵：逐条消费安装线程的消息。

        单条消息处理失败绝不能停掉整个泵：after 一旦不再重挂，日志/进度会永久冻结，
        占用询问的等待也会一直空等（向导假死）。所以每条消息独立兜底、after 放 finally。
        """
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                try:
                    if kind == "log":
                        self._append_log(payload)
                    elif kind == "progress":
                        self._set_progress(payload)
                    elif kind == "ask_occupancy":
                        # 安装线程投递的占用询问：在主线程弹窗后把选择回传给安装线程
                        locked_files, respond = payload
                        respond(self._prompt_occupancy(locked_files))
                    elif kind == "done":
                        self._show_finish(payload)
                    elif kind == "error":
                        self._show_error(payload)
                except Exception:
                    print(f"[setup] 处理安装向导消息失败: {kind}")
                    if kind == "ask_occupancy":
                        # 弹窗失败也必须给安装线程一个答复，否则它只能空等超时
                        try:
                            payload[1]("continue")
                        except Exception:
                            pass
        except queue.Empty:
            pass
        finally:
            try:
                self.after(50, self._drain_ui_queue)
            except Exception:
                pass  # 窗口已销毁：消息泵自然结束

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
        self._install_success = True
        self._install_dir = install_dir
        ycode_user_dir = os.path.join(os.path.expanduser("~"), ".ycode")
        if self.mode == "full":
            hints = ["双击桌面上的 [Y-code] 快捷方式，或到安装目录启动 Y-code.exe。"]
            if result["installed_cli"]:
                hints.append("打开新的命令行窗口 (CMD/PowerShell)，输入 ycode 使用命令行版。")
            else:
                hints.append("本次未安装命令行版；如需要，重新运行本安装向导并勾选「命令行版」即可补装。")
        else:
            hints = ["打开任意全新的命令行窗口 (CMD/PowerShell)，输入 ycode 敲回车即可开始使用。"]
        self._finish_detail.configure(
            text=f"安装路径: {install_dir}\n配置文件夹: {ycode_user_dir}"
        )
        self._finish_hint.configure(text="\n".join(hints))
        # 仅图形界面版安装成功时提供「立即运行」；CLI 模式装的是终端命令，无可启动窗口
        if self.mode == "full":
            self._launch_check.place(relx=0.5, y=340, anchor="center")
        self.show_page("finish")

    def _show_error(self, message):
        self._install_success = False
        self._install_dir = None
        self._launch_check.place_forget()
        self._finish_icon.configure(text="🚨")
        self._finish_title.configure(text="安装失败", fg="#c0392b")
        self._finish_detail.configure(text=message)
        self._finish_hint.configure(text="您可以关闭向导后重试，或更换安装位置。")
        self.show_page("finish")

    def _maybe_schedule_installer_cleanup(self):
        """安装成功且向导即将退出时，调度删除本次运行的安装包（幂等）。

        只在"安装成功"这一条路径上清理：
          * self._install_success 为 False（用户中途取消、安装失败）→ 直接返回，
            安装包原样保留，用户可再次运行，无需重新下载；
          * 非 frozen（开发态 `python ycode_setup.py`）→ 不清理，因为
            sys.executable 是 python.exe，删掉会破坏用户的 Python 环境；
          * 已调度过 → 不重复调度。
        调度失败只影响"安装包能不能自动删掉"，绝不影响安装流程本身。
        """
        if self._installer_cleanup_scheduled:
            return
        if not self._install_success:
            return
        if not getattr(sys, "frozen", False):
            return
        self._installer_cleanup_scheduled = True
        schedule_installer_self_cleanup(sys.executable, self.startupinfo)

    def _on_window_close(self):
        """右上角 X 关闭：与底部按钮走同一套「安装成功才清理安装包」逻辑。

        安装进行中拒绝关闭：安装线程是 daemon，窗口销毁后主循环退出会把它硬杀，
        _copy_payload 的失败回滚根本来不及执行——那正是"半新半旧、能启动但白屏"
        坏目录的成因。等安装进入成功/失败页再关，语义与安装页禁用取消按钮一致。
        """
        if self._current_page == "installing":
            messagebox.showinfo(
                "星瑶Code 安装",
                "安装正在进行，请等待完成后再关闭窗口。",
                parent=self,
            )
            return
        self._maybe_schedule_installer_cleanup()
        self.destroy()

    def _on_cancel_or_finish(self):
        """底部「取消/完成」按钮：安装成功且勾选「立即运行」时先启动程序再退出。"""
        if (self._current_page == "finish" and self._install_success
                and self.mode == "full" and self.var_launch.get()
                and self._install_dir):
            launch_gui_app(self._install_dir, self.startupinfo)
        # 安装成功才清理安装包（失败/取消时保留，供用户重跑）
        self._maybe_schedule_installer_cleanup()
        self.destroy()


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
