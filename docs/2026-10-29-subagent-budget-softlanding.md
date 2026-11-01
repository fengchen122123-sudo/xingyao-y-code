# 子代理执行完整度修复：时间预算软着陆与超时交付增强

日期：2026-10-29
范围：`core/tools.py`（SubagentManager / run_subagent）、`core/llm.py`（Step 边界提醒注入）

## 一、问题

用户反馈：子代理任务「执行完整程度差，经常超时」。

## 二、证据（来自本机历史数据，非推测）

1. **超时率**：`~/.ycode/ycode_gui.db` 的 `task-notification` 历史中，后台子代理终态为
   `timed_out` 41 次、`completed` 25 次（超时占 62%）。
2. **任务规模**：近 30 天子代理 LLM 调用 10311 次，按 180s 间隔切分为 1371 个任务。
   任务时长 p90=215s、p99=1219s、max=2921s；其中 `implement` p99=1344s，最长单任务
   356 次 LLM 调用。
3. **预算错配**：旧默认预算 explore 240 / review 300 / test 480 / implement 600，
   即 explore/review 卡在各自实测 p99 上，implement 只有 p99 的不到一半。
4. **无收尾机制**：全仓库检索 `deadline` / `budget` / `预算`，子代理路径没有任何
   「临近超时先收尾」逻辑，到点直接置 `cancel_event` 硬砍。
5. **交付残片**：超时后给主 Agent 的 `partial_result` 固定截断 1500 字符（最终结果上限
   16000），主 Agent 拿到的信息不足以判断成果是否已落地，倾向从头重做，形成
   「再派一次 → 再超时」的放大回路。

## 三、改动

### 1. 时间预算软着陆（core/llm.py + core/tools.py）

- `chat_with_model_events` 新增可选参数 `budget_notice_provider`，在**每个 Step 边界**
  （上一 Step 工具调用与结果已配对并 flush 之后）调用一次；返回文本则以 `user` 消息插入，
  并上报 `budget_notice` 事件。放在 Step 中间会制造悬空 `tool_calls`（OpenAI/Anthropic
  都会 400），因此位置固定在 stop-notice 注入之后。
- 子代理侧 `_budget_notice_provider` 读取 `task.execution_elapsed` 与 `timeout_seconds`：
  - 剩余 ≤ max(90s, 25% 预算) → 收尾提醒（级别 1）；
  - 剩余 ≤ max(45s, 10% 预算) → 立即终止探索并输出结论（级别 2，要求不再调用工具）；
  - 同一级别只注入一次（`SubagentTask.budget_notice_stage`）。
- 提醒同时写入任务阶段历史（`budget_notice`），右面板执行流程可见。
- provider 抛异常一律吞掉，不影响子代理回合。

### 2. 预算与任务规模匹配（core/tools.py）

- 默认预算：explore 240→300、review 300→420、test 480→900、implement 600→900、
  general 600→900（自定义类型兜底 900）。
- 前台硬上限 600→900（`SUBAGENT_FOREGROUND_MAX_TIMEOUT`）。
- 后台任务不阻塞用户：未显式传预算时按 1.5 倍放宽（上限 3600）。

### 3. 超时交付增强（core/tools.py）

- `partial_result` 交付上限 1500 → `SUBAGENT_PARTIAL_DELIVERY_MAX_CHARS`（12000），
  截断时明确标注「完整内容以工作区实际改动为准」。
- 失败文本新增进度行：阶段 / 工具成功次数 / 执行耗时（预算）/ 审批等待。
- 超时提示按是否有部分产出分流：
  - 有 partial：先核对工作区实际改动，在已有成果上继续，不要从头重做；
  - 无 partial：直接用主 Agent 自身工具完成或拆小重派。
- 超时/取消后给 worker 线程 2s 打捞窗口（`thread.join`），等 finally 把最后一段正文
  写进 `partial_result` 再返回，避免交付 2000 字符前的旧快照。

### 4. 结论格式收紧（core/tools.py）

子代理系统提示新增「结论格式」：只写主 Agent 决策所需事实（做了什么、改了哪些文件、
验证结果、未完成项与风险），不复述任务描述、不逐条回顾工具调用、不做推测性延伸。
实测一个「读一行」的子代理返回 800+ 字报告，这类铺陈会直接挤占时间预算。

## 四、验证

- `python -m py_compile core/tools.py core/llm.py` 通过。
- `tests/test_tools.py` + `tests/test_tool_registry.py`：243 passed。
- `tests/test_llm_events.py`：71 passed（含新增注入通道与异常吞噬用例）。
- 子代理相关 6 个测试文件：98 passed。
- `tests/test_tool_pipeline.py` + `tests/test_tool_presenter.py` +
  `tests/test_task_scheduler.py`：123 passed。
- 真实子代理冒烟：explore 类型读一行任务正常返回，链路与事件无异常。

## 五、已知边界

- 提醒只在 Step 边界生效：如果子代理正卡在一次长工具调用（如跑 5 分钟回归），
  提醒无法中途插入。工具执行路径已有 `cancel_event` 协作式检查，超时仍会被砍。
- 超时仍是协作式取消：`abort_inflight_streams` 关闭在飞 LLM 请求，但不可中断的
  工具进程只能等其返回或自身检查 cancel。
- 未改动模型可见 schema（`timeout_seconds` 仍由系统内部管理），避免模型用大预算
  把前台回合拖成无响应。
