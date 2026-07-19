"""Persisted dependency-aware execution plans for one active session."""

from __future__ import annotations

import json
import os
import threading
import warnings
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable


VALID_STATUSES = frozenset({
    "pending",
    "in_progress",
    "completed",
    "failed",
    "blocked",
    "cancelled",
})
VALID_STAGES = frozenset({"plan", "build", "review"})
VALID_KINDS = frozenset({"execution", "input_preparation"})
VALID_VERIFICATION_TYPES = frozenset({"result", "command"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "blocked", "cancelled"})
PlanEventCallback = Callable[[str, dict[str, Any]], None]


def _starts_with_cjk(text: str) -> bool:
    first = str(text or "").lstrip()[:1]
    return bool(first and "\u3400" <= first <= "\u9fff")


class PlanManager:
    def __init__(
        self,
        *,
        storage_dir: str | os.PathLike[str] | None = None,
        session_id: str | None = None,
        event_callback: PlanEventCallback | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._items: list[dict[str, Any]] = []
        self._event_callback = event_callback
        self._session_id = str(session_id).strip() if session_id else None
        self._storage_dir = Path(storage_dir or Path.cwd() / ".ycode_tasks" / "plans")
        if self._session_id:
            self.load()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def bind_session(self, session_id: str | None, *, load: bool = True) -> None:
        with self._lock:
            self._session_id = str(session_id).strip() if session_id else None
            self._items = []
            if load and self._session_id:
                self._load_locked()

    def replace(self, items: list[dict[str, Any]]) -> None:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in items:
            item = self._normalize_item(raw)
            if item["id"] in seen:
                raise ValueError(f"duplicate plan item id: {item['id']}")
            seen.add(item["id"])
            normalized.append(item)
        self._validate_graph(normalized)
        with self._lock:
            self._items = normalized
            self._commit_locked()

    def update(
        self,
        item_id: str,
        status: str | None = None,
        subject: str | None = None,
        *,
        depends_on: list[str] | None = None,
        owner: str | None = None,
        files: list[str] | None = None,
        verification: str | None = None,
        verification_type: str | None = None,
        stage: str | None = None,
        kind: str | None = None,
        expected_status: str | set[str] | frozenset[str] | None = None,
        result: str | None = None,
        error: str | None = None,
        attempts: int | None = None,
    ) -> bool:
        with self._lock:
            item = next((item for item in self._items if item["id"] == item_id), None)
            if item is None:
                return False
            if expected_status is not None:
                expected = (
                    {expected_status}
                    if isinstance(expected_status, str)
                    else set(expected_status)
                )
                if item["status"] not in expected:
                    return False
            # 先完成全部校验，再一次应用变更，避免部分生效后返回 False 造成内存/磁盘状态分裂
            if attempts is not None and int(attempts) < 0:
                return False
            if status is not None and status not in VALID_STATUSES:
                return False
            if status in {"in_progress", "completed"} and not self._dependencies_completed_locked(item):
                return False
            normalized_stage = None
            if stage is not None:
                normalized_stage = str(stage).strip().lower()
                if normalized_stage not in VALID_STAGES:
                    return False
            normalized_verification_type = None
            if verification_type is not None:
                normalized_verification_type = str(verification_type).strip().lower()
                if normalized_verification_type not in VALID_VERIFICATION_TYPES:
                    return False
            normalized_kind = None
            if kind is not None:
                normalized_kind = str(kind).strip().lower()
                if normalized_kind not in VALID_KINDS:
                    return False
            normalized_dependencies = None
            if depends_on is not None:
                normalized_dependencies = list(dict.fromkeys(
                    str(value).strip() for value in depends_on if str(value).strip()
                ))
                candidate_items = deepcopy(self._items)
                candidate = next(candidate for candidate in candidate_items if candidate["id"] == item_id)
                candidate["depends_on"] = normalized_dependencies
                try:
                    self._validate_graph(candidate_items)
                except ValueError:
                    return False
            if status is not None:
                item["status"] = status
            if subject is not None and subject.strip():
                item["subject"] = subject.strip()
            if normalized_dependencies is not None:
                item["depends_on"] = normalized_dependencies
            if owner is not None:
                item["owner"] = str(owner).strip()
            if files is not None:
                item["files"] = [str(value).strip() for value in files if str(value).strip()]
            if verification is not None:
                item["verification"] = str(verification).strip()
            if normalized_verification_type is not None:
                item["verification_type"] = normalized_verification_type
            if normalized_stage is not None:
                item["stage"] = normalized_stage
            if normalized_kind is not None:
                item["kind"] = normalized_kind
            if result is not None:
                item["result"] = str(result)
            if error is not None:
                item["error"] = str(error)
            if attempts is not None:
                item["attempts"] = int(attempts)
            self._commit_locked()
            return True

    def mark_started(self, item_id: str, *, owner: str | None = None) -> bool:
        return self.update(item_id, status="in_progress", owner=owner)

    def mark_completed(self, item_id: str, result: str = "") -> bool:
        return self.update(
            item_id,
            status="completed",
            result=result,
            error="",
            expected_status="in_progress",
        )

    def mark_failed(self, item_id: str, error: str, result: str = "") -> bool:
        return self.update(
            item_id,
            status="failed",
            error=error,
            result=result,
            expected_status="in_progress",
        )

    def mark_cancelled(self, item_id: str, error: str = "任务已取消") -> bool:
        return self.update(
            item_id,
            status="cancelled",
            error=error,
            expected_status={"pending", "in_progress", "blocked"},
        )

    def ready_items(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy([
                item
                for item in self._items
                if item["status"] == "pending"
                and self._dependencies_completed_locked(item)
            ])

    def blocked_items(self) -> list[dict[str, Any]]:
        with self._lock:
            statuses = {item["id"]: item["status"] for item in self._items}
            return deepcopy([
                item
                for item in self._items
                if item["status"] == "pending"
                and any(statuses.get(dep) in {"failed", "blocked", "cancelled"} for dep in item["depends_on"])
            ])

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._items)

    def in_progress(self) -> dict[str, Any] | None:
        with self._lock:
            for item in self._items:
                if item["status"] == "in_progress":
                    return deepcopy(item)
        return None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._commit_locked()

    def load(self) -> None:
        with self._lock:
            self._load_locked()

    def storage_path(self) -> Path | None:
        if not self._session_id:
            return None
        return self._storage_dir / f"{self._session_id}.json"

    def _normalize_item(self, raw: dict[str, Any]) -> dict[str, Any]:
        item_id = str(raw.get("id", "")).strip()
        subject = str(raw.get("subject", "")).strip()
        status = str(raw.get("status", "pending")).strip()
        if not item_id or not subject:
            raise ValueError("plan items require id and subject")
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid plan status: {status}")
        stage = str(raw.get("stage", "build")).strip().lower() or "build"
        if stage not in VALID_STAGES:
            raise ValueError(f"invalid plan stage: {stage}")
        verification_type = str(raw.get("verification_type", "result")).strip().lower() or "result"
        if verification_type not in VALID_VERIFICATION_TYPES:
            raise ValueError(f"invalid verification type: {verification_type}")
        kind = str(raw.get("kind", "execution")).strip().lower() or "execution"
        if kind not in VALID_KINDS:
            raise ValueError(f"invalid plan kind: {kind}")
        depends_on = [str(value).strip() for value in raw.get("depends_on", []) if str(value).strip()]
        if item_id in depends_on:
            raise ValueError(f"plan item cycle: {item_id}")
        return {
            "id": item_id,
            "subject": subject,
            "status": status,
            "depends_on": list(dict.fromkeys(depends_on)),
            "owner": str(raw.get("owner", "")).strip(),
            "files": [str(value).strip() for value in raw.get("files", []) if str(value).strip()],
            "verification": str(raw.get("verification", "")).strip(),
            "verification_type": verification_type,
            "stage": stage,
            "kind": kind,
            "attempts": max(0, int(raw.get("attempts", 0) or 0)),
            "result": str(raw.get("result", "")),
            "error": str(raw.get("error", "")),
        }

    @staticmethod
    def _validate_graph(items: list[dict[str, Any]]) -> None:
        ids = {item["id"] for item in items}
        for item in items:
            for dependency in item["depends_on"]:
                if dependency not in ids:
                    raise ValueError(f"unknown dependency: {dependency}")

        graph = {item["id"]: item["depends_on"] for item in items}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(item_id: str) -> None:
            if item_id in visiting:
                raise ValueError(f"plan dependency cycle involving: {item_id}")
            if item_id in visited:
                return
            visiting.add(item_id)
            for dependency in graph[item_id]:
                visit(dependency)
            visiting.remove(item_id)
            visited.add(item_id)

        for item_id in graph:
            visit(item_id)

    def _dependencies_completed_locked(self, item: dict[str, Any]) -> bool:
        status_by_id = {candidate["id"]: candidate["status"] for candidate in self._items}
        return all(status_by_id.get(dependency) == "completed" for dependency in item["depends_on"])

    def _commit_locked(self) -> None:
        self._persist_locked()
        callback = self._event_callback
        payload = {"session_id": self._session_id or "", "items": deepcopy(self._items)}
        if callback is not None:
            callback("plan_snapshot", payload)

    def _persist_locked(self) -> None:
        path = self.storage_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self._items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)

    def _load_locked(self) -> None:
        path = self.storage_path()
        if path is None or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                # 合法 JSON 但不是计划项列表（如 dict/数字）同样视为损坏
                raise ValueError("plan file must contain a JSON list of items")
            normalized: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in raw:
                normalized_item = self._normalize_item(item)
                if normalized_item["id"] in seen:
                    raise ValueError(f"duplicate plan item id: {normalized_item['id']}")
                seen.add(normalized_item["id"])
                normalized.append(normalized_item)
            self._validate_graph(normalized)
        # UnicodeDecodeError 是 ValueError 子类（read_text 解码失败），AttributeError
        # 覆盖列表元素不是 dict 时 _normalize_item 的 .get 调用失败
        except (ValueError, TypeError, AttributeError) as error:
            # 计划文件损坏：备份为 .corrupt 后以空计划启动，避免构造/bind_session 直接崩溃
            corrupt_path = path.with_name(path.name + ".corrupt")
            try:
                os.replace(path, corrupt_path)
            except OSError:
                pass
            self._items = []
            warnings.warn(
                f"计划文件 {path} 已损坏（{error}），已备份为 {corrupt_path.name} 并以空计划启动"
            )
            return
        for item in normalized:
            if item["status"] == "in_progress":
                item["status"] = "pending"
                item["error"] = "进程重启后已恢复为待执行"
        self._items = normalized


