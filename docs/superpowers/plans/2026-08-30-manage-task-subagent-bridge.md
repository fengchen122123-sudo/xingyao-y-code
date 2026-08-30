# manage_task 与子代理任务注册表桥接修复方案

日期：2026-08-30
状态：已实施（全量回归 1761 passed / 2 skipped）
来源：会话 92「你检查一下我未提交的代码」后台任务连续报错的事后诊断
（DB 证据见 `~/.ycode/ycode_gui.db` messages 8034–8045、harness_events 时间线 10:57:57–10:58:13）。

## 1. 背景与根因

模型启动 4 个后台审查子代理后，按 `core/llm.py:432-434` 白名单注释的指引调用
`manage_task(list/status/wait)` 轮询进度，连续撞上两类错误：

| 报错 | 根因 |
| --- | --- |
| `等待任务必须提供 after_revision 参数（非负整数）` | `core/tools.py` wait 校验硬性必填，但 JSON schema 非必填（`core/tool_registry.py` 仅 `minimum: 0`），且两个后台启动入口（`run_subagent` 后台返回、`execute_command` 后台启动文本）都不含初始 revision——模型手里从来没有「上次快照」，首次等待必然缺参。协议自身矛盾，不是模型失误。 |
| `未找到任务 subagent-3b7a73f7` | 两套互不相通的任务注册表：`manage_task` 只查 `core/task_manager.ACTIVE_TASKS`（shell 后台命令名册）；子代理注册在 `core/tools.py:3521` 模块级单例 `subagent_manager`（`SubagentManager._tasks`，纯内存）。对 `subagent-*` ID 永远返回「未找到」，与任务是否真实运行无关。`list` 同理只显示 shell 任务，令模型误判任务已丢失。 |

连带损失：后台子代理结果的正规回流通道（`drain_terminal_background_tasks` → 下一回合开始时
注入「📦 后台子代理任务结果汇总」）只在回合结束后生效；会话 92 中模型始终未结束回合，
4 个审查任务的成果最终没有进入对话。模型在回合内没有任何合法手段收割后台子代理结果。

已确认 `core/llm.py:432-434` 注释明确承诺「run_subagent(run_in_background=True) 启动子代理后
需要 manage_task(list/status) 轮询进度」——实现必须兑现该承诺，同时把回流机制写进工具描述。

## 2. 修复目标

1. `manage_task` 成为后台任务统一管理面：shell 后台命令与 `subagent-*` 子代理任务
   均可 list / status / wait / kill。
2. 消除首次等待必报错：shell `wait` 缺省 `after_revision` 按 0（首次等待）处理。
3. 协议文案与实现一致：注释与工具描述写清两类任务、两种收结果路径
   （回合内 manage_task 主动查、回合后自动汇总回流）。
4. 回合内可收割子代理终态结果（result 摘要），弥补「模型不结束回合则回流永不触发」的缺口。

非目标：不改 harness 阶段、不改 Prompt Ledger、不改后台 shell 任务的 revision 语义、
不改自动汇总回流机制本身。

## 3. 实施步骤

### P0-1 `SubagentManager` 新增查询/等待方法（`core/tools.py`）

在 `SubagentManager` 类内新增（全部经既有 `self._lock`，不新增锁）：

- `task_snapshot(task_id) -> SubagentTaskSnapshot | None`：不存在返回 None。
  （实施修正：类内已存在无参 `snapshot() -> list[SubagentTaskSnapshot]`（全量列表，gui_bridge
  与大量测试在用），为避免覆盖，按 ID 查单个任务的方法命名为 `task_snapshot`；
  list 合并直接复用既有 `snapshot()`，不再新增 `snapshots()`。）
- `wait_terminal(task_id, timeout_seconds, cancel_event=None) -> SubagentTaskSnapshot | None`：
  等待任务进入终态（`status in _SUBAGENT_TERMINAL_STATUSES` 且 `done_event` 已设置，
  done_event 是终态唯一提交屏障，`_complete`/`_terminate` 均最后设置）或超时；
  任务不存在返回 None；等待期间任务被容量清理则返回最后一次快照（被清理者必为终态）；
  `cancel_event` 置位抛 `KeyboardInterrupt`，与 `task_manager.wait_task_change` 语义一致。
  实现采用「锁内取快照 + 锁外 done_event.wait(≤0.5s)」轮询，与 shell 侧条件等待同构。

### P0-2 `manage_task` 路由桥接 + 缺省 after_revision（`core/tools.py:1545`）

- 路由谓词：既有 `is_subagent_task_id()`（正则 `^subagent-[0-9a-f]{8}$`，tools.py:2198）。
  `task_id` 命中即走子代理分支；`list` 无条件合并两类任务。
- `list`：shell（running-only，维持现状）+ 子代理（非终态）合并进 `{"tasks": [...]}`，
  每条新增 `"kind": "shell" | "subagent"`；两者皆空保持旧文案
  `ℹ 当前没有任何运行中的后台异步任务。`。
- `status`（子代理）：`subagent_manager.snapshot(task_id)`；None → 旧文案
  `🚨 未找到任务 {task_id}。`；否则 JSON 载荷（见下）。
