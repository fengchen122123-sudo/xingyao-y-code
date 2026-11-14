"""用量水位。一次事务里计算增量；提交前崩溃则下次重算，不记两遍。"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def shanghai_day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, SHANGHAI).strftime("%Y-%m-%d")


def apply_usage(database, account_id: str, metrics: dict, now: float, *, fail_before_commit: bool = False) -> None:
    """metrics.token_usage 是累计值。total_tokens 只留在水位里。

    worker 只有一个 turns_total。它记在每个模型的水位上，用来发现回退；
    日表的 turns 只加在第一条模型上，避免按模型乘倍。
    """
    day = shanghai_day(now)
    turns_total = int(metrics.get("turns_total") or 0)
    rows = list(metrics.get("token_usage") or [])
    if not rows:
        rows = [{"model": "", "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}]
    try:
        for index, current in enumerate(rows):
            model = str(current.get("model") or "")
            watermark = database.execute(
                """
                SELECT prompt_tokens, completion_tokens, total_tokens, turns_total
                FROM usage_watermarks WHERE account_id = ? AND model = ?
                """,
                (account_id, model),
            ).fetchone()
            previous_prompt = int(watermark["prompt_tokens"]) if watermark else 0
            previous_completion = int(watermark["completion_tokens"]) if watermark else 0
            previous_total = int(watermark["total_tokens"]) if watermark else 0
            previous_turns = int(watermark["turns_total"]) if watermark else 0
            prompt = int(current.get("prompt_tokens") or 0)
            completion = int(current.get("completion_tokens") or 0)
            total = int(current.get("total_tokens") or 0)
            deltas = {
                "prompt": prompt - previous_prompt,
                "completion": completion - previous_completion,
                "total": total - previous_total,
                "turns": turns_total - previous_turns,
            }
            if any(value < 0 for value in deltas.values()):
                logger.warning(
                    "用量水位回退，按累计值重新入账 account=%s model=%s",
                    account_id,
                    model,
                )
                deltas = {
                    "prompt": prompt,
                    "completion": completion,
                    "total": total,
                    "turns": turns_total,
                }
            daily_turns = deltas["turns"] if index == 0 else 0
            database.execute(
                """
                INSERT INTO usage_daily(account_id, day, model, input_tokens, output_tokens, turns)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, day, model) DO UPDATE SET
                  input_tokens = input_tokens + excluded.input_tokens,
                  output_tokens = output_tokens + excluded.output_tokens,
                  turns = turns + excluded.turns
                """,
                (account_id, day, model, deltas["prompt"], deltas["completion"], daily_turns),
            )
            database.execute(
                """
                INSERT INTO usage_watermarks(
                  account_id, model, prompt_tokens, completion_tokens, total_tokens, turns_total, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, model) DO UPDATE SET
                  prompt_tokens = excluded.prompt_tokens,
                  completion_tokens = excluded.completion_tokens,
                  total_tokens = excluded.total_tokens,
                  turns_total = excluded.turns_total,
                  updated_at = excluded.updated_at
                """,
                (account_id, model, prompt, completion, total, turns_total, int(now)),
            )
        if fail_before_commit:
            raise RuntimeError("用量事务在提交前失败")
        database.commit()
    except Exception:
        database.rollback()
        raise
