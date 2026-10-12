# 独立审查报告：Agent 循环加固与子 Agent 超时接管

- 审查日期：2026-08-28
- 审查对象：`dev` 分支工作树中"Agent 循环加固与子 Agent 超时接管"计划的全部未提交改动（基线 `bdebe13`）
- 审查方式：只读独立审查（不修改任何文件、不提交）；全部验证命令由审查者独立复跑，未采信执行者结论
- 计划文件：`docs/superpowers/plans/2026-08-28-agent-loop-hardening-and-subagent-timeout-takeover.md`
- 设计 spec：`docs/superpowers/specs/2026-08-28-agent-loop-and-empty-tool-result-fix-design.md`（第 14 节增补）

## 1. 总评

实现质量整体较高：状态机设计、测试覆盖（生产 API 断言、E2E）、Execution Record 的数字均经独立复核一致，Provider golden 与 Journal 纯净性未被破坏。但存在两个 P1 级闭环缺口：

1. 崩溃恢复降级后的 `takeover_queued` 项在生产代码中没有任何调用方会将其重新提升（`promote_next_takeover` 仅被 `plan_finish` 调用），接管链在"turn 未收尾即结束 / 进程重启"场景下永久卡死，且所有恢复入口互相拒绝；
2. `_terminate` 重排时丢弃了旧代码的 `not task.callback_invoked` 认领条件，cancelled 与进行中 completed 回调竞态下 terminal_callback 会被调用两次（回归）。

结论：需修复后交付。P1-1 直接违反计划自己声明的"重启后 promote 重新认领"设计。

## 2. Findings（按严重度）

### P1-1 接管生命周期闭环缺口：queued 接管项无生产提升路径

证据：

- `core/plan.py:509-515`（`_load_locked` 把遗留 `main_takeover` 降为 `takeover_queued` 并清 turn id）与计划 Task 4 Step 6 声称"下一真实主回合通过 `promote_next_takeover(new_turn_id)` 重新认领"；
- 但 `git grep promote_next_takeover` 显示生产代码仅 `core/plan.py:918`（`plan_finish` 内）一处调用，其余全是测试。`llm.py` / `agent_service.py` / `main.py` / `gui_bridge.py` 中无任何 turn 开始时的提升调用。

触发条件与后果（两条路径均死锁）：

- (a) 进程重启后：项为 `pending + takeover_queued`。`run_subagent` 被 blocked（`core/tools.py:3614-3630`，`main_takeover_required`）；`plan_finish` 拒绝（非 active 接管项）；`plan_retry` 拒绝（status=pending 不在 `{failed,blocked,cancelled}`，`core/task_scheduler.py:332-333`）。永久卡死。
- (b) 进程内接管 turn 未 `plan_finish` 即结束（用户取消 / NoProgressError / 异常 / 模型忘收尾）：项停留 `in_progress + main_takeover` 并绑定已死亡 turn id。后续 turn：`active_takeover` 因 turn 不匹配返回 None → 门禁不放行；其余入口同上全拒。GUI 侧无任何 turn 末清理（`gui_bridge.py` / `core/agent_service.py` 无等价逻辑）。CLI 侧 `main.py:3585-3601` 的 turn 末清理会把该项 `fail()`——这是误伤（错误文案"子 agent 可能异常退出"与事实不符），且 fail 后 `plan_retry` + `run_subagent` 恰好打开了计划明确禁止的 `timed_out → failed → retry → 新 run_subagent` 侧门。
- 变体：`_active_takeover_item`（`core/harness/tool_pipeline.py:615`）经 `get_default_scheduler()` 惰性建调度器，构造器 `_reset_orphaned_in_progress`（`core/task_scheduler.py:93-106`）只复位 status 不清 `execution_mode`，会把 `in_progress/main_takeover` 变成 `pending/main_takeover`——同样卡死。

最小修复方向：在新主回合开始处（如 `llm.py` 计划回合标记后）调用 `promote_next_takeover(当前 turn_id)`；turn 末清理识别 takeover 项并复位为 `takeover_queued` / 清租约而非 `fail()`；`_reset_orphaned_in_progress` 复位时同步清执行租约字段。

### P1-2 `_terminate` 双重 terminal_callback 回归

证据：

- `core/tools.py:2791-2792` 新代码无条件 `task.callback_invoked = True; callback = task.terminal_callback if invoke_callback else None`；基线 `bdebe13` 旧代码为 `if invoke_callback and not task.callback_invoked:`（条件认领，注释明确"保证 terminal_callback 全局只发一次"）。
- 守卫 `if status == "timed_out" and task.callback_invoked`（`core/tools.py:2782`）只挡 timed_out。

