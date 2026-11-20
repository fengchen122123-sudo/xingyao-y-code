<template>
  <section class="chat-width-card" data-test="chat-width-card">
    <header class="cwc-head">
      <h3>对话区宽度</h3>
      <p>控制对话消息与输入栏在宽屏或最大化窗口下的横向展示范围。</p>
    </header>

    <div class="cwc-options" role="radiogroup" aria-label="对话区宽度">
      <button
        v-for="opt in CHAT_LAYOUT_WIDTH_OPTIONS"
        :key="opt.id"
        type="button"
        role="radio"
        class="cwc-opt"
        :class="{ active: chatLayoutWidth === opt.id }"
        :aria-checked="chatLayoutWidth === opt.id"
        :data-test="`chat-width-${opt.id}`"
        @click="setChatLayoutWidth(opt.id)"
      >
        <span class="cwc-opt-title">{{ opt.label }}</span>
        <span class="cwc-opt-desc">{{ opt.desc }}</span>
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import {
  CHAT_LAYOUT_WIDTH_OPTIONS,
  chatLayoutWidth,
  setChatLayoutWidth,
} from '../composables/chatLayoutWidth';
</script>

<style scoped>
.chat-width-card {
  width: 100%;
  min-width: 0;
  max-width: 1080px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px 18px;
  margin: 0 auto 16px;
  border: 1px solid var(--xy-border, var(--border));
  border-radius: 14px;
  background: var(--xy-card, var(--bg2));
}
.cwc-head h3 {
  margin: 0 0 4px;
  font-size: 13.5px;
  color: var(--xy-text, var(--text));
}
.cwc-head p {
  margin: 0;
  font-size: 12px;
  line-height: 1.65;
  color: var(--xy-text-3, var(--text-faint));
}
.cwc-options {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}
.cwc-opt {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid var(--xy-border, var(--border));
  border-radius: 10px;
  background: var(--xy-surface, var(--bg1));
  color: var(--xy-text, var(--text));
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.cwc-opt:hover {
  border-color: var(--xy-border-strong, var(--text-faint));
  background: var(--xy-hover, var(--bg3));
}
.cwc-opt.active {
  border-color: var(--xy-accent, var(--accent));
  background: var(--xy-accent-soft, var(--accent-soft));
}
.cwc-opt-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--xy-text, var(--text));
}
.cwc-opt.active .cwc-opt-title {
  color: var(--xy-accent, var(--accent));
}
.cwc-opt-desc {
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--xy-text-3, var(--text-faint));
}
@media (max-width: 768px) {
  .cwc-options {
    grid-template-columns: 1fr;
  }
}
</style>