_DEFAULT_PLAN_MANAGER = PlanManager()


def get_default_plan_manager() -> PlanManager:
    return _DEFAULT_PLAN_MANAGER


def bind_default_plan_session(session_id: str | None) -> None:
    _DEFAULT_PLAN_MANAGER.bind_session(session_id)


def plan_write(items: list[dict[str, Any]]) -> str:
    if any(str(item.get("status", "pending")).strip() != "pending" for item in items):
        return "新建计划项必须是 pending，运行状态由真实调度器管理。"
    for item in items:
        verification_type = str(item.get("verification_type", "result")).strip().lower()
        verification = str(item.get("verification", "")).strip()
        if verification_type == "command" and _starts_with_cjk(verification):
            return (
                "计划配置错误：verification_type=command 必须填写可执行命令；"
                "中文验收说明请改为 verification_type=result。"
            )
    _DEFAULT_PLAN_MANAGER.replace(items)
    return "计划已更新。"


def plan_update(
    item_id: str,
    status: str | None = None,
    subject: str | None = None,
    depends_on: list[str] | None = None,
    owner: str | None = None,
    files: list[str] | None = None,
    verification: str | None = None,
    stage: str | None = None,
    kind: str | None = None,
    result: str | None = None,
    error: str | None = None,
    attempts: int | None = None,
    verification_type: str | None = None,
) -> str:
    if status in {"in_progress", "completed", "failed", "blocked", "cancelled"}:
        return "计划运行状态由真实调度器管理，不能通过 plan_update 伪造。"
    updated = _DEFAULT_PLAN_MANAGER.update(
        item_id,
        status=status,
        subject=subject,
        depends_on=depends_on,
        owner=owner,
        files=files,
        verification=verification,
        verification_type=verification_type,
        stage=stage,
        kind=kind,
        result=result,
        error=error,
        attempts=attempts,
    )
    return "计划项已更新。" if updated else f"无法更新计划项: {item_id}"
