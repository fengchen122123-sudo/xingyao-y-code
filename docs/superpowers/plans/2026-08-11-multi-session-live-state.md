# 多会话实时状态隔离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 允许一个会话执行任务时在其他会话发送消息，并保证每个会话的流式输出、工具状态和终态结果都不丢失。

**Architecture:** 后端继续按 `session_name` 并行执行；前端新增按会话索引的实时 runtime 缓存。桥接事件不再因为会话不在当前视图而丢弃，而是路由到对应 runtime；当前视图只展示当前会话 runtime。会话切换优先展示仍在运行的实时缓存，空闲会话再使用数据库历史校准。

**Tech Stack:** Vue 3、Pinia、TypeScript、Vitest、Python、pytest、pywebview bridge。

## Global Constraints

- 不修改模型选择、视觉路由和图片传输协议。
- 不覆盖当前工作区已有未提交改动。
- 每个回合级事件必须使用明确的 `session_name`，不能猜测当前会话。
- 当前会话的发送/停止状态只由当前会话 runtime 决定，不能使用全局运行布尔值代替。
- 先写能复现问题的失败测试，再写生产代码。

---

### Task 1: 建立会话级 runtime 和失败测试

**Files:**
- Create: `frontend/src/stores/sessionRuntime.ts`
- Modify: `frontend/src/stores/appStore.ts`
- Test: `frontend/src/stores/appStore.workspace.test.ts`
- Test: `frontend/src/stores/appStore.toolEvents.test.ts`

**Interfaces:**
- `SessionRuntimeState` 保存 `messages`、`streamingText`、`thinkingText`、`running` 和 `subagentTasks`。
- `createSessionRuntime()` 返回空的、可独立变更的 runtime。
- `appStore` 通过 `sessionRuntimeByName` 保存所有会话 runtime，并提供当前会话运行状态查询。

- [ ] **Step 1: 写新建会话不继承旧运行状态的失败测试**

测试流程：先让 `session-a` 收到 `turn_start`，再执行 `createGlobalSession()`，断言新会话不是运行状态且发送接口可以被调用。

- [ ] **Step 2: 写后台会话事件仍被保存的失败测试**

测试流程：当前打开 `session-b`，让 `session-a` 依次收到 `turn_start`、`token_delta`、`tool_start`；切回 `session-a` 前断言不会抛契约错误，切回后能看到 assistant 内容和工具调用。

- [ ] **Step 3: 运行新增 Vitest，确认因当前全局状态实现失败**

运行：`npm.cmd test -- --run src/stores/appStore.workspace.test.ts src/stores/appStore.toolEvents.test.ts`

预期：新增用例失败，原因是当前实现依赖全局 `running/messages`，并过滤非当前会话事件。

- [ ] **Step 4: 实现 `SessionRuntimeState` 和 runtime 创建函数**

```ts
export interface SessionRuntimeState {
  messages: ChatMessage[];
  streamingText: string;
  thinkingText: string;
  running: boolean;
  subagentTasks: SubagentTaskSnapshot[];
}

export function createSessionRuntime(): SessionRuntimeState {
  return {
    messages: [], streamingText: '', thinkingText: '',
    running: false, subagentTasks: [],
  };
}
```

- [ ] **Step 5: 将当前会话的消息和运行状态改为 runtime 投影**

新增 `sessionRuntimeByName`，实现 `getOrCreateSessionRuntime(sessionName)`、`activateSessionRuntime(sessionName)` 和 `currentSessionRunning`。所有会话级数据通过会话名访问；当前视图的 `messages` 指向当前 runtime 的数组。

- [ ] **Step 6: 运行新增 Vitest，确认 Task 1 通过**

运行：`npm.cmd test -- --run src/stores/appStore.workspace.test.ts src/stores/appStore.toolEvents.test.ts`

预期：新增会话状态和后台缓存用例通过，原有工具事件用例保持通过。

---

### Task 2: 重构事件路由，保留非当前会话实时输出

**Files:**
- Modify: `frontend/src/stores/appStore.ts`
- Modify: `frontend/src/stores/appStore.toolEvents.test.ts`
- Modify: `frontend/src/components/MessageBubble.vue`
- Test: `frontend/src/components/MessageBubble.test.ts`

**Interfaces:**
- `handleBridgeEvent(event)` 按 `data.session_name` 找到目标 runtime。
- `turn_start`、`round_started`、`thinking_delta`、`token_delta`、`tool_start`、`tool_finish`、`diff_ready`、`subagent_progress` 和终态事件都只修改目标 runtime。

- [ ] **Step 1: 写跨会话流式正文和工具事件失败测试**

覆盖以下序列：A 启动并输出一段正文，切到 B；A 继续输出正文并启动工具；B 输出自己的正文；再切回 A，断言 A/B 内容和工具调用完全独立。

- [ ] **Step 2: 写后台完成后无需二次切换的失败测试**

A 在后台收到 `turn_finish` 后，切回 A 的视图应直接显示已完成 assistant，不依赖再次调用 `switch_session`。

- [ ] **Step 3: 运行测试确认失败**

运行：`npm.cmd test -- --run src/stores/appStore.toolEvents.test.ts src/components/MessageBubble.test.ts`

