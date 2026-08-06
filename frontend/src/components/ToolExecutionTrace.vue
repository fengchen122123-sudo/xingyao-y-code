<template>
  <div class="exec-trace">
    <button class="trace-head" :aria-expanded="expanded" @click="expanded = !expanded">
      <span class="trace-caret" aria-hidden="true">{{ expanded ? '▾' : '▸' }}</span>
      <!-- 状态变化（进行中 -> 已完成 等）通过 aria-live 礼貌播报；
           行内 elapsed 秒数不在 live 区域内，避免高频打扰 -->
      <span class="trace-title" aria-live="polite">{{ title }}</span>
    </button>
    <div v-show="expanded" class="trace-body">
      <ToolExecutionRow
        v-for="row in rows"
        :key="row.id"
        :row="row"
        :elapsed-ms="activeElapsedMs"
      />
    </div>
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

// 默认展开；折叠状态只影响本组件 UI，不写入任何 store
const expanded = ref(true);

const STATE_LABELS: Record<ExecutionState, string> = {
  running: '进行中',
  completed: '已完成',
  cancelled: '已取消',
  error: '异常结束',
};

// 头部计数是“已注册的原始调用数”，不是预测总数
const title = computed(
  () => `执行过程 · ${props.calls.length} 项 · ${STATE_LABELS[props.state] ?? props.state}`,
);

const rows = computed(() => buildToolTraceRows(props.calls, props.legacyDiffs));

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
.exec-trace {
  margin: 8px 0;
}
.trace-head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  background: none;
  border: none;
  padding: 2px 0;
  cursor: pointer;
  font-family: inherit;
  font-size: 12px;
  color: var(--text-faint);
  text-align: left;
}
.trace-head:hover {
  color: var(--text-dim);
}
.trace-caret {
  flex-shrink: 0;
  font-size: 11px;
}
.trace-title {
  font-weight: 600;
  letter-spacing: 0.2px;
}
.trace-body {
  margin-top: 2px;
}
</style>
