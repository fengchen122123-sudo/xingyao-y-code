<template>
  <!-- 画笔标注：居中浮层。为什么遮罩点击不关闭：用户可能画了十几笔，
       一次误点空白就全丢，成本远高于「多按一次取消」。只能点「取消」或 × 退出 -->
  <div class="cv-anno-mask" data-test="annotation-dialog" @click.stop @pointerdown.stop>
    <div class="cv-anno-card" @click.stop @pointerdown.stop>
      <div class="cv-anno-head">
        <span class="cv-anno-title">画笔标注</span>
        <span class="cv-anno-sub" :title="title">{{ title }}</span>
        <span class="cv-anno-spacer"></span>
        <button class="cv-anno-x" data-test="annotation-close" title="取消（标注不保存）" @click.stop="emit('close')">
          ×
        </button>
      </div>

      <div class="cv-anno-tools">
        <button
          v-for="item in TOOLS"
          :key="item.value"
          class="cv-anno-btn"
          :class="{ 'is-on': tool === item.value }"
          :data-test="`annotation-tool-${item.value}`"
          @click.stop="tool = item.value"
        >
          {{ item.label }}
        </button>

        <span class="cv-anno-sep"></span>

        <!-- 颜色三档：色块比文字更直接，选中态用描边圈出 -->
        <button
          v-for="(color, index) in ANNOTATION_COLORS"
          :key="color.value"
          class="cv-anno-swatch"
          :class="{ 'is-on': colorIndex === index }"
          :style="{ background: color.value }"
          :title="`${color.label}色`"
          :data-test="`annotation-color-${index}`"
          @click.stop="colorIndex = index"
        ></button>

        <span class="cv-anno-sep"></span>

        <button
          v-for="(width, index) in ANNOTATION_WIDTHS"
          :key="width.value"
          class="cv-anno-btn"
          :class="{ 'is-on': widthIndex === index }"
          :title="`${width.label}笔宽`"
          :data-test="`annotation-width-${index}`"
          @click.stop="widthIndex = index"
        >
          {{ width.label }}
        </button>

        <span class="cv-anno-spacer"></span>

        <!-- 撤销走栈式回退而非「擦掉上一步」：笔迹是矢量的，整幅重绘才能
             保证擦除/形状混用时结果也正确 -->
        <button
          class="cv-anno-btn"
          data-test="annotation-undo"
          :disabled="!ops.length"
          @click.stop="onUndo"
        >
          撤销
        </button>
        <button
          class="cv-anno-btn"
          data-test="annotation-clear"
          :disabled="!ops.length"
          @click.stop="onClear"
        >
          清空
        </button>
      </div>

      <div class="cv-anno-stage" :style="stageStyle">
        <!-- 底图只显示不绘制：这条 url 来自本机媒体服务端口，属跨源图片，
             一旦 drawImage 进 canvas 就会把画布标脏，toBlob 直接抛安全错误 -->
        <img
          ref="imgRef"
          class="cv-anno-base"
          :src="url"
          :alt="title"
          draggable="false"
          @load="onImageLoad"
          @error="onImageError"
        />
        <!-- 标注层：内部像素尺寸 = 原图尺寸（在 onload 后设），CSS 显示尺寸另行
             等比适配，因此导出的 PNG 天然与原图同尺寸，后端无需再缩放 -->
        <canvas
          ref="canvasRef"
          class="cv-anno-layer"
          data-test="annotation-canvas"
          @pointerdown="onPointerDown"
          @pointermove="onPointerMove"
          @pointerup="onPointerUp"
          @pointercancel="onPointerUp"
        ></canvas>
        <div v-if="!ready" class="cv-anno-empty" data-test="annotation-empty">
          {{ loadError ? '底图加载失败，无法标注' : '底图载入中…' }}
        </div>
      </div>

      <p class="cv-anno-note">
        标注只是给模型看的视觉提示（上游接口没有选区参数），不保证只改圈内区域。
      </p>

      <textarea
        v-model="promptText"
        class="cv-anno-prompt"
        data-test="annotation-prompt"
        rows="2"
        placeholder="圈出要改的位置，再写一句要求，例如「把月亮换成一轮落日」"
      ></textarea>
      <p v-if="hint" class="cv-anno-hint" data-test="annotation-hint">{{ hint }}</p>

      <div class="cv-anno-foot">
        <span class="cv-anno-price">0.20 元/张</span>
        <span class="cv-anno-spacer"></span>
        <button class="cv-anno-btn" @click.stop="emit('close')">取消</button>
        <button
          class="cv-anno-btn cv-anno-btn--primary"
          data-test="annotation-generate"
          :disabled="!canSubmit"
          :title="canSubmit ? '带标注生成一版新图' : '先画几笔标注'"
          @click.stop="onSubmit"
        >
          {{ submitting ? '提交中…' : '生成' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// 标注浮层：只负责「画 + 导出透明 PNG」，不碰桥、不碰 store。
// 提交时把 base64 与指令一起 emit 给父级（CanvasPane），由父级调
// canvas_edit_image_annotated——组件层不自己发请求，与节点「只画不改数据」
// 的既有约定一致。
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import {
  ANNOTATION_COLORS,
  ANNOTATION_WIDTHS,
  annotationScale,
  displayToPixel,
  fitInto,
  type AnnotationScale,
  type PixelPoint,
  type PixelSize,
} from './annotation';

const props = withDefaults(
  defineProps<{
    /** 底图预览 url（只显示，绝不 drawImage 进 canvas：跨源会标脏画布） */
    url: string;
    /** 节点标题，用作浮层副标题 / 图片 alt */
    title?: string;
    /** 预填指令：父级把节点浮层里已写好的那句话带过来，避免用户重敲 */
    prompt?: string;
    /** 父级正在调桥提交：禁用「生成」防止连点（每点一次都是 0.2 元） */
    submitting?: boolean;
  }>(),
  { title: '', prompt: '', submitting: false },
);

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'submit', payload: { annotationB64: string; prompt: string }): void;
}>();

