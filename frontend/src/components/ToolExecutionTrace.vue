<template>
  <!-- 直接平铺工具行：不再有"执行过程 · N 项"外壳，也没有折叠开关。
       状态变化仍通过视觉隐藏的 aria-live 区域礼貌播报（行内耗时不在 live 区内） -->
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

// 只用于视觉隐藏的无障碍播报（"执行过程 · N 项 · 状态"），界面上不再渲染这层标题
const title = computed(
  () => `执行过程 · ${props.calls.length} 项 · ${STATE_LABELS[props.state] ?? props.state}`,
);

const rows = computed(() => buildToolTraceRows(props.calls, props.legacyDiffs, props.state));

// 运行期间用唯一定时器刷新 now，驱动运行中行的实时时长徽标
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

// 活动耗时：取最早一个仍在运行的调用的 started_at_ms
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
/* 平铺的工具行：行间距交给每行自己的 padding，这里只给块级留白 */
.exec-trace {
  display: flex;
  flex-direction: column;
  margin: 4px 0 6px;
}

/* 无障碍播报专用：视觉隐藏但仍在可访问树里 */
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
