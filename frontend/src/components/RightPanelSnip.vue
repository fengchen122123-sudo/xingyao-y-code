<template>
  <!-- 画笔截图覆盖层：铺满整个 #rightbar。底层是宿主捕获的冻结帧（进入模式
       前原生浏览器表面尚可见，故快照含其像素），其上压暗 + 画笔 canvas +
       选区红框 + 浮动工具条。所有坐标均为覆盖层 CSS px，裁剪时按快照物理
       分辨率等比放大回源，保证导出像素与冻结帧逐一对齐 -->
  <div ref="rootRef" class="snip-root" data-test="snip-overlay">
    <img ref="imgRef" class="snip-frame" :src="snapshot" alt="" draggable="false" />
    <div class="snip-dim"></div>
    <canvas
      ref="canvasRef"
      class="snip-canvas"
      data-test="snip-canvas"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="onPointerUp"
    ></canvas>

    <div
      v-if="selection"
      class="snip-selection"
      data-test="snip-selection"
      :style="{
        left: selection.x + 'px',
        top: selection.y + 'px',
        width: selection.w + 'px',
        height: selection.h + 'px',
      }"
    ></div>

    <div
      v-if="selection"
      class="snip-toolbar"
      data-test="snip-toolbar"
      :style="toolbarStyle"
      @pointerdown.stop
    >
      <span class="snip-colors">
        <button
          v-for="c in PEN_COLORS"
          :key="c.value"
          class="snip-color"
          :class="{ active: penColor === c.value }"
          :style="{ background: c.value }"
          :title="'画笔颜色：' + c.label"
          :data-test="'snip-color-' + c.label"
          @click="penColor = c.value"
        ></button>
      </span>
      <span class="snip-sep"></span>
      <button class="snip-btn snip-btn-primary" data-test="snip-insert" title="把圈选区域作为图片附件插入输入框" @click="insertToComposer">
        <AppIcon name="paperclip" :size="14" /> 插入输入框
      </button>
      <button class="snip-btn" data-test="snip-copy" title="复制圈选区域到剪贴板" @click="copySelection">
        <AppIcon name="copy" :size="14" /> 复制
      </button>
      <button class="snip-btn" data-test="snip-save" title="圈选区域另存为 PNG 文件" @click="saveSelection">
        <AppIcon name="download" :size="14" /> 保存
      </button>
      <span class="snip-sep"></span>
      <button class="snip-btn snip-icon-only" data-test="snip-redraw" title="清除笔画重新圈选" @click="redrawAll">
        <AppIcon name="undo" :size="14" />
      </button>
      <button class="snip-btn snip-icon-only" data-test="snip-cancel" title="退出截图（Esc）" @click="$emit('close')">
        <AppIcon name="x" :size="14" />
      </button>
    </div>

    <div v-if="!selection" class="snip-hint" data-test="snip-hint">
      <AppIcon name="pencil" :size="13" /> 按住左键用画笔圈选要截图的区域，可多次圈选扩大范围，Esc 退出
    </div>
    <div v-if="busy" class="snip-busy" data-test="snip-busy">处理中…</div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';
import { useAttachments } from '../composables/useAttachments';

const props = defineProps<{ snapshot: string }>();
const emit = defineEmits<{ (e: 'close'): void }>();

const store = useAppStore();
const { addPaths } = useAttachments();

// 画笔只影响笔迹显示，不影响裁剪内容（圈的是范围不是涂鸦）
const PEN_COLORS = [
  { label: '红', value: '#ef4444' },
  { label: '蓝', value: '#3b82f6' },
  { label: '黑', value: '#1f2328' },
] as const;
const PEN_WIDTH = 3;
// 笔画外扩：画笔中心线一半在包围盒外，外扩保证圈选轮廓完整落在导出图内
const BBOX_INFLATE = PEN_WIDTH / 2 + 2;
const MIN_SELECTION = 4;

