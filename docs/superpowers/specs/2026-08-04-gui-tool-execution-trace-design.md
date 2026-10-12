# Y-code GUI 紧凑工具执行时间线设计

## 1. 背景与目标

当前 GUI 在 `MessageBubble.vue` 中为每个工具调用渲染一张独立卡片。多工具任务会连续出现大量卡片，读取、搜索、命令、修改与失败之间缺少统一的执行顺序；工具失败后即使模型继续执行，用户也很难从界面上确认任务是否仍然连贯。

本设计将消息气泡内的工具卡片替换为一块紧凑的“执行过程”时间线，并与 [CLI CodeBuddy 工具进度设计](./2026-08-04-codebuddy-tool-progress-design.md) 使用同一份结构化工具事件契约。

目标：

- 每条助手消息只显示一块执行时间线；
- 连续读取和搜索聚合，减少重复卡片；
- 命令、修改、子代理和失败操作单独留痕；
- 运行中动作与耗时实时可见；
- 单个工具失败后继续展示后续工具与最终正文；
- 工具结果、错误输出和 Diff 可以原位展开；
- 实时消息与历史会话使用相同组件，不产生跨消息结果串联；
- CLI 与 GUI 对工具类别、动作、成功状态和失败原因保持一致。

## 2. 非目标

- 不重做右侧后台任务面板、内嵌终端或计划面板；
- 不改变模型的工具选择、权限确认、重试或失败处理策略；
- 不从任意工具文本中猜测测试数量、匹配数量或成功状态；
- 不在 GUI 中新增第二套任务调度器；
- 不把模型私有思考合并进执行时间线；
- 不重构与本功能无关的侧栏、空状态、安装或卸载逻辑。

## 3. 已选视觉方案

采用方案 A“紧凑执行时间线”。时间线位于思考块下方、最终 Markdown 正文上方。

```text
星瑶 · K3

┌ 执行过程                         6 项 · 进行中 ┐
│ ✓ 已读取 8 个文件                         1.6s │
│   core/llm.py 等 · 1,284 行                    │
│ ✓ 已完成 3 次代码搜索                    0.9s │
│   tool_finished · MessageBubble · bridge event │
│ ! 运行测试失败                           3.2s │
│   ModuleNotFoundError: pytest · 展开详情        │
│ ● 正在使用项目测试环境                   8.4s │
│   venv\Scripts\python -m pytest -q             │
└───────────────────────────────────────────────┘

最终正文……
```

视觉规则：

- 紫色动态圆点表示运行中；
- 绿色对勾表示成功；
- 红色感叹号表示工具失败；
- 灰色停止标记表示取消；
- 第一行展示动作和耗时，第二行只展示有诊断价值的目标、命令或错误摘要；
- 时间线使用当前 GUI 的中性色、紫色强调色和现有圆角体系，不引入新的主题变量；
- 整块时间线可以通过标题折叠，默认保持展开；
- 单行详情和完整结果使用独立的展开控制，不能通过点击整张消息误触。

## 4. 信息结构与交互

### 4.1 标题状态

标题只使用回合级终态，不由单个工具失败决定：

- 有活动工具：`执行过程 · N 项 · 进行中`；
- `turn_finish`：`执行过程 · N 项 · 已完成`；
- `turn_cancelled`：`执行过程 · N 项 · 已取消`；
- `turn_error`：`执行过程 · N 项 · 异常结束`。

运行中无法预知工具总数，`N 项`表示当前已经登记的原始工具调用数，不显示虚假的完成百分比。

### 4.2 聚合规则

- 相邻且成功的 `read_file` 合并为一行；
- 相邻且成功的 `search_code`、`glob_files` 合并为一行；
- `execute_command`、`write_file`、`edit_file`、`list_directory`、`fetch_url`、`run_subagent`、权限结果和未知工具独立显示；
- 技能 `SKILL.md` 读取独立显示为“使用技能”，不并入源码读取；
- 任一失败先结束前一个成功分组，再单独显示；
- 命令、修改、失败、新思考段和最终正文都是聚合边界；
- 并行读取或搜索运行时合并为一条活动行，完成顺序按模型原始工具调用顺序稳定输出；
- 未知类别不参与聚合，不解析输出推断类别。

聚合后的读取行可展开查看文件列表；搜索行可展开查看搜索动作与搜索词。聚合只改变展示，不改变数据库消息、工具执行顺序或模型可见历史。

### 4.3 命令与失败

- 命令行第二行始终显示经过控制字符清理、密钥脱敏和字符上限截断的原始命令；
- 成功命令点击后展开完整结果预览；
- 失败行默认显示稳定错误摘要和第一条关键原因；
- 完整错误结果继续保留在折叠区，沿用事件结果长度上限；
- 单个工具失败不会把标题改为“异常结束”，也不会把 `store.running` 置为 `false`；
- 下一个 `tool_start` 直接追加到同一时间线，渲染层不自动重试、不替换命令、不选择降级工具；
- 用户拒绝权限作为失败工具结果显示，不能标成成功。

