# 交接文档：Y-CODE 浏览器与 Windows 电脑操控实施（2026-09-03 第二次交接）

## 当前状态速览

- **分支**：`feature/computer-control-wip`（2026-09-03 应用户要求新建；Task 1–9 全部电脑操控代码已提交在此分支）。分支基于 dev 当前 HEAD `612e6ba`（该 commit 含并发会话已提交到 dev 的 hunyuan 重构，属 dev 既有历史，与本功能无关）。旧分支 `feature/computer-control` 停在 `400366c`（无任何提交），可忽略。
  注意：本仓库多会话共享同一工作区，期间工作区曾被并发会话切回 dev；接手时先用 `git branch --show-current` 确认在 `feature/computer-control-wip` 再动代码。
- **总进度**：17 个 Task 中 **Task 1–8 完成且全部通过审查**（Task 8 含 1 轮修复轮，RE-REVIEW PASS）；**Task 9 已实现、门禁全绿，但审查被中断尚未执行**——接手 agent 的第一个动作是派发 Task 9 reviewer（审查包已生成好，见下）。
- **测试现状**（2026-09-03 实测）：
  - Task 9 门禁（tool_adapter + provider_generic + tool_pipeline + tool_registry）：116 passed；
  - golden（prompt byte-stability + provider payload）：17 passed，期望零改动；
  - computer 套件（`pytest tests/ -q -k computer`）：450 passed；
  - 全量 pytest：2338 passed / 2 skipped / **1 failed**——`tests/test_tokenrhythm_image.py::PluginMetadataTest::test_plugin_json_disabled_by_default`，由并发会话把 `plugins/tokenrhythm_image/plugin.json` 的 `enabled` 翻转为 `true` 导致。该文件属其他会话的禁触文件，与本功能零关联，单跑可复现，**不计入本功能失败**。
- **执行模式**：superpowers:subagent-driven-development（每 Task：task-brief → implementer 子代理 → 手工审查包 → reviewer 子代理 → 修复轮 → ledger）。全部证据在 `.superpowers/sdd/2026-09-02-computer-control/`（该目录被 gitignore，仅存在于本机工作区）。

## 接手 agent 第一个动作：完成 Task 9 审查

Task 9（固定 generic 工具、follow-up 多模态消息与批处理约束）实现已完成并自验全绿，但 reviewer 派发被用户中断。审查包已生成：`.superpowers/sdd/2026-09-02-computer-control/task-9-review-package.txt`（git status + diff + 4 个新文件全文）。

派发 reviewer（**必须用 general-purpose 子代理，Explore 型子代理只返回代码摘录不产出审查结论，实测两次均如此，勿再试**），审查镜头：

1. 默认兼容：`ToolResult`/`ToolExecutionRecord` 新增 `followup_messages` 默认空不可变 tuple；现有工具零变化；tool message 恒 4 键（role/tool_call_id/name/content）。
2. 保留名防线：`ToolRegistry.register` 拒绝 `computer_action`/`computer_select_target`（插件加载器记 failed）；MCP 合并（`core/llm.py:get_tools_schema`）过滤保留名；默认 registry 与 get_tools_schema 输出不含保留名。
3. 默认字节零变化：无保留名时 tools 字节不变；golden 套件原样通过。
4. 固定适配：resolve_tool 之前识别保留名；`ComputerTurnLease` 校验（伪造名不能绕过设置）；ToolSpec 四语义（requires_permission=False / cancellable=True / parallel_safe=False / mutates_files=False）。
5. generic schema 与动态提示仅 enabled+generic 返回；七条说明语义齐备。
6. 批处理约束：多 computer call 或与其他工具混用 → `computer_invalid_action` 整批拒绝零副作用。
7. 隐私：tool result 恰好 9 键 compact JSON；PNG base64 只在 follow-up user 消息；输入正文不进 JSON/metadata/log。
8. 实施者自述三项偏差裁决：(a) `core/llm.py` 2 行 MCP 过滤接线（Task 9 Files 未列 llm.py，但 MCP 合并唯一现场在此，最小调整+披露）；(b) `_computer_v1.ordinal` 取 `controller.snapshot().action_count`；(c) follow-up 持久化 / validate 调度层接线 / memory 折叠兼容留 Task 10。
9. 无越界：未触碰禁触文件；无 fallback/吞异常/放宽断言。

有 Critical/Important → 修复轮（先写失败测试取得 RED 再修，resume 原 implementer 或新派 implementer 均可）；通过后更新 ledger 与计划文件，再进 Task 10。

## 必读文件（接手 agent 第一步完整读取）

