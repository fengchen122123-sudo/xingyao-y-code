# Composer Control Colors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the attachment label with a plus icon and make both composer permission buttons reflect the selected mode color without a leading checkbox.

**Architecture:** Apply the same semantic `permission-mode-btn perm-<mode>` classes in `KimiEmptyState` and `InputArea`. Keep the color palette in global CSS so both composers and the dropdown consume the existing theme variables without duplicating TypeScript mappings.

**Tech Stack:** Vue 3, TypeScript, Pinia, Vue Test Utils, Vitest, CSS custom properties

## Global Constraints

- Empty-state and active-conversation composers must render the same control semantics.
- Permission colors are confirm green, auto cyan, plan purple, and full red.
- Remove only the composer button's leading checkbox; retain the dropdown's selected check.
- Do not change attachment picking, permission persistence, or sending behavior.

---

### Task 1: Unify Composer Controls and Permission Colors

**Files:**
- Create: `frontend/src/components/ComposerControls.test.ts`
- Modify: `frontend/src/components/KimiEmptyState.vue`
- Modify: `frontend/src/components/InputArea.vue`
- Modify: `frontend/src/components/PermDropdown.vue`
- Modify: `frontend/src/style.css`

**Interfaces:**
- Consumes: `store.permissionMode: 'confirm' | 'auto' | 'plan' | 'full'` and existing theme color variables.
- Produces: `data-test="attachment-button"`, `data-test="permission-button"`, and semantic `perm-*` classes in both composers.

- [ ] **Step 1: Write failing composer control tests**

```ts
it.each([
  ['empty composer', KimiEmptyState],
  ['active composer', InputArea],
])('renders simplified and colored controls in the %s', async (_name, component) => {
  const store = useAppStore();
  const wrapper = mount(component);
  expect(wrapper.get('[data-test="attachment-button"]').text()).toBe('＋');
  expect(wrapper.get('[data-test="permission-button"]').text()).not.toContain('☑');
  expect(wrapper.get('[data-test="permission-button"]').classes()).toContain('perm-confirm');
  store.permissionMode = 'plan';
  await wrapper.vm.$nextTick();
  expect(wrapper.get('[data-test="permission-button"]').classes()).toContain('perm-plan');
});

it('uses green for confirm mode in the dropdown', () => {
  const wrapper = mount(PermDropdown, { props: { visible: true, anchorEl: document.createElement('button') } });
  expect(wrapper.findAll('.dd-dot')[0].attributes('style')).toContain('var(--green)');
});
```

- [ ] **Step 2: Run the new test and verify RED**

Run: `npm.cmd test -- --run src/components/ComposerControls.test.ts`

Expected: FAIL because the composer test selectors/classes do not exist and confirm still uses yellow.

- [ ] **Step 3: Implement shared semantic controls**

In both composer templates:

```vue
<button data-test="attachment-button">＋</button>
<button
  data-test="permission-button"
  :class="['permission-mode-btn', `perm-${store.permissionMode}`]"
>
  <span>{{ formatPermMode(store.permissionMode) }}</span>
  <span>▼</span>
</button>
```

In `PermDropdown.vue`, change the confirm color to `var(--green)`. In `style.css`, map `.perm-confirm`, `.perm-auto`, `.perm-plan`, and `.perm-full` to the existing green, cyan, purple, and red variables with matching border, text, and soft background.

- [ ] **Step 4: Run focused and full verification**

```powershell
npm.cmd test -- --run src/components/ComposerControls.test.ts
npm.cmd test -- --run
npm.cmd run build
```

Expected: the new tests, all frontend tests, and production build pass.

- [ ] **Step 5: Verify GUI mode switching and commit**

Use the local GUI to verify the empty composer and active composer, then switch all four modes and confirm green/cyan/purple/red states.

```powershell
git add frontend/src/components/ComposerControls.test.ts frontend/src/components/KimiEmptyState.vue frontend/src/components/InputArea.vue frontend/src/components/PermDropdown.vue frontend/src/style.css docs/superpowers/plans/2026-08-09-composer-control-colors.md
git commit -m "feat: color composer permission modes"
```
