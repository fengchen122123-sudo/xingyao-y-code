<template>
  <div class="fold">
    <button class="fold-head" type="button" :aria-expanded="open" @click="open = !open">
      <AppIcon
        class="fold-caret"
        :class="{ 'is-open': open, 'caret-end': caret === 'end' }"
        name="chevron-right"
        :size="12"
        :stroke-width="2"
      />
      <span class="fold-title">
        <slot name="title">{{ title }}</slot>
      </span>
      <slot name="head-right" />
    </button>
    <div v-show="open" class="fold-body">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
import AppIcon from './AppIcon.vue';

withDefaults(
  defineProps<{
    title?: string;
    caret?: 'start' | 'end';
  }>(),
  { title: '', caret: 'start' },
);

// 受控/非受控双模式：v-model:open 受控，否则组件内部自持
const open = defineModel<boolean>('open', { default: false });
</script>

<style scoped>
.fold {
}
.fold-head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  background: none;
  border: none;
  padding: 2px 0;
  cursor: pointer;
  font-family: inherit;
  font-size: 12px;
  color: var(--text-faint);
  text-align: left;
  border-radius: 6px;
}
.fold-head:hover {
  color: var(--text-dim);
}
.fold-head:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.fold-caret {
  transition: transform 0.15s ease;
  transform: rotate(0deg);
}
.fold-caret.is-open {
  transform: rotate(90deg);
}
.fold-caret.caret-end {
  order: 2;
  margin-left: auto;
}
.fold-title {
  font-weight: 600;
  letter-spacing: 0.2px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fold-body {
  margin-top: 2px;
}
@media (prefers-reduced-motion: reduce) {
  .fold-caret {
    transition: none;
  }
}
</style>
