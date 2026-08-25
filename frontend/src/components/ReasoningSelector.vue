<template>
  <button
    v-if="store.reasoning.visible"
    ref="buttonRef"
    data-test="reasoning-button"
    class="reasoning-button"
    :class="`reasoning-${variant}`"
    :disabled="store.reasoning.disabled"
    :title="buttonTitle"
    @click.stop="toggleMenu"
  >
    <AppIcon name="brain" :size="14" :stroke-width="1.9" />
    <span>{{ currentLabel }}</span>
    <AppIcon
      v-if="!store.reasoning.disabled"
      name="chevron-down"
      :size="10"
      :stroke-width="2"
      class="reasoning-chevron"
    />
  </button>

  <Teleport to="body">
    <div
      v-if="open"
      class="reasoning-menu"
      :style="menuStyle"
      data-test="reasoning-menu"
      @click.stop
    >
      <button
        v-for="option in store.reasoning.options"
        :key="option.value"
        data-test="reasoning-option"
        class="reasoning-option"
        :class="{ active: option.value === store.reasoning.choice }"
        @click="select(option.value)"
      >
        <span>{{ option.label }}</span>
        <AppIcon
          v-if="option.value === store.reasoning.choice"
          name="check"
          :size="14"
          :stroke-width="2.2"
        />
      </button>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue';
import type { ReasoningChoice } from '../types/pywebview';
import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';

withDefaults(defineProps<{ variant?: 'compact' | 'hero' }>(), {
  variant: 'compact',
});

const emit = defineEmits<{ open: [] }>();
const store = useAppStore();
const buttonRef = ref<HTMLElement | null>(null);
const open = ref(false);
const menuStyle = ref<Record<string, string>>({});

const currentLabel = computed(() => {
  if (store.reasoning.kind === 'fixed') return '开启';
  return store.reasoning.options.find(
    option => option.value === store.reasoning.choice,
  )?.label || store.reasoning.choice;
});

const buttonTitle = computed(() => {
  if (store.reasoning.disabled) return store.reasoning.disabled_reason;
  return `思考设置：${currentLabel.value}`;
});

function positionMenu() {
  const button = buttonRef.value;
  if (!button) return;
  const rect = button.getBoundingClientRect();
  const viewportPadding = 10;
  const gap = 8;
  const menuWidth = 184;
  const estimatedHeight = Math.max(48, store.reasoning.options.length * 40 + 12);
  const opensUp = rect.top >= window.innerHeight - rect.bottom;
  const left = Math.max(
    viewportPadding,
    Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - viewportPadding),
  );
  const top = opensUp
    ? Math.max(viewportPadding, rect.top - estimatedHeight - gap)
    : Math.min(rect.bottom + gap, window.innerHeight - estimatedHeight - viewportPadding);
  menuStyle.value = {
    position: 'fixed',
    left: `${Math.round(left)}px`,
    top: `${Math.round(Math.max(viewportPadding, top))}px`,
    width: `${menuWidth}px`,
    maxHeight: `${Math.max(48, window.innerHeight - viewportPadding * 2)}px`,
  };
}

function close() {
  if (!open.value) return;
  open.value = false;
  document.removeEventListener('click', onOutsideClick);
  window.removeEventListener('resize', positionMenu);
}

async function toggleMenu() {
  if (store.reasoning.disabled || !store.reasoning.options.length) return;
  if (open.value) {
    close();
    return;
  }
  emit('open');
  open.value = true;
  await nextTick();
  positionMenu();
  document.addEventListener('click', onOutsideClick);
  window.addEventListener('resize', positionMenu);
}

function onOutsideClick(event: MouseEvent) {
  if (!buttonRef.value?.contains(event.target as Node)) close();
}

async function select(choice: ReasoningChoice) {
  close();
  await store.setReasoningChoice(choice);
}

watch(() => store.activeModelId, close);
watch(() => store.reasoning.visible, visible => { if (!visible) close(); });
onUnmounted(close);
defineExpose({ close });
</script>

<style scoped>
.reasoning-button {
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  flex-shrink: 0;
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text-dim);
  cursor: pointer;
  transition: 0.15s;
  white-space: nowrap;
}
.reasoning-button:hover:not(:disabled) {
  border-color: var(--text-faint);
  color: var(--text);
}
.reasoning-button:disabled {
  cursor: default;
  opacity: 0.72;
}
.reasoning-compact {
  border-radius: 7px;
  padding: 4px 9px;
  font-size: 12.5px;
}
.reasoning-hero {
  border-radius: 10px;
  padding: 0 11px;
  font-size: 13px;
  background: transparent;
}
.reasoning-chevron {
  color: var(--text-faint);
}
.reasoning-menu {
  z-index: 1200;
  overflow-y: auto;
  padding: 6px;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--float-bg);
  box-shadow: 0 16px 42px rgba(0, 0, 0, 0.45);
  backdrop-filter: blur(16px);
}
.reasoning-option {
  width: 100%;
  min-height: 38px;
  padding: 8px 11px;
  border: none;
  border-radius: 9px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: transparent;
  color: var(--text-dim);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  text-align: left;
}
.reasoning-option:hover,
.reasoning-option.active {
  background: var(--bg3);
  color: var(--text);
}
.reasoning-option.active svg {
  color: var(--accent);
}
</style>
