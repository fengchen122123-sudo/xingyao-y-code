<template>
  <div class="theme-region-editor" :data-mode="mode">
    <!-- 分区模式：背景 / 文字 / 边框 / 强调色 + 透明度 + 背景图 -->
    <template v-if="mode === 'region'">
      <div class="editor-group">
        <div class="group-title">颜色</div>
        <ColorField
          v-for="field in REGION_COLOR_FIELDS"
          :key="field.key"
          :field="field"
          :prefix="''"
          :value="stringValue((modelValue as ThemeRegion)[field.key])"
          :disabled="disabled"
          :opaque-only="false"
          @commit="commitRegionField"
          @validity="valid => onFieldValidity(field.key, valid)"
        />
      </div>

      <div class="editor-group">
        <div class="group-title">背景透明度</div>
        <label class="range-row">
          <input
            data-test="background-opacity"
            type="range"
            min="0"
            max="1"
            step="0.05"
            :value="(modelValue as ThemeRegion).background_opacity"
            :disabled="disabled"
            @input="commitRegionNumber('background_opacity', $event)"
          />
          <span class="range-value">{{ (modelValue as ThemeRegion).background_opacity }}</span>
        </label>
      </div>

      <div class="editor-group">
        <div class="group-title">背景图片</div>
        <div v-if="(modelValue as ThemeRegion).image" class="image-row">
          <span class="image-path mono">{{ (modelValue as ThemeRegion).image }}</span>
          <button
            v-if="!disabled"
            data-test="remove-image"
            class="secondary-button compact"
            type="button"
            @click="$emit('remove-image')"
          >移除图片</button>
        </div>
        <button
          v-else-if="!disabled"
          data-test="choose-image"
          class="secondary-button compact"
          type="button"
          :disabled="disabled"
          @click="$emit('choose-image')"
        >选择背景图片…</button>
        <p v-else class="image-empty">未设置背景图片</p>

        <label class="range-row">
          <span class="range-label">图片透明度</span>
          <input
            data-test="image-opacity"
            type="range"
            min="0"
            max="1"
            step="0.05"
            :value="(modelValue as ThemeRegion).image_opacity"
            :disabled="disabled"
            @input="commitRegionNumber('image_opacity', $event)"
          />
          <span class="range-value">{{ (modelValue as ThemeRegion).image_opacity }}</span>
        </label>

        <div class="select-grid">
          <label class="select-field">
            <span>填充方式</span>
            <select
              data-test="image-size"
              :value="(modelValue as ThemeRegion).image_size"
              :disabled="disabled"
              @change="commitRegionSelect('image_size', $event)"
            >
              <option value="cover">cover（铺满）</option>
              <option value="contain">contain（完整显示）</option>
              <option value="auto">auto（原始尺寸）</option>
            </select>
          </label>
          <label class="select-field">
            <span>位置</span>
            <select
              data-test="image-position"
              :value="(modelValue as ThemeRegion).image_position"
              :disabled="disabled"
              @change="commitRegionSelect('image_position', $event)"
            >
              <option value="center">居中</option>
              <option value="top">顶部</option>
              <option value="bottom">底部</option>
              <option value="left">左侧</option>
              <option value="right">右侧</option>
            </select>
          </label>
        </div>
        <p class="editor-hint">支持 PNG / JPEG / WebP，单张最大 10 MiB，边长不超过 8192 像素。</p>
      </div>
    </template>

    <!-- 全局模式：语义颜色令牌 + 全局背景图 -->
    <template v-else-if="mode === 'global'">
      <div class="editor-group">
        <div class="group-title">全局语义颜色</div>
        <div class="token-grid">
          <ColorField
            v-for="field in GLOBAL_COLOR_FIELDS"
            :key="field.key"
            :field="field"
            :prefix="`token-`"
            :value="stringValue((modelValue as Record<string, string | number>)[field.key])"
            :disabled="disabled"
            :opaque-only="false"
            @commit="commitTokenField"
            @validity="valid => onFieldValidity(field.key, valid)"
          />
        </div>
        <p class="editor-hint">修改会即时预览；字体、圆角等非颜色令牌保持当前值不变。</p>
      </div>

      <div class="editor-group">
        <div class="group-title">全局背景图片</div>
        <div v-if="appImage.image" class="image-row">
          <span class="image-path mono">{{ appImage.image }}</span>
          <button v-if="!disabled" data-test="remove-app-image" class="secondary-button compact" type="button" @click="$emit('remove-image')">移除图片</button>
        </div>
        <button v-else-if="!disabled" data-test="choose-app-image" class="secondary-button compact" type="button" :disabled="disabled" @click="$emit('choose-image')">选择背景图片…</button>
        <p v-else class="image-empty">未设置背景图片</p>

        <label class="range-row">
          <span class="range-label">图片透明度</span>
          <input data-test="app-image-opacity" type="range" min="0" max="1" step="0.05" :value="appImage.image_opacity" :disabled="disabled" @input="commitAppImageNumber('image_opacity', $event)" />
          <span class="range-value">{{ appImage.image_opacity }}</span>
        </label>
        <div class="select-grid">
          <label class="select-field">
            <span>填充方式</span>
            <select data-test="app-image-size" :value="appImage.image_size" :disabled="disabled" @change="commitAppImageSelect('image_size', $event)">
              <option value="cover">cover（铺满）</option>
              <option value="contain">contain（完整显示）</option>
              <option value="auto">auto（原始尺寸）</option>
            </select>
          </label>
          <label class="select-field">
            <span>位置</span>
            <select data-test="app-image-position" :value="appImage.image_position" :disabled="disabled" @change="commitAppImageSelect('image_position', $event)">
              <option value="center">居中</option>
              <option value="top">顶部</option>
              <option value="bottom">底部</option>
              <option value="left">左侧</option>
              <option value="right">右侧</option>
            </select>
          </label>
        </div>
        <p class="editor-hint">全局背景图垫在所有分区之下，无图分区会透出它。支持 PNG / JPEG / WebP，单张最大 10 MiB。</p>
      </div>
    </template>

    <!-- 原生标题栏模式：仅 #RRGGBB 颜色与明暗模式，无图片 -->
    <template v-else>
      <div class="editor-group">
        <div class="group-title">Windows 系统标题栏</div>
        <ColorField
          v-for="field in NATIVE_COLOR_FIELDS"
          :key="field.key"
          :field="field"
          :prefix="'native-'"
          :value="stringValue((modelValue as NativeTitlebarTheme)[field.key])"
          :disabled="disabled"
          :opaque-only="true"
          @commit="commitNativeField"
          @validity="valid => onFieldValidity(field.key, valid)"
        />
        <label class="select-field">
          <span>标题栏按钮模式</span>
          <select
            data-test="native-mode"
            :value="(modelValue as NativeTitlebarTheme).mode"
            :disabled="disabled"
            @change="commitNativeMode"
          >
            <option value="light">light（浅色按钮）</option>
            <option value="dark">dark（深色按钮）</option>
          </select>
        </label>
        <p class="editor-hint">系统标题栏使用 Windows DWM 颜色，只接受 #RRGGBB，不支持背景图片。</p>
      </div>
    </template>

    <button
      v-if="!disabled && mode !== 'native'"
      data-test="clear-overrides"
      class="secondary-button compact clear-overrides"
      type="button"
      @click="$emit('clear-overrides')"
    >恢复继承值</button>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import ColorField from './ColorField.vue';
