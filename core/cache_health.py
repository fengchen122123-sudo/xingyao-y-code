"""车道级缓存健康派生层（跨渠道缓存命中率修复 · Task 3）。

纯函数派生：输入 cache_request_diagnostics 的行（dict，允许 None），输出 LaneHealth
汇总——把「渠道天花板」变成可读数字。本模块只读、零请求风险，不碰任何请求路径。

lane = (model, api_base, cache_scope) 三元组，与 core.llm_diagnostics._cache_usage_key
的分桶键同构（只差 session_id）。窗口内所有命中率均为 token 加权
sum(cached_tokens) / sum(prompt_tokens)，不是逐轮命中率的算术平均。

归因表述铁律：upstream_loss_rate 与 verdict='upstream_lossy' 只允许表述为
「客户端字节零改动（纯追加）但上游未命中」；它不构成对上游缓存驱逐/淘汰机制的
证明——本层可观测的字段无法区分「上游缓存被驱逐」与「上游从未建立缓存」，
任何超出「客户端 vs 上游」二分的因果断言都是越界。

未报告 ≠ 0：cache_reported=0 / usage_valid=0 的行绝不入健康分母、绝不按
命中率 0 记账（窗口内全为此类 → reported=False、verdict='unreported'）。
无法确定 prev_prompt 的行宁可少一个样本，也不把未知当 0。

写入轮单列（Task 7 遗留的派生层补全）：可判定轮次中 cache_write_tokens 有值
（非 None 且 > 0）的单独计入 write_turns / hot_write_tokens。**本期只单列，
不把写入轮从 samples 分母剔除**——「写入轮 ≠ 未命中」与「分母口径」是两件事，
Task 3 的分母与 verdict 规则已验收，动分母须另立验收。缺失 / None / 零 / 负值 /
非法值一律按未报告处理（不冒充 0）。
"""

from dataclasses import dataclass

DEFAULT_LOSS_THETA = 0.6
MIN_HEALTH_SAMPLES = 20

COLD_START_REASON = "冷启动"


@dataclass(frozen=True)
class LaneHealth:
    """一条车道的窗口健康汇总（字段语义见模块 docstring 与判定规则）。"""

    model: str
    api_base: str
    cache_scope: str
    reported: bool
    samples: int
    hot_prompt: int
    hot_cached: int
    hot_hit_rate: float
    upstream_loss_rate: float
    consecutive_loss: int
    client_rewrite_count: int
    write_turns: int  # 可判定行中 cache_write_tokens 非 None 且 >0 的轮次数
    hot_write_tokens: int  # 上述写入轮的写入量之和（未报告/非正数一律不计）
    verdict: str  # healthy | unreported | upstream_lossy | client_rewrite | insufficient


def _is_reported_row(row):
    """行是否「已报告且有效」：cache_reported 与 usage_valid 均为真。

    未报告/无效的轮次连入分母的资格都没有，也绝不记 0（铁律 3）。
    """
    return bool(row.get("cache_reported")) and bool(row.get("usage_valid"))


