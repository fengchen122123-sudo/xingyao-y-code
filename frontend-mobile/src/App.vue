<script setup lang="ts">
import { computed, onMounted } from 'vue';
import { useRoute } from 'vue-router';
import { useAppStore } from './stores/app';

const store = useAppStore();
const route = useRoute();
const activeTab = computed(() => String(route.meta.tab || 'convo'));
const showTabbar = computed(() => !!route.meta.tab);

onMounted(() => {
  store.init().catch(() => {
    // init 失败时保持骨架页可交互：离线横幅提示重试
    store.ready = true;
  });
});

async function retry() {
  try {
    await store.reloadInitialState();
  } catch { /* 心跳持续重试 */ }
}
</script>

<template>
  <!-- 线性图标 sprite：与桌面 AppIcon 同风格（stroke 1.6 / 无填充 / currentColor） -->
  <svg style="display:none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <symbol id="i-chat" viewBox="0 0 24 24"><path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v8a2.5 2.5 0 0 1-2.5 2.5H9l-4.2 3.4c-.4.3-.8 0-.8-.4V6.5z"/></symbol>
    <symbol id="i-warn" viewBox="0 0 24 24"><path d="M12 3.5 21 19H3l9-15.5z"/><path d="M12 10v4.2"/><path d="M12 16.8v.2"/></symbol>
    <symbol id="i-sliders" viewBox="0 0 24 24"><path d="M4 8h9"/><circle cx="16.5" cy="8" r="2.5"/><path d="M20 16h-9"/><circle cx="7.5" cy="16" r="2.5"/></symbol>
    <symbol id="i-plus" viewBox="0 0 24 24"><path d="M5 12h14"/><path d="M12 5v14"/></symbol>
    <symbol id="i-chev-d" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></symbol>
    <symbol id="i-chev-r" viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></symbol>
    <symbol id="i-more" viewBox="0 0 24 24"><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></symbol>
    <symbol id="i-undo" viewBox="0 0 24 24"><path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></symbol>
    <symbol id="i-redo" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-6.2-8.56"/><path d="M21 3v5h-5"/></symbol>
    <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></symbol>
    <symbol id="i-x" viewBox="0 0 24 24"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></symbol>
    <symbol id="i-up2" viewBox="0 0 24 24"><path d="m17 11-5-5-5 5"/><path d="m17 18-5-5-5 5"/></symbol>
    <symbol id="i-clip" viewBox="0 0 24 24"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/></symbol>
    <symbol id="i-image" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 15-4.5-4.5L7 20"/></symbol>
    <symbol id="i-chip" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/><rect x="10" y="10" width="4" height="4"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></symbol>
    <symbol id="i-shield" viewBox="0 0 24 24"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/></symbol>
    <symbol id="i-pencil" viewBox="0 0 24 24"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></symbol>
    <symbol id="i-broom" viewBox="0 0 24 24"><path d="M19 3 13 9"/><path d="M9 13 4 18c-.8.8-.8 2 0 2.8.8.8 2 .8 2.8 0L12 16"/><path d="M11 7c2-2 5-3 8-3-1 3-2 6-4 8l-4-4z"/></symbol>
    <symbol id="i-download" viewBox="0 0 24 24"><path d="M12 3v11"/><path d="m7 10 5 5 5-5"/><path d="M4 20h16"/></symbol>
    <symbol id="i-folder" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></symbol>
    <symbol id="i-trash" viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></symbol>
    <symbol id="i-check" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></symbol>
    <symbol id="i-copy" viewBox="0 0 24 24"><rect x="8" y="8" width="14" height="14" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></symbol>
    <symbol id="i-stop" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></symbol>
    <symbol id="i-arrow-up" viewBox="0 0 24 24"><path d="M12 19V5"/><path d="m5 12 7-7 7 7"/></symbol>
    <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></symbol>
  </svg>

  <div class="app-root">
    <!-- 离线横幅：shim 无主动断线回调，由心跳探测驱动 -->
    <div v-if="!store.connected" class="conn-banner" @click="retry">
      <span class="conn-dot" />
      <span>与电脑连接中断，正在重连…（点按立即重试）</span>
    </div>

    <router-view v-slot="{ Component }">
      <keep-alive include="ConvoListView">
        <component :is="Component" />
      </keep-alive>
    </router-view>

    <van-tabbar v-if="showTabbar" :model-value="activeTab" placeholder safe-area-inset-bottom route>
      <van-tabbar-item to="/convo" name="convo">
        会话
        <template #icon><svg class="i tab" viewBox="0 0 24 24"><use href="#i-chat" /></svg></template>
      </van-tabbar-item>
      <van-tabbar-item to="/approvals" name="approvals">
        审批
        <template #icon>
          <van-badge :content="store.pendingPermCount || ''" :show-zero="false">
            <svg class="i tab" viewBox="0 0 24 24"><use href="#i-warn" /></svg>
          </van-badge>
        </template>
      </van-tabbar-item>
      <van-tabbar-item to="/settings" name="settings">
        设置
        <template #icon><svg class="i tab" viewBox="0 0 24 24"><use href="#i-sliders" /></svg></template>
      </van-tabbar-item>
    </van-tabbar>
  </div>
</template>

<style scoped>
.app-root { height: 100dvh; display: flex; flex-direction: column; }
.conn-banner {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  font-size: var(--fs-aux);
  color: var(--warn);
  background: color-mix(in srgb, var(--warn) 12%, var(--bg-panel));
  border-bottom: 1px solid color-mix(in srgb, var(--warn) 30%, transparent);
  z-index: 10;
}
.conn-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--warn);
  animation: wx-pulse 1.4s ease-in-out infinite;
  flex: none;
}
</style>
