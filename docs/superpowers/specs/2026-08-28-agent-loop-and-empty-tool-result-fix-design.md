# YCODE Agent 工具循环与“空结果”修复设计

日期：2026-08-28  
状态：待实施  
范围：Harness 工具重复门禁、后台任务状态查询、主代理无进展终止、计划状态机停滞检测

## 1. 背景与问题陈述

2026-08-28 的 GUI 会话出现多次主代理长时间原地循环：模型反复调用工具、后台任务状态无法继续查询、界面连续显示“执行成功，但命令没有返回任何输出内容”，部分回合直到用户取消或达到 100 轮上限才结束。

运行数据库 `~/.ycode/ycode_gui.db` 的核查结果如下：

- 当天 26 个 Harness Turn 平均执行 22.3 个 Step，前一天为 9.4 个。
- 当天有 8 个 Turn 超过 30 个 Step，最长分别达到 100、78、72、61、57 个 Step。
- 当天持久化了 685 条工具结果，没有一条工具结果正文真正为空。
- 其中 18 条结果明确写入“执行成功，但命令没有返回任何输出内容”。这些调用主要是 `ping ... >nul`、输出重定向、删除文件或无匹配搜索，工具结果没有丢失。
- 重复调用门禁当天产生 24 次拦截，前一天只有 3 次。一个 Kimi 回合中，`manage_task(status)` 被同一门禁连续拦截 16 次。
- 一个 GLM 回合执行满 100 个 Step、112 次工具调用后才被全局轮次上限终止。

因此，“空结果”不是数据库写入、Harness Journal 或 GUI 重放丢失，而是模型在无法正常轮询后台任务后，主动执行无 stdout 的等待命令所形成的表象。

## 2. 根因

### 2.1 RepeatCallGuard 错误拦截状态查询

`core/harness/tool_pipeline.py:RepeatCallGuard` 以“工具名 + canonical arguments”为签名。相同签名成功两次后，第三次及后续调用不再执行。

这个规则适用于重复写文件、重复执行命令等静态或有副作用的调用，但不适用于 `manage_task(action="status")`：后台任务会随时间推进，相同参数的每次查询都可能返回新日志、新状态或退出码。

当前实现还有两个语义错误：

1. 拦截文案声称“结果未发生变化”，但实现只统计参数签名，从未比较工具结果。
2. 拦截文案以 `🛑` 开头，而 `_tool_result_ok` 只识别 `🚨` 等失败前缀，因此 Harness Journal 将“本次没有执行”的拦截结果记录为 `ok`。

### 2.2 缺少严格的无进展终止条件

主模型循环只存在 100 轮硬上限。只要模型持续生成不同参数，RepeatCallGuard 就无法阻止语义相同的尝试。

当前系统没有基于以下真实状态判断“本轮是否推进”：

- 后台任务状态和日志版本；
- 计划 canonical snapshot；
- 工具结果哈希；
- 成功的文件或进程变更。

因此模型可以在后台轮询、读取日志、睡眠命令、计划重试之间持续切换，直到 100 轮上限。

### 2.3 后台任务没有“等待变化”协议

`manage_task(status)` 只返回当前日志尾部，没有 revision/cursor，也没有等待状态变化的操作。模型若要等待后台测试，只能立即重复查询，或者调用 `ping >nul`、`sleep` 等命令人为等待。

RepeatCallGuard 又会拦截第三次状态查询，最终迫使模型选择空输出等待命令。

### 2.4 计划状态机错误状态可持续自旋

计划工具为保证 `plan_retry` 两段式语义而豁免重复门禁，这是必要行为。但当前没有基于计划快照判断状态是否真实变化。

以下调用组合可能持续循环：

- 对 completed 项反复调用 `plan_retry`；
- `run_subagent` 因依赖或状态不满足持续返回 blocked；
- `ask_user` 因计划项不可执行持续失败；
- 重复 `plan_read`，计划快照不变。

## 3. 设计目标