def _row_write_tokens(row):
    """行上已报告的写入量（cache_write_tokens 非 None 且 > 0）；否则返回 None。

    与 cached_tokens 一样坚持 NULL 语义：缺失 / None / 零 / 负值 / 非法值
    一律视为「未报告」，绝不冒充 0，也不把非正数计成写入轮（Task 7 铁律 4）。
    """
    value = row.get("cache_write_tokens")
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def classify_lane_health(rows, *, window=200, theta=DEFAULT_LOSS_THETA) -> LaneHealth:
    """把同一 lane 的窗口内诊断行归类为一条 LaneHealth。

    rows：同一 lane 内按 id 升序的 cache_request_diagnostics 行（dict，可含 None）。
    window：只取最近 N 行（不足则全取）；prev_prompt 链在窗口内滚动，窗口首行
        没有「上一行」→ 不可判定（不回溯窗口外，保证窗口外的行不影响结果）。
    theta：upstream loss 阈值——cached < min(prev_prompt, prompt_tokens) * theta。

    判定顺序即语义：unreported → client_rewrite → insufficient → upstream_lossy
    → healthy；样本不足时绝不判 upstream_lossy（Task 5 保险丝的前提）。
    """
    ordered = list(rows or ())
    window_rows = ordered[-window:] if window is not None and window > 0 else list(ordered)

    # 窗口内至少出现一条「已报告且有效」的行才算 reported；全部未报告/无效
    # （含空窗口）→ reported=False、verdict='unreported'，token 计数保持 0。
    reported = any(_is_reported_row(row) for row in window_rows)

    samples = 0
    hot_prompt = 0
    hot_cached = 0
    write_turns = 0
    hot_write_tokens = 0
    loss_count = 0
    client_rewrite_count = 0
    states = []
    prev_prompt = None

    for row in window_rows:
        reason = str(row.get("request_reason") or "")
        prompt_tokens = row.get("prompt_tokens")
        cached_tokens = row.get("cached_tokens")
        row_reported = _is_reported_row(row)

        if reason == COLD_START_REASON:
            # 冷启动：不入健康分母，也不计入换写观察（只排除）。
            states.append("skip")
        elif reason:
            # 客户端换代 / 历史重写：单独计数，不入健康分母（未报告行不计数）。
            if row_reported:
                client_rewrite_count += 1
            states.append("skip")
        elif not row_reported:
            # 未报告 / 无效：绝不入分母、绝不记 0。
            states.append("skip")
        elif prompt_tokens is None or cached_tokens is None or prev_prompt is None:
            # 无法判定就不猜：宁可少一个样本，也不把未知当 0。
            states.append("skip")
        else:
            prompt_value = int(prompt_tokens)
            cached_value = int(cached_tokens)
            samples += 1
            hot_prompt += prompt_value
            hot_cached += cached_value
            write_value = _row_write_tokens(row)
            if write_value is not None:
                write_turns += 1
                hot_write_tokens += write_value
            if cached_value < min(prev_prompt, prompt_value) * theta:
                loss_count += 1
                states.append("loss")
            else:
                states.append("ok")

        # 只有「已报告且有效、值非 NULL」的上一行才能把 prompt 传给下一行做 prev。
        prev_prompt = (
            int(prompt_tokens) if row_reported and prompt_tokens is not None else None
        )

    # 窗口末尾连续丢前缀：可判定且未丢前缀的行即停止；不可判定行不打断也不计数。
    consecutive_loss = 0
    for state in reversed(states):
        if state == "loss":
            consecutive_loss += 1
        elif state == "ok":
            break

    hot_hit_rate = (hot_cached / hot_prompt) if hot_prompt > 0 else 0.0
    upstream_loss_rate = (loss_count / samples) if samples > 0 else 0.0

    if not reported:
        verdict = "unreported"
    elif samples == 0 and client_rewrite_count > 0:
        verdict = "client_rewrite"
    elif samples < MIN_HEALTH_SAMPLES:
        verdict = "insufficient"
    elif consecutive_loss >= 2:
        verdict = "upstream_lossy"
    else:
        verdict = "healthy"

    anchor = window_rows[-1] if window_rows else {}
    return LaneHealth(
        model=str(anchor.get("model") or "").lower(),
        api_base=str(anchor.get("api_base") or "").rstrip("/").lower(),
        cache_scope=str(anchor.get("cache_scope") or "main"),
        reported=reported,
        samples=samples,
        hot_prompt=hot_prompt,
        hot_cached=hot_cached,
        hot_hit_rate=hot_hit_rate,
        upstream_loss_rate=upstream_loss_rate,
        consecutive_loss=consecutive_loss,
        client_rewrite_count=client_rewrite_count,
        write_turns=write_turns,
        hot_write_tokens=hot_write_tokens,
        verdict=verdict,
    )


def classify_lanes_health(rows_by_lane, *, window=200, theta=DEFAULT_LOSS_THETA) -> list[LaneHealth]:
    """按 lane 分组批量派生（返回顺序按 (model, api_base, cache_scope) 稳定排序）。

    rows_by_lane：{lane_key: [行, ...]} 映射；每组行须按 id 升序（身份以行内的
    model / api_base / cache_scope 为准，lane_key 只作分组标识）。
    """
    if not rows_by_lane:
        return []
    groups = (
        list(rows_by_lane.values()) if isinstance(rows_by_lane, dict) else list(rows_by_lane)
    )
    healths = [
        classify_lane_health(group, window=window, theta=theta) for group in groups
    ]
    return sorted(healths, key=lambda health: (health.model, health.api_base, health.cache_scope))