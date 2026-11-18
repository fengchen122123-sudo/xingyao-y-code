<template>
  <Transition name="confirm-modal">
    <div
      v-if="store.confirmModal.show"
      class="cm-mask"
      data-test="confirm-modal-mask"
      @mousedown.self="onMaskMousedown"
      @click.self="onMaskClick"
    >
      <div
        ref="cardRef"
        class="cm-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-modal-title"
        tabindex="-1"
        data-test="confirm-modal"
      >
        <div class="cm-head">
          <div class="cm-title-row">
            <span
              class="cm-icon"
              :class="`tone-${resolvedTone}`"
              :data-icon="iconVariant"
              aria-hidden="true"
            >
              <!-- 统一 20px 双色调极简线性图标，去除了厚重浅底色块与尖锐三角警示框 -->
              <svg
                class="cm-svg"
                viewBox="0 0 24 24"
                width="20"
                height="20"
                fill="none"
                stroke="currentColor"
                stroke-width="1.85"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <template v-if="iconVariant === 'trash'">
                  <path
                    d="M6.5 7 7.3 18.5Q7.45 20 9 20h6q1.55 0 1.7-1.5L17.5 7Z"
                    fill="currentColor"
                    fill-opacity="0.14"
                    stroke="none"
                  />
                  <path d="M4.5 7h15" />
                  <path d="M9.5 7V4.8A1.3 1.3 0 0 1 10.8 3.5h2.4a1.3 1.3 0 0 1 1.3 1.3V7" />
                  <path d="M6.5 7 7.3 18.5Q7.45 20 9 20h6q1.55 0 1.7-1.5L17.5 7" />
                  <path d="M10.2 11.2v4.6" />
                  <path d="M13.8 11.2v4.6" />
                </template>
                <template v-else-if="iconVariant === 'error'">
                  <circle cx="12" cy="12" r="9.25" fill="currentColor" fill-opacity="0.13" />
                  <path d="m14.7 9.3-5.4 5.4" />
                  <path d="m9.3 9.3 5.4 5.4" />
                </template>
                <template v-else-if="iconVariant === 'success'">
                  <circle cx="12" cy="12" r="9.25" fill="currentColor" fill-opacity="0.13" />
                  <path d="m8.6 12.2 2.25 2.25 4.55-4.8" />
                </template>
                <template v-else-if="iconVariant === 'alert'">
                  <circle cx="12" cy="12" r="9.25" fill="currentColor" fill-opacity="0.13" />
                  <path d="M12 8.1v4.2" />
                  <circle cx="12" cy="15.85" r="0.95" fill="currentColor" stroke="none" />
                </template>
                <template v-else>
                  <circle cx="12" cy="12" r="9.25" fill="currentColor" fill-opacity="0.13" />
                  <path d="M12 11.4v4.4" />
                  <circle cx="12" cy="8.15" r="0.95" fill="currentColor" stroke="none" />
                </template>
              </svg>
            </span>
            <h3 id="confirm-modal-title" class="cm-title">{{ displayTitle }}</h3>
          </div>
          <button
            type="button"
            class="cm-close"
            aria-label="关闭"
            data-test="confirm-modal-close"
            @click="onDismiss"
          >
            <AppIcon name="x" :size="15" />
          </button>
        </div>
        <p v-if="displaySub" class="cm-sub">{{ displaySub }}</p>
        <div v-if="store.confirmModal.message" class="cm-body">{{ store.confirmModal.message }}</div>

        <div class="cm-foot">
          <button
            v-if="!store.confirmModal.notice"
            ref="cancelBtnRef"
            type="button"
            class="cm-btn cm-btn-cancel"
            data-test="confirm-modal-cancel"
            @click="store.resolveConfirm(false)"
          >取消</button>
          <button
            ref="okBtnRef"
            type="button"
            class="cm-btn cm-btn-confirm"
            :class="`tone-${buttonTone}`"
            data-test="confirm-modal-ok"
            @click="store.resolveConfirm(true)"
          >{{ displayConfirmLabel }}</button>
        </div>
      </div>
    </div>
  </Transition>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore, type ConfirmTone } from '../stores/appStore';

