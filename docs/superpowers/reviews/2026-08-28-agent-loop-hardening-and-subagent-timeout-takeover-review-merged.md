# 独立审查报告（合并终版）：Agent 循环加固与子 Agent 超时接管

> **文件性质**：本文件是**两轮独立审查的合并终版**。同一审查提示词由两个互不知情的
> Agent 各自独立执行了一轮完整审查（下称 **A 轮**=ZCode 会话、**B 轮**=ycode 会话），
> 两轮各自复跑了全部验证命令、各自成稿。B 轮的成稿位于
> `docs/superpowers/reviews/2026-08-28-agent-loop-hardening-and-subagent-timeout-takeover-review.md`
> （该路径同时也是 A 轮当时的写稿路径，B 稿覆盖了 A 稿；A 稿原文已不在磁盘，其内容
> 完整并入本文件）。本文件 = 两轮发现的并集 + 交叉校验裁决，原两份稿件均不再需要单独维护。
>
> - **审查对象**：`dev` 分支工作树中"Agent 循环加固与子 Agent 超时接管"计划的全部未提交
>   改动（基线 `bdebe13`，37 个已跟踪文件修改 + 2 个未跟踪文档/测试，约 +2772/-170 行）
> - **审查日期**：2026-08-28
> - **计划文件**：`docs/superpowers/plans/2026-08-28-agent-loop-hardening-and-subagent-timeout-takeover.md`
> - **设计 spec**：`docs/superpowers/specs/2026-08-28-agent-loop-and-empty-tool-result-fix-design.md`（第 14 节增补）
> - **审查方式**：两轮均为只读独立审查；全部验证命令各自独立复跑，不采信执行者结论、
>   不互采信对方结论；分歧处给出裁决与依据（见第 5 节）。

---

## 1. 总评

**需修复后交付。**

主路径（首次超时 → 原子移交 → scoped 放行 → plan_finish → FIFO 提升 → 旧执行者 fencing）
实现正确、测试真实：两轮独立复跑的全部验证命令末行数字与 Execution Record 一致
（268 / 131 / 26 / 30 / 22 / 374-1 / build ok），Provider golden、DeepSeek Ledger、
Journal 纯净性均未被破坏。TDD 证据充分（RED 测试断言生产行为、Test Helper Rule 遵守）。

两轮审查在**核心结论上完全收敛**，在边缘发现与严重度裁量上互补（详见第 5 节差异裁决）。
合并后共 **2 个 P1、4 个 P2、6 个 P3**：

- **P1-1**：接管生命周期闭环缺口——`promote_next_takeover` 在生产代码中没有任何
  turn 开始处的调用方，崩溃恢复 / turn 未收尾 / 调度器重建三个场景下接管项永久卡死；
  （A、B 两轮一致认定为最高优先级）
- **P1-2**：`_terminate` 重排丢弃了基线的 `not task.callback_invoked` 条件认领，
  completed 回调 finalizing 窗口内的 cancelled/failed 二次终止会第二次触发
  terminal_callback（行为回归；损害有 scheduler CAS 兜底，故两轮评级分别为 P1/P2，
  合并取 P1 并注明边界性）。

诚实性结论：执行者预先存在失败记录属实（`test_llm_events_fixes.py` 两失败经基线对照
独立复证）、前端 guiTheme 失败属用户改动范围、全部 Commit 为 not-authorized 如实标注、
Task 6 对 llm.py 的偏离已披露。交接就绪，但 P1-1 必须先行修复再宣称闭环。

---

## 2. Findings（合并后统一编号）

> 每条标注来源轮次与两轮原始编号的映射关系（完整映射表见第 5.3 节）。

### P1-1 接管生命周期闭环缺口：queued 接管项无生产提升路径
**来源**：A、B 两轮一致（A"维度一 P1"；B"P1-1"）。

**证据**：
- `core/plan.py:509-517`：`_load_locked` 把遗留 `main_takeover` 降为 `takeover_queued`
  并清 turn id，与计划 Task 4 Step 6 声称的"下一真实主回合通过
  `promote_next_takeover(new_turn_id)` 重新认领"对应；
