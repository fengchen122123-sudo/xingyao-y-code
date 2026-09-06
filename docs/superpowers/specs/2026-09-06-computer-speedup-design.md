# 电脑操控提速与截图清理设计补遗（2026-09-06）

依据：会话 147 实测分析（111 次 computer_action 仅 20.1 秒工具执行 vs 2239 秒总耗时；
IME 乱码绕路 8 步；RepeatCallGuard 误拦 10 次；逐屏滚动读消息约 45 步）。
调研参照：microsoft/UFO（Speculative Multi-Action，官方数据减少约 51% LLM 调用）、
CursorTouch/Windows-MCP（Screenshot + Snapshot 双轨观察、元素 ID）、
cluic/wxauto（微信 UIA 路线验证；其插件化产品形态经用户裁决不采纳——账号风险）。

## 1. sequence 批量动作（阶段一）

- kind=`sequence`，字段 `steps`（1~10 个子动作）。子动作白名单：click/double_click/
  move/drag/scroll/type_text/keypress/wait；嵌套 sequence、observe、语义定位、
  request_user_takeover、navigate 一律 `computer_invalid_action` 拒绝。
- 子动作 target 必须与 sequence 本身一致；逐步复用 `parse_computer_action` 严格解析。
- controller 串行执行子动作（每步前复核取消/状态），中途不截图；整段计入
  input_ms，计 1 个 action ordinal，参与无进展计数（不在豁免清单）。
- 脱敏投影：`steps_count` + 逐步 `sanitized_action_summary`（正文/按键仍只留哈希）。
- schema 层 items 故意宽松（避免 tools 字节膨胀），严格校验在 parse 层。

## 2. type_text 剪贴板路径（阶段一）

- desktop 的 type_text 文本含任一非 ASCII 字符时：Win32 写剪贴板
  （CF_UNICODETEXT，OpenClipboard 10 次重试）→ 0.05s 间隔 → Ctrl+V。
  纯 ASCII 保持 KEYEVENTF_UNICODE 逐键注入。
- 根因：SendInput Unicode 注入在 IME 激活/微信输入处理下产生乱码（会话 147）。
- 取舍：不恢复用户原剪贴板（无可靠事务语义，与业界 RPA 一致）。
- seam：`Win32InputAdapter(set_clipboard_text=...)` 可注入，默认
  `_ctypes_set_clipboard_text`；失败统一 `computer_backend_error`。

## 3. RepeatCallGuard 豁免（阶段一）

- `computer_action` 的 kind ∈ {observe, scroll, wait, move, list_surfaces} 不消耗
  重复额度（合法重复：逐屏滚动/重观察/等待）。有副作用动作（click/type_text/
  keypress/drag/sequence）不豁免——同参数重复点击可能重复发送；真死循环由
  controller no-progress 熔断（连续 3 次画面与指纹不变即停止）兜底。

## 4. 截图清除（阶段一）

- DB 层：`MemoryManager.fold_session_computer_media(session_id)` 折叠该会话全部
  带 `_computer_v1` 元数据的观察消息图片块（含最新一条，占位符与既有折叠文本
  一致，幂等，一次性 DB 改写）。接线点：`llm.py:_finalize_computer_run` 在
  complete/stop 收尾后调用；provider_safety_required 提前 return 不折叠（run 仍
  存活，恢复后需要最后一张图）。与 `_fold_stale_media_locked`（保留最新一条、
  职责为运行中上下文）互斥不冲突。
- 磁盘层：backend 关闭删整个临时目录（既有）；启动时
  `sweep_stale_computer_temp_dirs()` 清扫 mtime 超 24h 的
  `ycode-computer-desktop-* / ycode-computer-browser-* / ycode-computer-preview`
  残留（时间窗防多实例误删）。
- 用户明确保留：截图本身保持 1920 PNG 不压缩。

## 5. UIA 元素观察与元素引用动作（阶段二，a11y-first）

仿 Windows-MCP Snapshot / ZCode get_app_state 的「文本观察优先」：

- 新 kind（desktop only）：`list_elements`（收集前台窗口可交互控件，BFS，
  ≤200 个、深度 14、name 截 80 字符）、`click_element`（InvokePattern）、
  `set_value_element`（ValuePattern.SetValue，中文直接写值不过输入法/剪贴板）。
- 底层：复用既有 comtypes UIAutomationCore 通道（windows_surfaces._ComThread
  模式，专用 COM 线程），不引入 uiautomation 新依赖。
- ref 生命周期与 surface_candidates 一致：仅在最近一次 list_elements 后有效，
  过期抛 `computer_element_stale`；backend 未启用定位器抛
  `computer_elements_unavailable`。
- follow-up 消息双路径：观察携带 elements 时为纯文本控件列表（请求从 ~900KB
  降到几 KB）；否则维持截图 data URL 路径，普通观察字节不变。
- RepeatCallGuard 豁免集合补充 `list_elements`。

## 6. 已取消（用户裁决 2026-09-06）

- 阶段三微信插件（wechat_tool）不实施：微信自动化有账号风险。微信场景提速
  完全依赖上述内核协议能力。

## 验证基线

- `tests/test_prompt_byte_stability.py` 全绿无需重基线：computer_action 是动态
  追加工具、动态提示在动态块，不在冻结 golden 集合内；守卫测试验证的是
  确定性/顺序/稳定前缀哈希。
