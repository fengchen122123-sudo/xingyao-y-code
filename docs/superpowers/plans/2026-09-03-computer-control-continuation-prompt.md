# Y-CODE 浏览器与 Windows 电脑操控 — 续作提示词（2026-09-03 晚班）

请继续实施 Y-CODE 浏览器与 Windows 电脑操控功能。Task 1–8 已完成并通过审查（Task 8 含修复轮 RE-REVIEW PASS），Task 9 已实现且门禁全绿但**审查被中断未执行**，你从中途接手。

仓库根目录：D:\fengchenhao\ycode\xingyao-y-code（用 git rev-parse --show-toplevel 确认）
当前分支：feature/computer-control-wip（Task 1–9 的全部电脑操控代码已提交在此分支；保持在此分支，禁止切换、合并、push、发布。后续每完成一个 Task 默认不主动 commit，用户明确要求时才提交）

【第一步：完整阅读以下文件，读完再动代码】
1. docs/HANDOVER-20260903-computer-control.md（交接文档——你的主要上下文来源，含当前状态、可复用接口、跨 Task 契约、环境要点、SDD 工作流与 brief 生成的 PowerShell 替代命令）
2. AGENTS.md
3. docs/superpowers/specs/2026-09-02-computer-control-design.md
4. docs/superpowers/plans/2026-09-02-computer-control.md（实施计划；Task 1–9 已有真实 Execution Record，你从 Task 9 审查开始，然后严格按 Task 10 → 17 顺序推进）
5. docs/superpowers/plans/2026-09-02-computer-control-k3-execution-prompt.md（执行纪律，全部仍然有效）
6. .superpowers/sdd/2026-09-02-computer-control/progress.md（SDD ledger）

【你的第一个动作：完成 Task 9 审查】
Task 9 实现已完成（门禁 116 passed / golden 17 passed / computer 套件 450 passed），审查包已生成在 .superpowers/sdd/2026-09-02-computer-control/task-9-review-package.txt，但 reviewer 未派发。先派发只读 reviewer 完成审查（审查镜头九条见交接文档"接手 agent 第一个动作"节），有 Critical/Important 进修复轮（先写失败测试取得真实 RED 再修），通过后更新 ledger，再进入 Task 10。实施者自述的三项偏差（core/llm.py 2 行 MCP 过滤接线、ordinal 取 snapshot().action_count、Task 10 接线点移交）需 reviewer 逐条裁决；与计划/设计文本冲突的 finding 必须问用户裁决。

【执行方式】
采用 subagent-driven-development 模式：每个 Task 先生成 brief（本机无 bash，task-brief 脚本不可用，用交接文档中的 PowerShell 等价命令抽取），派发 implementer 子代理实现；完成后手工生成 review package（git status + git diff + 新文件全文，不 commit），派发 reviewer 子代理只读审查；有 Critical/Important 进入修复轮（最多 5 轮）；与计划/设计文本冲突的 finding 必须问用户裁决。
- implementer 用 general-purpose 子代理；reviewer 也必须用 general-purpose 子代理——Explore 型子代理不产出审查结论（实测只返回代码摘录），勿用。
- 若当前环境不支持子代理，退化为 executing-plans 顺序执行，但 RED→GREEN→回归→diff check 纪律不变。

【每个 Task 必须做到】
1. 先写失败测试并取得真实 RED（失败原因必须是目标能力缺失），再写最小实现取得 GREEN，然后运行该 Task 关联回归和 git diff --check（新文件先 git add -N）。
2. 一个 Task 没全绿不得进入下一个。测试失败先定位根因，禁止 fallback、吞异常、放宽断言、改现有 golden/expected。
3. 把真实命令、退出码、测试数量、耗时按计划的 Execution Record Protocol 追加到计划文件该 Task 小节末尾（参照 Task 1–9 已有格式），勾选 checkbox，更新 ledger。
4. 每完成一个 Task 给用户一条简短进度汇报。

