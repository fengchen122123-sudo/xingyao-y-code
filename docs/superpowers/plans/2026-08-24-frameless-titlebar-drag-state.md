# Frameless Titlebar Drag State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复无边框窗口双击还原后不能立即拖动的问题，并消除同一前端拖动仲裁中的 mouseup 与旧 timer 竞态。

**Architecture:** 保留现有 `Topbar.vue → pywebview.api.window_begin_drag()` 与 `window_native_maximize()` 链路，只在组件内统一管理一轮“待拖动”的 timer、mousemove、mouseup 和 armed 状态。所有取消与执行路径复用同一个幂等清理函数，Python/Win32 层不变。

**Tech Stack:** Vue 3、TypeScript、Vitest、Vue Test Utils、fake timers

**Spec:** `docs/superpowers/specs/2026-08-24-frameless-titlebar-drag-state-design.md`

## Global Constraints

- 不修改 Python、Win32 或 pywebview 桥实现。
- 400ms 双击判定窗口和 4px 位移阈值保持不变。
- 不抽取新 composable，不修改打包产物或版本号。
- 只提交设计/计划文档、`frontend/src/components/Topbar.vue` 和 `frontend/src/components/Topbar.test.ts`。
- 必须先观察新增回归测试在旧实现上失败，再修改实现。

## File Structure

- `frontend/src/components/Topbar.vue`：负责标题栏交互、待拖动生命周期及桥调用；本次只调整该组件内的拖动仲裁。
- `frontend/src/components/Topbar.test.ts`：负责标题栏按钮、双击与拖动行为的组件级回归测试；本次新增 detail=3、mouseup 和旧 timer 场景。
- `docs/superpowers/specs/2026-08-24-frameless-titlebar-drag-state-design.md`：已批准的行为规格，不再修改。
- `docs/superpowers/plans/2026-08-24-frameless-titlebar-drag-state.md`：本实施计划。

---

### Task 1: 补全标题栏待拖动生命周期

**Files:**
- Modify: `frontend/src/components/Topbar.test.ts:90-160`
- Modify: `frontend/src/components/Topbar.vue:73-135`

**Interfaces:**
- Consumes: `window.pywebview.api.window_begin_drag(): Promise<{ ok: boolean; error?: string }>`、`MouseEvent.detail`、全局 `mousemove`/`mouseup` 事件。
- Produces: 组件内部 `cancelPendingDrag(): void` 与 `enterNativeMoveLoop(): void` 的完整幂等生命周期；不新增对外接口。

- [x] **Step 1: 写入三个失败回归测试**

在 `Topbar frameless window controls` describe 中、现有拖动测试后加入：

```ts
  it('allows an immediate drag after dblclick when mousedown detail is 3', async () => {
    const wrapper = mount(Topbar, { global: { stubs: { AppIcon: true } } });
    const beginDrag = (window as any).pywebview.api.window_begin_drag;

    wrapper.get('.titlebar-drag').element.dispatchEvent(new MouseEvent('mousedown', {
      bubbles: true, button: 0, detail: 3, clientX: 10, clientY: 10,
    }));
    window.dispatchEvent(new MouseEvent('mousemove', {
      bubbles: true, buttons: 1, clientX: 20, clientY: 10,
    }));

    expect(beginDrag).toHaveBeenCalledOnce();
  });

  it('cancels a pending drag when the mouse button is released', async () => {
    vi.useFakeTimers();
    try {
      const wrapper = mount(Topbar, { global: { stubs: { AppIcon: true } } });
      const beginDrag = (window as any).pywebview.api.window_begin_drag;

      wrapper.get('.titlebar-drag').element.dispatchEvent(new MouseEvent('mousedown', {
        bubbles: true, button: 0, detail: 1, clientX: 10, clientY: 10,
      }));
      window.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, button: 0 }));
      await vi.advanceTimersByTimeAsync(450);

      expect(beginDrag).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('clears an early-drag timer before the next drag attempt starts', async () => {
    vi.useFakeTimers();
    try {
      const wrapper = mount(Topbar, { global: { stubs: { AppIcon: true } } });
      const beginDrag = (window as any).pywebview.api.window_begin_drag;

      wrapper.get('.titlebar-drag').element.dispatchEvent(new MouseEvent('mousedown', {
        bubbles: true, button: 0, detail: 1, clientX: 0, clientY: 0,
      }));
      window.dispatchEvent(new MouseEvent('mousemove', {
        bubbles: true, buttons: 1, clientX: 10, clientY: 0,
      }));
      expect(beginDrag).toHaveBeenCalledOnce();

      await vi.advanceTimersByTimeAsync(300);
      wrapper.get('.titlebar-drag').element.dispatchEvent(new MouseEvent('mousedown', {
        bubbles: true, button: 0, detail: 1, clientX: 0, clientY: 0,
      }));
      await vi.advanceTimersByTimeAsync(101);
      expect(beginDrag).toHaveBeenCalledOnce();

      await vi.advanceTimersByTimeAsync(299);
      expect(beginDrag).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
```

