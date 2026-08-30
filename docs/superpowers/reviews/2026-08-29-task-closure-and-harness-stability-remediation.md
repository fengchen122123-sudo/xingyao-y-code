# 任务闭环与 Harness 稳定性审查及修复记录

日期：2026-08-29
分支：`dev`
基线：`bdebe13f8d4945898b9f86b106c86d477cb2e89f`（工作区未提交改动）

## 审查结论

本次范围内共确认并修复 7 个 P1、6 个 P2、2 个 P3 问题。修复后的任务状态、Harness Journal 终态、撤销/检查点恢复、权限边界、后台进程终止和测试入口隔离已形成闭环；Provider 请求历史、system prompt、tools schema、Prompt Ledger 和取消后不接手语义未被改动。

本次范围可以合入并进入发布候选验证。此前保留的 5 个失败已全部修复；独立审查补充发现的权限风险、`taskkill` 假成功和 raw terminal 选项绕过也已关闭。最终 Windows 全量结果为 `1707 passed, 2 skipped, 0 failed`，不再依赖“已知失败豁免”。

## 已修复发现

### P1-1 测试入口全局回调泄漏可令权限询问永久等待

- 文件：`tests/conftest.py:20-84`
- 问题：真实 `ApiBridge()` 除 `app_context` 和权限 broker 外，还会注册 token、MCP、子代理终态和后台命令终态四个进程级回调。旧桥对象残留会造成跨测试串扰；GUI 权限通道残留是全量套件挂起的直接条件。
- 修复：autouse fixture 对入口层六个全局、broker 三个 handler 和四个 GUI 回调统一快照/还原。
- 依据：最终全量套件在 Windows 上 3 分 49 秒完整结束，无永久等待。

### P1-2 普通 delegated 计划项重启后被误判为超时接管项

- 文件：`core/plan.py:394-447`、`core/plan.py:540-560`
- 问题：`handoff_task_id` 是所有委派项的普通字段，不能证明发生过超时移交。旧逻辑会把普通失败/崩溃项标成 `takeover_required`，同时保留死亡 owner/task lease，导致不能重试也不能继续委派。
- 修复：只从显式 `takeover_required` 或接管模式推断接管；加载时把崩溃遗留的 delegated `in_progress` 项恢复为 pending，并清空死亡租约字段。
- 回归：`tests/test_plan.py:52`、`tests/test_plan.py:74`。

### P1-3 write_file 撤销/检查点可能删错空文件或损坏非 UTF-8 字节

- 文件：`core/tools.py:482-570`、`core/memory.py:187-294`、`core/agent_service.py:574-659`、`core/harness/tool_adapters.py:465-507`、`main.py:2073-2197`
- 问题：`old_content == ""` 同时表示“原文件为空”和“文件原本不存在”，CLI 会把已存在空文件当作新文件删除；`errors="replace"` 读取非 UTF-8 后再写回会永久写入替换字符；Harness 适配器还会覆盖 `core.tools.write_file` 已记录的精确快照。
- 修复：撤销记录显式保存 `existed_before` 和 `old_bytes`；恢复时原子写回原始字节；相对/绝对路径按规范化真实路径去重；默认适配器不再二次覆盖记录；CLI `/undo` 与 `/checkpoint restore` 使用同一存在性语义，旧记录仍按原约定兼容。
- 回归：`tests/test_agent_service.py:328`、`tests/test_agent_service.py:351`、`tests/test_main_ui_events.py:10`、`tests/test_main_ui_events.py:42`、`tests/test_tool_pipeline.py:289`。

### P1-4 GUI 并行回合的 undo 状态落入共享 `_global_`

- 文件：`core/memory.py:176-190`
- 问题：GUI 回合线程绑定的是 `app_context` 会话 TLS，而 memory 自己的 undo TLS 只在 UI/RPC 线程被设置，线程之间不会继承；两个 GUI 会话的写文件记录会共同覆盖 `_global_`，按当前会话撤销时还可能查不到。
- 修复：显式 undo session 优先；未显式绑定时回退到 `app_context.get_thread_session()`，最后才使用 `_global_`。
- 回归：`tests/test_session_parallel_isolation.py` 并发启动两个 GUI 会话线程，各自记录并读取独立 undo 快照。

### P1-5 子代理写文件未进入父回合 checkpoint

- 文件：`core/memory.py:245-369`、`core/tools.py:2725-2800`
- 问题：checkpoint 只存在父 worker 的 TLS；实现型子代理在独立线程写文件时看不到父回合上下文，导致“整轮回滚”遗漏子代理产生的主要副作用。
- 修复：以 `(session, turn)` 注册活动 checkpoint，每个上下文用独立锁保护 `completed` 和快照列表；子代理只绑定借用记录权，父线程独占提交权。父回合结束时原子标记完成并注销，已经绑定但迟到的取消子代理也不能再向终态 checkpoint 追加记录。
- 回归：`tests/test_tools.py` 的子代理真实调用 `write_file` 后，父回合 checkpoint 包含原始字节快照。

### P1-6 沙箱外文件授权丢失原操作风险语义，可能放宽 plan/credential 写入