- [ ] **Step 4: 将事件处理从当前数组改为目标 runtime**

删除非当前会话中间事件的直接 `return`。所有正文和思考增量写入目标 runtime 的独立缓冲；工具事件使用目标 runtime 的最后一条 assistant，不再调用当前会话的全局 `activeAssistantOrThrow()`。

- [ ] **Step 5: 处理会话切换和历史响应竞态**

切换到运行中的会话时，如果 runtime 已存在，先展示 runtime；后端历史只用于没有实时 runtime 的会话，或者作为已完成会话的终态校准。任何过期 `switch_session` 响应都不能覆盖正在运行的 runtime。

- [ ] **Step 6: 处理终态和重命名**

`turn_finish/turn_cancelled/turn_error` 只更新对应 runtime；`session_renamed` 同步迁移 runtime key、运行集合和当前会话引用。

- [ ] **Step 7: 修正等待指示器和消息气泡的数据来源**

`MessageBubble.vue` 与 `App.vue` 的工作中状态改用当前会话 runtime，避免 A 运行时 B 显示“正在干活”，也避免切回 A 后因为全局状态变化丢失等待状态。

- [ ] **Step 8: 运行前端聚焦测试，确认通过**

运行：`npm.cmd test -- --run src/stores/appStore.toolEvents.test.ts src/components/MessageBubble.test.ts src/stores/appStore.workspace.test.ts`

---

### Task 3: 修复新会话发送和停止会话的 UI 门禁

**Files:**
- Modify: `frontend/src/components/InputArea.vue`
- Modify: `frontend/src/components/KimiEmptyState.vue`
- Modify: `frontend/src/stores/appStore.ts`
- Test: `frontend/src/components/InputArea.test.ts`
- Test: `frontend/src/components/KimiEmptyState.test.ts`

**Interfaces:**
- 输入区使用 `store.currentSessionRunning` 决定发送还是停止。
- `stopCurrentTurn()` 调用 `stop_current_turn(this.currentSessionName)`。

- [ ] **Step 1: 写新会话可发送、旧会话仍可停止的失败测试**

挂起 A 会话后创建 B，断言 B 的发送按钮没有 disabled；切回 A，断言按钮显示停止状态；点击停止时 API 收到 A 的会话名。

- [ ] **Step 2: 运行 UI 测试确认失败**

运行：`npm.cmd test -- --run src/components/InputArea.test.ts src/components/KimiEmptyState.test.ts`

- [ ] **Step 3: 替换组件中的全局 `store.running` 判断**

发送按钮、输入框发送逻辑和空状态发送逻辑统一使用 `currentSessionRunning`；当前会话未运行时允许发送，不受其他会话影响。

- [ ] **Step 4: 传递停止目标会话名并处理失败回滚**

停止请求使用当前会话名；只有目标会话终态到达后才清理该 runtime 的运行状态。

- [ ] **Step 5: 运行 UI 聚焦测试确认通过**

运行：`npm.cmd test -- --run src/components/InputArea.test.ts src/components/KimiEmptyState.test.ts`

---

### Task 4: 补强后端事件归属契约

**Files:**
- Modify: `gui_bridge.py`
- Modify: `frontend/src/types/pywebview.d.ts`
- Test: `tests/test_gui_tool_events.py`

**Interfaces:**
- `permission_request`、`permission_dismiss`、`background_subagent_terminal` 等事件必须携带 `session_name`。
- 前端事件类型为 `session_name: string`，缺失时按契约错误处理。

- [ ] **Step 1: 写后端事件归属失败测试**

模拟两个会话同时运行，断言工具事件、权限事件、后台子代理终态事件均包含正确会话名，且 A 的终态不会结束 B。

- [ ] **Step 2: 运行后端聚焦测试确认失败**

运行：`python -m pytest tests/test_gui_tool_events.py -q`

- [ ] **Step 3: 从 permission request owner/parent turn 推导并携带 session_name**

优先使用请求关联的 `parent_turn_id` 映射到活动会话；不使用可变的全局 `_current_session` 猜测归属。

- [ ] **Step 4: 给停止和后台事件补齐显式会话参数**

所有由回合线程发出的事件使用该线程的 `session_name`；后台回调保存触发任务的会话名，不从当前界面会话推导。

- [ ] **Step 5: 运行后端测试确认通过**

运行：`python -m pytest tests/test_gui_tool_events.py -q`

---

### Task 5: 全量回归和构建

**Files:**
- Test: `frontend/src/stores/appStore.toolEvents.test.ts`
- Test: `frontend/src/stores/appStore.workspace.test.ts`
- Test: `tests/test_gui_tool_events.py`

- [ ] **Step 1: 运行全部前端测试**

运行：`npm.cmd test -- --run`

- [ ] **Step 2: 运行前端生产构建**

运行：`npm.cmd run build`

- [ ] **Step 3: 编译后端并运行相关 Python 测试**

运行：`python -m py_compile gui_bridge.py`，再运行 GUI 事件和相关回归测试。

- [ ] **Step 4: 检查差异和未触碰模型配置**

运行：`git diff --check`，确认没有修改主模型、副模型或视觉路由配置，并保留工作区原有未提交文件。
