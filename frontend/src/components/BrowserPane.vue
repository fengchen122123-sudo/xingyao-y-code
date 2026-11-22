<template>
  <div class="bp-root" data-test="browser-pane-root">
    <div class="bp-toolbar">
      <button
        class="bp-btn"
        data-test="bp-back"
        title="后退"
        :disabled="unsupported || bridgeWaiting || !store.embeddedBrowser.can_go_back"
        @click="onBack"
      ><AppIcon name="chevron-left" :size="15" :stroke-width="2" /></button>

      <button
        class="bp-btn"
        data-test="bp-forward"
        title="前进"
        :disabled="unsupported || bridgeWaiting || !store.embeddedBrowser.can_go_forward"
        @click="onForward"
      ><AppIcon name="chevron-right" :size="15" :stroke-width="2" /></button>

      <button
        class="bp-btn"
        data-test="bp-reload"
        :title="store.embeddedBrowser.loading ? '停止加载' : '刷新'"
        :disabled="unsupported || bridgeWaiting"
        @click="onReloadOrStop"
      >
        <AppIcon
          :name="store.embeddedBrowser.loading ? 'loader' : 'refresh'"
          :size="15"
          :stroke-width="2"
          :class="{ 'icon-spin': store.embeddedBrowser.loading }"
        />
      </button>

      <button
        class="bp-btn"
        data-test="bp-size-toggle"
        title="自由尺寸"
        :disabled="unsupported || bridgeWaiting"
        :class="{ 'bp-btn-active': freeSize.on }"
        @click="toggleSizeMenu"
      >
        <AppIcon name="scan" :size="15" :stroke-width="2" />
      </button>

      <input
        ref="addrInputRef"
        class="bp-addr"
        data-test="bp-addr"
        type="text"
        placeholder="搜索或输入网址"
        spellcheck="false"
        autocomplete="off"
        :disabled="unsupported || bridgeWaiting"
        :value="displayUrl"
        @focus="onAddrFocus"
        @blur="onAddrBlur"
        @input="onAddrInput"
        @keydown="onAddrKeydown"
      />

      <button
        v-if="store.computerRunIsBrowser && !store.computerRunTerminal"
        class="bp-btn bp-btn-stop"
        data-test="bp-computer-stop"
        :disabled="store.computerControlMutating"
        title="停止当前浏览器操控（快捷键 Ctrl+Alt+Esc）"
        @click="stopBrowserControl"
      >
        <AppIcon name="square" :size="13" :stroke-width="2" />
      </button>
    </div>

    <div v-if="sizeMenuOpen" class="bp-size-menu" data-test="bp-size-menu">
      <div class="bp-size-presets">
        <button
          class="bp-size-chip"
          data-test="bp-size-fit"
          :class="{ active: !freeSize.on }"
          @click="applyFreeSize(null)"
        >适配面板</button>
        <button
          v-for="p in SIZE_PRESETS"
          :key="p.key"
          class="bp-size-chip"
          :data-test="'bp-size-' + p.key"
          :class="{ active: freeSize.on && freeSize.w === p.w && freeSize.h === p.h }"
          @click="applyFreeSize(p)"
        >{{ p.label }} {{ p.w }}×{{ p.h }}</button>
      </div>
      <div class="bp-size-custom">
        <input
          v-model.number="customW"
          class="bp-size-input"
          data-test="bp-size-w"
          type="number"
          min="120"
          max="8192"
          @keydown.enter="applyCustomSize"
        />
        <span class="bp-size-x">×</span>
        <input
          v-model.number="customH"
          class="bp-size-input"
          data-test="bp-size-h"
          type="number"
          min="200"
          max="8192"
          @keydown.enter="applyCustomSize"
        />
        <button class="bp-size-apply" data-test="bp-size-apply" @click="applyCustomSize">应用</button>
      </div>
      <p class="bp-size-hint">
        当前 {{ sizeLabel }}<template v-if="freeSize.on">（适应窗口 · 缩放 {{ zoomPct }}%）</template>；也可拖拽视口框右下角手柄
      </p>
    </div>

    <div ref="surfaceRef" class="bp-surface" data-test="bp-surface">
      <span
        class="bp-phase-marker"
        data-test="bp-surface-phase"
        :data-phase="surface.surfacePhase.value"
        aria-hidden="true"
      ></span>
      <div
        v-if="frameRenderable && !viewportConstrained"
        ref="frameRef"
        class="bp-frame"
        data-test="bp-frame"
        :style="{ width: frameW + 'px', height: frameH + 'px' }"
      >
        <div
          class="bp-frame-grip"
          data-test="bp-frame-grip"
          title="拖拽调整尺寸"
          @pointerdown="onGripDown"
          @pointermove="onGripMove"
          @pointerup="onGripUp"
          @pointercancel="onGripUp"
        ></div>
      </div>

      <div v-if="unsupported" class="bp-notice" data-test="bp-unsupported">
        <AppIcon name="globe" :size="26" :stroke-width="1.5" />
        <p class="bp-notice-title">浏览器面板需要桌面端 GUI 宿主支持</p>
        <p class="bp-notice-sub">内置浏览器仅在 Windows 桌面端 GUI 中可用。</p>
      </div>

      <!-- 桥尚未注入完成（启动竞态窗口，通常不足一秒）：等待而非判死 -->
      <div v-else-if="bridgeWaiting" class="bp-notice" data-test="bp-bridge-waiting">
        <AppIcon name="loader" :size="22" :class="{ 'icon-spin': true }" />
      </div>

      <div v-else-if="officePending" class="bp-notice" data-test="bp-office-pending">
        <AppIcon name="loader" :size="22" :class="{ 'icon-spin': true }" />
        <p class="bp-notice-title">正在把 {{ officePending.name }} 转换成预览页</p>
        <p class="bp-notice-sub">首次转换需要 10~25 秒，之后会命中缓存</p>
      </div>

      <div v-else-if="viewportConstrained" class="bp-constrained" data-test="bp-viewport-constrained">
        <div class="bp-constrained-banner">
          <strong>右栏空间不足，已暂停裁切式实时原生视图</strong>
          <p class="bp-notice-sub">
            {{ freeSize.w }} × {{ freeSize.h }} 在最小 {{ ZOOM_MIN }}× 缩放下至少需要
            {{ viewportMinRequired.width }} × {{ viewportMinRequired.height }} px 面板空间；请手动拖宽右栏查看完整实时画面。
          </p>
        </div>
        <img
          v-if="constrainedThumbUrl"
          class="bp-constrained-thumb"
          data-test="bp-viewport-thumb"
          :src="constrainedThumbUrl"
          alt="完整视口缩略图"
        />
      </div>

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

      <div v-else-if="surface.surfacePhase.value === 'error'" class="bp-notice" data-test="bp-surface-error">
        <AppIcon name="triangle-alert" :size="24" :stroke-width="1.6" />
        <p class="bp-notice-title">原生浏览器视图加载失败</p>
        <p class="bp-notice-sub">可尝试切换页签或点击刷新重试。</p>
      </div>

      <div v-else-if="!surfaceVisible || surface.surfacePhase.value === 'opening'" class="bp-notice" data-test="bp-surface-pending">
        <AppIcon name="loader" :size="22" :class="{ 'icon-spin': true }" />
        <p v-if="surface.surfacePhase.value === 'opening'" class="bp-notice-sub">正在准备内置浏览器视图…</p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';