【不可违反的约束】
- 不 push / merge / tag / 发布；默认不主动 commit（用户明确要求时才提交）。不修改现有 Provider golden/hash 迁就实现；默认关闭时 Provider 请求字节、stable prompt、tools schema 零变化。
- 不使用 fallback、降级、旧截图复用、模型名称猜测能力；不自动换浏览器/协议。
- pytest 中绝不真实移动鼠标、发送键盘、截屏、注册系统热键、启动浏览器——全部 fake 注入。真实探针（scripts/smoke_computer_control.py --probe-browser edge 等）只写不跑，留给用户受控执行。
- 不调用真实付费 Provider。
- 工作区有其他会话的无关改动（core/hunyuan_image.py、scripts/secret_filter.py、README.md、.gitattributes、assets/CHANGELOG.md、plugins/tokenrhythm_image/、tests/test_hunyuan_image_auth.py、frontend/src/components/Sidebar.vue、根目录 *.png/*.jpg 图片与 generated_images/），一律不碰。注意：tests/test_tokenrhythm_image.py 有 1 条预存失败（其他会话把 plugin.json enabled 翻转为 true 所致），禁触不修，不计入本功能。
- venv 命令用 venv/Scripts/python.exe；前端定向测试用 npm --prefix frontend test -- --run src/...（不带 frontend/ 前缀）。
- 不创建第二套配置/消息库/事件总线/工具注册表；新增逻辑集中在 core/computer/。
- 若计划引用的函数位置与当前代码不符，先取证做最小调整并把差异写入 Execution Record；若必须改变已确认的配置键、默认值、协议选择、权限边界、错误码、持久化策略或 UI 结构，停下来请求用户确认。

【跨 Task 契约（来自 ledger，必须传递）】
- Task 10 接线点（Task 9 移交）：① Turn 起点解析 capability 后 start_run 取 run_id，构造 ComputerTurnLease 注入 ToolExecutionContext.computer_lease；② 收到 tool_calls 后、执行前调用 validate_computer_tool_batch；③ 提交点先按 ordinal 持久化全部 tool results、再按相同 ordinal 追加 record.followup_messages；④ core/memory.py 媒体折叠兼容（_content_has_media/_fold_stale_media_locked 识别带 _computer_v1 的完整消息 dict，折叠幂等不删私有元数据，普通消息折叠字节不变）。
- Task 16：修 win32_adapter.py 的 OpenInputDesktop/GetForegroundWindow 缺 restype=c_void_p；复核 browser_backend start 超时 worker 泄漏；热键生命周期接线；retained browser 退出清理；controller 接 preview_server.publish。
- 模型目录：不得发明不存在的模型条目；新增 OpenAI native 预置前必须联网逐个复核官方模型页。
- 大字符串参数化测试必须带短 ids（Windows PYTEST_CURRENT_TEST 32767 上限坑）。
- Task 9 语义钉死：tool result 稳定 9 键 compact JSON（json.dumps ensure_ascii=False sort_keys separators=(",",":")）；follow-up 观察 user 消息只经 record.followup_messages 通道（tool message 恒 4 键）；OBSERVATION_FOLLOWUP_TEXT 固定文本在 provider_generic。

【环境要点（本机实测）】
- bash 不在 PATH，brief 生成用交接文档中的 PowerShell 命令。
- reviewer/implementer 均用 general-purpose 子代理（Explore 型不产出结论）。
- 部分 shell 命令结尾出现 "TRAE Sandbox Error: hit restricted ... EFSClipboardCtrl.log" 报错但命令实际已成功（pytest 输出 450 passed 同时 exit 1 的案例已证实）——按实际产物判断成败，忽略该噪音。

【用户已裁决的设计变更】
- 设计 12.3：用户手动关闭模型正在操作的标签页但仍有其他可见页面时，切换到最后可见 page 继续；全部页面/context 关闭才停止会话。设计文档已同步修订，以修订后为准。
- 设计 8.1 关联裁决：未绑定 target 的 run 按本次动作的 target 判定能力，开关已关闭则立即拒绝。

【完成标准】
Task 17 全量门禁（完整 pytest——允许且仅允许 tokenrhythm 那 1 条预存失败、Vitest、frontend build、py_compile/compileall、git diff --check、隐私审计）全部达到预期后按 K3 prompt 的「最终回复格式」输出报告。人工烟测/打包验证因环境无法执行时如实标「未执行」，结论定为「尚不可提交」。达到停止条件后停止，不做开放式重构。

任务可以持续很长时间，请持续推进直到全部完成。现在开始，先读交接文档。
