<template>
  <!-- 右侧面板「文档」工作台：纯 DOM 渲染，不叠加任何原生浏览器表面。
       PDF 由 pdf.js 逐页画进 canvas，工具栏（页码/缩放/适应宽度）全部自绘；
       .docx 由 docx-preview 直接渲染 OOXML；Excel / PPT / 旧版 doc 仍由后端先转成
       PDF，再走 pdf.js 这条渲染链。 -->
  <div class="dc-root" data-test="doc-pane-root">
    <div class="dc-toolbar">
      <AppIcon name="file" :size="13" :stroke-width="2" class="dc-fileicon" />
      <span class="dc-name" :title="currentPath || ''" data-test="dc-name">{{ displayName }}</span>

      <span class="dc-spacer"></span>

      <!-- 翻页只对 PDF 有意义：docx 由 docx-preview 排出连续页面，滚动阅读即可 -->
      <template v-if="ready && !docxMode">
        <button
          class="dc-btn"
          data-test="dc-prev"
          title="上一页"
          :disabled="pageNo <= 1"
          @click="goPage(pageNo - 1)"
        ><AppIcon name="chevron-left" :size="14" :stroke-width="2" /></button>
        <span class="dc-pager" data-test="dc-pager">{{ pageNo }} / {{ pageCount }}</span>
        <button
          class="dc-btn"
          data-test="dc-next"
          title="下一页"
          :disabled="pageNo >= pageCount"
          @click="goPage(pageNo + 1)"
        ><AppIcon name="chevron-right" :size="14" :stroke-width="2" /></button>
      </template>

      <template v-if="ready">
        <span class="dc-sep"></span>

        <button class="dc-btn dc-zoombtn" data-test="dc-zoom-out" title="缩小" @click="zoomBy(-0.2)">−</button>
        <span class="dc-zoom" data-test="dc-zoom">{{ zoomPct }}%</span>
        <button class="dc-btn dc-zoombtn" data-test="dc-zoom-in" title="放大" @click="zoomBy(0.2)">+</button>
        <button class="dc-textbtn" data-test="dc-fit" title="回到按面板宽度适配" @click="resetZoom">适应宽度</button>
      </template>

      <span class="dc-sep"></span>

      <!-- 编辑模式入口：只有文档渲染就绪后才能进（否则没有坐标基准可画） -->
      <button
        class="dc-btn dc-editbtn"
        :class="{ active: editing }"
        data-test="dc-edit-toggle"
        title="编辑 PDF（标注 / 页面操作；Word 文档只读）"
        :disabled="!ready || docxMode"
        @click="toggleEdit"
      >
        <AppIcon name="pencil" :size="14" :stroke-width="2" />
        <span v-if="dirty" class="dc-dirty" data-test="dc-dirty"></span>
      </button>
      <button
        class="dc-btn"
        data-test="dc-reveal"
        title="在文件夹中打开（资源管理器定位到该文件）"
        :disabled="!fileTarget"
        @click="revealInExplorer"
      ><AppIcon name="folder" :size="14" :stroke-width="2" /></button>
      <button
        class="dc-btn"
        data-test="dc-open-system"
        title="用系统默认程序打开"
        :disabled="!fileTarget"
        @click="openInSystem"
      ><AppIcon name="external-link" :size="14" :stroke-width="2" /></button>
      <button
        class="dc-btn"
        data-test="dc-reload"
        title="重新加载"
        :disabled="!currentPath || loading"
        @click="reload"
      ><AppIcon name="refresh" :size="14" :stroke-width="2" /></button>
    </div>

    <!-- 编辑模式第二行：标注工具 + 页面操作 + 撤销重做 + 保存。
         编辑只记录操作（PDF point 坐标系），页面重排要等后端保存时才发生。 -->
    <div v-if="editing" class="dc-edit-toolbar" data-test="dc-edit-toolbar">
      <button
        v-for="t in EDIT_TOOLS"
        :key="t.id"
        class="dc-toolbtn"
        :class="{ active: tool === t.id }"
        :data-test="'dc-tool-' + t.id"
        :title="t.title"
        @click="selectTool(t.id)"
      >{{ t.label }}</button>

      <span class="dc-sep"></span>

      <button
        class="dc-textbtn"
        data-test="dc-edit-delete"
        title="删除选中的标注（也可以按 Delete）"
        :disabled="selectedIndex === null"
        @click="deleteSelected"
      >删除选中</button>

      <span class="dc-sep"></span>

      <button class="dc-textbtn" data-test="dc-page-delete" title="删除当前页（保存时生效）" @click="deleteCurrentPage">删页</button>
      <button class="dc-textbtn" data-test="dc-page-rotate" title="当前页顺时针旋转 90°（保存时生效）" @click="rotateCurrentPage">旋转</button>
      <button class="dc-textbtn" data-test="dc-page-insert-blank" title="在当前页之后插入空白页（保存时生效）" @click="insertBlankPage">插页</button>

      <span class="dc-sep"></span>

      <button class="dc-textbtn" data-test="dc-edit-undo" title="撤销" :disabled="!canUndo" @click="undoEdit">撤销</button>
      <button class="dc-textbtn" data-test="dc-edit-redo" title="重做" :disabled="!canRedo" @click="redoEdit">重做</button>
      <button class="dc-textbtn" data-test="dc-edit-clear" title="清空全部标注与页面操作" :disabled="!dirty" @click="clearEdits">清空</button>

      <span class="dc-spacer"></span>

      <button class="dc-textbtn" data-test="dc-edit-save-copy" title="把编辑结果另存为副本" :disabled="saveBusy" @click="saveEdits('copy')">保存副本</button>
      <button class="dc-textbtn dc-saveprimary" data-test="dc-edit-save-overwrite" title="直接覆盖原文件" :disabled="saveBusy" @click="saveEdits('overwrite')">覆盖保存</button>
    </div>

    <div ref="scrollRef" class="dc-scroll" data-test="dc-scroll" @scroll.passive="onScroll">
      <!-- Word 宿主常驻 DOM（v-show 而非 v-if）：docx-preview 渲染时节点必须已经存在，
           挂在下面的提示链里会因为 loading 状态而拿不到 ref；缩放由 CSS zoom 控制 -->
      <div v-show="docxMode" class="dc-docx" data-test="dc-docx">
        <div ref="docxRef" class="dc-docx-host"></div>
      </div>

      <!-- 页面操作（删页/旋转/插页）不会改动当前 pdf.js 预览：真要重排只能由后端
           保存时执行，所以这里明确提示，避免用户以为按钮没生效 -->
      <div v-if="editing && pendingPageOps.length" class="dc-pageops" data-test="dc-pageops-pending">
        <AppIcon name="info" :size="13" :stroke-width="2" />
        <span>页面操作将在保存时生效，当前预览未重排：共 {{ pageOpsCount }} 项（{{ pageOpsSummary }}）</span>
      </div>

      <!-- Office 转 PDF 进行中：优先于其它状态显示，否则用户会以为点击没生效 -->
      <div v-if="store.officePreviewPending" class="dc-notice" data-test="dc-office-pending">
        <AppIcon name="loader" :size="22" class="icon-spin" />
        <p class="dc-notice-title">正在把 {{ store.officePreviewPending.name }} 转换成 PDF</p>
        <p class="dc-notice-sub">首次转换需要 10~25 秒，之后会命中缓存</p>
      </div>

      <div v-else-if="error" class="dc-notice" data-test="dc-error">
        <AppIcon name="info" :size="22" :stroke-width="1.5" />
        <p class="dc-notice-title">{{ error }}</p>
        <div class="dc-notice-actions">
          <button v-if="fileTarget" class="dc-textbtn" @click="openInSystem">用系统默认程序打开</button>
          <button v-if="fileTarget" class="dc-textbtn" @click="revealInExplorer">在文件夹中打开</button>
        </div>
      </div>

      <div v-else-if="loading" class="dc-notice" data-test="dc-loading">
        <AppIcon name="loader" :size="22" class="icon-spin" />
        <p class="dc-notice-sub">正在读取文档…</p>
      </div>

      <div v-else-if="!ready" class="dc-notice" data-test="dc-empty">
        <AppIcon name="file" :size="26" :stroke-width="1.5" />
        <p class="dc-notice-title">文档阅读器</p>
        <p class="dc-notice-sub">
          在对话里点 PDF / Word / Excel 文件路径，或在右栏文件树里点文档，就会在这里打开
        </p>
      </div>

      <div v-else class="dc-pages">
        <div
          v-for="p in pageCount"
          :key="p"
          class="dc-page"
          :data-page="p"
          :data-test="'dc-page-' + p"
          :style="pageStyle(p)"
        >
          <canvas :ref="(el) => setCanvas(p, el)" class="dc-canvas"></canvas>

          <!-- 编辑覆盖层：viewBox 直接用该页的 PDF point 尺寸，标注坐标可以原样画，
               不需要任何缩放换算（canvas 那边是像素，这里是一直是 point）。 -->
          <svg
            v-if="editing"
            :ref="(el) => setLayer(p, el)"
            class="dc-edit-layer"
            :class="{ 'is-drawing': !!tool }"
            :data-test="'dc-edit-layer-' + p"
            :viewBox="layerViewBox(p)"
            preserveAspectRatio="none"
            @pointerdown="onLayerDown(p, $event)"
            @pointermove="onLayerMove($event)"
            @pointerup="onLayerUp($event)"
            @pointercancel="onLayerCancel"
          >
            <g
              v-for="item in drawItemsFor(p)"
              :key="item.id"
              data-test="dc-annot"
              :data-annot-type="item.type"
              :data-annot-index="item.id"
              :class="{ 'is-selected': selectedIndex === item.id }"
              :transform="itemTransform(item.id)"
              @pointerdown="onAnnotDown(item.id, p, $event)"
            >
              <!-- 高亮：半透明色块，无描边 -->
              <rect
                v-if="item.shape === 'fill'"
                :x="item.rect.x"
                :y="item.rect.y"
                :width="item.rect.w"
                :height="item.rect.h"
                :fill="item.color"
                :fill-opacity="item.opacity"
                stroke="none"
              />
              <!-- 下划线 / 删除线：一条直线画在矩形底边 / 中线 -->
              <line
                v-else-if="item.shape === 'line'"
                :x1="item.x1"
                :y1="item.y1"
                :x2="item.x2"
                :y2="item.y2"
                :stroke="item.color"
                :stroke-width="item.width"
              />
              <!-- 方框 -->
              <rect
                v-else-if="item.shape === 'stroke'"
                :x="item.rect.x"
                :y="item.rect.y"
                :width="item.rect.w"
                :height="item.rect.h"
                fill="none"
                :stroke="item.color"
                :stroke-width="item.width"
              />
              <!-- 箭头：直线 + 箭头多边形 -->
              <g v-else-if="item.shape === 'arrow'">
                <line
                  :x1="item.x1"
                  :y1="item.y1"
                  :x2="item.x2"
                  :y2="item.y2"
                  :stroke="item.color"
                  :stroke-width="item.width"
                />
                <polygon :points="item.head" :fill="item.color" />
              </g>
              <!-- 自由笔 -->
              <polyline
                v-else-if="item.shape === 'ink'"
                :points="item.points"
                fill="none"
                :stroke="item.color"
                :stroke-width="item.width"
                stroke-linejoin="round"
                stroke-linecap="round"
              />
              <!-- 白块遮盖：白色不透明 + 浅色虚线框（提示这里还能选中/撤销） -->
              <rect
                v-else-if="item.shape === 'redact'"
                :x="item.rect.x"
                :y="item.rect.y"
                :width="item.rect.w"
                :height="item.rect.h"
                :fill="item.color"
                stroke="#c9ccd1"
                stroke-width="0.8"
                stroke-dasharray="3 2"
              />
              <!-- 便签 / 印章：矩形 + 文字（印章用红框大写） -->
              <g v-else-if="item.shape === 'boxed'">
                <rect
                  :x="item.rect.x"
                  :y="item.rect.y"
                  :width="item.rect.w"
                  :height="item.rect.h"
                  fill="none"
                  :stroke="item.color"
                  :stroke-width="item.width"
                />
                <text
                  :x="item.textX"
                  :y="item.textY"
                  :text-anchor="item.textAnchor"
                  :font-size="item.fontSize"
                  :fill="item.textColor"
                  font-family="sans-serif"
                >
                  <tspan
                    v-for="(line, li) in item.lines"
                    :key="li"
                    :x="item.textX"
                    :dy="li === 0 ? 0 : item.lineHeight"
                  >{{ line }}</tspan>
                </text>
              </g>
              <!-- 文本框：可选底色矩形 + 文字 -->
              <g v-else-if="item.shape === 'bgtext'">
                <rect
                  v-if="item.bg"
                  :x="item.rect.x"
                  :y="item.rect.y"
                  :width="item.rect.w"
                  :height="item.rect.h"
                  :fill="item.bg"
                  stroke="none"
                />
                <text
                  :x="item.textX"
                  :y="item.textY"
                  :font-size="item.fontSize"
                  dominant-baseline="hanging"
                  :fill="item.textColor"
                  font-family="sans-serif"
                >
                  <tspan
                    v-for="(line, li) in item.lines"
                    :key="li"
                    :x="item.textX"
                    :dy="li === 0 ? 0 : item.lineHeight"
                  >{{ line }}</tspan>
                </text>
              </g>
            </g>

            <!-- 拖拽中的实时预览（松手才生成 op） -->
            <g v-if="draft && draft.page === p" data-test="dc-edit-draft" class="dc-edit-draft">
              <polyline
                v-if="tool === 'ink'"
                :points="draftPoints"
                fill="none"
                :stroke="COLOR_LINE"
                stroke-width="1.5"
                stroke-linejoin="round"
                stroke-linecap="round"
              />
              <line
                v-else-if="tool === 'arrow'"
                :x1="draft.start.x"
                :y1="draft.start.y"
                :x2="draft.current.x"
                :y2="draft.current.y"
                :stroke="COLOR_LINE"
                stroke-width="1.5"
                stroke-dasharray="4 3"
              />
              <rect
                v-else
                :x="draftRect.x"
                :y="draftRect.y"
                :width="draftRect.w"
                :height="draftRect.h"
                fill="none"
                :stroke="COLOR_LINE"
                stroke-width="1"
                stroke-dasharray="4 3"
              />
            </g>
          </svg>

          <!-- 便签/印章/文本框的内联编辑器：拖完矩形直接就位输入 -->
          <div
            v-if="textEditor && textEditor.page === p"
            class="dc-text-editor"
            data-test="dc-text-editor"
            :style="textEditorStyle(textEditor)"
          >
            <textarea
              :ref="(el) => setTextareaEl(el)"
              v-model="textEditor.text"
              class="dc-text-input"
              :placeholder="textEditorPlaceholder"
              @keydown.enter.ctrl.prevent="confirmText"
              @keydown.enter.meta.prevent="confirmText"
            ></textarea>
            <div class="dc-text-actions">
              <button class="dc-textbtn" data-test="dc-text-cancel" @click="cancelText">取消</button>
              <button class="dc-textbtn dc-saveprimary" data-test="dc-text-confirm" @click="confirmText">确定</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';