import { useEmbeddedBrowserSurface } from '../composables/useEmbeddedBrowserSurface';

const props = defineProps<{ active: boolean }>();

const store = useAppStore();

function stopBrowserControl(): void {
  void store.stopComputerControl();
}

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

// ── 桥就绪判定（两步注入竞态防护，同 App.vue M25 的坑）──
// 本组件被 RightPanel 用 v-show 常驻挂载：setup/onMounted 跑在 pywebview
// 桥注入完成之前——api.js 先注入空 {}，finish.js 之后才把方法填进
// window.pywebview.api 并触发 pywebviewready。因此「此刻读不到桥方法」
// 绝不能当成「宿主不支持」锁死 supported：要等桥真正就绪，或宽限期内
// 始终无桥（纯浏览器调试等没有 pywebview 宿主的环境）才判缺失。
const BRIDGE_GRACE_MS = 5000;

type BridgePhase = 'waiting' | 'ready' | 'missing';
const bridgePhase = ref<BridgePhase>(
  typeof readApi()?.browser_pane_open === 'function' ? 'ready' : 'waiting',
);
const bridgeWaiting = computed(() => bridgePhase.value === 'waiting');
const unsupported = computed(
  () => bridgePhase.value === 'missing' || !store.embeddedBrowser.supported,
);

