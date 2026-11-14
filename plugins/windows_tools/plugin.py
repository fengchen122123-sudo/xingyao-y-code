"""windows_tools：Windows 系统能力插件（进程 / 剪贴板 / 注册表 / 通知）。

设计要点（对齐 docs/2026-11-09-windows-mcp-parity-design.md §5.3）：

- 平台门控：``register()`` 在非 Windows 或 CLI 轻量模式下直接返回，不注册任何
  工具——macOS/Linux 上不白占 tools schema 字节。
- 模块顶层只导入标准库 os / sys：psutil、ctypes、win32api 之类一律在函数内惰性
  导入，保证本文件在任何平台都能安全 exec，不会因缺依赖而让插件加载失败。
- 错误码英文小写下划线、message 中文；进程不存在 / 剪贴板被占用一律返回
  ToolResult(is_error=True) 的明确提示，绝不向上抛栈。
- 只依赖标准库与 psutil（后者已在 requirements.txt；通知额外用 pywin32 的
  win32gui，同样只在函数内惰性导入，缺依赖时返回明确错误而不是加载失败）。
- 只读 / 写入拆成独立工具：``ToolSpec.requires_permission`` 是工具级静态标记，
  双模式工具无法表达「list/get 免审批、kill/set 要审批」，因此进程与剪贴板各拆成
  ``windows_process_list`` / ``windows_process_kill``、
  ``windows_clipboard_get`` / ``windows_clipboard_set``。旧的双模式实现函数
  （``_process_tool`` / ``_clipboard_tool``）保留为底层逻辑，只改注册与入口。
"""

from __future__ import annotations

import os
import sys

from core.tool_protocol import ToolResult, ToolSpec

# 剪贴板常量（wingdi/winuser）：CF_UNICODETEXT = 13；
# GMEM_MOVEABLE(0x0002) | GMEM_ZEROINIT(0x0040) = 0x0042。
_CF_UNICODETEXT = 13
_GMEM_FLAGS = 0x0042
_OPEN_CLIPBOARD_ATTEMPTS = 5
_OPEN_CLIPBOARD_INTERVAL = 0.03
_CLIPBOARD_TEXT_LIMIT = 20000

# ── 注册表常量（winreg）：输出上限与根键白名单。
_REGISTRY_OUTPUT_LIMIT = 4000
_REGISTRY_LIST_VALUE_LIMIT = 200
_REGISTRY_LIST_SUBKEY_LIMIT = 200
_REGISTRY_ROOTS = ("HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE", "HKEY_CLASSES_ROOT")
_REGISTRY_VIEWS = ("default", "64", "32")

# 值类型编号 → 名称（winreg.REG_* 的数值，静态表避免顶层 import winreg）。
_REGISTRY_TYPE_NAMES = {
    0: "REG_NONE",
    1: "REG_SZ",
    2: "REG_EXPAND_SZ",
    3: "REG_BINARY",
    4: "REG_DWORD",
    5: "REG_DWORD_BIG_ENDIAN",
    6: "REG_LINK",
    7: "REG_MULTI_SZ",
    8: "REG_RESOURCE_LIST",
    9: "REG_FULL_RESOURCE_DESCRIPTOR",
    10: "REG_RESOURCE_REQUIREMENTS_LIST",
    11: "REG_QWORD",
}

# 通知常量（winuser）：MB_OK | MB_ICONINFORMATION。
_NOTIFY_TITLE_LIMIT = 64
_NOTIFY_MESSAGE_LIMIT = 256
_MB_OK = 0x00000000
_MB_ICONINFORMATION = 0x00000040

# 关键系统进程（小写名）：结束它们会直接导致蓝屏 / 掉登录 / 服务全停。
_PROTECTED_NAMES = frozenset({
    "system",
    "system idle process",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
})

# PID 0 = System Idle Process，PID 4 = System（Windows 内核伪进程，杀不掉也不该杀）。
_PROTECTED_PIDS = frozenset({0, 4})

_SORT_KEYS = ("cpu", "memory", "name")
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500


