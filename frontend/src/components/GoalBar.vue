<template>
  <!-- /goal 目标状态条：插在聊天标题栏下方、滚动区外（与电脑操控安全条同层），
       对话滚动时保持可见。只有当前会话真的有目标时才渲染，没有目标时整块不存在，
       不占布局高度 -->
  <div
    v-if="visible && goalState"
    class="goal-bar"
    :class="{ 'gb-achieved': isAchieved }"
    data-test="goal-bar"
    data-theme-region="conversation_toolbar"
  >
    <span class="gb-badge" :class="statusTone" data-test="goal-status">{{ statusLabel }}</span>
    <!-- 目标原文可能很长（用户随手写一段需求），单行省略 + title 悬浮看全文，
         避免把轮次和按钮挤出可视区 -->
    <span class="gb-goal" :title="goalState.goal" data-test="goal-text">{{ goalState.goal }}</span>
    <span class="gb-round" data-test="goal-round">第 {{ goalState.round }}/{{ goalState.max_rounds }} 轮</span>
    <span
      v-if="goalState.last_summary"
      class="gb-summary"
      :title="goalState.last_summary"
      data-test="goal-summary"
    >{{ goalState.last_summary }}</span>
    <span class="gb-spacer"></span>
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

// 轮询间隔：目标循环一轮动辄几十秒，5 秒足够让徽标跟上，又不至于把桥打满
const POLL_INTERVAL_MS = 5000;
// 达成后停留时长：让用户看到"已完成"这个结果，再自动收起（只是隐藏显示）
const ACHIEVED_VISIBLE_MS = 3000;

const store = useAppStore();

// 目标状态唯一数据源在 store 里：组件只读，不在组件内另存一份副本，
// 否则切会话/重拉后会出现"store 已更新、条上还是旧状态"
const goalState = computed(() => store.goalState);

// achieved 后只做"本地隐藏"：不改 store —— "目标已达成"这件事仍需可查
// （切走再切回、刷新页面都会重新拉到并再显示一次），直接清 store 会让
// 用户以为目标记录被删了
const achievedHidden = ref(false);

// 按钮点击期间的本地 busy：防止连点重复打桥（停止/继续都不是幂等重放友好的操作）
const busy = ref(false);

let pollTimer: ReturnType<typeof setInterval> | null = null;
let hideTimer: ReturnType<typeof setTimeout> | null = null;

// 状态码 -> 中文徽标：后端给的是英文码（权威定义在 core/slash_commands/goal/state.py），
// 中文文案只在这里维护一份
const STATUS_LABELS: Record<string, string> = {
  running: '进行中',
  awaiting_confirm: '待确认',
  blocked: '已达上限',
  stopped: '已停止',
  achieved: '已完成',
  interrupted: '已中断',
};

// 徽标配色分组：run 绿 / warn 黄 / ok 绿亮 / bad 红 / dim 灰
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

// running / awaiting_confirm 都是"还在推进"（后者等用户确认下一步），可停止；
// blocked/stopped/interrupted 是"停住了但没结论"，可继续（与后端 _GOAL_RESUMABLE 一致）
const canStop = computed(
  () => statusKey.value === 'running' || statusKey.value === 'awaiting_confirm',
);
const canResume = computed(
  () => statusKey.value === 'blocked'
    || statusKey.value === 'stopped'
    || statusKey.value === 'interrupted',
);

const visible = computed(() => !!goalState.value && !achievedHidden.value);

// 状态指纹：轮询每次都会换成新对象，直接 watch 对象会让"已隐藏"被反复重置；
// 只有状态/目标/轮次真的变了才重新决定是否展示
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
    // goalState 为空（当前会话没有目标）时不轮询：状态条本就不显示，空转只会
    // 白白打桥；新目标出现靠 sendMessage 里的 /goal 一次性刷新兜住。
    // achieved 已收起时同理：没有可展示的状态，不必继续拉。
    if (!goalState.value || achievedHidden.value) return;
    void store.loadGoalState();
  }, POLL_INTERVAL_MS);
}

onMounted(() => {
  // 挂载先探测一次：刷新页面/切到已有目标的会话时不必等第一个 5 秒
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
    // store 内部调用后已重拉状态，按钮/徽标会随新快照自动切换
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
/* 达成是终局好消息：整条换成低饱和绿底，靠颜色也能一眼分辨成功/停止 */
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

/* 窄屏：摘要先折叠（次要信息），目标原文与按钮永不隐藏 */
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
