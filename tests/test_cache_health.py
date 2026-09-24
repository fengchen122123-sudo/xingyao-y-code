"""跨渠道缓存命中率修复 · Task 3 —— 车道健康派生层测试（严格 TDD）。

fixture 按 cache_request_diagnostics 的真实行形状构造，关键数字逐字取自
设计文档《待开发/跨渠道缓存命中率修复/设计规格与实施计划.md》§1.2/§1.3（脱敏）：

- 阿里云 token-plan 车道（openai/deepseek-v4.1-flash @ maas 兼容域）：
  upstream loss 真实形状 prev=254057 → cached=6144、prev=226647 → cached=0、
  prev=164945 → cached=0；
- 基元律动车道（openai/deepseek-flash @ tokenrhythm.studio）：
  cached / prev ≈ 100%（prompt=70000, prev=69500, cached=69500）；
- 本地 vLLM 车道（qwen38-27b @ http://127.0.0.1:8000/v1）：
  71/71 全 NULL（cache_reported=0、usage_valid=0）→ 必须判 unreported，不是 0%；
- 冷启动 / 上下文压缩或历史重写 行：都不入健康分母，后者计入 client_rewrite_count。

正文类字段（sha256、session 名、Workspace ID）均为脱敏假值。
"""

import time

import pytest

from core.cache_health import (
    MIN_HEALTH_SAMPLES,
    classify_lane_health,
    classify_lanes_health,
)
from core.memory import MemoryManager

ALIYUN_MODEL = "openai/deepseek-v4.1-flash"
ALIYUN_BASE = "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
TOKENRHYTHM_MODEL = "openai/deepseek-flash"
TOKENRHYTHM_BASE = "https://tokenrhythm.studio/v1"
VLLM_MODEL = "qwen38-27b"
VLLM_BASE = "http://127.0.0.1:8000/v1"

COLD_START = "冷启动"
HISTORY_REWRITE = "上下文压缩或历史重写"


def _ts(date_text, clock="10:00:00"):
    return int(time.mktime(time.strptime(f"{date_text} {clock}", "%Y-%m-%d %H:%M:%S")))


_UNSET = object()


def _diag_row(
    row_id,
    *,
    ts=1790208000,
    session_id="diag-session",
    model=ALIYUN_MODEL,
    api_base=ALIYUN_BASE,
    cache_scope="main",
    request_reason="",
    prompt_tokens=None,
    cached_tokens=None,
    cache_reported=True,
    usage_valid=True,
    cache_write_tokens=_UNSET,
    prev_prompt=_UNSET,
):
    """按 cache_request_diagnostics 全列真实形状构造一行（None 表示未报告）。

    cache_write_tokens 不传时该键缺失（老诊断行形状）；显式传 None 表示
    「列存在但未报告」，传数值表示该轮已报告写入量（Task 7 新增列）。
    prev_prompt 同理：不传时该键缺失（派生层退回窗口内滚动取上一行），传值表示
    「查询层已按 (session, lane) 算好上一行」（设计 §Task3 口径）。
    """
    row = {
        "id": row_id,
        "ts": ts,
        "session_id": session_id,
        "model": model,
        "api_base": api_base,
        "cache_scope": cache_scope,
        "request_reason": request_reason,
        "messages_sha256": "a" * 64,
        "system_sha256": "b" * 64,
        "tools_sha256": "c" * 64,
        "message_count": 12,
        "system_message_count": 1,
        "common_prefix_messages": 11,
        "request_bytes": 5000,
        "system_bytes": 900,
        "tools_bytes": 8000,
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "cache_miss_tokens": (
            max(prompt_tokens - cached_tokens, 0)
            if prompt_tokens is not None and cached_tokens is not None
            else None
        ),
        "cache_reported": cache_reported,
        "usage_valid": usage_valid,
        "usage_errors": [],
        "cache_control_markers": 3,
        "request_bytes_final": 9000,
    }
    if cache_write_tokens is not _UNSET:
        row["cache_write_tokens"] = cache_write_tokens
    if prev_prompt is not _UNSET:
        row["prev_prompt"] = prev_prompt
    return row


