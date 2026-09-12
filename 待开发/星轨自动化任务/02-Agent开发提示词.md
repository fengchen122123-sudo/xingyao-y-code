# Agent 开发提示词

以下提示词可以按顺序复制给不同 Agent。提示词中的相对路径均以项目根目录为基准。

## 一、主开发 Agent 提示词

```text
请在当前 Y-CODE 项目中完整实现“星轨自动化任务”。

开始前必须完整阅读：
1. AGENTS.md
2. 待开发/星轨自动化任务/README.md
3. 待开发/星轨自动化任务/01-完整设计规格.md
4. 待开发/星轨自动化任务/03-验收清单.md

硬性要求：
- 先检查 git status 和当前分支。项目规定日常开发与提交只在 dev 分支进行；若当前不是 dev，不得在当前分支提交，也不要自行合并或重置，请先报告。
- 这是中大型任务，按 01 规格第 14 节的阶段 A→E 顺序实施，先测试后实现（TDD），每阶段一次提交，并持续更新 待开发/星轨自动化任务/实施进度.md。阶段只用于控制风险，不代表分版交付。
- 不做 MVP，不删减规格范围；最终交付必须覆盖规格全部功能。
- 调度引擎只存在于 GUI 常驻进程：core/automation/ 新包承载 scheduling（纯函数）、storage（CAS 认领）、engine（线程循环）；gui_bridge.py 只做接线，禁止把时间计算或 SQL 堆进桥层。
- 自动化回合必须复用既有回合管线：internal_kind="automation" 走 _run_turn_worker，提示词逐字经 _memory.add_message 落库；绝不旁路 Harness、绝不绕过权限、绝不引入第二执行引擎。
- 无人值守权限：confirm 档位下 ASK 请求必须在 _activate_permission_request 入口处立即自动拒绝并 notice 留痕，不弹 PermCard、不无限等待；accept_edits/full 只能由用户在任务表单显式选择。
- 单飞：同任务存在 queued/running 运行时本次触发记 skipped，不排队堆积；next_run_at 认领必须是单条 CAS UPDATE。
- 取消与崩溃：用户停止的自动化回合记 cancelled 且绝不重放；启动时 recover_stale_runs 把遗留 queued/running 记 interrupted，绝不自动补跑（run_once 补跑仅限“错过触发”这一种场景且最多一次）。
- 严格遵守 AGENTS.md 线程纪律：引擎线程不发起 LLM 请求、不触碰 evaluate_js，推事件只走 emit_event/_js_queue；JS 桥只在 gui-js-dispatcher 线程调用。
- 不修改 system prompt 内容、tools schema、metadata、extra_body、Prompt Ledger 与缓存亲和材料；自动化提示词只是普通 user 消息。
- 保留用户已有修改，不做无关重构；工作区已有未提交改动（如回合撤销功能）必须原样保留，提交只包含本功能文件。

工程方法：
1. 先盘点既有锚点（01 规格第 17 节全部行号位置）再动手；行号仅供参考，以符号名为准。
2. 阶段 A 的 scheduling.py 与 storage.py 必须可脱离 GUI 测试；引擎用注入 time_fn 与记录型 dispatch 测试。
3. 桥接层改动逐项对照规格 5.4/5.5：worker 签名追加 automation_run_id（默认空串，既有调用方零改动）、_drain_message_queue 透传、turn_id 回填、终态 exactly-once。
4. 前端按电脑操控范例的 5 处同步点接线（类型/store/事件/UI/测试）；任务列表 UI 仿 Subpage 现有 MCP/模型分支；事件负载幂等替换。
5. 新增测试文件与覆盖范围严格按 01 规格第 13 节表格执行。

验证至少包括：
- venv\Scripts\python -m py_compile 全部改动 py 文件；
- 新增 6 组测试全绿；
- 全量 pytest、frontend npm run test、npm run build；
- tests/test_prompt_byte_stability.py 专项确认通过；
- 既有 GUI 权限、消息队列、事件、turn_undo 回归测试全绿。

完成时按以下格式报告：
- 结果摘要
- 架构与关键设计落点
- 变更文件列表
- 自动化验证及实际输出摘要
- 03-验收清单逐项状态
- 风险或未完成项（没有就明确写“无”）
- 实际使用的 skill

不要在功能未完整、测试未通过或验收清单未闭合时声称完成。
```

## 二、代码评审 Agent 提示词

