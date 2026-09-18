<template>
  <div class="git-conflict-resolver" data-test="git-conflict-resolver">
    <div class="gcr-title">三方冲突解决 — {{ path }}</div>
    <div class="gcr-note">AI 建议不会直接落盘；Result 由你确认后生效（规格 §5.10）</div>
    <div class="gcr-columns">
      <div class="gcr-col" v-for="col in columns" :key="col.key" :data-test="'git-conflict-' + col.key">
        <div class="gcr-col-title">{{ col.label }}</div>
        <textarea
          v-if="col.key === 'result'"
          v-model="resultText"
          data-test="git-conflict-result"
          class="gcr-text gcr-result"
          rows="10"
        ></textarea>
        <pre v-else class="gcr-text">{{ col.value ?? '（不存在）' }}</pre>
      </div>
    </div>
    <div class="gcr-actions">
      <button data-test="git-conflict-accept-current" @click="accept('current')">接受 Current</button>
      <button data-test="git-conflict-accept-incoming" @click="accept('incoming')">接受 Incoming</button>
      <button data-test="git-conflict-apply" :disabled="!resultText" @click="applyResult">标记已解决</button>
      <span class="gcr-flex"></span>
      <button data-test="git-conflict-ai" @click="aiAdvice">AI 建议</button>
      <button data-test="git-conflict-close" @click="close">关闭</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useGitStore } from '../stores/gitStore';

const props = defineProps<{ path: string }>();
const store = useGitStore();

const base = ref<string | null>(null);
const ours = ref<string | null>(null);
const theirs = ref<string | null>(null);
const resultText = ref('');

const columns = computed(() => [
  { key: 'base', label: 'Base', value: base.value },
  { key: 'ours', label: 'Current / Ours', value: ours.value },
  { key: 'theirs', label: 'Incoming / Theirs', value: theirs.value },
  { key: 'result', label: 'Result（可编辑）', value: resultText.value },
]);

onMounted(async () => {
  const api = window.pywebview?.api;
  if (!api) return;
  const res = await api.git_load_conflict(store.workdir, props.path);
  if (res.ok && res.conflict) {
    base.value = res.conflict.base;
    ours.value = res.conflict.ours;
    theirs.value = res.conflict.theirs;
    resultText.value = res.conflict.worktree ?? mergeDefault();
  }
});

function mergeDefault(): string {
  return `${ours.value ?? ''}\n${theirs.value ?? ''}`;
}

async function accept(side: 'current' | 'incoming') {
  await apiSafe('git_resolve_conflict', store.workdir, props.path, '', side);
  await store.refresh();
  close();
}

async function applyResult() {
  await apiSafe('git_resolve_conflict', store.workdir, props.path, resultText.value, '');
  await store.refresh();
  close();
}

async function aiAdvice() {
  // §5.15：AI 只发可见对话消息给建议；不直接改文件
  await apiSafe('git_ai_assist', store.workdir, 'conflict_advice');
}

function close() {
  store.conflictResolverPath = '';
}

async function apiSafe(name: string, ...args: unknown[]) {
  const api = window.pywebview?.api as unknown as Record<string, (...a: unknown[]) => Promise<unknown>> | undefined;
  if (api && typeof api[name] === 'function') return api[name](...args);
  return { ok: false };
}
</script>

<style scoped>
.git-conflict-resolver { display: flex; flex-direction: column; gap: 6px; padding: 6px 0; }
.gcr-title { font-weight: 700; }
.gcr-note { font-size: 11px; color: var(--text-dim, #888); }
.gcr-columns { display: flex; gap: 6px; flex-wrap: wrap; }
.gcr-col { flex: 1 1 40%; min-width: 180px; display: flex; flex-direction: column; gap: 2px; }
.gcr-col-title { font-size: 11px; color: var(--text-dim, #888); }
.gcr-text {
  background: transparent; border: 1px solid rgba(127,127,127,.35); border-radius: 4px;
  color: inherit; font-family: Consolas, monospace; font-size: 12px;
  padding: 4px; white-space: pre-wrap; word-break: break-all; max-height: 200px; overflow: auto;
}
.gcr-result { min-height: 120px; }
.gcr-actions { display: flex; gap: 6px; align-items: center; }
.gcr-flex { flex: 1; }
.gcr-actions button {
  background: rgba(100,181,246,.15); border: 1px solid rgba(100,181,246,.4);
  color: inherit; border-radius: 4px; cursor: pointer; padding: 2px 8px; font-size: 12px;
}
.gcr-actions button:disabled { opacity: .45; cursor: default; }
</style>
