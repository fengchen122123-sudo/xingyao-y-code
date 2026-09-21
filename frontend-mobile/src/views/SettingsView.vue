<template>
  <div class="page">
    <van-nav-bar title="设置" fixed placeholder safe-area-inset-top>
      <template #left>
        <img class="nvlogo" :src="logoUrl" alt="星瑶" />
      </template>
    </van-nav-bar>
    <div class="body">
      <div class="about">
        <img class="about-logo" :src="logoUrl" alt="星瑶" />
        <div class="about-txt">
          <div class="about-name">星瑶<span class="mono ver">v{{ store.version || '—' }}</span></div>
          <div class="about-sub">Y-code 移动端 · 与客户端功能全量对齐</div>
        </div>
      </div>

      <div class="wx-caption">连接</div>
      <div class="wx-group">
        <div class="wx-cell">
          <span class="k">状态</span>
          <StatusTag :status="store.connected ? 'connected' : 'error'" :label="store.connected ? '已连接' : '离线'" />
        </div>
        <div class="wx-cell"><span class="k">星瑶版本</span><span class="v mono clip">{{ store.version || '—' }}</span></div>
        <div class="wx-cell"><span class="k">当前会话</span><span class="v mono clip">{{ sessionTitle }}</span></div>
        <div class="wx-cell"><span class="k">工作区</span><span class="v mono clip">{{ store.currentWorkdir || '自由对话' }}</span></div>
        <div class="wx-cell link" @click="goSessionOps">
          <span class="k">模型与权限</span>
          <span class="v">在会话操作中切换 <svg class="i sm"><use href="#i-chev-r" /></svg></span>
        </div>
      </div>

      <div class="wx-caption">外观</div>
      <div class="wx-group">
        <div class="wx-cell link" @click="store.applyTheme(store.theme === 'dark' ? 'light' : 'dark')">
          <span class="k">主题</span>
          <span class="v">{{ store.theme === 'dark' ? '暗色' : '亮色' }} <svg class="i sm"><use href="#i-chev-r" /></svg></span>
        </div>
      </div>

      <div class="tip">模型列表与权限档位与桌面端实时同步；在会话列表长按会话即可切换。</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useRouter } from 'vue-router';
import { useAppStore } from '../stores/app';
import StatusTag from '../components/StatusTag.vue';

// 星瑶品牌 logo（public 静态资源，运行时引用；/m/ 前缀与 lan_server 托管路径一致）
const logoUrl = '/m/icon.png';

const store = useAppStore();
const router = useRouter();

const sessionTitle = computed(() => {
  const s = store.flatSessions.find((x) => x.name === store.currentSession);
  return s?.display_name || store.currentSession || '—';
});

function goSessionOps() {
  // 会话操作（模型/权限切换）入口：回到会话详情页打开 ⋯ 菜单
  void router.push(`/convo/${encodeURIComponent(store.currentSession)}`);
}
</script>

<style scoped>
.page { height: 100dvh; display: flex; flex-direction: column; background: var(--bg); }
.nvlogo { width: 22px; height: 22px; border-radius: 6px; object-fit: cover; box-shadow: 0 0 8px var(--accent-soft); }
.body { flex: 1; min-height: 0; overflow-y: auto; padding-bottom: calc(16px + env(safe-area-inset-bottom)); }
.about { display: flex; align-items: center; gap: 13px; padding: 16px 4px 6px; }
.about-logo {
  width: 46px; height: 46px; border-radius: 12px; object-fit: cover;
  box-shadow: 0 0 24px var(--accent-soft), 0 0 10px var(--accent-soft);
  animation: logo-pulse 1.8s ease-in-out infinite;
}
@keyframes logo-pulse {
  0%, 100% { box-shadow: 0 0 14px var(--accent-soft); }
  50% { box-shadow: 0 0 26px var(--accent-soft), 0 0 10px var(--accent-soft); }
}
.about-name { font-size: 17px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
.about-name .ver { font-size: 11px; color: var(--accent); font-weight: 500; }
.about-sub { font-size: 11px; color: var(--text-3); margin-top: 2px; }
.wx-cell .k { color: var(--text-2); flex: 1; }
.wx-cell .v { color: var(--text-1); display: flex; align-items: center; gap: 4px; min-width: 0; }
.v.mono, .v.clip { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 60%; }
.tip { font-size: var(--fs-meta); color: var(--text-3); padding: 6px 4px; }
</style>
