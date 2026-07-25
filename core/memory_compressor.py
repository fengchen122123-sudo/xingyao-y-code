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

# 默认配置：在压缩收益与额度消耗之间取平衡
DEFAULT_RETAIN_COUNT = 8
DEFAULT_RETAIN_COUNT_FORCE = 4
DEFAULT_TOOL_RESULT_TRUNCATE_THRESHOLD_CHARS = 2000
DEFAULT_TOOL_RESULT_PREVIEW_LIMIT_CHARS = 800
DEFAULT_TOOL_RESULT_TOKEN_LIMIT = 600      # 近似 token 上限（优先于字符阈值）
DEFAULT_SUMMARY_LENGTH_FIRST = 250
DEFAULT_SUMMARY_LENGTH_MERGE = 350


def _load_compression_config():
    """读取 config.json 中的记忆压缩配置，失败时返回默认值。"""
    try:
        from core.model_manager import load_config
        cfg = load_config()
        comp_cfg = cfg.get("memory_compression", {})
        return {
            "retain_count": int(comp_cfg.get("retain_count", DEFAULT_RETAIN_COUNT)),
            "retain_count_force": int(comp_cfg.get("retain_count_force", DEFAULT_RETAIN_COUNT_FORCE)),
            "tool_result_truncate_threshold_chars": int(
                comp_cfg.get("tool_result_truncate_threshold_chars", DEFAULT_TOOL_RESULT_TRUNCATE_THRESHOLD_CHARS)
            ),
            "tool_result_preview_limit_chars": int(
                comp_cfg.get("tool_result_preview_limit_chars", DEFAULT_TOOL_RESULT_PREVIEW_LIMIT_CHARS)
            ),
            "tool_result_token_limit": int(
                comp_cfg.get("tool_result_token_limit", DEFAULT_TOOL_RESULT_TOKEN_LIMIT)
            ),
            "summary_length_first": int(comp_cfg.get("summary_length_first", DEFAULT_SUMMARY_LENGTH_FIRST)),
            "summary_length_merge": int(comp_cfg.get("summary_length_merge", DEFAULT_SUMMARY_LENGTH_MERGE)),
        }
    except Exception:
        return {
            "retain_count": DEFAULT_RETAIN_COUNT,
            "retain_count_force": DEFAULT_RETAIN_COUNT_FORCE,
            "tool_result_truncate_threshold_chars": DEFAULT_TOOL_RESULT_TRUNCATE_THRESHOLD_CHARS,
            "tool_result_preview_limit_chars": DEFAULT_TOOL_RESULT_PREVIEW_LIMIT_CHARS,
            "tool_result_token_limit": DEFAULT_TOOL_RESULT_TOKEN_LIMIT,
            "summary_length_first": DEFAULT_SUMMARY_LENGTH_FIRST,
            "summary_length_merge": DEFAULT_SUMMARY_LENGTH_MERGE,
        }


