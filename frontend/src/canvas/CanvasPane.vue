<template>
  <!-- 无限画布工作台：四类节点（image/video/prompt/generate）由 vue-flow 按
       store 里的 nodes/edges 渲染，节点视觉见 src/canvas/nodes/* -->
  <!-- 未激活过就整个不渲染：右面板根节点本身已经打过 right_panel 分区标记，
       这里再挂一个嵌套分区只在画布真的要用时才值得（也避免每次挂右面板都
       多两层主题分区伪元素） -->
  <div
    v-if="active || flowReady"
    ref="rootRef"
    class="cv-root"
    data-theme-region="right_panel"
    data-test="canvas-pane-root"
    :style="gridVars"
  >
    <!-- 画布只有一种形态：节点画布（vue-flow）。原 Excalidraw「自由画布」档已移除
         ——要的是「A 图改出 B 图、两图连线」的素材链路，纯涂画板与之无关 -->
    <!-- 只有页签真正被激活过才挂载 vue-flow：它会注册全局 store、ResizeObserver
         和 window resize 监听，用户没打开过画布页签时不该付这份初始化的代价。
         挂载后不再卸载，避免来回切页签时丢视口位置 -->
    <VueFlow
      v-if="flowReady"
      id="canvas-flow"
      class="cv-flow"
      data-test="canvas-flow"
      :nodes="flowNodes"
      :edges="flowEdges"
      :node-types="nodeTypes"
      :min-zoom="0.2"
      :max-zoom="2"
      :zoom-on-double-click="false"
      :edges-updatable="false"
      @pane-click="onPaneClick"
      @node-click="onNodeClick"
      @node-drag-stop="onNodeDragStop"
      @connect="onConnect"
      @edge-double-click="onEdgeDoubleClick"
    />
    <div v-else class="cv-idle">画布尚未激活</div>

    <!-- 缩放控件自己画：vue-flow 的 Controls 组件用的是它自带的浅色主题，
         与本项目的 CSS 变量体系对不上，自绘只花二十行还能跟着主题走 -->
    <div class="cv-zoom" data-test="canvas-zoom">
      <button class="cv-zbtn" data-test="canvas-zoom-out" title="缩小" @click="zoomOut()">－</button>
      <span class="cv-zval">{{ zoomPercent }}%</span>
      <button class="cv-zbtn" data-test="canvas-zoom-in" title="放大" @click="zoomIn()">＋</button>
      <button class="cv-zbtn cv-zfit" data-test="canvas-fit" title="适应内容" @click="fitView()">适应</button>
    </div>

    <!-- 操作提示：桥缺失 / 发送失败时浮在顶部中央。刻意不做成静默 console：
         用户点了「送去修改」却什么都没发生，是最难自诊断的一类故障 -->
    <div v-if="tip" class="cv-tip" data-test="canvas-tip">{{ tip }}</div>

    <!-- 最近产出托盘（左下角）：对话里直接生成的图先停在这里，用户点哪张才落哪张。
         没有内容时整块不渲染，空白浮层会平白挡住画布左下角的节点 -->
    <div
      v-if="recentOutputs.length"
      class="cv-tray"
      data-test="canvas-recent-tray"
    >
      <div class="cv-tray-head">
        <span class="cv-tray-title">最近产出</span>
        <span class="cv-tray-count">{{ recentOutputs.length }}</span>
        <span class="cv-tray-spacer"></span>
        <button
          class="cv-tray-clear"
          data-test="canvas-recent-clear"
          title="清空托盘（不影响画布上的节点）"
          @click="onClearTray()"
        >清空</button>
      </div>
      <div class="cv-tray-list">
        <button
          v-for="item in recentOutputs"
          :key="item.path"
          class="cv-tray-item"
          data-test="canvas-recent-item"
          :title="item.path"
          @click="onTrayPick(item)"
        >
          <!-- 有 url 画缩略图，没有就退回文件名占位：两者都不出现破图图标 -->
          <img v-if="item.url" class="cv-tray-img" :src="item.url" :alt="trayName(item.path)" />
          <span v-else class="cv-tray-ph">IMG</span>
          <span class="cv-tray-name">{{ trayName(item.path) }}</span>
        </button>
      </div>
    </div>

    <!-- 内嵌对话坞：浮在画布容器右下偏中（absolute，不用 fixed——右面板分区
         有 isolation，fixed 会被其它分区的层叠上下文盖住） -->
    <CanvasDock />

    <!-- 画笔标注浮层：打开期间盖住整块画布；底图只显示、笔迹画在透明层上，
         合成交给后端（跨源图片 drawImage 进 canvas 会标脏，toBlob 会抛错） -->
    <AnnotationDialog
      v-if="annotateTarget"
      :url="annotateTarget.url"
      :title="annotateTarget.title"
      :prompt="annotateTarget.prompt"
      :submitting="annotateSubmitting"
      @close="closeAnnotate"
      @submit="onAnnotateSubmit"
    />
  </div>
</template>

<script setup lang="ts">
import {
  computed,
  defineComponent,
  h,
  markRaw,
  ref,
  watch,
  type Component,
} from 'vue';
import {
  VueFlow,
  useVueFlow,
  type Connection,
  type Edge,
  type EdgeMouseEvent,
  type Node,
  type NodeMouseEvent,
  // 仅类型：NodeTypesObject 由包内 ./types 汇总导出，被 index.d.ts 的 `export *` 带出；
  // 只做类型断言，绝不改具名运行时导出（该包运行时导出面较窄）
  type NodeTypesObject,
} from '@vue-flow/core';
// 只引布局样式（定位/层级），不引 theme-default.css：
// 那套主题把节点/边颜色写死在 --vf-* 上，会和本项目主题变量打架
import '@vue-flow/core/dist/style.css';
import {
  useCanvasStore,
  CANVAS_DEFAULT_NODE_HEIGHT,
  CANVAS_DEFAULT_NODE_WIDTH,
  type RecentOutput,
} from '../stores/canvas';
import ImageNode from './nodes/ImageNode.vue';
import VideoNode from './nodes/VideoNode.vue';
import PromptNode from './nodes/PromptNode.vue';
import GenerateNode from './nodes/GenerateNode.vue';
import { fileBaseName, type CanvasNodeData, type NodeActionPayload } from './nodes/types';
import CanvasDock from './CanvasDock.vue';
import AnnotationDialog from './AnnotationDialog.vue';
// 会话名 / 提示文案都从 appStore 拿：appStore 只单向引用 canvas store，
// 这里反向引用 appStore 不会成环（canvas store 自身不 import 任何 store）
import { useAppStore } from '../stores/appStore';

// 与 BrowserPane 同一约定：页签未激活时不做重活（这里体现为延迟挂载）
const props = defineProps<{ active: boolean }>();

const store = useCanvasStore();
const appStore = useAppStore();

// 画布只有节点画布一种形态（原 Excalidraw 自由画布档已移除）：
// 对话产出的图 → 落成图片节点 → 送去修改 → 新图自动接「改图」边，
// 这条链路全部由 canvas store 承担，组件层不再需要档位状态。

// 显式传 id：让下面这行 useVueFlow 拿到的 store 与 <VueFlow id="canvas-flow">
// 内部实例是同一个，否则两边各建一个实例，缩放进度同步不上
// screenToFlowCoordinate：托盘点进来的图要落在用户当下看着的位置，得把
// 屏幕坐标换算回世界坐标（否则永远落在原点，用户以为没反应）
const { viewport, zoomIn, zoomOut, fitView, screenToFlowCoordinate } = useVueFlow('canvas-flow');

/** 画布容器：算视口中心要用它的 DOMRect */
const rootRef = ref<HTMLElement | null>(null);

