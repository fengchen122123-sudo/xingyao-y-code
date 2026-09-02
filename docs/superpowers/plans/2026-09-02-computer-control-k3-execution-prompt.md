# Y-CODE Browser and Windows Computer Control — K3 Execution Prompt

你现在是本项目的实施工程师。请在 `E:\fch\Y-code_tui` 中完整实现“浏览器与 Windows 电脑操控”，不要只给建议、伪代码或局部示例。

必须先完整阅读以下文件，读完再修改代码：

1. `AGENTS.md`
2. `docs/superpowers/specs/2026-09-02-computer-control-design.md`
3. `docs/superpowers/plans/2026-09-02-computer-control.md`
4. `core/harness/` 中与 request、stream、tool pipeline、runtime、journal 有关的现有实现
5. `core/llm.py` 中 Provider 请求、Responses、工具循环、持久化、取消、DeepSeek/Kimi/GLM 缓存分支
6. 现有 GUI 设置、Store、RightPanel、Bridge、CLI command 和 LAN RPC 实现及其测试

## 任务目标

按设计规格和实施计划完成以下产品能力：

- 系统设置页提供“浏览器操控”和“完整电脑操控”两个独立开关，默认都关闭；浏览器选择为 Edge/Chrome，默认 Edge。
- 浏览器使用系统 Edge/Chrome 和 Y-CODE 专用持久 profile；用户需要登录时在专用浏览器中手动登录。
- 完整桌面操控使用 Windows 截图、DPI-aware 坐标和 `SendInput`；锁屏/UAC secure desktop 等场景 fail-closed。
- OpenAI 官方且明确支持原生 Computer Use 的 Responses 模型走 native protocol。
- DeepSeek、Kimi、GLM 及其他显式声明视觉 + function calling + `computer_protocol=generic` 的模型正式走 generic 协议，不是“尝试调用”。
- 页面运行时显示顶部安全控制条和右侧“操控”页签；用户可暂停、恢复、立即停止，并有全局 `Ctrl+Alt+Esc` 紧急停止。
- CLI 只提供 `/computer status|pause|resume|stop`；永久开关只在 GUI 设置页。
- LAN/手机只能查看状态和紧急停止，不能扩大权限或启动操控。
- 默认关闭时，现有对话、工具、缓存、Prompt Ledger、Provider payload、CLI、GUI 和插件功能的行为及字节必须保持不变。

## 执行方式

1. 优先使用 `superpowers:subagent-driven-development` 按 Task 逐项执行并在每项后做两阶段审查；如果当前环境不支持子代理，使用 `superpowers:executing-plans` 顺序执行。
2. 实现任何 feature/fix 前使用 `superpowers:test-driven-development`；出现失败或代码与计划不一致时使用 `superpowers:systematic-debugging`；最终宣称完成前使用 `superpowers:verification-before-completion`。
3. 严格按计划 Task 1 → Task 17 顺序推进。不要并行修改共享的 `core/llm.py`、Harness、GUI Store 或 Bridge。
4. 每个 Task 都必须执行：先写失败测试 → 运行并确认是目标缺失导致 RED → 最小实现 → 定向 GREEN → 相关回归 → `git diff --check` → 回填真实 Execution Record。
5. 一个 Task 的定向和回归测试没有全部通过，不得进入下一个 Task。测试失败时先定位根因，不允许添加 fallback、吞异常、放宽断言或改 expected 让它变绿。
6. 任务可以长时间执行。每完成一个 Task 给用户一条简短进度更新；不要因为任务长而省略测试，也不要未经证据宣布完成。

## 授权与 Git 边界

- 本提示词授权修改实施计划 File Map 中列出的实现、测试、脚本和必要文档文件。
- 保持在 `dev` 分支，不创建、不切换、不合并其他分支。
- 本提示词没有授权 `git commit`、push、merge、tag、发布或安装系统级软件；计划中的 commit 文本只是建议提交点。
- 开始时运行 `git status --short --branch`。所有开始前已存在的修改和未跟踪文件都属于用户，必须保留；禁止 reset、checkout、clean、删除、覆盖或批量格式化。
- 如果用户已有修改与计划文件重叠（尤其 `README.md`、`ycode.spec`、GUI 或 core 文件），先查看 diff，再做最小局部合并。不能安全合并时停止该文件修改并报告具体冲突，不得覆盖。
- 不修改现有 Provider golden/hash 来迁就实现；`tests/test_prompt_byte_stability.py` 和 `tests/test_provider_payload_golden.py` 的既有 expected 必须原样通过。

