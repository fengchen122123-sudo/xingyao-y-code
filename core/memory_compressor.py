import contextlib
import logging
import re
import threading

from core.memory import message_content_to_text
# 压缩模型请求入口为懒导入：实际首次压缩时才加载 core.llm，
# 避免 import core.memory_compressor 就拉起 LLM 引擎全家桶
from prompt_toolkit import print_formatted_text  # noqa: F401 保留导入以兼容测试桩
from prompt_toolkit.formatted_text import HTML  # noqa: F401

# 压缩进行中守卫：防止同一会话被并发/重入压缩导致 summarized_until_msg_id 跳过新消息
_compress_guard = threading.Lock()
_compressing_sessions = set()

# tiktoken 编码器惰性加载状态：idle → loading → ready / failed
# ready 前（含等待超时）所有 token 估算走 CJK 感知的字符兜底
_encoding_state = {"status": "idle", "enc": None, "failed_at": None}
_encoding_lock = threading.Lock()
ENCODING_RETRY_COOLDOWN_SECONDS = 300.0

TRUNCATION_SUFFIX = "\n...[该条消息过长，中段已截断]..."

# 默认配置：在压缩收益与额度消耗之间取平衡
DEFAULT_RETAIN_COUNT = 8
DEFAULT_RETAIN_COUNT_FORCE = 4
DEFAULT_TOOL_RESULT_TRUNCATE_THRESHOLD_CHARS = 2000
DEFAULT_TOOL_RESULT_PREVIEW_LIMIT_CHARS = 800
DEFAULT_TOOL_RESULT_TOKEN_LIMIT = 600      # 近似 token 上限（优先于字符阈值）
DEFAULT_SUMMARY_LENGTH_FIRST = 250
DEFAULT_SUMMARY_LENGTH_MERGE = 350
COMPRESSION_CHUNK_WINDOW_RATIO = 0.25
COMPRESSION_TIMEOUT_TOKENS_PER_SECOND = 1500

# 单段 Chunk 预算上限：即使模型窗口高达 256k/1M，单段载荷也不超过 24000 token。
# 避免过大 Prompt 一次性打满服务商的 TPM (Tokens Per Minute) 突发限制触发 429，
# 同时中小载荷能显著提升模型对事实细节的提取质量。
DEFAULT_MAX_CHUNK_TOKEN_BUDGET = 24000

# 多段压缩时的段间平滑延时（秒）。默认 0：429 限流本就有独立的指数退避
# 重试兜底，无条件延时只白白拉长压缩耗时。段间延时改为自适应——仅当本次
# 压缩已经触发过 429 时，才在后续段间插入该延时给令牌桶恢复时间；
# 用户在 config.json 显式配置了非 0 值时仍按配置无条件生效。
COMPRESSION_INTER_CHUNK_DELAY_SECONDS = 0.0

# 429 触发后的段间自适应冷却延时（秒）：仅在本次压缩已发生限流后生效
COMPRESSION_ADAPTIVE_INTER_CHUNK_DELAY_SECONDS = 2.0

# 预压缩水位：占用达到窗口该比例时在后台提前触发记忆压缩（0 关闭），
# 使 0.95 关键水位到来时摘要通常已就绪，回合不再被压缩阻塞。
# 必须显著低于 SUMMARIZE_RATIO 且与 keep/活跃区保持足量间距，
# 避免压缩完成后立即再次触发（对应 OpenCode 的防抖教训）。
DEFAULT_PRECOMPRESS_RATIO = 0.85

# 单次压缩的总预算秒数上限（可经 config.json memory_compression.max_total_seconds 调优）
DEFAULT_MAX_TOTAL_SECONDS = 180.0

# 闲时自动维护触发延时（秒，0 关闭）：用户空闲该时长后跑水位线维护与压缩，
# 让维护发生在缓存还热着的窗口内并顺带预热新前缀（core/idle_maintenance.py）
DEFAULT_IDLE_MAINTENANCE_SECONDS = 240

# 压缩请求触发 429 RateLimitError 时的基础退避秒数与最大重试次数
COMPRESSION_RATE_LIMIT_BASE_DELAY_SECONDS = 5.0
COMPRESSION_MAX_RETRIES = 3


class CompressionOutcome:
    """记忆压缩执行结果三态封装（compressed / skipped / failed）。"""
    def __init__(self, status: str, reason: str = ""):
        self.status = status  # "compressed", "skipped", "failed"
        self.reason = reason

    def __bool__(self):
        return self.status == "compressed"

    def __repr__(self):
        return f"CompressionOutcome(status={self.status!r}, reason={self.reason!r})"


def is_session_compressing(manager, session_name: str) -> bool:
    """检查指定会话当前是否已有记忆压缩任务在后台执行。"""
    guard_key = (id(manager), session_name)
    with _compress_guard:
        return guard_key in _compressing_sessions


