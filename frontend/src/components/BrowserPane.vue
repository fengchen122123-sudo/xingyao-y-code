<template>
  <!-- 右侧面板「浏览器」工作台：DOM 工具栏 + 表面占位区。
       真实网页由宿主叠加的原生 WebView2 控件渲染在占位区矩形上，
       这里只负责工具栏交互、矩形上报与起始页/不支持态。 -->
  <div class="bp-root" data-test="browser-pane-root">
    <div class="bp-toolbar">
      <button
        class="bp-btn"
        data-test="bp-back"
        title="后退"
        :disabled="unsupported || !store.embeddedBrowser.can_go_back"
        @click="onBack"
      ><AppIcon name="chevron-left" :size="15" :stroke-width="2" /></button>

      <button
        class="bp-btn"
        data-test="bp-forward"
        title="前进"
        :disabled="unsupported || !store.embeddedBrowser.can_go_forward"
        @click="onForward"
      ><AppIcon name="chevron-right" :size="15" :stroke-width="2" /></button>

      <!-- loading 时变停止按钮并让图标旋转（lucide loader 弧线本就为旋转设计） -->
      <button
        class="bp-btn"
        data-test="bp-reload"
        :title="store.embeddedBrowser.loading ? '停止加载' : '刷新'"
        :disabled="unsupported"
        @click="onReloadOrStop"
      >
        <AppIcon
          :name="store.embeddedBrowser.loading ? 'loader' : 'refresh'"
          :size="15"
          :stroke-width="2"
          :class="{ 'icon-spin': store.embeddedBrowser.loading }"
        />
      </button>

      <input
        ref="addrInputRef"
        class="bp-addr"
        data-test="bp-addr"
        type="text"
        placeholder="搜索或输入网址"
        spellcheck="false"
        autocomplete="off"
        :disabled="unsupported"
        :value="displayUrl"
        @focus="onAddrFocus"
        @blur="onAddrBlur"
        @input="onAddrInput"
        @keydown="onAddrKeydown"
      />
    </div>

    <!-- 表面占位区：原生控件被宿主叠加在本矩形之上 -->
    <div ref="surfaceRef" class="bp-surface" data-test="bp-surface">
      <!-- 宿主不支持（非 Windows 桌面端 GUI 或缺桥）：工具栏已禁用 -->
      <div v-if="unsupported" class="bp-notice" data-test="bp-unsupported">
        <AppIcon name="globe" :size="26" :stroke-width="1.5" />
        <p class="bp-notice-title">浏览器面板需要桌面端 GUI 宿主支持</p>
        <p class="bp-notice-sub">内置浏览器仅在 Windows 桌面端 GUI 中可用。</p>
      </div>

      <!-- 起始页：没有页面时显示（此时原生表面被 hide，让出 DOM 起始页） -->
      <div v-else-if="isStartPage" class="bp-start" data-test="bp-start">
        <p class="bp-start-title">内置浏览器</p>
        <div class="bp-start-cards">
          <button
            v-for="site in QUICK_SITES"
            :key="site.key"
            class="bp-site"
            :data-test="'bp-site-' + site.key"
            @click="goTo(site.url)"
          >
            <AppIcon name="globe" :size="15" :stroke-width="2" />
            <span>{{ site.label }}</span>
          </button>
        </div>
      </div>

      <!-- 原生表面尚未就位（首帧 open 派发中/被临时隐藏）时的轻量占位 -->
      <div v-else-if="!surfaceVisible" class="bp-notice" data-test="bp-surface-pending">
        <AppIcon name="loader" :size="22" :class="{ 'icon-spin': true }" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';
import { useEmbeddedBrowserSurface } from '../composables/useEmbeddedBrowserSurface';

const props = defineProps<{ active: boolean }>();

const store = useAppStore();

const QUICK_SITES = [
  { key: 'baidu', label: '百度', url: 'https://www.baidu.com' },
  { key: 'bing', label: '必应', url: 'https://www.bing.com' },
  { key: 'github', label: 'GitHub', url: 'https://github.com' },
  { key: 'zhihu', label: '知乎', url: 'https://www.zhihu.com' },
  { key: 'bilibili', label: '哔哩哔哩', url: 'https://www.bilibili.com' },
];

function readApi(): any {
  try { return (window as any).pywebview?.api ?? null; } catch { return null; }
}

// 桥方法是否存在：pywebview 在应用挂载前注入，setup 期读一次即可；
// 缺失（如纯浏览器调试环境）即视为宿主不支持。
const apiReady = ref(typeof readApi()?.browser_pane_open === 'function');
// 平台不支持 = 桥缺失 或 宿主在任一次调用里回了 unsupported_platform
const unsupported = computed(() => !apiReady.value || !store.embeddedBrowser.supported);

function isBlankUrl(url: string): boolean {
  return !url || url === 'about:blank';
}

// 起始页条件：无页面地址且不在加载中（加载中的 about:blank 由原生表面接管）
const isStartPage = computed(() => {
  if (unsupported.value) return false;
  return isBlankUrl(String(store.embeddedBrowser.url || '')) && !store.embeddedBrowser.loading;
});

const surfaceRef = ref<HTMLElement | null>(null);

const surface = useEmbeddedBrowserSurface({
  isActive: () => props.active,
  // 起始页/不支持态时让宿主 hide：原生空白页会盖住 DOM 起始页与提示文案
  shouldShowSurface: () => !unsupported.value && !isStartPage.value,
  getSurfaceEl: () => surfaceRef.value,
  isUnsupported: () => unsupported.value,
  onUnsupported: () => { store.embeddedBrowser.supported = false; },
});
const surfaceVisible = surface.surfaceVisible;

