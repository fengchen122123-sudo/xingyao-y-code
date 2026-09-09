"""会话分享模块。

设计目标：把会话导出为可分享的 Markdown/HTML 格式，便于团队协作和归档。
参考：OpenCode 的 share 模块

用法：
- export_session_markdown(session_name): 导出为 Markdown
- export_session_html(session_name): 导出为 HTML（含样式）
- export_session_json(session_name): 导出为默认脱敏 JSON

与 OpenCode 区别：
- 不做服务端分享链接（Y-code 是本地工具）
- 仅本地导出文件，用户自行分享
"""

import datetime
import html
import json
import re
import os
from pathlib import Path

import core.app_context as app_context


def _redact_export_text(value):
    text = str(value or "")
    try:
        from core.model_manager import load_config
        for provider in load_config().get("providers", []):
            secret = str(provider.get("api_key") or "")
            if secret:
                text = text.replace(secret, "***")
    except Exception:
        pass
    text = re.sub(r"(?i)\bBearer\s+[^\s\"',;]+", "Bearer ***", text)
    text = re.sub(r"(?i)([a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)", r"\1***\2", text)
    # 按 key 名匹配敏感字段：token/secret/password/key= 等 k=v / k:v 形式
    text = re.sub(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|token|secret|password|passwd|auth|key)\s*[=:]\s*)[^\s,;&\"']+",
        r"\1***",
        text,
    )
    # 常见 provider 密钥前缀（dash/underscore 分隔族 + Google AIza / AWS AKIA|ASIA / Google OAuth ya29.）
    text = re.sub(
        r"\b(?:sk|ghp|gho|ghu|ghs|ghr|glpat|hf|xoxb|xoxp|xoxa|xoxs)[-_][A-Za-z0-9_.-]{8,}\b",
        "***",
        text,
    )
    text = re.sub(r"\bAIza[A-Za-z0-9_-]{20,}\b", "***", text)
    text = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "***", text)
    return re.sub(r"\bya29\.[A-Za-z0-9._-]{10,}\b", "***", text)


def _escape_html(text):
    """HTML 转义。"""
    return html.escape(str(text or ''), quote=True)


def _format_message_markdown(msg):
    """把单条消息格式化为 Markdown。"""
    role = msg.get('role', 'unknown')
    content = msg.get('content', '')

    # 角色显示名
    try:
        from core.persona_settings import get_agent_display_name
        _assistant_label = get_agent_display_name()
    except Exception:
        _assistant_label = "星瑶"
    role_display = {
        'user': '用户',
        'assistant': _assistant_label,
        'system': '系统',
        'tool': '工具结果',
    }.get(role, role)

    # 内容可能是 list（多模态）
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get('type') == 'text':
                    parts.append(item.get('text') or '')
                elif item.get('type') in ('image', 'image_url'):
                    parts.append('[图片]')
                else:
                    parts.append(str(item)[:200])
            else:
                parts.append(str(item))
        content = '\n'.join(parts)
    elif not isinstance(content, str):
        content = str(content)

    # 截断超长内容（避免导出文件过大）
    if len(content) > 5000:
        content = content[:5000] + '\n... (内容已截断，共 ' + str(len(content)) + ' 字符)'

    return f"### {role_display}\n\n{_redact_export_text(content)}\n"


def export_session_markdown(session_name, manager=None, output_path=None):
    """导出会话为 Markdown 文件。

    参数：
    - session_name: 会话名
    - manager: MemoryManager 实例（默认从 main 获取）
    - output_path: 输出路径（默认 ~/.ycode/exports/<session>_<timestamp>.md）

    返回：导出的文件路径，或错误信息
    """
    if manager is None:
        # 未显式传入时回落到运行时上下文（CLI / GUI 启动时都会注册）
        manager = app_context.get_manager()
    if manager is None:
        return "🚨 无法获取会话管理器: 运行时上下文尚未注册 MemoryManager"

    history = manager.get_history(session_name)
    if not history:
        return f"🚨 会话 {session_name!r} 没有历史记录"

    # 生成 Markdown
    lines = [
        f"# 会话导出: {session_name}",
        f"",
        f"导出时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"消息数: {len(history)}",
        f"",
        "---",
        "",
    ]
    for msg in history:
        # 上下文块（system 角色）是运行时脚手架，不进入导出文件
        if msg.get("role") == "system":
            continue
        lines.append(_format_message_markdown(msg))

    markdown_content = '\n'.join(lines)

    # 写文件
    if output_path is None:
        export_dir = Path.home() / '.ycode' / 'exports'
        export_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = ''.join(c if c.isalnum() or c in '-_' else '_' for c in session_name)
        output_path = export_dir / f"{safe_name}_{timestamp}.md"

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown_content, encoding='utf-8')

    return f"✔ 已导出会话 {session_name!r} 为 Markdown: {output_path}"


