from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from core.computer.actions import MAX_WAIT_MS, MIN_WAIT_MS, _parse_url
from core.computer.frames import coordinate_disabled_error
from core.computer.settings import resolve_headless
from core.computer.models import (
    ComputerAction,
    ComputerControlError,
    ComputerObservation,
    ObservationFrame,
)

_LOGGER = logging.getLogger(__name__)

_CHANNELS = {"edge": "msedge", "chrome": "chrome", "chromium": None}
WORKER_ARGV_FLAG = "--ycode-browser-worker"
_VIEWPORT_WIDTH = 1440
_VIEWPORT_HEIGHT = 900
_MAX_TITLE_CHARS = 200
_COMMAND_QUEUE_SIZE = 8
_DEFAULT_COMMAND_TIMEOUT = 30.0
_DEFAULT_START_TIMEOUT = 30.0
_KEEP_SCREENSHOTS = 3
_PROCESS_GUARD_SLACK = 5.0
_WINDOW_HINT_SETTLE_SECONDS = 1.0
_PROCESS_CLOSE_GRACEFUL = 2.0
_PROCESS_HARD_EXIT = 2.0
_THREAD_CLOSE_TIMEOUT = 10.0

_PROFILE_REGISTRY_GUARD = threading.Lock()
_PROFILE_HOLDERS: dict[str, int] = {}

_PROFILE_LOCK_HINTS = ("processsingleton", "singletonlock", "already in use", "profile locked")

_KEY_NAME_MAP = {
    "CTRL": "Control",
    "ALT": "Alt",
    "SHIFT": "Shift",
    "META": "Meta",
    "ENTER": "Enter",
    "TAB": "Tab",
    "ESC": "Escape",
    "SPACE": "Space",
    "BACKSPACE": "Backspace",
    "DELETE": "Delete",
    "HOME": "Home",
    "END": "End",
    "PAGEUP": "PageUp",
    "PAGEDOWN": "PageDown",
    "UP": "ArrowUp",
    "DOWN": "ArrowDown",
    "LEFT": "ArrowLeft",
    "RIGHT": "ArrowRight",
}

# R5 受控页可见性探针：只读取 visibilityState/hasFocus 两个状态，绝不读取
# 表单值、凭据或其他 DOM 内容（隐私边界，设计 22）。
_VISIBILITY_PROBE = "() => [document.visibilityState, document.hasFocus()]"

# E1 元素列表的防御性截断上限（对齐桌面 windows_elements.py 的 MAX_NAME_CHARS；
# 注入脚本内的 MAX_NAME 已做同样截断，这里兜底协议/求值结果的异常形态）。
_ELEMENT_MAX_NAME_CHARS = 80

_ELEMENT_ACTION_TIMEOUT_MS = 10_000

_SCREENSHOT_TIMEOUT_MS = 3000

_PAGE_TEXT_MAX_CHARS = 6000

PAGE_TEXT_SCRIPT = """() => {
  const root = document.body || document.documentElement;
  if (!root) return "";
  return (root.innerText || "").trim();
}"""

ELEMENT_COLLECT_SCRIPT = """() => {
  const SEL = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea',
    '[role="button"]', '[role="link"]', '[role="textbox"]', '[role="checkbox"]',
    '[role="radio"]', '[role="combobox"]', '[role="menuitem"]', '[role="tab"]',
    '[role="switch"]', '[role="searchbox"]', '[role="option"]',
    '[contenteditable]:not([contenteditable="false"])',
    '[onclick]', '[tabindex]:not([tabindex="-1"])'
  ].join(',');

  const MAX_NAME = 80;
  // 500 而不是 200：网格型页面（扫雷/表格/日历）一个盘面就有 256~480 个
  // 可点击单元，200 会在盘面中部截断，模型永远点不到下半部分；500 覆盖
  // 常见网格整盘，普通页面元素数远小于此，不受影响。
  const MAX_ELEMENTS = 500;
  const DOC_SYM = Symbol.for('__ycode_doc_state__');
  const docHost = document.documentElement || document;

  let state = docHost[DOC_SYM];
  if (!state) {
    const token = (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function')
      ? crypto.randomUUID()
      : (Math.random().toString(36).slice(2) + '-' + Date.now().toString(36));
    state = {
      docToken: token,
      nodeIds: new WeakMap(),
      idToNode: new Map(),
      nextId: 0
    };
    Object.defineProperty(docHost, DOC_SYM, { value: state, configurable: true });
  }

  // ---- 可见性判定 ----
  // 优先用 checkVisibility()（现代 Chromium 支持，含祖先隐藏判定）；
  // 不可用时回落到 getBoundingClientRect + computedStyle 组合判定。
  function isVisible(el) {
    if (typeof el.checkVisibility === 'function') {
      return el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
    }
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = getComputedStyle(el);
    return style.visibility !== 'hidden'
        && style.display !== 'none'
        && parseFloat(style.opacity) !== 0;
  }

  // ---- 视口内判定 ----
  function inViewport(rect) {
    return rect.bottom > 0
        && rect.right > 0
        && rect.top < window.innerHeight
        && rect.left < window.innerWidth;
  }

  // ---- kind 推导：HTML/ARIA 原生语义（不映射 UIA 控件类型名） ----
  function deriveKind(el, isCursorFallback) {
    const role = el.getAttribute('role');
    if (role) return role;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'submit' || type === 'button' || type === 'reset') return 'button';
      if (type === 'search') return 'searchbox';
      return 'textbox';
    }
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (el.isContentEditable) return 'textbox';
    if (isCursorFallback) return 'button';
    return tag;
  }

  // ---- name 推导：对齐 W3C AccName 主流实现顺序，绝不读表单值 ----
  function deriveName(el) {
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();

    const scope = (typeof el.getRootNode === 'function' ? el.getRootNode() : document) || document;
    const lookupById = (id) => {
      if (!id) return null;
      if (typeof scope.getElementById === 'function') return scope.getElementById(id) || document.getElementById(id);
      return (typeof scope.querySelector === 'function' ? scope.querySelector('#' + CSS.escape(id)) : null)
          || document.getElementById(id);
    };

    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const texts = labelledBy.split(/\\s+/)
        .map(id => lookupById(id))
        .filter(Boolean)
        .map(node => (node.innerText || '').trim())
        .filter(Boolean);
      if (texts.length) return texts.join(' ');
    }

    if (el.id) {
      const escaped = CSS.escape(el.id);
      const label = (typeof scope.querySelector === 'function' ? scope.querySelector(`label[for="${escaped}"]`) : null)
          || document.querySelector(`label[for="${escaped}"]`);
      if (label && label.innerText.trim()) return label.innerText.trim();
    }
    const wrappingLabel = el.closest('label');
    if (wrappingLabel && wrappingLabel.innerText.trim()) return wrappingLabel.innerText.trim();

    const placeholder = el.getAttribute('placeholder');
    if (placeholder && placeholder.trim()) return placeholder.trim();
    const title = el.getAttribute('title');
    if (title && title.trim()) return title.trim();
    const alt = el.getAttribute('alt');
    if (alt && alt.trim()) return alt.trim();

    const text = (el.innerText || '').trim();
    if (text) return text;

    return '';
  }

  // ---- 视觉可点击区域（E3 修复） ----
  function hitRect(el, rect) {
    let left = rect.left, top = rect.top, right = rect.right, bottom = rect.bottom;
    for (const child of el.querySelectorAll('img, svg, canvas, video')) {
      const r = child.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      if (r.left < left) left = r.left;
      if (r.top < top) top = r.top;
      if (r.right > right) right = r.right;
      if (r.bottom > bottom) bottom = r.bottom;
    }
    return { left: left, top: top, right: right, bottom: bottom };
  }

  function walkDOM(root, primary, primarySet, allElements) {
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let node = root.nodeType === 1 ? root : walker.nextNode();
    while (node) {
      if (node.nodeType === 1) {
        allElements.push(node);
        if (typeof node.matches === 'function' && node.matches(SEL)) {
          primary.push(node);
          primarySet.add(node);
        }
        if (node.shadowRoot && node.shadowRoot.mode === 'open') {
          walkDOM(node.shadowRoot, primary, primarySet, allElements);
        }
      }
      node = walker.nextNode();
    }
  }

  function hasAncestorIn(el, set) {
    let cur = el.parentElement;
    while (cur) {
      if (set.has(cur)) return true;
      cur = cur.parentElement;
    }
    return false;
  }

  const primary = [];
  const primarySet = new Set();
  const allElements = [];
  walkDOM(document.documentElement || document, primary, primarySet, allElements);

  // cursor:pointer 仅作候选扫描补充：语义控件优先，避免继承 cursor:pointer
  // 的子 span 泛滥或挤占真实控件槽位。
  const cursorExtras = [];
  const cursorSet = new Set();
  for (const el of allElements) {
    if (primarySet.has(el) || hasAncestorIn(el, primarySet)) continue;
    let style;
    try {
      style = getComputedStyle(el);
    } catch (_err) {
      continue;
    }
    if (!style || style.cursor !== 'pointer') continue;
    const parent = el.parentElement;
    if (parent) {
      try {
        if (getComputedStyle(parent).cursor === 'pointer' && (primarySet.has(parent) || cursorSet.has(parent))) {
          continue;
        }
      } catch (_err) {}
    }
    if (typeof el.querySelector === 'function' && el.querySelector(SEL)) continue;
    cursorSet.add(el);
    cursorExtras.push(el);
  }

  const ordered = primary.map(el => [el, false]).concat(cursorExtras.map(el => [el, true]));
  const out = [];
  const liveIds = new Set();

  for (const [el, isCursorFallback] of ordered) {
    if (out.length >= MAX_ELEMENTS) break;
    const rawRect = el.getBoundingClientRect();
    if (!isVisible(el) || !inViewport(rawRect)) continue;
    const rect = hitRect(el, rawRect);
    if (!(rect.right > rect.left && rect.bottom > rect.top)) continue;
    const derivedName = deriveName(el);
    if (isCursorFallback && !derivedName && !el.querySelector('img, svg')) continue;

    let ref = state.nodeIds.get(el);
    if (!ref) {
      state.nextId += 1;
      ref = 'el-' + state.nextId;
      state.nodeIds.set(el, ref);
    }
    state.idToNode.set(ref, typeof WeakRef === 'function' ? new WeakRef(el) : el);
    liveIds.add(ref);
    out.push({
      ref: ref,
      kind: deriveKind(el, isCursorFallback),
      // 按码点截断（Array.from），避免 UTF-16 代理对（emoji 等）被切半；
      // 与 Python 侧 [:_ELEMENT_MAX_NAME_CHARS] 的码点语义对齐。
      name: Array.from(derivedName).slice(0, MAX_NAME).join(''),
      bounds: [
        Math.round(rect.left), Math.round(rect.top),
        Math.round(rect.right), Math.round(rect.bottom)
      ],
      enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
    });
  }
  for (const existingRef of Array.from(state.idToNode.keys())) {
    if (!liveIds.has(existingRef)) {
      state.idToNode.delete(existingRef);
    }
  }
  return out;
}"""

_DOC_TOKEN_SCRIPT = """() => {
  const DOC_SYM = Symbol.for('__ycode_doc_state__');
  const docHost = document.documentElement || document;
  const existing = docHost[DOC_SYM];
  if (existing && existing.docToken) return existing.docToken;
  const token = (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function')
    ? crypto.randomUUID()
    : (Math.random().toString(36).slice(2) + '-' + Date.now().toString(36));
  const state = {
    docToken: token,
    nodeIds: new WeakMap(),
    idToNode: new Map(),
    nextId: 0
  };
  Object.defineProperty(docHost, DOC_SYM, { value: state, configurable: true });
  return token;
}"""

ELEMENT_RESOLVE_SCRIPT = """(spec) => {
  if (!spec || typeof spec !== 'object') return null;
  const docHost = document.documentElement || document;
  const state = docHost[Symbol.for('__ycode_doc_state__')];
  if (!state || state.docToken !== spec.docToken || !(state.idToNode instanceof Map)) {
    return null;
  }
  const entry = state.idToNode.get(spec.id);
  const el = (typeof WeakRef === 'function' && entry instanceof WeakRef) ? entry.deref() : entry;
  if (!el || !el.isConnected) return null;
  return el;
}"""