# ── 通用小工具 ──────────────────────────────────────────────


def _error(code: str, message: str) -> ToolResult:
    """统一错误格式：英文小写下划线错误码 + 中文说明。"""
    return ToolResult(content=f"{code}: {message}", is_error=True)


def _protected_reason(pid_value: int, name_value: str) -> str:
    """返回拒绝结束该进程的原因；空串表示可以结束。"""
    lowered = str(name_value or "").strip().lower()
    if lowered in _PROTECTED_NAMES:
        return "受保护的 Windows 关键系统进程"
    if pid_value in _PROTECTED_PIDS:
        return "受保护的系统内核伪进程（PID 0 / 4）"
    if pid_value == os.getpid():
        return "当前进程自身"
    if pid_value and pid_value == os.getppid():
        return "当前进程的父进程"
    return ""


def _iter_processes():
    """惰性产出 (proc, pid, name, rss_bytes)；跳过无权限 / 已退出的条目。"""
    import psutil

    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = proc.info or {}
            pid_value = int(info.get("pid") or 0)
            name_value = str(info.get("name") or "")
            memory_info = info.get("memory_info")
            rss = float(getattr(memory_info, "rss", 0) or 0)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
        yield proc, pid_value, name_value, rss


def _cpu_percent(proc) -> float:
    """进程 CPU 百分比；首次采样（无历史基准）返回 0.0。"""
    try:
        return float(proc.cpu_percent(None))
    except Exception:
        return 0.0


# ── windows_process ────────────────────────────────────────


def _list_processes(keyword: str, sort_by: str, limit: int) -> ToolResult:
    rows = []
    for proc, pid_value, name_value, rss in _iter_processes():
        rows.append({
            "proc": proc,
            "pid": pid_value,
            "name": name_value,
            "memory_mb": rss / (1024 * 1024),
        })

    needle = str(keyword or "").strip().lower()
    if needle:
        rows = [
            row for row in rows
            if needle in row["name"].lower() or needle == str(row["pid"])
        ]

    if sort_by == "name":
        rows.sort(key=lambda row: (row["name"].lower(), row["pid"]))
    elif sort_by == "cpu":
        for row in rows:
            row["cpu"] = _cpu_percent(row["proc"])
        rows.sort(key=lambda row: row["cpu"], reverse=True)
    else:  # memory（默认）
        rows.sort(key=lambda row: row["memory_mb"], reverse=True)

    total = len(rows)
    shown = rows[:limit]

    order_text = {"cpu": "CPU 占用降序", "memory": "内存占用降序", "name": "名称升序"}[sort_by]
    lines = [
        "Windows 进程列表（命中 %d 个，%s，显示前 %d 个）" % (total, order_text, len(shown)),
        "",
        "%7s  %9s  %s" % ("PID", "内存(MB)", "名称"),
    ]
    for row in shown:
        lines.append(
            "%7d  %9.1f  %s" % (row["pid"], row["memory_mb"], row["name"] or "<未知>")
        )
    if total > len(shown):
        lines.append("")
        lines.append("仅显示前 %d 个，可调小 filter 关键字或增大 limit（上限 %d）。" % (len(shown), _MAX_LIMIT))
    if sort_by == "cpu":
        lines.append("")
        lines.append("说明：CPU 百分比是两次采样之间的瞬时占用，进程首次被统计时为 0；连续调用一次更准。")
    if not shown:
        lines.append("")
        lines.append("没有匹配的进程。")
    return ToolResult(content="\n".join(lines))


