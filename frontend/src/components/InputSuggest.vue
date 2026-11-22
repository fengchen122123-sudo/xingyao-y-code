<template>
  <div v-if="items.length || emptyHint" class="suggest-panel">
    <!-- 空态：检索完成但两侧都没有命中时必须给反馈，否则面板直接消失，
         用户无法区分「没有结果」和「功能失效」 -->
    <div v-if="!items.length" class="sg-empty">{{ emptyHint }}</div>
    <div v-else class="sg-list">
      <template v-for="(it, i) in items" :key="it.key">
        <div v-if="it.group && (i === 0 || items[i - 1].group !== it.group)" class="sg-group">
          {{ it.group === 'session' ? '对话' : '文件' }}
        </div>
        <div
          class="sg-item"
          :class="{ active: i === activeIndex }"
          @mousedown.prevent="emit('select', it)"
          @mouseenter="emit('hover', i)"
        >
          <span class="sg-icon">
            <AppIcon v-if="it.iconName" :name="it.iconName" :size="14" :stroke-width="2.1" />
            <template v-else>{{ it.icon }}</template>
          </span>
          <span class="sg-label">{{ it.label }}</span>
          <span class="sg-desc">{{ it.desc }}</span>
        </div>
      </template>
    </div>
    <div class="sg-hint">↑↓ 选择 · Tab / Enter 确认 · Esc 关闭</div>
  </div>
</template>

<script setup lang="ts">
import AppIcon from './AppIcon.vue';
import type { IconName } from './iconPaths';

export interface SuggestItem {
  key: string;
  icon: string;
  iconName?: IconName;
  label: string;
  desc: string;
  apply: string;
  group?: 'session' | 'file';
}

defineProps<{
  items: SuggestItem[];
  activeIndex: number;
  emptyHint?: string;
}>();

const emit = defineEmits<{
  (e: 'select', item: SuggestItem): void;
  (e: 'hover', index: number): void;
}>();
</script>

<style scoped>
.suggest-panel {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 0;
  right: 0;
  display: flex;
  flex-direction: column;
  background: var(--xy-card, var(--float-bg));
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 12px;
  box-shadow: var(--xy-shadow-pop, 0 -8px 30px rgba(0, 0, 0, 0.25));
  backdrop-filter: blur(14px);
  padding: 5px;
  /* 空会话页卡片居中，面板向上弹出时上方空间有限（约 280px），
     限高 232px（约 6 条候选 + 吸底提示栏）避免顶部被消息容器裁剪；
     超出部分由 .sg-list 独立滚动，提示条固定在面板最底部不被推挤 */
  max-height: 232px;
  z-index: 100;
}
.sg-list {
  overflow-y: auto;
  min-height: 0;
}
.sg-empty {
  padding: 9px 10px;
  font-size: 12px;
  color: var(--text-faint);
}
.sg-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 7px;
  cursor: pointer;
  font-size: 12.5px;
  color: var(--text-dim);
}
.sg-item.active {
  background: var(--bg3);
  color: var(--text);
}
.sg-icon {
  width: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: var(--accent);
  font-size: 12px;
}
.sg-label {
  font-family: var(--mono);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sg-desc {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 55%;
}
.sg-group {
  padding: 4px 10px 2px;
  font-size: 10.5px;
  color: var(--text-faint);
}
.sg-hint {
  flex-shrink: 0;
  background: var(--float-bg);
  border-radius: 0 0 7px 7px;
  padding: 5px 10px 3px;
  font-size: 10.5px;
  color: var(--text-faint);
  border-top: 1px solid var(--border);
  margin-top: 4px;
}
</style>