/** 最近产出托盘（store 单一真源，组件只读） */
const recentOutputs = computed(() => store.recentOutputs);

/**
 * 一次性操作提示。为什么不用 console：这类失败（桥没接上、附件丢了）用户
 * 无从判断，必须画出来；4 秒后自动消失，不打断后续操作
 */
const tip = ref('');
let tipTimer: ReturnType<typeof setTimeout> | null = null;
function showTip(message: string): void {
  tip.value = message;
  if (tipTimer) clearTimeout(tipTimer);
  tipTimer = setTimeout(() => {
    tip.value = '';
    tipTimer = null;
  }, 4000);
}

/** 托盘里的一行文案：文件名优先（托盘很窄，不展示完整路径） */
function trayName(path: string): string {
  return fileBaseName(path) || '未命名图片';
}

/**
 * 节点操作按钮的落点。
 * vue-flow 用动态组件渲染节点，节点 emit 出来的自定义事件不会冒泡到 <VueFlow>，
 * 所以这里给每个节点类型套一层极薄的转发组件，把 send-to-agent 接回画布。
 *
 * 手上只拿得到节点 id：节点可能还没 file（演示图、手工建的空图），这种情况
 * 必须给出可见提示——发一个空附件过去，智能体只会回一句「没看到图」。
 */