import type { AppImageTheme, NativeTitlebarTheme, ThemeRegion } from '../../themes/types';

const props = defineProps<{
  mode: 'region' | 'global' | 'native';
  modelValue: ThemeRegion | Record<string, string | number> | NativeTitlebarTheme;
  disabled?: boolean;
  /** 全局模式下的全局背景图（由父组件从草稿读写） */
  appImage?: AppImageTheme;
}>();

const emit = defineEmits<{
  (e: 'update:modelValue', value: ThemeRegion | Record<string, string | number> | NativeTitlebarTheme): void;
  (e: 'update:appImage', value: AppImageTheme): void;
  (e: 'choose-image'): void;
  (e: 'remove-image'): void;
  (e: 'clear-overrides'): void;
  (e: 'validity', valid: boolean): void;
}>();

const REGION_COLOR_FIELDS = [
  { key: 'background', label: '背景' },
  { key: 'text', label: '文字' },
  { key: 'border', label: '边框' },
  { key: 'accent', label: '强调色' },
] as const;

const NATIVE_COLOR_FIELDS = [
  { key: 'background', label: '背景' },
  { key: 'text', label: '文字' },
  { key: 'border', label: '边框' },
] as const;

const GLOBAL_COLOR_FIELDS = [
  { key: 'background_app', label: '应用背景' },
  { key: 'background_panel', label: '面板背景' },
  { key: 'background_card', label: '卡片背景' },
  { key: 'background_elevated', label: '浮层背景' },
  { key: 'text_primary', label: '主文字' },
  { key: 'text_bright', label: '高亮文字' },
  { key: 'text_secondary', label: '次级文字' },
  { key: 'text_faint', label: '弱化文字' },
  { key: 'accent', label: '强调色' },
  { key: 'accent_secondary', label: '次强调色' },
  { key: 'accent_soft', label: '柔和强调色' },
  { key: 'border', label: '全局边框' },
  { key: 'success', label: '成功' },
  { key: 'warning', label: '警告' },
  { key: 'danger', label: '危险' },
  { key: 'orange', label: '橙色' },
  { key: 'info', label: '信息' },
  { key: 'purple', label: '紫色' },
  { key: 'code_background', label: '代码背景' },
  { key: 'float_background', label: '浮窗背景' },
  { key: 'shadow_color', label: '阴影颜色' },
] as const;