1. 允许后台状态查询重复执行，但不放宽副作用工具的重复保护。
2. 明确区分“工具执行成功”“工具执行失败”“工具未执行并被门禁阻止”。
3. 为后台任务提供基于 revision 的状态变化等待，不允许使用空输出命令充当计时器。
4. 使用真实状态哈希检测无进展，不依赖模型自然语言或模糊语义判断。
5. 连续无进展时尽早显式失败，不注入无限重试提示，不自动降级或偷偷换方案。
6. 保留 100 轮上限作为最后安全阀，但正常运行不应再触发该上限。
7. 不改变 DeepSeek Prompt Ledger、Kimi 消息规范、缓存前缀字节和历史中间消息字节。

## 4. 非目标

- 不通过简单降低 `max_turns` 掩盖根因。
- 不关闭 RepeatCallGuard。
- 不根据模型正文做“看起来像在循环”的自然语言识别。
- 不自动替模型改写命令、切换 Shell 或吞掉工具错误。
- 不修改权限策略、权限白名单或沙盒边界。
- 不重构整个 `core/llm.py` 或 Harness 生命周期。
- 不在本次修复中优化图片/base64 上下文；图片会放大循环成本，但不是循环根因。

## 5. 总体方案

采用“参数感知重复策略 + 后台任务 revision/wait + 严格进展指纹 + 显式停滞失败”的组合方案。

### 5.1 行为矩阵

| 场景 | 目标行为 |
|---|---|
| `manage_task(status/list/wait)` | 允许重复执行，因为返回值随外部状态变化 |
| `manage_task(kill)` | 继续受重复调用保护 |
| `execute_command`、`write_file`、`edit_file` | 保持现有重复调用保护 |
| 计划状态机工具 | 保留现有豁免，由计划快照哈希判断是否推进 |
| 重复调用被拦截 | 返回明确失败，本次调用不计作成功 |
| 后台任务等待 | 使用 `manage_task(wait)` 等待 revision 变化 |
| 连续 6 个模型 Step 无真实进展 | Turn 以 `NoProgressError` 显式失败 |
| 100 轮上限 | 继续作为最后安全阀 |

## 6. 详细设计

### 6.1 参数感知的 RepeatCallGuard 策略

修改 `core/harness/tool_pipeline.py`，将固定工具名豁免判断替换为参数感知函数：

```python
def repeat_guard_exempt(name: str, arguments: dict) -> bool:
    if name in {"plan_write", "plan_update", "plan_read", "plan_retry"}:
        return True
    if name == "manage_task":
        action = str(arguments.get("action", "")).strip().lower()
        return action in {"status", "list", "wait"}
    return False
```

要求：

- 判断使用已经严格解析完成的 `DecodedToolCall.arguments`，不重复解析原始 JSON。
- `manage_task(kill/stop/terminate/cancel)` 不豁免。
- 原有计划工具豁免保持不变，避免破坏 `plan_retry` 的两阶段流程。
- 未知 action 不豁免，按保守策略处理。

### 6.2 修正重复拦截结果语义

重复调用被阻止时使用以下固定正文：

```text
🚨 相同工具调用已经成功执行过 2 次，本次调用未执行。请检查已有结果或改变执行方案。
```

要求：

- 删除“结果未发生变化”表述，因为 RepeatCallGuard 不比较结果。
- `_tool_result_ok` 必须将该结果识别为失败。
- Harness `tool_result_committed.result_status` 写入 `error`，不能写 `ok`。
- RepeatCallGuard 不得把拦截结果再次计入成功次数。
- GUI 保持现有错误工具卡片展示，不新增静默处理。

### 6.3 后台任务 revision

为后台任务增加单调递增的 `revision: int`，初始值为 0。以下状态变化必须在持有任务锁时递增 revision：

- 捕获到新的 stdout/stderr 内容；
- 状态字段变化；
- 设置退出码；
- 进入 `completed`、`failed`、`timed_out` 或 `cancelled`；
- 任务被 kill 后完成清理。

禁止使用 wall-clock 时间、随机值或轮询次数生成 revision。revision 只能在可观察状态真正变化时增加。

后台任务状态快照定义为：

```python
@dataclass(frozen=True)
class TaskStatusSnapshot:
    task_id: str
    status: str
    revision: int
    terminal: bool
    output: str
    exit_code: int | None
```

