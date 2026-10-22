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

      <!-- loading 时变停止按钮并让图标旋转（lucide loader 弧线本就为旋转设计） -->
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

      <!-- 自由尺寸：自定义视口大小（原生表面贴合框矩形），对照桌面浏览器的设备模式 -->
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
    </div>

    <!-- 自由尺寸菜单：预设 + 自定义宽高。打开期间宿主 hide 原生表面，
         避免原生控件穿透盖住浮层（原生控件永远绘制在 DOM 之上） -->
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

    <!-- 表面占位区：原生控件被宿主叠加在本矩形之上 -->
    <div ref="surfaceRef" class="bp-surface" data-test="bp-surface">
      <!-- 自由尺寸视口框：渲染时矩形上报目标切到本框（原生表面精确贴合），
           框尺寸 = 逻辑尺寸 × 适应缩放，网页逻辑视口恒等于请求尺寸 -->
      <div
        v-if="frameRenderable"
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

      <!-- 宿主不支持（确认缺桥或桥显式回 unsupported_platform）：工具栏已禁用 -->
      <div v-if="unsupported" class="bp-notice" data-test="bp-unsupported">
        <AppIcon name="globe" :size="26" :stroke-width="1.5" />
        <p class="bp-notice-title">浏览器面板需要桌面端 GUI 宿主支持</p>
        <p class="bp-notice-sub">内置浏览器仅在 Windows 桌面端 GUI 中可用。</p>
      </div>

      <!-- 桥尚未注入完成（启动竞态窗口，通常不足一秒）：等待而非判死 -->
      <div v-else-if="bridgeWaiting" class="bp-notice" data-test="bp-bridge-waiting">
        <AppIcon name="loader" :size="22" :class="{ 'icon-spin': true }" />
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
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
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
// 平台不支持 = 确认缺桥 或 宿主桥在任一次调用里显式回了 unsupported_platform
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
  // 桥就绪晚于挂载是桌面端常态：此刻才能校准地址栏/前进后退等真实状态
  void calibrate();
  // 挂起的外部导航请求（点击 html 时桥还没就绪）：就绪后补上导航
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

// 起始页条件：无页面地址且不在加载中（加载中的 about:blank 由原生表面接管）
const isStartPage = computed(() => {
  if (unsupported.value) return false;
  return isBlankUrl(String(store.embeddedBrowser.url || '')) && !store.embeddedBrowser.loading;
});

const surfaceRef = ref<HTMLElement | null>(null);

// ── 自由尺寸（自定义视口 · 适应窗口）──
// 照桌面浏览器设备模式的「适应窗口」：请求的逻辑视口（如 1280×800）不变，
// 原生 WebView2 用 ZoomFactor 把渲染整体缩放后贴合到面板内的「视口框」矩形
// ——框尺寸 = 逻辑尺寸 × 缩放，网页真实视口恒等于请求的逻辑尺寸。
// 框四周留边距：拖拽手柄在框外，且 .bp-surface overflow:hidden。
const SIZE_PRESETS = [
  { key: 'phone', label: '手机', w: 375, h: 667 },
  { key: 'tablet', label: '平板', w: 768, h: 1024 },
  { key: 'desktop', label: '桌面', w: 1280, h: 800 },
] as const;
// 自定义输入的逻辑尺寸边界
const FREE_MIN_W = 120;
const FREE_MIN_H = 200;
const FREE_MAX = 8192;
// 视口框四周保留的最小边距（≥ 手柄外扩 7px + 呼吸空间）
const FREE_RING = 16;
// WebView2 ZoomFactor 允许范围（与后端 _ZOOM_MIN/_ZOOM_MAX 一致）
const ZOOM_MIN = 0.25;
const ZOOM_MAX = 8;

const freeSize = ref({ on: false, w: 1280, h: 800 });
const sizeMenuOpen = ref(false);
const customW = ref<number | ''>(1280);
const customH = ref<number | ''>(800);
const frameRef = ref<HTMLElement | null>(null);

