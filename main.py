import os
import sys
# 防止 PyInstaller 双重导入主入口模块导致全局变量（如 app_instance 等）在子线程内读取为 None 的双重导入陷阱
if '__main__' in sys.modules and 'main' not in sys.modules:
    sys.modules['main'] = sys.modules['__main__']
import time
import threading
import queue
import pyfiglet
from rich.console import Console
from rich.markdown import Markdown
from prompt_toolkit.widgets import TextArea, Frame
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.containers import HSplit, Window, ConditionalContainer, FloatContainer, Float
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.styles import Style
from prompt_toolkit.filters import Condition
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import HTML

from core.memory import MemoryManager
from core.llm import chat_with_model, session_token_usage
from core.model_manager import get_active_model
from config import MY_MODEL, MY_KEY, MY_BASE_URL, MY_THEME, MY_PERMISSION_MODE, DB_FILE

console = Console()
manager = MemoryManager(db_path=DB_FILE)

current_theme = MY_THEME
current_permission_mode = MY_PERMISSION_MODE
plan_approved_this_turn = False
show_memory_summary = False
confirm_prompt = None

# 全局运行时状态（优先从 config.json 读取激活模型）
import uuid
current_session = f"session_{uuid.uuid4().hex[:6]}"
_active = get_active_model()
current_model   = _active["litellm_model"]   if _active else MY_MODEL
current_model_name = _active["name"]         if _active else MY_MODEL.split('/')[-1]
current_key     = _active["api_key"]          if _active else MY_KEY
current_url     = _active["base_url"]          if _active else MY_BASE_URL
current_ctx_win = _active["context_window"]   if _active else 128000
msg_queue = queue.Queue()
app_instance = None
is_thinking = False
task_status = 'idle'  # 全局运行状态：'idle' (闲置), 'thinking' (思考中), 'executing' (执行中), 'waiting' (等待确认)
think_start_time = 0
pending_action = None   # 待处理的主线程操作：目前可能的值有 "open_models"


def recalc_token_usage():
    """全局刷新当前会话的精确 Token 使用量"""
    from core.llm import session_token_usage, estimate_tokens
    history = manager.get_history(current_session)
    session_token_usage["latest_total"] = estimate_tokens(current_model, history)

def trigger_session_auto_rename(session_name, user_query, api_model, api_key, api_url):
    """异步后台根据用户首个提问智能为会话重命名"""
    def _rename_job():
        global current_session
        try:
            # 提取纯文本内容
            query_text = ""
            if isinstance(user_query, str):
                query_text = user_query
            elif isinstance(user_query, list):
                for item in user_query:
                    if isinstance(item, dict) and item.get("type") == "text":
                        query_text += item.get("text", "")
            
            # 截取前 200 字符进行提炼，防长上下文文本耗时
            query_text = query_text[:200].strip()
            if not query_text:
                return
                
            prompt = (
                "你是一个会话起名助手。请根据以下用户的提问，提炼出一个非常简短、概括性强、通俗易懂的中文会话标题。\n"
                "要求：\n"
                "1. 必须在 2 到 6 个汉字以内，千万不要带标点符号，不要带任何解释说明文字，也不要带任何 Markdown 格式或引号。\n"
                "2. 必须直接返回会话标题本身。\n"
                "例如，用户问“帮我写个冒泡排序”，你应该返回“冒泡排序”；用户问“运行报错怎么办”，你应该返回“错误调试”。\n\n"
                f"用户提问：\"{query_text}\"\n"
                "会话标题："
            )
            from litellm import completion
            response = completion(
                model=api_model,
                api_key=api_key,
                api_base=api_url,
                messages=[{"role": "user", "content": prompt}],
                timeout=10
            )
            title = response.choices[0].message.content.strip()
            # 过滤特殊字符与多余标点
            import re
            title = re.sub(r'["\'“”‘’，。！、]', '', title)
            title = title[:8].strip()
            
            # 确保生成的标题有效且不是默认 Hex 格式
            if title and not re.match(r"^session_[a-f0-9]{6}$", title):
                success, final_title = manager.rename_session(session_name, title)
                if success:
                    def update_ui():
                        global current_session
                        if current_session == session_name:
                            current_session = final_title
                        print_formatted_text(HTML(f"\n<ansigray>💡 会话已自动命名为：{final_title}</ansigray>"))
                        if app_instance:
                            app_instance.invalidate()
                    if app_instance and app_instance.loop:
                        app_instance.loop.call_soon_threadsafe(update_ui)
                    else:
                        update_ui()
        except Exception:
            # 备用防线：如果大模型起名由于网络/超时失败，直接截取用户提问前 6 个有效字符作为会话名称
            try:
                import re
                clean_query = re.sub(r'[^\w\u4e00-\u9fa5]', '', query_text).strip()
                clean_query = "".join(clean_query.split())
                fallback_title = clean_query[:6]
                if not fallback_title:
                    fallback_title = "新对话"
                
                success, final_title = manager.rename_session(session_name, fallback_title)
                if success:
                    def update_fallback_ui():
                        global current_session
                        if current_session == session_name:
                            current_session = final_title
                        if app_instance:
                            app_instance.invalidate()
                    if app_instance and app_instance.loop:
                        app_instance.loop.call_soon_threadsafe(update_fallback_ui)
                    else:
                        update_fallback_ui()
            except Exception:
                pass

    import threading
    threading.Thread(target=_rename_job, daemon=True).start()

def parse_reply_segments(text):
    """解析并提取对话内容中的 <think> 标签段与普通内容段"""
    segments = []
    i = 0
    n = len(text)
    while i < n:
        idx_think = text.find("<think>", i)
        idx_thinking = text.find("<thinking>", i)
        
        start_idx = -1
        tag_len = 0
        if idx_think != -1 and idx_thinking != -1:
            if idx_think < idx_thinking:
                start_idx = idx_think
                tag_len = len("<think>")
            else:
                start_idx = idx_thinking
                tag_len = len("<thinking>")
        elif idx_think != -1:
            start_idx = idx_think
            tag_len = len("<think>")
        elif idx_thinking != -1:
            start_idx = idx_thinking
            tag_len = len("<thinking>")
            
        if start_idx == -1:
            segments.append(('normal', text[i:]))
            break
            
        if start_idx > i:
            segments.append(('normal', text[i:start_idx]))
            
        segments.append(('tag', text[start_idx:start_idx+tag_len]))
        
        i = start_idx + tag_len
        idx_end_think = text.find("</think>", i)
        idx_end_thinking = text.find("</thinking>", i)
        
        end_idx = -1
        end_tag_len = 0
        if idx_end_think != -1 and idx_end_thinking != -1:
            if idx_end_think < idx_end_thinking:
                end_idx = idx_end_think
                end_tag_len = len("</think>")
            else:
                end_idx = idx_end_thinking
                end_tag_len = len("</thinking>")
        elif idx_end_think != -1:
            end_idx = idx_end_think
            end_tag_len = len("</think>")
        elif idx_end_thinking != -1:
            end_idx = idx_end_thinking
            end_tag_len = len("</thinking>")
            
        if end_idx == -1:
            segments.append(('think', text[i:]))
            break
            
        if end_idx > i:
            segments.append(('think', text[i:end_idx]))
            
        segments.append(('tag', text[end_idx:end_idx+end_tag_len]))
        i = end_idx + end_tag_len
        
    return segments