import {
  EditHistory,
  clampRect,
  normalizeRect,
  pageSizeOf,
  summarizeOps,
  validateOps,
  type EditOp,
  type PageOp,
  type Point,
  type Rect,
} from '../utils/pdfEdit';

const props = defineProps<{ active: boolean }>();

const store = useAppStore();

function readApi(): any {
  try { return (window as any).pywebview?.api ?? null; } catch { return null; }
}

// ── pdf.js 惰性加载 ──
// 主包 + worker 约 1.7MB，只在真正打开文档时才动态 import：不用文档页签的用户
// 不为它付首屏代价。workerSrc 用 ?url 让 Vite 打成静态资源，WebView2 支持模块 worker。
type PdfModule = typeof import('pdfjs-dist');
let pdfjsPromise: Promise<PdfModule> | null = null;

function loadPdfjs(): Promise<PdfModule> {
  if (!pdfjsPromise) {
    pdfjsPromise = (async () => {
      const mod = await import('pdfjs-dist');
      const worker = await import('pdfjs-dist/build/pdf.worker.min.mjs?url');
      mod.GlobalWorkerOptions.workerSrc = worker.default;
      return mod;
    })();
  }
  return pdfjsPromise;
}

// ── docx-preview 惰性加载 ──
// Word 文档不再由后端转 PDF：docx-preview 在渲染层解析 OOXML 直接生成 DOM，点开即见，
// 不拉 Word 进程、也不要求本机装 Office/WPS。库同样按需加载，只用 PDF 的用户不付代价。
type DocxModule = typeof import('docx-preview');
let docxPromise: Promise<DocxModule> | null = null;