const frameRenderable = computed(
  () => freeSize.value.on && !unsupported.value && !bridgeWaiting.value && !isStartPage.value,
);
const sizeLabel = computed(() =>
  freeSize.value.on ? `${freeSize.value.w} × ${freeSize.value.h}` : '适配面板',
);

// 适应窗口缩放：面板可绘区（扣除边距）与逻辑视口的内切比例。
// 表面不可测量（clientWidth 为 0，如测试环境）时不缩放，保持 1:1。
const fitScale = ref(1);
const zoomPct = computed(() => Math.round(fitScale.value * 100));

function computeFitScale(w: number, h: number): number {
  const el = surfaceRef.value;
  const availW = (el?.clientWidth ?? 0) - FREE_RING * 2;
  const availH = (el?.clientHeight ?? 0) - FREE_RING * 2;
  if (availW <= 0 || availH <= 0 || w <= 0 || h <= 0) return 1;
  return Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.min(availW / w, availH / h)));
}

function updateFitScale() {
  fitScale.value = freeSize.value.on ? computeFitScale(freeSize.value.w, freeSize.value.h) : 1;
}

// 视口框是缩放后的视觉尺寸；上报该矩形 + 缩放值，网页逻辑视口即恒等于请求尺寸
const frameW = computed(() => Math.round(freeSize.value.w * fitScale.value));
const frameH = computed(() => Math.round(freeSize.value.h * fitScale.value));

const surface = useEmbeddedBrowserSurface({
  isActive: () => props.active,
  // 起始页/不支持态/尺寸菜单打开时让宿主 hide：原生空白页会盖住 DOM 起始页，
  // 原生页面会穿透盖住菜单浮层（原生控件永远绘制在 DOM 之上）。
  // 例外：星瑶自动化已下发视口时，起始页也要显示原生表面——自动化马上要经
  // CDP 导航，隐藏的 WebView2 截图会被节流/挂起（曾致首次 observe 超时、
  // backend 被判封闭报「无法打开浏览器」）；短暂可见的空白页是预期围观体验。
  // 画笔截图冻结帧接管整块面板时最先让位：冻结帧在进入模式前已捕获表面
  // 像素，原生控件若继续显示会盖住画笔覆盖层；门控放在 automationViewport
  // 之前——星瑶自动化的「起始页保显」也不能越过这个模态，snip 关闭后
  // 既有 watch 会自动 re-show 表面。
  shouldShowSurface: () =>
    !store.rightPanelSnipActive &&
    !unsupported.value &&
    !sizeMenuOpen.value &&
    (!isStartPage.value || !!store.embeddedBrowser.automationViewport),
  // 自由尺寸模式下量「视口框」而非整块表面
  getSurfaceEl: () => frameRef.value || surfaceRef.value,
  // 视口框是缩放后的视觉尺寸，逻辑视口 = 框尺寸 / 缩放 = 请求的逻辑尺寸。
  // 只在真正量「框」时才带缩放：量整块表面（起始页态）时缩放无意义，
  // 带上会让宿主把 surface 矩形除以 fitScale，得到错误的网页视口。
  getZoom: () => (frameRenderable.value && freeSize.value.on ? fitScale.value : 1),
  // 原生控件永远绘制在 DOM 之上且不受 overflow:hidden 裁剪：上报矩形一旦
  // 越出占位区就会盖住会话区（自由尺寸框在占位区不可测量时兜底缩放=1，
  // 曾按原始 1440×900 铺满整个主区）。所有上报矩形一律夹紧回占位区内。
  getClampEl: () => surfaceRef.value,
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
  if (!url || unsupported.value || bridgeWaiting.value) return;
  // 乐观更新：立即从起始页切到「表面」态（原生控件随即被创建/接管）
  store.embeddedBrowser.url = url;
  surface.navigate(url);
}

// ── 外部入口请求导航（对话正文 / 文件树点击 html）──
// store 里放一条导航请求 + nonce，这里消费它：桥未就绪先缓存（就绪后补，
// 见 onBridgeReady）；宿主不支持内嵌浏览器则回退源码预览弹窗——这条回退保证
// 点击 html 在任何宿主上都落得了地，不会「点了没反应」。
const pendingExternalUrl = ref('');

function consumeBrowserNavRequest() {
  const req = store.browserNavRequest;
  if (!req || !req.url) return;
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
  goTo(req.url);
}

// nonce 变化 = 新的导航请求（同一文件重复点击也会自增，watch 必触发）
watch(() => store.browserNavNonce, consumeBrowserNavRequest);

// ── 工具栏按钮 ──
function onBack() { surface.request('browser_pane_back'); }
function onForward() { surface.request('browser_pane_forward'); }
function onReloadOrStop() {
  if (store.embeddedBrowser.loading) surface.request('browser_pane_stop');
  else surface.request('browser_pane_reload');
}

// ── 自由尺寸交互 ──

function toggleSizeMenu() {
  sizeMenuOpen.value = !sizeMenuOpen.value;
  // 打开时把当前尺寸同步进自定义输入框，便于微调
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
  // 等视口框渲染/卸载完成后再量矩形（syncNow 内部有 rectKey 去重）
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
  try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); } catch { /* jsdom 等 */ }
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

