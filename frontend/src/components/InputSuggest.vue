<template>
  <!-- 输入自动补全面板：/ 斜杠命令 与 @ 文件引用共用 -->
  <div v-if="items.length" class="suggest-panel">
    <div class="sg-list">
      <template v-for="(it, i) in items" :key="it.key">
        <!-- 分组小标题：仅在 group 非空且与相邻前项不同时渲染（斜杠命令无 group 不渲染） -->
        <div v-if="it.group && (i === 0 || items[i - 1].group !== it.group)" class="sg-group">
          {{ it.group === 'session' ? '对话' : '文件' }}
        </div>
        <div
          class="sg-item"
          :class="{ active: i === activeIndex }"
          @mousedown.prevent="emit('select', it)"
          @mouseenter="emit('hover', i)"
        >
          <span class="sg-icon">{{ it.icon }}</span>
          <span class="sg-label">{{ it.label }}</span>
          <span class="sg-desc">{{ it.desc }}</span>
        </div>
      </template>
    </div>
    <div class="sg-hint">↑↓ 选择 · Tab / Enter 确认 · Esc 关闭</div>
  </div>
</template>

<script setup lang="ts">
export interface SuggestItem {
  key: string;
  icon: string;
  label: string;
  desc: string;
  // 选中后写回输入框的完整词（含 / 或 @ 前缀）
  apply: string;
  // 候选分组：@ 面板下区分「对话」与「文件」候选；斜杠命令无此字段
  group?: 'session' | 'file';
}

defineProps<{
  items: SuggestItem[];
  activeIndex: number;
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
  background: var(--float-bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: 0 -8px 30px rgba(0, 0, 0, 0.35);
  backdrop-filter: blur(14px);
  padding: 5px;
  /* 空会话页卡片居中，面板向上弹出时上方空间有限（约 280px），
     限高 232px（约 6 条候选 + 吸底提示栏）避免顶部被消息容器裁剪；
     超出部分由 .sg-list 独立滚动，提示条固定在面板最底部不被推挤 */
  max-height: 232px;
  z-index: 100;
}
.sg-list {
  /* 候选列表独立滚动区，把滚动与提示条隔离 */
  overflow-y: auto;
  min-height: 0;
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
  text-align: center;
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
  /* 固定最底部：flex 布局下不参与列表滚动，始终贴在面板底端 */
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
