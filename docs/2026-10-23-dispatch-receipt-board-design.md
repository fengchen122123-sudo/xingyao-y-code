# 派活回执看板 设计

日期：2026-10-23　状态：设计稿，未实现

## 一、要解决的问题

星瑶目前有三条派活路径，但都没有可回看的回执：

| 路径 | 发起 | 结果回流 | 缺口 |
|---|---|---|---|
| 秘书转交 | 秘书会话调 `open_session` / 转交工具 | 目标会话回复 | 没有「谁派的、派了什么、成了没」 |
| 子代理（前台） | `run_subagent` | 返回结果给主 Agent | 结果只进当前上下文，会话结束即丢 |
| 子代理（后台） | `run_subagent(run_in_background=true)` | 下一回合注入 + `manage_task` | 注入是一次性的，没有历史看板 |

用户的原话是「上周派出去的三件事，哪件成了、哪件卡了」——现在回答不了。

## 二、任务状态模型

```
dispatched → running → completed
     │          ├──→ failed
     │          ├──→ cancelled
     │          └──→ timed_out
     └──────────────→ rejected（派发即被拒）
```

- `dispatched`：已登记，执行体尚未启动
- `running`：子代理或目标会话已在跑
- `timed_out`：超时，但可能仍有部分产物（必须保留产物路径）
- `cancelled`：用户主动取消，与 `failed` 分开统计
- 终态不可回退，重试生成新行并带 `retry_of` 指向原回执

## 三、回执字段

| 字段 | 类型 | 说明 |
|---|---|---|
| receipt_id | TEXT PK | 回执唯一标识 |
| parent_session | TEXT | 派活方会话 |
| parent_turn | TEXT | 派活方回合 |
| child_kind | TEXT | `subagent` / `session` |
| child_ref | TEXT | 子代理 task_id 或目标会话名 |
| task_brief | TEXT | 派了什么，截断到 500 字 |
| status | TEXT | 上节状态枚举 |
| created_at / started_at / finished_at | INTEGER | 毫秒时间戳 |
| result_summary | TEXT | 结果摘要，截断到 1000 字 |
| artifact_paths | TEXT | JSON 数组，产物文件路径 |
| error_code | TEXT | 失败错误码，成功为空 |
| retry_of | TEXT | 重试来源回执 |

## 四、落库位置

新建表，不复用 `messages`：

```sql
CREATE TABLE IF NOT EXISTS dispatch_receipts (
  receipt_id     TEXT PRIMARY KEY,
  parent_session TEXT NOT NULL,
  parent_turn    TEXT NOT NULL,
  child_kind     TEXT NOT NULL,
  child_ref      TEXT NOT NULL,
  task_brief     TEXT NOT NULL DEFAULT '',
  status         TEXT NOT NULL,
  created_at     INTEGER NOT NULL,
  started_at     INTEGER,
  finished_at    INTEGER,
  result_summary TEXT NOT NULL DEFAULT '',
  artifact_paths TEXT NOT NULL DEFAULT '[]',
  error_code     TEXT NOT NULL DEFAULT '',
  retry_of       TEXT
);
CREATE INDEX IF NOT EXISTS idx_receipts_parent ON dispatch_receipts(parent_session, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_receipts_status ON dispatch_receipts(status);
```

不复用 `messages` 的理由：回执是结构化状态，需要按状态聚合与超时扫描；消息表存的是对话文本，按状态查询要全表扫且语义混杂。也不复用 `harness_turns`：那是回合执行细节，粒度和生命周期都不同。

## 五、看板读取入口

1. **GUI 面板**：右侧面板新增「派活」页签，按状态分组，默认显示最近 7 天，可切换 30 天。
2. **Agent 工具**：`dispatch_board`（list / get / cancel / retry 四个动作），走既有 tool_registry 注册，权限与 `manage_task` 同级。
3. **会话内摘要**：每回合准备阶段，若有 `failed` / `timed_out` 且未读的回执，注入一行摘要（最多 3 条），不阻塞回合。

## 六、失败与取消处理

- **重试**：`retry` 生成新回执并写 `retry_of`，原回执状态不变；同一 `child_ref` 同时最多一个 `running`。
- **幂等**：回执按 `(parent_turn, child_ref, created_at)` 去重，重复登记直接返回已有行。
- **孤儿回收**：应用启动时扫描 `running` 且 `started_at` 超过 2 小时、且 `manage_task` 里查不到对应任务的回执，置为 `timed_out` 并写明原因。
- **取消**：调用方取消时同步写 `cancelled`；执行体后续若仍回报结果，只追加到 `result_summary`，不改状态。
- **产物丢失**：`artifact_paths` 里的文件不存在时，看板显示「产物已删除」，不报错。

## 七、与 session_manage / manage_task 的边界

| 工具 | 管什么 | 与回执的关系 |
|---|---|---|
| `session_manage` | 会话本身的增删改查、归档、迁移 | 不感知派活 |
| `manage_task` | 当前进程内的后台任务：列表、等待、终止 | 实时操作；回执是它的历史账本 |
| `dispatch_board` | 历史回执的查询与重试 | 只读 + 重试，不直接操作进程内任务 |

一句话：`manage_task` 管「现在」，`dispatch_board` 管「过去」。

## 八、非目标

- 不做跨机器的派活跟踪；
- 不做任务的自动重新派发（只提供手动 retry）；
- 不替代计划系统（计划项是依赖图，回执是派活的流水账）；
- 不对回执做全文检索（`task_brief` 只是摘要，不是对话内容）。
