<template>
  <!-- C11：项目级 Hook 信任卡片（对话流内联，跟随 PermCard 之后；
       Hook 命令在信任前不执行，需用户明确确认） -->
  <div v-if="store.pendingProjectHooks.length" class="hook-card" data-test="hook-trust-card">
    <div class="hc-head">
      <span class="hc-ico"><AppIcon name="zap" :size="16" /></span>
      <div class="hc-title">
        <b>检测到项目级 Hook（{{ store.pendingProjectHooks.length }} 条）</b>
        <span v-if="sourcePath" class="hc-src">来源: {{ sourcePath }}</span>
      </div>
    </div>
    <div class="hc-list">
      <div
        v-for="hook in store.pendingProjectHooks"
        :key="hook.digest"
        class="hc-item"
        data-test="hook-trust-item"
      >
        <span class="hc-event">{{ hook.event }}</span>
        <code class="hc-cmd">{{ hook.command }}</code>
        <span v-if="hook.status === 'stale'" class="hc-stale">配置已变更</span>
      </div>
    </div>
    <div class="hc-warn">⚠ 这些命令将在本机执行。仅在信任该项目时确认。</div>
    <div class="hc-foot">
      <button
        class="hc-btn primary"
        data-test="hook-trust-grant-all"
        :disabled="busy"
        @click="grantAll"
      >信任并启用</button>
      <button
        class="hc-btn danger"
        data-test="hook-trust-deny-all"
        :disabled="busy"
        @click="denyAll"
      >拒绝</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';
const store = useAppStore();

// 来源取第一条待信任 Hook 的配置文件路径（同一项目来源一致）
const sourcePath = computed(() => store.pendingProjectHooks[0]?.source_path || '');
// 提交中禁用双按钮：避免连点重复触发批量信任动作
const busy = ref(false);

async function grantAll() {
  busy.value = true;
  try {
    await store.grantAllPendingHookTrusts();
  } finally {
    busy.value = false;
  }
}

async function denyAll() {
  busy.value = true;
  try {
    await store.denyAllPendingHookTrusts();
  } finally {
    busy.value = false;
  }
}
</script>

<style scoped>
.hook-card {
  max-width: var(--chat-col-width);
  margin: 10px auto;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-left: 3px solid var(--orange);
  border-radius: 12px;
  overflow: hidden;
}
.hc-head {
  padding: 12px 16px 8px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.hc-ico {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: rgba(240, 136, 62, 0.15);
  color: var(--orange);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.hc-title {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 14px;
}
.hc-src {
  font-size: 12px;
  color: var(--text-faint);
  word-break: break-all;
}
.hc-list {
  margin: 0 16px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.hc-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--code-bg);
  border-radius: 8px;
  font-size: 12.5px;
  min-width: 0;
}
.hc-event {
  flex-shrink: 0;
  color: var(--text-dim);
}
.hc-cmd {
  flex: 1;
  min-width: 0;
  font-family: var(--mono);
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-dim);
}
.hc-stale {
  flex-shrink: 0;
  padding: 1px 7px;
  border-radius: 6px;
  background: rgba(240, 136, 62, 0.15);
  color: var(--orange);
  font-size: 11px;
}
.hc-warn {
  margin: 0 16px 12px;
  font-size: 12.5px;
  color: var(--orange);
  line-height: 1.6;
}
.hc-foot {
  padding: 0 16px 13px;
  display: flex;
  gap: 9px;
  justify-content: flex-end;
}
.hc-btn {
  border-radius: 8px;
  padding: 6px 15px;
  font-size: 12.5px;
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text);
  transition: 0.15s;
  cursor: pointer;
}
.hc-btn:hover:not(:disabled) {
  border-color: var(--text-faint);
}
.hc-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.hc-btn.primary {
  background: var(--accent);
  border: none;
  color: #fff;
  font-weight: 600;
}
.hc-btn.danger {
  color: var(--red);
}
.hc-btn.danger:hover:not(:disabled) {
  border-color: var(--red);
  background: rgba(248, 81, 73, 0.08);
}
</style>