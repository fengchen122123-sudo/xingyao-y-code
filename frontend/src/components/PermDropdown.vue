<template>
  <div id="dropdown" :class="{ show: visible }" :style="styleObj" @click.stop>
    <div
      v-for="m in permModes"
      :key="m.v"
      class="dd-item"
      :title="m.desc"
      @click="selectMode(m.v)"
    >
      <span class="dd-dot" :style="{ background: m.c }"></span>
      {{ formatPermMode(m.v) }}
      <span v-if="store.permissionMode === m.v" class="ck">✓</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import { formatPermMode } from '../utils/markdown';
import type { PermissionMode } from '../types/pywebview';

const props = defineProps<{
  visible: boolean;
  anchorEl: HTMLElement | null;
}>();

const emit = defineEmits(['close']);
const store = useAppStore();

const styleObj = ref<Record<string, string>>({});

const permModes: Array<{ v: PermissionMode; c: string; desc: string }> = [
  { v: 'confirm', c: 'var(--green)', desc: '执行任何操作前都会向你确认' },
  { v: 'accept_edits', c: 'var(--yellow, #f1fa8c)', desc: '工作区内文件编辑自动放行，命令与沙盒外访问仍会询问' },
  { v: 'plan', c: 'var(--purple)', desc: '沙盒内可自由读取，只制定计划，不做任何修改' },
  { v: 'full', c: 'var(--red)', desc: '沙盒内全部放行，沙盒外操作仍会询问' },
];

function selectMode(val: PermissionMode) {
  store.setPermissionMode(val);
  emit('close');
}

watch(() => props.visible, (newVal) => {
  if (newVal && props.anchorEl) {
    const r = props.anchorEl.getBoundingClientRect();
    const isBottomHalf = r.top > window.innerHeight / 2;

    if (isBottomHalf) {
      styleObj.value = {
        position: 'fixed',
        bottom: `${window.innerHeight - r.top + 8}px`,
        left: `${Math.max(10, r.left)}px`,
        top: 'auto',
      };
    } else {
      styleObj.value = {
        position: 'fixed',
        top: `${r.bottom + 8}px`,
        left: `${Math.max(10, r.left)}px`,
        bottom: 'auto',
      };
    }
  }
});

function onGlobalClick(e: MouseEvent) {
  if (props.visible && props.anchorEl && !props.anchorEl.contains(e.target as Node)) {
    emit('close');
  }
}

onMounted(() => {
  document.addEventListener('click', onGlobalClick);
});
onUnmounted(() => {
  document.removeEventListener('click', onGlobalClick);
});
</script>

<style scoped>
#dropdown {
  position: fixed;
  background: var(--float-bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(14px);
  z-index: 999;
  min-width: 200px;
  display: none;
  padding: 5px;
}
#dropdown.show {
  display: block;
}
.dd-item {
  padding: 8px 12px;
  border-radius: 7px;
  cursor: pointer;
  font-size: 12.5px;
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-dim);
}
.dd-item:hover {
  background: var(--bg3);
  color: var(--text);
}
.dd-item .ck {
  margin-left: auto;
  color: var(--accent);
}
.dd-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}
</style>
