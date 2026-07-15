import sqlite3
import re
from litellm import completion
from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import HTML

def compress_session_memory(manager, session_name, api_model, api_key, api_url, force=False):
    """
    智能归纳压缩会话记忆：
    1. 读取全部历史对话记录；
    2. 保留最近的 8 条（force=True时保留 4 条，也就是 2-4 轮对话，user-assistant）作为当前活跃上下文；
    3. 把在此之前的所有更老对话打包，调用 LLM 提炼/合并生成简短中文记忆摘要并写入 sessions。
    """
    try:
        cursor = manager.conn.cursor()
        
        # 1. 查找会话基本信息
        cursor.execute('SELECT id, memory_summary, summarized_until_msg_id FROM sessions WHERE session_name = ?', (session_name,))
        row = cursor.fetchone()
        if not row:
            return False
            
        session_id = row[0]
        old_summary = row[1]
        summarized_until_msg_id = row[2] or 0
        
        # 2. 查询该会话下，目前所有活跃（未被旧总结排除）的消息
        cursor.execute('''
            SELECT id, role, content FROM messages
            WHERE session_id = ? AND id > ?
            ORDER BY id ASC
        ''', (session_id, summarized_until_msg_id))
        
        all_active_messages = []
        import json
        for msg_id, role, raw_content in cursor.fetchall():
            try:
                parsed_content = json.loads(raw_content)
                # 处理可能包含图片等多媒体内容的情况
                if isinstance(parsed_content, list):
                    text_parts = []
                    for item in parsed_content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                    content_str = " ".join(text_parts)
                else:
                    content_str = str(parsed_content)
            except (ValueError, TypeError):
                content_str = str(raw_content)
                
            all_active_messages.append({
                "id": msg_id,
                "role": role,
                "content": content_str
            })
            
        # 3. 统计需要保留的最近活跃消息（保留最后 8 条活跃消息，即 4 轮完整 user-assistant 交互）
        # 确保只有在活跃消息数显著多于保留数时才压缩，否则没意义
        RETAIN_COUNT = 4 if force else 8
        if len(all_active_messages) <= RETAIN_COUNT + 2:
            if force:
                print_formatted_text(HTML("<ansiyellow>ℹ 当前会话活跃对话过少（少于 6 条），无需进行记忆压缩。</ansiyellow>"))
            return False
            
        messages_to_summarize = all_active_messages[:-RETAIN_COUNT]
        # 获取被压缩的最大消息 id，以此作为下次截断的起点
        max_summarized_id = messages_to_summarize[-1]["id"]
        
        # 4. 组装待总结对话文本日志
        chat_log = ""
        for msg in messages_to_summarize:
            role_display = "用户" if msg["role"] == "user" else "星瑶"
            # 过滤内容里的思考标签以缩减 Token 负担
            clean_content = re.sub(r"<(?:think|thinking)>.*?</(?:think|thinking)>", "", msg["content"], flags=re.DOTALL).strip()
            if clean_content:
                chat_log += f"{role_display}: {clean_content}\n"
                
        if not chat_log.strip():
            return False
            
        # 5. 构造总结 / 合并 prompt 提交大模型
        if old_summary:
            prompt = (
                "你是一个高度专业的记忆管理器。请根据【前期历史记忆摘要】和最新的【追加对话记录】，进行梳理和整合，生成一个全新的、合并后的中文背景记忆摘要。\n"
                "新的记忆摘要必须完整包含前期和新近的所有关键事实：\n"
                "1. 曾经讨论的技术决策和确定的技术方案；\n"
                "2. 已经修改或创建的代码文件（包含文件名）；\n"
                "3. 当前遗留、尚在排查的 Bug 或者是未完工的待办开发任务；\n"
                "4. 用户当前正在进行的最终目标。\n"
                "要求：\n"
                "- 保持极度精炼，整体字数严格控制在 350 字以内，千万不要长篇大论。\n"
                "- 直接输出合并后的摘要内容，不要带废话，也不要带任何 Markdown 标题、说明性前言或结尾词。\n\n"
                f"【前期历史记忆摘要】\n{old_summary}\n\n"
                f"【追加对话记录】\n{chat_log}\n\n"
                "全新合并后的记忆摘要："
            )
        else:
            prompt = (
                "你是一个高度专业的记忆管理器。请为以下一段长对话记录生成一个简短、结构清晰的中文背景记忆摘要。\n"
                "你的摘要需要准确概括：\n"
                "1. 关键讨论主题和技术决策；\n"
                "2. 曾经修改或创建的关键代码文件名称；\n"
                "3. 目前未解决的 Bug、排查中的问题或未完工的待办事项；\n"
                "4. 用户当前正在进行的目标。\n"
                "要求：\n"
                "- 保持极其简练，字数严格控制在 250 字以内。\n"
                "- 不要带任何废话、Markdown标题或介绍，直接输出摘要内容。\n\n"
                f"[对话历史记录]\n{chat_log}\n\n"
                "记忆摘要："
            )
            
        print_formatted_text(HTML("\n<ansigray>🧠 正在后台智能压缩并整合长对话记忆...</ansigray>"))
        
        response = completion(
            model=api_model,
            api_key=api_key,
            api_base=api_url,
            messages=[{"role": "user", "content": prompt}],
            timeout=15
        )
        new_summary = response.choices[0].message.content.strip()
        # 清除多余的 markdown 格式或引导词
        new_summary = re.sub(r"^记忆摘要[：:]\s*", "", new_summary)
        new_summary = new_summary.replace("```", "").strip()
        
        if new_summary:
            # 6. 将合并后的记忆摘要和截止 id 存回数据库
            cursor.execute('''
                UPDATE sessions 
                SET memory_summary = ?, summarized_until_msg_id = ?
                WHERE id = ?
            ''', (new_summary, max_summarized_id, session_id))
            manager.conn.commit()
            
            print_formatted_text(HTML(f"<ansigreen>✔ 记忆压缩整合成功！腾出了约 {len(chat_log)//2} 字的历史对话空间。</ansigreen>"))
            return True
            
    except Exception as e:
        safe_e = str(e).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        print_formatted_text(HTML(f"<ansired>⚠️ 后台记忆压缩失败: {safe_e}</ansired>"))
        
    return False
