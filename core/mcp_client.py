import asyncio
import atexit
import concurrent.futures
import threading
import json
import os
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# 连接与初始化超时（秒），防止任一 server 挂起导致程序永久卡死
CONNECT_TIMEOUT = 10
INITIALIZE_TIMEOUT = 10


def _emit_status(message):
    """Render MCP connection status on the TUI loop when it exists."""
    try:
        import main
        app = getattr(main, "app_instance", None)
        if app is not None and app.loop is not None:
            from prompt_toolkit import print_formatted_text
            app.loop.call_soon_threadsafe(lambda: print_formatted_text(message))
            return
    except Exception:
        pass
    print(message)

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
        # 惰性连接标记：import 时绝不连接，首次使用时才启动
        self._connect_future = None
        self._closed = False

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _load_config(self):
        if not os.path.exists(self.config_file):
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": {}}, f, indent=4)
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        except Exception:
            self.config = {"mcpServers": {}}

    def _ensure_connected(self):
        """首次访问时惰性连接所有 MCP 服务；配置为空时完全不启动连接。"""
        with self._connect_lock:
            if self._connect_future is None:
                self._load_config()
                if self.config.get("mcpServers"):
                    self._connect_future = asyncio.run_coroutine_threadsafe(
                        self._connect_all(), self.loop
                    )
                else:
                    # 无配置：用一个已完成的 future 占位，后续访问零开销。
                    # 注意：空配置是进程级一次性决策——运行期再编辑 mcp.json
                    # 不会触发重连，需重启进程后生效。
                    done = concurrent.futures.Future()
                    done.set_result(None)
                    self._connect_future = done
            future = self._connect_future
        try:
            future.result(timeout=CONNECT_TIMEOUT)
        except Exception as e:
            # 连接失败/超时后重置标记，允许下次调用重新发起连接，
            # 避免一次失败导致 MCP 永久失效且每次调用反复阻塞等待超时
            try:
                future.cancel()
            except Exception:
                pass
            with self._connect_lock:
                if self._connect_future is future:
                    self._connect_future = None
            _emit_status(f"\n[MCP] 连接过程超时或失败: {e}")

    async def _connect_server(self, name, server_config):
        stack = AsyncExitStack()
        try:
            command = server_config.get("command")
            args = server_config.get("args", [])
            env = server_config.get("env", None)

            # 合并当前环境变量
            full_env = os.environ.copy()
            if env:
                full_env.update(env)

            server_params = StdioServerParameters(command=command, args=args, env=full_env)

            # 使用 async with 模式连接；spawn 子进程也要包超时，
            # 防止 server 启动卡死导致连接协程永久挂起
            read, write = await asyncio.wait_for(
                stack.enter_async_context(stdio_client(server_params)),
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
            self.tool_schemas = self.tool_schemas + new_schemas
            self.server_of_tool = {**self.server_of_tool, **new_tool_map}

            _emit_status(f"\n[MCP] 成功连接服务: {name}")
        except Exception as e:
            # 异常时释放已建立的子进程/资源，避免泄漏
            try:
                await stack.aclose()
            except Exception:
                pass
            _emit_status(f"\n[MCP] 连接服务 {name} 失败: {e}")

    async def _connect_all(self):
        servers = self.config.get("mcpServers", {})
        tasks = []
        for name, conf in servers.items():
            tasks.append(self._connect_server(name, conf))
        if tasks:
            await asyncio.gather(*tasks)

    def get_schemas(self):
        self._ensure_connected()
        return list(self.tool_schemas)

    async def _call_tool_async(self, tool_name, arguments):
        server_name = self.server_of_tool.get(tool_name)
        if not server_name or server_name not in self.servers:
            return f"MCP 工具 {tool_name} 不存在或服务未连接"

        session = self.servers[server_name]["session"]
        original_tool_name = tool_name[len(server_name)+2:] # 去除 name__ 前缀

        try:
            result = await session.call_tool(original_tool_name, arguments)
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
            return f"🚨 MCP 工具调用异常: {str(e)}"

    def call_tool_sync(self, tool_name, arguments):
        """提供给同步框架调用的阻塞方法"""
        self._ensure_connected()
        future = asyncio.run_coroutine_threadsafe(self._call_tool_async(tool_name, arguments), self.loop)
        return future.result()

    async def _close_all(self):
        for name, entry in list(self.servers.items()):
            try:
                await entry["stack"].aclose()
            except Exception:
                pass
        self.servers.clear()

    def close(self):
        """统一清理：关闭所有 server 子进程与 AsyncExitStack（进程退出时由 atexit 调用）。

        注意：close 不停止后台 event loop——该 loop 跑在 daemon 线程上随进程退出；
        运行期 stop loop 反而会让后续惰性重连不可用。
        """
        if self._closed:
            return
        self._closed = True
        try:
            if self._connect_future is not None and not self.loop.is_closed():
                future = asyncio.run_coroutine_threadsafe(self._close_all(), self.loop)
                future.result(timeout=5)
        except Exception:
            pass

# 单例实例
mcp_manager = MCPManager()
atexit.register(mcp_manager.close)
