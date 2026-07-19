import difflib
import re

def generate_diff(old_content, new_content, file_path):
    """生成标准 unified diff"""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    
    diff = list(difflib.unified_diff(
        old_lines, 
        new_lines, 
        fromfile=f"a/{file_path}", 
        tofile=f"b/{file_path}"
    ))
    return "".join(diff)

def render_diff_table(diff_text):
    """
    将 unified diff 文本解析并渲染为带有双列行号的 Rich Table。
    这能真实反映文件中的原始行号，而不仅仅是 Diff 字符串的序号。
    """
    from rich.table import Table
    from rich.text import Text

    table = Table(show_header=False, box=None, padding=(0, 1), collapse_padding=True)
    table.add_column(justify="right", style="dim")
    table.add_column(justify="right", style="dim")
    table.add_column()

    old_ln = 0
    new_ln = 0
    
    lines = diff_text.split('\n')
    for line in lines:
        if not line:
            continue
        # 只跳过真正的文件头（--- a/... 与 +++ b/...），
        # 避免把以 ---/+++ 开头的增删内容行误判为文件头而丢行
        if re.match(r"^--- a/", line) or re.match(r"^\+\+\+ b/", line):
            continue
        elif line.startswith('@@'):
            # 解析 @@ -9,4 +9,4 @@
            try:
                parts = line.split(' ')
                old_start = int(parts[1].split(',')[0].replace('-', ''))
                new_start = int(parts[2].split(',')[0].replace('+', ''))
                old_ln = old_start
                new_ln = new_start
            except Exception:
                # 解析失败时重置行号，避免沿用上一个 hunk 的行号
                old_ln = 0
                new_ln = 0
            table.add_row('...', '...', Text(line, style="cyan"))
        elif line.startswith('\\'):
            # "\ No newline at end of file" 只是注释行，不占文件行号
            continue
        elif line.startswith('-'):
            table.add_row(str(old_ln), '', Text(line, style="red"))
            old_ln += 1
        elif line.startswith('+'):
            table.add_row('', str(new_ln), Text(line, style="green"))
            new_ln += 1
        else:
            table.add_row(str(old_ln), str(new_ln), Text(line, style="default"))
            old_ln += 1
            new_ln += 1
            
    return table