interface Point { x: number; y: number }
interface Stroke { color: string; points: Point[] }

const rootRef = ref<HTMLElement | null>(null);
const imgRef = ref<HTMLImageElement | null>(null);
const canvasRef = ref<HTMLCanvasElement | null>(null);

const strokes = ref<Stroke[]>([]);
const penColor = ref<string>(PEN_COLORS[0].value);
const busy = ref(false);
const rootW = ref(0);
const rootH = ref(0);

let drawing = false;
let current: Stroke | null = null;
let ctx: CanvasRenderingContext2D | null = null;

// ---------------------------------------------------------------- 选区与工具条

const selection = computed(() => {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  const all: Stroke[] = current ? [...strokes.value, current] : strokes.value;
  for (const s of all) {
    for (const p of s.points) {
      if (p.x < minX) minX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.x > maxX) maxX = p.x;
      if (p.y > maxY) maxY = p.y;
    }
  }
  if (!Number.isFinite(minX)) return null;
  const x = Math.max(0, minX - BBOX_INFLATE);
  const y = Math.max(0, minY - BBOX_INFLATE);
  const w = Math.min(rootW.value - x, maxX + BBOX_INFLATE - x);
  const h = Math.min(rootH.value - y, maxY + BBOX_INFLATE - y);
  if (w < MIN_SELECTION || h < MIN_SELECTION) return null;
  return { x, y, w, h };
});

const toolbarStyle = computed(() => {
  const sel = selection.value;
  if (!sel || !rootW.value || !rootH.value) return {};
  const TOOLBAR_W = 268;
  const TOOLBAR_H = 34;
  let left = sel.x + sel.w - TOOLBAR_W;
  if (left < 8) left = Math.min(8, Math.max(8, sel.x));
  if (left + TOOLBAR_W > rootW.value - 8) left = rootW.value - TOOLBAR_W - 8;
  let top = sel.y + sel.h + 10;
  if (top + TOOLBAR_H > rootH.value - 8) top = sel.y - TOOLBAR_H - 10;
  if (top < 8) top = 8;
  return { left: left + 'px', top: top + 'px' };
});

// ---------------------------------------------------------------- 画笔绘制

function canvasPoint(e: PointerEvent): Point {
  const el = rootRef.value;
  const rect = el ? el.getBoundingClientRect() : { left: 0, top: 0 };
  return { x: e.clientX - rect.left, y: e.clientY - rect.top };
}

function drawSegment(s: Stroke, from: Point, to: Point): void {
  if (!ctx) return;
  ctx.strokeStyle = s.color;
  ctx.lineWidth = PEN_WIDTH;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.beginPath();
  ctx.moveTo(from.x, from.y);
  ctx.lineTo(to.x, to.y);
  ctx.stroke();
}

function redrawCanvas(): void {
  if (!ctx) return;
  const canvas = canvasRef.value;
  if (canvas) ctx.clearRect(0, 0, canvas.width, canvas.height);
  for (const s of strokes.value) {
    for (let i = 1; i < s.points.length; i++) {
      drawSegment(s, s.points[i - 1], s.points[i]);
    }
  }
}

function onPointerDown(e: PointerEvent): void {
  if (busy.value || e.button !== 0) return;
  e.preventDefault();
  try { (e.target as HTMLElement).setPointerCapture(e.pointerId); } catch { /* jsdom */ }
  drawing = true;
  current = { color: penColor.value, points: [canvasPoint(e)] };
}

function onPointerMove(e: PointerEvent): void {
  if (!drawing || !current) return;
  e.preventDefault();
  const pt = canvasPoint(e);
  const from = current.points[current.points.length - 1];
  current.points.push(pt);
  drawSegment(current, from, pt);
}