function loadDocx(): Promise<DocxModule> {
  if (!docxPromise) docxPromise = import('docx-preview');
  return docxPromise;
}

const scrollRef = ref<HTMLElement | null>(null);
const docxRef = ref<HTMLElement | null>(null);
// 当前文档走哪条渲染链：docx-preview（Word）还是 pdf.js（PDF / 转出来的 Excel、PPT）
const docxMode = ref(false);
const currentPath = ref('');
const loading = ref(false);
const ready = ref(false);
const error = ref('');
const pageCount = ref(0);
const pageNo = ref(1);
// 每页在 scale=1 下的宽高：占位块按它 × 当前缩放渲染，避免图片出来前页面高度跳动
const pageSizes = ref<{ w: number; h: number }[]>([]);
// zoomFactor 是相对「适应宽度」的倍数：面板变宽时 fitScale 重算，用户手动放大的
// 比例仍然保留
const zoomFactor = ref(1);
const fitScale = ref(1);
const scale = computed(() => fitScale.value * zoomFactor.value);
const zoomPct = computed(() => Math.round(scale.value * 100));
const displayName = computed(() => store.docPreviewRequest?.name || '文档');
// 「在文件夹中打开」「用系统默认程序打开」定位的都是源文件：Office 文档显示的
// 是后端转出的缓存 PDF，但用户点开的是磁盘上那个 .docx，所以要优先用 sourcePath
const fileTarget = computed(() => (
  store.docPreviewRequest?.sourcePath || store.docPreviewRequest?.path || currentPath.value
));

// pdf.js 的文档代理对象（类型来自 pdfjs-dist，运行时才有值）
let pdfDoc: { numPages: number; getPage: (n: number) => Promise<any>; destroy?: () => Promise<void> } | null = null;
const canvases = new Map<number, HTMLCanvasElement>();
// 已渲染的「页 → 缩放」键：缩放变化必须重画，滚回原页不重复画
const renderedKeys = new Map<number, string>();
let pageObserver: IntersectionObserver | null = null;
// 页与页之间的间距 + 两侧留白，只在本组件内部使用
const PAGE_GAP = 12;
const PAGE_RING = 12;
let consumedNonce = 0;

// ── 编辑模式 ──
// 坐标一律用 PDF point（原点在页面左上角，y 向下），等于 scale=1 的页面尺寸坐标系。
// 覆盖层 svg 的 viewBox 就是这一尺寸，所以标注坐标可以原样画，不用乘缩放。
// 真正的页面重排 / 内容擦除都发生在后端保存时，这里只维护操作列表（ops）。
type EditTool =
  | 'highlight'
  | 'underline'
  | 'strike'
  | 'rect'
  | 'arrow'
  | 'ink'
  | 'note'
  | 'stamp'
  | 'redact'
  | 'text'
  | 'select';

interface EditToolDef {
  id: EditTool;
  label: string;
  title: string;
}

const EDIT_TOOLS: EditToolDef[] = [
  { id: 'select', label: '选择', title: '选择：点选标注后可拖动移动，Delete 删除' },
  { id: 'highlight', label: '高亮', title: '高亮：拖出一个半透明色块' },
  { id: 'underline', label: '下划线', title: '下划线：拖出下划线' },
  { id: 'strike', label: '删除线', title: '删除线：拖出删除线' },
  { id: 'rect', label: '方框', title: '方框：拖出一个矩形边框' },
  { id: 'arrow', label: '箭头', title: '箭头：从起点拖到终点' },
  { id: 'ink', label: '自由笔', title: '自由笔：按住拖动写字' },
  { id: 'note', label: '便签', title: '文字便签：拖出一块后输入文字' },
  { id: 'stamp', label: '印章', title: '印章：拖出一块后输入文字（保存为大写）' },
  { id: 'redact', label: '白块', title: '白块遮盖：保存时真正擦除，用于脱敏' },
  { id: 'text', label: '文本', title: '文本框：拖出一块后输入文字' },
];

const COLOR_HIGHLIGHT = '#ffd400';
const COLOR_LINE = '#e5484d';
const COLOR_TEXT = '#111111';
const COLOR_WHITE = '#ffffff';
// 拖拽短于这个长度算误触（配合 clampRect 的退化尺寸判断）
const MIN_DRAG = 1;

interface DraftState {
  page: number;
  start: Point;
  current: Point;
  points: Point[];
}

interface TextEditorState {
  tool: 'note' | 'stamp' | 'text';
  page: number;
  rect: Rect;
  text: string;
}

const editing = ref(false);
const tool = ref<EditTool | null>(null);
const ops = ref<EditOp[]>([]);
const history = new EditHistory();
// EditHistory 是普通类实例，push/undo 不会触发视图更新：用计数驱动 canUndo/canRedo 重算
const historyTick = ref(0);
const saveBusy = ref(false);
// 保存流程自己触发的重载不算「用户丢弃改动」：置位期间 confirmDiscardEdits 直接放行
const suppressDiscardConfirm = ref(false);
const draft = ref<DraftState | null>(null);
const textEditor = ref<TextEditorState | null>(null);
const textareaEl = ref<HTMLTextAreaElement | null>(null);
// 选中项用 ops.value 的全局下标标识（drawItems 里的 item.id 就是它）
const selectedIndex = ref<number | null>(null);
// 拖动中的实时位移（SVG transform 预览用，松手才写进历史）
const moveDelta = ref<{ dx: number; dy: number } | null>(null);
let moveState: { index: number; page: number; origin: Point } | null = null;

const dirty = computed(() => ops.value.length > 0);
const canUndo = computed(() => {
  void historyTick.value;
  return history.canUndo;
});
const canRedo = computed(() => {
  void historyTick.value;
  return history.canRedo;
});
// 页面类操作：只记录、不重排预览，需要在界面上显式提示
const pendingPageOps = computed(() => ops.value.filter((op): op is PageOp => op.kind === 'page'));

function pageOpLabel(op: PageOp): string {
  switch (op.op) {
    case 'delete':
      return '删除页';
    case 'rotate':
      return '旋转页';
    case 'move':
      return '移动页';
    default:
      return '插入空白页';
  }
}

