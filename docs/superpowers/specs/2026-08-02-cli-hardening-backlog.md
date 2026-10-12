# CLI Hardening Backlog Snapshot

## 保存原因

2026-08-02，用户要求暂停并关闭原“逐项修复全部 CLI 审查问题”的长任务，优先交付当前可运行版本。本文件保存尚未执行的范围，后续可从隔离分支 `codex/cli-hardening` 恢复，不代表这些问题已经修复。

权威完整设计仍为 `docs/superpowers/specs/2026-08-02-cli-hardening-design.md`；权限阶段实施计划为 `docs/superpowers/plans/2026-08-02-cli-permission-network-hardening.md`。

## 已完成

- Task 1：结构化会话命令授权，提交 `ccb3210`。
- Task 2：区分可信 Skill 读取与受保护写入，提交 `f49df0e`。
- Task 3：计划验证命令授权，已完成代码与独立复审；最终提交以分支 HEAD 为准。

## 暂停、尚未修复

### 权限与网络阶段

- Task 4：MCP 管理操作与动态工具权限、配置全量脱敏、锁内原子写入和并发更新。
- Task 5：默认严格 SSRF、DNS 失败拒绝、私网目标精确 allowlist、逐次 HTTP 重定向复验。

### 数据安全与恢复

- 旧数据库迁移的可恢复性、失败可见性和回滚。
- Undo、checkpoint 与消息持久化的数据完整性。
- 崩溃/取消路径中的事务提交和状态一致性。

### Session、Turn 与队列归属

- 会话切换时消息、权限和持久化归属。
- 队列任务与子代理的 parent session / parent turn 绑定。
- 并发或取消情况下的跨轮次污染与迟到事件。

### Headless 与 CLI 入口

- 非交互模式退出码、标准输出/错误输出契约。
- 工具失败、取消、空响应和持久化失败不得表现为成功。
- CLI 参数解析与入口 smoke tests。

### 取消与资源生命周期

- 并行工具取消传播、后台进程清理、子代理终止与资源回收。
- 记忆压缩的并发一致性和失败行为。
- 精确编辑的确定性，移除模糊或启发式补救路径。

## 恢复条件

恢复时从本文件的“暂停、尚未修复”开始，重新检查分支 HEAD 和工作树；每项继续遵循先 RED、再最小修复、任务级审查、独立提交和全量验证。不得把本次可运行交付视为完整安全加固完成。

## 当前可运行快照验证

- Task 3 门禁：`19 passed`。
- Python 编译检查：`main.py`、`core/task_scheduler.py`、`core/tools.py` 通过。
- CLI 启动 smoke test：`.\.venv\Scripts\python.exe main.py --help` 返回退出码 0，并显示 `-p/--prompt`、`--session`、`-h/--help`。