- 但全仓检索（两轮各自独立执行）确认 `promote_next_takeover` 生产代码仅
  `core/plan.py:918`（`plan_finish` 内，且要求当下已存在 active 接管项）一处调用，
  `llm.py` / `agent_service.py` / `main.py` / `gui_bridge.py` 中无任何 turn 开始处的提升调用；
- 单测 `test_loaded_main_takeover_downgrades_to_queued_for_next_turn` 只证明原语可用
  （测试内手动调用 promote），未证明生产可达。

**三条卡死路径（两轮合并）**：

1. **崩溃恢复**：项恢复为 `pending + takeover_queued`。四面皆墙——
   `run_subagent` blocked（`core/tools.py:3614-3630`，`main_takeover_required`）；
   `plan_finish` 拒绝（非 active）；`plan_retry` 拒绝（pending 不在
   `{failed,blocked,cancelled}`，`core/task_scheduler.py:332-333`）；turn-end 清理只处理
   `in_progress`。**永久卡死**。
2. **turn 未 plan_finish 即结束**（用户取消 / NoProgressError / 异常 / 模型忘收尾）：
   项停留 `in_progress + main_takeover` 并绑定已死亡 turn id。后续 turn
   `active_takeover` 因 turn 不匹配返回 None → 门禁不放行，其余入口同上全拒。
   **GUI 侧无任何 turn 末清理**（`gui_bridge.py` 的 `turn_finish` 只调
   `maybe_clear_completed_plan`），该项在 GUI 中无出路；CLI 侧 `main.py:3585-3601`
   的 turn 末清理会把该项 `fail()`——这是**误伤**（错误文案"子 agent 可能异常退出"
   与事实不符），且 fail 后 `plan_retry` + `run_subagent` 恰好打开了计划明确禁止的
   `timed_out → failed → retry → 新 run_subagent` 侧门。
3. **调度器重建变体**（B 轮发现，A 轮复核属实）：`_active_takeover_item`
   （`core/harness/tool_pipeline.py:615`）经 `get_default_scheduler()` 惰性建调度器，
   构造器 `_reset_orphaned_in_progress`（`core/task_scheduler.py:93-106`）只复位
   `status` 不清 `execution_mode`，会把 `in_progress/main_takeover` 变成
   `pending/main_takeover`——同样卡死。（A 轮初判"实践中不会发生"系低估，已修正。）

**最小修复方向**：在新主回合开始处（如 `llm.py` 计划回合标记后）调用
`promote_next_takeover(当前 turn_id)`；turn 末清理识别 takeover 项并复位为
`takeover_queued` / 清租约而非 `fail()`（GUI 侧补齐与 CLI 对齐的清理）；
`_reset_orphaned_in_progress` 复位时同步清执行租约字段。注意 promote 的
`transition_execution` 含依赖检查，依赖未满足返回 None 时需有提示，否则依赖受损的
queued 项仍会搁浅。

### P1-2 `_terminate` 双重 terminal_callback 回归（需动态验证）
**来源**：A、B 两轮各自独立发现（A 原评级 P2、B 原评级 P1；合并取 P1，理由见下）。

**证据**：
- `core/tools.py:2791-2792` 新代码无条件
  `task.callback_invoked = True; callback = task.terminal_callback if invoke_callback else None`；
  基线 `bdebe13` 旧代码为 `if invoke_callback and not task.callback_invoked:`
  （条件认领，注释明确"保证 terminal_callback 全局只发一次"——该注释至今仍留在 `_complete` 中）；
- 守卫 `if status == "timed_out" and task.callback_invoked`（`core/tools.py:2782`）只挡 timed_out。

**触发条件**：`_complete`（`core/tools.py:2730-2752`）锁内认领回调后在锁外执行
terminal_callback（内含验证命令，最长 120 秒；confirm 模式下窗口等于用户应答时间），
期间 `task.status` 仍为 `"running"`。此时 `cancel_foreground` / `_run_task` 异常路径 /
watchdog 的 `_terminate("cancelled"/"failed")` 到达：终态检查不命中、timed_out 守卫
不命中 → 再次取到 callback 并**第二次执行**，`scheduler.cancel` 与并发
`scheduler.complete` 竞态。