const pageOpsSummary = computed(() => {
  const counts = new Map<string, number>();
  for (const op of pendingPageOps.value) {
    const label = pageOpLabel(op);
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return Array.from(counts, ([label, n]) => `${label}×${n}`).join('、');
});

// 提示条上的条数走 summarizeOps，与后端统计口径保持一致
const pageOpsCount = computed(() => summarizeOps(ops.value).pageOps);

function toggleEdit() {
  if (!ready.value) return;
  editing.value = !editing.value;
  if (!editing.value) {
    tool.value = null;
    draft.value = null;
    textEditor.value = null;
  }
}

function selectTool(next: EditTool) {
  // 再点一次同一个工具 = 取消选择（回到「只看不改」的状态，覆盖层不吃鼠标）
  tool.value = tool.value === next ? null : next;
  draft.value = null;
  textEditor.value = null;
  // 换工具就放弃选中，避免「看不见选中框却能被 Delete 删掉」
  if (tool.value !== 'select') {
    selectedIndex.value = null;
    cancelMove();
  }
}

/** 把新快照压进历史并同步到视图 */
function pushOps(next: EditOp[]) {
  history.push(next);
  ops.value = history.current;
  historyTick.value += 1;
}

// ── 单条标注：选中 / 拖动 / 删除 ──
// 坐标取整复用文件下方已有的 roundCoord（函数声明会提升，这里直接用）

/** 拖动预览：只给选中的那一条挂 translate，松手才落盘 */
function itemTransform(id: number): string | undefined {
  if (selectedIndex.value !== id || !moveDelta.value) return undefined;
  const { dx, dy } = moveDelta.value;
  if (!dx && !dy) return undefined;
  return `translate(${dx} ${dy})`;
}

/** 把一条操作整体平移（页面操作不参与移动） */
function shiftOp(op: EditOp, dx: number, dy: number): EditOp {
  if (op.kind === 'annot') {
    if (op.rect) {
      return { ...op, rect: { ...op.rect, x: roundCoord(op.rect.x + dx), y: roundCoord(op.rect.y + dy) } };
    }
    if (op.rects) {
      return {
        ...op,
        rects: op.rects.map((r) => ({ ...r, x: roundCoord(r.x + dx), y: roundCoord(r.y + dy) })),
      };
    }
    if (op.from && op.to) {
      return {
        ...op,
        from: { x: roundCoord(op.from.x + dx), y: roundCoord(op.from.y + dy) },
        to: { x: roundCoord(op.to.x + dx), y: roundCoord(op.to.y + dy) },
      };
    }
    if (op.points) {
      return {
        ...op,
        points: op.points.map((p) => ({ x: roundCoord(p.x + dx), y: roundCoord(p.y + dy) })),
      };
    }
    return op;
  }
  if (op.kind === 'redact' || op.kind === 'text') {
    return { ...op, rect: { ...op.rect, x: roundCoord(op.rect.x + dx), y: roundCoord(op.rect.y + dy) } };
  }
  return op;
}

function detachMoveListeners() {
  if (typeof window === 'undefined') return;
  window.removeEventListener('pointermove', onMovePointerMove);
  window.removeEventListener('pointerup', onMovePointerUp);
}

function cancelMove() {
  detachMoveListeners();
  moveState = null;
  moveDelta.value = null;
}

function onMovePointerMove(ev: PointerEvent) {
  const st = moveState;
  if (!st) return;
  const pt = eventToPoint(st.page, ev);
  moveDelta.value = {
    dx: roundCoord(pt.x - st.origin.x),
    dy: roundCoord(pt.y - st.origin.y),
  };
}

function onMovePointerUp() {
  const st = moveState;
  const delta = moveDelta.value;
  cancelMove();
  if (!st || !delta) return;
  // 位移小于半个点算「只是点选」，不入历史（否则点一下就多一步撤销）
  if (Math.abs(delta.dx) < 0.5 && Math.abs(delta.dy) < 0.5) return;
  pushOps(ops.value.map((op, i) => (i === st.index ? shiftOp(op, delta.dx, delta.dy) : op)));
}

/** 只有「选择」工具下点标注才是选中；其它工具要能正常在标注上继续画 */
function onAnnotDown(index: number, page: number, ev: PointerEvent) {
  if (!editing.value || tool.value !== 'select') return;
  ev.stopPropagation();
  if (ev.preventDefault) ev.preventDefault();
  selectedIndex.value = index;
  const origin = eventToPoint(page, ev);
  moveState = { index, page, origin };
  moveDelta.value = { dx: 0, dy: 0 };
  if (typeof window !== 'undefined') {
    window.addEventListener('pointermove', onMovePointerMove);
    window.addEventListener('pointerup', onMovePointerUp);
  }
}

function deleteSelected() {
  const idx = selectedIndex.value;
  if (idx === null) return;
  const next = ops.value.filter((_, i) => i !== idx);
  selectedIndex.value = null;
  pushOps(next);
}

/** Delete/Backspace 删除选中标注；正在输入文字时不动手 */
function onEditKeydown(ev: KeyboardEvent) {
  if (!editing.value || selectedIndex.value === null) return;
  const target = ev.target as HTMLElement | null;
  const tag = target && target.tagName ? target.tagName.toLowerCase() : '';
  if (tag === 'input' || tag === 'textarea' || (target && target.isContentEditable)) return;
  if (ev.key !== 'Delete' && ev.key !== 'Backspace') return;
  if (ev.preventDefault) ev.preventDefault();
  deleteSelected();
}

// 编辑模式开关时挂/摘键盘监听，避免非编辑态吃掉 Delete 键
watch(editing, (on) => {
  if (typeof window === 'undefined') return;
  if (on) window.addEventListener('keydown', onEditKeydown);
  else window.removeEventListener('keydown', onEditKeydown);
});

function undoEdit() {
  const snapshot = history.undo();
  if (!snapshot) return;
  ops.value = snapshot;
  historyTick.value += 1;
  draft.value = null;
  textEditor.value = null;
}

function redoEdit() {
  const snapshot = history.redo();
  if (!snapshot) return;
  ops.value = snapshot;
  historyTick.value += 1;
  draft.value = null;
  textEditor.value = null;
}

// 清空也作为一步入历史：误点「清空」还能撤销回来
function clearEdits() {
  if (!ops.value.length) return;
  pushOps([]);
}

function currentPage(): number {
  return Math.min(Math.max(1, pageNo.value), Math.max(1, pageCount.value));
}

function deleteCurrentPage() {
  if (!pageCount.value) return;
  const page = currentPage();
  pushOps([...ops.value, { kind: 'page', op: 'delete', page }]);
}

// 同一页连续旋转时把角度累加（90→180→270，再转回 90），避免堆一串 rotate
function rotateCurrentPage() {
  if (!pageCount.value) return;
  const page = currentPage();
  const next = ops.value.slice();
  for (let i = next.length - 1; i >= 0; i -= 1) {
    const op = next[i];
    if (op.kind === 'page' && op.op === 'rotate' && op.page === page) {
      const sum = (op.degrees + 90) % 360;
      const degrees = (sum === 0 ? 90 : sum) as 90 | 180 | 270;
      next[i] = { ...op, degrees };
      pushOps(next);
      return;
    }
  }
  pushOps([...next, { kind: 'page', op: 'rotate', page, degrees: 90 }]);
}

function insertBlankPage() {
  if (!pageCount.value) return;
  const page = currentPage();
  const size = pageSizeOf(pageSizes.value, page) ?? { w: 595, h: 842 };
  pushOps([
    ...ops.value,
    { kind: 'page', op: 'insertBlank', after: page, width: size.w, height: size.h },
  ]);
}

async function saveEdits(mode: 'copy' | 'overwrite') {
  if (saveBusy.value) return;
  // 覆盖保存不可逆（后端只做备份，不保证能回滚用户的心理预期）：先确认一次
  if (mode === 'overwrite' && typeof window.confirm === 'function') {
    let ok = true;
    try {
      ok = window.confirm('覆盖保存会先备份原文件，确认继续？');
    } catch {
      ok = true; // 非浏览器宿主（confirm 实现不可用）不拦
    }
    if (!ok) return;
  }
  // 本地校验先跑一遍：坐标/颜色/页号不合法时连桥都不必打
  const problems = validateOps(ops.value);
  if (problems.length) {
    store.notifyModal('PDF 编辑', `有 ${problems.length} 处问题：${problems[0]}`);
    return;
  }
  if (!ops.value.length) {
    store.notifyModal('PDF 编辑', '当前没有需要保存的编辑');
    return;
  }
  saveBusy.value = true;
  // 保存成功后 store 会把新文件重新开进「文档」页签，nonce 变化会触发 loadDoc；
  // 此时 ops 已清空、再问一句「会丢弃改动」只会让用户困惑，顺手抑制掉
  suppressDiscardConfirm.value = true;
  try {
    const res = await store.savePdfEdits(ops.value, mode);
    if (!res) return; // 失败/被拦下：编辑态与历史都不动，用户可以改完再试
    resetEditState();
    editing.value = false;
  } finally {
    saveBusy.value = false;
    setTimeout(() => { suppressDiscardConfirm.value = false; }, 0);
  }
}

// ── 覆盖层几何 ──

function layerSize(page: number): { w: number; h: number } {
  return pageSizeOf(pageSizes.value, page) ?? { w: 600, h: 800 };
}

function layerViewBox(page: number): string {
  const size = layerSize(page);
  return `0 0 ${size.w} ${size.h}`;
}

// v-for 里的 svg 用函数 ref 收集（模板 ref 在 v-for 下会变成数组）
const layerEls = new Map<number, SVGSVGElement>();

function setLayer(page: number, el: unknown) {
  if (el) layerEls.set(page, el as SVGSVGElement);
  else layerEls.delete(page);
}

function clampNumber(v: number, min: number, max: number): number {
  if (!Number.isFinite(v)) return min;
  return Math.min(Math.max(v, min), max);
}

/** 指针位置 → 该页 PDF point */
function eventToPoint(page: number, ev: PointerEvent): Point {
  const size = layerSize(page);
  const svg = layerEls.get(page);
  // jsdom / 隐藏容器里量到 0 时按页面原始尺寸兜底，避免除零
  const box = svg ? svg.getBoundingClientRect() : null;
  const width = box && box.width > 0 ? box.width : size.w;
  const height = box && box.height > 0 ? box.height : size.h;
  const left = box ? box.left : 0;
  const top = box ? box.top : 0;
  const rawX = ((Number(ev.clientX) || 0) - left) / width * size.w;
  const rawY = ((Number(ev.clientY) || 0) - top) / height * size.h;
  return { x: clampNumber(rawX, 0, size.w), y: clampNumber(rawY, 0, size.h) };
}

function clampedRect(a: Point, b: Point, page: number): Rect | null {
  const rect = clampRect(normalizeRect(a, b), layerSize(page));
  if (rect.w < MIN_DRAG || rect.h < MIN_DRAG) return null;
  return rect;
}

// ── 绘制交互 ──

function buildOp(
  t: Exclude<EditTool, 'note' | 'stamp' | 'text'>,
  page: number,
  start: Point,
  end: Point,
  points: Point[],
): EditOp | null {
  if (t === 'arrow') {
    if (Math.hypot(end.x - start.x, end.y - start.y) < MIN_DRAG) return null;
    return { kind: 'annot', type: 'arrow', page, color: COLOR_LINE, width: 1.5, from: start, to: end };
  }
  if (t === 'ink') {
    if (points.length < 2) return null;
    return { kind: 'annot', type: 'ink', page, color: COLOR_LINE, width: 1.5, points: points.slice() };
  }
  const rect = clampedRect(start, end, page);
  if (!rect) return null;
  if (t === 'redact') return { kind: 'redact', page, rect, fill: COLOR_WHITE };
  if (t === 'highlight') {
    return { kind: 'annot', type: 'highlight', page, color: COLOR_HIGHLIGHT, opacity: 0.35, rect };
  }
  if (t === 'underline' || t === 'strike') {
    return { kind: 'annot', type: t, page, color: COLOR_LINE, width: 1.5, rect };
  }
  return { kind: 'annot', type: 'rect', page, color: COLOR_LINE, width: 1.5, rect };
}

// 拖拽期间把 move/up 听到 window 上：指针移出页面也能继续画；
// 元素自身的 pointermove/pointerup 也照样处理（jsdom 里不一定冒泡到 window），
// 两边都会调，靠 onLayerUp 里先清 draft 做去重。
let watchingDrag = false;

function startDragWatch() {
  if (watchingDrag) return;
  watchingDrag = true;
  window.addEventListener('pointermove', onLayerMove);
  window.addEventListener('pointerup', onLayerUp);
}

function stopDragWatch() {
  if (!watchingDrag) return;
  watchingDrag = false;
  window.removeEventListener('pointermove', onLayerMove);
  window.removeEventListener('pointerup', onLayerUp);
}

function withNewPoint(d: DraftState, pt: Point): DraftState {
  const points = d.points.slice();
  if (tool.value === 'ink') {
    const last = points[points.length - 1];
    // 相邻点太近不记，避免一个 stroke 里塞几千个重复点
    if (!last || Math.hypot(pt.x - last.x, pt.y - last.y) > MIN_DRAG) points.push(pt);
  }
  return { ...d, current: pt, points };
}

function onLayerDown(page: number, ev: PointerEvent) {
  if (!editing.value || !tool.value) return;
  if (textEditor.value) return;
  if (ev.preventDefault) ev.preventDefault();
  const pt = eventToPoint(page, ev);
  draft.value = { page, start: pt, current: pt, points: [pt] };
  startDragWatch();
}

function onLayerMove(ev: PointerEvent) {
  const d = draft.value;
  if (!d) return;
  draft.value = withNewPoint(d, eventToPoint(d.page, ev));
}

function onLayerUp(ev: PointerEvent) {
  const d = draft.value;
  if (!d) return;
  const final = withNewPoint(d, eventToPoint(d.page, ev));
  draft.value = null;
  stopDragWatch();
  const t = tool.value;
  if (!t) return;
  if (t === 'note' || t === 'stamp' || t === 'text') {
    const rect = clampedRect(d.start, final.current, d.page);
    if (rect) openTextEditor(t, d.page, rect);
    return;
  }
  const op = buildOp(t, d.page, d.start, final.current, final.points);
  if (op) pushOps([...ops.value, op]);
}

function onLayerCancel() {
  draft.value = null;
  stopDragWatch();
}

// ── 便签 / 印章 / 文本框的内联编辑器 ──

const textEditorPlaceholder = computed(() => {
  const t = textEditor.value?.tool;
  if (t === 'stamp') return '印章文字（保存为大写）';
  if (t === 'text') return '文本框内容';
  return '便签内容';
});

function setTextareaEl(el: unknown) {
  textareaEl.value = (el as HTMLTextAreaElement | null) ?? null;
}

function openTextEditor(t: 'note' | 'stamp' | 'text', page: number, rect: Rect) {
  textEditor.value = { tool: t, page, rect, text: '' };
  void nextTick(() => {
    const el = textareaEl.value;
    if (el && typeof el.focus === 'function') el.focus();
  });
}

function buildTextOp(t: 'note' | 'stamp' | 'text', page: number, rect: Rect, text: string): EditOp {
  if (t === 'text') {
    return { kind: 'text', page, rect, text, fontSize: 12, color: COLOR_TEXT, bg: null };
  }
  if (t === 'stamp') {
    const fontSize = Math.round(Math.min(Math.max(rect.h * 0.5, 8), 28));
    return {
      kind: 'annot',
      type: 'stamp',
      page,
      rect,
      text: text.toUpperCase(),
      fontSize,
      color: COLOR_LINE,
    };
  }
  const fontSize = Math.round(Math.min(Math.max(rect.h * 0.22, 8), 14));
  return { kind: 'annot', type: 'note', page, rect, text, fontSize, color: COLOR_LINE };
}

function confirmText() {
  const editor = textEditor.value;
  if (!editor) return;
  const text = editor.text.trim();
  textEditor.value = null;
  if (!text) return; // 空文本算取消，不进历史
  pushOps([...ops.value, buildTextOp(editor.tool, editor.page, editor.rect, text)]);
}

function cancelText() {
  textEditor.value = null;
}

function textEditorStyle(editor: TextEditorState) {
  const s = scale.value;
  return {
    left: `${Math.round(editor.rect.x * s)}px`,
    top: `${Math.round(editor.rect.y * s)}px`,
    width: `${Math.max(180, Math.round(editor.rect.w * s))}px`,
  };
}

// ── 标注绘制模型 ──
// 把 EditOp 归一成「画什么形状」的描述，模板里只按 shape 分支，避免一堆类型判断。

type DrawShape = 'fill' | 'line' | 'stroke' | 'arrow' | 'ink' | 'boxed' | 'redact' | 'bgtext';

interface DrawItem {
  id: number;
  type: string;
  shape: DrawShape;
  rect: Rect;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  color: string;
  textColor: string;
  bg: string;
  width: number;
  opacity: number;
  fontSize: number;
  lines: string[];
  points: string;
  head: string;
  textX: number;
  textY: number;
  textAnchor: 'start' | 'middle';
  lineHeight: number;
}

function roundCoord(v: number): number {
  return Math.round(v * 100) / 100;
}

function splitLines(text: string): string[] {
  return String(text || '').split(/\r?\n/);
}

function arrowHead(from: Point, to: Point, width: number): string {
  const angle = Math.atan2(to.y - from.y, to.x - from.x);
  const size = Math.max(6, width * 4);
  const spread = Math.PI / 7;
  const p1 = `${roundCoord(to.x - Math.cos(angle - spread) * size)},${roundCoord(to.y - Math.sin(angle - spread) * size)}`;
  const p2 = `${roundCoord(to.x - Math.cos(angle + spread) * size)},${roundCoord(to.y - Math.sin(angle + spread) * size)}`;
  return `${to.x},${to.y} ${p1} ${p2}`;
}

function toDrawItem(op: EditOp, id: number): DrawItem {
  const base: DrawItem = {
    id,
    type: 'annot',
    shape: 'stroke',
    rect: { x: 0, y: 0, w: 0, h: 0 },
    x1: 0,
    y1: 0,
    x2: 0,
    y2: 0,
    color: COLOR_LINE,
    textColor: COLOR_TEXT,
    bg: '',
    width: 1.5,
    opacity: 1,
    fontSize: 10,
    lines: [],
    points: '',
    head: '',
    textX: 0,
    textY: 0,
    textAnchor: 'start',
    lineHeight: 13,
  };

  if (op.kind === 'page') return base; // 页面操作不参与绘制

  if (op.kind === 'redact') {
    return { ...base, type: 'redact', shape: 'redact', rect: op.rect, color: op.fill || COLOR_WHITE };
  }

  if (op.kind === 'text') {
    const fontSize = op.fontSize > 0 ? op.fontSize : 12;
    return {
      ...base,
      type: 'text',
      shape: 'bgtext',
      rect: op.rect,
      color: op.color,
      textColor: op.color,
      bg: op.bg || '',
      fontSize,
      lines: splitLines(op.text),
      textX: op.rect.x + 2,
      textY: op.rect.y + 2,
      lineHeight: fontSize * 1.3,
    };
  }

  const rect = op.rect ?? (op.rects && op.rects[0]) ?? base.rect;
  const color = op.color || COLOR_LINE;
  const width = op.width && op.width > 0 ? op.width : 1.5;
  const fontSize = op.fontSize && op.fontSize > 0 ? op.fontSize : 10;

  switch (op.type) {
    case 'highlight':
      return {
        ...base,
        type: 'highlight',
        shape: 'fill',
        rect,
        color,
        opacity: typeof op.opacity === 'number' ? op.opacity : 0.35,
      };
    case 'underline':
      return {
        ...base,
        type: 'underline',
        shape: 'line',
        rect,
        color,
        width,
        x1: rect.x,
        y1: rect.y + rect.h,
        x2: rect.x + rect.w,
        y2: rect.y + rect.h,
      };
    case 'strike':
      return {
        ...base,
        type: 'strike',
        shape: 'line',
        rect,
        color,
        width,
        x1: rect.x,
        y1: rect.y + rect.h / 2,
        x2: rect.x + rect.w,
        y2: rect.y + rect.h / 2,
      };
    case 'arrow': {
      const from = op.from ?? { x: 0, y: 0 };
      const to = op.to ?? { x: 0, y: 0 };
      return {
        ...base,
        type: 'arrow',
        shape: 'arrow',
        rect,
        color,
        width,
        x1: from.x,
        y1: from.y,
        x2: to.x,
        y2: to.y,
        head: arrowHead(from, to, width),
      };
    }
    case 'ink':
      return {
        ...base,
        type: 'ink',
        shape: 'ink',
        rect,
        color,
        width,
        points: (op.points ?? []).map((p) => `${p.x},${p.y}`).join(' '),
      };
    case 'note':
      return {
        ...base,
        type: 'note',
        shape: 'boxed',
        rect,
        color,
        width: 1,
        fontSize,
        lines: splitLines(op.text || ''),
        textColor: COLOR_TEXT,
        textX: rect.x + 3,
        textY: rect.y + fontSize,
        lineHeight: fontSize * 1.35,
      };
    case 'stamp':
      return {
        ...base,
        type: 'stamp',
        shape: 'boxed',
        rect,
        color,
        width: 1.5,
        fontSize,
        lines: splitLines((op.text || '').toUpperCase()),
        textColor: color,
        textX: rect.x + rect.w / 2,
        textY: rect.y + rect.h / 2 + fontSize * 0.35,
        textAnchor: 'middle',
        lineHeight: fontSize * 1.2,
      };
    default:
      return { ...base, type: op.type, shape: 'stroke', rect, color, width };
  }
}

const drawItems = computed(() => {
  const byPage = new Map<number, DrawItem[]>();
  // 逐条记住它在 ops.value 里的「全局下标」：选中/拖动/删除都要按这个下标回写，
  // 用页内序号会在多页都有标注时删错条目
  const entriesByPage = new Map<number, Array<{ op: EditOp; index: number }>>();
  ops.value.forEach((op, index) => {
    if (op.kind === 'page') return;
    const list = entriesByPage.get(op.page);
    if (list) list.push({ op, index });
    else entriesByPage.set(op.page, [{ op, index }]);
  });
  for (const [page, entries] of entriesByPage) {
    byPage.set(page, entries.map(({ op, index }) => toDrawItem(op, index)));
  }
  return byPage;
});

function drawItemsFor(page: number): DrawItem[] {
  return drawItems.value.get(page) ?? [];
}

// 拖拽中的实时预览
const draftRect = computed<Rect>(() => {
  const d = draft.value;
  if (!d) return { x: 0, y: 0, w: 0, h: 0 };
  return normalizeRect(d.start, d.current);
});

const draftPoints = computed(() =>
  (draft.value ? draft.value.points.map((p) => `${p.x},${p.y}`).join(' ') : ''),
);

/** 丢弃未保存的编辑前先问一句；没有 confirm（非浏览器宿主）时不拦 */
function confirmDiscardEdits(): boolean {
  if (suppressDiscardConfirm.value) return true;
  if (!dirty.value) return true;
  if (typeof window.confirm !== 'function') return true;
  try {
    return window.confirm('当前 PDF 有未保存的编辑，继续打开会丢弃这些改动。确定继续吗？');
  } catch {
    return true;
  }
}

function resetEditState() {
  history.reset();
  ops.value = [];
  historyTick.value += 1;
  draft.value = null;
  textEditor.value = null;
  tool.value = null;
  selectedIndex.value = null;
  cancelMove();
  editing.value = false;
  stopDragWatch();
}

function setCanvas(page: number, el: unknown) {
  if (el) canvases.set(page, el as HTMLCanvasElement);
  else canvases.delete(page);
}

function pageStyle(page: number) {
  const size = pageSizes.value[page - 1];
  if (!size) return {};
  return {
    width: `${Math.floor(size.w * scale.value)}px`,
    height: `${Math.floor(size.h * scale.value)}px`,
  };
}

function messageOf(err: unknown): string {
  if (!err) return '';
  if (typeof err === 'string') return err;
  const msg = (err as { message?: unknown }).message;
  return msg ? String(msg) : String(err);
}

// ── 缩放与适配 ──

function computeFitScale() {
  const el = scrollRef.value;
  const first = pageSizes.value[0];
  if (!el || !first || first.w <= 0) return;
  // 减去左右留白与滚动条余量；量不到（不可见/测试环境）时保持原值不抖动
  const avail = (el.clientWidth || 0) - PAGE_RING * 2 - 10;
  if (avail <= 0) return;
  fitScale.value = Math.min(3, Math.max(0.2, avail / first.w));
}

function clearRendered() {
  renderedKeys.clear();
}

async function zoomBy(delta: number) {
  const next = Math.min(4, Math.max(0.3, Math.round((zoomFactor.value + delta) * 100) / 100));
  if (next === zoomFactor.value) return;
  zoomFactor.value = next;
  // docx 没有 canvas 要重画，改一下 CSS zoom 就够
  if (docxMode.value) {
    applyDocxZoom();
    return;
  }
  clearRendered();
  await nextTick();
  renderVisible();
}

async function resetZoom() {
  if (zoomFactor.value === 1) return;
  zoomFactor.value = 1;
  if (docxMode.value) {
    applyDocxZoom();
    return;
  }
  clearRendered();
  await nextTick();
  renderVisible();
}

// ── 逐页渲染 ──

// 每页在跑的渲染任务：缩放/滚动会连打招呼，两个 render 往同一块 canvas 上画
// 会叠成残影，换比例时必须把旧任务 cancel 掉
const inflightTasks = new Map<number, { key: string; task: { cancel: () => void; promise: Promise<void> } }>();

async function renderPage(page: number) {
  const canvas = canvases.get(page);
  if (!canvas || !pdfDoc) return;
  const pageProxy = await pdfDoc.getPage(page);
  const key = scale.value.toFixed(3);
  if (renderedKeys.get(page) === key) return;
  const prev = inflightTasks.get(page);
  if (prev) {
    if (prev.key === key) return; // 同比例正在画，等它
    try { prev.task.cancel(); } catch { /* 已结束，忽略 */ }
  }
  renderedKeys.set(page, key);
  // canvas 位图尺寸必须由调用方按 viewport 像素定好：pdf.js v6 传 canvas 参数时
  // 不会自己改尺寸（实测留默认 300×150 时，整页内容被压进那一小片再被 CSS 拉伸，
  // 就成了一团看不清的糊字）。位图按 dpr 放大、CSS 尺寸缩回去，高分屏才清晰。
  const dpr = Math.min(2, Math.max(1, window.devicePixelRatio || 1));
  const viewport = pageProxy.getViewport({ scale: scale.value * dpr });
  canvas.width = Math.max(1, Math.floor(viewport.width));
  canvas.height = Math.max(1, Math.floor(viewport.height));
  const task = pageProxy.render({ canvas, viewport });
  inflightTasks.set(page, { key, task });
  try {
    await task.promise;
    canvas.style.width = `${Math.floor(viewport.width / dpr)}px`;
    canvas.style.height = `${Math.floor(viewport.height / dpr)}px`;
  } catch {
    // 渲染失败/被取消（该页损坏、被中途切走、换比例）：清掉标记，下次进入视口重试
    if (renderedKeys.get(page) === key) renderedKeys.delete(page);
  } finally {
    const current = inflightTasks.get(page);
    if (current && current.key === key) inflightTasks.delete(page);
  }
}

function teardownObserver() {
  if (pageObserver) {
    pageObserver.disconnect();
    pageObserver = null;
  }
}

function setupObserver() {
  teardownObserver();
  const el = scrollRef.value;
  const nodes = el ? Array.from(el.querySelectorAll<HTMLElement>('.dc-page')) : [];
  if (!el || !nodes.length) return;
  if (typeof IntersectionObserver === 'undefined') return; // 兜底：renderVisible 全量画
  pageObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const page = Number((entry.target as HTMLElement).dataset.page || 0);
      if (page) void renderPage(page);
    }
  }, { root: el, rootMargin: '300px 0px' });
  nodes.forEach(node => pageObserver?.observe(node));
}

