# 交接文档：Y-CODE 浏览器与 Windows 电脑操控实施（2026-09-03 深夜 · 第三次交接）

> 上一份交接文档：`docs/HANDOVER-20260903-computer-control.md`（当晚第二次交接，Task 1–9 状态）。
> 本文档接续其后，覆盖 Task 9 审查完成、Task 10 完成并通过审查、Task 11a 代码已写但**未跑任何测试**的当前时点。

## 当前状态速览

- **分支**：`feature/computer-control-wip`（保持此分支，禁止切换/合并/push/发布；默认不主动 commit，用户明确要求才提交）。
- **提交历史**：
  - `6e51423` feat(computer): 浏览器与 Windows 电脑操控 Task 1-9（本功能基线）
  - `2c1e7ff` fix(llm): Responses 桥接枚举事件 type 归一化（**并发会话提交**，非本功能，已与本功能干净分离）
- **总进度**（17 个 Task）：
  - Task 1–9：完成且全部通过审查（Task 9 审查由本轮接手 agent 完成，PASS，详见 ledger）。
  - **Task 10：完成并通过审查**（reviewer 九镜头全 PASS；F1 finding 用户已裁决）。代码**未提交**，全部在工作区。
  - **Task 11a：实现代码已写完（含 F1 收敛），但未跑任何测试、checkbox 未勾、无 Execution Record、无 report**——这是下一个 agent 的第一件事。
  - Task 11b → 17：未开始。
- **工作区是多会话共享的**：存在并发会话产物，见下文"工作区改动盘点"，注意甄别、一律不碰。

## 工作区未提交改动盘点（截至本交接时刻）

`git status --porcelain` 全量清单与归属：

### A. Task 10 改动（已完成 + 审查 PASS + ER 已写入计划文件）

- `core/context_builder.py`：RuntimeContext 增 `computer_capability` 默认字段；build_dynamic_prompt 末尾条件追加（stable prompt/_DECISION_POLICY 一字未改）
- `core/agent_service.py`：build_runtime_context 解析 capability（后被 Task 11a F1 追加 handoff 存入）
- `core/llm.py`：resolve_turn_computer_capability、tools 尾部追加 generic tool、followup 同事务提交、取消打捞 computer_cancelled、_finalize_computer_run 等（19 个 hunk）
- `core/memory.py`：_content_has_media/_fold_stale_media_locked 双形态识别 + _computer_v1 保留 + 幂等（普通消息折叠字节不变）
- `tests/test_computer_llm_generic.py`（新建，20 条测试，含 EXISTING_DEFAULT_REQUEST_BYTES 逐字节默认关闭断言）
- `tests/test_memory_maintenance.py`（+10 条）
- `tests/test_agent_service.py`（stub 补桩一行）
- `docs/superpowers/plans/2026-09-02-computer-control.md`（Task 10 七个 checkbox 已勾 + Execution Record 已写入，位于 Task 10 小节末尾约 1391 行起）
- Task 10 已知测试证据（实现时跑过）：GREEN 30 passed；golden/ledger/keepalive/token 64 passed 零改动；computer 套件 474 passed；全量 2365 passed / 0 failed

### B. Task 11a 改动（代码已写，**零测试运行**，状态 = "implementation-first，待验证"）