def _positive_int_or_none(value):
    """正整数校验：仅 int 且 >0 有效（bool 不算数），非法/缺失返回 None。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _normalize_model_watermark_bases(raw):
    """归一化「按模型的水位线计算基准」配置：仅 dict[str, 正整数] 有效。

    键统一 str 化 + strip + 小写（与模型名叶子名的匹配口径一致），
    值为合法正整数才保留；非法条目静默过滤，整体非 dict 时返回 {}。
    用户写错的新配置绝不能弄崩整个配置读取。
    """
    if not isinstance(raw, dict):
        return {}
    normalized = {}
    for key, value in raw.items():
        name = str(key).strip().lower()
        if not name:
            continue
        positive = _positive_int_or_none(value)
        if positive is None:
            continue
        normalized[name] = positive
    return normalized


def _load_compression_config():
    """读取 config.json 中的记忆压缩配置，失败时返回默认值。"""
    try:
        from core.model_manager import load_config
        cfg = load_config()
        comp_cfg = cfg.get("memory_compression", {})
        return {
            # 保留条数至少为 1：配置成 0/负数会让截断切片为空并抛 IndexError 被静默吞掉
            "retain_count": max(1, int(comp_cfg.get("retain_count", DEFAULT_RETAIN_COUNT))),
            "retain_count_force": max(1, int(comp_cfg.get("retain_count_force", DEFAULT_RETAIN_COUNT_FORCE))),
            "tool_result_truncate_threshold_chars": int(
                comp_cfg.get("tool_result_truncate_threshold_chars", DEFAULT_TOOL_RESULT_TRUNCATE_THRESHOLD_CHARS)
            ),
            "tool_result_preview_limit_chars": int(
                comp_cfg.get("tool_result_preview_limit_chars", DEFAULT_TOOL_RESULT_PREVIEW_LIMIT_CHARS)
            ),
            "tool_result_token_limit": int(comp_cfg.get("tool_result_token_limit", DEFAULT_TOOL_RESULT_TOKEN_LIMIT)),
            "summary_length_first": int(comp_cfg.get("summary_length_first", DEFAULT_SUMMARY_LENGTH_FIRST)),
            "summary_length_merge": int(comp_cfg.get("summary_length_merge", DEFAULT_SUMMARY_LENGTH_MERGE)),
            "max_chunk_token_budget": int(
                comp_cfg.get("max_chunk_token_budget", DEFAULT_MAX_CHUNK_TOKEN_BUDGET)
            ),
            "inter_chunk_delay_seconds": float(
                comp_cfg.get("inter_chunk_delay_seconds", COMPRESSION_INTER_CHUNK_DELAY_SECONDS)
            ),
            "max_total_seconds": float(
                comp_cfg.get("max_total_seconds", DEFAULT_MAX_TOTAL_SECONDS)
            ),
            # 闲时自动维护延时（秒，0 关闭），见 core/idle_maintenance.py
            "idle_maintenance_seconds": max(0, int(
                comp_cfg.get("idle_maintenance_seconds", DEFAULT_IDLE_MAINTENANCE_SECONDS)
            )),
            "precompress_ratio": float(
                comp_cfg.get("precompress_ratio", DEFAULT_PRECOMPRESS_RATIO)
            ),
            # 水位线计算基准上限（tokens）：仅正整数有效，非法/缺失 → None
            # （None = 不启用，保持纯窗口比例现行为）
            "watermark_base_tokens": _positive_int_or_none(
                comp_cfg.get("watermark_base_tokens")
            ),
            # 按模型覆盖的基准上限：非 dict 视为空表，非法条目静默过滤
            "model_watermark_bases": _normalize_model_watermark_bases(
                comp_cfg.get("model_watermark_bases")
            ),
            # 插入式压缩（Insert-then-Compress）开关：True 时优先在最后一次主
            # 请求字节尾部追加压缩指令以命中热前缀缓存；快照缺失/预算不足/
            # 请求失败一律回退分段路径
            "insert_mode": bool(comp_cfg.get("insert_mode", True)),
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
            "max_chunk_token_budget": DEFAULT_MAX_CHUNK_TOKEN_BUDGET,
            "inter_chunk_delay_seconds": COMPRESSION_INTER_CHUNK_DELAY_SECONDS,
            "max_total_seconds": DEFAULT_MAX_TOTAL_SECONDS,
            "idle_maintenance_seconds": DEFAULT_IDLE_MAINTENANCE_SECONDS,
            "precompress_ratio": DEFAULT_PRECOMPRESS_RATIO,
            "watermark_base_tokens": None,
            "model_watermark_bases": {},
            "insert_mode": True,
        }


def get_precompress_ratio() -> float:
    """预压缩水位（0~1，0 关闭）。读 config 失败时回落默认值。"""
    try:
        return float(_load_compression_config()["precompress_ratio"])
    except Exception:
        return DEFAULT_PRECOMPRESS_RATIO


def get_watermark_base_tokens(model_name):
    """该模型适用的「水位线计算基准」上限（tokens）；未配置/非法时返回 None。

    None 表示不启用（保持纯窗口比例，现行为）。匹配顺序：
    model_watermark_bases 精确命中（模型名取最后一个 "/" 之后的叶子名、小写）
    → 全局 watermark_base_tokens → None。任何异常都返回 None，绝不抛出。
    """
    try:
        config = _load_compression_config()
        leaf = str(model_name or "").rsplit("/", 1)[-1].strip().lower()
        per_model = config.get("model_watermark_bases")
        if leaf and isinstance(per_model, dict):
            hit = _positive_int_or_none(per_model.get(leaf))
            if hit is not None:
                return hit
        return _positive_int_or_none(config.get("watermark_base_tokens"))
    except Exception:
        return None


def persisted_injection_sources() -> frozenset:
    """PERSISTED 类注入的 source 清单（规格 02 §2.6：压缩的"必须保留"依据）。

    为什么压缩只需要这份清单而不需要 source 级白名单：星瑶的 PERSISTED 注入
    （运行上下文块 / 电脑操控说明 / 按回合撤销纠正）都由 build_dynamic_prompt
    合并进**同一条上下文块消息**，所以保留粒度就是"承载动态块的那条消息"
    ——比 ZCode 的消息级/source 级双层机制更简单。

    注册表不可用时返回空集：压缩主流程绝不因为"保留依据查不到"而失败或改变
    行为（保留判定退化为改造前口径）。
    """
    try:
        from core.context_injection import build_default_injection_registry

        return build_default_injection_registry().persisted_sources()
    except Exception:
        logging.getLogger("py.warnings").debug(
            "persisted_sources 不可用，压缩保留判定退化为默认口径", exc_info=True
        )
        return frozenset()


def _persisted_context_anchor_index(messages: list) -> int | None:
    """返回承载 PERSISTED 注入的最新上下文块消息下标（没有则 None）。

    动态块由 agent_service 内联到 user 消息末尾，标记为 DYNAMIC_CONTEXT_PREFIX；
    只有 PERSISTED 通道真的包含"运行上下文块"时才认为该标记代表需要保留的注入。
    任何导入异常都退回 None（= 不做锚定），保证压缩行为与改造前一致。
    """
    try:
        from core.context_builder import DYNAMIC_CONTEXT_PREFIX
        from core.context_injection import SOURCE_RUNTIME_CONTEXT_BLOCK

        if SOURCE_RUNTIME_CONTEXT_BLOCK not in persisted_injection_sources():
            return None
        if not DYNAMIC_CONTEXT_PREFIX:
            return None
    except Exception:
        return None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user":
            continue
        content = message.get("content") or ""
        if isinstance(content, str) and DYNAMIC_CONTEXT_PREFIX in content:
            return index
    return None


def _get_encoding(timeout=10.0):
    """惰性加载 tiktoken 编码器；网络不可达时绝不无限阻塞。

    tiktoken 首次加载需联网下载 cl100k_base 编码文件（缓存在 %TEMP%），
    网络受限时底层请求无超时、会永久挂起——此前压缩线程会被这一步卡死。
    这里改为后台守护线程加载，主线程最多等 timeout 秒；未就绪则返回 None，
    调用方退回字符估算。后台线程加载成功后后续调用直接复用。
    """
    import time as _time

    state = _encoding_state
    now = _time.monotonic()
    with _encoding_lock:
        status = state["status"]
        if status == "ready":
            return state["enc"]
        if status == "loading":
            # 已有唯一加载线程在跑；并发调用不重复等待或再次下载。
            return None
        if status == "failed":
            failed_at = state.get("failed_at")
            if (
                failed_at is not None
                and now - float(failed_at) < ENCODING_RETRY_COOLDOWN_SECONDS
            ):
                return None
        state["status"] = "loading"
        state["enc"] = None
        state["failed_at"] = None

    def _load():
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            with _encoding_lock:
                state["enc"] = enc
                state["status"] = "ready"
                state["failed_at"] = None
        except Exception:
            with _encoding_lock:
                state["enc"] = None
                state["status"] = "failed"
                state["failed_at"] = _time.monotonic()

    threading.Thread(target=_load, daemon=True, name="ycode-tiktoken-load").start()
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline and state["status"] == "loading":
        _time.sleep(0.05)
    return state["enc"] if state["status"] == "ready" else None


def _compression_chunk_budget(context_window, max_budget=None):
    """压缩正文预算：窗口的 25%，极小窗口至少保留 512 token，同时设置绝对上限。

    max_budget 缺省回落 DEFAULT_MAX_CHUNK_TOKEN_BUDGET；调用方可传入配置值，
    避免把默认上限写死成第二处真相。
    """
    if max_budget is None:
        max_budget = DEFAULT_MAX_CHUNK_TOKEN_BUDGET
    raw_budget = max(512, int(int(context_window) * COMPRESSION_CHUNK_WINDOW_RATIO))
    return min(max_budget, raw_budget)


def _compression_request_timeout(prompt_tokens):
    """按本段实际 prompt token 放大超时，并与主请求共用上下限。"""
    from core.llm import API_REQUEST_MAX_TIMEOUT_SECONDS, API_REQUEST_TIMEOUT_SECONDS

    estimated_tokens = max(0, int(prompt_tokens or 0))
    return min(
        API_REQUEST_MAX_TIMEOUT_SECONDS,
        API_REQUEST_TIMEOUT_SECONDS
        + estimated_tokens // COMPRESSION_TIMEOUT_TOKENS_PER_SECOND,
    )


def _truncate_to_token_budget(content, token_budget):
    """截断单条超长正文，并保证截断标记也计入 token 预算。"""
    token_count = _estimate_tokens(content)
    if token_count <= token_budget:
        return content, token_count
    suffix_tokens = _estimate_tokens(TRUNCATION_SUFFIX)
    prefix_budget = int(token_budget) - suffix_tokens
    if prefix_budget < 0:
        raise ValueError("token budget is smaller than the truncation suffix")
    lo, hi = 0, len(content)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _estimate_tokens(content[:mid]) <= prefix_budget:
            lo = mid
        else:
            hi = mid - 1
    truncated = content[:lo] + TRUNCATION_SUFFIX
    return truncated, _estimate_tokens(truncated)


def _estimate_tokens(text):
    """估算文本 token 数。优先用 tiktoken；不可用时退回 CJK 感知的字符估算。

    兜底规则：CJK 字符按 1 token/字（中文实际 1~1.5 token/字，取下界），
    其余按 1 token/4 字符（英文典型值）。相比一刀切的 len//2，
    中文为主的文本不再被低估一半，分段预算更不容易把请求顶出窗口。
    """
    if not isinstance(text, str):
        text = str(text)
    enc = _get_encoding()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    return cjk + (len(text) - cjk) // 4


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


def _build_summary_prompt(old_summary, chat_log, summary_limit):
    """构造总结 / 合并 prompt（分段压缩时逐段复用，文案与历史行为一致）。"""
    if old_summary:
        return (
            "你是一个高度专业的记忆管理器。请根据【前期历史记忆摘要】和最新的【追加对话记录】，进行梳理和整合，生成一个全新的、合并后的中文背景记忆摘要。\n"
            "新的记忆摘要必须完整包含前期和新近的所有关键事实：\n"
            "1. 曾经讨论的技术决策和确定的技术方案；\n"
            "2. 已经修改或创建的代码文件（包含文件名）；\n"
            "3. 当前遗留、尚在排查的 Bug 或者是未完工的待办开发任务；\n"
            "4. 用户当前正在进行的最终目标。\n"
            "要求：\n"
            "- 第一行输出 <topics>3~6 个主题词，逗号分隔</topics>，随后输出摘要正文；\n"
            f"- 保持极度精炼，整体字数严格控制在 {summary_limit} 字以内，千万不要长篇大论。\n"
            "- 摘要正文不要带任何 Markdown 标题、说明性前言或结尾词。\n\n"
            f"【前期历史记忆摘要】\n{old_summary}\n\n"
            f"【追加对话记录】\n{chat_log}\n\n"
            "全新合并后的记忆摘要："
        )
    return (
        "你是一个高度专业的记忆管理器。请为以下一段长对话记录生成一个简短、结构清晰的中文背景记忆摘要。\n"
        "你的摘要需要准确概括：\n"
        "1. 关键讨论主题和技术决策；\n"
        "2. 曾经修改或创建的关键代码文件名称；\n"
        "3. 目前未解决的 Bug、排查中的问题或未完工的待办事项；\n"
        "4. 用户当前正在进行的目标。\n"
            "要求：\n"
            "- 第一行输出 <topics>3~6 个主题词，逗号分隔</topics>，随后输出摘要正文；\n"
            f"- 保持极其简练，字数严格控制在 {summary_limit} 字以内。\n"
            "- 摘要正文不要带任何废话、Markdown标题或介绍。\n\n"
        f"[对话历史记录]\n{chat_log}\n\n"
        "记忆摘要："
    )


# ── 插入式压缩（Insert-then-Compress）─────────────────────────────────────

# 指令自身的 token 预算（参与窗口余量估算）与摘要输出 max_tokens 上限。
INSERT_PROMPT_TOKEN_BUDGET = 1200
INSERT_SUMMARY_MAX_TOKENS = 8192

_TOPICS_RE = re.compile(r"<topics>(.*?)</topics>\s*", re.DOTALL | re.IGNORECASE)


def _split_topics_from_summary(text):
    """剥出摘要首部的 <topics>…</topics> 行，返回 (topics|None, 纯摘要文本)。

    topics 是给回溯索引用的元数据，不能留在摘要正文里污染下一轮上下文。
    """
    raw = (text or "").strip()
    if not raw:
        return None, ""
    m = _TOPICS_RE.search(raw)
    if not m:
        return None, raw
    topics = m.group(1).strip()
    clean = (raw[: m.start()] + raw[m.end():]).strip()
    return (topics or None), clean


def _build_insert_compression_prompt(summary_limit):
    """插入式压缩指令：追加在最后一次主请求字节尾部，作为下一条 user 消息。

    模型在请求前缀里本就能看到 history[0] 的旧摘要块与全部活跃历史，因此
    指令只负责任务切换与输出格式，不重复任何对话内容。
    """
    return (
        "═══════════════════════════════════════════════════════════════\n"
        "重要：任务切换——记忆压缩模式\n"
        "═══════════════════════════════════════════════════════════════\n"
        "上面的对话到此结束。你现在进入记忆压缩模式，这是一个全新的独立任务：\n"
        "1. 不要回应上文对话中的任何请求；\n"
        "2. 不要调用任何工具；\n"
        "3. 只输出纯文本。\n\n"
        "你的唯一任务：把上文完整对话（包括首部的【历史对话记忆摘要】块，如果有）"
        "整合为一份全新的、合并后的中文背景记忆摘要。\n"
        "摘要必须完整覆盖：\n"
        "1. 关键讨论主题与技术决策；\n"
        "2. 已经修改或创建的代码文件（含文件名）；\n"
        "3. 尚未解决的 Bug、排查中的问题或未完工的待办事项；\n"
        "4. 用户当前正在进行的最终目标。\n"
        "输出要求：\n"
        "- 第一行输出 <topics>3~6 个主题词，逗号分隔</topics>；\n"
        f"- 正文保持极度精炼，字数严格控制在 {summary_limit} 字以内；\n"
        "- 不要 Markdown 标题、前言或结尾词，直接输出。"
    )


def _get_keepalive_snapshot(session_name):
    """只读获取会话最后一次主请求快照；任何异常一律返回 None（回退分段路径）。"""
    try:
        from core.cache_keepalive import cache_keepalive_service
        return cache_keepalive_service.get_session_snapshot(session_name)
    except Exception:
        return None


def _try_insert_compression(manager, session_name, session_id, api_model, api_key,
                            api_url, old_boundary_raw, boundary_msg_id,
                            config, emit, cancel_event=None):
    """插入式压缩：在最后一次主请求字节尾部追加压缩指令，单请求完成摘要。

    前缀与上一次真实请求字节完全一致 → 命中热前缀缓存，只付指令与摘要输出
    的增量 token；且模型看到的是原生结构化历史（含旧摘要块），合并质量优于
    拍平文本。返回 CompressionOutcome（已终结）或 None（条件不满足/失败，
    调用方回退分段路径）。本函数绝不抛出——一切异常吞掉转为 None。
    """
    from copy import deepcopy

    try:
        snapshot = _get_keepalive_snapshot(session_name)
        if not isinstance(snapshot, dict):
            # 保活关闭 / 路由不支持 / 主请求在飞或上次失败时快照不存在，
            # insert_mode 恒走分段路径——留诊断线索，避免"开了热路径却从未命中"
            logging.getLogger("py.warnings").debug(
                "插入式压缩跳过：会话 %s 无保活快照，回退分段路径", session_name
            )
            return None
        snapshot_messages = snapshot.get("messages")
        if not isinstance(snapshot_messages, list) or not snapshot_messages:
            return None
        # 快照必须属于当前主请求路线：模型/密钥/路由任一不符说明快照已过期
        # （如切模型后尚未产生新请求），此时热前缀不成立，回退分段路径
        if (snapshot.get("model") != api_model
                or snapshot.get("api_key") != api_key
                or snapshot.get("api_base") != api_url):
            return None

        # 窗口预算：快照 + 指令 + 摘要输出必须留出余量（0.9），否则压缩请求
        # 本身超窗直接失败，不如回退分段路径
        window = 0
        try:
            from core.model_manager import get_active_model
            active = get_active_model(session_name)
            if active and active.get("litellm_model") == api_model:
                window = int(active.get("context_window") or 0)
        except Exception:
            window = 0
        if window <= 0:
            window = 128000
        prompt = _build_insert_compression_prompt(config["summary_length_first"])
        blob = "".join(str(m.get("content") or "") for m in snapshot_messages)
        prompt_tokens = (_estimate_tokens(blob) + INSERT_PROMPT_TOKEN_BUDGET
                         + INSERT_SUMMARY_MAX_TOKENS)
        if prompt_tokens > int(window * 0.9):
            return None

        if cancel_event is not None and cancel_event.is_set():
            return None

        # 快照字节原样继承（含 tools 顺序与亲和参数）；只改消息尾部、输出上限
        # 与流式开关。快照可能包含边界之后的保留区消息：摘要覆盖面略微超前于
        # 提交边界，属保守方向的无害超覆盖，下次压缩自然合并。
        request = deepcopy(snapshot)
        request["messages"] = list(snapshot_messages) + [
            {"role": "user", "content": prompt}
        ]
        request["max_tokens"] = INSERT_SUMMARY_MAX_TOKENS
        request["stream"] = False
        request.pop("stream_options", None)
        request["timeout"] = _compression_request_timeout(prompt_tokens)

        from core.llm import _dispatch_completion

        emit("\n<ansigray>🧠 命中热缓存，插入式压缩中（复用上轮请求前缀，仅付增量 token）...</ansigray>")
        try:
            response = _dispatch_completion(**request)
        except Exception:
            # 热路径失败（超窗/路由异常等）静默回退：分段路径自带 429 退避与
            # 错误上报，这里不重试、不重复报错
            return None

        raw_text = (response.choices[0].message.content or "").strip()
        # 非原生思考通道可能把思考内容以标签形式混进正文
        raw_text = re.sub(
            r"<(?:think|thinking)>.*?</(?:think|thinking)>", "",
            raw_text, flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        raw_text = re.sub(r"^记忆摘要[：:]\s*", "", raw_text).strip()
        raw_text = raw_text.replace("```", "").strip()
        topics, summary_text = _split_topics_from_summary(raw_text)
        if not summary_text:
            return None

        if _commit_summary_progress(
            manager, session_id, summary_text, boundary_msg_id, old_boundary_raw,
        ):
            manager.archive_summary_generation(
                session_name, summary_text, topics, boundary_msg_id,
            )
            emit("<ansigreen>✔ 插入式记忆压缩完成（热缓存命中，仅付增量 token）。</ansigreen>")
            return CompressionOutcome("compressed", "insert")
        # 水位冲突（期间发生淘汰或并发压缩）：直接报冲突，不再空烧分段路径
        emit("<ansiyellow>ℹ 插入式压缩水位冲突（期间有淘汰或并发压缩），本次已跳过。</ansiyellow>")
        return CompressionOutcome("skipped", "conflict_stale")
    except Exception:
        logging.getLogger("py.warnings").debug(
            "插入式压缩快速路径异常（已回退分段路径）", exc_info=True
        )
        return None


def _cancellable_sleep(duration, cancel_event=None):
    """支持被 cancel_event 打断的切片休眠。"""
    if duration <= 0:
        return True
    import time as _time
    deadline = _time.monotonic() + duration
    while _time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return False
        rem = deadline - _time.monotonic()
        _time.sleep(min(0.2, rem) if rem > 0 else 0.2)
    return True


def _commit_summary_progress(manager, session_id, summary, boundary_id, expect_boundary_raw):
    """把「已完成段」的摘要与水位 CAS 写库。

    仅当库里水位仍等于本次读取时的旧值才更新：期间若有淘汰或并发压缩推进过水位，
    这段进度已不可信（可能重复摘要或静默跳过消息），放弃本次提交。
    返回 True 表示已提交，调用方须把 CAS 基准前移到 boundary_id。
    """
    lock = manager.acquire_lock() if hasattr(manager, "acquire_lock") else contextlib.nullcontext()
    with lock:
        cursor = manager.conn.cursor()
        cursor.execute(
            "UPDATE sessions SET memory_summary = ?, summarized_until_msg_id = ? "
            "WHERE id = ? AND summarized_until_msg_id IS ?",
            (summary, boundary_id, session_id, expect_boundary_raw),
        )
        if cursor.rowcount == 0:
            manager.conn.rollback()
            return False
        manager.conn.commit()
    return True


def _finish_with_progress(committed, reason):
    """中止收尾：只要有段落已落库就算 compressed（下游淘汰有摘要兜底），
    否则才是 skipped（下游必须禁止推水位，见 agent_service 的淘汰禁区）。

    注意：partial_timeout / partial_cancelled 等部分提交中止时，下游微调
    淘汰从已提交水位起吃的是「未提交段」的轮次——这些轮次没有摘要兜底
    （已提交摘要只覆盖水位之前的历史）。这是有意识的权衡：淘汰受
    MAX_EVICT_TURNS 硬上限约束，超窗请求由 agent_service 的 I4 内存级
    轮次裁剪兜底保证不 400；下一轮压缩从已提交水位续推，未提交段仍有
    机会被补上摘要。
    """
    if committed:
        return CompressionOutcome("compressed", "partial_" + reason)
    return CompressionOutcome("skipped", reason)


def compress_session_memory(
    manager,
    session_name,
    api_model,
    api_key,
    api_url,
    force=False,
    output_callback=None,
    cancel_event=None,
    max_timeout_seconds=None,
    return_outcome=False,
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
            if return_outcome:
                return CompressionOutcome("skipped", "already_running")
            return False
        _compressing_sessions.add(guard_key)

    try:
        outcome = _do_compress(
            manager, session_name, api_model, api_key, api_url,
            force=force, emit=emit, cancel_event=cancel_event,
            max_timeout_seconds=max_timeout_seconds,
        )
        if return_outcome:
            return outcome
        return outcome.status == "compressed"
    finally:
        with _compress_guard:
            _compressing_sessions.discard(guard_key)


def _do_compress(manager, session_name, api_model, api_key, api_url, force, emit, cancel_event=None, max_timeout_seconds=None):
    config = _load_compression_config()
    if max_timeout_seconds is None:
        # 默认回落到配置值（config.json memory_compression.max_total_seconds），
        # 限流调优不必改代码
        max_timeout_seconds = config["max_total_seconds"]
    try:
        # 多步读写期间持有 manager 的可重入锁，与 add_messages 等写路径互斥
        lock = manager.acquire_lock() if hasattr(manager, 'acquire_lock') else contextlib.nullcontext()
        with lock:
            cursor = manager.conn.cursor()

            # 1. 查找会话基本信息
            cursor.execute('SELECT id, memory_summary, summarized_until_msg_id FROM sessions WHERE session_name = ?', (session_name,))
            row = cursor.fetchone()
            if not row:
                return CompressionOutcome("skipped", "session_not_found")

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
                all_active_messages.append({
                    "id": msg_id,
                    "role": role,
                    "content": message_content_to_text(raw_content)
                })

            # 3. 统计需要保留的最近活跃消息
            retain_count = config["retain_count_force"] if force else config["retain_count"]
            if len(all_active_messages) <= retain_count + 2:
                # force 与常规路径同样提示：静默跳过会让用户看到"正在后台压缩"
                # 之后永远没有下文
                emit(f"<ansiyellow>ℹ 当前会话活跃对话过少（少于 {retain_count + 2} 条），无需进行记忆压缩。</ansiyellow>")
                return CompressionOutcome("skipped", "too_few_messages")

            retain_floor = len(all_active_messages) - retain_count
            # 【PERSISTED 注入保留锚点】规格 02 §2.6 / C-T7：PERSISTED 注入合并进
            # 动态块这一条消息，压缩必须让它活下来。保留区本来纯按条数截取，长
            # 工具链回合里"末条 user 消息"可能被挤出保留区，于是最新的运行上下文
            # 快照会被摘要吞掉。命中该情形时把边界回退到那条消息；正常情形下它本
            # 就在保留区内，retain_floor 不变——即对本行为零影响。
            anchor = _persisted_context_anchor_index(all_active_messages)
            if anchor is not None and anchor < retain_floor:
                retain_floor = anchor
            messages_to_summarize = all_active_messages[:retain_floor]
            # 截断点绝不能落在工具调用组中间：若保留区的第一条消息是 tool 结果，
            # 说明其对应的 assistant(tool_calls) 被归入了待压缩区，压缩后该 tool
            # 消息会变成孤儿，导致后续所有请求报 "tool_call_id is not found"，
            # 整个会话无法继续使用。此时把保留区开头的连续 tool 消息一并归入待压缩区。
            retained = all_active_messages[retain_floor:]
            extra = 0
            while extra < len(retained) and retained[extra]["role"] == "tool":
                extra += 1
            if extra == len(retained):
                # 保留区整条都是 tool 结果（其声明 assistant 必在待压缩区），
                # 全部归入待压缩区后保留区清空：本次压缩没有可安全推进边界的
                # 锚点，强行压缩会让孤儿 tool 进入摘要且活跃区无消息，直接拒绝。
                emit("<ansiyellow>ℹ 保留区均为工具结果且配对助手消息在待压缩区，本次压缩已跳过（等待更多对话后再压缩）。</ansiyellow>")
                return CompressionOutcome("skipped", "pairing_protect")
            if extra:
                messages_to_summarize = all_active_messages[:retain_floor + extra]

            # 4. 组装待总结对话文本日志（清洗思考标签 + 预截断工具结果）
            prepared_messages = []
            total_chars = 0
            for msg in messages_to_summarize:
                role = msg["role"]
                # 上下文块（动态运行上下文/时间注记/后台汇总）是脚手架而非对话内容，
                # 不参与归纳，避免把多份计划快照/时间注记写进记忆摘要
                if role == "system":
                    continue
                if role == "user":
                    role_display = "用户"
                elif role == "tool":
                    role_display = "工具结果"
                else:
                    # 摘要里的自称跟随当前会话（秘书会话 → 秘书名）
                    try:
                        from core.secretary import get_active_agent_name

                        role_display = get_active_agent_name()
                    except Exception:
                        role_display = "星瑶"
                # 过滤内容里的思考标签以缩减 Token 负担
                clean_content = re.sub(r"<(?:think|thinking)>.*?</(?:think|thinking)>", "", msg["content"], flags=re.DOTALL | re.IGNORECASE).strip()
                # 工具结果（role=tool）超长时预截断
                if role == "tool" and clean_content:
                    clean_content = _truncate_long_tool_result(clean_content, config)
                if clean_content:
                    prepared_messages.append((msg["id"], role_display, clean_content))
                    total_chars += len(clean_content)

            if not prepared_messages:
                return CompressionOutcome("skipped", "no_content")

            # 提交边界：保留区之前的最后一条待压缩消息（配对保护已就位）
            boundary_msg_id = messages_to_summarize[-1]["id"]

        # ── 插入式快速路径（Insert-then-Compress）─────────────────────────
        # 保活服务存有本会话最后一次主请求的完整字节（ledger 消息 + tools +
        # 亲和参数）。在其尾部追加一条压缩指令，前缀与上次真实请求完全一致
        # → 命中热前缀缓存，只付指令与摘要输出的增量。条件不满足或任何失败
        # 一律静默落回下方分段路径，绝不硬失败（开关见 insert_mode 配置）。
        if config["insert_mode"]:
            insert_outcome = _try_insert_compression(
                manager, session_name, session_id, api_model, api_key, api_url,
                old_boundary_raw, boundary_msg_id, config, emit, cancel_event,
            )
            if insert_outcome is not None:
                return insert_outcome

        # LLM 调用耗时较长，在锁外执行，避免阻塞其它线程的正常读写
        emit("\n<ansigray>🧠 正在后台智能压缩并整合长对话记忆...</ansigray>")

        # 优先使用 secondary（便宜）模型做摘要，降低后台记忆压缩的额度消耗
        compress_model, compress_key, compress_url = api_model, api_key, api_url
        compress_window = 0
        try:
            from core.model_manager import get_secondary_model, get_secondary_roles
            if get_secondary_roles().get("compression"):
                secondary = get_secondary_model()
                if secondary:
                    compress_model = secondary["litellm_model"]
                    compress_key = secondary["api_key"]
                    compress_url = secondary["base_url"]
                    compress_window = int(secondary.get("context_window") or 0)
        except Exception:
            pass
        if compress_window <= 0:
            try:
                # 压缩模型是主模型时取主模型窗口（按会话解析：GUI 会话绑定的模型
                # 可能与全局默认不同，取错窗口会让分段预算失真），仍取不到按 128k 兜底
                from core.model_manager import get_active_model
                active = get_active_model(session_name)
                if active and active.get("litellm_model") == compress_model:
                    compress_window = int(active.get("context_window") or 0)
            except Exception:
                pass
        if compress_window <= 0:
            compress_window = 128000

        # 历史一次性塞进单个 prompt 会在长会话下超过压缩模型窗口
        # （如 88 万 token 历史 vs 26 万窗口），压缩请求本身超窗失败并反复重试。
        # 按窗口的安全预算切成多段，逐段合并摘要，每段完成即增量写库（中止不白烧）。
        # 预算取窗口 25%：其余留给指令、累积摘要、输出、Provider 处理延迟
        # 与估算偏差（tiktoken 对非 OpenAI 分词器的估算普遍偏低）。
        chunk_token_budget = _compression_chunk_budget(compress_window, config["max_chunk_token_budget"])

        chunks = []
        current_chunk = []
        current_tokens = 0
        for msg_id, role_display, content in prepared_messages:
            token_count = _estimate_tokens(content)
            if token_count > chunk_token_budget:
                # 单条消息即超预算（如超长助手文本）：按 token 截断而非按字符，
                # 中文 1 字符 ≈ 1~1.5 token，按字符截会严重超窗
                content, token_count = _truncate_to_token_budget(
                    content, chunk_token_budget
                )
            if current_chunk and current_tokens + token_count > chunk_token_budget:
                chunks.append(current_chunk)
                current_chunk, current_tokens = [], 0
            current_chunk.append((msg_id, role_display, content))
            current_tokens += token_count
        if current_chunk:
            chunks.append(current_chunk)
        total_chunks = len(chunks)

        # 懒加载压缩模型请求入口（首次调用触发 core.llm 加载）
        from core.llm import _dispatch_completion, _provider_cache_affinity_options, is_rate_limit_error

        new_summary = old_summary
        topics = None
        committed = False
        import time as _time
        import random as _random
        start_monotonic = _time.monotonic()
        # 段间延时自适应：仅当本次压缩触发过 429 限流后，才对后续段间
        # 插入平滑延时；未触限流时 0 延时直通，显著缩短多段串行总耗时。
        rate_limited = False

        # 每多一段放宽 30 字，缓解串行累积合并的信息衰减；上限 1200，
        # 但绝不低于用户显式配置值本身。
        dynamic_merge_limit = max(
            config["summary_length_merge"],
            min(1200, config["summary_length_merge"] + 30 * max(0, total_chunks - 1)),
        )
        if total_chunks > 12:
            emit("\n<ansiyellow>ℹ 待压缩历史过长（分段 > 12 段），建议在新会话中继续复杂任务以维持最佳上下文记忆。</ansiyellow>")

        for chunk_index, chunk in enumerate(chunks):
            # 预算至少容得下 3 段：单段超时可达 106s，只给 180s 会退化到
            # 「刚完成一段就爆预算」，永远压不完。
            per_chunk_seconds = (_compression_request_timeout(chunk_token_budget)
                                 + config["inter_chunk_delay_seconds"])
            budget_seconds = max(max_timeout_seconds, 3 * per_chunk_seconds)

            if cancel_event is not None and cancel_event.is_set():
                emit("<ansiyellow>ℹ 记忆压缩已被用户取消。</ansiyellow>")
                return _finish_with_progress(committed, "cancelled")
            if _time.monotonic() - start_monotonic > budget_seconds:
                emit("<ansiyellow>ℹ 记忆压缩耗时超出预算，本次已中止（已完成段落进度已保存）。</ansiyellow>")
                return _finish_with_progress(committed, "timeout")

            if chunk_index > 0:
                # 用户显式配置非 0 延时 → 按配置无条件生效；否则仅在本次
                # 压缩已触发过 429 时插入自适应冷却延时（默认 0 直通）。
                if config["inter_chunk_delay_seconds"] > 0:
                    adaptive_delay = config["inter_chunk_delay_seconds"]
                elif rate_limited:
                    adaptive_delay = COMPRESSION_ADAPTIVE_INTER_CHUNK_DELAY_SECONDS
                else:
                    adaptive_delay = 0.0
                if adaptive_delay > 0 and not _cancellable_sleep(adaptive_delay, cancel_event):
                    return _finish_with_progress(committed, "cancelled")

            chat_log = "".join(
                "%s: %s\n" % (role_display, content) for _mid, role_display, content in chunk
            )
            summary_limit = dynamic_merge_limit if new_summary else config["summary_length_first"]
            prompt = _build_summary_prompt(new_summary, chat_log, summary_limit)
            prompt_tokens = _estimate_tokens(prompt)
            request_timeout = _compression_request_timeout(prompt_tokens)
            if total_chunks > 1:
                emit(
                    "\n<ansigray>🧠 历史过长，分 "
                    f"{total_chunks} 段压缩中（第 {chunk_index + 1}/{total_chunks} 段，"
                    f"模型 {compress_model}，约 {prompt_tokens // 1000}k token，"
                    f"超时 {request_timeout} 秒）...</ansigray>"
                )

            request_kwargs = {
                "model": compress_model,
                "api_key": compress_key,
                "api_base": compress_url,
                "messages": [{"role": "user", "content": prompt}],
                # 分段载荷仍可能有数万 token，按实际 prompt 大小放大超时；
                # 固定 60 秒会让 64K～100K token 的摘要请求稳定超时。
                "timeout": request_timeout,
            }
            request_kwargs.update(_provider_cache_affinity_options(
                compress_model,
                compress_url,
                cache_scope="secondary:compression",
                cache_session_id=session_name,
                tool_contract="no-tools",
            ))
            # 渠道请求形态覆盖：与 _provider_request_options 出口同一语义
            # （headers → extra_headers、body → extra_body），压缩 scope 由此对齐
            from core.llm_dialects import _apply_route_request_overrides

            request_kwargs = _apply_route_request_overrides(
                request_kwargs, compress_model, compress_url
            )

            candidate = None
            for attempt in range(COMPRESSION_MAX_RETRIES):
                if cancel_event is not None and cancel_event.is_set():
                    return _finish_with_progress(committed, "cancelled")
                try:
                    response = _dispatch_completion(**request_kwargs)
                    raw_text = (response.choices[0].message.content or "").strip()
                    raw_text = re.sub(r"^记忆摘要[：:]\s*", "", raw_text)
                    raw_text = raw_text.replace("```", "").strip()
                    if raw_text:
                        round_topics, clean_text = _split_topics_from_summary(raw_text)
                        if clean_text:
                            candidate = clean_text
                            if round_topics:
                                topics = round_topics
                        break
                    else:
                        break
                except Exception as api_err:
                    is_rate_limit, retry_after = is_rate_limit_error(api_err)
                    if is_rate_limit and attempt < COMPRESSION_MAX_RETRIES - 1:
                        rate_limited = True
                        if retry_after is not None and retry_after > 0:
                            backoff = retry_after
                        else:
                            backoff = COMPRESSION_RATE_LIMIT_BASE_DELAY_SECONDS * (2 ** attempt) + _random.uniform(0.5, 2.0)
                        emit(
                            f"\n<ansiyellow>ℹ 压缩请求触发服务商速率限制（429），正在等待 {backoff:.1f} 秒后重试（第 {attempt + 1}/{COMPRESSION_MAX_RETRIES} 次）...</ansiyellow>"
                        )
                        if not _cancellable_sleep(backoff, cancel_event):
                            return _finish_with_progress(committed, "cancelled")
                        continue
                    raise api_err

            if not candidate:
                # 某一段返回空结果：继续合并后续段会丢失该段事实，就此中止；
                # 已完成段落的进度已在上一轮循环提交，不回滚、不白烧。
                emit("<ansiyellow>ℹ 记忆压缩中途返回空摘要，本次已中止（已完成段落进度已保存）。</ansiyellow>")
                return _finish_with_progress(committed, "empty_summary")
            new_summary = candidate

            # 每段完成即落库：任何后续中止（429 用尽 / 超预算 / 取消 / 异常）
            # 最多损失当前段，不会把已付费的摘要一起作废。
            if _commit_summary_progress(
                manager, session_id, new_summary,
                chunk[-1][0], old_boundary_raw,
            ):
                committed = True
                old_boundary_raw = chunk[-1][0]        # CAS 基准随进度前移
                emit("<ansigray>💾 已保存第 %d/%d 段压缩进度。</ansigray>"
                     % (chunk_index + 1, total_chunks))
            else:
                emit("<ansiyellow>ℹ 压缩进度与库内水位冲突（期间有淘汰或并发压缩），已停止后续提交。</ansiyellow>")
                return _finish_with_progress(committed, "conflict_stale")

        # 多代归档：最终摘要 + topics + 边界入库（纯增量元数据，方法内部吞错，
        # 归档失败只损失回溯索引，绝不影响压缩成果）。old_boundary_raw 此时已
        # 随逐段提交前移到最后一段的 msg_id。
        manager.archive_summary_generation(session_name, new_summary, topics, old_boundary_raw)
        emit("<ansigreen>✔ 记忆压缩整合成功！腾出了约 %d 字的历史对话空间。</ansigreen>"
             % (total_chars // 2))
        return CompressionOutcome("compressed", "ok")

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
        return CompressionOutcome("failed", str(e))

    return CompressionOutcome("skipped", "unknown")
