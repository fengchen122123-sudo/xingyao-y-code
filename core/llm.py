import json
import logging
import litellm
from litellm import completion

# 彻底屏蔽 LiteLLM 启动时的网络请求超时警告
logging.getLogger("LiteLLM").setLevel(logging.ERROR)
logging.getLogger("litellm").setLevel(logging.ERROR)

# 🌟 注意这里：我们把四个工具全都引进来了！
from core.tools import read_file, write_file, list_directory, execute_command, search_news, search_code, edit_file, manage_task
from core.mcp_client import mcp_manager
from litellm import token_counter

litellm.suppress_debug_info = True

def ask_in_raw_terminal(function_name, arguments):
    print("\n" + "━"*60)
    if function_name == "write_file":
        file_path = arguments.get("file_path", "未知文件")
        content = arguments.get("content", "")
        print(f"🚨 Y-CODE 敏感操作拦截：写入文件 🚨")
        print(f"文件路径: {file_path}")
        # 打印前 10 行内容预览
        lines = content.splitlines()
        preview = "\n".join(lines[:10])
        if len(lines) > 10:
            preview += f"\n... (省略 {len(lines) - 10} 行)"
        print(f"内容预览:\n{preview}")
    elif function_name == "edit_file":
        file_path = arguments.get("file_path", "未知文件")
        target = arguments.get("target_content", "")
        repl = arguments.get("replacement_content", "")
        print(f"🚨 Y-CODE 敏感操作拦截：修改文件 🚨")
        print(f"文件路径: {file_path}")
        print(f"目标删除内容:\n{target}")
        print(f"替换新增内容:\n{repl}")
    elif function_name == "execute_command":
        cmd = arguments.get("command", "")
        bg = arguments.get("run_in_background", False)
        bg_str = " (后台运行)" if bg else ""
        print(f"🚨 Y-CODE 敏感操作拦截：执行命令{bg_str} 🚨")
        print(f"命令内容: {cmd}")
        
    print("━"*60)
    import main
    mode_display = {"confirm": "【每次确认】", "plan": "【计划确认，后续本轮将不再提示】"}
    mode_tip = mode_display.get(main.current_permission_mode, "")
    
    try:
        res = input(f"是否批准此项操作？{mode_tip} (y/n, 默认为 y): ").strip().lower()
        return res in ("", "y", "yes")
    except (KeyboardInterrupt, EOFError):
        print("\n[⚠️ 操作已取消]")
        return False

def check_permission_and_ask(function_name, arguments):
    import main
    import asyncio
    from prompt_toolkit.application import in_terminal
    
    # 1. 如果权限是完全放行，直接通过
    if main.current_permission_mode == "full":
        return True
        
    # 2. 如果是计划模式且本轮对话的计划已经获得批准，直接通过
    if main.current_permission_mode == "plan" and main.plan_approved_this_turn:
        return True
        
    # 3. 否则，需要挂起 TUI 并向用户征求意见
    app = main.app_instance
    if not app or not app.loop:
        # 兜底：如果前台没有活动 App 实例，采用普通 input() 获取
        return ask_in_raw_terminal(function_name, arguments)
        
    loop = app.loop
    
    async def ask_coro():
        async with in_terminal():
            return ask_in_raw_terminal(function_name, arguments)
            
    future = asyncio.run_coroutine_threadsafe(ask_coro(), loop)
    try:
        approved = future.result()  # 阻塞消费者线程，直至用户返回 y/n
        if approved and main.current_permission_mode == "plan":
            # 用户批准了，计划模式下本轮剩余的工具调用将自动放行
            main.plan_approved_this_turn = True
        return approved
    except Exception as e:
        print(f"\n[⚠️ 权限询问交互发生异常: {str(e)}]")
        return False

# 全局变量：分离“本次窗口占比”和“整体累积消耗”
session_token_usage = {
    # 累积消耗（用来算钱）
    "cumulative_total": 0,
    "cumulative_prompt": 0,
    "cumulative_cached": 0,
    
    # 窗口占用（用来防止爆 128k 上下文）
    "latest_total": 0,
    "latest_prompt": 0,
    "latest_cached": 0,
    
    "max_window": 128000  # 默认最大上下文窗口配置
}