async function onSendToAgent(payload: NodeActionPayload) {
  const node = store.nodes.find((n) => n.id === payload.id);
  const file = node?.file;
  if (!file) {
    showTip('这个节点还没有关联图片文件，先给它设置一张图再送来修改');
    return;
  }
  const api = (window as unknown as { pywebview?: { api?: Record<string, unknown> } }).pywebview?.api;
  const send = api?.canvas_send_to_agent;
  if (typeof send !== 'function') {
    // 浏览器里预览 / 后端没暴露方法时就是这个分支：不许静默
    showTip('后端桥未就绪，无法发送到对话（请在桌面应用内重试）');
    return;
  }
  // 指令写成中文自然语言：画布上的动作要能被智能体直接读懂，
  // 而不是靠它猜「edit / config-shot」这两个内部枚举
  const instruction =
    payload.action === 'config-shot'
      ? '请把这张图当作参考帧，帮我配置一条匹配的镜头：景别、机位、运动与时长都给出建议。'
      : '请先看清这张图的内容，然后按我下一步的具体要求修改它。';
  try {
    const res = await (
      send as (
        canvasId: string,
        files: string[],
        instruction: string,
        sessionName: string | null,
      ) => Promise<unknown>
    )(store.canvasId, [file], instruction, appStore.currentSessionName || null);
    const result = res as { ok?: boolean; msg?: string } | undefined;
    if (result && result.ok === false) {
      showTip(String(result.msg || '发送到对话失败'));
      return;
    }
    // 送出去了才记源：改图产出回来时，画布靠这条记录把新图接成源图的下游
    store.markEditSource({ nodeId: node.id, file, title: node.title });
  } catch (err) {
    showTip(`发送到对话失败：${err instanceof Error ? err.message : String(err)}`);
  }
}

/**
 * 就地改图：写一句话直接出图，不经过对话、不占 token、不留对话记录。
 *
 * 与 onSendToAgent 的分工：这条链路只有「图 + 指令」，模型不参与理解图，
 * 所以延迟与成本都低；需要模型先读图再帮你润 prompt 的场景仍走「送去对话修改」。
 * 末位 confirmed 必须显式 true：后端入口靠它承担「用户点了生成」的授权语义
 * （该工具声明 requires_permission=True，但本路径绕过了调度层的权限判定）。
 */