def _kill_processes(pid_value, name_value, force: bool) -> ToolResult:
    import psutil

    has_pid = pid_value is not None
    target_name = str(name_value or "").strip().lower()
    if not has_pid and not target_name:
        return _error("windows_invalid_arguments", "mode=kill 需要提供 pid 或 name 之一")

    candidates = []
    for proc, row_pid, row_name, _rss in _iter_processes():
        if has_pid:
            if row_pid != int(pid_value):
                continue
        else:
            lowered = row_name.strip().lower()
            # 允许省略 .exe（chrome 等价 chrome.exe）
            if lowered != target_name and lowered != target_name + ".exe":
                continue
        candidates.append((proc, row_pid, row_name))

    if not candidates:
        if has_pid:
            return _error(
                "windows_process_not_found",
                "未找到 PID 为 %s 的进程（可能已经退出）" % pid_value,
            )
        return _error(
            "windows_process_not_found",
            "未找到名为 %s 的进程（可用 mode=list 配合 filter 确认进程名）" % name_value,
        )

    method = "kill（强制）" if force else "terminate（请求正常退出）"
    done = []
    failures = []
    protected_hits = 0
    for proc, row_pid, row_name in candidates:
        reason = _protected_reason(row_pid, row_name)
        if reason:
            protected_hits += 1
            failures.append("%s（PID %d）拒绝结束：%s" % (row_name or "<未知>", row_pid, reason))
            continue
        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
        except psutil.NoSuchProcess:
            failures.append("%s（PID %d）已自行退出" % (row_name or "<未知>", row_pid))
        except psutil.AccessDenied:
            failures.append("%s（PID %d）权限不足（拒绝访问）" % (row_name or "<未知>", row_pid))
        except Exception as exc:  # ctypes / OS 层异常统一归一，不向上抛栈
            failures.append("%s（PID %d）失败：%s" % (row_name or "<未知>", row_pid, exc))
        else:
            done.append("%s（PID %d）" % (row_name or "<未知>", row_pid))

    if not done:
        code = "windows_process_protected" if protected_hits == len(failures) else "windows_process_failed"
        return _error(code, "；".join(failures))

    lines = ["已结束 %d 个进程（%s）：" % (len(done), method)]
    lines.extend("- " + item for item in done)
    if failures:
        lines.append("")
        lines.append("未处理 %d 个：" % len(failures))
        lines.extend("- " + item for item in failures)
    return ToolResult(content="\n".join(lines))


def _process_tool(
    mode: str = "list",
    filter: str = "",  # noqa: A002 - 参数名由 schema 决定，必须叫 filter
    sort_by: str = "memory",
    limit: int = _DEFAULT_LIMIT,
    pid: int | None = None,
    name: str = "",
    force: bool = False,
) -> ToolResult:
    """windows_process 实现：mode=list 列表 / mode=kill 结束。"""
    normalized_mode = str(mode or "list").strip().lower()
    if normalized_mode == "list":
        normalized_sort = str(sort_by or "memory").strip().lower()
        if normalized_sort not in _SORT_KEYS:
            return _error(
                "windows_invalid_arguments",
                "sort_by 只支持 cpu / memory / name，收到 %s" % sort_by,
            )
        try:
            normalized_limit = int(limit)
        except (TypeError, ValueError):
            normalized_limit = _DEFAULT_LIMIT
        normalized_limit = max(1, min(normalized_limit, _MAX_LIMIT))
        return _list_processes(filter, normalized_sort, normalized_limit)

    if normalized_mode == "kill":
        return _kill_processes(pid, name, bool(force))

    return _error("windows_invalid_mode", "mode 只支持 list / kill，收到 %s" % mode)


def _process_list_tool(
    filter: str = "",  # noqa: A002 - 参数名由 schema 决定，必须叫 filter
    sort_by: str = "memory",
    limit: int = _DEFAULT_LIMIT,
) -> ToolResult:
    """windows_process_list 入口：只读列出进程（复用 _process_tool 的 list 分支）。"""
    return _process_tool(mode="list", filter=filter, sort_by=sort_by, limit=limit)


def _process_kill_tool(
    pid: int | None = None,
    name: str = "",
    force: bool = False,
) -> ToolResult:
    """windows_process_kill 入口：结束进程（复用 _process_tool 的 kill 分支）。"""
    return _process_tool(mode="kill", pid=pid, name=name, force=force)


# ── windows_clipboard ──────────────────────────────────────


