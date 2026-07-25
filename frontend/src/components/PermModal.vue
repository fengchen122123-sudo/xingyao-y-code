<template>
  <div class="modal-mask" :class="{ show: store.permModal.show }">
    <div class="modal">
      <div class="modal-head">
        <div class="mh-ico">⚠️</div>
        <div>
          <h3>权限请求 · <span>{{ store.permModal.toolName }}</span></h3>
          <p>星瑶请求执行以下操作，当前为默认权限模式</p>
        </div>
      </div>
      <div class="modal-body">
        {{ store.permModal.details }}
      </div>
      <div class="modal-foot">
        <button class="btn" @click="store.respondPermission(true, true)">总是允许此类操作</button>
        <button class="btn danger" @click="store.respondPermission(false, false)">拒绝</button>
        <button class="btn primary" @click="store.respondPermission(true, false)">允许一次 ⏎</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useAppStore } from '../stores/appStore';
const store = useAppStore();
</script>

<style scoped>
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  backdrop-filter: blur(2px);
}
.modal-mask.show {
  display: flex;
}
.modal {
  width: 480px;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6);
  overflow: hidden;
}
.modal-head {
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 10px;
}
.modal-head .mh-ico {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: rgba(240, 136, 62, 0.15);
  color: var(--orange);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
}
.modal-head h3 {
  font-size: 14.5px;
}
.modal-head p {
  font-size: 12px;
  color: var(--text-faint);
}
.modal-body {
  padding: 16px 18px;
  font-family: var(--mono);
  font-size: 12.5px;
  background: #0a0e14;
  white-space: pre-wrap;
  line-height: 1.65;
  color: var(--text-dim);
  max-height: 240px;
  overflow-y: auto;
}
.modal-foot {
  padding: 13px 18px;
  display: flex;
  gap: 9px;
  justify-content: flex-end;
}
.btn {
  border-radius: 8px;
  padding: 7px 16px;
  font-size: 13px;
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text);
  transition: 0.15s;
}
.btn:hover {
  border-color: var(--accent);
}
.btn.primary {
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  border: none;
  color: #fff;
  font-weight: 600;
}
.btn.danger {
  color: var(--red);
}
.btn.danger:hover {
  border-color: var(--red);
  background: rgba(248, 81, 73, 0.08);
}
</style>