type Tool = 'pen' | 'rect' | 'arrow' | 'eraser';

const TOOLS: Array<{ value: Tool; label: string }> = [
  { value: 'pen', label: '画笔' },
  { value: 'rect', label: '矩形' },
  { value: 'arrow', label: '箭头' },
  { value: 'eraser', label: '橡皮' },
];

/** 自由笔迹（含橡皮）记一串点；形状只记起止两点 */
interface FreehandOp {
  tool: 'pen' | 'eraser';
  color: string;
  /** 已换算成原图像素宽度 */
  width: number;
  points: PixelPoint[];
}
interface ShapeOp {
  tool: 'rect' | 'arrow';
  color: string;
  width: number;
  from: PixelPoint;
  to: PixelPoint;
}
type AnnotationOp = FreehandOp | ShapeOp;

const imgRef = ref<HTMLImageElement | null>(null);
const canvasRef = ref<HTMLCanvasElement | null>(null);

/** 原图像素尺寸（= canvas 内部像素尺寸），img onload 后才拿到 */
const pixel = ref<PixelSize | null>(null);
/** 底图 CSS 显示尺寸（等比适配浮层可用框） */
const display = ref<PixelSize>({ width: 0, height: 0 });
const loadError = ref(false);

const tool = ref<Tool>('pen');
const colorIndex = ref(0);
// 默认「中」：细档在 1024 以上的图上几乎看不见，用户第一笔就画不出效果
const widthIndex = ref(1);
const promptText = ref(props.prompt || '');
const hint = ref('');

/** 已提交的矢量笔迹（栈）：撤销 = 弹栈后整幅重绘，导出前也靠它重放 */
const ops = ref<AnnotationOp[]>([]);
/** 正在画的那一笔：形状需要实时预览，所以单独持有、不进栈 */
let current: AnnotationOp | null = null;
let drawing = false;

const ready = computed(
  () => !loadError.value && !!pixel.value && display.value.width > 0 && display.value.height > 0,
);

