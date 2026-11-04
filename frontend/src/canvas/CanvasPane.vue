<template>
  <div ref="rootRef" class="canvas-wrap" data-test="canvas-pane-root" tabindex="0">
    <!-- 左列：画布项目侧栏（M3-T07）。列表/新建/改名/删除在 ProjectList 内，
         导入/导出入口也从侧栏 emit 上来，落 store 与文件读写统一留在本组件 -->
    <ProjectList @import="openImportPicker" @export="exportOpen = true" />

    <!-- 导入用隐藏文件选择器：accept 限定 .json，读完内容即清空 value（同一文件可重复选） -->
    <input
      ref="importInputRef"
      class="canvas-import-input"
      type="file"
      accept=".json,application/json"
      data-test="canvas-import-input"
      @change="onImportFileChange"
    />

    <!-- 导出对话框（M3-T07）：项目 JSON 下载 + 选中图片逐张下载 -->
    <ExportDialog :open="exportOpen" @close="exportOpen = false" />

    <!-- 右列：画布舞台（InfiniteCanvas 承载世界层 transform 与平移缩放手势） -->
    <section class="canvas-stage" data-test="canvas-stage">
      <div v-if="!store.project" class="canvas-hint" data-test="canvas-stage-empty">
        选择左侧项目，或新建一个画布开始
      </div>
      <div
        v-else
        ref="canvasMountRef"
        class="canvas-mount"
        data-test="canvas-mount"
        @contextmenu="onCanvasContextMenu"
      >
        <!-- 受控视口：手势结束/滚轮时 emit，store.setViewport 走 800ms 防抖落盘 -->
        <InfiniteCanvas
          ref="canvasRef"
          :viewport="store.project.viewport"
          :background-mode="store.project.backgroundMode"
          :selected-node-ids="selectedNodeIdList"
          :selected-connection-ids="selectedConnectionIdList"
          @update:viewport="store.setViewport"
          @selection-change="onSelectionChange"
          @selection-commit="onSelectionCommit"
          @canvas-click-empty="store.clearSelection()"
        >
          <!-- 世界坐标内容层：按 project.nodes 逐节点渲染。
               节点只 emit action（不 import store、不写 project），落库统一回到本组件的
               onNodeAction —— M3-T01 先占位，M3-T02 起在这里翻译成 store action -->
          <!-- 连线画在节点下面：世界坐标 SVG，线宽按 1/k 反缩（M3-T04） -->
          <ConnectionPath
            v-for="item in renderedConnections"
            :key="item.connection.id"
            :connection="item.connection"
            :from="item.from"
            :to="item.to"
            :selected="store.selectedConnectionIds.has(item.connection.id)"
            :scale="canvasScale"
            @select="onConnectionSelect"
          />
          <NodeShell
            v-for="node in store.project.nodes"
            :key="node.id"
            :node="node"
            :selected="store.selectedNodeIds.has(node.id)"
            :interactive="true"
            :scale="store.project.viewport.k"
            :style="nodeStyle(node)"
            @action="onNodeAction"
            @connect-request="onConnectRequest"
          >
            <component :is="NODE_COMPONENTS[node.type]" :node="node" @action="onNodeAction" />
          </NodeShell>
          <!-- 拉线中的虚线：起点取源节点右端口，终点跟指针（世界坐标，与连线同公式） -->
          <svg
            v-if="activeConnectionPathD"
            class="canvas-active-connection"
            data-test="canvas-active-connection"
            width="1"
            height="1"
            :style="ACTIVE_SVG_STYLE"
            aria-hidden="true"
          >
            <path
              :d="activeConnectionPathD"
              fill="none"
              stroke="#2f80ff"
              :stroke-width="activeStrokeWidth"
              stroke-dasharray="5,5"
              data-test="canvas-active-connection-line"
            />
          </svg>
          <!-- 框选矩形：与世界层同一 transform，左/top 直接吃世界坐标 -->
          <SelectionBox :state="selectionBox" />
        </InfiniteCanvas>
        <!-- 连线被拒绝的原因（store.connect 的 reason）：右上角短提示，2.4s 自动消失 -->
        <div v-if="connectError" class="canvas-connect-toast" data-test="canvas-connect-toast">
          {{ connectError }}
        </div>
        <!-- 空画布占位：浮在画布之上，不吃指针事件（不挡平移缩放） -->
        <div v-if="!store.project.nodes.length" class="canvas-empty-hint" data-test="canvas-empty-hint">
          拖入或生成素材开始
        </div>
        <!-- 选区工具条：有选中才出现；按钮只 emit，落 store 由 onToolbarAction 负责 -->
        <SelectionToolbar
          :selected-node-count="store.selectedNodeIds.size"
          :selected-connection-count="store.selectedConnectionIds.size"
          :visible="hasSelection"
          @action="onToolbarAction"
        />
        <!-- M3-T06：右下角小地图 / 左下角缩放控件 / 右键菜单（都只 emit，落 store 在本组件） -->
        <MiniMap
          :nodes="store.project.nodes"
          :viewport="store.project.viewport"
          :container-size="containerSize"
          @navigate="onMiniMapNavigate"
        />
        <ZoomControls
          :viewport="store.project.viewport"
          :container-size="containerSize"
          @update:viewport="store.setViewport"
          @fit="fitViewToContent"
        />
        <NodeContextMenu
          :state="contextMenu"
          :can-group="canGroupSelection"
          :can-ungroup="canUngroupSelection"
          @action="onContextMenuAction"
          @close="contextMenu = null"
        />
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch, type CSSProperties } from 'vue';

