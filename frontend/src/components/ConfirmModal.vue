<template>
  <div class="modal-mask" :class="{ show: store.confirmModal.show }">
    <div class="modal">
      <div class="modal-head">
        <div class="mh-ico">✕</div>
        <div>
          <h3>{{ store.confirmModal.title || '确认操作' }}</h3>
          <p>{{ store.confirmModal.sub || '请确认您的操作' }}</p>
        </div>
      </div>
      <div class="modal-body">
        {{ store.confirmModal.message }}
      </div>
      <div class="modal-foot">
        <button class="btn" @click="store.resolveConfirm(false)">取消 (Esc)</button>
        <button class="btn primary danger" @click="store.resolveConfirm(true)">{{ store.confirmModal.confirmLabel || '确定删除 ⏎' }}</button>
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
  animation: pop 0.2s;
}
@keyframes pop {
  from {
    transform: scale(0.95);
    opacity: 0;
  }
  to {
    transform: none;
    opacity: 1;
  }
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
  background: rgba(248, 81, 73, 0.15);
  color: var(--red);
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
  padding: 18px;
  font-family: var(--sans);
  font-size: 13px;
  background: var(--bg2);
  line-height: 1.6;
  color: var(--text);
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
  border-color: var(--text-faint);
}
.btn.danger {
  background: var(--red);
  border: none;
  color: #fff;
  font-weight: 600;
}
</style>
