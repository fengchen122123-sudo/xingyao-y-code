<template>
  <div class="debug-pane" data-test="debug-pane">
    <div class="dp-left">
      <section class="dp-section">
        <header class="dp-head">
          <span>调用栈</span>
          <button class="dp-icon" title="刷新调用栈" data-test="debug-refresh" @click="debug.refreshFrames()">
            <AppIcon name="refresh" :size="12" />
          </button>
        </header>
        <ul class="dp-list" data-test="debug-frames">
          <li
            v-for="frame in debug.frames"
            :key="frame.id"
            class="dp-frame"
            :class="{ active: frame.id === debug.selectedFrameId }"
            data-test="debug-frame"
            @click="debug.selectFrame(frame.id)"
          >
            <span class="dp-frame-name">{{ frame.name }}</span>
            <span class="dp-frame-loc">{{ baseName(frame.path) }}:{{ frame.line }}</span>
          </li>
          <li v-if="!debug.frames.length" class="dp-empty">未暂停或调用栈为空</li>
        </ul>
      </section>

      <section class="dp-section">
        <header class="dp-head"><span>变量</span></header>
        <!-- 作用域页签：每个作用域显示**自己**的变量（旧写法在 v-for scope 里渲染同一个
             debug.variables，Locals 的子节点被当成 Globals 的内容再渲染一遍，用户看不出
             自己在看哪个作用域）。超过一个作用域才需要页签，只有一个时直接显示名字。 -->
        <div v-if="debug.scopes.length > 1" class="dp-scopes" data-test="debug-scopes">
          <button
            v-for="scope in debug.scopes"
            :key="scope.variablesReference"
            class="dp-scope-tab"
            :class="{ active: scope.variablesReference === debug.activeScopeRef }"
            data-test="debug-scope"
            @click="debug.selectScope(scope.variablesReference)"
          >
            {{ scope.name }}
          </button>
        </div>
        <div v-else-if="debug.scopes.length === 1" class="dp-scopes">
          <span class="dp-scope-name" data-test="debug-scope">{{ debug.scopes[0].name }}</span>
        </div>
        <!-- 层级面包屑：展开 self / 大 dict 之后能看出自己在哪一层，并能走回去 -->
        <div v-if="debug.varPath.length" class="dp-crumbs" data-test="debug-var-path">
          <button
            v-if="debug.varPath.length > 1"
            class="dp-btn dp-crumb-back"
            title="返回上一层"
            data-test="debug-var-back"
            @click="debug.backVariables()"
          >
            返回上一层
          </button>
          <span
            v-for="(node, index) in debug.varPath"
            :key="`${index}:${node.reference}`"
            class="dp-crumb"
            :class="{ active: index === debug.varPath.length - 1 }"
            data-test="debug-var-crumb"
            >{{ node.name }}</span
          >
        </div>
        <div class="dp-vars">
          <div
            v-for="item in debug.variables"
            :key="item.name"
            class="dp-var"
            data-test="debug-variable"
            :class="{ clickable: item.variablesReference > 0 }"
            @click="item.variablesReference > 0 && debug.expandVariables(item.variablesReference, item.name)"
          >
            <!-- 展开箭头只在真有子节点时出现：variablesReference 为 0 表示无子节点，
                 给它画个箭头等于骗用户去点（点了也不会展开） -->
            <span v-if="item.variablesReference > 0" class="dp-expand">
              <AppIcon name="chevron-right" :size="11" />
            </span>
            <span class="dp-var-name">{{ item.name }}</span>
            <span class="dp-var-value">{{ item.value }}</span>
            <span class="dp-var-type">{{ item.type }}</span>
          </div>
          <div v-if="!debug.scopes.length" class="dp-empty">暂停后可查看变量</div>
        </div>
      </section>
    </div>

    <div class="dp-right">
      <section class="dp-section dp-watch">
        <header class="dp-head"><span>监视</span></header>
        <div class="dp-watch-add">
          <input
            v-model="watchInput"
            class="dp-input"
            placeholder="输入表达式后回车"
            data-test="debug-watch-input"
            @keyup.enter="addWatch"
          />
          <button class="dp-btn" data-test="debug-watch-add" @click="addWatch">添加</button>
        </div>
        <ul class="dp-list">
          <li v-for="row in debug.watchResults" :key="row.expression" class="dp-watch-row" data-test="debug-watch-row">
            <span class="dp-var-name">{{ row.expression }}</span>
            <span class="dp-var-value">{{ row.error || row.result }}</span>
            <button class="dp-icon" title="移除" data-test="debug-watch-remove" @click="debug.removeWatch(row.expression)">
              <AppIcon name="x" :size="12" />
            </button>
          </li>
          <li v-if="!debug.watchResults.length" class="dp-empty">添加表达式以在暂停时求值</li>
        </ul>
      </section>

      <section class="dp-section dp-console">
        <header class="dp-head"><span>调试控制台</span></header>
        <pre class="dp-console-out" data-test="debug-console-output">{{ consoleText }}</pre>
        <div class="dp-console-in">
          <input
            v-model="consoleInput"
            class="dp-input"
            placeholder="在此求值（如 print(x)）后回车"
            data-test="debug-console-input"
            @keyup.enter="runExpression"
          />
          <button class="dp-btn" data-test="debug-console-run" @click="runExpression">求值</button>
        </div>
      </section>

      <div v-if="debug.lastError" class="dp-error" data-test="debug-pane-error">{{ debug.lastError }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import AppIcon from './AppIcon.vue';
import { useIdeDebugStore } from '../stores/ideDebugStore';
import { onDapOutput } from '../utils/dapBus';

const debug = useIdeDebugStore();
const watchInput = ref('');
const consoleInput = ref('');
// 控制台行缓冲：高频原始输出不进 Pinia（同 ptyBus 的理由），上限 2000 行防内存膨胀
const consoleLines = ref<string[]>([]);
const unsubs: Array<() => void> = [];

const consoleText = computed(() => consoleLines.value.join('\n'));

function appendConsole(text: string) {
  const lines = String(text).split(/\r?\n/);
  // DAP 的 output 事件基本都带尾换行：split 会多出一个空串元素，push 进缓冲后每组
  // 输出之间就多出一个空行（控制台双倍行距）。末尾那个空分片只是「行结束符」，丢掉；
  // 中间的空行（连续两个 \n）是真内容，必须保留。
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
  for (const line of lines) {
    consoleLines.value.push(line.replace(/\x1b\[[0-9;]*m/g, ''));
  }
  if (consoleLines.value.length > 2000) {
    consoleLines.value.splice(0, consoleLines.value.length - 2000);
  }
}

function baseName(path: string): string {
  const parts = String(path || '').split(/[\\/]/);
  return parts[parts.length - 1] || '';
}

async function addWatch() {
  const expression = watchInput.value.trim();
  if (!expression) return;
  await debug.addWatch(expression);
  watchInput.value = '';
}

async function runExpression() {
  const expression = consoleInput.value.trim();
  if (!expression) return;
  consoleInput.value = '';
  appendConsole(`> ${expression}`);
  const res = await debug.evaluate(expression);
  if (res?.ok) {
    debug.lastError = '';
    appendConsole(String(res.result ?? ''));
    return;
  }
  // 求值失败必须可见：控制台留一行中文原因，面板底部（以及调试工具条右侧的
  // dt-error，读的是同一个字段）同时给出错误，绝不静默吞掉（交付要求 3）
  const msg = String(res?.msg ?? '求值失败');
  debug.lastError = msg;
  appendConsole(`求值失败：${msg}`);
}

onMounted(() => {
  unsubs.push(onDapOutput((payload) => appendConsole(payload.text)));
});

onBeforeUnmount(() => {
  for (const unsub of unsubs) unsub();
  unsubs.length = 0;
});
</script>

<style scoped>
.debug-pane {
  display: flex;
  gap: 10px;
  height: 100%;
  min-height: 0;
  padding: 6px 8px;
  font-size: 11.5px;
  color: var(--text);
  overflow: hidden;
}
.dp-left,
.dp-right {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  min-height: 0;
}
.dp-left {
  flex: 0 0 42%;
}
.dp-right {
  flex: 1;
}
.dp-section {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
}
.dp-console {
  flex: 1;
}
.dp-head {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 24px;
  padding: 0 7px;
  background: var(--bg2);
  color: var(--text-dim);
  font-weight: 600;
  flex-shrink: 0;
}
.dp-icon,
.dp-btn {
  border: none;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  border-radius: 4px;
}
.dp-btn {
  height: 20px;
  padding: 0 7px;
}
.dp-icon:hover,
.dp-btn:hover {
  background: var(--bg2);
  color: var(--text);
}
.dp-list {
  list-style: none;
  margin: 0;
  padding: 2px 0;
  overflow: auto;
  min-height: 0;
}
.dp-frame,
.dp-var,
.dp-watch-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 8px;
  white-space: nowrap;
}
.dp-frame.active {
  background: var(--accent-soft);
}
.dp-frame-name,
.dp-var-name {
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
}
.dp-frame-loc,
.dp-var-type {
  color: var(--text-faint);
  font-size: 11px;
}
.dp-var-value {
  color: var(--accent);
  overflow: hidden;
  text-overflow: ellipsis;
}
.dp-vars {
  overflow: auto;
  min-height: 0;
}
.dp-scopes {
  display: flex;
  gap: 4px;
  padding: 2px 6px;
  flex-shrink: 0;
  overflow-x: auto;
}
.dp-scope-tab {
  border: none;
  background: transparent;
  color: var(--text-dim);
  font-size: 11.5px;
  padding: 1px 6px;
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
}
.dp-scope-tab:hover {
  color: var(--text);
}
.dp-scope-tab.active {
  background: var(--bg2);
  color: var(--text);
  font-weight: 600;
}
.dp-crumbs {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0 6px 2px;
  flex-shrink: 0;
  overflow-x: auto;
  white-space: nowrap;
}
.dp-crumb {
  color: var(--text-faint);
  font-size: 11px;
}
.dp-crumb.active {
  color: var(--text);
}
.dp-crumb-back {
  height: 18px;
  padding: 0 6px;
  border: 1px solid var(--border);
  flex-shrink: 0;
}
.dp-scope-name {
  padding: 2px 8px;
  color: var(--text-dim);
}
.dp-var.clickable {
  cursor: pointer;
}
.dp-var.clickable:hover {
  background: var(--bg2);
}
.dp-expand {
  display: inline-flex;
  color: var(--text-faint);
}
.dp-empty {
  padding: 4px 8px;
  color: var(--text-faint);
}
.dp-watch-add,
.dp-console-in {
  display: flex;
  gap: 6px;
  padding: 4px 6px;
}
.dp-input {
  flex: 1;
  min-width: 0;
  height: 22px;
  padding: 0 6px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--bg0);
  color: var(--text);
  font-family: Consolas, "Courier New", monospace;
  font-size: 11.5px;
}
.dp-console-out {
  flex: 1;
  margin: 0;
  padding: 6px 8px;
  overflow: auto;
  min-height: 0;
  background: var(--bg0);
  color: var(--text);
  font-family: Consolas, "Courier New", monospace;
  font-size: 11.5px;
  white-space: pre-wrap;
}
.dp-error {
  color: var(--orange);
  padding: 2px 4px;
}
</style>