**后果评估（合并两轮）**：计划项 cancelled/completed 分裂（task 记录与计划项状态可能
不一致）、`plan_updated` 等事件重复派发；scheduler 层 CAS 保证计划项终态唯一、
无数据损坏——这是 A 轮评 P2 的依据；B 轮评 P1 的依据是"相对基线的行为回归 +
不变式破坏 + 用户可见状态分裂"。**合并评级 P1（边界性：损害有界，但属必须修复的回归）。**

**临时测试方案**（两轮各自给出，等价；勿写入仓库）：

```python
def test_cancel_during_complete_callback_does_not_double_invoke():
    import threading
    from core.tools import SubagentManager
    calls, entered, release = [], threading.Event(), threading.Event()
    def terminal_callback(task, status, result, error):
        calls.append(status)
        if status == "completed":
            entered.set(); release.wait(5)  # 模拟回调内长时间验证
    manager = SubagentManager(runner=lambda record: "done", wait_interval=0.001)
    th = threading.Thread(
        target=lambda: manager.run(
            "x", "s", "t", timeout_seconds=30, terminal_callback=terminal_callback
        )
    )
    th.start()
    assert entered.wait(5)
    manager.cancel_foreground("s", "t")
    release.set(); th.join(5)
    assert calls == ["completed"]  # 现状会得到 ["completed", "cancelled"]
```

**最小修复方向**：恢复条件认领
`if invoke_callback and not task.callback_invoked: task.callback_invoked = True; callback = ...`，
同时保留新顺序的"锁内先写 status/error/finished_at/cancel_event"。

### P2-1 会话名前缀误伤 fencing（B 轮独有发现，A 轮复核属实）
**证据**：主回合 `permission_owner` 默认取 turn_id（`core/llm.py:2188`
`permission_owner=permission_owner or resolved_parent_turn_id`），而
`turn_id = f"{session}:{hex8}"`（`main.py:3466` / `gui_bridge.py:3545`）。
`_stale_execution_lease` 按 `owner.startswith("subagent-")` 判定（
`core/harness/tool_pipeline.py:781`）：若会话名以 `subagent-` 开头（用户重命名或
自动重命名取自用户消息均可产生），主 Agent **全部**工具调用会被误 fence——
`is_execution_lease_active` 查无此 id → 返回取消错误。

**修复方向**：前缀判断收紧为 `subagent-` + 8 位 hex 的精确形态
（现任务 id 格式即 `subagent-{uuid4().hex[:8]}`）。

### P2-2 `_stale_execution_lease` 异常路径 fail-open，与注释/docstring 矛盾
**来源**：A、B 两轮一致。

`core/harness/tool_pipeline.py:787-789`：注释写"查询失败按保守策略处理：绝不因
fencing 自身异常放行"，代码 `except Exception: return None` 恰是放行（fail-open）。
实际异常概率极低（持锁查字典），但 fencing 是防旧执行者改写接管现场的最后一道闸。
二者必改其一：建议 fail-closed（异常时返回 STALE_EXECUTION_LEASE_TEXT）或如实改注释。

### P2-3 `plan_finish` 失败文案指向不存在的恢复路径 + blocked 文案误导
**来源**：A、B 两轮一致（B"P2-1" + A"P2-4"）。

`core/plan.py` plan_finish 失败分支文案："不要重新委派子代理；请分析失败原因、
修正现场后重新接管收尾"。但验证失败后项为 `failed + main_takeover`，无 active 接管，
`plan_finish` 只会再被拒；唯一出路是 `plan_retry(force=True)` + `run_subagent`
（即重派，与文案相反）。同时 failed+main_takeover 项再调 `run_subagent` 得到的
blocked 文案称"主 Agent 正在接管"（`core/tools.py:3620-3629`），与实际状态不符。
模型按文案操作会原地打转直至 NoProgressError。
**修复**：失败文案改为指引 `plan_retry`，blocked 文案按状态区分措辞。

### P2-4 非计划场景的 run_subagent 结果去噪有误杀窗口
**来源**：A、B 两轮一致（A"P2-5"、B"P2-3"）。

