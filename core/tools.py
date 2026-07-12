import os
import subprocess
import urllib.request
import urllib.parse
import re
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
        # os.listdir 会列出该目录下所有的文件名字
        items = os.listdir(dir_path)
        # 把列表拼成一段带有换行的文字返回给大模型
        return "\n".join(items)
    except Exception as e:
        return f"🚨 目录扫描失败: {str(e)}"

def execute_command(command):
    """引擎工具：在本地终端默默执行命令，并把终端里的输出抓取回来"""
    try:
        # subprocess.run 是 Python 里极其强大的命令执行器
        result = subprocess.run(
            command,
            shell=True,          # 允许执行复杂的 shell 命令
            capture_output=True, # 把本该打印在屏幕上的结果偷偷抓到变量里
            text=True,           # 把抓到的字节直接变成普通文字
            encoding='utf-8',    # 🌟 强行指定 UTF-8 编码
            errors='replace',    # 🌟 遇到奇葩字符直接替换，绝不报错崩溃！
            timeout=30           # 加上 30 秒超时限制，防止星瑶写个死循环卡死电脑
        )
        
        # 很多命令即使成功了，也会把信息输出到 stderr (比如安装库的进度条)
        # 所以我们把标准输出 (stdout) 和标准错误 (stderr) 结合起来返回
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
            # 直接调用底层接口，绕过反爬虫，告别脆弱的正则表达式
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
    import os
    
    # 需要过滤的无关目录
    IGNORE_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.idea', '.vscode', '.ycode', 'dist', 'build'}
    # 允许扫描的文件扩展名（防乱码和二进制文件）
    ALLOWED_EXTS = {
        '.py', '.js', '.ts', '.html', '.css', '.json', '.md', '.txt', 
        '.java', '.c', '.cpp', '.h', '.go', '.rs', '.php', '.rb', '.sh',
        '.yml', '.yaml', '.toml', '.ini', '.cfg', '.xml'
    }
    
    results = []
    match_count = 0
    max_matches = 50 # 限制最多返回 50 条结果，防止撑爆上下文
    
    try:
        for root, dirs, files in os.walk(dir_path):
            # 过滤无关目录
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
                            # 获取前后文 (各 1 行)
                            start = max(0, line_num - 2)
                            end = min(len(lines), line_num + 1)
                            snippet = "".join(lines[start:end])
                            
                            results.append(f"📄 {file_path} (行 {line_num}):\n{snippet.strip()}")
                            match_count += 1
                            
                            if match_count >= max_matches:
                                results.append(f"\n⚠️ 搜索结果太多，已截断前 {max_matches} 条，请尝试更精确的关键字。")
                                return "\n---\n".join(results)
                except Exception:
                    # 忽略读取失败的文件（比如非 utf-8）
                    continue
                    
        if not results:
            return f"🔍 搜索完毕，未在代码文件中找到包含 `{query}` 的内容。"
        return "\n---\n".join(results)
    except Exception as e:
        return f"🚨 搜索失败: {str(e)}"
