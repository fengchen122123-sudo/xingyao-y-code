import ipaddress
import logging
import os
import json
import re
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import warnings
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
from xml.sax.saxutils import escape

DDGS = None

from core.version import __version__ as YCODE_VERSION
from core import tool_cache as _tool_cache
from core.lsp import _get_rg_executable
from core.subagent_common import build_subagent_common_block
# 秘书调度台的错误类型：仅用于把服务层的可读原因转成提示文本（纯类型，无 I/O）
from core.secretary_dispatch import SecretaryDispatchError
from core import project_profile


HTTP_TIMEOUT_SECONDS = 20
HTTP_MAX_BYTES = 512 * 1024
HTTP_USER_AGENT = f"ycode/{YCODE_VERSION} (terminal coding assistant)"
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
READ_FILE_MAX_LINES = 2000
# 大文件阈值：超过此字节数的文件，未显式指定 limit 时自动只读前部分并提示分页
READ_FILE_LARGE_BYTES = 50 * 1024
READ_FILE_LARGE_DEFAULT_LINES = 500
EXECUTE_COMMAND_MAX_OUTPUT_BYTES = 1024 * 1024

_logger = logging.getLogger(__name__)


class _ToolCancelledError(Exception):
    pass


def _raise_if_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise _ToolCancelledError()


# 子代理只读工具基底：除本地文件与检索外，还必须覆盖主 Agent 侧常用的只读
# 内置与插件工具（小说体检、办公文档读取等）。白名单是硬过滤——不在集合里的
# 工具对子代理完全不可见。历史故障：计划项要求「跑 novel_audit 复核」，子代理
# 只拿到 7 个基础工具，只能回「工具不可用」；而计划门禁又不许主 Agent 自己跑，
# 整条链路死锁（2026-11-02 小说项目实测）。
_SUBAGENT_READ_ONLY_TOOLS = frozenset({
    "read_file", "list_directory", "search_code", "glob_files",
    "find_definition", "get_outline", "find_references", "tool_result_read",
    "search_news", "fetch_url", "fetch_image", "get_weather",
    "office_read",
    "novel_status", "novel_wordcount", "novel_outline", "novel_audit",
})

_SUBAGENT_DEFAULT_ALLOWED_TOOLS = {
    "explore": _SUBAGENT_READ_ONLY_TOOLS,
    "implement": _SUBAGENT_READ_ONLY_TOOLS | frozenset({
        "write_file", "edit_file", "multi_edit", "execute_command",
        "office_write", "novel_init", "novel_track",
    }),
    "test": frozenset({
        "read_file", "list_directory", "search_code", "glob_files",
        "find_definition", "get_outline", "find_references", "tool_result_read", "execute_command",
        "novel_status", "novel_wordcount", "novel_audit",
    }),
    "review": _SUBAGENT_READ_ONLY_TOOLS,
    "general": None,
}


def default_subagent_allowed_tools(role: str):
    """内置角色默认工具白名单；未知角色按 general 处理（不限定）。"""
    return _SUBAGENT_DEFAULT_ALLOWED_TOOLS.get(role, None)


def _agent_display_name() -> str:
    try:
        from core.secretary import get_active_agent_name

        return get_active_agent_name()
    except Exception:
        return "星瑶"


# 敏感信息脱敏：后台任务命令回显/日志前屏蔽内联凭据，
# 避免 export TOKEN=sk-... 这类命令把密钥明文喂回模型或写入持久化记录。
# 实现集中在 command_policy，供 task_manager 等持久化方复用
# ---------------------------------------------------------------------------
from core.command_policy import redact_sensitive_text as _redact_sensitive_text


_EDIT_FILE_LOCKS_GUARD = threading.Lock()
_EDIT_FILE_LOCKS = {}


def _edit_file_lock(resolved):
    key = str(resolved)
    with _EDIT_FILE_LOCKS_GUARD:
        lock = _EDIT_FILE_LOCKS.get(key)
        if lock is None:
            # 防止长期运行后锁表无限增长，超限时整体重建（持锁中，安全）
            if len(_EDIT_FILE_LOCKS) > 1024:
                _EDIT_FILE_LOCKS.clear()
            lock = _EDIT_FILE_LOCKS[key] = threading.Lock()
        return lock