// ── 地址栏 ──
const addrInputRef = ref<HTMLInputElement | null>(null);
const editing = ref(false);
const draft = ref('');

// 非聚焦显示当前地址（about:blank/空显示空串）；聚焦进入编辑态显示草稿
const displayUrl = computed(() => {
  if (editing.value) return draft.value;
  const url = String(store.embeddedBrowser.url || '');
  return isBlankUrl(url) ? '' : url;
});

function onAddrFocus() {
  const url = String(store.embeddedBrowser.url || '');
  draft.value = isBlankUrl(url) ? '' : url;
  editing.value = true;
  // 聚焦即全选：习惯「输入即替换」的用户不必先删旧地址
  void nextTick(() => addrInputRef.value?.select());
}

function onAddrInput(e: Event) {
  draft.value = (e.target as HTMLInputElement).value;
}

// 失焦未提交：退出编辑态即丢弃草稿，displayUrl 自动回落到 state.url
function onAddrBlur() {
  editing.value = false;
}

// IME 组合态守卫：中文输入法确认候选词的那次 Enter 会先到 keydown，
// 此刻文字尚未上屏（isComposing=true / keyCode 229）——若直接提交会送出旧值。
function isImeComposing(e: KeyboardEvent): boolean {
  return e.isComposing === true || (e as any).keyCode === 229;
}

function onAddrKeydown(e: KeyboardEvent) {
  if (e.key !== 'Enter') return;
  if (isImeComposing(e)) return; // 组合中交给输入法上屏
  e.preventDefault();
  const raw = draft.value.trim();
  editing.value = false;
  if (!raw) return; // 空输入忽略：不改动当前页面
  goTo(normalizeUrl(raw));
}

const SEARCH_PREFIX = 'https://www.bing.com/search?q=';

function searchUrl(query: string): string {
  return SEARCH_PREFIX + encodeURIComponent(query);
}

// 提交归一化（参照浏览器地址栏习惯）：
// - 已带 scheme（xx://）原样放行
// - 含空格、或首段无 '.' 且非 localhost/IP → 走必应搜索
// - 其余视为域名，补 https://
function normalizeUrl(input: string): string {
  const text = input.trim();
  if (!text) return '';
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) return text;
  if (/\s/.test(text)) return searchUrl(text);
  const firstSegment = text.split('/')[0];
  const host = firstSegment.split(':')[0];
  const isLocalhost = host.toLowerCase() === 'localhost';
  const isIp = /^\d{1,3}(\.\d{1,3}){3}$/.test(host);
  if (firstSegment.includes('.') || isLocalhost || isIp) return 'https://' + text;
  return searchUrl(text);
}

/** 导航到目标地址：乐观更新地址栏，再驱动原生表面 */
function goTo(url: string) {
  if (!url || unsupported.value) return;
  // 乐观更新：立即从起始页切到「表面」态（原生控件随即被创建/接管）
  store.embeddedBrowser.url = url;
  surface.navigate(url);
}

// ── 工具栏按钮 ──
function onBack() { surface.request('browser_pane_back'); }
function onForward() { surface.request('browser_pane_forward'); }
function onReloadOrStop() {
  if (store.embeddedBrowser.loading) surface.request('browser_pane_stop');
  else surface.request('browser_pane_reload');
}

// 挂载后校准一次：宿主可能已在别处创建过控件（如窗口重载后），
// 用 get_state 把地址栏/前进后退能力拉回真实值
async function calibrate() {
  try {
    const res = await readApi()?.browser_pane_get_state?.();
    if (res && res.ok && res.state) {
      store.applyEmbeddedBrowserState(res.state);
    } else if (res && res.ok === false && res.error === 'unsupported_platform') {
      store.embeddedBrowser.supported = false;
    }
  } catch { /* 桥未就绪：保持空态 */ }
}

onMounted(() => {
  // 再校正一次（兼容注入晚于 setup 的极端情况）
  apiReady.value = typeof readApi()?.browser_pane_open === 'function';
  if (!apiReady.value) { store.embeddedBrowser.supported = false; return; }
  void calibrate();
});

// 桥缺失导致的不支持态：同步到 store，供模板与工具栏复用同一判据
watch(apiReady, (ready) => { if (!ready) store.embeddedBrowser.supported = false; });
</script>

<style scoped>
.bp-root {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  width: 100%;
}
.bp-toolbar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.bp-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 6px;
  border: none;
  background: none;
  color: var(--text-dim);
  cursor: pointer;
  transition: 0.15s;
  flex-shrink: 0;
}
.bp-btn:hover:not(:disabled) {
  background: var(--bg3);
  color: var(--text);
}
.bp-btn:disabled {
  opacity: 0.4;
  cursor: default;
}
.bp-addr {
  flex: 1;
  min-width: 0;
  height: 26px;
  padding: 0 10px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text);
  font-size: 12.5px;
  font-family: inherit;
  outline: none;
}
.bp-addr:focus {
  border-color: var(--accent);
}
.bp-addr:disabled {
  opacity: 0.5;
}
/* 表面占位区：原生 WebView2 被宿主叠加在本矩形之上 */
.bp-surface {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow: hidden;
  background: var(--bg1);
}
.bp-start {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 28px 16px;
}
.bp-start-title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-dim);
}
.bp-start-cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bp-site {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text);
  font-size: 13px;
  font-weight: 600;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: 0.15s;
}
.bp-site:hover {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent);
}
.bp-notice {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 24px;
  text-align: center;
  color: var(--text-faint);
}
.bp-notice-title {
  margin: 4px 0 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-dim);
}
.bp-notice-sub {
  margin: 0;
  font-size: 12px;
}
</style>