def test_single_upstream_loss_row_detected_with_correct_rate():
    """设计文档 §1.3 真实形状：prev=254057 → cached=6144 判一次 upstream loss。"""
    rows = [
        _diag_row(1, prompt_tokens=254057, cached_tokens=253000),
        _diag_row(2, prompt_tokens=255100, cached_tokens=6144),
        _diag_row(3, prompt_tokens=260000, cached_tokens=259500),
    ]

    health = classify_lane_health(rows)

    assert health.model == ALIYUN_MODEL
    assert health.api_base == ALIYUN_BASE
    assert health.cache_scope == "main"
    assert health.reported is True
    assert health.samples == 2
    assert health.hot_prompt == 255100 + 260000
    assert health.hot_cached == 6144 + 259500
    assert health.hot_hit_rate == pytest.approx(265644 / 515100)
    assert health.upstream_loss_rate == pytest.approx(0.5)
    assert health.consecutive_loss == 0
    assert health.client_rewrite_count == 0
    assert health.verdict == "insufficient"  # 样本 2 < 20：先按样本不足处理


def test_upstream_loss_threshold_uses_min_of_prev_and_prompt():
    """loss 条件 = cached < min(prev, prompt) * theta，且严格小于。"""
    base = [_diag_row(1, prompt_tokens=100000, cached_tokens=99000)]

    at_threshold = classify_lane_health(
        base + [_diag_row(2, prompt_tokens=200000, cached_tokens=50000)],
        theta=0.5,
    )
    below_threshold = classify_lane_health(
        base + [_diag_row(2, prompt_tokens=200000, cached_tokens=49999)],
        theta=0.5,
    )

    # min(prev=100000, prompt=200000) * 0.5 = 50000：恰等于不算 loss（严格小于）；
    # 若误用 prompt（200000 * 0.5 = 100000）则 50000 会被误判为 loss。
    assert at_threshold.samples == 1
    assert at_threshold.upstream_loss_rate == 0.0
    assert below_threshold.samples == 1
    assert below_threshold.upstream_loss_rate == 1.0


def test_fully_unreported_vllm_lane_is_never_reported_as_zero_hit_rate():
    """本地 vLLM 71/71 全 NULL：判 unreported，绝不当 0% 命中率展示。"""
    rows = [
        _diag_row(
            index,
            model=VLLM_MODEL,
            api_base=VLLM_BASE,
            session_id="vllm-session",
            prompt_tokens=None,
            cached_tokens=None,
            cache_reported=False,
            usage_valid=False,
        )
        for index in range(1, 72)
    ]

    health = classify_lane_health(rows)

    assert len(rows) == 71
    assert health.reported is False
    assert health.verdict == "unreported"
    assert health.samples == 0
    assert health.hot_prompt == 0
    assert health.hot_cached == 0
    assert health.hot_hit_rate == 0.0
    assert health.upstream_loss_rate == 0.0
    assert health.consecutive_loss == 0
    assert health.client_rewrite_count == 0


def test_cold_start_and_history_rewrite_rows_stay_out_of_health_denominator():
    rows = [
        _diag_row(1, prompt_tokens=100000, cached_tokens=99000),
        _diag_row(2, prompt_tokens=101000, cached_tokens=100000),
        _diag_row(
            3,
            prompt_tokens=90000,
            cached_tokens=0,
            request_reason=HISTORY_REWRITE,
        ),
        _diag_row(4, prompt_tokens=90500, cached_tokens=89500),
        _diag_row(5, prompt_tokens=5000, cached_tokens=0, request_reason=COLD_START),
        _diag_row(6, prompt_tokens=95000, cached_tokens=94000),
    ]

    health = classify_lane_health(rows)

    # 分母只有 3 条纯追加行；冷启动连换写计数都不进（只排除）。
    assert health.samples == 3
    assert health.client_rewrite_count == 1
    assert health.hot_prompt == 101000 + 90500 + 95000
    assert health.hot_cached == 100000 + 89500 + 94000
    assert health.upstream_loss_rate == 0.0
    assert health.consecutive_loss == 0
    assert health.verdict == "insufficient"


