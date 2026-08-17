# 多会话并行执行 — 线程本地 Workdir 上下文设计

日期：2026-08-17
状态：已批准

## 目标

移除后端 `_turn_workdir_lock` 的整回合串行化，让同一项目内多个会话、以及不同项目间的会话都能真正并行执行，且各会话的文件操作严格落在各自的工作目录内，互不干扰。

## 根因

`os.chdir()` 与 `Path.cwd()` 是进程级全局状态，多线程无法隔离。当前后端为避免不同会话的回合互相抢占进程 cwd（导致相对路径文件操作落错目录），用 `_turn_workdir_lock` 把完整回合串行化。

依赖链（已逐一审计）：

| 位置 | 依赖 |
|------|------|
| `gui_bridge.py` `_run_turn_worker` | `os.chdir(turn_workdir)` + `memory.set_workdir()` |
| `core/tools.py` `_resolve_workspace_path` | `Path.cwd()` 作为沙盒边界 |
| `core/tools.py` `is_outside_workspace` | `Path.cwd()` |
| `core/tools.py` `execute_command` | `Popen` 不传 cwd，子进程继承进程 cwd |
| `core/agent_service.py` | `working_dir=os.getcwd()` 写入系统提示词 |
| `core/permission_store.py` | `os.getcwd()` 作权限记录路径基准 |

## 方案

引入线程本地 workdir 上下文，替代所有进程级 cwd 依赖。

### 新增 `core/workspace_context.py`

```python
_workspace_tls = threading.local()

def set_workdir(path): _workspace_tls.workdir = _normalize(path)
def get_workdir(): return getattr(_workspace_tls, "workdir", None) or Path.cwd().resolve()
@contextlib.contextmanager
def workspace_scope(path): ...
```

未设置时兜底返回 `Path.cwd()`，保证 headless / 单线程 / 测试场景行为不变。

### 改造点

1. **回合线程绑定 workdir**：`gui_bridge.py` 的 `_run_turn_worker` 用 `workspace_scope(turn_workdir)` 替代 `os.chdir` + `memory.set_workdir`。
2. **移除串行锁**：删除 `_turn_workdir_lock` 串行化（`_run_turn_worker_serialized` 改为直接调 `_run_turn_worker`）。
3. **工具函数**：`_resolve_workspace_path` / `is_outside_workspace` / `execute_command`（Popen cwd）改用 `get_workdir()`。
4. **提示词与权限**：`agent_service.py` / `permission_store.py` 改用 `get_workdir()`。
5. **子代理继承**：`SubagentTask` 增加 `workdir` 字段；`run_subagent` 创建任务时读取父线程 workdir；`_run_task` 线程启动时 `set_workdir(task.workdir)`。

## 边界（已与用户确认）

- 同一项目多个会话并行：各自独立 workdir，互不干扰。
- 不同项目各自会话并行：各绑定各项目目录。
- 同一文件被两个会话同时写：后写覆盖先写（文件系统本质，非 bug）。

## 风险控制

- 每个任务独立测试，改一处验证一处。
- `get_workdir()` 兜底 `Path.cwd()`，未显式设置的路径行为与改造前完全一致。
- 最终全量跑前端与后端测试 + 构建产物。