`core/llm.py:3397` `_PLAN_STATE_RESULT_TOOLS = {"run_subagent", "plan_retry", "plan_read"}`
无条件把 run_subagent 结果移出进展哈希。计划绑定场景有计划指纹兜底
（status/execution_mode 变化，每次 run_subagent 步都有状态转换信号）；
但无计划的纯委派扇出（连续 6+ 步只做 run_subagent，尤其后台批量派发）不产生任何
进展信号 → 第 7 次 Provider 请求被 NoProgressError 阻断。计划的去噪取舍针对
"计划项动态 task_id 伪进展"，非计划扇出误伤是副作用。
**修复建议**：仅当 `plan_item_id` 非空时排除正文，或为成功的 run_subagent 记录一次性签名。

### P3-1 `TaskScheduler.retry()` 两段提交，CAS 失败返回值被忽略
**来源**：A"P2-3" / B"P3-3"（A 原评 P2，合并降 P3：窗口极窄）。

`core/task_scheduler.py:345-353`：先 `plan.update(status="pending")` 再
`transition_execution(清租约)` 两次提交，窗口内观察者可见 `pending + 旧租约`；
若 failed 项残留 `takeover_queued` 模式，窗口内并发 promote 可能误认领；
并发认领抢先时第二步 CAS 失败但 retry 仍返回 True（良性，新认领已原子覆盖）。
**修复**：合并为单次 `transition_execution(expected_status={"failed","blocked","cancelled"}, status="pending", ...)`。

### P3-2 `queue_position` 锁外重算
**来源**：A、B 两轮一致（A"P2-7 评注" / B"P3-1"）。

`core/tools.py:3693-3710`：调度器锁外从快照重算，并发 handoff 下展示位置可能不准
——纯展示字段，影响可接受。

### P3-3 `_takeover_files_violation` 不解析符号链接
**来源**：A、B 两轮一致（A"P2-7" / B"P3-2"）。

`core/harness/tool_pipeline.py:653-655` 用 `abspath`+`normcase`（大小写、相对路径、
`..` 收敛可靠），但 Windows junction/符号链接可绕过 files 限制；仍受权限模式与沙盒
层约束，属纵深防御缺口。另 B 轮指出：`execute_command` 绕过 files 属计划明示接受项，
但代码注释未注明此取舍（评注级）。**修复**：`os.path.realpath` + 补注释。

### P3-4 ToolExecutionRow 徽标多批次解析失败（仅徽标缺失）
**来源**：A、B 两轮一致（A"P2-6" / B"P3-5"）。

`frontend/src/components/ToolExecutionRow.vue:190-205`：多调用结果 `join` 后整体
`JSON.parse` 必然失败进 catch，徽标静默缺失（无崩溃，可接受）；`handoff:"failed"`
无徽标；无组件测试。**修复**：逐 call 解析取第一个匹配 + 补组件测试。

### P3-5 `wait_for_idle` 提前 idle 窗口（B 轮独有）
`wait_for_idle` / `get_active_count` 在 `_terminate` 锁内写终态后即可报 idle，
而 handoff 回调仍在执行——窗口极小，影响低。

### P3-6 测试名实不符（B 轮独有）
`tests/test_llm_events.py:1276` `test_stalled_plan_after_repeated_failures_allows_direct_fix`
现仅断言 QUERY 豁免，docstring 已按计划更新为只读诊断语义，但测试名仍暗示
"允许直接修复"。建议改名。

### 记录级备注（非缺陷）
- **Task 5/6 验证数字漂移**（A 轮独有）：Record 记 227/321，当前工作树同命令为
  238/326——系 Task 6-9 后续追加测试的正当时间漂移，非虚报（Task 1/7/8/9/10 数字
  两轮均精确复现）。
- **计划内取舍**（两轮一致，不算 finding）：旧"stalled attempts>=2 全局放行"分支按
  计划删除（QUERY 豁免与计划清空死锁防护保留）；`execute_command` 绕过 files 限制
  为计划明示接受项；`transition_execution` 未禁止对终态项改写 execution_mode
  （当前无调用方这么用）。

---

## 3. 验证命令真实输出末行汇总（两轮独立复跑均一致）