def print_line_segments(line, in_think_state):
    i = 0
    n = len(line)
    in_think = in_think_state
    
    while i < n:
        if not in_think:
            idx_think = line.find("<think>", i)
            idx_thinking = line.find("<thinking>", i)
            
            start_idx = -1
            tag_len = 0
            if idx_think != -1 and idx_thinking != -1:
                if idx_think < idx_thinking:
                    start_idx = idx_think
                    tag_len = len("<think>")
                else:
                    start_idx = idx_thinking
                    tag_len = len("<thinking>")
            elif idx_think != -1:
                start_idx = idx_think
                tag_len = len("<think>")
            elif idx_thinking != -1:
                start_idx = idx_thinking
                tag_len = len("<thinking>")
                
            if start_idx == -1:
                normal_text = line[i:]
                normal_text = normal_text.replace("**", "").replace("*", "")
                print_formatted_text(normal_text, end="")
                break
                
            if start_idx > i:
                normal_text = line[i:start_idx]
                normal_text = normal_text.replace("**", "").replace("*", "")
                print_formatted_text(normal_text, end="")
                
            in_think = True
            i = start_idx + tag_len
            
        else:
            idx_end_think = line.find("</think>", i)
            idx_end_thinking = line.find("</thinking>", i)
            
            end_idx = -1
            end_tag_len = 0
            if idx_end_think != -1 and idx_end_thinking != -1:
                if idx_end_think < idx_end_thinking:
                    end_idx = idx_end_think
                    end_tag_len = len("</think>")
                else:
                    end_idx = idx_end_thinking
                    end_tag_len = len("</thinking>")
            elif idx_end_think != -1:
                end_idx = idx_end_think
                end_tag_len = len("</think>")
            elif idx_end_thinking != -1:
                end_idx = idx_end_thinking
                end_tag_len = len("</thinking>")
                
            if end_idx == -1:
                think_text = line[i:]
                safe_text = think_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                print_formatted_text(HTML(f"<span fg='#666666'>{safe_text}</span>"), end="")
                break
                
            if end_idx > i:
                think_text = line[i:end_idx]
                safe_text = think_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                print_formatted_text(HTML(f"<span fg='#666666'>{safe_text}</span>"), end="")
                
            in_think = False
            i = end_idx + end_tag_len
            
    return in_think

def print_logo(clear_screen=True):
    """打印 Logo (画卡版) 及系统信息卡片"""
    if clear_screen:
        os.system('cls' if os.name == 'nt' else 'clear')
        
    import pyfiglet
    custom_fig = pyfiglet.Figlet(font='slant')
    logo_text = custom_fig.renderText('Y-CODE')
    safe_logo = logo_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    from core.themes import THEMES
    theme_color = THEMES.get(current_theme, {}).get("border", "#00b4d8")
    
    print_formatted_text(HTML(f"<style fg='{theme_color}'><b>{safe_logo}</b></style>"))
    
    cwd = os.getcwd()
    
    cwd_display = cwd
    if len(cwd) > 35:
        cwd_display = "..." + cwd[-32:]
        
    model_display = current_model_name
    safe_session = current_session.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    
    def display_width(s):
        import re
        clean_s = re.sub(r'<[^>]+>', '', s)
        clean_s = clean_s.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        width = 0
        for char in clean_s:
            code = ord(char)
            if 0x2500 <= code <= 0x259F:
                width += 1
            elif code > 127:
                width += 2
            else:
                width += 1
        return width
        
    def make_box_line(content, inside_width=56):
        w = display_width(content)
        spaces = inside_width - w
        if spaces < 0:
            spaces = 0
        return f"<style fg='{theme_color}'>│</style>{content}{' ' * spaces}<style fg='{theme_color}'>│</style>\n"
        
    def make_data_line(label, val):
        val_str = str(val)
        w = display_width(val_str)
        if w > 38:
            truncated = ""
            curr_w = 0
            for char in val_str:
                char_w = 2 if ord(char) > 127 else 1
                if curr_w + char_w > 35:
                    truncated += "..."
                    break
                truncated += char
                curr_w += char_w
            val_str = truncated
            
        escaped_val = val_str.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        return make_box_line(f"{label}<ansigray>{escaped_val}</ansigray>")

    avatar_row1 = f"  <style fg='{theme_color}'>▄▄    ▄▄</style>    <b>Welcome to Y-CODE CLI!</b>"
    avatar_row2 = f" <style fg='{theme_color}'>██████████</style>   星瑶 (Hacker Girl) 为您服务"
    avatar_row3 = f" <style fg='{theme_color}'>██ █  █ ██</style>   输入 /help 获取帮助"
    avatar_row4 = f"  <style fg='{theme_color}'>▀██████▀</style>    "
    
    card = (
        f"<style fg='{theme_color}'>┌────────────────────────────────────────────────────────┐</style>\n"
        + make_box_line(avatar_row1)
        + make_box_line(avatar_row2)
        + make_box_line(avatar_row3)
        + make_box_line(avatar_row4)
        + make_box_line("")
        + make_data_line("   Directory : ", cwd_display)
        + make_data_line("   Session   : ", safe_session)
        + make_data_line("   Model     : ", model_display)
        + make_data_line("   Theme     : ", current_theme)
        + make_data_line("   Version   : ", "v1.0")
        + make_box_line("")
        + make_box_line("   提示: 星瑶是您的编程伙伴，使用 <ansigreen>@文件</ansigreen> 可附带上下文。")
        + f"<style fg='{theme_color}'>└────────────────────────────────────────────────────────┘</style>\n"
    )
    
    print_formatted_text(HTML(card))
    print_formatted_text(HTML("<ansigray>   Enter 发送 | Alt+Enter 换行 | Ctrl+C 清空/中断 | Ctrl+D 退出 | Ctrl+T 记忆</ansigray>\n"))


def show_session_history(session_name):
    """在终端里漂亮地重现该会话的所有历史对话消息"""
    history = manager.get_history(session_name)
    # 过滤掉 system 提示词与 tool 节点，只展示人类和智能体的核心对话内容
    chat_history = [m for m in history if m.get("role") in ("user", "assistant")]
    
    safe_session = session_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    if not chat_history:
        print_formatted_text(HTML(f"<ansiyellow>ℹ 会话 {safe_session} 当前没有任何对话记录。</ansiyellow>"))
        return
        
    print_formatted_text(HTML(f"\n<ansicyan><b>--- 会话 {safe_session} 历史对话记录 ---</b></ansicyan>"))
    import re
    for msg in chat_history:
        role = msg.get("role")
        content = msg.get("content") or ""
        
        # 过滤掉回答中冗长的心路历程思考标签
        content_clean = re.sub(r"<(?:think|thinking)>.*?</(?:think|thinking)>", "", content, flags=re.DOTALL).strip()
        
        if not content_clean:
            continue
            
        if role == "user":
            safe_input = content_clean.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            print_formatted_text(HTML(f"✨ <b><cyan>You &gt;</cyan></b> {safe_input}"))
        elif role == "assistant":
            print_formatted_text(HTML(f"<b><magenta>🤖 星瑶 &gt;</magenta></b> "))
            print_formatted_text(content_clean)
            print_formatted_text("") # 留出段落空行
    print_formatted_text(HTML("<ansicyan><b>--------------------------------------</b></ansicyan>\n"))





