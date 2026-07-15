import os
import subprocess
from ddgs import DDGS

def read_file(file_path):
    """读取文件内容并返回"""
    with open(file_path, 'r', encoding='utf-8') as f:
        return f.read()

def write_file(file_path, content):
    """将内容写入文件"""
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)

# --- 下面是给星瑶装配的两个新神装 ---

def list_directory(dir_path="."):
    """雷达工具：扫描文件夹，返回里面的所有文件和文件夹列表"""
    try:
        items = os.listdir(dir_path)
        return "\n".join(items)
    except Exception as e:
        return f"🚨 目录扫描失败: {str(e)}"

def check_and_install_dependencies(command):
    """检测命令是否执行了 skills 中的 python 脚本，若是则自动检查并热安装缺少的库依赖"""
    import re
    # 匹配形如 ...skills/<skill_name>/scripts/<script>.py 的路径
    match = re.search(r'[\'"]?([^\'"\s]*skills/[^\'"\s]+/scripts/[^\'"\s]+\.py)[\'"]?', command)
    if not match:
        return
        
    script_path = match.group(1)
    if not os.path.exists(script_path):
        return
        
    skill_dir = os.path.dirname(os.path.dirname(script_path))
    req_file = os.path.join(skill_dir, "requirements.txt")
    deps = []
    
    # 1. 尝试从 requirements.txt 中读取依赖包名
    if os.path.exists(req_file):
        try:
            with open(req_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        dep_name = re.split(r'[=<>~!]', line)[0].strip()
                        if dep_name:
                            deps.append(dep_name)
        except Exception:
            pass
            
    # 2. 如果无 requirements.txt，通过正则粗暴静态扫描 import/from 关键字提取模块名作为备用
    if not deps:
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                code = f.read()
            found_imports = re.findall(r'^\s*(?:import|from)\s+(\w+)', code, re.MULTILINE)
            import sys
            std_libs = set(sys.builtin_module_names)
            for m in found_imports:
                if m not in std_libs and m not in ("core", "utils", "config", "main") and not m.startswith("_"):
                    deps.append(m)
        except Exception:
            pass
            
    # 3. 对提取出的依赖进行环境 specs 检查，找出缺失的依赖包
    missing_deps = []
    import importlib.util
    for dep in set(deps):
        dep_base = dep.split(".")[0].strip()
        if not dep_base:
            continue
        try:
            if importlib.util.find_spec(dep_base) is None:
                local_dir = os.path.dirname(script_path)
                if not os.path.exists(os.path.join(local_dir, f"{dep_base}.py")) and \
                   not os.path.exists(os.path.join(local_dir, dep_base)):
                    missing_deps.append(dep_base)
        except Exception:
            pass
            
    # 4. 如果发现依赖缺失，自动跑 pip install 进行热装补全！
    if missing_deps:
        missing_str = ", ".join(missing_deps)
        from prompt_toolkit import print_formatted_text
        from prompt_toolkit.formatted_text import HTML
        print_formatted_text(HTML(f"\n<ansiyellow>📦 检测到技能脚本缺少运行依赖: <b>{missing_str}</b>，正在自动热安装...</ansiyellow>"))
        try:
            pip_cmd = f"pip install {' '.join(missing_deps)}"
            subprocess.run(pip_cmd, shell=True, capture_output=True, timeout=60)
            print_formatted_text(HTML("<ansigreen>✔ 依赖包自动热装补齐成功！开始执行命令...</ansigreen>\n"))
        except Exception as install_err:
            print_formatted_text(HTML(f"<ansired>⚠️ 自动安装依赖失败: {install_err}</ansired>\n"))

def execute_command(command, run_in_background=False):
    """引擎工具：在本地终端默默执行命令，并把终端里的输出抓取回来"""
    # 自动做前置依赖检测与安装
    try:
        check_and_install_dependencies(command)
    except Exception:
        pass
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
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=30
        )
        
        output = result.stdout
        if result.stderr:
            output += f"\n[附加信息/警告]:\n{result.stderr}"
            
        return output or "执行成功，但命令没有返回任何输出内容。"
        
    except subprocess.TimeoutExpired:
        return "🚨 命令执行超时（超过 30 秒），已被强行终止！"
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
    
def search_code(query, dir_path="."):
    """精准搜索：跨文件全局查找代码片段或关键字"""
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

def manage_task(action, task_id=None):
    """
    后台进程管理器：对丢在后台运行的长周期任务（如服务器）进行查杀和状态管理。
    参数:
      action: 可选值：'list'（查看所有运行中的后台任务）, 'status'（查看指定任务的实时日志尾部）, 'kill'（强制杀掉指定任务）。
      task_id: 目标任务的 ID，如果是 'status' 或 'kill' 行动，则为必填项。
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
        success = kill_background_task(task_id)
        if success:
            return f"✔ 后台任务 {task_id} 已被强行终结清除。"
        else:
            return f"🚨 未能成功杀掉任务 {task_id}（可能进程已经提前结束，或找不到 ID）。"
            
    else:
        return "🚨 错误：未知行动参数。可选值有 'list', 'status', 'kill'。"

def edit_file(file_path, target_content, replacement_content):
    """
    手术刀局部替换工具：用于对已有代码文件进行点对点局部精准替换，不用每次都重写整张文件，避免开销和误删。
    参数:
      file_path: 要修改的文件的相对或绝对路径。
      target_content: 文件中要被替换的旧代码段（必须完全一致，注意保留原始缩进和换行）。
      replacement_content: 准备换上的新代码段。
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # 写入撤销栈，以防出错
        from core.memory import set_undo_state
        set_undo_state(file_path, content)
        
        if target_content not in content:
            # 容错：如果换行符因为跨平台不匹配 (\r\n vs \n)
            norm_content = content.replace("\r\n", "\n")
            norm_target = target_content.replace("\r\n", "\n")
            if norm_target in norm_content:
                new_content = norm_content.replace(norm_target, replacement_content.replace("\r\n", "\n"))
            else:
                return (
                    f"🚨 精准修改失败！在文件 {file_path} 中没有找到指定的旧代码段。\n"
                    f"请确保你传入的 target_content 的缩进、空格、换行完全匹配，或者你可以尝试使用 write_file 覆写工具。"
                )
        else:
            new_content = content.replace(target_content, replacement_content)
            
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
            
        return f"✔ 文件 {file_path} 精准替换修改成功！"
    except Exception as e:
        return f"🚨 精准修改发生异常: {str(e)}"
