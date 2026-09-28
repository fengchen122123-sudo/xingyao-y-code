<template>
  <div v-if="store.currentSessionBackgroundTasks.length > 0" class="bg-bar" data-test="bg-bar">
    <div class="bg-bar-row" @click.stop>
      <AppIcon name="loader" :size="13" :stroke-width="2" class="bg-spin" />
      <span class="bg-text" data-test="bg-bar-count">
        <b class="bg-num">{{ store.currentSessionBackgroundTasks.length }}</b> 个后台任务在执行
      </span>
      <span class="bg-spacer"></span>
      <button class="chip-btn" data-test="bg-bar-details" @click.stop="open = !open">
        {{ open ? '收起' : '详情' }}
      </button>
    </div>

    <!-- 任务卡片：悬浮在横条上方、盖住对话区底部，无全屏遮罩 -->
    <div v-if="open" class="bg-pop" data-test="bg-card">
      <div class="bg-pop-head">
        <span class="bg-pop-title">正在执行的后台任务</span>
        <span class="bg-pop-note">每 5 秒自动刷新</span>
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
              <button class="chip-btn" :data-test="'bg-detail-' + t.task_id" title="在右侧面板查看执行过程" @click.stop="openDetail(t.task_id)">详情</button>
              <button class="chip-btn danger" :data-test="'bg-kill-' + t.task_id" title="终止该后台任务" @click="killTask(t.task_id)">终止</button>
            </span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue';
import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';

// 会话底部“后台任务执行条”：只显示当前会话发起的任务（渲染源
// currentSessionBackgroundTasks 按 session_id 过滤全局快照，其他会话/CLI
// 启动的任务不串显）；快照由 App.vue 每 5 秒轮询刷新；详情展开任务卡片
// 悬浮于对话区底部，无全屏遮罩，点外部/再点详情收起。
const store = useAppStore();
const open = ref(false);

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
  open.value = false;
}
onMounted(() => document.addEventListener('click', onDocClick));
onUnmounted(() => document.removeEventListener('click', onDocClick));

// 详情：打开右侧面板后台任务页签查看该任务执行过程，任务卡保持展开
// （bgTaskOpenNonce 每次自增，同一任务重复点击也能从其他页签切回）
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
.bg-bar {
  position: relative;
  flex-shrink: 0;
  border-top: 1px solid var(--border);
  background: var(--bg2);
}
.bg-bar-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 16px;
  font-size: 12.5px;
  color: var(--text-dim);
}
.bg-spin {
  color: var(--accent);
  animation: bg-spin 0.9s linear infinite;
  flex-shrink: 0;
}
@keyframes bg-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
.bg-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bg-num {
  color: var(--accent);
  font-weight: 700;
}
.bg-spacer {
  flex: 1;
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
.chip-btn:hover {
  color: var(--text);
  border-color: var(--text-faint);
}
.chip-btn.danger {
  color: var(--red);
}
.chip-btn.danger:hover {
  background: rgba(248, 81, 73, 0.15);
  border-color: var(--red);
}

.bg-pop {
  position: absolute;
  bottom: calc(100% + 8px);
  left: 12px;
  right: 12px;
  background: var(--float-bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(14px);
  z-index: 210;
  display: flex;
  flex-direction: column;
  max-height: min(420px, 46vh);
  overflow: hidden;
}
.bg-pop-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.bg-pop-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text);
}
.bg-pop-note {
  font-size: 11px;
  color: var(--text-faint);
}
.bg-pop-list {
  padding: 10px 12px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow-y: auto;
}
.bg-task {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 12px;
}
.bg-task-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 5px;
}
.bg-task-cmd {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--text);
  font-weight: 600;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bg-task-status {
  font-size: 11px;
  color: var(--green);
  flex-shrink: 0;
}
.bg-task-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.bg-task-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.bg-task-time {
  font-size: 11px;
  color: var(--text-faint);
}
</style>
