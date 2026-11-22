<template>
  <div class="exec-trace">
    <p class="sr-only" aria-live="polite">{{ title }}</p>
    <ToolExecutionRow
      v-for="(row, index) in rows"
      :key="row.id"
      :row="row"
      :elapsed-ms="activeElapsedMs"
      :class="{
        'trace-first': index === 0,
        'trace-last': index === rows.length - 1,
      }"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import ToolExecutionRow from './ToolExecutionRow.vue';
import {
  buildToolTraceRows,
  type ExecutionState,
  type LegacyDiff,
  type ToolExecutionCall,
} from '../utils/toolTrace';

const props = defineProps<{
  calls: ToolExecutionCall[];
  legacyDiffs: LegacyDiff[];
  state: ExecutionState;
}>();

const STATE_LABELS: Record<ExecutionState, string> = {
  running: '进行中',
  completed: '已完成',
  cancelled: '已取消',
  error: '异常结束',
};

const title = computed(
  () => `执行过程 · ${props.calls.length} 项 · ${STATE_LABELS[props.state] ?? props.state}`,
);

const rows = computed(() => buildToolTraceRows(props.calls, props.legacyDiffs, props.state));

const now = ref(Date.now());
let timer: number | undefined;

function stopTimer() {
  if (timer !== undefined) {
    window.clearInterval(timer);
    timer = undefined;
  }
}

watch(
  () => props.state,
  (state) => {
    if (state === 'running') {
      now.value = Date.now();
      if (timer === undefined) {
        timer = window.setInterval(() => {
          now.value = Date.now();
        }, 500);
      }
    } else {
      stopTimer();
    }
  },
  { immediate: true },
);
onUnmounted(stopTimer);

const activeElapsedMs = computed(() => {
  let earliest: number | undefined;
  for (const call of props.calls) {
    if (call.result || call.started_at_ms === undefined) continue;
    if (earliest === undefined || call.started_at_ms < earliest) {
      earliest = call.started_at_ms;
    }
  }
  if (earliest === undefined) return undefined;
  return Math.max(0, now.value - earliest);
});
</script>
<style scoped>
.exec-trace {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin: 4px 0 2px;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
</style>
