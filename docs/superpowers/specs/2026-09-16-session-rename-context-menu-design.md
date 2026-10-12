# 会话重命名（右键菜单）设计

日期：2026-09-16
状态：已确认交互方式，待实施

## 背景

侧栏「对话」列表目前只支持删除会话（hover 出现 `✕`，再点「确认」）。用户希望能重命名对话。

现状盘点（实施前已核实）：

- 后端 `gui_bridge.rename_session(old_name, new_name)` 已完整实现：回合运行中拒绝、空名拒绝、撞名自动加后缀（`core/memory.py:rename_session`）、权限授权与排队消息随会话迁移、返回 `{ok, name, sessions}`。
- 前端契约 `frontend/src/types/pywebview.d.ts` 已声明 `rename_session`。
- **缺口只有两处**：前端没有任何调用入口；`gui_bridge.rename_session` 成功后不推送 `session_renamed` 事件（自动重命名路径 `_rename_session_to_title` 会推）。

## 目标交互

1. 右键会话项 → 弹出上下文菜单，两项：**重命名** / **删除**。
2. 点「重命名」→ 该条目就地变为输入框，预填当前显示名且全选。
   - `Enter` 保存，`Esc` 取消，失焦保存。
   - 值未变化或为空 → 不发请求，直接退出编辑态。
3. 点「删除」→ 复用现有二次确认流程（进入 `confirming` 态，仍需点红色「确认」）。
4. 菜单关闭时机：选中任一项、点击菜单外任意位置、`Esc`、窗口滚动/尺寸变化。
5. 保存成功后侧栏列表即时刷新；失败（如回合运行中）弹 `notifyModal` 提示，不改动列表。

## 实现

### 后端 `gui_bridge.py`

`rename_session` 成功分支补发事件（与 `_rename_session_to_title` 的负载保持一致，含 `old`/`new` 与兼容字段 `old_name`/`new_name`）：

```python
self.emit_event("session_renamed", {
    "old": old_name, "new": final_name,
    "old_name": old_name, "new_name": final_name,
    "sessions": self.get_sessions(),
})
```

理由：前端 `appStore.handleBridgeEvent('session_renamed')` 已实现完整迁移——`currentSessionName`/`currentSessionDisplayName`、`runningSessions`、`queuedSessions`、`sessionStatusDots`、`permRequest.sessionName`、`sessionRuntimeByName` 以及 `sessionGroups` 内条目。手动与自动两条重命名路径共用同一段同步逻辑，避免在前端再写一份。

### 前端 `appStore.ts`

新增 action：

```ts
async renameSession(oldName: string, newName: string)
```

- `trim` 后为空或与原名相同 → 静默返回，不请求。
- 调 `api.rename_session(oldName, target)`；成功则用 `res.sessions` 覆盖 `sessionGroups`（事件丢失时兜底刷新，撞名加后缀后的最终名也由此同步），静默完成。
- 失败 → `this.notifyModal('会话管理', '重命名失败: ' + msg)`；`catch` 中置 `connected = false`（与 `deleteSingleSession` 同风格）。

### 前端 `Sidebar.vue`

新增状态：`ctxMenu`（`show/x/y/name/display`）、`editingName`、`editingValue`。

- 会话项加 `@contextmenu.prevent.stop="openCtxMenu($event, s)"`。
- 菜单为 `position: fixed` 浮层，`data-test="session-ctx-menu"`，跟随鼠标坐标，边界处向视口内收拢。
- 编辑态下条目渲染 `<input data-test="session-rename-input">`，挂载后 `focus` + `select`；`@click.stop` 防止触发 `onSessionClick` 的切换逻辑。
- 菜单与编辑态互斥：进入编辑态即关闭菜单；`onSessionClick` 时清理两者。

## 边界

| 场景 | 行为 |
|---|---|
| 回合运行中 | 后端拒绝，前端弹提示「该会话正在运行回合，请先停止或等待完成后再重命名」 |
| 新名撞已存在的会话 | `memory.rename_session` 自动加 `_1` 后缀，前端用返回列表展示最终名 |
| 新名为空/纯空白 | 前端不发请求；后端亦拒绝 |
| 重命名的是当前会话 | 事件把 `currentSessionName`/`currentSessionDisplayName` 迁移到新名 |
| 重命名后立刻切会话 | 无额外处理，列表与当前会话名已同步 |

## 测试

- 前端 `frontend/src/components/Sidebar.rename.test.ts`（vitest + @vue/test-utils，mock `window.pywebview.api`）：
  1. 右键会话项弹出菜单，含「重命名 / 删除」两项；
  2. 点「重命名」进入输入框且预填显示名；
  3. 改名 + `Enter` → 以 `(old, new)` 调 `rename_session`，并用返回 `sessions` 刷新列表；
  4. `Esc` 取消 → 不调 API；
  5. 空值 / 未改动 → 不调 API；
  6. 后端返回 `ok: false` → 弹提示且列表不变；
  7. 点「删除」→ 进入二次确认态，仍不调 `delete_session`。
- 后端 `tests/test_gui_workspace_groups.py` 增补：重命名成功后推送 `session_renamed`，负载含 `old`/`new` 与最新会话列表；回合运行中被拒时不推事件。

## 不做（YAGNI）

- 拖拽重排、批量重命名、双击改名的第二入口。
- 自动化任务 `target_session` 的跟随更新：任务绑定的是会话名，手动重命名后任务仍指向旧名（自动重命名路径同样存在该问题）。本次不改，作为独立问题另议。