let readyPollTimer: ReturnType<typeof setInterval> | undefined;
let graceTimer: ReturnType<typeof setTimeout> | undefined;

function stopBridgeWait(): void {
  if (readyPollTimer !== undefined) { clearInterval(readyPollTimer); readyPollTimer = undefined; }
  if (graceTimer !== undefined) { clearTimeout(graceTimer); graceTimer = undefined; }
}

function onBridgeReady(): void {
  if (bridgePhase.value !== 'waiting') return;
  bridgePhase.value = 'ready';
  stopBridgeWait();
  window.removeEventListener('pywebviewready', onBridgeReady);
  void calibrate();
  if (pendingExternalUrl.value) {
    const url = pendingExternalUrl.value;
    pendingExternalUrl.value = '';
    goTo(url);
  }
}

function watchBridge(): void {
  if (bridgePhase.value !== 'waiting') return;
  if (typeof readApi()?.browser_pane_open === 'function') { onBridgeReady(); return; }
  // 双通道：pywebviewready 事件 + 100ms 兜底轮询（事件可能先于监听注册触发）
  window.addEventListener('pywebviewready', onBridgeReady);
  readyPollTimer = setInterval(() => {
    if (typeof readApi()?.browser_pane_open === 'function') onBridgeReady();
  }, 100);
  graceTimer = setTimeout(() => {
    window.removeEventListener('pywebviewready', onBridgeReady);
    stopBridgeWait();
    if (bridgePhase.value === 'waiting') bridgePhase.value = 'missing';
  }, BRIDGE_GRACE_MS);
}

function isBlankUrl(url: string): boolean {
  return !url || url === 'about:blank';
}

const isStartPage = computed(() => {
  if (unsupported.value) return false;
  return isBlankUrl(String(store.embeddedBrowser.url || '')) && !store.embeddedBrowser.loading;
});

// Office 文档转 PDF 进行中的提示对象（{path,name} | null）：模板据此显示
// 「正在把 xxx 转换成预览页」浮层，并让原生表面暂时让位（原生控件绘制在
// DOM 之上，否则浮层会被已打开的页面盖住）
const officePending = computed(() => store.officePreviewPending);

const surfaceRef = ref<HTMLElement | null>(null);

const SIZE_PRESETS = [
  { key: 'phone', label: '手机', w: 375, h: 667 },
  { key: 'tablet', label: '平板', w: 768, h: 1024 },
  { key: 'desktop', label: '桌面', w: 1280, h: 800 },
] as const;
const FREE_MIN_W = 120;
const FREE_MIN_H = 200;
const FREE_MAX = 8192;
const FREE_RING = 16;
const ZOOM_MIN = 0.25;
const ZOOM_MAX = 8;

const freeSize = ref({ on: false, w: 1280, h: 800 });
const sizeMenuOpen = ref(false);
const customW = ref<number | ''>(1280);
const customH = ref<number | ''>(800);
const frameRef = ref<HTMLElement | null>(null);

const frameRenderable = computed(
  () => freeSize.value.on && !unsupported.value && !bridgeWaiting.value
    && !isStartPage.value && !officePending.value,
);
const sizeLabel = computed(() =>
  freeSize.value.on ? `${freeSize.value.w} × ${freeSize.value.h}` : '适配面板',
);

// 适应窗口缩放：面板可绘区（扣除边距）与逻辑视口的内切比例。
// 表面不可测量（clientWidth 为 0，如测试环境）时不缩放，保持 1:1。
const fitScale = ref(1);
const surfaceSize = ref({ width: 0, height: 0 });
const zoomPct = computed(() => Math.round(fitScale.value * 100));

const viewportMinRequired = computed(() => ({
  width: Math.ceil(freeSize.value.w * ZOOM_MIN + FREE_RING * 2),
  height: Math.ceil(freeSize.value.h * ZOOM_MIN + FREE_RING * 2),
}));