在 `core/tools.py` 内部从受锁保护的任务状态构造该不可变快照；不得向调用方暴露可变任务对象。

### 6.4 `manage_task(wait)`

扩展 `manage_task` 工具 schema：

```python
manage_task(
    action="wait",
    task_id="task-id",
    after_revision=12,
    timeout_seconds=30,
)
```

参数规则：

- `task_id`：必填，必须存在。
- `after_revision`：必填，非负整数。
- `timeout_seconds`：可选，默认 30，最小 1，最大 60。

等待规则：

1. 当前 revision 大于 `after_revision` 时立即返回。
2. 任务已经终态时立即返回，即使 revision 未增加。
3. 否则等待条件变量通知，直到 revision 变化、任务终止、用户取消或超时。
4. 用户取消时遵守现有 `cancel_event`，立即抛出取消路径，不伪装成普通超时。
5. 超时不是工具失败，返回 `changed=false` 和当前快照。
6. 禁止使用固定 `sleep` 忙等；使用 `threading.Condition` 或等价的条件等待。

模型可见结果使用稳定 JSON：

```json
{
  "task_id": "3fda6b5a",
  "status": "running",
  "revision": 13,
  "changed": true,
  "terminal": false,
  "exit_code": null,
  "output": "新增日志内容"
}
```

`manage_task(status)` 同样返回该结构，但不等待；`list` 继续返回任务快照列表。CLI 展示层负责把结构格式化成人类可读文本，Harness 传给模型的工具正文保留机器可解析字段。

### 6.5 TurnProgressGuard

新建 `core/harness/progress_guard.py`，避免继续扩大 `core/llm.py`。

接口：

```python
@dataclass(frozen=True)
class ProgressObservation:
    task_snapshots_sha256: str
    plan_snapshot_sha256: str
    tool_results_sha256: str
    mutation_committed: bool


class TurnProgressGuard:
    MAX_STALLED_STEPS = 6

    def observe(self, observation: ProgressObservation) -> bool:
        """返回本 Step 是否产生真实进展。"""

    @property
    def stalled_steps(self) -> int:
        ...

    def should_abort(self) -> bool:
        ...
```

进展判定必须只依赖确定性状态：

- 任一后台任务的 `revision/status/terminal/exit_code` 变化；
- 计划 canonical snapshot 哈希变化；
- 本 Step 成功提交修改类工具；
- 非观察类工具产生新的结果哈希。

以下行为不算进展：

- RepeatCallGuard 拦截结果；
- 同一任务相同 revision 的 `status/wait` 结果；
- 计划快照不变时重复 `plan_read/plan_retry/run_subagent`；
- 仅执行 `ping >nul`、`sleep`、`Start-Sleep` 等无业务状态变化的等待命令；
- 只改变工具 call id、JSON 键顺序或空白字符。

哈希要求：

- JSON 必须使用 `ensure_ascii=False`、`sort_keys=True`、紧凑分隔符 canonicalize。
- 哈希使用 SHA-256。
- 不写入用户正文、工具参数原文或工具结果原文到 Harness Journal；Journal 仍只保存哈希与状态。

### 6.6 无进展终止

当 `TurnProgressGuard.should_abort()` 为真时：

1. 不再创建新的 Provider Step。
2. 当前 Step 完成必要的持久化后标记为 failed。
3. Turn 标记为 failed，`error_code="NoProgressError"`。
4. 向用户输出固定错误：

```text
🚨 连续 6 轮没有检测到后台任务、计划或工具结果状态变化，回合已终止，避免继续循环消耗。
```

5. 不自动重放请求，不自动重启后台任务，不注入“再试一次”提示。
6. 用户后续发送新消息时可以创建新 Turn，不污染新 Turn 的进展计数。

### 6.7 计划快照

计划 canonical snapshot 至少包含：

- plan session id；
- 每个 item 的 id、status、attempts、dependencies；
- 当前 in-progress item；
- auto-continue suppression 状态。

不得把 display 文案、时间戳或无关字段纳入哈希，避免无业务变化却不断重置停滞计数。

`plan_retry` 的两段式行为应当自然通过：第一次返回分析提示不改变状态，不算进展；第二次真正把 failed/blocked/cancelled 重置为 pending 时计划哈希变化，停滞计数清零。

