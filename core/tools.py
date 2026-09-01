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
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
from xml.sax.saxutils import escape

from ddgs import DDGS

from core.version import __version__ as YCODE_VERSION
from core import tool_cache as _tool_cache
from core.lsp import _get_rg_executable


HTTP_TIMEOUT_SECONDS = 20
HTTP_MAX_BYTES = 512 * 1024
HTTP_USER_AGENT = f"ycode/{YCODE_VERSION} (terminal coding assistant)"
# 伪装成 Chrome 浏览器的 User-Agent：很多新闻网站（NYT、AP News 等）有反爬虫机制，
# 看到非浏览器 UA（如 ycode/...）会直接返回 403 Forbidden。
# 伪装成主流浏览器 UA 可绕过这类基础反爬，让 fetch_url 能正常抓取网页内容。
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
READ_FILE_MAX_LINES = 2000
# 大文件阈值：超过此字节数的文件，未显式指定 limit 时自动只读前部分并提示分页
READ_FILE_LARGE_BYTES = 50 * 1024
READ_FILE_LARGE_DEFAULT_LINES = 500
# execute_command 前台执行的输出总量上限（stdout+stderr 字节）：
# yes 类命令在 0.1s 轮询下 600s 可累积数 GB 输出撑爆内存，超限即终止进程树
EXECUTE_COMMAND_MAX_OUTPUT_BYTES = 1024 * 1024

_logger = logging.getLogger(__name__)


class _ToolCancelledError(Exception):
    """工具在等待期间被用户取消（协作式取消，不暴露给模型调用方）。"""


def _raise_if_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise _ToolCancelledError()


# 敏感信息脱敏：后台任务命令回显/日志前屏蔽内联凭据，
# 避免 export TOKEN=sk-... 这类命令把密钥明文喂回模型或写入持久化记录。
# 实现集中在 command_policy，供 task_manager 等持久化方复用
# ---------------------------------------------------------------------------
from core.command_policy import redact_sensitive_text as _redact_sensitive_text


# ---------------------------------------------------------------------------
# 文件编辑并发防护（多子代理并发编辑同一文件时后写者不再静默覆盖）：
# 1. 按路径分桶的模块级锁，串行化同一文件的 读→改→写 全过程
# 2. mtime 乐观校验：读时记录 mtime，写入前比对，变了则报错让调用方重读
# ---------------------------------------------------------------------------
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
    """压缩源代码文本以减少 token 消耗，同时保留语义完整性。

    优化项：
    1. 去除每行行尾空白（trailing whitespace，包括空格和制表符）
    2. 连续 2+ 空行合并为 1 行（保留代码块之间的视觉分隔，但避免大段空白烧 token）
    3. 文件末尾多余空行裁剪

    不做的事（避免破坏语义）：
    - 不剥离行首缩进（Python 等语言依赖缩进）
    - 不剥离注释（注释对理解代码逻辑很重要）
    - 不剥离字符串字面量

    注意：压缩后内容与原始文件不完全一致（行尾空白、空行数有差异）。
    若后续要用 edit_file 修改该文件，须用 compress=False 重读以保证 target_content 匹配。
    """
    if not text:
        return text
    lines = text.splitlines()
    # 1. 去除行尾空白（保留行首缩进与内容）
    stripped = [line.rstrip() for line in lines]
    # 2. 连续 2+ 空行合并为 1 行
    result = []
    blank_run = 0
    for line in stripped:
        if line == "":
            blank_run += 1
            if blank_run <= 1:
                result.append(line)
            # 超过 1 个连续空行直接跳过，最终只保留 1 个
        else:
            blank_run = 0
            result.append(line)
    # 3. 末尾多余空行裁剪（保留 1 个换行符即可）
    while result and result[-1] == "":
        result.pop()
    return "\n".join(result) + ("\n" if result else "")


# 线程内临时放行（用户批准的单次沙盒外操作）与任务级放行
# 任务级放行按线程绑定的稳定任务 ID 分桶存储：任务 A 放行不影响任务 B；
# 未绑定任务的 headless 流程使用 __default__ 桶。
_sandbox_tls = threading.local()
_session_sandbox_bypass = False
_session_sandbox_bypass_by_session: dict[str, bool] = {}
_session_sandbox_lock = threading.Lock()

# ---------------------------------------------------------------------------
# 子代理嵌套深度限制（参考 OpenCode 的 limit subagent nesting depth）
# ---------------------------------------------------------------------------
# 防止子代理递归调用 run_subagent 形成无限嵌套导致：
# - 资源耗尽（每个子代理是一个独立线程 + 一轮 LLM 对话）
# - 计划项状态错乱（深度嵌套的 callback 链难追溯）
# - 用户难以追踪到底是谁在执行
# 默认最大深度 3：主代理 → 子代理 → 孙代理 → 终止
# 超限时返回明确错误，让上层 LLM 改用直接执行
_subagent_depth_tls = threading.local()
SUBAGENT_MAX_DEPTH = 3


def _get_current_subagent_depth():
    """获取当前线程的子代理嵌套深度（0 表示主代理线程）。"""
    return getattr(_subagent_depth_tls, "depth", 0)


@contextlib.contextmanager
def _subagent_depth_scope():
    """进入子代理执行上下文：深度 +1，退出时 -1。"""
    prev = getattr(_subagent_depth_tls, "depth", 0)
    _subagent_depth_tls.depth = prev + 1
    try:
        yield prev + 1
    finally:
        _subagent_depth_tls.depth = prev


def _resolve_against_workdir(file_path):
    """解析为绝对路径：相对路径按线程本地 workdir 解析，而非进程 cwd。

    与 execute_command 的 cwd=str(get_workdir()) 语义一致。GUI 多会话并行时
    进程 cwd 可能停在安装目录或上一会话，裸 Path.resolve() 会把相对路径
    （含 list_directory 等目录工具默认的 "."）解析到错误目录，沙盒守卫
    据此把项目根内的路径误判为"工作目录之外"。
    """
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
    """返回稳定任务 ID；执行线程绑定优先于当前可见任务。"""
    try:
        import core.app_context as _app_context

        sid = _app_context.get_thread_session() or _app_context.get_current_session()
        return str(sid) if sid else "__default__"
    except Exception:
        return "__default__"


def set_session_sandbox_bypass(enabled):
    """任务级沙盒放行（用户选择“本任务内放行沙盒外访问”时置位）。

    绑定了任务的回合写入对应任务桶；未绑定任务写入 __default__ 桶。
    """
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


def _outside_workspace_allowed():
    """读取运行时状态：是否允许访问工作目录外的文件。

    依次为：线程内单次放行 > 任务级放行 > 配置开关（/sandbox on）。
    开关打开后项目外读写均直接放行、不再逐次询问（~/.ycode 凭据文件除外，
    见 tool_adapters._sandbox_outside_guard）。
    """
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
    """将路径解析为绝对路径；默认强制限定在当前工作目录内，越界直接拒绝。

    当配置 allow_outside_workspace 为 True（/sandbox on）时解除目录限制。
    相对路径按线程本地 workdir 解析（_resolve_against_workdir）。
    """
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
        # ~/.ycode 数据目录读取豁免（与沙盒守卫的读取豁免同语义）：
        # 覆盖 @附件挂载与 read_file 的免 bypass 直读；写入路径不传
        # allow_trusted_read，凭据写入仍由沙盒守卫逐次询问。
        try:
            from core.memory_kairos import is_ycode_data_path
            if is_ycode_data_path(str(candidate)):
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
    """快速统计文件行数（二进制模式，避免解码整个文件）。"""
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
    """读取文件内容并返回（仅限当前工作目录内，默认最多 2000 行，超出时给出截断提示）

    参数：
    - offset: 从第几行开始读取（0 起）
    - limit: 最多读取的行数（上限 2000），未指定时按文件大小自适应：
             原始文件 > 50KB 且未指定 limit 时，默认只读前 500 行并提示分页
    - compress: 是否压缩源代码文本（默认 False）。
                压缩项：去除行尾空白、连续 2+ 空行合并为 1 行、裁剪末尾空行。
                不影响语义：保留缩进、注释、字符串字面量。
                设为 True 可节省 token，但内容与原始文件不完全一致——
                若后续要用 edit_file 修改该文件，请用 compress=False 重读以保证匹配。
                建议流程：先 compress=True 快速浏览理解结构 → 若需编辑再 compress=False 重读。
                注意：with_line_numbers=True 时 compress 会被忽略。
    - with_line_numbers: 是否在每行前添加 1-based 行号前缀（默认 False）。
                设为 True 时返回格式如 "1: line1\n2: line2\n..."，便于配合
                edit_file 的 start_line/end_line 参数精准定位。
                适用于大文件、重复代码段、字符串匹配易失败的场景。
    """
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

    # 行号格式化：with_line_numbers=True 时把 selected 转为 "N: line" 格式
    # 1-based 行号，与 offset 配合（selected[i] 对应原始第 start+1+i 行）
    if with_line_numbers and selected:
        numbered_lines = []
        for i, line in enumerate(selected):
            # 兼容 \r\n 和 \n：去掉行尾换行符，统一输出不带行尾换行
            line_text = line.rstrip("\r\n")
            line_no = start + 1 + i
            numbered_lines.append(f"{line_no}: {line_text}")
        result = "\n".join(numbered_lines)
        # 行号路径跳过压缩（compress 即使为 True 也不处理，行号视图必须保留原始行结构）

    # 压缩处理（with_line_numbers 时跳过）
    compressed_note = ""
    if compress and result and not with_line_numbers:
        original_len = len(result)
        result = _compress_source_text(result)
        compressed_len = len(result)
        if original_len > compressed_len:
            compressed_note = f"\n[已压缩：{original_len} → {compressed_len} 字节，省 {original_len - compressed_len} 字节]"

    # 截断提示
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
    return final

