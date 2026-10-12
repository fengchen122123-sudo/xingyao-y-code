# 电脑操控面板主动关闭 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 GUI 右侧「操控」页签在 run 进入终态后 60 秒自动关闭，并提供页签 × 手动关闭入口，同时保证关闭后旧 run 的迟到事件/快照不能复活页签。

**Architecture:** 纯前端改动。Store 层新增「已关闭 run 黑名单」与终态 getter，作为关闭语义的唯一事实来源；`RightPanel.vue` 承担页签渲染、手动关闭与自动关闭计时；`ComputerControlPane.vue` 只做一处按钮可见性修正。

**Tech Stack:** Vue 3 + Pinia + TypeScript + Vitest（`@vue/test-utils`）。

**设计文档：** `docs/superpowers/specs/2026-09-10-computer-pane-dismiss-design.md`

## Global Constraints

- 只改前端，**不得改动后端**（Bridge API、事件契约、快照语义、预览服务）。
- 不改顶部安全控制条 `COMPUTER_BAR_STATUSES` 的现有规则。
- 运行中（starting/running/paused/waiting_for_user/provider_safety_required/stopping/failed 未终态）**不提供任何关闭入口**。
- 已有前端测试必须继续通过，尤其是 `RightPanel.test.ts` 中通过 `text().trim() === '操控'` 定位页签的既有用例 —— 关闭按钮**不能渲染任何文本节点**（用 `AppIcon` 图标）。
- 测试命令一律在 `frontend/` 目录下执行。
- 前端测试 mock 方式沿用仓库惯例：`(window as any).pywebview = { api: {...} }` + `setActivePinia(createPinia())`。

---

### Task 1: Store 层关闭语义

**Files:**
- Modify: `frontend/src/stores/appStore.ts`
- Test: `frontend/src/stores/appStore.computerRunDismiss.test.ts`（新建）

**Interfaces:**
- Consumes: 现有 `emptyComputerRun()`、`reduceComputerEvent`、`_applyComputerSnapshot`
- Produces:
  - state `_computerClosedRunId: string`
  - getter `computerRunTerminal: boolean`
  - action `clearComputerRun(): void`

- [ ] **Step 1: 写失败测试**

新建 `frontend/src/stores/appStore.computerRunDismiss.test.ts`：

```ts
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => {
  setActivePinia(createPinia());
});

it('clearComputerRun 清空运行态与轨迹并记录关闭黑名单', () => {
  const store = useAppStore();
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running' });
  store.reduceComputerEvent({
    type: 'computer_action_started', run_id: 'run-1', event_seq: 2, action_ordinal: 1, action_kind: 'click',
  });
  expect(store.computerTrace).toHaveLength(1);

  store.clearComputerRun();

  expect(store.computerRun.run_id).toBe('');
  expect(store.computerRun.status).toBe('idle');
  expect(store.computerTrace).toEqual([]);
  expect(store._computerClosedRunId).toBe('run-1');
});

it('已关闭 run 的迟到事件不再复活运行态', () => {
  const store = useAppStore();
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 5, status: 'stopped' });
  store.clearComputerRun();

  store.reduceComputerEvent({ type: 'computer_observation', run_id: 'run-1', event_seq: 6, preview_url: '/late' });
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 7, status: 'running' });

  expect(store.computerRun.run_id).toBe('');
  expect(store.computerRun.preview_url).toBe('');
  expect(store.computerRun.status).toBe('idle');
});

it('已关闭 run 的快照不复活运行态，新 run 快照正常生效', () => {
  const store = useAppStore();
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'completed' });
  store.clearComputerRun();

  store._applyComputerSnapshot({ run_id: 'run-1', status: 'completed' });
  expect(store.computerRun.run_id).toBe('');

  store._applyComputerSnapshot({ run_id: 'run-2', status: 'running' });
  expect(store.computerRun.run_id).toBe('run-2');
  expect(store.computerRun.status).toBe('running');
});

it('关闭后新 run 事件正常建 run，轨迹从空开始', () => {
  const store = useAppStore();
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running' });
  store.reduceComputerEvent({
    type: 'computer_action_started', run_id: 'run-1', event_seq: 2, action_ordinal: 1, action_kind: 'click',
  });
  store.clearComputerRun();

  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-2', event_seq: 1, status: 'starting' });
  expect(store.computerRun.run_id).toBe('run-2');
  expect(store.computerTrace).toEqual([]);
});

it('computerRunTerminal 只对 completed / stopped / failed 返回 true', () => {
  const store = useAppStore();
  const statuses: Array<[string, boolean]> = [
    ['idle', false],
    ['starting', false],
    ['running', false],
    ['paused', false],
    ['waiting_for_user', false],
    ['provider_safety_required', false],
    ['stopping', false],
    ['completed', true],
    ['stopped', true],
    ['failed', true],
  ];
  for (const [status, expected] of statuses) {
    store.computerRun = { ...store.computerRun, run_id: 'run-1', status };
    expect(store.computerRunTerminal).toBe(expected);
  }
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend; npx vitest run src/stores/appStore.computerRunDismiss.test.ts`
Expected: FAIL，报 `store.clearComputerRun is not a function` / `store.computerRunTerminal is undefined`