触发条件：`_complete`（`core/tools.py:2730-2752`）锁内认领回调后在锁外执行 terminal_callback（内含验证命令，最长 120 秒），期间 `task.status` 仍为 `"running"`。此时 `cancel_foreground` / watchdog 的 `_terminate("cancelled")` 到达：首个终态检查不命中、timed_out 守卫不命中 → 再次取到 callback 并第二次执行。

后果：`scheduler.cancel` 与并发 `scheduler.complete` 竞态（计划项 cancelled/completed 分裂）、`plan_updated` 等事件重复派发、`task.status` 被覆盖为 cancelled 而计划项可能已 completed。需动态验证。

临时测试方案（勿写入仓库）：

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

最小修复方向：恢复条件认领 `if invoke_callback and not task.callback_invoked: task.callback_invoked = True; callback = ...`，同时保留新顺序的"锁内先写 status/error/finished_at/cancel_event"。

### P2-1 `plan_finish` 失败文案指向不存在的恢复路径

`core/plan.py` plan_finish 失败分支文案："不要重新委派子代理；请分析失败原因、修正现场后重新接管收尾"。但验证失败后项为 `failed + main_takeover`，无 active 接管，`plan_finish` 只会再被拒；唯一出路是 `plan_retry(force=True)` + `run_subagent`（即重派，与文案相反）。模型按文案操作会原地打转直至 NoProgressError。修复：失败文案改为指引 `plan_retry`，或明确说明接管已结束。

### P2-2 `_stale_execution_lease` 注释与行为矛盾 + 会话名前缀误伤

`core/harness/tool_pipeline.py:773-790`：注释写"查询失败按保守策略处理：绝不因 fencing 自身异常放行"，代码 `except Exception: return None` 恰是放行（fail-open），二者必改其一。另：主回合 `permission_owner` 默认值为 `{session}:{hex}`（`core/llm.py:2188`；`main.py:3466` / `gui_bridge.py:3545`），若用户会话名以 `subagent-` 开头，主 Agent 全部工具调用会被误 fence（`is_execution_lease_active` 查无此 id → 返回取消错误）。建议前缀判断收紧为 `subagent-` + 8 位 hex 的精确形态。

### P2-3 非计划场景的 run_subagent 结果去噪有误杀窗口

`core/llm.py:3397` `_PLAN_STATE_RESULT_TOOLS = {"run_subagent", "plan_retry", "plan_read"}` 无条件把 run_subagent 结果移出进展哈希。计划绑定场景有计划指纹兜底（status/execution_mode 变化），但无计划的纯委派扇出（连续 6+ 步只做 run_subagent，尤其后台批量派发）不产生任何进展信号 → 第 7 次 Provider 请求被 NoProgressError 阻断。计划的去噪取舍针对"计划项动态 task_id 伪进展"，非计划扇出误伤是副作用。需动态验证；建议非计划回合保留 run_subagent 结果哈希。

### P3（建议 / 观察项）

- P3-1 `core/tools.py:3693-3710`：`queue_position` 在调度器锁外从快照重算，并发 handoff 下展示位置可能不准——纯展示字段，影响可接受。
- P3-2 `_takeover_files_violation`（`core/harness/tool_pipeline.py:633-660`）用 `abspath` 非 `realpath`，Windows junction/符号链接可绕过 files 限制；`execute_command` 绕过 files 属计划明示接受项，但注释未注明此取舍。
- P3-3 `TaskScheduler.retry()`（`core/task_scheduler.py:345-353`）忽略 `transition_execution` 的 CAS 失败返回值，极端竞态下残留租约字段。
- P3-4 `wait_for_idle` / `get_active_count` 在 `_terminate` 锁内写终态后即可报 idle，handoff 回调仍在执行——窗口极小，影响低。
- P3-5 `ToolExecutionRow.vue` 徽标对多调用批次（join 后 JSON.parse 必失败）仅缺徽标不崩溃，可接受。
- P3-6 `tests/test_llm_events.py:1276` `test_stalled_plan_after_repeated_failures_allows_direct_fix` 名实不符：现仅断言 QUERY 豁免，docstring 已更新但测试名仍暗示"允许直接修复"。

## 3. 验证命令真实输出末行汇总