def write_file(file_path, content):
    """创建或完整写入文件（仅限当前工作目录内，统一按 utf-8 编码写入）。

    保护规则：如果目标文件已存在且超过 500 行 / 50KB，write_file 会拒绝直接覆盖，
    要求改用 edit_file 进行局部片段修改。这是为了避免把已有大文件整篇重写，
    导致 diff 全红、Token 浪费以及误删未改动的代码。
    """
    resolved = _resolve_workspace_path(file_path)

    # 并发防护：同一文件的 检查→写入 全过程串行化，写前做 mtime 乐观校验
    with _edit_file_lock(resolved):
        # 大文件覆盖保护：强制使用 edit_file 做片段修改。
        # 先 stat 大小短路拒绝：>50KB 的文件不再全量读入数行数（1GB 文件会 OOM），
        # 只有小文件才读全量统计行数。
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
        if existed_before:
            try:
                old_bytes = resolved.read_bytes()
            except OSError as error:
                return f"🚨 无法读取文件原始内容，已拒绝覆盖 {file_path}：{error}"
            try:
                old_content = old_bytes.decode("utf-8")
            except UnicodeDecodeError:
                old_content = old_bytes.decode("gbk", errors="replace")

        # 与 edit_file 对齐：先写同目录临时文件再 os.replace，避免写入中断留下半个文件
        # 自动创建父目录：用户/Agent 可能直接写到不存在的子目录（如 memory/feedback/），
        # 不自动创建会导致 mkstemp 抛 FileNotFoundError，让 Agent 陷入无效重试。
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
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
                f.write(content)
            conflict = _mtime_conflict_error(file_path, resolved, expected_mtime_ns)
            if conflict:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                return conflict
            os.replace(temp_name, resolved)
        except BaseException:
            # 使用 BaseException 确保 KeyboardInterrupt、MemoryError 等非 OSError 异常
            # 同样能触发临时文件清理，避免孤儿 .ycode-tmp 文件在磁盘积累。
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        # 与 edit_file 对齐：仅在写入成功后入撤销栈。
        from core.memory import set_undo_state
        set_undo_state(
            str(resolved),
            old_content,
            existed_before=existed_before,
            old_bytes=old_bytes,
        )
    return f"✔ 文件 {file_path} 写入成功！"

# --- 下面是给星瑶装配的两个新神装 ---

def list_directory(dir_path=".", max_entries=200):
    """雷达工具：扫描文件夹，返回里面的所有文件和文件夹列表（仅限当前工作目录内）"""
    try:
        resolved = _resolve_workspace_path(dir_path)
        if not os.path.exists(resolved):
            return f"🚨 目录扫描失败：路径不存在 '{dir_path}'"
        if os.path.isfile(resolved):
            # 模型把文件路径误传给目录扫描是常见的可恢复误用（尤其 .v 这类
            # 名字像目录的单文件），不用 🚨 错误级返回——否则 UI 会渲染成红色失败。
            # 用普通提示文案引导模型改用 read_file 即可。
            return f"'{dir_path}' 是一个文件而不是目录，无法列出。请改用 read_file 工具读取该文件内容。"
        items = os.listdir(resolved)
        if not items:
            return f"📂 目录 '{dir_path}' 为空。"
        max_entries = max(1, min(int(max_entries or 200), 1000))
        if len(items) > max_entries:
            shown = items[:max_entries]
            shown.append(f"... (目录过大，已截断前 {max_entries} 项，共 {len(items)} 项)")
            return "\n".join(shown)
        return "\n".join(items)
    except Exception as e:
        return f"🚨 目录扫描失败: {str(e)}"

def find_missing_skill_dependencies(command):
    """Return unmet requirements declared by a referenced skill script."""
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
                # 非 PEP508 行（如 -r / -e / --index-url）：按"未知"放行并警告，不阻断命令
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
    from core.command_policy import is_dangerous_command
    from core.workspace_context import get_workdir

    if is_dangerous_command(command):
        return "🚨 危险命令已被安全策略拒绝。请改用范围明确、可恢复的操作。"
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
            background_kwargs = {"session_id": session_id}
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
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = output_encoding
        env["PYTHONUTF8"] = "0"

        # 输出重定向到临时文件而非 PIPE：管道方案依赖 communicate 内部缓冲，
        # yes 类命令会在内存里无限累积（0.1s 轮询 × 600s 可达数 GB）；
        # 写盘 + 周期性检查文件大小才能可靠执行总量上限
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
            process = subprocess.Popen(
                invocation.argv,
                **popen_kwargs,
            )
            start_time = time.time()
            output_capped = False
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
                if time.time() - start_time >= timeout_seconds:
                    _stop_process_tree(process)
                    return f"🚨 命令执行超时（超过 {timeout_seconds} 秒），已被强行终止！"
                time.sleep(0.1)

            if _execution_result is not None:
                _execution_result.returncode = process.returncode
            stdout_raw, stdout_size = _read_sink_capped(stdout_sink)
            stderr_raw, stderr_size = _read_sink_capped(stderr_sink)
        finally:
            try:
                stdout_sink.close()
                stderr_sink.close()
            except Exception:
                pass

        output = decode_process_output(stdout_raw, output_encoding)
        if stderr_raw:
            stderr = decode_process_output(stderr_raw, output_encoding)
            output += f"\n[附加信息/警告]:\n{stderr}"

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
                # 非零退出 + 完全无输出：对 findstr/grep 类搜索命令而言，
                # "没有找到任何匹配"就是退出码 1 且无输出，属正常结果。
                # 不用 🚨 错误级返回（避免 UI 红色失败刷屏），但如实说明退出码，
                # 并提示模型如何区分"无匹配"与"真出错"。
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

# --- 星瑶的新神装：新闻搜索 ---

def search_news(query, num_results=5, cancel_event=None):
    """🌐 现代化新闻搜索工具：稳定可靠的联网引擎"""
    try:
        # 钳制结果数：模型传 100000 会触发 DuckDuckGo 限流
        try:
            num_results = int(num_results)
        except (TypeError, ValueError):
            num_results = 5
        num_results = max(1, min(num_results, 20))
        _raise_if_cancelled(cancel_event)
        with DDGS() as ddgs:
            results = list(ddgs.news(query, max_results=num_results))
            _raise_if_cancelled(cancel_event)

            if not results:
                # 用 🚨 前缀让 _tool_result_ok 识别为工具失败，进而触发子代理 _terminate("failed")
                # 这样主代理会收到结构化失败文本（含 search_news 失败的兜底建议），
                # 而不是误判为 completed 后返回一段"我没搜到"的文本。
                # 语义上"无结果"对 LLM 而言就是任务失败，应走失败流程让主代理决策兜底。
                return (
                    f"🚨 新闻搜索无结果：查询「{query}」未返回任何新闻。"
                    f"可能原因：关键词过窄、网络受限或 DuckDuckGo 服务波动。"
                    f"建议主 Agent 改用 fetch_url 抓取已知新闻站点，或告知用户当前无法联网。"
                )

            formatted_results = []
            for i, r in enumerate(results):
                title = r.get('title', '无标题')
                url = r.get('url', '')
                body = r.get('body', '无摘要')
                formatted_results.append(f"{i+1}. {title}\n   摘要: {body}\n   链接: {url}")

            return "\n\n".join(formatted_results)

    except _ToolCancelledError:
        return "🛑 操作已被用户取消。"
    except Exception as e:
        return f"🚨 新闻搜索工具发生异常: {str(e)}"


class _ResponseTooLargeError(ValueError):
    pass


def _build_browser_like_headers():
    """构造伪装成 Chrome 浏览器的 HTTP 请求头。

    很多新闻网站（NYT、AP News、BBC 等）有反爬虫机制，会检查：
    1. User-Agent 是否为浏览器（ycode/... 会被直接 403）
    2. Accept-Language 是否存在（缺少会被识别为爬虫）
    3. Accept-Encoding 等常见浏览器头

    伪装成主流 Chrome 浏览器可绕过这类基础反爬，让 fetch_url 能正常抓取。
    仅用于 fetch_url 工具读取公开网页，不影响 API 调用（API 仍用 HTTP_USER_AGENT）。
    """
    return {
        "User-Agent": _BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",  # 不压缩，避免需要解压 gzip/br
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        # 参考 Referrer-Policy：同源 Referer 可降低部分反爬拦截概率
        "Referer": "https://www.google.com/",
    }


def _validate_http_url(url):
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL 必须是完整的 http 或 https 地址")
    return parsed.geturl()


# ---------------------------------------------------------------------------
# SSRF 防护
# ---------------------------------------------------------------------------
# 防 SSRF：阻止 LLM 通过 fetch_url 探测内网/云元数据接口
# （参考 Grok Build：阻止非公开 IP，本地需显式 opt-in）
# 防护范围：
#   1. 私网（10.0.0.0/8、172.16.0.0/12、192.168.0.0/16）
#   2. Loopback（127.0.0.0/8、::1）
#   3. Link-local（169.254.0.0/16，含 AWS/GCP/Azure 元数据接口 169.254.169.254）
#   4. 未分配/保留地址（0.0.0.0/8、240.0.0.0/4 等）
#   5. DNS 解析后的 IP 也要校验（防 DNS rebinding 攻击）
# 白名单：用户可在 config.json 加 fetch_url_allow_hosts 显式放行特定主机名