const scale = computed<AnnotationScale>(() => {
  const p = pixel.value;
  if (!p) return { sx: 1, sy: 1 };
  return annotationScale(display.value.width, display.value.height, p.width, p.height);
});

const stageStyle = computed(() => ({
  width: `${display.value.width}px`,
  height: `${display.value.height}px`,
}));

/** 生图按钮：没有笔迹就不给点——白花 0.2 元买一张「什么都没改」的图 */
const canSubmit = computed(
  () => ready.value && ops.value.length > 0 && !props.submitting,
);

/** 浮层留给底图的最大可用框（工具条 / prompt 输入 / 底部按钮要占地方） */
function stageBox(): PixelSize {
  return {
    width: Math.max(240, Math.min(880, window.innerWidth - 96)),
    height: Math.max(180, Math.min(560, window.innerHeight - 340)),
  };
}

function context(): CanvasRenderingContext2D | null {
  // jsdom 里没有 canvas 实现，getContext 返回 null——所有绘制入口都要容忍
  return canvasRef.value?.getContext('2d') ?? null;
}

/** 当前笔宽换算成原图像素宽度（同一档笔宽在大小图上视觉粗细一致） */
function brushWidth(): number {
  const base = ANNOTATION_WIDTHS[widthIndex.value]?.value ?? ANNOTATION_WIDTHS[0].value;
  return Math.max(1, Math.round(base * scale.value.sx));
}

function drawOp(ctx: CanvasRenderingContext2D, op: AnnotationOp): void {
  ctx.save();
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.lineWidth = Math.max(1, op.width);
  ctx.strokeStyle = op.color;
  // 橡皮是真的擦掉像素，不是画白色：导出层是透明背景 PNG，画白色会在合成时
  // 糊一层白把原图盖住，看起来像"擦出一个白块"
  ctx.globalCompositeOperation = op.tool === 'eraser' ? 'destination-out' : 'source-over';

  if (op.tool === 'pen' || op.tool === 'eraser') {
    if (op.points.length === 1) {
      // 单点也要看得见：长度为 0 的线段在部分实现里不可见，直接画圆点
      ctx.beginPath();
      ctx.arc(op.points[0].x, op.points[0].y, Math.max(0.5, op.width / 2), 0, Math.PI * 2);
      ctx.fillStyle = op.color;
      ctx.fill();
    } else {
      ctx.beginPath();
      ctx.moveTo(op.points[0].x, op.points[0].y);
      for (let i = 1; i < op.points.length; i += 1) {
        ctx.lineTo(op.points[i].x, op.points[i].y);
      }
      ctx.stroke();
    }
  } else if (op.tool === 'rect') {
    ctx.beginPath();
    ctx.rect(op.from.x, op.from.y, op.to.x - op.from.x, op.to.y - op.from.y);
    ctx.stroke();
  } else if (op.tool === 'arrow') {
    // 写成正向判别而不是 else：tool 是字面量联合（'pen'|'eraser' / 'rect'|'arrow'），
    // TS 对 else 分支不做收窄，只有正向比较才能把类型收成单一成员。
    const dx = op.to.x - op.from.x;
    const dy = op.to.y - op.from.y;
    ctx.beginPath();
    ctx.moveTo(op.from.x, op.from.y);
    ctx.lineTo(op.to.x, op.to.y);
    ctx.stroke();
    if (Math.hypot(dx, dy) >= 1) {
      // 箭头头部长度随笔宽走，粗笔配小箭头会显得头轻脚重
      const angle = Math.atan2(dy, dx);
      const head = Math.max(op.width * 3.5, 6);
      for (const offset of [Math.PI / 6, -Math.PI / 6]) {
        ctx.beginPath();
        ctx.moveTo(op.to.x, op.to.y);
        ctx.lineTo(op.to.x - head * Math.cos(angle - offset), op.to.y - head * Math.sin(angle - offset));
        ctx.stroke();
      }
    }
  }
  ctx.restore();
}

