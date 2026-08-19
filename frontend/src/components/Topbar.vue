<template>
  <div id="topbar">
    <button class="icon-btn" id="btn-toggle-side" title="切换侧栏" @click="store.toggleSidebar()">
      <AppIcon name="menu" :size="17" />
    </button>
    <div class="logo" :title="store.version ? ('Y-CODE 星瑶 v' + store.version) : 'Y-CODE 星瑶'">
      <img class="mark" src="/icon.png" alt="Y-CODE">
      <div><span class="name">Y-CODE</span></div>
    </div>
    <div class="top-spacer"></div>

    <button class="icon-btn" id="btn-theme" title="切换主题" @click="store.toggleTheme()">
      <AppIcon :name="store.themeLight ? 'sun' : 'moon'" :size="16" />
    </button>
    <button class="icon-btn" id="btn-toggle-right" title="切换面板" @click="store.toggleRightPanel()">
      <AppIcon name="panel-right" :size="16" />
    </button>
  </div>
</template>

<script setup lang="ts">
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();
</script>

<style scoped>
#topbar {
  height: 48px;
  background: var(--bg1);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 14px;
  gap: 12px;
  flex-shrink: 0;
}
#topbar .logo {
  display: flex;
  align-items: center;
  gap: 9px;
  user-select: none;
}
#topbar .logo .mark {
  width: 28px;
  height: 28px;
  border-radius: 7px;
  object-fit: cover;
  display: block;
}
#topbar .logo .name {
  font-weight: 600;
  letter-spacing: 0.5px;
}
#topbar .logo .sub {
  color: var(--text-faint);
  font-size: 12px;
}
.top-spacer {
  flex: 1;
}
</style>