// 星瑶浏览器自动化：面板按后端下发的项目分辨率做适应窗口呈现（用户围观）。
// 与手动自由尺寸共用同一 rect+zoom 上报路径；后端嵌入控件的 CDP 连接与
// 本面板看到的逻辑视口由此保持一致。
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

// 面板尺寸变化时重算适应缩放：视口框随之缩放并重报矩形，
// 保证任意面板尺寸下请求的逻辑视口都完整可见
let surfaceRo: ResizeObserver | null = null;

onMounted(() => {
  // 挂载前到达的外部导航请求（点击 html 时本组件可能还没挂载）统一在此消费：
  // 桥未就绪先挂起（就绪后补导航），宿主不支持则回退源码预览
  consumeBrowserNavRequest();
  // 桥已在 setup 前就绪（热重载/重挂载）：直接校准；waiting 则挂双通道等待
  if (bridgePhase.value === 'ready') { void calibrate(); return; }
  if (bridgePhase.value === 'waiting') watchBridge();
  // missing（确认缺桥）：不再做任何桥调用
  const el = surfaceRef.value;
  if (el && typeof ResizeObserver !== 'undefined') {
    try {
      surfaceRo = new ResizeObserver(() => {
        if (!freeSize.value.on) return;
        const prev = fitScale.value;
        updateFitScale();
        if (fitScale.value !== prev) void nextTick(() => surface.syncNow());
      });
      surfaceRo.observe(el);
    } catch { surfaceRo = null; }
  }
});

onUnmounted(() => {
  window.removeEventListener('pywebviewready', onBridgeReady);
  stopBridgeWait();
  if (typeof document !== 'undefined') {
    document.removeEventListener('pointerdown', onDocPointerDown, true);
  }
  if (surfaceRo) { try { surfaceRo.disconnect(); } catch { /* 忽略 */ } surfaceRo = null; }
  dragStart = null;
  if (dragRaf) { cancelAnimationFrame(dragRaf); dragRaf = 0; }
});
</script>

<style scoped>
.bp-root {
  position: relative; /* 自由尺寸菜单的定位锚点 */
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
/* ── 自由尺寸 ── */
.bp-size-menu {
  position: absolute;
  top: 40px; /* 工具栏（约 38px）下方 */
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
/* 视口框：原生表面精确贴合本框；居中锚定，translate 后的矩形由
   getBoundingClientRect 量得（含 transform，上报坐标天然正确） */
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
</style>
