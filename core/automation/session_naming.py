from __future__ import annotations

from datetime import datetime

from core.automation.models import MAX_SESSION_CHARS

SESSION_DATE_FORMAT = "%Y-%m-%d"


def build_run_session_name(task_name: str, now: datetime) -> str:
    date_part = f"{now:{SESSION_DATE_FORMAT}}"
    name = str(task_name or "").strip()
    if not name:
        return date_part
    return f"{date_part} {name}"[:MAX_SESSION_CHARS]