import { useCanvasStore } from '../stores/canvas';
import ProjectList from './canvas-side-panel/ProjectList.vue';
import ExportDialog from './tools/ExportDialog.vue';
import InfiniteCanvas from './render/InfiniteCanvas.vue';
import ConnectionPath from './render/ConnectionPath.vue';
import SelectionBox from './render/SelectionBox.vue';
import NodeShell from './nodes/NodeShell.vue';
import { NODE_COMPONENTS } from './nodes';
import type { CanvasConnectRequest, NodeActionPayload } from './nodes/actions';
import { connectionDrag } from './nodes/connection-drag';
import SelectionToolbar from './tools/SelectionToolbar.vue';
import MiniMap from './render/MiniMap.vue';
import ZoomControls from './render/ZoomControls.vue';
import NodeContextMenu from './tools/NodeContextMenu.vue';
import {
  CanvasNodeType,
  type CanvasNodeData,
  type ContextMenuState,
  type Position,
  type SelectionBoxState,
} from './types';
import { getNodeBounds } from './utils/canvas-group';
import {
  buildActiveConnectionPathD,
  getConnectionsInSelectionRect,
} from './utils/canvas-connection-path';
import {
  getNodesInSelectionRect,
  type SelectionCommitPayload,
  type SelectionToolbarAction,
} from './utils/canvas-selection';

const props = withDefaults(defineProps<{ active?: boolean }>(), { active: false });

const store = useCanvasStore();
/** 导出对话框开关（M3-T07）：纯 UI 态，不进 store */
const exportOpen = ref(false);
/** 导入用文件选择器句柄（模板里的隐藏 input） */
const importInputRef = ref<HTMLInputElement | null>(null);
/** InfiniteCanvas 暴露的 clientToWorld：拉线虚线要按指针位置算世界坐标 */
const canvasRef = ref<InstanceType<typeof InfiniteCanvas> | null>(null);

/** 框选帧内的矩形态：由 InfiniteCanvas 的 selection-change 推来，只用于渲染 SelectionBox */
const selectionBox = ref<SelectionBoxState | null>(null);