- `wait`（子代理）：
  - 不要求 `after_revision`（子代理无 revision，传入则忽略）；
  - `timeout_seconds` 沿用 1–60 归一化；
  - `wait_terminal` 返回 None → `🚨 未找到任务 {task_id}。`；
  - 载荷 `"changed"` 语义 = 是否等到终态（超时未终态为 false，不是错误，与 shell 一致）。
- `kill`（子代理）：沿用现有 `check_permission_and_ask` 用户确认，批准后调
  `subagent_manager.cancel(task_id, reason="user requested via manage_task")`
  （协作式取消）；成功文案注明「已请求取消（协作式）」。
- shell `wait` 缺省 `after_revision`：按 0 处理（revision 本从 0 起，即首次等待语义）；
  类型错误 / 负数仍报错（保留既有文案）。同步修正 JSON schema 描述。

子代理快照 JSON 载荷（`_subagent_snapshot_payload`）：

```json
{
  "kind": "subagent",
  "task_id": "subagent-xxxxxxxx",
  "status": "running|completed|failed|timed_out|cancelled",
  "subagent_type": "review",
  "run_in_background": true,
  "stage": "running",
  "execution_elapsed": 12.3,
  "terminal": false,
  "description": "（截断 120 字符）",
  "result": "仅终态且非空时附，截断 1500 字符（对齐 agent_service._BG_RESULT_MAX_CHARS）",
  "error": "仅非空时附",
  "changed": "仅 wait 附"
}
```

shell 载荷追加 `"kind": "shell"`，其余字段不变（revision/exit_code/output 原样）。

### P1 协议与注释纠偏

- `core/llm.py:432-434`：白名单注释改为真实行为——manage_task 已可管理两类后台任务；
  子代理结果优先等回合后自动回流（省 token），manage_task 用于回合内主动查进度/等终态。
- `core/tool_registry.py` `manage_task` 描述：声明同时管理 shell 后台命令与子代理任务；
  `wait` 对子代理为「等待终态」，缺省 `after_revision` 视为 0。
- `core/tool_registry.py` `run_subagent` 描述：补充后台模式两条收结果路径——
  回合结束后的下一回合自动注入「📦 后台子代理任务结果汇总」；
  回合内可用 `manage_task(status/wait, task_id="subagent-…")` 主动查询或等待终态。

### P2 测试（`tests/test_tools.py`）

既有模式复用：`isolated_task_manager` 夹具、`_py_cmd`、`_wait_until`、
`SubagentManager(runner=...)` 假 runner、monkeypatch 模块单例 `tools_module.subagent_manager`。

更新：

- `test_manage_task_wait_validates_arguments`：删除「缺 after_revision 报错」断言，
  改为缺省触发首次等待语义；保留 task_id 缺失 / 负数 / 类型错误 / 未知 ID 报错断言。

新增：

1. `test_manage_task_wait_shell_defaults_after_revision_to_zero`：后台任务延迟输出，
   不传 `after_revision` 等 wait → 阻塞至输出出现，`changed=True` 且 `revision>0`。
2. `test_manage_task_status_and_wait_route_subagent`：假 runner（Event 阻塞）后台子代理，
   status 返回 `kind=subagent` 且 running；释放后 wait 返回 terminal、`changed=True`、
   result 含结论（≤1500 字符）。
3. `test_manage_task_wait_subagent_ignores_after_revision_and_unknown`：
   子代理 wait 不传 `after_revision` 合法；未知 `subagent-00000000` → 「未找到」。
4. `test_manage_task_list_merges_shell_and_subagent`：shell running 任务 + 子代理 running
   任务同时出现在 `tasks`，kind 字段正确。
5. `test_manage_task_kill_subagent_uses_cooperative_cancel`：批准路径下子代理任务进入
   cancelled 终态；拒绝路径不触碰任务（复用 1631 行 fake_check 模式）。

### P2 回归与验证

1. `pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q`
   ——已确认真实注册表工具描述无固定哈希（仅确定性/顺序/受控注册表断言），
   描述变更不应碰黄金文件；跑通即为证。
2. `pytest tests/test_tools.py tests/test_task_manager.py tests/test_tool_registry.py -q`
3. `pytest tests -q` 全量收尾。
4. 手工冒烟（可选）：GUI 会话内 `run_in_background=true` 启动子代理 →
   `manage_task list` 能看到 `kind=subagent` 条目。

### 清理

删除诊断临时脚本 `~/.ycode/diag_query.py`、`~/.ycode/diag_check_head.py`。

## 4. 风险与边界

- `list` 载荷新增 `kind` 字段：消费方是模型与测试断言（键级断言，非整包相等），兼容。
- 子代理 `wait` 阻塞上限 60s（schema maximum），不改变回合步长语义；
  `cancel_event` 透传保证 GUI stop / CLI Ctrl+C 可打断。
- 终态 result 摘要截 1500 字符：与自动汇总回流上限一致，避免大段结果重复入上下文；
  完整结果仍走回流通道或 status 复查。
- 子代理任务被容量清理（TTL 600s / max_records=256）后 status/wait 返回「未找到」：
  与 shell 终态淘汰语义一致，文案不特判。
- 不触碰 `RepeatCallGuard` 豁免清单：manage_task 本就不在豁免清单，wait 阻塞期间
  无重复同签名成功调用问题（每次快照不同）。
