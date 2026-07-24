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


def _contains_cjk(text: str) -> bool:
    """检查文本是否包含任何 CJK 统一表意文字（中日韩汉字）。"""
    return any("\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf" for ch in str(text or ""))


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
        if not isinstance(raw, dict):
            raise ValueError(f"plan item must be an object, got {type(raw).__name__}: {raw!r}")
        item_id = str(raw.get("id", "")).strip()
        subject = str(raw.get("subject", "")).strip()
        status = str(raw.get("status", "pending")).strip()
        if not item_id:
            raise ValueError(f"plan item missing required field 'id': {raw!r}")
        if not subject:
            raise ValueError(f"plan item '{item_id}' missing required field 'subject'")
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


def plan_write(items: list[dict[str, Any]] | None = None) -> str:
    if items is None:
        return (
            "计划参数错误：调用 plan_write 时必须传入 items 数组（计划项列表）。"
            "如果计划为空，请传入空数组 []，不要省略 items 参数。"
        )
    if not isinstance(items, list):
        return (
            f"计划参数错误：items 必须是数组，当前收到的是 {type(items).__name__}。"
            "请重新调用 plan_write(items=[...]) 并传入正确的计划项列表。"
        )
    non_pending = [
        f"'{item.get('id', '?')}'(status={item.get('status', 'pending')})"
        for item in items
        if str(item.get("status", "pending")).strip() != "pending"
    ]
    if non_pending:
        return (
            f"新建计划项必须是 pending，运行状态由真实调度器管理。"
            f"以下 {len(non_pending)} 项的状态不是 'pending': {', '.join(non_pending)}。"
            f"请将所有项的 status 改为 'pending'，调度器会在 run_subagent 执行对应项时自动将状态设为 in_progress。"
        )
    for item in items:
        verification_type = str(item.get("verification_type", "result")).strip().lower()
        verification = str(item.get("verification", "")).strip()
        if verification_type == "command" and _contains_cjk(verification):
            return (
                "计划配置错误：verification_type=command 时必须填写可执行 shell 命令，"
                "不能包含中文。当前 verification 包含中文描述，请改为："
                "verification_type='result' 并在 verification 中写自然语言验收说明，"
                "或保持 verification_type='command' 但填写纯英文/命令字符的可执行命令（"
                "例如 'cd test20260722 && python -m pytest test_wc.py -v'）。"
            )
    try:
        _DEFAULT_PLAN_MANAGER.replace(items)
    except ValueError as exc:
        return f"计划写入失败：{exc}。请修正每一项的 id、subject 和依赖关系后重试。"
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
    if status in {"in_progress", "completed"}:
        # LLM 经常尝试通过 plan_update 设置受管状态来标记进度。
        # 返回明确的引导消息，告知正确的方式是通过 run_subagent 执行计划项。
        return (
            f"状态变更由调度器自动管理，不能通过 plan_update 手动设置。"
            f"要执行计划项，请使用 run_subagent(plan_item_id='{item_id}')，"
            f"调度器会自动将状态从 pending 推进到 in_progress。"
        )
    if status in {"failed", "blocked", "cancelled"}:
        return (
            f"不能通过 plan_update 将计划项置为 {status}。"
            f"状态为 failed/blocked/cancelled 的项如需重试，"
            f"请使用 plan_retry(item_id='{item_id}') 重置为 pending。"
        )
    # status == "pending" 是 LLM 重置失败项的自然操作，允许从 terminal 状态恢复
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


def plan_retry(item_id: str) -> str:
    """将失败/阻塞/取消的计划项重置为 pending，允许重新调度执行。"""
    from core.task_scheduler import get_default_scheduler

    scheduler = get_default_scheduler()
    if scheduler.retry(item_id):
        return f"计划项 '{item_id}' 已重置为 pending，可重新调用 run_subagent(plan_item_id='{item_id}') 执行。"
    item = next((i for i in _DEFAULT_PLAN_MANAGER.snapshot() if i["id"] == item_id), None)
    if item is None:
        return f"计划项 '{item_id}' 不存在。"
    return (
        f"无法重置计划项 '{item_id}'。"
        f"当前状态为 '{item.get('status', 'unknown')}'，"
        f"只有 failed/blocked/cancelled 状态可以重试。"
    )


def plan_read(item_id: str | None = None) -> str:
    """返回当前计划状态，用于 LLM 查看计划详情。

    若提供 item_id，返回该计划项的详细信息；
    否则返回完整计划快照。
    """
    snapshot = _DEFAULT_PLAN_MANAGER.snapshot()
    if not snapshot:
        return "当前没有计划项。"

    if item_id:
        item = next((i for i in snapshot if i["id"] == str(item_id).strip()), None)
        if item is None:
            available = ", ".join(i["id"] for i in snapshot)
            return f"计划项 '{item_id}' 不存在。当前计划中的项: [{available}]"
        return (
            f"计划项: id='{item['id']}', subject='{item['subject']}', "
            f"status='{item['status']}', kind='{item['kind']}', "
            f"depends_on={item['depends_on']}, stage='{item['stage']}', "
            f"attempts={item['attempts']}, error='{item['error']}'"
        )

    lines = [f"计划共 {len(snapshot)} 项:"]
    for item in snapshot:
        deps = f" ← {item['depends_on']}" if item["depends_on"] else ""
        ready_mark = " ✓就绪" if (
            item["status"] == "pending"
            and _DEFAULT_PLAN_MANAGER._dependencies_completed_locked(item)
        ) else ""
        lines.append(
            f"  [{item['status']:12s}] {item['id']:20s} | {item['subject'][:40]}{deps}{ready_mark}"
        )
    return "\n".join(lines)