| 命令 | 末行（A 轮实测 / B 轮实测） | Execution Record | 结论 |
|---|---|---|---|
| pytest tool_pipeline+tools+tool_registry+progress_guard | `268 passed`（9.86s / 9.88s） | 268 passed | ✅ 两轮精确一致 |
| pytest harness_runtime+journal+message_commit+llm_events+plan | `131 passed`（71.49s / 72.60s） | 131 passed | ✅ |
| pytest cancellation+agent_turn_e2e+session_parallel_isolation | `26 passed`（2.71s / 2.60s） | 26 passed | ✅ |
| pytest prompt_byte_stability+provider_payload_golden+deepseek_prompt_ledger | `30 passed`（2.35s / 2.29s） | 30 passed | ✅ |
| npm test（两个 store 文件） | `22 passed` | store 22 passed | ✅ |
| npm run build | `✓ built`（462ms / 409ms） | build ok | ✅ |
| npm test 全量前端 | `374 passed / 1 failed`（App.guiTheme.test.ts 窗口最大化用例，属用户主题改动范围） | 374/375 | ✅ 两轮一致 |
| pytest tests/test_llm_events_fixes.py | `2 failed, 75 passed` | 记录为预先存在 | ✅ 经基线对照独立复证（pywintypes DLL 环境问题 + plan 模式断言），与本计划零交集 |
| Task 1 定向（tools -k background_task/terminal/tombstone/watcher） | `23 passed` | 23 passed | ✅ 精确 |
| pytest tests/test_task_scheduler.py | `22 passed` | Task 5 附带 22 passed | ✅ 精确 |
| Task 7 定向（progress_guard+harness_runtime+llm_events+plan） | `117 passed` | 117 passed | ✅ 精确 |
| Task 8 定向（tool_registry+tools -k subagent/timeout） | `55 passed, 149 deselected` | 55 passed | ✅ 精确 |
| Task 9 CLI（llm_events+isolation+main_ui_events） | `124 passed` | 124 passed | ✅ 精确 |
| pytest tools+tool_pipeline+cancellation | A 轮 `238 passed` | Task 5 记 227 | ⚠️ +11（后续任务追加测试漂移） |
| pytest tool_pipeline+tools+plan+llm_events | A 轮 `326 passed` | Task 6 记 321 | ⚠️ +5（同上） |
| py_compile 11 个改动文件 | 通过 | 通过 | ✅ |

**Journal 纯净性**（两轮各自复核）：diff 无任何 Journal 写入改动；读取签名仅存
Turn 内存（`TurnProgressGuard._seen_takeover_reads`），符合 AGENTS.md 铁律。

**基线对照方法说明**：A 轮用 `git archive bdebe13` 在临时目录
（`%TEMP%\yc_baseline`）导出基线并复跑两个失败用例，确认预先存在；B 轮在同一目录
复跑并核对了其与 `bdebe13` 逐字节一致。该目录系 A 轮审查过程创建（B 轮原稿误记为
"执行者遗留"，此处更正），两轮结论一致且互相独立成立。

---

## 4. 闭环性对照表（合并）

