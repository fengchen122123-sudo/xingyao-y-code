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
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

from ddgs import DDGS

from core.version import __version__ as YCODE_VERSION
from core import tool_cache as _tool_cache
from core.lsp import _get_rg_executable


HTTP_TIMEOUT_SECONDS = 20
HTTP_MAX_BYTES = 512 * 1024
HTTP_USER_AGENT = f"ycode/{YCODE_VERSION} (terminal coding assistant)"
READ_FILE_MAX_LINES = 2000
# 大文件阈值：超过此字节数的文件，未显式指定 limit 时自动只读前部分并提示分页
READ_FILE_LARGE_BYTES = 50 * 1024
READ_FILE_LARGE_DEFAULT_LINES = 500

_logger = logging.getLogger(__name__)


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


# 线程内临时放行（用户批准的单次沙盒外操作）与会话级放行（用户选“始终放行”）
_sandbox_tls = threading.local()
_session_sandbox_bypass = False
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


def is_outside_workspace(path_value):
    """判断路径是否在当前工作目录之外（resolve 后比较，符号链接/.. 均可识别）。"""
    candidate = Path(str(path_value or "")).expanduser().resolve()
    workspace = Path.cwd().resolve()
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


def set_session_sandbox_bypass(enabled):
    """会话级沙盒放行（用户在沙盒外访问确认中选择“始终放行”时置位）。"""
    global _session_sandbox_bypass
    with _session_sandbox_lock:
        _session_sandbox_bypass = bool(enabled)


def _outside_workspace_allowed():
    """读取运行时状态：是否允许访问工作目录外的文件。

    依次为：线程内单次放行 > 会话级放行 > 配置开关（/sandbox on）。
    开放后写操作仍需权限确认（write_file/edit_file 的权限流程不变）。
    """
    if getattr(_sandbox_tls, "depth", 0) > 0:
        return True
    with _session_sandbox_lock:
        if _session_sandbox_bypass:
            return True
    try:
        from core.model_manager import load_config
        return bool(load_config().get("allow_outside_workspace", False))
    except Exception:
        return False


def _resolve_workspace_path(file_path):
    """将路径解析为绝对路径；默认强制限定在当前工作目录内，越界直接拒绝。

    当配置 allow_outside_workspace 为 True（/sandbox on）时解除目录限制。
    """
    candidate = Path(str(file_path or "")).expanduser().resolve()
    if _outside_workspace_allowed():
        return candidate
    workspace = Path.cwd().resolve()
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


