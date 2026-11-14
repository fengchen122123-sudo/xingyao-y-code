"""Edge/Chrome 专用 Playwright 浏览器 backend（设计 12、21、22）。

线程归属铁律（设计 12.1）：构造函数不导入也不启动 Playwright；默认 factory
在 worker 线程内才惰性 ``from playwright.sync_api import sync_playwright``。
``start()`` 创建专用 worker 线程，worker 独占 runtime/context/page；
外部 start/observe/execute/release_inputs/close 全部经有界命令队列 marshal
到 worker，调用线程绝不直接触碰 Playwright 对象。worker 未就绪、命令超时或
worker 退出都明确失败（稳定错误码），绝不在调用线程重建 Playwright 对象。

无降级原则（设计 21）：所选浏览器启动失败抛 ``computer_browser_unavailable``，
绝不 fallback 另一个浏览器；profile 锁冲突抛 ``computer_profile_locked``；
context/page 被用户关闭后后续动作抛 ``computer_browser_closed``，不自动重开；
截图失败不再让观察报错（窗口被遮挡/最小化时渲染被节流），而是降级为纯 DOM
观察（元素列表 + 页面正文 + screenshot_error 说明），且绝不复用旧截图。

隐私（设计 22）：观察只暴露 ``scheme://netloc`` origin 与截断 200 字符的 title；
不读取 DOM 输入值、Cookie、localStorage 或密码库。
"""

from __future__ import annotations

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
from core.computer.models import (
    ComputerAction,
    ComputerControlError,
    ComputerObservation,
)

_LOGGER = logging.getLogger(__name__)

# chromium 使用 Playwright 自带浏览器，channel 留空。
_CHANNELS = {"edge": "msedge", "chrome": "chrome", "chromium": None}
WORKER_ARGV_FLAG = "--ycode-browser-worker"
_VIEWPORT_WIDTH = 1440
_VIEWPORT_HEIGHT = 900
_MAX_TITLE_CHARS = 200
_COMMAND_QUEUE_SIZE = 8
_DEFAULT_COMMAND_TIMEOUT = 30.0
_DEFAULT_START_TIMEOUT = 30.0
_KEEP_SCREENSHOTS = 3
# R2 第二段：受管执行进程的父进程守卫超时（须大于子进程内部命令超时，
# 让子进程自己的超时错误先于父进程守卫返回）与硬停止预算。
_PROCESS_GUARD_SLACK = 5.0
# R5：窗口标题落后于页面标题（Edge/Chrome 异步更新）的有界收敛等待；
# 每轮重新取窗口提示并重新匹配，超时仍 fail-closed。
_WINDOW_HINT_SETTLE_SECONDS = 1.0
_PROCESS_CLOSE_GRACEFUL = 2.0
_PROCESS_HARD_EXIT = 2.0

# 进程内 profile 锁注册表：同一 browser profile 只允许一个 Y-CODE context 持有
# （设计 12.2）。键为 resolve 后的 profile 目录字符串，值为持有者 id。
_PROFILE_REGISTRY_GUARD = threading.Lock()
_PROFILE_HOLDERS: dict[str, int] = {}

# launch_persistent_context 报错文案中的 profile 锁线索（真实 Playwright 在
# profile 被其他进程占用时抛 ProcessSingleton / already in use 类错误）。
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

# E2 元素动作超时（毫秒）：明显小于命令预算（默认 30s），元素等待/可点击性
# 检查失败时由元素语义错误码（computer_element_unsupported）返回，而不是拖到
# 命令超时变成笼统的 computer_action_timeout；实际取与命令剩余预算的较小值。
_ELEMENT_ACTION_TIMEOUT_MS = 10_000

# B 方案（窗口被遮挡时的降级观察）常量。
# _SCREENSHOT_TIMEOUT_MS：截图超时必须显式设短（Playwright 默认 30s）。浏览器
# 窗口被遮挡/最小化后 WebView2 渲染被节流，screenshot 等不到新帧会一直阻塞，
# 默认超时基本等于把整个命令预算（30s）用完，调用侧只剩"观察超时失败"；3s
# 足够识别"这次取不到图"，把剩余预算留给 DOM 观察与返回。
_SCREENSHOT_TIMEOUT_MS = 3000

# _PAGE_TEXT_MAX_CHARS：页面正文摘要上限。按 Python 码点切片截断（与前端
# Array.from 的码点语义一致），够模型判断页面内容，又不把整篇长文塞进上下文。
_PAGE_TEXT_MAX_CHARS = 6000

# B 方案：页面正文提取脚本（降级观察的唯一 DOM 来源）。
# - 隐私铁律：只读可见文本（innerText 已排除 display:none / visibility:hidden
#   的内容），绝不读 input.value / textarea.value 里的用户输入，绝不读 cookie、
#   localStorage 或 sessionStorage；脚本源码不得出现这些字样。
# - 为什么用 innerText 而不是 textContent：innerText 近似"用户看到的文本"，
#   不含隐藏文本与脚本内容，泄露面最小。
# - 为什么不需要可见性/焦点：窗口被遮挡时 DOM 求值照常返回，正是降级要用的通道。
PAGE_TEXT_SCRIPT = """() => {
  const root = document.body || document.documentElement;
  if (!root) return "";
  return (root.innerText || "").trim();
}"""