// 兜底渲染：视口附近（含 300px 预取带）的页直接画，不依赖 IntersectionObserver 是否
// 已派发首帧回调（缩放后重建观察者时也走这条，保证"当前看到的页立刻出新比例"）
function renderVisible() {
  const el = scrollRef.value;
  if (!el) return;
  const rect = el.getBoundingClientRect();
  el.querySelectorAll<HTMLElement>('.dc-page').forEach((node) => {
    const box = node.getBoundingClientRect();
    if (box.bottom >= rect.top - 300 && box.top <= rect.bottom + 300) {
      void renderPage(Number(node.dataset.page || 0));
    }
  });
}

// ── 页码与滚动 ──

function onScroll() {
  const el = scrollRef.value;
  if (!el || !pageCount.value) return;
  const rect = el.getBoundingClientRect();
  let current = 1;
  el.querySelectorAll<HTMLElement>('.dc-page').forEach((node) => {
    const box = node.getBoundingClientRect();
    if (box.top - rect.top <= 16) current = Number(node.dataset.page || current);
  });
  pageNo.value = current;
}

function goPage(target: number) {
  const el = scrollRef.value;
  const clamped = Math.min(Math.max(1, target), pageCount.value || 1);
  if (!el) return;
  const node = el.querySelector<HTMLElement>(`.dc-page[data-page="${clamped}"]`);
  if (node) {
    const offset = node.getBoundingClientRect().top - el.getBoundingClientRect().top;
    const top = Math.max(0, el.scrollTop + offset - PAGE_GAP);
    if (typeof el.scrollTo === 'function') el.scrollTo({ top, behavior: 'smooth' });
    else el.scrollTop = top;
  }
  pageNo.value = clamped;
}