def _clipboard_api():
    """惰性构造 user32/kernel32 句柄并显式声明签名（64 位默认 restype 会截断指针）。"""
    import ctypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.OpenClipboard.argtypes = (ctypes.c_void_p,)
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.CloseClipboard.restype = ctypes.c_bool
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = (ctypes.c_uint,)
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)
    return ctypes, user32, kernel32


def _open_clipboard(user32) -> bool:
    """尝试打开剪贴板：被别的程序占用时短暂重试几次。"""
    import time

    for _attempt in range(_OPEN_CLIPBOARD_ATTEMPTS):
        if user32.OpenClipboard(None):
            return True
        time.sleep(_OPEN_CLIPBOARD_INTERVAL)
    return False


def _clipboard_get() -> ToolResult:
    ctypes, user32, kernel32 = _clipboard_api()
    if not _open_clipboard(user32):
        return _error(
            "windows_clipboard_busy",
            "剪贴板被其他程序占用，暂时无法读取，请稍后重试",
        )
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return ToolResult(
                content="剪贴板暂无文本：当前内容为空，或不是文本格式（如图片 / 文件列表）。"
            )
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return _error("windows_clipboard_lock_failed", "锁定剪贴板数据失败，请稍后重试")
        try:
            text = ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    except Exception as exc:  # ctypes 函数缺失抛 AttributeError，统一归一
        return _error("windows_clipboard_read_failed", "读取剪贴板失败：%s" % exc)
    finally:
        user32.CloseClipboard()

    if not text:
        return ToolResult(content="剪贴板暂无文本：当前内容为空。")
    body = text
    suffix = ""
    if len(body) > _CLIPBOARD_TEXT_LIMIT:
        body = body[:_CLIPBOARD_TEXT_LIMIT]
        suffix = "\n\n（内容过长，已截断；完整长度 %d 字符）" % len(text)
    return ToolResult(content="剪贴板文本（%d 字符）：\n%s%s" % (len(text), body, suffix))


def _clipboard_set(text: str) -> ToolResult:
    payload = str(text or "")
    if not payload:
        return _error(
            "windows_invalid_arguments",
            "写入剪贴板失败：text 为空，没有可写入的内容",
        )

    ctypes, user32, kernel32 = _clipboard_api()
    if not _open_clipboard(user32):
        return _error(
            "windows_clipboard_busy",
            "剪贴板被其他程序占用，写入失败，请稍后重试",
        )
    handle = None
    try:
        data = payload.encode("utf-16-le") + b"\x00\x00"
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(_GMEM_FLAGS, len(data))
        if not handle:
            return _error("windows_clipboard_alloc_failed", "写入剪贴板失败：分配剪贴板内存失败")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return _error("windows_clipboard_lock_failed", "写入剪贴板失败：锁定剪贴板内存失败")
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
            return _error("windows_clipboard_write_failed", "写入剪贴板失败：系统拒绝接收该数据")
        # 成功后句柄归系统所有，不得 GlobalFree
        handle = None
    except Exception as exc:
        return _error("windows_clipboard_write_failed", "写入剪贴板失败：%s" % exc)
    finally:
        if handle:
            try:
                kernel32.GlobalFree(handle)
            except Exception:
                pass
        user32.CloseClipboard()

    return ToolResult(content="已写入剪贴板（%d 字符）。" % len(payload))


def _clipboard_tool(mode: str = "get", text: str = "") -> ToolResult:
    """windows_clipboard 实现：mode=get 读文本 / mode=set 写文本。"""
    normalized_mode = str(mode or "get").strip().lower()
    if normalized_mode == "get":
        return _clipboard_get()
    if normalized_mode == "set":
        return _clipboard_set(text)
    return _error("windows_invalid_mode", "mode 只支持 get / set，收到 %s" % mode)


def _clipboard_get_tool() -> ToolResult:
    """windows_clipboard_get 入口：只读读取剪贴板文本（复用 _clipboard_tool 的 get 分支）。"""
    return _clipboard_tool(mode="get")


def _clipboard_set_tool(text: str = "") -> ToolResult:
    """windows_clipboard_set 入口：写入剪贴板文本（复用 _clipboard_tool 的 set 分支）。"""
    return _clipboard_tool(mode="set", text=text)


