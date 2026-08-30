# Configurable Turn Step Limit and Explicit Closure Execution Prompt

请在项目 `E:\fch\Y-code_tui` 中执行以下修复：

- 设计规格：`docs/superpowers/specs/2026-08-30-configurable-turn-step-limit-and-closure-design.md`
- 实施计划：`docs/superpowers/plans/2026-08-30-configurable-turn-step-limit-and-closure-fix.md`

执行要求：

1. 开始前完整阅读：
   - `AGENTS.md`
   - 上述设计规格
   - 上述实施计划
   - `docs/superpowers/specs/2026-08-28-agent-loop-and-empty-tool-result-fix-design.md`
   - `docs/superpowers/plans/2026-08-28-agent-loop-hardening-review-remediation.md`

2. 本次授权修改实施计划 File Map 中列出的实现、测试和文档文件，但没有授权执行 `git commit`、push、merge 或发布。保持在 `dev` 分支，不创建或切换其他分支。

3. 使用 `superpowers:subagent-driven-development` 逐 Task 执行；若当前环境不能使用子代理，则使用 `superpowers:executing-plans` 顺序执行。必须按 Task 1 → Task 6 的顺序推进，不并行修改共享文件。

4. 每个实现 Task 严格执行 TDD：
   - 先写计划指定的失败测试；
   - 运行测试并确认因目标缺陷而 RED；
   - 只写使该缺陷通过的最小实现；
   - 运行定向测试确认 GREEN；
   - 运行该 Task 的相关回归；
   - 把真实命令、退出码、测试数量、持续时间和必要诊断回填到实施计划的 `Execution Record`。

5. 固定设计决策不得擅自改变：
   - 配置键为 `agent_turn_step_limit`；
   - 默认值为 100；
   - 合法范围为 10–1000 的整数；
   - `bool`、浮点数、数字字符串和越界值必须拒绝；
   - 不提供 `0 = 无限`；
   - 每个 Turn 只读取一次配置，运行中修改只影响后续 Turn；
   - 轮次只统计真实 Provider 请求/Harness Step，不统计工具调用；
   - 达到上限必须是 `failed / StepLimitExceeded`；
   - 最后一个已完整提交的 Step 保持 `completed`；
   - 必须发出 `turn_error`，不得再发 `turn_finished`；
   - 保留已提交的代码、工具结果和测试记录；
   - 不自动续跑、不自动加大上限、不回滚或重放副作用；
   - 精确清理 `[System: Empty message content sanitised to satisfy protocol]`，不得用宽泛规则删除普通正文。

6. 保持现有核心不变量：
   - `TurnProgressGuard.MAX_STALLED_STEPS = 6` 不变；
   - `RepeatCallGuard`、计划状态机、取消和崩溃恢复语义不变；
   - 一个 Step 对应一次真实模型请求，Provider 内部重试仍留在同一 Step；
   - `messages` 继续是正文唯一事实源，Harness Journal 不写正文、工具参数或工具结果原文；
   - 不改变 Provider 请求构造、稳定 system prompt、tools 顺序/schema、DeepSeek Prompt Ledger、Kimi/GLM 路由、缓存或 token 统计口径。

7. 严格限制范围：
   - 只实现设计规格和计划列出的配置、Harness 终态、协议文本清理与 GUI 设置；
   - 不开展开放式找 bug；
   - 不顺手重构 `core/llm.py`、配置系统或设置页；
   - 不新增自动重试、fallback、降级、启发式后处理或吞异常逻辑；
   - 不更新 Provider golden/hash 来迁就实现；
   - 保留工作树中所有用户已有修改，遇到重叠时先检查并做最小合并，不覆盖他人改动。

8. 如果代码现状与计划不一致，先用 `superpowers:systematic-debugging` 获取证据，说明差异和最小调整，再继续。不得凭猜测扩大设计；若必须改变已确认的配置范围、终态或公开接口，停止并请求用户确认。

9. Task 1–5 完成后，严格执行 Task 6 的全部门禁：
   - Python 语法检查；
   - Turn limit、Harness、LLM events、renderer、history 和 system settings 定向测试；
   - Provider byte-stability、DeepSeek Ledger、缓存与 Token 统计测试；
   - 完整 pytest；
   - 完整前端测试；
   - 前端生产构建；
   - `git diff --check`；
   - `git status --short --branch` 和变更边界检查。

10. 最终停止条件：
    - Task 1–5 均有真实 RED → GREEN 证据；
    - 默认 100、10–1000 校验、Turn 冻结语义均被测试覆盖；
    - 达到小测试上限后，Turn 为 `failed / StepLimitExceeded`、最后 Step 为 `completed`、只有 `turn_error`、没有 `turn_finished`；
    - 最后工具结果和明确的“任务未完成”提示均已持久化；
    - 协议占位符在实时、持久化公开正文和历史回放中均不可见；
    - 所有门禁退出码为 0；
    - 没有 Critical 或 Important 遗留；
    - 工作树没有意外临时文件，仍处于 `dev` 分支。

最终输出必须包含：

- 修复摘要；
- 变更文件列表；
- 每个 Task 的 RED/GREEN 证据；
- 配置键、默认值、范围和生效时机；
- `StepLimitExceeded` 的数据库、Step、事件和 GUI 证据；
- 协议占位符清理证据；
- 完整验证命令、测试数量、持续时间和退出码；
- Provider/Prompt/Token 字节稳定性结果；
- 剩余风险或未完成项；
- 是否已经达到“当前可以提交”。

达到停止条件后停止，不执行 commit，也不要再启动新一轮开放式代码审查。