// 非法颜色会留在 ColorField 内并占用 invalidKeys；聚合后向编辑器上层上报，
// 上层用它阻断保存（非法值绝不静默写回草稿）。
const invalidKeys = new Set<string>();
function onFieldValidity(key: string, valid: boolean) {
  if (valid) invalidKeys.delete(key);
  else invalidKeys.add(key);
  emit('validity', invalidKeys.size === 0);
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function commitRegionField(key: string, value: string) {
  emit('update:modelValue', { ...(props.modelValue as ThemeRegion), [key]: value } as ThemeRegion);
}

function commitRegionNumber(key: 'background_opacity' | 'image_opacity', event: Event) {
  const parsed = Number((event.target as HTMLInputElement).value);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 1) return;
  emit('update:modelValue', { ...(props.modelValue as ThemeRegion), [key]: parsed } as ThemeRegion);
}

function commitRegionSelect(key: 'image_size' | 'image_position', event: Event) {
  const value = (event.target as HTMLSelectElement).value as ThemeRegion['image_size'] | ThemeRegion['image_position'];
  emit('update:modelValue', { ...(props.modelValue as ThemeRegion), [key]: value } as ThemeRegion);
}

function commitTokenField(key: string, value: string) {
  emit('update:modelValue', { ...(props.modelValue as Record<string, string | number>), [key]: value });
}

function commitNativeField(key: string, value: string) {
  emit('update:modelValue', { ...(props.modelValue as NativeTitlebarTheme), [key]: value } as NativeTitlebarTheme);
}

function commitNativeMode(event: Event) {
  const mode = (event.target as HTMLSelectElement).value as NativeTitlebarTheme['mode'];
  emit('update:modelValue', { ...(props.modelValue as NativeTitlebarTheme), mode } as NativeTitlebarTheme);
}

// 全局背景图：空默认兜底，字段编辑向上层 emit 更新草稿
const appImage = computed<AppImageTheme>(() => props.appImage ?? {
  image: null, image_opacity: 1, image_size: 'cover', image_position: 'center',
});
function commitAppImageNumber(key: 'image_opacity', event: Event) {
  const parsed = Number((event.target as HTMLInputElement).value);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 1) return;
  emit('update:appImage', { ...appImage.value, [key]: parsed });
}
function commitAppImageSelect(key: 'image_size' | 'image_position', event: Event) {
  const value = (event.target as HTMLSelectElement).value as AppImageTheme['image_size'];
  emit('update:appImage', { ...appImage.value, [key]: value });
}
</script>

<style scoped>
.theme-region-editor { display: flex; flex-direction: column; gap: 18px; }
.editor-group { border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; background: var(--bg1); }
.group-title { margin-bottom: 10px; font-size: 12px; font-weight: 600; color: var(--text-dim); }
.editor-hint { margin: 10px 0 0; font-size: 11px; color: var(--text-faint); }
.token-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 8px 18px; }
.range-row { display: flex; align-items: center; gap: 12px; margin: 8px 0; font-size: 12px; color: var(--text-dim); }
.range-row input[type='range'] { flex: 1; accent-color: var(--accent); }
.range-label { min-width: 64px; }
.range-value { min-width: 34px; text-align: right; font-family: var(--mono); font-size: 11.5px; }
.image-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 6px; }
.image-path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-dim); font-size: 12px; }
.image-empty { margin: 4px 0 6px; font-size: 12px; color: var(--text-faint); }
.select-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 10px; }
.select-field { display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: var(--text-dim); margin-top: 8px; }
.select-field select { min-height: 36px; padding: 0 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg2); color: var(--text); }
.clear-overrides { align-self: flex-start; }
.secondary-button { border: 1px solid var(--border); background: var(--bg3); color: var(--text); border-radius: 8px; min-height: 32px; padding: 0 12px; font-size: 12px; }
.secondary-button.compact { min-height: 30px; }
.secondary-button:disabled { opacity: 0.5; cursor: not-allowed; }
.mono { font-family: var(--mono); }
</style>
