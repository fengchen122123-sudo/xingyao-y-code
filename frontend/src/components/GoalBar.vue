<template>
  <div
    v-if="visible && goalState"
    class="goal-bar"
    :class="{ 'gb-achieved': isAchieved }"
    data-test="goal-bar"
    data-theme-region="conversation_toolbar"
  >
    <span class="gb-badge" :class="statusTone" data-test="goal-status">{{ statusLabel }}</span>
    <span class="gb-goal" :title="goalState.goal" data-test="goal-text">{{ goalState.goal }}</span>
    <span class="gb-round" data-test="goal-round">第 {{ goalState.round }}/{{ goalState.max_rounds }} 轮</span>
    <span
      v-if="goalState.last_summary"
      class="gb-summary"
      :title="goalState.last_summary"
      data-test="goal-summary"
    >{{ goalState.last_summary }}</span>
    <span class="gb-spacer"></span>
    <!-- 待确认是自评档的必经状态：模型自称完成但用户还没点头，这里必须给出
         确认入口，否则状态条只剩「停止」，用户看到"待确认"却无处确认 -->
    <button
      v-if="canConfirm"
      class="chip-btn ok"
      data-test="goal-confirm"
      title="确认目标已达成：自评档要你点头才会收尾"
      :disabled="busy"
      @click.stop="onConfirm"
    >确认达成</button>
    <button
      v-if="canStop"
      class="chip-btn danger"
      data-test="goal-stop"
      title="停止目标：本轮中断，不再自动续跑"
      :disabled="busy"
      @click.stop="onStop"
    >停止</button>
    <button
      v-else-if="canResume"
      class="chip-btn"
      data-test="goal-resume"
      title="继续推进目标（沿用剩余轮次）"
      :disabled="busy"
      @click.stop="onResume"
    >继续</button>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';

const POLL_INTERVAL_MS = 5000;
const ACHIEVED_VISIBLE_MS = 3000;

const store = useAppStore();

// 目标状态唯一数据源在 store 里：组件只读，不在组件内另存一份副本，
// 否则切会话/重拉后会出现"store 已更新、条上还是旧状态"
const goalState = computed(() => store.goalState);

const achievedHidden = ref(false);

// 按钮点击期间的本地 busy：防止连点重复打桥（停止/继续都不是幂等重放友好的操作）
const busy = ref(false);

let pollTimer: ReturnType<typeof setInterval> | null = null;
let hideTimer: ReturnType<typeof setTimeout> | null = null;

const STATUS_LABELS: Record<string, string> = {
  running: '进行中',
  awaiting_confirm: '待确认',
  blocked: '已达上限',
  stopped: '已停止',
  achieved: '已完成',
  interrupted: '已中断',
};

const STATUS_TONES: Record<string, string> = {
  running: 'run',
  awaiting_confirm: 'warn',
  achieved: 'ok',
  blocked: 'bad',
  stopped: 'dim',
  interrupted: 'dim',
};

const statusKey = computed(() => String(goalState.value?.status || ''));
const statusTone = computed(() => STATUS_TONES[statusKey.value] || 'dim');
const statusLabel = computed(() => STATUS_LABELS[statusKey.value] || statusKey.value);
const isAchieved = computed(() => statusKey.value === 'achieved');

const canStop = computed(
  () => statusKey.value === 'running' || statusKey.value === 'awaiting_confirm',
);
const canResume = computed(
  () => statusKey.value === 'blocked'
    || statusKey.value === 'stopped'
    || statusKey.value === 'interrupted',
);
// 待确认（自评档）：模型自己打了达成标记，但自评不可信，必须用户点头。
// 这一态同时保留「停止」——用户认为没达成时可以停掉，再「继续」接着跑剩余轮次。
const canConfirm = computed(() => statusKey.value === 'awaiting_confirm');

const visible = computed(() => !!goalState.value && !achievedHidden.value);

const stateKey = computed(() => {
  const g = goalState.value;
  return g ? `${g.status}|${g.goal}|${g.round}|${g.max_rounds}` : '';
});