def test_two_consecutive_losses_is_upstream_lossy_but_single_loss_is_not():
    healthy_19 = [
        _diag_row(index, prompt_tokens=70000, cached_tokens=69500)
        for index in range(1, 20)
    ]
    two_losses = classify_lane_health(
        healthy_19
        + [
            _diag_row(20, prompt_tokens=70500, cached_tokens=0),
            _diag_row(21, prompt_tokens=71000, cached_tokens=0),
        ]
    )
    assert two_losses.samples == 20
    assert two_losses.consecutive_loss == 2
    assert two_losses.upstream_loss_rate == pytest.approx(0.1)
    assert two_losses.verdict == "upstream_lossy"

    healthy_21 = [
        _diag_row(index, prompt_tokens=70000, cached_tokens=69500)
        for index in range(1, 22)
    ]
    single_tail_loss = classify_lane_health(
        healthy_21 + [_diag_row(22, prompt_tokens=70500, cached_tokens=0)]
    )
    assert single_tail_loss.samples == 21
    assert single_tail_loss.consecutive_loss == 1
    assert single_tail_loss.upstream_loss_rate == pytest.approx(1 / 21)
    assert single_tail_loss.verdict == "healthy"
    assert single_tail_loss.verdict != "upstream_lossy"

    middle_loss = classify_lane_health(
        [
            _diag_row(index, prompt_tokens=70000, cached_tokens=69500)
            for index in range(1, 11)
        ]
        + [_diag_row(11, prompt_tokens=70500, cached_tokens=0)]
        + [
            _diag_row(index, prompt_tokens=71000, cached_tokens=70500)
            for index in range(12, 23)
        ]
    )
    assert middle_loss.samples == 21
    assert middle_loss.consecutive_loss == 0
    assert middle_loss.upstream_loss_rate == pytest.approx(1 / 21)
    assert middle_loss.verdict == "healthy"


def test_small_sample_is_insufficient_and_can_never_be_upstream_lossy():
    rows = [
        _diag_row(1, prompt_tokens=70000, cached_tokens=69500),
        _diag_row(2, prompt_tokens=70500, cached_tokens=0),
        _diag_row(3, prompt_tokens=71000, cached_tokens=0),
    ]

    health = classify_lane_health(rows)

    assert MIN_HEALTH_SAMPLES == 20
    assert health.samples == 2
    assert health.consecutive_loss == 2
    assert health.verdict == "insufficient"
    assert health.verdict != "upstream_lossy"


def test_healthy_tokenrhythm_lane_hits_approximately_full_rate():
    rows = [_diag_row(1, prompt_tokens=69500, cached_tokens=69500)] + [
        _diag_row(
            index,
            model=TOKENRHYTHM_MODEL,
            api_base=TOKENRHYTHM_BASE,
            prompt_tokens=70000,
            cached_tokens=69500,
        )
        for index in range(2, 22)
    ]

    health = classify_lane_health(rows)

    assert health.model == TOKENRHYTHM_MODEL
    assert health.reported is True
    assert health.samples == 20
    assert health.hot_prompt == 70000 * 20
    assert health.hot_cached == 69500 * 20
    assert health.hot_hit_rate == pytest.approx(69500 / 70000)
    assert health.hot_hit_rate > 0.99
    assert health.upstream_loss_rate == 0.0
    assert health.consecutive_loss == 0
    assert health.verdict == "healthy"