# E1 浏览器元素收集脚本（设计 2.2~2.4）：一次 JS 求值收集视口内可交互元素。
# - 隐私铁律：只读标签/ARIA/文本等结构信息，绝不读输入值、Cookie 或存储
#   （脚本源码不得出现 .value / cookie / localStorage / sessionStorage 字样）；
# - 确定性：querySelectorAll 的文档顺序 + 顺序编号，无随机数/时间戳，bounds
#   取整方式固定（Math.round），同一页面状态产生逐字节相同的结果；
# - 副作用：给选中元素打 data-ycode-ref 标记（E2 的 Playwright locator 依赖它；
#   属性随页面生命周期结束自然消失，不主动清理、不监听不加事件）；
# - 视口外元素不收集：模型看不到截图也无法点击，滚动后重新观察即可。
ELEMENT_COLLECT_SCRIPT = """() => {
  const SEL = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea',
    '[role="button"]', '[role="link"]', '[role="textbox"]', '[role="checkbox"]',
    '[role="radio"]', '[role="combobox"]', '[role="menuitem"]', '[role="tab"]',
    '[role="switch"]', '[role="searchbox"]', '[role="option"]',
    '[contenteditable="true"]', '[onclick]', '[tabindex]:not([tabindex="-1"])'
  ].join(',');

  const MAX_NAME = 80;
  // 500 而不是 200：网格型页面（扫雷/表格/日历）一个盘面就有 256~480 个
  // 可点击单元，200 会在盘面中部截断，模型永远点不到下半部分；500 覆盖
  // 常见网格整盘，普通页面元素数远小于此，不受影响。
  const MAX_ELEMENTS = 500;

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
  function deriveKind(el) {
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
    return tag;
  }

  // ---- name 推导：对齐 W3C AccName 主流实现顺序，绝不读表单值 ----
  function deriveName(el) {
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();

    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const texts = labelledBy.split(/\\s+/)
        .map(id => document.getElementById(id))
        .filter(Boolean)
        .map(node => (node.innerText || '').trim())
        .filter(Boolean);
      if (texts.length) return texts.join(' ');
    }

    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
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
  // inline 元素（典型：line-height:0 的 <a> 包一张 <img>）自身的 border box
  // 会退化成一整行 0~1px 高、并且落在图片基线下方——它既不覆盖图片，中心点
  // 也不在图片上。按自身 rect 报 bounds 会让模型/click_element 系统性地点空
  // （实测扫雷：a 的 rect 是 39x1、y 比格子底边还低 1px，点击后游戏毫无反应）。
  // 因此 bounds 取「自身矩形 ∪ 可见图标类后代矩形」，即用户实际看到的、
  // 鼠标能命中的那块区域。
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

  const out = [];
  let counter = 0;
  for (const el of document.querySelectorAll(SEL)) {
    if (counter >= MAX_ELEMENTS) break;
    const rawRect = el.getBoundingClientRect();
    if (!isVisible(el) || !inViewport(rawRect)) continue;
    const rect = hitRect(el, rawRect);
    counter += 1;
    const ref = 'el-' + counter;
    el.setAttribute('data-ycode-ref', ref);
    out.push({
      ref: ref,
      kind: deriveKind(el),
      // 按码点截断（Array.from），避免 UTF-16 代理对（emoji 等）被切半；
      // 与 Python 侧 [:_ELEMENT_MAX_NAME_CHARS] 的码点语义对齐。
      name: Array.from(deriveName(el)).slice(0, MAX_NAME).join(''),
      bounds: [
        Math.round(rect.left), Math.round(rect.top),
        Math.round(rect.right), Math.round(rect.bottom)
      ],
      enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
    });
  }
  return out;
}"""


