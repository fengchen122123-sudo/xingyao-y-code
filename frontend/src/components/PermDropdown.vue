<template>
  <div id="dropdown" :class="{ show: visible }" :style="styleObj" @click.stop>
    <div 
      v-for="m in permModes" 
      :key="m.n"
      class="dd-item"
      @click="selectMode(m.n, m.v)"
    >
      🛡 {{ m.n }}
      <span v-if="store.permissionMode === m.n" class="ck">✓</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted } from 'vue';
import { useAppStore } from '../stores/appStore';

const props = defineProps<{
  visible: boolean;
  anchorEl: HTMLElement | null;
}>();

const emit = defineEmits(['close']);
const store = useAppStore();

const styleObj = ref<Record<string, string>>({});

const permModes = [
  { n: '默认模式', v: 'confirm', c: 'var(--yellow)' },
  { n: '自动接受编辑', v: 'auto', c: 'var(--cyan)' },
  { n: '计划模式', v: 'plan', c: 'var(--purple)' },
  { n: 'YOLO (全部允许)', v: 'yolo', c: 'var(--red)' }
];

function selectMode(name: string, val: string) {
  store.setPermissionMode(name, val);
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
  background: rgba(22, 27, 34, 0.95);
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
</style>