## 不可更改的设计决策

### 设置和授权

- 配置键精确为：
  - `browser_control_enabled: bool = false`
  - `computer_control_enabled: bool = false`
  - `computer_browser: "edge"|"chrome" = "edge"`
- 缺失布尔键表示 false；显式非 bool 必须报损坏配置，不能 truthy 转换。
- 开启前必须通过平台、浏览器/backend 和全局热键 preflight；失败不能写入 true。
- 关闭必须先落盘 false 并增加 settings generation，再通知 controller stop；新动作从此立即拒绝。
- 用户开启后，Y-CODE 不逐动作询问权限。OpenAI Provider 强制 safety check 不能绕过、自动确认或伪造确认。

### 浏览器和桌面

- 浏览器必须使用 Python Playwright headed persistent context，系统 channel 只能是 `msedge` 或 `chrome`。
- 不运行 `playwright install`，不下载、不打包、不 fallback 到 Playwright Chromium。
- profile 固定在 `%YCODE_DATA_DIR%/browser-profiles/edge|chrome`，未设置时沿用 `config.YCODE_DIR`；Edge/Chrome 隔离，不能复制日常 profile。
- viewport 固定 1440×900，device scale factor 固定 1；模型只控制网页内容区，导航由显式 action 完成。
- desktop 使用 `ImageGrab.grab(all_screens=True)`，最长边 1920，记录虚拟桌面原点和精确比例；`SendInput` 部分成功也算失败。
- pytest 中禁止真实移动鼠标、点击、输入、注册真实热键或启动真实浏览器，全部使用 fake/injection。真实 smoke 必须通过脚本显式参数并由用户当场确认。

### 模型和协议

- 模型配置新增 `supports_function_calling` 和 `computer_protocol=none|generic|native_openai`。
- generic 必须同时显式满足 `supports_vision=true`、`supports_function_calling=true`、`computer_protocol=generic`。
- native 必须同时满足显式 native 元数据、项目 Responses 协议和官方 `api.openai.com` route。
- 禁止按模型名、厂商名或字符串包含 `glm/kimi/deepseek/openai` 猜能力。
- 当前官方 GA 形状是 `tools: [{"type":"computer"}]`、`computer_call.actions[]`、`computer_call_output` 和 `previous_response_id`；`computer_use_preview` 是旧集成，本项目首版不得采用或 fallback。当前官方明确支持 Computer use 的预置至少包括 `gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`、`gpt-5.4`、`gpt-5.4-mini`。实施前仍需逐个打开当日 OpenAI 官方模型页和 Computer use guide 复核准确型号、tool 字段、call/output 和可能返回的 safety 结构；只依据官方 primary source，不按家族推断。
- GA native 可以在一个 `computer_call.actions[]` 中返回多个动作。它是 generic 单动作规则的显式例外：必须按原序逐个串行执行，每个动作前检查 cancel/pause/owner/generation 并发 trace，失败或停止后不执行余项；整批成功后捕获一次截图并回传。禁止并行或把整批做成不可中断 backend 调用。
- native 解析失败立即以 `computer_provider_protocol_error` 失败，不能改走 generic。

### Controller 和安全