const viewportConstrained = computed(() => {
  if (!freeSize.value.on || unsupported.value || bridgeWaiting.value || officePending.value) {
    return false;
  }
  const sw = surfaceSize.value.width;
  const sh = surfaceSize.value.height;
  if (sw <= 0 || sh <= 0) return false;
  const availW = sw - FREE_RING * 2;
  const availH = sh - FREE_RING * 2;
  return availW < freeSize.value.w * ZOOM_MIN || availH < freeSize.value.h * ZOOM_MIN;
});

const constrainedThumbUrl = computed(() => {
  if (!viewportConstrained.value) return '';
  return String(store.computerRun.preview_url || '');
});

function computeFitScale(w: number, h: number): number {
  const el = surfaceRef.value;
  const cw = el?.clientWidth ?? 0;
  const ch = el?.clientHeight ?? 0;
  surfaceSize.value = { width: cw, height: ch };
  const availW = cw - FREE_RING * 2;
  const availH = ch - FREE_RING * 2;
  if (availW <= 0 || availH <= 0 || w <= 0 || h <= 0) return 1;
  return Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.min(availW / w, availH / h)));
}

function updateFitScale() {
  if (!freeSize.value.on) {
    const el = surfaceRef.value;
    surfaceSize.value = { width: el?.clientWidth ?? 0, height: el?.clientHeight ?? 0 };
    fitScale.value = 1;
    return;
  }
  fitScale.value = computeFitScale(freeSize.value.w, freeSize.value.h);
}

const frameW = computed(() => Math.round(freeSize.value.w * fitScale.value));
const frameH = computed(() => Math.round(freeSize.value.h * fitScale.value));

const surface = useEmbeddedBrowserSurface({
  isActive: () => props.active,
  shouldShowSurface: () =>
    !store.rightPanelSnipActive &&
    !store.embeddedBrowserOverlayBlocking &&
    !viewportConstrained.value &&
    !unsupported.value &&
    !sizeMenuOpen.value &&
    // Office 转 PDF 期间先把原生表面收起：原生控件永远画在 DOM 之上，
    // 否则「正在转换」浮层会被上一页盖住（转换结束/失败后自动 re-show）
    !store.officePreviewPending &&
    (!isStartPage.value || !!store.embeddedBrowser.automationViewport),
  getSurfaceEl: () => frameRef.value || surfaceRef.value,
  getZoom: () => (frameRenderable.value && freeSize.value.on ? fitScale.value : 1),
  getSessionName: () => store.currentSessionName,
  // 原生控件永远绘制在 DOM 之上且不受 overflow:hidden 裁剪：上报矩形一旦
  // 越出占位区就会盖住会话区（自由尺寸框在占位区不可测量时兜底缩放=1，
  // 曾按原始 1440×900 铺满整个主区）。所有上报矩形一律夹紧回占位区内。
  getClampEl: () => surfaceRef.value,
  isUnsupported: () => unsupported.value,
  onUnsupported: () => { store.embeddedBrowser.supported = false; },
});
const surfaceVisible = surface.surfaceVisible;

const addrInputRef = ref<HTMLInputElement | null>(null);
const editing = ref(false);
const draft = ref('');

const displayUrl = computed(() => {
  if (editing.value) return draft.value;
  const url = String(store.embeddedBrowser.url || '');
  return isBlankUrl(url) ? '' : url;
});

function onAddrFocus() {
  const url = String(store.embeddedBrowser.url || '');
  draft.value = isBlankUrl(url) ? '' : url;
  editing.value = true;
  void nextTick(() => addrInputRef.value?.select());
}

function onAddrInput(e: Event) {
  draft.value = (e.target as HTMLInputElement).value;
}

function onAddrBlur() {
  editing.value = false;
}

function isImeComposing(e: KeyboardEvent): boolean {
  return e.isComposing === true || (e as any).keyCode === 229;
}

function onAddrKeydown(e: KeyboardEvent) {
  if (e.key !== 'Enter') return;
  if (isImeComposing(e)) return;
  e.preventDefault();
  const raw = draft.value.trim();
  editing.value = false;
  if (!raw) return;
  goTo(normalizeUrl(raw));
}

const SEARCH_PREFIX = 'https://www.bing.com/search?q=';

