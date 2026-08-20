<template>
  <div class="modal-mask" :class="{ show: store.updateState.showUpdateModal }" @click.self="store.closeUpdateModal()">
    <div class="modal update-modal">
      <div class="modal-head">
        <div class="mh-ico">
          <AppIcon name="sparkles" :size="18" />
        </div>
        <div class="head-text">
          <div class="title-row">
            <h3>{{ modalTitle }}</h3>
            <span class="version-badge">v{{ store.updateState.latestVersion }}</span>
          </div>
          <p>当前运行版本：v{{ store.version || store.updateState.currentVersion || '1.2.5' }}</p>
        </div>
        <button class="close-btn" title="关闭" @click="store.closeUpdateModal()">
          <AppIcon name="x" :size="16" />
        </button>
      </div>

      <div class="modal-body">
        <div class="release-info-section">
          <div class="section-label">更新日志</div>
          <div class="release-notes-box" v-if="formattedNotes.length">
            <ul>
              <li v-for="(line, idx) in formattedNotes" :key="idx">{{ line }}</li>
            </ul>
          </div>
          <div class="release-notes-box empty" v-else>
            {{ store.updateState.releaseNotes || '性能优化与体验改进。' }}
          </div>
        </div>

        <div class="asset-meta" v-if="store.updateState.assetSize">
          <span>安装包大小：{{ formatSize(store.updateState.assetSize) }}</span>
          <span v-if="store.updateState.assetName" class="asset-name">({{ store.updateState.assetName }})</span>
        </div>

        <!-- 下载中进度条 -->
        <div v-if="store.updateState.status === 'downloading'" class="download-progress-box">
          <div class="progress-info">
            <span>正在下载新版本安装包...</span>
            <span class="mono">{{ store.updateState.downloadProgress }}%</span>
          </div>
          <div class="progress-bar-bg">
            <div class="progress-bar-fill" :style="{ width: store.updateState.downloadProgress + '%' }"></div>
          </div>
          <div class="progress-sub" v-if="store.updateState.totalBytes">
            <span>{{ formatSize(store.updateState.downloadedBytes) }} / {{ formatSize(store.updateState.totalBytes) }}</span>
          </div>
        </div>

        <!-- 下载完成提示 -->
        <div v-else-if="store.updateState.status === 'downloaded'" class="downloaded-tip">
          <AppIcon name="check-circle" :size="16" class="tip-icon-success" />
          <span>安装包已就绪，安装完成后将自动清理安装包。</span>
        </div>
      </div>

      <div class="modal-foot">
        <!-- 未下载状态 -->
        <template v-if="store.updateState.status === 'available' || store.updateState.status === 'idle'">
          <button class="btn" @click="store.closeUpdateModal()">稍后提醒</button>
          <button class="btn primary" @click="store.startDownloadUpdate()">立即下载更新</button>
        </template>

        <!-- 下载中状态 -->
        <template v-else-if="store.updateState.status === 'downloading'">
          <button class="btn danger" @click="store.cancelDownloadUpdate()">取消下载</button>
          <button class="btn primary" @click="store.closeUpdateModal()">后台下载</button>
        </template>

        <!-- 下载完成状态 -->
        <template v-else-if="store.updateState.status === 'downloaded'">
          <button class="btn" @click="store.closeUpdateModal()">稍后安装</button>
          <button class="btn primary install-btn" @click="handleInstall">立即安装并重启</button>
        </template>

        <!-- 安装中状态 -->
        <template v-else-if="store.updateState.status === 'installing'">
          <button class="btn primary" disabled>正在启动安装向导...</button>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();

const modalTitle = computed(() => {
  if (store.updateState.status === 'downloaded') return '新版本已就绪';
  if (store.updateState.status === 'downloading') return '正在下载更新';
  return store.updateState.releaseTitle || `发现新版本 v${store.updateState.latestVersion}`;
});

const formattedNotes = computed(() => {
  const raw = store.updateState.releaseNotes || '';
  return raw
    .split('\n')
    .map(line => line.trim())
    .filter(line => line.length > 0)
    .map(line => line.replace(/^[-*•]\s*/, ''));
});

function formatSize(bytes: number): string {
  if (!bytes) return '0 B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function handleInstall() {
  store.closeUpdateModal();
  store.handleInstallClick();
}
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
.update-modal {
  width: 520px;
  max-width: 90vw;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6);
  overflow: hidden;
  animation: pop 0.2s cubic-bezier(0.16, 1, 0.3, 1);
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
  gap: 12px;
  position: relative;
}
.modal-head .mh-ico {
  width: 34px;
  height: 34px;
  border-radius: 9px;
  background: var(--accent-soft);
  color: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.head-text {
  flex: 1;
  min-width: 0;
}
.title-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.title-row h3 {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
}
.version-badge {
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 10px;
  font-family: var(--mono);
}
.head-text p {
  font-size: 12px;
  color: var(--text-faint);
  margin: 3px 0 0;
}
.close-btn {
  background: transparent;
  border: none;
  color: var(--text-faint);
  cursor: pointer;
  padding: 4px;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: 0.15s;
}
.close-btn:hover {
  background: var(--bg3);
  color: var(--text);
}

.modal-body {
  padding: 18px;
  font-size: 13px;
  background: var(--bg2);
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.section-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-faint);
  margin-bottom: 6px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.release-notes-box {
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 14px;
  max-height: 180px;
  overflow-y: auto;
  line-height: 1.6;
  color: var(--text);
}
.release-notes-box ul {
  margin: 0;
  padding-left: 16px;
}
.release-notes-box li {
  margin-bottom: 4px;
}
.release-notes-box li:last-child {
  margin-bottom: 0;
}
.release-notes-box.empty {
  color: var(--text-faint);
  font-style: italic;
}

.asset-meta {
  font-size: 12px;
  color: var(--text-faint);
  display: flex;
  gap: 6px;
  align-items: center;
}
.asset-name {
  font-family: var(--mono);
  opacity: 0.8;
}

/* 下载进度条 */
.download-progress-box {
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.progress-info {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  font-weight: 500;
}
.progress-bar-bg {
  height: 8px;
  background: var(--bg3);
  border-radius: 4px;
  overflow: hidden;
}
.progress-bar-fill {
  height: 100%;
  background: var(--accent);
  border-radius: 4px;
  transition: width 0.2s ease;
}
.progress-sub {
  font-size: 11px;
  color: var(--text-faint);
  text-align: right;
  font-family: var(--mono);
}

.downloaded-tip {
  display: flex;
  align-items: center;
  gap: 8px;
  background: rgba(35, 134, 54, 0.1);
  border: 1px solid rgba(35, 134, 54, 0.25);
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 12.5px;
  color: var(--text);
}
.tip-icon-success {
  color: #238636;
  flex-shrink: 0;
}

.modal-foot {
  padding: 13px 18px;
  display: flex;
  gap: 10px;
  justify-content: flex-end;
  border-top: 1px solid var(--border);
  background: var(--bg1);
}
.btn {
  border-radius: 8px;
  padding: 7px 16px;
  font-size: 13px;
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text);
  cursor: pointer;
  transition: 0.15s;
}
.btn:hover {
  border-color: var(--text-faint);
}
.btn.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
  font-weight: 600;
}
.btn.primary:hover {
  opacity: 0.9;
}
.btn.primary.install-btn {
  background: #238636;
  border-color: #238636;
}
.btn.danger {
  background: var(--red);
  border-color: var(--red);
  color: #fff;
}
.mono {
  font-family: var(--mono);
}
</style>