### 6.8 100 轮上限

保留 `max_turns = 100`，暂不降低。

原因：

- 100 轮仍作为最终资源安全阀。
- 新的 NoProgressError 应在第 6 个无进展 Step 后终止真实循环。
- 降低上限会误伤确实持续产生文件、计划或后台状态变化的长任务。

若修复上线后的运行数据仍出现大量 50+ Step 且每步都有真实进展，再单独评估是否需要可配置轮次预算。

## 7. 文件变更范围

### 新建

- `core/harness/progress_guard.py`：确定性进展观察、停滞计数和终止判断。
- `tests/test_progress_guard.py`：ProgressGuard 纯单元测试。

### 修改

- `core/harness/tool_pipeline.py`：参数感知重复豁免、拦截错误语义、进展事件所需元数据。
- `core/tools.py`：后台任务 revision、条件通知、`manage_task(wait)`。
- `core/tool_registry.py`：`manage_task` 的 action 与参数 schema。
- `core/harness/tool_adapters.py`：透传 wait/revision 参数和结构化结果。
- `core/llm.py`：每个工具 Step 后提交 ProgressObservation，在下一 Provider Step 前执行 NoProgressError 门禁。
- `tests/test_tool_pipeline.py`：RepeatCallGuard 与 result_status 回归。
- `tests/test_tools.py`：后台 revision/wait/cancel 回归。
- `tests/test_harness_runtime.py`：无进展 Turn 生命周期回归。
- `tests/test_llm_events.py`：模型事件与用户可见错误回归。
- `tests/test_plan.py` 或现有计划状态机测试文件：计划快照变化与停滞回归。

## 8. TDD 实施顺序

### 阶段 A：修复 RepeatCallGuard

1. 写失败测试：`manage_task(status)` 连续三次均真实执行。
2. 写失败测试：`manage_task(kill)` 第三次相同调用被拦截。
3. 写失败测试：拦截结果的 Journal `result_status` 为 `error`。
4. 实现参数感知豁免与错误前缀。
5. 运行 `tests/test_tool_pipeline.py`。

### 阶段 B：后台 revision 与 wait

1. 写失败测试：输出新增时 revision 单调增加。
2. 写失败测试：状态变化和终态会增加 revision。
3. 写失败测试：`wait` 在 revision 变化时立即返回。
4. 写失败测试：`wait` 超时返回 `changed=false`。
5. 写失败测试：取消事件立即中断 wait。
6. 实现条件变量和 schema。
7. 运行 `tests/test_tools.py`、`tests/test_tool_registry.py`、`tests/test_tool_pipeline.py`。

### 阶段 C：TurnProgressGuard

1. 写 ProgressGuard 纯单元失败测试。
2. 实现 canonical fingerprint 和 6 Step 停滞计数。
3. 写 Harness E2E 失败测试：模拟模型持续返回相同状态查询。
4. 接入 `core/llm.py` 与 Harness 状态转换。
5. 验证第 6 个无进展 Step 后不再产生下一次 Provider 请求。

### 阶段 D：计划停滞

1. 写失败测试：计划快照不变时连续控制调用会触发停滞。
2. 写失败测试：`plan_retry` 真正改变状态后计数清零。
3. 将 canonical plan snapshot 接入 ProgressObservation。
4. 运行全部计划、子代理和取消语义回归。

## 9. 测试矩阵

| 用例 | 期望结果 |
|---|---|
| `manage_task(status)` 连续调用 3 次 | 三次均执行，不触发 RepeatCallGuard |
| `manage_task(kill)` 连续调用 3 次 | 第三次被拦截，记录为 error |
| 后台日志新增 | revision 增加，wait 立即返回 changed=true |
| 后台任务无变化 | wait 到期返回 changed=false，不视为工具错误 |
| 后台任务完成 | wait 立即返回 terminal=true 和退出码 |
| 用户取消 wait | 进入现有取消路径，不生成网络/工具错误重试 |
| 6 个 Step 状态完全不变 | Turn failed，error_code=NoProgressError |
| 第 5 个 Step 后 revision 变化 | 停滞计数清零，Turn 继续 |
| 计划 read/retry/subagent 循环且快照不变 | 触发 NoProgressError |
| `plan_retry` 第二次改变状态 | 不误杀，停滞计数清零 |
| 正常连续编辑多个文件 | 每次 mutation 均算进展，不触发熔断 |
| 相同 read_file 第三次调用 | 保持原有重复保护 |
| DeepSeek/Kimi 请求 golden | 请求字节与既有 golden 保持稳定 |