# ── windows_registry（只读） ────────────────────────────────


def _split_registry_path(path: str):
    """拆分注册表路径：返回 (规范化路径, 错误说明)；非法时路径为 None。"""
    text = str(path or "").strip()
    if not text:
        return None, "path 不能为空：需形如 HKEY_CURRENT_USER\\Software\\Microsoft 的绝对根键路径"

    head, _, tail = text.partition("\\")
    root_name = head.strip().upper()
    if root_name not in _REGISTRY_ROOTS:
        return None, (
            "path 必须以 HKEY_CURRENT_USER\\ / HKEY_LOCAL_MACHINE\\ / "
            "HKEY_CLASSES_ROOT\\ 开头（收到 %s）" % text
        )

    subpath = tail.strip().strip("\\")
    if not subpath:
        return None, "path 缺少子路径：根键之后必须给出具体项（收到 %s）" % text
    segments = [segment.strip() for segment in subpath.split("\\")]
    if any(segment in ("", ".", "..") for segment in segments):
        return None, "path 不允许包含空段、. 或 .. 段（收到 %s）" % text
    return "%s\\%s" % (root_name, subpath), ""


def _truncate_text(text: str, limit: int) -> str:
    """超限截断并追加说明，避免把巨量注册表内容灌进上下文。"""
    if len(text) <= limit:
        return text
    return "%s\n\n（输出过长，已截断到 %d 字符；完整长度 %d 字符）" % (
        text[:limit], limit, len(text),
    )


def _registry_type_name(value_type) -> str:
    try:
        code = int(value_type)
    except (TypeError, ValueError):
        return str(value_type)
    return _REGISTRY_TYPE_NAMES.get(code, "REG_TYPE_%d" % code)


def _registry_value_text(value) -> str:
    if isinstance(value, bytes):
        return "0x" + value.hex() if value else "<空二进制>"
    if isinstance(value, (list, tuple)):
        return " | ".join(str(item) for item in value)
    return str(value)


def _registry_list_text(winreg, key, normalized_path: str) -> str:
    """枚举项下的值与子键（各自有上限，避免超大项拖垮上下文）。"""
    values = []
    index = 0
    while len(values) < _REGISTRY_LIST_VALUE_LIMIT:
        try:
            value_name, value, value_type = winreg.EnumValue(key, index)
        except Exception:  # 枚举结束抛 OSError(22)，统一按「没有了」处理
            break
        values.append(
            "[值] %s (%s) = %s"
            % (value_name or "(默认)", _registry_type_name(value_type), _registry_value_text(value))
        )
        index += 1

    subkeys = []
    index = 0
    while len(subkeys) < _REGISTRY_LIST_SUBKEY_LIMIT:
        try:
            subkeys.append("[子键] " + str(winreg.EnumKey(key, index)))
        except Exception:
            break
        index += 1

    lines = [
        "注册表项 %s（值 %d 个，子键 %d 个）" % (normalized_path, len(values), len(subkeys)),
    ]
    lines.extend(values)
    lines.extend(subkeys)
    if not values and not subkeys:
        lines.append("（该项下没有值也没有子键）")
    return "\n".join(lines)


