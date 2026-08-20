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

    <!-- 软件更新提示按钮：发现新版本或下载就绪时展示 -->
    <button
      v-if="store.updateState.hasUpdate"
      class="update-topbar-btn"
      :class="{
        'is-downloaded': store.updateState.status === 'downloaded',
        'is-downloading': store.updateState.status === 'downloading'
      }"
      @click="handleUpdateClick"
      :title="topbarUpdateTitle"
    >
      <AppIcon v-if="store.updateState.status === 'downloaded'" name="check-circle" :size="14" />
      <AppIcon v-else name="sparkles" :size="14" />
      <span class="btn-label">{{ topbarUpdateLabel }}</span>
      <span v-if="store.updateState.status === 'downloading'" class="progress-tag">{{ store.updateState.downloadProgress }}%</span>
    </button>

    <button class="icon-btn" id="btn-theme" title="切换主题" @click="store.toggleTheme()">
      <AppIcon :name="store.themeLight ? 'sun' : 'moon'" :size="16" />
    </button>
    <button class="icon-btn" id="btn-toggle-right" title="切换面板" @click="store.toggleRightPanel()">
      <AppIcon name="panel-right" :size="16" />
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();

const topbarUpdateLabel = computed(() => {
  if (store.updateState.status === 'downloaded') return '立即安装更新';
  if (store.updateState.status === 'downloading') return '下载中';
  return `发现新版本 v${store.updateState.latestVersion}`;
});

const topbarUpdateTitle = computed(() => {
  if (store.updateState.status === 'downloaded') return '安装包已就绪，点击开始安装';
  if (store.updateState.status === 'downloading') return `正在下载：${store.updateState.downloadProgress}%`;
  return `有新版本可用 (v${store.updateState.latestVersion})，点击查看详情`;
});

function handleUpdateClick() {
  if (store.updateState.status === 'downloaded') {
    store.handleInstallClick();
  } else {
    store.openUpdateModal();
  }
}
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

/* 更新提示按钮 */
.update-topbar-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 11px;
  border-radius: 20px;
  border: 1px solid var(--accent);
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: 0.2s cubic-bezier(0.16, 1, 0.3, 1);
  animation: pulse-border 2.5s infinite;
}
.update-topbar-btn:hover {
  background: var(--accent);
  color: #fff;
  transform: translateY(-1px);
}
.update-topbar-btn.is-downloaded {
  border-color: #238636;
  background: rgba(35, 134, 54, 0.15);
  color: #238636;
  animation: none;
}
.update-topbar-btn.is-downloaded:hover {
  background: #238636;
  color: #fff;
}
.update-topbar-btn.is-downloading {
  border-color: var(--accent);
  animation: none;
}
.progress-tag {
  background: rgba(0, 0, 0, 0.15);
  padding: 1px 5px;
  border-radius: 10px;
  font-size: 10.5px;
  font-family: var(--mono);
}

@keyframes pulse-border {
  0% {
    box-shadow: 0 0 0 0 rgba(var(--accent-rgb, 100, 100, 255), 0.4);
  }
  70% {
    box-shadow: 0 0 0 6px rgba(var(--accent-rgb, 100, 100, 255), 0);
  }
  100% {
    box-shadow: 0 0 0 0 rgba(var(--accent-rgb, 100, 100, 255), 0);
  }
}
</style>