- [ ] **Step 3: 实现 Store 改动**

`frontend/src/stores/appStore.ts`：

**(a) 常量**：紧跟 `COMPUTER_BAR_STATUSES` 定义之后新增

```ts
// run 终态（2026-09-10 设计）：页签关闭入口与自动关闭计时的共同判据
const COMPUTER_TERMINAL_STATUSES = ['completed', 'stopped', 'failed'];
```

**(b) state**：在 `_computerActionStartMs: {} as Record<number, number>,` 之后新增

```ts
    // 已关闭 run 黑名单（不参与渲染）：清空运行态后拒绝该 run 的迟到事件与快照，
    // 防止「操控」页签被旧事件复活
    _computerClosedRunId: '',
```

**(c) getter**：在 `computerControlBarVisible` getter 之后新增

```ts
    // run 是否处于终态：驱动页签关闭入口与终态自动关闭计时
    computerRunTerminal(state): boolean {
      return COMPUTER_TERMINAL_STATUSES.includes(state.computerRun.status);
    },
```

**(d) 事件门闩**：`reduceComputerEvent` 中

```ts
      const runId = String(raw.run_id || '');
      const seq = Number(raw.event_seq) || 0;
      if (!runId) return;
```

改为

```ts
      const runId = String(raw.run_id || '');
      const seq = Number(raw.event_seq) || 0;
      if (!runId) return;
      // 已关闭 run 的迟到事件一律丢弃：清空后 Store 无 run，若无此门闩
      // reducer 的「首个事件直接认领」分支会把旧事件当成新 run
      if (runId === this._computerClosedRunId) return;
```

**(e) 快照门闩**：`_applyComputerSnapshot` 中

```ts
      const runId = String(raw.run_id || '');
      if (!runId) return;
```

改为

```ts
      const runId = String(raw.run_id || '');
      if (!runId) return;
      // 已关闭 run 的快照不得复活运行态（新 run_id 不受影响）
      if (runId === this._computerClosedRunId) return;
```

**(f) 新增 action**：放在 `_applyComputerSnapshot` 之前

```ts
    // 关闭「操控」面板（用户点 × 或终态自动关闭共用）：
    // 先把当前 run_id 记入黑名单，再清空运行态与内存轨迹
    clearComputerRun() {
      const runId = this.computerRun.run_id;
      if (!runId) return;
      this._computerClosedRunId = runId;
      this.computerRun = emptyComputerRun();
      this.computerTrace = [];
      this._computerActionStartMs = {};
      this._computerEventSeq = 0;
    },
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend; npx vitest run src/stores/appStore.computerRunDismiss.test.ts`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/stores/appStore.ts frontend/src/stores/appStore.computerRunDismiss.test.ts
git commit -m "feat(gui): 电脑操控运行态支持主动关闭并拒绝旧 run 复活"
```

---

### Task 2: 页签关闭入口与终态自动关闭

**Files:**
- Modify: `frontend/src/components/RightPanel.vue`
- Test: `frontend/src/components/RightPanel.test.ts`（追加用例）

**Interfaces:**
- Consumes: Task 1 的 `store.clearComputerRun()`、`store.computerRunTerminal`
- Produces: 无对外接口（组件内部行为）

- [ ] **Step 1: 写失败测试**

在 `frontend/src/components/RightPanel.test.ts` **文件末尾**追加（复用文件中已有的 `tabLabels` / `computerTab` helper）：

```ts
// ── 操控页签关闭（2026-09-10 设计） ──

