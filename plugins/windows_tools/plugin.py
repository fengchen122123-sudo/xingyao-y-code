"""windows_tools：Windows 系统能力插件（进程 / 剪贴板）。

设计要点（对齐 docs/2026-11-09-windows-mcp-parity-design.md §5.3）：

- 平台门控：``register()`` 在非 Windows 或 CLI 轻量模式下直接返回，不注册任何
  工具——macOS/Linux 上不白占 tools schema 字节。
- 模块顶层只导入标准库 os / sys：psutil、ctypes、win32api 之类一律在函数内惰性
  导入，保证本文件在任何平台都能安全 exec，不会因缺依赖而让插件加载失败。
- 错误码英文小写下划线、message 中文；进程不存在 / 剪贴板被占用一律返回
  ToolResult(is_error=True) 的明确提示，绝不向上抛栈。
- 只依赖标准库与 psutil（后者已在 requirements.txt），不新增第三方依赖。
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


# ── ToolSpec 注册 ──────────────────────────────────────────


def _process_spec() -> ToolSpec:
    return ToolSpec(
        name="windows_process",
        description=(
            "Windows 进程查看与结束。mode=list 列出进程（PID / 内存 MB / 名称），支持 "
            "filter 名称关键字、sort_by（cpu | memory | name）、limit 条数；"
            "mode=kill 结束进程，pid 与 name 二选一（name 可省略 .exe，同名多进程会全部结束），"
            "force=true 立即强制结束（kill），否则先请求正常退出（terminate）。"
            "内置保护：System / System Idle Process / csrss.exe / wininit.exe / winlogon.exe / "
            "services.exe / lsass.exe、PID 0 / 4、当前进程与父进程一律拒绝结束。"
            "【主动使用时机】需要看哪个程序占用内存或 CPU、结束卡死的应用或残留进程时。"
            "【不要主动使用】只想查端口占用先用 execute_command 的 netstat；结束进程前必须确认用户意图。"
            "【仅 Windows】非 Windows 平台不注册本工具。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["list", "kill"],
                    "description": "list=列出进程（默认）；kill=结束进程",
                },
                "filter": {
                    "type": "string",
                    "description": "仅 list 有效：按进程名子串或精确 PID 筛选，不区分大小写",
                },
                "sort_by": {
                    "type": "string",
                    "enum": list(_SORT_KEYS),
                    "description": "仅 list 有效：排序字段，默认 memory（降序）；name 为升序",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_LIMIT,
                    "description": "仅 list 有效：最多返回条数，默认 50，上限 500",
                },
                "pid": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "仅 kill 有效：目标进程 PID（与 name 二选一）",
                },
                "name": {
                    "type": "string",
                    "description": "仅 kill 有效：目标进程名，如 chrome.exe 或 chrome（与 pid 二选一）",
                },
                "force": {
                    "type": "boolean",
                    "description": "仅 kill 有效：true 立即强制结束；false 先请求正常退出，默认 false",
                },
            },
            "required": ["mode"],
        },
        execute=_process_tool,
        read_only=False,
        mutates_files=False,
        requires_permission=True,
        cancellable=True,
        parallel_safe=False,
        side_effect_scope="system",
        activity_description="操作 Windows 进程",
    )


def _clipboard_spec() -> ToolSpec:
    return ToolSpec(
        name="windows_clipboard",
        description=(
            "Windows 剪贴板文本读写。mode=get 读取当前剪贴板文本（非文本内容会提示「剪贴板暂无文本」）；"
            "mode=set 把 text 写入剪贴板（会覆盖用户当前剪贴板内容）。"
            "【主动使用时机】需要在程序之间搬运一段文字、或读取用户刚复制的内容时。"
            "【不要主动使用】图片剪贴板用 clipboard_image；不要用它暂存大段代码以外的东西，"
            "覆盖用户剪贴板前先确认意图。"
            "【仅 Windows】非 Windows 平台不注册本工具。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["get", "set"],
                    "description": "get=读取剪贴板文本（默认）；set=写入剪贴板文本",
                },
                "text": {
                    "type": "string",
                    "description": "仅 set 有效：要写入剪贴板的文本，不可为空",
                },
            },
            "required": ["mode"],
        },
        execute=_clipboard_tool,
        read_only=False,
        mutates_files=False,
        requires_permission=True,
        cancellable=True,
        parallel_safe=False,
        side_effect_scope="system",
        activity_description="读写 Windows 剪贴板",
    )


def register(registry):
    """平台门控注册：非 Windows 与 CLI 轻量模式一律不注册。"""
    if sys.platform != "win32":
        return
    if os.environ.get("YCODE_TOOL_PROFILE", "").strip().lower() == "cli":
        return
    registry.register(_process_spec())
    registry.register(_clipboard_spec())