def test_lane_health_lists_write_turns_without_shrinking_denominator():
    """写入轮单列（Task 7 派生层补全）：write_turns / hot_write_tokens 只单列，
    不把写入轮从 samples 分母剔除（Task 3 的分母与 verdict 规则已验收）。"""
    rows = [
        _diag_row(1, prompt_tokens=10000, cached_tokens=9500, cache_write_tokens=None),
        _diag_row(2, prompt_tokens=10000, cached_tokens=9500, cache_write_tokens=800),
        _diag_row(3, prompt_tokens=10000, cached_tokens=0, cache_write_tokens=900),
    ]

    health = classify_lane_health(rows)

    # 首行无 prev 不入分母（write=None 也不计写入轮）；第 2/3 行都留在分母里。
    assert health.samples == 2
    assert health.write_turns == 2
    assert health.hot_write_tokens == 1700
    assert health.upstream_loss_rate == pytest.approx(0.5)


def test_lane_health_write_turns_zero_when_unreported():
    """两行都不带 cache_write_tokens 键：缺失 ≠ 0，写入轮计数与写入量保持 0。"""
    rows = [
        _diag_row(1, prompt_tokens=10000, cached_tokens=9500),
        _diag_row(2, prompt_tokens=10000, cached_tokens=9500),
    ]
    assert all("cache_write_tokens" not in row for row in rows)

    health = classify_lane_health(rows)

    assert health.samples == 1
    assert health.write_turns == 0
    assert health.hot_write_tokens == 0


def test_hot_hit_rate_is_token_weighted_not_per_turn_average():
    rows = [
        _diag_row(1, prompt_tokens=1000, cached_tokens=1000),
        _diag_row(2, prompt_tokens=1000, cached_tokens=1000),
        _diag_row(3, prompt_tokens=1000000, cached_tokens=0),
    ]

    health = classify_lane_health(rows)

    weighted = 1000 / (1000 + 1000000)
    per_turn_average = (1.0 + 0.0) / 2
    assert health.samples == 2
    assert health.hot_prompt == 1000 + 1000000
    assert health.hot_cached == 1000
    assert health.hot_hit_rate == pytest.approx(weighted)
    assert health.hot_hit_rate != pytest.approx(per_turn_average)


def test_row_after_unreported_row_is_excluded_from_denominator():
    rows = [
        _diag_row(1, prompt_tokens=50000, cached_tokens=49000),
        _diag_row(2, prompt_tokens=51000, cached_tokens=50000),
        _diag_row(
            3,
            prompt_tokens=None,
            cached_tokens=None,
            cache_reported=False,
            usage_valid=False,
        ),
        _diag_row(4, prompt_tokens=52000, cached_tokens=51000),
        _diag_row(5, prompt_tokens=53000, cached_tokens=52000),
    ]

    health = classify_lane_health(rows)

    # 行 4 的上一行（行 3）未报告 → 行 4 不可判定、不进分母；
    # 行 5 的上一行（行 4）已报告有值 → 行 5 进分母。
    assert health.samples == 2
    assert health.hot_prompt == 51000 + 53000
    assert health.hot_cached == 50000 + 52000
    assert health.upstream_loss_rate == 0.0


def test_window_keeps_only_the_most_recent_rows():
    early_losses = [
        _diag_row(index, prompt_tokens=50000 + index * 1000, cached_tokens=0)
        for index in range(1, 6)
    ]
    recent_healthy = [
        _diag_row(
            index,
            prompt_tokens=55000 + index * 1000,
            cached_tokens=54000 + index * 1000,
        )
        for index in range(6, 206)
    ]
    rows = early_losses + recent_healthy

    recent = classify_lane_health(rows, window=200)
    assert recent.samples == 199  # 窗口首行没有窗口内上一行 → 不可判定
    assert recent.upstream_loss_rate == 0.0
    assert recent.consecutive_loss == 0

    full = classify_lane_health(rows, window=len(rows))
    assert full.samples == 204
    assert full.upstream_loss_rate == pytest.approx(4 / 204)


