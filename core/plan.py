"""Persisted dependency-aware execution plans for one active session."""

from __future__ import annotations

import json
import logging
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable


logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# 计划续推抑制：用户主动取消回合后，llm 收尾时的"计划尚未执行完毕，请继续
# run_subagent" nudge 会把刚取消的任务复活。suppress 后不再自动续推；
# 计划项再次进入 in_progress（用户明确要求继续、模型重新派发）或计划清空时
# 自动解除（见 clear_plan_auto_continue_suppression 的调用点）。
# ---------------------------------------------------------------------------
_PLAN_AUTO_CONTINUE_SUPPRESSED: set[str] = set()
_PLAN_AUTO_CONTINUE_LOCK = threading.Lock()


def suppress_plan_auto_continue(session_id: str) -> None:
    """标记某会话的计划自动续推被用户取消抑制（GUI stop / CLI Ctrl+C 调用）。"""
    if not session_id:
        return
    with _PLAN_AUTO_CONTINUE_LOCK:
        _PLAN_AUTO_CONTINUE_SUPPRESSED.add(str(session_id))


def plan_auto_continue_suppressed(session_id: str) -> bool:
    with _PLAN_AUTO_CONTINUE_LOCK:
        return str(session_id) in _PLAN_AUTO_CONTINUE_SUPPRESSED


def clear_plan_auto_continue_suppression(session_id: str) -> None:
    if not session_id:
        return
    with _PLAN_AUTO_CONTINUE_LOCK:
        _PLAN_AUTO_CONTINUE_SUPPRESSED.discard(str(session_id))


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
        if storage_dir is None:
            # 存储目录跟随当前线程的工作目录（回合线程内即会话归属项目目录），
            # 不再冻结在进程启动目录——GUI 多项目会话的计划各自落在各自项目下
            from core.workspace_context import get_workdir

            storage_dir = Path(get_workdir()) / ".ycode_tasks" / "plans"
        self._storage_dir = Path(storage_dir)
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

        # 迭代式 DFS：超长依赖链（>1000 项）不会触发 RecursionError
        for start in graph:
            if start in visited:
                continue
            visiting.add(start)
            stack = [(start, iter(graph[start]))]
            while stack:
                node, dependencies = stack[-1]
                descended = False
                for dependency in dependencies:
                    if dependency in visiting:
                        raise ValueError(f"plan dependency cycle involving: {dependency}")
                    if dependency not in visited:
                        visiting.add(dependency)
                        stack.append((dependency, iter(graph[dependency])))
                        descended = True
                        break
                if not descended:
                    visiting.discard(node)
                    visited.add(node)
                    stack.pop()

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
            logger.warning(
                "计划文件 %s 已损坏（%s），已备份为 %s 并以空计划启动",
                path, error, corrupt_path.name
            )
            return
        for item in normalized:
            if item["status"] == "in_progress":
                item["status"] = "pending"
                item["error"] = "进程重启后已恢复为待执行"
        self._items = normalized


_DEFAULT_PLAN_MANAGER = PlanManager()

# ---------------------------------------------------------------------------
# 多会话并行：按会话实例化的计划管理器注册表 + 线程本地路由
# ---------------------------------------------------------------------------
# 进程级单例 _DEFAULT_PLAN_MANAGER 只服务未绑定线程（CLI 主流程 / 测试主线程），
# 语义与单会话时代完全一致（bind_default_plan_session 就地重绑）。
# GUI 回合线程 / 子代理线程通过 set_current_plan_session 绑定自己的会话，
# get_default_plan_manager 按线程路由到该会话专属的 PlanManager 实例：
# 并行回合各自读写各自的管理器（实例内 RLock 串行化同一会话的并发访问，
# 不同会话之间零共享临界区），彻底消除 A 回合读到/改写 B 计划的串扰。
_PLAN_MANAGERS: dict[str, PlanManager] = {}
_PLAN_MANAGERS_LOCK = threading.RLock()
_plan_route_tls = threading.local()


def set_current_plan_session(session_id: str | None, *, storage_dir=None) -> None:
    """绑定当前线程的计划会话路由（GUI 回合线程 / 子代理线程入口调用）。

    回合 finally 中传 None 解绑。storage_dir 可显式指定计划存储目录，
    缺省在首次创建该会话管理器时按当前线程工作目录推导。
    """
    _plan_route_tls.session_id = (
        str(session_id).strip() if session_id else None
    )
    _plan_route_tls.storage_dir = str(storage_dir) if storage_dir else None