| 命令 | 末行（实测） | Execution Record | 一致 |
|---|---|---|---|
| tool_pipeline+tools+tool_registry+progress_guard | `268 passed in 9.88s` | 268 passed | ✓ |
| harness_runtime+journal+message_commit+llm_events+plan | `131 passed in 72.60s` | 131 passed | ✓ |
| cancellation+agent_turn_e2e+session_parallel_isolation | `26 passed in 2.60s` | 26 passed | ✓ |
| prompt_byte_stability+provider_payload_golden+deepseek_prompt_ledger | `30 passed in 2.29s` | 30 passed | ✓ |
| npm test（两个 store 文件） | `Test Files 2 passed / Tests 22 passed` | store 22 passed | ✓ |
| npm run build | `✓ built in 409ms` | build ok | ✓ |
| Task 1 定向（background_task/terminal/tombstone/watcher） | `23 passed, 162 deselected` | 23 passed（143 deselected） | ✓（deselected 差异因后续测试增长） |
| Task 8 定向（subagent/timeout） | `55 passed, 149 deselected` | 55 passed | ✓ |
| Task 9 CLI（llm_events+isolation+main_ui_events） | `124 passed in 52.35s` | CLI 124 passed | ✓ |
| 全量前端 npm test | `374 passed / 1 failed`（App.guiTheme.test.ts 窗口最大化句柄用例） | 374/375 + 同一用例失败 | ✓ |
| py_compile 11 文件 | 通过 | 通过 | ✓ |
| tests/test_llm_events_fixes.py | `2 failed, 75 passed` | 记录为预先存在 | ✓ 属实（见下） |

预先存在失败独立验证：`%TEMP%\yc_baseline`（已核对其 `core/llm.py` 与测试文件与 `bdebe13` 逐字节一致）上两用例同样失败（pywintypes DLL 环境问题 + plan 模式断言），与本计划无关，执行者记录属实。

## 4. 闭环性对照表

| 验收项 | 证明来源 | 结论 |
|---|---|---|
| FAC1 6 步熔断 / 第 7 次请求不发 | `test_harness_runtime.py::test_plan_read_loop_without_state_change_aborts`（request_count==6），组2 通过 | ✓ |
| FAC2 首次超时移交不重派 | `test_run_subagent_timeout_hands_off`（handoff_task_id==payload.task_id、attempts 不变）+ `rejects_redelegation`（manager Unused 断言）+ E2E（manager 任务数恒 1） | ✓ |
| FAC3 先取消 + fencing | `test_timeout_sets_cancel_before_handoff_callback`；`test_stale_subagent_owner_tools_are_fenced`（adapter 未执行）；fencing 位于 plan 门禁后、resolve/announce/权限前（tool_pipeline.py:795-799） | ✓（取消竞态见 P1-2） |
| FAC4 scoped 权限 | 本回合放行 / 他回合拒绝 / confirm 仍询问 / files 限制 4 个 pipeline 测试 | ✓ |
| FAC5 显式 plan_finish | `test_plan_finish_*`（active 可完成、错 turn/queued/普通/缺上下文拒绝、验证失败 failed 不重派、成功自动提升）；schema 仅 item_id/result | ✓（失败文案见 P2-1） |
| FAC6 FIFO + 会话隔离 | `test_multiple_handoffs_queue_and_promote_fifo` + `test_takeover_queue_isolated` | 部分缺口：崩溃恢复 promote 无生产接线（P1-1） |
| FAC7 去噪 / 指纹 | `test_different_subagent_task_ids`、`test_plan_state_tools`、guard 12/13/重复三测试；指纹含 execution_mode 不含动态字段 | ✓（非计划扇出风险见 P2-3） |
| FAC8 后台任务 | Task1/2 定向复核 23 passed；终态复活/容量/墓碑/poll 异常四测试均在 | ✓ |
| FAC9 GUI/CLI 展示 | `appStore.plan.test.ts`（跨会话不污染）+ `test_main_ui_events.py`（文案去重）；build 通过 | ✓ |
| FAC10 全量矩阵 + 记录真实 | 第 3 节全部独立复跑一致 | ✓ |
| Journal 纯净 | `git diff core/` 无 journal 写入新增；`test_tool_result_journal_draft` 守卫在 | ✓ |
| cancelled 不触发接管 / 不复活 | terminal_callback cancelled → scheduler.cancel 分支；test_cancellation.py 组3 通过 | ✓ |
| 遗留项诚实性 | llm_events_fixes 两失败基线复现 ✓；guiTheme 失败属用户范围（失败点在窗口句柄，与本计划文件零交集）✓；全部 Commit=not-authorized（git log 确认 bdebe13 为最后提交）✓；llm.py `_PLAN_INTERNAL_PERMISSION_TOOLS` 偏离已在 Notes 声明 ✓ | ✓ |
| Test Helper Rule | `_install_active_takeover` / `_fenced_owner_manager` 等均经生产 API 搭状态，断言指向生产行为 | ✓ |
| 交接就绪 | Execution Record 数字全部可复现；未授权提交说明完整 | ✓ |

## 5. 与 Implementation Notes 不一致点