function searchUrl(query: string): string {
  return SEARCH_PREFIX + encodeURIComponent(query);
}

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

function goTo(url: string) {
  if (!url || unsupported.value || bridgeWaiting.value) return;
  store.embeddedBrowser.url = url;
  surface.navigate(url);
}

// ── 外部入口请求导航（对话正文 / 文件树点击 html）──
// store 里放一条导航请求 + nonce，这里消费它：桥未就绪先缓存（就绪后补，
// 见 onBridgeReady）；宿主不支持内嵌浏览器则回退源码预览弹窗——这条回退保证
// 点击 html 在任何宿主上都落得了地，不会「点了没反应」。
const pendingExternalUrl = ref('');

// 已消费的请求 nonce：页签激活/面板展开会再次触发消费，靠它避免重复导航
let consumedNavNonce = 0;

function consumeBrowserNavRequest() {
  const req = store.browserNavRequest;
  if (!req || !req.url) return;
  // 页签未激活（面板折叠/停在别的页签）：等 active 变 true 再消费，
  // 否则会对着 0 尺寸占位区 open，导航被丢弃
  if (!props.active) return;
  if (req.session && req.session !== store.currentSessionName) return;
  if (store.browserNavNonce === consumedNavNonce) return;
  if (unsupported.value) {
    // path 为空 = 不是本地文件请求（如账号控制台页面）：回退系统浏览器，
    // 否则 openFilePreview('') 只会弹一个读文件失败的错，点了等于没反应
    if (!req.path) {
      store.notifyChat('内嵌浏览器不可用，已改用系统浏览器打开');
      void store.openExternalUrl(req.url);
      return;
    }
    store.notifyChat('内嵌浏览器不可用，已改为源码预览');
    void store.openFilePreview(req.path);
    return;
  }
  if (bridgeWaiting.value) {
    pendingExternalUrl.value = req.url;
    return;
  }
  consumedNavNonce = store.browserNavNonce;
  goTo(req.url);
}

watch(() => store.browserNavNonce, consumeBrowserNavRequest);
watch(() => props.active, (active) => {
  if (active) consumeBrowserNavRequest();
});

function onBack() { surface.request('browser_pane_back'); }
function onForward() { surface.request('browser_pane_forward'); }
function onReloadOrStop() {
  if (store.embeddedBrowser.loading) surface.request('browser_pane_stop');
  else surface.request('browser_pane_reload');
}

function toggleSizeMenu() {
  sizeMenuOpen.value = !sizeMenuOpen.value;
  if (sizeMenuOpen.value) {
    customW.value = freeSize.value.w;
    customH.value = freeSize.value.h;
  }
}

// 菜单外 pointerdown（捕获阶段）即关闭：菜单打开期间原生表面被压住，
// 不能让用户点去别处后菜单仍悬着挡视线
function onDocPointerDown(e: PointerEvent) {
  const target = e.target as Node | null;
  if (!target) return;
  const menu = document.querySelector('[data-test="bp-size-menu"]');
  const toggle = document.querySelector('[data-test="bp-size-toggle"]');
  if ((menu && menu.contains(target)) || (toggle && toggle.contains(target))) return;
  sizeMenuOpen.value = false;
}

watch(sizeMenuOpen, (open) => {
  if (typeof document === 'undefined') return;
  if (open) document.addEventListener('pointerdown', onDocPointerDown, true);
  else document.removeEventListener('pointerdown', onDocPointerDown, true);
});

function applyFreeSize(preset: { w: number; h: number } | null) {
  if (preset) {
    freeSize.value = { on: true, w: preset.w, h: preset.h };
  } else {
    freeSize.value = { ...freeSize.value, on: false };
  }
  updateFitScale();
  sizeMenuOpen.value = false;
  void nextTick(() => surface.syncNow());
}

function applyCustomSize() {
  const w = Number(customW.value);
  const h = Number(customH.value);
  if (!w || !h || !Number.isFinite(w) || !Number.isFinite(h)) return;
  freeSize.value = {
    on: true,
    w: Math.min(FREE_MAX, Math.max(FREE_MIN_W, Math.round(w))),
    h: Math.min(FREE_MAX, Math.max(FREE_MIN_H, Math.round(h))),
  };
  updateFitScale();
  sizeMenuOpen.value = false;
  void nextTick(() => surface.syncNow());
}