| 验收项 | 证明来源 | 结论 |
|---|---|---|
| FAC1 6 步熔断 / 第 7 次请求不发 | 既有 TurnProgressGuard 测试 + harness_runtime 回归（组 1/2 两轮全过） | ✓ |
| FAC2 首次超时移交不重派 | `test_run_subagent_timeout_hands_off_plan_item_to_main`（JSON 全字段、attempts 不变、handoff_task_id==task_id）+ `rejects_redelegation`（UnusedManager 断言）+ E2E `test_timeout_handoff_takeover_completes_without_redelegation`（5 Step completed、manager 任务数恒 1） | ✓ |
| FAC3 先取消 + fencing | `test_timeout_sets_cancel_before_handoff_callback`；`test_stale_subagent_owner_tools_are_fenced`（adapter 未执行）+ 反向测试；fencing 位于 plan 门禁后、resolve/announce/权限前（tool_pipeline.py:792-799） | ✓（取消竞态见 P1-2，前缀误伤见 P2-1） |
| FAC4 scoped 权限 | 本回合放行 / 他回合拒绝 / confirm 仍询问 / files 限制 4 个 pipeline 测试 | ✓ |
| FAC5 显式 plan_finish | `test_plan_finish_*`（active 可完成、错 turn/queued/普通/缺上下文拒绝、验证失败 failed 不重派、成功自动提升）；schema 仅 item_id/result | ✓（失败文案见 P2-3） |
| FAC6 FIFO + 会话隔离 | `test_multiple_handoffs_queue_and_promote_fifo` + `test_takeover_queue_is_isolated_between_session_schedulers` | 部分缺口：崩溃恢复 promote 无生产接线（P1-1） |
| FAC7 去噪 / 指纹 | `test_different_subagent_task_ids`、`test_plan_state_tools`、guard 12/13/重复三测试；指纹含 execution_mode 不含动态字段 | ✓（非计划扇出风险见 P2-4） |
| FAC8 后台任务 | Task 1/2 定向 23 passed 精确复现；终态复活/容量/墓碑/poll 异常四测试均在 | ✓ |
| FAC9 GUI/CLI 展示 | store 测试（跨会话不污染）+ `test_main_ui_events.py`（文案去重）+ build 通过 | ✓（ToolExecutionRow 徽标无组件测试，见 P3-4） |
| FAC10 全量矩阵 + 记录真实 | 第 3 节两轮独立复跑一致 | ✓ |
| Journal 纯净 | diff 无 journal 写入；签名不落盘 | ✓ |
| 用户取消不触发接管/复活 | terminal_callback cancelled → scheduler.cancel 分支；test_cancellation.py 全过 | ✓ |
| 遗留项诚实性 | llm_events_fixes 两失败基线复证 ✓；guiTheme 失败属用户范围 ✓；全部 Commit=not-authorized（git log 确认 bdebe13 为最后提交）✓；Task 6 llm.py 偏离已披露 ✓ | ✓ |
| Test Helper Rule | `_install_active_takeover` / `_fenced_owner_manager` 等均经生产 API 搭状态，断言指向生产行为 | ✓ |
| 交接就绪 | Execution Record 数字两轮均可复现；未授权提交说明完整 | ✓（P1-1 修复后方可宣称闭环） |

**覆盖缺口**（两轮一致）：GUI turn 中止后的接管项状态（P1-1 场景 2）无任何测试。

---

## 5. 两轮审查差异与裁决记录

同一提示词、两轮独立执行，结果不同的原因：LLM 采样非确定 + 审查是开放式搜索
（清单约束"查什么"不约束"查多深"）+ 运行环境状态不同（B 轮跑在 A 轮之后，
复用了 A 轮创建的基线临时目录）。核心结论收敛，边缘发现互补——重叠部分即高置信信号。

### 5.1 裁决：B 轮发现、A 轮初判有误的
| 项 | 裁决 |
|---|---|
| 会话名前缀误伤 fencing（B-P2-2 后半） | **B 对**。A 轮只核查了 `subagent-`（任务 id）与 `subagent:`（验证 runner owner）的区分，漏查了主 Agent owner 默认取 turn_id、turn_id 以会话名为前缀的路径。已复核属实，并入 P2-1 |
| `_reset_orphaned_in_progress` 复位不清 execution_mode 的卡死变体（B-P1-1 变体） | **B 对**。A 轮初判"实践中不会发生"系低估；复位仅 plan.update(status)，不清租约字段，代码属实。并入 P1-1 场景 3 |

### 5.2 裁决：A 轮发现、B 轮遗漏/有误的
| 项 | 裁决 |
|---|---|
| Task 5/6 验证数字漂移（227→238、321→326） | A 对。B 轮未做定向数字抽查；漂移属后续任务追加测试，非虚报（记录级） |
| `%TEMP%\yc_baseline` 归属 | **B 错**。该目录系 A 轮审查用 `git archive bdebe13` 创建，非"执行者遗留"。已在本文件第 3 节更正（不影响两轮的基线对照结论本身） |
| GUI 无 turn-end 清理作为独立面向 | A 展开为完整论证（GUI 接管项无出路），B 在 P1-1(b) 内一句带过——结论一致，已合并 |

