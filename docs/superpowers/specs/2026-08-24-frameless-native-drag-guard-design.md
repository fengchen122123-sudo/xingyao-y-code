# 无边框标题栏原生拖动状态守卫设计

日期：2026-08-24

## 背景与根因

用户完成“标题栏双击最大化 → 双击还原”后，继续点击或拖动标题栏时，鼠标会短暂显示 Windows 禁用光标，窗口无法立即拖动。

视频画面与 `gui-runtime.log` 已对齐。异常发生时：

- 前端仍调用 `window_begin_drag`；
- 调用进入 GUI 线程只需约 0.3–0.5 ms，不存在还原动画或线程排队造成的明显延迟；
- Windows `GetAsyncKeyState(VK_LBUTTON)` 返回未按下；
- 现有实现仍执行 `ReleaseCapture` 和 `SendMessageW(SC_MOVE + HTCAPTION)`。

无边框窗口把标题栏区域注册为原生 `HTCAPTION` 命中区后，WebView 并不能保证每次都收到对应的 DOM `mouseup`。因此，仅依靠前端 `mouseup` 清理 400 ms 拖动定时器并不可靠。定时器在左键已经松开后触发，导致原生移动命令在无有效按键手势的情况下启动，Windows 随即显示禁用光标。

## 方案比较

### 方案 A：原生桥接入口增加权威状态守卫（采用）

在 GUI 线程真正执行 `SC_MOVE` 前读取 `VK_LBUTTON`。如果左键已松开，明确拒绝该过期请求，不调用 `ReleaseCapture`，也不发送 `SC_MOVE`。

优点：使用 Windows 当前真实输入状态作为最终判据，能覆盖 WebView 丢失 `mouseup`、定时器竞态和桥调用排队等情况；改动集中，正常拖动、双击与 Aero Snap 路径保持不变。

代价：拒绝结果需要通过桥返回值和日志明确呈现，不能静默吞掉。

### 方案 B：继续加强前端事件追踪（不采用）

增加 `pointerup`、`pointercancel`、`blur`、`mouseleave` 等监听，尝试覆盖缺失的 `mouseup`。

问题：这些事件与 `mouseup` 一样受 WebView/原生非客户区消息分发影响，仍无法成为权威状态；监听组合会扩大前端状态机复杂度，却不能消除竞态。

### 方案 C：重写标题栏交互为完整原生非客户区行为（不采用）

移除前端延迟拖动状态机，让窗口过程统一处理命中、双击和移动。

问题：涉及 pywebview、WinForms 和现有自绘控件的较大改造，风险与当前单一缺陷不匹配。

## 详细设计

### 原生拖动入口

修改 `gui_bridge.py::window_begin_drag` 内部的 GUI 线程回调：

1. 获取当前左键状态 `GetAsyncKeyState(VK_LBUTTON)`。
2. 若左键未按下：
   - 不调用 `ReleaseCapture`；
   - 不调用 `SendMessageW(SC_MOVE + HTCAPTION)`；
   - 返回 `ok: false` 和稳定、可测试的错误原因 `left_button_released`；
   - 记录一条简洁诊断日志，说明拒绝了过期拖动请求。
3. 若左键仍按下，保持现有原生移动循环和贴边快捷处理不变。

状态检查必须放在 GUI 线程内、紧邻 `ReleaseCapture` 和 `SendMessageW`，不能只在桥请求线程提前检查，否则请求入队后仍存在按键状态变化竞态。

### 前端行为

`Topbar.vue` 现有双击判定、位移阈值、定时器和清理逻辑保持不变。本次修复不再增加新的 DOM 事件监听，也不改变 400 ms 双击判定窗口。

### 诊断日志

当前为定位问题加入的详细逐次计时日志属于临时诊断代码。实施时删除高频诊断输出，只保留“因左键已释放而拒绝过期拖动请求”的低噪声日志，避免正常拖动持续污染运行日志。

## 测试设计

先增加失败测试，再修改实现：

1. 左键已释放：验证返回 `ok: false`、错误为 `left_button_released`，且未调用 `ReleaseCapture` 和 `SendMessageW`。
2. 左键仍按下：验证继续调用 `ReleaseCapture` 和 `SendMessageW(SC_MOVE + HTCAPTION)`，返回成功。
3. 保持已有前端标题栏测试全部通过，确认双击最大化/还原、第三次按下拖动、`mouseup` 清理和旧定时器隔离没有回归。

## 验收标准

- 视频中的复现步骤不再出现 Windows 禁用光标。
- 双击最大化、双击还原后可立即开始有效拖动。
- 正常标题栏拖动和 Aero Snap 行为不变。
- 左键已经释放的过期拖动请求不会进入 Windows 原生移动循环。
- 前端完整测试、相关 Python 测试、语法检查与生产构建全部通过。

## 范围边界

本次只修复无有效左键按下时仍启动 `SC_MOVE` 的问题。不重构无边框窗口架构，不调整双击时间，不改变窗口按钮、缩放或其他输入行为。