- 文件：`core/harness/tool_pipeline.py:740-760`、`core/llm.py:507-603`、`core/llm.py:831-930`
- 问题：外部 `edit_file` 先按普通工具名进入兼容权限注入点，adapter 随后可能在沙箱授权前探测文件；改为 `sandbox_outside_access` 后若只把 `operation` 当展示字段，风险计算会退化为 QUERY，使 plan 外部写、full + 沙箱开放或任务 bypass 下的 credential 写可能被错误放行。raw terminal 还曾无视实际 option keys，凭据请求输入 `a` 会额外建立任务沙箱放行。
- 修复：外部读写编辑统一先以 `sandbox_outside_access` 和结构化 `{path, operation}` 授权；授权器对白名单 operation 重新做路径分类，并用原 read/write/edit 计算风险、plan 门禁和授权规则。credential 写强制逐次确认、忽略任务 sandbox bypass，只提供 allow-once/deny。raw terminal 严格依据实际 options 展示/接受 `a`。只有旧式布尔权限替身在批准后获得本次临时 bypass，结构化授权严格尊重自身 `sandbox_bypass`。
- 安全核对：plan 外部写在提示前硬拒；credential 文件在 full、沙箱开放、任务 bypass 同时存在时仍逐次询问；拒绝授权时不执行文件存在性探测；普通外部只读的任务 bypass 保持有效。
- 回归：`tests/test_llm_events_fixes.py:1087`、`tests/test_tool_pipeline.py:488`、`tests/test_tool_pipeline.py:537`、`tests/test_permission_upgrade.py:182`、`tests/test_permission_upgrade.py:206`。

### P1-7 kill 失败或进程身份不可验证时被误报为 cancelled

- 文件：`core/task_manager.py:793-911`
- 问题：PID 已复用、无法验证进程身份、`taskkill` 失败但进程仍存活三类情况共用清理分支；而 `taskkill` 仅返回 0 也会被当成真实终止。结果是未被终止的任务可能标成 cancelled、关闭仍在写入的日志句柄并停止观察。
- 修复：状态显式分流，并新增有限等待的真实存活复核：已证明 PID 复用/初始已退出按自然终态保留且返回 false；身份不可验证 fail-closed 为 failed；taskkill/fallback kill 后仍存活保持 running、revision 与日志句柄不变；只有复核已退出才转 cancelled。终态记录继续保留供 snapshot/wait 查询。
- 回归：`tests/test_task_manager.py:108`、`tests/test_task_manager.py:148`、`tests/test_task_manager.py:214`、`tests/test_task_manager.py:258`、`tests/test_task_manager.py:305`、`tests/test_task_manager.py:363`。

### P2-1 回合中途产生的主 Agent 接管未在回合终态释放

- 文件：`core/llm.py:2198-2310`
- 问题：旧释放条件要求接管必须在回合开始时已认领；子 Agent 在回合中途超时移交时，正常结束会遗留 active takeover，取消后还可能在下一回合复活。
- 修复：按当前 turn id 幂等释放，不再依赖开始时的 `takeover_claimed` 快照；只有实际释放成功后才置幂等标志。
- 回归：`tests/test_llm_events.py:226`、`tests/test_llm_events.py:246`。

### P2-2 接管认领异常会让 Harness Turn 永久停在 preparing

- 文件：`core/llm.py:2218-2310`
- 问题：`_claim_takeover_at_turn_start` 和生成器构造位于异常收口 `try` 之外，存储异常时已创建的 Journal Turn 没有失败终态。
- 修复：把接管认领和生成器构造纳入同一 `try/except/finally`；异常调用 `harness_runtime.fail(...)` 并发送 `turn_error`。
- 回归：`tests/test_harness_runtime.py:169`。

### P2-3 NoProgress 熔断的公共事件被误报为成功完成

- 文件：`core/llm.py:4406-4427`
- 问题：内部 Journal 已标记 failed，但事件层只发送通用 `error` 后正常 return，外层仍发送 `turn_finished`。
- 修复：熔断同步发送 `turn_error`，外层不再补成功终态。
- 回归：`tests/test_harness_runtime.py` 的 no-progress 用例明确断言一个 `turn_error` 且没有 `turn_finished`。

### P2-4 测试旁路桥仍缺 LAN 初始化字段

- 文件：`tests/test_gui_model_settings.py:1-18`、`tests/test_gui_token_usage.py:8-17`
- 问题：`ApiBridge.__new__` 绕过 `__init__`，`get_initial_state` 的后台线程读取 `_lan_server` 时抛 `AttributeError`，形成 pytest 未处理线程警告。
- 修复：旁路 helper 补齐 `_turn_start_lock`、`_message_queues`、`_lan_server`、`_lan_lock` 的真实类型/默认值，并在状态快照测试中禁止启动真实 LAN 服务。
- 回归：相关用例以 `PytestUnhandledThreadExceptionWarning` 提升为 error 后通过。

### P2-5 全局技能扫描仍有第三处硬编码 `~/.ycode`