1. Task 4 Notes 称崩溃恢复"下一真实主回合通过 `promote_next_takeover(new_turn_id)` 重新认领"——机制代码存在且有单测，但生产代码从未接线（无 turn 开始处的调用方），该闭环实际不可达（P1-1）。Notes 的措辞让人误以为闭环已完成。
2. Task 5 Notes 称 `_terminate` 重排，未提及丢弃了 `not task.callback_invoked` 认领条件，引入了 cancelled 双回调回归（P1-2）。
3. 其余 Notes 陈述（测试数字、文件清单、偏离声明、预先存在失败）经复核全部属实。

## 6. 维度一逐条核查结论（24 条速览）

A 终态与回调时序：
1. (a)(b)(c) 锁内原子认领终态 + 状态先写可防住 watchdog/worker/前台竞态；(d) timed_out+callback_invoked 早退守卫仍有效；但 cancelled 未受守卫（P1-2）。
2. done_event 晚于 callback：前台循环 / `_watch_background` / `drain_notifications` 均安全；`wait_for_idle` 有极小提前 idle 窗口（P3-4）；`_foreground_result` 读 `task.handoff` 经锁+事件 happens-before，无竞态。
3. callback 失败只追加 error、保留 timed_out，无改写 failed 路径。✓

B 接管状态机：
4. `start_delegated_item` 绑定与 `run(task_id=)` 碰撞拒绝构成闭环；启动失败回滚为 failed+delegated，可经 plan_retry 恢复。✓
5. `handoff_to_main` 四条件校验无绕过；FIFO max+1 与归零策略混合时顺序仍正确（平局按 id）。✓
6. `promote_next_takeover` 忽略 turn 符合"每会话唯一 active"；deps 检查由 `transition_execution` 覆盖。✓ 但无生产调用方（P1-1）。
7. `retry()` 两次提交窗口极窄，CAS 失败被忽略（P3-3）；观察者无实质误导。
8. 崩溃恢复降级逻辑正确但 promote 不可达（P1-1）。

C 门禁与 fencing：
9. 旧 stalled 全局放行分支删除是计划明示取舍；failed 项仍可走 plan_retry+run_subagent，无死锁。✓
10. 门禁路径惰性建调度器仅影响本会话桶，但复位不清 execution_mode 可造成卡死变体（并入 P1-1）。
11. 路径归一化在大小写/相对路径下可靠；符号链接不解析（P3-2）；edit_file 的 file_path 同样被覆盖；execute_command 绕过属计划接受项。
12. `subagent-` 前缀不误伤 `subagent:角色`（验证 runner）；会话名前缀误伤见 P2-2；fail-open 与注释矛盾（P2-2）。
13. fencing 位于 `_plan_constraint` 之后、resolve/announce/权限/adapter 之前，失效租约不会进入执行段。✓

D 进展守卫与去噪：
14. 非计划前台子代理链存在误杀窗口（P2-3）；计划绑定场景有计划指纹兜底。✓
15. 主循环调用点 llm.py:4990 确认传入 `tool_calls_by_id`；签名仅计 ok 的 `_PLAN_READ_ONLY_TOOLS`；重复签名不计。✓
16. 第 12 计进展 / 第 13 不计 / 重复不计，与 tests/test_progress_guard.py 三测试一致（实现符合断言，非迁就）。✓
17. `plan_session_takeover_active` peek 不惰性创建，与指纹 peek 语义一致，无注册表污染。✓

E 时间预算与 schema：
18. 600 秒硬上限只作用于显式传入值；角色默认 240/300/480/600 不受影响；后台 3600 保留；30-3600 校验不冲突。✓
19. adapter 透传 raw arguments，恶意传值被 600/3600 校验拦截，安全。✓
20. 6000 字符拒绝在计划项认领之前返回，无半认领。✓

F 事件与展示：
21. queue_position 无锁重算仅展示不精确（P3-1）。
22. `_last_printed_plan_state` global 提升后去重键互不干扰；handoff 分支先于通用 `plan_item_` 分支拦截完整。✓
23. appStore handoff 分支经 switchedRuntime 绑定/恢复机制不污染并行会话（有测试断言）；`_captureRuntime` 按引用共享是该 store 既有设计。✓
24. ToolExecutionRow 多批次徽标缺失不崩溃（P3-5），可接受。

## 7. 审查纪律声明

本次为只读审查，未修改仓库任何文件、未运行破坏性命令、未提交。排除清单中的用户既有改动（config.py / lan_server.py / GUI 主题相关 / token 统计测试 / TOML 设计文档）未予置评、未建议改动。`%TEMP%\yc_baseline` 为执行者遗留的基线副本，仅用于只读对照。