## 10. 验证命令

定向测试：

```powershell
venv\Scripts\python.exe -m pytest tests/test_tool_pipeline.py tests/test_tools.py tests/test_tool_registry.py tests/test_progress_guard.py -q
```

Harness 和计划回归：

```powershell
venv\Scripts\python.exe -m pytest tests/test_harness_runtime.py tests/test_harness_journal.py tests/test_harness_message_commit.py tests/test_llm_events.py tests/test_plan.py -q
```

消息与 Provider 字节稳定性：

```powershell
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py -q
```

取消、后台任务与会话隔离：

```powershell
venv\Scripts\python.exe -m pytest tests/test_cancellation.py tests/test_agent_turn_e2e.py tests/test_session_parallel_isolation.py -q
```

语法检查：

```powershell
venv\Scripts\python.exe -m py_compile core/llm.py core/tools.py core/tool_registry.py core/harness/tool_pipeline.py core/harness/progress_guard.py
```

## 11. 验收标准

修复只有同时满足以下条件才可交付：

1. `manage_task(status/list/wait)` 可重复观察真实状态，副作用 action 仍受重复保护。
2. RepeatCallGuard 拦截结果不再记录为成功。
3. 后台任务等待不再需要 `ping >nul`、`sleep` 或轮询日志文件。
4. 确定性无进展最多持续 6 个 Step，第 7 次 Provider 请求不得发送。
5. 计划状态机在快照不变时能够显式失败，在真实状态变化时不会误杀。
6. Harness Journal 仍不保存正文、reasoning、工具参数或工具结果原文。
7. DeepSeek/Kimi 请求 golden、Prompt Ledger 和 prompt byte stability 全部通过。
8. 正常长任务只要持续产生真实状态变化，就不会因固定总轮数提前失败。
9. GUI 能显示明确的 `NoProgressError`，用户无需等待到 100 轮才知道任务停滞。
10. 不修改权限策略、缓存前缀、消息历史中间字节和用户现有配置。

## 12. 风险与控制

### 误判长任务无进展

控制措施：只使用后台 revision、计划 canonical snapshot、结果哈希和已提交 mutation 判断，不读取模型自然语言；`manage_task(wait)` 在无日志但进程仍运行时返回稳定状态，连续 6 次确实无变化才终止当前 Turn。

### 状态查询放开后高频轮询

控制措施：提示模型优先使用 `manage_task(wait)`；`status` 保持立即返回，但 ProgressGuard 会阻止无变化的高频循环。

### revision 并发竞态

控制措施：revision 更新、状态更新和条件变量通知必须在同一任务锁内完成；等待方只读取不可变快照。

### 计划两段式重试被误杀

控制措施：保留计划工具 RepeatCallGuard 豁免，并以计划快照真实变化清零停滞计数。

### Provider 请求字节漂移

控制措施：ProgressGuard 状态不写入 system prompt、动态 prompt 或历史消息；仅在 Harness 内部观察，只有终止时新增固定用户可见错误消息。

## 13. 发布后观测

发布后通过 Harness Journal 统计以下指标：

- 每日 Turn 平均和 P95 Step 数；
- 30+ Step Turn 数量；
- RepeatCallGuard 拦截次数及工具分布；
- `NoProgressError` 次数；
- 单 Turn 最大 Provider 请求数；
- `manage_task(wait)` 的 changed/timeout/terminal 分布。

预期结果：

- 不再出现由后台状态轮询导致的 60～100 Step 循环；
- `manage_task(status)` 不再出现第三次开始持续拦截；
- “执行成功但没有输出”的等待命令显著下降；
- 真正停滞的 Turn 在 6 个无进展 Step 后明确失败；
- 正常执行和长任务成功率不下降。
