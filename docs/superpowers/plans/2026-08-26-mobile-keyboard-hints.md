# Mobile Keyboard Hints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide the input-area keyboard shortcut instructions at viewport widths of 768px or less while preserving the current desktop layout and keyboard behavior.

**Architecture:** Keep the existing shortcut hint markup in `InputArea.vue`, give it an explicit semantic hook, and hide that hook inside the component's scoped mobile media query. No runtime device detection or duplicate mobile component is introduced.

**Tech Stack:** Vue 3 single-file components, scoped CSS, Vitest, Vue Test Utils, Vite.

## Global Constraints

- The mobile breakpoint is exactly `max-width: 768px`.
- Desktop shortcut hints remain visible and unchanged.
- Enter, Shift+Enter, and Escape behavior remains unchanged.
- Do not alter other composer controls or the bottom status bar in this change.

---

### Task 1: Mobile-only keyboard hint visibility

**Files:**
- Modify: `frontend/src/components/InputArea.vue`
- Modify: `frontend/src/components/InputArea.test.ts`

**Interfaces:**
- Consumes: the existing `#input-meta` shortcut hint markup and project mobile breakpoint.
- Produces: a `data-test="keyboard-hints"` element with a `keyboard-hints` class hidden only at widths up to 768px.

- [ ] **Step 1: Write the failing component contract test**

Add this test to `frontend/src/components/InputArea.test.ts`:

```ts
it('为键盘快捷键提示提供独立的手机端隐藏标记', () => {
  const wrapper = mount(InputArea);
  const hints = wrapper.get('[data-test="keyboard-hints"]');

  expect(hints.classes()).toContain('keyboard-hints');
  expect(hints.text()).toContain('Enter');
  expect(hints.text()).toContain('Shift+Enter');
  expect(hints.text()).toContain('Esc');
  wrapper.unmount();
});
```

- [ ] **Step 2: Run the focused test and confirm it fails**

Run: `npm test -- --run frontend/src/components/InputArea.test.ts` from `frontend/`.

Expected: FAIL because `[data-test="keyboard-hints"]` does not exist.

- [ ] **Step 3: Add the semantic hook and mobile-only CSS**

Change the shortcut hint wrapper in `InputArea.vue` to:

```vue
<div class="r keyboard-hints" data-test="keyboard-hints">
  <span class="kbd">Enter</span> 发送 · <span class="kbd">Shift+Enter</span> 换行 · <span class="kbd">Esc</span> 中断
</div>
```

Add this scoped CSS at the end of `InputArea.vue`:

```css
@media (max-width: 768px) {
  .keyboard-hints {
    display: none;
  }
}
```

- [ ] **Step 4: Run focused verification**

Run: `npm test -- --run frontend/src/components/InputArea.test.ts` from `frontend/`.

Expected: all `InputArea` tests PASS.

- [ ] **Step 5: Run the production build**

Run: `npm run build` from `frontend/`.

Expected: TypeScript checking and Vite production build both complete successfully.

- [ ] **Step 6: Commit the focused change**

```bash
git add frontend/src/components/InputArea.vue frontend/src/components/InputArea.test.ts docs/superpowers/plans/2026-08-26-mobile-keyboard-hints.md
git commit -m "fix: hide keyboard hints on mobile"
```
