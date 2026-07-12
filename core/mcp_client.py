import asyncio
import threading
import json
import os
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

class MCPManager:
    def __init__(self):
        self.config_file = "mcp.json"
        self.servers = {} # name -> {"session": session, "stack": stack}
        self.tool_schemas = []
        self.server_of_tool = {} # tool_name -> server_name
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        
        self._load_config()
        # 启动时连接所有服务
        asyncio.run_coroutine_threadsafe(self._connect_all(), self.loop).result()

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

    async def _connect_server(self, name, server_config):
        try:
            command = server_config.get("command")
            args = server_config.get("args", [])
            env = server_config.get("env", None)
            
            # 合并当前环境变量
            full_env = os.environ.copy()
            if env:
                full_env.update(env)
            
            server_params = StdioServerParameters(command=command, args=args, env=full_env)
            stack = AsyncExitStack()
            
            # 使用 async with 模式连接
            read, write = await stack.enter_async_context(stdio_client(server_params))
            session = await stack.enter_async_context(ClientSession(read, write))
            
            await session.initialize()
            self.servers[name] = {"session": session, "stack": stack}
            
            # 获取工具列表
            tools_response = await session.list_tools()
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
                self.tool_schemas.append(schema)
                self.server_of_tool[f"{name}__{tool.name}"] = name
                
            print(f"\n[MCP] 成功连接服务: {name}")
        except Exception as e:
            print(f"\n[MCP] 连接服务 {name} 失败: {e}")

    async def _connect_all(self):
        servers = self.config.get("mcpServers", {})
        tasks = []
        for name, conf in servers.items():
            tasks.append(self._connect_server(name, conf))
        if tasks:
            await asyncio.gather(*tasks)

    def get_schemas(self):
        return self.tool_schemas

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
        future = asyncio.run_coroutine_threadsafe(self._call_tool_async(tool_name, arguments), self.loop)
        return future.result()

# 单例实例
mcp_manager = MCPManager()