### 4.4 文件修改与 Diff

新产生的 `diff_ready` 事件必须携带 `tool_call_id`。前端按工具 ID 将 Diff 绑定到对应的 `write_file` 或 `edit_file` 行：

```text
✓ 已修改 core/turn_renderer.py · +28 / -14 行
  查看 Diff ▸
```

点击后在当前行下方展开现有 Diff 视图，不再为新事件渲染独立 Diff 卡片。

旧历史 Diff 没有 `tool_call_id` 时，不根据文件名或相邻位置猜测归属；统一放入时间线末尾的“历史文件变更”行，展开后按原顺序显示。这样既不丢数据，也不把 Diff 错绑到某次修改。

### 4.5 展开、键盘和动画

- 时间线标题、结果详情和 Diff 展开按钮使用原生 `button`；
- 支持 Enter 和 Space，维护 `aria-expanded` 与可见焦点；
- 活动状态使用 `aria-live="polite"`，只播报动作变化，不每秒播报耗时；
- 运行中秒数每秒刷新，工具完成后固定为后端 `duration_ms`；
- 系统启用 `prefers-reduced-motion: reduce` 时停止旋转动画，保留静态紫色圆点；
- 完整结果最高 240px，超出后内部滚动；
- 命令、路径和错误摘要使用单行省略，悬停标题显示同一份已脱敏文本。

## 5. 共享事件契约

CLI 实施计划 [2026-08-04-codebuddy-tool-progress.md](../plans/2026-08-04-codebuddy-tool-progress.md) 中的 `build_tool_display()` 与 `duration_ms` 是本设计的共享前置契约。

### 5.1 `tool_start`

`gui_bridge.py` 将 `tool_requested` 与 `tool_started` 按 ID 去重后发送：

```json
{
  "type": "tool_start",
  "data": {
    "id": "cmd-1",
    "name": "execute_command",
    "label": "星瑶正在执行命令：pytest -q",
    "display": {
      "category": "command",
      "action": "运行命令",
      "target": "pytest -q",
      "command": "pytest -q",
      "metrics": {},
      "error_summary": "",
      "is_skill": false
    }
  }
}
```

`display` 中所有字符串由共享阶段的 `core/tool_presenter.py` 统一完成控制字符清理、密钥脱敏和长度限制；CLI 与 GUI 只消费展示安全字段。完整原始参数不通过新字段额外发送到前端。这样 GUI 开发线不依赖尚未完成的 CLI 渲染模块，可以在共享契约稳定后真正并行。

### 5.2 `tool_finish`

```json
{
  "type": "tool_finish",
  "data": {
    "id": "cmd-1",
    "name": "execute_command",
    "ok": false,
    "summary": "pytest -q — 命令执行失败",
    "result": "命令执行失败（退出码 1）……",
    "duration_ms": 3200,
    "display": {
      "category": "command",
      "action": "运行命令",
      "target": "pytest -q",
      "command": "pytest -q",
      "metrics": {},
      "error_summary": "ModuleNotFoundError: pytest",
      "is_skill": false
    }
  }
}
```

`display` 和 `duration_ms` 为向后兼容的新增字段，现有 `id`、`name`、`label`、`summary`、`ok`、`result` 保留。

### 5.3 `diff_ready`

```json
{
  "type": "diff_ready",
  "data": {
    "tool_call_id": "edit-1",
    "file_path": "core/turn_renderer.py",
    "diff_text": "--- a/core/turn_renderer.py\n+++ b/core/turn_renderer.py\n……"
  }
}
```

`core/llm.py` 在当前工具调用的事件回调上下文中补充 `tool_call_id`；GUI 不使用“最近一次修改工具”等时序猜测。

## 6. 前端数据模型

`ChatMessage` 增加消息级状态，替代组件从全局 `store.toolResults` 查询实时结果：

```ts
interface ToolDisplay {
  category: 'read' | 'search' | 'command' | 'mutation' | 'skill' | 'other';
  action: string;
  target: string;
  command: string;
  metrics: Record<string, number>;
  error_summary: string;
  is_skill: boolean;
}

interface ToolExecutionResult {
  ok: boolean;
  summary: string;
  result: string;
  duration_ms?: number;
  display?: ToolDisplay;
}

interface ToolExecutionCall {
  id: string;
  name: string;
  label: string;
  display?: ToolDisplay;
  result?: ToolExecutionResult;
  diff?: { file_path: string; diff_text: string };
  subagent_tasks?: Array<{
    id: string;
    description: string;
    status: string;
    stage: string;
    stage_detail?: string;
    elapsed: number;
  }>;
}

interface ChatMessage {
  // 现有字段省略
  tool_calls?: ToolExecutionCall[];
  legacy_diffs?: Array<{ file_path: string; diff_text: string }>;
  execution_state?: 'running' | 'completed' | 'cancelled' | 'error';
}
```