function goFilesTab(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('.rtab').find(t => t.text().trim() === '文件')!;
}

it('终态 60 秒且用户不在操控页签时自动关闭页签', async () => {
  vi.useFakeTimers();
  const store = useAppStore();
  const wrapper = mount(RightPanel);
  // run 开始：自动切到操控页签
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running' });
  await nextTick();
  // 用户切到「文件」页签
  await goFilesTab(wrapper).trigger('click');
  // run 进入终态
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 2, status: 'completed' });
  await nextTick();
  expect(computerTab(wrapper)).toBeTruthy();

  vi.advanceTimersByTime(59999);
  await nextTick();
  expect(computerTab(wrapper)).toBeTruthy();

  vi.advanceTimersByTime(1);
  await nextTick();
  expect(computerTab(wrapper)).toBeUndefined();
  expect(tabLabels(wrapper)).toEqual(['计划', '任务', '终端', '文件']);

  wrapper.unmount();
  vi.useRealTimers();
});

it('用户停留在操控页签时到点顺延，不被自动关闭', async () => {
  vi.useFakeTimers();
  const store = useAppStore();
  const wrapper = mount(RightPanel);
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running' });
  await nextTick();
  expect(computerTab(wrapper).classes()).toContain('active');
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 2, status: 'stopped' });
  await nextTick();

  vi.advanceTimersByTime(60000);
  await nextTick();
  expect(computerTab(wrapper)).toBeTruthy();
  vi.advanceTimersByTime(60000);
  await nextTick();
  expect(computerTab(wrapper)).toBeTruthy();

  wrapper.unmount();
  vi.useRealTimers();
});

it('鼠标悬停在右面板内时到点顺延，移开后下一个周期关闭', async () => {
  vi.useFakeTimers();
  const store = useAppStore();
  const wrapper = mount(RightPanel);
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running' });
  await nextTick();
  await goFilesTab(wrapper).trigger('click');
  await wrapper.find('#rightbar').trigger('mouseenter');
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 2, status: 'failed' });
  await nextTick();

  vi.advanceTimersByTime(60000);
  await nextTick();
  expect(computerTab(wrapper)).toBeTruthy();

  await wrapper.find('#rightbar').trigger('mouseleave');
  vi.advanceTimersByTime(60000);
  await nextTick();
  expect(computerTab(wrapper)).toBeUndefined();

  wrapper.unmount();
  vi.useRealTimers();
});

it('点击页签 × 立即关闭操控面板并切回计划页签', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel);
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running' });
  await nextTick();
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 2, status: 'completed' });
  await nextTick();

  const closeEl = wrapper.find('[data-test="computer-tab-close"]');
  expect(closeEl.exists()).toBe(true);
  await closeEl.trigger('click');
  await nextTick();

  expect(computerTab(wrapper)).toBeUndefined();
  expect(wrapper.findAll('.rtab')[0].classes()).toContain('active');
  expect(store.computerRun.run_id).toBe('');
  wrapper.unmount();
});

