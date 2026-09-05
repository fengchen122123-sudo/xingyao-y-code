# Agent 开发提示词

以下提示词可以按顺序复制给不同 Agent。提示词中的相对路径均以项目根目录为基准。

## 一、主开发 Agent 提示词

```text
请在当前 Y-CODE 项目中完整实现“星瑶 GUI + CLI Git 管理”。

开始前必须完整阅读：
1. AGENTS.md
2. 待开发/星瑶Git管理/README.md
3. 待开发/星瑶Git管理/01-完整设计规格.md
4. 待开发/星瑶Git管理/03-验收清单.md

硬性要求：
- 先检查 git status 和当前分支。项目规定日常开发与提交只在 dev 分支进行；若当前不是 dev，不得在当前分支提交，也不要自行合并或重置，请先报告。
- 这是中大型任务，先根据设计规格写可执行实施计划，再开始修改。计划必须按核心模型与 Runner、安全与协调、查询/变更服务、外部监控、GUI、CLI、高级历史/冲突、综合验收拆成可恢复阶段，并持续更新 `待开发/星瑶Git管理/实施进度.md`；阶段只用于控制风险，不代表分版交付。
- 不做 MVP，不按“第一版/后续版”删减功能；最终交付必须覆盖规格中的完整范围。
- GUI 与 CLI 必须共用结构化 Git 服务层，不能分别拼接 Git 命令或解析人类文本。
- 模型侧继续使用现有 execute_command 和权限流程，不新增 Git 模型工具，不修改 tools schema 或 system prompt 来重复暴露 Git 能力。
- 星瑶发起分支切换时，只要存在已暂存、未暂存或未跟踪内容就拒绝；不得自动 Stash、强制切换或移动修改。
- 并行分支只通过 Git Worktree 实现。同一目录的所有会话必须显示同一真实分支。
- 必须检测外部终端或 IDE 的 Git 变化并刷新 GUI、CLI、文件树和相关会话。
- 所有 Git 写操作必须使用白名单参数数组、Git 自身 Ref 校验、快照版本校验和相应锁；不得提供任意参数透传。
- Git 工作台最低支持 Git 2.36.0，并维护逐操作能力表；低版本明确拒绝，不使用旧命令静默模拟。
- 参数数组不能阻止 Git Hooks、Filter、fsmonitor Hook、external diff/textconv、Credential Helper 或 Remote Helper 执行程序。必须实现 GitRepositoryTrustPolicy；未信任仓库不得借 Git 配置绕过星瑶安全边界。
- 高风险操作必须使用服务端 GitRiskPolicy 的两段式 preview + 一次性确认令牌；前端弹窗不能作为唯一安全门禁。
- 操作状态必须把 operation_kind、operation_phase 和 conflicts 正交建模，不能把 conflicted 与 rebase/merge 等做成互斥状态。
- 严格遵守 Let it crash：错误显式返回，禁止静默降级、自动重试、删除 Git 锁、模糊 Patch、自动冲突解决或其他后处理补救。
- 保留用户已有修改，不做无关重构，不使用 git reset --hard 或 checkout 丢弃工作。
- GUI evaluate_js 只通过既有 gui-js-dispatcher；CLI 后台刷新只通过 prompt_toolkit 主循环线程安全调度。

工程方法：
1. 先盘点现有 core/worktree.py、core/tools.py、core/command_policy.py、core/workspace_context.py、gui_bridge.py、main.py、frontend 状态栏/右侧面板/文件树/Diff 和对应测试。
2. 建立职责清晰的 core/git/ 包，按规格中的 runner、models、repository、query、mutation、patch、coordinator、monitor、service 边界实现。
3. 先为解析、状态机、安全校验、锁和真实临时仓库流程写测试，再写实现。
4. 保持 core/worktree.py 与 /worktree 兼容，并迁移为新服务的兼容门面。
5. GUI 桥只提供窄的结构化方法和事件，Git 业务逻辑不得堆进 gui_bridge.py。
6. CLI /git 使用严格子命令解析器和全屏管理器，不允许 /git -- 任意透传。
7. 对大仓库、特殊文件名、二进制、Detached、Unborn、Bare、Worktree、冲突和进行中操作做真实测试。
8. 接入 Workspace Process Registry，并按规格兼容矩阵区分只读、Index、Ref-only、Worktree rewrite 和 Common repository 操作；长期后台服务不能无条件阻塞 Stage/Commit。
9. AI 辅助入口只能发送用户可见模板消息，禁止隐藏 System Message、Prompt Ledger 修改或无提示上传 Diff。
10. Worktree 默认根目录、首次选择、路径安全编码、冲突和沙盒规则必须按规格实现，不能自行发明目录。
11. 更新 README、/help、类型声明和用户可见说明。

验证至少包括：
- Python 语法检查；
- 新增 Git 单元与真实临时仓库集成测试；
- 现有 worktree、workspace、多会话、权限、命令、GUI 事件和 Diff 回归测试；
- 前端单元测试；
- npm run build；
- 与改动风险相称的完整 pytest。

如果实现触碰请求构造、system prompt、tools schema、metadata 或缓存亲和材料，必须停止并说明为什么；未经设计要求不得改这些内容。若确有必要，先运行并保持 Provider byte-golden 与 prompt byte-stability 不变。

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
请对当前分支中“星瑶 GUI + CLI Git 管理”的实现做严格只读代码评审，不要直接修改代码。

必须完整阅读：
- AGENTS.md
- 待开发/星瑶Git管理/01-完整设计规格.md
- 待开发/星瑶Git管理/03-验收清单.md

先查看 git status、分支 Diff、相关测试和实现文件。重点审查：
1. 是否完整覆盖规格，而不是用 MVP、占位按钮、TODO 或假数据冒充完成。
2. GUI 与 CLI 是否真正共用结构化 Git 服务。
3. Git 是否使用参数数组、-- 路径分隔、Ref 校验、稳定机器格式和凭据脱敏。
4. 脏工作区切换是否对 staged/unstaged/untracked 全部严格拒绝。
5. 外部 Git 切换、提交、Fetch、冲突状态能否触发真实刷新。
6. 同 Worktree 多会话和不同 Worktree 并行语义是否正确。
7. GitOperationCoordinator、Workspace Process Registry、快照版本和锁顺序是否存在竞态或死锁。
8. 是否错误地用任意活动进程阻塞全部 Git 写操作，或者反过来允许 Worktree rewrite 与长期服务并发。
9. Merge/Rebase/Cherry-pick/Revert 的 kind/phase/conflicts 正交状态机以及 Continue/Skip/Abort 是否完整。
10. Restore、Clean、Reset Hard、Drop Stash、Force-with-lease、远程删除等操作是否由服务端两段式确认和一次性令牌保护，而非只有前端弹窗。
11. 未信任仓库能否通过 Hooks、Filter、fsmonitor、external diff/textconv、Credential Helper 或 Remote Helper 执行程序。
12. Hunk Patch 是否先 check，快照过期是否失败，是否存在模糊兜底。
13. GUI 事件线程和 CLI UI 线程安全是否符合 AGENTS.md。
14. AI 辅助是否只发用户可见消息；模型工具、Prompt Ledger、tools schema 和缓存稳定前缀是否保持不变。
15. 特殊文件名、大仓库、二进制、Detached、Unborn、Bare、Worktree、进程中断和失败路径是否有测试。
16. 是否泄露 Remote 凭据、环境变量、命令参数或 Git Credential Manager 内容。

只报告真实、可复现的问题。每个问题给出：优先级、文件与行号、触发条件、实际后果、建议的最小修复方向。按严重程度排序。若没有问题，明确说明未发现阻塞项，并列出仍未由测试覆盖的风险。
```

