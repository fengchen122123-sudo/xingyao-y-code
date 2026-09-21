<script setup lang="ts">
/** 统一状态标签（移植自 co-team mobile）：语义色映射，双主题经 token 自适应。 */
import { computed } from 'vue';

const props = withDefaults(defineProps<{ status: string; label?: string }>(), { label: '' });

type Tone = 'green' | 'yellow' | 'red' | 'accent' | 'neutral';

const TONE_MAP: Record<string, Tone> = {
  completed: 'green', success: 'green', passed: 'green', done: 'green', ok: 'green', connected: 'green',
  running: 'accent', retrying: 'accent', active: 'accent', executing: 'accent', open: 'accent', in_progress: 'accent',
  pending: 'neutral', planned: 'neutral', queued: 'neutral', idle: 'neutral', todo: 'neutral', main: 'neutral',
  failed: 'red', error: 'red', blocked: 'red', cancelled: 'red',
  waiting_approval: 'yellow', waiting_clarify: 'yellow', clarifying: 'yellow', takeover_queued: 'yellow', main_takeover: 'yellow',
};

const tone = computed<Tone>(() => TONE_MAP[props.status] || 'neutral');
const text = computed(() => props.label || props.status);
</script>

<template>
  <span class="st-tag" :class="tone">{{ text }}</span>
</template>

<style scoped>
/* 方角 mono 小标签（4px 圆角 + 发丝线 + 语义 tint），保留状态点 */
.st-tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: var(--fs-meta);
  font-family: var(--font-mono);
  font-weight: 500;
  line-height: 1;
  height: 18px;
  padding: 0 7px;
  border-radius: 4px;
  border: 1px solid transparent;
  white-space: nowrap;
}
.st-tag::before {
  content: '';
  width: 5px; height: 5px;
  border-radius: 50%;
  background: currentColor;
  flex: none;
}
.st-tag.green { color: var(--ok); background: color-mix(in srgb, var(--ok) 10%, transparent); border-color: color-mix(in srgb, var(--ok) 25%, transparent); }
.st-tag.yellow { color: var(--warn); background: color-mix(in srgb, var(--warn) 10%, transparent); border-color: color-mix(in srgb, var(--warn) 25%, transparent); }
.st-tag.red { color: var(--danger); background: color-mix(in srgb, var(--danger) 10%, transparent); border-color: color-mix(in srgb, var(--danger) 25%, transparent); }
.st-tag.accent { color: var(--accent); background: var(--accent-soft); border-color: var(--accent-line); }
.st-tag.neutral { color: var(--text-2); background: var(--bg-raised); border-color: var(--line); }
</style>
