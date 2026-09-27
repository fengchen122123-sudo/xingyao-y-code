<template>
  <div class="plan-pop" data-test="task-dialog" @click.stop>
    <div class="pop-head">
      <div class="ph-ico"><AppIcon name="chart" :size="14" :stroke-width="2" /></div>
      <div class="ph-title">
        <h3>任务计划</h3>
        <p class="ph-sub">星瑶编排复杂任务时的计划执行进度</p>
      </div>
      <button class="chip-btn" title="关闭" @click="$emit('close')"><AppIcon name="x" :size="12" :stroke-width="2" /> 关闭</button>
    </div>

    <div class="pop-body">
      <div class="progress-line" data-test="task-dialog-plan">
        <span>{{ store.planData?.title || '当前任务计划' }}</span>
        <span class="progress-pct">{{ store.planData?.progress ?? 0 }}%</span>
      </div>
      <div class="progress-bar"><i :style="{ width: (store.planData?.progress ?? 0) + '%' }"></i></div>

      <div v-if="planItems.length > 0" class="plan-list">
        <div
          v-for="item in planItems"
          :key="item.id"
          class="plan-item-row"
          :class="item.status"
          :data-test="'plan-item-' + item.status"
        >
          <span class="p-status-ico">
            <AppIcon
              :name="item.status === 'done' ? 'check' : (item.status === 'in_progress' ? 'refresh' : 'circle')"
              :size="12"
              :stroke-width="2"
              :class="{ 'tp-icon-spin': item.status === 'in_progress' }"
            />
          </span>
          <span class="p-text" :title="item.handoff_note || item.text">{{ item.handoff_note || item.text }}</span>
        </div>
      </div>

      <div v-else class="pop-empty" data-test="task-dialog-empty">
        计划还没有条目。星瑶拆解任务后，各条目的执行进度会在这里实时更新。
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';

// 会话头部“计划”入口的悬浮面板：锚定在按钮下方（.plan-entry-wrap 内
// absolute 定位），无全屏遮罩；计划数据由 plan_updated 事件流实时驱动，
// 这里纯展示、不拉取后端。点外部/Esc 由 App.vue 统一收起。
defineEmits<{ close: [] }>();

const store = useAppStore();

const planItems = computed(() => store.planData?.items ?? []);
</script>

<style scoped>
.plan-pop {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  width: 460px;
  max-width: min(460px, calc(100vw - 32px));
  background: var(--float-bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(14px);
  z-index: 210;
  display: flex;
  flex-direction: column;
  max-height: min(560px, 62vh);
  overflow: hidden;
  text-align: left;
}
.pop-head {
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}
.pop-head .ph-ico {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: var(--accent-soft);
  color: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.ph-title {
  flex: 1;
  min-width: 0;
}
.ph-title h3 {
  margin: 0;
  font-size: 13.5px;
}
.ph-sub {
  margin: 1px 0 0;
  font-size: 11px;
  color: var(--text-faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.pop-body {
  padding: 12px 14px 14px;
}
.pop-empty {
  padding: 14px 4px;
  text-align: center;
  color: var(--text-faint);
  font-size: 12px;
}

.progress-line {
  display: flex;
  justify-content: space-between;
  font-size: 12.5px;
  margin-bottom: 8px;
  font-weight: 600;
  gap: 8px;
}
.progress-pct {
  color: var(--accent);
  flex-shrink: 0;
}
.progress-bar {
  height: 6px;
  background: var(--bg3);
  border-radius: 99px;
  overflow: hidden;
  margin-bottom: 12px;
}
.progress-bar i {
  display: block;
  height: 100%;
  background: var(--accent);
  transition: width 0.3s ease;
}

/* 列表最多露出 6 个单行条目（行高 34px + 间距 7px），更多项在列表内滚动；
   条目单行省略、悬停 title 看全文，保证“6 项”不受文字换行影响 */
.plan-list {
  display: flex;
  flex-direction: column;
  gap: 7px;
  max-height: 239px;
  overflow-y: auto;
}
.plan-item-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 8px;
  background: var(--bg2);
  border: 1px solid var(--border);
  font-size: 12px;
  line-height: 1.5;
}
.plan-item-row.done {
  border-color: rgba(63, 185, 80, 0.3);
  background: rgba(63, 185, 80, 0.05);
}
.p-status-ico {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
}
.plan-item-row.done .p-status-ico {
  color: var(--green);
}
.plan-item-row.done .p-text {
  text-decoration: line-through;
  color: var(--text-faint);
}
.plan-item-row.in_progress {
  border-color: var(--accent);
  background: rgba(88, 166, 255, 0.08);
}
.plan-item-row.in_progress .p-status-ico {
  color: var(--accent);
}
.plan-item-row.in_progress .p-text {
  color: var(--text);
  font-weight: 600;
}
.plan-item-row.todo {
  color: var(--text-dim);
}
.p-text {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tp-icon-spin {
  animation: tp-spin 0.9s linear infinite;
}
@keyframes tp-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