it('运行中不渲染关闭入口，关闭后新 run 重新开页签', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel);
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running' });
  await nextTick();
  expect(wrapper.find('[data-test="computer-tab-close"]').exists()).toBe(false);

  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 2, status: 'stopped' });
  await nextTick();
  await wrapper.find('[data-test="computer-tab-close"]').trigger('click');
  await nextTick();
  expect(computerTab(wrapper)).toBeUndefined();

  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-2', event_seq: 1, status: 'starting' });
  await nextTick();
  expect(computerTab(wrapper)).toBeTruthy();
  expect(computerTab(wrapper).classes()).toContain('active');
  wrapper.unmount();
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend; npx vitest run src/components/RightPanel.test.ts`
Expected: FAIL，找不到 `[data-test="computer-tab-close"]`，且终态后页签不会消失

- [ ] **Step 3: 实现 RightPanel.vue 改动**

**(a) 模板**：页签循环体改为（新增关闭元素）

```html
      <button
        v-for="tab in tabs"
        :key="tab.id"
        class="rtab"
        :class="{ active: activeTab === tab.id }"
        @click="activeTab = tab.id"
      >
        {{ tab.label }}
        <span v-if="tab.id === 'plan' && store.planData.items.length > 0" class="badge-dot"></span>
        <span
          v-if="tab.closable"
          class="rtab-close"
          data-test="computer-tab-close"
          title="关闭操控面板"
          aria-label="关闭操控面板"
          @click.stop="dismissComputerPane()"
        >
          <AppIcon name="x" :size="11" :stroke-width="2.5" />
        </span>
      </button>
```

> 注意：关闭元素内**不得出现文本节点**，否则既有用例中 `text().trim() === '操控'` 的页签定位会失效。

**(b) 脚本 — tabs 计算属性**

```ts
const tabs = computed<Array<{ id: TabType; label: string; closable?: boolean }>>(() => (
  store.computerRun.run_id
    ? [...baseTabs, { id: 'computer', label: '操控', closable: store.computerRunTerminal }]
    : baseTabs
));
```

> `baseTabs` 的类型标注同步改为 `Array<{ id: TabType; label: string }>`（保持原样即可，联合类型兼容）。

**(c) 脚本 — 关闭与自动关闭**：紧跟 `autoOpenComputerTabOnce` / `watch(() => store.computerRun.run_id, autoOpenComputerTabOnce);` 之后新增

```ts
// ── 操控页签关闭（2026-09-10 设计）──
// 手动关闭与终态自动关闭共用同一出口：清计时器 → 清空运行态 → 页签回退
const COMPUTER_AUTO_DISMISS_MS = 60000;
let computerDismissTimer: ReturnType<typeof setTimeout> | null = null;
const computerPanelHovered = ref(false);

function clearComputerDismissTimer() {
  if (computerDismissTimer !== null) {
    clearTimeout(computerDismissTimer);
    computerDismissTimer = null;
  }
}

function dismissComputerPane() {
  clearComputerDismissTimer();
  if (!store.computerRun.run_id) return;
  store.clearComputerRun();
  // 面板折叠时 activeTab 可能仍停在 computer，页签消失后必须回退
  if (activeTab.value === 'computer') activeTab.value = 'plan';
}

// 终态起算 60 秒：到点若用户仍在看（停在操控页签且未折叠，或鼠标悬停在面板内）
// 则顺延一个周期，否则关闭。「在看就不关」是唯一规则
function scheduleComputerAutoDismiss() {
  clearComputerDismissTimer();
  if (!store.computerRun.run_id || !store.computerRunTerminal) return;
  computerDismissTimer = setTimeout(() => {
    computerDismissTimer = null;
    const stillWatching = (activeTab.value === 'computer' && !store.rightPanelCollapsed)
      || computerPanelHovered.value;
    if (stillWatching) {
      scheduleComputerAutoDismiss();
      return;
    }
    dismissComputerPane();
  }, COMPUTER_AUTO_DISMISS_MS);
}

// run_id / 状态变化都重排计时：新 run 会先清掉旧计时器
watch(
  () => [store.computerRun.run_id, store.computerRun.status],
  () => scheduleComputerAutoDismiss(),
  { immediate: true },
);
```

**(d) 模板根元素**：`#rightbar` 上加悬停侦测

```html
  <div
    id="rightbar"
    data-theme-region="right_panel"
    :class="{ collapsed: store.rightPanelCollapsed }"
    :style="panelStyle"
    @mouseenter="computerPanelHovered = true"
    @mouseleave="computerPanelHovered = false"
  >
```