class SSRFBlockedError(ValueError):
    """SSRF 防护拦截的请求"""


# 默认私网/保留 IP 段（ipaddress 模块的 is_private/is_loopback/is_link_local/is_reserved 已覆盖）
# 额外明确禁止的元数据接口
_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal", "metadata.azure.com"}


def _is_safe_public_ip(ip_str):
    """判断 IP 字符串是否为安全的公网 IP（非私网/loopback/link-local/保留）。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    # 公网 IP 的判定：四个属性全部为 False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _get_fetch_url_allow_hosts():
    """读取 config.json 的 fetch_url_allow_hosts 白名单（用户显式 opt-in）。"""
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
    """读取 config.json 的 fetch_url_strict_ssrf 开关（默认 True 严格模式）。

    默认严格：IP 字面量 loopback/私网/保留段拒绝，主机名解析后校验，
    防止 LLM 通过 fetch_url 探测内网/云元数据接口。
    仅当用户显式在 config.json 设置 "fetch_url_strict_ssrf": false 时
    才降级为宽松模式（只拦云元数据主机名，适用于本机使用 Clash/V2Ray
    等代理工具 fake-ip 段被误判的场景）。
    """
    try:
        import config
        data = config._read_json_with_backup(config._CONFIG_FILE, default=None)
        if not isinstance(data, dict):
            return True
        return bool(data.get("fetch_url_strict_ssrf", True))
    except Exception:
        return True


def _check_ssrf_url(url):
    """SSRF 防护：校验 URL 的主机名是否为安全目标。

    两种模式共有（始终生效）：
      1. 主机名在显式白名单中 → 放行
      2. 主机名在云元数据黑名单中 → 拒绝（核心防护，始终保留）

    严格模式（fetch_url_strict_ssrf=True，默认）：
      在上述基础上增加：
      3. 如果 host 是 IP 字面量，校验是否公网（loopback/私网/保留段拒绝）
      4. 否则 DNS 解析所有 A/AAAA 记录，任一非公网即拒绝
      5. DNS 解析失败 → 拒绝（保守策略）

    宽松模式（用户显式设置 fetch_url_strict_ssrf=false 才启用，对齐 kimicode/opencode）：
      只做 1-2，不查 IP，避免误杀 Clash/V2Ray fake-ip 段（198.18.0.0/15 等）。
    """
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()

    if not host:
        raise SSRFBlockedError("URL 缺少主机名")

    # 1. 显式白名单优先（两种模式都支持）
    allow_hosts = _get_fetch_url_allow_hosts()
    if host in allow_hosts:
        return

    # 2. 云元数据接口黑名单（始终拦截，这是 SSRF 防护的核心价值）
    if host in _METADATA_HOSTS:
        raise SSRFBlockedError(
            f"已阻止访问云元数据接口 {host}（防止 SSRF 泄露凭据）"
        )

    # 宽松模式：到此为止，不再做 IP 校验
    if not _get_fetch_url_strict_ssrf():
        return

    # ---- 以下为严格模式专属的 IP 校验 ----
    # 3. 如果 host 本身就是 IP 字面量，直接校验
    try:
        ipaddress.ip_address(host)
        if not _is_safe_public_ip(host):
            raise SSRFBlockedError(
                f"已阻止访问非公网 IP {host}（防止 SSRF 访问内网）"
            )
        return
    except ValueError:
        pass  # 不是 IP 字面量，是域名，继续 DNS 解析

    # 4. DNS 解析后校验所有 IP
    try:
        # getaddrinfo 返回 (family, type, proto, canonname, sockaddr) 列表
        # sockaddr 对 IPv4 是 (ip, port)，对 IPv6 是 (ip, port, flowinfo, scope_id)
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
    # SSRF 防护：在发起请求前校验主机与解析后的 IP
    # 即使 LLM 拼接了内网/元数据接口 URL，也会在此被拦截
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
        # 截断模式：读到 HTTP_MAX_BYTES 上限后停止读取，附加截断提示
        # 而非直接报错——文章正文通常在前几十 KB，剩余多为模板/脚本/广告，
        # 截断后 LLM 仍能拿到正文，比直接报错让 LLM 啥也拿不到更实用。
        raw = response.read(HTTP_MAX_BYTES)
        truncated = False
        # 探测是否还有更多数据：再读 1 字节，如果能读到说明被截断了
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
            # IP 类拦截：提示可能是代理软件 fake-ip，或关闭严格模式
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
# Content-Type 白名单 → 文件扩展名。
# 明确排除 image/svg+xml：SVG 可内嵌 <script>，GUI 渲染时有脚本注入风险
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
        # SSRF 防护与 fetch_url 一致：发起请求前校验主机与解析后的 IP
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
            # 去掉 ; 后的参数（如 image/png; charset=binary）再匹配白名单
            content_type = str(content_type).split(";", 1)[0].strip().lower()
            extension = _IMAGE_CONTENT_TYPES.get(content_type)
            if extension is None:
                raise ValueError(
                    f"不支持的内容类型 {content_type or '未知'}"
                    "（仅支持 png/jpg/jpeg/gif/webp；svg 有脚本注入风险，已拒绝）"
                )
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            # 分块读取并累计体积，超过 10MB 立即中止（不落盘半成品文件）
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
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"download_{timestamp}.{extension}"
        target = Path.cwd() / filename
        # 同秒重复/并发下载时避免互相覆盖
        counter = 2
        while target.exists():
            filename = f"download_{timestamp}_{counter}.{extension}"
            target = Path.cwd() / filename
            counter += 1
        target.write_bytes(bytes(data))
        return (
            f"HTTP {status} ({content_type})\n"
            f"图片已下载到本地：{target}（{len(data)} 字节）\n"
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
    
# search_code 的 ripgrep 超时（秒）：大型仓库（含 target/node_modules 等）下 15 秒
# 经常不够，默认 30 秒；可用环境变量 YCODE_SEARCH_TIMEOUT 覆盖。
try:
    SEARCH_CODE_TIMEOUT_SECONDS = max(1, int(os.environ.get("YCODE_SEARCH_TIMEOUT", "30") or 30))
except (TypeError, ValueError):
    SEARCH_CODE_TIMEOUT_SECONDS = 30


def _run_process_polled(cmd, timeout_seconds, cancel_event=None):
    """subprocess.run 的可取消替代：Popen + communicate 短轮询。

    - 每 0.2s 检查一次 cancel_event，取消时终止进程树并返回 None
    - 超时行为与 subprocess.run 对齐：抛 subprocess.TimeoutExpired
    返回 subprocess.CompletedProcess。
    """
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
    """合并默认排除目录与 config.json 的 search_exclude_dirs 自定义配置。"""
    dirs = set(_SEARCH_DEFAULT_IGNORE_DIRS)
    try:
        from core.model_manager import load_config
        extra = load_config().get("search_exclude_dirs")
        if isinstance(extra, (list, tuple, set)):
            dirs.update(str(d).strip() for d in extra if str(d).strip())
    except Exception:
        pass
    return dirs


def search_code(query, dir_path=".", cancel_event=None):
    """精准搜索：跨文件全局查找代码片段或关键字。优先尝试 ripgrep。"""
    import subprocess
    try:
        # 与 read_file 等工具一致，强制限定在当前工作目录内
        dir_path = str(_resolve_workspace_path(dir_path))
    except ValueError as e:
        return f"🚨 搜索失败: {e}"

    if cancel_event is not None and cancel_event.is_set():
        return "🛑 搜索已被用户取消。"

    cache_key = _tool_cache.search_key("search_code", query, dir_path)
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
        cmd = [
            rg_executable, "-n", "-i", "--no-heading",
            "--max-columns", "200", "-C", "1",
            "-m", "20",  # 每个文件最多 20 处匹配，避免单文件刷屏
        ]
        # 统一排除构建产物目录：git 仓库 rg 靠 .gitignore 排除，
        # 非 git 目录必须显式下发，否则会命中 target/dist 产物
        for _d in sorted(_get_search_exclude_dirs()):
            cmd += ["--glob", f"!**/{_d}/"]
        if literal:
            cmd.append("-F")
        cmd += [args_query, dir_path]
        # 可取消轮询版本：取消时返回 None（原先 subprocess.run 阻塞最长 30s 不响应取消）
        return _run_process_polled(cmd, SEARCH_CODE_TIMEOUT_SECONDS, cancel_event)

    def _format_rg_output(output):
        if not output:
            return "未找到匹配的代码片段。"
        lines = output.split("\n")
        if len(lines) > 60:
            return "\n".join(lines[:60]) + f"\n\n... (匹配结果过多，已截断，共 {len(lines)} 行)"
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
                # 正则语法错误 → 按字面量重试（模型经常传入含特殊字符的代码片段）
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
    max_matches = 50
    
    try:
        for root, dirs, files in os.walk(dir_path):
            if cancel_event is not None and cancel_event.is_set():
                return "🛑 搜索已被用户取消。"
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith('.')]
            
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext not in ALLOWED_EXTS:
                    continue
                    
                file_path = os.path.join(root, file)
                
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                        
                    for line_num, line in enumerate(lines, 1):
                        if query.lower() in line.lower():
                            start = max(0, line_num - 2)
                            end = min(len(lines), line_num + 1)
                            snippet = "".join(lines[start:end])
                            
                            results.append(f"📄 {file_path} (行 {line_num}):\n{snippet.strip()}")
                            match_count += 1
                            
                            if match_count >= max_matches:
                                results.append(f"\n⚠️ 搜索结果太多，已截断前 {max_matches} 条，请尝试更精确的关键字。")
                                result_text = "\n---\n".join(results)
                                _tool_cache.search_put(cache_key, result_text)
                                return result_text
                except Exception:
                    continue
                    
        if not results:
            result_text = f"🔍 搜索完毕，未在代码文件中找到包含 `{query}` 的内容。"
        else:
            result_text = "\n---\n".join(results)
        _tool_cache.search_put(cache_key, result_text)
        if session_id:
            _tool_cache.shared_put(session_id, ("search_code", cache_key), result_text)
        return result_text
    except Exception as e:
        return f"🚨 搜索失败: {str(e)}"

# --- 星瑶的新神装：后台任务管理与局部代码修改 ---

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
    """
    后台任务统一管理器：同时管理两类后台任务——
      1. shell 后台命令（execute_command run_in_background 启动，ID 为 8 位十六进制）；
      2. 后台/前台子代理任务（run_subagent 启动，task_id 形如 subagent-xxxxxxxx）。
    参数:
      action: 可选值：'list'（列出运行中的后台任务，两类合并）,
        'status'（查看指定任务快照；shell 附日志尾部，子代理附阶段与耗时）,
        'wait'（条件等待）, 'kill'（强制终止指定任务）。
      task_id: 目标任务的 ID，'status'/'wait'/'kill' 行动必填。
      after_revision: 仅对 shell 任务生效，缺省视为 0（首次等待）；子代理任务无 revision，传入则忽略。
      timeout_seconds: wait 的最长等待秒数（1-60，默认 30）；超时返回当前快照且 changed=false，不是错误。
      event_callback/permission_owner/cancel_event: 调度层透传的权限上下文（不暴露给模型），
        子代理线程内调用 kill 时必须传入，缺省路径仅在主线程上下文可用。
    wait 语义：shell 任务为「revision 超过 after_revision、进入终态或超时」；
      子代理任务为「进入终态或超时」（终态时附 result 摘要，供回合内直接收割结果）。
    status/wait 返回 JSON（含 "kind": "shell"|"subagent"）；shell 载荷为
    {"task_id","status","revision","terminal","exit_code","output"}，wait 另含 "changed"；
    list 返回 {"tasks": [...]}，每条含 kind 字段。
    """
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
                if len(result) > _SUBAGENT_STATUS_RESULT_MAX_CHARS:
                    omitted = len(result) - _SUBAGENT_STATUS_RESULT_MAX_CHARS
                    result = (
                        result[:_SUBAGENT_STATUS_RESULT_MAX_CHARS]
                        + f"… [结果已截断，省略 {omitted} 字符；完整结果将在回合结束后的自动汇总中注入]"
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
            # 子代理无 revision：after_revision 忽略，等待条件为进入终态或超时
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
            after_revision = 0  # 首次等待语义：等任意输出或终态
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
        # 注意：缺省（无 event_callback）路径会在调用线程内直接 safe_print 触碰终端，
        # 仅在主线程上下文中可用；子代理/调度层调用时必须透传权限上下文参数。
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
            # 子代理为协作式取消：置位 cancel_event 并走终态提交，不强制杀线程
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
            # 进程在 kill 之前已自行结束：不是终止失败，按自然终态落定
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

def _fuzzy_replace(content, target, replacement, replace_all=False):
    """
    在 content 中智能定位 target 并替换为 replacement。

    匹配策略（按优先级）：
    1. 精确字符串匹配（含换行风格自适应）。
    2. 忽略首尾空白差异的匹配（保留 content 中实际的空白边界）。
    3. 基于 difflib 的模糊匹配（相似度 >= 0.90），仅用于 target_content 存在
       轻微字符污染但仍能安全定位的场景；明确截断的 target 不会被强行匹配，
       避免 replacement 与剩余原文产生结构重复。

    返回 (new_content, matched_count)。matched_count == 0 表示未找到。
    """
    import difflib

    target_len = len(target)
    if target_len == 0:
        return content, 0

    # 1. 精确匹配
    occurrences = content.count(target)
    if occurrences == 1:
        return content.replace(target, replacement, 1), 1
    if occurrences > 1:
        if replace_all:
            return content.replace(target, replacement), occurrences
        return content, occurrences

    # 2. 忽略首尾空白：找到 content 中 stripped_target 唯一出现的位置，
    #    用该位置的实际边界（含原始首尾空白）进行替换，避免缩进丢失。
    stripped_target = target.strip("\r\n\t ")
    if stripped_target and stripped_target != target:
        stripped_occ = content.count(stripped_target)
        if stripped_occ == 1:
            start = content.find(stripped_target)
            end = start + len(stripped_target)
            # 扩展到 content 中连续的空白边界
            while start > 0 and content[start - 1] in "\r\n\t ":
                start -= 1
            while end < len(content) and content[end] in "\r\n\t ":
                end += 1
            return content[:start] + replacement + content[end:], 1
        if stripped_occ > 1:
            return content, stripped_occ

    # 3. 模糊匹配：按 target 的物理行数在 content 中滑动，取相似度最高的窗口。
    #    适用于 target_content 存在个别字符污染但仍与原文高度相似的场景。
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
                # 取与 target 行数相同的窗口作为候选
                j = i + len(target_lines)
                if j > len(content_lines):
                    continue
                # 同时要求窗口最后一行包含 target 最后一行的文本
                if last_line_text not in content_lines[j - 1]:
                    continue
                candidate = "".join(content_lines[i:j])
                ratio = difflib.SequenceMatcher(None, target, candidate).ratio()
                candidates.append((ratio, i, j, candidate))
            if candidates:
                candidates.sort(reverse=True)
                best_ratio, i, j, candidate = candidates[0]
                # 要求整段高度相似，且长度接近，拒绝明显截断或不匹配的候选
                if best_ratio >= 0.90 and abs(len(candidate) - target_len) <= max(target_len // 10, 10):
                    start = sum(len(ln) for ln in content_lines[:i])
                    end = sum(len(ln) for ln in content_lines[:j])
                    return content[:start] + replacement + content[end:], 1

    # 4. 字符级滑动窗口兜底（仅对中小文件/目标，避免 O(n^2) 灾难）
    # 先用 target 前 50 字符作为锚点快速定位候选区域，再仅在锚点邻域内做
    # difflib 相似度比较，把最坏情况从 4 亿次 ratio() 降到可接受范围。
    content_len = len(content)
    if target_len <= 2000 and content_len <= 200_000:
        best_ratio = 0.0
        best_start = 0
        anchor_len = min(50, max(1, target_len // 4))
        anchor = target[:anchor_len]
        search_start = 0
        # 锚点邻域半径：保证能覆盖轻微位移（如插入/删除少量字符）
        window_radius = 200
        while True:
            anchor_idx = content.find(anchor, search_start)
            if anchor_idx == -1:
                break
            # 在锚点附近做小范围滑动窗口
            region_start = max(0, anchor_idx - window_radius)
            region_end = min(content_len - target_len + 1, anchor_idx + window_radius)
            for i in range(region_start, region_end + 1):
                window = content[i : i + target_len]
                ratio = difflib.SequenceMatcher(None, target, window).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_start = i
            search_start = anchor_idx + 1

        # 锚点完全找不到时（target 被严重污染或全为空白），不再做全量字符滑动窗口。
        # 全量滑窗在最坏情况下是 O(n*m) 的 difflib 比较，常见幻觉 target 会让
        # 工具假死数分钟，因此直接放弃匹配，把失败信息返回给模型。
        if best_ratio >= 0.90:
            best_end = best_start + target_len
            return content[:best_start] + replacement + content[best_end:], 1

    return content, 0


def _edit_file_by_lines(
    file_path, resolved, content, encoding, start_line, end_line, replacement_content,
    expected_mtime_ns=None,
):
    """行号路径：按 1-based 行号范围（inclusive）切片替换，跳过字符串匹配。

    用于大文件、重复代码段、字符串匹配易失败的场景。
    建议先用 read_file(with_line_numbers=True) 获取行号；文件被外部修改后行号可能失效。
    边界处理：end_line 超出总行数时自动收敛到总行数（不报错，更宽容）；
              start_line 超出总行数或 < 1 时报错。
    """
    # 参数校验
    if start_line is None or end_line is None:
        return (
            f"🚨 行号编辑失败！start_line 和 end_line 必须同时提供"
            f"（当前 start_line={start_line}, end_line={end_line}）。"
        )
    # 防御性：拒绝 bool（bool 是 int 的子类，True==1 会被误用）
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

    # 切行（保留换行符）
    lines = content.splitlines(keepends=True)
    total = len(lines)
    if total == 0:
        return f"🚨 行号编辑失败！文件 {file_path} 为空，无法按行号编辑。"
    if start_line > total:
        return (
            f"🚨 行号编辑失败！start_line={start_line} 超出文件总行数 {total}。\n"
            f"建议：重新 read_file(with_line_numbers=True) 获取最新行号。"
        )
    # end_line 超过总行数时自动收敛（不报错，更宽容）
    actual_end = min(end_line, total)

    # 提取被替换的原文（用于预览展示）
    replaced_block = "".join(lines[start_line - 1:actual_end])

    replacement = str(replacement_content or "")

    # 行号路径换行语义：replacement 按行切分，每行末尾补文件原换行符。
    # 这样 LLM 给 "return 42" 时会变成 "return 42\n"，与原行结构对齐，
    # 避免 replacement 不带换行符时把后续行合并成一行。
    # splitlines() 会去掉所有换行符，得到纯行内容列表；
    # 空字符串 → []（删除所有行）；"X\nY" → ["X","Y"]；"X\nY\n" → ["X","Y"]（末尾换行不影响）。
    if "\r\n" in content:
        line_sep = "\r\n"
    else:
        line_sep = "\n"
    replacement_lines = replacement.splitlines()
    replacement = "".join(line + line_sep for line in replacement_lines)

    # 拼接新内容：[行号前的所有行] + replacement + [行号后的所有行]
    new_content = (
        "".join(lines[:start_line - 1]) + replacement + "".join(lines[actual_end:])
    )

    if new_content == content:
        return (
            f"🚨 行号编辑失败！第 {start_line}-{actual_end} 行替换后内容与原文相同，"
            f"未发生实际修改（{file_path}）。"
        )

    # 原子写入（含 mtime 乐观校验），成功后才写入撤销栈，避免失败编辑污染撤销历史
    write_err = _atomic_write_edit_file(
        resolved, encoding, new_content,
        file_path=file_path, expected_mtime_ns=expected_mtime_ns,
    )
    if write_err:
        return write_err
    from core.memory import set_undo_state
    set_undo_state(file_path, content)

    # 构建预览：被替换的原文前 3 行 + 后 2 行（超过 5 行时省略中间）
    replaced_preview_lines = replaced_block.splitlines()
    if len(replaced_preview_lines) <= 5:
        preview = replaced_block.rstrip("\r\n")
    else:
        preview = "\n".join(
            replaced_preview_lines[:3]
            + ["... (省略中间部分) ..."]
            + replaced_preview_lines[-2:]
        )

    return (
        f"✔ 文件 {file_path} 第 {start_line}-{actual_end} 行替换成功！\n"
        f"被替换原文预览：\n{preview}"
    )


def _atomic_write_edit_file(resolved, encoding, new_content, file_path=None, expected_mtime_ns=None):
    """原子写入 edit_file 结果：先写同目录临时文件再 os.replace。

    expected_mtime_ns 不为 None 时，写入前做 mtime 乐观校验：
    文件在读取后被外部/并发修改则拒绝覆盖，返回错误让调用方重读。
    返回 None 表示成功，返回字符串表示错误。
    """
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


def _smart_replace_without_target(file_path, content, replacement, replace_all):
    """智能降级：LLM 只传了 replacement_content，未提供 target_content 和行号。

    设计思路：
    用 difflib.SequenceMatcher 在文件中找出与 replacement 最相似的连续行块，
    按"相似度 × 行数匹配权重"综合排序，取最佳候选替换。

    适用场景：LLM 想"重写某段代码"但没说清替换哪段。
    不适用场景：极小改动（如 while→for），因为新旧行数相近时难以精确定位——
    这种场景应让 LLM 显式传 target_content。

    返回：
    - 成功：(new_content, occurrences, replaced_preview)
    - 失败：错误字符串
    """
    import difflib

    replacement_lines = [line.rstrip("\r\n") for line in replacement.splitlines()]
    content_lines = content.splitlines(keepends=True)
    content_lines_stripped = [line.rstrip("\r\n") for line in content_lines]

    if not replacement_lines or not content_lines_stripped:
        return (
            f"🚨 智能替换失败：replacement_content 或文件内容为空（{file_path}）。"
        )

    # 文件行数 < replacement 行数时，整个文件就是候选
    if len(content_lines_stripped) < len(replacement_lines):
        return (
            f"🚨 智能替换失败：文件 {file_path} 共 {len(content_lines_stripped)} 行，"
            f"少于 replacement_content 的 {len(replacement_lines)} 行。"
            f"若要整体重写，请显式使用 write_file。"
        )

    # 综合评分：相似度 × 行数匹配权重
    # 行数匹配权重：行数完全相同 = 1.0，行数差异越大权重越低（线性衰减到 0）
    # 这样优先选行数匹配的块，避免"3行replacement匹配到2行原文"的错位
    repl_len = len(replacement_lines)
    candidates = []  # [(start_idx, combined_score, ratio, block_lines)]
    # 搜索窗口范围：repl_len 的 50%~200%
    min_len = max(1, repl_len // 2)
    max_len = min(len(content_lines_stripped), repl_len * 2)

    for window_len in range(min_len, max_len + 1):
        # 行数匹配权重：差异越小权重越高
        len_diff = abs(window_len - repl_len)
        len_weight = 1.0 - (len_diff / max(repl_len, window_len))
        for i in range(len(content_lines_stripped) - window_len + 1):
            block = content_lines_stripped[i:i + window_len]
            # 行级相似度：对每对行计算相似度后取平均（比字符级 ratio 更宽容）
            # 这样结构相似但内容不同的代码段也能被识别
            line_ratios = []
            for r_line, b_line in zip(replacement_lines, block):
                # 每行也用 SequenceMatcher，但只比较单行
                lr = difflib.SequenceMatcher(
                    None, r_line, b_line, autojunk=False
                ).ratio()
                line_ratios.append(lr)
            # 不足的行（block 比 replacement 长）按 0 计入
            while len(line_ratios) < max(repl_len, window_len):
                line_ratios.append(0.0)
            ratio = sum(line_ratios) / len(line_ratios)
            if ratio >= 0.3:  # 行级相似度阈值放宽到 0.3
                combined = ratio * len_weight
                candidates.append((i, combined, ratio, block))

    if not candidates:
        return (
            f"🚨 智能替换失败：在 {file_path} 中未找到与 replacement_content "
            f"足够相似的代码段。"
            f"请显式提供 target_content（字符串匹配）或 start_line/end_line（行号路径）。"
        )

    # 按综合评分降序
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

    # 多候选检测：综合评分 >= 0.7 的候选如果超过 1 个且未启用 replace_all，拒绝替换
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

    # 确定要替换的候选集合：replace_all 时替换所有高评分候选（按起始行排序，
    # 跳过与已选块重叠的窗口——滑窗会在同一区域产生大量重叠高评分候选）；
    # 单点替换时只有唯一高评分候选
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

    # 执行替换
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

    # 构建被替换原文预览（第一处替换块的前 5 行）
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
    """
    手术刀局部替换工具：用于对已有代码文件进行点对点局部精准替换，不用每次都重写整张文件，避免开销和误删。

    支持两种编辑路径（二选一）：
    1. 字符串匹配路径（默认）：提供 target_content + replacement_content，
       系统用四层智能匹配定位（精确 → 忽略空白 → difflib 模糊 → 字符滑窗）。
       适用于 target_content 易于复述原文、文件较小的场景。
    2. 行号路径：提供 start_line + end_line + replacement_content，
       直接按 1-based 行号范围（inclusive）切片替换，跳过字符串匹配。
       适用于大文件、重复代码段、字符串匹配易失败的场景。
       建议先用 read_file(with_line_numbers=True) 获取行号。

    参数:
      file_path: 要修改的文件的相对或绝对路径（仅限当前工作目录内）。
      target_content: 字符串匹配路径下，文件中要被替换的旧代码段
                      （尽量保持原始缩进和换行；允许轻微差异，系统会尝试智能定位）。
                      行号路径下可省略（即使提供也忽略）。
      replacement_content: 准备换上的新代码段。
      replace_all: 字符串匹配路径下，是否替换全部出现位置（默认 False，旧代码段
                   出现多次时必须显式开启）。行号路径下忽略此参数。
      start_line, end_line: 行号路径参数，1-based 行号范围（inclusive）。
                            同时提供时启用行号路径；同时为 None 时启用字符串匹配路径。
                            end_line 超过文件总行数时自动收敛不报错；start_line 超出或 <1 报错。
    """
    try:
        resolved = _resolve_workspace_path(file_path)
    except ValueError as e:
        return f"🚨 精准修改失败！{e}"
    # 并发防护：同一文件的 读→改→写 全过程按路径串行化（多子代理并发编辑同一
    # 文件时后写者不再静默覆盖），写前另有 mtime 乐观校验检测外部修改
    with _edit_file_lock(resolved):
        # 先记录 mtime 再读取：写入前若 mtime 变化即拒绝覆盖。
        # 宁可误报冲突（让调用方重读），也不放过读后写前的外部修改窗口。
        expected_mtime_ns = _stat_mtime_ns(resolved)
        try:
            raw = resolved.read_bytes()
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

        # 行号路径：直接按行号切片替换，跳过字符串匹配
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
            # 原子写入（含 mtime 校验），成功后才写入撤销栈，避免失败编辑污染撤销历史
            write_err = _atomic_write_edit_file(
                resolved, encoding, new_content,
                file_path=file_path, expected_mtime_ns=expected_mtime_ns,
            )
            if write_err:
                return write_err
            from core.memory import set_undo_state
            set_undo_state(file_path, content)
            return (
                f"✔ 智能修改 {file_path}（自动定位最相似代码段，共 {occurrences} 处替换）\n"
                f"被替换的原文（前 5 行）：\n{replaced_preview}"
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
            return (
                f"🚨 精准修改失败！在文件 {file_path} 中没有找到与 target_content 足够相似的代码段。\n"
                f"建议：1) 使用 read_file(compress=False) 重新读取原始文本；"
                f"2) 把 target_content 缩短到 1500 字符以内并包含唯一上下文；"
                f"3) 若确实需要整体重写，再使用 write_file（write_file 会覆盖整个文件，diff 会显示全红）。"
            )
        if occurrences > 1 and not replace_all:
            return (
                f"🚨 精准修改失败！旧代码段在文件 {file_path} 中出现了 {occurrences} 次，无法确定唯一修改位置。\n"
                f"请提供更长的、包含更多上下文的 target_content 以保证唯一匹配；如果确实需要全部替换，请显式设置 replace_all=True。"
            )

        if new_content == content:
            return f"🚨 精准修改失败！定位成功但替换后的内容与原文相同，未发生实际修改（{file_path}）。"

        # 原子写入（含 mtime 校验），成功后才写入撤销栈，避免失败的编辑污染撤销历史
        write_err = _atomic_write_edit_file(
            resolved, encoding, new_content,
            file_path=file_path, expected_mtime_ns=expected_mtime_ns,
        )
        if write_err:
            return write_err
        from core.memory import set_undo_state
        set_undo_state(file_path, content)
        return f"✔ 已修改文件：{file_path}"

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

# 同一时刻真正执行的子代理上限：超出的任务保持排队状态等待空位。
# 无上限时模型一次派发 6+ 个子代理会挤爆 API 并发与本机资源。
_MAX_CONCURRENT_SUBAGENTS = 5

# 并发闸门排队等待上限按任务自身超时预算计：排队时间不计入执行超时，
# 但若排队超过自身预算仍未拿到名额（例如名额被卡死任务占用），直接判超时，
# 避免前台 run_subagent 无限阻塞（曾因此整回合静默几十分钟）。

# 审批等待独立上限（秒）：与 gui_bridge.PERMISSION_TIMEOUT_SECONDS（300）对齐。
# 审批等待不计入执行超时，但权限 broker 卡死时任务不能无限悬挂占用并发闸门，
# 超过该上限即终止任务并取消其名下权限请求。
_SUBAGENT_APPROVAL_WAIT_LIMIT = 300

# 子代理前台结果进入主代理上下文的长度上限（字符）。此前为 4000，多段式汇报
# （如 explore 代理按任务分段的结论）会被拦腰截断，主代理只能基于残缺信息决策；
# 与 core/llm.py 工具结果入上下文的截断上限（16000）对齐。
SUBAGENT_RESULT_MAX_CHARS = 16000


def _has_meaningful_subagent_result(text: str) -> bool:
    return bool(re.sub(r"[\s`*_#>~\-\[\](){}|.,!?，。！？:：;；/\\]+", "", text or ""))


_ADVISOR_TAG_RE = re.compile(
    r"\[(?:Advisor consultation[^\]]*|Advisor review|End of advisor consultation[^\]]*)\]"
    r"|\(\s*advisor[^\)]*\)",
    re.IGNORECASE,
)
# 模型（尤其 Kimi 系）偶尔把内部协议整块泄漏到正文：<advisor-request> 求助文本、
# XML 形式的 <function_calls>/<tool_call> 伪工具调用。这些不是结论，整块剔除。
# 未闭合的块（流被截断）一律剔到文本末尾。
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


@dataclass
class SubagentTask:
    task_id: str
    parent_session: str
    parent_turn_id: str
    description: str
    workdir: str = ""
    subagent_type: str = "general"
    plan_item_id: str = ""
    run_in_background: bool = False
    status: str = "pending"
    started_at: float | None = None
    finished_at: float | None = None
    result: str = ""
    error: str = ""
    tool_errors: dict[str, str] = field(default_factory=dict, repr=False)
    stage: str = "queued"
    stages: list[SubagentStage] = field(default_factory=list)
    execution_elapsed: float = 0.0
    approval_wait_elapsed: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    done_event: threading.Event = field(default_factory=threading.Event, repr=False)
    notification_consumed: bool = False
    # 独立于 notification_consumed：标记任务结果是否已注入到汇总 turn 的上下文中。
    # 避免与 drain_notifications 的消费标志冲突——drain_notifications 用于普通 turn
    # 的 LLM 上下文注入，summary_injected 用于自动汇总 turn 的上下文注入。
    summary_injected: bool = False
    terminal_callback: Callable | None = field(default=None, repr=False)
    callback_invoked: bool = field(default=False, repr=False)
    model_config: tuple | None = field(default=None, repr=False)
    # 子代理 diff_ready 等事件转发回父回合的事件回调（主对话 emit）。
    # 子代理工具（edit_file/write_file）产生的 diff 若不转发，父回合的
    # "文件更改"面板会丢失子代理修改的文件。
    event_callback: Callable | None = field(default=None, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    custom_config: dict | None = field(default=None, repr=False)
    timeout_seconds: int = field(default=600, repr=False)
    # 并发名额生命周期标记：超时/取消是协作式，线程可能仍卡在不可中断的
    # LLM 请求里，但名额要立即归还（见 _release_slot），两者都必须幂等
    slot_acquired: bool = field(default=False, repr=False)
    slot_released: bool = field(default=False, repr=False)
    # 超时移交结果（run_subagent 的 terminal_callback 写入）：
    # ""=未移交；"main_takeover"/"takeover_queued"=移交成功；
    # "failed"=移交失败（保留 timed_out 事实，不得改写为普通 failed 重新委派）
    handoff: str = field(default="", repr=False)


SubagentRunner = Callable[[SubagentTask], str]

# 后台子代理终态记录未被父会话 drain 时的存活时间上限（秒），超时后可被容量清理回收
_BACKGROUND_NOTIFICATION_TTL_SECONDS = 600


def _snapshot_model_config(
    prefer_secondary: bool = True,
    session_name: str | None = None,
) -> tuple | None:
    """启动时快照当前模型配置，避免子代理运行途中用户切换模型/密钥导致配置混用。

    若 config.json 中配置了 secondary_model_id 且 prefer_secondary=True（默认），
    子代理优先使用 cheaper secondary 模型，降低后台任务额度消耗。
    """
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
        # 并发闸门：限制同时真正执行的子代理数量（排队不占名额）
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
            # 失败时给 error，成功时给 result 摘要，方便用户快速判断
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
    ) -> str:
        """运行一个子代理任务。

        注意：超时是协作式取消——超时/取消只设置 cancel_event 并记录终态，
        子代理线程需要响应 cancel_event 才会真正退出；前后台任务都受同一超时监管。
        task_id：调用方（run_subagent）预先分配的真实执行租约 id，必须在
        manager 锁内注册前拒绝碰撞；省略时保持自动生成行为（后台/兼容调用）。
        """
        if not 30 <= timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be between 30 and 3600")
        description = str(task_description or "").strip()
        if not description:
            raise ValueError("task_description must not be empty")
        if model_config is None and self._uses_default_runner:
            model_config = _snapshot_model_config(session_name=parent_session)

        # 加载自定义子 agent 配置：subagent_type 非内置 5 种时尝试从注册中心获取
        # 容错：加载失败回退到 None（_run_default_subagent 会按内置 general 处理）
        custom_config = None
        normalized_type = str(subagent_type or "general")
        if normalized_type and normalized_type not in {
            "explore", "implement", "test", "review", "general"
        }:
            try:
                from core.subagent_registry import get_custom_subagent
                custom_config = get_custom_subagent(normalized_type)
            except Exception:
                custom_config = None

        from core.workspace_context import get_workdir
        task = SubagentTask(
            task_id=str(task_id or _new_subagent_task_id()),
            parent_session=str(parent_session or ""),
            parent_turn_id=str(parent_turn_id or ""),
            description=description,
            workdir=str(get_workdir()),
            subagent_type=normalized_type,
            plan_item_id=str(plan_item_id or ""),
            run_in_background=bool(run_in_background),
            timeout_seconds=int(timeout_seconds),
            terminal_callback=terminal_callback,
            model_config=model_config,
            custom_config=custom_config,
            event_callback=event_callback,
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
        approval_wait = 0.0  # 连续审批等待计时（独立于 execution_elapsed）
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

        return self._foreground_result(task.task_id)

    def _watch_background(
        self,
        task: SubagentTask,
        timeout_seconds: int,
        parent_cancel_event: threading.Event | None,
    ) -> None:
        """后台子代理的看门狗：执行与前台一致的超时/父取消监管（协作式取消）。"""
        last_tick = self._monotonic()
        approval_wait = 0.0  # 连续审批等待计时（独立于 execution_elapsed）
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
        # 并发闸门：拿到执行名额前保持 queued 状态（排队时间不计入执行超时），
        # 期间响应取消与外部终态（如父回合取消、超时监管）。
        # 排队超过任务自身超时预算仍未拿到名额时直接判超时：名额可能被卡死
        # 任务占用（协作式取消杀不死阻塞中的 LLM 请求），不能无限等。
        # 注意：只有真正排过队才读取时钟（单任务直拿名额是主流路径，
        # 不额外消耗注入的 monotonic 时钟序列）。
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
        try:
            with self._lock:
                if task.status != "pending":
                    return
                task.status = "running"
                task.started_at = time.time()
                self._record_stage_locked(task, "running")

            # 子代理线程标记父会话，使工具级共享缓存可被复用
            _tool_cache.set_current_session(task.parent_session)
            # 子代理线程继承父回合的工作目录上下文（多会话并行隔离）
            from core.workspace_context import set_workdir
            if task.workdir:
                set_workdir(task.workdir)
            # 计划/授权/沙盒放行等“每会话”状态同样路由到父会话：
            # 子代理执行计划项、触发权限询问时操作的是父会话的桶，
            # 不会串到其他并行会话
            import core.app_context as _subagent_app_context

            _subagent_app_context.set_thread_session(task.parent_session)
            from core.plan import set_current_plan_session as _bind_plan_route

            _bind_plan_route(task.parent_session)
            from core.memory import bind_turn_checkpoint as _bind_turn_checkpoint

            _bind_turn_checkpoint(task.parent_session, task.parent_turn_id)
            try:
                result = _strip_advisor_tags(str(self._runner(task) or "").strip())
            except BaseException as error:
                if task.cancel_event.is_set():
                    self._terminate(task.task_id, "cancelled", "task cancelled")
                else:
                    self._terminate(task.task_id, "failed", str(error))
                _tool_cache.set_current_session(None)
                return

            try:
                if task.cancel_event.is_set():
                    self._terminate(task.task_id, "cancelled", "task cancelled")
                elif task.tool_errors:
                    details = "; ".join(
                        f"{name}: {detail}" for name, detail in task.tool_errors.items()
                    )
                    self._terminate(task.task_id, "failed", details)
                elif not _has_meaningful_subagent_result(result):
                    self._terminate(task.task_id, "failed", "subagent returned no public answer")
                else:
                    self._complete(task.task_id, result)
            finally:
                _tool_cache.set_current_session(None)
        finally:
            from core.memory import clear_turn_checkpoint_binding

            clear_turn_checkpoint_binding()
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
                # 回调期间标记 finalizing，前台/后台监管循环据此暂停超时计时
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
            self._record_stage_locked(task, "completed")
        # 后台任务终态实时通知（worker 线程内，回调自行切回 UI 线程）
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
            # 硬中断该子代理的在飞 LLM 请求：关闭底层流让阻塞读立即退出，
            # 而不是静默接收数据直到请求超时（最长数百秒）
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
        # 后台任务终态实时通知（worker 线程内，回调自行切回 UI 线程）
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
                # 子代理结果过长时截断，避免完整大段内容进入主代理上下文浪费 token。
                max_len = SUBAGENT_RESULT_MAX_CHARS
                if len(result) > max_len:
                    omitted = len(result) - max_len
                    result = (
                        result[:max_len]
                        + f"\n\n... [子代理结果已截断，省略 {omitted} 字符]"
                    )
                return result
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
                    "error": task.error,
                }, ensure_ascii=False)
            # 失败/超时/取消：返回结构化文本，让主 agent 能快速理解决策下一步
            # 不用 JSON 是因为终端环境可读性差，主 agent 直接读文本即可
            return self._format_failure_text(task)

    def _format_failure_text(self, task: SubagentTask) -> str:
        """格式化子代理失败结果为结构化文本，供主 agent 快速决策。

        包含：状态、失败原因、失败的工具清单、耗时、下一步建议。
        主 agent 看到后应直接用自身工具兜底，不要盲目重试同一子 agent。
        """
        status_label = {
            "failed": "失败",
            "timed_out": "超时",
            "cancelled": "已取消",
        }.get(task.status, task.status)

        # 失败工具清单（从 tool_errors 收集）
        tool_errors = dict(task.tool_errors) if task.tool_errors else {}
        tool_lines = []
        if tool_errors:
            for name, detail in tool_errors.items():
                # 截断过长的单工具错误详情，避免一个工具的堆栈淹没其他信息
                detail_text = str(detail)
                if len(detail_text) > 200:
                    detail_text = detail_text[:200] + "..."
                tool_lines.append(f"  - {name}: {detail_text}")

        # 错误主因
        error_main = str(task.error or "").strip()
        if not error_main and not tool_lines:
            error_main = "无具体错误信息"

        # 下一步建议（根据失败类型给不同指引）
        hints = []
        if task.status == "timed_out":
            hints.append("子代理超时，主 Agent 应直接用自身工具完成该任务，或拆分为更小的子任务")
        elif task.status == "cancelled":
            hints.append("子代理被取消（可能是父轮次结束），如仍需执行请重新发起")
        elif tool_lines:
            hints.append("子代理工具调用失败，主 Agent 应分析失败工具的原因并用自身工具兜底")
            # 针对常见联网工具失败给具体建议
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
        parts.append(f"耗时: 执行 {round(task.execution_elapsed, 1)}s / 等待审批 {round(task.approval_wait_elapsed, 1)}s")
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
        """判断指定计划项是否有仍在运行（非终态）的子代理任务。

        用于 turn 结束清理逻辑：避免把后台子代理正在执行的 in_progress 计划项
        误判为"子 agent 异常退出"而标记为 failed。
        只关心 plan_item_id 关联的任务是否还在运行，不区分前台/后台。
        """
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
        """判断执行租约是否仍然有效：任务存在且未进入终态。

        超时/取消/失败后租约立即失效：ToolPipeline 据此 fencing 旧子代理的
        延迟工具调用（permission_owner=该 task_id 的调用在权限询问与 adapter
        之前被取消），防止已由主 Agent 接管的计划项被旧执行者继续改写。
        """
        with self._lock:
            task = self._tasks.get(str(task_id or ""))
            return (
                task is not None
                and task.status not in _SUBAGENT_TERMINAL_STATUSES
                and not task.cancel_event.is_set()
            )

    def has_running_background_tasks(self, parent_session: str) -> bool:
        """检查指定会话是否仍有运行中（非终态）的后台子代理任务。

        用于自动汇总触发：当所有后台任务都进入终态时，主代理应自动汇总结果。
        """
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

    def has_unconsumed_terminal_background_tasks(self, parent_session: str) -> bool:
        """检查指定会话是否有未消费的终态后台子代理任务（成功+失败都算）。

        用于决定是否在 turn 开始时注入汇总上下文。
        """
        if not parent_session:
            return False
        with self._lock:
            for task in self._tasks.values():
                if (
                    task.run_in_background
                    and task.parent_session == parent_session
                    and task.status in _SUBAGENT_TERMINAL_STATUSES
                    and task.done_event.is_set()
                    and not task.notification_consumed
                ):
                    return True
            return False

    def drain_terminal_background_tasks(self, parent_session: str) -> list[SubagentTaskSnapshot]:
        """提取本会话中所有终态（completed/failed/timed_out/cancelled）且尚未注入汇总上下文的后台子代理任务。

        用于主代理汇总/兜底机制：在汇总 turn 开始时调用，把所有终态后台任务（成功+失败）
        的结果摘要注入到主代理上下文，让主代理能汇总成功结果并用自身工具接手失败任务。
        调用后会标记这些任务为 summary_injected=True，避免重复注入。
        注意：使用独立的 summary_injected 标志，与 drain_notifications 的 notification_consumed
        互不干扰——即使 drain_notifications 已经消费过通知，汇总 turn 仍能看到任务结果。
        """
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
        """取消指定子代理任务（协作式取消，非强制 kill）。

        用于 /tasks kill <ID> 命令，让用户可以终止卡住的后台子代理。
        与 cancel_foreground 不同，这里按精确 task_id 取消单个任务，
        不区分前台/后台，只对非终态任务生效。
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status in _SUBAGENT_TERMINAL_STATUSES:
                return False
        return self._terminate(task_id, "cancelled", reason)

    def task_snapshot(self, task_id: str) -> SubagentTaskSnapshot | None:
        """返回指定任务当前快照；任务不存在（含已被容量清理）返回 None。

        供 manage_task(status) 桥接查询，终态任务在其被清理前仍可查。
        与既有 snapshot()（全量列表）区分：本方法按 task_id 精确查单个任务。
        """
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
        """等待任务进入终态或超时，返回快照。

        任务不存在返回 None；等待期间任务被容量清理（清理者必为终态）则返回
        最后一次快照；超时返回当前快照（是否终态由调用方检查，超时不是错误）；
        cancel_event 置位抛 KeyboardInterrupt（不伪装超时）。

        done_event 是终态提交的唯一屏障（_complete/_terminate 均最后设置），
        锁内取快照与事件引用、锁外等待，与 task_manager.wait_task_change 同构。
        """
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
        )

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
        summary = f"子任务「{task.description[:40]}」{task.status}"
        return f"""<task-notification>
  <task-id>{escape(task.task_id)}</task-id>
  <status>{escape(task.status)}</status>
  <summary>{escape(summary)}</summary>
  <result>{escape(task.result)}</result>
  <error>{escape(task.error)}</error>
</task-notification>"""

    def _run_default_subagent(self, task: SubagentTask) -> str:
        from core.llm import chat_with_model_events

        custom = task.custom_config
        # 决定使用的模型：
        # - 自定义子 agent model=secondary → 副模型（失败回退 fallback）
        # - 自定义子 agent model=inherit → 主模型（失败回退 fallback）
        # - 其他 → task.model_config（启动时快照）或 main 全局
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
        # 各角色何时使用的简要说明，注入主 Agent 系统提示词用于自动委派
        role_when_to_use = {
            "explore": "只读探索代码库、查找文件、搜索内容、回答代码相关问题时使用，预期会超过 3 次工具调用的探索任务优先用此类型。",
            "implement": "需要修改代码、实现功能、修复 bug 时使用，会读写文件和执行命令。",
            "test": "需要运行测试、定位失败根因、验证实现时使用，可执行命令但不修改业务代码。",
            "review": "需要审查代码质量、安全性、可维护性时使用，只读不修改。",
            "general": "独立完成综合性任务时使用，按需调用所有可用工具。",
        }
        if custom:
            # 自定义子 agent：用 base_type 决定 role，用 system_prompt 覆盖提示词
            role = custom.get("base_type", "general")
            if role not in role_instructions:
                role = "general"
            custom_system_prompt = custom.get("system_prompt") or ""
            custom_allowed_tools = custom.get("allowed_tools")  # None 或 set
        else:
            role = task.subagent_type if task.subagent_type in role_instructions else "general"
            custom_system_prompt = ""
            custom_allowed_tools = None

        default_allowed_tools = {
            "explore": {
                "read_file", "list_directory", "search_code", "glob_files",
                "search_news", "fetch_url", "get_weather",
            },
            "implement": {
                "read_file", "list_directory", "search_code", "glob_files",
                "search_news", "fetch_url", "get_weather", "write_file",
                "edit_file", "execute_command",
            },
            "test": {
                "read_file", "list_directory", "search_code", "glob_files",
                "execute_command",
            },
            "review": {
                "read_file", "list_directory", "search_code", "glob_files",
                "search_news", "fetch_url", "get_weather",
            },
            "general": None,
        }
        allowed_tools = (
            custom_allowed_tools if custom_allowed_tools is not None
            else default_allowed_tools[role]
        )
        # 自定义子 agent 的系统提示词优先；未配置时用内置 role_instructions
        instruction_text = custom_system_prompt if custom_system_prompt else role_instructions[role]
        messages = [
            {
                "role": "system",
                "content": (
                    f"你是星瑶子代理({task.task_id})，角色是 {role}。"
                    f"{instruction_text}"
                    "你只执行主 Agent 已分配的单一子任务，不得拆解、创建或修改主任务计划；"
                    "如实使用可用工具；结束时只输出供主 Agent 汇总的公开结论。"
                    "【速度要求】子任务必须快速收敛：优先用 search_code / glob_files / "
                    "list_directory 定位关键位置后精读相关片段，不要逐文件全文通读；"
                    "信息足以支撑结论时立即停止探索并输出最终结论，不要追求穷尽。"
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
        )
        answer = []
        tool_errors: dict[str, str] = {}
        # 进入子代理深度作用域：在此作用域内 _get_current_subagent_depth() 返回新深度。
        # 必须把事件消费循环包在作用域内，因为生成器创建时并不执行子代理逻辑；
        # 若只包生成器创建语句，深度会在真正执行前复位，嵌套深度限制将永久失效。
        with _subagent_depth_scope():
            for event in events:
                if event.kind == "thinking_started":
                    self._record_stage(task.task_id, "thinking")
                elif event.kind == "tool_started":
                    self._record_stage(
                        task.task_id,
                        "tool",
                        str(event.payload.get("name", "")),
                    )
                    # 结果只保留最后一次工具调用之后的文本。工具调用前的过程叙述
                    # （"我先给你一版初步结论，继续补读…"）不是最终结论，拼进结果
                    # 会让主 Agent 把中间思路当成事实汇总（软件识别会话的真实事故）。
                    answer.clear()
                elif event.kind == "permission_requested":
                    self._record_stage(task.task_id, "waiting_approval")
                elif event.kind == "permission_resolved":
                    self._record_stage(task.task_id, "running", "approval resolved")
                elif event.kind == "assistant_delta":
                    self._record_stage(task.task_id, "composing")
                    answer.append(str(event.payload.get("text", "")))
                elif event.kind == "tool_finished":
                    name = str(event.payload.get("name", "tool"))
                    if event.payload.get("ok") is False:
                        tool_errors[name] = str(
                            event.payload.get("summary")
                            or event.payload.get("error")
                            or "工具执行失败"
                        )
                    else:
                        tool_errors.pop(name, None)
                elif event.kind == "diff_ready":
                    # 子代理修改文件产生的 diff 必须转发回父回合，否则父回合的
                    # "文件更改"面板会丢失子代理的改动。tool_call_id 是子代理
                    # 内部的调用 id，父端匹配不到会归入 legacy_diffs 兜底，
                    # 仍能汇总展示。后台子代理在父回合结束后才转发时，父端
                    # 事件流已收尾，转发为 best-effort（失败不影响主流程）。
                    if task.event_callback is not None:
                        try:
                            task.event_callback("diff_ready", dict(event.payload))
                        except Exception:
                            logger.warning("转发子代理 diff_ready 事件失败", exc_info=True)
        task.tool_errors.update(tool_errors)
        result_text = "".join(answer)

        # summary_continuation：结果过短或为空时追加要求展开，避免子代理敷衍
        # 参考 Kimi Code 的 SUMMARY_MIN_LENGTH 机制。
        # 为空也要触发：最后一次工具调用之后模型可能没再输出文本（结果提取
        # 只保留末次工具调用后的内容），此时必须补一轮总结才有可交付结论。
        if len(result_text.strip()) < 200 and not task.cancel_event.is_set():
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
                        f"你是星瑶子代理({task.task_id})，角色是 {role}。"
                        f"{instruction_text}"
                        "你只执行主 Agent 已分配的单一子任务；"
                        "结束时只输出供主 Agent 汇总的公开结论。"
                        "所有输出必须使用中文。"
                    ),
                },
                {"role": "user", "content": f"请完成以下子任务：\n{task.description}"},
                # 结果为空时用占位文本，空 assistant content 会被部分 API 拒绝（400）
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
                    allowed_tools=None,  # 总结阶段不需要工具
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
    "explore": 240,
    "review": 300,
    "test": 480,
    "implement": 600,
    "general": 600,
}