- `core/computer/provider_openai.py`（新建，472 行）：
  - `OPENAI_COMPUTER_TOOL_DEFINITION = {"type":"computer"}`、`COMPUTER_SELECT_TARGET_TOOL_SCHEMA`（brief 逐字 schema）
  - `SafetyCheck`、`NativeComputerCall(response_id, call_id, actions, pending_safety_checks)`、`NativeFinalText`、`NativeCallResult`、`NativeTargetSelection` 值对象
  - `build_openai_computer_tools(capability, target_selected)`、`native_has_dual_targets(capability)`
  - `parse_openai_computer_events(events, target=None)`（严格校验：缺 response/call id、selector+computer 同 response、未知动作/字段、completed/incomplete/failed 三终态区分；官方 screenshot→observe、drag path/keys 直接映射并再校验）
  - `native_computer_call_from_stream(payload, target)`（终态事件载荷构造）
  - `execute_native_computer_call(call, controller)`（串行逐动作：每动作前 cancel/pause/owner/generation 检查、每动作后 fresh observation + 无进展判断、失败即停不执行余项、整批成功只回传最后 observation；pending safety → 零执行）
  - `execute_reserved_computer_selector(decoded, selection)`（selector 固定 adapter：重复选择/未开启 target/选择后变更均明确失败）
  - `_OFFICIAL_ACTION_FIELDS`/`_KEY_ALIASES`/`_BUTTON_ALIASES` 等映射表
- `tests/test_computer_provider_openai.py`（新建，38 条测试）：brief 样例逐字 + 事件完整性/官方动作映射矩阵/串行执行/pending safety/selector 租约（首次写入/重复/变更/未开启/非法值）/流终态合并/`test_capability_resolved_once_per_production_turn`（F1 收敛验证）
- `core/harness/stream_assembler.py`（+14 行）：AssembledResponse.computer_call 字段；终态事件（output_item.done / response.completed）载荷合并，半截 JSON 不进执行路径
- `core/harness/tool_pipeline.py`（+34 行）：ToolExecutionContext.computer_native_selection 字段；`_execute_reserved_computer` 增 computer_select_target 分支（调 provider_openai.execute_reserved_computer_selector，不进普通 ToolRegistry）
- `core/harness/request_envelope.py`（+19 行）：prepare_stream_request 增 keyword-only `native_computer_tools` 参数——只追加在 flattened function tools 之后、不经 _responses_tools_from_chat_tools（其仍拒绝任意 built-in）；非 responses 协议携带非空原生工具显式 ValueError
- `core/computer/capability.py`（+21 行）：**F1 收敛**——`set_turn_computer_capability` / `take_turn_computer_capability` 线程级一次性 handoff（prepare_turn_messages 侧解析一次，chat_with_model_events 取出复用，取出即清空，非跨回合全局状态）
- `core/computer/provider_generic.py`（+31 行，其中 computer_error_tool_message 属 Task 10）：11a 相关为 selector/native 复用的错误合成扩展
- `core/llm.py`（Task 11a 相关 hunk 约 7 个，叠加在 Task 10 的 19 个之上）：import provider_openai 系列、`_COMPUTER_RUN_TERMINAL_STATUSES`、事件桥 `_normalize_response_payload`（computer_call 载荷规范化 + response id 记录）、native 分支请求构造（native_computer_tools 注入、双 target 时 parallel_tool_calls=false）、native 续传/执行接缝
- `tests/computer_fakes.py`（+54 行）：NATIVE_DUAL_TARGET_CAPABILITY 等新常量
- `tests/test_llm_responses.py`（+153 行，6 条新测试）：reject builtin tool definitions（保持）、终态 computer_call 转发、半截事件忽略、completed output 携带、冲突 response id 拒绝、assembler 合并 chunk

### C. 并发会话产物（与本功能无关，**一律不碰**）

- `tests/test_llm.py`（+40 行：stepfun anthropic 路由 effort 映射，非本功能）
- 未跟踪：`docs/ycode_overview_flowchart.mmd`、`docs/ycode_overview_flowchart.png`、`待开发/` 目录
- 历史：`2c1e7ff` 已提交（core/llm.py 的 _normalize_responses_event_type 归一化 + test_llm_responses.py 当时改动）

### D. SDD 证据目录

