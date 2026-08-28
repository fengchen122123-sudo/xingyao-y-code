<template>
  <!-- Teleport 到 body：fixed 浮层必须逃离所在分区的 isolation 层叠上下文
       （composer 分区 z=5，否则往上弹时被 workspace(6)/会话工具栏(10) 盖住） -->
  <Teleport to="body">
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
        <span v-if="store.permissionMode === m.v" class="ck"><AppIcon name="check" :size="13" :stroke-width="2.25" /></span>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted } from 'vue';
import AppIcon from './AppIcon.vue';
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
  { v: 'confirm', c: 'var(--green)', desc: '查询自动放行；任何变更都需要确认' },
  { v: 'accept_edits', c: 'var(--yellow, #f1fa8c)', desc: '普通编辑和构建自动放行；删除、提交、推送等敏感操作需要确认' },
  { v: 'plan', c: 'var(--purple)', desc: '允许读取、查询和制定计划；禁止修改或删除任务文件' },
  { v: 'full', c: 'var(--red)', desc: '沙盒内放行所有非灾难性操作；工作区外由沙盒开关控制' },
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