- 进程内只有一个活动 run，第二 owner 返回 `computer_session_busy`，不排队、不自动接管。
- 一个 run 固定一个 target。generic 双 target 时第一次合法 observe 建租约；native 双 target 时先用固定 `computer_select_target` function tool。
- generic assistant Step 只允许一个 `computer_action`；多个 generic computer call 或与其他工具混用时，该批次电脑动作全部不执行。OpenAI GA native 的 `actions[]` 只按上一条的可中断串行规则执行。
- 动作严格按 owner/state/cancel/generation → before → execute → fresh after → no-progress → events → Provider projection。
- 三次预期改变界面的动作出现相同 screenshot hash + active fingerprint 时停止为 `computer_no_progress`；observe/wait 不计本地 no-progress，但仍计 Harness Step。
- 所有停止入口汇聚到同一个 controller.stop：GUI stop、Turn stop、CLI stop、Ctrl+C、Ctrl+Alt+Esc、设置关闭、退出、Provider safety stop、安全桌面、浏览器关闭、无进展和 Step 上限。
- 截图失败不能返回旧图；浏览器失败不能改 desktop；SendInput 失败不能报告成功；配置非法不能纠正；禁止 `except Exception: return success`。

### Harness、缓存和隐私

- `computer_action`/`computer_select_target` 是固定保留名，不能进入普通 ToolRegistry，也不能被插件/MCP 注册。
- generic tool 只在 Turn capability enabled 时追加到现有 tools 尾部；native built-in 只在受控 OpenAI Responses 分支出现。
- capability 每 Turn 只计算一次并冻结。默认关闭的 `get_tools_schema()`、stable prompt、请求 bytes 必须与功能前一致。
- generic tool result 后追加一条 user 多模态最新截图，二者在同一 Step 提交边界持久化；图片走现有媒体维护链。
- native 使用 `previous_response_id` continuation；response id 只留 Turn 内存，不能进入 DB、Journal、event 或日志。
- native `_computer_v1` 中间行只保存脱敏摘要；输入只存长度和 SHA-256，截图只存 hash/尺寸，不能存 base64。
- Harness Journal 不能写用户/助手正文、动作参数原文、截图、URL、Provider safety 原文、profile、Cookie 或凭据。
- GUI trace 最多 200 条且不持久；preview server 只绑定 127.0.0.1、随机 token、当前 run 最近三张 PNG，错误 token 404，退出删除。
- `evaluate_js` 只能由现有 `gui-js-dispatcher` 调用；事件线程只投递小 JSON。
- DeepSeek Prompt Ledger、Kimi/GLM reasoning/cache-control/metadata/extra_body、cache keepalive、token 统计和默认工具顺序必须保持现有语义。

## 变更纪律

- 遵循现有架构和命名，新增逻辑集中在 `core/computer/`，不要把 controller/backend 大段塞进 `core/llm.py` 或 `gui_bridge.py`。
- 不做与本功能无关的开放式重构、格式化或找 bug。
- 不新增自动重试、fallback、降级、启发式 OCR/相似度、模糊能力推断、旧截图复用或静默容错。
- 不创建第二套配置、消息库、事件总线、工具注册表或 Provider client。
- 数据库写操作使用现有事务并 commit；不要让 screenshot base64、input text 或 response id 进入 Journal。
- 后台线程不能直接触碰 prompt_toolkit/PyWebView/Vue；所有 UI 更新回到现有主线程/dispatcher。
- 若计划引用的函数位置因当前代码变化而不同，先用 `rg` 和测试证据找到真实接缝，在不改变公开契约的前提下做最小调整，并把差异写入 Execution Record。
- 若必须改变已确认的配置键、默认值、协议选择、权限边界、错误码、持久化策略或 UI 结构，停止并请求用户确认，不得自行扩大范围。

## 基线和每阶段门禁

开始实现前必须通过：

