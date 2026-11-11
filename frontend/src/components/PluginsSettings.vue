<template>
  <div class="plugins-settings" :class="{ 'settings-page--mobile': isMobile }">
    <header class="settings-header">
      <div>
        <h1>插件</h1>
        <p>插件随安装/删除自动出现或消失；提供设置页的插件可在这里配置。</p>
      </div>
      <button class="icon-button" title="刷新插件列表" data-test="plugins-refresh" @click="store.refreshPlugins()">
        <AppIcon name="refresh" :size="14" />
      </button>
    </header>

    <section class="model-settings-card">
      <aside class="provider-list">
        <div class="list-label">已安装插件</div>
        <button
          v-for="p in store.plugins"
          :key="p.name"
          class="provider-item"
          :class="{ active: selectedName === p.name }"
          :data-test="`plugin-item-${p.name}`"
          @click="selectPlugin(p.name)"
        >
          <span class="provider-logo"><AppIcon name="layers" :size="13" /></span>
          <span class="provider-name">{{ p.name }}</span>
          <span class="plugin-badge" :class="'tone-' + badgeTone(p)">{{ badgeText(p) }}</span>
        </button>
        <div v-if="!store.plugins.length" class="provider-empty">
          暂无插件：把插件目录放入 ~/.ycode/plugins/ 后点右上角刷新
        </div>
      </aside>

      <!-- 详情区：插件设置页已抽成独立组件（供设置页左侧导航直接挂载指定插件） -->
      <PluginSettingsPanel v-if="selectedName" :name="selectedName" />
        <div v-if="!selectedName" class="provider-detail">
          <div class="provider-empty">选择左侧的插件查看它的设置页</div>
        </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import AppIcon from './AppIcon.vue';
import PluginSettingsPanel from './PluginSettingsPanel.vue';
import { useIsMobile } from '../composables/useViewport';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();

const selectedName = ref('');
// 与 SettingsPage 同口径：手机端列表/详情改单列堆叠（store 无移动端标记，用同一断点 composable）
const isMobile = useIsMobile();

function badgeText(p: any): string {
  if (p.status === 'untrusted') return '未信任';
  if (p.status === 'disabled') return '未启用';
  if (p.status === 'failed') return '加载失败';
  return '使用中';
}
function badgeTone(p: any): string {
  if (p.status === 'failed') return 'err';
  if (p.status === 'untrusted') return 'warn';
  if (p.status === 'disabled') return 'muted';
  return 'ok';
}

// 列表选择：详情面板由 PluginSettingsPanel 按 name 自行加载
function selectPlugin(name: string) {
  selectedName.value = name;
}

watch(
  () => (store.plugins || []).map((p: any) => p.name).join(','),
  (names) => {
    // 插件被删除/改名：当前选中项消失时清空详情（"删除即消失"）
    if (selectedName.value && !String(names).split(',').includes(selectedName.value)) {
      selectedName.value = '';
    }
  }
);
</script>

<style scoped>
.plugins-settings { max-width: 1120px; margin: 0 auto; }

/* 标题栏 / 刷新按钮 / 左列表右详情骨架：与 SettingsPage 同款视觉 */
.settings-header { display: flex; align-items: flex-end; justify-content: space-between; margin: 0 auto 26px; }
.settings-header h1 { margin: 0 0 10px; font-size: 32px; letter-spacing: -1px; }
.settings-header p { margin: 0; color: var(--text-faint); font-size: 13px; }
.icon-button { background: transparent; color: var(--text-dim); border: 0; font-size: 20px; }
.icon-button:hover { color: var(--text); }
.model-settings-card { display: grid; grid-template-columns: 280px minmax(0, 1fr); min-height: 610px; border: 1px solid var(--border); border-radius: 16px; background: var(--bg1); overflow: hidden; }
.provider-list { padding: 16px 12px; border-right: 1px solid var(--border); background: var(--bg2); overflow-y: auto; }
/* 未选中插件时的占位也需要详情区同款内边距（子组件根元素共享父组件 scoped 属性） */
.provider-detail { padding: 38px; }
.list-label { padding: 4px 10px 10px; color: var(--text-faint); font-size: 11px; font-weight: 600; }
.provider-empty { padding: 12px 10px; color: var(--text-faint); font-size: 11px; }
.provider-item { width: 100%; display: flex; align-items: center; gap: 9px; padding: 9px 10px; border: 1px solid transparent; border-radius: 9px; background: transparent; color: var(--text-dim); text-align: left; cursor: pointer; }
.provider-item:hover, .provider-item.active { background: var(--bg3); color: var(--text); }
.provider-item.active { border-color: var(--border); }
.provider-logo { width: 24px; height: 24px; display: grid; place-items: center; border-radius: 7px; background: var(--accent-soft); color: var(--accent); flex-shrink: 0; }
.provider-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }

/* 列表状态徽标（详情面板内的徽标样式随面板组件一起搬走） */
.plugin-badge { flex-shrink: 0; padding: 1px 8px; border: 1px solid var(--border); border-radius: 999px; background: var(--bg3); color: var(--text-faint); font-size: 10.5px; white-space: nowrap; }
.plugin-badge.tone-ok { color: var(--green); border-color: color-mix(in srgb, var(--green) 35%, var(--border)); }
.plugin-badge.tone-warn { color: var(--yellow); border-color: color-mix(in srgb, var(--yellow) 35%, var(--border)); }
.plugin-badge.tone-err { color: var(--red); border-color: color-mix(in srgb, var(--red) 35%, var(--border)); }
.plugin-badge.tone-muted { color: var(--text-faint); }

/* 手机端：列表/详情改单列堆叠（根节点由 useIsMobile 断点加上 settings-page--mobile） */
.plugins-settings.settings-page--mobile .settings-header { align-items: flex-start; }
.plugins-settings.settings-page--mobile .settings-header h1 { font-size: 24px; letter-spacing: -0.5px; }
.plugins-settings.settings-page--mobile .model-settings-card { grid-template-columns: 1fr; min-height: 0; }
.plugins-settings.settings-page--mobile .provider-list { border-right: 0; border-bottom: 1px solid var(--border); padding: 12px; }
.plugins-settings.settings-page--mobile .provider-detail { padding: 18px 16px 22px; }
</style>