`.superpowers/sdd/2026-09-02-computer-control/`（被 gitignore，仅本机）：
- `progress.md`（ledger，Task 1–10 状态与裁决全记录，**接手先读**）
- `task-9-review-package.txt`、`task-10-brief.md`、`task-10-report.md`、`task-10-review-package.txt`、`task-11a-brief.md`
- 注意：该目录曾因环境迁移丢失一次，本轮已重建；如再丢失，可从计划文件各 Task 的 Execution Record 恢复主要证据。

## 下一个 agent 的动作序列

### 第一步：验证 Task 11a（实现已存在，TDD RED 已不可补，如实记录）

1. `git branch --show-current` 确认分支；`git status --porcelain` 与上文明细核对。
2. 跑定向测试：`venv\Scripts\python.exe -m pytest tests/test_computer_provider_openai.py tests/test_llm_responses.py -q`
   - 全绿 → 补跑回归（命令见下）；有失败 → systematic-debugging 定位根因修复（禁 fallback/吞异常/放宽断言/改 golden）。
3. 回归命令：
   - `venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_cache_keepalive.py tests/test_token_accounting.py -q`（默认关闭字节零变化，现有 expected/hash 不得改）
   - `venv\Scripts\python.exe -m pytest tests/test_computer_llm_generic.py tests/test_computer_provider_generic.py tests/test_memory_maintenance.py -q`（Task 10 保持绿）
   - `venv\Scripts\python.exe -m pytest tests/ -q -k "computer" --tb=no`（预期 ≥512：474 + 11a 新增 38）
4. `git add -N core/computer/provider_openai.py tests/test_computer_provider_openai.py` 后 `git diff --check`。
5. Execution Record 写入计划文件 Task 11a 小节末尾（格式参照 Task 10 记录）：**如实标注 implementation-first 偏差**（代码先于验证轮完成，RED 证据不可补；以定向+回归全绿 + 审查弥补），勾选 5 个 checkbox。F1 收敛证据（test_capability_resolved_once_per_production_turn）一并记录。
6. 手工生成 review package（git status + diff + 新文件全文 → `task-11a-review-package.txt`），派发 reviewer（**general-purpose 子代理，Explore 型不产出结论，勿用**），审查镜头：brief Steps 1–5 逐条 + 设计 11.1/11.2 + F1 收敛正确性 + 默认关闭字节零变化 + 无越界（不碰 Task 10 语义与并发产物）+ stream_assembler 半截 JSON 防线 + selector 不进 ToolRegistry + native 解析失败不 fallback generic。
7. 有 Critical/Important → 修复轮（先写失败测试取得真实 RED 再修，最多 5 轮）；通过后更新 ledger，进入 Task 11b。

### 之后：严格按 Task 11b → 12 → 13a → 13b → 14 → 15 → 16 → 17 顺序

每个 Task 重复 SDD 循环：PowerShell 抽 brief（命令见旧交接文档）→ implementer（general-purpose）→ 审查包 → reviewer（general-purpose）→ 修复轮 → ledger。Task 11b 起 TDD RED 必须真实（先写测试跑出目标缺失再实现）。

## 跨 Task 契约传递（必须携带）