async function onEditInline(payload: NodeActionPayload) {
  const node = store.nodes.find((n) => n.id === payload.id);
  const file = node?.file;
  if (!node || !file) {
    showTip('这个节点还没有关联图片文件，先给它设置一张图再改');
    return;
  }
  // 前端先挡一层：后端对同一画布也有并发闸门，但让用户先看到原因比往返一次好
  if (store.pendingEdits[node.id]) {
    showTip('这张图正在改，等它回来');
    return;
  }
  const prompt = String(payload.prompt || '').trim();
  if (!prompt) {
    showTip('请先写一句改图要求');
    return;
  }
  const api = (window as unknown as { pywebview?: { api?: Record<string, unknown> } }).pywebview?.api;
  const edit = api?.canvas_edit_image;
  if (typeof edit !== 'function') {
    showTip('后端桥未就绪，无法就地改图（请在桌面应用内重试）');
    return;
  }
  try {
    // model 传空串 = 用插件默认模型，不在前端硬编码模型名
    const res = await (
      edit as (
        canvasId: string,
        nodeFile: string,
        prompt: string,
        sourceNodeId: string,
        model: string,
        confirmed: boolean,
      ) => Promise<unknown>
    )(store.canvasId, file, prompt, node.id, '', true);
    const result = res as { ok?: boolean; msg?: string; job_id?: string } | undefined;
    if (result && result.ok === false) {
      showTip(String(result.msg || '改图请求失败'));
      return;
    }
    if (result?.ok !== true || !result.job_id) {
      // 没拿到任务号 = 没起任务，绝不能进 pending（那会挂一个永远不消失的遮罩）
      showTip('改图请求没有返回任务号，请重试');
      return;
    }
    // 进入 pending 只为了画遮罩、禁用按钮；真正的产物由 canvas_edit_result 事件落节点
    store.markEditPending(node.id, String(result.job_id));
  } catch (err) {
    showTip(`改图请求失败：${err instanceof Error ? err.message : String(err)}`);
  }
}

/**
 * 画笔标注：打开居中标注浮层（底图只显示，笔迹画在独立的透明 canvas 层上）。
 * 前端不做合成：底图 url 来自本机媒体服务端口，属跨源图片，drawImage 进
 * canvas 会把它标脏、toBlob 直接抛安全错误；合成交给后端 alpha_composite。
 */
interface AnnotateTarget {
  nodeId: string;
  file: string;
  url: string;
  title: string;
  prompt: string;
}

const annotateTarget = ref<AnnotateTarget | null>(null);
const annotateSubmitting = ref(false);

function onAnnotate(payload: NodeActionPayload): void {
  const node = store.nodes.find((n) => n.id === payload.id);
  if (!node || !node.file) {
    showTip('这个节点还没有关联图片文件，先给它设置一张图再标注');
    return;
  }
  if (store.pendingEdits[node.id]) {
    showTip('这张图正在改，等它回来');
    return;
  }
  if (!node.url) {
    // 没有预览 url 就没有底图可画，标注无从下手：顺手补一次解析再让用户重试
    showTip('这张图还没有预览地址，稍等片刻再试');
    void store.resolveMedia();
    return;
  }
  annotateTarget.value = {
    nodeId: node.id,
    file: node.file,
    url: node.url,
    title: node.title || fileBaseName(node.file),
    prompt: String(payload.prompt || ''),
  };
}

/** 关浮层即丢弃笔迹：不做草稿保存，避免半张标注被当成完整意图误用 */
function closeAnnotate(): void {
  // 提交中不许关：请求已发出，关掉会让用户以为没生效而重复点（每次都是钱）
  if (annotateSubmitting.value) return;
  annotateTarget.value = null;
}

// canvas_edit_result 事件统一由 appStore 的 handleBridgeEvent 分发（那里是
// window.onBridgeEvent 的唯一赋值点）。本组件不再包装/抢占它：包装层要靠
// 定时器反复重挂才不被启动顺序顶掉，既会漏事件，也可能把主对话区的流式
// 处理器踩掉——那会让主对话区整条流都没有信号。

/**
 * 标注改图提交：把「底图 + 笔迹 base64 + 一句要求」交给后端合成并出图。
 *
 * 与 onEditInline 只差一张标注层：后端会先 alpha_composite 再送模型，前端绝不
 * 自己拼图（跨源底图一旦 drawImage 进 canvas 就被标脏，toBlob 直接抛安全错误）。
 * 两者共用同一套 pending 契约：只有拿到 job_id 才进 pending，产物仍由
 * canvas_edit_result 事件落到画布上。
 */
