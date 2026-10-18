<template>
  <section class="app-glass-card" data-test="app-glass-card">
    <header class="agc-head">
      <h3>玻璃效果</h3>
      <p>
        在<strong>壁纸</strong>与<strong>界面内容</strong>之间叠一层可调的磨砂玻璃。
        默认 0% = 关闭，与改之前完全一致。
      </p>
    </header>

    <label class="agc-row">
      <span class="agc-label">玻璃透明度</span>
      <input
        data-test="glass-opacity"
        type="range"
        min="0"
        :max="GLASS_OPACITY_MAX"
        step="1"
        :value="glassOpacity"
        @input="onOpacity"
      />
      <b class="agc-value">{{ glassOpacity }}%</b>
    </label>

    <label class="agc-row">
      <span class="agc-label">模糊强度</span>
      <input
        data-test="glass-blur"
        type="range"
        min="0"
        :max="GLASS_BLUR_MAX"
        step="1"
        :value="glassBlur"
        @input="onBlur"
      />
      <b class="agc-value">{{ glassBlur }}px</b>
    </label>

    <div class="agc-actions">
      <button class="agc-btn" data-test="glass-preset-light" type="button" @click="preset(25, 16)">
        轻磨砂（25% + 16px）
      </button>
      <button class="agc-btn" data-test="glass-preset-strong" type="button" @click="preset(45, 28)">
        重磨砂（45% + 28px）
      </button>
      <button class="agc-btn" data-test="glass-reset" type="button" @click="preset(0, 0)">
        恢复默认（关闭）
      </button>
    </div>

    <p class="agc-hint">
      透明度越高、模糊越强，界面越"实"、壁纸越被压暗；透明度低 + 模糊高则偏通透。
      参数存在本地（localStorage），重启后保持；这是纯前端效果，不影响主题文件。
    </p>
  </section>
</template>

<script setup lang="ts">
import {
  GLASS_BLUR_MAX,
  GLASS_OPACITY_MAX,
  glassBlur,
  glassOpacity,
  setGlassBlur,
  setGlassOpacity,
} from '../composables/appGlass';

function onOpacity(event: Event) {
  setGlassOpacity(Number((event.target as HTMLInputElement).value));
}

function onBlur(event: Event) {
  setGlassBlur(Number((event.target as HTMLInputElement).value));
}

function preset(opacity: number, blur: number) {
  setGlassOpacity(opacity);
  setGlassBlur(blur);
}
</script>

<style scoped>
.app-glass-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px 18px;
  margin-bottom: 16px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg2);
}
.agc-head h3 { margin: 0 0 4px; font-size: 13.5px; color: var(--text); }
.agc-head p { margin: 0; font-size: 11.5px; line-height: 1.7; color: var(--text-faint); }
.agc-head strong { color: var(--text-dim); }
.agc-row { display: flex; align-items: center; gap: 10px; font-size: 12.5px; color: var(--text-dim); }
.agc-label { min-width: 84px; }
.agc-row input[type='range'] { flex: 1; max-width: 320px; }
.agc-value { min-width: 48px; color: var(--text); font-variant-numeric: tabular-nums; }
.agc-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.agc-btn {
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text);
  border-radius: 8px;
  padding: 5px 12px;
  font-size: 12px;
  cursor: pointer;
  transition: 0.15s;
}
.agc-btn:hover { border-color: var(--accent); color: var(--accent); }
.agc-hint { margin: 0; font-size: 11.5px; line-height: 1.7; color: var(--text-faint); }
</style>
