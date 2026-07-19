import contextlib
import json
import logging
import re
import threading
# litellm 改为懒导入：避免在 import core.memory_compressor 时强制加载 litellm（~1.4s）
# 实际首次压缩时才加载，与 core.llm 的懒加载策略一致
from prompt_toolkit import print_formatted_text  # noqa: F401 保留导入以兼容测试桩
from prompt_toolkit.formatted_text import HTML  # noqa: F401

# 压缩进行中守卫：防止同一会话被并发/重入压缩导致 summarized_until_msg_id 跳过新消息
_compress_guard = threading.Lock()
_compressing_sessions = set()

# 单条消息预压缩阈值：超过此长度的工具结果先截断再交给 LLM 总结
# 工具结果（如 read_file 大文件）往往是 LLM 总结的 token 大户，预截断可显著降低压缩成本
TOOL_RESULT_PREVIEW_LIMIT = 800
TOOL_RESULT_TRUNCATE_THRESHOLD = 2000


def _truncate_long_tool_result(content):
    """对超长工具结果做预截断，保留开头 + 末尾 + 占位符。

    工具结果（特别是 read_file 返回的完整文件内容）在压缩时全部塞进 prompt 让 LLM 总结，
    等于把整个文件又发一遍。预截断把单条工具结果限制在 TOOL_RESULT_PREVIEW_LIMIT 字符内，
    能显著降低压缩调用的 token 成本（典型大文件 read_file 可省 80%+）。

    策略：保留开头（通常是文件头/结构信息）和末尾（通常是关键结论），
    中间用占位符标注截断长度，让 LLM 知道有内容被省略。
    """
    if not isinstance(content, str):
        content = str(content)
    if len(content) <= TOOL_RESULT_TRUNCATE_THRESHOLD:
        return content
    head = content[:TOOL_RESULT_PREVIEW_LIMIT // 2]
    tail = content[-(TOOL_RESULT_PREVIEW_LIMIT // 2):]
    omitted = len(content) - TOOL_RESULT_PREVIEW_LIMIT
    return f"{head}\n... [已省略 {omitted} 字符的工具结果内容] ...\n{tail}"


def compress_session_memory(
    manager,
    session_name,
    api_model,
    api_key,
    api_url,
    force=False,
    output_callback=None,
):
    """
    智能归纳压缩会话记忆：
    1. 读取全部历史对话记录；
    2. 保留最近的 8 条（force=True时保留 4 条，也就是 2-4 轮对话，user-assistant）作为当前活跃上下文；
    3. 把在此之前的所有更老对话打包，调用 LLM 提炼/合并生成简短中文记忆摘要并写入 sessions。
    """
    def emit(message):
        # 强制要求 output_callback：后台线程绝不允许直接 print_formatted_text 触碰终端 UI。
        # 无 callback 时静默降级为诊断日志。
        if output_callback is not None:
            output_callback(message)
        else:
            logging.getLogger("py.warnings").warning(
                "memory_compressor output dropped (no callback): %s",
                re.sub(r"<[^>]+>", "", message),
            )

    # 防重入：同一会话已有压缩在进行时直接放弃
    guard_key = (id(manager), session_name)
    with _compress_guard:
        if guard_key in _compressing_sessions:
            emit("<ansiyellow>ℹ 记忆压缩已在进行中，本次请求已跳过。</ansiyellow>")
            return False
        _compressing_sessions.add(guard_key)

    try:
        return _do_compress(
            manager, session_name, api_model, api_key, api_url,
            force=force, emit=emit,
        )
    finally:
        with _compress_guard:
            _compressing_sessions.discard(guard_key)


def _do_compress(manager, session_name, api_model, api_key, api_url, force, emit):
    try:
        # 多步读写期间持有 manager 的可重入锁，与 add_messages 等写路径互斥
        lock = manager.acquire_lock() if hasattr(manager, "acquire_lock") else contextlib.nullcontext()
        with lock:
            cursor = manager.conn.cursor()

            # 1. 查找会话基本信息
            cursor.execute('SELECT id, memory_summary, summarized_until_msg_id FROM sessions WHERE session_name = ?', (session_name,))
            row = cursor.fetchone()
            if not row:
                return False

            session_id = row[0]
            old_summary = row[1]
            old_boundary_raw = row[2]  # 原始值（可能为 NULL），用于乐观校验
            summarized_until_msg_id = row[2] or 0

            # 2. 查询该会话下，目前所有活跃（未被旧总结排除）的消息
            cursor.execute('''
                SELECT id, role, content FROM messages
                WHERE session_id = ? AND id > ?
                ORDER BY id ASC
            ''', (session_id, summarized_until_msg_id))

            all_active_messages = []
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
                    # 阈值随 RETAIN_COUNT 动态生成（force=True 时 RETAIN_COUNT=4）
                    emit(f"<ansiyellow>ℹ 当前会话活跃对话过少（少于 {RETAIN_COUNT + 2} 条），无需进行记忆压缩。</ansiyellow>")
                return False

            messages_to_summarize = all_active_messages[:-RETAIN_COUNT]
            # 获取被压缩的最大消息 id，以此作为下次截断的起点
            max_summarized_id = messages_to_summarize[-1]["id"]

            # 4. 组装待总结对话文本日志
            # 对超长工具结果做预截断：read_file 等工具返回的大文件原文在压缩时
            # 全部塞给 LLM 等于又发一遍，预截断保留头尾即可让 LLM 理解上下文，
            # 同时显著降低压缩调用的 token 成本。
            chat_log = ""
            for msg in messages_to_summarize:
                # 角色显示：user→用户，assistant→星瑶，tool→工具结果（避免误标为助手发言）
                role = msg["role"]
                if role == "user":
                    role_display = "用户"
                elif role == "tool":
                    role_display = "工具结果"
                else:
                    role_display = "星瑶"
                # 过滤内容里的思考标签以缩减 Token 负担
                clean_content = re.sub(r"<(?:think|thinking)>.*?</(?:think|thinking)>", "", msg["content"], flags=re.DOTALL).strip()
                # 工具结果（role=tool）超长时预截断
                if role == "tool" and clean_content:
                    clean_content = _truncate_long_tool_result(clean_content)
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

        # LLM 调用耗时较长，在锁外执行，避免阻塞其它线程的正常读写
        emit("\n<ansigray>🧠 正在后台智能压缩并整合长对话记忆...</ansigray>")

        # 懒加载 litellm.completion（首次调用触发 import，约 1.4s）
        from core.llm import _litellm_completion
        response = _litellm_completion(
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
            # 6. 将合并后的记忆摘要和截止 id 存回数据库。
            # 乐观校验：仅当 summarized_until_msg_id 仍为读取时的旧值才更新，
            # 防止压缩期间并发写入/压缩导致边界错乱、新消息被静默跳过。
            with manager.acquire_lock() if hasattr(manager, "acquire_lock") else contextlib.nullcontext():
                cursor = manager.conn.cursor()
                cursor.execute('''
                    UPDATE sessions
                    SET memory_summary = ?, summarized_until_msg_id = ?
                    WHERE id = ? AND summarized_until_msg_id IS ?
                ''', (new_summary, max_summarized_id, session_id, old_boundary_raw))
                if cursor.rowcount == 0:
                    manager.conn.rollback()
                    emit("<ansiyellow>ℹ 记忆摘要已过期（期间有新压缩写入），本次结果已丢弃。</ansiyellow>")
                    return False
                manager.conn.commit()

            emit(f"<ansigreen>✔ 记忆压缩整合成功！腾出了约 {len(chat_log)//2} 字的历史对话空间。</ansigreen>")
            return True

    except Exception as e:
        safe_e = str(e).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        emit(f"<ansired>⚠️ 后台记忆压缩失败: {safe_e}</ansired>")

    return False