# 星瑶的终极武器库说明书
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取本地文件的内容，用于查看代码",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "要读取的文件相对路径"}
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "将内容写入本地文件，用于创建新文件或修改代码",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "要写入的文件路径"},
                    "content": {"type": "string", "description": "要写入的具体代码内容"}
                },
                "required": ["file_path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "雷达：扫描指定文件夹，查看里面有哪些文件和目录。如果不确定项目结构，优先使用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "dir_path": {"type": "string", "description": "要扫描的文件夹路径，默认为 '.'"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "全局代码搜索雷达：跨文件搜索特定的变量、类名、函数或任意关键字。当你不知道某段逻辑在哪个文件时，使用此工具查找。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要搜索的关键字或代码片段"},
                    "dir_path": {"type": "string", "description": "搜索范围目录，默认是 '.'", "default": "."}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "引擎：在本地终端执行 shell 或命令行指令，并获取输出结果。对于 npm run dev、python server.py 等常驻进程服务，请务必设置 run_in_background=true，否则会卡死阻塞对话。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要在终端执行的命令，比如 'python hello.py'"},
                    "run_in_background": {"type": "boolean", "description": "是否在后台异步运行该命令。若是常驻服务器或进程，设为 true。"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_news",
            "description": "联网工具：在网上搜索最新的新闻标题和链接。当用户询问现实世界的新闻或最新资讯时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "num_results": {"type": "integer", "description": "返回结果数量，默认 5 条"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "手术刀：对本地已有的代码文件进行精准的局部搜索与替换修改。相较于 write_file 覆写整张大文件，编辑已有代码时应优先调用此工具以节省 Token 并防止丢代码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "要修改的文件的相对或绝对路径"},
                    "target_content": {"type": "string", "description": "文件里准备被替换的旧代码块（必须完全匹配，注意空格缩进和换行）"},
                    "replacement_content": {"type": "string", "description": "替换进去的新代码块"}
                },
                "required": ["file_path", "target_content", "replacement_content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_task",
            "description": "后台进程管理器：对丢在后台运行的长周期任务（如 npm run dev 启动的服务器）进行列表查看（list）、状态及实时日志尾部查询（status）、强制杀掉（kill）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "对任务的动作。可选值: 'list', 'status', 'kill'"},
                    "task_id": {"type": "string", "description": "目标任务 ID。如果 action 是 'status' 或 'kill' 则为必填参数。"}
                },
                "required": ["action"]
            }
        }
    }
] + mcp_manager.get_schemas()

def estimate_tokens(model_name, messages):
    """本地离线快速估算 Token 数，防止超载崩溃"""
    try:
        return token_counter(model=model_name, messages=messages)
    except Exception:
        # 如果模型不支持或报错，采用 2.5 字符 / Token 的经验值进行兜底估算
        return len(str(messages)) // 2

def _yield_content(text):
    """假流式生成器：把完整文本逐字符 yield，模拟打字机效果"""
    for char in text:
        yield char

cancel_requested = False