// ── 载入与清理 ──

async function destroyDoc() {
  teardownObserver();
  const doc = pdfDoc;
  pdfDoc = null;
  renderedKeys.clear();
  canvases.clear();
  dropDocx();
  // 换文档时把没画完的渲染任务撤掉，避免旧任务往新 canvas 上画
  for (const entry of inflightTasks.values()) {
    try { entry.task.cancel(); } catch { /* 已结束，忽略 */ }
  }
  inflightTasks.clear();
  if (doc && typeof doc.destroy === 'function') {
    try { await doc.destroy(); } catch { /* 关闭失败不影响后续载入 */ }
  }
}

// ── docx 渲染 ──

/** 清掉上一份 Word 渲染结果（换文档/重载时先抹掉，避免闪一下旧内容） */
function dropDocx() {
  const host = docxRef.value;
  if (host) host.innerHTML = '';
  docxMode.value = false;
}

// data URL → ArrayBuffer：这里不用 fetch(data:)，WebView2 对超大 data URL 的 fetch
// 行为有实现差异，手动解码最稳（32MB 上限内的文档耗时也是毫秒级）
function dataUrlToArrayBuffer(dataUrl: string): ArrayBuffer {
  const comma = dataUrl.indexOf(',');
  const b64 = comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl;
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

/** docx 字节 → DOM（docx-preview 自己按分页符切成多张"纸"） */
async function renderDocx(dataUrl: string) {
  // 先切到 docx 视图：容器节点要等这一轮 DOM 更新才存在，ref 才有值
  docxMode.value = true;
  await nextTick();
  const host = docxRef.value;
  if (!host) throw new Error('文档容器未就绪');
  const mod = await loadDocx();
  host.innerHTML = '';
  await mod.renderAsync(dataUrlToArrayBuffer(dataUrl), host, host, {
    className: 'docx',
    inWrapper: true,
    breakPages: true,
    ignoreWidth: false,
    ignoreHeight: false,
    renderHeaders: true,
    renderFooters: true,
    renderFootnotes: true,
    useBase64URL: true,
  });
}

/** 「适应宽度」：docx 页宽由文档页面设置决定（A4 = 21cm），量一次换算比例 */
function computeDocxFit() {
  const host = docxRef.value;
  const wrapper = host?.querySelector('.docx-wrapper') as HTMLElement | null;
  const page = host?.querySelector('.docx-wrapper > section') as HTMLElement | null;
  const el = scrollRef.value;
  if (!host || !wrapper || !page || !el) return;
  // CSS zoom 会改变 offsetWidth，量之前先把缩放归零
  wrapper.style.zoom = '1';
  const pageWidth = page.offsetWidth || 0;
  const avail = (el.clientWidth || 0) - PAGE_RING * 2 - 10;
  if (pageWidth <= 0 || avail <= 0) return;
  fitScale.value = Math.min(3, Math.max(0.2, avail / pageWidth));
}

/** 把 scale（fitScale × zoomFactor）落到 docx 上：用 CSS zoom 让布局尺寸跟着变 */
function applyDocxZoom() {
  const host = docxRef.value;
  const wrapper = host?.querySelector('.docx-wrapper') as HTMLElement | null;
  if (!wrapper) return;
  const z = Math.min(4, Math.max(0.2, scale.value));
  wrapper.style.zoom = String(Math.round(z * 1000) / 1000);
}

async function loadDoc(path: string) {
  if (!path) return;
  // 有未保存的编辑时先征询：换文档意味着这些标注会丢
  if (!confirmDiscardEdits()) return;
  currentPath.value = path;
  error.value = '';
  loading.value = true;
  ready.value = false;
  pageCount.value = 0;
  pageSizes.value = [];
  pageNo.value = 1;
  zoomFactor.value = 1;
  resetEditState();
  await destroyDoc();

  const api = readApi();
  if (!api || typeof api.read_file_preview !== 'function') {
    loading.value = false;
    error.value = '当前宿主不支持应用内文档预览';
    return;
  }
  let res: any = null;
  try {
    res = await api.read_file_preview(path);
  } catch (err) {
    loading.value = false;
    error.value = `读取文档失败：${messageOf(err)}`;
    return;
  }
  if (!res || !res.ok) {
    loading.value = false;
    error.value = (res && res.msg) || '读取文档失败';
    return;
  }
  // docx：后端把原始 OOXML 字节 base64 过桥，这里直接渲染，不经过任何 PDF 转换
  if (res.kind === 'docx') {
    if (typeof res.data_url !== 'string' || !res.data_url) {
      loading.value = false;
      error.value = 'Word 文档内容为空，无法渲染';
      return;
    }
    try {
      await renderDocx(res.data_url);
      loading.value = false;
      ready.value = true;
      await nextTick();
      computeDocxFit();
      applyDocxZoom();
    } catch (err) {
      loading.value = false;
      // JSZip / docx-preview 的报错是英文的，这里补一句人话并指向兜底入口
      error.value = `Word 文档渲染失败：${messageOf(err)}。可点「用系统默认程序打开」检查文件本身`;
    }
    return;
  }
  // 其余能进这里的只有 PDF：Excel / PPT / 旧版 doc 已由调用方先转成 PDF
  if (res.kind !== 'pdf' || typeof res.data_url !== 'string') {
    loading.value = false;
    error.value = '该文件不是可渲染的 PDF';
    return;
  }

  try {
    const pdfjs = await loadPdfjs();
    pdfDoc = await pdfjs.getDocument({
      url: res.data_url,
      // 没随包带 cmaps：让 pdf.js 用系统字体兜底非嵌入字体的中文
      // （Office 转出的 PDF 内嵌子集字体，通常用不到这条兜底）
      useSystemFonts: true,
    }).promise;
    pageCount.value = pdfDoc.numPages || 0;
    const sizes: { w: number; h: number }[] = [];
    for (let i = 1; i <= pageCount.value; i += 1) {
      const pageProxy = await pdfDoc.getPage(i);
      const viewport = pageProxy.getViewport({ scale: 1 });
      sizes.push({ w: viewport.width, h: viewport.height });
    }
    pageSizes.value = sizes;
    loading.value = false;
    ready.value = true;
    await nextTick();
    computeFitScale();
    await nextTick();
    setupObserver();
    renderVisible();
  } catch (err) {
    loading.value = false;
    error.value = `PDF 渲染失败：${messageOf(err)}`;
  }
}

function consumeDocRequest() {
  const req = store.docPreviewRequest;
  if (!req || !req.path) return;
  // 页签/面板没激活时等激活（否则量不到容器宽度，缩放会算错）
  if (!props.active) return;
  // 别的会话的请求不在当前视图消费：切到该会话时由 active 变化再触发
  if (req.session && req.session !== store.currentSessionName) return;
  if (store.docPreviewNonce === consumedNonce) return;
  consumedNonce = store.docPreviewNonce;
  void loadDoc(req.path);
}

function reload() {
  if (!currentPath.value) return;
  void loadDoc(currentPath.value);
}

function openInSystem() {
  const target = fileTarget.value;
  if (!target) return;
  void store.openInSystem(target);
}

// 在资源管理器里定位到源文件（打开所在目录并选中该文件）
function revealInExplorer() {
  const target = fileTarget.value;
  if (!target) return;
  void store.revealInExplorer(target);
}

// nonce 变化 = 新的打开请求（同一文件重复点击也会自增，watch 必触发）
watch(() => store.docPreviewNonce, consumeDocRequest);
watch(() => props.active, (active) => { if (active) consumeDocRequest(); });

// 面板宽度变化：重算「适应宽度」比例并重画（缩放因子保留）
let resizeObserver: ResizeObserver | null = null;

onMounted(() => {
  consumeDocRequest();
  const el = scrollRef.value;
  if (el && typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(() => {
      if (!ready.value) return;
      // docx 的内容排版与面板宽度无关：只重算缩放比并重新套上去，不重排
      if (docxMode.value) {
        computeDocxFit();
        applyDocxZoom();
        return;
      }
      const prev = fitScale.value;
      computeFitScale();
      if (Math.abs(fitScale.value - prev) < 0.001) return;
      clearRendered();
      void nextTick(() => renderVisible());
    });
    resizeObserver.observe(el);
  }
});

onBeforeUnmount(() => {
  if (resizeObserver) {
    try { resizeObserver.disconnect(); } catch { /* 忽略 */ }
    resizeObserver = null;
  }
  stopDragWatch();
  cancelMove();
  if (typeof window !== 'undefined') {
    window.removeEventListener('keydown', onEditKeydown);
  }
  void destroyDoc();
});
</script>

<style scoped>
.dc-root {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  width: 100%;
}
.dc-toolbar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.dc-fileicon {
  color: var(--text-dim);
  flex-shrink: 0;
}
.dc-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text);
  max-width: 46%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dc-spacer {
  flex: 1;
  min-width: 4px;
}
.dc-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 6px;
  border: none;
  background: none;
  color: var(--text-dim);
  font-family: inherit;
  cursor: pointer;
  transition: 0.15s;
  flex-shrink: 0;
}
.dc-btn:hover:not(:disabled) {
  background: var(--bg3);
  color: var(--text);
}
.dc-btn:disabled {
  opacity: 0.4;
  cursor: default;
}
.dc-zoombtn {
  font-size: 15px;
  line-height: 1;
}
.dc-pager,
.dc-zoom {
  font-size: 11.5px;
  color: var(--text-dim);
  min-width: 42px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
.dc-sep {
  width: 1px;
  height: 16px;
  margin: 0 4px;
  background: var(--border);
  flex-shrink: 0;
}
.dc-textbtn {
  padding: 4px 8px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text-dim);
  font-size: 11.5px;
  font-family: inherit;
  cursor: pointer;
  transition: 0.15s;
  flex-shrink: 0;
}
.dc-textbtn:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.dc-scroll {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow: auto;
  background: var(--bg1);
}
.dc-pages {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 12px;
}
.dc-page {
  position: relative;
  background: #fff;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.16);
  border-radius: 2px;
  overflow: hidden;
  flex-shrink: 0;
}
.dc-canvas {
  display: block;
}
/* ── Word（docx-preview）渲染 ── */
.dc-docx {
  padding: 12px;
}
/* docx-preview 自带的 wrapper 是灰底 + 默认内边距，这里跟随面板主题；
   纸张观感与 PDF 页保持一致，缩放由 wrapper 上的 CSS zoom 控制 */