# E3（2026-11-02）元素点击点：按「视觉可点击区域」的中心点点击。
# 为什么需要：click_element 之前用 locator.click()，Playwright 点的是元素自身
# border box 的中心；对 line-height:0 的 inline <a>（典型的老式图片链接、网格
# 游戏格子）这个中心会落在图标之外——实测扫雷页面 a 的 box 是 39x1、y 在 16px
# 格子底边以下，点击后游戏毫无反应。这里实时计算「自身 ∪ 图标类后代」的并集
# 中心，并用 elementFromPoint 复核该点确实命中元素本身或后代，命中才用鼠标
# 点击；否则（被遮挡/已滚出视口）回落 locator.click 保留原有可点击性语义。
ELEMENT_HIT_SCRIPT = """(ref) => {
  const el = document.querySelector('[data-ycode-ref="' + CSS.escape(ref) + '"]');
  if (!el) return null;
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
  const x = Math.round((left + right) / 2);
  const y = Math.round((top + bottom) / 2);
  const hit = document.elementFromPoint(x, y);
  return {
    x: x,
    y: y,
    contains: !!(hit && (hit === el || el.contains(hit))),
    inViewport: x >= 0 && y >= 0 && x < innerWidth && y < innerHeight
  };
}"""


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
    """投递到 worker 的一条命令；fn 为 None 表示关闭哨兵。

    生命周期（状态迁移只发生在 ``lock`` 内，worker 领取与调用方撤销原子互斥）：

    - ``queued``：尚未取得执行权，可撤销，撤销后零副作用；
    - ``running``：worker 原子领取后进入，此后不得假称“尚未执行”；
    - ``succeeded`` / ``failed``：正常终态；``cancelled``：排队阶段已撤销。

    ``deadline`` 为 monotonic 绝对截止时间，提交锁等待、入队与执行等待共用
    同一预算；到期仍未被领取的命令由 worker 的 ``try_start`` 直接判 cancelled，
    绝不执行。
    """

    __slots__ = ("command_id", "fn", "deadline", "lock", "done", "state", "result", "error")

    def __init__(self, command_id: int, fn: Callable | None, deadline: float) -> None:
        self.command_id = command_id
        self.fn = fn
        self.deadline = deadline
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
    """父进程侧一个在飞请求的等待点。"""

    __slots__ = ("done", "response", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.response: dict | None = None
        self.error: BaseException | None = None


class _WorkerProcessTransport:
    """受管浏览器执行进程的父进程侧传输（R2 第二段）。

    - 只发送结构化 JSON 请求（协议白名单），不传 Callable/eval/shell；
    - Windows 上子进程加入专属 Job Object（KILL_ON_JOB_CLOSE）：父进程退出
      时执行进程与它拉起的浏览器进程树一并终止；close 无法优雅收尾时按
      ``TerminateJobObject`` 硬终止，等待真实退出后才返回；
    - stdout 读线程按 id 派发响应；EOF/崩溃把所有在飞请求归一为
      computer_browser_closed，绝不悬挂调用线程。
    """

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
        # stdin 写入串行化：execute（回合线程）与 release/close（停止线程）
        # 可并发写管道，单行 JSON 被拆多次 WriteFile 时可能交错、子进程解析
        # 失败即自杀。_lock 只覆盖序号/登记，刻意保持小临界区，故独立锁。
        self._stdin_lock = threading.Lock()
        self._seq = 0
        self._pending: dict[int, _PendingCommand] = {}
        self._ready = threading.Event()
        self._boot_error: tuple[str, str] | None = None
        self._dead = threading.Event()
        self.browser_pid: int | None = None
        self.screenshot_dir: str = ""

    # ---------------- 启动 ----------------

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

    # ---------------- 请求 ----------------

    def request(self, op: str, payload: dict, timeout: float) -> dict | None:
        """发送一个请求并等待响应；超时返回 None（由调用方决定封闭语义）。"""
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
            with self._stdin_lock:
                line = _encode_worker_request(command_id, op, **payload)
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write((line + "\n").encode("utf-8"))
                self._proc.stdin.flush()
        except (OSError, ValueError) as error:
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

    # ---------------- 读线程与终止 ----------------

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
            self._ready.set()  # 崩溃在 ready 之前也要唤醒 start
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
            # 会话期协议错误：worker 随即退出（EOF 路径统一归一
            # computer_browser_closed），此处只留痕
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
                    proc.stdin.close()  # EOF：让子进程退出命令循环并自行收尾
            except OSError:
                pass
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    pass
        if proc.poll() is None:
            # 无法优雅收尾：终止本次专用执行进程与能确认归属的浏览器进程树。
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


# ---------------- Windows Job Object（ctypes，不新增依赖） ----------------

_JOB_HANDLE = None


def _create_kill_on_close_job():
    """创建 KILL_ON_JOB_CLOSE 的 Job Object；父进程退出时进程树随之终止。"""
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
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(limits), ctypes.sizeof(limits)  # JobObjectExtendedLimitInformation
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
        # 观察坐标系（逻辑视口）：外部浏览器 = 启动视口（dsf=1）；
        # CDP 模式由子类覆写为项目分辨率/页面真实视口。
        self._viewport_width, self._viewport_height = (int(viewport[0]), int(viewport[1]))
        # 是否拥有 context 的关闭权：外部浏览器 owned（teardown 关 context）；
        # CDP 直连内置浏览器时浏览器归 GUI 所有，teardown 绝不触碰 context。
        self._owns_context = True
        # R5：系统侧窗口标题提示（平台层提供）；None 时退化为 DOM 可见性裁决。
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
        self._ready = threading.Event()
        # R8：worker 停止事件（close 时置位，显式 wait 可中断）与当前命令
        # 截止时间（worker 内导航 timeout 共用 R2 的同一预算）。
        self._worker_stop = threading.Event()
        self._current_deadline: float | None = None
        self._worker: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._failed = False  # running 命令超时后实例永久封闭，禁止后续输入
        self._command_seq = 0
        self._boot_error: ComputerControlError | None = None
        # 以下字段只由 worker 线程读写（_active_page 同时作为测试只读 seam）。
        self._runtime = None
        self._playwright = None
        self._context = None
        self._active_page = None
        self._context_closed = False
        # R5 受控页身份：id(page) → 本地稳定序号；_observed_page_token 是最近
        # 一次观察锁定的页面，surface_generation 在受控页切换时递增。
        self._page_tokens: dict[int, int] = {}
        self._page_serial = 0
        self._observed_page_token: int | None = None
        # E1：最近一次观察收集到的元素 ref 全集。与 _observed_page_token 同
        # 生命周期——每次观察整体替换（空集即旧 ref 全部失效）；E2 的
        # click_element/set_value_element 依赖它做 computer_element_stale 校验。
        self._observed_element_refs: frozenset[str] = frozenset()
        # E2：最近一次观察时的页面 URL。页面 token 不随同页导航变化（SPA 路由/
        # 整页跳转仍落在同一 Page 对象上），URL 对比补全「页面变化 → 旧 ref 失效」
        # 的产品决定（验收场景 4）；与 _observed_element_refs 同生命周期。
        self._observed_url: str = ""
        self._surface_generation = 0
        self._pressed_buttons: set[str] = set()
        # 修饰键按下记录绑定按下时的 page（R3）：popup 切换 active page 后
        # 仍回到原页面释放；条目为 (page, playwright_key)，按按下顺序排列。
        self._pressed_keys: list = []
        self._profile_lock_key: str | None = None
        self._screenshot_dir: Path | None = None
        self._screenshot_counter = 0
        # E5（2026-11-03）：browser 局部观察（observe_region）。
        # _pending_region 由 execute(observe_region) 置位，紧随其后的 observe
        # 据此裁剪区域（controller 每个动作后都会再 observe，所以区域观察就是
        # 这次动作的返回画面）。_coord_frame 记录当前观察的坐标映射：
        # (origin_x, origin_y, css_w, css_h, model_w, model_h)——模型坐标 → CSS。
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
        """测试/诊断只读 seam：返回 worker 持有的当前 active page 对象。

        调用方不得对返回对象调用任何 Playwright 方法（线程归属在 worker）。
        """
        return self._active_page

    def is_reusable(self) -> bool:
        """retained 领取裁决（R4）：未封闭、worker 存活、无 pending 命令、窗口未被关闭。"""
        with self._state_lock:
            transport = self._transport
            if transport is not None:
                worker = transport  # 进程模式：worker 即受管执行进程
                alive = transport.is_alive() and transport.pending_count() == 0
            else:
                worker = self._worker
                alive = worker is not None and worker.is_alive()
            if (
                not self._started
                or self._closed
                or self._failed
                or not alive
            ):
                return False
        if transport is None and self._context_closed:
            return False
        if transport is None:
            return self._queue.empty()
        return True

    # ---------------- 外部 API（全部 marshal 到 worker） ----------------

    def start(self) -> None:
        if self._use_process_worker:
            self._start_process_worker()
            return
        self._start_thread_worker()

    def _default_worker_command(self) -> list[str]:
        if self._worker_command_override is not None:
            return list(self._worker_command_override)
        # 项目视口随 argv 传入受管执行进程（进程模式的 Playwright 在子进程，
        # 启动视口在子进程 _boot 生效）。
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

            # JSON 往返会把 elements 外层 tuple 变成 list：归一化后再构造，
            # 保证进程模式与线程模式的观察形状一致。
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

    def _remote_call(self, op: str, payload: dict, description: str) -> dict:
        """进程模式请求：admission 检查 + 守卫超时 + 超时即封闭。"""
        with self._state_lock:
            admission = self._admission_error()
            transport = self._transport
        if admission is not None:
            raise admission
        if transport is None:
            raise ComputerControlError(
                "computer_browser_unavailable", "受管浏览器执行进程尚未启动"
            )
        response = transport.request(
            op, payload, timeout=self._command_timeout + self._process_guard_slack
        )
        if response is None:
            # 受管进程在守卫超时内无响应：结果未知，实例永久封闭；调用方
            # 停止（close）将按有界流程硬终止执行进程与浏览器进程树。
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

    def release_inputs(self) -> None:
        """尽力释放 worker 内跟踪的按下状态；backend 不可用时静默 no-op。"""
        if self._transport is not None:
            with self._state_lock:
                transport = self._transport
            if transport is None or not transport.is_alive():
                return
            try:
                # 停止路径上的尽力清理：有界短预算，卡死的进程由 close 硬终止。
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
            )
        except ComputerControlError:
            _LOGGER.warning("释放浏览器输入状态失败", exc_info=True)

    def close(self) -> None:
        if self._transport is not None:
            with self._state_lock:
                if self._closed or not self._started:
                    return
                # 先关闭 admission（100ms 量级内新命令一律拒绝）。
                self._closed = True
                transport = self._transport
                self._transport = None
            screenshot_dir = transport.screenshot_dir
            stats = transport.close(graceful_timeout=_PROCESS_CLOSE_GRACEFUL)
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
            if self._closed or not self._started:
                # 幂等；未启动时 no-op，不消耗生命周期。
                return
            # 先关闭 admission：此后 _call 一律拒绝新命令。
            self._closed = True
            worker = self._worker
        # R8：先中断 worker 内可中断等待（显式 wait 立即以取消收尾）。
        self._worker_stop.set()
        # 再撤销全部排队命令（零副作用终态通知，调用线程不悬挂），最后才
        # 投递关闭哨兵——哨兵绝不能排在待执行输入之后任其照常执行。
        with self._submit_lock:
            with self._queue.mutex:
                pending = list(self._queue.queue)
                self._queue.queue.clear()
            for command in pending:
                command.cancel_if_queued(
                    "computer_browser_closed", "浏览器 backend 已关闭，排队命令已撤销"
                )
                self._queue.task_done()
            try:
                self._queue.put_nowait(
                    _Command(self._next_command_id(), None, float("inf"))
                )
            except queue.Full:
                _LOGGER.warning("浏览器 worker 命令队列已满，关闭哨兵投递失败")
        if worker.is_alive():
            worker.join(timeout=10)
            if worker.is_alive():
                _LOGGER.warning("浏览器 worker 在 close 超时后仍未退出（daemon 线程）")

    # ---------------- worker 线程：启动、命令循环、收尾 ----------------

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
            # 惰性导入：只有真实启动时才触碰 playwright，且发生在 worker 线程。
            # 安装包已不再自带 playwright（见 ycode.spec）：先确保按需组件就绪，
            # 再导入。组件缺失/未装好时转成电脑操控领域错误码，提示用户去
            # 「设置 → 电脑操控」点下载组件，而不是抛裸 ImportError。
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
            # 已恢复的历史 page：无焦点信息时取最后一个可见 page。
            self._active_page = pages[-1]
        else:
            # 初始零 page：创建且只创建一个 about:blank。
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
        """Windows 保持有界面、不附加容器参数。Linux 恒无头。"""
        kwargs = {
            "user_data_dir": user_data_dir,
            "headless": sys.platform != "win32",
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
        """按 profile 目录从进程命令行识别受控浏览器主进程（不读取用户浏览器）。"""
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
                    continue  # renderer/gpu 等子进程
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
                    return
                # 领取即原子检查：已撤销或过截止时间的排队命令零执行。
                if not command.try_start(time.monotonic()):
                    continue
                # R8：worker 内导航/等待共用该命令剩余预算，不另起独立超时。
                self._current_deadline = command.deadline
                try:
                    command.result = command.fn()
                except BaseException as error:
                    command.error = error
                finally:
                    self._current_deadline = None
                    command.finish()
            finally:
                # 与 close 的队列抽空配对记账，让测试可用 queue.join 做确定屏障。
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
        self._observed_page_token = None
        self._observed_element_refs = frozenset()
        self._observed_url = ""
        self._active_page = None

    # ---------------- worker 内：页面事件 ----------------

    def _on_new_page(self, page) -> None:
        """context page 事件（R5）：新 page 只登记本地身份，不无条件设为 active。

        是否成为受控页由下一次解析按可观测可见性/焦点决定；每次切换都会
        令旧坐标失效（surface_generation 递增 + 输入前身份复核）。
        """
        self._page_token(page)
        callback = self._on_page_change
        if callback is not None:
            try:
                callback()
            except Exception:
                _LOGGER.exception("浏览器页面变化事件回调失败")

    def _on_context_close(self, *args) -> None:
        self._context_closed = True

    # ---------------- worker 内：受控页解析（R5） ----------------

    def _page_token(self, page) -> int:
        """为 page 分配本地稳定身份（独立于 title/url；同标题同域也不同）。"""
        token = self._page_tokens.get(id(page))
        if token is None:
            self._page_serial += 1
            token = self._page_serial
            self._page_tokens[id(page)] = token
        return token

    def _probe_page_visibility(self, page) -> tuple[bool, bool]:
        """读取 (visible, focused)；探针失败的页按不可见/无焦点保守处理。"""
        try:
            result = page.evaluate(_VISIBILITY_PROBE)
        except Exception:
            return False, False
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            return False, False
        return result[0] == "visible", bool(result[1])

    def _resolve_active_page(self):
        """跟随用户当前可辨认的可见受控页；无法唯一确定时明确报错。

        裁决顺序：单页直选 → DOM 可见性快路径（恰好一个 visible+focused 且
        有页明确 hidden，信号可区分时可信）→ 系统侧窗口标题提示（R5：实测
        Playwright 默认启动参数下 visibilityState/hasFocus 对标签切换不生效，
        必须交叉验证）。全部 hidden/窗口不可见（最小化）回落既有约定：当前
        active 仍打开则保持，否则最后打开的页。
        """
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
            # 恰好一个可见焦点页（其余明确 unfocused/hidden）：信号可区分，可信。
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
                    # 同标题多页：等待无法解决，立即 fail-closed。
                    raise ComputerControlError(
                        "computer_surface_ambiguous",
                        "前台浏览器窗口无法唯一匹配受控页面（同标题多页或标题缺失），请重新观察",
                    )
                # matched 为空：窗口标题落后于页面标题的短暂过渡态 → 有界收敛。
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
        """输入前复核：当前受控页必须与最近一次观察锁定的是同一页。

        观察后用户切页/新前台页 → 旧坐标全部失效，零输入并显式报错；
        controller 据此要求重新 observe。
        """
        if self._observed_page_token is None:
            return
        if self._page_token(page) != self._observed_page_token:
            raise ComputerControlError(
                "computer_surface_changed",
                "受控页面已切换，旧坐标已失效，必须重新观察（observe）后再操作",
            )

    def _require_ref_valid(self, page, element_ref: str) -> None:
        """元素动作前复核（E2）：ref 必须属于最近一次观察且页面未变化。

        三项检查全部抛 computer_element_stale（与桌面 UIA 的 ref 失效语义
        一致，不发明新码；元素动作的错误码统一 stale，不用 surface_changed）：
        1. 页面 token——从未观察（None）与受控页切换（popup/新前台页）都让
           旧 ref 失效，但文案区分：前者引导先 observe，后者说明页面已切换；
        2. URL 对比——裁决点：token 不随同页导航变化（SPA 路由、整页跳转仍
           落在同一 Page 对象上），URL 对比补全「页面变化 → 旧 ref 失效」的
           产品决定，对齐验收场景 4（导航到新 URL 后旧 ref 必须 stale）；
        3. ref 集合——ref 不在最近一次 observe 收集的元素集合内。
        """
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
        if page.url != self._observed_url:
            raise ComputerControlError(
                "computer_element_stale",
                "页面地址已变化，元素引用失效，请重新 observe",
            )
        if element_ref not in self._observed_element_refs:
            raise ComputerControlError(
                "computer_element_stale",
                f"元素引用 {element_ref} 已失效，请重新 observe",
            )

    # ---------------- worker 内：命令实现 ----------------

    def _collect_interactive_elements(self, page) -> tuple:
        """收集视口内可交互元素（E1）；任何失败都归一为空 tuple，绝不中断观察。

        为什么吞掉异常：元素不可用时回落截图路径是产品决定（设计 1.5/8.2），
        observe 的截图与页面信息仍然有效；E2 侧凭空 ref 集合拒绝旧引用，
        不会静默点错。返回 tuple of dict（ref/kind/name/bounds/enabled），
        bounds 为 4 元 int list，与桌面 windows_elements_uia 的脱敏形状对齐。
        """
        try:
            raw = page.evaluate(ELEMENT_COLLECT_SCRIPT)
        except Exception:
            _LOGGER.warning("浏览器元素收集失败，本次观察回落截图路径", exc_info=True)
            return ()
        if not isinstance(raw, (list, tuple)):
            _LOGGER.warning(
                "浏览器元素收集返回非列表（%s），本次观察回落截图路径",
                type(raw).__name__,
            )
            return ()
        listing: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue  # 非 dict 项无法被引用，直接丢弃
            ref = item.get("ref")
            if not isinstance(ref, str) or not ref:
                continue  # 无 ref 的项无法被 E2 的 locator 引用
            kind = item.get("kind")
            name = item.get("name")
            enabled = item.get("enabled")
            listing.append({
                "ref": ref,
                "kind": kind if isinstance(kind, str) else "",
                "name": (name if isinstance(name, str) else "")[:_ELEMENT_MAX_NAME_CHARS],
                "bounds": _normalize_element_bounds(item.get("bounds")),
                "enabled": enabled if isinstance(enabled, bool) else True,
            })
        return tuple(listing)

    def _collect_page_text(self, page) -> str:
        """提取页面可见正文摘要（B 方案降级观察的文本来源）。

        为什么需要：窗口被遮挡/最小化后 WebView2 渲染被节流，截图会卡住或失败；
        DOM 求值不受渲染节流影响，所以正文是"没有截图的观察"里最可靠的信息。
        任何失败都归一为空串并留 warning，绝不中断观察（缺正文比观察失败更轻）。
        返回前按 _PAGE_TEXT_MAX_CHARS 码点截断，正文绝不写日志（隐私设计 22）。
        """
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
        """登记局部观察区域（CSS 视口坐标），并校验区域完整落在视口内。

        不 clamp：越界区域直接拒绝，避免模型拿到"看起来是区域、实际被截断"
        的图并按错误尺寸点坐标。
        """
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
        """把归一化后的整屏 PNG 裁成区域图，返回 (png_bytes, 观察字段)。

        PNG 此时已由 _normalize_frame 重采样到 CSS 视口尺寸，所以裁剪坐标
        与模型坐标同一空间；区域不再放大——原分辨率已经比整屏清楚数倍，
        放大只会插值糊化。
        """
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
        page = self._resolve_active_page()
        page_token = self._page_token(page)
        url = page.url if isinstance(page.url, str) else ""
        # E2：同步观察时的 URL，供 _require_ref_valid 做「页面地址已变化」校验
        # （token 不随同页导航变化，URL 对比是旧 ref 失效裁决的补全）。
        self._observed_url = url
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        # B 方案（降级观察）：先做不依赖渲染的 DOM 收集。窗口被遮挡/最小化时
        # 渲染被节流而 DOM 求值照常，先把元素与正文拿到手，保证"这次观察至少
        # 有文本信息"；截图放到最后，失败也只降级、不让整条观察链报错。
        pending_region = self._pending_region
        self._pending_region = None
        if pending_region is None:
            elements = self._collect_interactive_elements(page)
            page_text = self._collect_page_text(page)
        else:
            # 局部观察：模型要的是区域画面，不收集 DOM 清单（清空同时避免旧
            # el-N 与新坐标系混用）。
            elements = ()
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
            # 收集期间页面再次变化（活动页切换 / 同页导航）：元素与页面身份
            # 快照不同源，整体清空回落截图路径（E2 的 ref 集合随之置空，
            # 旧引用由 stale 校验拒绝）。
            elements = ()
        # 只有真正拿到 PNG 才落盘：降级路径不写文件、也绝不复用任何旧截图。
        if png_bytes:
            png_bytes = self._normalize_frame(png_bytes, page)
        # E5：局部观察在归一化之后裁剪（此时 PNG 与 CSS 视口同尺寸）。
        viewport = self._observation_viewport(page)
        region_fields: dict | None = None
        action_name = "observe"
        if pending_region is not None and png_bytes:
            png_bytes, region_fields = self._build_region_frame(png_bytes, pending_region)
            action_name = "observe_region"
        if region_fields is None:
            view_w = int(viewport["width"])
            view_h = int(viewport["height"])
            # 全屏观察：模型坐标就是 CSS 视口坐标。
            self._coord_frame = (0, 0, view_w, view_h, view_w, view_h)
            self._region_view = False
            observation_view = viewport
        else:
            # 区域观察：模型坐标以区域左上角为原点，点击时加回 origin。
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
        # ref 集合与页面身份同生命周期：每次观察以本次结果整体替换，
        # 空集即旧 ref 全部失效（E2 据此拒绝过期引用，不静默失败）。
        self._observed_element_refs = frozenset(item["ref"] for item in elements)
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
            elements=elements,
        )

    def _observation_viewport(self, page) -> dict:
        """观察坐标系：外部浏览器 = 启动视口常量（device_scale_factor=1，
        PNG 像素与坐标空间 1:1）。CDP 模式由子类覆写为页面真实视口。"""
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
            # E5：browser 局部观察——只登记区域，真正的裁剪发生在紧随其后的
            # observe（controller 在动作后统一再观察一次）。这里不产生输入。
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
        page = self._resolve_active_page()
        if kind in ("click_element", "set_value_element"):
            # E2：元素动作按 ref 校验（页面 token + URL + ref 集合），错误码
            # 统一 computer_element_stale；坐标动作维持 surface_changed 语义。
            self._require_ref_valid(page, action.element_ref)
        else:
            self._require_observed_surface(page)
        try:
            self._dispatch_action(page, action)
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
        # 修饰键回到按下时的原 page 释放（popup 切换 active page 后也不串页）。
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

    # ---------------- worker 内：可中断等待与导航预算（R8） ----------------

    def _remaining_budget_ms(self) -> int:
        """当前命令的剩余预算（毫秒）；与 R2 的 deadline 共用，不另起独立超时。"""
        deadline = self._current_deadline
        if deadline is None or deadline == float("inf"):
            return int(self._command_timeout * 1000)
        return max(1, int((deadline - time.monotonic()) * 1000))

    def _element_action_timeout_ms(self) -> int:
        """元素动作单段超时：10s 上限，并为命令收尾预留 1s 余量。

        防止 fill/click/type 累计耗时越过命令 deadline——越过会触发调用侧
        超时并封闭实例，而不是返回元素语义错误码（E2 评审修复）。
        """
        return max(1, min(_ELEMENT_ACTION_TIMEOUT_MS, self._remaining_budget_ms() - 1000))

    def _wait_interruptible(self, duration_ms: int) -> None:
        """显式 wait：服务取消/页面事件，stop 时立即中断（不用整段 sleep 伪装就绪）。"""
        end = time.monotonic() + duration_ms / 1000
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            if self._worker_stop.wait(min(remaining, 0.05)):
                raise ComputerControlError(
                    "computer_cancelled", "显式等待被停止中断"
                )

    # ---------------- worker 内：修饰键生命周期（R3） ----------------
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
        """逆序释放；up 失败保留记录供 release_inputs 重试，错误向上传播。"""
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
        """click/double_click/drag 的修饰键生命周期：down → 动作 → 逆序 up。"""
        if not action.keys:
            invoke()
            return
        pressed = self._press_keys(page, action.keys)
        try:
            invoke()
        finally:
            self._release_keys(pressed)

    def _dispatch_action(self, page, action: ComputerAction) -> None:
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
            # R7：浏览器滚动只接受 css_pixel（正值向下/向右）；像素语义与
            # Playwright mouse.wheel 一致，单位/范围在 parse 层已严格校验，
            # 此处防御性复核直接构造的 ComputerAction。
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
            # 复用 parse 层校验：只接受带主机名的 http/https URL。
            # R8：导航以 DOMContentLoaded 为文档基线，超时取命令剩余预算；
            # 慢子资源不再拖住整步；SPA 业务目标是否就绪由后续观察判定。
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
            self._exec_click_element(page, action.element_ref)
        elif kind == "set_value_element":
            self._exec_set_value_element(page, action.element_ref, action.text)
        else:
            raise ComputerControlError(
                "computer_invalid_action",
                f"浏览器 backend 不支持动作：{kind!r}",
            )

    def _raise_element_action_error(self, locator, element_ref: str, label: str, error: Exception) -> None:
        """元素动作失败分类：元素不在 DOM → stale（引导重新 observe）；存在但不可操作 → unsupported。

        同 URL 整页重载/DOM 重建时，token/URL/ref 集合三项校验都看不出来；
        以 locator 命中数兜底（E2 评审修复）。
        """
        try:
            missing = locator.count() == 0
        except Exception:
            missing = False
        if missing:
            raise ComputerControlError(
                "computer_element_stale",
                f"元素 {element_ref} 已不存在于页面，请重新 observe",
            ) from error
        raise ComputerControlError(
            "computer_element_unsupported",
            f"元素 {element_ref} {label}失败：{type(error).__name__}",
        ) from error

    def _exec_click_element(self, page, element_ref: str) -> None:
        """按元素引用点击（E2）：ref 有效性已在 _cmd_execute 校验。

        Playwright locator 白送自动等待/滚动到视口/可点击性检查；元素存在但
        不可点击（被遮挡/禁用/超时）时按元素语义报 computer_element_unsupported，
        与桌面元素动作错误码对齐。
        """
        locator = page.locator(f'[data-ycode-ref="{element_ref}"]')
        # E3：优先点「视觉可点击区域」中心。老式 inline 链接的自身盒可能退化
        # 成 1px 高且不覆盖图标，locator.click 会点空；实时求并集中心并复核
        # 命中后走鼠标坐标点击，命中失败才回落 locator.click（保留自动等待与
        # 可点击性检查）。求值失败一律回落，不让新路径成为新的失败面。
        timeout = self._element_action_timeout_ms()
        point = None
        try:
            point = page.evaluate(ELEMENT_HIT_SCRIPT, element_ref)
        except Exception:
            point = None
        if (
            isinstance(point, dict)
            and point.get("contains")
            and point.get("inViewport")
        ):
            try:
                page.mouse.click(int(point["x"]), int(point["y"]))
                return
            except Exception as error:
                self._raise_element_action_error(locator, element_ref, "点击", error)
        try:
            locator.click(timeout=timeout)
        except Exception as error:
            self._raise_element_action_error(locator, element_ref, "点击", error)

    def _exec_set_value_element(self, page, element_ref: str, text: str) -> None:
        """按元素引用写值（E2）：fill 优先，失败回落「点击聚焦 + 键盘输入」。

        fill 直接设值不经输入法（中文免疫 IME 乱码）且触发 input/change 事件；
        contenteditable/部分富文本编辑器不支持 fill，此时回落键盘输入。裁决：
        回落仍失败按与 click 失败对齐的语义报 computer_element_unsupported
        （不用坐标动作的 surface_changed / 命令超时错误码）。
        """
        locator = page.locator(f'[data-ycode-ref="{element_ref}"]')
        try:
            locator.fill(text, timeout=self._element_action_timeout_ms())
            return
        except Exception as error:
            _LOGGER.debug(
                "元素 %s fill 失败，回落键盘输入：%s", element_ref, type(error).__name__
            )
        try:
            # 回落 click 单独重算剩余预算：fill 已消耗的时间不得让 click 越过
            # 命令 deadline（越过触发调用侧超时并封闭实例，E2 评审修复）。
            locator.click(timeout=self._element_action_timeout_ms())
            page.keyboard.type(text)
        except Exception as error:
            self._raise_element_action_error(locator, element_ref, "写入", error)

    def _exec_drag(self, page, action: ComputerAction) -> None:
        points = action.path
        if len(points) < 2:
            raise ComputerControlError("computer_invalid_action", "drag 需要至少 2 个 path 点")
        # E5：逐点换算到 CSS 坐标（局部观察后模型坐标以区域左上角为原点）。
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
            # try/finally 保证 mouse.up；up 失败时保留按下标记供 release_inputs 重试。
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
        """模型坐标 → 浏览器 CSS 坐标（E5 起支持局部观察坐标系）。

        全屏观察时两者恒等；局部观察后模型坐标以区域左上角为原点，需要按
        「区域 CSS 尺寸 / 区域图片尺寸」缩放并加回 origin。模型坐标按当前
        观察画面做半开区间校验，越界拒绝而不是 clamp。
        """
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

    # ---------------- worker 内：截图短期存储 ----------------

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

    # ---------------- 命令 marshal 与进程内 profile 锁 ----------------

    def _next_command_id(self) -> int:
        with self._state_lock:
            self._command_seq += 1
            return self._command_seq

    def _admission_error(self) -> ComputerControlError | None:
        """admission 检查（调用方须持有 _state_lock）；不可投递时返回错误。"""
        if self._closed:
            return ComputerControlError("computer_browser_closed", "浏览器 backend 已关闭")
        if self._failed:
            return ComputerControlError(
                "computer_backend_error",
                "浏览器 backend 已封闭：先前命令执行超时且结果未知，拒绝后续输入",
            )
        if not self._started:
            return ComputerControlError(
                "computer_browser_unavailable",
                "浏览器 backend 尚未启动",
            )
        if self._transport is None:
            # 线程模式：worker 线程存活即 admission；进程模式由 transport 判断。
            if self._worker is None or not self._worker.is_alive():
                return ComputerControlError(
                    "computer_browser_closed",
                    "浏览器 worker 已退出，浏览器不可用",
                )
        return None

    def _call(self, description: str, fn: Callable, timeout: float | None = None):
        budget = self._command_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + budget
        with self._state_lock:
            admission = self._admission_error()
        if admission is not None:
            raise admission
        command = _Command(self._next_command_id(), fn, deadline)
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
            with self._state_lock:
                admission = self._admission_error()
            if admission is not None:
                raise admission
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ComputerControlError(
                    "computer_action_timeout",
                    f"浏览器命令排队超时：{description}",
                )
            try:
                self._queue.put(command, timeout=remaining)
            except queue.Full as error:
                raise ComputerControlError(
                    "computer_action_timeout",
                    f"浏览器命令排队超时：{description}",
                ) from error
            with self._state_lock:
                closed_after_put = self._closed
            if closed_after_put:
                # close 已抽空队列：本条若在抽空后入队，worker 不会再来取；
                # 立即按已关闭撤销，不让调用方悬挂到超时。
                if command.cancel_if_queued(
                    "computer_browser_closed", "浏览器 backend 已关闭，排队命令已撤销"
                ):
                    raise command.error
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
        """done.wait 超时后的终态裁决：排队未执行可撤销；执行中结果未知即封闭。"""
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
                # 恰在超时边界完成：回落到正常终态分支返回结果或错误。
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