## 三、修复 Agent 提示词

```text
请根据上一轮代码评审发现修复“星瑶 GUI + CLI Git 管理”问题。

先完整阅读 AGENTS.md、待开发/星瑶Git管理/01-完整设计规格.md、03-验收清单.md 和全部评审意见。不要机械接受评审结论：逐条复现或用代码证据验证后再修复。只做与评审和设计规格相关的最小修改，保留用户其他改动。

每个修复先补充能失败的回归测试，再修改实现。禁止使用静默降级、额外兜底、宽泛异常吞噬、自动重试或后处理来让测试表面通过。完成后运行受影响测试及完整相关回归，并逐条说明评审项的验证结果。
```

## 四、最终验收 Agent 提示词

```text
请对“星瑶 GUI + CLI Git 管理”执行最终验收。可以运行测试和创建临时 Git 仓库，但不要修改产品代码，除非用户随后明确要求修复。

完整阅读 AGENTS.md、待开发/星瑶Git管理/01-完整设计规格.md、03-验收清单.md 和开发过程中生成的实施进度.md。逐项验证功能、并发、安全、外部状态同步、GUI/CLI 一致性、Worktree、冲突状态、特殊文件名和错误路径。不得只根据测试名称推断通过，必须查看实际命令输出。

输出：
- 总体结论：通过或不通过
- 自动化测试命令及结果
- 验收清单逐项结果
- 未覆盖或需要人工操作的项目
- 发现的问题，含文件/行号或明确复现步骤

只有所有适用硬性项目通过，且没有 P0/P1 问题时才能判定通过。
```