async function onAnnotateSubmit(payload: { annotationB64: string; prompt: string }): Promise<void> {
  const target = annotateTarget.value;
  // 目标为空（浮层已关）或正在提交：直接忽略，避免重复点击发两次请求（每次都是钱）
  if (!target || annotateSubmitting.value) return;
  const prompt = String(payload.prompt || '').trim();
  if (!prompt) {
    showTip('请先写一句改图要求');
    return;
  }
  const annotationB64 = String(payload.annotationB64 || '').trim();
  if (!annotationB64) {
    // 空白笔迹层等于没画：后端合成出来与原图一致，白花一次生成成本
    showTip('先在图上圈出要改的地方');
    return;
  }
  const api = (window as unknown as { pywebview?: { api?: Record<string, unknown> } }).pywebview?.api;
  const edit = api?.canvas_edit_image_annotated;
  if (typeof edit !== 'function') {
    showTip('后端桥未就绪，无法按标注改图（请在桌面应用内重试）');
    return;
  }
  annotateSubmitting.value = true;
  try {
    // model 传空串 = 用插件默认模型，不在前端硬编码模型名；
    // 末位 confirmed 必须显式 true：后端入口靠它承担「用户点了生成」的授权语义
    const res = await (
      edit as (
        canvasId: string,
        nodeFile: string,
        prompt: string,
        sourceNodeId: string,
        annotationB64: string,
        model: string,
        confirmed: boolean,
      ) => Promise<unknown>
    )(store.canvasId, target.file, prompt, target.nodeId, annotationB64, '', true);
    const result = res as { ok?: boolean; msg?: string; job_id?: string } | undefined;
    if (result && result.ok === false) {
      showTip(String(result.msg || '改图请求失败'));
      return;
    }
    if (result?.ok !== true || !result.job_id) {
      // 没拿到任务号 = 没起任务，绝不能进 pending（那会挂一个永远不消失的遮罩）
      showTip('改图请求没有返回任务号，请重试');
      return;
    }
    // 进 pending 只为了画遮罩、禁用按钮；真正的产物由 canvas_edit_result 事件落节点
    store.markEditPending(target.nodeId, String(result.job_id));
    // 这里直接置 null 而不走 closeAnnotate：那个函数在 submitting 时会拒关，
    // 可此刻请求已被受理，浮层必须收起，否则会一直盖在刚落的节点前面
    annotateTarget.value = null;
  } catch (err) {
    showTip(`改图请求失败：${err instanceof Error ? err.message : String(err)}`);
  } finally {
    annotateSubmitting.value = false;
  }
}

/**
 * 托盘里点一张图：把它作为图片节点落到视口中心附近。
 * 不自动落节点的原则只针对「刚生成」，用户主动点选就是明确要它进画布。
 */
function onTrayPick(item: RecentOutput): void {
  const rect = rootRef.value?.getBoundingClientRect();
  const center = rect
    ? screenToFlowCoordinate({ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 })
    : { x: 0, y: 0 };
  // 递增错位：连点几张不会全部叠在同一个像素上，用户还得自己一张张拖开
  const wave = (store.nodes.length % 6) * 22;
  const created = store.addNode({
    type: 'image',
    x: center.x - CANVAS_DEFAULT_NODE_WIDTH / 2 + wave,
    y: center.y - CANVAS_DEFAULT_NODE_HEIGHT / 2 + wave,
    file: item.path,
    url: item.url,
    title: trayName(item.path),
    meta: { created_at: item.addedAt },
  });
  // 选中它：落点若被浮层挡住，高亮也还能告诉用户「图已经进来了」
  store.selectNode(created.id);
  // 托盘条目可能还没解析出 url（后端慢/刚入托盘），补一次
  void store.resolveMedia();
}

/** 清空托盘：只清缓冲，画布上已有的节点一个都不动 */
function onClearTray(): void {
  store.clearRecentOutputs();
}

/**
 * 把节点组件包一层，只做「子组件事件 → 画布」的转发，属性原样透传。
 *
 * 第三个参数 pendingAware 只给图片节点开：vue-flow 传给节点组件的 props 是
 * 固定一组（id/type/data/selected…），节点想拿到「我正在改图」只能由画布塞进
 * data 再提出来；这里顺势把它提成节点自己的 editPending prop，节点因此完全
 * 不必认识 store。其它三类节点没声明这个 prop，传过去只会变成根元素上的
 * 无意义属性，索性不传。
 */