type ResolvedTone = ConfirmTone | 'success';
type IconVariant = 'trash' | 'alert' | 'error' | 'success' | 'info';

const store = useAppStore();

const cardRef = ref<HTMLElement | null>(null);
const cancelBtnRef = ref<HTMLButtonElement | null>(null);
const okBtnRef = ref<HTMLButtonElement | null>(null);

let maskMouseDown = false;
let prevFocused: HTMLElement | null = null;

const resolvedTone = computed<ResolvedTone>(() => {
  if (store.confirmModal.tone) return store.confirmModal.tone;
  if (store.confirmModal.notice) {
    const combined = `${store.confirmModal.title}\n${store.confirmModal.message}`;
    if (/失败|错误|异常|不可用|无法|不能|问题|超时/.test(combined)) return 'error';
    if (/表单校验|警告|请先|请填写|请尝试/.test(combined)) return 'warning';
    if (/成功|已完成|已保存|已更新|已添加|已复制|已是最新|已恢复到会话列表/.test(combined)) return 'success';
    return 'info';
  }
  return 'danger';
});

const buttonTone = computed<'danger' | 'primary'>(() => {
  if (store.confirmModal.notice) return 'primary';
  return resolvedTone.value === 'danger' ? 'danger' : 'primary';
});

const isDeleteAction = computed(() => {
  if (resolvedTone.value !== 'danger') return false;
  const combined = `${store.confirmModal.title}\n${store.confirmModal.confirmLabel}`;
  return /删除|清空|移除/.test(combined);
});

const iconVariant = computed<IconVariant>(() => {
  if (resolvedTone.value === 'success') return 'success';
  if (resolvedTone.value === 'error') return 'error';
  if (resolvedTone.value === 'warning') return 'alert';
  if (resolvedTone.value === 'danger') return isDeleteAction.value ? 'trash' : 'alert';
  return 'info';
});

const displayTitle = computed(() => {
  return store.confirmModal.title || (store.confirmModal.notice ? '提示' : '确认操作');
});

const displaySub = computed(() => (store.confirmModal.sub || '').trim());

const displayConfirmLabel = computed(() => {
  const raw = store.confirmModal.confirmLabel || (store.confirmModal.notice ? '确定' : '确定删除');
  return raw.replace(/\s*⏎\s*$/, '');
});

function onDismiss() {
  store.resolveConfirm(false);
}

function onMaskMousedown() {
  maskMouseDown = true;
}

function onMaskClick() {
  const startedOnMask = maskMouseDown;
  maskMouseDown = false;
  if (!startedOnMask) return;
  // 通知类弹窗需显式点击按钮/关闭钮/Esc 确认，防止误触遮罩漏看提示
  if (!store.confirmModal.notice) {
    onDismiss();
  }
}

function onWindowKeydown(e: KeyboardEvent) {
  if (!store.confirmModal.show) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    e.stopPropagation();
    onDismiss();
    return;
  }
  if (e.key === 'Enter' && store.confirmModal.notice && document.activeElement === cardRef.value) {
    e.preventDefault();
    e.stopPropagation();
    store.resolveConfirm(true);
  }
}

watch(
  () => store.confirmModal.show,
  (open) => {
    maskMouseDown = false;
    if (open) {
      prevFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      void nextTick(() => {
        const target = store.confirmModal.notice || buttonTone.value === 'primary'
          ? okBtnRef.value
          : cancelBtnRef.value;
        (target ?? cardRef.value)?.focus();
      });
    } else if (prevFocused && prevFocused.isConnected) {
      prevFocused.focus();
      prevFocused = null;
    } else {
      prevFocused = null;
    }
  },
);

onMounted(() => {
  window.addEventListener('keydown', onWindowKeydown, true);
});

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onWindowKeydown, true);
});
</script>