// 拖拽手柄：指针位移按当前缩放换算回逻辑尺寸再增量调整；rAF 合并上报，
// syncNow 内 rectKey 去重兜底，避免每个 pointermove 都打一次桥
let dragStart: { x: number; y: number; w: number; h: number; scale: number } | null = null;
let dragRaf = 0;

function onGripDown(e: PointerEvent) {
  if (!freeSize.value.on) return;
  dragStart = {
    x: e.clientX,
    y: e.clientY,
    w: freeSize.value.w,
    h: freeSize.value.h,
    scale: fitScale.value || 1,
  };
  try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); } catch { }
  e.preventDefault();
}

function onGripMove(e: PointerEvent) {
  if (!dragStart) return;
  const z = dragStart.scale;
  freeSize.value = {
    on: true,
    w: Math.min(FREE_MAX, Math.max(FREE_MIN_W, Math.round(dragStart.w + (e.clientX - dragStart.x) / z))),
    h: Math.min(FREE_MAX, Math.max(FREE_MIN_H, Math.round(dragStart.h + (e.clientY - dragStart.y) / z))),
  };
  updateFitScale();
  if (!dragRaf) {
    dragRaf = requestAnimationFrame(() => {
      dragRaf = 0;
      surface.syncNow();
    });
  }
}

function onGripUp() {
  dragStart = null;
  if (dragRaf) { cancelAnimationFrame(dragRaf); dragRaf = 0; }
  surface.syncNow();
}

watch(
  () => store.embeddedBrowser.automationViewport,
  (viewport) => {
    if (!viewport) return;
    freeSize.value = { on: true, w: viewport[0], h: viewport[1] };
    sizeMenuOpen.value = false;
    // 事件到达时浏览器页签可能还没参与布局（面板折叠/选择页态，占位区
    // clientWidth=0），此刻计算的适应缩放会走「不可测量兜底 1」。兜底值若
    // 不在布局就绪后重算，视口框就按原始逻辑尺寸（如 1440×900）渲染出
    // 面板、原生控件随之铺满会话区。必须等 DOM 更新后重算再上报。
    void nextTick(() => {
      updateFitScale();
      surface.syncNow();
    });
  },
  { immediate: true },
);

// 视口框的挂载/卸载由 frameRenderable 翻转驱动（起始页→有页面），ResizeObserver
// 只盯占位区尺寸、感知不到这次翻转。翻转时若 fitScale 仍是「不可测量兜底 1」，
// 框会按原始逻辑尺寸渲染（同上，铺满会话区的事故源）：渲染前先重算缩放，
// DOM 更新后再重算一次并重报矩形（占位区此间从不可见变可见时 clientWidth 才就绪）。
watch(frameRenderable, (renderable) => {
  if (!renderable) return;
  updateFitScale();
  void nextTick(() => {
    updateFitScale();
    surface.syncNow();
  });
});

async function calibrate() {
  try {
    const res = await readApi()?.browser_pane_get_state?.();
    if (res && res.ok && res.state) {
      store.applyEmbeddedBrowserState(res.state);
    } else if (res && res.ok === false && res.error === 'unsupported_platform') {
      store.embeddedBrowser.supported = false;
    }
  } catch { }
}

let surfaceRo: ResizeObserver | null = null;
const onWindowResizeMetrics = () => {
  if (!freeSize.value.on) return;
  const prev = fitScale.value;
  updateFitScale();
  if (fitScale.value !== prev) void nextTick(() => surface.syncNow());
};