```powershell
git branch --show-current
git status --short
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_token_accounting.py -q
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

如果基线失败，先报告并用 systematic-debugging 区分既有失败，不要继续写功能。

每个 Task 完成后，按实施计划中的精确命令运行定向测试和相关回归，并在计划相应 Task 末尾追加真实记录：

```text
Execution Record — Task N
- RED: 精确命令 | exit=实际值 | 目标失败原因 | 实际时长
- GREEN: 精确命令 | exit=0 | 实际 passed 数 | 实际时长
- Regression: 精确命令 | exit=0 | 实际 passed 数 | 实际时长
- Diff check: 精确命令 | exit=0
- Files touched: 实际清单
- Review: spec PASS; privacy PASS; default-byte PASS 或 NOT-APPLICABLE; unresolved none
```

不要留下模板占位文字。RED 如果不是目标缺失导致，就不能作为有效证据。

## 必须执行的最终门禁

Tasks 1–16 全部完成后，严格执行 Task 17 的所有命令，至少包括：

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_dependency.py tests/test_computer_models.py tests/test_computer_settings.py tests/test_computer_capability.py tests/test_computer_hotkey.py tests/test_computer_preview_server.py tests/test_computer_browser_backend.py tests/test_computer_windows_backend.py tests/test_computer_controller.py tests/test_computer_tool_adapter.py tests/test_computer_provider_generic.py tests/test_computer_provider_openai.py tests/test_computer_llm_generic.py tests/test_computer_cli.py tests/test_computer_privacy.py -q
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_llm_responses.py tests/test_harness_runtime.py tests/test_tool_pipeline.py tests/test_tool_adapters.py tests/test_tool_registry.py tests/test_cache_keepalive.py tests/test_token_accounting.py tests/test_llm_cancellation.py tests/test_llm_events_fixes.py -q
venv\Scripts\python.exe -m py_compile main.py core/llm.py core/memory.py core/memory_compressor.py core/themes.py
venv\Scripts\python.exe -m compileall -q core/computer
venv\Scripts\python.exe -m pytest -q
npm --prefix frontend test -- --run
npm --prefix frontend run build
git diff --check
git status --short --branch
```

还必须执行敏感信息审计，确认 DB、Journal、runtime log、GUI/CLI trace 没有输入正文、response id、Cookie、profile 内容、完整 URL query 或 native screenshot base64。

人工 smoke 必须等自动测试全部通过后进行：

- Edge/Chrome system channel observe；
- 两个专用 profile 隔离、登录态重启保留；
- browser close fail-closed；
- desktop 多屏/DPI observe；
- 用户明确确认后才运行一次受控真实输入 smoke；
- pause 后无新动作、resume 后必须先 observe；
- GUI/CLI/Ctrl+C/Ctrl+Alt+Esc/设置关闭/应用退出都停止并释放输入；
- 锁屏/UAC secure desktop fail-closed；
- 退出后无残留浏览器子进程和临时 PNG。

如果当前环境不能安全执行某项人工 smoke，不要伪造成功；明确标记“未执行”和阻塞原因，此时最终结论必须是“尚不可提交”。

## 完成标准

只有同时满足以下条件，才能说“实现完成，当前可以提交”：

1. 实施计划 Tasks 1–16 每项都有真实 RED → GREEN → regression 证据；
2. 完整 pytest、完整 Vitest、frontend production build、`git diff --check` 全部 exit 0；
3. 现有 Provider/prompt golden/hash 未改并全部通过；
4. generic 和 native 协议分别有严格 request/event/continuation 测试，且无互相 fallback；
5. 两个设置开关默认 false，浏览器选项默认 edge，所有 setter/preflight/generation 行为通过测试；
6. GUI 顶部安全条、右侧操控页、CLI、热键和 LAN 边界通过测试；
7. 所有停止入口释放输入、backend、profile lock、preview、Provider stream 和活动租约；
8. 敏感信息审计通过；
9. 没有 Critical/Important 遗留，没有 skipped 的核心安全测试；
10. 所有用户原有工作树修改都被完整保留。

## 最终回复格式

最终回复必须先给结论，然后包含：

- 实现摘要；
- 变更文件清单；
- Task 1–16 每项 RED/GREEN/回归证据；
- Edge、Chrome、desktop 和全局热键 preflight/smoke 结果；
- generic 与 OpenAI native 的请求/事件/continuation 证据；
- GUI、CLI、LAN、Provider safety 和停止路径证据；
- 默认关闭的 Provider/prompt/cache/token 字节稳定性结果；
- 隐私和持久化审计结果；
- 完整验证命令、passed 数、实际时长和 exit code；
- 用户已有修改及未触碰说明；
- 剩余风险、未执行项目和最终“当前可以提交/尚不可提交”结论；
- 本次实际使用的 skills。

达到完成标准后停止，不执行 commit、push、merge、发布，也不要再启动新一轮开放式重构。
