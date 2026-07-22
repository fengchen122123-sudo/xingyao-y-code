import ipaddress
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


HTTP_TIMEOUT_SECONDS = 20
HTTP_MAX_BYTES = 512 * 1024
HTTP_USER_AGENT = "Y-Code/1.0.19 (terminal coding assistant)"
READ_FILE_MAX_LINES = 2000
# 大文件阈值：超过此字节数的文件，未显式指定 limit 时自动只读前部分并提示分页
READ_FILE_LARGE_BYTES = 50 * 1024
READ_FILE_LARGE_DEFAULT_LINES = 500


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

    # 大文件自适应：未显式指定 limit 时，按文件大小决定默认读取行数
    file_size = resolved.stat().st_size
    auto_limited = False
    if limit is None and file_size > READ_FILE_LARGE_BYTES:
        max_lines = READ_FILE_LARGE_DEFAULT_LINES
        auto_limited = True
    else:
        max_lines = READ_FILE_MAX_LINES if limit is None else max(1, min(int(limit), READ_FILE_MAX_LINES))

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

    return result + compressed_note + truncation_note

def write_file(file_path, content):
    """将内容写入文件（仅限当前工作目录内，统一按 utf-8 编码写入）"""
    resolved = _resolve_workspace_path(file_path)
    # 与 edit_file 对齐：先写同目录临时文件再 os.replace，避免写入中断留下半个文件
    fd, temp_name = tempfile.mkstemp(
        prefix=resolved.name + ".", suffix=".ycode-tmp", dir=str(resolved.parent)
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
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

# --- 下面是给星瑶装配的两个新神装 ---

def list_directory(dir_path="."):
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
                return "🔍 互联网上没有找到相关新闻，换个关键词试试？"
                
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


def _check_ssrf_url(url):
    """SSRF 防护：校验 URL 的主机名与解析后的 IP 是否为安全公网目标。

    策略：
    1. 主机名在显式白名单中 → 直接放行（不查 IP）
    2. 主机名在元数据黑名单中 → 拒绝
    3. DNS 解析所有 A/AAAA 记录，任一 IP 不安全 → 拒绝
    4. 解析失败 → 拒绝（保守策略）
    """
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()

    if not host:
        raise SSRFBlockedError("URL 缺少主机名")

    # 1. 显式白名单优先
    allow_hosts = _get_fetch_url_allow_hosts()
    if host in allow_hosts:
        return

    # 2. 元数据接口黑名单
    if host in _METADATA_HOSTS:
        raise SSRFBlockedError(
            f"已阻止访问云元数据接口 {host}（防止 SSRF 泄露凭据）"
        )

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
        # SSRF 防护拦截：清晰提示用户被阻止的原因，并说明如何加白名单
        return (
            f"🚨 {exc}\n"
            f"如需访问该内网/私有地址，请在 ~/.ycode/config.json 的 "
            f"fetch_url_allow_hosts 数组中显式添加该主机名以 opt-in。"
        )
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
    try:
        cmd = ["rg", "-n", "-i", "--no-heading", "--max-columns", "200", "-C", "1", query, dir_path]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        if result.returncode in (0, 1):
            output = result.stdout.strip()
            if not output:
                return "未找到匹配的代码片段。"
            lines = output.split("\n")
            if len(lines) > 60:
                return "\n".join(lines[:60]) + f"\n\n... (匹配结果过多，已截断，共 {len(lines)} 行)"
            return output
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
                                return "\n---\n".join(results)
                except Exception:
                    continue
                    
        if not results:
            return f"🔍 搜索完毕，未在代码文件中找到包含 `{query}` 的内容。"
        return "\n---\n".join(results)
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
        from core.llm import check_permission_and_ask
        # 注意：缺省（无 event_callback）路径会在调用线程内直接 safe_print 触碰终端，
        # 仅在主线程上下文中可用；子代理/调度层调用时必须透传权限上下文参数。
        if not check_permission_and_ask(
            "manage_task",
            {"action": "kill", "task_id": task_id},
            event_callback=event_callback,
            permission_owner=permission_owner,
            cancel_event=cancel_event,
        ):
            return "🚨 用户拒绝了终结后台任务的操作。"
        success = kill_background_task(task_id)
        if success:
            return f"✔ 后台任务 {task_id} 已被强行终结清除。"
        else:
            return f"🚨 未能成功杀掉任务 {task_id}（可能进程已经提前结束，或找不到 ID）。"
            
    else:
        return "🚨 错误：未知行动参数。可选值有 'list', 'status', 'kill'。"

def edit_file(file_path, target_content, replacement_content, replace_all=False):
    """
    手术刀局部替换工具：用于对已有代码文件进行点对点局部精准替换，不用每次都重写整张文件，避免开销和误删。
    参数:
      file_path: 要修改的文件的相对或绝对路径（仅限当前工作目录内）。
      target_content: 文件中要被替换的旧代码段（必须完全一致且唯一，注意保留原始缩进和换行）。
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

    # 换行策略：优先按原文精确匹配；匹配不到时把目标串转换成文件实际使用的换行风格再匹配，
    # 写回时保持文件原有换行，不做全量换行归一化。
    match_target, match_replacement = target, replacement
    if content.count(match_target) == 0:
        if "\r\n" in content:
            # 先归一（\r\n -> \n）再统一转成文件实际的 CRLF 风格，
            # 避免把 target/replacement 中已有的 \r\n 腐蚀成 \r\r\n
            alt_target = target.replace("\r\n", "\n").replace("\n", "\r\n")
            alt_replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
        elif "\r\n" in target:
            alt_target = target.replace("\r\n", "\n")
            alt_replacement = replacement.replace("\r\n", "\n")
        else:
            alt_target, alt_replacement = match_target, match_replacement
        if alt_target != match_target and content.count(alt_target) > 0:
            match_target, match_replacement = alt_target, alt_replacement

    occurrences = content.count(match_target)
    if occurrences == 0:
        return (
            f"🚨 精准修改失败！在文件 {file_path} 中没有找到指定的旧代码段。\n"
            f"请确保你传入的 target_content 的缩进、空格、换行完全匹配，或者你可以尝试使用 write_file 覆写工具。"
        )
    if occurrences > 1 and not replace_all:
        return (
            f"🚨 精准修改失败！旧代码段在文件 {file_path} 中出现了 {occurrences} 次，无法确定唯一修改位置。\n"
            f"请提供更长的、包含更多上下文的 target_content 以保证唯一匹配；如果确实需要全部替换，请显式设置 replace_all=True。"
        )

    # 校验全部通过后才写入撤销栈，避免失败的编辑污染撤销历史
    from core.memory import set_undo_state
    set_undo_state(file_path, content)

    new_content = (
        content.replace(match_target, match_replacement)
        if replace_all
        else content.replace(match_target, match_replacement, 1)
    )
    # 原子写入：先写同目录临时文件再 os.replace，避免写入中断留下半个文件；
    # 用 tempfile.mkstemp 生成唯一临时文件名，避免并发编辑同一文件时互相覆盖
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


SubagentRunner = Callable[[SubagentTask], str]

# 后台子代理终态记录未被父会话 drain 时的存活时间上限（秒），超时后可被容量清理回收
_BACKGROUND_NOTIFICATION_TTL_SECONDS = 600


def _snapshot_model_config() -> tuple | None:
    """启动时快照当前模型配置，避免子代理运行途中用户切换模型/密钥导致配置混用。"""
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

    def run(
        self,
        task_description: str,
        parent_session: str,
        parent_turn_id: str,
        *,
        run_in_background: bool = False,
        timeout_seconds: int = 120,
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

        task = SubagentTask(
            task_id=f"subagent-{uuid.uuid4().hex[:8]}",
            parent_session=str(parent_session or ""),
            parent_turn_id=str(parent_turn_id or ""),
            description=description,
            subagent_type=str(subagent_type or "general"),
            plan_item_id=str(plan_item_id or ""),
            run_in_background=bool(run_in_background),
            terminal_callback=terminal_callback,
            model_config=model_config,
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
                self._terminate(task.task_id, "timed_out", "foreground timeout")
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
                self._terminate(task.task_id, "timed_out", "background timeout")
                return

    def _run_task(self, task: SubagentTask) -> None:
        with self._lock:
            if task.status != "pending":
                return
            task.status = "running"
            task.started_at = time.time()
            self._record_stage_locked(task, "running")

        try:
            result = str(self._runner(task) or "").strip()
        except BaseException as error:
            if task.cancel_event.is_set():
                self._terminate(task.task_id, "cancelled", "task cancelled")
            else:
                self._terminate(task.task_id, "failed", str(error))
            return

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
        return True

    def _foreground_result(self, task_id: str) -> str:
        with self._lock:
            task = self._tasks[task_id]
            if task.status == "completed":
                return task.result
            return json.dumps({
                "task_id": task.task_id,
                "status": task.status,
                "error": task.error,
                "latest_stage": task.stage,
                "execution_elapsed": round(task.execution_elapsed, 3),
                "approval_wait_elapsed": round(task.approval_wait_elapsed, 3),
            }, ensure_ascii=False)

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

        if task.model_config is not None:
            # 使用启动时快照的模型配置，避免运行途中切换模型/密钥
            current_model, current_key, current_url = task.model_config
        else:
            # 兼容直接构造 SubagentTask 的旧用法：回退到读取 main 全局
            import main
            current_model, current_key, current_url = (
                main.current_model,
                main.current_key,
                main.current_url,
            )

        role_instructions = {
            "explore": "只读探索代码和资料，不修改文件，不执行有副作用命令。",
            "implement": "负责按任务修改代码，修改前先读取相关文件，完成后说明改动和验证。",
            "test": "负责运行针对性测试和检查，优先定位失败根因，不修改业务代码。",
            "review": "只读审查实现和测试结果，列出风险和未覆盖项，不修改文件。",
            "general": "独立完成任务，按需使用可用工具。",
        }
        role = task.subagent_type if task.subagent_type in role_instructions else "general"
        allowed_tools = {
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
        }[role]
        messages = [
            {
                "role": "system",
                "content": (
                    f"你是星瑶子代理({task.task_id})，角色是 {role}。"
                    f"{role_instructions[role]}"
                    "你只执行主 Agent 已分配的单一子任务，不得拆解、创建或修改主任务计划；"
                    "如实使用可用工具；结束时只输出供主 Agent 汇总的公开结论。"
                ),
            },
            {
                "role": "user",
                "content": f"请完成以下子任务：\n{task.description}",
            },
        ]
        self._record_stage(task.task_id, "model")
        # 进入子代理深度作用域：在此作用域内 _get_current_subagent_depth() 返回新深度
        # 该深度会被 run_subagent 入口的硬限制检查读取，防止子代理内嵌套创建子代理
        with _subagent_depth_scope():
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
                parent_session=task.parent_session,
                parent_turn_id=task.parent_turn_id,
                permission_owner=task.task_id,
                permission_source=f"子代理：{task.description[:40]}",
            )
        answer = []
        tool_errors: dict[str, str] = {}
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
        return "".join(answer)


subagent_manager = SubagentManager()


def run_subagent(
    task_description,
    run_in_background=False,
    timeout_seconds=120,
    parent_session=None,
    parent_turn_id=None,
    parent_cancel_event=None,
    subagent_type="general",
    plan_item_id="",
    event_callback=None,
):
    """Run one isolated child agent and return its foreground result or background ID."""
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
            return json.dumps({
                "status": "blocked",
                "plan_item_id": str(plan_item_id),
                "error": "计划项尚未满足依赖、已暂停或已有冲突任务在执行。",
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
            return "计划项验证失败，未勾选完成"
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

    try:
        cmd = ["rg", "--files", "--glob", pattern, str(base)]
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
                return res
    except Exception:
        pass

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
            return "没有找到匹配该模式的文件。"
        max_results = 100
        truncated = len(matches) > max_results
        res = "\n".join(matches[:max_results])
        if truncated:
            res += f"\n\n... (匹配结果过多，已截断，共 {len(matches)} 个匹配文件)"
        return res
    except Exception as e:
        return f"🚨 快速匹配工具发生异常: {str(e)}"
