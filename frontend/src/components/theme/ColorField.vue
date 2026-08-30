<template>
  <div class="color-field" :class="{ invalid: !valid }">
    <span class="color-label">{{ field.label }}</span>
    <input
      :data-test="`${prefix}${field.key}-picker`"
      class="color-picker"
      type="color"
      :value="pickerValue"
      :disabled="disabled"
      @input="onPickerInput"
    />
    <input
      :data-test="`${prefix}${field.key}-hex`"
      class="color-hex mono"
      type="text"
      :value="hexText"
      :disabled="disabled"
      spellcheck="false"
      @input="onHexInput"
    />
    <span v-if="!valid" data-test="field-error" class="field-error">{{ errorText }}</span>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';

const props = defineProps<{
  field: { key: string; label: string };
  prefix: string;
  value: string;
  disabled?: boolean;
  // Windows 原生标题栏走 DWM COLORREF，无 alpha 通道，只接受 #RRGGBB。
  opaqueOnly?: boolean;
}>();

const emit = defineEmits<{
  (e: 'commit', key: string, value: string): void;
  (e: 'validity', valid: boolean): void;
}>();

const hexText = ref(props.value);
const valid = ref(true);
const errorText = ref('');

// <input type="color"> 只认 #rrggbb：#RRGGBBAA 值取前 7 位展示，
// 提交时以文本框完整值为准（避免选色器静默丢掉 alpha）。
const pickerValue = computed(() => {
  const text = hexText.value.trim();
  return /^#[0-9a-fA-F]{6}$/.test(text) || /^#[0-9a-fA-F]{8}$/.test(text)
    ? text.slice(0, 7).toLowerCase()
    : '#000000';
});

function errorFor(text: string): string | null {
  if (props.opaqueOnly) {
    return /^#[0-9a-fA-F]{6}$/.test(text) ? null : '原生标题栏颜色必须是 #RRGGBB';
  }
  return /^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(text) ? null : '颜色必须是 #RRGGBB 或 #RRGGBBAA';
}

function validate(text: string): boolean {
  const error = errorFor(text);
  const nextValid = error === null;
  errorText.value = error ?? '';
  if (nextValid !== valid.value) {
    valid.value = nextValid;
    emit('validity', nextValid);
  }
  return nextValid;
}

function onHexInput(event: Event) {
  const text = (event.target as HTMLInputElement).value;
  hexText.value = text;
  // 非法输入保留原文并显示关联错误，绝不静默修正或写回草稿。
  if (validate(text)) emit('commit', props.field.key, text);
}

function onPickerInput(event: Event) {
  const value = (event.target as HTMLInputElement).value;
  hexText.value = value;
  if (validate(value)) emit('commit', props.field.key, value);
}

// 外部替换模型（取消/切换主题/恢复继承）时同步文本框并清除错误。
watch(
  () => props.value,
  (value) => {
    hexText.value = value;
    validate(value);
  },
  { immediate: true },
);
</script>

<style scoped>
.color-field { display: flex; align-items: center; gap: 8px; min-height: 34px; }
.color-label { min-width: 72px; font-size: 12px; color: var(--text-dim); }
.color-picker { width: 34px; height: 28px; padding: 2px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg2); cursor: pointer; }
.color-hex { width: 108px; min-height: 28px; padding: 0 9px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg2); color: var(--text); font-size: 12px; }
.color-hex:disabled, .color-picker:disabled { opacity: 0.55; cursor: not-allowed; }
.color-field.invalid .color-hex { border-color: var(--red); }
.field-error { font-size: 11px; color: var(--red); }
.mono { font-family: var(--mono); }

/* 手机端（≤480px）：标签/文本框允许收缩，错误信息换行显示，避免一行撑破卡片 */
@media (max-width: 480px) {
  .color-label { min-width: 0; flex: none; }
  .color-hex { flex: 1 1 auto; width: auto; min-width: 0; }
  .field-error { flex: 1 1 100%; }
}
</style>