def make_authorized_verification_runner(
    *,
    permission_owner,
    parent_turn_id,
    permission_source,
    cancel_event,
    event_callback=None,
):
    """构造计划项验证命令执行器：先经本回合授权，再真实执行并返回 (passed, detail)。

    run_subagent（子代理 completed 路径）与 plan_finish（主 Agent 接管收尾）
    共用同一套验证逻辑，不得复制两套。
    """
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
                # 与旧 _run_verification 一致：验证命令（如跑测试套件）
                # 耗时常超过 execute_command 默认的 30 秒，保持 120 秒上限
                timeout_seconds=120,
                cancel_event=cancel_event,
                _execution_result=execution_result,
            )
            or ""
        ).strip()
        return execution_result.returncode == 0, detail or "验证通过"

    return authorized_verification_runner


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
    """运行一个隔离的子代理，返回前台结果或后台任务 ID。

    subagent_type 可选：
    - 内置类型：explore（只读探索）/ implement（修改代码）/ test（运行测试）/ review（只读审查）/ general（综合）
    - 自定义类型：在 ~/.ycode/agents/*.md 或 <cwd>/.ycode/agents/*.md 中定义的子 agent name
      自定义子 agent 加载失败时自动回退为 general 类型执行，不影响主流程。
    """
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
        if not run_in_background and timeout_seconds > 600:
            # 前台硬上限（内部调用同样受限）：模型不得把前台子 Agent 超时扩大到
            # 2400/3600 秒；长命令应使用后台任务 + manage_task(wait) 等待。
            return json.dumps({
                "status": "failed",
                "plan_item_id": str(plan_item_id or ""),
                "error": (
                    "前台子代理 timeout_seconds 硬上限为 600 秒（当前 {} 秒）。"
                    "长任务请拆分为更小的计划项，或改用 run_in_background=true "
                    "启动后台任务并用 manage_task(action='wait') 等待结果。"
                ).format(timeout_seconds),
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
                    # GUI/CLI 接管可见性：真实调度状态唯一来源仍是
                    # PlanManager/TaskScheduler，本事件只做状态展示
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
            # 优先透传真实失败原因（如验证命令的 stderr）。complete() 验证失败时已把
            # 计划项标记为 failed 并写入 error，若先判断状态只会得到"不是 in_progress"
            # 的误导信息，主 Agent 看不到根因就会陷入 retry 死循环。
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
    # 只读角色（explore/review）默认更短：子代理设计目标是"快"，
    # 10 分钟级别的探索/审查违背设计初衷（自定义类型回落到 600 秒）。
    if timeout_seconds is None:
        timeout_seconds = _SUBAGENT_DEFAULT_TIMEOUTS.get(str(subagent_type or "general"), 600)

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
        )
    except Exception as error:
        # 启动失败（如容量满）时回滚计划项，释放 _in_flight_files 占用
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
    """
    在后台发起一次梦境归类整理，将日记账中的记忆提炼合并，并更新 MEMORY.md 索引。
    """
    from core.memory_kairos import run_dream_consolidation_sync
    task_model, task_key, task_url, _task_model_id = _unpack_model_config(
        _active_model_tuple_or_none()
    )

    if not task_key:
        return "🚨 整理失败：大模型密钥尚未配置，无法提炼记忆。"
        
    return run_dream_consolidation_sync(task_model, task_key, task_url)

def glob_files(pattern, dir_path=".", cancel_event=None):
    """快速文件模式匹配：使用 glob 表达式在项目目录中搜索匹配的文件路径列表。优先尝试 ripgrep。"""
    import glob
    import subprocess
    from pathlib import Path

    try:
        # 与 read_file 等工具一致，强制限定在当前工作目录内
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
        # 与 search_code 一致：统一排除构建产物目录（非 git 目录下 rg 无 .gitignore 可依）
        for _d in sorted(_get_search_exclude_dirs()):
            cmd += ["--glob", f"!**/{_d}/"]
        cmd.append(str(base))
        # 与 search_code 统一使用 SEARCH_CODE_TIMEOUT_SECONDS（原先硬编码 15s 不一致），
        # 且用可取消轮询版本响应 cancel_event
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
            # rg 正常跑完但没有匹配：直接返回结论，
            # 不再落到 Python glob 把同一棵目录树重扫一遍
            res = f"未找到匹配 `{pattern}` 的文件。"
            _tool_cache.glob_put(cache_key, res)
            if session_id:
                _tool_cache.shared_put(session_id, ("glob_files", cache_key), res)
            return res
        elif result.returncode != 0:
            # rg 返回真实错误（如 glob 语法错误），记录诊断日志而不是静默吞掉
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