def handle_slash_command(command_text):
    """斜杠指令路由器"""
    global current_session, current_model

    parts = command_text.strip().split()
    cmd = parts[0].lower()

    if cmd == "/help":
        print_formatted_text(HTML("<b><ansiyellow>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</ansiyellow></b>"))
        print_formatted_text(HTML("🌟 <b><ansigreen>Y-code CLI 系统指令与操作指南</ansigreen></b>"))
        print_formatted_text(HTML("<b><ansiyellow>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</ansiyellow></b>"))
        print_formatted_text(HTML("<b><ansiyellow>【1. 会话与上下文管理】</ansiyellow></b>"))
        print_formatted_text(HTML("  <ansicyan>/session</ansicyan>                             : 打开全屏会话管理器 UI (支持回车切换，Del 删除，R/F2改名)"))
        print_formatted_text(HTML("  <ansicyan>/session rename &lt;新名称&gt;</ansicyan>          : 重命名当前活动会话"))
        print_formatted_text(HTML("  <ansicyan>/session delete &lt;会话名称&gt;</ansicyan>          : 物理删除指定会话（安全切回防御）"))
        print_formatted_text(HTML("  <ansicyan>/undo</ansicyan>                                   : 撤销星瑶上一次修改的代码，并同步抹除模型记忆"))
        print_formatted_text(HTML("  <ansicyan>/clear</ansicyan>                                  : 清理终端当前屏幕"))
        print_formatted_text(HTML(""))
        print_formatted_text(HTML("<b><ansiyellow>【2. 模型与样式配置】</ansiyellow></b>"))
        print_formatted_text(HTML("  <ansicyan>/models</ansicyan>                              : 打开全屏模型管理器 UI (切换模型、API 密钥、API 地址)"))
        print_formatted_text(HTML("  <ansicyan>/theme</ansicyan>                               : 列出当前所有可用的终端配色主题"))
        print_formatted_text(HTML("  <ansicyan>/theme &lt;主题名称&gt;</ansicyan>                    : 切换至指定主题 (支持: cyan, matrix, nord, cyberpunk, dracula, monokai)"))
        print_formatted_text(HTML("  <ansicyan>/permission &lt;confirm|plan|full&gt;</ansicyan>        : 切换安全权限级别 (confirm:变更前确认 | plan:计划模式 | full:完全访问)"))
        print_formatted_text(HTML(""))
        print_formatted_text(HTML("<b><ansiyellow>【3. 技能插件管理】</ansiyellow></b>"))
        print_formatted_text(HTML("  <ansicyan>/skills</ansicyan>                              : 打开全屏技能选择器 UI (空格勾选开启/关闭)"))
        print_formatted_text(HTML("  <ansicyan>/skills list</ansicyan>                         : 在终端列出所有扫描到的技能，展示启用状态与短 ID"))
        print_formatted_text(HTML("  <ansicyan>/skills enable &lt;短ID&gt;</ansicyan>             : 启用指定短 ID 对应的技能"))
        print_formatted_text(HTML("  <ansicyan>/skills disable &lt;短ID&gt;</ansicyan>            : 禁用指定短 ID 对应的技能"))
        print_formatted_text(HTML("  <ansicyan>/&lt;短ID&gt; [提问内容]</ansicyan>                   : <b>主动且强制调用指定技能</b>让星瑶为您执行任务"))
        print_formatted_text(HTML(""))
        print_formatted_text(HTML("<b><ansiyellow>【4. 任务托管与后台进程】</ansiyellow></b>"))
        print_formatted_text(HTML("  <ansicyan>/tasks</ansicyan>                               : 查看或接管目前正在运行的后台长周期服务/命令"))
        print_formatted_text(HTML(""))
        print_formatted_text(HTML("<b><ansiyellow>【5. 终端常用快捷操作】</ansiyellow></b>"))
        print_formatted_text(HTML("  <ansicyan>@文件名</ansicyan>                               : <b>在提问中挂载本地文件</b> (例如: <ansigray>这行代码怎么改 @main.py</ansigray>)"))
        print_formatted_text(HTML("  <ansicyan>Enter</ansicyan>                                 : 提交当前文本给星瑶（闲置立即执行，繁忙则自动排队等待）"))
        print_formatted_text(HTML("  <ansicyan>Alt+Enter</ansicyan>                             : 在输入框内输入新的一行换行符"))
        print_formatted_text(HTML("  <ansicyan>Ctrl+C</ansicyan>                                : <b>星瑶思考中时立即发送强行中断信号</b>；闲置时清空输入框"))
        print_formatted_text(HTML("  <ansicyan>Ctrl+T</ansicyan>                                : <b>切换展开/收起底部浮窗显示当前会话的背景记忆摘要</b>"))
        print_formatted_text(HTML("  <ansicyan>Shift+Tab</ansicyan>                             : <b>循环切换 Y-CODE 权限模式</b> (变更前确认 ➡️ 计划模式 ➡️ 完全访问)"))
        print_formatted_text(HTML("  <ansicyan>Ctrl+D / /exit</ansicyan>                        : 安全退出 Y-code CLI"))
        print_formatted_text(HTML("<b><ansiyellow>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</ansiyellow></b>"))

    elif cmd in ("/models", "/model"):
        # 不在后台线程里直接开选择器（会和主线程的 event loop 冲突）
        # 而是设一个标志，然后退出主 App，让主线程安全地开启选择器
        global pending_action
        pending_action = "open_models"
        if app_instance:
            app_instance.exit()

    elif cmd in ("/skills", "/skill"):
        from core.skills import scan_all_skills, get_active_skill_ids, save_active_skill_ids
        
        if len(parts) > 1:
            sub = parts[1].lower()
            if sub == "list":
                skills = scan_all_skills()
                active_ids = set(get_active_skill_ids())
                if not skills:
                    print_formatted_text(HTML("<ansiyellow>ℹ 没有扫描到任何技能。请在 .agents/skills 或 ~/.ycode/skills 中放置带有 SKILL.md 的技能包。</ansiyellow>"))
                else:
                    print_formatted_text(HTML("<b><ansicyan>可用技能插件列表：</ansicyan></b>"))
                    for s in skills:
                        short_id = os.path.basename(os.path.dirname(s["id"]))
                        is_active = s["id"] in active_ids
                        check = "<ansigreen>[✓]</ansigreen>" if is_active else "<ansigray>[ ]</ansigray>"
                        print_formatted_text(HTML(f"  {check} 短ID: <b><ansiyellow>{short_id}</ansiyellow></b> | 名称: {s['name']} - <ansigray>{s['description']}</ansigray>"))
            elif sub == "enable" and len(parts) > 2:
                target = parts[2].lower()
                skills = scan_all_skills()
                active_ids = list(get_active_skill_ids())
                matched = [s for s in skills if os.path.basename(os.path.dirname(s["id"])).lower() == target]
                if matched:
                    sid = matched[0]["id"]
                    if sid not in active_ids:
                        active_ids.append(sid)
                        save_active_skill_ids(active_ids)
                    print_formatted_text(HTML(f"<ansigreen>✔ 技能 <b>{target}</b> 已成功启用！</ansigreen>"))
                else:
                    print_formatted_text(HTML(f"<ansired>❌ 找不到短 ID 为 {target} 的技能，输入 `/skills list` 查看可用技能。</ansired>"))
            elif sub == "disable" and len(parts) > 2:
                target = parts[2].lower()
                skills = scan_all_skills()
                active_ids = list(get_active_skill_ids())
                matched = [s for s in skills if os.path.basename(os.path.dirname(s["id"])).lower() == target]
                if matched:
                    sid = matched[0]["id"]
                    if sid in active_ids:
                        active_ids.remove(sid)
                        save_active_skill_ids(active_ids)
                    print_formatted_text(HTML(f"<ansigreen>✔ 技能 <b>{target}</b> 已禁用。</ansigreen>"))
                else:
                    print_formatted_text(HTML(f"<ansired>❌ 找不到短 ID 为 {target} 的技能。</ansired>"))
            else:
                print_formatted_text(HTML("<ansired>❌ 用法错误。可选指令：</ansired>"))
                print_formatted_text(HTML("  <ansicyan>/skills list</ansicyan>             : 列出所有技能"))
                print_formatted_text(HTML("  <ansicyan>/skills enable &lt;短ID&gt;</ansicyan>  : 启用指定技能"))
                print_formatted_text(HTML("  <ansicyan>/skills disable &lt;短ID&gt;</ansicyan> : 禁用指定技能"))
        else:
            pending_action = "open_skills"
            if app_instance:
                app_instance.exit()

    elif cmd == "/theme":
        from core.themes import THEMES
        if len(parts) > 1:
            theme_name = parts[1].lower().strip("<>\"'")
            if theme_name == "list":
                theme_descs = {
                    "cyan": ("极光青", "#00b4d8"),
                    "matrix": ("骇客绿", "#39ff14"),
                    "nord": ("北欧蓝", "#88c0d0"),
                    "cyberpunk": ("赛博粉", "#ff007f"),
                    "dracula": ("德古拉紫", "#bd93f9"),
                    "lava": ("熔岩红", "#ff5555"),
                    "sunset": ("落日橙", "#ff9f43"),
                    "amber": ("琥珀黄", "#f1fa8c"),
                    "white": ("象牙白", "#ffffff"),
                }
                print_formatted_text(HTML("<b><ansicyan>可用主题配色方案：</ansicyan></b>"))
                for t in THEMES.keys():
                    desc, color = theme_descs.get(t, ("未知", "#ffffff"))
                    print_formatted_text(HTML(f"  - <b>{t:<10}</b> ({desc:<4}) : <style fg='{color}'>████</style>"))
            elif theme_name in THEMES:
                from config import save_config_value
                save_config_value("theme", theme_name)
                global current_theme
                current_theme = theme_name
                print_formatted_text(HTML(f"<ansigreen>✔ 已切换配色主题为: <b>{theme_name}</b></ansigreen>"))
                if app_instance:
                    app_instance.exit()
            else:
                available = ", ".join(THEMES.keys())
                safe_theme_name = theme_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                print_formatted_text(HTML(f"<ansired>❌ 未知主题配色「{safe_theme_name}」，可选主题：{available}</ansired>"))
        else:
            theme_descs = {
                "cyan": ("极光青", "#00b4d8"),
                "matrix": ("骇客绿", "#39ff14"),
                "nord": ("北欧蓝", "#88c0d0"),
                "cyberpunk": ("赛博粉", "#ff007f"),
                "dracula": ("德古拉紫", "#bd93f9"),
                "lava": ("熔岩红", "#ff5555"),
                "sunset": ("落日橙", "#ff9f43"),
                "amber": ("琥珀黄", "#f1fa8c"),
                "white": ("象牙白", "#ffffff"),
            }
            print_formatted_text(HTML("<b><ansicyan>可用主题配色方案：</ansicyan></b>"))
            for t in THEMES.keys():
                desc, color = theme_descs.get(t, ("未知", "#ffffff"))
                print_formatted_text(HTML(f"  - <b>{t:<10}</b> ({desc:<4}) : <style fg='{color}'>████</style>"))
            print_formatted_text(HTML("用法: <ansiyellow>/theme &lt;主题名称&gt;</ansiyellow>"))

    elif cmd == "/permission":
        if len(parts) > 1:
            mode_name = parts[1].lower().strip("<>\"'")
            if mode_name == "list":
                print_formatted_text(HTML("<b><ansicyan>可用权限模式：</ansicyan></b>"))
                print_formatted_text(HTML("  - <b>confirm</b> : 变更前确认 (修改文件、执行命令均需询问)"))
                print_formatted_text(HTML("  - <b>plan</b>    : 计划模式 (开始执行修改前需一次性批准计划)"))
                print_formatted_text(HTML("  - <b>full</b>    : 完全访问 (自动运行，无需任何确认)"))
            elif mode_name in ("confirm", "plan", "full"):
                from config import save_config_value
                save_config_value("permission_mode", mode_name)
                global current_permission_mode
                current_permission_mode = mode_name
                mapping = {"confirm": "变更前确认", "plan": "计划模式", "full": "完全访问"}
                print_formatted_text(HTML(f"<ansigreen>✔ 权限控制模式已切换为: <b>{mapping[mode_name]}</b></ansigreen>"))
                if app_instance:
                    app_instance.invalidate()
            else:
                print_formatted_text(HTML(f"<ansired>❌ 未知权限模式「{mode_name}」，可选模式：confirm, plan, full</ansired>"))
        else:
            print_formatted_text(HTML("<b><ansicyan>可用权限模式：</ansicyan></b>"))
            print_formatted_text(HTML("  - <b>confirm</b> : 变更前确认 (修改文件、执行命令均需询问)"))
            print_formatted_text(HTML("  - <b>plan</b>    : 计划模式 (开始执行修改前需一次性批准计划)"))
            print_formatted_text(HTML("  - <b>full</b>    : 完全访问 (自动运行，无需任何确认)"))
            print_formatted_text(HTML("用法: <ansiyellow>/permission &lt;confirm|plan|full&gt;</ansiyellow>"))

    elif cmd in ("/sessions", "/session"):
        if len(parts) > 1:
            sub = parts[1].lower()
            if sub == "delete" and len(parts) > 2:
                # 物理删除会话
                target = parts[2].strip("<>")
                manager.delete_session(target)
                safe_target = target.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                print_formatted_text(HTML(f"<ansigreen>✔ 会话 {safe_target} 已被成功删除。</ansigreen>"))
                
                # 如果删除的是当前活动会话，触发安全切回
                if target == current_session:
                    sessions = manager.get_all_sessions()
                    if sessions:
                        current_session = sessions[0]["name"]
                    else:
                        import uuid
                        current_session = f"session_{uuid.uuid4().hex[:6]}"
                    recalc_token_usage()
                    safe_session = current_session.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    print_formatted_text(HTML(f"<ansiyellow>⚠️ 已删除当前活动会话，系统自动切换至：{safe_session}</ansiyellow>"))
                    print_logo(clear_screen=False)
                    show_session_history(current_session)
            elif sub == "rename" and len(parts) > 2:
                # 重命名会话
                new_name = " ".join(parts[2:]).strip('"\'<>')
                if new_name:
                    success, final_name = manager.rename_session(current_session, new_name)
                    if success:
                        current_session = final_name
                        recalc_token_usage()
                        safe_final = final_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                        print_formatted_text(HTML(f"<ansigreen>✔ 会话已成功重命名为: {safe_final}</ansigreen>"))
                        print_logo(clear_screen=False)
                    else:
                        print_formatted_text(HTML("<ansired>❌ 会话重命名失败。</ansired>"))
                else:
                    print_formatted_text(HTML("<ansired>❌ 用法错误。请输入：/session rename &lt;新名称&gt;</ansired>"))
            else:
                # 向后兼容：默认会话切换 /session <会话名>
                target = parts[1].strip("<>")
                current_session = target
                recalc_token_usage()
                safe_session = current_session.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                print_formatted_text(HTML(f"\n<ansigreen>Switching to session {safe_session}...</ansigreen>"))
                print_logo(clear_screen=False)
                show_session_history(current_session)
        else:
            pending_action = "open_sessions"
            if app_instance:
                app_instance.exit()

    elif cmd in ("/tasks", "/task"):
        from core.task_manager import list_active_tasks, get_task_output, kill_background_task

        if len(parts) > 1:
            subcmd = parts[1]
            if subcmd == "kill" and len(parts) > 2:
                tid = parts[2]
                cleaned_tid = tid.strip("<>")
                safe_tid = cleaned_tid.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                if kill_background_task(cleaned_tid):
                    print_formatted_text(HTML(f"<ansigreen>✔ 后台任务 {safe_tid} 已成功终结。</ansigreen>"))
                else:
                    print_formatted_text(HTML(f"<ansired>❌ 找不到任务 ID: {safe_tid}，或任务已经提前结束。</ansired>"))
            elif subcmd in ("tail", "log", "status") and len(parts) > 2:
                tid = parts[2]
                cleaned_tid = tid.strip("<>")
                safe_tid = cleaned_tid.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                output = get_task_output(cleaned_tid, tail_lines=30)
                print_formatted_text(HTML(f"<ansicyan><b>--- 任务 {safe_tid} 最新 30 行日志 ---</b></ansicyan>"))
                print(output)
            else:
                print_formatted_text(HTML("<ansired>❌ 用法错误。可选指令：</ansired>"))
                print_formatted_text(HTML("  <ansicyan>/tasks</ansicyan>           : 列出所有后台任务"))
                print_formatted_text(HTML("  <ansicyan>/tasks kill &lt;ID&gt;</ansicyan> : 终结指定后台任务"))
                print_formatted_text(HTML("  <ansicyan>/tasks tail &lt;ID&gt;</ansicyan> : 查看任务日志输出"))
        else:
            tasks = list_active_tasks()
            if not tasks:
                print_formatted_text(HTML("<ansiyellow>ℹ 当前没有运行中的后台异步任务。</ansiyellow>"))
            else:
                print_formatted_text(HTML("<ansicyan><b>当前运行的后台任务：</b></ansicyan>"))
                for t in tasks:
                    safe_cmd = t['command'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    print_formatted_text(HTML(f"  <ansigreen>• ID: {t['task_id']}</ansigreen> | PID: <ansicyan>{t['pid']}</ansicyan> | 运行: {t['duration']} | 命令: {safe_cmd}"))
                print_formatted_text(HTML("<ansigray>提示：你可以输入 `/tasks tail &lt;ID&gt;` 查看日志，或 `/tasks kill &lt;ID&gt;` 终结任务。</ansigray>"))



    elif cmd == "/undo":
        from core.memory import get_undo_state
        from core.tools import write_file
        import os
        
        state = get_undo_state()
        file_path = state.get("file_path")
        old_content = state.get("old_content")
        
        if not file_path:
            print_formatted_text(HTML("<ansired>⚠️ 没有可以撤销的操作记录。</ansired>"))
        else:
            try:
                if old_content == "": # 代表原来没有这个文件，大模型是新建的
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    safe_file_path = file_path.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    print_formatted_text(HTML(f"<ansigreen>↩️ 撤销成功！已删除星瑶新建的文件: {safe_file_path}</ansigreen>"))
                else:
                    write_file(file_path, old_content)
                    safe_file_path = file_path.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    print_formatted_text(HTML(f"<ansigreen>↩️ 撤销成功！已将 {safe_file_path} 恢复到上一个版本。</ansigreen>"))
                
                # 清理大模型的错误记忆，防止幻觉
                manager.undo_last_interaction(current_session)
                recalc_token_usage()  # 撤销后重新计算 token
                print_formatted_text(HTML("<ansigreen>🧠 已同步抹除星瑶对刚才操作的记忆，防止幻觉复发。</ansigreen>"))

                # 清空状态防止重复撤销
                from core.memory import set_undo_state
                set_undo_state(None, None)
            except Exception as e:
                safe_e = str(e).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                print_formatted_text(HTML(f"<ansired>⚠️ 撤销失败: {safe_e}</ansired>"))

    elif cmd == "/clear":
        print_logo()

    elif cmd == "/exit":
        console.print("👋 星瑶去休息啦，下次见！")
        if app_instance:
            app_instance.exit()
    else:
        # 使用 print_formatted_text 避免 rich 在 prompt_toolkit patch_stdout 下出现 ANSI 乱码
        safe_cmd = cmd.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        print_formatted_text(HTML(f"<ansired>❌ 未知指令: {safe_cmd}，输入 /help 查看帮助。</ansired>"))


def _make_input_app():
    """
    构建 Claude Code 风格的输入框：
    ┌──────────────────────────────────────┐
    │ >                                    │
    └──────────────────────────────────────┘
     model · session · dir     快捷键提示
    """
    result = {"text": None, "exit": False}

    COMMAND_COMPLETIONS = [
        ("/help", "查看系统指令说明"),
        ("/session", "切换或创建新的记忆会话"),
        ("/models", "打开模型管理器"),
        ("/tasks", "查看或托管后台长周期进程"),
        ("/undo", "撤销星瑶上一次修改的代码"),
        ("/skills", "查看或安装/开启/禁用技能插件"),
        ("/theme", "切换配色主题 (Nord, Cyberpunk, Matrix等)"),
        ("/permission", "切换安全权限级别 (confirm/plan/full)"),
        ("/clear", "清理当前屏幕"),
        ("/exit", "退出 Y-code"),
    ]

    class SlashCommandCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            
            # 1. 尝试匹配斜杠命令
            if text.startswith('/'):
                word = document.get_word_before_cursor(WORD=True)
                for cmd, desc in COMMAND_COMPLETIONS:
                    if cmd.startswith(word):
                        yield Completion(
                            cmd, 
                            start_position=-len(word),
                            display_meta=desc
                        )
                        
                # 额外动态补充已开启的技能插件到下拉菜单供主动调用
                try:
                    from core.skills import get_active_skills_list
                    active_skills = get_active_skills_list()
                    for short_id, s in active_skills.items():
                        cmd = f"/{short_id}"
                        if cmd.startswith(word):
                            yield Completion(
                                cmd,
                                start_position=-len(word),
                                display_meta=f"⚡ 技能: {s['description']}"
                            )
                except Exception:
                    pass
                        
            # 2. 尝试匹配 @文件
            import re, os
            match = re.search(r'@([^\s]*)$', text)
            if match:
                prefix = match.group(1)
                
                # 简单遍历当前目录文件（排除隐藏文件夹和 venv）
                IGNORE_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.idea', '.vscode'}
                
                # 为防止深层目录卡死 UI，加一个小限制
                count = 0
                for root, dirs, files in os.walk('.'):
                    dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith('.')]
                    for f in files:
                        if f.startswith('.'): continue
                        
                        rel_path = os.path.relpath(os.path.join(root, f), '.')
                        rel_path = rel_path.replace('\\', '/')
                        
                        if prefix.lower() in rel_path.lower():
                            yield Completion(rel_path, start_position=-len(prefix))
                            count += 1
                        if count > 50:
                            break
                    if count > 50:
                        break

    input_area = TextArea(
        multiline=True,
        wrap_lines=True,
        height=D(min=2, max=2),   # 固定 3 行高度，不占满全屏
        style="class:input-field",
        focus_on_click=True,
        read_only=Condition(lambda: confirm_prompt is not None),
        completer=SlashCommandCompleter(),
        complete_while_typing=True,
    )

    kb = KeyBindings()

    @kb.add("enter")
    def submit(event):
        """Enter 提交（如果是指令则立即在主线程同步执行，不阻塞队列；如果是普通对话则放入后台队列）"""
        global confirm_prompt
        if confirm_prompt:
            idx = confirm_prompt['current_idx']
            fut = confirm_prompt['future']
            confirm_prompt = None
            event.app.loop.call_soon_threadsafe(lambda: fut.set_result(idx))
            event.app.invalidate()
            return
            
        text = input_area.text.strip()
        if text:
            if text.startswith('/'):
                # 检查是否是主动调用激活的技能插件快捷指令（形如 /chembl_database hello）
                parts = text.split(maxsplit=1)
                cmd = parts[0]
                
                is_skill = False
                try:
                    from core.skills import get_active_skills_list
                    active_skills = get_active_skills_list()
                    skill_cmd = cmd[1:].lower()
                    if skill_cmd in active_skills:
                        is_skill = True
                except Exception:
                    pass
                    
                if is_skill:
                    # 主动调用技能，放入后台队列处理
                    import core.llm
                    core.llm.cancel_requested = False
                    
                    was_queued = is_thinking
                    safe_input = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    if was_queued:
                        print_formatted_text(HTML(f"✨ <b><cyan>You &gt;</cyan></b> <ansigray>(排队等待中...) {safe_input}</ansigray>"))
                    else:
                        print_formatted_text(HTML(f"✨ <b><cyan>You &gt;</cyan></b> {safe_input}"))
                        
                    msg_queue.put({"text": text, "was_queued": was_queued, "is_skill_call": True})
                    input_area.text = ""
                else:
                    # 正常的系统命令，主线程同步直接执行
                    input_area.text = ""
                    safe_input = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    print_formatted_text(HTML(f"✨ <b><cyan>You &gt;</cyan></b> {safe_input}"))
                    handle_slash_command(text)
            else:
                import core.llm
                core.llm.cancel_requested = False
                
                was_queued = is_thinking
                safe_input = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                if was_queued:
                    # 立即打印置灰的排队等待提示
                    print_formatted_text(HTML(f"✨ <b><cyan>You &gt;</cyan></b> <ansigray>(排队等待中...) {safe_input}</ansigray>"))
                else:
                    # 正常的青色高亮提示
                    print_formatted_text(HTML(f"✨ <b><cyan>You &gt;</cyan></b> {safe_input}"))
                
                msg_queue.put({"text": text, "was_queued": was_queued})
                input_area.text = ""

    @kb.add("escape", "enter")
    def newline(event):
        """Alt+Enter 换行"""
        event.current_buffer.insert_text("\n")

    @kb.add("c-c")
    def clear_input(event):
        """Ctrl+C: 如果星瑶正在执行，则中断执行；如果闲置，则清空输入框"""
        global is_thinking, confirm_prompt
        if is_thinking:
            import core.llm
            core.llm.cancel_requested = True
            print_formatted_text(HTML("\n<ansired>🛑 收到中断信号，正在终止星瑶的执行...</ansired>"))
            # 如果 TUI 确认面板的 Future 还在等待，立即以"拒绝"(index=1)解除阻塞，防止死锁
            if confirm_prompt is not None and 'future' in confirm_prompt:
                fut = confirm_prompt['future']
                if not fut.done():
                    event.app.loop.call_soon_threadsafe(lambda: fut.set_result(1))
                confirm_prompt = None
                event.app.invalidate()
        else:
            input_area.text = ""


    @kb.add("c-d")
    def exit_app(event):
        """Ctrl+D 退出"""
        event.app.exit()

    @kb.add("up", filter=Condition(lambda: confirm_prompt is not None))
    def confirm_arrow_up(event):
        global confirm_prompt
        if confirm_prompt:
            confirm_prompt['current_idx'] = (confirm_prompt['current_idx'] - 1) % len(confirm_prompt['options'])
            event.app.invalidate()

    @kb.add("down", filter=Condition(lambda: confirm_prompt is not None))
    def confirm_arrow_down(event):
        global confirm_prompt
        if confirm_prompt:
            confirm_prompt['current_idx'] = (confirm_prompt['current_idx'] + 1) % len(confirm_prompt['options'])
            event.app.invalidate()

    @kb.add("y", filter=Condition(lambda: confirm_prompt is not None))
    def confirm_y(event):
        global confirm_prompt
        if confirm_prompt:
            fut = confirm_prompt['future']
            confirm_prompt = None
            event.app.loop.call_soon_threadsafe(lambda: fut.set_result(0))
            event.app.invalidate()

    @kb.add("n", filter=Condition(lambda: confirm_prompt is not None))
    def confirm_n(event):
        global confirm_prompt
        if confirm_prompt:
            fut = confirm_prompt['future']
            confirm_prompt = None
            event.app.loop.call_soon_threadsafe(lambda: fut.set_result(1))
            event.app.invalidate()

    @kb.add("a", filter=Condition(lambda: confirm_prompt is not None))
    def confirm_a(event):
        global confirm_prompt
        if confirm_prompt:
            fut = confirm_prompt['future']
            confirm_prompt = None
            event.app.loop.call_soon_threadsafe(lambda: fut.set_result(2))
            event.app.invalidate()

    @kb.add("c-t")
    def toggle_memory_summary(event):
        """Ctrl+T 切换记忆与任务拆解面板显示状态"""
        global show_memory_summary
        show_memory_summary = not show_memory_summary
        event.app.invalidate()

    @kb.add("s-tab")
    def toggle_permission_mode(event):
        """Shift+Tab 循环切换 Y-CODE 权限模式"""
        global current_permission_mode
        modes = ["confirm", "plan", "full"]
        try:
            curr_idx = modes.index(current_permission_mode)
        except ValueError:
            curr_idx = 0
        next_mode = modes[(curr_idx + 1) % len(modes)]
        current_permission_mode = next_mode
        from config import save_config_value
        save_config_value("permission_mode", next_mode)
        event.app.invalidate()

    def get_status():
        """底部状态栏内容：极简无底色，带 Token 统计与后台任务监控"""
        # 1. 提取短模型名（去掉前缀 openai/ 等）
        model_display = current_model_name

        # 2. 计算 Token 消耗与缓存命中（分离概念）
        cum_total = session_token_usage["cumulative_total"]
        cum_cached = session_token_usage["cumulative_cached"]
        cum_miss = max(0, cum_total - cum_cached)
        
        latest_total = session_token_usage["latest_total"]
        max_win = current_ctx_win
        
        hit_rate = (cum_cached / cum_total * 100) if cum_total > 0 else 0
        pct = (latest_total / max_win * 100) if max_win > 0 else 0
        
        def format_num(n):
            if n >= 1_000_000:
                val = n / 1_000_000
                return f"{int(val)}M" if val.is_integer() else f"{val:.1f}M"
            if n >= 1_000:
                val = n / 1_000
                return f"{int(val)}K" if val.is_integer() else f"{val:.1f}K"
            return str(n)
            
        cum_total_str = format_num(cum_total)
        latest_total_str = format_num(latest_total)
        max_win_str = format_num(max_win)
        
        # 计算 8 字符宽度的进度条
        bar_width = 8
        filled_count = int(pct / (100 / bar_width))
        filled_count = max(0, min(bar_width, filled_count))
        if latest_total > 0 and filled_count == 0:
            filled_count = 1
        empty_count = bar_width - filled_count
        bar = "█" * filled_count + "░" * empty_count
        
        token_info = f"tokens: {cum_total_str} | 上下文: [{bar}] {latest_total_str}/{max_win_str} | 缓存命中: {hit_rate:.1f}%"

        # 3. 统计星瑶的执行状态与播放动画，证明星瑶仍在操作/思考
        import main
        status_map = {
            "idle": (" 闲置 ", "class:status.task_idle"),
            "thinking": (" 思考中... ", "class:status.task_active"),
            "executing": (" 执行命令中... ", "class:status.task_active"),
            "waiting": (" 等待授权确认... ", "class:status.task_active"),
        }
        status_label, task_style = status_map.get(main.task_status, (" 闲置 ", "class:status.task_idle"))
        
        if main.task_status != "idle":
            # 播放流式旋转动画
            elapsed = time.time()
            frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            frame = frames[int(elapsed * 5) % len(frames)]
            task_info = f" {frame}{status_label}"
        else:
            task_info = status_label

        # 2.5 渲染权限模式标识
        perm_style = "class:status.permission_confirm"
        perm_label = " 变更前确认 "
        if current_permission_mode == "confirm":
            perm_label = " 变更前确认 "
            perm_style = "class:status.permission_confirm"
        elif current_permission_mode == "plan":
            perm_label = " 计划模式 "
            perm_style = "class:status.permission_plan"
        elif current_permission_mode == "full":
            perm_label = " 完全访问 "
            perm_style = "class:status.permission_full"

        return [
            ("class:status.model",   f" {model_display} "),
            ("class:status.sep",     "  |  "),
            (perm_style,             perm_label),
            ("class:status.sep",     "  |  "),
            ("class:status.token",   token_info),
            ("class:status.sep",     "  |  "),
            (task_style,             task_info),
        ]

    def get_thinking_text():
        """渲染独立区域的思考中状态"""
        if is_thinking:
            elapsed = time.time() - think_start_time
            frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            frame = frames[int(elapsed * 10) % len(frames)]
            return f"  {frame} 星瑶正在思考... {elapsed:.1f}s"
        return ""

    @Condition
    def is_thinking_cond():
        return is_thinking

    # 确认面板内容生成器
    def get_confirm_prompt_content():
        if not confirm_prompt:
            return HTML("")
        func = confirm_prompt['function_name']
        args = confirm_prompt['arguments']
        text = ""
        if func == "execute_command":
            cmd = args.get('command', '')
            cmd_lines = cmd.splitlines()
            if len(cmd_lines) > 2:
                cmd_display = "\n".join(cmd_lines[:2]) + f"\n    ... (共 {len(cmd_lines)} 行)"
            else:
                cmd_display = cmd
            if len(cmd_display) > 250:
                cmd_display = cmd_display[:247] + "..."
            cmd_escaped = cmd_display.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            text = f"💻  星瑶申请执行命令: {cmd_escaped}\n"
        elif func == "write_file":
            file_path = args.get('file_path', '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            text = f"✍️  星瑶申请写入文件: {file_path}\n"
        elif func == "edit_file":
            file_path = args.get('file_path', '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            text = f"📝  星瑶申请修改文件: {file_path}\n"
            
        text += "👉 请选择确认操作:\n"
        for idx, opt in enumerate(confirm_prompt['options']):
            prefix = "  > " if idx == confirm_prompt['current_idx'] else "    "
            if idx == confirm_prompt['current_idx']:
                text += f"<ansigreen><b>{prefix}{opt}</b></ansigreen>\n"
            else:
                text += f"{prefix}{opt}\n"
        return HTML(text)

    confirm_window = ConditionalContainer(
        content=Frame(
            Window(
                content=FormattedTextControl(get_confirm_prompt_content),
                height=D(min=5, max=10),
                wrap_lines=True
            ),
            title="🚨 敏感操作授权确认 (用方向键选择，回车确认)",
            style="class:status.confirm-frame"
        ),
        filter=Condition(lambda: confirm_prompt is not None)
    )

    # 记忆与任务拆解面板内容生成器
    def get_memory_summary_content():
        summary = manager.get_memory_summary(current_session) or "暂无会话记忆摘要。"
        
        # 从历史消息中自动提取大模型拆解的步骤
        history = manager.get_history(current_session)
        tasks = []
        import re
        for msg in reversed(history):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                lines = content.splitlines()
                for line in lines:
                    m = re.search(r"[-*]\s*\[([ x/])\]\s*(.*)", line)
                    if not m:
                        m = re.search(r"\d+\.\s*\[([ x/])\]\s*(.*)", line)
                    if m:
                        status_char = m.group(1)
                        task_desc = m.group(2).strip()
                        status_label = "⏳ 待执行"
                        if status_char == 'x': status_label = "✅ 已完成"
                        elif status_char == '/': status_label = "⚡ 执行中"
                        tasks.append(f"  {status_label} | {task_desc}")
                if tasks:
                    break
                    
        display_text = f"💡 [会话记忆摘要]\n  {summary}\n"
        if tasks:
            display_text += "\n📋 [当前任务规划]\n" + "\n".join(tasks)
        else:
            display_text += "\n📋 [当前任务规划]\n  暂无计划步骤 (可在对话中输入以产生任务)"
            
        return display_text

    memory_window = ConditionalContainer(
        content=Frame(
            Window(
                content=FormattedTextControl(get_memory_summary_content),
                height=6,
                wrap_lines=True
            ),
            title="🧠 会话记忆与任务拆解面板 (Ctrl+T 折叠/展开)",
            style="class:status.memory-frame"
        ),
        filter=Condition(lambda: show_memory_summary)
    )

    root_container = HSplit([
        confirm_window,
        memory_window,
        ConditionalContainer(
            content=Window(
                content=FormattedTextControl(get_thinking_text),
                height=1,
                style="class:status.think"
            ),
            filter=is_thinking_cond
        ),
        Frame(
            input_area,
            style="class:input-frame",
        ),
        Window(
            content=FormattedTextControl(get_status),
            height=1,
        ),
    ])
    
    # 真正的下拉菜单容器
    main_container = FloatContainer(
        content=root_container,
        floats=[
            Float(
                xcursor=True,
                ycursor=True,
                content=CompletionsMenu(max_height=10, scroll_offset=1)
            )
        ]
    )

    layout = Layout(main_container)

    from core.themes import THEMES
    theme_cfg = THEMES.get(current_theme, THEMES["cyan"])
    border_color = theme_cfg.get("border", "#00b4d8")
    label_color = theme_cfg.get("label", "bold #00b4d8")
    status_main = theme_cfg.get("status_main", "bold #00b4d8")
    status_text = theme_cfg.get("status_text", "#718096")
    status_sep = theme_cfg.get("status_sep", "#445566")
    text_input = theme_cfg.get("text_input", "#dde1e7")
    menu_current = theme_cfg.get("menu_current", "bg:#00b4d8 #ffffff bold")
    menu_current_meta = theme_cfg.get("menu_current_meta", "bg:#00b4d8 #ffffff")

    style = Style.from_dict({
        # 输入框边框
        "input-frame":     "bg:#0a0a0f",
        "status.memory-frame": f"bg:#0a0a0f {status_text}",

        "status.confirm-frame": f"bg:#0a0a0f {status_main} bold",
        "frame.border":    border_color,
        "frame.label":     label_color,
        # 输入文字
        "input-field":     f"bg:#0a0a0f {text_input}",
        # 状态栏：去掉所有底色，直接使用前景色
        "status.model":    status_main,
        "status.sep":      status_sep,
        "status.token":    status_text,
        "status.think":    "bold #e2e8f0",
        "status.task_active": "bold #39ff14",  # 矩阵亮绿
        "status.task_idle":   status_sep,
        "status.permission_confirm": "bold #50fa7b",
        "status.permission_plan":    "bold #88c0d0",
        "status.permission_full":    "bold #ffb86c",
        
        # 下拉补全菜单的颜色
        "completion-menu": "bg:#1a2a3a #e2e8f0",
        "completion-menu.completion": "bg:#1a2a3a #e2e8f0",
        "completion-menu.completion.current": menu_current,
        "completion-menu.meta.completion": "bg:#1a2a3a #718096",
        "completion-menu.meta.completion.current": menu_current_meta,
    })

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=False,
        mouse_support=False,
        erase_when_done=True,
    )

    return app, result


def _background_worker():
    """后台消费者线程：专门处理排队的对话请求"""
    global is_thinking, think_start_time, plan_approved_this_turn, task_status
    while True:
        task_data = msg_queue.get()
        if task_data is None:
            break
        # 新一轮对话，重置计划模式授权状态与已用子 Agent 编号缓存
        plan_approved_this_turn = False
        try:
            from core.tools import used_subagent_numbers_this_turn, subagents_lock
            with subagents_lock:
                used_subagent_numbers_this_turn.clear()
        except Exception:
            pass
            
        user_input = task_data["text"]
        was_queued = task_data["was_queued"]
        is_skill_call = task_data.get("is_skill_call", False)
        
        # 判定如果是系统 slash 密令且非主动技能调用，在此同步路由
        if user_input.startswith('/') and not is_skill_call:
            handle_slash_command(user_input)
        else:
            task_status = 'thinking'
            if was_queued:
                safe_input = user_input.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                display_input = safe_input[:50] + "..." if len(safe_input) > 50 else safe_input
                print_formatted_text(HTML(f"⚡ <b><ansigreen>[开始执行排队任务]</ansigreen></b> <ansigray>{display_input}</ansigray>"))
                
            # 捕捉配置
            task_session = current_session
            task_model = current_model
            task_key = current_key
            task_url = current_url
            task_ctx_win = current_ctx_win
            
            from core.input_parser import parse_user_input
            if is_skill_call:
                # 提取短 ID 与实际的提问及挂载文件
                parts = user_input.split(maxsplit=1)
                skill_name = parts[0][1:]
                actual_query = parts[1] if len(parts) > 1 else "请启动并运行该技能。"
                
                print_formatted_text(HTML(f"⚡ <b><ansigreen>[主动调用技能: {skill_name}]</ansigreen></b>"))
                
                parsed_content, attached_msgs = parse_user_input(actual_query)
                # 在发送前强行贴上显式激活指示，指导大模型重点发挥对应技能作用
                instruction = f"【指示：用户已明确指定并主动调用技能【{skill_name}】来处理以下提问。请务必优先执行该技能指令并做出响应。】\n提问："
                if isinstance(parsed_content, list):
                    for item in parsed_content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            item["text"] = instruction + item.get("text", "")
                            break
                else:
                    parsed_content = instruction + parsed_content
            else:
                parsed_content, attached_msgs = parse_user_input(user_input)
                # 在终端打印挂载的文件提示
                for msg in attached_msgs:
                    safe_msg = msg.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    print_formatted_text(HTML(f"  <ansigreen>{safe_msg}</ansigreen>"))
            
            # 将用户最新输入存入数据库
            manager.add_message(task_session, "user", parsed_content)
            
            # 【阶段4】后台静默清理超长历史文件记录
            manager.prune_history_files(task_session, max_turns=16, retain_recent=4)
            
            # 获取专门供给大模型的已压缩截断与记忆挂载历史
            history = manager.get_history_for_llm(task_session)
            
            # 【全新机制】在向大模型请求前，实时估算当前历史的 Token 数
            from core.llm import session_token_usage, estimate_tokens
            estimated_tokens = estimate_tokens(task_model, history)
            
            if estimated_tokens > task_ctx_win * 0.8:
                # 超过 80% 安全线，触发智能记忆压缩提炼
                from core.memory_compressor import compress_session_memory
                compress_session_memory(manager, task_session, task_model, task_key, task_url)
                history = manager.get_history_for_llm(task_session)
                estimated_tokens = estimate_tokens(task_model, history)
                
                # 兜底：如果总结归纳后依然超限，触发物理大文件折叠机制
                if estimated_tokens > task_ctx_win * 0.8:
                    manager.prune_history_files(task_session, max_turns=1, retain_recent=4)
                    history = manager.get_history_for_llm(task_session)
                    estimated_tokens = estimate_tokens(task_model, history)
                
                # 智能滑窗淘汰：如果仍超限，按轮次（user+assistant）逐步从内存中丢弃最早的旧消息，直到满足 Token 安全阈值
                while len(history) > 2 and estimated_tokens > task_ctx_win * 0.8:
                    removed = 0
                    i = 0
                    while i < len(history) and removed < 2:
                        if history[i].get("role") != "system":
                            history.pop(i)
                            removed += 1
                        else:
                            i += 1
                    if removed < 2:
                        break
                    estimated_tokens = estimate_tokens(task_model, history)
                
            # 更新最新 Token 数供界面显示
            session_token_usage["latest_total"] = estimated_tokens
            
            # 动态注入最新的人设，同时丢弃历史里可能缓存的老人设
            history = [m for m in history if m.get("role") != "system"]
            import os
            import datetime
            from config import SYSTEM_PROMPT_FILE
            prompt_path = SYSTEM_PROMPT_FILE
            system_prompt = ""
            if os.path.exists(prompt_path):
                try:
                    with open(prompt_path, "r", encoding="utf-8") as f:
                        system_prompt = f.read().strip()
                except Exception:
                    pass
            
            if not system_prompt:
                system_prompt = (
                    "你叫星瑶，是一个顶尖的女性黑客与代码编程高手。\n"
                    "在给出回答前，请先将思考过程用 <think> 和 </think> 标签包裹起来输出。\n\n"
                    "【💬 终端输出格式规范】\n"
                    "由于你的回答会被直接输出在没有 Markdown 渲染引擎的终端中，因此：\n"
                    "1. 严禁使用诸如 **（加粗）或 *（斜体）等 Markdown 标记，这会在终端直接显示为难看的星号。请直接输出纯文本，结构和重点通过换行或缩进展示。\n"
                    "2. 尽量少用复杂的 Markdown 原生语法（如 `|` 表格等），在终端中请使用缩进和换行来展示结构化数据。\n"
                )
            
            # 动态前置注入实时系统时间与星期，消除大模型“无法获取实时时间”的障碍
            try:
                now = datetime.datetime.now()
                weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
                time_info = f"【🔔 当前系统实时时间：{now.strftime('%Y-%m-%d %H:%M:%S')} {weekdays[now.weekday()]}】\n\n"
                system_prompt = time_info + system_prompt
            except Exception:
                pass
            
            # 追加已启用的通用 Markdown Skills
            from core.skills import get_active_skills_markdown
            system_prompt += get_active_skills_markdown()
            
            history.insert(0, {"role": "system", "content": system_prompt})

            # 开启底部状态栏的思考动画与计时器
            is_thinking = True
            think_start_time = time.time()
                
            # 思考期间界面重绘已交由全局 UI 刷新线程托管，此处无需重复启动本地子线程

            try:
                # 调用大模型 (使用捕捉的快照配置进行调用，保障竞态安全)
                response_gen = chat_with_model(task_model, task_key, history, task_url, stream=True)
                    
                # 打印回复头部
                print_formatted_text(HTML("\n<b><magenta>🤖 星瑶 &gt;</magenta></b> "))
                    
                full_raw_reply = ""
                line_buffer = ""
                in_think = False
                
                try:
                    for chunk in response_gen:
                        full_raw_reply += chunk
                        for char in chunk:
                            line_buffer += char
                            if char == '\n':
                                in_think = print_line_segments(line_buffer, in_think)
                                line_buffer = ""
                                
                    if line_buffer:
                        print_line_segments(line_buffer + "\n", in_think)
                                
                    is_thinking = False
                    if app_instance:
                        app_instance.invalidate()
                except KeyboardInterrupt:
                    print_formatted_text(HTML("\n<ansired>🛑 任务已被用户强行中断！</ansired>\n"))
                    is_thinking = False
                    task_status = 'idle'
                    import core.llm
                    core.llm.cancel_requested = False
                    if app_instance:
                        app_instance.invalidate()
                    msg_queue.task_done()
                    continue

                print_formatted_text("\n")
                
                # 从原始内容中剔除 think 标签存入正式的历史记录
                import re
                final_content = full_raw_reply
                think_match = re.search(r"<(?:think|thinking)>(.*?)</(?:think|thinking)>", full_raw_reply, flags=re.DOTALL)
                if think_match:
                    final_content = full_raw_reply.replace(think_match.group(0), "").strip()
                else:
                    unclosed_match = re.search(r"<(?:think|thinking)>(.*)", full_raw_reply, flags=re.DOTALL)
                    if unclosed_match:
                        final_content = full_raw_reply[:unclosed_match.start()].strip()

                manager.add_message(task_session, "assistant", final_content)
                
                # 检查是否需要自动重命名会话（当且仅当是新建的默认 hex 会话）
                if re.match(r"^session_[a-f0-9]{6}$", task_session):
                    history = manager.get_history(task_session)
                    user_msgs = [m for m in history if m.get("role") == "user"]
                    if user_msgs:
                        first_user_query = user_msgs[0].get("content", "")
                        if first_user_query:
                            trigger_session_auto_rename(
                                task_session, 
                                first_user_query, 
                                task_model, 
                                task_key, 
                                task_url
                            )
                    
                # 请求完成，通知 UI 刷新底部的 Token 状态栏
                task_status = 'idle'
                if app_instance:
                    app_instance.invalidate()
            except Exception as e:
                is_thinking = False
                task_status = 'idle'
                if app_instance:
                    app_instance.invalidate()
                safe_e = str(e).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                print_formatted_text(HTML(f"\n<ansired>⚠️ 连接或回复生成失败: {safe_e}</ansired>\n"))
                    
        msg_queue.task_done()

def run_cli():
    """主入口 — 封装在 while 循环里，支持弹出全屏选择器后自动重启"""
    global app_instance, pending_action, current_session
    global current_model, current_key, current_url, current_ctx_win

    print_logo()

    # 首次启动且 API Key 为空时，提示输入 DeepSeek 密钥并保存
    if not current_key:
        print("\n🔑 检测到您尚未配置大模型 API 密钥。")
        print("已为您预设默认模型：deepseek-v4-pro")
        print("接口协议：OpenAI | 厂商：深度求索 (DeepSeek) | 上下文支持：1M (1,000,000)")
        print("请在下方输入您的 DeepSeek API 密钥 (API Key)：")
        try:
            key_input = input("👉 API Key: ").strip()
            if key_input:
                current_key = key_input
                # 持久化保存到配置文件中
                from core.model_manager import load_config, _write_config
                cfg = load_config()
                for provider in cfg.get("providers", []):
                    if provider.get("id") == "deepseek-provider":
                        provider["api_key"] = key_input
                        break
                _write_config(cfg)
                print("✔ 密钥已成功保存到配置文件中！")
            else:
                print("⚠️ 未输入密钥，系统将使用空密钥启动。")
        except (KeyboardInterrupt, EOFError):
            print("\n👋 启动已取消。")
            import sys
            sys.exit(0)

    # 启动后台处理线程
    worker_thread = threading.Thread(target=_background_worker, daemon=True)
    worker_thread.start()

    # 启动全局常驻 UI 刷新线程（用以维持思考计时器和星瑶运转状态微动画）
    def _ui_refresher_loop():
        while True:
            # 只要星瑶在操作或思考，就主动刷新 UI 以更新旋转动画
            if is_thinking:
                if app_instance:
                    app_instance.invalidate()
            time.sleep(0.2)
            
    threading.Thread(target=_ui_refresher_loop, daemon=True).start()

    # 首次启动，计算当前 session 的 Token
    recalc_token_usage()

    # 主循环：支持 /models 弹出选择器并重启主 App
    while True:
        pending_action = None
        app_instance, _ = _make_input_app()

        with patch_stdout():
            try:
                app_instance.run()
            except (KeyboardInterrupt, EOFError):
                pass

        if pending_action == "open_models":
            # 在主线程安全地开启全屏选择器
            from core.model_selector import open_model_selector

            def _on_selected(info: dict):
                global current_model, current_key, current_url, current_ctx_win, current_model_name
                current_model   = info["litellm_model"]
                current_model_name = info["name"]
                current_key     = info["api_key"]
                current_url     = info["base_url"]
                current_ctx_win = info["context_window"]

            open_model_selector(on_model_selected=_on_selected)
            # 选择器关闭后，因为可能切了模型，重新算一下 Token
            recalc_token_usage()
            
            pending_action = None
            continue
            
        elif pending_action == "open_skills":
            from core.skill_selector import open_skill_selector
            open_skill_selector()
            pending_action = None
            continue
            
        elif pending_action == "open_sessions":
            from core.session_selector import open_session_selector
            switched_session = [None]
            rename_target = [None]
            
            def _on_switch(new_session):
                global current_session
                current_session = new_session
                switched_session[0] = new_session
                
            def _on_rename(session_name):
                rename_target[0] = session_name
                
            open_session_selector(on_switch=_on_switch, on_rename=_on_rename)
            
            # 选择器全屏 App 已退出，判断是否在里面按了 [R/F2] 触发重命名
            if rename_target[0]:
                target = rename_target[0]
                try:
                    new_name = input(f"请输入会话「{target}」的新名称: ").strip()
                    if new_name:
                        success, final_name = manager.rename_session(target, new_name)
                        if success:
                            if current_session == target:
                                current_session = final_name
                            safe_final = final_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                            print_formatted_text(HTML(f"<ansigreen>✔ 会话已成功重命名为: {safe_final}</ansigreen>"))
                            recalc_token_usage()
                            print_logo(clear_screen=False)
                        else:
                            print_formatted_text(HTML("<ansired>❌ 会话重命名失败。</ansired>"))
                except (KeyboardInterrupt, EOFError):
                    print_formatted_text(HTML("\n<ansiyellow>⚠️ 已取消重命名操作。</ansiyellow>"))
            
            # 防御：检测当前会话是否在选择器里被物理删除了，自动安全切回
            sessions = manager.get_all_sessions()
            session_names = [s["name"] for s in sessions]
            if current_session not in session_names:
                if session_names:
                    current_session = session_names[0]
                else:
                    import uuid
                    current_session = f"session_{uuid.uuid4().hex[:6]}"
                recalc_token_usage()
                print_logo(clear_screen=True)
                show_session_history(current_session)
            elif switched_session[0]:
                recalc_token_usage()
                safe_session = switched_session[0].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                print_formatted_text(HTML(f"\n<ansigreen>Switching to session {safe_session}...</ansigreen>"))
                print_logo(clear_screen=False)
                show_session_history(switched_session[0])
                
            pending_action = None
            continue
            
        else:
            # 正常退出（Ctrl+D 或其他原因）
            break


if __name__ == "__main__":
    run_cli()