def export_session_html(session_name, manager=None, output_path=None):
    """导出会话为 HTML 文件（含样式，便于阅读）。"""
    if manager is None:
        # 未显式传入时回落到运行时上下文（CLI / GUI 启动时都会注册）
        manager = app_context.get_manager()
    if manager is None:
        return "🚨 无法获取会话管理器: 运行时上下文尚未注册 MemoryManager"

    history = manager.get_history(session_name)
    if not history:
        return f"🚨 会话 {session_name!r} 没有历史记录"

    # 生成 HTML
    messages_html = []
    for msg in history:
        role = msg.get('role', 'unknown')
        # 上下文块（system 角色）是运行时脚手架，不进入导出文件
        if role == 'system':
            continue
        content = msg.get('content', '')

        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'text':
                    parts.append(item.get('text') or '')
                elif isinstance(item, dict) and item.get('type') in ('image', 'image_url'):
                    parts.append('[图片]')
                else:
                    parts.append(str(item)[:200])
            content = '\n'.join(parts)
        elif not isinstance(content, str):
            content = str(content)

        if len(content) > 5000:
            content = content[:5000] + '\n... (内容已截断)'

        role_class = {
            'user': 'user',
            'assistant': 'assistant',
            'system': 'system',
            'tool': 'tool',
        }.get(role, 'other')

        try:
            from core.persona_settings import get_agent_display_name
            _assistant_label = get_agent_display_name()
        except Exception:
            _assistant_label = "星瑶"
        role_display = {
            'user': '用户',
            'assistant': _assistant_label,
            'system': '系统',
            'tool': '工具结果',
        }.get(role, role)

        content = _redact_export_text(content)
        messages_html.append(f"""
        <div class="message {role_class}">
            <div class="role">{_escape_html(role_display)}</div>
            <div class="content"><pre>{_escape_html(content)}</pre></div>
        </div>""")

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>会话导出: {_escape_html(session_name)}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
        h1 {{ color: #333; border-bottom: 2px solid #00b4d8; padding-bottom: 10px; }}
        .meta {{ color: #666; font-size: 14px; margin-bottom: 20px; }}
        .message {{ margin-bottom: 16px; padding: 16px; border-radius: 8px; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .message .role {{ font-weight: 600; margin-bottom: 8px; font-size: 14px; }}
        .message .content pre {{ white-space: pre-wrap; word-wrap: break-word; margin: 0; font-family: ui-monospace, "Cascadia Code", monospace; font-size: 13px; line-height: 1.5; }}
        .message.user {{ border-left: 4px solid #00b4d8; }}
        .message.user .role {{ color: #00b4d8; }}
        .message.assistant {{ border-left: 4px solid #00a86b; }}
        .message.assistant .role {{ color: #00a86b; }}
        .message.system {{ border-left: 4px solid #888; background: #fafafa; }}
        .message.system .role {{ color: #888; }}
        .message.tool {{ border-left: 4px solid #ffa500; background: #fff8e1; }}
        .message.tool .role {{ color: #ffa500; }}
    </style>
</head>
<body>
    <h1>会话导出: {_escape_html(session_name)}</h1>
    <div class="meta">导出时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · 消息数: {len(history)}</div>
    {''.join(messages_html)}
</body>
</html>"""

    if output_path is None:
        export_dir = Path.home() / '.ycode' / 'exports'
        export_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = ''.join(c if c.isalnum() or c in '-_' else '_' for c in session_name)
        output_path = export_dir / f"{safe_name}_{timestamp}.html"

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_content, encoding='utf-8')

    return f"✔ 已导出会话 {session_name!r} 为 HTML: {output_path}"


def export_session_json(session_name, manager=None, output_path=None):
    """导出可分享 JSON；默认移除推理并脱敏常见凭据。"""
    if manager is None:
        # 未显式传入时回落到运行时上下文（CLI / GUI 启动时都会注册）
        manager = app_context.get_manager()
    if manager is None:
        return "🚨 无法获取会话管理器: 运行时上下文尚未注册 MemoryManager"

    history = manager.get_history(session_name)
    if not history:
        return f"🚨 会话 {session_name!r} 没有历史记录"

    messages = []
    for message in history:
        if message.get('role') == 'system':
            continue
        cleaned = dict(message)
        cleaned.pop('reasoning_content', None)
        messages.append(cleaned)
    json_content = json.dumps({
        'session_name': session_name,
        'exported_at': datetime.datetime.now().isoformat(),
        'message_count': len(history),
        'messages': messages,
    }, ensure_ascii=False, indent=2)
    json_content = _redact_export_text(json_content)

    if output_path is None:
        export_dir = Path.home() / '.ycode' / 'exports'
        export_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = ''.join(c if c.isalnum() or c in '-_' else '_' for c in session_name)
        output_path = export_dir / f"{safe_name}_{timestamp}.json"

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json_content, encoding='utf-8')

    return f"✔ 已导出会话 {session_name!r} 为 JSON: {output_path}"