def read_file(file_path, offset=0, limit=None, compress=False):
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
    """
    resolved = _resolve_workspace_path(file_path)

    cache_key = _tool_cache.read_file_key(str(resolved), offset, limit, compress)
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

    # 压缩处理
    compressed_note = ""
    if compress and result:
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

    # 大文件覆盖保护：强制使用 edit_file 做片段修改
    if resolved.exists() and resolved.is_file():
        existing_size = resolved.stat().st_size
        existing_lines = 0
        try:
            existing_lines = len(resolved.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            pass
        if existing_size > 50 * 1024 or existing_lines > 500:
            return (
                f"🚨 write_file 拒绝覆盖已有大文件（{file_path}，约 {existing_lines} 行 / {existing_size / 1024:.1f}KB）。\n"
                f"为保护未改动代码并避免 diff 全红，请改用 edit_file 对目标片段进行局部修改；"
                f"只有确定要完整重写整个文件时才告知用户此限制。"
            )

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
        os.replace(temp_name, resolved)
    except BaseException:
        # 使用 BaseException 确保 KeyboardInterrupt、MemoryError 等非 OSError 异常
        # 同样能触发临时文件清理，避免孤儿 .ycode-tmp 文件在磁盘积累。
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return f"✔ 文件 {file_path} 写入成功！"

# --- 下面是给星瑶装配的两个新神装 ---

def list_directory(dir_path=".", max_entries=200):
    """雷达工具：扫描文件夹，返回里面的所有文件和文件夹列表（仅限当前工作目录内）"""
    try:
        resolved = _resolve_workspace_path(dir_path)
        if not os.path.exists(resolved):
            return f"🚨 目录扫描失败：路径不存在 '{dir_path}'"
        if os.path.isfile(resolved):
            return f"🚨 目录扫描失败：'{dir_path}' 是一个文件而不是目录。若要查看文件内容，请使用 read_file 工具。"
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

def execute_command(command, run_in_background=False, timeout_seconds=30, cancel_event=None):
    """引擎工具：在本地终端默默执行命令，并把终端里的输出抓取回来"""
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
            task_id = start_background_task(command)
            return (
                f"✔ 异步后台任务已成功启动！\n"
                f"任务 ID: {task_id}\n"
                f"命令内容: {command}\n"
                f"系统已经把输出重定向到了日志中。你可以随时调用 `manage_task(action='status', task_id='{task_id}')` 来查看运行日志尾部，"
                f"或者调用 `manage_task(action='kill', task_id='{task_id}')` 终结此后台服务。"
            )
        except Exception as e:
            return f"🚨 启动后台异步任务失败: {e}"

    try:
        from core.process_io import decode_process_output, platform_console_encoding
        # 复用调度器的进程树终止逻辑，避免 subprocess.run(timeout=...) 超时只杀
        # 直接子进程（cmd.exe）而泄露孙进程
        from core.task_scheduler import _stop_process_tree

        def _close_process_pipes(proc):
            """显式关闭子进程的 stdout/stderr 管道，防止 fd 泄漏。"""
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

        output_encoding = platform_console_encoding()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = output_encoding
        env["PYTHONUTF8"] = "0"

        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=env,
        )
        start_time = time.time()
        stdout_raw = b""
        stderr_raw = b""
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _stop_process_tree(process)
                # 二次 communicate 加硬超时，防止孙进程 daemon 化脱离进程树后
                # pipe 永不关闭导致 communicate() 无限阻塞卡死 worker 线程；
                # 超时后显式关闭管道释放 fd，避免资源泄漏
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                _close_process_pipes(process)
                return "🛑 命令已被用户强行中断终止。"
            try:
                stdout_raw, stderr_raw = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if time.time() - start_time >= timeout_seconds:
                    _stop_process_tree(process)
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    _close_process_pipes(process)
                    return f"🚨 命令执行超时（超过 {timeout_seconds} 秒），已被强行终止！"

        output = decode_process_output(stdout_raw, output_encoding)
        if stderr_raw:
            stderr = decode_process_output(stderr_raw, output_encoding)
            output += f"\n[附加信息/警告]:\n{stderr}"

        # 必须如实汇报退出码：此前非零退出但只要无输出就返回"执行成功"，
        # 调用方（尤其子代理）无法区分成败，会把失败命令当成通过。
        if process.returncode:
            detail = output.strip() or "（命令没有任何输出）"
            return f"🚨 命令执行失败（退出码 {process.returncode}）。\n{detail}"
        return output or "执行成功，但命令没有返回任何输出内容。"

    except Exception as e:
        return f"🚨 引擎执行遇到未知错误: {str(e)}"

# --- 星瑶的新神装：新闻搜索 ---

def search_news(query, num_results=5):
    """🌐 现代化新闻搜索工具：稳定可靠的联网引擎"""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.news(query, max_results=num_results))

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

    except Exception as e:
        return f"🚨 新闻搜索工具发生异常: {str(e)}"


class _ResponseTooLargeError(ValueError):
    pass


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
    """读取 config.json 的 fetch_url_strict_ssrf 开关（默认 False）。

    默认宽松模式（对齐 kimicode/opencode）：只拦截云元数据接口，
    不做 DNS 解析后的 IP 校验。这样不会误杀 Clash/V2Ray 等代理工具的
    fake-ip 段（如 198.18.0.0/15，被 Python ipaddress 判为 is_private）。

    严格模式（True）：保留完整 IP 校验，适用于企业部署、多租户、
    不信任用户输入的场景。
    """
    try:
        import config
        data = config._read_json_with_backup(config._CONFIG_FILE, default=None)
        if not isinstance(data, dict):
            return False
        return bool(data.get("fetch_url_strict_ssrf", False))
    except Exception:
        return False


def _check_ssrf_url(url):
    """SSRF 防护：校验 URL 的主机名是否为安全目标。

    双模式设计（参考 kimicode/opencode 的做法，它们默认不做 IP 校验）：

    默认宽松模式（fetch_url_strict_ssrf=False，对齐 kimicode/opencode）：
      1. 主机名在显式白名单中 → 放行
      2. 主机名在云元数据黑名单中 → 拒绝（核心防护，始终保留）
      3. 其他一律放行，不做 DNS 解析后的 IP 校验
      这样不会误杀 Clash/V2Ray 等代理工具的 fake-ip 段（198.18.0.0/15 等），
      也不会因为 CDN 双栈中一个 IPv6 解析到保留段就全拒。

    严格模式（fetch_url_strict_ssrf=True）：
      在默认模式基础上增加：
      4. 如果 host 是 IP 字面量，校验是否公网
      5. 否则 DNS 解析所有 A/AAAA 记录，任一非公网即拒绝
      6. DNS 解析失败 → 拒绝（保守策略）
      适用于企业部署、多租户、不信任用户输入的场景。
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


