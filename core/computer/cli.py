"""CLI 电脑操控运行控制与脱敏显示（设计第 17 节）。

命令固定为 /computer status|pause|resume|stop：只做运行控制，绝不修改
永久设置开关（GUI 系统设置页专属）；shlex.split 后必须恰好两个 token，
不接受别名或额外参数。显示全部为纯文本（无 Markdown 强调符号），动作
摘要脱敏：输入文本只显示长度、URL 只显示 host。

本模块不导入 main/GUI/Provider/Playwright，只依赖 controller 公开 API
与 ComputerRunSnapshot 公开字段；controller listener 侧只产生待投递
文本，绝不直接打印（线程安全铁律见 AGENTS.md）。
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from core.computer.models import ComputerControlError, ComputerRunSnapshot

_COMMANDS = {"status", "pause", "resume", "stop"}

# 存在可作用于 pause/resume/stop 的活动 run 的状态集合（终态与 idle 之外）。
_ACTIVE_RUN_STATUSES = frozenset(
    {
        "starting",
        "running",
        "paused",
        "waiting_for_user",
        "provider_safety_required",
        "completing",
        "stopping",
    }
)

_USAGE_TEXT = "用法：/computer status|pause|resume|stop；永久开关请在 GUI 系统设置页修改"
_NO_ACTIVE_RUN_TEXT = "电脑操控: 当前没有进行中的操控任务（可先 /computer status 查看状态）"
# Task 14 审查裁决：stopping 窗口内 pause/resume/stop 的 computer_invalid_state
# 内部文案（"非法状态转换：stopping → stopping"）改为友好提示。
_STOPPING_RUN_TEXT = "电脑操控: run 已在停止中，请稍候（可用 /computer status 查看进度）"

_STATUS_LABELS = {
    "starting": "启动中",
    "running": "运行中",
    "paused": "已暂停",
    "waiting_for_user": "等待用户接管",
    "provider_safety_required": "Provider 安全检查",
    "completing": "完成中",
    "completed": "已完成",
    "stopping": "停止中",
    "stopped": "已停止",
    "failed": "失败",
}

_ACTION_LABELS = {
    "observe": "观察画面",
    "click": "点击",
    "double_click": "双击",
    "move": "移动鼠标",
    "drag": "拖拽",
    "scroll": "滚动",
    "keypress": "发送按键",
    "wait": "等待",
    "back": "后退",
    "forward": "前进",
    "reload": "刷新",
    "request_user_takeover": "请求用户接管",
}


@dataclass(frozen=True)
class ComputerCommandResult:
    """/computer 命令结果：ok + 单行纯文本（无 Markdown 强调符号）。"""

    ok: bool
    text: str


COMPUTER_CLI_EVENT_KIND = "computer_cli_line"


@dataclass(frozen=True)
class ComputerCliEvent:
    """电脑操控动作摘要经现有 CLI UI 事件队列投递的最小信封（设计 17.3）。

    listener 在 worker 线程只 put 队列；主线程 _render_ui_event 读取
    payload["text"] 单行打印，绝不在此处直接输出。
    """

    kind: str = COMPUTER_CLI_EVENT_KIND
    payload: dict = field(default_factory=dict)
    turn_id: str = "computer-control"


def _target_label(snapshot: ComputerRunSnapshot) -> str:
    if snapshot.target == "desktop":
        return "桌面"
    browser = (snapshot.browser or "").strip()
    if browser.lower() == "edge":
        return "Edge"
    if browser.lower() == "chrome":
        return "Chrome"
    if browser:
        return browser
    return snapshot.target or "未绑定"


def render_computer_snapshot(snapshot: ComputerRunSnapshot) -> str:
    """把 run 快照渲染为一行纯文本状态（无 Markdown 强调符号）。

    无活动 run（idle/空 run id）视为关闭并提示到 GUI 系统设置页开启；
    有 run 时固定为 ``电脑操控: 状态 · 目标 · N 步``，失败态追加错误码。
    """
    if not snapshot.run_id or snapshot.status == "idle":
        return "电脑操控: 已关闭（请在 GUI 系统设置页开启）"
    label = _STATUS_LABELS.get(snapshot.status, snapshot.status)
    parts = [f"电脑操控: {label}", _target_label(snapshot), f"{snapshot.action_count} 步"]
    text = " · ".join(parts)
    if snapshot.error_code:
        text = f"{text} · 错误码 {snapshot.error_code}"
    return text


def render_computer_status_bar(snapshot: ComputerRunSnapshot) -> str:
    """CLI 底部状态栏专用段（设计 17.2，Task 14 审查裁决①）。

    活动 run 才返回 ``电脑操控: 运行中 · Edge · N 步``；idle/终态/空 run
    返回空串（状态栏保持既有布局，绝不常驻占位）。
    """
    if not snapshot.run_id or snapshot.status not in _ACTIVE_RUN_STATUSES:
        return ""
    return render_computer_snapshot(snapshot)


def _url_host(url) -> str:
    """URL 只保留 host：剥掉 userinfo、路径、查询与锚点；非法输入返回空。"""
    try:
        host = urlsplit(str(url)).hostname or ""
    except ValueError:
        return ""
    host = host.strip().strip(".")
    if not host or not all(ch.isalnum() or ch in ".-" for ch in host):
        return ""
    return host


def _typed_length(event: dict) -> int:
    length = event.get("text_length")
    if isinstance(length, bool) or not isinstance(length, int) or length < 0:
        text = event.get("text")
        length = len(text) if isinstance(text, str) else 0
    return length


def render_computer_event(event: dict) -> str:
    """把动作事件渲染为一行脱敏摘要：输入只显示长度，URL 只显示 host。"""
    kind = str(event.get("action_kind") or event.get("action") or "")
    if kind == "type_text":
        return f"电脑操控: 输入 {_typed_length(event)} 个字符"
    if kind == "navigate":
        host = _url_host(event.get("url"))
        return f"电脑操控: 打开页面 {host}" if host else "电脑操控: 打开页面"
    label = _ACTION_LABELS.get(kind)
    if label:
        return f"电脑操控: {label}"
    return f"电脑操控: 动作 {kind}" if kind else "电脑操控: 电脑动作"


def render_computer_cli_event(event: dict) -> str | None:
    """把 controller 事件映射为 CLI 单行显示文本；无需显示时返回 None。

    只显示动作开始摘要与失败动作（含错误码）；状态迁移与观察事件由
    /computer status 与命令结果承担，不在事件流里重复刷屏。
    """
    event_type = str(event.get("type", ""))
    if event_type == "computer_action_started":
        return render_computer_event(event)
    if event_type == "computer_action_finished":
        if not event.get("ok", True):
            code = str(event.get("error_code") or "").strip() or "computer_backend_error"
            return f"电脑操控: 动作失败 · 错误码 {code}"
        return None
    return None


def handle_computer_command(command: str, controller) -> ComputerCommandResult:
    """严格解析并执行 /computer 命令（设计 17.1）。

    shlex.split 后必须恰好两个 token 且子命令在白名单内；status 只读快照，
    pause/resume/stop 要求活动 run 并把 controller.snapshot() 的当前 run id
    传给 controller（run_id 校验语义与 GUI Bridge 一致）。任何路径都不修改
    永久设置键。
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ComputerCommandResult(ok=False, text=_USAGE_TEXT)
    if len(tokens) != 2 or tokens[0].lower() != "/computer":
        return ComputerCommandResult(ok=False, text=_USAGE_TEXT)
    sub = tokens[1].lower()
    if sub not in _COMMANDS:
        return ComputerCommandResult(ok=False, text=_USAGE_TEXT)

    snapshot = controller.snapshot()
    if sub == "status":
        return ComputerCommandResult(ok=True, text=render_computer_snapshot(snapshot))
    if not snapshot.run_id or snapshot.status not in _ACTIVE_RUN_STATUSES:
        return ComputerCommandResult(ok=False, text=_NO_ACTIVE_RUN_TEXT)
    if snapshot.status == "stopping":
        # Task 14 审查裁决②：stopping 窗口内的控制命令不再触发
        # computer_invalid_state（内部状态机文案），改回友好提示；run 已在
        # 固定停止顺序中收尾，无需也不得重复 stop。
        return ComputerCommandResult(ok=False, text=_STOPPING_RUN_TEXT)

    try:
        if sub == "pause":
            next_snapshot = controller.pause(snapshot.run_id, "cli_pause")
        elif sub == "resume":
            next_snapshot = controller.resume(snapshot.run_id)
        else:
            next_snapshot = controller.stop(snapshot.run_id, "cli_stop")
    except ComputerControlError as error:
        return ComputerCommandResult(
            ok=False, text=f"电脑操控: {error.message}（错误码 {error.code}）"
        )
    return ComputerCommandResult(ok=True, text=render_computer_snapshot(next_snapshot))


def stop_active_run_for_cancel(controller) -> bool:
    """Ctrl+C 路径：存在活动 run 时先 controller.stop(run_id, "cli_ctrl_c")。

    controller.stop 内部按固定顺序触发 owner cancel 与 Provider 在飞流
    中断，因此必须先于 Turn 级取消执行；run 在快照与停止之间已结束
    （竞态）时视为无事发生。
    """
    snapshot = controller.snapshot()
    if not snapshot.run_id or snapshot.status not in _ACTIVE_RUN_STATUSES:
        return False
    try:
        controller.stop(snapshot.run_id, "cli_ctrl_c")
    except ComputerControlError:
        return False
    return True


def make_cli_event_listener(
    post_event: Callable[[ComputerCliEvent], None],
) -> Callable[[dict], None]:
    """构建 controller listener：worker 线程触发，只投递现有 UI 事件队列。

    绝不直接 print_formatted_text 或改 layout（AGENTS.md 线程安全铁律）；
    显示文本在此预先脱敏渲染，主线程只负责打印。
    """
    def _on_computer_cli_event(event: dict) -> None:
        text = render_computer_cli_event(event)
        if text:
            post_event(ComputerCliEvent(payload={"text": text}))

    return _on_computer_cli_event