```text
请对当前分支中“星轨自动化任务”的实现做严格只读代码评审，不要直接修改代码。

必须完整阅读：
- AGENTS.md
- 待开发/星轨自动化任务/README.md
- 待开发/星轨自动化任务/01-完整设计规格.md
- 待开发/星轨自动化任务/03-验收清单.md

先查看 git status、分支 Diff、相关测试和实现文件。重点审查：
1. 是否完整覆盖规格，而不是用占位按钮、TODO、假数据或砍掉 missed_run_policy/权限档位等方式冒充完成。
2. core/automation/ 分层是否干净：scheduling 纯函数可脱离 GUI 测试；storage 是否全部 SQL + CAS；engine 是否只做认领与分发；gui_bridge 是否只做接线。
3. next_run_at 认领是否单条 CAS UPDATE；同任务并发/重复触发、应用双开等场景是否可能双跑。
4. 单飞判定是否覆盖 manual 触发；skipped 是否留痕。
5. _spawn_turn_worker/_run_turn_worker/_drain_message_queue 的 automation_run_id 透传链是否完整，旧调用方/旧排队条目是否零影响；终态写入是否 exactly-once，completed/cancelled/error 三路径是否都有测试。
6. 无人值守权限：confirm 档位 ASK 是否在发 permission_request 事件前被立即拒绝；accept_edits/full 是否只能来自用户显式表单选择；自动拒绝是否有 notice 留痕；人工回合权限行为是否分毫未动（对照既有权限测试）。
7. 取消与崩溃语义：cancelled 绝不重放；recover_stale_runs 只标 interrupted；run_once 补跑是否只有一次且不连环。
8. 线程纪律：引擎线程是否有任何 LLM 请求/evaluate_js/直接 UI 触碰；事件是否全部经 emit_event；shutdown 是否先停引擎再取消回合。
9. 数据库：建表/索引/清理/级联删除是否符合规格；写后是否 commit；是否复用桥的同一 MemoryManager。
10. 是否有任何对 system prompt、tools schema、metadata、缓存亲和材料的改动（出现即阻塞项）。
11. 前端 5 处同步点是否齐全（pywebview.d.ts、appStore、Subpage、Sidebar、测试）；事件是否幂等替换；表单校验是否前后端一致。
12. 异常路径：分发异常、DB 写失败、任务超上限、prompt 超长、JS 队列满，行为是否与规格第 12 节一致。

只报告真实、可复现的问题。每个问题给出：优先级、文件与行号、触发条件、实际后果、建议的最小修复方向。按严重程度排序。若没有问题，明确说明未发现阻塞项，并列出仍未由测试覆盖的风险。
```

## 三、修复 Agent 提示词

```text
请根据上一轮代码评审发现修复“星轨自动化任务”问题。

先完整阅读 AGENTS.md、待开发/星轨自动化任务/README.md、01-完整设计规格.md、03-验收清单.md 和全部评审意见。不要机械接受评审结论：逐条复现或用代码证据验证后再修复。只做与评审和设计规格相关的最小修改，保留用户其他改动。

每个修复先补充能失败的回归测试，再修改实现。禁止使用静默降级、额外兜底、宽泛异常吞噬、自动重试或后处理来让测试表面通过。完成后运行受影响测试及完整相关回归，并逐条说明评审项的验证结果。
```

## 四、最终验收 Agent 提示词

```text
请对“星轨自动化任务”执行最终验收。可以运行测试，但不要修改产品代码，除非用户随后明确要求修复。

完整阅读 AGENTS.md、待开发/星轨自动化任务/README.md、01-完整设计规格.md、03-验收清单.md 和开发过程中生成的实施进度.md。逐项验证功能、调度语义、权限安全、恢复行为、GUI 与事件、回归质量。不得只根据测试名称推断通过，必须查看实际命令输出。

除自动化测试外，必须完成 03-验收清单中标注【人工】的场景（可启动 GUI 实际操作验证：创建每天任务、立即执行、会话忙时触发、停止回合、重启应用验证 interrupted、禁用任务、删除任务等）。

输出：
- 总体结论：通过或不通过
- 自动化测试命令及结果
- 验收清单逐项结果
- 未覆盖或需要人工操作的项目
- 发现的问题，含文件/行号或明确复现步骤

只有所有适用硬性项目通过，且没有 P0/P1 问题时才能判定通过。
```
