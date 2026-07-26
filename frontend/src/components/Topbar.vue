<template>
  <div id="topbar">
    <button class="icon-btn" id="btn-toggle-side" title="切换侧栏" @click="store.toggleSidebar()">☰</button>
    <div class="logo" :title="store.version ? ('Y-CODE 星瑶 v' + store.version) : 'Y-CODE 星瑶'">
      <div class="mark">Y</div>
      <div><span class="name">Y-CODE</span> <span class="sub">星瑶 · Vue 3</span></div>
    </div>
    <div class="top-spacer"></div>

    <button class="icon-btn" id="btn-theme" title="切换主题" @click="store.toggleTheme()">
      {{ store.themeLight ? '☀️' : '🌙' }}
    </button>
    <button class="icon-btn" id="btn-toggle-right" title="切换面板" @click="store.toggleRightPanel()">▐</button>
  </div>
</template>

<script setup lang="ts">
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
  width: 26px;
  height: 26px;
  border-radius: 7px;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  color: #fff;
  font-size: 13px;
  box-shadow: 0 0 12px rgba(79, 140, 255, 0.4);
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
