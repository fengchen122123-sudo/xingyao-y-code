<template>
  <!-- codex 风格：权限询问以卡片形式内联在对话流末尾，不再用模态弹窗打断 -->
  <div v-if="store.permRequest.show" class="perm-card">
    <div class="pc-head">
      <span class="pc-ico">⚠</span>
      <div class="pc-title">
        <b>权限请求</b>
        <span class="pc-src">
          星瑶请求执行以下操作{{ store.permRequest.source ? '（来源: ' + store.permRequest.source + '）' : '' }}
        </span>
      </div>
    </div>
    <div class="pc-body">{{ store.permRequest.summary }}</div>
    <div class="pc-foot">
      <button class="pc-btn primary" @click="store.respondPermission(true, false)">允许一次</button>
      <button
        v-if="store.permRequest.allowAlways"
        class="pc-btn"
        @click="store.respondPermission(true, true)"
      >总是允许此类操作</button>
      <button class="pc-btn danger" @click="store.respondPermission(false, false)">拒绝</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useAppStore } from '../stores/appStore';
const store = useAppStore();
</script>

<style scoped>
.perm-card {
  max-width: 880px;
  margin: 10px auto;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-left: 3px solid var(--orange);
  border-radius: 12px;
  overflow: hidden;
}
.pc-head {
  padding: 12px 16px 8px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.pc-ico {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: rgba(240, 136, 62, 0.15);
  color: var(--orange);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  flex-shrink: 0;
}
.pc-title {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 14px;
}
.pc-src {
  font-size: 12px;
  color: var(--text-faint);
}
.pc-body {
  margin: 0 16px 12px;
  padding: 10px 14px;
  font-family: var(--mono);
  font-size: 12.5px;
  background: var(--code-bg);
  border-radius: 8px;
  white-space: pre-wrap;
  word-break: break-all;
  line-height: 1.65;
  color: var(--text-dim);
  max-height: 220px;
  overflow-y: auto;
}
.pc-foot {
  padding: 0 16px 13px;
  display: flex;
  gap: 9px;
  justify-content: flex-end;
}
.pc-btn {
  border-radius: 8px;
  padding: 6px 15px;
  font-size: 12.5px;
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text);
  transition: 0.15s;
  cursor: pointer;
}
.pc-btn:hover {
  border-color: var(--text-faint);
}
.pc-btn.primary {
  background: var(--accent);
  border: none;
  color: #fff;
  font-weight: 600;
}
.pc-btn.danger {
  color: var(--red);
}
.pc-btn.danger:hover {
  border-color: var(--red);
  background: rgba(248, 81, 73, 0.08);
}
</style>
