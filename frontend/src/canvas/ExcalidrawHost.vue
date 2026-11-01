<template>
  <!-- Excalidraw 子应用宿主：本身不含任何画布逻辑，只负责把已构建的
       frontend-canvas/dist 用 iframe 挂进来。
       为什么是 iframe：子应用自带 React 运行时、字体与 20MB+ 依赖，直接打进主
       前端会把首屏时间翻倍，也把两套框架的全局样式塞进同一个页面。独立 iframe
       还顺手拿到了样式与全局变量的隔离（子应用有自己的主题切换）。 -->
  <div class="eh-root" data-test="excalidraw-host">
    <iframe
      v-if="url"
      :src="url"
      class="eh-frame"
      :class="{ 'eh-frame-ready': loaded }"
      data-test="excalidraw-frame"
      title="自由画布"
      scrolling="no"
      allow="clipboard-write"
      @load="onLoad"
    ></iframe>

    <!-- 加载骨架：21MB 静态资源 + Excalidraw 首次初始化，在本机上也有可感知的
         延迟（且 iframe onload 之前整块是空的），必须有东西告诉用户"正在加载"
         而不是"坏了"。骨架盖在 iframe 之上，onload 后立刻撤掉。 -->
    <div v-if="!loaded && !error" class="eh-skeleton" data-test="excalidraw-loading">
      <div class="eh-spinner"></div>
      <span class="eh-hint">正在加载自由画布…</span>
      <span class="eh-sub">首次进入需要加载 Excalidraw 运行时，请稍候</span>
    </div>

    <!-- 失败态：桥不可用（浏览器里预览）或子应用没构建。必须把构建命令写出来，
         否则用户只会看到一个空白页，也无从判断该做什么。 -->
    <div v-if="error" class="eh-error" data-test="excalidraw-error">
      <span class="eh-error-text">{{ error }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue';

/**
 * 桥方法只返回 {"ok", "url"} 或 {"ok": false, "msg"}，字段全可选：
 * 前端不假设后端一定补齐 msg（老版本桥可能只有 ok）。
 */
interface CanvasIframeResult {
  ok?: boolean;
  url?: string;
  msg?: string;
}

interface CanvasBridge {
  canvas_iframe_url?: () => Promise<CanvasIframeResult | undefined>;
}

/** 父级可监听 ready（首帧加载完），用于收起自己的 loading 或做后续初始化 */
const emit = defineEmits<{ (e: 'ready'): void }>();

const url = ref('');
const loaded = ref(false);
const error = ref('');

/** 子应用缺失时给用户的行动指引；文案里直接带命令，省一次追问 */
const BUILD_HINT = '画布子应用未构建，请先运行 npm --prefix frontend-canvas run build';

/** pywebview 的 js_api 挂在 window.pywebview.api 上；浏览器预览时整块不存在 */
function bridgeApi(): CanvasBridge | undefined {
  return (window as unknown as { pywebview?: { api?: CanvasBridge } }).pywebview?.api;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** 组件已卸载就不再回写状态，避免卸载后 setState 之类的告警 */
let disposed = false;

/**
 * 取子应用地址。pywebview 一般在页面加载前就注入好 api，但注入时机与 Vue 挂载
 * 顺序无关，偶尔会晚几十毫秒；这里做有限次重试，最终仍拿不到就展示构建提示。
 */
async function resolveUrl(): Promise<void> {
  const attempts = 20;
  for (let i = 0; i < attempts; i += 1) {
    if (disposed) return;
    const api = bridgeApi();
    // 方法要连着 api 对象调用：pywebview 注入的桥是普通对象方法，
    // 取出来单独调用会丢 this，部分版本会直接抛错
    if (api && typeof api.canvas_iframe_url === 'function') {
      try {
        const res = await api.canvas_iframe_url();
        if (disposed) return;
        if (res && res.ok && res.url) {
          url.value = res.url;
          return;
        }
        // 后端明确说"未构建"时统一话术，其它失败原因原样带出并追加构建提示
        error.value =
          res && res.msg && res.msg !== '画布子应用未构建'
            ? `${res.msg}（${BUILD_HINT}）`
            : BUILD_HINT;
        return;
      } catch {
        // 桥抛异常同上：绝不留白屏，也不把原始异常直接糊到用户脸上
        error.value = BUILD_HINT;
        return;
      }
    }
    await sleep(250);
  }
  error.value = BUILD_HINT;
}

void resolveUrl();

function onLoad(): void {
  loaded.value = true;
  emit('ready');
}

onBeforeUnmount(() => {
  disposed = true;
});
</script>

<style scoped>
/* 撑满父级内容区（CanvasPane 的 .cv-root 是纵向 flex 容器） */
.eh-root {
  position: relative;
  display: flex;
  flex: 1;
  min-height: 0;
  width: 100%;
}
/* 全宽全高无边框；沙箱不要加：Excalidraw 依赖 module script 与同源存储，
   加上 sandbox 会被浏览器按"非安全上下文"降级甚至直接拒绝执行。 */
.eh-frame {
  flex: 1;
  width: 100%;
  height: 100%;
  border: 0;
  display: block;
  /* 子应用自绘背景，这里先用主题底色兜住加载前的白闪 */
  background: var(--bg0);
  opacity: 0;
  transition: opacity 0.18s ease;
}
.eh-frame-ready {
  opacity: 1;
}
/* 骨架与 iframe 叠在同一区域：居中一列（转圈 + 主文案 + 副文案） */
.eh-skeleton {
  position: absolute;
  inset: 0;
  z-index: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: var(--bg0);
  color: var(--text-faint);
  font-size: 12px;
}
.eh-spinner {
  width: 18px;
  height: 18px;
  border: 2px solid color-mix(in srgb, var(--border) 80%, transparent);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: eh-spin 0.9s linear infinite;
}
@keyframes eh-spin {
  to {
    transform: rotate(1turn);
  }
}
.eh-hint {
  color: var(--text-dim);
}
.eh-sub {
  font-family: var(--mono);
  font-size: 10.5px;
  letter-spacing: 0.3px;
}
.eh-error {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--bg0);
}
.eh-error-text {
  max-width: 420px;
  color: var(--text-dim);
  font-size: 12.5px;
  line-height: 1.6;
  text-align: center;
  word-break: break-word;
}
</style>