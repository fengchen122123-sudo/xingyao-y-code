<template>
  <!-- 输入自动补全面板：/ 斜杠命令 与 @ 文件引用共用 -->
  <div v-if="items.length" class="suggest-panel">
    <div
      v-for="(it, i) in items"
      :key="it.key"
      class="sg-item"
      :class="{ active: i === activeIndex }"
      @mousedown.prevent="emit('select', it)"
      @mouseenter="emit('hover', i)"
    >
      <span class="sg-icon">{{ it.icon }}</span>
      <span class="sg-label">{{ it.label }}</span>
      <span class="sg-desc">{{ it.desc }}</span>
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
  background: var(--float-bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: 0 -8px 30px rgba(0, 0, 0, 0.35);
  backdrop-filter: blur(14px);
  padding: 5px;
  max-height: 264px;
  overflow-y: auto;
  z-index: 100;
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
.sg-hint {
  padding: 5px 10px 3px;
  font-size: 10.5px;
  color: var(--text-faint);
  border-top: 1px solid var(--border);
  margin-top: 4px;
}
</style>
