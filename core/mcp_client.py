import asyncio
import atexit
import concurrent.futures
import logging
import threading
import json
import os
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from core import credentials

# 连接与初始化超时（秒），防止任一 server 挂起导致程序永久卡死
CONNECT_TIMEOUT = 10
INITIALIZE_TIMEOUT = 10
CALL_TOOL_TIMEOUT = 60

_ALLOWED_MCP_COMMAND = re.compile(
    r"^(?:npx(?:\.cmd|\.exe)?|node(?:\.exe)?|python(?:\d+(?:\.\d+)*)?(?:\.exe)?)$",
    re.IGNORECASE,
)
_DANGEROUS_MCP_ENV_KEYS = {
    "BASH_ENV",
    "ENV",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "NODE_OPTIONS",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "PYTHONHOME",
    "NODE_PATH",
}
_SENSITIVE_NAME_PARTS = (
    "api_key", "apikey", "auth", "credential", "dsn", "password",
    "secret", "token", "conn_str", "connection_string",
)
_SENSITIVE_ARG_FLAGS = {
    "--api-key", "--apikey", "--app-secret", "--appsecret", "--auth",
    "--credential", "--dsn", "--password", "--secret", "--token",
}
_INHERITED_ENV_ALLOWLIST = {
    "APPDATA", "COMSPEC", "HOME", "HOMEDRIVE", "HOMEPATH", "LANG", "LOCALAPPDATA",
    "PATH", "PATHEXT", "PROGRAMDATA", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP",
    "USERPROFILE", "WINDIR",
}


def _is_sensitive_name(name):
    normalized = re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")
    return any(part in normalized for part in _SENSITIVE_NAME_PARTS)


def _redact_secret(value):
    return "***" if value is not None else None


def _redact_argument(argument):
    text = str(argument)
    # Authorization headers and other embedded secret shapes.
    text = _redact_text(text)
    # URI userinfo: scheme://user:password@host
    text = re.sub(
        r"(?P<prefix>[a-z][a-z0-9+.-]*://[^:/@\s]+:)[^@/\s]+@",
        r"\g<prefix>***@",
        text,
        flags=re.IGNORECASE,
    )
    # --secret=value / key=value forms.
    if "=" in text:
        key, _ = text.split("=", 1)
        if _is_sensitive_name(key):
            return f"{key}=***"
    try:
        parsed = urlsplit(text)
        if parsed.scheme and parsed.netloc:
            hostname = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            userinfo = "***@" if parsed.username is not None else ""
            query = []
            for key, value in parse_qsl(parsed.query, keep_blank_values=True):
                query.append((key, "***" if _is_sensitive_name(key) else value))
            text = urlunsplit((parsed.scheme, userinfo + hostname + port, parsed.path, urlencode(query), parsed.fragment))
    except ValueError:
        pass
    return text


def _redact_arguments(args):
    redacted = []
    hide_next = False
    for argument in args if isinstance(args, list) else []:
        text = str(argument)
        if hide_next:
            redacted.append("***")
            hide_next = False
            continue
        redacted.append(_redact_argument(text))
        hide_next = text.lower() in _SENSITIVE_ARG_FLAGS
    return redacted


def _redact_text(value):
    text = str(value or "")
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer ***", text)
    text = re.sub(r"(?i)([a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)", r"\1***\2", text)
    text = re.sub(
        r"(?i)(\b(?:api[_-]?key|token|secret|password|credential|auth|dsn)\s*[=:]\s*)[^\s,;]+",
        r"\1***",
        text,
    )
    return text


def _validate_mcp_launch(command, env):
    command_text = str(command or "").strip()
    if command_text != os.path.basename(command_text) or "/" in command_text or "\\" in command_text:
        return False, f"MCP 启动命令不允许包含路径: {command_text}"
    if not _ALLOWED_MCP_COMMAND.fullmatch(command_text):
        return False, f"MCP 启动命令不允许: {command_text}"
    for key in (env or {}):
        upper_key = str(key).upper()
        if upper_key in _DANGEROUS_MCP_ENV_KEYS or upper_key.startswith("DYLD_"):
            return False, f"MCP 环境变量不允许: {key}"
    return True, ""