/** 整幅重绘：清空 → 重放已提交笔迹 → 叠加正在画的形状预览 */
function redraw(): void {
  const ctx = context();
  const p = pixel.value;
  if (!ctx || !p) return;
  ctx.clearRect(0, 0, p.width, p.height);
  for (const op of ops.value) drawOp(ctx, op);
  if (current && (current.tool === 'rect' || current.tool === 'arrow')) drawOp(ctx, current);
}

/** 按当前可用框重算显示尺寸 + 同步 canvas 内部像素尺寸 */
function applyLayout(): void {
  const p = pixel.value;
  if (!p) return;
  const box = stageBox();
  display.value = fitInto(p.width, p.height, box.width, box.height);
  const canvas = canvasRef.value;
  if (canvas) {
    // 内部像素 = 原图像素：导出 PNG 天然与原图同尺寸，后端只做一层
    // alpha_composite，不再缩放——缩放只发生在前端一次，避免两处换算互相污染。
    // 只在真的变了才赋值：给 canvas.width/height 赋值本身就会清空画面
    if (canvas.width !== p.width) canvas.width = p.width;
    if (canvas.height !== p.height) canvas.height = p.height;
  }
  redraw();
}

async function onImageLoad(): Promise<void> {
  const img = imgRef.value;
  if (!img) return;
  const width = img.naturalWidth;
  const height = img.naturalHeight;
  if (!(width > 0 && height > 0)) {
    loadError.value = true;
    return;
  }
  // 关键取值点：naturalWidth/Height 才是原图真实像素，offsetWidth 是 CSS 尺寸
  pixel.value = { width, height };
  loadError.value = false;
  // 等底图尺寸落到模板上（stage 有了宽高）再设 canvas 内部像素
  await nextTick();
  applyLayout();
}

function onImageError(): void {
  loadError.value = true;
  pixel.value = null;
}

/** 指针位置 → 原图像素坐标 */
function toPixel(event: PointerEvent): PixelPoint | null {
  const canvas = canvasRef.value;
  const p = pixel.value;
  if (!canvas || !p) return null;
  const rect = canvas.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  // 换算用 rect 实测尺寸而不是 state 里的 display 尺寸：CSS 取整、浏览器缩放
  // 都会让实际渲染尺寸与算出来的差零点几像素，只有实测值才与指针所见一致
  const local = annotationScale(rect.width, rect.height, p.width, p.height);
  return displayToPixel(
    { x: event.clientX - rect.left, y: event.clientY - rect.top },
    local,
    p,
  );
}

function onPointerDown(event: PointerEvent): void {
  if (!ready.value || props.submitting) return;
  // 只认左键：右键在这里可能触发浏览器菜单，中键是滚轮手势
  if (event.button !== 0) return;
  const canvas = canvasRef.value;
  const ctx = context();
  const point = toPixel(event);
  if (!canvas || !ctx || !point) return;
  event.preventDefault();
  try {
    // 捕获指针：拖出浮层边界时仍然收得到 move/up，否则笔迹会断在半路
    canvas.setPointerCapture(event.pointerId);
  } catch {
    // 部分环境不支持捕获，退化为普通事件流即可
  }
  drawing = true;
  hint.value = '';
  const width = brushWidth();
  const color = ANNOTATION_COLORS[colorIndex.value]?.value ?? ANNOTATION_COLORS[0].value;
  if (tool.value === 'pen' || tool.value === 'eraser') {
    current = { tool: tool.value, color, width, points: [point] };
    drawOp(ctx, current);
  } else {
    current = { tool: tool.value, color, width, from: point, to: point };
  }
}

function onPointerMove(event: PointerEvent): void {
  if (!drawing || !current) return;
  const point = toPixel(event);
  if (!point) return;
  if (current.tool === 'pen' || current.tool === 'eraser') {
    const last = current.points[current.points.length - 1];
    if (last && last.x === point.x && last.y === point.y) return;
    current.points.push(point);
    const ctx = context();
    // 只增量画新的一段：长笔迹整幅重绘会越画越卡
    if (ctx && last) {
      drawOp(ctx, { tool: current.tool, color: current.color, width: current.width, points: [last, point] });
    }
  } else if (current.tool === 'rect' || current.tool === 'arrow') {
    // 同样写正向判别：tool 是字面量联合，else 分支不收窄，取不到 to/from
    current.to = point;
    // 形状要实时看到预览，整幅重绘一次（笔迹条数很少，代价可忽略）
    redraw();
  }
}