def _registry_tool(
    mode: str = "get",
    path: str = "",
    name: str = "",
    view: str = "default",
) -> ToolResult:
    """windows_registry 实现：只读的 mode=get / list，写入与删除一律拒绝。"""
    normalized_mode = str(mode or "get").strip().lower()
    if normalized_mode in ("set", "delete"):
        return _error(
            "windows_registry_read_only",
            "本版本不提供注册表写入/删除：windows_registry 只支持只读的 get / list；"
            "如需修改注册表，请让用户手动操作",
        )
    if normalized_mode not in ("get", "list"):
        return _error("windows_invalid_mode", "mode 只支持 get / list，收到 %s" % mode)

    normalized_path, path_error = _split_registry_path(path)
    if path_error:
        return _error("windows_registry_invalid_path", path_error)

    normalized_view = str(view or "default").strip().lower()
    if normalized_view not in _REGISTRY_VIEWS:
        return _error(
            "windows_invalid_arguments",
            "view 只支持 default / 64 / 32，收到 %s" % view,
        )

    value_name = str(name or "")
    if normalized_mode == "get" and not value_name.strip():
        return _error("windows_invalid_arguments", "mode=get 需要提供 name（要读取的值名）")

    try:
        import winreg
    except ImportError:
        return _error(
            "windows_registry_unavailable",
            "当前环境没有 winreg 标准库模块，无法读取注册表",
        )

    root_name, _, subpath = normalized_path.partition("\\")
    root = getattr(winreg, root_name, None)
    if root is None:
        return _error("windows_registry_unavailable", "当前环境不支持根键 %s" % root_name)

    access = winreg.KEY_READ
    if normalized_view == "64":
        access |= getattr(winreg, "KEY_WOW64_64KEY", 0)
    elif normalized_view == "32":
        access |= getattr(winreg, "KEY_WOW64_32KEY", 0)

    key = None
    try:
        key = winreg.OpenKey(root, subpath, 0, access)
        if normalized_mode == "get":
            value, value_type = winreg.QueryValueEx(key, value_name)
            text = "注册表项 %s\n[值] %s (%s) = %s" % (
                normalized_path,
                value_name,
                _registry_type_name(value_type),
                _registry_value_text(value),
            )
        else:
            text = _registry_list_text(winreg, key, normalized_path)
    except FileNotFoundError:
        return _error(
            "windows_registry_path_not_found",
            "注册表项或值不存在：%s" % normalized_path,
        )
    except PermissionError:
        return _error(
            "windows_registry_access_denied",
            "没有权限读取该注册表项：%s" % normalized_path,
        )
    except OSError as exc:
        return _error("windows_registry_read_failed", "读取注册表失败：%s" % exc)
    except Exception as exc:  # 兜底归一，绝不向上抛栈
        return _error("windows_registry_read_failed", "读取注册表失败：%s" % exc)
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass

    return ToolResult(content=_truncate_text(text, _REGISTRY_OUTPUT_LIMIT))


# ── windows_notify ─────────────────────────────────────────


def _notify_tool(title: str = "", message: str = "") -> ToolResult:
    """windows_notify 实现：弹出系统 MessageBox 提醒（阻塞到用户点确定）。"""
    clean_title = str(title or "").strip()
    clean_message = str(message or "").strip()

    if not clean_message:
        return _error(
            "windows_invalid_arguments",
            "message 不能为空：没有要提醒用户的内容",
        )
    if len(clean_title) > _NOTIFY_TITLE_LIMIT:
        return _error(
            "windows_invalid_arguments",
            "title 最多 %d 个字符，收到 %d 个" % (_NOTIFY_TITLE_LIMIT, len(clean_title)),
        )
    if len(clean_message) > _NOTIFY_MESSAGE_LIMIT:
        return _error(
            "windows_invalid_arguments",
            "message 最多 %d 个字符，收到 %d 个" % (_NOTIFY_MESSAGE_LIMIT, len(clean_message)),
        )

    try:
        import win32gui
    except ImportError:
        return _error(
            "windows_notify_unavailable",
            "当前环境缺少 pywin32（win32gui），无法弹出系统提示框",
        )

    try:
        try:
            foreground = int(win32gui.GetForegroundWindow() or 0)
        except Exception:
            foreground = 0
        win32gui.MessageBox(
            foreground,
            clean_message,
            clean_title,
            _MB_OK | _MB_ICONINFORMATION,
        )
    except Exception as exc:
        return _error("windows_notify_failed", "弹出系统提示框失败：%s" % exc)

    return ToolResult(
        content=(
            "已弹出系统提示框（标题「%s」）；该提示会一直显示到用户点「确定」。"
            % (clean_title or "<无标题>",)
        )
    )


# ── ToolSpec 注册 ──────────────────────────────────────────


