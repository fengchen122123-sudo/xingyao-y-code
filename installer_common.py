"""installer_common.py
安装向导 (ycode_setup.py) 与卸载向导 (ycode_uninstall.py) 共享的基础工具函数。

独立成模块的目的：卸载向导只需要这几个零依赖函数（os/winreg/ctypes），
历史上它从 ycode_setup.py 导入，会把安装向导整棵依赖树（tkinter 界面、
PIL 横幅处理等）一起拖进卸载器二进制。改从本模块导入后，卸载器不再
链接 ycode_setup，体积显著缩小。

约束：本模块只允许导入标准库，且不得反向导入 ycode_setup / ycode_uninstall。
"""
import os
import winreg


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


def is_user_an_admin():
    """当前进程是否已拥有管理员权限。"""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _is_reparse_path(path):
    """Return whether an existing path redirects filesystem traversal."""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))
