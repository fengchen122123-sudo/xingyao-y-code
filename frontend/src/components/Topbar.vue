<template>
  <div id="topbar" data-theme-region="app_header">
    <button class="icon-btn no-drag" id="btn-toggle-side" title="切换侧栏" @click="store.toggleSidebar()">
      <AppIcon name="menu" :size="17" />
    </button>
    <div class="logo no-drag" :title="store.version ? ('Y-CODE 星瑶 v' + store.version) : 'Y-CODE 星瑶'">
      <img class="mark" :src="'/icon.png'" alt="Y-CODE">
      <div><span class="name">Y-CODE</span></div>
    </div>

    <!-- 标题栏拖拽区：mousedown 进系统原生移动循环（SC_MOVE+HTCAPTION），
         Aero Snap / 动画全部原生；双击最大化 -->
    <div
      ref="dragRegionRef"
      class="titlebar-drag"
      @mousedown.left="beginDrag"
      @dblclick="toggleMaximize"
    ></div>

    <!-- 软件更新提示按钮：发现新版本或下载就绪时展示 -->
    <button
      v-if="store.updateState.hasUpdate"
      class="update-topbar-btn no-drag"
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

    <button class="icon-btn no-drag" id="btn-theme" title="切换主题" @click="store.toggleTheme()">
      <AppIcon :name="store.themeLight ? 'sun' : 'moon'" :size="16" />
    </button>
    <button class="icon-btn no-drag" id="btn-toggle-right" title="切换面板" @click="store.toggleRightPanel()">
      <AppIcon name="panel-right" :size="16" />
    </button>

    <!-- 自绘窗口控制按钮（frameless）：颜色随主题，hover 态清晰 -->
    <div class="window-controls no-drag">
      <button class="win-btn" title="最小化" data-test="win-min" @click="winMinimize">—</button>
      <button class="win-btn" :title="maximized ? '还原' : '最大化'" data-test="win-max" @click="toggleMaximize">
        {{ maximized ? '❐' : '▢' }}
      </button>
      <button class="win-btn win-close" title="关闭" data-test="win-close" @click="winClose">✕</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();

// 自绘标题栏窗口控制：通过 pywebview 桥调用，窗体控制按钮颜色随主题
const maximized = ref(false);
const dragRegionRef = ref<HTMLElement | null>(null);
const api = () => (window as any).pywebview?.api;

async function toggleMaximize() {
  const result = await api()?.window_toggle_maximize?.();
  if (result?.ok) maximized.value = !!result.maximized;
}
async function beginDrag() {
  // 系统原生移动循环：Snap / 动画原生。桥调用会阻塞到拖拽结束，不 await 结果
  void api()?.window_begin_drag?.();
}
async function winMinimize() {
  await api()?.window_minimize?.();
}
function winClose() {
  // 关闭走 destroy 触发 closing 事件，由后端生命周期层做退出确认
  void api()?.window_close?.();
}

// 拖拽区矩形上报（物理像素）：HTCAPTION 区域由 WM_NCHITTEST 判定，窗口 resize
// 或布局变化时更新；客户区坐标系下窗口移动不影响矩形
let resizeObserver: ResizeObserver | null = null;
function reportDragRegion() {
  const el = dragRegionRef.value;
  if (!el) return;
  const rect = el.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  void api()?.notify_drag_region?.(
    Math.round(rect.x * dpr), Math.round(rect.y * dpr),
    Math.round(rect.width * dpr), Math.round(rect.height * dpr),
  );
}
onMounted(async () => {
  try {
    const result = await api()?.window_is_maximized?.();
    if (result?.ok) maximized.value = !!result.maximized;
  } catch {
    // 桥未就绪（浏览器开发态）时忽略
  }
  reportDragRegion();
  // jsdom 等环境无 ResizeObserver；窗口 resize 兜底上报
  if (typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(() => reportDragRegion());
    if (dragRegionRef.value) resizeObserver.observe(dragRegionRef.value);
  }
  window.addEventListener('resize', reportDragRegion);
  // 桥注入晚于组件挂载时首轮上报会丢失：load 后与延迟多次补报，确保登记
  window.addEventListener('load', reportDragRegion);
  for (const delay of [1000, 3000, 8000]) {
    window.setTimeout(reportDragRegion, delay);
  }
});
onBeforeUnmount(() => {
  resizeObserver?.disconnect();
  window.removeEventListener('resize', reportDragRegion);
  window.removeEventListener('load', reportDragRegion);
});

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

/* 自绘标题栏拖拽区：占据按钮之间的空隙，双击最大化；pywebview 据此拖动窗口 */
.titlebar-drag {
  flex: 1;
  align-self: stretch;
  min-width: 40px;
}

/* 自绘窗口控制按钮：颜色随主题文字色，关闭 hover 用危险色 */
.window-controls {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-left: 4px;
}
.win-btn {
  width: 40px;
  height: 32px;
  border: none;
  background: transparent;
  color: var(--text-dim);
  font-size: 14px;
  line-height: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.win-btn:hover {
  background: var(--bg2);
  color: var(--text);
}
.win-close:hover {
  background: var(--red);
  color: #fff;
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