// 传给 InfiniteCanvas 的选中集快照（Shift 加选基底）：用数组 prop 而非 Set，
// 只在 store 选中集变化时重算，不随指针帧刷新
const selectedNodeIdList = computed(() => Array.from(store.selectedNodeIds));
const selectedConnectionIdList = computed(() => Array.from(store.selectedConnectionIds));
const hasSelection = computed(
  () => store.selectedNodeIds.size + store.selectedConnectionIds.size > 0,
);

/** 世界层缩放：传给 ConnectionPath 做线宽反缩（k 非法时退化为 1） */
const canvasScale = computed(() => {
  const k = store.project?.viewport.k;
  return typeof k === 'number' && Number.isFinite(k) && k > 0 ? k : 1;
});

/** 拉线虚线容器的样式：世界层内绝对定位 + overflow visible，常量避免每帧新建对象 */
const ACTIVE_SVG_STYLE: CSSProperties = {
  position: 'absolute',
  left: '0px',
  top: '0px',
  overflow: 'visible',
  pointerEvents: 'none',
};

/**
 * 已存在连线 → 可渲染的 from/to 实体。
 * 悬空连线（节点已删）直接跳过：宁可少画一根线，也不画出没有端点的半截线。
 */
const renderedConnections = computed(() => {
  const project = store.project;
  if (!project) return [];
  const byId = new Map(project.nodes.map((node) => [node.id, node]));
  return project.connections.flatMap((connection) => {
    const from = byId.get(connection.fromNodeId);
    const to = byId.get(connection.toNodeId);
    return from && to ? [{ connection, from, to }] : [];
  });
});

/**
 * 拉线中的虚线路径（世界坐标）：起点取源节点右端口，终点取指针所在世界坐标。
 * 换算走 InfiniteCanvas 的 clientToWorld（含手势期间的实时视口），拖动画布再拉线也不会偏。
 */
const activeConnectionPathD = computed(() => {
  if (!connectionDrag.active) return '';
  const from = store.project?.nodes.find((node) => node.id === connectionDrag.fromNodeId);
  if (!from) return '';
  const start = { x: from.position.x + from.width, y: from.position.y + from.height / 2 };
  const end = canvasRef.value?.clientToWorld(connectionDrag.clientX, connectionDrag.clientY) ?? start;
  return buildActiveConnectionPathD(start, end);
});

/** 虚线线宽同样按 1/k 反缩：缩放画布时粗细不变 */
const activeStrokeWidth = computed(() => 2 / canvasScale.value);

/** 点连线 → 只选中这条连线（清掉节点选中，与参考仓点线口径一致） */
function onConnectionSelect(connectionId: string): void {
  if (!connectionId) return;
  store.setSelection([], [connectionId], false);
}

// ── M3-T04 连线被拒绝的提示（右上角 toast，不引第三方依赖） ──────────
const CONNECT_ERROR_MS = 2400;
const connectError = ref('');
let connectErrorTimer: ReturnType<typeof setTimeout> | null = null;

function showConnectError(reason: string): void {
  connectError.value = reason;
  if (connectErrorTimer !== null) clearTimeout(connectErrorTimer);
  connectErrorTimer = setTimeout(() => {
    connectErrorTimer = null;
    connectError.value = '';
  }, CONNECT_ERROR_MS);
}

/** 目标端口松手 → store.connect；guard 拒绝时把 reason 原样展示，不吞掉原因 */
function onConnectRequest(payload: CanvasConnectRequest): void {
  const result = store.connect(payload.fromNodeId, payload.toNodeId);
  if (!result.ok) showConnectError(result.reason || '连线不可用');
}

// ── M3-T05 快捷键（画布内键位）─────────────────────────────────
const rootRef = ref<HTMLElement | null>(null);

/** 焦点在可编辑控件里就不接管：聊天输入框、节点标题/正文编辑都该走浏览器原生行为 */
function isEditableTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof el.tagName !== 'string') return false;
  const tag = el.tagName.toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
  if (el.isContentEditable) return true;
  return typeof el.closest === 'function' ? el.closest('[contenteditable="true"]') !== null : false;
}