function onPointerUp(event: PointerEvent): void {
  if (!drawing || !current) return;
  drawing = false;
  try {
    canvasRef.value?.releasePointerCapture(event.pointerId);
  } catch {
    // 未捕获时释放会抛错，忽略
  }
  const op = current;
  current = null;
  ops.value = [...ops.value, op];
  // 提交后统一整幅重绘一次：增量画出来的和重放出来的必须完全一致，
  // 否则用户看到的样子和导出的 PNG 会对不上
  redraw();
}

function onUndo(): void {
  if (drawing || !ops.value.length) return;
  ops.value = ops.value.slice(0, -1);
  redraw();
}

function onClear(): void {
  if (drawing || !ops.value.length) return;
  ops.value = [];
  redraw();
}

/**
 * 导出透明标注层：toBlob → dataURL → 去掉 `data:image/png;base64,` 前缀。
 * 后端对带前缀的入参已容错，但日志/错误信息里干净的 base64 才读得下去。
 */
function onSubmit(): void {
  // 提交中再点没有意义（每点一次都是一次真实计费），静默忽略
  if (props.submitting) return;
  if (!canSubmit.value) {
    if (ready.value) hint.value = '先在图上画几笔标注，再点生成';
    return;
  }
  const prompt = promptText.value.trim();
  if (!prompt) {
    hint.value = '再写一句要改什么，标注才知道往哪个方向改';
    return;
  }
  const canvas = canvasRef.value;
  if (!canvas || typeof canvas.toBlob !== 'function') {
    hint.value = '当前环境不支持导出标注，请换桌面端重试';
    return;
  }
  // 正在画的形状还没提交：先收尾，避免最后一笔漏导出
  if (current) {
    ops.value = [...ops.value, current];
    current = null;
    redraw();
  }
  try {
    canvas.toBlob((blob) => {
      if (!blob) {
        hint.value = '导出标注失败，请重试';
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        const dataUrl = String(reader.result || '');
        const b64 = dataUrl.replace(/^data:image\/png;base64,/, '');
        if (!b64) {
          hint.value = '导出标注失败，请重试';
          return;
        }
        emit('submit', { annotationB64: b64, prompt });
      };
      reader.onerror = () => {
        hint.value = '导出标注失败，请重试';
      };
      reader.readAsDataURL(blob);
    }, 'image/png');
  } catch {
    // 理论上到不了这里（我们从没把跨源底图 drawImage 进 canvas）；
    // 真到了就说明画布被标脏，宁可明确提示也不要静默失败
    hint.value = '标注导出被浏览器拒绝，请重试';
  }
}

/**
 * 窗口尺寸变化要重算显示尺寸：canvas 内部像素不动，笔迹是矢量（点存的是
 * 原图坐标），重放一遍即可跟着新显示尺寸走，用户的标注不会丢。
 */
function onWindowResize(): void {
  if (!pixel.value) return;
  applyLayout();
}

onMounted(() => {
  window.addEventListener('resize', onWindowResize);
  // 底图走缓存时 load 事件可能早于监听挂载：complete=true 就补一次
  const img = imgRef.value;
  if (img && img.complete && img.naturalWidth > 0) void onImageLoad();
});

onBeforeUnmount(() => {
  window.removeEventListener('resize', onWindowResize);
});
</script>

<style scoped>
/* 遮罩：absolute 而不是 fixed——右面板分区有 isolation，fixed 会被其它分区
   的层叠上下文盖住（与内嵌对话坞同一处理） */
