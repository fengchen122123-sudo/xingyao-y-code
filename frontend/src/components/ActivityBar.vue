<template>
  <div class="activity-bar">
    <button
      v-for="item in items"
      :key="item.id"
      type="button"
      class="activity-btn"
      :class="{ active: model === item.id }"
      :title="item.title"
      @click="$emit('update:model', item.id)"
    >
      <AppIcon :name="item.icon" />
    </button>
  </div>
</template>

<script setup lang="ts">
import AppIcon from './AppIcon.vue';

defineProps<{ model: string }>();
defineEmits<{ (e: 'update:model', value: string): void }>();

// 图标名必须是 frontend/src/components/iconPaths.ts 里已注册的键（该文件第 2 行
// 明确要求保持纯字符串注册表以便控制体积），下面三个均已存在，不要新增图标
const items = [
  { id: 'files', icon: 'folder', title: '文件' },
  { id: 'git', icon: 'git-branch', title: 'Git' },
  { id: 'debug', icon: 'play', title: '调试' },
];
</script>

<style scoped>
.activity-bar {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 4px;
  background: var(--bg1);
  border-right: 1px solid var(--border);
}

.activity-btn {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
}

.activity-btn:hover {
  background: var(--bg2);
  color: var(--text);
}

.activity-btn.active {
  background: var(--bg3);
  color: var(--text-bright, var(--text));
  box-shadow: inset 2px 0 0 var(--accent);
}
</style>