function onPointerUp(e: PointerEvent): void {
  if (!drawing || !current) return;
  e.preventDefault();
  drawing = false;
  const finished = current;
  current = null;
  // 单击（无拖动）视为误触丢弃；有效笔画入库并统一重绘
  if (finished.points.length >= 2) strokes.value.push(finished);
  redrawCanvas();
  try { (e.target as HTMLElement).releasePointerCapture(e.pointerId); } catch { /* jsdom */ }
}

function redrawAll(): void {
  strokes.value = [];
  redrawCanvas();
}

// ---------------------------------------------------------------- 裁剪与动作

function cropSelection(): string | null {
  const sel = selection.value;
  const img = imgRef.value;
  if (!sel || !img || !img.naturalWidth || !rootW.value) return null;
  // 冻结帧物理分辨率 / 覆盖层 CSS 尺寸：DPI 缩放比可能不是 devicePixelRatio
  const ratio = img.naturalWidth / rootW.value;
  const sx = Math.max(0, Math.round(sel.x * ratio));
  const sy = Math.max(0, Math.round(sel.y * ratio));
  const sw = Math.max(1, Math.min(img.naturalWidth - sx, Math.round(sel.w * ratio)));
  const sh = Math.max(1, Math.min(img.naturalHeight - sy, Math.round(sel.h * ratio)));
  const out = document.createElement('canvas');
  out.width = sw;
  out.height = sh;
  const octx = out.getContext('2d');
  if (!octx) return null;
  octx.drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
  return out.toDataURL('image/png');
}

async function withBusy(action: () => Promise<void>): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  try {
    await action();
  } finally {
    busy.value = false;
  }
}

// 插入输入框：复用剪贴板粘贴的落盘通道（save_clipboard_image 会把文件登记为
// 受信粘贴路径，模型可直接读取），拿到路径后加入当前会话附件列表
function insertToComposer(): void {
  void withBusy(async () => {
    const dataUrl = cropSelection();
    if (!dataUrl) { store.notifyChat('圈选范围无效，请重新圈选'); return; }
    const api = window.pywebview?.api;
    const res = api?.save_clipboard_image
      ? await api.save_clipboard_image(dataUrl)
      : null;
    if (!res?.ok || !res.path) {
      store.notifyChat(String(res?.msg || '截图落盘失败，请重试'));
      return;
    }
    await addPaths([res.path]);
    store.notifyChat('截图已加入输入框附件');
    emit('close');
  });
}

function copySelection(): void {
  void withBusy(async () => {
    const dataUrl = cropSelection();
    if (!dataUrl) { store.notifyChat('圈选范围无效，请重新圈选'); return; }
    // 优先 WebView2 异步剪贴板（写 PNG 原始格式）；失败回退宿主 DIB 写入
    try {
      if (typeof ClipboardItem === 'function' && navigator.clipboard?.write) {
        const blob = await (await fetch(dataUrl)).blob();
        await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
        store.notifyChat('截图已复制到剪贴板');
        emit('close');
        return;
      }
      throw new Error('clipboard api unavailable');
    } catch {
      const api = window.pywebview?.api;
      const res = api?.snip_copy_clipboard ? await api.snip_copy_clipboard(dataUrl) : null;
      if (res?.ok) {
        store.notifyChat('截图已复制到剪贴板');
        emit('close');
      } else {
        store.notifyChat(String(res?.msg || '复制失败，请改用「保存」或「插入输入框」'));
      }
    }
  });
}

function saveSelection(): void {
  void withBusy(async () => {
    const dataUrl = cropSelection();
    if (!dataUrl) { store.notifyChat('圈选范围无效，请重新圈选'); return; }
    const api = window.pywebview?.api;
    const res = api?.snip_save_image
      ? await api.snip_save_image(dataUrl, `ycode截图_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')}.png`)
      : null;
    if (res?.ok && res.path) {
      store.notifyChat(`截图已保存：${res.path}`);
      emit('close');
    } else if (res && res.msg !== '已取消选择') {
      store.notifyChat(String(res.msg || '保存失败，请重试'));
    }
  });
}