def get_bound_plan_session() -> str | None:
    """返回当前线程绑定的计划会话；未绑定返回 None（走进程级单例）。"""
    sid = getattr(_plan_route_tls, "session_id", None)
    return str(sid) if sid else None


def get_plan_manager_for(session_id: str, *, storage_dir=None) -> PlanManager:
    """取（或惰性创建）指定会话的专属计划管理器。

    每次变更都会落盘（_commit_locked），因此即使注册表条目被清理后重建，
    也能从磁盘完整恢复该会话的计划状态。
    """
    key = str(session_id).strip()
    with _PLAN_MANAGERS_LOCK:
        manager = _PLAN_MANAGERS.get(key)
        if manager is None:
            manager = PlanManager(session_id=key, storage_dir=storage_dir)
            _PLAN_MANAGERS[key] = manager
        return manager


def release_plan_manager(session_id: str) -> None:
    """清理某会话的注册表条目（会话删除 / 测试隔离时调用）。"""
    with _PLAN_MANAGERS_LOCK:
        _PLAN_MANAGERS.pop(str(session_id), None)


def get_default_plan_manager() -> PlanManager:
    bound = get_bound_plan_session()
    if bound:
        return get_plan_manager_for(
            bound, storage_dir=getattr(_plan_route_tls, "storage_dir", None)
        )
    return _DEFAULT_PLAN_MANAGER