状态更新规则：

1. `turn_start` 新建当前 assistant 消息并设为 `running`；
2. `tool_start` 按 ID 新增或更新该消息的 `tool_calls`；
3. `tool_finish` 将结果写回同一个调用对象；
4. `diff_ready` 按 `tool_call_id` 写入对应调用；
5. `subagent_progress` 写入本轮当前的 `run_subagent` 调用，保留现有子代理阶段与耗时展示；`run_subagent` 当前为串行同步工具，因此同一时刻只有一个调用接收实时快照，完成时把最后快照固化在该调用上；
6. `turn_finish`、`turn_cancelled`、`turn_error` 只更新当前消息终态；
7. 切换会话时从历史消息重建消息级调用关系，不复用上一会话全局结果表。

为了兼容历史会话，旧的 assistant `tool_calls` 与后续 tool 消息在会话加载时按明确 `tool_call_id` 配对。旧数据缺少 `display` 时只使用显式工具名映射；缺少 `duration_ms` 时不显示耗时。旧 Diff 进入 `legacy_diffs`。

## 7. 组件边界

### `ToolExecutionTrace.vue`

- 接收一条 assistant 消息的 `tool_calls`、`legacy_diffs` 与 `execution_state`；
- 渲染标题、活动状态、时间线行和整块折叠；
- 不访问全局消息列表，不直接处理桥接事件；
- 不解析任意结果文本推断成功状态。

### `ToolExecutionRow.vue`

- 渲染单行状态、图标、耗时、第二行详情和结果展开；
- 命令、失败、技能、普通工具使用同一组件的显式变体；
- 修改行承载 Diff 展开；
- `run_subagent` 展开后继续显示现有子任务阶段、描述、阶段详情和耗时；
- 只维护本行的展开状态。

### `frontend/src/utils/toolTrace.ts`

- 定义显式工具名到类别的历史兼容映射；
- 将相邻已完成调用转换为稳定的 `ToolTraceRow[]`；
- 聚合读取行数等结构化指标；
- 计算 unified diff 的 `+N / -N`，仅解析标准 diff 行前缀；
- 不操作 Pinia、不访问 DOM、无定时器，便于单元测试。

### `MessageBubble.vue`

- 保留用户消息、头像、思考块和 Markdown 正文；
- 用一个 `ToolExecutionTrace` 替换当前逐工具卡片与独立 Diff 列表；
- 保留等待首个真实事件前的“星瑶正在干活”提示；首个工具事件到达后由时间线接管状态展示。

### `appStore.ts`

- 只负责把桥接事件归入当前 assistant 消息；
- 不负责聚合展示；
- 删除新链路对全局 `toolResults` 的依赖；迁移完成后移除该全局字段。

## 8. 错误与一致性边界

- 工具级 `ok=false`：显示失败行，回合继续；
- `turn_error`：冻结所有活动行，标记“异常结束”，显示桥接层错误提示；
- `turn_cancelled`：冻结活动行，标记“已取消”；
- `tool_finish` 找不到同 ID 的开始事件：创建一条独立完成行并在开发日志记录契约错误，不能静默丢结果；
- 重复 `tool_start`：按 ID 更新同一调用，不新增重复行；
- 重复 `tool_finish`：相同 ID 的最后事件覆盖结果，但开发日志记录重复完成；
- 缺失 ID：显示“未知工具事件”契约错误行，不把它与其他工具聚合；
- 失败结果进入模型消息历史的控制流保持现状，GUI 渲染不得影响是否继续调用模型；
- 活动计时器随组件卸载和终态立即清理，防止切换会话后继续刷新旧消息。

## 9. 安全与性能

- 命令、目标和错误摘要清理 ANSI、回车和不可见控制字符，并按显示长度截断；
- API Key、Authorization、Bearer Token、密码字段和常见 `sk-` 密钥必须脱敏；
- 完整结果沿用后端 `TOOL_RESULT_EVENT_MAX_CHARS` 上限，并通过 Vue 文本插值渲染，禁止 `v-html`；
- Diff 通过文本节点逐行渲染，不注入 HTML；
- 每条活动消息只维护一个一秒计时器，不为每个工具创建定时器；
- 聚合转换由 `computed` 完成，输入只限当前消息的工具调用；
- 时间线展开结果最大高度固定，避免长输出撑大整个聊天页面。