def test_consecutive_loss_skips_undecidable_rows_without_breaking():
    rows = [
        _diag_row(1, prompt_tokens=70000, cached_tokens=69500),
        _diag_row(2, prompt_tokens=70500, cached_tokens=0),
        _diag_row(
            3,
            prompt_tokens=None,
            cached_tokens=None,
            cache_reported=False,
            usage_valid=False,
        ),
        _diag_row(4, prompt_tokens=71000, cached_tokens=0),
        _diag_row(5, prompt_tokens=71500, cached_tokens=0),
    ]

    health = classify_lane_health(rows)
    # 行 2 是 loss；行 3/4 不可判定（不打断也不计数）；行 5 是 loss → 末尾连续 2。
    assert health.consecutive_loss == 2

    broken = classify_lane_health(
        [
            _diag_row(1, prompt_tokens=70000, cached_tokens=69500),
            _diag_row(2, prompt_tokens=70500, cached_tokens=0),
            _diag_row(3, prompt_tokens=71000, cached_tokens=0),
            _diag_row(4, prompt_tokens=71500, cached_tokens=71000),
            _diag_row(5, prompt_tokens=72000, cached_tokens=0),
        ]
    )
    # 末尾 loss 之后遇到行 4（可判定、未丢前缀）即停止计数。
    assert broken.consecutive_loss == 1


def test_classify_lanes_health_sorts_by_model_api_base_scope():
    lanes = {
        (VLLM_MODEL, VLLM_BASE, "main"): [
            _diag_row(
                1,
                model=VLLM_MODEL,
                api_base=VLLM_BASE,
                prompt_tokens=None,
                cached_tokens=None,
                cache_reported=False,
                usage_valid=False,
            ),
        ],
        (ALIYUN_MODEL, ALIYUN_BASE, "main"): [
            _diag_row(1, prompt_tokens=254057, cached_tokens=253000),
            _diag_row(2, prompt_tokens=255100, cached_tokens=6144),
        ],
        (TOKENRHYTHM_MODEL, TOKENRHYTHM_BASE, "main"): [
            _diag_row(
                1,
                model=TOKENRHYTHM_MODEL,
                api_base=TOKENRHYTHM_BASE,
                prompt_tokens=69500,
                cached_tokens=69500,
            ),
            _diag_row(
                2,
                model=TOKENRHYTHM_MODEL,
                api_base=TOKENRHYTHM_BASE,
                prompt_tokens=70000,
                cached_tokens=69500,
            ),
        ],
    }

    healths = classify_lanes_health(lanes)

    assert [h.model for h in healths] == [
        TOKENRHYTHM_MODEL,
        ALIYUN_MODEL,
        VLLM_MODEL,
    ]
    assert healths[0].verdict == "insufficient"
    assert healths[1].upstream_loss_rate == 1.0
    assert healths[2].verdict == "unreported"
    assert classify_lanes_health({}) == []


def _store_diagnostic(manager, row):
    payload = dict(row)
    payload.pop("id", None)
    manager.add_cache_request_diagnostic(payload)


# ---------------------------------------------------------------------------
# 显式 prev_prompt（设计 §Task3：prev 由查询层按 session 分链算好，不是窗口内滚动）
# ---------------------------------------------------------------------------

def test_explicit_prev_prompt_makes_window_head_decidable():
    """带 prev_prompt 的行即使是窗口首行也可判定：不再「宁可少一个样本」。"""
    rows = [
        _diag_row(1, prompt_tokens=254057, cached_tokens=253000, prev_prompt=250000),
    ]

    health = classify_lane_health(rows)

    assert health.samples == 1
    assert health.hot_prompt == 254057
    assert health.hot_cached == 253000
    assert health.upstream_loss_rate == 0.0


def test_explicit_prev_prompt_detects_loss_on_the_first_row_of_the_window():
    """真实形状 prev=254057 → cached=6144：首行也能判出整段丢前缀。"""
    health = classify_lane_health([
        _diag_row(1, prompt_tokens=255100, cached_tokens=6144, prev_prompt=254057),
    ])

    assert health.samples == 1
    assert health.upstream_loss_rate == pytest.approx(1.0)