def _decrypt_env_value(value):
    """mcp.json 的 env 值支持 credentials 模块的加密存储（dpapi:/keyring: 前缀），
    无前缀的旧配置按明文原样返回（向后兼容）。"""
    text = str(value or "")
    if not text.startswith((credentials.DPAPI_PREFIX, credentials.KEYRING_PREFIX)):
        return text
    try:
        return credentials.unprotect_secret(text)
    except Exception as exc:
        logging.getLogger(__name__).warning("MCP 环境变量解密失败，按原值使用: %s", exc)
        return text


def _encrypt_env_values(env):
    """写盘前加密 env 值；单个值加密失败时保留明文并记日志，不阻断配置写入。"""
    encrypted = {}
    for key, value in env.items():
        try:
            encrypted[key] = credentials.protect_secret(str(value))
        except Exception as exc:
            logging.getLogger(__name__).warning("MCP 环境变量 %s 加密失败，按明文保存: %s", key, exc)
            encrypted[key] = str(value)
    return encrypted


# MCP 状态输出回调：默认写日志，由入口层（main.py / gui_bridge.py）
# 按各自 UI 形态注册真正的展示方式，本模块不依赖任何界面库。
_status_logger = None


def set_status_logger(callback):
    """注册 MCP 状态展示回调；传 None 恢复为默认的日志输出。

    回调会在 MCP 后台线程中被调用，注册方必须自行切回各自的 UI 线程。
    """
    global _status_logger
    _status_logger = callback


def _emit_status(message):
    """输出 MCP 连接状态：优先交给已注册的展示回调，否则记入日志。"""
    callback = _status_logger
    if callback is not None:
        try:
            callback(message)
            return
        except Exception:
            pass
    logging.getLogger(__name__).info("%s", message)


_STDIO_ERRLOG_FALLBACK = None  # 惰性打开的 os.devnull 流，进程级单例避免泄漏 fd


def _resolve_stdio_errlog():
    """为 stdio_client 选择可用的子进程 stderr 重定向目标。

    stdio_client 默认 errlog=sys.stderr；打包 GUI（console=False）下 sys.stderr
    是 None 或无 fileno 的哑流（见 packaging/runtime_hook_nullstdout.py），
    anyio 拉起子进程时取 fileno() 会抛 'null stream has no fileno'，
    导致所有 stdio MCP 连接失败。此时回退到 os.devnull。

    连接只发生在 MCP 专用事件循环线程内，惰性单例无需加锁。
    """
    err = sys.stderr
    if err is not None:
        try:
            err.fileno()
            return err
        except (OSError, ValueError, AttributeError):
            pass
    global _STDIO_ERRLOG_FALLBACK
    if _STDIO_ERRLOG_FALLBACK is None or _STDIO_ERRLOG_FALLBACK.closed:
        _STDIO_ERRLOG_FALLBACK = open(os.devnull, "w", encoding="utf-8", errors="replace")
    return _STDIO_ERRLOG_FALLBACK