def chat_with_model(model_name, api_key, messages, api_base=None, stream=False):
    """返回 str（stream=False）或 Generator[str]（stream=True）"""
    """大模型通信网关：带有连续与并发工具调用（Agent Loop）能力"""
    
    kwargs = {
        "model": model_name,
        "api_key": api_key,
        "messages": messages,
        "tools": TOOLS_SCHEMA,
        "tool_choice": "auto",
        "stream": stream,
        "stream_options": {"include_usage": True},
        "num_retries": 2
    }
    
    if api_base:
        kwargs["api_base"] = api_base
        
    try:
        max_turns = 20
        for turn in range(max_turns):
            global cancel_requested
            if cancel_requested:
                raise KeyboardInterrupt("用户终止了对话。")
                
            if stream:
                kwargs["stream"] = True
                response_stream = completion(**kwargs, timeout=30)
                
                accumulated_content = ""
                tool_calls_dict = {}
                in_native_think = False
                
                import time as native_time
                request_start = native_time.time()
                for chunk in response_stream:
                    if cancel_requested:
                        raise KeyboardInterrupt("用户终止了对话。")
                    if native_time.time() - request_start > 45:
                        raise TimeoutError("Siliconflow API 响应超时（流式读取累计耗时已超 45s），已强行切断，请稍后重试或更换模型。")
                    # 1. 解析 Token 消耗 (如果在流中提供)
                    if hasattr(chunk, 'usage') and chunk.usage:
                        usage = chunk.usage
                        global session_token_usage
                        session_token_usage["latest_total"] = getattr(usage, "total_tokens", 0)
                        session_token_usage["latest_prompt"] = getattr(usage, "prompt_tokens", 0)
                        cache_details = getattr(usage, "prompt_tokens_details", None)
                        latest_cached = cache_details.cached_tokens if cache_details and hasattr(cache_details, "cached_tokens") else 0
                        session_token_usage["latest_cached"] = latest_cached
                        session_token_usage["cumulative_total"] += session_token_usage["latest_total"]
                        session_token_usage["cumulative_prompt"] += session_token_usage["latest_prompt"]
                        session_token_usage["cumulative_cached"] += latest_cached

                    if not chunk.choices:
                        continue
                        
                    delta = chunk.choices[0].delta
                    
                    # 2. 处理原生 reasoning_content (DeepSeek R1/V3)
                    reasoning = getattr(delta, "reasoning_content", "") or ""
                    if reasoning:
                        if not in_native_think:
                            yield "<think>\n"
                            accumulated_content += "<think>\n"
                            in_native_think = True
                        yield reasoning
                        accumulated_content += reasoning
                        
                    # 3. 处理标准文本
                    content = getattr(delta, "content", "") or ""
                    if content:
                        if in_native_think:
                            yield "\n</think>\n"
                            accumulated_content += "\n</think>\n"
                            in_native_think = False
                        yield content
                        accumulated_content += content
                        
                    # 4. 拦截并组装 Tool Calls 碎片
                    if getattr(delta, "tool_calls", None):
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls_dict:
                                tool_calls_dict[idx] = {
                                    "id": getattr(tc, "id", f"call_{idx}"),
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""}
                                }
                            if getattr(tc.function, "name", None):
                                tool_calls_dict[idx]["function"]["name"] += tc.function.name
                            if getattr(tc.function, "arguments", None):
                                tool_calls_dict[idx]["function"]["arguments"] += tc.function.arguments
                
                if in_native_think:
                    yield "\n</think>\n"
                    accumulated_content += "\n</think>\n"
                    
                # 组装完整的工具调用列表
                tool_calls_list = []
                for idx in sorted(tool_calls_dict.keys()):
                    tool_calls_list.append(tool_calls_dict[idx])
                    
                # 记录这轮的回复到历史
                message_to_append = {
                    "role": "assistant",
                    "content": accumulated_content if accumulated_content else None,
                }
                if tool_calls_list:
                    message_to_append["tool_calls"] = tool_calls_list
                messages.append(message_to_append)
                
                # 如果没有调用工具，流式输出彻底完成
                if not tool_calls_list:
                    return
                    
                # 如果有工具，则开始执行工具 (不 yield 任何东西，让 UI 保持等待)
                for tc in tool_calls_list:
                    function_name = tc["function"]["name"]
                    arguments_str = tc["function"]["arguments"]
                    try:
                        arguments = json.loads(arguments_str)
                    except Exception:
                        try:
                            # 尝试修复大模型经常犯的错：在 JSON 字符串值里直接写原生的换行符，而不是 \n
                            fixed_str = arguments_str.replace('\n', '\\n')
                            # 处理控制字符
                            fixed_str = fixed_str.replace('\r', '\\r').replace('\t', '\\t')
                            arguments = json.loads(fixed_str)
                        except Exception as e:
                            print(f"\n[⚠️ 工具参数 JSON 解析严重失败，原始数据片段：{arguments_str[:100]}... 报错：{str(e)}]")
                            arguments = {}
                    
                    # 生成友好的中文提示
                    friendly_msg = f"⚙️ 星瑶正在后台悄悄使用工具: {function_name} 哒～"
                    if function_name == "read_file":
                        friendly_msg = f"🔍 星瑶正在读取文件：{arguments.get('file_path', '未知文件')}"
                    elif function_name == "write_file":
                        friendly_msg = f"✍️ 星瑶正在写入文件：{arguments.get('file_path', '未知文件')}"
                    elif function_name == "list_directory":
                        friendly_msg = f"📂 星瑶正在查看目录：{arguments.get('dir_path', '当前目录')}"
                    elif function_name == "execute_command":
                        cmd = arguments.get('command', '...')
                        if len(cmd) > 20: cmd = cmd[:20] + "..."
                        friendly_msg = f"💻 星瑶正在执行命令：{cmd}"
                    elif function_name == "search_code":
                        friendly_msg = f"🔍 星瑶正在全局查找: {arguments.get('query', '')}"
                    elif function_name == "search_news":
                        friendly_msg = f"🌐 星瑶正在联网搜索：{arguments.get('query', '')}"

                    # 打印一条提示给控制台
                    from prompt_toolkit import print_formatted_text
                    from prompt_toolkit.formatted_text import HTML
                    safe_friendly_msg = friendly_msg.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    print_formatted_text(HTML(f"<ansigreen>[ {safe_friendly_msg} ]</ansigreen>"))
                
                    result = ""
                    if function_name in ("write_file", "edit_file", "execute_command"):
                        approved = check_permission_and_ask(function_name, arguments)
                        if not approved:
                            result = "🚨 用户拒绝了执行该操作。"

                    try:
                        if result == "🚨 用户拒绝了执行该操作。":
                            pass
                        elif function_name == "read_file":
                            result = read_file(arguments.get("file_path", ""))
                        elif function_name == "write_file":
                            file_path = arguments.get("file_path", "")
                            new_content = arguments.get("content", "")
                            
                            # 1. 尝试读取旧内容
                            old_content = ""
                            import os
                            if os.path.exists(file_path):
                                try:
                                    with open(file_path, 'r', encoding='utf-8') as f:
                                        old_content = f.read()
                                except FileNotFoundError:
                                    old_content = "" # 如果文件不存在，撤销时应该置为空
                                    
                                from core.memory import set_undo_state
                                set_undo_state(file_path, old_content)

                            # 2. 执行写入
                            write_file(file_path, new_content)
                            
                            # 3. 生成 diff 并内联展示 (Kimi Style)
                            try:
                                from core.diff_viewer import generate_diff
                                from rich.panel import Panel
                                from rich.syntax import Syntax
                                from rich.console import Console
                                
                                diff_text = generate_diff(old_content, new_content, file_path)
                                
                                if diff_text:
                                    # 截断过长的 diff，防止刷屏
                                    lines = diff_text.split('\n')
                                    max_lines = 25
                                    if len(lines) > max_lines:
                                        display_text = '\n'.join(lines[:max_lines]) + f"\n... (省略 {len(lines) - max_lines} 行，已成功写入文件)"
                                    else:
                                        display_text = diff_text
                                        
                                    from core.diff_viewer import render_diff_table
                                    diff_table = render_diff_table(display_text)
                                    
                                    panel = Panel(
                                        diff_table, 
                                        title=f"📝 [bold green]已修改文件: {file_path}[/bold green]",
                                        border_style="green",
                                        expand=False
                                    )
                                    
                                    # 解决 patch_stdout 下 rich 直接 print 产生 ANSI 乱码的问题
                                    from prompt_toolkit import print_formatted_text
                                    from prompt_toolkit.formatted_text import ANSI
                                    
                                    rich_console = Console(force_terminal=True, color_system="truecolor")
                                    with rich_console.capture() as capture:
                                        rich_console.print(panel)
                                    
                                    print_formatted_text(ANSI(capture.get()))
                                    
                            except Exception as e:
                                print(f"\n[⚠️ Diff 生成失败: {str(e)}]")
                                
                            result = f"已成功将内容写入文件：{file_path}，并已向用户展示 Diff。"
                        elif function_name == "list_directory":
                            dir_path = arguments.get("dir_path", ".")
                            result = list_directory(dir_path)
                        elif function_name == "search_code":
                            query = arguments.get("query")
                            dir_path = arguments.get("dir_path", ".")
                            result = search_code(query, dir_path)
                        elif function_name == "execute_command":
                            cmd = arguments.get("command", "")
                            bg = arguments.get("run_in_background", False)
                            result = execute_command(cmd, run_in_background=bg)
                        elif function_name == "search_news":
                            num = arguments.get("num_results", 5)
                            result = search_news(arguments.get("query", ""), num)
                        elif function_name == "manage_task":
                            act = arguments.get("action")
                            tid = arguments.get("task_id")
                            result = manage_task(act, tid)
                        elif function_name == "edit_file":
                            file_path = arguments.get("file_path")
                            target = arguments.get("target_content")
                            repl = arguments.get("replacement_content")
                            
                            try:
                                with open(file_path, 'r', encoding='utf-8') as f:
                                    old_content = f.read()
                            except FileNotFoundError:
                                old_content = ""
                                
                            result = edit_file(file_path, target, repl)
                            
                            if not result.startswith("🚨"):
                                try:
                                    with open(file_path, 'r', encoding='utf-8') as f:
                                        new_content = f.read()
                                        
                                    from core.diff_viewer import generate_diff
                                    from rich.panel import Panel
                                    from rich.console import Console
                                    
                                    diff_text = generate_diff(old_content, new_content, file_path)
                                    if diff_text:
                                        lines = diff_text.split('\n')
                                        max_lines = 25
                                        if len(lines) > max_lines:
                                            display_text = '\n'.join(lines[:max_lines]) + f"\n... (省略 {len(lines) - max_lines} 行，已局部替换并修改成功)"
                                        else:
                                            display_text = diff_text
                                            
                                        from core.diff_viewer import render_diff_table
                                        diff_table = render_diff_table(display_text)
                                        
                                        panel = Panel(
                                            diff_table,
                                            title=f"📝 [bold green]局部修改文件: {file_path}[/bold green]",
                                            border_style="green",
                                            expand=False
                                        )
                                        
                                        from prompt_toolkit import print_formatted_text
                                        from prompt_toolkit.formatted_text import ANSI
                                        
                                        rich_console = Console(force_terminal=True, color_system="truecolor")
                                        with rich_console.capture() as capture:
                                            rich_console.print(panel)
                                            
                                        print_formatted_text(ANSI(capture.get()))
                                except Exception as e:
                                    print(f"\n[⚠️ Diff 生成失败: {str(e)}]")
                        else:
                            # 尝试去 MCP manager 里面找
                            if any(s["function"]["name"] == function_name for s in mcp_manager.get_schemas()):
                                result = mcp_manager.call_tool_sync(function_name, arguments)
                            else:
                                result = f"🚨 未找到名为 {function_name} 的工具！"
                    except Exception as tool_err:
                        result = f"🚨 调用工具 {function_name} 时发生系统内部错误：{str(tool_err)}"
                        
                    # 挨个把工具执行的结果塞进历史记录，和前面的并发指令一一对应
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": function_name,
                        "content": str(result)
                    })
            
            # 循环继续，带着所有工具的执行结果发起下一轮对话
            
        msg = f"🚨 任务轮次超过上限（{max_turns} 轮），请把任务拆小一点再试！"
        if stream:
            yield from _yield_content(msg)
            return
        else:
            return msg
        
    except Exception as e:
        msg = f"🚨 糟糕！错误信息：{str(e)}"
        if stream:
            yield from _yield_content(msg)
            return
        else:
            return msg