watch(
  stateKey,
  () => {
    // 新快照先恢复显示：否则上一轮 achieved 的隐藏会盖住新目标的运行态
    achievedHidden.value = false;
    if (hideTimer) {
      clearTimeout(hideTimer);
      hideTimer = null;
    }
    if (isAchieved.value) {
      hideTimer = setTimeout(() => {
        achievedHidden.value = true;
        hideTimer = null;
      }, ACHIEVED_VISIBLE_MS);
    }
  },
  { immediate: true },
);

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(() => {
    if (!goalState.value || achievedHidden.value) return;
    void store.loadGoalState();
  }, POLL_INTERVAL_MS);
}

onMounted(() => {
  void store.loadGoalState();
  startPolling();
});

onUnmounted(() => {
  // 定时器必须清干净：会话切走/页面重建后残留的 interval 会继续打桥
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  if (hideTimer) {
    clearTimeout(hideTimer);
    hideTimer = null;
  }
});

async function onStop() {
  if (busy.value) return;
  busy.value = true;
  try {
    await store.stopGoal();
  } finally {
    busy.value = false;
  }
}

async function onResume() {
  if (busy.value) return;
  busy.value = true;
  try {
    await store.resumeGoal();
  } finally {
    busy.value = false;
  }
}

async function onConfirm() {
  if (busy.value) return;
  busy.value = true;
  try {
    await store.confirmGoal();
  } finally {
    busy.value = false;
  }
}
</script>

<style scoped>
.goal-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
  padding: 6px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
  font-size: 12.5px;
  color: var(--text-dim);
  overflow: hidden;
}
.goal-bar.gb-achieved {
  background: color-mix(in srgb, var(--green) 12%, var(--bg2));
  border-bottom-color: color-mix(in srgb, var(--green) 45%, var(--border));
}
.gb-badge {
  flex-shrink: 0;
  padding: 1px 8px;
  border-radius: var(--theme-radius-small);
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text);
  font-size: 11.5px;
  font-weight: 600;
  white-space: nowrap;
}
.gb-badge.run {
  color: var(--green);
  border-color: color-mix(in srgb, var(--green) 45%, var(--border));
}
.gb-badge.ok {
  color: var(--green);
  border-color: color-mix(in srgb, var(--green) 60%, var(--border));
  background: color-mix(in srgb, var(--green) 18%, var(--bg3));
}
.gb-badge.warn {
  color: var(--yellow);
  border-color: color-mix(in srgb, var(--yellow) 45%, var(--border));
}
.gb-badge.bad {
  color: var(--red);
  border-color: color-mix(in srgb, var(--red) 45%, var(--border));
}
.gb-badge.dim {
  color: var(--text-faint);
}
.gb-goal {
  min-width: 0;
  max-width: 46ch;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
  font-weight: 600;
}
.gb-round {
  flex-shrink: 0;
  white-space: nowrap;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}
.gb-summary {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-faint);
  font-size: 11.5px;
}
.gb-spacer {
  flex: 1;
  min-width: 0;
}

.chip-btn {
  background: var(--bg3);
  border: 1px solid var(--border);
  color: var(--text-dim);
  border-radius: 7px;
  padding: 3px 10px;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
  transition: 0.15s;
}
.chip-btn:hover:not(:disabled) {
  color: var(--text);
  border-color: var(--text-faint);
}
.chip-btn:disabled {
  opacity: 0.55;
  cursor: default;
}
.chip-btn.danger {
  color: var(--red);
}
.chip-btn.danger:hover:not(:disabled) {
  background: rgba(248, 81, 73, 0.15);
  border-color: var(--red);
}
.chip-btn.ok {
  color: var(--green);
  border-color: color-mix(in srgb, var(--green) 45%, var(--border));
}
.chip-btn.ok:hover:not(:disabled) {
  background: color-mix(in srgb, var(--green) 15%, transparent);
  border-color: color-mix(in srgb, var(--green) 70%, var(--border));
}

@media (max-width: 900px) {
  .gb-summary {
    display: none;
  }
  .gb-goal {
    max-width: 32ch;
  }
}
@media (max-width: 560px) {
  .goal-bar {
    gap: 8px;
    padding: 5px 10px;
  }
  .gb-goal {
    max-width: 18ch;
  }
}
</style>
