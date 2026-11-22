<template>
  <div v-if="store.remoteDirBrowser.show" class="rdb-mask" data-test="remote-dir-browser" @click.self="store.closeRemoteDirBrowser()">
    <div class="rdb-card">
      <div class="rdb-head">
        <span class="rdb-title">{{ store.remoteDirBrowser.plainPick ? '选择工作目录' : '选择项目文件夹' }}</span>
        <button class="rdb-close" data-test="rdb-close" @click="store.closeRemoteDirBrowser()">✕</button>
      </div>

      <div class="rdb-pathbar">
        <button
          v-if="!store.remoteDirBrowser.isRoot"
          class="rdb-up"
          data-test="rdb-up"
          @click="goUp"
        >⬆ 上一级</button>
        <span class="rdb-path" data-test="rdb-current">{{ store.remoteDirBrowser.current || '我的电脑' }}</span>
      </div>

      <div class="rdb-list">
        <div v-if="store.remoteDirBrowser.loading" class="rdb-hint">读取中…</div>
        <div v-else-if="store.remoteDirBrowser.error" class="rdb-hint rdb-err">{{ store.remoteDirBrowser.error }}</div>
        <div v-else-if="store.remoteDirBrowser.dirs.length === 0" class="rdb-hint">没有子文件夹了</div>
        <div
          v-for="d in store.remoteDirBrowser.dirs"
          :key="d.path"
          class="rdb-item"
          :class="{ selected: store.remoteDirBrowser.selected === d.path }"
          @click="enterDir(d)"
        >
          <span class="rdb-icon">📁</span>
          <span class="rdb-name">{{ d.name }}</span>
          <button
            class="rdb-pick"
            :data-test="'rdb-select-' + d.name"
            @click.stop="store.remoteDirBrowser.selected = store.remoteDirBrowser.selected === d.path ? '' : d.path"
          >{{ store.remoteDirBrowser.selected === d.path ? '✓ 已选' : '选这个' }}</button>
        </div>
      </div>

      <div class="rdb-foot">
        <span class="rdb-selected-path" data-test="rdb-selected">{{ store.remoteDirBrowser.selected || '未选择（当前目录可作为工作区）' }}</span>
        <button
          class="rdb-confirm"
          data-test="rdb-confirm"
          :disabled="store.remoteDirBrowser.loading"
          @click="onConfirm"
        >{{ store.remoteDirBrowser.selected
            ? (store.remoteDirBrowser.plainPick ? '选定该文件夹' : '打开选中的文件夹')
            : '使用当前目录' }}</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useAppStore } from '../stores/appStore';

const store = useAppStore();

function enterDir(d: { name: string; path: string }) {
  store.browseRemoteDirs(d.path);
}

function goUp() {
  const cur = store.remoteDirBrowser.current;
  if (!cur) return;
  const norm = cur.replace(/[\\/]+$/, '');
  const idx = Math.max(norm.lastIndexOf('\\'), norm.lastIndexOf('/'));
  if (idx <= 2 && /^[A-Za-z]:/.test(norm)) {
    store.browseRemoteDirs('');
  } else if (idx > 0) {
    store.browseRemoteDirs(norm.slice(0, idx));
  } else {
    store.browseRemoteDirs('');
  }
}

function onConfirm() {
  // 勾选了子目录就打开它；否则把当前浏览的目录作为工作区
  store.confirmRemoteWorkspace(!store.remoteDirBrowser.selected);
}
</script>

<style scoped>
.rdb-mask {
  position: fixed; inset: 0; z-index: 5400;
  background: var(--xy-modal-mask, rgba(0, 0, 0, 0.36));
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  display: flex; align-items: center; justify-content: center;
  padding: 16px;
}
.rdb-card {
  width: min(560px, 94vw); max-height: 80vh;
  background: var(--xy-card, var(--bg1, #fff)); color: var(--xy-text, var(--text, #222));
  border: 1px solid var(--xy-border-strong, var(--border, #eee));
  border-radius: 16px; display: flex; flex-direction: column;
  box-shadow: var(--xy-shadow-pop, 0 12px 40px rgba(0, 0, 0, 0.25));
  overflow: hidden;
}
.rdb-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px; border-bottom: 1px solid var(--xy-border, #eee);
}
.rdb-title { font-weight: 600; font-size: 15px; }
.rdb-close {
  border: none; background: transparent; font-size: 15px;
  cursor: pointer; color: var(--xy-text-3, #999); padding: 4px 8px; border-radius: 6px;
}
.rdb-close:hover { background: var(--xy-hover, rgba(0, 0, 0, 0.05)); color: var(--xy-text, #222); }
.rdb-pathbar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 18px; border-bottom: 1px solid var(--xy-border, #eee);
}
.rdb-up {
  border: 1px solid var(--xy-border-strong, #ddd); background: transparent;
  border-radius: 8px; padding: 4px 10px; font-size: 12px; cursor: pointer;
  color: var(--xy-text, #222); white-space: nowrap;
}
.rdb-up:hover { background: var(--xy-hover, rgba(0, 0, 0, 0.05)); }
.rdb-path {
  font-size: 12px; color: var(--xy-text-3, #888);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  direction: rtl; text-align: left;
}
.rdb-list { flex: 1; overflow-y: auto; padding: 6px 10px; min-height: 200px; }
.rdb-hint { padding: 24px 0; text-align: center; color: var(--xy-text-3, #999); font-size: 13px; }
.rdb-err { color: var(--xy-rose, #d33); }
.rdb-item {
  display: flex; align-items: center; gap: 8px;
  padding: 9px 10px; border-radius: 8px; font-size: 13.5px;
  cursor: pointer;
}
.rdb-item:hover { background: var(--xy-hover, rgba(0, 0, 0, 0.04)); }
.rdb-item:active { background: var(--xy-hover, rgba(0, 0, 0, 0.05)); }
.rdb-item.selected { background: var(--xy-accent-soft, rgba(64, 128, 255, 0.12)); }
.rdb-icon { flex: none; }
.rdb-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rdb-pick {
  flex: none; border: 1px solid var(--xy-border-strong, #ddd);
  background: transparent; border-radius: 999px;
  padding: 3px 10px; font-size: 12px; cursor: pointer;
  color: var(--xy-text, #222);
}
.rdb-foot {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 18px; border-top: 1px solid var(--xy-border, #eee);
}
.rdb-selected-path {
  flex: 1; font-size: 11.5px; color: var(--xy-text-3, #888);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.rdb-confirm {
  flex: none; border: none; border-radius: 9px;
  background: var(--xy-accent, var(--accent, #3b82f6)); color: var(--xy-accent-fg, #fff);
  padding: 8px 16px; font-size: 13px; font-weight: 500; cursor: pointer;
}
.rdb-confirm:disabled { opacity: 0.5; }
</style>