1. **Task 10 已落地语义**（后续 Task 直接依赖）：capability Turn 起点解析一次冻结（经线程 handoff，F1 已收敛——**11a 验证轮须确认 handoff 链路测试真实通过**）；lease 惰性建立（首次 computer_action 才 start_run，纯对话不占排他租约，busy 返回禁用 lease 继续）；followup 与 tool results 同事务按 ordinal 先 results 后 followups；`_computer_v1` 绝不进 Provider 请求；媒体折叠双形态兼容。
2. **Task 11a 实现要点**（验证时关注）：native 双 target 首请求含 computer_select_target + computer built-in 且 parallel_tool_calls=false；selector 与 computer_call 同 response 是协议错误（computer_provider_protocol_error，顺序无关）；native 解析失败绝不改走 generic；pending safety → enter_provider_safety_required 零执行；actions[] 串行可中断、只回传最后 observation 的 computer_call_output。
3. **Task 11b 范围**（下一个 Task）：build_continuation_request（重发同一 model/同序 tools/instructions/options + previous_response_id，input 只有 computer_call_output，不重发历史）；previous_response_id 只留 Turn 内存不进 DB/Journal/日志；acknowledged_safety_checks 确认字段在官方 GA schema 验证 + byte-golden + 用户显式操作三条件齐备前**不得产出**；_computer_v1 原生中间行脱敏投影（输入只存长度+SHA-256、截图只存 hash/尺寸）；崩溃恢复只标 interrupted。
4. **Task 16 待办**：win32_adapter.py 的 OpenInputDesktop/GetForegroundWindow 缺 `restype=c_void_p`（64 位句柄截断，真实 smoke 前必修）；browser_backend start 超时 worker 泄漏复核；热键生命周期接线（任一开关开启持续注册/启动按已开启配置注册/退出注销）；retained browser 退出清理；controller 接 preview_server.publish。
5. **模型目录**：无任何 native_openai 预置条目（刻意不发明）；新增 OpenAI 预置前必须联网逐个复核官方模型页。
6. **Task 9 语义钉死**：tool result 稳定 9 键 compact JSON；follow-up 只经 record.followup_messages；OBSERVATION_FOLLOWUP_TEXT 固定文本在 provider_generic。
7. 大字符串参数化测试带短 ids（Windows PYTEST_CURRENT_TEST 32767 上限）。

## 用户已裁决事项

- 设计 12.3：用户手动关闭模型正在操作的标签页但仍有其他可见页面时，切换到最后可见 page 继续；全部页面/context 关闭才停止会话。
- 设计 8.1 关联：未绑定 target 的 run 按本次动作的 target 判定能力，开关已关闭立即拒绝。
- **F1（2026-09-03）**：Task 10 capability 双次解析 → 接受现状并 Task 11 顺带收敛为单次解析（11a 已实现线程 handoff 方案，待验证）。

## 环境要点

- 无 bash；shell 用 PowerShell；Python 用 `venv\Scripts\python.exe`；前端定向测试 `npm --prefix frontend test -- --run src/...`（不带 frontend/ 前缀）。
- playwright 已重新装入 venv（环境迁移曾丢失，2026-09-03 修复；computer 套件 450 passed 时点验证过）。
- 部分命令结尾 "TRAE Sandbox Error: hit restricted ... EFSClipboardCtrl.log" 报错但命令实际成功——按实际产物判断成败。
- tests/test_tokenrhythm_image.py 预存失败曾存在（并发会话翻转 plugin.json enabled），当前工作树该文件为 enabled:false、全量测试曾 0 failed——若再现 1 failed 先查 plugin.json 是否又被并发会话改动，禁触不修。
- implementer/reviewer 均用 general-purpose 子代理。

## 铁律提醒（K3 prompt + AGENTS.md 摘要）

- 不 push/merge/tag/发布；默认不 commit（Task 10+11a 改动目前全部未提交——是否提交由用户指示）。
- 不修改现有 golden/hash 迁就实现；默认关闭时 Provider 请求字节、stable prompt、tools schema 零变化。
- 无 fallback/降级/吞异常/旧截图复用/模型名猜测/自动换浏览器换协议；native 解析失败不得改走 generic。
- pytest 绝不真实键鼠/截屏/注册热键/启动浏览器；真实探针（scripts/smoke_computer_control.py）只写不跑。
- 不调用真实付费 Provider。不创建第二套配置/消息库/事件总线/工具注册表；新增逻辑集中在 core/computer/。
- messages 表是正文唯一事实源；Journal 只记录状态/ID/序号/时间/耗时/哈希。
- 修改请求构造/消息规范化/system prompt/tools/metadata/extra_body/缓存亲和前，必须先跑 byte-golden 与 prompt byte-stability 回归。
- 与计划/设计文本冲突的 finding 必须问用户裁决，不得自行选择。