function bridgeNode(target: Component, name: string, pendingAware = false) {
  return markRaw(
    defineComponent({
      name,
      inheritAttrs: false,
      setup(_, { attrs }) {
        return () =>
          h(target, {
            ...attrs,
            onSendToAgent,
            // 就地改图 / 画笔标注：与 send-to-agent 同一套转发，节点不必认识桥
            onEditInline,
            onAnnotate,
            ...(pendingAware
              ? { editPending: Boolean((attrs.data as CanvasNodeData | undefined)?.editPending) }
              : {}),
          });
      },
    }),
  );
}

// 四类节点的注册表：key 与 CanvasNodeType 中的视觉类型一一对应
const nodeTypes = {
  image: bridgeNode(ImageNode, 'CanvasImageNode', true),
  video: bridgeNode(VideoNode, 'CanvasVideoNode'),
  prompt: bridgeNode(PromptNode, 'CanvasPromptNode'),
  generate: bridgeNode(GenerateNode, 'CanvasGenerateNode'),
  // vue-flow 的 NodeComponent 静态类型要求组件声明 NodeProps（id/type/selected…），
  // 而上面这几个转发组件靠 inheritAttrs 运行期透传属性，类型上自然对不上；
  // 这里只做类型断言，运行行为不变
} as unknown as NodeTypesObject;

const flowReady = ref(false);
watch(
  () => props.active,
  (active) => {
    if (active) flowReady.value = true;
  },
  // post：等右面板把 v-show 切回可见后再挂载，vue-flow 首次测量才能拿到真实尺寸
  { immediate: true, flush: 'post' },
);

/**
 * 后端 canvas_load 桥就绪前，空画布放一张演示图：
 * 一是没有数据源，二是能立刻看到四类节点的真实排版。
 * 桥存在时跳过（真实文档优先，绝不让演示数据盖住用户画布）。
 */
function seedDemoGraph() {
  if (store.nodes.length > 0) return;
  const api = (window as unknown as { pywebview?: { api?: Record<string, unknown> } }).pywebview?.api;
  if (api && typeof api.canvas_load === 'function') return;

  const prompt = store.addNode({
    id: 'demo-prompt',
    type: 'prompt',
    x: 128,
    y: 470,
    title: '橘猫提示词',
    meta: {
      text: '水彩风格的橘猫坐在窗台上，柔和侧光，38mm 广角镜头，浅景深，颗粒质感。',
      tags: ['水彩橘猫', '窗边', '35mm'],
    },
  });
  const cat = store.addNode({
    id: 'demo-cat',
    type: 'image',
    x: 128,
    y: 196,
    title: '水彩橘猫',
    meta: { size: '1024²', model: 'qwen-image-2.0', time: '14:22 创建' },
  });
  const gen = store.addNode({
    id: 'demo-gen',
    type: 'generate',
    x: 548,
    y: 196,
    title: '生成 · 赛博猫 v2',
    meta: { progress: 62, stage: 'sampling · 12/20 步', params: ['1024×1024', 'seed 20481', '6.4s'] },
  });
  const cyber = store.addNode({
    id: 'demo-cyber',
    type: 'image',
    x: 928,
    y: 196,
    title: '赛博猫 v2',
    meta: { size: '1024²', model: '赛博霓虹 · 水彩橘猫', time: '14:31 创建' },
  });
  const video = store.addNode({
    id: 'demo-video',
    type: 'video',
    x: 1288,
    y: 366,
    title: '雨夜街道',
    meta: { duration: '4.0s', progress: 34, time: '刚刚生成' },
  });

  store.addEdge(prompt.id, cat.id, '风格参考');
  store.addEdge(cat.id, gen.id, '改图');
  store.addEdge(prompt.id, gen.id, '提示词');
  store.addEdge(gen.id, cyber.id, '出图');
  store.addEdge(cyber.id, video.id, '参考帧');
  // 演示图不是用户改动，别把画布标成「未保存」
  store.dirty = false;
}

watch(
  flowReady,
  (ready) => {
    if (ready) seedDemoGraph();
  },
  { immediate: true },
);