def test_explicit_none_prev_still_excludes_the_row():
    """prev_prompt 显式为 None（该会话首行 / 上一行未报告）→ 仍不可判定、不入分母。"""
    health = classify_lane_health([
        _diag_row(1, prompt_tokens=100000, cached_tokens=99000, prev_prompt=None),
    ])

    assert health.samples == 0
    assert health.hot_prompt == 0


def test_prev_prompt_also_unblocks_write_turn_counting():
    """首行不再被整体剔除后，它的写入量也要计入单列。"""
    health = classify_lane_health([
        _diag_row(1, prompt_tokens=100000, cached_tokens=90000,
                  cache_write_tokens=5000, prev_prompt=95000),
    ])

    assert health.samples == 1
    assert health.write_turns == 1
    assert health.hot_write_tokens == 5000


def test_query_lane_cache_health_prev_prompt_follows_session_chain(tmp_path):
    """查询层必须把 prev 算好：时间/窗口过滤的第一行照样可判定，且不跨会话借 prev。"""
    manager = MemoryManager(tmp_path / "prev.db")
    base_ts = _ts("2026-09-24", "10:00:00")
    for offset, (prompt, cached) in enumerate(((100000, 99000), (101000, 100000))):
        _store_diagnostic(
            manager,
            _diag_row(
                0,
                ts=base_ts + offset * 60,
                session_id="prev-s1",
                prompt_tokens=prompt,
                cached_tokens=cached,
            ),
        )

    # 只要第二行：它的 prev 来自同会话第一行（在时间范围之外）→ 仍应可判定
    healths = manager.query_lane_cache_health(start_ts=base_ts + 60)
    assert len(healths) == 1
    assert healths[0].samples == 1, "时间范围首行必须拿到会话内上一行"
    assert healths[0].hot_prompt == 101000

    # 不得跨会话借 prev：另一会话的首行没有本会话上一行 → 不可判定
    other = MemoryManager(tmp_path / "prev2.db")
    _store_diagnostic(
        other,
        _diag_row(0, ts=base_ts, session_id="prev-a", prompt_tokens=254057, cached_tokens=253000),
    )
    _store_diagnostic(
        other,
        _diag_row(0, ts=base_ts + 60, session_id="prev-b", prompt_tokens=100000, cached_tokens=0),
    )
    lane = other.query_lane_cache_health()
    assert len(lane) == 1
    assert lane[0].samples == 0, "prev 只认同 (session, model, api_base, scope) 的上一行"
    assert lane[0].verdict == "insufficient"


def test_query_lane_cache_health_groups_lanes_and_filters_by_lane(tmp_path):
    manager = MemoryManager(tmp_path / "x.db")
    base_ts = _ts("2026-09-24", "10:00:00")
    for offset, (prompt, cached) in enumerate(((254057, 253000), (255100, 6144))):
        _store_diagnostic(
            manager,
            _diag_row(0, ts=base_ts + offset, prompt_tokens=prompt, cached_tokens=cached),
        )
    for offset, (prompt, cached) in enumerate(((69500, 69500), (70000, 69500))):
        _store_diagnostic(
            manager,
            _diag_row(
                0,
                ts=base_ts + 100 + offset,
                model=TOKENRHYTHM_MODEL,
                api_base=TOKENRHYTHM_BASE,
                prompt_tokens=prompt,
                cached_tokens=cached,
            ),
        )

    healths = manager.query_lane_cache_health()

    assert [(h.model, h.api_base, h.cache_scope) for h in healths] == [
        (TOKENRHYTHM_MODEL, TOKENRHYTHM_BASE, "main"),
        (ALIYUN_MODEL, ALIYUN_BASE, "main"),
    ]
    aliyun = next(h for h in healths if h.model == ALIYUN_MODEL)
    assert aliyun.samples == 1
    assert aliyun.upstream_loss_rate == 1.0

    filtered = manager.query_lane_cache_health(lane=(ALIYUN_MODEL, ALIYUN_BASE, "main"))
    assert len(filtered) == 1
    assert filtered[0].model == ALIYUN_MODEL
    assert filtered[0].upstream_loss_rate == 1.0