# E3（2026-11-02）元素点击点：按「视觉可点击区域」的中心点点击；无 DOM 属性依赖，
# 支持 open Shadow DOM 内的 shadowRoot.elementFromPoint 命中核验。
ELEMENT_HIT_SCRIPT = """(spec) => {
  const id = (spec && typeof spec === 'object') ? spec.id : spec;
  const docToken = (spec && typeof spec === 'object') ? spec.docToken : null;
  const docHost = document.documentElement || document;
  const state = docHost[Symbol.for('__ycode_doc_state__')];
  if (!state || !(state.idToNode instanceof Map)) return null;
  if (docToken && state.docToken !== docToken) return { stale: true };
  const entry = state.idToNode.get(id);
  const el = (typeof WeakRef === 'function' && entry instanceof WeakRef) ? entry.deref() : entry;
  if (!el || !el.isConnected) return { stale: true };
  const base = el.getBoundingClientRect();
  let left = base.left, top = base.top, right = base.right, bottom = base.bottom;
  for (const child of el.querySelectorAll('img, svg, canvas, video')) {
    const r = child.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    if (r.left < left) left = r.left;
    if (r.top < top) top = r.top;
    if (r.right > right) right = r.right;
    if (r.bottom > bottom) bottom = r.bottom;
  }
  if (!(right > left && bottom > top)) return null;
  if (bottom <= 0 || right <= 0 || top >= innerHeight || left >= innerWidth) {
    return { stale: true, inViewport: false };
  }
  const x = Math.round((left + right) / 2);
  const y = Math.round((top + bottom) / 2);
  const inViewport = x >= 0 && y >= 0 && x < innerWidth && y < innerHeight;
  let contains = false;
  if (inViewport) {
    let hit = document.elementFromPoint(x, y);
    let currentHost = hit;
    while (currentHost && currentHost.shadowRoot && typeof currentHost.shadowRoot.elementFromPoint === 'function') {
      const innerHit = currentHost.shadowRoot.elementFromPoint(x, y);
      if (!innerHit || innerHit === currentHost) break;
      hit = innerHit;
      currentHost = innerHit;
    }
    contains = !!(hit && (hit === el || el.contains(hit)));
  }
  return {
    stale: false,
    x: x,
    y: y,
    contains: contains,
    inViewport: inViewport
  };
}"""

_FRAME_OFFSET_SCRIPT = """(frameEl) => {
  if (!frameEl || !frameEl.isConnected) return null;
  const r = frameEl.getBoundingClientRect();
  const cs = getComputedStyle(frameEl);
  if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return null;
  const bx = parseFloat(cs.borderLeftWidth) || 0;
  const by = parseFloat(cs.borderTopWidth) || 0;
  const px = parseFloat(cs.paddingLeft) || 0;
  const py = parseFloat(cs.paddingTop) || 0;
  const w = frameEl.clientWidth;
  const h = frameEl.clientHeight;
  if (w <= 0 || h <= 0) return null;
  return { x: r.left + bx + px, y: r.top + by + py, w: w, h: h };
}"""

ELEMENT_WAIT_PROBE_SCRIPT = """(spec) => {
  if (!spec || typeof spec !== 'object') return { stale: true };
  const docHost = document.documentElement || document;
  const state = docHost[Symbol.for('__ycode_doc_state__')];
  if (!state || state.docToken !== spec.docToken || !(state.idToNode instanceof Map)) {
    return { stale: true };
  }
  const entry = state.idToNode.get(spec.id);
  const el = (typeof WeakRef === 'function' && entry instanceof WeakRef) ? entry.deref() : entry;
  if (!el || !el.isConnected) return { stale: false, exists: false, enabled: false };
  let visible = false;
  if (typeof el.checkVisibility === 'function') {
    visible = el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
  } else {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    visible = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) !== 0;
  }
  const enabled = visible && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
  return { stale: false, exists: visible, enabled: enabled };
}"""


@dataclass(frozen=True)
class _BrowserElementRecord:
    ref: str
    run_id: str
    page_token: int
    frame_id: str
    frame: object
    document_generation: str
    observation_generation: int
    viewport_generation: int
    local_id: str
    offset_x: int
    offset_y: int
    bounds: tuple[int, int, int, int]
    kind: str


def _normalize_element_bounds(value) -> list[int]:
    """bounds 防御归一化：4 元 int list（对齐桌面脱敏 dict 形状），非法值取 0。"""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return [0, 0, 0, 0]
    normalized: list[int] = []
    for component in value:
        try:
            normalized.append(int(component))
        except (TypeError, ValueError):
            normalized.append(0)
    return normalized


def _map_playwright_key(name: str) -> str:
    """统一动作键名 → Playwright key；字母在组合键中必须小写（Control+a）。"""
    if name in _KEY_NAME_MAP:
        return _KEY_NAME_MAP[name]
    if len(name) == 1 and name.isalpha():
        return name.lower()
    if len(name) == 1 and name.isdigit():
        return name
    if name.startswith("F") and name[1:].isdigit() and 1 <= int(name[1:]) <= 12:
        return name
    raise ComputerControlError("computer_invalid_action", f"不支持的键名: {name!r}")


class _Command:

    __slots__ = (
        "command_id", "fn", "deadline", "allow_stopping", "lock", "done",
        "state", "result", "error",
    )

    def __init__(
        self,
        command_id: int,
        fn: Callable | None,
        deadline: float,
        *,
        allow_stopping: bool = False,
    ) -> None:
        self.command_id = command_id
        self.fn = fn
        self.deadline = deadline
        self.allow_stopping = allow_stopping
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.state = "queued"
        self.result = None
        self.error: BaseException | None = None

    def try_start(self, now: float) -> bool:
        """worker 原子领取：仅 queued 且未到期才可进入 running。"""
        with self.lock:
            if self.state != "queued":
                return False
            if now >= self.deadline:
                self.state = "cancelled"
                self.error = ComputerControlError(
                    "computer_action_timeout", "浏览器命令已过截止时间，排队阶段即取消"
                )
                self.done.set()
                return False
            self.state = "running"
            return True

    def cancel_if_queued(self, code: str, message: str) -> bool:
        """调用方/close 撤销排队命令；已被 worker 领取则返回 False（不得谎报）。"""
        with self.lock:
            if self.state != "queued":
                return False
            self.state = "cancelled"
            self.error = ComputerControlError(code, message)
            self.done.set()
            return True

    def finish(self) -> None:
        """worker 写入终态并唤醒调用方。"""
        with self.lock:
            self.state = "failed" if self.error is not None else "succeeded"
            self.done.set()


