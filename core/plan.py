"""Structured, in-memory execution plans for one active session."""

from __future__ import annotations

from copy import deepcopy


VALID_STATUSES = frozenset({"pending", "in_progress", "completed", "failed", "cancelled"})


class PlanManager:
    def __init__(self) -> None:
        self._items: list[dict[str, str]] = []

    def replace(self, items: list[dict[str, str]]) -> None:
        normalized = []
        for item in items:
            item_id = str(item.get("id", "")).strip()
            subject = str(item.get("subject", "")).strip()
            status = str(item.get("status", "pending"))
            if not item_id or not subject:
                raise ValueError("plan items require id and subject")
            if status not in VALID_STATUSES:
                raise ValueError(f"invalid plan status: {status}")
            normalized.append({"id": item_id, "subject": subject, "status": status})
        self._items = normalized

    def update(self, item_id: str, status: str | None = None, subject: str | None = None) -> bool:
        if status is not None and status not in VALID_STATUSES:
            return False
        for item in self._items:
            if item["id"] != item_id:
                continue
            if status is not None:
                item["status"] = status
            if subject is not None and subject.strip():
                item["subject"] = subject.strip()
            return True
        return False

    def snapshot(self) -> list[dict[str, str]]:
        return deepcopy(self._items)

    def in_progress(self) -> dict[str, str] | None:
        for item in self._items:
            if item["status"] == "in_progress":
                return deepcopy(item)
        return None

    def clear(self) -> None:
        self._items.clear()


_DEFAULT_PLAN_MANAGER = PlanManager()


def get_default_plan_manager() -> PlanManager:
    return _DEFAULT_PLAN_MANAGER


def plan_write(items: list[dict[str, str]]) -> str:
    _DEFAULT_PLAN_MANAGER.replace(items)
    return "计划已更新。"


def plan_update(item_id: str, status: str | None = None, subject: str | None = None) -> str:
    if not _DEFAULT_PLAN_MANAGER.update(item_id, status=status, subject=subject):
        return f"无法更新计划项: {item_id}"
    return "计划项已更新。"