def test_query_lane_cache_health_filters_by_time_window(tmp_path):
    manager = MemoryManager(tmp_path / "x.db")
    base_ts = _ts("2026-09-24", "10:00:00")
    for offset, (prompt, cached) in enumerate(
        ((254057, 253000), (255100, 6144), (260000, 259500))
    ):
        _store_diagnostic(
            manager,
            _diag_row(
                0,
                ts=base_ts + offset * 60,
                prompt_tokens=prompt,
                cached_tokens=cached,
            ),
        )

    early = manager.query_lane_cache_health(end_ts=base_ts + 60)
    assert early[0].samples == 1
    assert early[0].upstream_loss_rate == 1.0

    # 新口径（设计 §Task3）：prev 由查询层在**过滤前的全表**上按会话分链算好，
    # 时间范围只决定「取哪些行做样本」，不切断 prev 链 → 区间首行不再白丢一个样本。
    late = manager.query_lane_cache_health(start_ts=base_ts + 120)
    assert late[0].samples == 1
    assert late[0].hot_prompt == 260000
    # 上一行 prev=255100、本轮 cached=259500 → 不算丢前缀
    assert late[0].upstream_loss_rate == 0.0
    assert late[0].reported is True
    assert late[0].verdict == "insufficient"  # 仅 1 轮，低于 MIN_HEALTH_SAMPLES=20

    # 区间首行仍拿不到 prev 的情形：它本身就是该会话首行
    only_first = manager.query_lane_cache_health(
        start_ts=base_ts, end_ts=base_ts
    )
    assert only_first[0].samples == 0
    assert only_first[0].hot_prompt == 0


def test_query_lane_cache_health_preserves_null_versus_real_zero(tmp_path):
    manager = MemoryManager(tmp_path / "x.db")
    base_ts = _ts("2026-09-24", "10:00:00")
    _store_diagnostic(
        manager,
        _diag_row(
            0,
            ts=base_ts,
            model=VLLM_MODEL,
            api_base=VLLM_BASE,
            prompt_tokens=29000,
            cached_tokens=28500,
        ),
    )
    _store_diagnostic(
        manager,
        _diag_row(
            0,
            ts=base_ts + 1,
            model=VLLM_MODEL,
            api_base=VLLM_BASE,
            prompt_tokens=30000,
            cached_tokens=0,
        ),
    )
    _store_diagnostic(
        manager,
        _diag_row(
            0,
            ts=base_ts + 2,
            model=VLLM_MODEL,
            api_base=VLLM_BASE,
            prompt_tokens=None,
            cached_tokens=None,
            cache_reported=False,
            usage_valid=False,
        ),
    )

    queried = manager.query_cache_request_diagnostics()
    assert queried[1]["cached_tokens"] == 0
    assert queried[2]["cached_tokens"] is None

    health = manager.query_lane_cache_health()[0]
    # 真实 0 进分母（0 < 29000 * 0.6 → loss）；NULL 不进分母。
    assert health.samples == 1
    assert health.hot_prompt == 30000
    assert health.hot_cached == 0
    assert health.upstream_loss_rate == 1.0


def test_module_docstring_pins_upstream_loss_attribution_wording():
    import core.cache_health as cache_health

    doc = cache_health.__doc__ or ""
    assert "客户端字节零改动" in doc
    assert "纯追加" in doc
    assert "上游未命中" in doc
    assert "不构成" in doc
    for banned in ("已证明的上游驱逐", "已证明的上游淘汰", "上游驱逐已被证明"):
        assert banned not in doc