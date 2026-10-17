"""闲时自动维护：用户空闲 N 秒后跑水位线维护（snip/elide）与压缩。

线程模型对齐 cache_keepalive：单守护线程 + Condition，按会话隔离条目，
锁外执行任务。用户发新消息即开始新回合（on_turn_started）→ 撤销未触发的
空闲任务并协作中断在跑的闲时压缩；与回合的共存依赖既有协议——回合组装
检测 is_session_compressing 后禁推水位/禁账本轮换（agent_service），闲时
任务触发前在锁内复查忙闲，压缩复用 _trigger_background_precompress 的
防抖/防重入/CAS 增量落库。
"""

from __future__ import annotations

import logging
import threading
import time

try:
    from core.memory_compressor import DEFAULT_IDLE_MAINTENANCE_SECONDS
except Exception:  # pragma: no cover - memory_compressor 导入失败时保持兜底默认
    DEFAULT_IDLE_MAINTENANCE_SECONDS = 240

_LOGGER = logging.getLogger(__name__)

# 孤儿 Blob 回收最短间隔（秒）：需全库扫媒体清单，闲时维护节流为每小时至多一次
BLOB_GC_MIN_INTERVAL_SECONDS = 3600


def get_idle_maintenance_seconds():
    """闲时维护触发延时（秒，0 关闭）；读 config 失败回落默认值。"""
    try:
        from core.memory_compressor import _load_compression_config
        return max(0, int(
            _load_compression_config().get(
                "idle_maintenance_seconds", DEFAULT_IDLE_MAINTENANCE_SECONDS
            )
        ))
    except Exception:
        return DEFAULT_IDLE_MAINTENANCE_SECONDS