def _request_http_text(url, timeout_seconds=HTTP_TIMEOUT_SECONDS):
    normalized_url = _validate_http_url(url)
    # SSRF 防护：在发起请求前校验主机与解析后的 IP
    # 即使 LLM 拼接了内网/元数据接口 URL，也会在此被拦截
    _check_ssrf_url(normalized_url)
    timeout = float(timeout_seconds)
    if not 1 <= timeout <= 60:
        raise ValueError("timeout_seconds 必须在 1 到 60 秒之间")
    request = Request(
        normalized_url,
        headers={
            "User-Agent": HTTP_USER_AGENT,
            "Accept": "application/json, text/plain, text/html;q=0.9, */*;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(HTTP_MAX_BYTES + 1)
        if len(raw) > HTTP_MAX_BYTES:
            raise _ResponseTooLargeError(
                f"响应超过 {HTTP_MAX_BYTES // 1024} KB 限制，已停止读取"
            )
        return _decode_http_response(response, raw)


def _format_http_error(error):
    if isinstance(error, HTTPError):
        return f"HTTP {error.code} {error.reason}"
    if isinstance(error, URLError):
        return f"网络连接失败：{error.reason}"
    return str(error)


def fetch_url(url, timeout_seconds=HTTP_TIMEOUT_SECONDS):
    """通过本机 HTTP 客户端获取网页或 JSON 文本。"""
    try:
        status, content_type, text = _request_http_text(url, timeout_seconds)
        header = f"HTTP {status}"
        if content_type:
            header += f" ({content_type})"
        return f"{header}\n{text}"
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
                "\"fetch_url_strict_ssrf\": false 关闭严格 IP 校验"
                "（默认已是 false，若曾开启请关闭）"
            )
        return f"🚨 {msg}\n" + "\n".join(hint_lines)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return f"🚨 FetchURL 请求失败：{_format_http_error(exc)}"
    except Exception as exc:
        return f"🚨 FetchURL 请求发生异常：{exc}"


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


def get_weather(city="", timeout_seconds=HTTP_TIMEOUT_SECONDS):
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
        _, _, geocoding_text = _request_http_text(geocoding_url, timeout_seconds)
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
        status, _, forecast_text = _request_http_text(forecast_url, timeout_seconds)
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
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return f"🚨 天气查询失败：{_format_http_error(exc)}"
    except Exception as exc:
        return f"🚨 天气查询发生异常：{exc}"
    
def search_code(query, dir_path="."):
    """精准搜索：跨文件全局查找代码片段或关键字。优先尝试 ripgrep。"""
    import subprocess
    try:
        # 与 read_file 等工具一致，强制限定在当前工作目录内
        dir_path = str(_resolve_workspace_path(dir_path))
    except ValueError as e:
        return f"🚨 搜索失败: {e}"

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

    try:
        rg_executable = _get_rg_executable()
        cmd = [rg_executable, "-n", "-i", "--no-heading", "--max-columns", "200", "-C", "1", query, dir_path]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        if result.returncode in (0, 1):
            output = result.stdout.strip()
            if not output:
                result_text = "未找到匹配的代码片段。"
            else:
                lines = output.split("\n")
                if len(lines) > 60:
                    result_text = "\n".join(lines[:60]) + f"\n\n... (匹配结果过多，已截断，共 {len(lines)} 行)"
                else:
                    result_text = output
            _tool_cache.search_put(cache_key, result_text)
            if session_id:
                _tool_cache.shared_put(session_id, ("search_code", cache_key), result_text)
            return result_text
    except Exception:
        pass

    IGNORE_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.idea', '.vscode', '.ycode', 'dist', 'build'}
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

def manage_task(action, task_id=None, event_callback=None, permission_owner=None, cancel_event=None):
    """
    后台进程管理器：对丢在后台运行的长周期任务（如服务器）进行查杀和状态管理。
    参数:
      action: 可选值：'list'（查看所有运行中的后台任务）, 'status'（查看指定任务的实时日志尾部）, 'kill'（强制杀掉指定任务）。
      task_id: 目标任务的 ID，如果是 'status' 或 'kill' 行动，则为必填项。
      event_callback/permission_owner/cancel_event: 调度层透传的权限上下文（不暴露给模型），
        子代理线程内调用 kill 时必须传入，缺省路径仅在主线程上下文可用。
    """
    from core.task_manager import list_active_tasks, get_task_output, kill_background_task
    
    if action == "list":
        tasks = list_active_tasks()
        if not tasks:
            return "ℹ 当前没有任何运行中的后台异步任务。"
        res = ["当前活跃后台任务列表："]
        for t in tasks:
            res.append(f"- [任务 ID: {t['task_id']}] 运行 PID: {t['pid']} | 运行时间: {t['duration']} | 命令: {t['command']}")
        return "\n".join(res)
        
    elif action == "status":
        if not task_id:
            return "🚨 错误：查看状态必须提供 task_id 参数。"
        output = get_task_output(task_id, tail_lines=40)
        return f"--- 任务 {task_id} 最新 40 行日志输出 ---\n{output}"
        
    elif action == "kill":
        if not task_id:
            return "🚨 错误：终结任务必须提供 task_id 参数。"
        # 只在 kill 类操作时要求用户确认，list/status 只读操作不弹确认
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
        success = kill_background_task(task_id)
        if success:
            return f"✔ 后台任务 {task_id} 已被强行终结清除。"
        else:
            return f"🚨 未能成功杀掉任务 {task_id}（可能进程已经提前结束，或找不到 ID）。"
            
    else:
        return "🚨 错误：未知行动参数。可选值有 'list', 'status', 'kill'。"

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


def edit_file(file_path, target_content, replacement_content, replace_all=False):
    """
    手术刀局部替换工具：用于对已有代码文件进行点对点局部精准替换，不用每次都重写整张文件，避免开销和误删。
    参数:
      file_path: 要修改的文件的相对或绝对路径（仅限当前工作目录内）。
      target_content: 文件中要被替换的旧代码段（尽量保持原始缩进和换行；允许轻微差异，系统会尝试智能定位）。
      replacement_content: 准备换上的新代码段。
      replace_all: 是否替换全部出现位置（默认 False，旧代码段出现多次时必须显式开启）。
    """
    try:
        resolved = _resolve_workspace_path(file_path)
    except ValueError as e:
        return f"🚨 精准修改失败！{e}"
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

    target = str(target_content or "")
    replacement = str(replacement_content or "")
    if not target:
        return "🚨 精准修改失败！target_content 不能为空。"

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

    # 校验全部通过后才写入撤销栈，避免失败的编辑污染撤销历史
    from core.memory import set_undo_state
    set_undo_state(file_path, content)

    # 原子写入：先写同目录临时文件再 os.replace，避免写入中断留下半个文件；
    # 用 tempfile.mkstemp 生成唯一临时文件名，避免并发编辑同一文件时互相覆盖
    # 自动创建父目录（防御性：与 write_file 对齐，虽然 edit_file 通常改的是已存在文件）
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
        os.replace(temp_path, resolved)
    except BaseException as e:
        # 使用 BaseException 确保 UnicodeEncodeError、KeyboardInterrupt 等非 OSError 异常
        # 同样能触发临时文件清理，避免孤儿 .ycode-tmp 文件在磁盘积累（与 write_file 对齐）
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return f"🚨 精准修改发生异常: {str(e)}"

    return f"✔ 文件 {file_path} 精准替换修改成功！"

_SUBAGENT_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "timed_out",
    "cancelled",
}