onMounted(() => {
  // 挂载前到达的外部导航请求（点击 html 时本组件可能还没挂载）统一在此消费：
  // 桥未就绪先挂起（就绪后补导航），宿主不支持则回退源码预览
  consumeBrowserNavRequest();
  window.addEventListener('resize', onWindowResizeMetrics);
  // ResizeObserver 必须先于桥状态分支注册：面板尺寸变化与桥是否就绪无关。
  // 旧实现把它放在 `bridgePhase === 'ready' → return` 之后，正常启动（桥已就绪）
  // 永远走不到这里，于是拖动右侧面板宽度后 fitScale 不重算，视口框仍按旧尺寸
  // 渲染，网页被裁掉一截（只能看到左上区域）。
  const el = surfaceRef.value;
  if (el && typeof ResizeObserver !== 'undefined') {
    try {
      surfaceRo = new ResizeObserver(onWindowResizeMetrics);
      surfaceRo.observe(el);
    } catch { surfaceRo = null; }
  }
  // 初始校准：resize 观测只在尺寸变化时触发，首帧的适应缩放要主动算一次。
  // 否则 fitScale 会一直停在兜底 1，视口框按原始逻辑尺寸渲染并溢出面板。
  updateFitScale();
  if (freeSize.value.on) {
    void nextTick(() => surface.syncNow());
  }
  if (bridgePhase.value === 'ready') { void calibrate(); return; }
  if (bridgePhase.value === 'waiting') watchBridge();
});

onUnmounted(() => {
  window.removeEventListener('pywebviewready', onBridgeReady);
  window.removeEventListener('resize', onWindowResizeMetrics);
  stopBridgeWait();
  if (typeof document !== 'undefined') {
    document.removeEventListener('pointerdown', onDocPointerDown, true);
  }
  if (surfaceRo) { try { surfaceRo.disconnect(); } catch { } surfaceRo = null; }
  dragStart = null;
  if (dragRaf) { cancelAnimationFrame(dragRaf); dragRaf = 0; }
});
</script>

<style scoped>
.bp-root {
  position: relative;
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
.bp-btn-active {
  color: var(--accent);
  background: var(--accent-soft);
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
/* 提示/加载浮层是被动展示层：pointer-events:none 让 elementFromPoint 的
   遮挡检测穿透到测量目标。否则自由尺寸模式下表面隐藏时转圈铺满表面、
   盖住视口框中心，遮挡检测会把「自家转圈」当成外部遮挡而永不重新显示
   原生表面（表现为加载图标永远旋转的假死） */
.bp-notice {
  pointer-events: none;
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
.bp-size-menu {
  position: absolute;
  top: 40px;
  right: 8px;
  z-index: 30;
  width: 224px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--bg2);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
}
.bp-size-presets {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.bp-size-chip {
  padding: 4px 8px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg1);
  color: var(--text-dim);
  font-size: 11.5px;
  font-family: inherit;
  cursor: pointer;
  transition: 0.15s;
}
.bp-size-chip:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.bp-size-chip.active {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-soft);
}
.bp-size-custom {
  display: flex;
  align-items: center;
  gap: 6px;
}
.bp-size-input {
  width: 62px;
  height: 24px;
  padding: 0 6px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg1);
  color: var(--text);
  font-size: 12px;
  font-family: inherit;
  outline: none;
}
.bp-size-input:focus {
  border-color: var(--accent);
}
.bp-size-x {
  color: var(--text-faint);
  font-size: 12px;
}
.bp-size-apply {
  margin-left: auto;
  padding: 4px 10px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg1);
  color: var(--text);
  font-size: 11.5px;
  font-family: inherit;
  cursor: pointer;
  transition: 0.15s;
}
.bp-size-apply:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.bp-size-hint {
  margin: 0;
  font-size: 11px;
  color: var(--text-faint);
}
.bp-frame {
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg1);
  box-shadow: 0 4px 18px rgba(0, 0, 0, 0.14);
}
.bp-frame-grip {
  position: absolute;
  right: -7px;
  bottom: -7px;
  width: 14px;
  height: 14px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg3);
  cursor: nwse-resize;
  touch-action: none;
}
.bp-frame-grip:hover {
  border-color: var(--accent);
}
.bp-phase-marker {
  display: none;
}
.bp-constrained {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  height: 100%;
  box-sizing: border-box;
  overflow: auto;
}
.bp-constrained-banner {
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, var(--yellow) 45%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--yellow) 10%, transparent);
  font-size: 12px;
  line-height: 1.5;
  color: var(--text);
}
.bp-constrained-banner strong {
  display: block;
  margin-bottom: 4px;
}
.bp-constrained-thumb {
  display: block;
  width: 100%;
  height: auto;
  max-height: calc(100% - 92px);
  object-fit: contain;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--code-bg);
}
</style>