// store 里的画布模型 → vue-flow 的 Node：位置/宽度透传（宽度决定节点定宽 264），
// 高度交给节点内容撑开，所以不传 height；selected 由 store 单一真源决定
const flowNodes = computed<Node[]>(() =>
  store.nodes.map((n) => ({
    id: n.id,
    type: n.type,
    position: { x: n.x, y: n.y },
    width: n.width,
    data: { node: n, editPending: Boolean(store.pendingEdits[n.id]) },
    selected: n.id === store.selectedNodeId,
  })),
);

const flowEdges = computed<Edge[]>(() =>
  store.edges.map((e) => {
    // 生成节点流出的边是「活跃」边：视觉稿里只有它走虚线流动动画
    const from = store.nodes.find((n) => n.id === e.fromNode);
    return {
      id: e.id,
      source: e.fromNode,
      target: e.toNode,
      label: e.label,
      class: from?.type === 'generate' ? 'cv-edge-hot' : undefined,
    };
  }),
);

// 背景点阵随视口平移/缩放：把 transform 摊成 CSS 变量交给 background-position/size，
// 比额外铺一层 SVG pattern 省一次大范围重绘
const gridVars = computed(() => ({
  '--cv-grid-size': `${Math.max(8, 16 * viewport.value.zoom)}px`,
  '--cv-grid-x': `${viewport.value.x}px`,
  '--cv-grid-y': `${viewport.value.y}px`,
}));

const zoomPercent = computed(() => Math.round(viewport.value.zoom * 100));

/** 节点点击：选中态的唯一真源仍然是 store（p5 的节点高亮也读它） */
function onNodeClick({ node }: NodeMouseEvent) {
  store.selectNode(node.id);
}

/** 拖拽结束才回写坐标：拖拽过程中回写会让受控 nodes 和拖拽状态互相打架 */
function onNodeDragStop({ node }: NodeMouseEvent) {
  store.updateNodePosition(node.id, node.position.x, node.position.y);
}

function onPaneClick() {
  // 点空白处取消选中
  store.selectNode(null);
}

/**
 * 用户拖端口手动连线：源节点右侧出点 → 目标节点左侧入点。
 * 合法性（自环 / 重复边 / 端点不存在）全部复用 store.addEdge 的校验，
 * 它返回 null 时必须给出可见提示——拖完什么都没发生是最难自诊断的交互。
 */
function onConnect(conn: Connection): void {
  if (!conn.source || !conn.target) return;
  const edge = store.addEdge(conn.source, conn.target, '关联');
  if (!edge) showTip('这两张图已经关联过了，或不能连到自己');
}

/** 双击连线删除关联：手连错了总得有路可退，不必删节点重建 */
function onEdgeDoubleClick({ edge }: EdgeMouseEvent): void {
  store.removeEdge(edge.id);
  showTip('已删除这条关联');
}
</script>

<style scoped>
/* 满幅撑满右侧内容区（父级 flush 下 .rpane.active 是 flex 容器） */
.cv-root {
  position: relative;
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  width: 100%;
}
.cv-flow {
  flex: 1;
  min-height: 0;
  /* 点阵画在 vue-flow 根节点上而不是 .cv-root：分区标记的负 z-index
     背景层会盖住父元素自身的 background */
  background-color: var(--bg0);
  background-image: radial-gradient(var(--border) 1px, transparent 1px);
  background-size: var(--cv-grid-size) var(--cv-grid-size);
  background-position: var(--cv-grid-x) var(--cv-grid-y);
}
.cv-idle {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-faint);
  font-size: 12px;
}

/* 连线样式：.vue-flow__edge-path 由库内部渲染，scoped 下必须用 :deep 才能命中。
   默认细灰线；生成节点流出的活跃线换成 accent + 虚线流动，角标文案同视觉稿 */
.cv-flow :deep(.vue-flow__edge-path) {
  stroke: color-mix(in srgb, var(--text-dim) 28%, transparent);
  stroke-width: 1.6;
  fill: none;
}
.cv-flow :deep(.cv-edge-hot .vue-flow__edge-path) {
  stroke: color-mix(in srgb, var(--accent) 75%, transparent);
  stroke-width: 2;
  stroke-dasharray: 5 9;
  animation: cv-dash 1.4s linear infinite;
}
@keyframes cv-dash {
  to {
    stroke-dashoffset: -28;
  }
}
.cv-flow :deep(.vue-flow__edge-text) {
  fill: var(--text-faint);
  font-family: var(--mono);
  font-size: 10.5px;
  letter-spacing: 0.3px;
}