def _estimate_tokens(text):
    """估算文本 token 数。优先用 tiktoken，失败时按字符数近似（中文/英文混合约 1 token / 2 字符）。"""
    if not isinstance(text, str):
        text = str(text)
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        # 保守近似：英文 1 token / 4 字符，中文 1 token / 1 字符；混合取 1 token / 2 字符
        return max(1, len(text) // 2)


def _truncate_long_tool_result(content, config=None):
    """对超长工具结果做预截断，保留开头 + 末尾 + 占位符。

    工具结果（特别是 read_file 返回的完整文件内容）在压缩时全部塞进 prompt 让 LLM 总结，
    等于把整个文件又发一遍。预截断把单条工具结果限制在可控长度内，
    能显著降低压缩调用的 token 成本（典型大文件 read_file 可省 80%+）。

    策略：
    - 字符长度超过阈值 或 token 数超过阈值时触发截断；
    - 保留开头（通常是文件头/结构信息）和末尾（通常是关键结论），
      中间用占位符标注截断长度，让 LLM 知道有内容被省略。
    """
    if not isinstance(content, str):
        content = str(content)
    if config is None:
        config = _load_compression_config()

    threshold_chars = config["tool_result_truncate_threshold_chars"]
    preview_limit = config["tool_result_preview_limit_chars"]
    token_limit = config["tool_result_token_limit"]

    if len(content) <= threshold_chars and _estimate_tokens(content) <= token_limit:
        return content

    head_len = preview_limit // 2
    tail_len = preview_limit // 2
    head = content[:head_len]
    tail = content[-tail_len:] if tail_len else ""
    omitted = len(content) - preview_limit
    omitted_tokens = _estimate_tokens(content) - _estimate_tokens(head + tail)
    return (
        f"{head}\n"
        f"... [已省略 {omitted} 字符 / 约 {omitted_tokens} token 的工具结果内容] ...\n"
        f"{tail}"
    )


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
    2. 保留最近的 N 条（force=True 时保留更少条）作为当前活跃上下文；
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
    config = _load_compression_config()
    try:
        # 多步读写期间持有 manager 的可重入锁，与 add_messages 等写路径互斥
        lock = manager.acquire_lock() if hasattr(manager, 'acquire_lock') else contextlib.nullcontext()
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

            # 3. 统计需要保留的最近活跃消息
            retain_count = config["retain_count_force"] if force else config["retain_count"]
            if len(all_active_messages) <= retain_count + 2:
                if force:
                    emit(f"<ansiyellow>ℹ 当前会话活跃对话过少（少于 {retain_count + 2} 条），无需进行记忆压缩。</ansiyellow>")
                return False

            messages_to_summarize = all_active_messages[:-retain_count]
            # 获取被压缩的最大消息 id，以此作为下次截断的起点
            max_summarized_id = messages_to_summarize[-1]["id"]

            # 4. 组装待总结对话文本日志
            chat_log = ""
            for msg in messages_to_summarize:
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
                    clean_content = _truncate_long_tool_result(clean_content, config)
                if clean_content:
                    chat_log += f"{role_display}: {clean_content}\n"

            if not chat_log.strip():
                return False

            summary_limit = config["summary_length_merge"] if old_summary else config["summary_length_first"]

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
                    f"- 保持极度精炼，整体字数严格控制在 {summary_limit} 字以内，千万不要长篇大论。\n"
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
                    f"- 保持极其简练，字数严格控制在 {summary_limit} 字以内。\n"
                    "- 不要带任何废话、Markdown标题或介绍，直接输出摘要内容。\n\n"
                    f"[对话历史记录]\n{chat_log}\n\n"
                    "记忆摘要："
                )

        # LLM 调用耗时较长，在锁外执行，避免阻塞其它线程的正常读写
        emit("\n<ansigray>🧠 正在后台智能压缩并整合长对话记忆...</ansigray>")

        # 优先使用 secondary（便宜）模型做摘要，降低后台记忆压缩的额度消耗
        compress_model, compress_key, compress_url = api_model, api_key, api_url
        try:
            from core.model_manager import get_secondary_model, get_secondary_roles
            if get_secondary_roles().get("compression"):
                secondary = get_secondary_model()
                if secondary:
                    compress_model = secondary["litellm_model"]
                    compress_key = secondary["api_key"]
                    compress_url = secondary["base_url"]
        except Exception:
            pass

        # 懒加载 litellm.completion（首次调用触发 import，约 1.4s）
        from core.llm import _litellm_completion
        response = _litellm_completion(
            model=compress_model,
            api_key=compress_key,
            api_base=compress_url,
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
            with manager.acquire_lock() if hasattr(manager, 'acquire_lock') else contextlib.nullcontext():
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
        # 异常路径必须回滚，否则未提交的事务会使 SQLite 连接进入锁定状态，
        # 导致后续所有写操作报 "database is locked"。
        try:
            if hasattr(manager, 'conn') and manager.conn:
                manager.conn.rollback()
        except Exception:
            pass
        safe_e = str(e).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        emit(f"<ansired>⚠️ 后台记忆压缩失败: {safe_e}</ansired>")

    return False
