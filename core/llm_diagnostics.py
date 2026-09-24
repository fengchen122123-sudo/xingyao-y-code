"""Token usage accounting and prompt cache diagnostics."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

# 全局变量：分离“本次窗口占比”和“整体累积消耗”
session_token_usage = {
    # 累积消耗（用来算钱）
    "cumulative_total": 0,
    "cumulative_prompt": 0,
    "cumulative_completion": 0,
    "cumulative_cached": 0,
    "cumulative_cache_prompt": 0,

    # 窗口占用（用来防止爆 128k 上下文）
    "latest_total": 0,
    "latest_prompt": 0,
    "latest_cached": 0,

    # 缓存遥测（仅主代理镜像到状态栏；按会话独立累计）
    "cache_latest_rate": 0.0,
    "cache_hot_rate": 0.0,
    "cache_cumulative_rate": 0.0,
    "cache_hot_turns": 0,
    "cache_cumulative_cached": 0,
    "cache_invalidation_reason": "",

    "max_window": 128000,  # 默认最大上下文窗口配置
}
# session_token_usage 会被后台 worker 线程与 UI 线程同时读写，所有读写必须走锁
token_usage_lock = threading.Lock()

# 记账完成回调：GUI（gui_bridge.py）启动时注册 _emit_token_usage，
# 让统计栏/上下文圆圈在每次 LLM 请求记账后立即刷新，而不必等整个
# 回合结束。CLI 不注册则完全无感。仅保存回调引用，不持锁调用，
# 避免回调里再取 token_usage_lock 时死锁。
_token_usage_listener = None


def set_token_usage_listener(callback):
    """注册/注销（传 None）token 记账完成监听器；幂等且线程安全（GIL 原子赋值）。"""
    global _token_usage_listener
    _token_usage_listener = callback


_cache_request_states = {}
_cache_usage_states = {}


def _cache_usage_key(session_id, model_name, api_base, cache_scope="main"):
    """生成缓存遥测身份；同会话的不同模型、接口与作用域绝不合并。"""
    return (
        str(session_id),
        str(model_name or "").lower(),
        str(api_base or "").rstrip("/").lower(),
        str(cache_scope or "main"),
    )


def _strip_cache_control_markers(messages):
    """返回消息列表的「客户端逻辑前缀」副本：脱离所有 cache_control 标记。

    覆盖两种标记形态（跨渠道缓存命中率修复 · Task 2）：
    - 消息顶层 ``cache_control``（Anthropic 协议）；
    - ``content`` 列表内每个块的 ``cache_control``（Qwen / DashScope 兼容形态，
      ``_apply_cache_control`` 会把字符串 content 改写为
      ``[{"type": "text", "text": …, "cache_control": …}]``）。

    除 ``cache_control`` 键外不改变任何字段值；打标引入的单文本块包装
    （剥完后恰为 ``[{"type": "text", "text": X}]``）还原为客户端原字符串 X，
    否则「同一批消息打标前后」的 sha256 会因 content 形态改写而漂移。
    多块 content（如文本 + 图片）原样保留列表形态，只剥标记。
    不修改传入对象（逐消息拷贝），调用方随后仍要拿原始消息打标、发送。
    """
    normalized = []
    for message in messages or []:
        if not isinstance(message, dict):
            normalized.append(message)
            continue
        clean = copy.deepcopy(message)
        clean.pop("cache_control", None)
        content = clean.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
            if (
                len(content) == 1
                and isinstance(content[0], dict)
                and content[0].get("type") == "text"
                and set(content[0]) == {"type", "text"}
            ):
                clean["content"] = content[0]["text"]
        normalized.append(clean)
    return normalized


def _cache_message_fingerprints(messages):
    fingerprints = []
    for message in _strip_cache_control_markers(messages):
        payload = _canonical_diagnostic_json(message)
        fingerprints.append(hashlib.sha256(payload.encode("utf-8")).hexdigest())
    return tuple(fingerprints)


def _canonical_diagnostic_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _build_cache_request_diagnostic(
    session_id,
    cache_scope,
    model_name,
    api_base,
    tools,
    messages,
    route_override_fingerprint="",
):
    """构建不含正文的确定性请求形状诊断。

    route_override_fingerprint 为渠道请求形态覆盖的生效指纹（无覆盖为空串），
    只作记录，便于事后按 route_override_sha256 归因缓存换代是否由覆盖变更引起。
    """
    normalized_messages = _strip_cache_control_markers(messages)
    system_messages = [
        message for message in normalized_messages
        if message.get("role") == "system"
    ]
    normalized_tools = list(tools or [])
    messages_json = _canonical_diagnostic_json(normalized_messages)
    system_json = _canonical_diagnostic_json(system_messages)
    tools_json = _canonical_diagnostic_json(normalized_tools)
    return {
        "session_id": str(session_id),
        "model": str(model_name or "").lower(),
        "api_base": str(api_base or "").rstrip("/").lower(),
        "cache_scope": str(cache_scope or "main"),
        "messages_sha256": hashlib.sha256(messages_json.encode("utf-8")).hexdigest(),
        "system_sha256": hashlib.sha256(system_json.encode("utf-8")).hexdigest(),
        "tools_sha256": hashlib.sha256(tools_json.encode("utf-8")).hexdigest(),
        "message_count": len(normalized_messages),
        "system_message_count": len(system_messages),
        "request_bytes": len(messages_json.encode("utf-8")),
        "system_bytes": len(system_json.encode("utf-8")),
        "tools_bytes": len(tools_json.encode("utf-8")),
        "route_override_sha256": str(route_override_fingerprint or ""),
        "_message_fingerprints": _cache_message_fingerprints(normalized_messages),
    }


def _cache_control_request_metrics(messages):
    """统计打标后线上消息的（显式 cache_control 标记数, 规范化字节数）。

    与 `_build_cache_request_diagnostic` 的 request_bytes 使用同一规范化函数
    （canonical JSON），保证「客户端逻辑前缀字节」(request_bytes) 与
    「线上字节事实」(request_bytes_final) 可直接对照；标记计数覆盖消息顶层
    与 content 块两种形态。该函数按线上原样测量，不做任何剥离。
    """
    marker_count = 0
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        if "cache_control" in message:
            marker_count += 1
        content = message.get("content")
        if isinstance(content, list):
            marker_count += sum(
                1
                for block in content
                if isinstance(block, dict) and "cache_control" in block
            )
    payload = _canonical_diagnostic_json(messages or [])
    return marker_count, len(payload.encode("utf-8"))


def _classify_cache_request_details(
    session_id,
    cache_scope,
    model_name,
    api_base,
    tool_contract,
    messages,
    tools=None,
    route_override_fingerprint="",
):
    """记录请求形状并返回可持久化的原因、哈希与公共前缀长度。

    route_override_fingerprint 只透传进诊断记录（route_override_sha256），
    不参与下方 current 的换代比较：覆盖变化不该被误判成一次缓存换代。
    """
    state_key = (str(session_id), str(cache_scope or "main"))
    details = _build_cache_request_diagnostic(
        session_id, cache_scope, model_name, api_base, tools, messages,
        route_override_fingerprint=route_override_fingerprint,
    )
    current = {
        "model": details["model"],
        "api_base": details["api_base"],
        "tool_contract": str(tool_contract or details["tools_sha256"]),
        "messages": details["_message_fingerprints"],
    }
    with token_usage_lock:
        previous = _cache_request_states.get(state_key)
        common_prefix = 0
        if previous is not None:
            for old_hash, new_hash in zip(previous["messages"], current["messages"]):
                if old_hash != new_hash:
                    break
                common_prefix += 1
        if previous is None:
            reason = "冷启动"
        elif (
            previous["model"] != current["model"]
            or previous["api_base"] != current["api_base"]
        ):
            reason = "模型或接口变化"
        elif previous["tool_contract"] != current["tool_contract"]:
            reason = "工具契约变化"
        elif common_prefix != len(previous["messages"]):
            reason = "上下文压缩或历史重写"
        else:
            reason = ""
        _cache_request_states[state_key] = current
        if len(_cache_request_states) > 128:
            _cache_request_states.pop(next(iter(_cache_request_states)))
    details["request_reason"] = reason
    details["common_prefix_messages"] = common_prefix
    del details["_message_fingerprints"]
    return details


def _classify_cache_request(
    session_id,
    cache_scope,
    model_name,
    api_base,
    tool_contract,
    messages,
):
    """记录请求形状并返回确定性的缓存失效原因；空字符串表示严格追加。"""
    return _classify_cache_request_details(
        session_id,
        cache_scope,
        model_name,
        api_base,
        tool_contract,
        messages,
    )["request_reason"]


def _record_cache_usage(
    session_id,
    cache_scope,
    prompt_tokens,
    cached_tokens,
    invalidation_reason,
    model_name="",
    api_base="",
):
    """分别累计当前轮、热启动与会话总命中率，并返回该通道的最新快照。"""
    if isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int) or prompt_tokens < 0:
        raise ValueError("prompt_tokens must be a non-negative integer")
    if isinstance(cached_tokens, bool) or not isinstance(cached_tokens, int) or cached_tokens < 0:
        raise ValueError("cached_tokens must be a non-negative integer")
    if cached_tokens > prompt_tokens:
        raise ValueError("cached_tokens cannot exceed prompt_tokens")
    prompt = prompt_tokens
    cached = cached_tokens
    state_key = _cache_usage_key(
        session_id, model_name, api_base, cache_scope
    )
    with token_usage_lock:
        state = _cache_usage_states.setdefault(state_key, {
            "cumulative_prompt": 0,
            "cumulative_cached": 0,
            "hot_prompt": 0,
            "hot_cached": 0,
            "hot_turns": 0,
            "latest_prompt": 0,
            "latest_cached": 0,
            "usage_valid": True,
            "usage_error": "",
            "invalidation_reason": "",
        })
        state["latest_prompt"] = prompt
        state["latest_cached"] = cached
        state["cumulative_prompt"] += prompt
        state["cumulative_cached"] += cached
        if not invalidation_reason:
            state["hot_prompt"] += prompt
            state["hot_cached"] += cached
            state["hot_turns"] += 1
        state["usage_valid"] = True
        state["usage_error"] = ""
        state["invalidation_reason"] = str(invalidation_reason or "")

        snapshot = dict(state)
        snapshot["latest_rate"] = (cached / prompt * 100) if prompt else 0.0
        snapshot["hot_rate"] = (
            state["hot_cached"] / state["hot_prompt"] * 100
            if state["hot_prompt"] else 0.0
        )
        snapshot["cumulative_rate"] = (
            state["cumulative_cached"] / state["cumulative_prompt"] * 100
            if state["cumulative_prompt"] else 0.0
        )
        snapshot["invalidation_reason"] = str(invalidation_reason or "")

        if str(cache_scope or "main") == "main":
            session_token_usage["cache_latest_rate"] = snapshot["latest_rate"]
            session_token_usage["cache_hot_rate"] = snapshot["hot_rate"]
            session_token_usage["cache_cumulative_rate"] = snapshot["cumulative_rate"]
            session_token_usage["cache_hot_turns"] = snapshot["hot_turns"]
            session_token_usage["cache_cumulative_cached"] = snapshot["cumulative_cached"]
            session_token_usage["cache_invalidation_reason"] = snapshot["invalidation_reason"]
        return snapshot


def _mark_cache_usage_unavailable(
    session_id,
    model_name,
    api_base,
    cache_scope="main",
    reason="usage 不可用",
):
    """标记最新缓存 usage 不可用，同时保留此前已验证的累计计数。"""
    state_key = _cache_usage_key(
        session_id, model_name, api_base, cache_scope
    )
    with token_usage_lock:
        state = _cache_usage_states.setdefault(state_key, {
            "cumulative_prompt": 0,
            "cumulative_cached": 0,
            "hot_prompt": 0,
            "hot_cached": 0,
            "hot_turns": 0,
            "latest_prompt": 0,
            "latest_cached": 0,
            "usage_valid": False,
            "usage_error": "",
            "invalidation_reason": "",
        })
        state["usage_valid"] = False
        state["usage_error"] = str(reason or "usage 不可用")
        if str(cache_scope or "main") == "main":
            session_token_usage["cache_invalidation_reason"] = state["usage_error"]


def record_keepalive_lane_usage(
    session_id,
    model_name,
    api_base,
    prompt_tokens,
    cached_tokens,
):
    """把一轮保活 usage 计入 keepalive 车道桶：token 走既有记账，轮次单独 +1。

    跨渠道缓存命中率修复 · Task 5 的证据数据源补记：保活亏损是稳态特征，
    判定需要「该车道 keepalive 的轮次 + token 加权命中率」，而现有
    _record_cache_usage 的 hot_turns 语义是「无失效原因的轮次」——保活请求
    在 _account_server_usage 侧的 invalidation_reason 是「保活请求」（非空），
    hot_turns 对 keepalive 桶恒为 0，不能当样本数用，因此轮次用独立的
    ``turns`` 计数（桶结构是 dict，零迁移成本；旧桶无该键 → 0 → 样本不足 →
    不动作，方向保守）。

    本函数只做「保活 scope 补记」这一件事，不改 _record_cache_usage /
    _normalize_server_usage / _cache_usage_key 的任何语义；main 记账路径
    （_account_server_usage 的 main_scope 门）完全不动。
    """
    _record_cache_usage(
        session_id,
        "keepalive",
        prompt_tokens,
        cached_tokens,
        "",
        model_name,
        api_base,
    )
    state_key = _cache_usage_key(session_id, model_name, api_base, "keepalive")
    with token_usage_lock:
        state = _cache_usage_states.get(state_key)
        if state is not None:
            state["turns"] = int(state.get("turns", 0) or 0) + 1


def _cache_usage_view(state):
    """把某车道的 usage 状态转成 latest/hot/session_all 三口径视图。

    usage_valid=False（usage 未报告/不一致）时三个 rate 一律为 None：
    未报告 ≠ 0 命中（铁律 3），调用方按 None 展示为 "--" 而不是 0%。
    """
    if state is None:
        return {
            "available": False,
            "usage_valid": False,
            "latest": {"prompt_tokens": 0, "cached_tokens": 0, "rate": None},
            "hot": {"prompt_tokens": 0, "cached_tokens": 0, "rate": None},
            "session_all": {"prompt_tokens": 0, "cached_tokens": 0, "rate": None},
            "hot_turns": 0,
            "invalidation_reason": "",
        }
    valid = bool(state.get("usage_valid", True))

    def _rate(cached_value, prompt_value):
        if not valid or not prompt_value:
            return None
        return cached_value / prompt_value * 100

    latest_prompt = int(state.get("latest_prompt", 0) or 0)
    latest_cached = int(state.get("latest_cached", 0) or 0)
    hot_prompt = int(state.get("hot_prompt", 0) or 0)
    hot_cached = int(state.get("hot_cached", 0) or 0)
    cumulative_prompt = int(state.get("cumulative_prompt", 0) or 0)
    cumulative_cached = int(state.get("cumulative_cached", 0) or 0)
    return {
        "available": valid,
        "usage_valid": valid,
        "latest": {
            "prompt_tokens": latest_prompt,
            "cached_tokens": latest_cached,
            "rate": _rate(latest_cached, latest_prompt),
        },
        "hot": {
            "prompt_tokens": hot_prompt,
            "cached_tokens": hot_cached,
            "rate": _rate(hot_cached, hot_prompt),
        },
        "session_all": {
            "prompt_tokens": cumulative_prompt,
            "cached_tokens": cumulative_cached,
            "rate": _rate(cumulative_cached, cumulative_prompt),
        },
        "hot_turns": int(state.get("hot_turns", 0) or 0),
        "invalidation_reason": str(
            state.get("usage_error")
            if not valid
            else state.get("invalidation_reason", "")
        ),
    }


def get_token_usage_snapshot(
    session_id,
    model_name,
    api_base,
    cache_scope="main",
):
    """返回线程安全的 token 与当前缓存身份快照，不暴露内部可变状态。

    cache 仍是「当前 scope」的视图（键与语义不变）；cache_scopes 是
    同一 (会话, 模型, 接口) 下按 scope 分离的只读视图（如 keepalive 单列），
    供展示层把保活与 main 分开，不改变任何记账语义。
    """
    state_key = _cache_usage_key(
        session_id, model_name, api_base, cache_scope
    )
    with token_usage_lock:
        state = _cache_usage_states.get(state_key)
        window = {
            "latest_total": int(session_token_usage.get("latest_total", 0) or 0),
            "latest_prompt": int(session_token_usage.get("latest_prompt", 0) or 0),
            "latest_cached": int(session_token_usage.get("latest_cached", 0) or 0),
            "max_window": int(session_token_usage.get("max_window", 0) or 0),
        }
        process = {
            "cumulative_total": int(session_token_usage.get("cumulative_total", 0) or 0),
            "cumulative_prompt": int(session_token_usage.get("cumulative_prompt", 0) or 0),
            "cumulative_completion": int(
                session_token_usage.get("cumulative_completion", 0) or 0
            ),
            "cumulative_cached": int(session_token_usage.get("cumulative_cached", 0) or 0),
            "cumulative_cache_prompt": int(
                session_token_usage.get("cumulative_cache_prompt", 0) or 0
            ),
        }
        cache = _cache_usage_view(state)
        current_scope = str(cache_scope or "main")
        cache_scopes = {current_scope: cache}
        lane_identity = state_key[:3]
        for key, scope_state in _cache_usage_states.items():
            if key[:3] != lane_identity or key[3] == current_scope:
                continue
            cache_scopes[key[3]] = _cache_usage_view(scope_state)
    return {
        "window": window,
        "process": process,
        "cache": cache,
        "cache_scopes": cache_scopes,
    }


# 保活车道证据的短 TTL 缓存：_supports_cache_keepalive 在热路径上每轮被多次
# 调用，跨会话聚合是 O(桶数) 扫描，不能每轮都做。30~60 秒窗口对「分钟级保活 +
# 小时级止损」完全够用。测试可清空/置零 expire_at 强制重读（与
# _keepalive_routes_cfg_cache 同款手法）。
_KEEPALIVE_LANE_EVIDENCE_TTL_SECONDS = 45.0
_keepalive_lane_evidence_cache = {"expire_at": 0.0, "lanes": {}}
_keepalive_lane_evidence_lock = threading.RLock()


def _scan_keepalive_lane_evidence():
    """扫描一次 _cache_usage_states：按 (model, api_base) 聚合 keepalive 桶。

    只读聚合，不写任何状态：轮次取桶的 turns 计数，token 取累计口径。
    未报告缓存明细的轮次从不写桶，天然不进分母（未报告 ≠ 0，铁律 3）。
    键口径与 _cache_usage_key 一致（小写 + 去尾斜杠）。
    """
    lanes = {}
    with token_usage_lock:
        for key, state in _cache_usage_states.items():
            if str(key[3]) != "keepalive":
                continue
            lane = (key[1], key[2])
            turns, prompt_tokens, cached_tokens = lanes.get(lane, (0, 0, 0))
            lanes[lane] = (
                turns + int(state.get("turns", 0) or 0),
                prompt_tokens + int(state.get("cumulative_prompt", 0) or 0),
                cached_tokens + int(state.get("cumulative_cached", 0) or 0),
            )
    return lanes


def lane_keepalive_evidence(model_name, api_base):
    """返回该车道 keepalive 的 (turns, prompt_tokens, cached_tokens, hit_rate)。

    跨会话聚合同一 (model, api_base) 下所有 cache_scope='keepalive' 桶：
    hit_rate = sum(cached)/sum(prompt)（token 加权）；无样本（prompt=0）时为
    None——调用方必须先看 turns，无样本绝不当作 0 命中处理。
    """
    lane = (
        str(model_name or "").lower(),
        str(api_base or "").rstrip("/").lower(),
    )
    now = time.monotonic()
    with _keepalive_lane_evidence_lock:
        if _keepalive_lane_evidence_cache["expire_at"] > now:
            lanes = _keepalive_lane_evidence_cache["lanes"]
        else:
            lanes = _scan_keepalive_lane_evidence()
            _keepalive_lane_evidence_cache["lanes"] = lanes
            _keepalive_lane_evidence_cache["expire_at"] = (
                now + _KEEPALIVE_LANE_EVIDENCE_TTL_SECONDS
            )
    turns, prompt_tokens, cached_tokens = lanes.get(lane, (0, 0, 0))
    hit_rate = (cached_tokens / prompt_tokens) if prompt_tokens else None
    return (turns, prompt_tokens, cached_tokens, hit_rate)


# 保活止损证据的跨进程存活（启动播种）。证据桶是内存态、进程重启即清零，而判定
# 门槛是「该车道保活 ≥10 轮且命中率 <0.5」——意味着每次重启都要重新白烧 10 轮全价
# 前缀才够判一次（实测亏损车道保活单轮平均 10.6 万 token，10 轮 ≈ 106 万）。
# token_usage 里本来就有 cache_scope='keepalive' 的落库行，启动时按车道播进内存
# 即可零成本续上，重启后第一次保活尝试前就能止损。
PERSISTED_KEEPALIVE_EVIDENCE_SESSION = "__persisted_keepalive__"
_KEEPALIVE_EVIDENCE_SEED_WINDOW_DAYS = 7
# 进程启动时刻：播种窗口的右边界，用来把「本进程自己已计入内存的轮次」排除掉。
_KEEPALIVE_EVIDENCE_PROCESS_STARTED_AT = int(time.time())
_keepalive_lane_evidence_seeded = False
_keepalive_lane_evidence_seed_lock = threading.RLock()


def seed_keepalive_lane_evidence_from_store(
    before_ts=None,
    window_days=_KEEPALIVE_EVIDENCE_SEED_WINDOW_DAYS,
    force=False,
    manager=None,
):
    """把上一个进程留下的保活轮次播进内存证据桶，返回本次播种的车道数。

    - `before_ts` 默认取进程启动时刻：本进程记的轮次已经在内存桶里，再播一遍会
      双计（轮次翻倍 → 样本门槛被提前跨过，等于把「≥10 轮才动」的铁律削弱成 5 轮）。
    - 窗口只取最近 `window_days` 天：保活亏损是稳态特征，但陈旧数据不该永久压制
      一条已经好转的车道复活。
    - 播进独立的合成会话键，不冒充任何真实会话：`get_token_usage_snapshot` 的
      `cache_scopes` 按 (会话, 模型, 接口) 过滤，因此 GUI 的单会话保活展示不受影响，
      而 `_scan_keepalive_lane_evidence` 的跨会话聚合能看到它。
    - 只播一次（幂等）：`force=True` 才允许重复播种；manager 尚未就绪时不置
      「已播种」标记，留给启动路径重试。失败只记日志，绝不影响启动。
    """
    global _keepalive_lane_evidence_seeded
    with _keepalive_lane_evidence_seed_lock:
        if _keepalive_lane_evidence_seeded and not force:
            return 0
        resolved = manager
        if resolved is None:
            try:
                from core import app_context

                resolved = app_context.get_manager()
            except Exception:
                resolved = None
        if resolved is None:
            return 0
        cutoff = int(
            before_ts
            if before_ts is not None
            else _KEEPALIVE_EVIDENCE_PROCESS_STARTED_AT
        )
        try:
            lanes = resolved.query_keepalive_lane_evidence(
                before_ts=cutoff,
                since_ts=cutoff - int(window_days) * 86400,
            )
        except Exception:
            logger.debug("保活车道证据播种查询失败（已忽略）", exc_info=True)
            return 0
        seeded = 0
        with token_usage_lock:
            for (model_key, base_key), totals in sorted((lanes or {}).items()):
                turns, prompt_total, cached_total = totals
                if int(turns) <= 0 or int(prompt_total) <= 0:
                    continue
                state_key = (
                    PERSISTED_KEEPALIVE_EVIDENCE_SESSION,
                    model_key,
                    base_key,
                    "keepalive",
                )
                if state_key in _cache_usage_states:
                    continue
                _cache_usage_states[state_key] = {
                    "cumulative_prompt": int(prompt_total),
                    "cumulative_cached": int(cached_total),
                    "hot_prompt": int(prompt_total),
                    "hot_cached": int(cached_total),
                    "hot_turns": int(turns),
                    # 播种是历史合计，不冒充「最新一轮」：latest 一律留 0，
                    # 展示层按 prompt=0 → rate=None 处理（未报告 ≠ 0）。
                    "latest_prompt": 0,
                    "latest_cached": 0,
                    "usage_valid": True,
                    "usage_error": "",
                    "invalidation_reason": "持久化保活证据播种",
                    "turns": int(turns),
                }
                seeded += 1
        with _keepalive_lane_evidence_lock:
            _keepalive_lane_evidence_cache["expire_at"] = 0.0
            _keepalive_lane_evidence_cache["lanes"] = {}
        _keepalive_lane_evidence_seeded = True
        if seeded:
            logger.info(
                "保活车道证据已从持久化用量播种：%d 条车道（本进程启动前、最近 %d 天）",
                seeded,
                int(window_days),
            )
        return seeded


def rename_cache_usage_session(old_name, new_name):
    """会话重命名时原子迁移缓存请求形状与 usage 遥测。"""
    old_key = str(old_name)
    new_key = str(new_name)
    if old_key == new_key:
        return
    with token_usage_lock:
        for mapping in (_cache_request_states, _cache_usage_states):
            moves = [
                (key, (new_key, *key[1:]), value)
                for key, value in list(mapping.items())
                if key and key[0] == old_key
            ]
            for source, target, value in moves:
                mapping[target] = value
                del mapping[source]
    try:
        from core.agent_service import rename_session_stable_sections

        rename_session_stable_sections(old_key, new_key)
    except Exception:
        logger.debug("稳定段落快照迁移失败（已忽略）", exc_info=True)


def clear_cache_usage_session(session_name):
    """清理指定会话的进程内缓存遥测，防止同名新会话继承旧状态。"""
    session_key = str(session_name)
    with token_usage_lock:
        for mapping in (_cache_request_states, _cache_usage_states):
            for key in [key for key in mapping if key and key[0] == session_key]:
                del mapping[key]
    try:
        from core.agent_service import clear_session_stable_sections

        clear_session_stable_sections(session_key)
    except Exception:
        logger.debug("稳定段落快照清理失败（已忽略）", exc_info=True)


def _persist_token_usage(model_name, prompt_tokens, completion_tokens,
                         cached_tokens, total_tokens, session_id=None,
                         api_base="", cache_scope="legacy",
                         cache_write_tokens=None):
    """把本轮真实 token 用量追加到本地 token_usage 表，供 /usage 按模型/日期统计。

    失败仅记日志，绝不影响对话主流程。model_name 为 provider 路由模型串
    （如 "openai/deepseek-v4-pro"），按第一个 "/" 拆出 provider 与 model。
    """
    try:
        from core import app_context
        manager = app_context.get_manager()
        if manager is None:
            return
        full_name = str(model_name or "")
        if "/" in full_name:
            provider, model = full_name.split("/", 1)
        else:
            provider, model = "", full_name
        manager.add_token_usage(
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens,
            session_id=session_id,
            api_base=api_base,
            cache_scope=cache_scope,
            cache_write_tokens=cache_write_tokens,
        )
    except Exception as _persist_err:
        try:
            logger.debug("token 用量持久化失败（已忽略）: %s", _persist_err)
        except Exception:
            pass


_USAGE_FIELD_MISSING = object()


def _usage_field(value, name):
    """读取 dict 或 SDK 对象字段，并保留“缺失”与显式 None/0 的区别。"""
    if isinstance(value, dict):
        return value[name] if name in value else _USAGE_FIELD_MISSING
    if value is None:
        return _USAGE_FIELD_MISSING
    return getattr(value, name, _USAGE_FIELD_MISSING)


def _optional_nonnegative_int(raw, field_name, errors):
    """把服务端整数规范为 int；拒绝会丢精度或掩盖异常的隐式转换。"""
    if raw is _USAGE_FIELD_MISSING or raw is None:
        return None
    if isinstance(raw, bool):
        errors.append(f"{field_name} must be a non-negative integer")
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
    else:
        errors.append(f"{field_name} must be a non-negative integer")
        return None
    if value < 0:
        errors.append(f"{field_name} must be a non-negative integer")
        return None
    return value


def _normalize_server_usage(usage):
    """归一化并严格校验一次服务端 usage，不估算或夹断任何真实 token。"""
    errors = []
    prompt_raw = _usage_field(usage, "prompt_tokens")
    completion_raw = _usage_field(usage, "completion_tokens")
    total_raw = _usage_field(usage, "total_tokens")

    # Anthropic 协议端点（Kimi /coding、聚合网关等）返回 input_tokens/
    # output_tokens 且通常不返回 total_tokens。OpenAI 字段缺失时改读
    # anthropic 字段兜底，避免整条记账被 "prompt_tokens is required" 拒绝。
    if prompt_raw is _USAGE_FIELD_MISSING or prompt_raw is None:
        anthropic_prompt = _usage_field(usage, "input_tokens")
        if anthropic_prompt is not _USAGE_FIELD_MISSING and anthropic_prompt is not None:
            prompt_raw = anthropic_prompt
    if completion_raw is _USAGE_FIELD_MISSING or completion_raw is None:
        anthropic_completion = _usage_field(usage, "output_tokens")
        if anthropic_completion is not _USAGE_FIELD_MISSING and anthropic_completion is not None:
            completion_raw = anthropic_completion

    prompt = _optional_nonnegative_int(prompt_raw, "prompt_tokens", errors)
    completion = _optional_nonnegative_int(
        completion_raw, "completion_tokens", errors
    )
    total = _optional_nonnegative_int(total_raw, "total_tokens", errors)

    if prompt_raw is _USAGE_FIELD_MISSING or prompt_raw is None:
        errors.append("prompt_tokens is required")
    if total_raw is _USAGE_FIELD_MISSING or total_raw is None:
        # Anthropic 协议不返回 total_tokens：由 input+output 推导，不算缺失。
        if prompt is not None and completion is not None:
            total = prompt + completion
        else:
            errors.append("total_tokens is required")

    details = _usage_field(usage, "prompt_tokens_details")
    details_cached_raw = _usage_field(details, "cached_tokens")
    direct_cached_raw = _usage_field(usage, "prompt_cache_hit_tokens")
    anthropic_cached_raw = _usage_field(usage, "cache_read_input_tokens")
    miss_raw = _usage_field(usage, "prompt_cache_miss_tokens")

    details_cache_reported = (
        details_cached_raw is not _USAGE_FIELD_MISSING
        and details_cached_raw is not None
    )
    direct_cache_reported = (
        direct_cached_raw is not _USAGE_FIELD_MISSING
        and direct_cached_raw is not None
    )
    details_cached = _optional_nonnegative_int(
        details_cached_raw, "prompt_tokens_details.cached_tokens", errors
    )
    direct_cached = _optional_nonnegative_int(
        direct_cached_raw, "prompt_cache_hit_tokens", errors
    )
    if (
        details_cache_reported
        and direct_cache_reported
        and details_cached is not None
        and direct_cached is not None
        and details_cached != direct_cached
    ):
        errors.append("cached token sources disagree")

    if details_cache_reported:
        cached_raw = details_cached_raw
    elif direct_cache_reported:
        cached_raw = direct_cached_raw
    else:
        cached_raw = anthropic_cached_raw
    cache_reported = cached_raw is not _USAGE_FIELD_MISSING and cached_raw is not None
    miss_reported = miss_raw is not _USAGE_FIELD_MISSING and miss_raw is not None

    cached = _optional_nonnegative_int(cached_raw, "cached_tokens", errors)
    cache_miss = _optional_nonnegative_int(miss_raw, "cache_miss_tokens", errors)

    completion_was_reported = (
        completion_raw is not _USAGE_FIELD_MISSING and completion_raw is not None
    )
    if not completion_was_reported and prompt is not None and total is not None:
        if total < prompt:
            errors.append("total_tokens is smaller than prompt_tokens")
        else:
            completion = total - prompt

    if (
        prompt is not None
        and completion is not None
        and total is not None
        and prompt + completion != total
    ):
        errors.append("prompt + completion does not equal total_tokens")
    if cached is not None and prompt is not None and cached > prompt:
        errors.append("cached_tokens exceeds prompt_tokens")
    if miss_reported and not cache_reported:
        errors.append("cache miss reported without cache hit")
    if (
        cache_reported
        and miss_reported
        and cached is not None
        and cache_miss is not None
        and prompt is not None
        and cached + cache_miss != prompt
    ):
        errors.append("cache hit + miss does not equal prompt_tokens")

    return {
        "valid": not errors,
        "errors": tuple(errors),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": cached if cache_reported else None,
        "cache_miss_tokens": cache_miss if miss_reported else None,
        "cache_reported": cache_reported,
    }


def _extract_cached_tokens(usage):
    """兼容旧调用方：读取缓存命中 token；未报告时返回 0。"""
    record = _normalize_server_usage(usage)
    return int(record["cached_tokens"] or 0)


def _extract_cache_write_tokens(usage):
    """读取本轮缓存**写入** token；未报告 → None（Task 7 · 缓存读写分列）。

    候选名与 cached 同构的三个来源：details 内嵌、OpenAI 兼容直读、
    Anthropic 形态（`_anthropic_usage_to_openai` 会把 cache_creation 单独透出）。
    刻意**不**进 `_normalize_server_usage` 的返回结构：那是既有调用方与测试的
    精确契约，而写入量只影响「写入轮不算未命中」这一列展示，不该改变记账结构。

    铁律：未报告 ≠ 0。非法值一律按未报告处理（返回 None），不猜、不夹断。
    """
    if usage is None:
        return None
    details = _usage_field(usage, "prompt_tokens_details")
    for raw in (
        _usage_field(details, "cache_creation_tokens"),
        _usage_field(usage, "cache_write_tokens"),
        _usage_field(usage, "cache_creation_input_tokens"),
    ):
        if raw is _USAGE_FIELD_MISSING or raw is None:
            continue
        errors = []
        value = _optional_nonnegative_int(raw, "cache_write_tokens", errors)
        if errors or value is None:
            logger.debug("忽略非法的缓存写入量字段（按未报告处理）")
            return None
        return value
    return None


def _persist_cache_request_diagnostic(request_details, usage):
    """补齐 usage 并持久化请求诊断；失败不影响回复或正式 token 记账。"""
    if not request_details:
        logger.warning("缓存请求诊断缺少请求形状，已跳过持久化")
        return
    if usage is None:
        usage_record = {
            "valid": False,
            "errors": ("usage missing",),
            "prompt_tokens": None,
            "cached_tokens": None,
            "cache_miss_tokens": None,
            "cache_reported": False,
        }
    else:
        usage_record = _normalize_server_usage(usage)
    diagnostic = dict(request_details)
    diagnostic.update({
        "prompt_tokens": usage_record["prompt_tokens"],
        "cached_tokens": usage_record["cached_tokens"],
        "cache_miss_tokens": usage_record["cache_miss_tokens"],
        "cache_reported": usage_record["cache_reported"],
        "usage_valid": usage_record["valid"],
        "usage_errors": tuple(usage_record["errors"]),
    })
    # Task 7（缓存读写分列）：写入量从原始 usage 单独提取，未报告 → None。
    diagnostic["cache_write_tokens"] = _extract_cache_write_tokens(usage)
    try:
        from core import app_context
        manager = app_context.get_manager()
        if manager is not None:
            manager.add_cache_request_diagnostic(diagnostic)
    except Exception as exc:
        logger.warning("缓存请求诊断持久化失败: %s", exc)


def _account_server_usage(
    usage,
    *,
    model_name,
    api_base,
    session_id,
    cache_scope,
    invalidation_reason,
):
    """校验并一次性记账最终 usage；无效记录绝不进入正式统计。"""
    record = _normalize_server_usage(usage)
    if not record["valid"]:
        reason = "usage 不一致: " + "; ".join(record["errors"])
        if str(cache_scope or "main") == "main":
            _mark_cache_usage_unavailable(
                session_id, model_name, api_base, cache_scope, reason
            )
        logger.warning(
            "拒绝 token 记账 model=%s api_base=%s session=%s scope=%s errors=%s raw=%r",
            model_name,
            api_base,
            session_id,
            cache_scope,
            record["errors"],
            usage,
        )
        return None

    prompt_tokens = record["prompt_tokens"]
    completion_tokens = record["completion_tokens"]
    total_tokens = record["total_tokens"]
    cached_tokens = record["cached_tokens"]
    cache_reported = record["cache_reported"]
    main_scope = str(cache_scope or "main") == "main"

    with token_usage_lock:
        session_token_usage["cumulative_total"] += total_tokens
        session_token_usage["cumulative_prompt"] += prompt_tokens
        session_token_usage["cumulative_completion"] += completion_tokens
        if cache_reported:
            session_token_usage["cumulative_cached"] += cached_tokens
            session_token_usage["cumulative_cache_prompt"] += prompt_tokens
        if main_scope:
            session_token_usage["latest_total"] = total_tokens
            session_token_usage["latest_prompt"] = prompt_tokens
            session_token_usage["latest_cached"] = cached_tokens or 0

    if main_scope:
        if cache_reported:
            _record_cache_usage(
                session_id,
                cache_scope,
                prompt_tokens,
                cached_tokens,
                invalidation_reason,
                model_name,
                api_base,
            )
        else:
            _mark_cache_usage_unavailable(
                session_id,
                model_name,
                api_base,
                cache_scope,
                "服务端未报告缓存 token 明细",
            )

    # llm-first 解析：core.llm 保留兼容入口，测试/外部调用方 patch
    # core.llm._persist_token_usage 必须生效；解析失败回落本模块实现。
    persist = None
    try:
        import core.llm as _llm
        persist = getattr(_llm, "_persist_token_usage", None)
    except Exception:
        persist = None
    if persist is None:
        persist = _persist_token_usage
    persist_kwargs = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": total_tokens,
        "session_id": session_id,
        "api_base": api_base,
        "cache_scope": cache_scope,
    }
    # 未报告写入量时不带该键：调用方（含测试替身）对 kwargs 做精确断言，
    # 恒定多一个 None 键同样会改变既有契约。未报告 → 落库 NULL，语义不变。
    cache_write_tokens = _extract_cache_write_tokens(usage)
    if cache_write_tokens is not None:
        persist_kwargs["cache_write_tokens"] = cache_write_tokens
    persist(model_name, **persist_kwargs)

    listener = _token_usage_listener
    if listener is not None:
        try:
            listener()
        except Exception:
            logger.debug("token_usage 监听器执行失败", exc_info=True)
    return record