def _process_list_spec() -> ToolSpec:
    return ToolSpec(
        name="windows_process_list",
        description=(
            "Windows 进程列表（只读，不需要审批）。列出运行中的进程及其 PID / 内存 MB / 名称，"
            "支持 filter 按名称子串或精确 PID 筛选、sort_by（cpu | memory | name，默认 memory "
            "降序）、limit 返回条数（默认 50，上限 500）。"
            "【主动使用时机】需要看哪个程序占用内存或 CPU、确认目标进程名与 PID 时。"
            "【不要主动使用】只想查端口占用先用 execute_command 的 netstat；结束进程用 "
            "windows_process_kill（该工具会弹审批）。"
            "【仅 Windows】非 Windows 平台不注册本工具。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "description": "按进程名子串或精确 PID 筛选，不区分大小写",
                },
                "sort_by": {
                    "type": "string",
                    "enum": list(_SORT_KEYS),
                    "description": "排序字段，默认 memory（降序）；name 为升序",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_LIMIT,
                    "description": "最多返回条数，默认 50，上限 500",
                },
            },
        },
        execute=_process_list_tool,
        read_only=True,
        mutates_files=False,
        requires_permission=False,
        cancellable=True,
        parallel_safe=True,
        side_effect_scope="none",
        activity_description="列出 Windows 进程",
    )


def _process_kill_spec() -> ToolSpec:
    return ToolSpec(
        name="windows_process_kill",
        description=(
            "Windows 结束进程（写操作，需要用户审批）。pid 与 name 二选一（name 可省略 .exe，"
            "同名多进程会全部结束），force=true 立即强制结束（kill），否则先请求正常退出"
            "（terminate）。内置保护：System / System Idle Process / csrss.exe / wininit.exe / "
            "winlogon.exe / services.exe / lsass.exe、PID 0 / 4、当前进程与父进程一律拒绝结束。"
            "【主动使用时机】需要结束卡死的应用或残留进程，且用户已明确要求时。"
            "【不要主动使用】先确认用户意图；只想看进程别占用内存请用 windows_process_list。"
            "【仅 Windows】非 Windows 平台不注册本工具。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pid": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "目标进程 PID（与 name 二选一）",
                },
                "name": {
                    "type": "string",
                    "description": "目标进程名，如 chrome.exe 或 chrome（与 pid 二选一）",
                },
                "force": {
                    "type": "boolean",
                    "description": "true 立即强制结束；false 先请求正常退出，默认 false",
                },
            },
        },
        execute=_process_kill_tool,
        read_only=False,
        mutates_files=False,
        requires_permission=True,
        cancellable=True,
        parallel_safe=False,
        side_effect_scope="system",
        activity_description="结束 Windows 进程",
    )


def _clipboard_get_spec() -> ToolSpec:
    return ToolSpec(
        name="windows_clipboard_get",
        description=(
            "读取 Windows 剪贴板文本（只读，不需要审批）。剪贴板里不是文本（如图片 / 文件"
            "列表）时返回「剪贴板暂无文本」而不是报错；超长文本会截断并标注完整长度。"
            "【主动使用时机】需要读取用户刚复制的内容时。"
            "【不要主动使用】剪贴板图片用 clipboard_image；写剪贴板用 windows_clipboard_set。"
            "【仅 Windows】非 Windows 平台不注册本工具。"
        ),
        input_schema={
            "type": "object",
            "properties": {},
        },
        execute=_clipboard_get_tool,
        read_only=True,
        mutates_files=False,
        requires_permission=False,
        cancellable=True,
        parallel_safe=True,
        side_effect_scope="none",
        activity_description="读取 Windows 剪贴板文本",
    )


def _clipboard_set_spec() -> ToolSpec:
    return ToolSpec(
        name="windows_clipboard_set",
        description=(
            "把 text 写入 Windows 剪贴板（写操作，需要用户审批）。会覆盖用户当前剪贴板内容，"
            "text 不可为空。"
            "【主动使用时机】需要在程序之间搬运一段文字时。"
            "【不要主动使用】覆盖用户剪贴板前先确认意图；读取剪贴板用 windows_clipboard_get。"
            "【仅 Windows】非 Windows 平台不注册本工具。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要写入剪贴板的文本，不可为空",
                },
            },
            "required": ["text"],
        },
        execute=_clipboard_set_tool,
        read_only=False,
        mutates_files=False,
        requires_permission=True,
        cancellable=True,
        parallel_safe=False,
        side_effect_scope="system",
        activity_description="写入 Windows 剪贴板",
    )