class _PendingCommand:

    __slots__ = ("done", "response", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.response: dict | None = None
        self.error: BaseException | None = None


class _WorkerProcessTransport:

    def __init__(
        self,
        worker_command: list[str],
        *,
        start_timeout: float,
        guard_timeout: float,
    ) -> None:
        self._worker_command = list(worker_command)
        self._start_timeout = float(start_timeout)
        self._guard_timeout = float(guard_timeout)
        self._proc: subprocess.Popen | None = None
        self._job = None
        self._lock = threading.Lock()
        self._stdin_lock = threading.Lock()
        self._seq = 0
        self._pending: dict[int, _PendingCommand] = {}
        self._ready = threading.Event()
        self._boot_error: tuple[str, str] | None = None
        self._dead = threading.Event()
        self.browser_pid: int | None = None
        self.screenshot_dir: str = ""

    def start(self) -> None:
        if sys.platform == "win32":
            self._job = _create_kill_on_close_job()
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        try:
            popen_kwargs = {}
            if sys.platform != "win32":
                popen_kwargs["start_new_session"] = True
            self._proc = subprocess.Popen(
                self._worker_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                **popen_kwargs,
            )
        except OSError as error:
            self._close_job()
            raise ComputerControlError(
                "computer_backend_start_failed",
                f"无法启动受管浏览器执行进程：{error}",
            ) from error
        if self._job is not None:
            _assign_process_to_job(self._job, int(self._proc._handle))  # noqa: SLF001
        reader = threading.Thread(
            target=self._read_loop, name="computer-browser-worker-reader", daemon=True
        )
        reader.start()
        if not self._ready.wait(timeout=self._start_timeout):
            self.close(graceful_timeout=0.5)
            raise ComputerControlError(
                "computer_backend_start_failed",
                "受管浏览器执行进程启动超时",
            )
        if self._boot_error is not None:
            self.close(graceful_timeout=0.5)
            code, message = self._boot_error
            raise ComputerControlError(code, message)
        if self._dead.is_set():
            self.close(graceful_timeout=0.5)
            raise ComputerControlError(
                "computer_backend_start_failed",
                "受管浏览器执行进程启动后立即退出",
            )

    def request(
        self,
        op: str,
        payload: dict,
        timeout: float,
        *,
        before_send: Callable[[], None] | None = None,
    ) -> dict | None:
        """发送一个请求并等待响应；超时返回 None（由调用方决定封闭语义）。"""
        if self._dead.is_set():
            raise ComputerControlError(
                "computer_browser_closed", "受管浏览器执行进程已退出"
            )
        with self._stdin_lock:
            if before_send is not None:
                before_send()
            if self._dead.is_set():
                raise ComputerControlError(
                    "computer_browser_closed", "受管浏览器执行进程已退出"
                )
            with self._lock:
                self._seq += 1
                command_id = self._seq
                pending = _PendingCommand()
                self._pending[command_id] = pending
            try:
                line = _encode_worker_request(command_id, op, **payload)
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write((line + "\n").encode("utf-8"))
                self._proc.stdin.flush()
            except (OSError, ValueError) as error:
                with self._lock:
                    self._pending.pop(command_id, None)
                self._fail_pending_closed()
                raise ComputerControlError(
                    "computer_browser_closed",
                    f"受管浏览器执行进程管道不可用：{type(error).__name__}",
                ) from error
        if not pending.done.wait(timeout):
            with self._lock:
                self._pending.pop(command_id, None)
            return None
        if pending.error is not None:
            raise pending.error
        return pending.response

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def is_alive(self) -> bool:
        proc = self._proc
        return (
            not self._dead.is_set()
            and proc is not None
            and proc.poll() is None
        )

    def process_has_exited(self) -> bool:
        proc = self._proc
        return proc is None or proc.poll() is not None

    def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            for raw in proc.stdout:
                try:
                    message = _decode_worker_message(raw)
                except (ValueError, json.JSONDecodeError) as error:
                    _LOGGER.warning("受管执行进程返回不合法消息（已忽略）: %s", error)
                    continue
                if "event" in message:
                    self._handle_event(message)
                    continue
                command_id = message.get("id")
                with self._lock:
                    pending = self._pending.pop(command_id, None)
                if pending is not None:
                    pending.response = message
                    pending.done.set()
        finally:
            self._dead.set()
            self._ready.set()
            self._fail_pending_closed()

    def _handle_event(self, message: dict) -> None:
        event = message.get("event")
        if event == "ready":
            self.browser_pid = int(message.get("browser_pid") or 0) or None
            self.screenshot_dir = str(message.get("screenshot_dir") or "")
            self._ready.set()
        elif event == "boot_failed":
            self._boot_error = (
                str(message.get("error_code") or "computer_backend_start_failed"),
                str(message.get("error_message") or "受管执行进程启动失败"),
            )
            self._ready.set()
        elif event == "worker_protocol_error":
            _LOGGER.warning(
                "受管浏览器执行进程报告协议错误：%s",
                message.get("error_message"),
            )

    def _fail_pending_closed(self) -> None:
        with self._lock:
            pending_list = list(self._pending.values())
            self._pending.clear()
        for pending in pending_list:
            pending.error = ComputerControlError(
                "computer_browser_closed", "受管浏览器执行进程已退出"
            )
            pending.done.set()

    def close(self, graceful_timeout: float = _PROCESS_CLOSE_GRACEFUL) -> dict:
        """有界关闭：优雅 close → EOF → 硬终止 Job → 等待真实退出。

        返回诊断信息（是否硬终止、终止到退出的耗时）；绝不因子进程卡死而
        悬挂，也绝不在确认退出前释放调用方资源语义（调用方负责在其之后
        释放 profile/租约）。
        """
        stats = {"terminated": False, "exit_after_kill_ms": None}
        proc = self._proc
        if proc is None:
            self._close_job()
            return stats
        deadline = time.monotonic() + graceful_timeout
        if proc.poll() is None:
            try:
                self.request("close", {}, timeout=max(0.05, graceful_timeout))
            except ComputerControlError:
                pass
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except OSError:
                pass
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    pass
        if proc.poll() is None:
            stats["terminated"] = True
            kill_started = time.perf_counter()
            if self._job is not None:
                _terminate_job(self._job)
            elif sys.platform != "win32":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    try:
                        proc.kill()
                    except OSError:
                        pass
            try:
                proc.wait(timeout=_PROCESS_HARD_EXIT)
            except subprocess.TimeoutExpired:
                _LOGGER.error("受管执行进程在硬终止后仍未退出（%s）", proc.pid)
            if proc.poll() is None:
                try:
                    if sys.platform != "win32":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                    proc.wait(timeout=1.0)
                except OSError:
                    pass
            stats["exit_after_kill_ms"] = round(
                (time.perf_counter() - kill_started) * 1000, 1
            )
        self._dead.set()
        self._ready.set()
        self._fail_pending_closed()
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._close_job()
        return stats

    def _close_job(self) -> None:
        job, self._job = self._job, None
        if job is not None:
            _close_job_handle(job)


_JOB_HANDLE = None


def _create_kill_on_close_job():
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = _EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = 0x2000
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        _close_job_handle(job)
        return None
    return job


def _assign_process_to_job(job, process_handle: int) -> bool:
    import ctypes

    return bool(ctypes.windll.kernel32.AssignProcessToJobObject(job, process_handle))


def _terminate_job(job) -> None:
    import ctypes

    ctypes.windll.kernel32.TerminateJobObject(job, 1)


def _close_job_handle(job) -> None:
    import ctypes

    ctypes.windll.kernel32.CloseHandle(job)


def _encode_worker_request(command_id: int, op: str, **payload) -> str:
    from core.computer.browser_worker_protocol import encode_request

    return encode_request(command_id, op, **payload)


def _decode_worker_message(raw) -> dict:
    from core.computer.browser_worker_protocol import decode_response

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return decode_response(raw)


class BrowserComputerBackend:
    """有头 persistent context 浏览器 backend；线程模型见模块 docstring。"""

    target = "browser"

    def __init__(
        self,
        browser: str,
        profile_dir: Path,
        playwright_factory: Callable | None = None,
        *,
        command_timeout: float = _DEFAULT_COMMAND_TIMEOUT,
        start_timeout: float = _DEFAULT_START_TIMEOUT,
        on_page_change: Callable[[], None] | None = None,
        window_hint_provider: Callable | None = None,
        use_process_worker: bool = False,
        worker_command: list[str] | None = None,
        process_guard_slack: float = _PROCESS_GUARD_SLACK,
        viewport: tuple[int, int] = (_VIEWPORT_WIDTH, _VIEWPORT_HEIGHT),
    ) -> None:
        # 构造函数只校验参数，绝不导入或启动 Playwright。
        # "embedded" 仅由 EmbeddedBrowserBackend（CDP 直连内置 WebView2）使用：
        # 不在 _CHANNELS 中，其启动/收尾分支由子类完全接管。
        if type(browser) is not str or browser not in (*_CHANNELS, "embedded"):
            raise ComputerControlError(
                "computer_invalid_config",
                f"不支持的浏览器：{browser!r}，只支持 edge、chrome 或 chromium",
            )
        if not isinstance(profile_dir, Path):
            raise ComputerControlError("computer_invalid_config", "profile_dir 必须是 pathlib.Path")
        self._browser = browser
        self._profile_dir = profile_dir
        self._playwright_factory = playwright_factory
        self._command_timeout = float(command_timeout)
        self._start_timeout = float(start_timeout)
        self._on_page_change = on_page_change
        self._viewport_width, self._viewport_height = (int(viewport[0]), int(viewport[1]))
        # 是否拥有 context 的关闭权：外部浏览器 owned（teardown 关 context）；
        # CDP 直连内置浏览器时浏览器归 GUI 所有，teardown 绝不触碰 context。
        self._owns_context = True
        self._window_hint_provider = window_hint_provider
        self._browser_pid: int | None = None
        # R2 第二段：执行器隔离边界。进程模式把 Playwright 全部对象移入受管
        # 子进程（父进程卡死命令可在有界时间内硬终止）；线程模式保留给
        # 注入 fake 的测试与非冻结环境的降级路径。
        self._use_process_worker = bool(use_process_worker)
        self._worker_command_override = worker_command
        self._process_guard_slack = float(process_guard_slack)
        self._transport: _WorkerProcessTransport | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=_COMMAND_QUEUE_SIZE)
        self._state_lock = threading.Lock()
        self._submit_lock = threading.Lock()
        self._admission_lock = threading.Lock()
        self._ready = threading.Event()
        self._worker_stop = threading.Event()
        self._current_deadline: float | None = None
        self._worker: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._failed = False
        self._stop_requested = False
        self._stop_error_code = ""
        self._stop_error_message = ""
        self._shutdown_sentinel_sent = False
        self._command_seq = 0
        self._boot_error: ComputerControlError | None = None
        # 以下字段只由 worker 线程读写（_active_page 同时作为测试只读 seam）。
        self._runtime = None
        self._playwright = None
        self._context = None
        self._active_page = None
        self._context_closed = False
        self._page_tokens: dict[int, int] = {}
        self._page_serial = 0
        self._frame_tokens: dict[int, int] = {}
        self._frame_serial = 0
        self._observed_page_token: int | None = None
        self._observed_element_refs: frozenset[str] = frozenset()
        self._observed_url: str = ""
        self._surface_generation = 0
        self._viewport_generation = 0
        self._last_viewport_size: tuple[int, int] | None = None
        self._frame_seq = 0
        self._frames: dict[str, ObservationFrame] = {}
        self._active_frame: ObservationFrame | None = None
        self._run_ref_scope: str = ""
        self._ref_seq = 0
        self._obs_seq = 0
        self._ref_by_identity: dict[tuple, str] = {}
        self._active_refs: dict[str, _BrowserElementRecord] = {}
        self._pressed_buttons: set[str] = set()
        self._pressed_keys: list = []
        self._profile_lock_key: str | None = None
        self._screenshot_dir: Path | None = None
        self._screenshot_counter = 0
        self._pending_region: tuple[int, int, int, int] | None = None
        self._coord_frame: tuple[int, int, int, int, int, int] | None = None
        # 当前观察是否为局部视图（区域图不放大，尺寸与区域 CSS 相同，不能用
        # 尺寸差异判断，必须单独记标志）。
        self._region_view = False

    @property
    def browser(self) -> str:
        return self._browser

    @property
    def active_page(self):
        return self._active_page

    def is_reusable(self) -> bool:
        """retained 领取裁决（R4）：未封闭、worker 存活、无 pending 命令、窗口未被关闭。"""
        with self._state_lock:
            transport = self._transport
            if transport is not None:
                worker = transport
                alive = transport.is_alive() and transport.pending_count() == 0
            else:
                worker = self._worker
                alive = worker is not None and worker.is_alive()
            if (
                not self._started
                or self._closed
                or self._failed
                or self._stop_requested
                or not alive
            ):
                return False
        if transport is None and self._context_closed:
            return False
        if transport is None:
            return self._queue.empty()
        return True

    def start(self) -> None:
        if self._use_process_worker:
            self._start_process_worker()
            return
        self._start_thread_worker()

    def _default_worker_command(self) -> list[str]:
        if self._worker_command_override is not None:
            return list(self._worker_command_override)
        viewport_args = [
            "--viewport-width",
            str(self._viewport_width),
            "--viewport-height",
            str(self._viewport_height),
        ]
        if getattr(sys, "frozen", False):
            return [
                sys.executable,
                WORKER_ARGV_FLAG,
                "--browser",
                self._browser,
                "--profile",
                str(self._profile_dir),
                "--command-timeout",
                str(self._command_timeout),
                *viewport_args,
            ]
        return [
            sys.executable,
            "-m",
            "core.computer.browser_worker",
            "--browser",
            self._browser,
            "--profile",
            str(self._profile_dir),
            "--command-timeout",
            str(self._command_timeout),
            *viewport_args,
        ]

    def _start_process_worker(self) -> None:
        with self._state_lock:
            if self._started:
                return
            if self._closed:
                raise ComputerControlError(
                    "computer_browser_closed",
                    "浏览器 backend 已关闭，请创建新实例",
                )
        # 父进程侧进程内 profile 注册表先行占位（同父进程双实例快速失败）；
        # 真实 Chromium profile 锁仍由子进程的 launch 兜底。
        resolved = str(self._profile_dir.resolve())
        self._acquire_profile_lock(resolved)
        transport = _WorkerProcessTransport(
            self._default_worker_command(),
            start_timeout=self._start_timeout,
            guard_timeout=self._command_timeout + self._process_guard_slack,
        )
        try:
            transport.start()
        except Exception:
            self._release_profile_lock(resolved)
            raise
        with self._state_lock:
            self._transport = transport
            self._started = True
            self._browser_pid = transport.browser_pid

    def _start_thread_worker(self) -> None:
        with self._state_lock:
            if self._started:
                return
            if self._closed:
                raise ComputerControlError(
                    "computer_browser_closed",
                    "浏览器 backend 已关闭，请创建新实例",
                )
            thread = threading.Thread(
                target=self._worker_main,
                name=f"computer-browser-{self._browser}",
                daemon=True,
            )
            self._worker = thread
            thread.start()
        if not self._ready.wait(timeout=self._start_timeout):
            with self._state_lock:
                self._closed = True
            # 超时后 worker 可能仍在 _boot（慢浏览器启动）：投递关闭哨兵，
            # 让 boot 无论成败都能立即收尾（成功→命令循环取到哨兵退出并
            # teardown；失败→boot 异常路径自带 teardown）——绝不遗留浏览器
            # 进程、profile 注册表锁与临时截图目录（Task 6 M1 复核，Task 16）。
            try:
                self._queue.put(
                    _Command(self._next_command_id(), None, float("inf")), timeout=5
                )
            except queue.Full:
                _LOGGER.warning("浏览器启动超时，关闭哨兵投递失败（worker 可能残留）")
            raise ComputerControlError(
                "computer_backend_start_failed",
                f"{self._browser} 浏览器启动超时",
            )
        if self._boot_error is not None:
            with self._state_lock:
                self._closed = True
            thread.join(timeout=10)
            raise self._boot_error
        with self._state_lock:
            self._started = True

    def observe(self, run_id: str) -> ComputerObservation:
        if self._transport is not None:
            response = self._remote_call("observe", {"run_id": run_id}, "observe")
            from core.computer.browser_worker_protocol import (
                normalize_observation_payload,
            )

            return ComputerObservation(
                **normalize_observation_payload(response["observation"])
            )
        return self._call("observe", lambda: self._cmd_observe(run_id))

    def execute(self, run_id: str, action: ComputerAction) -> None:
        if self._transport is not None:
            from dataclasses import asdict

            from core.computer.browser_worker_protocol import normalize_action_payload

            self._remote_call(
                "execute",
                {
                    "run_id": run_id,
                    "action": normalize_action_payload(asdict(action)),
                },
                f"execute:{action.kind}",
            )
            return
        self._call(f"execute:{action.kind}", lambda: self._cmd_execute(run_id, action))

    def sync_observation_frame(self, frame: ObservationFrame) -> None:
        if not frame.frame_id:
            return
        if self._transport is not None:
            from core.computer.browser_worker_protocol import frame_payload

            self._remote_call(
                "sync_frame",
                {"frame": frame_payload(frame)},
                "sync_frame",
            )
            return
        self._call("sync_frame", lambda: self._cmd_sync_frame(frame))

    def _remote_call(self, op: str, payload: dict, description: str) -> dict:
        with self._state_lock:
            admission = self._admission_error()
            transport = self._transport
        if admission is not None:
            raise admission
        if transport is None:
            raise ComputerControlError(
                "computer_browser_unavailable", "受管浏览器执行进程尚未启动"
            )

        def check_before_send() -> None:
            # 写管道前于 _admission_lock + _state_lock 下复核准入，阻止与
            # request_stop/close 并发跨越提交栅栏的命令落盘。
            with self._admission_lock:
                with self._state_lock:
                    err = self._admission_error()
                if err is not None:
                    raise err

        response = transport.request(
            op,
            payload,
            timeout=self._command_timeout + self._process_guard_slack,
            before_send=check_before_send,
        )
        if response is None:
            with self._state_lock:
                self._failed = True
            raise ComputerControlError(
                "computer_action_timeout",
                f"受管浏览器执行进程命令超时且结果未知（{description}）；"
                "该实例已封闭，拒绝后续输入，停止后可收回执行权",
            )
        if response.get("ok") is False:
            raise ComputerControlError(
                str(response.get("error_code") or "computer_backend_error"),
                str(response.get("error_message") or "受管执行进程返回错误"),
            )
        return response

    def request_stop(
        self,
        *,
        error_code: str = "computer_cancelled",
        message: str = "电脑操控正在停止，排队命令已撤销",
    ) -> None:
        """Seal action admission and cancel commands that have not reached the worker."""
        with self._admission_lock:
            with self._state_lock:
                if self._closed:
                    return
                already_stopped = self._stop_requested
                if not already_stopped or error_code == "computer_browser_closed":
                    self._stop_error_code = error_code
                    self._stop_error_message = message
                if already_stopped:
                    return
                self._stop_requested = True
            self._worker_stop.set()
        if self._transport is not None:
            return
        with self._submit_lock:
            with self._queue.mutex:
                pending = list(self._queue.queue)
                self._queue.queue.clear()
            for command in pending:
                command.cancel_if_queued(
                    error_code, message
                )
                self._queue.task_done()

    def release_inputs(self) -> None:
        """尽力释放 worker 内跟踪的按下状态；backend 不可用时静默 no-op。"""
        if self._transport is not None:
            with self._state_lock:
                transport = self._transport
            if transport is None or not transport.is_alive():
                return
            try:
                transport.request(
                    "release", {}, timeout=min(self._command_timeout, 2.0)
                )
            except ComputerControlError:
                _LOGGER.warning("释放浏览器输入状态失败", exc_info=True)
            return
        with self._state_lock:
            worker = self._worker
            available = (
                self._started
                and not self._closed
                and not self._failed
                and worker is not None
                and worker.is_alive()
            )
        if not available:
            return
        try:
            # 释放是停止路径上的尽力清理：使用有界短预算，绝不在卡死的
            # worker 后面排队一整个命令超时周期。
            self._call(
                "release_inputs",
                self._cmd_release_inputs,
                timeout=min(self._command_timeout, 5.0),
                allow_stopping=True,
            )
        except ComputerControlError:
            _LOGGER.warning("释放浏览器输入状态失败", exc_info=True)

    def close(self) -> None:
        self.request_stop(
            error_code="computer_browser_closed",
            message="浏览器 backend 已关闭，排队命令已撤销",
        )
        if self._transport is not None:
            with self._state_lock:
                transport = self._transport
                if transport is None or not self._started:
                    return
            try:
                transport.request("release", {}, timeout=min(self._command_timeout, 2.0))
            except ComputerControlError:
                _LOGGER.warning("停止时释放受管浏览器输入失败；随后确认执行进程退出", exc_info=True)
            with self._state_lock:
                if self._transport is not transport:
                    return
                self._closed = True
            screenshot_dir = transport.screenshot_dir
            stats = transport.close(graceful_timeout=_PROCESS_CLOSE_GRACEFUL)
            if not transport.process_has_exited():
                raise ComputerControlError(
                    "computer_stop_unconfirmed",
                    "浏览器执行进程未确认退出；继续保留 profile 与操控租约",
                )
            with self._state_lock:
                if self._transport is transport:
                    self._transport = None
            if stats.get("terminated"):
                _LOGGER.info(
                    "受管浏览器执行进程经硬终止收尾（终止到退出 %sms）",
                    stats.get("exit_after_kill_ms"),
                )
            if screenshot_dir:
                shutil.rmtree(screenshot_dir, ignore_errors=True)
            self._release_profile_lock(str(self._profile_dir.resolve()))
            return
        with self._state_lock:
            if not self._started:
                # 幂等；未启动时 no-op，不消耗生命周期。
                return
            worker = self._worker
            if self._closed and (worker is None or not worker.is_alive()):
                return
            need_shutdown_enqueue = not self._shutdown_sentinel_sent
            self._closed = True
            self._shutdown_sentinel_sent = True
        self._worker_stop.set()
        if need_shutdown_enqueue:
            # 撤销全部非停止释放的排队命令，并在留存 release_inputs 之后投递退出哨兵。
            with self._submit_lock:
                with self._queue.mutex:
                    pending = list(self._queue.queue)
                    self._queue.queue.clear()
                release_commands = []
                for command in pending:
                    self._queue.task_done()
                    if command.allow_stopping:
                        release_commands.append(command)
                    else:
                        command.cancel_if_queued(
                            "computer_browser_closed", "浏览器 backend 已关闭，排队命令已撤销"
                        )
                for command in release_commands:
                    self._queue.put_nowait(command)
                try:
                    self._queue.put_nowait(
                        _Command(self._next_command_id(), None, float("inf"))
                    )
                except queue.Full:
                    _LOGGER.warning("浏览器 worker 命令队列已满，关闭哨兵投递失败")
        if worker.is_alive():
            worker.join(timeout=_THREAD_CLOSE_TIMEOUT)
            if worker.is_alive():
                raise ComputerControlError(
                    "computer_stop_unconfirmed",
                    "浏览器 worker 未确认退出；继续保留操控租约",
                )

    def _worker_main(self) -> None:
        try:
            self._boot()
        except BaseException as error:
            if isinstance(error, ComputerControlError):
                self._boot_error = error
            else:
                _LOGGER.exception("浏览器 backend worker 启动失败")
                self._boot_error = ComputerControlError(
                    "computer_backend_start_failed",
                    f"浏览器 backend 启动失败：{type(error).__name__}",
                )
            self._ready.set()
            self._teardown()
            return
        self._ready.set()
        try:
            self._command_loop()
        finally:
            self._teardown()

    def _boot(self) -> None:
        try:
            self._profile_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ComputerControlError(
                "computer_backend_start_failed",
                f"无法创建浏览器 profile 目录：{error}",
            ) from error
        resolved = str(self._profile_dir.resolve())
        self._acquire_profile_lock(resolved)
        self._profile_lock_key = resolved
        factory = self._playwright_factory
        if factory is None:
            from core.components import ComponentError, ensure_playwright

            try:
                ensure_playwright()
            except ComponentError as error:
                raise ComputerControlError(
                    "playwright_component_missing", str(error)
                ) from error
            from playwright.sync_api import sync_playwright as factory
        try:
            self._runtime = factory()
            self._playwright = self._runtime.start()
        except ComputerControlError:
            raise
        except Exception as error:
            raise ComputerControlError(
                "computer_backend_start_failed",
                f"Playwright runtime 启动失败：{type(error).__name__}",
            ) from error
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                **self._persistent_context_kwargs(resolved)
            )
        except Exception as error:
            raise self._map_launch_error(error) from error
        self._screenshot_dir = Path(tempfile.mkdtemp(prefix="ycode-computer-browser-"))
        context = self._context
        pages = [page for page in context.pages if not page.is_closed()]
        if pages:
            self._active_page = pages[-1]
        else:
            page = context.new_page()
            page.goto("about:blank")
            self._active_page = page
        # 初始 page 落定后再挂事件，避免启动过程误报页面变化。
        context.on("page", self._on_new_page)
        context.on("close", self._on_context_close)
        # R5：解析受控浏览器主进程 PID（系统窗口标题提示的前提）；失败仅降级
        # 为 DOM 可见性裁决，绝不影响启动。
        try:
            self._browser_pid = self._resolve_browser_pid()
        except Exception:
            _LOGGER.warning("解析受控浏览器进程 PID 失败，退化为 DOM 可见性裁决", exc_info=True)
            self._browser_pid = None

    def _persistent_context_kwargs(self, user_data_dir: str) -> dict:
        kwargs = {
            "user_data_dir": user_data_dir,
            "headless": resolve_headless(),
            "viewport": {"width": self._viewport_width, "height": self._viewport_height},
            "device_scale_factor": 1,
        }
        channel = _CHANNELS[self._browser]
        if channel:
            kwargs["channel"] = channel
        if sys.platform != "win32":
            kwargs["args"] = ["--no-sandbox", "--disable-dev-shm-usage"]
        return kwargs

    def _resolve_browser_pid(self) -> int | None:
        import psutil

        if sys.platform == "win32":
            exe_names = {"edge": {"msedge.exe"}, "chrome": {"chrome.exe"}, "chromium": {"chrome.exe"}}
            profile_key = str(self._profile_dir.resolve()).lower().replace("/", "\\")
        else:
            exe_names = {
                "edge": {"msedge"},
                "chrome": {"chrome", "chromium", "headless_shell"},
                "chromium": {"chrome", "chromium", "headless_shell", "google-chrome"},
            }
            profile_key = os.path.normcase(str(self._profile_dir.resolve()))
        allowed_names = exe_names[self._browser]
        matches = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = os.path.normcase(str(proc.info.get("name") or ""))
                if sys.platform == "win32":
                    name = name.lower()
                if name not in allowed_names:
                    continue
                cmdline = [str(arg) for arg in (proc.info.get("cmdline") or [])]
                if any(arg.startswith("--type=") for arg in cmdline):
                    continue
                if any(
                    arg.startswith("--user-data-dir=")
                    and profile_key in (
                        arg.lower().replace("/", "\\") if sys.platform == "win32" else os.path.normcase(arg)
                    )
                    for arg in cmdline
                ):
                    matches.append(int(proc.info["pid"]))
            except Exception:
                continue
        if len(matches) == 1:
            return matches[0]
        if matches:
            _LOGGER.warning("受控浏览器主进程不唯一（%s），窗口提示不可用", matches)
        return None

    def _map_launch_error(self, error: Exception) -> ComputerControlError:
        message = str(error).lower()
        if any(hint in message for hint in _PROFILE_LOCK_HINTS):
            return ComputerControlError(
                "computer_profile_locked",
                f"浏览器 profile 被其他进程占用：{self._profile_dir}",
            )
        return ComputerControlError(
            "computer_browser_unavailable",
            f"无法启动所选浏览器 {self._browser}（channel {_CHANNELS[self._browser]}），"
            "请确认已安装；不会自动改用其他浏览器",
        )

    def _command_loop(self) -> None:
        while True:
            command = self._queue.get()
            try:
                if command.fn is None:
                    if self._pressed_buttons or self._pressed_keys:
                        self._cmd_release_inputs()
                    return
                # Seal-and-claim are mutually exclusive: a queued action cannot
                # cross the worker start boundary after stop admission closes.
                with self._admission_lock:
                    with self._state_lock:
                        stopping = self._stop_requested
                    if stopping and not command.allow_stopping:
                        command.cancel_if_queued(
                            "computer_cancelled", "电脑操控已停止，排队动作未执行"
                        )
                        continue
                    if not command.try_start(time.monotonic()):
                        continue
                self._current_deadline = command.deadline
                try:
                    command.result = command.fn()
                except BaseException as error:
                    command.error = error
                finally:
                    self._current_deadline = None
                    command.finish()
            finally:
                self._queue.task_done()

    def _teardown(self) -> None:
        context = self._context
        self._context = None
        if context is not None and self._owns_context:
            try:
                context.close()
            except Exception:
                _LOGGER.exception("关闭浏览器 context 失败")
        playwright = self._playwright
        self._playwright = None
        self._runtime = None
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                _LOGGER.exception("停止 Playwright runtime 失败")
        if self._profile_lock_key is not None:
            self._release_profile_lock(self._profile_lock_key)
            self._profile_lock_key = None
        if self._screenshot_dir is not None:
            shutil.rmtree(self._screenshot_dir, ignore_errors=True)
            self._screenshot_dir = None
        self._pressed_buttons = set()
        self._pressed_keys = []
        self._page_tokens = {}
        self._frame_tokens = {}
        self._observed_page_token = None
        self._observed_element_refs = frozenset()
        self._observed_url = ""
        self._frames = {}
        self._active_frame = None
        self._run_ref_scope = ""
        self._ref_by_identity = {}
        self._active_refs = {}
        self._last_viewport_size = None
        self._active_page = None

    def _on_new_page(self, page) -> None:
        self._page_token(page)
        callback = self._on_page_change
        if callback is not None:
            try:
                callback()
            except Exception:
                _LOGGER.exception("浏览器页面变化事件回调失败")

    def _on_context_close(self, *args) -> None:
        self._context_closed = True

    def _page_token(self, page) -> int:
        token = self._page_tokens.get(id(page))
        if token is None:
            self._page_serial += 1
            token = self._page_serial
            self._page_tokens[id(page)] = token
        return token

    def _frame_token(self, page, frame) -> str:
        if frame is page:
            return "frame-main"
        main_frame = getattr(page, "main_frame", None)
        if main_frame is not None and frame is main_frame:
            return "frame-main"
        token = self._frame_tokens.get(id(frame))
        if token is None:
            self._frame_serial += 1
            token = self._frame_serial
            self._frame_tokens[id(frame)] = token
        return f"frame-{token}"

    def _reset_run_scope(self, run_id: str) -> None:
        if run_id == self._run_ref_scope:
            return
        self._run_ref_scope = run_id
        self._ref_seq = 0
        self._obs_seq = 0
        self._frame_seq = 0
        self._ref_by_identity = {}
        self._active_refs = {}
        self._observed_element_refs = frozenset()
        self._frames = {}
        self._active_frame = None

    def _cmd_sync_frame(self, frame: ObservationFrame) -> None:
        if not frame.frame_id:
            return
        self._frames[frame.frame_id] = frame
        while len(self._frames) > 32:
            self._frames.pop(next(iter(self._frames)))
        if self._active_frame is not None and self._active_frame.frame_id == frame.frame_id:
            self._active_frame = frame
            if (
                frame.coordinate_input_enabled
                and frame.image_width > 0
                and frame.image_height > 0
                and frame.crop_width > 0
                and frame.crop_height > 0
            ):
                self._coord_frame = (
                    int(frame.origin_x),
                    int(frame.origin_y),
                    int(frame.crop_width),
                    int(frame.crop_height),
                    int(frame.image_width),
                    int(frame.image_height),
                )

    def _current_viewport_generation(self, page) -> int:
        viewport = self._observation_viewport(page)
        cur_vp = (int(viewport["width"]), int(viewport["height"]))
        if self._last_viewport_size != cur_vp:
            self._viewport_generation += 1
            self._last_viewport_size = cur_vp
        return self._viewport_generation

    def _require_live_viewport(self, page) -> None:
        active = self._active_frame
        expected_vp = (
            int(active.viewport_generation)
            if active is not None
            else self._viewport_generation
        )
        current_vp = self._current_viewport_generation(page)
        if active is not None and expected_vp and current_vp != expected_vp:
            raise ComputerControlError(
                "computer_surface_changed",
                "浏览器视口尺寸已变化，旧坐标与元素引用已失效，请重新 observe",
            )

    def _probe_page_visibility(self, page) -> tuple[bool, bool]:
        try:
            result = page.evaluate(_VISIBILITY_PROBE)
        except Exception:
            return False, False
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            return False, False
        return result[0] == "visible", bool(result[1])

    def _resolve_active_page(self):
        if self._context_closed or self._context is None:
            raise ComputerControlError(
                "computer_browser_closed",
                "浏览器窗口已被用户关闭，不会自动重开",
            )
        open_pages = []
        alive_ids = set()
        for candidate in list(self._context.pages):
            try:
                if not candidate.is_closed():
                    open_pages.append(candidate)
                    alive_ids.add(id(candidate))
            except Exception:
                continue
        # 清理已消失 page 的身份映射，避免 id 复用串号。
        for stale in [key for key in self._page_tokens if key not in alive_ids]:
            del self._page_tokens[stale]
        if not open_pages:
            raise ComputerControlError(
                "computer_browser_closed",
                "浏览器页面已全部关闭，不会自动重开",
            )
        if len(open_pages) == 1:
            self._active_page = open_pages[0]
            return open_pages[0]
        probed = [(page, *self._probe_page_visibility(page)) for page in open_pages]
        any_hidden = any(not visible for _page, visible, _f in probed)
        focused = [page for page, visible, is_focused in probed if visible and is_focused]
        if len(focused) == 1:
            self._active_page = focused[0]
            return focused[0]
        if not focused and any_hidden:
            visible = [page for page, is_visible, _f in probed if is_visible]
            if len(visible) == 1:
                self._active_page = visible[0]
                return visible[0]
        # DOM 信号不可区分（实测 Playwright 默认启动参数下标签切换后全部页
        # 仍报 visible+focused）或存在矛盾：用系统侧窗口标题提示交叉裁决。
        chosen = self._resolve_via_window_hints(open_pages)
        self._active_page = chosen
        return chosen

    def _resolve_via_window_hints(self, open_pages):
        """系统侧裁决：受控浏览器进程可见窗口标题前缀匹配页面标题。

        前台窗口属于受控浏览器 → 其标题对应的页面即前台页；前台属于其他
        应用且本浏览器仅一个可见窗口 → 该窗口页面。标题缺失/多页同标题/
        多窗口均可见 → computer_surface_ambiguous，绝不任意选页继续。
        """
        provider = self._window_hint_provider
        pid = self._browser_pid
        if provider is None or not pid:
            raise ComputerControlError(
                "computer_surface_ambiguous",
                "多个受控页面无法由页面可见性唯一确定前台页"
                "（系统侧窗口提示不可用），请重新观察",
            )

        def fetch_hints():
            try:
                return provider(pid)
            except ComputerControlError:
                raise
            except Exception as error:
                raise ComputerControlError(
                    "computer_surface_ambiguous",
                    f"读取受控浏览器窗口提示失败：{type(error).__name__}",
                ) from error

        deadline = time.monotonic() + _WINDOW_HINT_SETTLE_SECONDS
        while True:
            hints = fetch_hints()
            if not hints:
                # 浏览器窗口不可见（最小化等）：回落既有约定。
                current = self._active_page
                if current is not None and any(current is page for page in open_pages):
                    return current
                return open_pages[-1]
            titles = {}
            for page in open_pages:
                try:
                    title = page.title()
                except Exception:
                    title = ""
                titles[id(page)] = title if isinstance(title, str) else ""
            matched_by_hwnd = {}
            for hwnd, window_title, _is_foreground in hints:
                matched = [
                    page
                    for page in open_pages
                    if titles[id(page)] and window_title.startswith(titles[id(page)])
                ]
                if not matched:
                    untitled = [page for page in open_pages if not titles[id(page)]]
                    if len(untitled) == 1:
                        matched = untitled
                matched_by_hwnd[hwnd] = matched
            foreground = [entry[0] for entry in hints if entry[2]]
            if foreground:
                matched = matched_by_hwnd[foreground[0]]
                if len(matched) == 1:
                    return matched[0]
                if matched:
                    raise ComputerControlError(
                        "computer_surface_ambiguous",
                        "前台浏览器窗口无法唯一匹配受控页面（同标题多页或标题缺失），请重新观察",
                    )
            elif len(hints) == 1:
                matched = matched_by_hwnd[hints[0][0]]
                if len(matched) == 1:
                    return matched[0]
            if time.monotonic() >= deadline:
                raise ComputerControlError(
                    "computer_surface_ambiguous",
                    "多个浏览器窗口均可见且无法确定前台页，无法唯一确定操作目标",
                )
            time.sleep(0.05)

    def _require_observed_surface(self, page) -> None:
        if self._observed_page_token is None:
            return
        if self._page_token(page) != self._observed_page_token:
            raise ComputerControlError(
                "computer_surface_changed",
                "受控页面已切换，旧坐标已失效，必须重新观察（observe）后再操作",
            )
        self._require_live_viewport(page)

    def _read_doc_token(self, frame) -> str:
        try:
            token = frame.evaluate(_DOC_TOKEN_SCRIPT)
            return str(token) if isinstance(token, str) else ""
        except Exception:
            return ""

    def _require_ref_valid(self, page, element_ref: str) -> _BrowserElementRecord:
        if self._observed_page_token is None:
            raise ComputerControlError(
                "computer_element_stale",
                "尚未观察页面，请先 observe 再操作元素",
            )
        if self._page_token(page) != self._observed_page_token:
            raise ComputerControlError(
                "computer_element_stale",
                "页面已切换，元素引用失效，请重新 observe",
            )
        self._require_live_viewport(page)
        if page.url != self._observed_url:
            raise ComputerControlError(
                "computer_element_stale",
                "页面地址已变化，元素引用失效，请重新 observe",
            )
        record = self._active_refs.get(element_ref)
        if record is None:
            raise ComputerControlError(
                "computer_element_stale",
                f"元素引用 {element_ref} 已失效，请重新 observe",
            )
        frames = getattr(page, "frames", None)
        if isinstance(frames, (list, tuple)) and record.frame is not page:
            if record.frame not in list(frames):
                raise ComputerControlError(
                    "computer_element_stale",
                    f"元素 {element_ref} 所属框架已不存在，请重新 observe",
                )
            is_detached = getattr(record.frame, "is_detached", None)
            if callable(is_detached) and is_detached():
                raise ComputerControlError(
                    "computer_element_stale",
                    f"元素 {element_ref} 所属框架已卸载，请重新 observe",
                )
        if record.document_generation:
            live_doc = self._read_doc_token(record.frame)
            if live_doc != record.document_generation:
                raise ComputerControlError(
                    "computer_element_stale",
                    f"文档或框架已重新加载，元素引用 {element_ref} 已失效，请重新 observe",
                )
        return record

    def _resolve_frame_offset(
        self, page, frame, view_w: int, view_h: int
    ) -> tuple[int, int, tuple[int, int, int, int]] | None:
        main_frame = getattr(page, "main_frame", None)
        if frame is page or (main_frame is not None and frame is main_frame):
            return 0, 0, (0, 0, view_w, view_h)
        chain: list = []
        cur = frame
        while cur is not None and cur is not main_frame and len(chain) < 16:
            parent = getattr(cur, "parent_frame", None)
            if parent is None:
                return None
            chain.append((parent, cur))
            cur = parent
        if cur is not main_frame:
            return None
        ox, oy = 0.0, 0.0
        clip = [0.0, 0.0, float(view_w), float(view_h)]
        for parent, child in reversed(chain):
            try:
                is_detached = getattr(child, "is_detached", None)
                if callable(is_detached) and is_detached():
                    return None
                fe = child.frame_element()
                if fe is None:
                    return None
                info = parent.evaluate(_FRAME_OFFSET_SCRIPT, fe)
            except Exception:
                return None
            if not isinstance(info, dict):
                return None
            fx = ox + float(info.get("x") or 0.0)
            fy = oy + float(info.get("y") or 0.0)
            fw = float(info.get("w") or 0.0)
            fh = float(info.get("h") or 0.0)
            if fw <= 0 or fh <= 0:
                return None
            clip = [
                max(clip[0], fx),
                max(clip[1], fy),
                min(clip[2], fx + fw),
                min(clip[3], fy + fh),
            ]
            if clip[2] <= clip[0] or clip[3] <= clip[1]:
                return None
            ox, oy = fx, fy
        return round(ox), round(oy), (round(clip[0]), round(clip[1]), round(clip[2]), round(clip[3]))

    def _collect_interactive_elements(
        self,
        page,
        *,
        run_id: str = "",
        page_token: int = 0,
        obs_gen: int = 0,
        viewport_gen: int = 0,
        view_w: int = _VIEWPORT_WIDTH,
        view_h: int = _VIEWPORT_HEIGHT,
    ) -> tuple[tuple, dict[str, _BrowserElementRecord]]:
        frames_attr = getattr(page, "frames", None)
        frames = (
            list(frames_attr)
            if isinstance(frames_attr, (list, tuple)) and frames_attr
            else [page]
        )
        is_fake_stub = hasattr(page, "element_collect_result")
        listing: list[dict] = []
        records: dict[str, _BrowserElementRecord] = {}
        for frame in frames:
            offset_info = self._resolve_frame_offset(page, frame, view_w, view_h)
            if offset_info is None:
                continue
            offset_x, offset_y, clip = offset_info
            try:
                raw = frame.evaluate(ELEMENT_COLLECT_SCRIPT)
            except Exception:
                _LOGGER.warning("浏览器元素收集失败，本次观察回落截图路径", exc_info=True)
                continue
            if not isinstance(raw, (list, tuple)):
                _LOGGER.warning(
                    "浏览器元素收集返回非列表（%s），本次观察回落截图路径",
                    type(raw).__name__,
                )
                continue
            frame_id = self._frame_token(page, frame)
            doc_token = self._read_doc_token(frame)
            for item in raw:
                if not isinstance(item, dict):
                    continue
                local_id = item.get("ref")
                if not isinstance(local_id, str) or not local_id:
                    continue
                kind = item.get("kind")
                name = item.get("name")
                enabled = item.get("enabled")
                raw_bounds = _normalize_element_bounds(item.get("bounds"))
                bounds = [
                    raw_bounds[0] + offset_x,
                    raw_bounds[1] + offset_y,
                    raw_bounds[2] + offset_x,
                    raw_bounds[3] + offset_y,
                ]
                if len(frames) > 1 and (
                    bounds[2] <= clip[0]
                    or bounds[3] <= clip[1]
                    or bounds[0] >= clip[2]
                    or bounds[1] >= clip[3]
                ):
                    continue
                if is_fake_stub:
                    public_ref = local_id
                else:
                    identity = (run_id, page_token, frame_id, doc_token, local_id)
                    public_ref = self._ref_by_identity.get(identity)
                    if public_ref is None:
                        self._ref_seq += 1
                        public_ref = f"el-{self._ref_seq}"
                        self._ref_by_identity[identity] = public_ref
                listing.append({
                    "ref": public_ref,
                    "kind": kind if isinstance(kind, str) else "",
                    "name": (name if isinstance(name, str) else "")[:_ELEMENT_MAX_NAME_CHARS],
                    "bounds": bounds,
                    "enabled": enabled if isinstance(enabled, bool) else True,
                })
                records[public_ref] = _BrowserElementRecord(
                    ref=public_ref,
                    run_id=run_id,
                    page_token=page_token,
                    frame_id=frame_id,
                    frame=frame,
                    document_generation=doc_token,
                    observation_generation=obs_gen,
                    viewport_generation=viewport_gen,
                    local_id=local_id,
                    offset_x=offset_x,
                    offset_y=offset_y,
                    bounds=tuple(bounds),
                    kind=kind if isinstance(kind, str) else "",
                )
        return tuple(listing), records

    def _collect_page_text(self, page) -> str:
        try:
            raw = page.evaluate(PAGE_TEXT_SCRIPT)
        except Exception:
            _LOGGER.warning("浏览器页面正文读取失败，本次观察仅保留元素与截图", exc_info=True)
            return ""
        if not isinstance(raw, str):
            _LOGGER.warning(
                "浏览器页面正文返回非字符串（%s），按空正文处理", type(raw).__name__
            )
            return ""
        return raw[:_PAGE_TEXT_MAX_CHARS]

    def _set_pending_region(self, page, action: ComputerAction) -> None:
        viewport = self._observation_viewport(page)
        view_w = int(viewport["width"])
        view_h = int(viewport["height"])
        x = action.x
        y = action.y
        width = action.width
        height = action.height
        if not all(type(value) is int for value in (x, y, width, height)):
            raise ComputerControlError(
                "computer_invalid_action", "局部观察需要整数 x/y/width/height"
            )
        if width <= 0 or height <= 0:
            raise ComputerControlError(
                "computer_invalid_action", "局部观察的 width/height 必须是正整数"
            )
        if x < 0 or y < 0 or x + width > view_w or y + height > view_h:
            raise ComputerControlError(
                "computer_coordinate_out_of_bounds",
                f"局部观察区域 ({x},{y},{width}x{height}) 超出浏览器视口 "
                f"{view_w}x{view_h}，拒绝裁剪（不做 clamp）",
            )
        self._pending_region = (x, y, width, height)

    def _build_region_frame(
        self, png_bytes: bytes, region: tuple[int, int, int, int]
    ) -> tuple[bytes, dict]:
        try:
            import io

            from PIL import Image
        except Exception as error:  # noqa: BLE001
            raise ComputerControlError(
                "computer_backend_error",
                "局部观察需要 Pillow 处理截图（未安装或导入失败）",
            ) from error
        x, y, width, height = region
        try:
            with Image.open(io.BytesIO(png_bytes)) as image:
                image = image.convert("RGB")
                if x + width > image.width or y + height > image.height:
                    raise ComputerControlError(
                        "computer_coordinate_out_of_bounds",
                        f"局部观察区域 ({x},{y},{width}x{height}) 超出截图 "
                        f"{image.width}x{image.height}（观察期间视口可能已变化）",
                    )
                cropped = image.crop((x, y, x + width, y + height))
                buffer = io.BytesIO()
                cropped.save(buffer, format="PNG", optimize=True)
                cropped_bytes = buffer.getvalue()
        except ComputerControlError:
            raise
        except Exception as error:  # noqa: BLE001
            raise ComputerControlError(
                "computer_backend_error",
                f"裁剪局部观察截图失败：{type(error).__name__}",
            ) from error
        return cropped_bytes, {
            "width": width,
            "height": height,
            "original_width": width,
            "original_height": height,
            "origin_x": x,
            "origin_y": y,
            "scale_x": 1.0,
            "scale_y": 1.0,
        }

    def _cmd_observe(self, run_id: str) -> ComputerObservation:
        self._reset_run_scope(run_id)
        page = self._resolve_active_page()
        page_token = self._page_token(page)
        url = page.url if isinstance(page.url, str) else ""
        self._observed_url = url
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        self._obs_seq += 1
        obs_gen = self._obs_seq
        viewport = self._observation_viewport(page)
        view_w = int(viewport["width"])
        view_h = int(viewport["height"])
        viewport_gen = self._current_viewport_generation(page)
        # B 方案（降级观察）：先做不依赖渲染的 DOM 收集。窗口被遮挡/最小化时
        # 渲染被节流而 DOM 求值照常，先把元素与正文拿到手，保证"这次观察至少
        # 有文本信息"；截图放到最后，失败也只降级、不让整条观察链报错。
        pending_region = self._pending_region
        self._pending_region = None
        if pending_region is None:
            elements, ref_records = self._collect_interactive_elements(
                page,
                run_id=run_id,
                page_token=page_token,
                obs_gen=obs_gen,
                viewport_gen=viewport_gen,
                view_w=view_w,
                view_h=view_h,
            )
            page_text = self._collect_page_text(page)
        else:
            # 局部观察：模型要的是区域画面，不收集 DOM 清单（清空同时避免旧
            # el-N 与新坐标系混用）。
            elements = ()
            ref_records = {}
            page_text = ""
        png_bytes = b""
        screenshot_error = ""
        try:
            # 显式短超时：默认 30s 在窗口被遮挡时会一直等新帧，把命令预算耗尽
            # 变成笼统超时；短超时能快速判定"这次取不到图"并落到文本降级。
            png = page.screenshot(type="png", timeout=_SCREENSHOT_TIMEOUT_MS)
        except ComputerControlError:
            raise
        except Exception as error:
            # 不抛错：窗口被遮挡/最小化时截图等不到新帧属于预期内的环境状态，
            # 抛错会让模型完全失去观察能力；降级为纯 DOM 观察更有用。
            screenshot_error = (
                f"浏览器窗口不可见或被遮挡，本次未取到截图：{type(error).__name__}"
            )
            _LOGGER.warning(
                "浏览器截图失败，本次降级为纯文本观察：%s", type(error).__name__
            )
            png = None
        if png is not None:
            if not isinstance(png, (bytes, bytearray)) or not bytes(png):
                raise ComputerControlError(
                    "computer_action_failed", "浏览器截图未返回 PNG bytes"
                )
            png_bytes = bytes(png)
        # 截图后再验证仍是同一受控页：期间切换则本次观察作废（R5）。降级路径
        # 同样必须复核——页面切换时旧页的文本/元素不得冒充新页观察。
        if self._page_token(self._resolve_active_page()) != page_token:
            raise ComputerControlError(
                "computer_surface_changed",
                "观察期间受控页面已切换，本次观察作废，请重新观察",
            )
        try:
            title = page.title()
        except Exception as error:
            raise ComputerControlError(
                "computer_action_failed",
                f"读取页面标题失败：{type(error).__name__}",
            ) from error
        if not isinstance(title, str):
            title = ""
        if elements and (
            self._page_token(self._resolve_active_page()) != page_token
            or (page.url if isinstance(page.url, str) else "") != url
        ):
            elements = ()
            ref_records = {}
        # 只有真正拿到 PNG 才落盘：降级路径不写文件、也绝不复用任何旧截图。
        if png_bytes:
            png_bytes = self._normalize_frame(png_bytes, page)
        viewport = self._observation_viewport(page)
        viewport_gen = self._current_viewport_generation(page)
        region_fields: dict | None = None
        action_name = "observe"
        if pending_region is not None and png_bytes:
            png_bytes, region_fields = self._build_region_frame(png_bytes, pending_region)
            action_name = "observe_region"
        if region_fields is None:
            view_w = int(viewport["width"])
            view_h = int(viewport["height"])
            self._coord_frame = (0, 0, view_w, view_h, view_w, view_h)
            self._region_view = False
            observation_view = viewport
        else:
            self._region_view = True
            self._coord_frame = (
                int(region_fields["origin_x"]),
                int(region_fields["origin_y"]),
                int(region_fields["original_width"]),
                int(region_fields["original_height"]),
                int(region_fields["width"]),
                int(region_fields["height"]),
            )
            observation_view = region_fields
        path = self._store_screenshot(png_bytes) if png_bytes else None
        if self._observed_page_token != page_token:
            self._surface_generation += 1
            self._observed_page_token = page_token
        self._frame_seq += 1
        frame_id = f"frame-{self._surface_generation}-{viewport_gen}-{self._frame_seq}"
        img_w = int(observation_view["width"])
        img_h = int(observation_view["height"])
        crop_w = int(observation_view["original_width"])
        crop_h = int(observation_view["original_height"])
        frame = ObservationFrame(
            frame_id=frame_id,
            run_id=run_id,
            target="browser",
            surface_id=f"page-{page_token}",
            surface_generation=self._surface_generation,
            viewport_generation=viewport_gen,
            origin_x=int(observation_view["origin_x"]),
            origin_y=int(observation_view["origin_y"]),
            crop_width=crop_w,
            crop_height=crop_h,
            image_width=img_w,
            image_height=img_h,
            model_to_css_x=float(crop_w) / max(1, img_w),
            model_to_css_y=float(crop_h) / max(1, img_h),
            coordinate_input_enabled=bool(png_bytes),
            disabled_reason="" if png_bytes else "no_screenshot",
        )
        self._frames[frame_id] = frame
        while len(self._frames) > 32:
            self._frames.pop(next(iter(self._frames)))
        self._active_frame = frame
        self._active_refs = ref_records
        self._observed_element_refs = frozenset(ref_records.keys())
        return ComputerObservation(
            run_id=run_id,
            target="browser",
            action=action_name,
            ok=True,
            screen_sha256=hashlib.sha256(png_bytes).hexdigest() if png_bytes else "",
            png_path=str(path) if path is not None else "",
            **observation_view,
            page_origin=origin,
            page_title=title[:_MAX_TITLE_CHARS],
            page_text=page_text,
            screenshot_error=screenshot_error,
            surface_id=f"page-{page_token}",
            surface_generation=self._surface_generation,
            frame_id=frame_id,
            viewport_generation=viewport_gen,
            elements=elements,
        )

    def _observation_viewport(self, page) -> dict:
        vp = getattr(page, "viewport_size", None)
        if isinstance(vp, dict):
            try:
                w = int(vp.get("width") or 0)
                h = int(vp.get("height") or 0)
                if w > 0 and h > 0:
                    return {
                        "width": w,
                        "height": h,
                        "original_width": w,
                        "original_height": h,
                        "origin_x": 0,
                        "origin_y": 0,
                        "scale_x": 1.0,
                        "scale_y": 1.0,
                    }
            except (TypeError, ValueError):
                pass
        return {
            "width": self._viewport_width,
            "height": self._viewport_height,
            "original_width": self._viewport_width,
            "original_height": self._viewport_height,
            "origin_x": 0,
            "origin_y": 0,
            "scale_x": 1.0,
            "scale_y": 1.0,
        }

    def _normalize_frame(self, png_bytes: bytes, page) -> bytes:
        """截图后、落盘前的帧归一化钩子：默认原样返回。

        CDP 模式（内置 WebView2）覆写它，把截图重采样到 CSS 视口尺寸，保证
        「模型看到的图片坐标 = 动作坐标」——否则高 DPI 下 PNG 是 视口×dpr，
        模型按图片给的坐标会被当成 CSS 坐标直接点击，越往右下偏得越多。
        """
        return png_bytes

    def _cmd_execute(self, run_id: str, action: ComputerAction) -> None:
        if action.target != "browser":
            raise ComputerControlError(
                "computer_invalid_action",
                f"浏览器 backend 只执行 browser target，收到 {action.target!r}",
            )
        kind = action.kind
        if kind == "observe":
            return
        if kind == "observe_region":
            page = self._resolve_active_page()
            self._require_observed_surface(page)
            self._set_pending_region(page, action)
            return
        if kind == "request_user_takeover":
            raise ComputerControlError(
                "computer_invalid_action",
                "request_user_takeover 由 controller 处理，不进入 backend",
            )
        if kind == "wait":
            duration = action.duration_ms
            if type(duration) is not int or not MIN_WAIT_MS <= duration <= MAX_WAIT_MS:
                raise ComputerControlError(
                    "computer_invalid_action",
                    f"duration_ms 必须在 {MIN_WAIT_MS}-{MAX_WAIT_MS} 毫秒之间",
                )
            self._wait_interruptible(duration)
            return
        if kind == "wait_for":
            self._exec_wait_for(run_id, action)
            return
        page = self._resolve_active_page()
        record: _BrowserElementRecord | None = None
        if kind in ("click_element", "set_value_element"):
            record = self._require_ref_valid(page, action.element_ref)
        else:
            self._require_observed_surface(page)
        try:
            self._dispatch_action(page, action, element_record=record)
        except ComputerControlError:
            raise
        except Exception as error:
            raise ComputerControlError(
                "computer_action_failed",
                f"浏览器动作 {kind} 执行失败：{type(error).__name__}",
            ) from error

    def _cmd_release_inputs(self) -> None:
        buttons = list(self._pressed_buttons)
        self._pressed_buttons = set()
        keys = list(self._pressed_keys)
        self._pressed_keys = []
        if self._context_closed or self._context is None:
            return
        for recorded_page, mapped in reversed(keys):
            try:
                if not recorded_page.is_closed():
                    recorded_page.keyboard.up(mapped)
            except Exception:
                _LOGGER.exception("释放浏览器修饰键失败")
        page = self._active_page
        if page is None or page.is_closed():
            return
        for button in buttons:
            try:
                page.mouse.up(button=button)
            except Exception:
                _LOGGER.exception("释放鼠标按键失败")

    def _remaining_budget_ms(self) -> int:
        deadline = self._current_deadline
        if deadline is None or deadline == float("inf"):
            return int(self._command_timeout * 1000)
        return max(1, int((deadline - time.monotonic()) * 1000))

    def _element_action_timeout_ms(self) -> int:
        return max(1, min(_ELEMENT_ACTION_TIMEOUT_MS, self._remaining_budget_ms() - 1000))

    def _collect_wait_text(self, page) -> str:
        frames_attr = getattr(page, "frames", None)
        frames = (
            list(frames_attr)
            if isinstance(frames_attr, (list, tuple)) and frames_attr
            else [page]
        )
        chunks: list[str] = []
        for frame in frames:
            try:
                raw = frame.evaluate(PAGE_TEXT_SCRIPT)
            except Exception:
                continue
            if isinstance(raw, str) and raw:
                chunks.append(raw)
        return "\n".join(chunks)

    def _exec_wait_for(self, run_id: str, action: ComputerAction) -> None:
        self._reset_run_scope(run_id)
        condition = str(action.condition or "")
        if condition not in ("text_exists", "element_exists", "element_enabled"):
            raise ComputerControlError(
                "computer_invalid_action",
                f"browser wait_for 不支持条件：{condition!r}",
            )
        page = self._resolve_active_page()
        expected_page_token = self._page_token(page)
        expected_url = page.url if isinstance(page.url, str) else ""
        record: _BrowserElementRecord | None = None
        if condition in ("element_exists", "element_enabled"):
            record = self._require_ref_valid(page, action.element_ref)
            target_frame = record.frame
            expected_doc_token = record.document_generation
        else:
            target_frame = page
            expected_doc_token = self._read_doc_token(page)
        timeout_ms = int(action.timeout_ms or 8000)
        interval_ms = int(action.interval_ms or 300)
        deadline = time.monotonic() + max(0.05, timeout_ms / 1000.0)
        if self._current_deadline is not None and self._current_deadline != float("inf"):
            deadline = min(deadline, self._current_deadline - 0.1)
        interval_s = max(0.02, interval_ms / 1000.0)

        while True:
            if self._worker_stop.is_set():
                raise ComputerControlError(
                    "computer_cancelled", "浏览器条件等待被停止中断"
                )
            cur_page = self._resolve_active_page()
            if self._page_token(cur_page) != expected_page_token:
                raise ComputerControlError(
                    "computer_surface_changed",
                    "条件等待期间受控页面已切换，请重新 observe",
                    details={"submission_state": "not_submitted"},
                )
            cur_url = cur_page.url if isinstance(cur_page.url, str) else ""
            doc_changed = bool(
                expected_doc_token and self._read_doc_token(target_frame) != expected_doc_token
            )
            if cur_url != expected_url or doc_changed:
                raise ComputerControlError(
                    "computer_element_stale" if record is not None else "computer_surface_changed",
                    "条件等待期间文档或页面已改变，请重新 observe",
                    details={"submission_state": "not_submitted"},
                )
            if condition == "text_exists":
                visible_text = self._collect_wait_text(cur_page)
                if action.text and action.text in visible_text:
                    if time.monotonic() <= deadline:
                        return
            else:
                assert record is not None
                probe = target_frame.evaluate(
                    ELEMENT_WAIT_PROBE_SCRIPT,
                    {"id": record.local_id, "docToken": record.document_generation},
                )
                if isinstance(probe, dict):
                    if probe.get("stale"):
                        raise ComputerControlError(
                            "computer_element_stale",
                            f"元素 {record.ref} 已失效，请重新 observe",
                            details={"submission_state": "not_submitted"},
                        )
                    matched = (
                        bool(probe.get("exists"))
                        if condition == "element_exists"
                        else bool(probe.get("enabled"))
                    )
                    if matched and time.monotonic() <= deadline:
                        return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ComputerControlError(
                    "computer_wait_timeout",
                    f"浏览器条件等待超时（{condition}，{timeout_ms}ms 内未满足）",
                    details={"submission_state": "not_submitted"},
                )
            if self._worker_stop.wait(min(remaining, interval_s)):
                raise ComputerControlError(
                    "computer_cancelled", "浏览器条件等待被停止中断"
                )

    def _wait_interruptible(self, duration_ms: int) -> None:
        end = time.monotonic() + duration_ms / 1000
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            if self._worker_stop.wait(min(remaining, 0.05)):
                raise ComputerControlError(
                    "computer_cancelled", "显式等待被停止中断"
                )

    def _press_keys(self, page, keys) -> list:
        """按输入顺序按下修饰键；只有 down 成功的键进入按下记录。

        第 N 个 down 失败：逆序释放已按下的键后原样传播，绝不留半截组合。
        """
        pressed = []
        for name in keys:
            mapped = _map_playwright_key(name)
            try:
                page.keyboard.down(mapped)
            except Exception:
                self._release_keys(pressed)
                raise
            entry = (page, mapped)
            pressed.append(entry)
            self._pressed_keys.append(entry)
        return pressed

    def _release_keys(self, pressed) -> None:
        first_error = None
        for entry in reversed(pressed):
            recorded_page, mapped = entry
            try:
                recorded_page.keyboard.up(mapped)
            except Exception as error:
                _LOGGER.exception("释放浏览器修饰键失败")
                first_error = first_error or error
            else:
                try:
                    self._pressed_keys.remove(entry)
                except ValueError:
                    pass
        if first_error is not None:
            raise first_error

    def _with_pressed_keys(self, page, action: ComputerAction, invoke) -> None:
        if not action.keys:
            invoke()
            return
        pressed = self._press_keys(page, action.keys)
        try:
            invoke()
        finally:
            self._release_keys(pressed)

    def _dispatch_action(
        self,
        page,
        action: ComputerAction,
        *,
        element_record: _BrowserElementRecord | None = None,
    ) -> None:
        kind = action.kind
        if kind == "click":
            x, y = self._checked_point(action)
            self._with_pressed_keys(
                page, action, lambda: page.mouse.click(x, y, button=action.button)
            )
        elif kind == "double_click":
            x, y = self._checked_point(action)
            self._with_pressed_keys(
                page, action, lambda: page.mouse.dblclick(x, y, button=action.button)
            )
        elif kind == "move":
            x, y = self._checked_point(action)
            page.mouse.move(x, y)
        elif kind == "scroll":
            if action.x is not None or action.y is not None:
                x, y = self._checked_point(action)
                page.mouse.move(x, y)
            unit = action.unit or "css_pixel"
            if unit != "css_pixel":
                raise ComputerControlError(
                    "computer_invalid_action",
                    f"浏览器滚动仅支持 css_pixel 单位，收到 {unit!r}",
                )
            if (
                type(action.delta_x) is not int
                or type(action.delta_y) is not int
                or abs(action.delta_x) > 4000
                or abs(action.delta_y) > 4000
                or (not action.delta_x and not action.delta_y)
            ):
                raise ComputerControlError(
                    "computer_invalid_action",
                    "浏览器滚动每轴范围 ±4000 CSS 像素且两轴不得同时为零",
                )
            page.mouse.wheel(action.delta_x, action.delta_y)
        elif kind == "drag":
            self._with_pressed_keys(page, action, lambda: self._exec_drag(page, action))
        elif kind == "type_text":
            if not isinstance(action.text, str) or not action.text:
                raise ComputerControlError("computer_invalid_action", "type_text 需要非空 text")
            page.keyboard.insert_text(action.text)
        elif kind == "keypress":
            if not action.keys:
                raise ComputerControlError("computer_invalid_action", "keypress 需要至少一个键")
            page.keyboard.press("+".join(_map_playwright_key(key) for key in action.keys))
        elif kind == "navigate":
            page.goto(
                _parse_url(action.url),
                wait_until="domcontentloaded",
                timeout=self._remaining_budget_ms(),
            )
        elif kind == "back":
            page.go_back(
                wait_until="domcontentloaded", timeout=self._remaining_budget_ms()
            )
        elif kind == "forward":
            page.go_forward(
                wait_until="domcontentloaded", timeout=self._remaining_budget_ms()
            )
        elif kind == "reload":
            page.reload(
                wait_until="domcontentloaded", timeout=self._remaining_budget_ms()
            )
        elif kind == "click_element":
            assert element_record is not None
            self._exec_click_element(page, element_record)
        elif kind == "set_value_element":
            assert element_record is not None
            self._exec_set_value_element(page, element_record, action.text)
        else:
            raise ComputerControlError(
                "computer_invalid_action",
                f"浏览器 backend 不支持动作：{kind!r}",
            )

    def _resolve_element_handle(self, record: _BrowserElementRecord):
        spec = {"id": record.local_id, "docToken": record.document_generation}
        try:
            handle = record.frame.evaluate_handle(ELEMENT_RESOLVE_SCRIPT, spec)
        except Exception as error:
            raise ComputerControlError(
                "computer_element_stale",
                f"元素 {record.ref} 无法解析或页面已切换，请重新 observe",
            ) from error
        el = handle.as_element() if handle is not None else None
        if el is None:
            if handle is not None:
                try:
                    handle.dispose()
                except Exception:
                    pass
            raise ComputerControlError(
                "computer_element_stale",
                f"元素 {record.ref} 已不存在于页面，请重新 observe",
            )
        return handle, el

    @staticmethod
    def _is_timeout_exception(error: BaseException) -> bool:
        return (
            isinstance(error, TimeoutError)
            or "timeout" in type(error).__name__.lower()
            or "timeout" in str(error).lower()
        )

    def _raise_element_action_error(
        self, record: _BrowserElementRecord, label: str, error: Exception
    ) -> None:
        if self._is_timeout_exception(error):
            raise ComputerControlError(
                "computer_action_timeout",
                f"元素 {record.ref} {label}超时且结果未知，请重新 observe 确认页面状态",
            ) from error
        lower_msg = str(error).lower()
        missing = "not attached" in lower_msg or "detached" in lower_msg or "closed" in lower_msg
        if not missing:
            try:
                spec = {"id": record.local_id, "docToken": record.document_generation}
                check = record.frame.evaluate_handle(ELEMENT_RESOLVE_SCRIPT, spec)
                if check is None or check.as_element() is None:
                    missing = True
                if check is not None:
                    try:
                        check.dispose()
                    except Exception:
                        pass
            except Exception:
                missing = True
        if missing:
            raise ComputerControlError(
                "computer_element_stale",
                f"元素 {record.ref} 已不存在于页面，请重新 observe",
            ) from error
        raise ComputerControlError(
            "computer_element_unsupported",
            f"元素 {record.ref} {label}失败：{type(error).__name__}",
        ) from error

    def _exec_click_element(self, page, record: _BrowserElementRecord) -> None:
        timeout = self._element_action_timeout_ms()
        handle, el = self._resolve_element_handle(record)
        try:
            point = None
            try:
                point = record.frame.evaluate(
                    ELEMENT_HIT_SCRIPT,
                    {"id": record.local_id, "docToken": record.document_generation},
                )
            except Exception:
                point = None
            if isinstance(point, dict) and point.get("stale"):
                raise ComputerControlError(
                    "computer_element_stale",
                    f"元素 {record.ref} 已脱离文档或移出视口，请重新 observe",
                )
            if (
                isinstance(point, dict)
                and point.get("contains")
                and point.get("inViewport")
            ):
                try:
                    page.mouse.click(
                        int(point["x"]) + record.offset_x,
                        int(point["y"]) + record.offset_y,
                    )
                    return
                except Exception as error:
                    self._raise_element_action_error(record, "点击", error)
            try:
                el.click(timeout=timeout)
            except Exception as error:
                self._raise_element_action_error(record, "点击", error)
        finally:
            try:
                handle.dispose()
            except Exception:
                pass

    def _exec_set_value_element(
        self, page, record: _BrowserElementRecord, text: str
    ) -> None:
        handle, el = self._resolve_element_handle(record)
        try:
            # 输入框和 contenteditable 统一优先 fill；超时或结果未知绝不回退 Ctrl+A/键盘。
            try:
                el.fill(text, timeout=self._element_action_timeout_ms())
                return
            except Exception as error:
                self._raise_element_action_error(record, "写入", error)
        finally:
            try:
                handle.dispose()
            except Exception:
                pass

    def _exec_drag(self, page, action: ComputerAction) -> None:
        points = action.path
        if len(points) < 2:
            raise ComputerControlError("computer_invalid_action", "drag 需要至少 2 个 path 点")
        css_points = [self._point_to_css(point_x, point_y) for point_x, point_y in points]
        start_x, start_y = css_points[0]
        button = action.button
        page.mouse.move(start_x, start_y)
        page.mouse.down(button=button)
        self._pressed_buttons.add(button)
        try:
            for point_x, point_y in css_points[1:]:
                page.mouse.move(point_x, point_y)
        finally:
            try:
                page.mouse.up(button=button)
            except Exception:
                _LOGGER.exception("拖拽收尾 mouse.up 失败")
                raise
            else:
                self._pressed_buttons.discard(button)

    def _checked_point(self, action: ComputerAction) -> tuple[int, int]:
        x, y = action.x, action.y
        if type(x) is not int or type(y) is not int:
            raise ComputerControlError("computer_invalid_action", "坐标 x/y 必须是整数")
        return self._point_to_css(x, y)

    def _point_to_css(self, x: int, y: int) -> tuple[int, int]:
        active_frame = self._active_frame
        if active_frame is not None and not active_frame.coordinate_input_enabled:
            raise coordinate_disabled_error(active_frame.disabled_reason)
        frame = self._coord_frame
        if frame is None:
            self._check_coordinates(x, y)
            return x, y
        origin_x, origin_y, css_w, css_h, model_w, model_h = frame
        if not (0 <= x < model_w and 0 <= y < model_h):
            suffix = (
                "（当前为局部观察，坐标以区域左上角为原点）"
                if self._region_view
                else ""
            )
            raise ComputerControlError(
                "computer_coordinate_out_of_bounds",
                f"坐标 ({x}, {y}) 超出当前观察画面 {model_w}x{model_h} 半开区间{suffix}",
            )
        css_x = origin_x + round(x * css_w / model_w)
        css_y = origin_y + round(y * css_h / model_h)
        return css_x, css_y

    def _check_coordinates(self, x: int, y: int) -> None:
        if not (0 <= x < self._viewport_width and 0 <= y < self._viewport_height):
            raise ComputerControlError(
                "computer_coordinate_out_of_bounds",
                f"坐标 ({x}, {y}) 超出浏览器视口 "
                f"{self._viewport_width}x{self._viewport_height} 半开区间",
            )

    def _store_screenshot(self, png: bytes) -> Path:
        assert self._screenshot_dir is not None
        self._screenshot_counter += 1
        path = self._screenshot_dir / f"shot-{self._screenshot_counter}.png"
        path.write_bytes(png)
        shots = sorted(
            self._screenshot_dir.glob("shot-*.png"),
            key=lambda item: int(item.stem.rsplit("-", 1)[1]),
        )
        for old in shots[:-_KEEP_SCREENSHOTS]:
            old.unlink(missing_ok=True)
        return path

    def _next_command_id(self) -> int:
        with self._state_lock:
            self._command_seq += 1
            return self._command_seq

    def _admission_error(
        self, *, allow_stopping: bool = False
    ) -> ComputerControlError | None:
        """admission 检查（调用方须持有 _state_lock）；不可投递时返回错误。"""
        if self._closed and not allow_stopping:
            return ComputerControlError("computer_browser_closed", "浏览器 backend 已关闭")
        if self._failed:
            return ComputerControlError(
                "computer_backend_error",
                "浏览器 backend 已封闭：先前命令执行超时且结果未知，拒绝后续输入",
            )
        if self._stop_requested and not allow_stopping:
            return ComputerControlError(
                self._stop_error_code or "computer_cancelled",
                self._stop_error_message or "浏览器 backend 已停止，拒绝新动作",
            )
        if not self._started:
            return ComputerControlError(
                "computer_browser_unavailable",
                "浏览器 backend 尚未启动",
            )
        if self._transport is None:
            if self._worker is None or not self._worker.is_alive():
                return ComputerControlError(
                    "computer_browser_closed",
                    "浏览器 worker 已退出，浏览器不可用",
                )
        return None

    def _call(
        self,
        description: str,
        fn: Callable,
        timeout: float | None = None,
        *,
        allow_stopping: bool = False,
    ):
        budget = self._command_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + budget
        with self._state_lock:
            admission = self._admission_error(allow_stopping=allow_stopping)
        if admission is not None:
            raise admission
        command = _Command(
            self._next_command_id(), fn, deadline, allow_stopping=allow_stopping
        )
        # 提交锁等待、入队共用同一 deadline 预算；锁只在「复核 admission + 入队
        # + 入队后复核 close」的短临界区持有，绝不在执行等待期间持有——否则
        # close() 会被一个等待中的调用者挡住整个命令超时周期。
        remaining = deadline - time.monotonic()
        if not self._submit_lock.acquire(timeout=max(0.0, remaining)):
            raise ComputerControlError(
                "computer_action_timeout",
                f"浏览器命令提交等待超时：{description}",
            )
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ComputerControlError(
                        "computer_action_timeout",
                        f"浏览器命令排队超时：{description}",
                    )
                with self._admission_lock:
                    with self._state_lock:
                        admission = self._admission_error(
                            allow_stopping=allow_stopping
                        )
                        closed_after_stop = self._closed
                    if admission is not None:
                        raise admission
                    if closed_after_stop:
                        raise ComputerControlError(
                            "computer_browser_closed",
                            "浏览器 backend 已关闭，排队命令已撤销",
                        )
                    try:
                        self._queue.put_nowait(command)
                        break
                    except queue.Full:
                        pass
                self._worker_stop.wait(min(remaining, 0.01))
        finally:
            self._submit_lock.release()
        remaining = deadline - time.monotonic()
        if not command.done.wait(timeout=max(0.0, remaining)):
            self._command_timed_out(command, description)
        with command.lock:
            state = command.state
            result = command.result
            error = command.error
        if state == "cancelled":
            raise error
        if error is not None:
            raise error
        return result

    def _command_timed_out(self, command: _Command, description: str) -> None:
        with command.lock:
            if command.state == "queued":
                command.state = "cancelled"
                command.error = ComputerControlError(
                    "computer_action_timeout",
                    f"浏览器命令排队超时：{description}",
                )
                command.done.set()
                raise command.error
            if command.state in ("succeeded", "failed"):
                return
        # state == "running"：已开始的执行无法假称未发生，也无法在本线程
        # 打断同步 Playwright 调用；实例永久封闭，禁止后续输入，profile
        # 执行权在 worker 真实退出前绝不释放给新实例。
        with self._state_lock:
            self._failed = True
        raise ComputerControlError(
            "computer_action_timeout",
            f"浏览器命令执行超时且结果未知（{description}）；"
            "该实例已封闭，拒绝后续输入，请停止后重新开始",
        )

    def _acquire_profile_lock(self, resolved: str) -> None:
        with _PROFILE_REGISTRY_GUARD:
            if resolved in _PROFILE_HOLDERS:
                raise ComputerControlError(
                    "computer_profile_locked",
                    f"浏览器 profile 已被本进程另一个会话占用：{resolved}",
                )
            _PROFILE_HOLDERS[resolved] = id(self)

    def _release_profile_lock(self, resolved: str) -> None:
        with _PROFILE_REGISTRY_GUARD:
            if _PROFILE_HOLDERS.get(resolved) == id(self):
                del _PROFILE_HOLDERS[resolved]