def _has_meaningful_subagent_result(text: str) -> bool:
    return bool(re.sub(r"[\s`*_#>~\-\[\](){}|.,!?，。！？:：;；/\\]+", "", text or ""))


_ADVISOR_TAG_RE = re.compile(
    r"\[(?:Advisor consultation[^\]]*|Advisor review|End of advisor consultation[^\]]*)\]"
)


def _strip_advisor_tags(text: str) -> str:
    """剔除模型泄漏到正文里的 Advisor 内部协议标记，避免污染子代理结果与计划回写。"""
    return _ADVISOR_TAG_RE.sub("", text or "").strip()


_SUBAGENT_TIMEOUT_HINT = (
    "（子代理可能已产出部分结果，例如文件已写入；"
    "请先用 read_file/list_directory 确认工作成果，再决定 plan_retry 重试或调整任务，"
    "避免重复执行已完成的工作）"
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
    terminal_callback: Callable | None = field(default=None, repr=False)
    callback_invoked: bool = field(default=False, repr=False)
    model_config: tuple | None = field(default=None, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    custom_config: dict | None = field(default=None, repr=False)


SubagentRunner = Callable[[SubagentTask], str]

# 后台子代理终态记录未被父会话 drain 时的存活时间上限（秒），超时后可被容量清理回收
_BACKGROUND_NOTIFICATION_TTL_SECONDS = 600


def _snapshot_model_config(prefer_secondary: bool = True) -> tuple | None:
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
            )
    except Exception:
        pass
    try:
        import main
        return (main.current_model, main.current_key, main.current_url)
    except Exception:
        return None


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
    ) -> str:
        """运行一个子代理任务。

        注意：超时是协作式取消——超时/取消只设置 cancel_event 并记录终态，
        子代理线程需要响应 cancel_event 才会真正退出；前后台任务都受同一超时监管。
        """
        if not 30 <= timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be between 30 and 3600")
        description = str(task_description or "").strip()
        if not description:
            raise ValueError("task_description must not be empty")
        if model_config is None and self._uses_default_runner:
            model_config = _snapshot_model_config()

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

        task = SubagentTask(
            task_id=f"subagent-{uuid.uuid4().hex[:8]}",
            parent_session=str(parent_session or ""),
            parent_turn_id=str(parent_turn_id or ""),
            description=description,
            subagent_type=normalized_type,
            plan_item_id=str(plan_item_id or ""),
            run_in_background=bool(run_in_background),
            terminal_callback=terminal_callback,
            model_config=model_config,
            custom_config=custom_config,
        )
        with self._lock:
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
        while not task.done_event.wait(self._wait_interval):
            if parent_cancel_event is not None and parent_cancel_event.is_set():
                self._terminate(task.task_id, "cancelled", "parent turn cancelled")
                break

            now = self._monotonic()
            elapsed = max(0.0, now - last_tick)
            last_tick = now
            waiting_for_approval = self._permission_waiting(task.task_id)
            with self._lock:
                if task.status in _SUBAGENT_TERMINAL_STATUSES:
                    break
                if waiting_for_approval:
                    task.approval_wait_elapsed += elapsed
                    self._record_stage_locked(task, "waiting_approval")
                elif task.callback_invoked:
                    # 终态回调正在执行（可能同步跑验证命令），暂停计时避免误判超时
                    task.approval_wait_elapsed += elapsed
                else:
                    task.execution_elapsed += elapsed
                    if task.stage in {"waiting_approval", "finalizing"}:
                        self._record_stage_locked(
                            task,
                            "running",
                            "approval resolved",
                        )
                execution_elapsed = task.execution_elapsed
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
                if waiting_for_approval or task.callback_invoked:
                    # 等待审批或终态回调执行期间暂停计时，避免误判超时
                    task.approval_wait_elapsed += elapsed
                else:
                    task.execution_elapsed += elapsed
                execution_elapsed = task.execution_elapsed
            if execution_elapsed >= timeout_seconds:
                self._terminate(
                    task.task_id,
                    "timed_out",
                    "background timeout" + _SUBAGENT_TIMEOUT_HINT,
                )
                return

    def _run_task(self, task: SubagentTask) -> None:
        with self._lock:
            if task.status != "pending":
                return
            task.status = "running"
            task.started_at = time.time()
            self._record_stage_locked(task, "running")

        # 子代理线程标记父会话，使工具级共享缓存可被复用
        _tool_cache.set_current_session(task.parent_session)
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
            task.done_event.set()
        # 后台任务终态实时通知（worker 线程内，回调自行切回 UI 线程）
        self._fire_terminal_notification(task)
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
        with self._lock:
            task = self._tasks[task_id]
            if task.status in _SUBAGENT_TERMINAL_STATUSES:
                return False
            if status == "timed_out" and task.callback_invoked:
                # 终态回调正在执行（可能同步跑验证命令），不再因超时二次终止，
                # 避免监管侧 timed_out 与回调侧计划回写状态分裂
                return False
            if invoke_callback and not task.callback_invoked:
                # 锁内原子认领回调，保证 terminal_callback 全局只发一次
                task.callback_invoked = True
                callback = task.terminal_callback
                if callback is not None:
                    # 回调期间标记 finalizing，前台/后台监管循环据此暂停超时计时
                    self._record_stage_locked(task, "finalizing")
        if callback is not None:
            try:
                callback_error = callback(task, status, "", str(error))
                if callback_error:
                    error = f"{error}; {callback_error}"
            except BaseException as callback_exception:
                error = f"{error}; 计划回写失败: {callback_exception}"
        with self._lock:
            task = self._tasks[task_id]
            if task.status in _SUBAGENT_TERMINAL_STATUSES:
                return False
            task.status = status
            task.error = str(error)
            task.finished_at = time.time()
            self._record_stage_locked(task, status, str(error))
            task.cancel_event.set()
            task.done_event.set()
        if status in {"timed_out", "cancelled"}:
            from core.permission_broker import permission_broker
            permission_broker.cancel_owner(task_id)
        # 后台任务终态实时通知（worker 线程内，回调自行切回 UI 线程）
        self._fire_terminal_notification(task)
        return True


    def _foreground_result(self, task_id: str) -> str:
        with self._lock:
            task = self._tasks[task_id]
            if task.status == "completed":
                result = str(task.result or "")
                # 子代理结果过长时截断，避免完整大段内容进入主代理上下文浪费 token。
                max_len = 4000
                if len(result) > max_len:
                    omitted = len(result) - max_len
                    result = (
                        result[:max_len]
                        + f"\n\n... [子代理结果已截断，省略 {omitted} 字符]"
                    )
                return result
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

    def drain_notifications(self, parent_session: str) -> list[str]:
        with self._lock:
            tasks = [
                task
                for task in self._tasks.values()
                if task.run_in_background
                and task.parent_session == parent_session
                and task.status in _SUBAGENT_TERMINAL_STATUSES
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
                task.status not in _SUBAGENT_TERMINAL_STATUSES
                for task in self._tasks.values()
            )

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
                return task.model_config
            try:
                import main
                return (main.current_model, main.current_key, main.current_url)
            except Exception:
                return (None, None, None)

        model_pref = custom.get("model") if custom else None
        if model_pref == "secondary":
            try:
                from core.model_manager import get_secondary_model
                secondary = get_secondary_model()
                if secondary:
                    current_model = secondary["litellm_model"]
                    current_key = secondary["api_key"]
                    current_url = secondary["base_url"]
                else:
                    current_model, current_key, current_url = _fallback_model()
            except Exception:
                current_model, current_key, current_url = _fallback_model()
        elif model_pref == "inherit":
            try:
                import main
                current_model = main.current_model
                current_key = main.current_key
                current_url = main.current_url
            except Exception:
                current_model, current_key, current_url = _fallback_model()
        else:
            current_model, current_key, current_url = _fallback_model()

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
                    "所有输出必须使用中文，包括过程说明与最终结论；"
                    "不要输出 [Advisor consultation] 等内部协议标记。"
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
        task.tool_errors.update(tool_errors)
        result_text = "".join(answer)

        # summary_continuation：结果过短时追加要求展开，避免子代理敷衍
        # 参考 Kimi Code 的 SUMMARY_MIN_LENGTH 机制
        if 0 < len(result_text.strip()) < 200 and not task.cancel_event.is_set():
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
                {"role": "assistant", "content": result_text},
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


