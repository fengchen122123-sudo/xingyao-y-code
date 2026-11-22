<template>
  <div
    v-if="store.currentSessionBackgroundTasks.length > 0"
    class="bg-entry-wrap"
    data-test="bg-bar"
  >
    <button
      type="button"
      class="chip-btn bg-entry-btn"
      :class="{ active: isOpen }"
      :aria-expanded="isOpen"
      data-test="bg-bar-details"
      :title="triggerTitle"
      @click.stop="toggleOpen"
    >
      <AppIcon name="loader" :size="12" :stroke-width="2" class="bg-spin" /><span class="chip-label">后台</span><span class="bg-entry-badge" data-test="bg-bar-count">{{ store.currentSessionBackgroundTasks.length }}</span>
    </button>

    <div v-if="isOpen" class="bg-pop" data-test="bg-card" @click.stop>
      <div class="bg-pop-head">
        <div class="bg-pop-head-l">
          <div class="ph-ico"><AppIcon name="square-terminal" :size="14" :stroke-width="2" /></div>
          <div>
            <h3 class="bg-pop-title">正在执行的后台任务</h3>
            <p class="bg-pop-note">共 {{ store.currentSessionBackgroundTasks.length }} 项 · 每 5 秒自动刷新</p>
          </div>
        </div>
        <button
          type="button"
          class="pop-close-btn"
          title="关闭"
          aria-label="关闭"
          data-test="bg-pop-close"
          @click.stop="closePopover"
        >
          <AppIcon name="x" :size="13" :stroke-width="2" />
        </button>
      </div>
      <div class="bg-pop-list">
        <div v-for="t in store.currentSessionBackgroundTasks" :key="t.task_id" class="bg-task">
          <div class="bg-task-top">
            <span class="bg-task-cmd" :title="t.command || t.task_id">{{ t.command || t.task_id }}</span>
            <span class="bg-task-status">{{ t.status || 'running' }}</span>
          </div>
          <div class="bg-task-foot">
            <span class="bg-task-time">{{ taskFootLabel(t) }}</span>
            <span class="bg-task-actions">
              <button class="chip-btn bg-task-btn" :data-test="'bg-detail-' + t.task_id" title="在右侧面板查看执行过程" @click.stop="openDetail(t.task_id)">详情</button>
              <button class="chip-btn bg-task-btn danger" :data-test="'bg-kill-' + t.task_id" title="终止该后台任务" @click="killTask(t.task_id)">终止</button>
            </span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';

const props = withDefaults(defineProps<{
  open?: boolean | null;
}>(), {
  open: null,
});

const emit = defineEmits<{
  (e: 'toggle'): void;
  (e: 'close'): void;
}>();

const store = useAppStore();
const internalOpen = ref(false);
const isControlled = computed(() => props.open !== null && props.open !== undefined);
const isOpen = computed(() => (isControlled.value ? Boolean(props.open) : internalOpen.value));

function toggleOpen() {
  if (isControlled.value) emit('toggle');
  else internalOpen.value = !internalOpen.value;
}

function closePopover() {
  if (isControlled.value) emit('close');
  else internalOpen.value = false;
}

const triggerTitle = computed(() => {
  const list = store.currentSessionBackgroundTasks;
  if (!list.length) return '后台任务';
  const first = list[0];
  const cmd = String(first.command || first.task_id || '').trim();
  return list.length === 1 && cmd
    ? `后台任务执行中：${cmd}`
    : `${list.length} 个后台任务在执行`;
});

// 任务卡脚注：时长 + 输出心跳。尚无实际输出（仅启动标记）时明确提示
// "等待首行输出"，避免把缓冲管道命令的正常静默误读为卡死
function taskFootLabel(t: { running_time?: string; output_size?: number; no_output_yet?: boolean }) {
  const parts = [t.running_time || '活跃'];
  if (t.no_output_yet) {
    parts.push('等待首行输出');
  } else if ((t.output_size ?? 0) > 0) {
    const s = t.output_size as number;
    parts.push(s < 1024 ? `输出 ${s} B` : `输出 ${(s / 1024).toFixed(1)} KB`);
  }
  return parts.join(' · ');
}

function onDocClick() {
  closePopover();
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && isOpen.value) {
    closePopover();
  }
}

watch(
  () => store.currentSessionBackgroundTasks.length,
  (len) => {
    if (len === 0 && isOpen.value) closePopover();
  },
);

onMounted(() => {
  document.addEventListener('click', onDocClick);
  document.addEventListener('keydown', onKeydown);
});
onUnmounted(() => {
  document.removeEventListener('click', onDocClick);
  document.removeEventListener('keydown', onKeydown);
});