### 5.3 严重度裁量差异与两轮编号映射
| 合并编号 | A 轮原编号/评级 | B 轮原编号/评级 | 合并评级与理由 |
|---|---|---|---|
| P1-1 | P1 | P1-1 / P1 | P1（两轮一致） |
| P1-2 | P2-1 / P2 | P1-2 / P1 | **P1（边界）**：回归+用户可见分裂 vs CAS 兜底损害有界；取严 |
| P2-1 | —（漏） | P2-2 后半 / （并入 P2） | P2（触发窄：需会话名为 subagent- 前缀；命中则全会话工具被 fence） |
| P2-2 | P2-2 / P2 | P2-2 前半 / （并入 P2） | P2（两轮一致） |
| P2-3 | P2-4 / P2 | P2-1 / P2 | P2（两轮一致） |
| P2-4 | P2-5 / P2 | P2-3 / P2 | P2（两轮一致） |
| P3-1 | P2-3 / P2 | P3-3 / P3 | P3（窗口极窄，取宽） |
| P3-2 | P2-7 评注 | P3-1 / P3 | P3 |
| P3-3 | P2-7 / P2 | P3-2 / P3 | P3（纵深防御） |
| P3-4 | P2-6 / P2 | P3-5 / P3 | P3（仅展示缺失） |
| P3-5 | —（未报） | P3-4 / P3 | P3（B 独有，核实属实） |
| P3-6 | —（未报） | P3-6 / P3 | P3（B 独有，核实属实） |

---

## 6. 维度一 24 项速览（合并结论）

**A 终态与回调时序**：1. 锁内原子认领可防 watchdog/worker/前台竞态，timed_out 守卫有效，但 cancelled 未受守卫（P1-2）；2. done_event 晚于 callback 的各等待路径安全，`_foreground_result` 读 handoff 有 happens-before 保证，`wait_for_idle` 有极小提前窗口（P3-5）；3. 无 timed_out 改写 failed 路径。

**B 接管状态机**：4. `start_delegated_item` 绑定与 `run(task_id=)` 碰撞拒绝闭环，启动回滚释放 `_in_flight_ids/_in_flight_files` 无容量泄漏；5. `handoff_to_main` 四条件无绕过，FIFO max+1 与归零混合顺序正确；6. promote 忽略 turn 符合状态机，deps 检查由 transition_execution 覆盖——但无生产调用方（P1-1）；7. retry 两段提交窗口极窄（P3-1）；8. 崩溃降级逻辑正确但 promote 不可达（P1-1）。

**C 门禁与 fencing**：9. 旧全局放行删除属计划取舍，failed 项仍可经 plan_retry 恢复，无死锁；10. 惰性建调度器仅影响本会话桶，但复位不清 execution_mode（并入 P1-1 场景 3）；11. 路径归一化可靠，符号链接不解析（P3-3），edit_file 的 file_path 同被覆盖，execute_command 绕过属计划接受项；12. `subagent:` 验证 runner 不受影响，会话名前缀误伤（P2-1），fail-open 矛盾（P2-2）；13. fencing 位于权限询问与 adapter 之前，失效租约不进执行段。

**D 进展守卫与去噪**：14. 计划绑定场景有指纹兜底，非计划扇出有误杀窗口（P2-4）；15. 调用点确认传 `tool_calls_by_id`，签名仅计 ok 的只读工具，重复签名不计；16. 第 12 计进展/第 13 不计/重复不计与三测试逐条对齐；17. takeover peek 不惰性创建，无注册表污染。

**E 时间预算与 schema**：18. 600 秒硬上限仅作用显式传值，角色默认与后台 3600 不受影响；19. 恶意传值被 600/3600 校验拦截；20. 6000 字符拒绝在认领之前，无半认领。

**F 事件与展示**：21. queue_position 锁外重算仅展示不精确（P3-2）；22. main.py 去重键互不干扰、handoff 分支先于通用分支拦截完整；23. appStore 经 switchedRuntime 绑定/恢复不污染并行会话；24. 徽标多批次缺失不崩溃（P3-4）。

---

## 7. 审查纪律声明

两轮均为只读审查：未修改仓库任何文件、未运行破坏性命令、未提交。排除清单中的用户
既有改动（config.py / lan_server.py / GUI 主题相关 / token 统计测试 / TOML 设计文档）
未予置评、未建议改动。本合并文件为应用户要求新建的唯一产物，原 B 轮稿件保留未动。