/**
 * 键位：Ctrl+C / Ctrl+V / Ctrl+X / Ctrl+D / Delete（含 Backspace）/ Ctrl+Z /
 * Ctrl+Y 与 Ctrl+Shift+Z / Ctrl+A。mac 上用 metaKey 同效。
 * 只在「画布面板激活 + 焦点没落在别处输入控件里」时接管；store action 返回 false
 * （无选中/无可撤销）就不 preventDefault，把浏览器原生行为留给用户。
 */
function onKeydown(event: KeyboardEvent): void {
  if (!props.active || !store.project) return;
  if (event.defaultPrevented || isEditableTarget(event.target)) return;
  const root = rootRef.value;
  const target = event.target as Node | null;
  // 焦点既不在画布内、也不在 body（说明用户正在别处：聊天框/侧栏）→ 不抢键
  if (root && target && target !== document.body && !root.contains(target)) return;

  const mod = event.ctrlKey || event.metaKey;
  const key = event.key.toLowerCase();
  let handled = false;

  if (mod && key === 'z' && !event.shiftKey) handled = store.undo();
  else if (mod && (key === 'y' || (key === 'z' && event.shiftKey))) handled = store.redo();
  else if (mod && key === 'c') handled = store.copySelection();
  else if (mod && key === 'v') handled = store.pasteClipboard();
  else if (mod && key === 'x') handled = store.copySelection() && store.deleteSelection();
  else if (mod && key === 'd') handled = store.duplicateSelection();
  else if (mod && key === 'a') {
    store.selectAll();
    handled = true;
  } else if (!mod && (event.key === 'Delete' || event.key === 'Backspace')) {
    handled = store.deleteSelection();
  }

  if (handled) {
    event.preventDefault();
    event.stopPropagation();
  }
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown);
});

onBeforeUnmount(() => {
  resizeObserver?.disconnect();
  resizeObserver = null;
  window.removeEventListener('keydown', onKeydown);
  if (connectErrorTimer !== null) {
    clearTimeout(connectErrorTimer);
    connectErrorTimer = null;
  }
});

/** 节点在世界层的盒模型：位置/尺寸取 store，z 序对齐参考仓（分组 5 / 普通 10 / 选中 50） */
function nodeStyle(node: CanvasNodeData): CSSProperties {
  const selected = store.selectedNodeIds.has(node.id);
  // 分组垫在子节点下面，选中节点抬到最上层（与参考仓 canvas-node.tsx 的 z-index 同口径）
  const zIndex = node.type === CanvasNodeType.Group ? 5 : selected ? 50 : 10;
  return {
    left: `${node.position.x}px`,
    top: `${node.position.y}px`,
    width: `${node.width}px`,
    height: `${node.height}px`,
    zIndex,
  };
}

/**
 * 节点 → 画布 的唯一上行入口（八个节点组件 + NodeShell 都 emit 到这里）。
 * M3-T02 接线：update-text / move-nodes / resize-node / commit-group-snap / select；
 * 未列出的动作（update-title、open-director…）先留痕，待后续任务落 store。
 */
function onNodeAction(payload: NodeActionPayload): void {
  const { nodeId, action, args } = payload;
  switch (action) {
    case 'update-text':
      store.updateNodeText(nodeId, typeof args?.content === 'string' ? args.content : '');
      return;
    case 'move-nodes': {
      const ids = Array.isArray(args?.ids) ? (args.ids as string[]) : [nodeId];
      const dx = Number(args?.dx);
      const dy = Number(args?.dy);
      if (Number.isFinite(dx) && Number.isFinite(dy)) store.moveNodes(ids, dx, dy);
      return;
    }
    case 'resize-node': {
      const rect = args?.rect as { x?: number; y?: number; width: number; height: number } | undefined;
      if (rect && Number.isFinite(rect.width) && Number.isFinite(rect.height)) {
        store.resizeNode(nodeId, rect);
      }
      return;
    }
    case 'commit-group-snap':
      store.commitGroupSnap(nodeId);
      return;
    case 'select':
      // M3-T03：换成 store 的正式选中 action（单击/Shift 点选口径统一收在 store）
      store.selectNode(nodeId, args?.additive === true);
      return;
    default:
      // update-title / open-director 等：M3-T05（历史栈）、M7（导演台）再接线，先不吞掉证据
      console.debug('[canvas] node action（未接线）', payload);
  }
}