function openDetail(id: string) {
  store.openBackgroundTaskDetail(id);
}

async function killTask(id: string) {
  if (!(window.pywebview && window.pywebview.api)) return;
  try {
    const res = await window.pywebview.api.kill_task(id);
    if (!res || !res.ok) {
      store.notifyModal('后台任务', res?.msg ? `终止任务失败: ${res.msg}` : '终止任务失败');
    } else {
      store.notifyModal('后台任务', res.msg ? `${res.msg}: ${id}` : '已请求终止后台任务: ' + id);
    }
  } catch (e) {
    store.notifyModal('后台任务', '终止任务失败: ' + e);
  }
  await store.refreshBackgroundTasks();
}
</script>

<style scoped>
.bg-entry-wrap {
  position: relative;
}
.bg-entry-btn {
  height: 28px;
  background: transparent;
  border: 1px solid var(--xy-border, var(--border));
  color: var(--xy-text-2, var(--text-dim));
  border-radius: 8px;
  padding: 0 10px;
  font-size: 12px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
  flex-shrink: 0;
  position: relative;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.bg-entry-btn:hover {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
}
.bg-entry-btn.active {
  background: var(--xy-active, var(--bg3));
  color: var(--xy-text, var(--text));
  border-color: var(--xy-border-strong, var(--border));
}
.bg-spin {
  color: var(--xy-accent, var(--accent));
  animation: bg-spin 0.9s linear infinite;
  flex-shrink: 0;
}
@keyframes bg-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
.bg-entry-badge {
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 99px;
  background: var(--xy-accent, var(--accent));
  color: var(--xy-accent-fg, #fff);
  font-size: 10.5px;
  font-weight: 700;
  line-height: 16px;
  text-align: center;
  display: inline-block;
}

.bg-pop {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  width: 460px;
  max-width: min(460px, calc(100vw - 32px));
  background: var(--xy-card, var(--float-bg));
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 14px;
  box-shadow: var(--xy-shadow-pop, 0 12px 32px rgba(0, 0, 0, 0.35));
  backdrop-filter: blur(14px);
  z-index: 210;
  display: flex;
  flex-direction: column;
  max-height: min(520px, 68vh);
  overflow: hidden;
  text-align: left;
  color: var(--xy-text, var(--text));
}
.bg-pop-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 11px 14px;
  border-bottom: 1px solid var(--xy-border, var(--border));
  flex-shrink: 0;
}
.bg-pop-head-l {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.ph-ico {
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  border-radius: 9px;
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
  flex: none;
}
.bg-pop-title {
  margin: 0;
  font-size: 13.5px;
  font-weight: 600;
  line-height: 1.3;
  color: var(--xy-text, var(--text));
}
.bg-pop-note {
  margin: 2px 0 0;
  font-size: 11px;
  line-height: 1.3;
  color: var(--xy-text-3, var(--text-faint));
}
.pop-close-btn {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: 8px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--xy-text-3, var(--text-faint));
  cursor: pointer;
  flex: none;
  transition: background 0.14s, color 0.14s;
}
.pop-close-btn:hover {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
}
.bg-pop-list {
  padding: 10px 12px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow-y: auto;
}
.bg-task {
  background: var(--xy-surface, var(--bg2));
  border: 1px solid var(--xy-border, var(--border));
  border-radius: 10px;
  padding: 9px 12px;
}
.bg-task-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}
.bg-task-cmd {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--xy-text, var(--text));
  font-weight: 600;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bg-task-status {
  font-size: 11px;
  color: var(--xy-green, var(--green));
  flex-shrink: 0;
}
.bg-task-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.bg-task-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.bg-task-time {
  font-size: 11px;
  color: var(--xy-text-3, var(--text-faint));
}
.bg-task-btn {
  height: 24px;
  background: var(--xy-card, var(--bg3));
  border: 1px solid var(--xy-border-strong, var(--border));
  color: var(--xy-text-2, var(--text-dim));
  border-radius: 7px;
  padding: 0 9px;
  font-size: 11.5px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  transition: 0.15s;
}
.bg-task-btn:hover {
  color: var(--xy-text, var(--text));
  border-color: var(--xy-text-3, var(--text-faint));
}
.bg-task-btn.danger {
  color: var(--xy-rose, var(--red));
}
.bg-task-btn.danger:hover {
  background: var(--xy-rose-soft, rgba(248, 81, 73, 0.15));
  border-color: var(--xy-rose, var(--red));
}
</style>