.cv-anno-mask {
  position: absolute;
  inset: 0;
  z-index: 30;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 14px;
  background: color-mix(in srgb, var(--bg0) 72%, transparent);
  backdrop-filter: blur(2px);
}
/* 卡片宽度自适应底图：小图不会被撑出大片空白，大图也不会把浮层顶出视口 */
.cv-anno-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: min(900px, 100%);
  max-height: 100%;
  padding: 10px;
  border: 1px solid color-mix(in srgb, var(--accent) 32%, var(--border));
  border-radius: 12px;
  background: var(--bg1);
  box-shadow: 0 20px 48px -18px color-mix(in srgb, var(--bg0) 90%, transparent);
  cursor: default;
  overflow: auto;
}
.cv-anno-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.cv-anno-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-bright);
}
.cv-anno-sub {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.2px;
  color: var(--text-faint);
}
.cv-anno-spacer {
  flex: 1;
}
.cv-anno-x {
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--text-faint);
  font-family: inherit;
  font-size: 15px;
  line-height: 1;
  cursor: pointer;
  transition: 0.12s;
}
.cv-anno-x:hover {
  border-color: var(--border);
  background: var(--bg3);
  color: var(--text-bright);
}
.cv-anno-tools {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
}
.cv-anno-btn {
  padding: 3px 9px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: transparent;
  color: var(--text-dim);
  font-family: inherit;
  font-size: 11px;
  cursor: pointer;
  transition: 0.12s;
}
.cv-anno-btn:hover:not(:disabled) {
  background: var(--bg3);
  color: var(--text-bright);
}
.cv-anno-btn:disabled {
  opacity: 0.4;
  cursor: default;
}
/* 选中的工具/笔宽：accent 描边 + 浅底，一眼看出当前落在哪一档 */
.cv-anno-btn.is-on {
  border-color: color-mix(in srgb, var(--accent) 60%, transparent);
  background: var(--accent-soft);
  color: var(--accent);
}
.cv-anno-btn--primary {
  border-color: color-mix(in srgb, var(--accent) 55%, transparent);
  background: var(--accent-soft);
  color: var(--accent);
}
.cv-anno-btn--primary:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 26%, transparent);
  color: var(--text-bright);
}
.cv-anno-swatch {
  width: 18px;
  height: 18px;
  padding: 0;
  border: 2px solid transparent;
  border-radius: 50%;
  cursor: pointer;
  transition: 0.12s;
}
.cv-anno-swatch.is-on {
  border-color: var(--text-bright);
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--bg1) 100%, transparent) inset;
}
.cv-anno-sep {
  width: 1px;
  height: 14px;
  margin: 0 3px;
  background: var(--border);
}
/* 底图与标注层严格重叠：标注层 absolute 盖在底图上，坐标才对得上 */
.cv-anno-stage {
  position: relative;
  align-self: center;
  max-width: 100%;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg0);
  overflow: hidden;
}
.cv-anno-base {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
  user-select: none;
}
.cv-anno-layer {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  /* touch-action:none：触屏上手指滑动要画线，不能被当成滚动/缩放 */
  touch-action: none;
  cursor: crosshair;
}
.cv-anno-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: color-mix(in srgb, var(--bg0) 78%, transparent);
  color: var(--text-faint);
  font-size: 11.5px;
}
.cv-anno-note {
  margin: 0;
  font-size: 10px;
  line-height: 1.5;
  color: var(--text-faint);
}
.cv-anno-prompt {
  box-sizing: border-box;
  width: 100%;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--bg0);
  color: var(--text-bright);
  font-family: inherit;
  font-size: 11.5px;
  line-height: 1.5;
  resize: none;
  outline: none;
}
.cv-anno-prompt::placeholder {
  color: var(--text-faint);
}
.cv-anno-prompt:focus {
  border-color: color-mix(in srgb, var(--accent) 60%, var(--border));
}
.cv-anno-hint {
  margin: 0;
  font-size: 10.5px;
  line-height: 1.45;
  color: var(--orange);
}
.cv-anno-foot {
  display: flex;
  align-items: center;
  gap: 6px;
}
.cv-anno-price {
  font-family: var(--mono);
  font-size: 9.5px;
  letter-spacing: 0.2px;
  color: var(--text-faint);
}
</style>