**(e) 卸载清理**：`onUnmounted` 中追加

```ts
  clearComputerDismissTimer();
```

**(f) 样式**：`<style scoped>` 中新增（与 `.rtab` 视觉风格协调）

```css
.rtab-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  margin-left: 5px;
  border-radius: 4px;
  color: var(--text-faint);
  cursor: pointer;
  transition: 0.15s;
}
.rtab-close:hover {
  background: var(--bg3);
  color: var(--text);
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend; npx vitest run src/components/RightPanel.test.ts`
Expected: PASS（既有 5 个用例 + 新增 5 个用例全部通过）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/RightPanel.vue frontend/src/components/RightPanel.test.ts
git commit -m "feat(gui): 操控页签支持 × 关闭与终态 60 秒自动关闭"
```

---

### Task 3: 终态隐藏「立即停止」

**Files:**
- Modify: `frontend/src/components/ComputerControlPane.vue`
- Test: `frontend/src/components/ComputerControlPane.test.ts`（追加用例）

**Interfaces:**
- Consumes: Task 1 的 `store.computerRunTerminal`
- Produces: 无

- [ ] **Step 1: 写失败测试**

在 `frontend/src/components/ComputerControlPane.test.ts` **文件末尾**追加（复用文件内已有的 `mountPane`）：

```ts
// ── 终态按钮可见性（2026-09-10 设计） ──

it('终态不渲染「立即停止」按钮', () => {
  const store = useAppStore();
  store.computerRun = { ...store.computerRun, run_id: 'run-1', status: 'completed' };
  const wrapper = mountPane();
  expect(wrapper.find('[data-test="computer-stop"]').exists()).toBe(false);
  wrapper.unmount();
});

it('活动期仍渲染「立即停止」按钮', () => {
  const store = useAppStore();
  store.computerRun = { ...store.computerRun, run_id: 'run-1', status: 'running' };
  const wrapper = mountPane();
  expect(wrapper.find('[data-test="computer-stop"]').exists()).toBe(true);
  wrapper.unmount();
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend; npx vitest run src/components/ComputerControlPane.test.ts`
Expected: 第一个用例 FAIL（终态下按钮仍存在）

- [ ] **Step 3: 实现**

`ComputerControlPane.vue` 中「立即停止」按钮的 `v-if`：

```html
        <button
          v-if="store.computerRun.run_id && !store.computerRunTerminal"
```

其余不变（`data-test` / `title` / `@click` / 图标全部保留）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend; npx vitest run src/components/ComputerControlPane.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/ComputerControlPane.vue frontend/src/components/ComputerControlPane.test.ts
git commit -m "fix(gui): 操控终态不再渲染无效的立即停止按钮"
```

---

### Task 4: 全量验证

**Files:** 无改动

- [ ] **Step 1: 前端全量测试**

Run: `cd frontend; npm test`
Expected: 全部测试文件通过，无新增失败

- [ ] **Step 2: 类型检查**

Run: `cd frontend; npx vue-tsc -b`
Expected: 退出码 0，无类型错误

- [ ] **Step 3: 人工核对验收标准（对照 spec 第「验收标准」节）**

1. 终态 60 秒后页签与内容自动消失（用户不在操控页签、未悬停时）
2. 停在操控页签或悬停面板时不自动关闭
3. 终态点 × 立即关闭且不残留空页签
4. 关闭后迟到事件/快照不复活；新 run 自动重开页签
5. 运行中无关闭入口
6. 测试与类型检查均通过

---

### Task 5: 代码评审

**Files:** 只读，无改动

- [ ] **Step 1: 对照 spec 审查 diff**

Run: `git diff HEAD~3 -- frontend/src`
检查项：
- Store 门闩是否同时覆盖事件与快照两条路径
- `dismissComputerPane` 是否两处（手动 / 自动）共用
- 关闭元素是否确实没有文本节点（不破坏既有页签定位）
- 定时器是否在 `onUnmounted` 清理
- 是否引入任何后端改动