/** 框选帧内：只更新本地矩形态（渲染 SelectionBox），不进 store、不落盘 */
function onSelectionChange(state: SelectionBoxState): void {
  selectionBox.value = state;
}

/**
 * 框选提交：世界坐标矩形与节点包围盒求交 → store.setSelection（additive=Shift 加选）。
 * 连线命中（M3-T04）：与 ConnectionPath 同一套贝塞尔公式（connectionIntersectsWorldRect），
 * 框住线本身也算选中——否则 2px 宽的视觉线几乎点不到。
 */
function onSelectionCommit(payload: SelectionCommitPayload): void {
  selectionBox.value = null;
  const project = store.project;
  if (!project) return;
  const nodeIds = getNodesInSelectionRect(project.nodes, payload.rect);
  const connectionIds = getConnectionsInSelectionRect(project.connections, project.nodes, {
    x: payload.rect.x,
    y: payload.rect.y,
    w: payload.rect.width,
    h: payload.rect.height,
  });
  store.setSelection(nodeIds, connectionIds, payload.additive);
}

/**
 * 选区工具条 → store 的唯一落点。
 * M3-T05 接「删除/复制」：删除走 store.deleteSelection（节点 + 连线 + 分组归属 + 历史点一次处理），
 * 复制走 store.copySelection（内部剪贴板，Ctrl+V 可再粘出来）。
 * 成组/取消组留给后续任务，先留痕，绝不静默改 project。
 */
function onToolbarAction(action: SelectionToolbarAction): void {
  if (action === 'delete') {
    store.deleteSelection();
    return;
  }
  if (action === 'copy') {
    store.copySelection();
    return;
  }
  if (action === 'group') {
    store.groupSelection();
    return;
  }
  store.ungroupSelection();
}

// ── M3-T06 小地图 / 缩放控件 / 右键菜单 ───────────────────────
/** 画布容器元素：量尺寸、算右键菜单相对坐标都要用它 */
const canvasMountRef = ref<HTMLElement | null>(null);
/** 画布容器实测尺寸：小地图按它算视口矩形，fit 与导航也要用它做居中 */
const containerSize = ref({ width: 0, height: 0 });
let resizeObserver: ResizeObserver | null = null;

function measureContainer(): void {
  const el = canvasMountRef.value;
  if (!el) return;
  const rect = el.getBoundingClientRect();
  containerSize.value = {
    width: el.clientWidth || Math.round(rect.width),
    height: el.clientHeight || Math.round(rect.height),
  };
}

function observeContainer(el: HTMLElement | null): void {
  resizeObserver?.disconnect();
  resizeObserver = null;
  if (!el) return;
  measureContainer();
  if (typeof ResizeObserver === 'undefined') return;
  resizeObserver = new ResizeObserver(() => measureContainer());
  resizeObserver.observe(el);
}

watch(canvasMountRef, (el) => observeContainer(el), { flush: 'post' });

/** 小地图点击/拖拽 → 把该世界坐标居中：x = 容器宽/2 - worldX * k */
function onMiniMapNavigate(center: Position): void {
  const project = store.project;
  if (!project) return;
  const { width, height } = containerSize.value;
  if (!(width > 0) || !(height > 0)) return;
  const k = canvasScale.value;
  store.setViewport({ x: width / 2 - center.x * k, y: height / 2 - center.y * k, k });
}

/** 「适应内容」内边距（计划口径 padding 80） */
const FIT_PADDING = 80;

