# Model Modal Dismiss Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent backdrop clicks from closing the add/edit model modal while preserving all explicit close paths.

**Architecture:** Keep modal visibility owned by the existing `modelModal.show` state and `closeModelModal()` function. Remove the backdrop event binding only; the close button, cancel button, and successful submit continue using the existing explicit state transition.

**Tech Stack:** Vue 3, TypeScript, Vue Test Utils, Vitest

## Global Constraints

- Only change the model editor modal in `SettingsPage.vue`.
- Backdrop clicks must leave the modal and entered form values intact.
- Close, cancel, and successful save remain valid close paths.
- Failed save must continue to leave the modal open.

---

### Task 1: Make Model Modal Dismissal Explicit

**Files:**
- Modify: `frontend/src/components/SettingsPage.vue`
- Test: `frontend/src/components/SettingsPage.test.ts`

**Interfaces:**
- Consumes: existing `modelModal.show`, `openModelModal()`, `closeModelModal()`, and `saveModel()`.
- Produces: a modal backdrop that intercepts background clicks without changing modal state.

- [ ] **Step 1: Add the failing backdrop behavior test**

```ts
it('keeps the model editor open when the backdrop is clicked', async () => {
  const wrapper = mount(SettingsPage);
  await wrapper.get('[data-test="add-model"]').trigger('click');
  await wrapper.get('.modal-mask').trigger('click');
  expect(wrapper.find('[data-test="model-modal"]').exists()).toBe(true);
});
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `npm.cmd test -- --run src/components/SettingsPage.test.ts`

Expected: FAIL because the existing `@click.self="closeModelModal"` removes the modal.

- [ ] **Step 3: Remove the backdrop close binding**

Change the mask root from:

```vue
<div v-if="modelModal.show" class="modal-mask" @click.self="closeModelModal">
```

to:

```vue
<div v-if="modelModal.show" class="modal-mask">
```

Do not change the close button, cancel button, or `if (result?.ok) closeModelModal()` submit behavior.

- [ ] **Step 4: Add explicit close-path assertions and run verification**

Extend the component tests so the top-right close button and cancel button each remove the modal. Run:

```powershell
npm.cmd test -- --run src/components/SettingsPage.test.ts
npm.cmd test -- --run
npm.cmd run build
```

Expected: all component tests, all frontend tests, and the production build pass.

- [ ] **Step 5: Verify the real GUI and commit**

In the local GUI, open the model editor and verify backdrop click stays open, while close and cancel dismiss it.

```powershell
git add frontend/src/components/SettingsPage.vue frontend/src/components/SettingsPage.test.ts docs/superpowers/plans/2026-08-09-model-modal-dismiss-policy.md
git commit -m "fix: require explicit model modal dismissal"
```
