# Model Cascader Row Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align the first model row in the secondary model panel with the currently hovered provider row.

**Architecture:** Keep the existing anchor-based positioning for the main provider panel. On provider hover, derive a fixed secondary-panel position from the hovered row rectangle, subtract the secondary panel border/padding inset, and cap its height at the viewport safe boundary so overflow scrolls internally.

**Tech Stack:** Vue 3, TypeScript, Pinia, Vue Test Utils, Vitest

## Global Constraints

- Do not change the main provider panel anchor or upward/downward opening rules.
- Keep both panels inside a 12px viewport safe area.
- Model overflow must scroll inside the secondary panel.
- Add no dependencies and perform no unrelated refactor.

---

### Task 1: Align the Secondary Model Panel to the Hovered Provider

**Files:**
- Modify: `frontend/src/components/ModelCascader.vue`
- Test: `frontend/src/components/ModelCascader.test.ts`

**Interfaces:**
- Consumes: provider `MouseEvent.currentTarget`, `window.innerHeight`, and existing `activeProviderName` state.
- Produces: `activateProvider(provider: string, event: MouseEvent): void` and inline style state for the secondary panel.

- [x] **Step 1: Write the failing component test**

```ts
it('aligns the first model row with the hovered provider row', async () => {
  const wrapper = mount(ModelCascader, { props: { visible: true, anchorEl: anchor } });
  const provider = wrapper.findAll('.cascader-main-panel .cascader-item')[0];
  provider.element.getBoundingClientRect = () => providerRectAtTop(200);
  await provider.trigger('mouseenter');
  expect(wrapper.get('.cascader-sub-panel').attributes('style')).toContain('top: 193px');
  expect(wrapper.get('.cascader-sub-panel').attributes('style')).toContain('max-height: 563px');
});
```

- [x] **Step 2: Run the focused test and verify RED**

Run: `npm.cmd test -- --run src/components/ModelCascader.test.ts`

Expected: FAIL because `.cascader-sub-panel` has no inline aligned `top` or `max-height`.

- [x] **Step 3: Implement minimal hover positioning**

```ts
const subPanelStyle = ref<Record<string, string>>({});

function activateProvider(provider: string, event: MouseEvent) {
  activeProviderName.value = provider;
  const rowRect = (event.currentTarget as HTMLElement).getBoundingClientRect();
  const mainPanelRect = (event.currentTarget as HTMLElement)
    .closest('.cascader-main-panel')!.getBoundingClientRect();
  const top = Math.max(12, Math.floor(rowRect.top - 7));
  subPanelStyle.value = {
    position: 'fixed',
    top: `${top}px`,
    left: `${Math.min(mainPanelRect.right + 8, window.innerWidth - 252)}px`,
    maxHeight: `${Math.max(0, Math.floor(window.innerHeight - top - 12))}px`,
  };
}
```

Bind `subPanelStyle` to the secondary panel, invoke `activateProvider` on provider hover, and reset the style whenever the cascader opens.

- [x] **Step 4: Run focused and complete verification**

Run: `npm.cmd test -- --run src/components/ModelCascader.test.ts`

Expected: all ModelCascader tests PASS.

Run: `npm.cmd test -- --run`

Expected: all frontend tests PASS.

Run: `npm.cmd run build`

Expected: TypeScript and Vite production build PASS.

- [x] **Step 5: Verify in a real browser and commit**

Use the local Vite GUI with mocked providers, hover providers at different rows, and confirm the first model row stays aligned without viewport overflow.

```bash
git add frontend/src/components/ModelCascader.vue frontend/src/components/ModelCascader.test.ts docs/superpowers/plans/2026-08-09-model-cascader-row-alignment.md
git commit -m "fix: align model submenu with provider"
```