1. `AGENTS.md`
2. `docs/superpowers/specs/2026-09-02-computer-control-design.md`（设计规格；12.3 节已按 2026-09-03 用户裁决修订）
3. `docs/superpowers/plans/2026-09-02-computer-control.md`（实施计划；Task 1–9 已有真实 Execution Record 且 checkbox 已勾选）
4. `docs/superpowers/plans/2026-09-02-computer-control-k3-execution-prompt.md`（K3 执行纪律，全部仍然有效）
5. `.superpowers/sdd/2026-09-02-computer-control/progress.md`（SDD ledger）
6. `docs/superpowers/plans/2026-09-03-computer-control-continuation-prompt.md`（本次交接配套的续作提示词）

## SDD 工作流（每个 Task 严格重复）

```powershell
# 1. 生成任务 brief——注意：本机 PowerShell 无 bash，task-brief 脚本不可直接运行，
#    用下面的 PowerShell 等价命令（把 <N> 换成 Task 号；正则已处理 11a/11b/13a/13b 等）：
$n = '<N>'
$lines = Get-Content "docs/superpowers/plans/2026-09-02-computer-control.md"
$infence = $false; $intask = $false; $out = @()
foreach ($line in $lines) {
  if ($line -like '```*') { $infence = -not $infence }
  if (-not $infence -and $line -match '^#+[ \t]+Task[ \t]+[0-9]+') {
    $intask = ($line -match ('^#+[ \t]+Task[ \t]+' + $n + '([^0-9]|$)'))
  }
  if ($intask) { $out += $line }
}
$out | Out-File ".superpowers/sdd/2026-09-02-computer-control/task-$n-brief.md" -Encoding utf8
"wrote task-$n-brief.md: $($out.Count) lines"
```

```text
# 2. 派发 implementer 子代理（Agent 工具，subagent_type=general-purpose，前台），
#    模板：开头一句 Task 位置 + 已完成前置；brief 路径"先读，精确值逐字使用"；
#    列已存在可复用接口；硬性纪律（TDD/禁 commit/git add -N/diff check/禁真实键鼠浏览器/
#    不碰禁触文件/不碰 golden）；GREEN 与回归命令照 brief；Execution Record 追加到计划文件
#    + 勾选 checkbox；报告写到 task-N-report.md。

# 3. 完成后手工生成审查包（无 commit）：
#    git status --porcelain + git diff（本 Task 触碰的 tracked 文件）+ 新文件全文
#    → .superpowers/sdd/2026-09-02-computer-control/task-N-review-package.txt

# 4. 派发 reviewer 子代理（general-purpose，只读），给 brief/report/审查包三路径
#    + 设计文档相关章节号 + 审查镜头逐条。允许跑只读 pytest 复验。

# 5. 有 Critical/Important → 修复轮（先 RED 再修）→ scoped re-review（最多 5 轮）。
#    与计划/设计文本冲突的 finding → 问用户裁决，不得自行选择。