def plan_state_fingerprint(session_id: str | None = None) -> str:
    """计划 canonical 快照指纹（SHA-256），供 TurnProgressGuard 判定真实进展。

    只纳入影响调度的确定性字段：plan session id、每个计划项的
    id/status/attempts/depends_on、当前 in-progress 项、auto-continue
    suppression 状态。展示文案（subject/result/error 等）与时间戳不参与，
    避免无业务变化却不断重置停滞计数。
    显式 session_id 只 peek 注册表已有条目，绝不惰性创建（与
    maybe_clear_completed_plan 的早退防护同理）。
    """
    import hashlib

    manager = None
    if session_id:
        key = str(session_id).strip()
        with _PLAN_MANAGERS_LOCK:
            manager = _PLAN_MANAGERS.get(key)
        if manager is None:
            payload = {
                "session": key,
                "items": [],
                "in_progress": None,
                "auto_continue_suppressed": plan_auto_continue_suppressed(key),
            }
            canonical = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    else:
        manager = get_default_plan_manager()

    items = manager.snapshot()
    in_progress = manager.in_progress()
    payload = {
        "session": manager.session_id,
        "items": sorted(
            (
                [
                    str(item.get("id", "")),
                    str(item.get("status", "")),
                    int(item.get("attempts", 0)),
                    list(item.get("depends_on", [])),
                ]
                for item in items
            ),
            key=lambda row: row[0],
        ),
        "in_progress": (in_progress or {}).get("id"),
        "auto_continue_suppressed": (
            plan_auto_continue_suppressed(manager.session_id)
            if manager.session_id
            else False
        ),
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def maybe_clear_completed_plan(session_id: str | None = None) -> bool:
    """回合结束时调用：若指定会话（缺省取当前路由）的计划所有项都进入
    终态（completed/failed/blocked/cancelled），清空计划快照并返回 True
    （发生了清空）；否则返回 False。

    任务完成后计划面板/任务规划自动归零，避免已完成计划长期残留。
    只要还有 pending/in_progress 项（任务未完成）就不清空，保证跨回合任务
    的计划不会被提前抹掉。

    session_id 必须由 GUI 回合结束路径显式传入：finish_once 执行时计划会话
    TLS 路由已解绑（_run_turn_worker finally 前段），get_default_plan_manager()
    会退回进程级单例——多会话并行下该单例可能已被 _set_current_session 重绑
    到其他会话，裸用会清错会话的计划。显式 session_id 只 peek 注册表已有条目
    （条目在回合内创建后跨回合存活，storage_dir 正确），绝不惰性创建：
    回合可能在 TLS 绑定前早退（无激活模型 / 项目目录缺失 / 中途异常），
    此时 workdir scope 未进入，get_plan_manager_for 会以进程 cwd 推导
    storage_dir 创建指向错误路径的污染条目并永久占据注册表 key，
    导致该会话后续所有回合的计划工具命中污染条目（plan_read 永远空、
    plan_write 落错误位置），计划功能静默失效。
    CLI 单会话顺序执行，不传 session_id 走 get_default_plan_manager() 默认路由。
    """
    if session_id:
        key = str(session_id).strip()
        with _PLAN_MANAGERS_LOCK:
            manager = _PLAN_MANAGERS.get(key)
        if manager is None:
            return False
    else:
        manager = get_default_plan_manager()
    items = manager.snapshot()
    if not items:
        return False
    if any(item.get("status") not in TERMINAL_STATUSES for item in items):
        return False
    manager.clear()
    return True


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
    invalid_items = [item for item in items if not isinstance(item, dict)]
    if invalid_items:
        # LLM 可能生成 items=[{...}, "oops"] 这类混合数组，必须在推导字段前
        # 拦截，否则后续 item.get 会抛 AttributeError 击穿工具的错误文案契约
        return (
            f"计划参数错误：items 数组中有 {len(invalid_items)} 个元素不是对象"
            f"（第 1 个非法元素：{invalid_items[0]!r}，类型 {type(invalid_items[0]).__name__}）。"
            "每个计划项必须是对象，至少包含 id 和 subject 字段，"
            '例如 {"id": "task1", "subject": "任务描述"}。请修正后重新调用 plan_write。'
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
        # 必须走线程路由：绑定会话的回合线程里写进程级单例，run_subagent 的
        # 调度器读的是会话专属管理器，会永远报"计划项不存在"
        get_default_plan_manager().replace(items)
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
    updated = get_default_plan_manager().update(
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
    """将失败/阻塞/取消的计划项重置为 pending，允许重新调度执行。

    防盲目重试：当 attempts >= 2 且上次失败原因仍然存在时，提示主 Agent
    先分析根因并修改任务描述/工具选择，不要原样重试导致重复失败。
    """
    from core.task_scheduler import get_default_scheduler

    scheduler = get_default_scheduler()
    snapshot = get_default_plan_manager().snapshot()
    item = next((i for i in snapshot if i["id"] == item_id), None)
    if item is None:
        return f"计划项 '{item_id}' 不存在。"

    attempts = int(item.get("attempts", 0))
    last_error = str(item.get("error", "")).strip()

    # 盲目重试防护：已失败 2 次以上时，要求主 Agent 先分析根因
    if attempts >= 2 and last_error:
        return (
            f"⚠️ 计划项 '{item_id}' 已失败 {attempts} 次，上次失败原因：\n"
            f"  {last_error}\n"
            f"盲目重试大概率会再次失败。请主 Agent 先分析根因：\n"
            f"  1. 用 plan_read(item_id='{item_id}') 查看详情\n"
            f"  2. 根据失败原因修改任务描述（如换工具、换方法、拆分子任务）\n"
            f"  3. 用 plan_update(item_id='{item_id}', subject='修改后的任务描述') 更新\n"
            f"  4. 确认修改后再调用 plan_retry(item_id='{item_id}')\n"
            f"如确信根因已排除（如网络恢复、模型可用），可再次调用 plan_retry 强制重试。"
        )

    if scheduler.retry(item_id):
        if attempts >= 1:
            return (
                f"计划项 '{item_id}' 已重置为 pending（第 {attempts + 1} 次尝试），"
                f"可重新调用 run_subagent(plan_item_id='{item_id}') 执行。"
                f"提示：上次失败原因是「{last_error or '未知'}」，请确保本次执行方式已调整。"
            )
        return f"计划项 '{item_id}' 已重置为 pending，可重新调用 run_subagent(plan_item_id='{item_id}') 执行。"
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
    snapshot = get_default_plan_manager().snapshot()
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
            and get_default_plan_manager()._dependencies_completed_locked(item)
        ) else ""
        lines.append(
            f"  [{item['status']:12s}] {item['id']:20s} | {item['subject'][:40]}{deps}{ready_mark}"
        )
    return "\n".join(lines)
