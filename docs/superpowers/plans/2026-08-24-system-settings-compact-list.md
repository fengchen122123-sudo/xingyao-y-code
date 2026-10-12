# System Settings Compact List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render the command-interpreter setting as a compact extensible settings row with a right-aligned select control.

**Architecture:** Keep the existing Pinia snapshot and bridge API unchanged. Replace only `SystemSettings.vue` presentation and its component test: the component maps backend options to native select options and preserves the store action used to save a selection.

**Tech Stack:** Vue 3, TypeScript, Pinia, Vitest.

## Global Constraints

- Do not change command-shell persistence, interpreter discovery, execution behavior, or permission behavior.
- Disabled backend options must remain disabled in the select control.
- Do not add frontend dependencies.
- Follow the existing scoped CSS and `data-test` conventions.

---

### Task 1: Compact command-shell setting row

**Files:**
- Modify: `frontend/src/components/SystemSettings.vue`
- Modify: `frontend/src/components/SystemSettings.test.ts`

**Interfaces:**
- Consumes: `store.systemSettings.command_shell`, `store.systemSettings.command_shell_options`, `store.refreshSystemSettings()`, and `store.setCommandShell(shellId)`.
- Produces: a `select[data-test="command-shell-select"]` whose value is the selected shell ID and whose options reflect backend availability.

- [x] **Step 1: Write the failing component test**

```ts
expect(wrapper.find('[data-test="command-shell-select"]').exists()).toBe(true)
expect(wrapper.find('[data-test="shell-powershell"]').attributes('disabled')).toBeDefined()
```

- [x] **Step 2: Run the focused test to verify it fails**

Run: `npm test -- --run src/components/SystemSettings.test.ts`

Expected: FAIL because the component still renders radio cards and has no `command-shell-select`.

- [x] **Step 3: Replace radio cards with a compact row and select**

```vue
<section class="setting-row">
  <div class="setting-copy">
    <h2>默认命令解释器</h2>
    <p>AI 命令、后台任务、计划验证和右侧终端统一优先使用所选解释器。</p>
  </div>
  <select data-test="command-shell-select" :value="store.systemSettings.command_shell" @change="selectShell(($event.target as HTMLSelectElement).value)">
    <option v-for="option in store.systemSettings.command_shell_options" :key="option.id" :value="option.id" :disabled="!option.available">
      {{ option.label }}{{ option.available ? '' : '（未安装）' }}
    </option>
  </select>
</section>
```

Use `setting-row` CSS with a bottom divider, left text column, right control column, and a mobile media query that stacks the columns.

- [x] **Step 4: Run focused frontend tests to verify they pass**

Run: `npm test -- --run src/components/SystemSettings.test.ts src/stores/appStore.systemSettings.test.ts`

Expected: PASS with the new select, disabled unavailable option, and unchanged store persistence behavior.

- [x] **Step 5: Run full frontend verification**

Run: `npm test -- --run && npm run build`

Expected: all Vitest files pass and Vite production build exits 0.

- [x] **Step 6: Commit**

```bash
git add frontend/src/components/SystemSettings.vue frontend/src/components/SystemSettings.test.ts
git commit -m "style(settings): compact command shell selector"
```