# 6. 通过后往 progress.md 追加条目，更新 todo，进入下一 Task。
```

## 环境要点（本机实测，接手 agent 必读）

1. **无 bash**：`task-brief` 脚本无法按交接模板原样运行，用上面的 PowerShell 等价命令。
2. **reviewer 用 general-purpose 子代理**：Explore 型子代理不产出审查结论（实测两次只返回代码摘录，SendMessage resume 也无效——agent 已结束无法 resume）。
3. **沙箱噪音**：部分 shell 命令结尾出现 `TRAE Sandbox Error: hit restricted ... EFSClipboardCtrl.log` 报错，但命令实际已成功执行（文件已生成/测试已跑完）。按实际产物判断成败，忽略该噪音。验证方法：检查目标文件是否存在/测试输出行。
4. **venv 命令**：`venv\Scripts\python.exe`；前端定向测试 `npm --prefix frontend test -- --run src/...`（不带 frontend/ 前缀）。
5. **预存失败**：`tests/test_tokenrhythm_image.py` 1 条失败属其他会话（plugin.json enabled 翻转），禁触不修，不计入本功能。
6. **大字符串参数化测试带短 ids**（Windows PYTEST_CURRENT_TEST 环境变量 32767 上限，Task 2 踩过）。
7. **提交纪律变更**：经用户 2026-09-03 授权，Task 1–9 已提交到 `feature/computer-control-wip`。后续 Task 是否逐 Task 提交由用户指示；默认仍不主动 commit。

## 已存在可复用接口（Task 1–9 汇总，后续 Task 直接 import）

- `core/computer/models.py`：5 个 str+Enum、`ComputerControlError(code, message, details)`、`ComputerOwner`、`ComputerAction`（frozen，text repr=False，end_x/end_y 恒 None）、`ComputerObservation`、`ComputerCapability`、`ComputerRunSnapshot`（`to_public_dict()`）、runtime_checkable `ComputerBackend` Protocol。
- `core/computer/actions.py`：`COMPUTER_ACTION_TOOL_SCHEMA`（含设计 10.1 全部 7 条说明）、`parse_computer_action()`（严格白名单）、`observation_provider_projection()`（恰好 9 键）、`_parse_url`（私有，browser_backend 已复用）。
- `core/computer/settings.py`：`load_computer_settings()`、三个 setter（preflight 失败不落盘；关闭先落盘再增 generation）、`list_browser_options()`、`get_browser_profile_dir()`、`get_settings_generation()`；错误码 `computer_invalid_config`/`computer_preflight_failed`/`computer_session_busy`。
- `core/computer/capability.py`：`resolve_computer_capability(model, provider, settings, turn_source)` 纯函数；provider 字段名是 `protocol`（"openai"/"responses"），官方 base = urlsplit 后 hostname 恰为 `api.openai.com`。
- `core/computer/hotkey.py`：`EmergencyStopHotkey(api, on_stop)`、`get_emergency_hotkey_snapshot()`、`EMERGENCY_HOTKEY_LABEL="Ctrl+Alt+Esc"`。
- `core/computer/preview_server.py`：`ComputerPreviewServer(root)`，token URL `/computer-preview/{token}/{run_id}/{ordinal}.png`，保留最近 3 张。
- `core/computer/browser_backend.py`：`BrowserComputerBackend(browser, profile_dir, playwright_factory=None)`；`active_page` 只读测试 seam。
- `core/computer/win32_adapter.py`：`Win32InputAdapter`（全 seam 注入）；`core/computer/windows_backend.py`：`WindowsComputerBackend(image_grab, win32_adapter)`、`screenshot_to_physical(x, y, observation)`。
- `core/computer/controller.py`：`ComputerControlService`（start_run/execute/complete_run/pause/resume/stop/snapshot/enter_provider_safety_required/add_listener/remove_listener；构造注入 backend_factory/settings_loader/generation_provider/abort_streams）、`get_computer_control_service()` 惰性单例；四类事件小 JSON 含 run_id+event_seq 严格递增；孤儿 backend best-effort close（`_close_backend_quietly`）；未绑定 run 的 generation 检查按动作 target 判定（effective_target）。
- `core/computer/tool_adapter.py`（Task 9）：`RESERVED_COMPUTER_TOOL_NAMES`、`ComputerTurnLease`（capability+owner+controller+run_id，Task 10 注入 `ToolExecutionContext.computer_lease`）、`computer_action_tool_spec()`（四语义）、`lease_from_context()`、`computer_turn_context_error()`。
- `core/computer/provider_generic.py`（Task 9）：`build_generic_computer_tool(capability)`、`build_generic_dynamic_prompt(capability)`、`validate_computer_tool_batch(tool_calls)`、`execute_reserved_computer_tool(decoded, context)`、`OBSERVATION_FOLLOWUP_TEXT`。
- 管线扩展（Task 9）：`ToolResult.followup_messages`/`ToolExecutionRecord.followup_messages`（默认空 tuple）、`ToolExecutionContext.computer_lease`、`execute_decoded` 在 resolve_tool 前固定适配（私有暂存键 `_ycode_followup_messages`，execute_batch/execute_one 均剥离）、`tool_registry.register` 保留名拒绝 + `filter_reserved_tool_schemas()`、`llm.get_tools_schema` MCP 合并过滤。
- `core/command_shell.py:get_system_settings()`：聚合 6 静态键 + hotkey 两键 + `computer_run` 键（全部惰性导入）。
- `tests/computer_fakes.py`：`use_temp_config`、`enabled_settings`、`openai_compatible_provider`、`GENERIC_CAPABILITY`/`NATIVE_BROWSER_CAPABILITY`/`DISABLED_CAPABILITY`、`FakeHotkeyApi`、`fake_png_bytes`、`http_status`、`FakePlaywrightRuntime` 家族、`FakeImage`、`FakeWin32Adapter`、`FakeComputerBackend`、`FakeController`。**保留全部已有导出**，后续 Task 按契约继续扩展（capture_responses_request/capture_continuation_request/read_text_surfaces/sample_computer_actions/SafetyCheck 尚未建）。

## 跨 Task 契约传递（ledger 汇总，dispatch 必须携带）

1. **Task 10 接线点（来自 Task 9 移交）**：① Turn 起点解析 capability 后 `start_run` 取 run_id，构造 `ComputerTurnLease` 注入 `ToolExecutionContext.computer_lease`；② 收到 tool_calls 后、执行前调用 `validate_computer_tool_batch`；③ 现有提交点先按 ordinal 持久化全部 tool results、再按相同 ordinal 追加 `record.followup_messages`；④ `core/memory.py` 媒体折叠兼容（`_content_has_media`/`_fold_stale_media_locked` 识别 content 列表与带 `_computer_v1` 的完整消息 dict，折叠幂等不删私有元数据，普通消息折叠字节不变）。
2. **Task 16 待办**：修 `win32_adapter.py` 的 `OpenInputDesktop`/`GetForegroundWindow` 未设 `restype=c_void_p`（64 位句柄截断，真实 smoke 前必须修）；复核 browser_backend start 超时 worker 泄漏（Task 6 M1）；热键生命周期接线（任一开关开启时持续注册/启动时按已开启配置注册/退出注销）；retained browser 应用退出清理；controller 未接 preview_server.publish（GUI 预览接线归 Task 13）。
3. **模型目录**：catalog 无任何 `native_openai` 条目（无 OpenAI 预置厂商，未发明条目是刻意的）；若未来新增 OpenAI 预置，须先联网逐个复核官方模型页。
4. **重复避免**：大字符串参数化测试带短 `ids`。
5. **Task 8 已履约契约**：`command_shell.get_system_settings` 的 `computer_run` 键已接入；generation 失配重读有效配置、仅削弱/撤销才停（含 effective_target 修复）。
6. **Task 9 语义钉死**：tool result 稳定 9 键 compact JSON；follow-up 观察 user 消息只经 `record.followup_messages` 通道；`_computer_v1.ordinal = controller.snapshot().action_count`。

## 用户已裁决的设计变更

- 设计 12.3：用户手动关闭模型正在操作的标签页但仍有其他可见页面时，切换到最后可见 page 继续；全部页面/context 关闭才停止会话（选项 B，2026-09-03）。设计文档已同步修订。
- 设计 8.1 关联裁决（Task 8 修复轮）：未绑定 target 的 run 按**本次动作的 target** 判定能力，browser 开关已关闭则立即拒绝，不能因另一开关仍开启而放行。

## 铁律提醒（K3 prompt + AGENTS.md）

- 后续默认不 commit/push/merge/发布（用户另有指示除外）；不更新现有 golden/hash 迁就实现；默认关闭时 Provider 请求字节、stable prompt、tools schema 零变化。
- 无 fallback/降级/吞异常/旧截图复用/模型名猜测/自动换浏览器换协议。
- 工作区其他会话的无关改动一律不碰：`core/hunyuan_image.py`、`scripts/secret_filter.py`、`README.md`、`.gitattributes`、`assets/CHANGELOG.md`、`plugins/tokenrhythm_image/`、`tests/test_hunyuan_image_auth.py`、`frontend/src/components/Sidebar.vue`、根目录 `*.png/*.jpg` 图片与 `generated_images/`。
- venv 命令用 `venv/Scripts/python.exe`；前端定向测试用 `npm --prefix frontend test -- --run src/...`。
- 测试绝不真实移动鼠标/键盘/截屏/注册热键/启动浏览器；真实探针（`scripts/smoke_computer_control.py --probe-browser edge` 等）留给用户受控执行；不调用真实付费 Provider。
- 不创建第二套配置/消息库/事件总线/工具注册表；新增逻辑集中在 `core/computer/`。
- 若计划引用的函数位置与当前代码不符，先取证做最小调整并把差异写入 Execution Record；若必须改变已确认的配置键、默认值、协议选择、权限边界、错误码、持久化策略或 UI 结构，停下来请求用户确认。

## 剩余工作（严格顺序）

```
0. Task 9 审查（审查包已生成，reviewer 未派发）→ 修复轮（如有）→ ledger
10. Turn capability 冻结 + generic Provider 请求接入（改 context_builder、agent_service、llm、
    request_envelope、memory；Task 9 移交的 4 个接线点在此落地）
11a. OpenAI 原生事件解析与串行执行；11b. 原生 continuation/safety/持久化投影
12. GUI 系统设置与模型能力编辑（前后端）
13a. GUI Bridge 事件桥与控制 API；13b. 前端安全条/操控页签/reducer（Vitest）
14. CLI `/computer status|pause|resume|stop`（接 main.py 现有分派，不建平行 handler）
15. LAN 边界（FORBIDDEN_METHODS + turn_source=lan 注入）
16. 生命周期/打包/隐私审计/受控烟测脚本完善（含上面契约传递第 2 条）
17. 全量门禁 + 规格逐条验收 + 最终报告（格式见 K3 prompt「最终回复格式」）
```

每个 Task 完成后给用户一条简短进度汇报。最终若有人工烟测/打包验证因环境无法执行，如实标「未执行」并把结论定为「尚不可提交」。