## 10. 测试设计

### 后端 pytest

- `tool_start` 转发结构化 `display`；
- `tool_finish` 转发 `display` 和 `duration_ms`；
- `diff_ready` 带准确 `tool_call_id`；
- `tool_requested` 与 `tool_started` 按 ID 去重；
- 工具失败后仍能收到后续 `tool_start` 和最终 `turn_finish`；
- 命令和错误摘要不会泄漏测试密钥。

### 前端自动测试

前端新增 Vitest 和 Vue Test Utils 作为开发依赖，并增加 `npm test`：

- `toolTrace.ts`：相邻读取聚合、搜索聚合、类别边界、失败边界、并行稳定顺序、旧数据无耗时、Diff 计数；
- `ToolExecutionTrace.vue`：运行/完成/取消/异常标题、失败后下一项仍运行、整块折叠；
- `ToolExecutionRow.vue`：命令详情、错误详情、Diff 展开、键盘与 `aria-expanded`；
- `run_subagent`：运行中展开后显示子任务阶段，完成后保留最终子任务快照；
- `MessageBubble.vue`：时间线位于思考和正文之间，首个工具事件后隐藏空等待提示。

### 联动验收

固定端到端事件序列：

```text
思考 → 连续读取 3 个文件 → 搜索 2 次 → 命令失败
→ 模型读取失败结果 → 修正命令成功 → 修改文件并产生 Diff
→ 测试成功 → 一次最终正文 → turn_finish
```

CLI 验收：无“思考完成”刷屏、读取/搜索聚合、失败可见、后续工具和正文继续。

GUI 验收：只有一块执行时间线、活动耗时更新、失败原因可见、Diff 原位展开、最终正文只出现一次。

还需执行：

- 完整 pytest；
- 前端 Vitest；
- `npm run build`；
- Python `py_compile`；
- 源码 TUI 的真实 K3 多工具烟测；
- GUI 开发服务器的暗色/亮色、窄窗口和键盘操作人工检查。

## 11. 并行开发边界

共享事件契约必须先完成并通过测试，之后 CLI 与 GUI 可以并行开发：

```text
共享契约：core/tool_presenter.py + core/llm.py
                         │
              ┌──────────┴──────────┐
              │                     │
CLI：core/tool_progress.py       GUI：gui_bridge.py
    core/turn_renderer.py            appStore.ts
    main.py                           ToolExecutionTrace.vue
                                      ToolExecutionRow.vue
                                      toolTrace.ts
              │                     │
              └──────────┬──────────┘
                         │
                   联动测试与烟测
```

并行任务不得同时编辑共享契约文件。共享契约合入并稳定后，CLI 与 GUI 任务只编辑各自文件；最终联调任务负责解决契约偏差，不通过静默兼容或文本猜测掩盖错误。

当前工作区已有 `Sidebar.vue`、`appStore.ts`、`gui_bridge.py` 等未提交修改。实施时必须保留用户现有修改，只在相关代码块做小范围补丁；提交时按任务逐文件检查和暂存。

## 12. 预期文件范围

共享与 CLI 范围沿用现有 CLI 实施计划。GUI 新增或修改：

- 修改 `gui_bridge.py`；
- 修改 `frontend/package.json` 与锁文件；
- 修改 `frontend/src/types/pywebview.d.ts`；
- 修改 `frontend/src/stores/appStore.ts`；
- 修改 `frontend/src/components/MessageBubble.vue`；
- 新增 `frontend/src/components/ToolExecutionTrace.vue`；
- 新增 `frontend/src/components/ToolExecutionRow.vue`；
- 新增 `frontend/src/utils/toolTrace.ts`；
- 新增对应 Vitest 测试与测试配置；
- 修改 README 中 GUI 工具执行反馈说明。

不修改右侧后台任务、内嵌终端、计划面板、侧栏和安装流程的行为。

## 13. 验收标准

1. GUI 每条助手消息最多出现一块工具执行时间线。
2. 相邻读取和搜索按 B+C 规则稳定聚合。
3. 命令、修改、技能、子代理和失败操作单独显示。
4. 活动行实时显示动作、诊断性详情和耗时。
5. 普通工具失败可见，失败后的工具与最终正文继续出现。
6. 新 Diff 精确绑定到对应工具 ID，旧 Diff 不做错误猜测。
7. 工具结果绑定消息，不跨回合或会话串联。
8. 老历史缺少新字段时正常显示，并省略未知耗时。
9. CLI 与 GUI 对同一事件序列给出一致的类别、动作和终态。
10. 命令与错误输出无控制字符和已知敏感信息泄漏。
11. 前端自动测试、构建、后端测试和编译检查全部通过。
12. 未覆盖、暂存或提交工作区中与本功能无关的用户改动。