/** 适应内容：节点包围盒 + padding 80 → k 与平移，写回 store.setViewport（空画布回落 100% 居中） */
function fitViewToContent(): void {
  const project = store.project;
  if (!project) return;
  const { width, height } = containerSize.value;
  if (!(width > 0) || !(height > 0)) return;
  if (!project.nodes.length) {
    store.setViewport({ x: width / 2, y: height / 2, k: 1 });
    return;
  }
  const bounds = getNodeBounds(project.nodes);
  const contentWidth = Math.max(bounds.right - bounds.left, 1);
  const contentHeight = Math.max(bounds.bottom - bounds.top, 1);
  const fitW = Math.max((width - FIT_PADDING * 2) / contentWidth, 0.05);
  const fitH = Math.max((height - FIT_PADDING * 2) / contentHeight, 0.05);
  const k = Math.min(fitW, fitH, 5);
  store.setViewport({
    x: (width - contentWidth * k) / 2 - bounds.left * k,
    y: (height - contentHeight * k) / 2 - bounds.top * k,
    k,
  });
}

/** 右键菜单态（相对画布容器的像素坐标；null = 不显示） */
const contextMenu = ref<ContextMenuState | null>(null);

/** 成组要至少两个非分组节点（与选区工具条 canGroup 同口径） */
const canGroupSelection = computed(() => {
  const project = store.project;
  if (!project) return false;
  return (
    project.nodes.filter(
      (node) => store.selectedNodeIds.has(node.id) && node.type !== CanvasNodeType.Group,
    ).length >= 2
  );
});

/** 选中里含分组节点或带归属的子节点 → 显示「取消组」 */
const canUngroupSelection = computed(() => {
  const project = store.project;
  if (!project) return false;
  return project.nodes.some(
    (node) =>
      store.selectedNodeIds.has(node.id) &&
      (node.type === CanvasNodeType.Group || Boolean(node.metadata?.groupId)),
  );
});

/**
 * 画布右键：节点 → 节点菜单，连线 → 连线菜单，空白 → 关闭菜单。
 * 原生菜单一律 preventDefault（画布内不需要浏览器菜单）。
 */
function onCanvasContextMenu(event: MouseEvent): void {
  const project = store.project;
  const mount = canvasMountRef.value;
  if (!project || !mount) return;
  event.preventDefault();
  const rect = mount.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const target = event.target as Element | null;

  const nodeId = (target?.closest('[data-node-id]') as HTMLElement | null)?.getAttribute('data-node-id') || '';
  if (nodeId) {
    if (!store.selectedNodeIds.has(nodeId)) store.selectNode(nodeId);
    contextMenu.value = { type: 'node', x, y, nodeId };
    return;
  }
  const connectionId =
    (target?.closest('[data-connection-id]') as HTMLElement | null)?.getAttribute('data-connection-id') || '';
  if (connectionId) {
    store.setSelection([], [connectionId], false);
    contextMenu.value = { type: 'connection', x, y, connectionId };
    return;
  }
  contextMenu.value = null;
}

/** 菜单动作 → store 既有 action（复制/删除/成组/取消组/删连线） */
function onContextMenuAction(payload: { type: 'node' | 'connection'; id: string; action: string }): void {
  contextMenu.value = null;
  if (payload.type === 'connection') {
    if (payload.action === 'delete') store.disconnect(payload.id);
    return;
  }
  // 右键的那个节点若不在选中集里：先选中它，动作才作用在「你点的那个」上
  if (!store.selectedNodeIds.has(payload.id)) store.selectNode(payload.id);
  if (payload.action === 'copy') store.copySelection();
  else if (payload.action === 'delete') store.deleteSelection();
  else if (payload.action === 'group') store.groupSelection();
  else if (payload.action === 'ungroup') store.ungroupSelection();
}

/** 换项目时清掉可能残留的框（避免上一个项目的框画在新项目上） */
watch(
  () => store.project?.id,
  () => {
    selectionBox.value = null;
    contextMenu.value = null;
  },
);