class MCPManager:
    def __init__(self):
        import config
        self.config_file = config.MCP_FILE
        self.servers = {} # name -> {"session": session, "stack": stack}
        self.tool_schemas = []
        self.server_of_tool = {} # tool_name -> server_name
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

        self.config = {"mcpServers": {}}
        self._connect_lock = threading.Lock()
        self._config_lock = threading.RLock()
        # 惰性连接标记：import 时绝不连接，首次使用时才启动
        self._connect_future = None
        self._connect_future_is_placeholder = False
        self._closed = False

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _load_config(self):
        with self._config_lock:
            if not os.path.exists(self.config_file):
                self.config = {"mcpServers": {}}
                self._save_config_locked()
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    self.config = json.load(f)
            except Exception:
                self.config = {"mcpServers": {}}

    def _save_config_locked(self):
        directory = os.path.dirname(os.path.abspath(self.config_file))
        os.makedirs(directory, exist_ok=True)
        temp_path = os.path.join(
            directory,
            f".{os.path.basename(self.config_file)}.{os.getpid()}.{threading.get_ident()}.tmp",
        )
        try:
            with open(temp_path, "w", encoding="utf-8") as stream:
                json.dump(self.config, stream, indent=4, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.config_file)
        finally:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass

    def _maybe_start_connection(self):
        """启动 MCP 连接或创建占位 future，不等待完成。

        必须在已持有 self._connect_lock 时调用。
        """
        if self._closed:
            return
        if self._connect_future is None:
            self._load_config()
            if self.config.get("mcpServers"):
                self._connect_future = asyncio.run_coroutine_threadsafe(
                    self._connect_all(), self.loop
                )
                self._connect_future_is_placeholder = False
            else:
                # 无配置：用一个已完成的 future 占位，后续访问零开销。
                # 注意：空配置是进程级一次性决策——运行期再编辑 mcp.json
                # 不会触发重连，需重启进程后生效。
                done = concurrent.futures.Future()
                done.set_result(None)
                self._connect_future = done
                self._connect_future_is_placeholder = True

    def _ensure_connected(self):
        """首次访问时惰性连接所有 MCP 服务；配置为空时完全不启动连接。

        支持运行期配置变化：若之前是空配置占位 future，而当前 mcp.json 已
        包含 server，则重置连接标记并重新发起连接。
        """
        if self._closed:
            return False
        with self._connect_lock:
            if self._closed:
                return False
            self._load_config()
            servers_now = bool(self.config.get("mcpServers"))
            if servers_now and self._connect_future_is_placeholder:
                self._connect_future = None
                self._connect_future_is_placeholder = False
            self._maybe_start_connection()
            future = self._connect_future
        try:
            future.result(timeout=CONNECT_TIMEOUT)
        except Exception as e:
            # 连接失败/超时后重置标记，允许下次调用重新发起连接，
            # 避免一次失败导致 MCP 永久失效且每次调用反复阻塞等待超时。
            # 注意：future.cancel() 只能阻止尚未启动的协程任务；
            # 若协程已挂在 session.initialize()，还需向事件循环投递取消信号。
            try:
                future.cancel()
            except Exception:
                pass
            # 尝试取消事件循环中可能残留的冻结协程，减少僵尸协程积累
            try:
                asyncio.run_coroutine_threadsafe(
                    self._cancel_pending_connections(), self.loop
                )
            except Exception:
                pass
            with self._connect_lock:
                if self._connect_future is future:
                    self._connect_future = None
                    self._connect_future_is_placeholder = False
            _emit_status(f"\n[MCP] 连接过程超时或失败: {_redact_text(e)}")
            return False
        return True

    async def _connect_server(self, name, server_config):
        stack = AsyncExitStack()
        try:
            command = server_config.get("command")
            args = server_config.get("args", [])
            env = server_config.get("env", None)

            # 手工编辑 mcp.json 会绕过 add_server 的校验，连接前强制复核同一套规则，
            # 校验失败拒绝 spawn，防止任意命令执行 / 危险环境变量注入。
            if env is not None and not isinstance(env, dict):
                _emit_status(f"\n[MCP] 拒绝连接服务 {name}: env 必须是对象")
                return None
            launch_ok, launch_error = _validate_mcp_launch(command, env)
            if not launch_ok:
                logging.getLogger(__name__).warning(
                    "MCP 服务 %s 启动校验失败，已拒绝连接: %s", name, launch_error
                )
                _emit_status(f"\n[MCP] 拒绝连接服务 {name}: {launch_error}")
                return None

            # 读取连接时解密 env 值（dpapi:/keyring: 前缀），旧明文配置原样使用
            if env:
                env = {key: _decrypt_env_value(value) for key, value in env.items()}

            # MCP 子进程只继承非敏感运行环境；模型 API 凭据必须显式配置到该 server。
            full_env = {
                key: value for key, value in os.environ.items()
                if str(key).upper() in _INHERITED_ENV_ALLOWLIST
            }
            if env:
                # 合并时剔除对 PATH/COMSPEC/SYSTEMROOT 等继承键的覆盖尝试（双保险）
                full_env.update({
                    key: value for key, value in env.items()
                    if str(key).upper() not in _INHERITED_ENV_ALLOWLIST
                })

            # 幂等重连：同名旧连接先关闭并移除旧 schema，
            # 避免连接超时取消后的重试竞态产生双份子进程/双份工具
            old = self.servers.pop(name, None)
            if old is not None:
                try:
                    await old["stack"].aclose()
                except Exception:
                    pass
                prefix = f"{name}__"
                self.tool_schemas = [
                    s for s in self.tool_schemas
                    if not str(s.get("function", {}).get("name", "")).startswith(prefix)
                ]
                self.server_of_tool = {
                    tool: srv for tool, srv in self.server_of_tool.items() if srv != name
                }

            server_params = StdioServerParameters(command=command, args=args, env=full_env)

            # 使用 async with 模式连接；spawn 子进程也要包超时，
            # 防止 server 启动卡死导致连接协程永久挂起
            read, write = await asyncio.wait_for(
                stack.enter_async_context(
                    stdio_client(server_params, errlog=_resolve_stdio_errlog())
                ),
                timeout=CONNECT_TIMEOUT,
            )
            session = await stack.enter_async_context(ClientSession(read, write))

            await asyncio.wait_for(session.initialize(), timeout=INITIALIZE_TIMEOUT)
            self.servers[name] = {"session": session, "stack": stack}

            # 获取工具列表
            tools_response = await asyncio.wait_for(session.list_tools(), timeout=INITIALIZE_TIMEOUT)
            # 先在局部构建，再整体替换为不可变快照，避免跨线程读到半成品列表
            new_schemas = []
            new_tool_map = {}
            for tool in tools_response.tools:
                # 转换 MCP 工具 schema 到 OpenAI 兼容格式
                schema = {
                    "type": "function",
                    "function": {
                        "name": f"{name}__{tool.name}",
                        "description": tool.description or "",
                        "parameters": tool.inputSchema
                    }
                }
                new_schemas.append(schema)
                new_tool_map[f"{name}__{tool.name}"] = name
            # schema 按 server 名幂等替换而非追加：取消-重试竞态下同 server
            # 重复连接不会产生双份工具
            prefix = f"{name}__"
            self.tool_schemas = [
                s for s in self.tool_schemas
                if not str(s.get("function", {}).get("name", "")).startswith(prefix)
            ] + new_schemas
            self.server_of_tool = {**self.server_of_tool, **new_tool_map}

            _emit_status(f"\n[MCP] 成功连接服务: {name}")
            # 连接成功后把仍是明文的 env 值回写为加密版本（一次性迁移）
            self._encrypt_plaintext_env_on_disk(name)
            return name
        except asyncio.CancelledError:
            # 取消晚于 initialize 完成的竞态：entry 可能已入 self.servers，
            # 必须移除并关闭，否则下次访问会重复连接出双份子进程
            self.servers.pop(name, None)
            try:
                await stack.aclose()
            except Exception:
                pass
            raise
        except Exception as e:
            # 异常时释放已建立的子进程/资源，避免泄漏
            try:
                await stack.aclose()
            except Exception:
                pass
            _emit_status(f"\n[MCP] 连接服务 {name} 失败: {_redact_text(e)}")
            return None

    def _encrypt_plaintext_env_on_disk(self, name):
        """把 mcp.json 中该 server 仍是明文的 env 值加密后回写（一次性迁移）。
        失败仅记日志，绝不影响已建立的连接。"""
        try:
            with self._config_lock:
                servers = self.config.get("mcpServers", {})
                conf = servers.get(name)
                if not isinstance(conf, dict):
                    return
                env = conf.get("env")
                if not env or not isinstance(env, dict):
                    return
                plaintext = {
                    key: value for key, value in env.items()
                    if not str(value).startswith(
                        (credentials.DPAPI_PREFIX, credentials.KEYRING_PREFIX)
                    )
                }
                if not plaintext:
                    return
                env.update(_encrypt_env_values(plaintext))
                self._save_config_locked()
        except Exception as exc:
            logging.getLogger(__name__).warning("MCP 服务 %s env 加密回写失败: %s", name, exc)

    async def _cancel_pending_connections(self) -> None:
        """尝试取消当前事件循环中所有与 MCP 连接相关的挂起任务。
        当连接超时时调用，减少僵尸协程在事件循环中积累。
        显式传入目标 loop 枚举任务，避免依赖隐式 running-loop 解析的版本差异。"""
        try:
            for task in asyncio.all_tasks(self.loop):
                if task.done():
                    continue
                coro_name = getattr(task.get_coro(), "__name__", "")
                if coro_name in ("_connect_all", "_connect_server"):
                    task.cancel()
        except Exception:
            pass

    async def _connect_all(self):
        servers = self.config.get("mcpServers", {})
        names = []
        tasks = []
        for name, conf in servers.items():
            # 支持 enabled 字段：显式 false 时跳过连接，配置保留方便随时启用
            # 未设置 enabled 或 enabled=true 都按启用处理（向后兼容旧配置）
            if isinstance(conf, dict) and conf.get("enabled", True) is False:
                _emit_status(f"\n[MCP] 跳过已禁用的服务: {name}")
                continue
            names.append(name)
            tasks.append(self._connect_server(name, conf))
        if tasks:
            # _connect_server 成功返回 name、失败返回 None；
            # 记录失败名单让 gather 结果可区分成败，不再把异常吞成“成功”
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failed = [
                name for name, result in zip(names, results)
                if result is None or isinstance(result, Exception)
            ]
            if failed:
                logging.getLogger(__name__).warning(
                    "MCP 连接失败的服务: %s", ", ".join(failed)
                )

    def get_schemas(self):
        """返回当前已就绪的 MCP 工具 schema（按工具名排序），不阻塞等待连接完成。

        首次访问时惰性触发连接（或空配置占位 future），但不等待连接结果，
        避免阻塞主流程；连接成功后 tool_schemas 会自动更新。
        排序返回：各 server 连接完成的先后顺序随网络波动，排序保证 tools 列表
        跨轮次逐字节稳定，这是 DeepSeek/Kimi 前缀缓存命中的前提。
        """
        if self._closed:
            return []
        with self._connect_lock:
            self._maybe_start_connection()
        return sorted(
            self.tool_schemas,
            key=lambda s: s.get("function", {}).get("name", ""),
        )

    def get_schema(self, tool_name):
        """Return one current schema without connecting or mutating state."""
        with self._connect_lock:
            for schema in self.tool_schemas:
                if schema.get("function", {}).get("name") == tool_name:
                    return schema
        return None

    async def _call_tool_async(self, tool_name, arguments):
        server_name = self.server_of_tool.get(tool_name)
        if not server_name or server_name not in self.servers:
            return f"MCP 工具 {tool_name} 不存在或服务未连接"

        session = self.servers[server_name]["session"]
        original_tool_name = tool_name[len(server_name)+2:] # 去除 name__ 前缀

        try:
            result = await asyncio.wait_for(
                session.call_tool(original_tool_name, arguments),
                timeout=CALL_TOOL_TIMEOUT,
            )
            if result.isError:
                return f"🚨 MCP 工具执行错误: {result.content}"
            # 将执行结果内容转为字符串
            text_outputs = []
            for content in result.content:
                if content.type == "text":
                    text_outputs.append(content.text)
                else:
                    text_outputs.append(str(content))
            return "\n".join(text_outputs)
        except Exception as e:
            return f"🚨 MCP 工具调用异常: {_redact_text(e)}"

    def call_tool_sync(self, tool_name, arguments):
        """提供给同步框架调用的阻塞方法"""
        if not self._ensure_connected():
            return "MCP 管理器已关闭或连接失败"
        future = asyncio.run_coroutine_threadsafe(self._call_tool_async(tool_name, arguments), self.loop)
        try:
            return future.result(timeout=CALL_TOOL_TIMEOUT + 5)
        except (TimeoutError, asyncio.CancelledError, concurrent.futures.CancelledError):
            # 超时/取消不能抛给调用线程：按契约返回友好错误文本，
            # 并尽力取消事件循环中的底层调用，避免挂起协程泄漏
            try:
                future.cancel()
            except Exception:
                pass
            return f"🚨 MCP 工具调用超时（>{CALL_TOOL_TIMEOUT + 5}s），已尝试取消，请稍后重试或 /mcp reconnect"

    async def _close_all(self):
        for name, entry in list(self.servers.items()):
            try:
                await entry["stack"].aclose()
            except Exception:
                pass
        self.servers.clear()

    def reconnect(self):
        """运行期重连：先关闭所有现有连接与子进程，再清空状态，下次 get_schemas()/call_tool_sync() 时惰性重建。

        供 /mcp reconnect 调用，避免用户编辑 mcp.json 后必须重启进程。
        返回 (ok: bool, message: str)。
        """
        if self._closed:
            return False, "MCP 管理器已关闭（进程退出中）"
        try:
            # 1) 在事件循环中关闭所有现有 server stack
            try:
                if not self.loop.is_closed():
                    future = asyncio.run_coroutine_threadsafe(self._close_all(), self.loop)
                    future.result(timeout=10)
            except Exception as e:
                _emit_status(f"\n[MCP] 重连-关闭旧连接失败（继续重置）: {_redact_text(e)}")
            # 2) 清空状态，重置占位标记，下次访问会重新读 mcp.json 并发起连接
            with self._connect_lock:
                self.tool_schemas = []
                self.server_of_tool = {}
                self.servers = {}
                self._connect_future = None
                self._connect_future_is_placeholder = False
            # 3) 重新读盘 + 立即触发一次连接（不阻塞等待结果）
            self._ensure_connected()
            return True, "已清空旧连接并重新发起连接，下次调用 MCP 工具时生效"
        except Exception as e:
            return False, f"重连失败: {_redact_text(e)}"

    def set_server_enabled(self, name, enabled):
        """启用/禁用某个 MCP server，并立即重连生效。

        会写回 mcp.json：把 server.enabled 字段设为 True/False。
        若 server 不存在返回失败；若原本就没 enabled 字段，会新增。
        禁用后下次 reconnect 不会拉起该 server，但配置保留以便随时启用。
        返回 (ok: bool, message: str)。
        """
        if self._closed:
            return False, "MCP 管理器已关闭（进程退出中）"
        try:
            # 1) 重新读盘拿最新配置
            with self._config_lock:
                self._load_config()
                servers = self.config.get("mcpServers", {})
                if name not in servers:
                    return False, f"未找到 MCP 服务: {name}（可用 /mcp status 查看已配置项）"
                if not isinstance(servers[name], dict):
                    return False, f"服务 {name} 的配置格式异常（应为对象）"
                servers[name]["enabled"] = bool(enabled)
                self._save_config_locked()
            # 4) 触发重连让改动立即生效
            ok, msg = self.reconnect()
            if not ok:
                return True, f"已更新 {name}.enabled={enabled}，但重连失败: {msg}"
            action = "启用" if enabled else "禁用"
            return True, f"已{action}服务 {name}，并重新发起连接"
        except Exception as e:
            return False, f"更新失败: {_redact_text(e)}"

    def add_server(self, name, command, args, env=None, enabled=True):
        """新增一个 MCP server 配置，并立即重连生效。

        供 AI 工具和 /mcp CLI 配置器调用，避免用户手动编辑 JSON。
        参数：
          name: server 名称（用作 mcpServers 下的 key，也是工具名前缀）
          command: 启动命令（如 "npx"）
          args: 命令参数数组（如 ["-y", "firecrawl-mcp"]）
          env: 环境变量字典（如 {"FIRECRAWL_API_KEY": "fc-xxx"}），可为 None
          enabled: 是否立即启用，默认 True
        返回 (ok: bool, message: str)。
        """
        if self._closed:
            return False, "MCP 管理器已关闭（进程退出中）"
        if not name or not isinstance(name, str):
            return False, "server 名称不能为空"
        if not command or not isinstance(command, str):
            return False, "command 不能为空"
        if not isinstance(args, list):
            return False, "args 必须是数组（如 [\"-y\", \"firecrawl-mcp\"]）"
        if env is not None and not isinstance(env, dict):
            return False, "env 必须是字典或 null"
        launch_ok, launch_error = _validate_mcp_launch(command, env)
        if not launch_ok:
            return False, launch_error
        try:
            with self._config_lock:
                self._load_config()
                servers = self.config.setdefault("mcpServers", {})
                if name in servers:
                    return False, f"已存在同名服务: {name}（如需更新请先 /mcp remove {name}）"
                servers[name] = {
                    "enabled": bool(enabled),
                    "command": command,
                    "args": args,
                }
                if env:
                    # env 值（API Key 等）写盘前用 credentials 模块加密，
                    # 避免与 config.json 的加密策略不对称、明文落盘
                    servers[name]["env"] = _encrypt_env_values(env)
                self._save_config_locked()
            ok, msg = self.reconnect()
            if not ok:
                return True, f"已写入 {name} 配置，但重连失败: {msg}"
            return True, f"已新增服务 {name}（command={command}），并重新发起连接"
        except Exception as e:
            return False, f"新增失败: {_redact_text(e)}"

    def remove_server(self, name):
        """删除一个 MCP server 配置，并立即重连生效。

        供 AI 工具和 /mcp CLI 配置器调用。
        返回 (ok: bool, message: str)。
        """
        if self._closed:
            return False, "MCP 管理器已关闭（进程退出中）"
        if not name:
            return False, "server 名称不能为空"
        try:
            with self._config_lock:
                self._load_config()
                servers = self.config.get("mcpServers", {})
                if name not in servers:
                    return False, f"未找到 MCP 服务: {name}"
                del servers[name]
                self._save_config_locked()
            ok, msg = self.reconnect()
            if not ok:
                return True, f"已删除 {name}，但重连失败: {msg}"
            return True, f"已删除服务 {name}，并重新发起连接"
        except Exception as e:
            return False, f"删除失败: {_redact_text(e)}"

    def list_servers(self):
        """返回所有 MCP server 配置的 JSON 字符串（供 AI 工具读取）。

        包含 enabled/command/args/env 字段，env 中的 API Key 会做掩码处理
        （前 4 位 + **** + 后 4 位），避免敏感信息泄露给 LLM。
        """
        try:
            with self._config_lock:
                self._load_config()
                servers = dict(self.config.get("mcpServers", {}))
            result = []
            for name, conf in servers.items():
                if not isinstance(conf, dict):
                    continue
                item = {
                    "name": name,
                    "enabled": conf.get("enabled", True) is not False,
                    "command": conf.get("command", ""),
                    "args": _redact_arguments(conf.get("args", [])),
                    "connected": name in self.servers,
                }
                # MCP 配置返回给模型时，环境变量值一律隐藏。调用方无需读取明文。
                env = conf.get("env")
                if env:
                    item["env"] = {key: _redact_secret(value) for key, value in env.items()}
                else:
                    item["env"] = None
                result.append(item)
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": _redact_text(e)}, ensure_ascii=False)

    def close(self):
        """Close servers and stop/join the private event-loop thread."""
        with self._connect_lock:
            if self._closed:
                return
            self._closed = True
            future = self._connect_future
        try:
            if future is not None and not future.done():
                future.cancel()
            if not self.loop.is_closed():
                close_future = asyncio.run_coroutine_threadsafe(self._close_all(), self.loop)
                close_future.result(timeout=5)
        except Exception:
            pass
        try:
            if not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.loop.stop)
            if threading.current_thread() is not self.thread:
                self.thread.join(timeout=5)
            if not self.thread.is_alive():
                if not self.loop.is_closed():
                    self.loop.close()
            else:
                # 线程未在超时内退出：尽力强制关闭兜底，并记日志而不是静默泄漏事件循环
                logging.getLogger(__name__).warning(
                    "MCP 事件循环线程未在超时内退出，事件循环可能泄漏"
                )
                try:
                    if not self.loop.is_closed():
                        self.loop.close()
                except Exception:
                    pass
        except Exception:
            pass

# 单例实例
mcp_manager = MCPManager()
atexit.register(mcp_manager.close)