<style scoped>
.cm-mask {
  position: fixed;
  inset: 0;
  z-index: 5500;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: var(--xy-modal-mask, rgba(0, 0, 0, 0.36));
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
}

.cm-card {
  width: min(416px, calc(100vw - 32px));
  background: var(--xy-card, var(--bg1));
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 16px;
  box-shadow: var(--xy-shadow-pop, 0 16px 40px rgba(0, 0, 0, 0.32));
  padding: 20px 22px 18px;
  color: var(--xy-text, var(--text));
  outline: none;
}

.cm-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.cm-title-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  flex: 1;
  min-width: 0;
}

.cm-icon {
  width: 20px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.cm-svg {
  display: block;
  width: 20px;
  height: 20px;
}
.cm-icon.tone-danger,
.cm-icon.tone-error {
  color: var(--xy-rose, var(--red));
}
.cm-icon.tone-warning {
  color: var(--xy-amber, var(--orange));
}
.cm-icon.tone-success {
  color: var(--xy-green, var(--green));
}
.cm-icon.tone-info,
.cm-icon.tone-primary {
  color: var(--xy-accent, var(--accent));
}

.cm-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  line-height: 22px;
  letter-spacing: -0.01em;
  color: var(--xy-text, var(--text-bright));
}

.cm-close {
  width: 26px;
  height: 26px;
  margin: -2px -6px 0 0;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--xy-text-3, var(--text-faint));
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  cursor: pointer;
  transition: background 0.14s ease, color 0.14s ease;
}
.cm-close:hover {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
}

.cm-sub {
  margin: 4px 0 0;
  font-size: 12.5px;
  line-height: 1.45;
  color: var(--xy-text-3, var(--text-faint));
}

.cm-body {
  margin-top: 12px;
  font-family: var(--sans);
  font-size: 13.5px;
  line-height: 1.6;
  color: var(--xy-text-2, var(--text-dim));
  white-space: pre-line;
  word-break: break-word;
  max-height: 46vh;
  overflow-y: auto;
}

.cm-foot {
  margin-top: 20px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

.cm-btn {
  height: 34px;
  padding: 0 15px;
  border-radius: 9px;
  font-size: 13px;
  font-weight: 500;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: background 0.14s ease, border-color 0.14s ease, color 0.14s ease, transform 0.1s ease, filter 0.14s ease;
}
.cm-btn:active {
  transform: scale(0.98);
}
.cm-btn:focus-visible,
.cm-close:focus-visible {
  outline: 2px solid var(--xy-accent, var(--accent));
  outline-offset: 2px;
}

.cm-btn-cancel {
  background: transparent;
  border: 1px solid var(--xy-border-strong, var(--border));
  color: var(--xy-text-2, var(--text));
}
.cm-btn-cancel:hover {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text-bright));
}

.cm-btn-confirm {
  padding: 0 16px;
  border: none;
  background: var(--xy-accent, var(--accent));
  color: var(--xy-accent-fg, #ffffff);
  box-shadow: var(--xy-shadow-sm, none);
}
.cm-btn-confirm:hover {
  filter: brightness(0.93);
}
.cm-btn-confirm.tone-danger {
  background: var(--xy-rose, var(--red));
  color: #ffffff;
}

.confirm-modal-enter-active,
.confirm-modal-leave-active {
  transition: opacity 0.18s ease;
}
.confirm-modal-enter-active .cm-card,
.confirm-modal-leave-active .cm-card {
  transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.18s ease;
}
.confirm-modal-enter-from,
.confirm-modal-leave-to {
  opacity: 0;
}
.confirm-modal-enter-from .cm-card,
.confirm-modal-leave-to .cm-card {
  opacity: 0;
  transform: translateY(6px) scale(0.97);
}

@media (max-width: 768px) {
  .cm-card {
    padding: 18px 18px 16px;
    border-radius: 14px;
  }
  .cm-btn {
    min-height: 38px;
  }
}
</style>