- 文件：`core/skills.py:195`
- 问题：发布与标记已使用 `config.YCODE_DIR`，但扫描入口仍读用户 HOME，`YCODE_DATA_DIR` 覆盖时会发布到 A、扫描 B。
- 修复：扫描路径改为动态读取 `config.YCODE_DIR`。
- 回归：`tests/test_skills.py:32`。

### P2-6 压缩测试依赖缓存模块的导入副作用，无法独立或乱序运行

- 文件：`tests/test_llm_events_fixes.py:162-238`、`tests/test_llm_events_fixes.py:428-509`、`tests/test_llm_events_fixes.py:1621-1668`
- 问题：D1 夹具会按设计还原 `app_context`；测试在此后只 patch 已缓存的 `main.manager/current_session`，但 Python 不会重新执行 `main` 的注册副作用，导致压缩拿到空 manager/session，形成顺序相关失败。
- 修复：核心层压缩测试直接 patch `app_context.get_manager/get_current_session`，与被测依赖边界一致，不再依赖入口模块的首次导入时机。
- 回归：5 个压缩、回滚和超窗熔断用例可单独运行并通过，全量套件同样通过。

### P3-1 后台命令展示依赖可变中文结果文案

- 文件：`core/tool_presenter.py:160-164`
- 问题：用结果是否包含“异步后台任务已成功启动”判断 background，文案调整或插件返回相同文字都会误判。
- 修复：只在 `execute_command` 成功且结构化参数 `run_in_background is True` 时标记。
- 回归：`tests/test_tool_presenter.py:60`。

### P3-2 D3 元数据扩展后旧测试桩签名未兼容

- 文件：`tests/test_llm_events_fixes.py:559-566`
- 问题：测试把 `set_undo_state` patch 成只收两个位置参数的 lambda，新实现传关键字元数据后测试桩抛错，掩盖原本要验证的 GBK fallback。
- 修复：测试桩显式接收并记录 `**metadata`。

## 关键不变量核对

- Provider 历史中间字节：未修改请求历史维护、消息规范化、system prompt、tools schema 或请求 envelope。
- DeepSeek Prompt Ledger：未修改 CAS、追加、切代、tools 顺序和 reasoning 字段。
- 取消语义：取消路径释放/取消 takeover，不注入“接手完成”；现有取消回归通过。
- Harness 非插件化：所有修复均在固定 runtime/入口/adapter 中完成，未增加动态 Harness 阶段。
- 崩溃恢复：仅清理死亡租约并恢复为待执行/排队，不自动重放模型请求或副作用工具。
- 撤销安全：新建文件仍需 force 删除确认；已存在空文件和非 UTF-8 文件可无损恢复。

## 验证结果

1. `venv\Scripts\python.exe -m pytest -q`
   - 首次：`6 failed, 1689 passed, 2 skipped, 1 warning in 242.75s`；其中 1 个 D3 测试桩失败和 1 条 D2 线程警告属于本次耦合并已修复。
   - 独立审查前：`5 failed, 1690 passed, 2 skipped in 229.03s`；完整结束且无线程警告。
   - 中间：`5 failed, 1694 passed, 2 skipped in 224.00s`；确认剩余失败全部位于权限边界和 kill 语义。
   - 权限/kill 独立复审修复后：`1705 passed, 2 skipped in 243.03s`。
   - raw terminal 选项修复后的发版最终：`1707 passed, 2 skipped in 232.74s`；0 failed，完整退出，无挂起与线程警告。
2. 审查清单后端集：`281 passed, 1 skipped in 9.82s`。
3. 最后 5 个阻塞及沙箱相邻用例：`8 passed in 0.65s`。
4. 5 个压缩/回滚/熔断顺序隔离用例：`5 passed in 115.85s`。
5. `tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py`：最终 `30 passed in 3.12s`。
6. `frontend npm test`：`57 files / 383 tests passed in 33.30s`。
7. `frontend npm run build`：`vue-tsc -b && vite build` 成功，174 modules transformed。
8. `py_compile`：`main.py`、任务/Harness/撤销/技能/展示相关模块全部通过。
9. `git diff --check`：无空白错误，仅现有 Windows LF→CRLF 提示。
10. 独立审查修复后的受影响回归：`473 passed in 83.23s`。
11. 父 checkpoint 提交与迟到子代理记录竞态：`2 passed in 0.55s`（含正常子代理继承用例）。
12. 权限策略、raw terminal、沙箱全链路与后台 kill 相邻集：`141 passed in 2.91s`。
13. 独立只读复审：首轮发现 1 Critical、1 Important、1 Minor，复审确认三项已关闭；二次复审补充发现 raw terminal 选项遗漏，随后已按原审查建议修复并以双向回归覆盖。最终确认回合因审查子任务额度耗尽未返回新判定，故合入结论以实际修复、完整测试及字节黄金结果为依据。

## 合入建议

本次任务闭环与 Harness 稳定性修复可以合入，并已满足自动化发版门禁：Python 全量零失败、Provider 字节黄金全绿、前端测试与生产构建成功、语法与 diff 检查通过。正式发布前仍应按项目发布流程执行打包产物启动、CLI/GUI 各一次真实交互和目标 Provider 冒烟；这些属于环境/凭据驱动的发布验证，不应由单元测试结果替代。