.dc-docx :deep(.docx-wrapper) {
  padding: 0;
  background: transparent;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}
.dc-docx :deep(.docx-wrapper > section.docx) {
  margin: 0;
  background: #fff;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.16);
  border-radius: 2px;
}
.dc-notice {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 24px;
  text-align: center;
  color: var(--text-faint);
}
.dc-notice-title {
  margin: 4px 0 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-dim);
  max-width: 90%;
  word-break: break-all;
}
.dc-notice-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: center;
}
.dc-notice-sub {
  margin: 0;
  font-size: 12px;
  max-width: 88%;
  line-height: 1.6;
}

/* ── 编辑模式 ── */
.dc-editbtn {
  position: relative;
}
.dc-editbtn.active {
  background: var(--accent-soft);
  color: var(--accent);
}
/* 有未保存改动的小圆点 */
.dc-dirty {
  position: absolute;
  top: 3px;
  right: 3px;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--accent);
}
.dc-edit-toolbar {
  display: flex;
  align-items: center;
  gap: 3px;
  padding: 5px 8px;
  flex-wrap: wrap;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.dc-toolbtn {
  padding: 3px 7px;
  border-radius: 6px;
  border: 1px solid transparent;
  background: none;
  color: var(--text-dim);
  font-size: 11.5px;
  font-family: inherit;
  cursor: pointer;
  transition: 0.15s;
  flex-shrink: 0;
}
.dc-toolbtn:hover {
  background: var(--bg3);
  color: var(--text);
}
.dc-toolbtn.active {
  background: var(--bg3);
  border-color: var(--accent);
  color: var(--accent);
}
.dc-textbtn:disabled {
  opacity: 0.45;
  cursor: default;
}
.dc-saveprimary {
  border-color: var(--accent);
  color: var(--accent);
}
.dc-pageops {
  position: sticky;
  top: 0;
  z-index: 6;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-dim);
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
}
/* 覆盖层铺满该页：viewBox 就是 PDF point，坐标无需换算。
   没选工具时不吃鼠标（保证能正常滚动），选了工具才接管并换成十字光标 */
.dc-edit-layer {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  z-index: 2;
  pointer-events: none;
  touch-action: none;
}
.dc-edit-layer.is-drawing {
  pointer-events: all;
  cursor: crosshair;
}
.dc-edit-draft {
  pointer-events: none;
}
/* 选中的标注：外发光 + 可拖动光标（选择工具下才有意义） */
.dc-edit-layer > g.is-selected {
  filter: drop-shadow(0 0 2px var(--accent));
  cursor: move;
}
.dc-text-editor {
  position: absolute;
  z-index: 5;
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 6px;
  background: var(--bg2);
  border: 1px solid var(--accent);
  border-radius: 6px;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.28);
}
.dc-text-input {
  min-height: 56px;
  resize: vertical;
  padding: 4px 6px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg1);
  color: var(--text);
  font-family: inherit;
  font-size: 12px;
  line-height: 1.4;
}
.dc-text-actions {
  display: flex;
  gap: 4px;
  justify-content: flex-end;
}
</style>