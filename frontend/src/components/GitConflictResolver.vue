<template>
  <div class="git-conflict-resolver" data-test="git-conflict-resolver">
    <div class="gcr-head">
      <span class="gcr-badge">冲突解决</span>
      <div class="gcr-path-wrap" :title="path">
        <span class="gcr-file-name">{{ fileParts.name }}</span>
        <span v-if="fileParts.dir" class="gcr-file-dir">{{ fileParts.dir }}</span>
      </div>
      <button class="gcr-close-btn" data-test="git-conflict-close" title="关闭 (Esc)" @click="close">
        <AppIcon name="x" :size="15" :stroke-width="2" />
      </button>
    </div>

    <div class="gcr-note">
      <AppIcon name="info" :size="13" :stroke-width="2" class="gcr-note-icon" />
      <span>AI 建议不会直接落盘；Result 由你确认并点击「标记已解决」后生效（规格 §5.10）</span>
    </div>

    <div class="gcr-columns">
      <div
        v-for="col in columns"
        :key="col.key"
        class="gcr-col"
        :class="`col-${col.key}`"
        :data-test="'git-conflict-' + col.key"
      >
        <div class="gcr-col-title">
          <span class="gcr-col-dot"></span>
          <span>{{ col.label }}</span>
        </div>
        <textarea
          v-if="col.key === 'result'"
          v-model="resultText"
          data-test="git-conflict-result"
          class="gcr-text gcr-result"
          rows="8"
        ></textarea>
        <pre v-else class="gcr-text">{{ col.value ?? '（不存在）' }}</pre>
      </div>
    </div>

    <div class="gcr-actions">
      <button class="gcr-btn" data-test="git-conflict-accept-current" @click="accept('current')">接受 Current</button>
      <button class="gcr-btn" data-test="git-conflict-accept-incoming" @click="accept('incoming')">接受 Incoming</button>
      <span class="gcr-flex"></span>
      <button class="gcr-btn sparkle" data-test="git-conflict-ai" @click="aiAdvice">
        <AppIcon name="sparkles" :size="12" :stroke-width="2" />
        <span>AI 建议</span>
      </button>
      <button
        class="gcr-btn primary"
        data-test="git-conflict-apply"
        :disabled="!resultText"
        @click="applyResult"
      >
        <AppIcon name="check" :size="12" :stroke-width="2.2" />
        <span>标记已解决</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useGitStore } from '../stores/gitStore';
import { splitGitPath } from '../utils/gitFormat';
import AppIcon from './AppIcon.vue';

const props = defineProps<{ path: string }>();
const store = useGitStore();

const base = ref<string | null>(null);
const ours = ref<string | null>(null);
const theirs = ref<string | null>(null);
const resultText = ref('');

const fileParts = computed(() => splitGitPath(props.path));

const columns = computed(() => [
  { key: 'base', label: 'Base（共同祖先）', value: base.value },
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
.git-conflict-resolver {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  min-height: 0;
  color: var(--xy-text, var(--text));
}
.gcr-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--xy-border, var(--border));
  flex-shrink: 0;
}
.gcr-badge {
  flex-shrink: 0;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--xy-rose, var(--red));
  background: var(--xy-rose-soft, color-mix(in srgb, var(--red) 12%, transparent));
  border-radius: 999px;
  padding: 2px 10px;
}
.gcr-path-wrap {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.gcr-file-name {
  font-weight: 600;
  font-size: 13.5px;
  white-space: nowrap;
  flex-shrink: 0;
}
.gcr-file-dir {
  font-family: var(--mono, Consolas, monospace);
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.gcr-close-btn {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--xy-text-3, var(--text-faint));
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.gcr-close-btn:hover {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
}
.gcr-note {
  margin: 0 16px;
  padding: 8px 12px;
  border-radius: 10px;
  background: var(--xy-surface, var(--bg2));
  border: 1px solid var(--xy-border, var(--border));
  font-size: 12px;
  color: var(--xy-text-2, var(--text-dim));
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.gcr-note-icon {
  color: var(--xy-accent, var(--accent));
  flex-shrink: 0;
}
.gcr-columns {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 0 16px;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 10px;
}
.gcr-col {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  border-radius: 12px;
  background: var(--xy-surface, var(--bg2));
  border: 1px solid var(--xy-border, var(--border));
  min-height: 150px;
}
.gcr-col.col-result {
  background: var(--xy-card, var(--bg1));
  border-color: color-mix(in srgb, var(--xy-accent, var(--accent)) 45%, var(--xy-border, var(--border)));
}
.gcr-col-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--xy-text-2, var(--text-dim));
}
.gcr-col-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--xy-text-3, var(--text-faint));
}
.col-ours .gcr-col-title { color: var(--xy-accent, var(--accent)); }
.col-ours .gcr-col-dot { background: var(--xy-accent, var(--accent)); }
.col-theirs .gcr-col-title { color: var(--xy-green, var(--green)); }
.col-theirs .gcr-col-dot { background: var(--xy-green, var(--green)); }
.col-result .gcr-col-title { color: var(--xy-text, var(--text)); }
.col-result .gcr-col-dot { background: var(--xy-amber, var(--orange)); }
.gcr-text {
  flex: 1;
  margin: 0;
  background: var(--code-bg, var(--theme-code-background));
  border: 1px solid var(--xy-border, var(--border));
  border-radius: 8px;
  color: var(--xy-text, var(--text));
  font-family: var(--mono, Consolas, monospace);
  font-size: 12px;
  line-height: 1.55;
  padding: 8px 10px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 220px;
  overflow: auto;
}
.gcr-result {
  resize: vertical;
  min-height: 120px;
}
.gcr-result:focus {
  outline: none;
  border-color: var(--xy-accent, var(--accent));
}
.gcr-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  padding: 12px 16px;
  border-top: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
  flex-shrink: 0;
}
.gcr-flex { flex: 1; }
.gcr-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 12px;
  border-radius: 8px;
  border: 1px solid var(--xy-border-strong, var(--border));
  background: var(--xy-card, var(--bg1));
  color: var(--xy-text-2, var(--text));
  font-family: inherit;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s, color 0.12s;
}
.gcr-btn:hover:not(:disabled) {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
  border-color: var(--xy-accent, var(--accent));
}
.gcr-btn.sparkle {
  background: var(--xy-accent-soft, var(--accent-soft));
  border-color: transparent;
  color: var(--xy-accent, var(--accent));
}
.gcr-btn.sparkle:hover:not(:disabled) {
  border-color: var(--xy-accent, var(--accent));
}
.gcr-btn.primary {
  background: var(--xy-accent, var(--accent));
  border-color: var(--xy-accent, var(--accent));
  color: var(--xy-accent-fg, #fff);
  font-weight: 600;
}
.gcr-btn.primary:hover:not(:disabled) {
  color: var(--xy-accent-fg, #fff);
  filter: brightness(1.06);
}
.gcr-btn:disabled {
  opacity: 0.45;
  cursor: default;
}
</style>