.cv-zoom {
  position: absolute;
  right: 10px;
  bottom: 10px;
  z-index: 2;
  display: flex;
  align-items: center;
  gap: 3px;
  padding: 3px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg2);
}
.cv-zbtn {
  min-width: 22px;
  height: 22px;
  padding: 0 6px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--bg3);
  color: var(--text-dim);
  font-family: inherit;
  font-size: 11.5px;
  line-height: 1;
  cursor: pointer;
  transition: 0.15s;
}
.cv-zbtn:hover {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent);
}
.cv-zval {
  min-width: 40px;
  text-align: center;
  color: var(--text-faint);
  font-family: var(--mono);
  font-size: 11px;
}
/* 「适应」是文字按钮，宽度自适应比固定 22px 好读 */
.cv-zfit {
  min-width: auto;
  color: var(--text);
}

/* 顶部一次性提示：与画布坞/托盘同一套半透明浮层语言，只借全局变量取色 */
.cv-tip {
  position: absolute;
  top: 10px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 4;
  max-width: min(420px, calc(100% - 24px));
  padding: 6px 11px;
  border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg1) 94%, transparent);
  color: var(--text);
  font-size: 12px;
  line-height: 1.5;
  box-shadow: 0 8px 20px color-mix(in srgb, var(--border) 60%, transparent);
  backdrop-filter: blur(6px);
}

/* 最近产出托盘：左下角，宽度比画布坞窄得多（它是边角料区，越窄挡住的节点越少） */
.cv-tray {
  position: absolute;
  left: 10px;
  bottom: 10px;
  z-index: 2;
  width: 214px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: color-mix(in srgb, var(--bg1) 94%, transparent);
  box-shadow: 0 12px 26px color-mix(in srgb, var(--border) 55%, transparent);
  backdrop-filter: blur(6px);
  overflow: hidden;
}
.cv-tray-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px 6px 10px;
  border-bottom: 1px solid var(--border);
}
/* 标题沿用节点头部的等宽小写距观感（视觉稿 .node .kind 那一套） */
.cv-tray-title {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.7px;
  text-transform: uppercase;
  color: var(--text-faint);
}
.cv-tray-count {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--accent);
  border: 1px solid color-mix(in srgb, var(--accent) 34%, transparent);
  background: var(--accent-soft);
  border-radius: 99px;
  padding: 0 6px;
}
.cv-tray-spacer {
  flex: 1;
}
.cv-tray-clear {
  padding: 1px 6px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  color: var(--text-faint);
  font-family: inherit;
  font-size: 11px;
  cursor: pointer;
  transition: 0.15s;
}
.cv-tray-clear:hover {
  border-color: var(--border);
  background: var(--bg3);
  color: var(--text-bright);
}
/* 最多显示 4 行多一点，再多就内部滚动：托盘不该长到盖住半个画布 */
.cv-tray-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px;
  max-height: 168px;
  overflow-y: auto;
}
.cv-tray-list::-webkit-scrollbar {
  width: 8px;
}
.cv-tray-list::-webkit-scrollbar-thumb {
  background: var(--bg3);
  border-radius: 4px;
}
.cv-tray-item {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 4px;
  border: 1px solid transparent;
  border-radius: 7px;
  background: transparent;
  color: var(--text-dim);
  font-family: inherit;
  font-size: 11.5px;
  text-align: left;
  cursor: pointer;
  transition: 0.15s;
}
.cv-tray-item:hover {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--text-bright);
}
.cv-tray-img {
  flex: 0 0 auto;
  width: 30px;
  height: 30px;
  object-fit: cover;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--bg0);
}
/* 没解析出 url 时的占位：虚线框 + IMG 字样，和图片节点的占位同一个语感 */
.cv-tray-ph {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border: 1px dashed var(--border);
  border-radius: 5px;
  background: var(--bg0);
  color: var(--text-faint);
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.4px;
}
.cv-tray-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