def _registry_spec() -> ToolSpec:
    return ToolSpec(
        name="windows_registry",
        description=(
            "只读 Windows 注册表（不需要审批）。mode=list 列出某个注册表项下的值（值名 / 类型 / "
            "数值）与子键；mode=get 按 name 读取单个值。path 必须是绝对根键路径，以 "
            "HKEY_CURRENT_USER\\ / HKEY_LOCAL_MACHINE\\ / HKEY_CLASSES_ROOT\\ 开头，根键之后"
            "必须给出具体项，且不允许 . / .. / 空段；view 可选 default / 64 / 32（HKLM / HKCR "
            "下切换 32/64 位视图）。输出超过 4000 字符会截断。本版本不提供注册表写入 / 删除。"
            "【主动使用时机】需要确认某个程序是否安装、查文件关联或系统设置项时。"
            "【不要主动使用】想改注册表请让用户手动操作；查文件是否存在用 glob_files 更快。"
            "【仅 Windows】非 Windows 平台不注册本工具。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["get", "list"],
                    "description": "get=按 name 读取单个值；list=列出该路径下的值与子键",
                },
                "path": {
                    "type": "string",
                    "description": "绝对根键路径，如 HKEY_CURRENT_USER\\Software\\Microsoft",
                },
                "name": {
                    "type": "string",
                    "description": "仅 mode=get 有效：要读取的值名；默认值写作 (默认) 或空串",
                },
                "view": {
                    "type": "string",
                    "enum": list(_REGISTRY_VIEWS),
                    "description": "注册表视图，默认 default；64 / 32 仅影响 HKLM 与 HKCR",
                },
            },
            "required": ["mode", "path"],
        },
        execute=_registry_tool,
        read_only=True,
        mutates_files=False,
        requires_permission=False,
        cancellable=True,
        parallel_safe=True,
        side_effect_scope="none",
        activity_description="只读查询注册表",
    )


def _notify_spec() -> ToolSpec:
    return ToolSpec(
        name="windows_notify",
        description=(
            "弹一个 Windows 系统提示框（MessageBox）提醒用户，不需要审批。title 最多 64 字符、"
            "message 最多 256 字符。注意：该提示框会阻塞到用户点「确定」为止，调用期间本轮对话"
            "会停在这里，因此只在用户确实需要立刻被提醒时使用；不要用它播报进度或解释结果。"
            "【主动使用时机】长任务完成、需要用户立刻注意的提醒时。"
            "【不要主动使用】不要用它代替对话回复；频繁弹窗会打断用户。"
            "【仅 Windows】非 Windows 平台不注册本工具。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "提示框标题，最多 64 个字符，省略时无标题",
                },
                "message": {
                    "type": "string",
                    "description": "提示框正文，最多 256 个字符，不可为空",
                },
            },
            "required": ["message"],
        },
        execute=_notify_tool,
        read_only=False,
        mutates_files=False,
        requires_permission=False,
        cancellable=False,
        parallel_safe=False,
        side_effect_scope="none",
        activity_description="弹出 Windows 系统提示框",
    )


def register(registry):
    """平台门控注册：非 Windows 与 CLI 轻量模式一律不注册。"""
    if sys.platform != "win32":
        return
    if os.environ.get("YCODE_TOOL_PROFILE", "").strip().lower() in ("cli", "server"):
        return
    # 顺序即 schema 顺序：先只读查询，再写操作，最后是注册表与通知。
    registry.register(_process_list_spec())
    registry.register(_process_kill_spec())
    registry.register(_clipboard_get_spec())
    registry.register(_clipboard_set_spec())
    registry.register(_registry_spec())
    registry.register(_notify_spec())