// ---------------------------------------------------------------- 生命周期

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    e.preventDefault();
    emit('close');
  }
}

// 窗口尺寸/DPI 变化会让冻结帧与布局错位：直接退出最安全
function onWindowResize(): void {
  emit('close');
}

// 拖拽右面板宽度手柄只改 store.rightPanelWidth，不会派发 window.resize：
// 面板尺寸一变，冻结帧与覆盖层按 naturalWidth/rootW 的反算就错位、导出区域
// 与所见不符——与窗口 resize 同策略，尺寸变化即退出（下次进入重拍冻结帧）
watch(
  () => [store.rightPanelWidth, store.rightPanelCollapsed],
  () => { emit('close'); },
);

function setupCanvas(): void {
  const canvas = canvasRef.value;
  const el = rootRef.value;
  if (!canvas || !el) return;
  rootW.value = el.clientWidth;
  rootH.value = el.clientHeight;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rootW.value * dpr));
  canvas.height = Math.max(1, Math.round(rootH.value * dpr));
  ctx = canvas.getContext('2d');
  if (ctx) ctx.scale(dpr, dpr);
}

onMounted(() => {
  setupCanvas();
  window.addEventListener('keydown', onKeydown, true);
  window.addEventListener('resize', onWindowResize);
  // 快照是大 dataURL：等 <img> 解码完成再清一次画布尺寸（防御布局迟到）
  void nextTick(setupCanvas);
});

onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown, true);
  window.removeEventListener('resize', onWindowResize);
});
</script>

<style scoped>
.snip-root {
  position: absolute;
  inset: 0;
  z-index: 60;
  background: var(--bg1);
  cursor: crosshair;
  overflow: hidden;
  user-select: none;
}
/* 冻结帧 1:1 铺回覆盖层：宽高即进入模式时量到的面板矩形，像素逐一对齐 */
.snip-frame {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  object-fit: fill;
  pointer-events: none;
}
.snip-dim {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.18);
  pointer-events: none;
}
.snip-canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  touch-action: none;
}
.snip-selection {
  position: absolute;
  border: 2px solid #ef4444;
  border-radius: 2px;
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.55), 0 0 12px rgba(239, 68, 68, 0.35);
  pointer-events: none;
}
.snip-toolbar {
  position: absolute;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  border-radius: 9px;
  border: 1px solid var(--border);
  background: var(--bg1);
  box-shadow: 0 8px 26px rgba(0, 0, 0, 0.35);
  cursor: default;
}
.snip-colors {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.snip-color {
  width: 15px;
  height: 15px;
  border-radius: 50%;
  border: 2px solid transparent;
  padding: 0;
  cursor: pointer;
  transition: 0.15s;
}
.snip-color.active {
  border-color: var(--text);
  transform: scale(1.12);
}
.snip-sep {
  width: 1px;
  height: 18px;
  background: var(--border);
  margin: 0 2px;
  flex-shrink: 0;
}
.snip-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: none;
  background: none;
  color: var(--text-dim);
  font-size: 12px;
  font-family: inherit;
  padding: 4px 7px;
  border-radius: 6px;
  cursor: pointer;
  transition: 0.15s;
  flex-shrink: 0;
}
.snip-btn:hover {
  background: var(--bg3);
  color: var(--text);
}
.snip-btn-primary {
  color: var(--accent);
  font-weight: 600;
}
.snip-btn-primary:hover {
  color: var(--accent);
  background: var(--accent-soft);
}
.snip-icon-only {
  padding: 4px 5px;
}
.snip-hint {
  position: absolute;
  top: 44px;
  left: 50%;
  transform: translateX(-50%);
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--bg1);
  color: var(--text-dim);
  font-size: 12px;
  white-space: nowrap;
  pointer-events: none;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.25);
}
.snip-busy {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.25);
  color: var(--text);
  font-size: 13px;
  pointer-events: auto;
}
</style>