class IdleMaintenanceService:
    """按会话调度的闲时维护服务。入口层只需两个调用：

    - on_turn_started(session)：回合开始（撤销空闲任务 + 中断在跑压缩）
    - on_turn_finished(session, notify)：回合结束（重新计时，到期跑维护）
    """

    def __init__(self):
        self._condition = threading.Condition()
        self._entries = {}      # session -> {"deadline", "notify"}
        self._busy = set()      # 有回合在跑的会话
        self._cancel_events = {}  # 在跑闲时压缩的协作取消事件
        self._thread = None
        self._shutdown = False
        self._last_blob_gc_at = None  # 上次孤儿 Blob 回收时间（monotonic，节流用）

    # ── 入口层接线 API ────────────────────────────────────────────────────

    def on_turn_started(self, session):
        """回合开始：撤销该会话未触发的空闲任务，协作中断在跑的闲时压缩。

        中断只作用于闲时压缩自身（cancel_event → 压缩侧 _cancellable_sleep
        与 429 退避检查点）；若压缩请求已在飞行中，交由压缩完成后的 CAS 水位
        与回合组装协议兜底，不做硬杀。
        """
        with self._condition:
            self._busy.add(str(session or ""))
            self._entries.pop(str(session or ""), None)
            event = self._cancel_events.get(str(session or ""))
            self._condition.notify_all()
        if event is not None:
            event.set()

    def on_turn_finished(self, session, notify=None):
        """回合结束：按配置延时调度闲时维护（0 = 关闭，不调度）。"""
        session = str(session or "")
        delay = get_idle_maintenance_seconds()
        with self._condition:
            self._busy.discard(session)
            if delay <= 0 or self._shutdown:
                self._condition.notify_all()
                return
            self._entries[session] = {
                "deadline": time.monotonic() + delay,
                "notify": notify,
            }
            self._ensure_thread_locked()
            self._condition.notify_all()

    def status(self):
        with self._condition:
            return {
                "pending": len(self._entries),
                "busy": len(self._busy),
                "compressing": len(self._cancel_events),
            }

    def shutdown(self):
        with self._condition:
            self._shutdown = True
            self._entries.clear()
            self._condition.notify_all()

    # ── 调度线程与维护执行 ────────────────────────────────────────────────

    def _ensure_thread_locked(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run, name="idle-maintenance-scheduler", daemon=True,
            )
            self._thread.start()

    def _run(self):
        while True:
            with self._condition:
                while True:
                    if self._shutdown:
                        return
                    if not self._entries:
                        self._condition.wait()
                        continue
                    session = min(
                        self._entries, key=lambda s: self._entries[s]["deadline"]
                    )
                    remaining = self._entries[session]["deadline"] - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(timeout=remaining)
                        continue
                    entry = dict(self._entries.pop(session))
                    entry["session"] = session
                    break
            # 到期恰逢回合在跑：条目按设计消费掉不重排（不空转），
            # 会话下次 on_turn_finished 会重新计时排期
            if session in self._busy:
                continue
            threading.Thread(
                target=self._run_maintenance,
                args=(entry,),
                name=f"idle-maintenance-{session}",
                daemon=True,
            ).start()

    def _run_maintenance(self, entry):
        session = entry["session"]
        notify = entry.get("notify")
        cancel_event = threading.Event()
        with self._condition:
            if self._shutdown or session in self._busy:
                return
            if session in self._cancel_events:
                # 已有闲时压缩在跑：复用其注册事件，绝不覆盖——覆盖会让在跑
                # 压缩的事件失去注册，回合开始（on_turn_started）再也中断不到它
                cancel_event = self._cancel_events[session]
            else:
                self._cancel_events[session] = cancel_event
        try:
            self._do_maintenance(session, notify, cancel_event)
        except Exception:
            _LOGGER.debug("会话 %s 闲时维护异常（已忽略）", session, exc_info=True)
        finally:
            with self._condition:
                # 只清理自己注册的事件；复用场景下注册表条目归上一轮所有
                if self._cancel_events.get(session) is cancel_event:
                    self._cancel_events.pop(session, None)

    def _do_maintenance(self, session, notify, cancel_event):
        from core.app_context import get_manager

        manager = get_manager()
        if manager is None:
            return
        # 免费本地维护：旧工具结果折叠/占位（一次性、幂等、DB 层，语义照旧）。
        # 顺带把改写后的新前缀在空闲窗口内预热。
        try:
            manager.maintain_tool_results(session, tier=1)
            manager.maintain_tool_results(session, tier=2)
        except Exception:
            _LOGGER.debug("会话 %s 闲时本地维护失败", session, exc_info=True)

        # 媒体仓库治理（免费本地维护）：每次闲时清超期 .part 残留（目录通常
        # 为空，成本极低）；孤儿 Blob 回收需全库扫媒体清单，按最短间隔节流。
        try:
            from core.media_store import prune_orphan_blobs, prune_orphan_tmp

            prune_orphan_tmp()
            now = time.monotonic()
            if (
                self._last_blob_gc_at is None
                or now - self._last_blob_gc_at >= BLOB_GC_MIN_INTERVAL_SECONDS
            ):
                self._last_blob_gc_at = now
                prune_orphan_blobs(manager.iter_referenced_media_digests())
        except Exception:
            _LOGGER.debug("会话 %s 闲时媒体仓库治理失败", session, exc_info=True)

        if cancel_event.is_set():
            return

        # LLM 压缩：水位达标才触发，复用预压缩入口的防抖/防重入/notify 管道
        try:
            from core.model_manager import get_active_model
            active = get_active_model(session)
        except Exception:
            active = None
        if not active:
            return
        window = int(active.get("context_window") or 0)
        if window <= 0:
            return
        try:
            from core.agent_service import (
                SUMMARIZE_RATIO,
                _trigger_background_precompress,
                get_session_token_baseline,
            )
            from core.memory_compressor import (
                get_precompress_ratio,
                get_watermark_base_tokens,
            )

            history = manager.get_history_for_llm(session)
            baseline = get_session_token_baseline(
                manager, session, active.get("litellm_model") or "",
                history=history, api_base=active.get("base_url") or "",
            )
            watermark_base = get_watermark_base_tokens(active.get("litellm_model") or "")
            watermark_window = min(window, watermark_base) if watermark_base else window
            precompress_ratio = get_precompress_ratio()
            threshold = precompress_ratio if precompress_ratio > 0 else SUMMARIZE_RATIO
            if baseline <= watermark_window * threshold:
                return
            done = threading.Event()
            spawned = _trigger_background_precompress(
                manager, session, active.get("litellm_model") or "",
                active.get("api_key") or "", active.get("base_url") or "",
                cancel_event, notify, done_event=done,
            )
            if spawned:
                # 阻塞到压缩真正结束再返回：_trigger 是发射后不管的，若现在
                # 就返回，finally 会把 cancel_event 弹出注册表，压缩在飞的
                # 整个过程里回合开始都中断不到它（done_event 由压缩线程
                # 的 finally 置位，包括失败/异常路径，不会永久挂起）
                done.wait()
        except Exception:
            _LOGGER.debug("会话 %s 闲时压缩触发失败", session, exc_info=True)


# 进程级单例：GUI 与 CLI 共用同一调度器
idle_maintenance_service = IdleMaintenanceService()