- [x] **Step 2: 运行新增测试并确认旧实现失败**

Run:

```powershell
cd frontend
npm test -- --run src/components/Topbar.test.ts
```

Expected: 新增的三个测试失败；分别表现为 detail=3 未调用、mouseup 后仍调用、旧 timer 在第二轮开始 101ms 后提前调用。

- [x] **Step 3: 实现统一的待拖动清理与奇偶点击仲裁**

在 `Topbar.vue` 中把当前 timer/armed/beginDrag 区域改为：

```ts
const DBLCLICK_MS = 400;
let dragTimer: ReturnType<typeof setTimeout> | null = null;
let dragArmed = false;
let removePendingDragListeners: (() => void) | null = null;

function cancelPendingDrag() {
  if (dragTimer !== null) {
    clearTimeout(dragTimer);
    dragTimer = null;
  }
  removePendingDragListeners?.();
  removePendingDragListeners = null;
  dragArmed = false;
}

function enterNativeMoveLoop() {
  const shouldStart = dragArmed;
  cancelPendingDrag();
  if (!shouldStart) return;
  // 系统原生移动循环：Snap / 动画原生。桥调用阻塞到拖拽结束，不 await。
  void api()?.window_begin_drag?.();
}

const DRAG_THRESHOLD_PX = 4;

async function beginDrag(event: MouseEvent) {
  // 连续点击序列中的偶数次按下交给 dblclick；第三次等奇数按下可开始新拖动。
  if (event.detail > 0 && event.detail % 2 === 0) {
    cancelPendingDrag();
    return;
  }
  cancelPendingDrag();
  dragArmed = true;
  const startX = event.clientX;
  const startY = event.clientY;
  const onMove = (ev: MouseEvent) => {
    if (Math.abs(ev.clientX - startX) > DRAG_THRESHOLD_PX ||
        Math.abs(ev.clientY - startY) > DRAG_THRESHOLD_PX) {
      enterNativeMoveLoop();
    }
  };
  const onMouseUp = () => cancelPendingDrag();
  removePendingDragListeners = () => {
    window.removeEventListener('mousemove', onMove, true);
    window.removeEventListener('mouseup', onMouseUp, true);
  };
  window.addEventListener('mousemove', onMove, true);
  window.addEventListener('mouseup', onMouseUp, true);
  dragTimer = setTimeout(enterNativeMoveLoop, DBLCLICK_MS);
}
```

并在 `toggleMaximizeNative()` 开头调用 `cancelPendingDrag()`，确保所有 `dblclick` 路径都先清理待拖动状态。函数声明会被提升，因此保留现有函数顺序即可。

- [x] **Step 4: 运行组件测试并确认修复通过**

Run:

```powershell
cd frontend
npm test -- --run src/components/Topbar.test.ts
```

Expected: `Topbar.test.ts` 全部测试通过。

- [x] **Step 5: 运行前端完整测试与构建**

Run:

```powershell
cd frontend
npm test
npm run build
```

Expected: 所有 Vitest 测试通过；`vue-tsc -b && vite build` 退出码为 0。

- [x] **Step 6: 检查改动边界并提交实现**

Run:

```powershell
git diff --check
git status --short
git diff -- frontend/src/components/Topbar.vue frontend/src/components/Topbar.test.ts
git add docs/superpowers/plans/2026-08-24-frameless-titlebar-drag-state.md frontend/src/components/Topbar.vue frontend/src/components/Topbar.test.ts
git commit -m "fix(frameless): 修复双击还原后的标题栏拖动"
```

Expected: 无空白错误；提交只包含计划、组件和组件测试。