/** 侧栏「导入」：打开系统文件选择器（.json 由模板上的 accept 限定） */
function openImportPicker(): void {
  importInputRef.value?.click();
}

/**
 * 导入落点：读文件文本 → store.importProject（校验 schema_version=2、清洗、id 全部重生成、建项目保存）。
 * 不管成功失败都清空 input.value，否则同一个文件第二次选不会触发 change。
 * 成功/失败都复用右上角短提示（走 showConnectError 的同一套定时清理）。
 */
async function onImportFileChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement | null;
  const file = input?.files?.[0];
  if (!file) return;
  try {
    const res = await store.importProject(await file.text());
    showConnectError(res.ok ? '导入成功' : res.msg || '导入失败');
  } catch {
    showConnectError('导入失败：读不出文件内容');
  } finally {
    if (input) input.value = '';
  }
}

// 面板是 v-show 常驻挂载：只在「首次激活」与「再次激活」时拉列表，
// 未激活时不打桥（启动阶段右栏默认不显示画布，不给桥加无谓调用）
watch(
  () => props.active,
  (active) => {
    if (active) void store.refreshProjectList();
  },
  { immediate: true },
);
</script>

<style scoped>
.canvas-wrap {
  display: flex;
  height: 100%;
  min-height: 0;
  background: var(--bg-primary, #1b1b1f);
  color: var(--text-primary, #ddd);
  font-size: 12px;
}

/* tabindex=0 只为把键盘焦点从输入框收回来，不需要可见焦点圈 */
.canvas-wrap:focus {
  outline: none;
}

.canvas-projects {
  width: 176px;
  flex: 0 0 176px;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border-color, #333);
  padding: 8px 6px;
  gap: 6px;
  overflow-y: auto;
}

.cp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 2px 4px;
}

.cp-title {
  font-weight: 600;
  color: var(--text-secondary, #aaa);
}

.cp-new {
  padding: 3px 8px;
  border: 1px solid var(--border-color, #3a3a3a);
  border-radius: 6px;
  background: var(--bg-secondary, #26262b);
  color: inherit;
  cursor: pointer;
}

.cp-new:disabled {
  opacity: 0.5;
  cursor: default;
}

.cp-error {
  padding: 4px 6px;
  border-radius: 6px;
  color: #e08a8a;
  background: rgba(224, 138, 138, 0.12);
  word-break: break-all;
}

.cp-empty {
  color: var(--text-secondary, #888);
  padding: 6px 2px;
}

.cp-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  width: 100%;
  padding: 6px 8px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: var(--bg-secondary, #26262b);
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.cp-item.active {
  border-color: var(--accent, #4b8bf5);
}

.cp-item.disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.cp-name {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cp-badge {
  font-size: 11px;
  color: #c9a24a;
}

.cp-time {
  font-size: 11px;
  color: var(--text-secondary, #888);
}

.canvas-stage {
  position: relative;
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  background: var(--bg-secondary, #202024);
}

.canvas-mount {
  position: absolute;
  inset: 0;
}

.canvas-empty-hint {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-secondary, #888);
  /* 占位文案不抢指针：平移/缩放/后续框选都要能穿过去 */
  pointer-events: none;
}

.canvas-hint {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-secondary, #888);
}

/* 连线被拒绝的短提示（M3-T04）：右上角浮层，不吃指针事件，不挡画布操作 */
.canvas-connect-toast {
  position: absolute;
  top: 12px;
  right: 12px;
  z-index: 60;
  max-width: 320px;
  padding: 6px 10px;
  border: 1px solid rgba(239, 68, 68, 0.55);
  border-radius: 8px;
  background: rgba(127, 29, 29, 0.92);
  color: #fee2e2;
  font-size: 12px;
  line-height: 1.5;
  word-break: break-all;
  pointer-events: none;
}
</style>