def run_subagent(
    task_description,
    run_in_background=False,
    timeout_seconds=600,
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
    if not str(task_description or "").strip():
        return json.dumps({
            "status": "failed",
            "plan_item_id": str(plan_item_id or ""),
            "error": "task_description 不能为空。",
        }, ensure_ascii=False)
    if parent_session is None or parent_turn_id is None:
        import main
        parent_session = parent_session or main.current_session
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
        owner = f"subagent:{subagent_type or 'general'}"
        if not scheduler.start_item(
            str(plan_item_id),
            owner=owner,
            event_callback=event_callback,
        ):
            # 构造详细的失败原因，帮助 LLM 诊断
            actual_status = plan_item.get("status", "unknown") if plan_item else "不存在"
            reason_parts = [f"计划项 '{plan_item_id}' 当前状态为 '{actual_status}'"]
            if plan_item and actual_status in {"failed", "blocked", "cancelled"}:
                reason_parts.append("（需先用 plan_retry(item_id='{0}') 重置为 pending 后重试）".format(plan_item_id))
            if plan_item:
                deps = plan_item.get("depends_on", [])
                if deps:
                    dep_statuses = []
                    for dep_id in deps:
                        dep_item = next((i for i in scheduler.plan.snapshot() if i["id"] == dep_id), None)
                        dep_statuses.append(f"'{dep_id}'({dep_item['status'] if dep_item else '不存在'})")
                    reason_parts.append(f"依赖项状态: {', '.join(dep_statuses)}")
            return json.dumps({
                "status": "blocked",
                "plan_item_id": str(plan_item_id),
                "error": "计划项尚未满足依赖、已暂停或已有冲突任务在执行。" + " ".join(reason_parts),
            }, ensure_ascii=False)

    def terminal_callback(task, status, result, error):
        if scheduler is None or not task.plan_item_id:
            return None
        item_id = task.plan_item_id
        if status == "completed":
            if scheduler.complete(item_id, result, cancel_event=task.cancel_event):
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
    import main
    from core.memory_kairos import run_dream_consolidation_sync
    task_model = main.current_model
    task_key = main.current_key
    task_url = main.current_url
    
    if not task_key:
        return "🚨 整理失败：大模型密钥尚未配置，无法提炼记忆。"
        
    return run_dream_consolidation_sync(task_model, task_key, task_url)

def glob_files(pattern, dir_path="."):
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
        cmd = [rg, "--files", "--glob", pattern, str(base)]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
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