def _stat_mtime_ns(path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _mtime_conflict_error(file_path, resolved, expected_mtime_ns):
    """写入前乐观校验：读时记录的 mtime 若已变化，说明文件被并发/外部修改。

    expected_mtime_ns 为 None 表示操作开始时文件尚不存在，写入前它仍不应存在。
    返回 None 表示无冲突；否则返回错误文本（让调用方重读后再试）。
    """
    current = _stat_mtime_ns(resolved)
    if expected_mtime_ns is None:
        if current is not None:
            return (
                f"🚨 写入中止：文件 {file_path} 在本次操作期间被外部创建，"
                f"为避免覆盖他人改动，请先 read_file 确认后再试。"
            )
    elif current != expected_mtime_ns:
        return (
            f"🚨 写入中止：文件 {file_path} 在读取后被外部修改"
            f"（可能由并发子代理或其他程序写入）。为避免静默覆盖，"
            f"请重新 read_file 后再重试本次编辑。"
        )
    return None


def _compress_source_text(text):
    if not text:
        return text
    lines = text.splitlines()
    stripped = [line.rstrip() for line in lines]
    result = []
    blank_run = 0
    for line in stripped:
        if line == "":
            blank_run += 1
            if blank_run <= 1:
                result.append(line)
        else:
            blank_run = 0
            result.append(line)
    while result and result[-1] == "":
        result.pop()
    return "\n".join(result) + ("\n" if result else "")


_sandbox_tls = threading.local()
_session_sandbox_bypass = False
_session_sandbox_bypass_by_session: dict[str, bool] = {}
_session_sandbox_lock = threading.Lock()

_subagent_depth_tls = threading.local()
SUBAGENT_MAX_DEPTH = 3


def _get_current_subagent_depth():
    return getattr(_subagent_depth_tls, "depth", 0)


@contextlib.contextmanager
def _subagent_depth_scope():
    prev = getattr(_subagent_depth_tls, "depth", 0)
    _subagent_depth_tls.depth = prev + 1
    try:
        yield prev + 1
    finally:
        _subagent_depth_tls.depth = prev


def _resolve_against_workdir(file_path):
    from core.workspace_context import get_workdir

    candidate = Path(str(file_path or "")).expanduser()
    if not candidate.is_absolute():
        candidate = get_workdir() / candidate
    return candidate.resolve()


def is_outside_workspace(path_value):
    """判断路径是否在当前工作目录之外（resolve 后比较，符号链接/.. 均可识别；
    相对路径按线程本地 workdir 解析，见 _resolve_against_workdir）。"""
    from core.workspace_context import get_workdir
    candidate = _resolve_against_workdir(path_value)
    workspace = get_workdir()
    return candidate != workspace and workspace not in candidate.parents


@contextlib.contextmanager
def sandbox_bypass():
    """线程内临时放行沙盒外访问（用于用户已明确批准的单次操作，可嵌套）。"""
    depth = getattr(_sandbox_tls, "depth", 0)
    _sandbox_tls.depth = depth + 1
    try:
        yield
    finally:
        _sandbox_tls.depth = depth


def _sandbox_session_id():
    try:
        import core.app_context as _app_context

        sid = _app_context.get_thread_session() or _app_context.get_current_session()
        return str(sid) if sid else "__default__"
    except Exception:
        return "__default__"


def set_session_sandbox_bypass(enabled):
    global _session_sandbox_bypass
    sid = _sandbox_session_id()
    with _session_sandbox_lock:
        if sid:
            _session_sandbox_bypass_by_session[sid] = bool(enabled)
        else:
            _session_sandbox_bypass = bool(enabled)


def clear_session_sandbox_bypass(task_id: str) -> None:
    normalized = str(task_id or "").strip()
    if not normalized:
        raise ValueError("任务 ID 不能为空")
    with _session_sandbox_lock:
        _session_sandbox_bypass_by_session.pop(normalized, None)


def rename_session_sandbox_bypass(old_task_id: str, new_task_id: str) -> None:
    old_key = str(old_task_id or "").strip()
    new_key = str(new_task_id or "").strip()
    if not old_key or not new_key:
        raise ValueError("任务 ID 不能为空")
    if old_key == new_key:
        return
    with _session_sandbox_lock:
        if _session_sandbox_bypass_by_session.get(new_key, False):
            raise ValueError(f"目标任务 {new_key} 已有沙盒授权，不能覆盖")
        enabled = _session_sandbox_bypass_by_session.pop(old_key, None)
        if enabled is not None:
            _session_sandbox_bypass_by_session[new_key] = enabled


def is_session_sandbox_bypass_enabled():
    """读取任务级沙盒放行标志（供 core.llm 权限决策使用）。"""
    sid = _sandbox_session_id()
    with _session_sandbox_lock:
        if sid:
            return _session_sandbox_bypass_by_session.get(sid, False)
        return _session_sandbox_bypass


_session_user_attached_paths_by_session: dict[str, set[str]] = {}
_session_user_attached_paths_lock = threading.Lock()

_paste_file_registry: set[str] = set()
_paste_file_registry_lock = threading.Lock()


def register_paste_file(path) -> None:
    """登记本进程落盘的粘贴/上传文件路径（落盘入口调用一次）。"""
    if not path:
        return
    try:
        target = os.path.realpath(os.path.expanduser(str(path)))
    except Exception:
        return
    with _paste_file_registry_lock:
        _paste_file_registry.add(target)


def is_trusted_paste_path(path) -> bool:
    """判定路径是否为本进程落盘登记过的粘贴/上传文件（ycode_paste 目录内）。"""
    if not path:
        return False
    try:
        import tempfile
        paste_dir = os.path.realpath(os.path.join(tempfile.gettempdir(), "ycode_paste"))
        target = os.path.realpath(os.path.expanduser(str(path)))
        if target != paste_dir and not target.startswith(paste_dir + os.sep):
            return False
    except Exception:
        return False
    with _paste_file_registry_lock:
        return target in _paste_file_registry


def register_session_user_attached_paths(paths, session_id: str | None = None) -> None:
    """登记用户主动在 UI 中提供/挂载的附件绝对路径（代表用户显式授权）。"""
    if not paths:
        return
    sid = str(session_id or "").strip() or _sandbox_session_id()
    resolved = set()
    for p in paths:
        if not p:
            continue
        try:
            resolved.add(os.path.realpath(os.path.expanduser(str(p))))
        except Exception:
            pass
    if not resolved:
        return
    with _session_user_attached_paths_lock:
        bucket = _session_user_attached_paths_by_session.setdefault(sid, set())
        bucket.update(resolved)


def is_session_user_attached_path(path, session_id: str | None = None) -> bool:
    """判断给定路径是否已由用户显式作为附件挂载。

    仅匹配当前会话（显式传入 session_id 或线程绑定的会话）的登记，
    绝不跨会话兜底：其他会话挂载过的路径不代表本会话用户已授权，
    否则并行会话里 A 挂载的文件会被 B 免确认读取（跨会话数据泄漏）。
    """
    if not path:
        return False
    try:
        target = os.path.realpath(os.path.expanduser(str(path)))
    except Exception:
        return False
    sid = str(session_id or "").strip() or _sandbox_session_id()
    with _session_user_attached_paths_lock:
        bucket = _session_user_attached_paths_by_session.get(sid)
        return bool(bucket and target in bucket)


def _clear_sandbox_state_for_tests() -> None:
    global _session_sandbox_bypass
    with _session_sandbox_lock:
        _session_sandbox_bypass = False
        _session_sandbox_bypass_by_session.clear()
    with _session_user_attached_paths_lock:
        _session_user_attached_paths_by_session.clear()
    with _paste_file_registry_lock:
        _paste_file_registry.clear()
    if hasattr(_sandbox_tls, "depth"):
        _sandbox_tls.depth = 0


def _outside_workspace_allowed():
    if getattr(_sandbox_tls, "depth", 0) > 0:
        return True
    if is_session_sandbox_bypass_enabled():
        return True
    try:
        from core.model_manager import load_config
        return bool(load_config().get("allow_outside_workspace", False))
    except Exception:
        return False


def _resolve_workspace_path(file_path, *, allow_trusted_read=False):
    candidate = _resolve_against_workdir(file_path)
    if _outside_workspace_allowed():
        return candidate
    if allow_trusted_read:
        try:
            from core.skills import is_trusted_read_path
            if is_trusted_read_path(str(candidate)):
                return candidate
        except Exception:
            pass
        try:
            from core.memory_kairos import is_ycode_data_path
            if is_ycode_data_path(str(candidate)):
                return candidate
        except Exception:
            pass
        try:
            if is_trusted_paste_path(candidate) or is_session_user_attached_path(candidate):
                return candidate
        except Exception:
            pass
        # 秘书跨项目只读豁免：只允许只读路径落入已登记项目工作区内
        try:
            from core.secretary import is_secretary_registered_workspace_read

            if is_secretary_registered_workspace_read("read_file", str(candidate)):
                return candidate
        except Exception:
            pass
    try:
        from core.ai_output_paths import is_ai_output_path

        if is_ai_output_path(candidate):
            return candidate
    except Exception:
        pass
    # 沙盒外路径必须经过 _sandbox_outside_guard 询问用户，
    # 用户批准后通过 sandbox_bypass() 设置线程级放行，_outside_workspace_allowed() 即返回 True。
    from core.workspace_context import get_workdir
    workspace = get_workdir()
    if candidate != workspace and workspace not in candidate.parents:
        raise ValueError(
            f"路径越界：只允许访问当前工作目录（{workspace}）内的文件: {file_path}"
            "（用户可通过 /sandbox on 开放工作目录外访问）"
        )
    return candidate


def _count_lines_fast(path):
    count = 0
    with open(path, 'rb') as f:
        for _ in f:
            count += 1
    return count


def _read_lines_streaming(path, start, count):
    """流式读取指定行范围，UTF-8 优先，失败回退 GBK。"""
    from itertools import islice
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return list(islice(f, start, start + count))
    except UnicodeDecodeError:
        with open(path, 'r', encoding='gbk', errors='replace') as f:
            return list(islice(f, start, start + count))


def read_file(file_path, offset=0, limit=None, compress=False, with_line_numbers=False):
    resolved = _resolve_workspace_path(file_path, allow_trusted_read=True)

    cache_key = _tool_cache.read_file_key(
        str(resolved), offset, limit, compress, with_line_numbers
    )
    cached = _tool_cache.read_file_get(cache_key)
    if cached is not None:
        return cached

    session_id = _tool_cache.get_current_session()
    if session_id:
        shared_key = ("read_file", cache_key)
        shared = _tool_cache.shared_get(session_id, shared_key)
        if shared is not None:
            return shared

    # 大文件自适应：未显式指定 limit 时，按文件大小决定默认读取行数
    # 对 stat/open 的 OSError 做友好返回（write_file/edit_file 均有同类保护）：
    # 读已删除/无权限文件不应抛出异常栈
    try:
        file_size = resolved.stat().st_size
        if file_size > 0:
            with open(resolved, "rb") as _probe_f:
                _sample = _probe_f.read(min(file_size, 8192))
            if b"\x00" in _sample and not _sample.startswith((b"\xff\xfe", b"\xfe\xff")):
                return (
                    f"🚨 检测到二进制文件（{file_path}，大小 {file_size} 字节），"
                    "read_file 仅支持文本文件。"
                )
        auto_limited = False
        if limit is None and file_size > READ_FILE_LARGE_BYTES:
            max_lines = READ_FILE_LARGE_DEFAULT_LINES
            auto_limited = True
        else:
            max_lines = READ_FILE_MAX_LINES if limit is None else max(1, min(int(limit), READ_FILE_MAX_LINES))

        # 大文件使用流式读取 + 快速行数统计，避免全量载入内存
        if file_size > READ_FILE_LARGE_BYTES:
            total = _count_lines_fast(resolved)
            start = max(0, int(offset or 0))
            if total == 0:
                return ""
            if start >= total:
                return f"⚠️ 文件共 {total} 行，offset={start} 已超出范围。"
            selected = _read_lines_streaming(resolved, start, max_lines)
            result = "".join(selected)
            end = start + len(selected)
        else:
            # 非 UTF-8 文件回退 GBK 读取（与原行为一致）
            try:
                with open(resolved, 'r', encoding='utf-8') as f:
                    lines = f.read().splitlines(keepends=True)
            except UnicodeDecodeError:
                with open(resolved, 'r', encoding='gbk', errors='replace') as f:
                    lines = f.read().splitlines(keepends=True)

            total = len(lines)
            start = max(0, int(offset or 0))
            if total == 0:
                return ""
            if start >= total:
                return f"⚠️ 文件共 {total} 行，offset={start} 已超出范围。"
            selected = lines[start:start + max_lines]
            result = "".join(selected)
            end = start + len(selected)
    except FileNotFoundError:
        return f"🚨 读取文件失败：文件不存在或已被删除：{file_path}"
    except PermissionError:
        return f"🚨 读取文件失败：没有权限读取该文件：{file_path}"
    except OSError as e:
        return f"🚨 读取文件失败：{file_path}（{e}）"

    if with_line_numbers and selected:
        numbered_lines = []
        for i, line in enumerate(selected):
            # 兼容 \r\n 和 \n：去掉行尾换行符，统一输出不带行尾换行
            line_text = line.rstrip("\r\n")
            line_no = start + 1 + i
            numbered_lines.append(f"{line_no}: {line_text}")
        result = "\n".join(numbered_lines)
        # 行号路径跳过压缩（compress 即使为 True 也不处理，行号视图必须保留原始行结构）

    compressed_note = ""
    if compress and result and not with_line_numbers:
        original_len = len(result)
        result = _compress_source_text(result)
        compressed_len = len(result)
        if original_len > compressed_len:
            compressed_note = f"\n[已压缩：{original_len} → {compressed_len} 字节，省 {original_len - compressed_len} 字节]"

    truncation_note = ""
    if end < total:
        if auto_limited:
            truncation_note = (
                f"\n... (文件较大：共 {total} 行 / {file_size} 字节，"
                f"已显示第 {start + 1}-{end} 行；请使用 offset/limit 参数继续读取，"
                f"或显式传 limit 参数突破自动限制)"
            )
        else:
            truncation_note = f"\n... (文件共 {total} 行，已显示第 {start + 1}-{end} 行；请使用 offset/limit 参数继续读取)"

    final = result + compressed_note + truncation_note
    _tool_cache.read_file_put(cache_key, final)
    if session_id:
        _tool_cache.shared_put(session_id, ("read_file", cache_key), final)

    # 登记本次读取指纹，供写前新鲜度守卫校验。
    # 只有「一个字节都没截断」（start == 0 且 end >= total）才算完整读取；
    # 登记失败绝不影响 read_file 正常返回，因此异常全部吞掉。
    try:
        from core.fresh_read_guard import record_read
        record_read(
            session_id or "",
            str(resolved),
            full_read=(start == 0 and end >= total),
        )
    except Exception:
        pass
    return final


_EDIT_DIFF_TLS = threading.local()


def capture_edit_diff():
    """上下文管理器：收集单次内置 edit_file / multi_edit 在内存中已解析的新旧全文，
    供 ToolAdapterSet 复用以展示 Diff，避免重复读盘 3 次。
    """
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        prev = getattr(_EDIT_DIFF_TLS, "slot", None)
        slot = {}
        _EDIT_DIFF_TLS.slot = slot
        try:
            yield slot
        finally:
            _EDIT_DIFF_TLS.slot = prev

    return _ctx()


def _publish_edit_diff(old_text: str, new_text: str) -> None:
    slot = getattr(_EDIT_DIFF_TLS, "slot", None)
    if isinstance(slot, dict):
        slot["old"] = old_text
        slot["new"] = new_text


def _check_post_edit_diagnostics(resolved_path: Path, text: str) -> str:
    suffix = resolved_path.suffix.lower()
    if suffix == ".py":
        import ast

        try:
            ast.parse(text, filename=str(resolved_path))
        except SyntaxError as exc:
            col = f"第 {exc.offset} 列" if exc.offset else ""
            return f"\n⚠ 静态语法检查发现问题（Python）：第 {exc.lineno} 行{col}：{exc.msg}"
    elif suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return f"\n⚠ 静态语法检查发现问题（JSON）：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
    return ""


def _on_file_mutated(resolved_path) -> None:
    session_id = _tool_cache.get_current_session() or ""
    try:
        from core.fresh_read_guard import record_write

        record_write(session_id, str(resolved_path))
    except Exception:
        pass
    try:
        _tool_cache.invalidate_after_mutation(str(resolved_path))
    except Exception:
        pass


def _invalidate_caches_after_command(command: str) -> None:
    try:
        from core.command_policy import classify_command_risk
        from core.permission_policy import PermissionRisk

        if classify_command_risk(command) is not PermissionRisk.QUERY:
            _tool_cache.invalidate_after_mutation()
    except Exception:
        pass


def write_file(file_path, content):
    """创建或完整写入文件（仅限当前工作目录内，统一按 utf-8 编码写入）。

    保护规则：如果目标文件已存在且超过 500 行 / 50KB，write_file 会拒绝直接覆盖，
    要求改用 edit_file 进行局部片段修改。这是为了避免把已有大文件整篇重写，
    导致 diff 全红、Token 浪费以及误删未改动的代码。
    """
    resolved = _resolve_workspace_path(file_path)

    # 写前新鲜度守卫：覆盖已有文件前必须本会话完整读过，且内容未变
    from core.fresh_read_guard import (
        StaleReadError,
        is_guard_enabled,
        require_fresh_read,
    )
    try:
        require_fresh_read(
            _tool_cache.get_current_session() or "",
            str(resolved),
            tool_name="write_file",
            skip=not is_guard_enabled(),
        )
    except StaleReadError as e:
        return f"🚨 {e}"

    from core.turn_undo import coordinator as _tu_coordinator
    from core.turn_undo import recorder as _tu_recorder
    from core.turn_undo.models import NodeKind, TurnUndoError

    coordinator = _tu_coordinator.get_coordinator()
    if coordinator.is_path_frozen_any(str(resolved)):
        return (
            f"🚨 目标路径处于撤销中断事务的冻结范围，已拒绝修改：{file_path}\n"
            f"请先在 GUI 或 /undo recover 中选择继续撤销或取消撤销。"
        )
    with coordinator.workspace_lock(_tu_coordinator.thread_workspace_root()):
        return _write_file_locked(resolved, file_path, content,
                                  _tu_recorder, _tu_coordinator, NodeKind, TurnUndoError)


def _write_file_locked(resolved, file_path, content, _tu_recorder, coordinator, NodeKind, TurnUndoError):

    with _edit_file_lock(resolved):
        expected_mtime_ns = None
        if resolved.exists() and resolved.is_file():
            expected_mtime_ns = _stat_mtime_ns(resolved)
            existing_size = resolved.stat().st_size
            if existing_size > 50 * 1024:
                return (
                    f"🚨 write_file 拒绝覆盖已有大文件（{file_path}，约 {existing_size / 1024:.1f}KB）。\n"
                    f"为保护未改动代码并避免 diff 全红，请改用 edit_file 对目标片段进行局部修改；"
                    f"只有确定要完整重写整个文件时才告知用户此限制。"
                )
            existing_lines = 0
            try:
                existing_lines = len(resolved.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                pass
            if existing_lines > 500:
                return (
                    f"🚨 write_file 拒绝覆盖已有大文件（{file_path}，约 {existing_lines} 行 / {existing_size / 1024:.1f}KB）。\n"
                    f"为保护未改动代码并避免 diff 全红，请改用 edit_file 对目标片段进行局部修改；"
                    f"只有确定要完整重写整个文件时才告知用户此限制。"
                )

        # D3：撤销优先保存原始字节，确保空文件与非 UTF-8 文件
        # 能精确恢复；old_content 只用于 Diff/旧调用方兼容。
        existed_before = resolved.exists() and resolved.is_file()
        old_content = ""
        old_bytes = None
        write_encoding = "utf-8"
        if existed_before:
            try:
                old_bytes = resolved.read_bytes()
            except OSError as error:
                return f"🚨 无法读取文件原始内容，已拒绝覆盖 {file_path}：{error}"
            if old_bytes.startswith(b"\xef\xbb\xbf"):
                old_content = old_bytes.decode("utf-8-sig", errors="replace")
                if not content.startswith("\ufeff"):
                    write_encoding = "utf-8-sig"
            else:
                try:
                    old_content = old_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        old_content = old_bytes.decode("gbk")
                        # 仅当新内容也能无损编码为 GBK 时沿用原 GBK 编码
                        content.encode("gbk")
                        write_encoding = "gbk"
                    except UnicodeError:
                        old_content = old_bytes.decode("gbk", errors="replace")

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return (
                f"🚨 创建父目录失败：{resolved.parent}\n"
                f"错误：{e}\n"
                f"请检查路径是否正确，或权限是否充足。"
            )

        new_bytes = content.encode(write_encoding)
        handle = None
        try:
            handle = _tu_recorder.prepare_mutation(
                display_path=str(resolved),
                after_kind=NodeKind.REGULAR_FILE,
                after_content=new_bytes,
                tool_name="write_file",
                tool_call_id="",
            )
        except TurnUndoError as error:
            if error.code == "no_active_checkpoint":
                handle = None
            elif error.code == "workspace_busy":
                # 同目录另有活跃回合：可重试，未触盘，不产生不可撤销修改
                return (
                    f"🚨 该文件正在被另一个回合写入，本次未修改文件（{file_path}）：\n"
                    f"{error.message}\n"
                    f"请稍后重试。"
                )
            else:
                return (
                    f"🚨 该回合的撤销检查点已封闭，迟到的写入已被拒绝（{file_path}）：\n"
                    f"{error.message}\n"
                    f"请重新发起新回合后再试，避免产生不可撤销的文件修改。"
                )

        fd, temp_name = tempfile.mkstemp(
            prefix=resolved.name + ".", suffix=".ycode-tmp", dir=str(resolved.parent)
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(new_bytes)
            conflict = _mtime_conflict_error(file_path, resolved, expected_mtime_ns)
            if conflict:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                if handle is not None:
                    _tu_recorder.rollback_prepared_mutation(handle)
                return conflict
            os.replace(temp_name, resolved)
        except BaseException:
            # 使用 BaseException 确保 KeyboardInterrupt、MemoryError 等非 OSError 异常
            # 同样能触发临时文件清理，避免孤儿 .ycode-tmp 文件在磁盘积累。
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            if handle is not None:
                try:
                    _tu_recorder.rollback_prepared_mutation(handle)
                except Exception:
                    pass
            raise
        if handle is not None:
            try:
                _tu_recorder.verify_after_mutation(handle)
            except TurnUndoError as error:
                return (
                    f"🚨 文件已写入但未能通过可撤销性验证，本轮撤销保证失效"
                    f"（{file_path}）：\n{error.message}"
                )
    _on_file_mutated(resolved)
    diag = _check_post_edit_diagnostics(resolved, content)
    return f"✔ 文件 {file_path} 写入成功！{diag}"

_LIST_DIR_IGNORE_NOISE = frozenset({
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
})


def list_directory(dir_path=".", max_entries=200, depth=1):
    try:
        resolved = _resolve_workspace_path(dir_path)
        if not os.path.exists(resolved):
            return f"🚨 目录扫描失败：路径不存在 '{dir_path}'"
        if os.path.isfile(resolved):
            # 模型把文件路径误传给目录扫描是常见的可恢复误用（尤其 .v 这类
            # 名字像目录的单文件），不用 🚨 错误级返回——否则 UI 会渲染成红色失败。
            # 用普通提示文案引导模型改用 read_file 即可。
            return f"'{dir_path}' 是一个文件而不是目录，无法列出。请改用 read_file 工具读取该文件内容。"

        max_entries = max(1, min(int(max_entries or 200), 1000))
        tree_depth = max(1, min(int(depth or 1), 4))

        if tree_depth == 1:
            items = sorted(
                os.listdir(resolved),
                key=lambda name: (not os.path.isdir(os.path.join(resolved, name)), name.lower()),
            )
            if not items:
                return f"📂 目录 '{dir_path}' 为空。"
            formatted = [
                f"{name}/" if os.path.isdir(os.path.join(resolved, name)) else name
                for name in items[:max_entries]
            ]
            if len(items) > max_entries:
                formatted.append(f"... (目录过大，已截断前 {max_entries} 项，共 {len(items)} 项)")
            return "\n".join(formatted)

        lines: list[str] = []
        truncated = False

        def _walk(cur_path: str, level: int) -> None:
            nonlocal truncated
            if truncated or level > tree_depth:
                return
            try:
                raw_names = os.listdir(cur_path)
            except OSError:
                return
            raw_names.sort(
                key=lambda name: (not os.path.isdir(os.path.join(cur_path, name)), name.lower())
            )
            indent = "  " * (level - 1)
            for name in raw_names:
                full = os.path.join(cur_path, name)
                is_dir = os.path.isdir(full)
                if is_dir and name in _LIST_DIR_IGNORE_NOISE:
                    continue
                if len(lines) >= max_entries:
                    truncated = True
                    return
                lines.append(f"{indent}{name}/" if is_dir else f"{indent}{name}")
                if is_dir and level < tree_depth:
                    _walk(full, level + 1)
                    if truncated:
                        return

        _walk(str(resolved), 1)
        if not lines:
            return f"📂 目录 '{dir_path}' 为空。"
        if truncated:
            lines.append(f"... (已达条目上限 {max_entries}，后续项已省略；可指定子目录继续查看)")
        return "\n".join(lines)
    except Exception as e:
        return f"🚨 目录扫描失败: {str(e)}"

def find_missing_skill_dependencies(command):
    import shlex
    from importlib.metadata import PackageNotFoundError, version
    from pathlib import Path

    from packaging.requirements import Requirement

    if not re.search(r"(?i)\.py(?:[\s\"']|$)", str(command or "")):
        return []
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        # Dependency preflight is not a shell parser. Let the real shell expose
        # invalid quoting instead of rejecting a potentially valid dialect.
        return []
    script_paths = [
        Path(token.strip('"\''))
        for token in tokens
        if token.strip('"\'').lower().endswith(".py")
    ]
    missing = []
    for script_path in script_paths:
        if not script_path.is_file() or script_path.parent.name != "scripts":
            continue
        skill_dir = script_path.parent.parent
        if "skills" not in {part.lower() for part in skill_dir.parts}:
            continue

        requirements_path = skill_dir / "requirements.txt"
        if not requirements_path.is_file():
            continue

        for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                requirement = Requirement(line)
            except Exception:
                warnings.warn(f"无法解析的技能依赖声明已跳过: {line}")
                continue
            if requirement.marker and not requirement.marker.evaluate():
                continue
            try:
                installed_version = version(requirement.name)
            except PackageNotFoundError:
                missing.append(requirement.name)
                continue
            if requirement.specifier and installed_version not in requirement.specifier:
                missing.append(str(requirement))
    return sorted(set(missing))

@dataclass
class _CommandExecutionResult:
    returncode: int | None = None


def execute_command(
    command,
    run_in_background=False,
    timeout_seconds=30,
    cancel_event=None,
    *,
    shell_id="",
    _execution_result: _CommandExecutionResult | None = None,
):
    """引擎工具：在本地终端默默执行命令，并把终端里的输出抓取回来"""
    from core.command_policy import (
        build_child_env,
        is_dangerous_command,
        load_shell_env_policy,
    )
    from core.workspace_context import get_workdir

    from core.turn_undo import coordinator as _tu_coordinator
    from core.workspace_context import get_workdir as _get_workdir

    if _tu_coordinator.get_coordinator().any_frozen_paths(get_workdir() or os.getcwd()):
        return (
            "🚨 当前工作区存在未完成的撤销中断事务，已拒绝执行命令。\n"
            "请先在 GUI 或 /undo recover 中选择继续撤销或取消撤销。"
        )

    if is_dangerous_command(command):
        return (
            "🚨 危险命令已被安全策略拒绝。请改用范围明确、可恢复的操作。"
            "若这是文档/文件内容（而非要执行的命令），请改用文件写入工具（write_file）保存。"
        )

    from core.turn_undo import recorder as _tu_recorder

    _tu_recorder.mark_uncovered_command_effects("execute_command")

    try:
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        timeout_seconds = 30
    timeout_seconds = min(max(timeout_seconds, 1), 600)
    try:
        missing_dependencies = find_missing_skill_dependencies(command)
    except Exception as exc:
        return f"🚨 技能依赖清单无效: {exc}"
    if missing_dependencies:
        missing = ", ".join(missing_dependencies)
        return f"🚨 技能脚本缺少依赖: {missing}。请先显式安装依赖，再重新执行。"
    if run_in_background:
        from core.task_manager import start_background_task
        try:
            # 记录父会话，任务完成回调据此触发 LLM 简短总结。
            # 归属必须取发起回合的会话：线程级绑定（GUI 回合 worker / 子代理
            # 线程入口 set_thread_session）优先；进程级 current_session 是
            # "UI 当前查看的会话"，草稿会话首条消息流程不更新它，只能作最后
            # 兜底，否则任务总结会误注入上一个真实会话
            try:
                import core.app_context as app_context
                session_id = (
                    app_context.get_thread_session()
                    or _tool_cache.get_current_session()
                    or app_context.get_current_session()
                    or ""
                )
            except Exception:
                session_id = _tool_cache.get_current_session() or ""
            try:
                turn_id = _tu_coordinator.get_coordinator().active_turn_id(
                    get_workdir() or os.getcwd()
                ) or ""
            except Exception:
                turn_id = ""
            background_kwargs = {"session_id": session_id, "turn_id": turn_id}
            if shell_id:
                background_kwargs["shell_id"] = shell_id
            task_id = start_background_task(command, **background_kwargs)
            return (
                f"✔ 异步后台任务已成功启动！\n"
                f"任务 ID: {task_id}\n"
                f"命令内容: {_redact_sensitive_text(command)}\n"
                f"系统已经把输出重定向到了日志中。你可以随时调用 `manage_task(action='status', task_id='{task_id}')` 来查看运行日志尾部，"
                f"或者调用 `manage_task(action='kill', task_id='{task_id}')` 终结此后台服务。"
            )
        except Exception as e:
            return f"🚨 启动后台异步任务失败: {e}"

    try:
        from core.process_io import decode_process_output, platform_console_encoding
        from core.command_shell import CommandShellError, build_command_invocation
        # 复用调度器的进程树终止逻辑，避免 subprocess.run(timeout=...) 超时只杀
        # 直接解释器进程而泄露孙进程
        from core.task_scheduler import _stop_process_tree

        output_encoding = platform_console_encoding()
        env = build_child_env(os.environ, policy=load_shell_env_policy())
        env["PYTHONIOENCODING"] = output_encoding
        env["PYTHONUTF8"] = "0"

        stdout_sink = tempfile.TemporaryFile(prefix="ycode-cmd-out-")
        stderr_sink = tempfile.TemporaryFile(prefix="ycode-cmd-err-")

        def _sink_size(sink):
            try:
                return os.fstat(sink.fileno()).st_size
            except OSError:
                return 0

        def _read_sink_capped(sink):
            size = _sink_size(sink)
            sink.seek(0)
            return sink.read(min(size, EXECUTE_COMMAND_MAX_OUTPUT_BYTES)), size

        _git_registry_handle = ""
        try:
            try:
                invocation = build_command_invocation(command, shell_id=shell_id)
            except CommandShellError as exc:
                return f"🚨 命令解释器不可用: {exc}"
            popen_kwargs = {
                "shell": invocation.use_system_shell,
                "stdout": stdout_sink,
                "stderr": stderr_sink,
                "text": False,
                "env": env,
                "cwd": str(get_workdir()),
                # GUI（无控制台）下避免系统为子进程分配新控制台窗口
                "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                "start_new_session": os.name != "nt",
            }
            if invocation.executable:
                popen_kwargs["executable"] = invocation.executable
            # Workspace Process Registry 登记（规格 §9.2）：登记失败绝不影响命令执行
            try:
                from core.git import process_registry as _proc_registry

                _git_registry_handle = _proc_registry.register_process(
                    popen_kwargs.get("cwd") or os.getcwd(),
                    kind="execute_command",
                    foreground=True,
                    long_running=False,
                    risk_class=_proc_registry.risk_class_for_command(command),
                ).handle
            except Exception:
                _git_registry_handle = ""
            process = subprocess.Popen(
                invocation.argv,
                **popen_kwargs,
            )
            start_time = time.time()
            output_capped = False
            timed_out = False
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    _stop_process_tree(process)
                    return "🛑 命令已被用户强行中断终止。"
                if (
                    _sink_size(stdout_sink) + _sink_size(stderr_sink)
                    > EXECUTE_COMMAND_MAX_OUTPUT_BYTES
                ):
                    # 输出总量超限：终止进程树，只保留前 1MB，防止内存膨胀
                    output_capped = True
                    _stop_process_tree(process)
                    break
                if process.poll() is not None:
                    break
                elapsed = time.time() - start_time
                if elapsed >= timeout_seconds:
                    timed_out = True
                    _stop_process_tree(process)
                    break
                if elapsed < 2.0:
                    interval = 0.1
                elif elapsed < 10.0:
                    interval = 0.25
                else:
                    interval = 0.5
                time.sleep(interval)

            if _execution_result is not None:
                _execution_result.returncode = process.returncode
            stdout_raw, stdout_size = _read_sink_capped(stdout_sink)
            stderr_raw, stderr_size = _read_sink_capped(stderr_sink)
        finally:
            if _git_registry_handle:
                try:
                    from core.git import process_registry as _proc_registry

                    _proc_registry.unregister_process(_git_registry_handle)
                except Exception:
                    pass
            try:
                stdout_sink.close()
                stderr_sink.close()
            except Exception:
                pass

        _invalidate_caches_after_command(command)
        output = decode_process_output(stdout_raw, output_encoding)
        if stderr_raw:
            stderr = decode_process_output(stderr_raw, output_encoding)
            output += f"\n[附加信息/警告]:\n{stderr}"

        if timed_out:
            detail = output.strip()
            if detail:
                max_tail = 4000
                tail = detail[-max_tail:] if len(detail) > max_tail else detail
                prefix = "...(已截断前段输出)...\n" if len(detail) > max_tail else ""
                return (
                    f"🚨 命令执行超时（超过 {timeout_seconds} 秒），已被强行终止！\n"
                    f"超时前已捕获输出（尾部）：\n{prefix}{tail}"
                )
            return f"🚨 命令执行超时（超过 {timeout_seconds} 秒），已被强行终止！"

        if output_capped:
            return (
                f"🚨 命令输出超过 {EXECUTE_COMMAND_MAX_OUTPUT_BYTES // 1024}KB 上限，"
                f"进程已被强行终止（防止 yes 类命令撑爆内存）。以下为截断保留的部分输出：\n{output}"
            )
        if stdout_size > EXECUTE_COMMAND_MAX_OUTPUT_BYTES or stderr_size > EXECUTE_COMMAND_MAX_OUTPUT_BYTES:
            output += (
                f"\n\n[⚠️ 输出超过 {EXECUTE_COMMAND_MAX_OUTPUT_BYTES // 1024}KB 上限，已截断，"
                f"仅保留前部分内容]"
            )

        # 必须如实汇报退出码：此前非零退出但只要无输出就返回"执行成功"，
        # 调用方（尤其子代理）无法区分成败，会把失败命令当成通过。
        if process.returncode:
            detail = output.strip()
            if not detail:
                return (
                    f"命令执行完毕（退出码 {process.returncode}），但没有任何输出。\n"
                    "提示：findstr / grep 类搜索命令在没有找到任何匹配时就会以退出码 1 "
                    "结束且无输出，通常属于正常结果而非命令出错；若预期应有输出，"
                    "请检查命令本身或换用其他方式验证。"
                )
            return f"🚨 命令执行失败（退出码 {process.returncode}）。\n{detail}"
        return output or "执行成功，但命令没有返回任何输出内容。"

    except Exception as e:
        return f"🚨 引擎执行遇到未知错误: {str(e)}"

# 组件缺失时回给模型的可读提示（不是异常）：搜索是可选能力，缺组件只应让本次
# 工具调用「不可用」，不能让整个 agent 轮次炸掉。提示里必须带设置页路径，
# 这样模型可以把原话转述给用户，用户点两下就能装好。
_SEARCH_COMPONENT_MISSING_HINT = (
    "网页搜索需要先安装「网页搜索组件」。请在 设置 → 网页搜索 中点击下载，"
    "或改用其它方式获取信息。"
)


def _load_ddgs():
    global DDGS
    if DDGS is not None:
        return DDGS
    try:
        # 惰性导入：core.tools 被大量模块早期引用，避免 import 期就把组件探测
        # （含可能的 sys.path/import 动作）拖进启动路径。
        from core.components import ensure_search_component

        ensure_search_component()
    except Exception as error:
        _logger.info("网页搜索组件不可用：%s", error)
        return None
    try:
        from ddgs import DDGS as _DDGS
    except Exception as error:
        _logger.info("导入 ddgs 失败：%s", error)
        return None
    DDGS = _DDGS
    return DDGS


_SEARCH_NUM_RESULTS_QUESTION = {
    "type": "choice",
    "instructions": "判断这次联网检索的信息量需求：3=单一事实即可，5=常规，8=需要多来源交叉核对。只依据查询词本身判断。",
    # 上游 choice 题的 criteria 是「选项名 → 描述」的对象，不可省略
    "criteria": {
        3: "单一事实即可（如某个具体数字、日期、定义）",
        5: "常规信息量（一般性的资讯或说明）",
        8: "需要多来源交叉核对（争议话题、对比评估、时效性强的动态）",
    },
}
_SEARCH_NUM_RESULTS_OPTIONS = (3, 5, 8)
_SEARCH_DECISION_TIMEOUT_MS = 1500


def _decide_search_num_results(query):
    try:
        # 惰性导入：避免 core.tools 与 core.decision_gateway 之间的循环依赖，
        # 也避免模块导入期就拉起决策链路的副作用（tools 被大量模块早期引用）。
        from core.decision_gateway import decide

        state = f"用户检索词：{query}"
        question = dict(_SEARCH_NUM_RESULTS_QUESTION)
        picked = decide(
            "search",
            state,
            question,
            options=_SEARCH_NUM_RESULTS_OPTIONS,
            timeout_ms=_SEARCH_DECISION_TIMEOUT_MS,
        )
    except Exception as error:
        # 网关本身已兜底；这里再加一层，确保决策永远不成为检索的故障点
        _logger.debug("search 场景决策失败，沿用原 num_results: %s", error)
        return None
    if isinstance(picked, bool) or not isinstance(picked, int):
        return None
    if not 1 < picked < 50:
        return None
    return picked


def _format_web_results(items, body_key):
    formatted_results = []
    for i, r in enumerate(items):
        title = r.get('title') or '无标题'
        url = r.get('url') or ''
        body = r.get(body_key) or '无摘要'
        formatted_results.append(f"{i+1}. {title}\n   摘要: {body}\n   链接: {url}")
    return "\n\n".join(formatted_results)


def search_news(query, num_results=5, cancel_event=None):
    query = str(query or "").strip()
    if not query:
        return "🚨 新闻搜索失败：query 不能为空。"
    try:
        try:
            num_results = int(num_results)
        except (TypeError, ValueError):
            num_results = 5
        num_results = max(1, min(num_results, 20))
        _raise_if_cancelled(cancel_event)
        decided_num_results = _decide_search_num_results(query)
        if decided_num_results is not None:
            num_results = max(1, min(decided_num_results, 20))
        # 免费后端内部按需探测取消状态，避免长时间抓取期间无法中断
        cancel_check = cancel_event.is_set if cancel_event is not None else None
        failures = []

        from core.web_search import SearchCancelled, search_web

        try:
            items = search_web(query, num_results, cancel_check=cancel_check)
        except SearchCancelled:
            # 取消是用户意图，不是"后端故障"，不参与兜底也不拼进失败原因
            return "🛑 操作已被用户取消。"
        except Exception as error:
            failures.append(f"360/必应 {type(error).__name__}: {error}")
            items = []
        if items:
            _raise_if_cancelled(cancel_event)
            return _format_web_results(items, "snippet")
        if not failures:
            failures.append("360/必应未解析到结果")

        ddgs_cls = _load_ddgs()
        if ddgs_cls is None:
            failures.append("ddgs 网页搜索组件未安装")
        else:
            try:
                with ddgs_cls() as ddgs:
                    ddgs_items = list(ddgs.news(query, max_results=num_results))
                _raise_if_cancelled(cancel_event)
                if ddgs_items:
                    return _format_web_results(ddgs_items, "body")
                failures.append("ddgs 无结果")
            except _ToolCancelledError:
                return "🛑 操作已被用户取消。"
            except Exception as error:
                failures.append(f"ddgs {type(error).__name__}: {error}")

        # 用 🚨 前缀让 _tool_result_ok 识别为工具失败，进而触发子代理 _terminate("failed")
        # 这样主代理会收到结构化失败文本（含兜底建议），而不是误判为 completed。
        return (
            f"🚨 新闻搜索无结果：查询「{query}」未返回任何网页结果。"
            f"各通道失败原因：{'；'.join(failures)}。"
            f"建议主 Agent 改用 fetch_url 抓取已知站点，或告知用户当前无法联网。"
        )

    except _ToolCancelledError:
        return "🛑 操作已被用户取消。"
    except Exception as e:
        return f"🚨 新闻搜索工具发生异常: {str(e)}"


class _ResponseTooLargeError(ValueError):
    pass


def _build_browser_like_headers():
    return {
        "User-Agent": _BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.google.com/",
    }


def _validate_http_url(url):
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL 必须是完整的 http 或 https 地址")
    return parsed.geturl()


class SSRFBlockedError(ValueError):
    """SSRF 防护拦截的请求"""


_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal", "metadata.azure.com"}


def _is_safe_public_ip(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _get_fetch_url_allow_hosts():
    try:
        import config
        data = config._read_json_with_backup(config._CONFIG_FILE, default=None)
        if not isinstance(data, dict):
            return set()
        hosts = data.get("fetch_url_allow_hosts")
        if not isinstance(hosts, list):
            return set()
        return {str(h).lower().strip() for h in hosts if h}
    except Exception:
        return set()


def _get_fetch_url_strict_ssrf():
    try:
        import config
        data = config._read_json_with_backup(config._CONFIG_FILE, default=None)
        if not isinstance(data, dict):
            return True
        return bool(data.get("fetch_url_strict_ssrf", True))
    except Exception:
        return True


def _check_ssrf_url(url):
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()

    if not host:
        raise SSRFBlockedError("URL 缺少主机名")

    allow_hosts = _get_fetch_url_allow_hosts()
    if host in allow_hosts:
        return

    if host in _METADATA_HOSTS:
        raise SSRFBlockedError(
            f"已阻止访问云元数据接口 {host}（防止 SSRF 泄露凭据）"
        )

    if not _get_fetch_url_strict_ssrf():
        return

    try:
        ipaddress.ip_address(host)
        if not _is_safe_public_ip(host):
            raise SSRFBlockedError(
                f"已阻止访问非公网 IP {host}（防止 SSRF 访问内网）"
            )
        return
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SSRFBlockedError(
            f"DNS 解析失败 {host}：{exc}（保守拒绝以防止 SSRF）"
        )

    if not infos:
        raise SSRFBlockedError(f"DNS 解析返回空结果 {host}（保守拒绝）")

    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip_str = sockaddr[0]
        if not _is_safe_public_ip(ip_str):
            raise SSRFBlockedError(
                f"已阻止访问 {host}（解析到非公网 IP {ip_str}，防止 SSRF）"
            )


def _decode_http_response(response, raw):
    headers = getattr(response, "headers", {})
    content_type = ""
    charset = None
    get_header = getattr(headers, "get", None)
    if get_header is not None:
        content_type = get_header("Content-Type", "") or get_header("content-type", "")
    get_charset = getattr(headers, "get_content_charset", None)
    if get_charset is not None:
        charset = get_charset()
    if not charset:
        match = re.search(r"charset=([\w-]+)", str(content_type), flags=re.IGNORECASE)
        charset = match.group(1) if match else "utf-8"
    return int(getattr(response, "status", getattr(response, "code", 200))), str(content_type), raw.decode(charset, errors="replace")


class _SSRFRecheckRedirectHandler(HTTPRedirectHandler):
    """重定向逐跳 SSRF 校验：302 跳转目标必须重新过 _check_ssrf_url，

    否则攻击者可先给一个合法公网 URL，再通过 302 跳到 169.254.169.254
    或内网地址绕过首次校验。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_ssrf_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# 用带 SSRF 逐跳校验的 opener 替换裸 urlopen；调用签名与 urlopen 兼容
urlopen = build_opener(_SSRFRecheckRedirectHandler()).open


def _request_http_text(url, timeout_seconds=HTTP_TIMEOUT_SECONDS, cancel_event=None):
    normalized_url = _validate_http_url(url)
    _check_ssrf_url(normalized_url)
    timeout = float(timeout_seconds)
    if not 1 <= timeout <= 60:
        raise ValueError("timeout_seconds 必须在 1 到 60 秒之间")
    # urlopen 本身不可中断，至少在发起请求前后检查取消
    _raise_if_cancelled(cancel_event)
    request = Request(
        normalized_url,
        headers=_build_browser_like_headers(),
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(HTTP_MAX_BYTES)
        truncated = False
        peek = response.read(1)
        if peek:
            truncated = True
        status, content_type, text = _decode_http_response(response, raw)
        if truncated:
            text = (
                text
                + f"\n\n[⚠️ 响应超过 {HTTP_MAX_BYTES // 1024} KB 限制，已截断。"
                f"文章正文通常已包含在上方内容中；如需完整内容请缩小请求范围或使用 search_news。]"
            )
        _raise_if_cancelled(cancel_event)
        return status, content_type, text


def _format_http_error(error):
    if isinstance(error, HTTPError):
        return f"HTTP {error.code} {error.reason}"
    if isinstance(error, URLError):
        return f"网络连接失败：{error.reason}"
    return str(error)


def fetch_url(url, timeout_seconds=HTTP_TIMEOUT_SECONDS, cancel_event=None):
    """通过本机 HTTP 客户端获取网页或 JSON 文本。"""
    try:
        status, content_type, text = _request_http_text(url, timeout_seconds, cancel_event)
        header = f"HTTP {status}"
        if content_type:
            header += f" ({content_type})"
        return f"{header}\n{text}"
    except _ToolCancelledError:
        return "🛑 操作已被用户取消。"
    except SSRFBlockedError as exc:
        # SSRF 防护拦截：清晰提示用户被阻止的原因，并说明如何加白名单或关闭严格模式
        msg = str(exc)
        hint_lines = []
        # 云元数据接口拦截：只提示白名单（这类目标绝不应该放行，但保留 opt-in 通道）
        if "元数据接口" in msg:
            hint_lines.append(
                "如确需访问，请在 ~/.ycode/config.json 的 fetch_url_allow_hosts "
                "数组中显式添加该主机名以 opt-in。"
            )
        else:
            hint_lines.append(
                "可能原因：本机使用了 Clash/V2Ray 等代理软件的 fake-ip 模式，"
                "DNS 解析到 198.18.x.x 等保留段被误判为内网。"
            )
            hint_lines.append(
                "解决方案（任选其一）：\n"
                "  1. 在 ~/.ycode/config.json 的 fetch_url_allow_hosts 数组中"
                "添加该主机名以 opt-in（推荐，精确放行）\n"
                "  2. 在 ~/.ycode/config.json 中设置 "
                "\"fetch_url_strict_ssrf\": false 降级为宽松模式"
                "（默认严格；使用 Clash/V2Ray 代理时可考虑降级）"
            )
        return f"🚨 {msg}\n" + "\n".join(hint_lines)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return f"🚨 FetchURL 请求失败：{_format_http_error(exc)}"
    except Exception as exc:
        return f"🚨 FetchURL 请求发生异常：{exc}"


# fetch_image 专用：图片下载体积上限 10MB，分块累计超限立即中止，
# 防止把内存/磁盘打爆
IMAGE_MAX_BYTES = 10 * 1024 * 1024
_IMAGE_CONTENT_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def fetch_image(url, timeout_seconds=HTTP_TIMEOUT_SECONDS, cancel_event=None):
    """下载网页中的图片到本地并返回本地绝对路径。"""
    try:
        normalized_url = _validate_http_url(url)
        _check_ssrf_url(normalized_url)
        timeout = float(timeout_seconds)
        if not 1 <= timeout <= 60:
            raise ValueError("timeout_seconds 必须在 1 到 60 秒之间")
        # urlopen 本身不可中断，至少在发起请求前后检查取消
        _raise_if_cancelled(cancel_event)
        request = Request(
            normalized_url,
            headers=_build_browser_like_headers(),
        )
        with urlopen(request, timeout=timeout) as response:
            headers = getattr(response, "headers", {})
            get_header = getattr(headers, "get", None)
            content_type = ""
            if get_header is not None:
                content_type = get_header("Content-Type", "") or get_header("content-type", "")
            content_type = str(content_type).split(";", 1)[0].strip().lower()
            extension = _IMAGE_CONTENT_TYPES.get(content_type)
            if extension is None:
                raise ValueError(
                    f"不支持的内容类型 {content_type or '未知'}"
                    "（仅支持 png/jpg/jpeg/gif/webp；svg 有脚本注入风险，已拒绝）"
                )
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            data = bytearray()
            while True:
                _raise_if_cancelled(cancel_event)
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > IMAGE_MAX_BYTES:
                    raise _ResponseTooLargeError(
                        f"图片体积超过 {IMAGE_MAX_BYTES // (1024 * 1024)}MB 限制，已中止下载"
                    )
            _raise_if_cancelled(cancel_event)
        from core.ai_output_paths import resolve_ai_output_dir

        target_dir = resolve_ai_output_dir()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"download_{timestamp}.{extension}"
        target = target_dir / filename
        # 同秒重复/并发下载时避免互相覆盖
        counter = 2
        while target.exists():
            filename = f"download_{timestamp}_{counter}.{extension}"
            target = target_dir / filename
            counter += 1
        payload = bytes(data)
        from core.turn_undo import coordinator as _tu_coordinator
        from core.turn_undo import recorder as _tu_recorder
        from core.turn_undo.models import NodeKind, TurnUndoError

        coordinator = _tu_coordinator.get_coordinator()
        handle = None
        with coordinator.workspace_lock(_tu_coordinator.thread_workspace_root()):
            try:
                handle = _tu_recorder.prepare_mutation(
                    display_path=str(target),
                    after_kind=NodeKind.REGULAR_FILE,
                    after_content=payload,
                    tool_name="fetch_image",
                    tool_call_id="",
                )
            except TurnUndoError as error:
                if error.code == "no_active_checkpoint":
                    handle = None
                elif error.code == "workspace_busy":
                    return (
                        f"🚨 该文件正在被另一个回合写入，图片未写入（{target}）：\n"
                        f"{error.message}\n请稍后重试。"
                    )
                else:
                    return (
                        f"🚨 该回合的撤销检查点已封闭，迟到的图片写入已被拒绝（{target}）：\n"
                        f"{error.message}"
                    )
            target.write_bytes(payload)
            if handle is not None:
                try:
                    _tu_recorder.verify_after_mutation(handle)
                except TurnUndoError as error:
                    return (
                        f"🚨 图片已下载但未能通过可撤销性验证（{target}）：\n{error.message}"
                    )
        return (
            f"HTTP {status} ({content_type})\n"
            f"图片已下载到本地：{target}（{len(payload)} 字节）\n"
            f"GUI 会话中用 Markdown 图片语法 ![]({target}) 内嵌展示；CLI 终端直接给出该路径即可。"
        )
    except _ToolCancelledError:
        return "🛑 操作已被用户取消。"
    except SSRFBlockedError as exc:
        return f"🚨 {exc}"
    except _ResponseTooLargeError as exc:
        return f"🚨 {exc}"
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return f"🚨 FetchImage 请求失败：{_format_http_error(exc)}"
    except Exception as exc:
        return f"🚨 FetchImage 请求发生异常：{exc}"


def _weather_value(value, default="未知"):
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("value", default)
    return str(value if value not in (None, "") else default)


_WEATHER_CODES = {
    0: "晴",
    1: "大部晴朗",
    2: "少云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    95: "雷雨",
    96: "雷雨伴冰雹",
    99: "强雷雨伴冰雹",
}
_WIND_DIRECTIONS = (
    "北", "东北", "东", "东南", "南", "西南", "西", "西北"
)


def _open_meteo_value(values, index, default="未知"):
    if not isinstance(values, list) or index >= len(values):
        return default
    value = values[index]
    return default if value in (None, "") else str(value)


def _weather_code_label(value):
    try:
        return _WEATHER_CODES.get(int(value), f"天气代码 {value}")
    except (TypeError, ValueError):
        return "未知"


def _wind_direction_label(value):
    try:
        return _WIND_DIRECTIONS[int((float(value) + 22.5) // 45) % 8]
    except (TypeError, ValueError):
        return "未知"


def _format_open_meteo_payload(payload, location, forecast_url):
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    current_lines = [
        "🌤 实时天气（Open-Meteo）",
        f"城市：{location}",
        f"当前温度：{_weather_value(current.get('temperature_2m'))}°C",
        f"体感温度：{_weather_value(current.get('apparent_temperature'))}°C",
        f"天气：{_weather_code_label(current.get('weather_code'))}",
        f"湿度：{_weather_value(current.get('relative_humidity_2m'))}%",
        (
            "风向/风速："
            f"{_wind_direction_label(current.get('wind_direction_10m'))} "
            f"{_weather_value(current.get('wind_speed_10m'))} km/h"
        ),
        "",
        "未来预报：",
    ]
    dates = daily.get("time") or []
    for index, date in enumerate(dates[:3]):
        forecast = (
            f"{date}：{_weather_code_label(_open_meteo_value(daily.get('weather_code'), index))}，"
            f"{_open_meteo_value(daily.get('temperature_2m_min'), index)}°C ~ "
            f"{_open_meteo_value(daily.get('temperature_2m_max'), index)}°C"
        )
        rain = _open_meteo_value(daily.get("precipitation_probability_max"), index, "")
        if rain:
            forecast += f"，降雨概率 {rain}%"
        sunrise = _open_meteo_value(daily.get("sunrise"), index, "")
        sunset = _open_meteo_value(daily.get("sunset"), index, "")
        if sunrise and sunset:
            forecast += f"，日出 {sunrise[-5:]}，日落 {sunset[-5:]}"
        current_lines.append(forecast)
    if not dates:
        current_lines.append("暂无预报数据")
    current_lines.extend(["", f"数据来源：{forecast_url}"])
    return "\n".join(current_lines)


def get_weather(city="", timeout_seconds=HTTP_TIMEOUT_SECONDS, cancel_event=None):
    """通过 Open-Meteo 地理编码和预报接口获取指定城市天气。"""
    requested_city = str(city or "").strip()
    if not requested_city:
        return "🚨 天气查询失败：必须先提供城市名称，不能猜测所在城市"
    encoded_city = quote(requested_city, safe="")
    geocoding_url = (
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={encoded_city}&count=1&language=zh&format=json"
    )
    try:
        _, _, geocoding_text = _request_http_text(geocoding_url, timeout_seconds, cancel_event)
        geocoding_payload = json.loads(geocoding_text)
        locations = geocoding_payload.get("results") or []
        if not locations:
            return f"🚨 天气查询失败：没有找到城市「{requested_city}」"
        location_data = locations[0]
        latitude = location_data.get("latitude")
        longitude = location_data.get("longitude")
        if latitude is None or longitude is None:
            raise ValueError("地理编码结果缺少经纬度")
        location = " ".join(
            value for value in (
                str(location_data.get("country") or "").strip(),
                str(location_data.get("name") or requested_city).strip(),
            ) if value
        )
        forecast_url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={quote(str(latitude))}&longitude={quote(str(longitude))}"
            "&current=temperature_2m,apparent_temperature,weather_code,"
            "relative_humidity_2m,wind_speed_10m,wind_direction_10m"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,sunrise,sunset"
            "&timezone=auto&forecast_days=3"
        )
        status, _, forecast_text = _request_http_text(forecast_url, timeout_seconds, cancel_event)
        forecast_payload = json.loads(forecast_text)
        if not isinstance(forecast_payload, dict):
            raise ValueError("天气服务返回的数据格式不是 JSON 对象")
        return f"HTTP {status}\n" + _format_open_meteo_payload(
            forecast_payload,
            location,
            forecast_url,
        )
    except json.JSONDecodeError:
        return "🚨 天气查询失败：天气服务返回的不是有效 JSON"
    except _ToolCancelledError:
        return "🛑 操作已被用户取消。"
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return f"🚨 天气查询失败：{_format_http_error(exc)}"
    except Exception as exc:
        return f"🚨 天气查询发生异常：{exc}"
    
try:
    SEARCH_CODE_TIMEOUT_SECONDS = max(1, int(os.environ.get("YCODE_SEARCH_TIMEOUT", "30") or 30))
except (TypeError, ValueError):
    SEARCH_CODE_TIMEOUT_SECONDS = 30


def _run_process_polled(cmd, timeout_seconds, cancel_event=None):
    from core.task_scheduler import _stop_process_tree

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    deadline = time.monotonic() + timeout_seconds
    while True:
        if cancel_event is not None and cancel_event.is_set():
            _stop_process_tree(proc)
            try:
                proc.communicate(timeout=2)
            except Exception:
                pass
            return None
        try:
            stdout, stderr = proc.communicate(timeout=0.2)
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                _stop_process_tree(proc)
                try:
                    proc.communicate(timeout=2)
                except Exception:
                    pass
                raise subprocess.TimeoutExpired(cmd, timeout_seconds)


# search_code 的默认排除目录（Python 兜底路径使用）。
# 可通过 config.json 的 "search_exclude_dirs": ["target", "out", ...] 追加自定义排除，
# ripgrep 路径与 Python 兜底路径均生效（rg 以 --glob '!**/<dir>/' 形式下发）。
_SEARCH_DEFAULT_IGNORE_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.idea', '.vscode', '.ycode', 'dist', 'build'}


def _get_search_exclude_dirs():
    dirs = set(_SEARCH_DEFAULT_IGNORE_DIRS)
    try:
        from core.model_manager import load_config
        extra = load_config().get("search_exclude_dirs")
        if isinstance(extra, (list, tuple, set)):
            dirs.update(str(d).strip() for d in extra if str(d).strip())
    except Exception:
        pass
    return dirs


def search_code(
    query,
    dir_path=".",
    cancel_event=None,
    glob="",
    output_mode="content",
    case_sensitive=False,
    context_lines=1,
):
    """精准搜索：跨文件全局查找代码片段或关键字。优先尝试 ripgrep。"""
    import fnmatch
    import subprocess

    try:
        dir_path = str(_resolve_workspace_path(dir_path))
    except ValueError as e:
        return f"🚨 搜索失败: {e}"

    if cancel_event is not None and cancel_event.is_set():
        return "🛑 搜索已被用户取消。"

    mode = str(output_mode or "content").strip().lower()
    if mode not in {"content", "files", "count"}:
        mode = "content"
    glob_pattern = str(glob or "").strip()
    is_case_sensitive = bool(case_sensitive)
    ctx_lines = max(0, min(int(context_lines if context_lines is not None else 1), 5))

    cache_key = _tool_cache.search_key(
        "search_code",
        (query, glob_pattern, mode, is_case_sensitive, ctx_lines),
        dir_path,
    )
    cached = _tool_cache.search_get(cache_key)
    if cached is not None:
        return cached

    session_id = _tool_cache.get_current_session()
    if session_id:
        shared_key = ("search_code", cache_key)
        shared = _tool_cache.shared_get(session_id, shared_key)
        if shared is not None:
            return shared

    def _run_rg(args_query, literal=False):
        cmd = [rg_executable]
        if is_case_sensitive:
            cmd.append("-s")
        else:
            cmd.append("-i")
        if mode == "files":
            cmd.append("-l")
        elif mode == "count":
            cmd.append("--count")
        else:
            cmd += [
                "-n",
                "--no-heading",
                "--max-columns",
                "200",
                "-C",
                str(ctx_lines),
                "-m",
                "20",
            ]
        # 统一排除构建产物目录：git 仓库 rg 靠 .gitignore 排除，
        # 非 git 目录必须显式下发，否则会命中 target/dist 产物
        for _d in sorted(_get_search_exclude_dirs()):
            cmd += ["--glob", f"!**/{_d}/"]
        if glob_pattern:
            cmd += ["--glob", glob_pattern]
        if literal:
            cmd.append("-F")
        cmd += [args_query, dir_path]
        return _run_process_polled(cmd, SEARCH_CODE_TIMEOUT_SECONDS, cancel_event)

    def _format_rg_output(output):
        if not output:
            return "未找到匹配的代码片段。"
        lines = output.split("\n")
        max_lines = 100 if mode in {"files", "count"} else 80
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n\n... (匹配结果过多，已截断前 {max_lines} 行，共 {len(lines)} 行)"
        return output

    rg_executable = None
    try:
        rg_executable = _get_rg_executable()
    except Exception:
        rg_executable = None

    if rg_executable:
        # ripgrep 路径：坏正则时自动降级为字面量搜索重试；
        # 超时/其他失败直接报错返回，绝不再落到全仓 Python 遍历
        # （慢查询叠加一个更慢的遍历只会让卡顿翻倍）。
        try:
            result = _run_rg(query)
            if result is None:
                return "🛑 搜索已被用户取消。"
            if result.returncode == 2:
                result = _run_rg(query, literal=True)
                if result is None:
                    return "🛑 搜索已被用户取消。"
            if result.returncode in (0, 1):
                result_text = _format_rg_output(result.stdout.strip())
                _tool_cache.search_put(cache_key, result_text)
                if session_id:
                    _tool_cache.shared_put(session_id, ("search_code", cache_key), result_text)
                return result_text
            return f"🚨 搜索失败（ripgrep 退出码 {result.returncode}）：{(result.stderr or '').strip()[:300]}"
        except subprocess.TimeoutExpired:
            return (
                f"🚨 搜索超时（{SEARCH_CODE_TIMEOUT_SECONDS} 秒）。目录可能过大，请缩小 dir_path 范围"
                "或使用更精确的关键字重试。"
            )
        except Exception:
            # rg 意外不可用（进程启动失败等）：走下面的 Python 兜底
            pass

    IGNORE_DIRS = _get_search_exclude_dirs()
    ALLOWED_EXTS = {
        '.py', '.js', '.ts', '.html', '.css', '.json', '.md', '.txt', 
        '.java', '.c', '.cpp', '.h', '.go', '.rs', '.php', '.rb', '.sh',
        '.yml', '.yaml', '.toml', '.ini', '.cfg', '.xml'
    }
    
    results = []
    match_count = 0
    max_matches = 100 if mode in {"files", "count"} else 50
    query_cmp = query if is_case_sensitive else query.lower()

    try:
        for root, dirs, files in os.walk(dir_path):
            if cancel_event is not None and cancel_event.is_set():
                return "🛑 搜索已被用户取消。"
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith('.')]
            
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if not glob_pattern and ext not in ALLOWED_EXTS:
                    continue
                    
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, dir_path).replace("\\", "/")
                if glob_pattern and not (
                    fnmatch.fnmatch(file, glob_pattern) or fnmatch.fnmatch(rel_path, glob_pattern)
                ):
                    continue
                
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        lines = f.readlines()

                    file_hits = 0
                    for line_num, line in enumerate(lines, 1):
                        hay = line if is_case_sensitive else line.lower()
                        if query_cmp in hay:
                            file_hits += 1
                            if mode == "content":
                                start = max(0, line_num - 1 - ctx_lines)
                                end = min(len(lines), line_num + ctx_lines)
                                snippet = "".join(lines[start:end])
                                results.append(f"📄 {file_path} (行 {line_num}):\n{snippet.strip()}")
                                match_count += 1
                                if match_count >= max_matches:
                                    results.append(f"\n⚠️ 搜索结果太多，已截断前 {max_matches} 条，请尝试更精确的关键字。")
                                    result_text = "\n---\n".join(results)
                                    _tool_cache.search_put(cache_key, result_text)
                                    return result_text
                    if file_hits > 0 and mode in {"files", "count"}:
                        results.append(rel_path if mode == "files" else f"{rel_path}:{file_hits}")
                        match_count += 1
                        if match_count >= max_matches:
                            break
                except Exception:
                    continue
            if match_count >= max_matches:
                break
                    
        if not results:
            result_text = f"🔍 搜索完毕，未在代码文件中找到包含 `{query}` 的内容。"
        else:
            sep = "\n" if mode in {"files", "count"} else "\n---\n"
            result_text = sep.join(results)
        _tool_cache.search_put(cache_key, result_text)
        if session_id:
            _tool_cache.shared_put(session_id, ("search_code", cache_key), result_text)
        return result_text
    except Exception as e:
        return f"🚨 搜索失败: {str(e)}"

def _normalize_wait_timeout(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return "🚨 错误：timeout_seconds 必须是 1 到 60 的整数。"
    if not 1 <= value <= 60:
        return "🚨 错误：timeout_seconds 必须是 1 到 60 的整数。"
    return value


# manage_task 子代理快照的 result 摘要上限：与 agent_service._BG_RESULT_MAX_CHARS
# （自动汇总回流上限）对齐，避免回合内主动查询与回合后回流重复灌入大段相同内容。
_SUBAGENT_STATUS_RESULT_MAX_CHARS = 1500
_SUBAGENT_STATUS_DESC_MAX_CHARS = 120


def manage_task(action, task_id=None, after_revision=None, timeout_seconds=30, event_callback=None, permission_owner=None, cancel_event=None):
    from core.task_manager import (
        KILL_RESULT_FAILED, KILL_RESULT_IDENTITY_UNVERIFIABLE,
        KILL_RESULT_KILLED, KILL_RESULT_NATURAL_DEATH, KILL_RESULT_NOT_FOUND,
        get_task_snapshot, kill_background_task_detailed, list_task_snapshots,
        wait_task_change,
    )

    def _snapshot_payload(snap, changed=None):
        payload = {
            "kind": "shell",
            "task_id": snap.task_id,
            "status": snap.status,
            "revision": snap.revision,
            "terminal": snap.terminal,
            "exit_code": snap.exit_code,
            "output": snap.output,
        }
        if changed is not None:
            payload["changed"] = changed
        return payload

    def _subagent_snapshot_payload(snap, changed=None, include_result=False):
        description = str(snap.description or "")
        if len(description) > _SUBAGENT_STATUS_DESC_MAX_CHARS:
            description = description[:_SUBAGENT_STATUS_DESC_MAX_CHARS] + "…"
        payload = {
            "kind": "subagent",
            "task_id": snap.task_id,
            "status": snap.status,
            "subagent_type": snap.subagent_type,
            "run_in_background": snap.run_in_background,
            "stage": snap.stage,
            "execution_elapsed": round(float(snap.execution_elapsed or 0.0), 1),
            "terminal": snap.status in _SUBAGENT_TERMINAL_STATUSES,
            "description": description,
        }
        if include_result:
            result = str(snap.result or "")
            if result:
                result = _truncate_subagent_result(
                    result,
                    _SUBAGENT_STATUS_RESULT_MAX_CHARS,
                    "… [结果已截断，省略 {omitted} 字符；完整结果将在回合结束后的自动汇总中注入]",
                )
                payload["result"] = result
            if snap.error:
                payload["error"] = str(snap.error)
        if changed is not None:
            payload["changed"] = changed
        return payload

    if action == "list":
        running = [s for s in list_task_snapshots() if not s.terminal]
        subagent_running = [
            s for s in subagent_manager.snapshot()
            if s.status not in _SUBAGENT_TERMINAL_STATUSES
        ]
        if not running and not subagent_running:
            return "ℹ 当前没有任何运行中的后台异步任务。"
        tasks = [_snapshot_payload(s) for s in running]
        tasks.extend(_subagent_snapshot_payload(s) for s in subagent_running)
        return json.dumps({"tasks": tasks}, ensure_ascii=False)

    elif action == "status":
        if not task_id:
            return "🚨 错误：查看状态必须提供 task_id 参数。"
        if is_subagent_task_id(task_id):
            snap = subagent_manager.task_snapshot(task_id)
            if snap is None:
                return f"🚨 未找到任务 {task_id}。"
            return json.dumps(
                _subagent_snapshot_payload(snap, include_result=True),
                ensure_ascii=False,
            )
        snap = get_task_snapshot(task_id)
        if snap is None:
            return f"🚨 未找到任务 {task_id}。"
        return json.dumps(_snapshot_payload(snap), ensure_ascii=False)

    elif action == "wait":
        if not task_id:
            return "🚨 错误：等待任务必须提供 task_id 参数。"
        timeout = _normalize_wait_timeout(timeout_seconds)
        if isinstance(timeout, str):
            return timeout
        if is_subagent_task_id(task_id):
            snap = subagent_manager.wait_terminal(
                task_id, timeout, cancel_event=cancel_event
            )
            if snap is None:
                return f"🚨 未找到任务 {task_id}。"
            return json.dumps(
                _subagent_snapshot_payload(
                    snap, changed=snap.status in _SUBAGENT_TERMINAL_STATUSES,
                    include_result=True,
                ),
                ensure_ascii=False,
            )
        if after_revision is None:
            after_revision = 0
        if isinstance(after_revision, bool) or not isinstance(after_revision, int):
            return "🚨 错误：after_revision 必须是非负整数。"
        if after_revision < 0:
            return "🚨 错误：after_revision 必须是非负整数。"
        snap = wait_task_change(
            task_id, after_revision, timeout, cancel_event=cancel_event
        )
        if snap is None:
            return f"🚨 未找到任务 {task_id}。"
        return json.dumps(
            _snapshot_payload(snap, changed=snap.revision > after_revision),
            ensure_ascii=False,
        )

    elif action == "kill":
        if not task_id:
            return "🚨 错误：终结任务必须提供 task_id 参数。"
        # 只在 kill 类操作时要求用户确认，list/status/wait 只读操作不弹确认
        from core.llm import check_permission_and_ask, PermissionSystemError
        try:
            approved = check_permission_and_ask(
                "manage_task",
                {"action": "kill", "task_id": task_id},
                event_callback=event_callback,
                permission_owner=permission_owner,
                cancel_event=cancel_event,
            )
        except PermissionSystemError:
            return "🚨 权限询问系统异常，未能终结后台任务。请稍后重试。"
        if not approved:
            return "🚨 用户拒绝了终结后台任务的操作。"
        if is_subagent_task_id(task_id):
            if subagent_manager.cancel(task_id, reason="user requested via manage_task"):
                return (
                    f"✔ 后台子代理任务 {task_id} 已请求取消（协作式，任务会尽快退出）；"
                    "可用 manage_task status 跟踪其终态。"
                )
            return (
                f"🚨 未能取消任务 {task_id}（可能不存在或已进入终态）；"
                "可用 manage_task status 查看当前状态。"
            )
        success = kill_background_task_detailed(task_id)
        if success == KILL_RESULT_KILLED:
            return f"✔ 后台任务 {task_id} 已被强行终结清除。"
        if success == KILL_RESULT_NATURAL_DEATH:
            return (
                f"ℹ 后台任务 {task_id} 在终结前已自行结束，终态按自然退出结果落定，"
                "无需强行终止；可用 manage_task status 查看其最终状态与输出。"
            )
        if success == KILL_RESULT_NOT_FOUND:
            return f"🚨 未找到任务 {task_id}。"
        if success == KILL_RESULT_IDENTITY_UNVERIFIABLE:
            return (
                f"🚨 未能终结任务 {task_id}：无法校验进程身份（psutil 缺失或权限不足），"
                "已按保守策略拒绝终止，避免误杀 PID 被复用的无关进程。"
            )
        return f"🚨 终止后台任务 {task_id} 失败，进程可能仍在运行，请稍后重试。"

    else:
        return "🚨 错误：未知行动参数。可选值有 'list', 'status', 'wait', 'kill'。"

def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _shift_replacement_indent(replacement: str, mode: str, delta: str, newline: str) -> str:
    if not delta:
        return replacement
    ends_with_nl = replacement.endswith(("\r\n", "\n"))
    lines = replacement.splitlines()
    out: list[str] = []
    for line in lines:
        if not line.strip():
            out.append("")
        elif mode == "add":
            out.append(delta + line)
        elif line.startswith(delta):
            out.append(line[len(delta) :])
        else:
            out.append(line.lstrip(" \t"))
    joined = newline.join(out)
    if ends_with_nl and out:
        joined += newline
    return joined


def _indent_normalized_replace(content: str, target: str, replacement: str, replace_all: bool = False):
    """按统一缩进偏移匹配多行 target（至少 2 行），兼容整块少/多缩进几个空格的场景。"""
    target_lines_raw = target.splitlines()
    if len(target_lines_raw) < 2 or not any(ln.strip() for ln in target_lines_raw):
        return content, 0

    target_stripped = [ln.lstrip(" \t") for ln in target_lines_raw]
    content_lines = content.splitlines(keepends=True)
    n = len(target_lines_raw)
    if len(content_lines) < n:
        return content, 0

    matches: list[tuple[int, int, str, str]] = []
    newline = "\r\n" if "\r\n" in content else "\n"
    line_offsets: list[int] = [0]
    for cl in content_lines:
        line_offsets.append(line_offsets[-1] + len(cl))

    i = 0
    while i <= len(content_lines) - n:
        window = content_lines[i : i + n]
        mode_delta: tuple[str, str] | None = None
        matched = True
        for k in range(n):
            c_no_nl = window[k].rstrip("\r\n")
            if not c_no_nl.strip() and not target_lines_raw[k].strip():
                continue
            if c_no_nl.lstrip(" \t") != target_stripped[k]:
                matched = False
                break
            c_ws = _leading_ws(c_no_nl)
            t_ws = _leading_ws(target_lines_raw[k])
            if c_ws == t_ws:
                cur = ("none", "")
            elif c_ws.endswith(t_ws):
                cur = ("add", c_ws[: len(c_ws) - len(t_ws)])
            elif t_ws.endswith(c_ws):
                cur = ("strip", t_ws[: len(t_ws) - len(c_ws)])
            else:
                matched = False
                break
            if mode_delta is None:
                mode_delta = cur
            elif mode_delta != cur:
                matched = False
                break
        if matched and mode_delta is not None:
            start_pos = line_offsets[i]
            if target.endswith(("\r\n", "\n")):
                end_pos = line_offsets[i + n]
            else:
                last_line = window[-1]
                nl_len = 2 if last_line.endswith("\r\n") else (1 if last_line.endswith("\n") else 0)
                end_pos = line_offsets[i + n] - nl_len
            matches.append((start_pos, end_pos, mode_delta[0], mode_delta[1]))
            i += n
        else:
            i += 1

    if not matches:
        return content, 0
    if len(matches) > 1 and not replace_all:
        return content, len(matches)

    result = content
    for start_pos, end_pos, mode, delta in reversed(matches):
        shifted = _shift_replacement_indent(replacement, mode, delta, newline)
        result = result[:start_pos] + shifted + result[end_pos:]
    return result, len(matches)


def _closest_match_hint(content: str, target: str) -> str:
    import difflib

    content_lines = content.splitlines()
    target_lines = target.splitlines()
    if not content_lines or not target_lines or len(content_lines) > 5000:
        return ""
    win = max(1, min(len(target_lines), len(content_lines)))
    target_norm = "\n".join(ln.strip() for ln in target_lines if ln.strip())
    if not target_norm:
        return ""
    keywords = {ln.strip() for ln in target_lines if len(ln.strip()) >= 6}

    scored_indices: list[tuple[int, int]] = []
    anchor_tokens = [tok for tok in target_lines[0].strip().split() if len(tok) >= 3][:3]
    for i in range(0, len(content_lines) - win + 1):
        chunk = content_lines[i : i + win]
        hits = sum(1 for ln in chunk if ln.strip() in keywords) if keywords else 0
        if not hits and anchor_tokens:
            joined_lower = " ".join(chunk)
            hits = sum(1 for tok in anchor_tokens if tok in joined_lower)
        if hits > 0:
            scored_indices.append((hits, i))

    if not scored_indices:
        return ""
    scored_indices.sort(reverse=True)
    best_ratio = 0.0
    best_span = (0, win)
    for _, i in scored_indices[:6]:
        cand = "\n".join(ln.strip() for ln in content_lines[i : i + win] if ln.strip())
        if not cand:
            continue
        ratio = difflib.SequenceMatcher(None, target_norm, cand).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_span = (i, i + win)

    if best_ratio < 0.45:
        return ""
    start_i, end_i = best_span
    preview_lines = [
        f"  {lineno}: {content_lines[lineno - 1]}"
        for lineno in range(start_i + 1, min(end_i + 1, start_i + 7))
    ]
    if end_i - start_i > 6:
        preview_lines.append("  ...")
    return (
        f"\n最接近的代码段位于第 {start_i + 1}-{end_i} 行（相似度 {best_ratio:.0%}）：\n"
        + "\n".join(preview_lines)
    )


def _fuzzy_replace(content, target, replacement, replace_all=False):
    import difflib

    target_len = len(target)
    if target_len == 0:
        return content, 0

    occurrences = content.count(target)
    if occurrences == 1:
        return content.replace(target, replacement, 1), 1
    if occurrences > 1:
        if replace_all:
            return content.replace(target, replacement), occurrences
        return content, occurrences

    stripped_target = target.strip("\r\n\t ")
    if stripped_target and stripped_target != target:
        stripped_occ = content.count(stripped_target)
        if stripped_occ == 1:
            start = content.find(stripped_target)
            end = start + len(stripped_target)
            while start > 0 and content[start - 1] in "\r\n\t ":
                start -= 1
            while end < len(content) and content[end] in "\r\n\t ":
                end += 1
            return content[:start] + replacement + content[end:], 1
        if stripped_occ > 1:
            return content, stripped_occ

    new_content, indent_occ = _indent_normalized_replace(
        content, target, replacement, replace_all=replace_all
    )
    if indent_occ > 0:
        return new_content, indent_occ

    target_lines = target.splitlines(keepends=True)
    if len(target_lines) >= 2:
        first_line_text = target_lines[0].strip()
        last_line_text = target_lines[-1].strip()
        if first_line_text and last_line_text:
            content_lines = content.splitlines(keepends=True)
            candidates = []
            for i, line in enumerate(content_lines):
                if first_line_text not in line:
                    continue
                j = i + len(target_lines)
                if j > len(content_lines):
                    continue
                if last_line_text not in content_lines[j - 1]:
                    continue
                candidate = "".join(content_lines[i:j])
                ratio = difflib.SequenceMatcher(None, target, candidate).ratio()
                candidates.append((ratio, i, j, candidate))
            if candidates:
                candidates.sort(reverse=True)
                best_ratio, i, j, candidate = candidates[0]
                if best_ratio >= 0.90 and abs(len(candidate) - target_len) <= max(target_len // 10, 10):
                    start = sum(len(ln) for ln in content_lines[:i])
                    end = sum(len(ln) for ln in content_lines[:j])
                    return content[:start] + replacement + content[end:], 1

    content_len = len(content)
    if target_len <= 2000 and content_len <= 200_000:
        best_ratio = 0.0
        best_start = 0
        anchor_len = min(50, max(1, target_len // 4))
        anchor = target[:anchor_len]
        search_start = 0
        window_radius = 200
        while True:
            anchor_idx = content.find(anchor, search_start)
            if anchor_idx == -1:
                break
            region_start = max(0, anchor_idx - window_radius)
            region_end = min(content_len - target_len + 1, anchor_idx + window_radius)
            for i in range(region_start, region_end + 1):
                window = content[i : i + target_len]
                ratio = difflib.SequenceMatcher(None, target, window).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_start = i
            search_start = anchor_idx + 1

        if best_ratio >= 0.90:
            best_end = best_start + target_len
            return content[:best_start] + replacement + content[best_end:], 1

    return content, 0


def _edit_file_by_lines(
    file_path, resolved, content, encoding, start_line, end_line, replacement_content,
    expected_mtime_ns=None,
):
    if start_line is None or end_line is None:
        return (
            f"🚨 行号编辑失败！start_line 和 end_line 必须同时提供"
            f"（当前 start_line={start_line}, end_line={end_line}）。"
        )
    if isinstance(start_line, bool) or isinstance(end_line, bool):
        return "🚨 行号编辑失败！start_line 和 end_line 不能是布尔值。"
    if not isinstance(start_line, int) or not isinstance(end_line, int):
        return (
            "🚨 行号编辑失败！start_line 和 end_line 必须是整数"
            f"（当前 start_line={type(start_line).__name__}, "
            f"end_line={type(end_line).__name__}）。"
        )
    if start_line < 1:
        return f"🚨 行号编辑失败！start_line 必须 >= 1（当前 {start_line}）。"
    if end_line < start_line:
        return (
            f"🚨 行号编辑失败！end_line({end_line}) 不能小于 start_line({start_line})。"
        )

    lines = content.splitlines(keepends=True)
    total = len(lines)
    if total == 0:
        return f"🚨 行号编辑失败！文件 {file_path} 为空，无法按行号编辑。"
    if start_line > total:
        return (
            f"🚨 行号编辑失败！start_line={start_line} 超出文件总行数 {total}。\n"
            f"建议：重新 read_file(with_line_numbers=True) 获取最新行号。"
        )
    actual_end = min(end_line, total)

    replaced_block = "".join(lines[start_line - 1:actual_end])

    replacement = str(replacement_content or "")

    if "\r\n" in content:
        line_sep = "\r\n"
    else:
        line_sep = "\n"
    replacement_lines = replacement.splitlines()
    replacement = "".join(line + line_sep for line in replacement_lines)

    new_content = (
        "".join(lines[:start_line - 1]) + replacement + "".join(lines[actual_end:])
    )

    if new_content == content:
        return (
            f"🚨 行号编辑失败！第 {start_line}-{actual_end} 行替换后内容与原文相同，"
            f"未发生实际修改（{file_path}）。"
        )

    # 按回合撤销记录写入：prepare → 原子写 → verify（无回合检查点时退化为普通写入）
    write_err = _recorded_atomic_write(
        resolved, encoding, new_content,
        file_path=file_path, expected_mtime_ns=expected_mtime_ns,
    )
    if write_err:
        return write_err

    replaced_preview_lines = replaced_block.splitlines()
    if len(replaced_preview_lines) <= 5:
        preview = replaced_block.rstrip("\r\n")
    else:
        preview = "\n".join(
            replaced_preview_lines[:3]
            + ["... (省略中间部分) ..."]
            + replaced_preview_lines[-2:]
        )

    _publish_edit_diff(content, new_content)
    diag = _check_post_edit_diagnostics(resolved, new_content)
    return (
        f"✔ 文件 {file_path} 第 {start_line}-{actual_end} 行替换成功！\n"
        f"被替换原文预览：\n{preview}{diag}"
    )


def _atomic_write_edit_file(resolved, encoding, new_content, file_path=None, expected_mtime_ns=None):
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return (
            f"🚨 创建父目录失败：{resolved.parent}\n"
            f"错误：{e}\n"
            f"请检查路径是否正确，或权限是否充足。"
        )
    fd, temp_name = tempfile.mkstemp(
        prefix=resolved.name + ".", suffix=".ycode-tmp", dir=str(resolved.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as temp_file:
            temp_file.write(new_content.encode(encoding))
        if expected_mtime_ns is not None:
            conflict = _mtime_conflict_error(file_path or str(resolved), resolved, expected_mtime_ns)
            if conflict:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return conflict
        os.replace(temp_path, resolved)
    except BaseException as e:
        # 使用 BaseException 确保 UnicodeEncodeError、KeyboardInterrupt 等非 OSError 异常
        # 同样能触发临时文件清理，避免孤儿 .ycode-tmp 文件在磁盘积累
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return f"🚨 精准修改发生异常: {str(e)}"
    return None


def _recorded_atomic_write(resolved, encoding, new_content, *, file_path=None,
                           expected_mtime_ns=None, tool_name="edit_file"):
    if expected_mtime_ns is not None:
        conflict = _mtime_conflict_error(
            file_path or str(resolved), resolved, expected_mtime_ns
        )
        if conflict:
            return conflict

    from core.turn_undo import coordinator as _tu_coordinator
    from core.turn_undo import recorder as _tu_recorder
    from core.turn_undo.models import NodeKind, TurnUndoError

    coordinator = _tu_coordinator.get_coordinator()
    if coordinator.is_path_frozen_any(str(resolved)):
        return (
            f"🚨 目标路径处于撤销中断事务的冻结范围，已拒绝修改"
            f"（{file_path or resolved}）：\n请先完成撤销恢复（GUI 或 /undo recover）。"
        )
    new_bytes = new_content.encode(encoding)

    with coordinator.workspace_lock(_tu_coordinator.thread_workspace_root()):
        handle = None
        try:
            handle = _tu_recorder.prepare_mutation(
                display_path=str(resolved),
                after_kind=NodeKind.REGULAR_FILE,
                after_content=new_bytes,
                tool_name=tool_name,
                tool_call_id="",
            )
        except TurnUndoError as error:
            if error.code == "no_active_checkpoint":
                write_err = _atomic_write_edit_file(
                    resolved, encoding, new_content,
                    file_path=file_path, expected_mtime_ns=None,
                )
                if not write_err:
                    _on_file_mutated(resolved)
                return write_err
            if error.code == "workspace_busy":
                return (
                    f"🚨 该文件正在被另一个回合写入，本次未修改文件"
                    f"（{file_path or resolved}）：\n{error.message}\n请稍后重试。"
                )
            return (
                f"🚨 该回合的撤销检查点已封闭，迟到的修改已被拒绝"
                f"（{file_path or resolved}）：\n{error.message}"
            )
        try:
            write_err = _atomic_write_edit_file(
                resolved, encoding, new_content,
                file_path=file_path, expected_mtime_ns=None,
            )
            if write_err:
                _tu_recorder.rollback_prepared_mutation(handle)
                return write_err
            _tu_recorder.verify_after_mutation(handle)
        except TurnUndoError as error:
            return (
                f"🚨 本次修改未能通过可撤销性验证，请重读文件后重试"
                f"（{file_path or resolved}）：\n{error.message}"
            )
    _on_file_mutated(resolved)
    return None


def _smart_replace_without_target(file_path, content, replacement, replace_all):
    import difflib

    replacement_lines = [line.rstrip("\r\n") for line in replacement.splitlines()]
    content_lines = content.splitlines(keepends=True)
    content_lines_stripped = [line.rstrip("\r\n") for line in content_lines]

    if not replacement_lines or not content_lines_stripped:
        return (
            f"🚨 智能替换失败：replacement_content 或文件内容为空（{file_path}）。"
        )

    if len(content_lines_stripped) < len(replacement_lines):
        return (
            f"🚨 智能替换失败：文件 {file_path} 共 {len(content_lines_stripped)} 行，"
            f"少于 replacement_content 的 {len(replacement_lines)} 行。"
            f"若要整体重写，请显式使用 write_file。"
        )

    repl_len = len(replacement_lines)
    candidates = []
    min_len = max(1, repl_len // 2)
    max_len = min(len(content_lines_stripped), repl_len * 2)

    for window_len in range(min_len, max_len + 1):
        len_diff = abs(window_len - repl_len)
        len_weight = 1.0 - (len_diff / max(repl_len, window_len))
        for i in range(len(content_lines_stripped) - window_len + 1):
            block = content_lines_stripped[i:i + window_len]
            line_ratios = []
            for r_line, b_line in zip(replacement_lines, block):
                lr = difflib.SequenceMatcher(
                    None, r_line, b_line, autojunk=False
                ).ratio()
                line_ratios.append(lr)
            while len(line_ratios) < max(repl_len, window_len):
                line_ratios.append(0.0)
            ratio = sum(line_ratios) / len(line_ratios)
            if ratio >= 0.3:
                combined = ratio * len_weight
                candidates.append((i, combined, ratio, block))

    if not candidates:
        return (
            f"🚨 智能替换失败：在 {file_path} 中未找到与 replacement_content "
            f"足够相似的代码段。"
            f"请显式提供 target_content（字符串匹配）或 start_line/end_line（行号路径）。"
        )

    candidates.sort(key=lambda x: x[1], reverse=True)

    # 只有综合评分 >= 0.7 的候选才允许替换：低评分候选（如 0.3 相似度）
    # 是瞎猜位置，必须让 LLM 显式提供 target 精确定位
    high_score_candidates = [c for c in candidates if c[1] >= 0.7]
    if not high_score_candidates:
        best_combined = candidates[0][1]
        return (
            f"🚨 智能替换失败：在 {file_path} 中找到的最佳候选综合评分仅 "
            f"{best_combined:.2f}（要求 >= 0.7），相似度不足以安全定位。"
            f"请显式提供 target_content（字符串匹配）或 start_line/end_line（行号路径）。"
        )

    if len(high_score_candidates) > 1 and not replace_all:
        locations = []
        for c in high_score_candidates[:3]:
            start_idx, combined_score, ratio, block = c
            end_line = start_idx + len(block)
            locations.append(
                f"第 {start_idx + 1}-{end_line} 行（综合评分 {combined_score:.2f}，相似度 {ratio:.2f}）"
            )
        return (
            f"🚨 智能替换失败：在 {file_path} 中找到 {len(high_score_candidates)} 个 "
            f"高相似度代码段，无法确定唯一替换位置：\n"
            + "\n".join(locations)
            + f"\n请显式提供 target_content 或 start_line/end_line 精确定位。"
        )

    if replace_all:
        chosen = []
        last_end = -1
        for cand in sorted(high_score_candidates, key=lambda c: c[0]):
            start_idx = cand[0]
            block_len = len(cand[3])
            if start_idx < last_end:
                continue
            chosen.append(cand)
            last_end = start_idx + block_len
    else:
        chosen = [high_score_candidates[0]]

    if "\r\n" in content:
        line_sep = "\r\n"
    else:
        line_sep = "\n"

    new_lines = []
    cursor = 0
    for cand in chosen:
        start_idx, _, _, block = cand
        new_lines.extend(content_lines_stripped[cursor:start_idx])
        new_lines.extend(replacement_lines)
        cursor = start_idx + len(block)
    new_lines.extend(content_lines_stripped[cursor:])

    new_content = line_sep.join(new_lines)
    if content.endswith("\n") or content.endswith("\r\n"):
        new_content += line_sep

    first_block = chosen[0][3]
    replaced_preview_lines = first_block[:5]
    if len(first_block) > 5:
        replaced_preview_lines.append(f"...（共 {len(first_block)} 行）")
    replaced_preview = "\n".join(replaced_preview_lines)

    return (new_content, len(chosen), replaced_preview)


def edit_file(
    file_path,
    target_content="",
    replacement_content="",
    replace_all=False,
    start_line=None,
    end_line=None,
):
    try:
        resolved = _resolve_workspace_path(file_path)
    except ValueError as e:
        return f"🚨 精准修改失败！{e}"
    with _edit_file_lock(resolved):
        from core.fresh_read_guard import (
            StaleReadError,
            is_guard_enabled,
            require_fresh_read,
        )
        try:
            require_fresh_read(
                _tool_cache.get_current_session() or "",
                str(resolved),
                tool_name="edit_file",
                skip=not is_guard_enabled(),
            )
        except StaleReadError as e:
            return f"🚨 {e}"

        expected_mtime_ns = _stat_mtime_ns(resolved)
        try:
            raw = resolved.read_bytes()
        except FileNotFoundError:
            return (
                f"🚨 精准修改失败！文件不存在：{file_path}。"
                "edit_file 只能修改已存在的文件；新建文件请改用 write_file。"
            )
        except OSError as e:
            return f"🚨 精准修改失败！无法读取文件 {file_path}: {e}"
        try:
            # 优先 UTF-8，失败时回退 GBK，写回时保持原编码
            content = raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            try:
                content = raw.decode("gbk")
                encoding = "gbk"
            except UnicodeDecodeError:
                return f"🚨 精准修改失败！无法解码文件 {file_path}（既不是 UTF-8 也不是 GBK 编码）。"

        if start_line is not None or end_line is not None:
            return _edit_file_by_lines(
                file_path, resolved, content, encoding,
                start_line, end_line, replacement_content,
                expected_mtime_ns=expected_mtime_ns,
            )

        target = str(target_content or "")
        replacement = str(replacement_content or "")
        if not target:
            # 智能降级：LLM 只传了 replacement_content（未提供 target_content 也未提供行号）。
            # 与其报错让 LLM 重试 3 次最后 fallback 到 write_file，不如直接用 difflib
            # 在文件中找出与 replacement 最相似的代码块进行替换。
            # 这种"宽容调用"显著降低 LLM 工具使用门槛，提升用户体验。
            if not replacement:
                return (
                    f"🚨 edit_file 调用参数缺失：target_content 和 replacement_content 均为空，"
                    f"且未提供 start_line/end_line（{file_path}）。"
                )
            result = _smart_replace_without_target(
                file_path, content, replacement, replace_all
            )
            if isinstance(result, str) and result.startswith("🚨"):
                return result
            new_content, occurrences, replaced_preview = result
            if new_content == content:
                return (
                    f"🚨 edit_file 智能替换失败：在 {file_path} 中未找到与 replacement_content "
                    f"足够相似的代码段（建议显式提供 target_content 或 start_line/end_line）。"
                )
            # 按回合撤销记录写入：prepare → 原子写 → verify
            write_err = _recorded_atomic_write(
                resolved, encoding, new_content,
                file_path=file_path, expected_mtime_ns=expected_mtime_ns,
            )
            if write_err:
                return write_err
            _publish_edit_diff(content, new_content)
            diag = _check_post_edit_diagnostics(resolved, new_content)
            return (
                f"✔ 智能修改 {file_path}（自动定位最相似代码段，共 {occurrences} 处替换）\n"
                f"被替换的原文（前 5 行）：\n{replaced_preview}{diag}"
            )

        # 换行风格自适应：把 target/replacement 统一成文件实际使用的换行风格，
        # 写回时仍保持文件原编码/换行。先归一化再转换，避免已有 \r\n 被腐蚀成 \r\r\n。
        match_target, match_replacement = target, replacement
        if "\r\n" in content:
            if "\r\n" not in target:
                match_target = target.replace("\r\n", "\n").replace("\n", "\r\n")
                match_replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
        elif "\r\n" in target:
            match_target = target.replace("\r\n", "\n")
            match_replacement = replacement.replace("\r\n", "\n")

        new_content, occurrences = _fuzzy_replace(
            content, match_target, match_replacement, replace_all=replace_all
        )

        if occurrences == 0:
            closest_hint = _closest_match_hint(content, match_target)
            return (
                f"🚨 精准修改失败！在文件 {file_path} 中没有找到与 target_content 足够相似的代码段。\n"
                f"建议：1) 使用 read_file(compress=False) 重新读取原始文本；"
                f"2) 把 target_content 缩短到 1500 字符以内并包含唯一上下文；"
                f"3) 若确实需要整体重写，再使用 write_file（write_file 会覆盖整个文件，diff 会显示全红）。"
                f"{closest_hint}"
            )
        if occurrences > 1 and not replace_all:
            return (
                f"🚨 精准修改失败！旧代码段在文件 {file_path} 中出现了 {occurrences} 次，无法确定唯一修改位置。\n"
                f"请提供更长的、包含更多上下文的 target_content 以保证唯一匹配；如果确实需要全部替换，请显式设置 replace_all=True。"
            )

        if new_content == content:
            return f"🚨 精准修改失败！定位成功但替换后的内容与原文相同，未发生实际修改（{file_path}）。"

        # 按回合撤销记录写入：prepare → 原子写 → verify
        write_err = _recorded_atomic_write(
            resolved, encoding, new_content,
            file_path=file_path, expected_mtime_ns=expected_mtime_ns,
        )
        if write_err:
            return write_err
        _publish_edit_diff(content, new_content)
        diag = _check_post_edit_diagnostics(resolved, new_content)
        return f"✔ 已修改文件：{file_path}{diag}"


def _apply_edit_operation_in_memory(file_path, content, edit_item, step_label):
    if not isinstance(edit_item, dict):
        return None, f"🚨 {step_label} 参数格式错误：每条编辑项必须是 JSON 对象。"

    start_line = edit_item.get("start_line")
    end_line = edit_item.get("end_line")
    replacement_content = edit_item.get("replacement_content")
    if replacement_content is None:
        return None, f"🚨 {step_label} 缺少 replacement_content 参数。"
    replacement = str(replacement_content)

    if start_line is not None or end_line is not None:
        if start_line is None or end_line is None:
            return None, f"🚨 {step_label} 缺少成对的 start_line / end_line。"
        try:
            start_num = int(start_line)
            end_num = int(end_line)
        except (TypeError, ValueError):
            return None, f"🚨 {step_label} start_line 和 end_line 必须是整数。"
        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        if start_num < 1 or start_num > total_lines:
            return None, f"🚨 {step_label} start_line={start_num} 超出范围（1-{total_lines}）。"
        if end_num < start_num:
            return None, f"🚨 {step_label} end_line={end_num} 小于 start_line={start_num}。"
        actual_end = min(end_num, total_lines)
        newline = "\r\n" if "\r\n" in content else "\n"
        if "\r\n" in content and "\r\n" not in replacement:
            replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
        elif "\r\n" not in content and "\r\n" in replacement:
            replacement = replacement.replace("\r\n", "\n")
        replaced_block = "".join(lines[start_num - 1 : actual_end])
        if replaced_block.endswith(("\r\n", "\n")) and not replacement.endswith(("\r\n", "\n")):
            replacement += newline
        new_content = "".join(lines[: start_num - 1]) + replacement + "".join(lines[actual_end:])
        return new_content, None

    target = str(edit_item.get("target_content") or "")
    if not target:
        return None, f"🚨 {step_label} 字符串模式必须提供非空的 target_content。"

    replace_all = bool(edit_item.get("replace_all", False))
    match_target, match_replacement = target, replacement
    if "\r\n" in content:
        if "\r\n" not in target:
            match_target = target.replace("\r\n", "\n").replace("\n", "\r\n")
            match_replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
    elif "\r\n" in target:
        match_target = target.replace("\r\n", "\n")
        match_replacement = replacement.replace("\r\n", "\n")

    new_content, occurrences = _fuzzy_replace(
        content, match_target, match_replacement, replace_all=replace_all
    )
    if occurrences == 0:
        hint = _closest_match_hint(content, match_target)
        return (
            None,
            f"🚨 {step_label} 在 {file_path} 中未找到匹配的代码段。{hint}",
        )
    if occurrences > 1 and not replace_all:
        return (
            None,
            f"🚨 {step_label} 目标片段在 {file_path} 中出现 {occurrences} 次，请提供更长上下文或设 replace_all=True。",
        )
    return new_content, None


def multi_edit(file_path, edits):
    """对同一文件执行原子性多处批量局部编辑（单次加锁、单次写盘与单个撤销记录）。

    任意一项失败立即整批中止，磁盘文件保持原样。
    """
    if not isinstance(edits, list) or not edits:
        return "🚨 multi_edit 失败：edits 必须为非空编辑列表。"

    try:
        resolved = _resolve_workspace_path(file_path)
    except ValueError as e:
        return f"🚨 批量修改失败！{e}"

    with _edit_file_lock(resolved):
        from core.fresh_read_guard import (
            StaleReadError,
            is_guard_enabled,
            require_fresh_read,
        )

        try:
            require_fresh_read(
                _tool_cache.get_current_session() or "",
                str(resolved),
                tool_name="multi_edit",
                skip=not is_guard_enabled(),
            )
        except StaleReadError as e:
            return f"🚨 {e}"

        expected_mtime_ns = _stat_mtime_ns(resolved)
        try:
            raw = resolved.read_bytes()
        except FileNotFoundError:
            return (
                f"🚨 批量修改失败！文件不存在：{file_path}。"
                "multi_edit 只能修改已存在的文件；新建文件请改用 write_file。"
            )
        except OSError as e:
            return f"🚨 批量修改失败！无法读取文件 {file_path}: {e}"

        if raw.startswith(b"\xef\xbb\xbf"):
            original_content = raw.decode("utf-8-sig")
            encoding = "utf-8-sig"
        else:
            try:
                original_content = raw.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                try:
                    original_content = raw.decode("gbk")
                    encoding = "gbk"
                except UnicodeDecodeError:
                    return f"🚨 批量修改失败！无法解码文件 {file_path}（既不是 UTF-8 也不是 GBK 编码）。"

        working = original_content
        total_edits = len(edits)
        for idx, edit_item in enumerate(edits, start=1):
            updated, err = _apply_edit_operation_in_memory(
                file_path, working, edit_item, f"批量修改第 {idx}/{total_edits} 项"
            )
            if err:
                return f"{err}\n（整批原子中止，文件未被修改）"
            working = updated

        if working == original_content:
            return f"🚨 批量修改失败！全部 {total_edits} 项编辑后文件内容未发生变化（{file_path}）。"

        write_err = _recorded_atomic_write(
            resolved,
            encoding,
            working,
            file_path=file_path,
            expected_mtime_ns=expected_mtime_ns,
            tool_name="multi_edit",
        )
        if write_err:
            return write_err
        _publish_edit_diff(original_content, working)
        diag = _check_post_edit_diagnostics(resolved, working)
        return f"✔ 已批量修改文件 {file_path}（共应用 {total_edits} 处变更）{diag}"

_SUBAGENT_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "timed_out",
    "cancelled",
}

_SUBAGENT_TASK_ID_RE = re.compile(r"^subagent-[0-9a-f]{8}$")


def is_subagent_task_id(value: str) -> bool:
    """仅接受生产生成的子 Agent 执行租约 ID，避免会话/turn 前缀误判。"""
    return _SUBAGENT_TASK_ID_RE.fullmatch(str(value or "")) is not None


def _new_subagent_task_id() -> str:
    task_id = f"subagent-{uuid.uuid4().hex[:8]}"
    if not is_subagent_task_id(task_id):
        raise RuntimeError("generated invalid subagent task id")
    return task_id

_MAX_CONCURRENT_SUBAGENTS = 5

# 并发闸门排队等待上限按任务自身超时预算计：排队时间不计入执行超时，
# 但若排队超过自身预算仍未拿到名额（例如名额被卡死任务占用），直接判超时，
# 避免前台 run_subagent 无限阻塞（曾因此整回合静默几十分钟）。

# 审批等待独立上限（秒）：与 gui_bridge.PERMISSION_TIMEOUT_SECONDS（300）对齐。
# 审批等待不计入执行超时，但权限 broker 卡死时任务不能无限悬挂占用并发闸门，
# 超过该上限即终止任务并取消其名下权限请求。
_SUBAGENT_APPROVAL_WAIT_LIMIT = 300

SUBAGENT_RESULT_MAX_CHARS = 16000

SUBAGENT_PARTIAL_DELIVERY_MAX_CHARS = 12000

_SUBAGENT_BUDGET_NOTICE_RATIOS = ((0.10, 45, 2), (0.25, 90, 1))


def build_subagent_budget_notice(remaining, budget, stage):
    try:
        remaining = float(remaining)
        budget = float(budget)
        stage = int(stage)
    except (TypeError, ValueError):
        return stage, None
    if budget <= 0 or remaining <= 0:
        return stage, None
    for ratio, floor, level in _SUBAGENT_BUDGET_NOTICE_RATIOS:
        if stage >= level:
            continue
        if remaining > max(float(floor), budget * ratio):
            continue
        budget_text = f"{int(budget)}s"
        remaining_text = f"{int(max(0.0, remaining))}s"
        if level >= 2:
            return level, (
                f"⏳ 时间预算即将耗尽：本次子任务总预算 {budget_text}，"
                f"剩余约 {remaining_text}。立即停止一切探索、读取和新增工具调用，"
                "把你已经完成的工作整理成最终结论输出（已改动的文件、已验证的结果、"
                "未完成的部分都要写清）。不要再调用任何工具。"
            )
        return level, (
            f"⏳ 时间预算提醒：本次子任务总预算 {budget_text}，"
            f"剩余约 {remaining_text}。请立即收敛范围：优先完成手头最关键的一步，"
            "随后直接输出当前结论，不要再开启新的探索或大范围改动。"
        )
    return stage, None


def _has_meaningful_subagent_result(text: str) -> bool:
    return bool(re.sub(r"[\s`*_#>~\-\[\](){}|.,!?，。！？:：;；/\\]+", "", text or ""))


_SUBAGENT_WARNING_MAX_ITEMS = 5
_SUBAGENT_WARNING_ITEM_MAX_CHARS = 200
SUBAGENT_WARNING_MARKER = "\n\n⚠️ 子代理执行期有工具报错"


def _with_tool_error_footer(result: str, tool_errors: dict, turn_error: str = "") -> str:
    """把工具报错降级为结果尾部的告警清单，不再一票否决整任务。"""
    if not tool_errors and not turn_error:
        return result
    items = list(tool_errors.items())
    shown = items[:_SUBAGENT_WARNING_MAX_ITEMS]
    lines = [
        SUBAGENT_WARNING_MARKER
        + f"（{len(items)} 个，可能影响结论可靠性，供主 Agent 判断是否复核）："
    ]
    if turn_error:
        lines.append(f"  - 回合级中止原因: {turn_error}")
    for name, detail in shown:
        text = str(detail)
        if len(text) > _SUBAGENT_WARNING_ITEM_MAX_CHARS:
            text = text[:_SUBAGENT_WARNING_ITEM_MAX_CHARS] + "..."
        lines.append(f"  - {name}: {text}")
    if len(items) > len(shown):
        lines.append(f"  ... 另有 {len(items) - len(shown)} 个未展开")
    return result + "\n".join(lines)


def _truncate_subagent_result(result: str, max_len: int, trailer: str) -> str:
    if len(result) <= max_len:
        return result
    tail = ""
    marker_at = result.rfind(SUBAGENT_WARNING_MARKER)
    if marker_at != -1:
        candidate = result[marker_at:]
        budget = max_len - len(candidate)
        if budget >= max_len // 2 and budget <= marker_at:
            tail = candidate
    keep = max_len - len(tail)
    head = result[:keep]
    omitted = len(result) - len(head) - len(tail)
    return head + trailer.replace("{omitted}", str(omitted)) + tail


_ADVISOR_TAG_RE = re.compile(
    r"\[(?:Advisor consultation[^\]]*|Advisor review|End of advisor consultation[^\]]*)\]"
    r"|\(\s*advisor[^\)]*\)",
    re.IGNORECASE,
)
_XML_JUNK_BLOCK_RE = re.compile(
    r"<(advisor-request|function_calls|tool_call)>[\s\S]*?(?:</\1>|\Z)",
    re.IGNORECASE,
)


def _strip_advisor_tags(text: str) -> str:
    """剔除模型泄漏到正文里的 Advisor 内部协议标记与 XML 伪工具调用块，避免污染子代理结果与计划回写。"""
    cleaned = _ADVISOR_TAG_RE.sub("", text or "")
    cleaned = _XML_JUNK_BLOCK_RE.sub("", cleaned)
    return cleaned.strip()


_SUBAGENT_TIMEOUT_HINT = (
    "（子代理可能已产出部分结果，例如文件已写入；"
    "请先用 read_file/list_directory 确认工作成果，再由主 Agent 直接接手继续，"
    "避免重复执行已完成的工作；接管项验证失败时使用 plan_resume_takeover）"
)


@dataclass(frozen=True)
class SubagentStage:
    name: str
    at: float
    detail: str = ""


_MAX_SUBAGENT_TOOL_RECORDS = 200


@dataclass(frozen=True)
class SubagentTaskSnapshot:
    task_id: str
    parent_session: str
    parent_turn_id: str
    description: str
    subagent_type: str
    plan_item_id: str
    run_in_background: bool
    status: str
    started_at: float | None
    finished_at: float | None
    result: str
    error: str
    stage: str
    stages: tuple[SubagentStage, ...]
    execution_elapsed: float
    approval_wait_elapsed: float
    partial_result: str = ""
    tool_call_id: str = ""
    timeout_seconds: int = 600
    handoff: str = ""
    model_name: str = ""
    rounds: int = 0
    tool_success_count: int = 0
    tool_error_count: int = 0
    approval_summary: str = ""
    tool_records: tuple[dict[str, Any], ...] = ()
    changed_files: tuple[str, ...] = ()


@dataclass
class SubagentTask:
    task_id: str
    parent_session: str
    parent_turn_id: str
    description: str
    workdir: str = ""
    tool_call_id: str = ""
    subagent_type: str = "general"
    plan_item_id: str = ""
    run_in_background: bool = False
    status: str = "pending"
    started_at: float | None = None
    finished_at: float | None = None
    result: str = ""
    partial_result: str = ""
    error: str = ""
    tool_errors: dict[str, str] = field(default_factory=dict, repr=False)
    # 回合级中止原因（无进展熔断/轮次上限只走 turn_error 事件通道，
    # 不 yield 正文；不单独记录就会退化成"子代理无公开结论"）
    turn_error: str = ""
    tool_success_count: int = 0
    tool_error_count: int = 0
    rounds: int = 0
    model_name: str = ""
    approval_summary: str = ""
    tool_records: list[dict[str, Any]] = field(default_factory=list, repr=False)
    changed_files: list[str] = field(default_factory=list, repr=False)
    stage: str = "queued"
    stages: list[SubagentStage] = field(default_factory=list)
    execution_elapsed: float = 0.0
    approval_wait_elapsed: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    done_event: threading.Event = field(default_factory=threading.Event, repr=False)
    notification_consumed: bool = False
    summary_injected: bool = False
    terminal_callback: Callable | None = field(default=None, repr=False)
    callback_invoked: bool = field(default=False, repr=False)
    model_config: tuple | None = field(default=None, repr=False)
    event_callback: Callable | None = field(default=None, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    custom_config: dict | None = field(default=None, repr=False)
    timeout_seconds: int = field(default=600, repr=False)
    # 已发出的时间预算提醒级别（0 未提醒 / 1 已提醒收尾 / 2 已提醒终止）。
    # 由子代理线程在每步边界读取 execution_elapsed 后写回，避免同一级别反复注入。
    budget_notice_stage: int = field(default=0, repr=False)
    # 并发名额生命周期标记：超时/取消是协作式，线程可能仍卡在不可中断的
    # LLM 请求里，但名额要立即归还（见 _release_slot），两者都必须幂等
    slot_acquired: bool = field(default=False, repr=False)
    slot_released: bool = field(default=False, repr=False)
    # 超时移交结果（run_subagent 的 terminal_callback 写入）：
    # ""=未移交；"main_takeover"/"takeover_queued"=移交成功；
    # "failed"=移交失败（保留 timed_out 事实，不得改写为普通 failed 重新委派）
    handoff: str = field(default="", repr=False)
    # 父回合创建子代理时快照的权限档位（线程本地，子代理线程不继承）。
    # 子代理线程执行工具时用它包裹，鉴权路径据此读到会话档位而非全局默认，
    # 否则父会话 full + 全局 confirm/accept_edits 时子代理仍会反复弹审批。
    permission_mode: str | None = field(default=None, repr=False)


SubagentRunner = Callable[[SubagentTask], str]

_BACKGROUND_NOTIFICATION_TTL_SECONDS = 600


def _snapshot_model_config(
    prefer_secondary: bool = True,
    session_name: str | None = None,
) -> tuple | None:
    try:
        from core.model_manager import get_secondary_model, get_secondary_roles
        roles = get_secondary_roles()
        secondary = (
            get_secondary_model()
            if prefer_secondary and roles.get("subagent")
            else None
        )
        if secondary:
            return (
                secondary["litellm_model"],
                secondary["api_key"],
                secondary["base_url"],
                secondary.get("id"),
            )
    except Exception:
        pass
    return _active_model_tuple_or_none(session_name)


def _active_model_tuple_or_none(session_name: str | None = None):
    """按会话取主模型 tuple；无会话调用方保留 CLI 全局兼容语义。"""
    if session_name:
        try:
            from core.model_manager import get_active_model

            active = get_active_model(session_name)
            if active:
                return (
                    active.get("litellm_model"),
                    active.get("api_key"),
                    active.get("base_url"),
                    active.get("id"),
                )
        except Exception:
            return None
        # 显式会话路径到此即终止：无模型时不得读取进程级最后写入者。
        return None
    try:
        import core.app_context as app_context
        current = app_context.get_active_model_tuple()
        if current and current[0]:
            try:
                from core.model_manager import get_active_model
                active = get_active_model() or {}
                same_model = str(active.get("litellm_model") or "") == str(current[0] or "")
                same_base = (
                    str(active.get("base_url") or "").rstrip("/").lower()
                    == str(current[2] or "").rstrip("/").lower()
                )
                model_id = active.get("id") if same_model and same_base else None
            except Exception:
                model_id = None
            return (*tuple(current), model_id)
    except Exception:
        pass
    try:
        from core.model_manager import get_active_model
        active = get_active_model()
        if active:
            return (
                active.get("litellm_model"),
                active.get("api_key"),
                active.get("base_url"),
                active.get("id"),
            )
    except Exception:
        pass
    return None


def _unpack_model_config(model_config: tuple | None):
    """兼容旧三元组，并把新模型 ID 作为第四项贯穿到请求层。"""
    values = tuple(model_config or ())
    if not values:
        return (None, None, None, None)
    if len(values) == 3:
        return (*values, None)
    if len(values) == 4:
        return values
    raise ValueError("model_config 必须是 (model, key, base_url[, model_id])")


def _resolve_subagent_model_name(
    litellm_model: str | None,
    model_id: str | None = None,
) -> str:
    """提取子代理页面展示用的简短模型名称，失败时静默回退模型串尾部。"""
    if model_id:
        try:
            from core.model_manager import _find_config_model, load_config

            found = _find_config_model(load_config(), str(model_id))
            if isinstance(found, dict):
                name = str(found.get("name") or "").strip()
                if name:
                    return name
        except Exception:
            pass
    raw = str(litellm_model or "").strip()
    if "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    return raw


def _format_tool_stage_duration(duration_ms) -> str:
    """tool_finished 阶段详情里的耗时片段：≥1s 显示秒，否则毫秒。"""
    try:
        ms = max(0, int(duration_ms or 0))
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms"


def _permission_owner_waiting(owner_id: str) -> bool:
    from core.permission_broker import permission_broker

    return permission_broker.has_owner(owner_id)


class SubagentManager:
    def __init__(
        self,
        runner: SubagentRunner | None = None,
        *,
        max_records: int = 256,
        monotonic: Callable[[], float] = time.monotonic,
        wait_interval: float = 0.05,
        permission_waiting: Callable[[str], bool] | None = None,
        on_terminal: Callable[[str, str, str, str], None] | None = None,
    ) -> None:
        if max_records < 1:
            raise ValueError("max_records must be positive")
        if wait_interval <= 0:
            raise ValueError("wait_interval must be positive")
        self._runner = runner or self._run_default_subagent
        self._uses_default_runner = runner is None
        self._max_records = max_records
        self._monotonic = monotonic
        self._wait_interval = wait_interval
        self._permission_waiting = permission_waiting or _permission_owner_waiting
        self._tasks: dict[str, SubagentTask] = {}
        self._lock = threading.Lock()
        self._concurrency = threading.BoundedSemaphore(_MAX_CONCURRENT_SUBAGENTS)
        # 后台子代理进入终态时的实时通知回调。
        # 签名：on_terminal(task_id, status, description, error_or_result_summary)
        # 仅对 run_in_background=True 的任务触发（前台任务在状态栏已有实时显示）。
        # 回调内禁止直接操作 UI，必须通过 app.loop.call_soon_threadsafe 回到 UI 线程。
        self._on_terminal = on_terminal

    def set_on_terminal(self, callback: Callable[[str, str, str, str], None] | None) -> None:
        """运行时注册/替换 on_terminal 回调（main.py 初始化 UI 后调用）。"""
        self._on_terminal = callback

    def _fire_terminal_notification(self, task: "SubagentTask") -> None:
        """后台任务终态时触发实时通知回调（在 worker 线程中调用）。

        回调内必须自己做线程切换（call_soon_threadsafe），不能直接操作 UI。
        只通知后台任务；前台任务的状态栏已有实时显示，重复通知会刷屏。
        """
        if not task.run_in_background:
            return
        cb = self._on_terminal
        if cb is None:
            return
        try:
            if task.status == "completed":
                summary = (task.result or "")[:200]
            else:
                summary = task.error or ""
            cb(task.task_id, task.status, task.description, summary)
        except BaseException:
            # 通知失败不能影响子代理主流程
            pass


    def run(
        self,
        task_description: str,
        parent_session: str,
        parent_turn_id: str,
        *,
        run_in_background: bool = False,
        timeout_seconds: int = 600,
        parent_cancel_event: threading.Event | None = None,
        subagent_type: str = "general",
        plan_item_id: str = "",
        terminal_callback: Callable | None = None,
        model_config: tuple | None = None,
        event_callback: Callable | None = None,
        task_id: str | None = None,
        tool_call_id: str = "",
    ) -> str:
        if not 30 <= timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be between 30 and 3600")
        description = str(task_description or "").strip()
        if not description:
            raise ValueError("task_description must not be empty")
        normalized_type = str(subagent_type or "general")
        if model_config is None and self._uses_default_runner:
            prefer_secondary = normalized_type in {"explore", "test"}
            model_config = _snapshot_model_config(
                prefer_secondary=prefer_secondary,
                session_name=parent_session,
            )

        # 加载自定义子 agent 配置：subagent_type 非内置 5 种时尝试从注册中心获取
        # 容错：加载失败回退到 None（_run_default_subagent 会按内置 general 处理）
        custom_config = None
        if normalized_type and normalized_type not in {
            "explore", "implement", "test", "review", "general"
        }:
            try:
                from core.subagent_registry import get_custom_subagent
                custom_config = get_custom_subagent(normalized_type)
            except Exception:
                custom_config = None

        from core.workspace_context import get_workdir
        # 权限档位随任务快照：子代理线程不继承调用线程的线程本地档位，
        # 必须在创建时（仍处于父回合线程）取值，_run_task 内再用 scope 恢复。
        from core import app_context as _app_context
        _parent_permission_mode = _app_context.get_permission_mode()
        _init_model, _, _, _init_model_id = _unpack_model_config(model_config)
        task = SubagentTask(
            task_id=str(task_id or _new_subagent_task_id()),
            parent_session=str(parent_session or ""),
            parent_turn_id=str(parent_turn_id or ""),
            description=description,
            workdir=str(get_workdir()),
            tool_call_id=str(tool_call_id or ""),
            subagent_type=normalized_type,
            plan_item_id=str(plan_item_id or ""),
            run_in_background=bool(run_in_background),
            timeout_seconds=int(timeout_seconds),
            terminal_callback=terminal_callback,
            model_config=model_config,
            model_name=_resolve_subagent_model_name(_init_model, _init_model_id),
            custom_config=custom_config,
            event_callback=event_callback,
            permission_mode=_parent_permission_mode,
        )
        with self._lock:
            if task.task_id in self._tasks:
                # 执行租约 id 冲突属于调用方 invariant 破坏：显式失败，
                # 绝不允许静默改写 id 造成计划项租约与任务记录脱钩
                raise ValueError(f"duplicate subagent task id: {task.task_id}")
            self._make_capacity_locked()
            self._tasks[task.task_id] = task
            self._record_stage_locked(task, "queued")

        thread = threading.Thread(
            target=self._run_task,
            args=(task,),
            daemon=True,
            name=task.task_id,
        )
        task.thread = thread
        thread.start()

        if run_in_background:
            # 后台任务同样挂超时监管（协作式取消），避免无限期运行和线程泄露
            watchdog = threading.Thread(
                target=self._watch_background,
                args=(task, timeout_seconds, parent_cancel_event),
                daemon=True,
                name=f"{task.task_id}-watchdog",
            )
            watchdog.start()
            return json.dumps({
                "task_id": task.task_id,
                "status": "running",
                "run_in_background": True,
            }, ensure_ascii=False)

        last_tick = self._monotonic()
        approval_wait = 0.0
        while not task.done_event.wait(self._wait_interval):
            if parent_cancel_event is not None and parent_cancel_event.is_set():
                self._terminate(task.task_id, "cancelled", "parent turn cancelled")
                break

            now = self._monotonic()
            elapsed = max(0.0, now - last_tick)
            last_tick = now
            waiting_for_approval = self._permission_waiting(task.task_id)
            with self._lock:
                if waiting_for_approval:
                    task.approval_wait_elapsed += elapsed
                    approval_wait += elapsed
                    self._record_stage_locked(task, "waiting_approval")
                elif task.callback_invoked or task.stage == "queued":
                    # 终态回调执行中 / 并发闸门排队中：暂停计时避免误判超时
                    task.approval_wait_elapsed += elapsed
                    approval_wait = 0.0
                else:
                    approval_wait = 0.0
                    task.execution_elapsed += elapsed
                    if task.stage in {"waiting_approval", "finalizing"}:
                        self._record_stage_locked(
                            task,
                            "running",
                            "approval resolved",
                        )
                execution_elapsed = task.execution_elapsed
            if waiting_for_approval and approval_wait >= _SUBAGENT_APPROVAL_WAIT_LIMIT:
                # 权限 broker 卡死（无人响应审批）：终止任务，避免无限悬挂占并发闸门；
                # _terminate(timed_out) 会一并 cancel_owner 取消名下权限请求
                self._terminate(
                    task.task_id,
                    "timed_out",
                    f"审批等待超过 {_SUBAGENT_APPROVAL_WAIT_LIMIT}s 未决"
                    "（权限通道可能卡死），任务已终止" + _SUBAGENT_TIMEOUT_HINT,
                )
                break
            if execution_elapsed >= timeout_seconds:
                self._terminate(
                    task.task_id,
                    "timed_out",
                    "foreground timeout" + _SUBAGENT_TIMEOUT_HINT,
                )
                break

        if task.status in {"timed_out", "cancelled"} and task.thread is not None:
            task.thread.join(timeout=2.0)
        return self._foreground_result(task.task_id)

    def _watch_background(
        self,
        task: SubagentTask,
        timeout_seconds: int,
        parent_cancel_event: threading.Event | None,
    ) -> None:
        last_tick = self._monotonic()
        approval_wait = 0.0
        while not task.done_event.wait(self._wait_interval):
            if parent_cancel_event is not None and parent_cancel_event.is_set():
                self._terminate(task.task_id, "cancelled", "parent turn cancelled")
                return
            now = self._monotonic()
            elapsed = max(0.0, now - last_tick)
            last_tick = now
            waiting_for_approval = self._permission_waiting(task.task_id)
            with self._lock:
                if task.status in _SUBAGENT_TERMINAL_STATUSES:
                    return
                if waiting_for_approval or task.callback_invoked or task.stage == "queued":
                    # 等待审批 / 终态回调执行 / 并发闸门排队期间暂停计时，避免误判超时
                    task.approval_wait_elapsed += elapsed
                    approval_wait = approval_wait + elapsed if waiting_for_approval else 0.0
                else:
                    approval_wait = 0.0
                    task.execution_elapsed += elapsed
                execution_elapsed = task.execution_elapsed
            if waiting_for_approval and approval_wait >= _SUBAGENT_APPROVAL_WAIT_LIMIT:
                # 权限 broker 卡死（无人响应审批）：终止任务，避免无限悬挂占并发闸门
                self._terminate(
                    task.task_id,
                    "timed_out",
                    f"审批等待超过 {_SUBAGENT_APPROVAL_WAIT_LIMIT}s 未决"
                    "（权限通道可能卡死），任务已终止" + _SUBAGENT_TIMEOUT_HINT,
                )
                return
            if execution_elapsed >= timeout_seconds:
                self._terminate(
                    task.task_id,
                    "timed_out",
                    "background timeout" + _SUBAGENT_TIMEOUT_HINT,
                )
                return

    def _release_slot(self, task: SubagentTask) -> None:
        """释放并发名额（幂等）。

        超时/取消是协作式：子代理线程可能仍卡在不可中断的 LLM 请求里，但名额
        必须立即归还，否则泄漏线程占满闸门（上限 5 个），后续子代理无限排队，
        整回合假死。
        """
        with self._lock:
            if task.slot_released or not task.slot_acquired:
                return
            task.slot_released = True
        self._concurrency.release()

    def _run_task(self, task: SubagentTask) -> None:
        queue_wait_started = None
        while not self._concurrency.acquire(timeout=0.2):
            if task.cancel_event.is_set() or task.status in _SUBAGENT_TERMINAL_STATUSES:
                return
            if queue_wait_started is None:
                queue_wait_started = self._monotonic()
            elif self._monotonic() - queue_wait_started >= max(60, task.timeout_seconds):
                self._terminate(
                    task.task_id,
                    "timed_out",
                    f"排队超过 {int(self._monotonic() - queue_wait_started)}s 仍未获得并发名额"
                    "（可能有卡死任务占用）" + _SUBAGENT_TIMEOUT_HINT,
                )
                return
        task.slot_acquired = True
        _own_undo_turn = None
        # 线程本地权限档位作用域：子代理线程不继承父回合的线程绑定，必须用
        # 创建时快照的档位重新绑定，否则鉴权路径回退到全局默认（父会话 full、
        # 全局 confirm/accept_edits 时子代理工具仍反复弹审批）。
        from contextlib import nullcontext as _nullcontext

        from core import app_context as _subagent_app_context

        _permission_scope = (
            _subagent_app_context.permission_mode_scope(task.permission_mode)
            if task.permission_mode
            else _nullcontext()
        )
        _permission_scope.__enter__()
        _prev_thread_session = _subagent_app_context.get_thread_session()
        from core import plan as _subagent_plan_mod

        _prev_plan_session, _prev_plan_storage_dir = _subagent_plan_mod.get_plan_route()
        try:
            with self._lock:
                if task.status != "pending":
                    return
                task.status = "running"
                task.started_at = time.time()
                self._record_stage_locked(task, "running")

            # 启动段（会话/工作目录/undo 检查点绑定）异常兜底：这里抛错会让
            # 线程带着 running 状态裸崩（永无终态回调与 done_event），必须
            # 落成终态让等待方可见（取消中 → cancelled，其余 → failed）
            try:
                _tool_cache.set_current_session(task.parent_session)
                from core.workspace_context import set_workdir
                if task.workdir:
                    set_workdir(task.workdir)
                # 计划/授权/沙盒放行等“每会话”状态同样路由到父会话：
                # 子代理执行计划项、触发权限询问时操作的是父会话的桶，
                # 不会串到其他并行会话
                _subagent_app_context.set_thread_session(task.parent_session)
                _subagent_plan_mod.set_current_plan_session(task.parent_session)
                from core.turn_undo import recorder as _tu_recorder
                from core.turn_undo import service as _tu_service

                _own_undo_turn = None
                if task.run_in_background:
                    _own_undo_turn = _tu_service.begin_background_task_turn(
                        parent_turn_id=task.parent_turn_id,
                        description=f"后台子代理 {task.task_id}: {task.description[:60]}",
                    )
                else:
                    _tu_recorder.bind_thread_for_turn(task.parent_turn_id, owner=False)
            except BaseException as error:
                _logger.warning("子代理启动段失败 %s: %s", task.task_id, error)
                _tool_cache.set_current_session(None)
                if task.cancel_event.is_set():
                    self._terminate(task.task_id, "cancelled", "task cancelled")
                else:
                    self._terminate(
                        task.task_id, "failed", f"子代理启动失败: {error}"
                    )
                return
            try:
                result = _strip_advisor_tags(str(self._runner(task) or "").strip())
            except BaseException as error:
                if task.cancel_event.is_set():
                    self._terminate(task.task_id, "cancelled", "task cancelled")
                else:
                    self._terminate(task.task_id, "failed", str(error))
                _tool_cache.set_current_session(None)
                if _own_undo_turn is not None:
                    try:
                        _tu_service.finish_background_task_turn(
                            _own_undo_turn[1], reason="后台任务异常终止"
                        )
                    except Exception:
                        pass
                return

            self._record_partial(task.task_id, result)

            try:
                if task.cancel_event.is_set():
                    self._terminate(task.task_id, "cancelled", "task cancelled")
                elif task.turn_error or not _has_meaningful_subagent_result(result):
                    reason = (
                        f"子代理回合被中止：{task.turn_error}"
                        if task.turn_error
                        else "subagent returned no public answer"
                    )
                    if not task.turn_error and task.tool_errors:
                        reason += "；工具错误: " + "; ".join(
                            f"{name}: {detail}"
                            for name, detail in task.tool_errors.items()
                        )
                    self._terminate(task.task_id, "failed", reason)
                elif task.tool_errors and task.tool_success_count == 0:
                    # 所有工具调用都失败：结论只是错误复述，不能当成功
                    details = "; ".join(
                        f"{name}: {detail}" for name, detail in task.tool_errors.items()
                    )
                    self._terminate(task.task_id, "failed", details)
                else:
                    final_result = _with_tool_error_footer(
                        result, task.tool_errors, task.turn_error
                    )
                    review_verdict = None
                    try:
                        from core.task_decision_hooks import decide_subagent_review

                        review_verdict = decide_subagent_review(
                            description=task.description,
                            subagent_type=task.subagent_type,
                            result_text=result,
                            tool_success_count=task.tool_success_count,
                            tool_errors=task.tool_errors,
                            plan_item_id=task.plan_item_id,
                        )
                    except Exception:
                        review_verdict = None
                    if review_verdict == "failed":
                        self._terminate(
                            task.task_id,
                            "failed",
                            "决策模型验收未通过：子代理结论显示未能完成核心目标",
                        )
                    else:
                        if review_verdict == "incomplete":
                            final_result += (
                                "\n\n[JEV 验收提醒] 决策模型判定该子代理结论可能不完整，"
                                "请核查关键交付物后决定是否继续补全或调用 plan_retry。"
                            )
                        self._complete(task.task_id, final_result)
            finally:
                _tool_cache.set_current_session(None)
        finally:
            if _own_undo_turn is not None:
                try:
                    from core.turn_undo import service as _tu_service

                    _tu_service.finish_background_task_turn(_own_undo_turn[1])
                except Exception:
                    pass
            # H2：子代理线程的检查点绑定必须解除，避免下一任务复用线程时
            # TLS 残留旧 ctx 触发"检查点已封闭"误拒或跨回合串记录
            try:
                from core.turn_undo import recorder as _tu_recorder

                _tu_recorder.unbind_thread()
            except Exception:
                pass
            # 线程本地权限档位与会话路由必须随任务结束解绑，避免复用线程（如测试主线程
            # 直调）时档位与会话残留到下一个任务
            try:
                _subagent_plan_mod.set_current_plan_session(
                    _prev_plan_session, storage_dir=_prev_plan_storage_dir
                )
            except Exception:
                pass
            try:
                _subagent_app_context.set_thread_session(_prev_thread_session)
            except Exception:
                pass
            try:
                _permission_scope.__exit__(None, None, None)
            except Exception:
                pass
            self._release_slot(task)

    def _complete(self, task_id: str, result: str) -> bool:
        with self._lock:
            task = self._tasks[task_id]
            if task.status in _SUBAGENT_TERMINAL_STATUSES or task.callback_invoked:
                return False
            # 锁内原子认领回调，保证 terminal_callback 全局只发一次
            task.callback_invoked = True
            callback = task.terminal_callback
            if callback is not None:
                self._record_stage_locked(task, "finalizing")
        if callback is not None:
            try:
                callback_error = callback(task, "completed", result, "")
            except BaseException as error:
                callback_error = f"计划回写失败: {error}"
            if callback_error:
                return self._terminate(
                    task_id,
                    "failed",
                    str(callback_error),
                    invoke_callback=False,
                )
        with self._lock:
            task = self._tasks[task_id]
            if task.status in _SUBAGENT_TERMINAL_STATUSES:
                return False
            task.status = "completed"
            task.result = result
            task.finished_at = time.time()
            task.approval_summary = ""
            self._record_stage_locked(task, "completed")
        self._fire_terminal_notification(task)
        # done_event 是整个终态提交（计划回写 + 通知）的唯一完成屏障。
        # 必须最后设置，否则 idle/capacity 观察者会看到半完成记录。
        with self._lock:
            task.done_event.set()
        return True

    def _terminate(
        self,
        task_id: str,
        status: str,
        error: str,
        *,
        invoke_callback: bool = True,
    ) -> bool:
        if status not in _SUBAGENT_TERMINAL_STATUSES - {"completed"}:
            raise ValueError(f"invalid terminal status: {status}")
        callback = None
        task = None
        signal_only = False
        with self._lock:
            task = self._tasks[task_id]
            if task.status in _SUBAGENT_TERMINAL_STATUSES:
                return False
            if invoke_callback and task.callback_invoked:
                # _complete 已原子认领 completed 回调时，外部取消/看门狗只能
                # 发出协作式中断信号，不能篡改终态或再次调用 terminal_callback。
                task.cancel_event.set()
                signal_only = True
            if signal_only:
                callback = None
            else:
                # 锁内原子认领 callback；invoke_callback=False 用于已认领回调
                # 返回错误后的内部终态提交，同样阻止任何后续回调竞争者。
                task.callback_invoked = True
                callback = task.terminal_callback if invoke_callback else None
                task.status = status
                task.error = str(error)
                task.finished_at = time.time()
                task.approval_summary = ""
                for rec in task.tool_records:
                    if rec.get("status") == "running":
                        rec["status"] = "cancelled" if status == "cancelled" else "err"
                        rec["finished_at"] = task.finished_at
                        if not rec.get("error_summary"):
                            rec["error_summary"] = (
                                "已中止" if status == "cancelled"
                                else ("已超时" if status == "timed_out" else "任务已结束")
                            )
                self._record_stage_locked(task, status, str(error))
                task.cancel_event.set()
            # 锁内原子认领终态与回调：先写终态事实（status/error/finished_at）
            # 并 set cancel_event，再在锁外执行后续清理与计划回调。
            # 顺序铁律：cancel_event 必须先于 terminal callback——超时移交
            # （handoff_to_main）与权限 fencing 都依赖"回调执行时任务已取消"；
            # done_event 是前台结果可见屏障，绝不能早于 handoff callback。
        if status in {"timed_out", "cancelled"}:
            from core.permission_broker import permission_broker
            permission_broker.cancel_owner(task_id)
            from core.llm import abort_inflight_streams
            abort_inflight_streams(task.cancel_event)
            # 协作式取消杀不死阻塞中的线程（如 LLM 请求），但并发名额立即归还，
            # 避免泄漏线程占满闸门导致排队子代理与前台回合无限等待
            self._release_slot(task)
        if signal_only:
            return True
        if callback is not None:
            try:
                callback_error = callback(task, status, "", str(error))
                if callback_error:
                    error = f"{error}; {callback_error}"
                    # callback 失败时保留 timed_out/cancelled 终态事实，
                    # 只追加错误详情，绝不把超时改写成普通 failed 重新委派
                    with self._lock:
                        task.error = str(error)
            except BaseException as callback_exception:
                error = f"{error}; 计划回写失败: {callback_exception}"
                with self._lock:
                    task.error = str(error)
        self._fire_terminal_notification(task)
        with self._lock:
            task = self._tasks[task_id]
            task.done_event.set()
        return True


    def _foreground_result(self, task_id: str) -> str:
        with self._lock:
            task = self._tasks[task_id]
            if task.status == "completed":
                result = str(task.result or "")
                # 子代理结果过长时截断，避免完整大段内容进入主代理上下文浪费 token；
                # 尾部告警块（工具报错/回合级中止原因）由 helper 原样保留。
                return _truncate_subagent_result(
                    result,
                    SUBAGENT_RESULT_MAX_CHARS,
                    "\n\n... [子代理结果已截断，省略 {omitted} 字符]",
                )
            if task.status == "timed_out" and task.handoff:
                # 计划项子代理超时移交：返回结构化 JSON，让主 Agent 直接进入
                # 接管核查流程（检查现场 → 继续执行 → plan_finish），
                # 绝不允许把超时当成普通失败重新委派同一计划项。
                return json.dumps({
                    "status": "timed_out",
                    "handoff": task.handoff,
                    "plan_item_id": task.plan_item_id,
                    "task_id": task.task_id,
                    "inspection_required": True,
                    "stage": task.stage,
                    "execution_elapsed": round(task.execution_elapsed, 1),
                    "partial_result": str(task.partial_result or "")[
                        :SUBAGENT_PARTIAL_DELIVERY_MAX_CHARS
                    ],
                    "error": task.error,
                }, ensure_ascii=False)
            # 失败/超时/取消：返回结构化文本，让主 agent 能快速理解决策下一步
            # 不用 JSON 是因为终端环境可读性差，主 agent 直接读文本即可
            return self._format_failure_text(task)

    def _format_failure_text(self, task: SubagentTask) -> str:
        status_label = {
            "failed": "失败",
            "timed_out": "超时",
            "cancelled": "已取消",
        }.get(task.status, task.status)

        tool_errors = dict(task.tool_errors) if task.tool_errors else {}
        tool_lines = []
        if tool_errors:
            for name, detail in tool_errors.items():
                # 截断过长的单工具错误详情，避免一个工具的堆栈淹没其他信息
                detail_text = str(detail)
                if len(detail_text) > 200:
                    detail_text = detail_text[:200] + "..."
                tool_lines.append(f"  - {name}: {detail_text}")

        error_main = str(task.error or "").strip()
        if not error_main and not tool_lines:
            error_main = "无具体错误信息"

        hints = []
        if task.status == "timed_out":
            if str(task.partial_result or "").strip():
                hints.append(
                    "子代理超时，但已产出部分成果：先用 read_file/list_directory 核对"
                    "工作区实际改动，确认哪些已完成，再在已有成果基础上继续；"
                    "不要从头重做已完成的工作。剩余工作量仍大时拆成更小的子任务。"
                )
            else:
                hints.append(
                    "子代理超时且没有可交付正文：主 Agent 应直接用自身工具完成该任务，"
                    "或拆分为更小的子任务后重新派发"
                )
        elif task.status == "cancelled":
            hints.append("子代理被取消（可能是父轮次结束），如仍需执行请重新发起")
        elif tool_lines:
            hints.append("子代理工具调用失败，主 Agent 应分析失败工具的原因并用自身工具兜底")
            failed_tool_names = set(tool_errors.keys())
            if "get_weather" in failed_tool_names:
                hints.append("  · get_weather 失败时可尝试 search_news（搜'城市 天气'）或 fetch_url 抓取天气网页")
            if "fetch_url" in failed_tool_names:
                hints.append("  · fetch_url 失败时检查 URL 是否可达，或改用 search_news 搜索相关信息")
            if "search_news" in failed_tool_names:
                hints.append("  · search_news 失败时可能网络受限，告知用户当前环境无法联网")
        else:
            hints.append("子代理执行失败，主 Agent 应直接用自身工具完成任务，不要反复重试同一子代理")

        parts = [
            f"子代理执行{status_label}（task_id={task.task_id}）",
            f"角色: {task.subagent_type}",
            f"失败原因: {error_main}",
        ]
        if tool_lines:
            parts.append("失败工具清单:")
            parts.extend(tool_lines)
        partial = str(task.partial_result or "").strip()
        if partial:
            if len(partial) > SUBAGENT_PARTIAL_DELIVERY_MAX_CHARS:
                partial = (
                    partial[:SUBAGENT_PARTIAL_DELIVERY_MAX_CHARS]
                    + "\n... [部分产出已截断，完整内容以工作区实际改动为准]"
                )
            parts.append(
                "已完成的部分（超时/失败前已产出的正文，据此判断成果是否已落地；"
                "通常可直接采用或在其基础上继续，不要从头重做）:"
            )
            parts.append(partial)
        parts.append(
            f"进度: 阶段 {task.stage} / 工具成功 {task.tool_success_count} 次 / "
            f"执行 {round(task.execution_elapsed, 1)}s（预算 {task.timeout_seconds}s）"
            f" / 等待审批 {round(task.approval_wait_elapsed, 1)}s"
        )
        parts.append("下一步建议:")
        for h in hints:
            parts.append(f"  {h}")
        return "\n".join(parts)

    def snapshot(self) -> list[SubagentTaskSnapshot]:
        with self._lock:
            return [self._snapshot(task) for task in self._tasks.values()]

    def shutdown(self, timeout: float = 5.0) -> None:
        """Cooperatively cancel and bounded-wait all managed subagent threads."""
        self.set_on_terminal(None)
        with self._lock:
            task_ids = [
                task.task_id for task in self._tasks.values()
                if task.status not in _SUBAGENT_TERMINAL_STATUSES
            ]
        for task_id in task_ids:
            self.cancel(task_id, "application exiting")
        deadline = time.monotonic() + max(0.0, float(timeout))
        current = threading.current_thread()
        with self._lock:
            threads = [task.thread for task in self._tasks.values() if task.thread is not None]
        for thread in threads:
            if thread is current or not thread.is_alive():
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def drain_notifications(self, parent_session: str) -> list[str]:
        with self._lock:
            tasks = [
                task
                for task in self._tasks.values()
                if task.run_in_background
                and task.parent_session == parent_session
                and task.status in _SUBAGENT_TERMINAL_STATUSES
                and task.done_event.is_set()
                and not task.notification_consumed
            ]
            for task in tasks:
                task.notification_consumed = True
            notifications = [self._notification(task) for task in tasks]
            self._prune_completed_locked()
            return notifications

    def cancel_foreground(self, parent_session: str, parent_turn_id: str) -> int:
        with self._lock:
            task_ids = [
                task.task_id
                for task in self._tasks.values()
                if not task.run_in_background
                and task.parent_session == parent_session
                and task.parent_turn_id == parent_turn_id
                and task.status not in _SUBAGENT_TERMINAL_STATUSES
            ]
        return sum(
            self._terminate(task_id, "cancelled", "parent turn cancelled")
            for task_id in task_ids
        )

    def get_active_count(self) -> int:
        with self._lock:
            return sum(
                not task.done_event.is_set()
                for task in self._tasks.values()
            )

    def is_plan_item_running(self, plan_item_id: str) -> bool:
        if not plan_item_id:
            return False
        with self._lock:
            for task in self._tasks.values():
                if (
                    task.plan_item_id == plan_item_id
                    and not task.done_event.is_set()
                ):
                    return True
            return False

    def is_execution_lease_active(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(str(task_id or ""))
            return (
                task is not None
                and task.status not in _SUBAGENT_TERMINAL_STATUSES
                and not task.cancel_event.is_set()
            )

    def has_running_background_tasks(self, parent_session: str) -> bool:
        if not parent_session:
            return False
        with self._lock:
            for task in self._tasks.values():
                if (
                    task.run_in_background
                    and task.parent_session == parent_session
                    and not task.done_event.is_set()
                ):
                    return True
            return False

    def drain_terminal_background_tasks(self, parent_session: str) -> list[SubagentTaskSnapshot]:
        drained: list[SubagentTaskSnapshot] = []
        with self._lock:
            for task in self._tasks.values():
                if (
                    task.run_in_background
                    and task.parent_session == parent_session
                    and task.status in _SUBAGENT_TERMINAL_STATUSES
                    and task.done_event.is_set()
                    and not task.summary_injected
                ):
                    task.summary_injected = True
                    drained.append(self._snapshot(task))
        return drained

    def cancel(self, task_id: str, reason: str = "user requested") -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status in _SUBAGENT_TERMINAL_STATUSES:
                return False
        return self._terminate(task_id, "cancelled", reason)

    def task_snapshot(self, task_id: str) -> SubagentTaskSnapshot | None:
        with self._lock:
            task = self._tasks.get(str(task_id or ""))
            if task is None:
                return None
            return self._snapshot(task)

    def wait_terminal(
        self,
        task_id: str,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> SubagentTaskSnapshot | None:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        last_snapshot: SubagentTaskSnapshot | None = None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt("用户终止了工具执行。")
            with self._lock:
                task = self._tasks.get(str(task_id or ""))
                if task is None:
                    return last_snapshot
                last_snapshot = self._snapshot(task)
                if (
                    task.status in _SUBAGENT_TERMINAL_STATUSES
                    and task.done_event.is_set()
                ):
                    return last_snapshot
                done_event = task.done_event
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return last_snapshot
            done_event.wait(timeout=min(remaining, 0.5))

    def wait_for_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.get_active_count() == 0:
                return True
            time.sleep(min(self._wait_interval, 0.01))
        return self.get_active_count() == 0

    def _make_capacity_locked(self) -> None:
        self._prune_completed_locked(reserve=1)
        if len(self._tasks) >= self._max_records:
            raise RuntimeError("subagent task capacity reached")

    def _prune_completed_locked(self, reserve: int = 0) -> None:
        now = time.time()
        removable = [
            task_id
            for task_id, task in self._tasks.items()
            if task.status in _SUBAGENT_TERMINAL_STATUSES
            and task.done_event.is_set()
            and (
                not task.run_in_background
                or task.notification_consumed
                # 终态记录超过 TTL 仍未被父会话 drain 时允许清理，避免永久占位
                or (
                    task.finished_at is not None
                    and now - task.finished_at > _BACKGROUND_NOTIFICATION_TTL_SECONDS
                )
            )
        ]
        while len(self._tasks) + reserve > self._max_records and removable:
            self._tasks.pop(removable.pop(0), None)

    @staticmethod
    def _snapshot(task: SubagentTask) -> SubagentTaskSnapshot:
        return SubagentTaskSnapshot(
            task_id=task.task_id,
            parent_session=task.parent_session,
            parent_turn_id=task.parent_turn_id,
            description=task.description,
            subagent_type=task.subagent_type,
            plan_item_id=task.plan_item_id,
            run_in_background=task.run_in_background,
            status=task.status,
            started_at=task.started_at,
            finished_at=task.finished_at,
            result=task.result,
            error=task.error,
            stage=task.stage,
            stages=tuple(task.stages),
            execution_elapsed=task.execution_elapsed,
            approval_wait_elapsed=task.approval_wait_elapsed,
            partial_result=task.partial_result,
            tool_call_id=task.tool_call_id,
            timeout_seconds=task.timeout_seconds,
            handoff=task.handoff,
            model_name=task.model_name,
            rounds=task.rounds,
            tool_success_count=task.tool_success_count,
            tool_error_count=task.tool_error_count,
            approval_summary=task.approval_summary,
            tool_records=tuple(dict(rec) for rec in task.tool_records),
            changed_files=tuple(task.changed_files),
        )

    def _set_task_model_name(self, task_id: str, model_name: str) -> None:
        name = str(model_name or "").strip()
        if not name:
            return
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task.model_name = name

    def _set_task_rounds(self, task_id: str, rounds: int) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None and task.status not in _SUBAGENT_TERMINAL_STATUSES:
                task.rounds = max(task.rounds, int(rounds))

    def _set_approval_summary(self, task_id: str, summary: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None and task.status not in _SUBAGENT_TERMINAL_STATUSES:
                task.approval_summary = str(summary or "")

    def _record_changed_file(self, task_id: str, file_path: str) -> None:
        fp = str(file_path or "").strip()
        if not fp:
            return
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status in _SUBAGENT_TERMINAL_STATUSES:
                return
            if fp not in task.changed_files:
                task.changed_files.append(fp)

    def _record_tool_started(
        self,
        task_id: str,
        call_id: str,
        round_no: int,
        payload: dict[str, Any],
    ) -> None:
        from core.tool_presenter import (
            format_tool_arguments_preview,
            sanitize_tool_display_text,
        )

        name = str(payload.get("name") or "tool")
        display = payload.get("display") if isinstance(payload.get("display"), dict) else {}
        category = str(display.get("category") or "other")
        action = str(display.get("action") or name)
        target = str(
            display.get("command")
            or display.get("target")
            or payload.get("preview")
            or ""
        )
        target = sanitize_tool_display_text(target, 180)
        args_preview = format_tool_arguments_preview(
            name, payload.get("arguments"), limit=800
        )
        now = time.time()
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status in _SUBAGENT_TERMINAL_STATUSES:
                return
            seq = (task.tool_records[-1]["seq"] + 1) if task.tool_records else 1
            record = {
                "id": str(call_id or f"tool-{seq}"),
                "seq": seq,
                "round": max(1, int(round_no)),
                "name": name,
                "category": category,
                "action": action,
                "target": target,
                "args_preview": args_preview,
                "status": "running",
                "started_at": now,
                "finished_at": None,
                "duration_ms": None,
                "duration_text": "",
                "error_summary": "",
                "result_preview": "",
            }
            task.tool_records.append(record)
            if len(task.tool_records) > _MAX_SUBAGENT_TOOL_RECORDS:
                del task.tool_records[:-_MAX_SUBAGENT_TOOL_RECORDS]

    def _record_tool_finished(
        self,
        task_id: str,
        call_id: str,
        round_no: int,
        payload: dict[str, Any],
    ) -> None:
        from core.tool_presenter import (
            format_tool_arguments_preview,
            sanitize_tool_display_text,
            sanitize_tool_multiline_text,
        )

        name = str(payload.get("name") or "tool")
        ok = payload.get("ok") is not False
        display = payload.get("display") if isinstance(payload.get("display"), dict) else {}
        duration_ms = payload.get("duration_ms")
        try:
            dur_int = max(0, int(duration_ms)) if duration_ms is not None else 0
        except (TypeError, ValueError):
            dur_int = 0
        duration_text = _format_tool_stage_duration(dur_int) or ("0ms" if ok else "")
        error_summary = ""
        if not ok:
            raw_err = str(
                display.get("error_summary")
                or payload.get("summary")
                or payload.get("error")
                or "工具执行失败"
            ).lstrip("🚨").strip()
            error_summary = sanitize_tool_display_text(raw_err, 180)
        raw_result = str(
            payload.get("result") or payload.get("summary") or ""
        ).lstrip("🚨").strip()
        result_preview = sanitize_tool_multiline_text(raw_result, limit=1500)
        now = time.time()

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status in _SUBAGENT_TERMINAL_STATUSES:
                return
            if ok:
                task.tool_success_count += 1
            else:
                task.tool_error_count += 1

            target_rec = None
            cid = str(call_id or "")
            if cid:
                for rec in reversed(task.tool_records):
                    if rec.get("id") == cid:
                        target_rec = rec
                        break
            if target_rec is None:
                for rec in reversed(task.tool_records):
                    if rec.get("status") == "running" and rec.get("name") == name:
                        target_rec = rec
                        break
            if target_rec is None:
                category = str(display.get("category") or "other")
                action = str(display.get("action") or name)
                target = sanitize_tool_display_text(
                    display.get("command") or display.get("target") or payload.get("preview") or "",
                    180,
                )
                seq = (task.tool_records[-1]["seq"] + 1) if task.tool_records else 1
                target_rec = {
                    "id": cid or f"tool-{seq}",
                    "seq": seq,
                    "round": max(1, int(round_no)),
                    "name": name,
                    "category": category,
                    "action": action,
                    "target": target,
                    "args_preview": format_tool_arguments_preview(
                        name, payload.get("arguments"), limit=800
                    ),
                    "started_at": now,
                }
                task.tool_records.append(target_rec)
                if len(task.tool_records) > _MAX_SUBAGENT_TOOL_RECORDS:
                    del task.tool_records[:-_MAX_SUBAGENT_TOOL_RECORDS]

            if display:
                if display.get("category"):
                    target_rec["category"] = str(display["category"])
                if display.get("action"):
                    target_rec["action"] = str(display["action"])
                better_target = str(display.get("command") or display.get("target") or "")
                if better_target and not target_rec.get("target"):
                    target_rec["target"] = sanitize_tool_display_text(better_target, 180)

            target_rec["status"] = "ok" if ok else "err"
            target_rec["finished_at"] = now
            target_rec["duration_ms"] = dur_int
            target_rec["duration_text"] = duration_text
            target_rec["error_summary"] = error_summary
            target_rec["result_preview"] = result_preview

    def _record_partial(self, task_id: str, text: str) -> None:
        cleaned = str(text or "").strip()
        if not cleaned:
            return
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            if len(cleaned) > len(task.partial_result):
                task.partial_result = cleaned

    def _record_stage(self, task_id: str, name: str, detail: str = "") -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None and task.status not in _SUBAGENT_TERMINAL_STATUSES:
                self._record_stage_locked(task, name, detail)

    @staticmethod
    def _record_stage_locked(
        task: SubagentTask,
        name: str,
        detail: str = "",
    ) -> None:
        normalized_name = str(name)
        normalized_detail = str(detail)
        if (
            task.stages
            and task.stages[-1].name == normalized_name
            and task.stages[-1].detail == normalized_detail
        ):
            task.stage = normalized_name
            return
        task.stage = normalized_name
        task.stages.append(
            SubagentStage(
                name=normalized_name,
                at=time.time(),
                detail=normalized_detail,
            )
        )
        if len(task.stages) > 64:
            del task.stages[:-64]

    @staticmethod
    def _notification(task: SubagentTask) -> str:
        from core.injection_markers import neutralize_reserved_markers

        summary = neutralize_reserved_markers(
            f"子任务「{task.description[:40]}」{task.status}"
        )
        return f"""<task-notification>
  <task-id>{escape(task.task_id)}</task-id>
  <status>{escape(task.status)}</status>
  <summary>{escape(summary)}</summary>
  <result>{escape(neutralize_reserved_markers(task.result))}</result>
  <error>{escape(neutralize_reserved_markers(task.error))}</error>
</task-notification>"""

    def _run_default_subagent(self, task: SubagentTask) -> str:
        from core.llm import chat_with_model_events

        custom = task.custom_config
        def _fallback_model():
            if task.model_config is not None:
                return _unpack_model_config(task.model_config)
            return _unpack_model_config(_active_model_tuple_or_none())

        model_pref = custom.get("model") if custom else None
        if model_pref == "secondary":
            try:
                from core.model_manager import get_secondary_model
                secondary = get_secondary_model()
                if secondary:
                    current_model = secondary["litellm_model"]
                    current_key = secondary["api_key"]
                    current_url = secondary["base_url"]
                    current_model_id = secondary.get("id")
                else:
                    current_model, current_key, current_url, current_model_id = _fallback_model()
            except Exception:
                current_model, current_key, current_url, current_model_id = _fallback_model()
        elif model_pref == "inherit":
            inherited = _active_model_tuple_or_none(task.parent_session)
            if inherited:
                current_model, current_key, current_url, current_model_id = _unpack_model_config(inherited)
            else:
                current_model, current_key, current_url, current_model_id = _fallback_model()
        else:
            current_model, current_key, current_url, current_model_id = _fallback_model()

        role_instructions = {
            "explore": (
                "只读探索代码和资料，不修改文件，不执行有副作用命令。"
                "尽可能并行发起多个 search_code/glob_files/read_file 调用以最大化速度："
                "用 glob_files 按模式定位文件，用 search_code 搜索内容，用 read_file 读取已知路径。"
                "【工具失败降级】任一工具失败时优先尝试同类替代工具，不要直接返回失败："
                "  · get_weather 失败（403/超时/SSRF）→ search_news 搜'城市名 天气'或 fetch_url 抓天气网页"
                "  · fetch_url 失败 → 检查 URL 拼写，或改用 search_news 搜索相同关键词"
                "  · search_news 失败 → 可能网络受限，明确告知主 Agent 当前环境无法联网"
                "  · search_code 无结果 → 改用 glob_files 按文件名模式查找，或扩大搜索范围"
                "  · read_file 路径不存在 → 先用 list_directory 或 glob_files 定位正确路径"
                "【本地优先】获取系统时间/日期/主机信息等本地可完成的任务，优先用 execute_command "
                "执行本地命令（如 PowerShell 的 Get-Date），不要依赖联网工具。"
                "所有替代尝试都失败后，明确说明失败原因和已尝试的替代方案，由主 Agent 决策下一步。"
            ),
            "implement": (
                "负责按任务修改代码，修改前先读取相关文件，完成后说明改动和验证。"
                "【工具失败降级】命令执行失败时先分析错误输出，调整命令或参数后重试；"
                "edit_file 匹配失败时先用 read_file 重新读取目标文件确认当前内容，再重试。"
            ),
            "test": (
                "负责运行针对性测试和检查，优先定位失败根因，不修改业务代码。"
                "【工具失败降级】测试命令失败时先分析 stderr 定位失败用例，"
                "改用更细粒度的命令（如单测）重试，避免一次跑全量测试。"
            ),
            "review": (
                "只读审查实现和测试结果，列出风险和未覆盖项，不修改文件。"
                "【工具失败降级】read_file/search_code 失败时参考 explore 角色的降级策略。"
            ),
            "general": (
                "独立完成任务，按需使用可用工具。"
                "【工具失败降级】任一工具失败时优先尝试同类替代工具，"
                "所有替代都失败后明确说明原因，由主 Agent 决策下一步。"
            ),
        }
        role_when_to_use = {
            "explore": "只读探索代码库、查找文件、搜索内容、回答代码相关问题时使用，预期会超过 3 次工具调用的探索任务优先用此类型。",
            "implement": "需要修改代码、实现功能、修复 bug 时使用，会读写文件和执行命令。",
            "test": "需要运行测试、定位失败根因、验证实现时使用，可执行命令但不修改业务代码。",
            "review": "需要审查代码质量、安全性、可维护性时使用，只读不修改。",
            "general": "独立完成综合性任务时使用，按需调用所有可用工具。",
        }
        if custom:
            role = custom.get("base_type", "general")
            if role not in role_instructions:
                role = "general"
            custom_system_prompt = custom.get("system_prompt") or ""
            custom_allowed_tools = custom.get("allowed_tools")
        else:
            role = task.subagent_type if task.subagent_type in role_instructions else "general"
            custom_system_prompt = ""
            custom_allowed_tools = None

        # 白名单定义在模块级 _SUBAGENT_DEFAULT_ALLOWED_TOOLS，便于单测直接断言
        # 角色工具面（曾因局部硬编码遗漏 novel_audit 等只读插件工具导致计划死锁）。
        allowed_tools = (
            custom_allowed_tools if custom_allowed_tools is not None
            else default_subagent_allowed_tools(role)
        )
        instruction_text = custom_system_prompt if custom_system_prompt else role_instructions[role]
        # T3 子代理公共约束块：安全 / 证据 / 工具面 / 防伪 / 信源分级五条对内置与自定义子代理
        # 一律生效。拼进 instruction_text 而非仅在首请求拼接——后续「续写总结」请求
        # （cont_messages）同样复用 instruction_text，两处请求自动带上同一份约束。
        # 顺序：身份正文在前、公共约束在后（约束是底线，不可被用户自定义 body 覆盖）。
        instruction_text = instruction_text + "\n" + build_subagent_common_block(allowed_tools)
        messages = [
            {
                "role": "system",
                "content": (
                    f"你是{_agent_display_name()}子代理({task.task_id})，角色是 {role}。"
                    f"{instruction_text}"
                    "你只执行主 Agent 已分配的单一子任务，不得拆解、创建或修改主任务计划；"
                    "如实使用可用工具；结束时只输出供主 Agent 汇总的公开结论。"
                    "【速度要求】子任务必须快速收敛：优先用 search_code / glob_files / "
                    "list_directory 定位关键位置后精读相关片段，不要逐文件全文通读；"
                    "信息足以支撑结论时立即停止探索并输出最终结论，不要追求穷尽。"
                    "【结论格式】最终结论只写主 Agent 决策所需的事实：做了什么、"
                    "改了哪些文件、验证结果、未完成项与风险。不要复述任务描述，"
                    "不要逐条回顾工具调用过程，不要做与任务无关的推测性延伸。"
                    "所有输出必须使用中文，包括过程说明与最终结论；"
                    "不要输出 [Advisor consultation] 等内部协议标记，"
                    "也不要在正文中书写 <function_calls>/<advisor-request> 之类的 XML 伪工具调用。"
                ),
            },
            {
                "role": "user",
                "content": f"请完成以下子任务：\n{task.description}",
            },
        ]
        def _budget_notice_provider():
            budget = int(task.timeout_seconds or 0)
            remaining = budget - float(task.execution_elapsed or 0.0)
            level, text = build_subagent_budget_notice(
                remaining,
                budget,
                task.budget_notice_stage,
            )
            if text is None:
                return None
            task.budget_notice_stage = level
            self._record_stage(
                task.task_id,
                "budget_notice",
                f"剩余约 {int(max(0.0, remaining))}s 已提醒收尾",
            )
            return text

        self._set_task_model_name(
            task.task_id,
            _resolve_subagent_model_name(current_model, current_model_id),
        )
        current_round = 1
        self._set_task_rounds(task.task_id, current_round)
        self._record_stage(task.task_id, "model")
        # 子代理执行具体计划项时，不应继承父会话的计划锁约束；
        # 否则子代理会被剥夺 write_file/edit_file/execute_command 等必要工具，
        # 导致只能只读探索而无法完成实现/测试任务，最终超时失败。
        events = chat_with_model_events(
            current_model,
            current_key,
            messages,
            current_url,
            persist=False,
            turn_id=task.task_id,
            cancel_event=task.cancel_event,
            allow_subagents=False,
            allowed_tools=allowed_tools,
            consume_background_notifications=False,
            parent_session=None,
            parent_turn_id=None,
            permission_owner=task.task_id,
            permission_source=f"子代理：{task.description[:40]}",
            cache_scope=f"subagent:{role}",
            cache_session_id=task.parent_session,
            model_id=current_model_id,
            budget_notice_provider=_budget_notice_provider,
        )

        answer = []
        tool_errors: dict[str, str] = {}
        success_count = 0
        partial_parts: list[str] = []
        partial_len = 0
        partial_flushed = 0
        last_partial_flush = self._monotonic()
        saw_tool_finished = False
        turn_error_message = ""
        # 进入子代理深度作用域：在此作用域内 _get_current_subagent_depth() 返回新深度。
        # 必须把事件消费循环包在作用域内，因为生成器创建时并不执行子代理逻辑；
        # 若只包生成器创建语句，深度会在真正执行前复位，嵌套深度限制将永久失效。
        try:
            with _subagent_depth_scope():
                for event in events:
                    if event.kind == "thinking_started":
                        if saw_tool_finished:
                            current_round += 1
                            saw_tool_finished = False
                            self._set_task_rounds(task.task_id, current_round)
                        self._record_stage(task.task_id, "thinking")
                    elif event.kind == "tool_started":
                        if saw_tool_finished:
                            current_round += 1
                            saw_tool_finished = False
                            self._set_task_rounds(task.task_id, current_round)
                        self._record_tool_started(
                            task.task_id,
                            str(event.payload.get("id") or ""),
                            current_round,
                            dict(event.payload),
                        )
                        self._record_stage(
                            task.task_id,
                            "tool",
                            str(event.payload.get("name", "")),
                        )
                        answer.clear()
                    elif event.kind == "permission_requested":
                        try:
                            from core.tool_presenter import describe_permission_request

                            approval_text = describe_permission_request(
                                str(event.payload.get("function_name") or ""),
                                dict(event.payload.get("arguments") or {}),
                            )
                        except Exception:
                            approval_text = str(event.payload.get("function_name") or "")
                        self._set_approval_summary(task.task_id, approval_text[:240])
                        self._record_stage(task.task_id, "waiting_approval")
                    elif event.kind == "permission_resolved":
                        self._set_approval_summary(task.task_id, "")
                        self._record_stage(task.task_id, "running", "approval resolved")
                    elif event.kind == "assistant_delta":
                        if saw_tool_finished:
                            current_round += 1
                            saw_tool_finished = False
                            self._set_task_rounds(task.task_id, current_round)
                        self._record_stage(task.task_id, "composing")
                        text = str(event.payload.get("text", ""))
                        answer.append(text)
                        partial_parts.append(text)
                        partial_len += len(text)
                        now_mono = self._monotonic()
                        if partial_len > partial_flushed and (
                            partial_len - partial_flushed >= 200
                            or now_mono - last_partial_flush >= 0.6
                        ):
                            self._record_partial(task.task_id, "".join(partial_parts))
                            partial_flushed = partial_len
                            last_partial_flush = now_mono
                    elif event.kind == "tool_finished":
                        saw_tool_finished = True
                        name = str(event.payload.get("name", "tool"))
                        self._record_tool_finished(
                            task.task_id,
                            str(event.payload.get("id") or ""),
                            current_round,
                            dict(event.payload),
                        )
                        if event.payload.get("ok") is False:
                            err = str(
                                event.payload.get("summary")
                                or event.payload.get("error")
                                or "工具执行失败"
                            )
                            tool_errors[name] = err
                            self._record_stage(
                                task.task_id,
                                "tool",
                                f"{name} ✗ {' '.join(err.split())[:120]}",
                            )
                        else:
                            self._record_stage(
                                task.task_id,
                                "tool",
                                f"{name} ✓ {_format_tool_stage_duration(event.payload.get('duration_ms'))}",
                            )
                            tool_errors.pop(name, None)
                            success_count += 1
                    elif event.kind == "turn_error":
                        turn_error_message = str(
                            event.payload.get("message") or turn_error_message
                        )
                    elif event.kind == "diff_ready":
                        self._record_changed_file(
                            task.task_id,
                            str(event.payload.get("file_path") or ""),
                        )
                        # 子代理修改文件产生的 diff 必须转发回父回合，否则父回合的
                        # "文件更改"面板会丢失子代理的改动。tool_call_id 是子代理
                        # 内部的调用 id，父端匹配不到会归入 legacy_diffs 兜底，
                        # 仍能汇总展示。后台子代理在父回合结束后才转发时，父端
                        # 事件流已收尾，转发为 best-effort（失败不影响主流程）。
                        if task.event_callback is not None:
                            try:
                                task.event_callback("diff_ready", dict(event.payload))
                            except Exception:
                                _logger.warning("转发子代理 diff_ready 事件失败", exc_info=True)
        finally:
            # 现场打捞兜底：与循环内的周期 flush 幂等（_record_partial 只在更长时覆盖）。
            # 事件循环中途抛异常（如网络错误）时，周期 flush 只保到上一个刷新边界，
            # 靠这里把尾部正文也捞住，不改变异常传播。
            self._record_partial(task.task_id, "".join(partial_parts))
        task.tool_errors.update(tool_errors)
        task.tool_success_count = max(task.tool_success_count, success_count)
        task.turn_error = turn_error_message
        result_text = "".join(answer)

        if (
            len(result_text.strip()) < 120
            and not task.cancel_event.is_set()
            and not turn_error_message
        ):
            continuation_prompt = (
                "你之前的回答过于简短。请提供更完整的总结，包含：\n"
                "1. 具体的技术细节和实现要点\n"
                "2. 关键发现与分析结论\n"
                "3. 主 Agent 需要知晓的所有重要信息\n"
                "请直接输出完整内容，不要重复之前的简短回答。"
            )
            cont_messages = [
                {
                    "role": "system",
                    "content": (
                        f"你是{_agent_display_name()}子代理({task.task_id})，角色是 {role}。"
                        f"{instruction_text}"
                        "你只执行主 Agent 已分配的单一子任务；"
                        "结束时只输出供主 Agent 汇总的公开结论。"
                        "所有输出必须使用中文。"
                    ),
                },
                {"role": "user", "content": f"请完成以下子任务：\n{task.description}"},
                {"role": "assistant", "content": result_text or "（未输出最终结论）"},
                {"role": "user", "content": continuation_prompt},
            ]
            self._record_stage(task.task_id, "summarizing")
            try:
                cont_events = chat_with_model_events(
                    current_model,
                    current_key,
                    cont_messages,
                    current_url,
                    persist=False,
                    turn_id=task.task_id,
                    cancel_event=task.cancel_event,
                    allow_subagents=False,
                    allowed_tools=None,
                    consume_background_notifications=False,
                    parent_session=None,
                    parent_turn_id=None,
                    permission_owner=task.task_id,
                    permission_source=f"子代理总结：{task.description[:40]}",
                    model_id=current_model_id,
                )
                cont_answer = []
                with _subagent_depth_scope():
                    for event in cont_events:
                        if event.kind == "assistant_delta":
                            cont_answer.append(str(event.payload.get("text", "")))
                continued = "".join(cont_answer).strip()
                # 只有重试结果更长时才采用，避免重试反而更差
                if len(continued) > len(result_text.strip()):
                    result_text = continued
            except Exception:
                # 总结续写失败不影响主流程，返回原始结果
                pass

        return result_text


subagent_manager = SubagentManager()


# 按角色的默认超时（秒）：只读角色必须快速收敛，写代码/跑测试允许更久。
# 调用方显式传 timeout_seconds 时以显式值为准。
_SUBAGENT_DEFAULT_TIMEOUTS = {
    "explore": 300,
    "review": 420,
    "test": 900,
    "implement": 900,
    "general": 900,
}

# 前台子代理的等待上限（秒）：前台会阻塞父回合，不能无限拉长；
# 超过该预算的大任务应转后台（后台默认预算按系数放宽）。
SUBAGENT_FOREGROUND_MAX_TIMEOUT = 900
SUBAGENT_BACKGROUND_TIMEOUT_FACTOR = 1.5


def make_authorized_verification_runner(
    *,
    permission_owner,
    parent_turn_id,
    permission_source,
    cancel_event,
    event_callback=None,
):
    def authorized_verification_runner(command):
        from core.llm import PermissionSystemError, check_permission_and_ask

        if cancel_event is not None and cancel_event.is_set():
            return False, "验证命令已取消"
        try:
            approved = check_permission_and_ask(
                "execute_command",
                {"command": command},
                event_callback=event_callback,
                permission_owner=permission_owner,
                parent_turn_id=parent_turn_id,
                permission_source=permission_source,
                cancel_event=cancel_event,
            )
        except PermissionSystemError:
            return False, "验证命令授权系统异常"
        if not approved:
            return False, "验证命令未获用户授权"

        execution_result = _CommandExecutionResult()
        detail = str(
            execute_command(
                command,
                timeout_seconds=120,
                cancel_event=cancel_event,
                _execution_result=execution_result,
            )
            or ""
        ).strip()
        return execution_result.returncode == 0, detail or "验证通过"

    return authorized_verification_runner


def _current_tool_call_id() -> str:
    try:
        import core.app_context as _app_context

        return str(_app_context.get_thread_tool_call_id() or "")
    except Exception:
        return ""


def run_subagent(
    task_description,
    run_in_background=False,
    timeout_seconds=None,
    parent_session=None,
    parent_turn_id=None,
    parent_cancel_event=None,
    subagent_type="general",
    plan_item_id="",
    event_callback=None,
):
    # 子代理深度硬限制：防止无限递归（参考 OpenCode limit subagent nesting depth）
    # 主代理线程 depth=0，子代理 depth=1，孙代理 depth=2，重孙 depth=3 → 超限拒绝
    current_depth = _get_current_subagent_depth()
    if current_depth >= SUBAGENT_MAX_DEPTH:
        return json.dumps({
            "status": "failed",
            "plan_item_id": str(plan_item_id or ""),
            "error": (
                f"子代理嵌套深度已达上限 {SUBAGENT_MAX_DEPTH}（当前 {current_depth}），"
                f"拒绝继续创建子代理。请改用直接工具执行任务，或重新组织任务结构。"
            ),
        }, ensure_ascii=False)

    # 先完成参数校验，再认领计划项，避免认领后校验失败导致计划项永久 in_progress
    # timeout_seconds 未传时保持 None，待 subagent_type 最终确定后按角色解析默认值
    if timeout_seconds is not None:
        try:
            timeout_seconds = int(timeout_seconds)
        except (TypeError, ValueError):
            timeout_seconds = -1
        if not 30 <= timeout_seconds <= 3600:
            return json.dumps({
                "status": "failed",
                "plan_item_id": str(plan_item_id or ""),
                "error": "timeout_seconds 必须在 30 到 3600 秒之间。",
            }, ensure_ascii=False)
        if not run_in_background and timeout_seconds > SUBAGENT_FOREGROUND_MAX_TIMEOUT:
            # 前台硬上限（内部调用同样受限）：模型不得把前台子 Agent 超时扩大到
            # 2400/3600 秒；长命令应使用后台任务 + manage_task(wait) 等待。
            return json.dumps({
                "status": "failed",
                "plan_item_id": str(plan_item_id or ""),
                "error": (
                    "前台子代理 timeout_seconds 硬上限为 {} 秒（当前 {} 秒）。"
                    "长任务请拆分为更小的计划项，或改用 run_in_background=true "
                    "启动后台任务并用 manage_task(action='wait') 等待结果。"
                ).format(SUBAGENT_FOREGROUND_MAX_TIMEOUT, timeout_seconds),
            }, ensure_ascii=False)
    if not str(task_description or "").strip():
        return json.dumps({
            "status": "failed",
            "plan_item_id": str(plan_item_id or ""),
            "error": "task_description 不能为空。",
        }, ensure_ascii=False)
    if len(str(task_description or "")) > 6000:
        # 超长任务描述明确失败：不截断、不静默改写，避免模型误判任务范围
        return json.dumps({
            "status": "failed",
            "plan_item_id": str(plan_item_id or ""),
            "error": (
                "task_description 超过 6000 字符上限（当前 {} 字符）。"
                "请拆分为多个计划项/子任务，或精简任务描述；本次输入未被截断或改写。"
            ).format(len(str(task_description))),
        }, ensure_ascii=False)
    # 全局子代理开关（设置 → 子代理）硬拦截：位于参数校验之后、认领计划项之前，
    # 保证插件/hook/工作流/恢复等一切直调路径同样不派发子代理（模型侧虽已把
    # run_subagent 从工具表摘除，直调仍需入口兜底）。返回结构与本函数其它失败
    # 返回完全一致（status/plan_item_id/error）。
    from core.subagent_settings import orchestration_allowed as _orchestration_allowed

    if not _orchestration_allowed(True):
        return json.dumps({
            "status": "failed",
            "plan_item_id": str(plan_item_id or ""),
            "error": (
                "子代理已在设置中关闭（设置 → 子代理），本次任务不派发子代理；"
                "请使用你自己的工具按顺序完成该子任务。"
            ),
        }, ensure_ascii=False)
    if parent_session is None or parent_turn_id is None:
        import core.app_context as app_context
        parent_session = parent_session or app_context.get_current_session()
        parent_turn_id = parent_turn_id or f"standalone-{uuid.uuid4().hex[:8]}"
    scheduler = None
    if plan_item_id:
        from core.task_scheduler import get_default_scheduler

        scheduler = get_default_scheduler()
        plan_item = next(
            (item for item in scheduler.plan.snapshot() if item["id"] == str(plan_item_id)),
            None,
        )
        if plan_item is None:
            # 计划项查无此项时必须区分两种根因，否则会把平台侧路由缺陷
            # 谎报成“计划项不存在”，误导模型放弃或改写计划：
            #   1) 执行线程未绑定计划会话（绑定环境缺失，如并行工具线程
            #      未继承计划路由）——可重试的环境问题；
            #   2) 绑定会话内确实没有该 id——模型应核对 id 而不是重试。
            from core.plan import get_bound_plan_session

            bound_session = get_bound_plan_session()
            if bound_session is None:
                return json.dumps({
                    "status": "blocked",
                    "reason": "plan_binding_missing",
                    "plan_item_id": str(plan_item_id),
                    "error": (
                        f"无法定位计划项 '{plan_item_id}'：当前执行线程未绑定计划会话"
                        "（绑定环境缺失，属平台侧计划路由问题，不是计划项状态冲突）。"
                        "请重试本项；若持续失败可改用单发（非同批并发）方式派发，"
                        "并先用 plan_read 核对计划项 id。"
                    ),
                }, ensure_ascii=False)
            return json.dumps({
                "status": "blocked",
                "reason": "plan_item_not_found",
                "plan_item_id": str(plan_item_id),
                "error": (
                    f"计划会话 '{bound_session}' 中不存在计划项 '{plan_item_id}'，"
                    "请先用 plan_read 核对当前会话的计划项 id。"
                ),
            }, ensure_ascii=False)
        if plan_item is not None and plan_item.get("kind") == "input_preparation":
            return json.dumps({
                "status": "blocked",
                "plan_item_id": str(plan_item_id),
                "error": "用户输入准备项必须由 ask_user 执行，不能分配给子代理。",
            }, ensure_ascii=False)
        if plan_item is not None and (not subagent_type or subagent_type == "general"):
            subagent_type = {
                "plan": "explore",
                "build": "implement",
                "review": "review",
            }.get(plan_item.get("stage"), "general")
        if plan_item is not None and bool(plan_item.get("takeover_required", False)):
            # 首次超时后主 Agent 已接管（或排队等待接管）：同一计划项禁止再次
            # 派发子代理（timed_out → takeover ≠ retry → 新子代理）。
            # 主 Agent 应先只读核查现场，再直接用自身工具继续并 plan_finish。
            status = str(plan_item.get("status", ""))
            mode = str(plan_item.get("execution_mode", ""))
            if status in {"failed", "blocked", "cancelled"}:
                guidance = (
                    f"请调用 plan_resume_takeover(item_id='{plan_item_id}')，"
                    "由当前主 Agent 回合继续原尝试。"
                )
            elif mode == "takeover_queued":
                guidance = "该项正在等待主 Agent 按 FIFO 认领，请勿重新派发。"
            else:
                guidance = "主 Agent 应直接检查现场并继续执行，完成后用 plan_finish 收尾。"
            return json.dumps({
                "status": "blocked",
                "reason": "main_takeover_required",
                "plan_item_id": str(plan_item_id),
                "error": (
                    f"计划项 '{plan_item_id}' 已发生过子代理超时接管"
                    f"（status={status}, execution_mode={mode or 'none'}）。"
                    f"禁止重新派发子代理；{guidance}"
                ),
            }, ensure_ascii=False)
        owner = f"subagent:{subagent_type or 'general'}"
        # 先生成真实执行租约 id：计划项认领（start_delegated_item）与
        # SubagentManager 注册必须绑定同一个 id，禁止事后猜测或用角色名代替
        delegated_task_id = _new_subagent_task_id()
        if not scheduler.start_delegated_item(
            str(plan_item_id),
            delegated_task_id,
            owner,
            event_callback,
        ):
            # 构造详细的失败原因，帮助 LLM 诊断
            actual_status = plan_item.get("status", "unknown") if plan_item else "不存在"
            reason_parts = [f"计划项 '{plan_item_id}' 当前状态为 '{actual_status}'"]
            if plan_item and actual_status in {"failed", "blocked", "cancelled"}:
                reason_parts.append("（普通失败项需先用 plan_retry 重置；超时接管项必须用 plan_resume_takeover）")
            if plan_item:
                deps = plan_item.get("depends_on", [])
                if deps:
                    dep_statuses = []
                    for dep_id in deps:
                        dep_item = next((i for i in scheduler.plan.snapshot() if i["id"] == dep_id), None)
                        dep_statuses.append(f"'{dep_id}'({dep_item['status'] if dep_item else '不存在'})")
                    reason_parts.append(f"依赖项状态: {', '.join(dep_statuses)}")
            # 并发已满是最常见且可自愈的原因：必须明确说出来，否则模型会把
            # "认领失败"误判为永久性冲突而直接放弃整个计划项
            in_progress_count = sum(
                1 for i in scheduler.plan.snapshot() if i.get("status") == "in_progress"
            )
            if (
                plan_item is not None
                and actual_status == "pending"
                and in_progress_count >= scheduler.max_workers
            ):
                reason_parts.append(
                    f"当前并行执行的计划项已达调度上限 {scheduler.max_workers}"
                    f"（{in_progress_count} 项运行中）。这不是冲突或死锁，"
                    "请等待任一运行中的计划项完成后重新调用本项即可成功。"
                )
            return json.dumps({
                "status": "blocked",
                "plan_item_id": str(plan_item_id),
                "error": "计划项尚未满足依赖、已暂停或已有冲突任务在执行。" + " ".join(reason_parts),
            }, ensure_ascii=False)

    def terminal_callback(task, status, result, error):
        if scheduler is None or not task.plan_item_id:
            return None
        item_id = task.plan_item_id
        if status == "timed_out":
            # 首次超时：原子移交主 Agent（同一次尝试的延续，不增加 attempts）。
            # handoff 失败时保留 timed_out 事实（handoff=failed），
            # 绝不改写成普通 failed 后重新委派。
            handoff_item = scheduler.handoff_to_main(
                item_id,
                task.task_id,
                parent_turn_id,
                error or "subagent timeout",
            )
            if handoff_item is not None:
                task.handoff = str(
                    handoff_item.get("execution_mode") or "main_takeover"
                )
                if event_callback is not None:
                    from core.task_scheduler import takeover_handoff_payload

                    event_callback(
                        "plan_item_handoff",
                        takeover_handoff_payload(handoff_item),
                    )
                return None
            task.handoff = "failed"
            return (
                f"计划项 {item_id} 移交主 Agent 失败（现场状态异常），"
                "请勿重新委派子代理；主 Agent 应直接用 plan_read 检查后接管。"
            )
        if status == "completed":
            plan_item = next(
                (item for item in scheduler.plan.snapshot() if item["id"] == item_id),
                None,
            )
            subject = str(plan_item.get("subject") or item_id) if plan_item else item_id

            authorized_verification_runner = make_authorized_verification_runner(
                permission_owner=owner,
                parent_turn_id=parent_turn_id,
                permission_source=f"计划项验证：{subject}",
                cancel_event=task.cancel_event,
                event_callback=event_callback,
            )

            if scheduler.complete(
                item_id,
                result,
                verification_runner=authorized_verification_runner,
                cancel_event=task.cancel_event,
            ):
                if event_callback is not None:
                    event_callback("plan_updated", {
                        "items": scheduler.plan.snapshot(),
                        "live_only": True,
                    })
                return None
            item = next((x for x in scheduler.plan.snapshot() if x["id"] == item_id), None)
            if item is None:
                return f"计划项 {item_id} 不存在，无法验证完成。"
            item_error = str(item.get("error") or "").strip()
            if item_error:
                if item.get("verification_type") == "command":
                    return f"计划项验证失败（command）：{item_error}"
                return f"计划项验证失败：{item_error}"
            if item.get("status") != "in_progress":
                return f"计划项 {item_id} 当前状态为 {item.get('status')}，不是 in_progress，无法验证完成。"
            return f"计划项 {item_id} 完成回写失败，请通过 plan_read 查看状态后重试。"
        if status == "cancelled":
            scheduler.cancel(item_id, error or "子代理已取消")
        else:
            scheduler.fail(item_id, error or f"子代理状态: {status}")
        if event_callback is not None:
            event_callback("plan_updated", {
                "items": scheduler.plan.snapshot(),
                "live_only": True,
            })
        return None

    # 此时 subagent_type 已最终确定（含计划 stage 自动映射），解析角色默认超时。
    # 只读角色（explore/review）预算更短：子代理设计目标是"快"；
    # 写代码/跑测试角色按实测 p99 给到 900s，避免"快做完被硬砍"。
    # 后台任务不阻塞用户，默认预算再放宽 1.5 倍；显式传入的预算不做放大。
    if timeout_seconds is None:
        timeout_seconds = _SUBAGENT_DEFAULT_TIMEOUTS.get(str(subagent_type or "general"), 900)
        if run_in_background:
            timeout_seconds = min(
                3600,
                int(timeout_seconds * SUBAGENT_BACKGROUND_TIMEOUT_FACTOR),
            )

    try:
        return subagent_manager.run(
            task_description,
            parent_session,
            parent_turn_id,
            run_in_background=run_in_background,
            timeout_seconds=timeout_seconds,
            parent_cancel_event=parent_cancel_event,
            subagent_type=subagent_type,
            plan_item_id=plan_item_id,
            terminal_callback=terminal_callback if scheduler is not None else None,
            event_callback=event_callback,
            task_id=delegated_task_id if scheduler is not None else None,
            tool_call_id=_current_tool_call_id(),
        )
    except Exception as error:
        if scheduler is not None and plan_item_id:
            scheduler.fail(str(plan_item_id), f"子代理启动失败: {error}")
            if event_callback is not None:
                event_callback("plan_updated", {
                    "items": scheduler.plan.snapshot(),
                    "live_only": True,
                })
        return json.dumps({
            "status": "failed",
            "plan_item_id": str(plan_item_id or ""),
            "error": str(error),
        }, ensure_ascii=False)

def dream_consolidation():
    from core.memory_kairos import run_dream_consolidation_sync
    task_model, task_key, task_url, _task_model_id = _unpack_model_config(
        _active_model_tuple_or_none()
    )

    if not task_key:
        return "🚨 整理失败：大模型密钥尚未配置，无法提炼记忆。"
        
    return run_dream_consolidation_sync(task_model, task_key, task_url)

def recall_history(before_msg_id, limit=16, cancel_event=None):
    import core.app_context as _app_context

    session_name = ""
    try:
        session_name = str(
            _app_context.get_thread_session() or _app_context.get_current_session() or ""
        ).strip()
    except Exception:
        session_name = ""
    if not session_name:
        return "无法确定当前会话，无法回溯历史。"
    try:
        manager = _app_context.get_manager()
    except Exception:
        manager = None
    if manager is None:
        return "会话存储未就绪，无法回溯历史。"

    if cancel_event is not None and cancel_event.is_set():
        return "🛑 操作已被用户取消。"

    try:
        rows = manager.get_compressed_messages(session_name, before_msg_id, limit)
    except Exception as exc:
        return f"回溯历史失败：{exc}"
    if not rows:
        return (
            "该范围内没有已压缩的原始消息。before_msg_id 需指向摘要水位覆盖的"
            "区间（取值见记忆摘要块尾部的[更早摘要回溯]索引中的 msg_id）。"
        )

    lines = []
    for row in rows:
        content = str(row.get("content") or "").strip()
        if len(content) > 600:
            content = content[:600] + "...[该条过长，已截断]"
        lines.append(f"#msg_id={row.get('id')} [{row.get('role')}]\n{content}")
    header = (
        f"共回溯 {len(rows)} 条已压缩原始消息（按时间正序；before_msg_id 含边界自身）。"
        "需要更早的内容时，把 before_msg_id 设为本批最早一条的 msg_id 减 1 再调用。"
    )
    return header + "\n\n" + "\n\n".join(lines)

def search_sessions(query, limit=10, session=None, cancel_event=None):
    keyword = str(query or "").strip()
    if not keyword:
        return "🚨 请提供要检索的关键词（query 不能为空）。"
    if cancel_event is not None and cancel_event.is_set():
        return "🛑 操作已被用户取消。"
    try:
        import core.app_context as _app_context
        manager = _app_context.get_manager()
    except Exception:
        manager = None
    if manager is None:
        return "会话存储未就绪，无法全文检索历史消息。"
    try:
        limit_n = int(limit)
    except (TypeError, ValueError):
        limit_n = 10
    limit_n = max(1, min(50, limit_n))
    session_name = str(session or "").strip() or None
    try:
        rows = manager.search_messages(
            keyword, limit=limit_n, session_name=session_name
        )
    except Exception as exc:
        return f"全文检索失败：{exc}"
    if rows is None:
        return "当前 SQLite 未启用 FTS5，全文检索不可用（不影响其它功能）。"
    if not rows:
        return (
            "未找到匹配消息；检索覆盖当前数据库内已写入的消息，"
            "索引会在每次检索前增量补齐。"
        )
    if cancel_event is not None and cancel_event.is_set():
        return "🛑 操作已被用户取消。"

    lines = []
    for row in rows:
        if cancel_event is not None and cancel_event.is_set():
            return "🛑 操作已被用户取消。"
        snippet = str(row.get("snippet") or "").replace("\n", " ").strip()
        if len(snippet) > 400:
            snippet = snippet[:400] + "…"
        if not snippet:
            snippet = "(该条无可见片段)"
        display = row.get("display_name") or row.get("session_name") or "(未知)"
        lines.append(
            f"#msg_id={row.get('id')} [{row.get('role')}] 会话: {display}\n{snippet}"
        )
    scope = f"（限定会话: {session_name}）" if session_name else "（跨全部会话）"
    header = (
        f"命中 {len(rows)} 条消息{scope}，按相关度排序，最多返回 {limit_n} 条；"
        "片段中的 《》 标记匹配到的关键词。"
    )
    return header + "\n\n" + "\n\n".join(lines)

def tool_result_read(handle, mode="head", lines=80, grep=None, cancel_event=None):
    """取回被 snip/elide 折叠的工具结果原文（tool_result_read 工具执行体）。

    句柄取自 marker 里的 `#xxxxxxxx`，如
    `[snipped tool result #ab12cd34 — read_file, ...]`。纯只读，不改任何状态。
    """
    from core.tool_result_maintenance import is_valid_handle

    if not is_valid_handle(handle):
        return (
            "🚨 无效的句柄。句柄是 marker 里 `#` 后的 8 位小写十六进制"
            "（形如 [snipped tool result #ab12cd34 — read_file, ...]）。"
        )
    if cancel_event is not None and cancel_event.is_set():
        return "🛑 操作已被用户取消。"
    try:
        import core.app_context as _app_context
        manager = _app_context.get_manager()
    except Exception:
        manager = None
    if manager is None:
        return "会话存储未就绪，无法取回工具结果。"
    try:
        row = manager.get_tool_result_archive(handle)
    except Exception as exc:
        return f"取回工具结果失败：{exc}"
    if not row:
        return (
            f"归档中没有句柄 #{handle} 的记录。归档仅覆盖被 snip/elide 折叠过的"
            "工具结果（未折叠的结果仍在上下文里，无需取回）。"
        )
    content = str(row.get("content") or "")
    body_lines = content.split("\n")
    total_lines = len(body_lines)
    header = (
        f"归档工具结果 #{row.get('handle')}｜来源工具: {row.get('tool_name') or '(未知)'}"
        f"｜原始 {row.get('bytes')} 字节 / {total_lines} 行"
        f"｜改写前保存于 {row.get('created_at') or '(未知)'}"
    )
    mode = str(mode or "head").strip().lower()
    if mode not in ("head", "tail", "grep", "full"):
        mode = "head"
    try:
        line_limit = int(lines)
    except (TypeError, ValueError):
        line_limit = 80
    line_limit = max(1, min(2000, line_limit))
    if mode == "grep":
        pattern = str(grep or "").strip()
        if not pattern:
            return header + "\n\n🚨 mode=grep 需要同时提供 grep 参数（要搜索的文本）。"
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = None
        hits = []
        for idx, line in enumerate(body_lines, 1):
            if cancel_event is not None and idx % 200 == 0 and cancel_event.is_set():
                return "🛑 操作已被用户取消。"
            if (regex.search(line) if regex else pattern.lower() in line.lower()):
                hits.append(f"{idx}:{line[:500]}")
                if len(hits) >= 100:
                    break
        if not hits:
            return header + f"\n\n命中 0 行（grep={pattern!r}）。"
        return header + f"\n\n命中 {len(hits)} 行（最多 100 行，格式 行号:内容）:\n" + "\n".join(hits)
    if mode == "full":
        # 全文保护上限：超过 200000 字符时截断，避免把一次折叠省下的字节又灌回上下文
        full_text = content
        note = ""
        if len(full_text) > 200000:
            full_text = full_text[:200000]
            note = "\n\n[... 全文超过 200000 字符，已截断；建议改用 mode=grep 精确检索]"
        return header + f"\n\n全文（{len(content)} 字符）:\n" + full_text + note
    if mode == "tail":
        selected = body_lines[-line_limit:]
        start = total_lines - len(selected) + 1
        label = f"末尾 {len(selected)} 行（从第 {start} 行开始）"
    else:
        selected = body_lines[:line_limit]
        label = f"开头 {len(selected)} 行"
    return header + f"\n\n{label}（全文共 {total_lines} 行）:\n" + "\n".join(selected)

def glob_files(pattern, dir_path=".", cancel_event=None):
    """快速文件模式匹配：使用 glob 表达式在项目目录中搜索匹配的文件路径列表。优先尝试 ripgrep。"""
    import glob
    import subprocess
    from pathlib import Path

    try:
        base = _resolve_workspace_path(dir_path)
    except ValueError as e:
        return f"🚨 错误: {e}"
    if not base.exists() or not base.is_dir():
        return f"🚨 错误：指定的目录不存在或非有效目录: {dir_path}"
    if cancel_event is not None and cancel_event.is_set():
        return "🛑 操作已被用户取消。"

    cache_key = _tool_cache.glob_key(pattern, str(base))
    cached = _tool_cache.glob_get(cache_key)
    if cached is not None:
        return cached

    session_id = _tool_cache.get_current_session()
    if session_id:
        shared_key = ("glob_files", cache_key)
        shared = _tool_cache.shared_get(session_id, shared_key)
        if shared is not None:
            return shared

    try:
        rg = _get_rg_executable()
        cmd = [rg, "--files", "--glob", pattern]
        for _d in sorted(_get_search_exclude_dirs()):
            cmd += ["--glob", f"!**/{_d}/"]
        cmd.append(str(base))
        result = _run_process_polled(cmd, SEARCH_CODE_TIMEOUT_SECONDS, cancel_event)
        if result is None:
            return "🛑 操作已被用户取消。"
        if result.returncode in (0, 1):
            output = result.stdout.strip()
            if output:
                lines = output.split("\n")
                rel_matches = []
                for line in lines:
                    try:
                        rel_matches.append(str(Path(line).relative_to(base)))
                    except ValueError:
                        rel_matches.append(line)
                max_results = 100
                truncated = len(rel_matches) > max_results
                res = "\n".join(rel_matches[:max_results])
                if truncated:
                    res += f"\n\n... (匹配结果过多，已截断，共 {len(rel_matches)} 个匹配文件)"
                _tool_cache.glob_put(cache_key, res)
                if session_id:
                    _tool_cache.shared_put(session_id, ("glob_files", cache_key), res)
                return res
            res = f"未找到匹配 `{pattern}` 的文件。"
            _tool_cache.glob_put(cache_key, res)
            if session_id:
                _tool_cache.shared_put(session_id, ("glob_files", cache_key), res)
            return res
        elif result.returncode != 0:
            _logger.warning("glob_files rg 错误 (rc=%s): %s", result.returncode, result.stderr.strip())
    except FileNotFoundError:
        _logger.debug("glob_files 未找到 ripgrep，回退到 Python glob")
    except Exception as e:
        _logger.warning("glob_files rg 调用异常: %s", e)

    try:
        matches = glob.glob(pattern, root_dir=str(base), recursive=True)
        try:
            matches = sorted(
                matches,
                key=lambda p: (base / p).stat().st_mtime,
                reverse=True,
            )
        except Exception:
            pass
        if not matches:
            result_text = "没有找到匹配该模式的文件。"
        else:
            max_results = 100
            truncated = len(matches) > max_results
            result_text = "\n".join(matches[:max_results])
            if truncated:
                result_text += f"\n\n... (匹配结果过多，已截断，共 {len(matches)} 个匹配文件)"
        _tool_cache.glob_put(cache_key, result_text)
        if session_id:
            _tool_cache.shared_put(session_id, ("glob_files", cache_key), result_text)
        return result_text
    except Exception as e:
        return f"🚨 快速匹配工具发生异常: {str(e)}"


def undo_turn(turn_id=""):
    """按回合撤销工具（规格 18）：撤销指定回合（缺省为最近可撤销回合）的
    全部受控文件修改。

    只接受回合标识；不接收文件路径、force 或任意撤销参数。权限由管线在
    执行前经权限中介卡片确认（卡片内展示服务预检的影响摘要）；一次性
    确认令牌在服务内部消费，绝不返回给模型或进入消息历史。
    """
    from core.turn_undo import service as tu_service
    from core.turn_undo.models import TurnUndoError, TurnUndoStatus

    from core import app_context

    session_name = app_context.get_thread_session() or ""
    if not session_name:
        return "🚨 无法确定当前会话，撤销工具不可用。"
    session_id = None
    resolved = None
    try:
        session_id = tu_service.resolve_session_id(session_name)
        resolved = tu_service.resolve_turn_id_in_session(session_id, turn_id)
        preview, token = tu_service.preview_turn_undo(session_id, resolved)
    except TurnUndoError as error:
        try:
            checkpoint = tu_service.get_default_storage().get_checkpoint_by_turn(
                session_id, resolved
            )
        except Exception:
            checkpoint = None
        if checkpoint is not None and checkpoint.status_enum is TurnUndoStatus.REVERTED:
            return "ℹ 该回合此前已撤销过，本次未修改任何文件（聊天历史保留）。"
        return f"🚨 无法撤销：{error.message}"
    except RuntimeError as error:
        return f"🚨 无法撤销：{error}"

    if preview.conflicts:
        lines = ["🚨 预检发现文件冲突，本次没有修改任何文件："]
        for conflict in preview.conflicts:
            reasons = "；".join(conflict.reasons) or "状态不一致"
            lines.append(f"- {conflict.display_path}: {reasons}")
        lines.append("提示：磁盘文件与回合结束时状态不一致，已拒绝整轮撤销。")
        return "\n".join(lines)

    if preview.status is not TurnUndoStatus.READY:
        return f"🚨 无法撤销：回合当前状态为 {preview.status.value}，本次没有修改任何文件。"

    try:
        result = tu_service.execute_turn_undo(session_id, resolved, token)
    except TurnUndoError as error:
        if error.code in ("revert_interrupted", "recovery_required"):
            return (
                f"🚨 撤销事务中断：{error.message}\n"
                "文件可能处于部分恢复状态，受影响路径已冻结；"
                "请通过 GUI 恢复选项或 /undo recover 显式继续或取消。"
            )
        return f"🚨 撤销失败：{error.message}"

    if result.idempotent:
        return "ℹ 该回合此前已撤销过，本次未修改任何文件（聊天历史保留）。"

    try:
        _tool_cache.invalidate_after_mutation()
    except Exception:
        pass

    total = (len(result.restored_files) + len(result.deleted_created_files)
             + len(result.restored_deleted_files))
    lines = [
        f"✔ 已撤销回合 {result.turn_id[:8]} 的 {total} 个文件修改"
        f"（恢复原内容 {len(result.restored_files)}，"
        f"删除本轮新建 {len(result.deleted_created_files)}，"
        f"恢复本轮删除 {len(result.restored_deleted_files)}）。",
        "聊天历史保留；终端命令与外部副作用未随文件恢复。",
        "后续继续相关工作前必须重新读取磁盘文件。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 秘书调度台工具（设计 2026-10-15 第 3/4/5 节）
# 三个工具只对秘书会话可见（core/secretary 的工具策略），真实能力由 GUI 侧实现
# 并经 core.secretary_dispatch 注册；core 层不 import gui_bridge。CLI / 脚本等
# 未注册场景返回可读中文错误，绝不抛异常。
# ---------------------------------------------------------------------------

_SECRETARY_DISPATCH_UNAVAILABLE = (
    "🚨 秘书调度能力当前不可用：会话检索 / 派活 / 跳转由界面进程提供，"
    "需要 GUI 运行中（CLI / 脚本场景下不可用）。"
)


def _secretary_dispatch_service():
    try:
        from core import secretary_dispatch as _dispatch
    except Exception as exc:
        return None, f"🚨 秘书调度能力加载失败：{exc}"
    try:
        service = _dispatch.get()
    except Exception as exc:
        return None, f"🚨 秘书调度能力加载失败：{exc}"
    if service is None:
        return None, _SECRETARY_DISPATCH_UNAVAILABLE
    return service, ""


def _secretary_scope_limit(value, default=20, maximum=50):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return min(parsed, maximum)


def _secretary_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def search_conversations(
    keyword="", date_from="", date_to="", workdir="", limit=20, cancel_event=None
):
    service, error = _secretary_dispatch_service()
    if service is None:
        return error
    _raise_if_cancelled(cancel_event)
    try:
        results = service.search_conversations(
            keyword=str(keyword or ""),
            date_from=str(date_from or ""),
            date_to=str(date_to or ""),
            workdir=str(workdir or ""),
            limit=_secretary_scope_limit(limit),
        )
    except SecretaryDispatchError as exc:
        return f"🚨 检索会话失败：{exc}"
    except Exception as exc:
        return f"🚨 检索会话失败：{exc}"
    _raise_if_cancelled(cancel_event)
    rows = [row for row in (results or []) if isinstance(row, dict)]
    payload = {"count": len(rows), "results": rows}
    if not rows:
        payload["hint"] = "没找到匹配的会话，可换关键词或放宽日期范围。"
    return _secretary_json(payload)


_SECRETARY_NEW_OPTION = "new"
_SECRETARY_TARGET_INSTRUCTIONS = (
    "判断这条任务应该派发到哪个已有会话执行；如果没有明显相关的会话，"
    "选 new 表示新建会话。只依据任务描述与会话名判断。"
)
_SECRETARY_DECISION_TIMEOUT_MS = 1500
# 候选上限：只把最近的 10 个会话交给模型，避免题目与上下文膨胀
_SECRETARY_CANDIDATE_LIMIT = 10
_SECRETARY_STATE_PROMPT_CHARS = 300
# 秘书检索协议要求「关键词或日期至少给一个」（否则会抛错拒绝全表倾倒）。这里给
# 一个早于任何数据的日期下界，语义等价于「按最近更新时间取候选会话」；刻意不拿
# 任务正文当 LIKE 关键词——任务正文通常是整句，几乎必然零命中，决策永远发不出去。
_SECRETARY_CANDIDATE_DATE_FLOOR = "1970-01-01"


def _secretary_decision_armed():
    """secretary 场景当前是否需要发起决策（关 / 未启用 → False）。

    这条前置判断的意义：JEV 全关时必须在**调用调度服务之前**就退出，保证
    ``dispatch_task`` 对调度服务的调用次数与接入前完全一致（不白跑一次会话检索）。
    读取配置失败一律视为未启用。
    """
    try:
        from core import decision_service as _decision

        config = _decision.load_decision_config()
    except Exception as error:
        _logger.debug("secretary 场景读取决策配置失败，按未启用处理: %s", error)
        return False
    if not isinstance(config, dict) or not bool(config.get("enabled")):
        return False
    scenes = config.get("scenes")
    if not isinstance(scenes, dict):
        return False
    scene_config = scenes.get("secretary")
    if not isinstance(scene_config, dict):
        return False
    return bool(scene_config.get("enabled"))


def _secretary_candidate_rows(service, workdir):
    try:
        rows = service.search_conversations(
            keyword="",
            date_from=_SECRETARY_CANDIDATE_DATE_FLOOR,
            date_to="",
            workdir=str(workdir or ""),
            limit=_SECRETARY_CANDIDATE_LIMIT,
        )
    except Exception as error:
        _logger.debug("secretary 场景取候选会话失败，跳过决策: %s", error)
        return []
    if not isinstance(rows, (list, tuple)):
        return []
    candidates = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("session_name") or "").strip()
        if not name:
            continue
        candidates.append(
            {
                "session_name": name,
                "display_name": str(row.get("display_name") or "").strip() or name,
                "updated_at": str(row.get("updated_at") or ""),
            }
        )
    candidates.sort(key=lambda item: item["updated_at"], reverse=True)
    return candidates[:_SECRETARY_CANDIDATE_LIMIT]


def _secretary_decision_state(prompt, workdir, candidates):
    """构造决策 state：任务描述前 300 字符 + 候选会话名 + 工作目录。

    隐私约束：这段文本会真实发送给决策模型，只允许出现任务描述截断、会话名
    （session_name / display_name）与 workdir；**绝不拼接会话历史、检索片段、
    文件内容或任何密钥**。
    """
    head = str(prompt or "").strip()[:_SECRETARY_STATE_PROMPT_CHARS]
    lines = [f"任务描述：{head}", "候选会话："]
    for candidate in candidates:
        name = candidate["session_name"]
        display = candidate["display_name"]
        if display and display != name:
            lines.append(f"- {name}（{display}）")
        else:
            lines.append(f"- {name}")
    lines.append(f"工作目录：{str(workdir or '').strip() or 'GLOBAL'}")
    return "\n".join(lines)


def _decide_secretary_target(service, prompt, workdir):
    try:
        if not _secretary_decision_armed():
            return None
        candidates = _secretary_candidate_rows(service, workdir)
        if not candidates:
            return None
        # 惰性导入：避免 core.tools 与 core.decision_gateway 循环依赖
        from core.decision_gateway import decide

        names = [candidate["session_name"] for candidate in candidates]
        options = [_SECRETARY_NEW_OPTION] + names
        criteria = {_SECRETARY_NEW_OPTION: "新建一个会话来处理这条任务"}
        for candidate in candidates:
            session_name = candidate["session_name"]
            display = str(candidate.get("display_name") or "").strip()
            criteria[session_name] = f"派给已有会话：{display or session_name}"
        question = {
            "type": "choice",
            "instructions": _SECRETARY_TARGET_INSTRUCTIONS,
            "criteria": criteria,
        }
        state = _secretary_decision_state(prompt, workdir, candidates)
        picked = decide(
            "secretary",
            state,
            question,
            options=options,
            timeout_ms=_SECRETARY_DECISION_TIMEOUT_MS,
        )
    except Exception as error:
        # gateway 已兜底；这里再兜一层，决策永远不能成为派活的故障点
        _logger.debug("secretary 场景决策失败，沿用原派发规则: %s", error)
        return None
    if not isinstance(picked, str):
        return None
    picked = picked.strip()
    if picked not in names:
        return None
    return picked


def dispatch_task(
    target="", prompt="", workdir="", model_id="", permission_mode="", cancel_event=None
):
    service, error = _secretary_dispatch_service()
    if service is None:
        return error
    text = str(prompt or "").strip()
    if not text:
        return "🚨 派活失败：prompt（任务正文）不能为空。"
    _raise_if_cancelled(cancel_event)
    target_value = str(target or "")
    decision_note = ""
    if not target_value.strip():
        decided_target = _decide_secretary_target(service, text, workdir)
        if decided_target:
            target_value = decided_target
            decision_note = f"已按决策模型建议派发到 {decided_target}"
            _logger.info("secretary 派活采纳决策建议: %s", decided_target)
    try:
        result = service.dispatch_task(
            target=target_value,
            prompt=text,
            workdir=str(workdir or ""),
            model_id=str(model_id or ""),
            permission_mode=str(permission_mode or ""),
        )
    except SecretaryDispatchError as exc:
        return f"🚨 派活失败：{exc}"
    except Exception as exc:
        return f"🚨 派活失败：{exc}"
    _raise_if_cancelled(cancel_event)
    if not isinstance(result, dict):
        return f"🚨 派活失败：调度服务返回了无法识别的结果：{result!r}"
    payload = dict(result)
    payload.setdefault("ok", True)
    if decision_note:
        payload["decision_note"] = decision_note
    if payload.get("ok") is False and payload.get("candidates"):
        payload["hint"] = (
            "目标不唯一：请把候选里的 session_name 交给用户确认后，用确定的"
            "target 重新派活。"
        )
    return _secretary_json(payload)


def dispatch_batch(tasks=None, cancel_event=None):
    service, error = _secretary_dispatch_service()
    if service is None:
        return error
    if isinstance(tasks, (str, bytes)) or not isinstance(tasks, (list, tuple)):
        return "🚨 批量派活失败：tasks 需要是任务数组（每项含 prompt 等字段）。"
    if not tasks:
        return "🚨 批量派活失败：tasks 至少需要一条任务。"
    _raise_if_cancelled(cancel_event)
    try:
        result = service.dispatch_batch(tasks=[dict(t) if isinstance(t, dict) else t for t in tasks])
    except SecretaryDispatchError as exc:
        return f"🚨 批量派活失败：{exc}"
    except Exception as exc:
        return f"🚨 批量派活失败：{exc}"
    _raise_if_cancelled(cancel_event)
    if not isinstance(result, dict):
        return f"🚨 批量派活失败：调度服务返回了无法识别的结果：{result!r}"
    payload = dict(result)
    payload.setdefault("ok", True)
    return _secretary_json(payload)


def open_session(target="", cancel_event=None):
    service, error = _secretary_dispatch_service()
    if service is None:
        return error
    wanted = str(target or "").strip()
    if not wanted:
        return "🚨 跳转失败：target（会话名或显示名）不能为空。"
    _raise_if_cancelled(cancel_event)
    try:
        result = service.open_session(target=wanted)
    except SecretaryDispatchError as exc:
        return f"🚨 跳转失败：{exc}"
    except Exception as exc:
        return f"🚨 跳转失败：{exc}"
    _raise_if_cancelled(cancel_event)
    if not isinstance(result, dict):
        return f"🚨 跳转失败：调度服务返回了无法识别的结果：{result!r}"
    payload = dict(result)
    payload.setdefault("ok", True)
    return _secretary_json(payload)


def create_project(path="", cancel_event=None):
    service, error = _secretary_dispatch_service()
    if service is None:
        return error
    wanted = str(path or "").strip()
    if not wanted:
        return "🚨 新建项目失败：path（完整路径）不能为空。"
    _raise_if_cancelled(cancel_event)
    try:
        result = service.create_project(path=wanted)
    except SecretaryDispatchError as exc:
        return f"🚨 新建项目失败：{exc}"
    except Exception as exc:
        return f"🚨 新建项目失败：{exc}"
    _raise_if_cancelled(cancel_event)
    if not isinstance(result, dict):
        return f"🚨 新建项目失败：调度服务返回了无法识别的结果：{result!r}"
    payload = dict(result)
    payload.setdefault("ok", True)
    return _secretary_json(payload)


_SECRETARY_LIST_EXCERPT_CHARS = 200

_SECRETARY_DISPATCH_DETAIL_FIELDS = (
    "id",
    "title",
    "target_session",
    "target_display",
    "workdir",
    "status",
    "started_at",
    "finished_at",
    "prompt",
    "result_excerpt",
    "error",
    "depends_on",
    "batch_id",
    "secretary_session",
)


def _secretary_dispatch_brief(row) -> dict:
    return {
        "id": row.get("id"),
        "title": str(row.get("title") or ""),
        "target_display": str(row.get("target_display") or ""),
        "workdir": str(row.get("workdir") or ""),
        "status": str(row.get("status") or ""),
        "started_at": str(row.get("started_at") or ""),
        "finished_at": str(row.get("finished_at") or ""),
        "result_excerpt": str(row.get("result_excerpt") or "")[
            :_SECRETARY_LIST_EXCERPT_CHARS
        ],
    }


def _secretary_dispatch_detail(row) -> dict:
    payload = {"ok": True}
    for field in _SECRETARY_DISPATCH_DETAIL_FIELDS:
        value = row.get(field)
        if field == "depends_on":
            dependency_ids = []
            for item in value or []:
                try:
                    dependency_ids.append(int(item))
                except (TypeError, ValueError):
                    continue
            payload[field] = dependency_ids
            continue
        if isinstance(value, str) or value is None or isinstance(value, int):
            payload[field] = value
        else:
            payload[field] = str(value)
    return payload


def list_dispatches(status="", workdir="", target="", limit=20, cancel_event=None):
    service, error = _secretary_dispatch_service()
    if service is None:
        return error
    _raise_if_cancelled(cancel_event)
    try:
        rows = service.list_dispatches(
            status=str(status or ""),
            workdir=str(workdir or ""),
            target=str(target or ""),
            limit=_secretary_scope_limit(limit),
        )
    except SecretaryDispatchError as exc:
        return f"🚨 查询派活记录失败：{exc}"
    except Exception as exc:
        return f"🚨 查询派活记录失败：{exc}"
    _raise_if_cancelled(cancel_event)
    items = [_secretary_dispatch_brief(row) for row in (rows or []) if isinstance(row, dict)]
    if not items:
        return "没有符合条件的派活记录"
    return _secretary_json({"count": len(items), "dispatches": items})


def get_dispatch_result(dispatch_id=None, cancel_event=None):
    service, error = _secretary_dispatch_service()
    if service is None:
        return error
    raw = "" if dispatch_id is None else str(dispatch_id).strip()
    if not raw:
        return "🚨 查询任务详情失败：dispatch_id（任务编号）不能为空。"
    try:
        parsed_id = int(raw)
    except (TypeError, ValueError):
        return f"🚨 查询任务详情失败：dispatch_id 需要是整数，收到「{raw}」。"
    _raise_if_cancelled(cancel_event)
    try:
        row = service.get_dispatch_result(dispatch_id=parsed_id)
    except SecretaryDispatchError as exc:
        return f"🚨 查询任务详情失败：{exc}"
    except Exception as exc:
        return f"🚨 查询任务详情失败：{exc}"
    _raise_if_cancelled(cancel_event)
    if not isinstance(row, dict):
        return f"没有找到任务 #{parsed_id}"
    payload = _secretary_dispatch_detail(row)
    payload["id"] = parsed_id
    return _secretary_json(payload)


#: remember_decision 单条内容上限（与 core.project_profile 同源，避免两处漂移）
_REMEMBER_DECISION_MAX_CHARS = project_profile.MAX_SECTION_CONTENT_CHARS

_PROFILE_RESERVED_WORKDIRS = frozenset({"GLOBAL", "SECRETARY"})


def _current_session_workdir():
    """当前会话的工作目录文本（取不到返回空串；绝不抛）。"""
    try:
        from core.workspace_context import get_workdir

        return str(get_workdir() or "")
    except Exception:
        return ""


def read_project_profile(workdir="", cancel_event=None):
    target = str(workdir or "").strip() or _current_session_workdir()
    _raise_if_cancelled(cancel_event)
    try:
        text = project_profile.read_profile(target)
    except Exception as exc:
        return f"🚨 读取项目画像失败：{exc}"
    if not str(text or "").strip():
        where = target or "未指定工作目录"
        return (
            f"该项目还没有画像（{where}）。"
            "可以用 remember_decision 先把技术栈 / 架构决策 / 硬约束记下来。"
        )
    return text


def remember_decision(workdir="", section="", content="", cancel_event=None):
    name = str(section or "").strip()
    if name not in project_profile.SECTION_ALLOWED:
        return (
            "🚨 记录失败：section 只能是 %s，收到「%s」。"
            % ("、".join(project_profile.SECTION_ALLOWED), name or "(空)")
        )
    body = str(content or "").strip()
    if not body:
        return "🚨 记录失败：content（要记住的内容）不能为空。"
    if len(body) > _REMEMBER_DECISION_MAX_CHARS:
        return (
            f"🚨 记录失败：content 太长（{len(body)} 字），"
            f"上限 {_REMEMBER_DECISION_MAX_CHARS} 字，请精简后再记。"
        )
    _raise_if_cancelled(cancel_event)
    target = str(workdir or "").strip() or _current_session_workdir()
    if not str(target or "").strip() or str(target).strip().upper() in _PROFILE_RESERVED_WORKDIRS:
        return (
            "🚨 记录失败：无法确定项目目录（当前会话没有归属项目）。"
            "请在 workdir 里给出该项目的完整路径。"
        )
    try:
        message = project_profile.append_section(target, name, body)
    except ValueError as exc:
        return f"🚨 记录失败：{exc}"
    except Exception as exc:
        return f"🚨 记录失败：{exc}"
    if not message:
        return f"🚨 记录失败：项目画像没有写成（{target}），请稍后重试。"
    return message


# ---------------------------------------------------------------------------
# 秘书只读分析与个人记忆工具（list_projects / list_sessions / read_session / manage_memory）
# ---------------------------------------------------------------------------

_SECRETARY_READ_SESSION_MAX_MSG_CHARS = 1500
_SECRETARY_READ_SESSION_BUDGET_CHARS = 14000
_SECRETARY_MEMORY_TYPES = ("user", "feedback", "project", "reference")


def _secretary_redact_text(text: str) -> str:
    """对摘要与会话正文做常见明文密钥/Token 脱敏。"""
    import re

    value = str(text or "")
    if not value:
        return ""
    value = re.sub(r"sk-[A-Za-z0-9_-]{16,}", "[已脱敏密钥]", value)
    value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._-]{20,}", r"\1[已脱敏Token]", value)
    return value


def _secretary_normalize_workdir(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.upper() == "GLOBAL":
        return "GLOBAL"
    try:
        from core.memory import _normalize_workdir

        return _normalize_workdir(text)
    except Exception:
        return os.path.normpath(text)


def _secretary_display_name(manager, session_name: str, source: str) -> str:
    import re

    if str(source or "").strip().upper() == "SECRETARY":
        try:
            getter = getattr(manager, "get_session_secretary_name", None)
            if callable(getter):
                name = str(getter(session_name) or "").strip()
                if name:
                    return name
        except Exception:
            pass
        return "秘书"
    if re.fullmatch(r"session_[0-9a-f]{6}", str(session_name or "")):
        return "新会话"
    return str(session_name or "")


def list_projects(cancel_event=None):
    """列出全部已登记项目工作区与全局会话统计（秘书专用，只读）。"""
    _raise_if_cancelled(cancel_event)
    try:
        from core import app_context

        manager = app_context.get_manager()
    except Exception as exc:
        return f"🚨 查询项目列表失败：{exc}"
    if manager is None:
        return "🚨 查询项目列表失败：会话存储尚未初始化。"

    try:
        workspaces = list(manager.get_all_workspaces() or [])
        sessions = list(manager.get_all_sessions(include_empty=True, include_archived=True) or [])
    except Exception as exc:
        return f"🚨 查询项目列表失败：{exc}"

    projects_map = {}

    def _ensure_entry(norm_wd: str):
        entry = projects_map.get(norm_wd)
        if entry is None:
            base = os.path.basename(norm_wd.rstrip("\\/")) or norm_wd
            entry = {
                "workdir": norm_wd,
                "display_name": base,
                "session_count": 0,
                "archived_count": 0,
                "last_updated_at": "",
                "has_profile": False,
                "profile_updated": "",
            }
            projects_map[norm_wd] = entry
        return entry

    for raw_ws in workspaces:
        norm = _secretary_normalize_workdir(str(raw_ws or ""))
        if not norm or norm == "GLOBAL":
            continue
        _ensure_entry(norm)

    global_count = 0
    secretary_count = 0
    for item in sessions:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "GUI").strip().upper()
        archived = bool(item.get("archived_at"))
        msg_count = int(item.get("count") or 0)
        if source == "SECRETARY":
            if not archived:
                secretary_count += 1
            continue
        raw_wd = str(item.get("workdir") or "").strip()
        norm = _secretary_normalize_workdir(raw_wd)
        if not norm or norm == "GLOBAL":
            if not archived and msg_count > 0:
                global_count += 1
            continue
        entry = _ensure_entry(norm)
        if archived:
            if msg_count > 0:
                entry["archived_count"] += 1
        else:
            if msg_count > 0:
                entry["session_count"] += 1
            updated = str(item.get("updated_at") or "")
            if updated > entry["last_updated_at"]:
                entry["last_updated_at"] = updated

    for norm_wd, entry in projects_map.items():
        _raise_if_cancelled(cancel_event)
        try:
            state = project_profile.read_state(norm_wd)
            if isinstance(state, dict):
                entry["has_profile"] = bool(state.get("has_profile"))
                entry["profile_updated"] = str(state.get("updated") or "")
        except Exception:
            pass

    projects = sorted(
        projects_map.values(),
        key=lambda e: (str(e.get("last_updated_at") or ""), str(e.get("display_name") or "")),
        reverse=True,
    )
    return _secretary_json({
        "project_count": len(projects),
        "projects": projects,
        "global_session_count": global_count,
        "secretary_session_count": secretary_count,
    })


def list_sessions(
    workdir="",
    keyword="",
    include_archived=False,
    include_secretary=False,
    limit=20,
    cancel_event=None,
):
    """按项目或关键词浏览历史会话列表（秘书专用，不强制要求关键词）。"""
    _raise_if_cancelled(cancel_event)
    try:
        from core import app_context

        manager = app_context.get_manager()
    except Exception as exc:
        return f"🚨 获取会话列表失败：{exc}"
    if manager is None:
        return "🚨 获取会话列表失败：会话存储尚未初始化。"

    norm_filter = _secretary_normalize_workdir(str(workdir or ""))
    kw = str(keyword or "").strip().lower()
    size = _secretary_scope_limit(limit, default=20, maximum=50)
    try:
        raw_sessions = list(
            manager.get_all_sessions(
                workdir=None,
                include_empty=False,
                include_archived=bool(include_archived),
            )
            or []
        )
    except Exception as exc:
        return f"🚨 获取会话列表失败：{exc}"

    candidates = []
    for item in raw_sessions:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "GUI").strip().upper()
        if not include_secretary and source == "SECRETARY":
            continue
        raw_wd = str(item.get("workdir") or "").strip()
        item_norm_wd = _secretary_normalize_workdir(raw_wd)
        if norm_filter:
            if norm_filter == "GLOBAL":
                if item_norm_wd and item_norm_wd != "GLOBAL":
                    continue
            elif item_norm_wd != norm_filter:
                continue
        name = str(item.get("name") or "")
        display_name = _secretary_display_name(manager, name, source)
        search_text = str(item.get("search_text") or "")
        updated_at = str(item.get("updated_at") or "")
        candidates.append((item, source, name, display_name, search_text, item_norm_wd or "GLOBAL", updated_at))

    candidates.sort(key=lambda row: row[6], reverse=True)

    results = []
    summary_getter = getattr(manager, "get_session_memory_summary", None)
    for item, source, name, display_name, search_text, item_wd, updated_at in candidates:
        _raise_if_cancelled(cancel_event)
        if kw:
            haystack = "\n".join((name, display_name, search_text)).lower()
            matched = kw in haystack
            summary_text = ""
            if callable(summary_getter):
                try:
                    summary_text = str(summary_getter(name) or "")
                except Exception:
                    summary_text = ""
            if not matched and kw not in summary_text.lower():
                continue
        else:
            summary_text = ""
            if callable(summary_getter):
                try:
                    summary_text = str(summary_getter(name) or "")
                except Exception:
                    summary_text = ""

        redacted_summary = _secretary_redact_text(summary_text.strip())
        if redacted_summary:
            preview = redacted_summary[:200]
        else:
            preview = _secretary_redact_text(search_text.strip())[:160]

        results.append({
            "session_name": name,
            "display_name": display_name,
            "source": source,
            "is_secretary": source == "SECRETARY",
            "workdir": item_wd,
            "updated_at": updated_at,
            "message_count": int(item.get("count") or 0),
            "archived": bool(item.get("archived_at")),
            "has_summary": bool(summary_text.strip()),
            "summary_preview": preview,
        })
        if len(results) >= size:
            break

    payload = {"count": len(results), "results": results}
    if not results:
        payload["hint"] = "未找到符合条件的会话，可放宽 workdir / keyword 或开启 include_archived。"
    return _secretary_json(payload)


def _secretary_extract_msg_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type")
                if ptype == "text":
                    txt = str(part.get("text") or "")
                    if txt:
                        parts.append(txt)
                elif ptype in ("image_url", "image"):
                    parts.append("[图片]")
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def read_session(
    target="",
    limit=30,
    offset=0,
    from_end=True,
    include_summary=True,
    include_tool_calls=True,
    cancel_event=None,
):
    """读取指定会话的压缩记忆摘要与时间线消息（秘书专用，只读分析）。"""
    _raise_if_cancelled(cancel_event)
    target_text = str(target or "").strip()
    if not target_text:
        return "🚨 读取会话失败：target（会话名或显示名）不能为空。"

    try:
        from core import app_context

        manager = app_context.get_manager()
    except Exception as exc:
        return f"🚨 读取会话失败：{exc}"
    if manager is None:
        return "🚨 读取会话失败：会话存储尚未初始化。"

    try:
        all_sessions = list(
            manager.get_all_sessions(
                workdir=None,
                include_empty=True,
                include_archived=True,
            )
            or []
        )
    except Exception as exc:
        return f"🚨 读取会话失败：{exc}"

    def _meta_entry(item):
        name = str(item.get("name") or "")
        source = str(item.get("source") or "GUI").strip().upper()
        display = _secretary_display_name(manager, name, source)
        return {
            "session_name": name,
            "display_name": display,
            "source": source,
            "is_secretary": source == "SECRETARY",
            "workdir": str(item.get("workdir") or "GLOBAL"),
            "updated_at": str(item.get("updated_at") or ""),
            "archived": bool(item.get("archived_at")),
            "search_text": str(item.get("search_text") or ""),
        }

    candidates_meta = [_meta_entry(item) for item in all_sessions if isinstance(item, dict)]

    matched = [m for m in candidates_meta if m["session_name"] == target_text]
    if not matched:
        matched = [m for m in candidates_meta if m["display_name"] == target_text]
    if not matched:
        low = target_text.lower()
        matched = [m for m in candidates_meta if m["display_name"].lower() == low]
    if not matched:
        low = target_text.lower()
        matched = [
            m for m in candidates_meta
            if low in m["session_name"].lower()
            or low in m["display_name"].lower()
            or low in m["search_text"].lower()
        ]

    if not matched:
        return f"没有找到匹配「{target_text}」的会话。请先用 list_sessions 或 search_conversations 确认会话名。"
    if len(matched) > 1:
        matched.sort(key=lambda m: str(m.get("updated_at") or ""), reverse=True)
        return _secretary_json({
            "ok": False,
            "error": f"匹配到多个会话（{len(matched)} 条），请改用更精确的 session_name。",
            "candidates": [
                {
                    "session_name": m["session_name"],
                    "display_name": m["display_name"],
                    "workdir": m["workdir"],
                    "updated_at": m["updated_at"],
                }
                for m in matched[:10]
            ],
        })

    resolved = matched[0]
    resolved_name = resolved["session_name"]
    summary_text = ""
    summary_generations = []
    if include_summary:
        try:
            summary_getter = getattr(manager, "get_session_memory_summary", None)
            if callable(summary_getter):
                summary_text = _secretary_redact_text(str(summary_getter(resolved_name) or "").strip())[:4000]
        except Exception:
            summary_text = ""
        try:
            gen_getter = getattr(manager, "get_summary_generation_index", None)
            if callable(gen_getter):
                raw_gens = gen_getter(resolved_name, limit=10)
                if isinstance(raw_gens, tuple) and len(raw_gens) == 2:
                    summary_generations = list(raw_gens[0] or [])
                elif raw_gens:
                    summary_generations = list(raw_gens)
        except Exception:
            summary_generations = []

    try:
        raw_history = list(manager.get_history(resolved_name) or [])
    except Exception as exc:
        return f"🚨 读取会话历史失败：{exc}"

    try:
        from core.injection_markers import is_system_injected_user_text
    except Exception:
        def is_system_injected_user_text(_text):
            return False
    try:
        from core.agent_service import strip_context_suffix
    except Exception:
        def strip_context_suffix(text):
            return str(text or "")

    entries = []
    for raw_msg in raw_history:
        _raise_if_cancelled(cancel_event)
        if not isinstance(raw_msg, dict):
            continue
        role = str(raw_msg.get("role") or "").strip().lower()
        if role == "system":
            continue
        raw_text = _secretary_extract_msg_text(raw_msg)
        if role == "user":
            if is_system_injected_user_text(raw_text):
                if raw_text.lstrip().startswith("<task-notification>"):
                    condensed = _secretary_redact_text(raw_text.strip())[:240]
                    entries.append({"role": "system_notice", "content": condensed})
                continue
            cleaned = strip_context_suffix(raw_text).strip()
            if not cleaned:
                continue
            entries.append({
                "role": "user",
                "content": _secretary_redact_text(cleaned)[:_SECRETARY_READ_SESSION_MAX_MSG_CHARS],
            })
        elif role == "assistant":
            cleaned = raw_text.strip()
            entry = {"role": "assistant", "content": _secretary_redact_text(cleaned)[:_SECRETARY_READ_SESSION_MAX_MSG_CHARS]}
            tool_calls = raw_msg.get("tool_calls") or []
            if include_tool_calls and isinstance(tool_calls, list) and tool_calls:
                names = []
                for tc in tool_calls:
                    fn = (tc or {}).get("function") or {}
                    tname = str(fn.get("name") or "").strip()
                    if tname:
                        names.append(tname)
                if names:
                    entry["tool_calls"] = names
            if entry["content"] or entry.get("tool_calls"):
                entries.append(entry)
        elif role == "tool":
            if not include_tool_calls:
                continue
            tname = str(raw_msg.get("name") or "tool")
            cleaned = _secretary_redact_text(raw_text.strip())[:500]
            if cleaned:
                entries.append({"role": "tool", "name": tname, "content": cleaned})

    total = len(entries)
    lim = _secretary_scope_limit(limit, default=30, maximum=80)
    try:
        off = max(0, int(offset or 0))
    except (TypeError, ValueError):
        off = 0

    if from_end:
        end = max(0, total - off)
        start = max(0, end - lim)
    else:
        start = min(total, off)
        end = min(total, start + lim)

    selected = entries[start:end]
    # 怕一条内容太长把这次读取的字数预算撑爆：从最旧的开始砍，留下最新的
    budget = _SECRETARY_READ_SESSION_BUDGET_CHARS
    trimmed = []
    used = 0
    for item in reversed(selected):
        chunk = len(json.dumps(item, ensure_ascii=False))
        if trimmed and used + chunk > budget:
            break
        trimmed.append(item)
        used += chunk
    trimmed.reverse()
    actual_start = end - len(trimmed)

    return _secretary_json({
        "ok": True,
        "session_name": resolved_name,
        "display_name": resolved["display_name"],
        "is_secretary": resolved["is_secretary"],
        "workdir": resolved["workdir"],
        "updated_at": resolved["updated_at"],
        "archived": resolved["archived"],
        "memory_summary": summary_text,
        "summary_generations": summary_generations,
        "total_messages": total,
        "returned_range": [actual_start, end],
        "has_more_before": actual_start > 0,
        "has_more_after": end < total,
        "messages": trimmed,
    })


def manage_memory(
    action="list",
    memory_type="user",
    title="",
    description="",
    content="",
    filename="",
    cancel_event=None,
):
    """管理跨项目个人持久记忆（~/.ycode/memory/，秘书专用）。

    只读/写 ~/.ycode/memory/ 下的 user / feedback / project / reference 四类
    Markdown 文件与 MEMORY.md 索引；严禁写工作区代码或 ~/.ycode 顶层配置/凭据。
    """
    import hashlib
    import re

    _raise_if_cancelled(cancel_event)
    try:
        from core import memory_kairos as mk
    except Exception as exc:
        return f"🚨 记忆模块加载失败：{exc}"

    act = str(action or "list").strip().lower()
    mtype = str(memory_type or "user").strip().lower()
    raw_fname = str(filename or "").strip()

    if act == "list":
        index_lines = []
        files = []
        try:
            with mk._memory_io_lock:
                mk.ensure_memory_dir()
                idx_path = mk.MEMORY_DIR / "MEMORY.md"
                if idx_path.is_file():
                    for line in idx_path.read_text(encoding="utf-8", errors="replace").splitlines():
                        stripped = line.strip()
                        if stripped:
                            index_lines.append(stripped)
                for subdir in _SECRETARY_MEMORY_TYPES:
                    dir_path = mk.MEMORY_DIR / subdir
                    if not dir_path.is_dir():
                        continue
                    for p in sorted(dir_path.glob("*.md")):
                        if not p.is_file():
                            continue
                        try:
                            text = p.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            text = ""
                        files.append({
                            "path": f"{subdir}/{p.name}",
                            "type": subdir,
                            "filename": p.name,
                            "size_bytes": len(text.encode("utf-8", errors="replace")),
                            "preview": text[:200],
                        })
        except Exception as exc:
            return f"🚨 读取记忆列表失败：{exc}"
        return _secretary_json({
            "ok": True,
            "action": "list",
            "memory_dir": str(mk.MEMORY_DIR),
            "index_lines": index_lines[: mk.MAX_MEMORY_INDEX_LINES],
            "files": files,
        })

    if act == "save":
        if mtype not in _SECRETARY_MEMORY_TYPES:
            return (
                "🚨 保存记忆失败：memory_type 只能是 "
                + "、".join(_SECRETARY_MEMORY_TYPES)
                + f"，收到「{mtype or '(空)'}」。"
            )
        ttl = str(title or "").strip()
        body = str(content or "").strip()
        desc = str(description or "").strip()
        if not ttl:
            return "🚨 保存记忆失败：title（记忆标题）不能为空。"
        if len(ttl) > 120:
            return f"🚨 保存记忆失败：title 过长（{len(ttl)} 字），上限 120 字。"
        if not body:
            return "🚨 保存记忆失败：content（记忆正文）不能为空。"
        if len(body) > 4000:
            return f"🚨 保存记忆失败：content 过长（{len(body)} 字），上限 4000 字。"
        if not desc:
            first_line = next((ln.strip() for ln in body.splitlines() if ln.strip()), ttl)
            desc = first_line[:120]
        if len(desc) > 220:
            desc = desc[:220]
        # 去掉换行，否则 MEMORY.md 一行一条的格式就乱了
        ttl_one = re.sub(r"[\r\n\[\]]+", " ", ttl).strip() or "memory"
        desc_one = re.sub(r"[\r\n]+", " ", desc).strip() or ttl_one

        if (
            mk._contains_memory_directive(ttl_one)
            or mk._contains_memory_directive(desc_one)
            or mk._contains_memory_directive(body)
        ):
            return "🚨 保存记忆失败：内容触发记忆安全过滤（包含可疑覆盖/指令句式），已拒绝写入。"

        # 拼一个安全的文件名（必须符合 _MEMORY_TARGET_RE 里限定的英文字符）
        fname = raw_fname.replace("\\", "/").strip("/")
        if "/" in fname:
            prefix, tail = fname.split("/", 1)
            if prefix in _SECRETARY_MEMORY_TYPES and prefix == mtype and "/" not in tail:
                fname = tail
            else:
                return f"🚨 保存记忆失败：filename 只能是裸文件名（无需目录前缀），收到「{raw_fname}」。"
        if fname:
            if not fname.endswith(".md"):
                fname += ".md"
        else:
            ascii_slug = re.sub(r"[^A-Za-z0-9._-]+", "_", ttl_one).strip("._-")
            digest = hashlib.sha1(ttl_one.encode("utf-8")).hexdigest()[:8]
            if ascii_slug and re.match(r"^[A-Za-z0-9]", ascii_slug):
                fname = f"{ascii_slug[:40]}_{digest}.md"
            else:
                fname = f"{mtype}_{digest}.md"

        if not mk._is_safe_memory_filename(fname) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.md", fname
        ):
            return f"🚨 保存记忆失败：filename「{fname}」不合法，只允许字母/数字/._- 且以 .md 结尾。"

        type_dir = mk.MEMORY_DIR / mtype
        target_path = type_dir / fname
        rel_target = f"{mtype}/{fname}"
        index_line = f"- [{ttl_one}]({rel_target}) — {desc_one}"
        if not mk._is_safe_memory_index_entry(index_line):
            return "🚨 保存记忆失败：标题或摘要未能通过记忆索引安全校验。"

        file_text = (
            f"---\nname: {ttl_one}\ndescription: {desc_one}\ntype: {mtype}\n---\n\n{body}\n"
        )
        try:
            with mk._memory_io_lock:
                mk.ensure_memory_dir()
                type_dir.mkdir(parents=True, exist_ok=True)
                if not mk._is_within_dir(target_path, type_dir):
                    return f"🚨 保存记忆失败：路径越界（{target_path}）。"
                mk._atomic_write_text(target_path, file_text)

                idx_path = mk.MEMORY_DIR / "MEMORY.md"
                existing = []
                if idx_path.is_file():
                    existing = idx_path.read_text(encoding="utf-8", errors="replace").splitlines()
                needle = f"]({rel_target})"
                filtered = [ln for ln in existing if needle not in ln]
                filtered.append(index_line)
                if len(filtered) > mk.MAX_MEMORY_INDEX_LINES:
                    filtered = filtered[-mk.MAX_MEMORY_INDEX_LINES:]
                mk._atomic_write_text(idx_path, "\n".join(filtered).rstrip() + "\n")
        except Exception as exc:
            return f"🚨 保存记忆失败：{exc}"

        return _secretary_json({
            "ok": True,
            "action": "save",
            "path": rel_target,
            "title": ttl_one,
            "type": mtype,
        })

    if act == "delete":
        spec = raw_fname.replace("\\", "/").strip("/")
        if not spec:
            return "🚨 删除记忆失败：请提供要删除的 filename（如 user/foo.md 或配合 memory_type）。"
        if "/" in spec:
            parts = spec.split("/")
            if len(parts) != 2 or parts[0] not in _SECRETARY_MEMORY_TYPES:
                return f"🚨 删除记忆失败：路径格式无效「{raw_fname}」，应为 <type>/<name>.md。"
            del_type, del_name = parts
        else:
            if mtype not in _SECRETARY_MEMORY_TYPES:
                return f"🚨 删除记忆失败：无效的 memory_type「{mtype}」。"
            del_type, del_name = mtype, spec
        if not del_name.endswith(".md"):
            del_name += ".md"
        if not mk._is_safe_memory_filename(del_name):
            return f"🚨 删除记忆失败：非法文件名「{del_name}」。"
        type_dir = mk.MEMORY_DIR / del_type
        target_path = type_dir / del_name
        rel_target = f"{del_type}/{del_name}"

        existed = False
        removed_index = 0
        try:
            with mk._memory_io_lock:
                mk.ensure_memory_dir()
                if not mk._is_within_dir(target_path, type_dir):
                    return f"🚨 删除记忆失败：路径越界（{target_path}）。"
                if target_path.is_file():
                    target_path.unlink()
                    existed = True
                idx_path = mk.MEMORY_DIR / "MEMORY.md"
                if idx_path.is_file():
                    existing = idx_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    needle = f"]({rel_target})"
                    kept = [ln for ln in existing if needle not in ln]
                    removed_index = len(existing) - len(kept)
                    if removed_index:
                        content_out = ("\n".join(kept).rstrip() + "\n") if kept else ""
                        mk._atomic_write_text(idx_path, content_out)
        except Exception as exc:
            return f"🚨 删除记忆失败：{exc}"

        return _secretary_json({
            "ok": True,
            "action": "delete",
            "path": rel_target,
            "deleted_file": existed,
            "removed_index_entries": removed_index,
        })

    return "🚨 记忆操作失败：action 只能是 list、save 或 delete。"
