<template>
  <div class="canvas-wrap" data-test="canvas-pane-root">
    <!-- 左列：项目列表。v1 旧数据照列但置灰（后端 supported=false），
         提示「旧版数据，不支持」而不隐藏——用户至少能看到并手动清理 -->
    <aside class="canvas-projects" data-test="canvas-project-list">
      <div class="cp-head">
        <span class="cp-title">画布项目</span>
        <button
          class="cp-new"
          data-test="canvas-new-project"
          :disabled="store.saving || creating"
          title="新建空白画布"
          @click="createProject()"
        >新建</button>
      </div>

      <div v-if="store.loadError" class="cp-error" data-test="canvas-error">{{ store.loadError }}</div>

      <div v-if="!store.projectList.length" class="cp-empty" data-test="canvas-project-empty">
        暂无画布项目
      </div>

      <button
        v-for="p in store.projectList"
        :key="p.id"
        class="cp-item"
        :class="{ active: store.project?.id === p.id, disabled: !p.supported }"
        :disabled="!p.supported"
        :title="p.supported ? p.title : '旧版数据，不支持'"
        :data-test="'canvas-project-' + p.id"
        @click="openProject(p)"
      >
        <span class="cp-name">{{ p.title }}</span>
        <span v-if="!p.supported" class="cp-badge" data-test="canvas-v1-badge">旧版数据，不支持</span>
        <span v-else class="cp-time">{{ formatTime(p.updatedAt) }}</span>
      </button>
    </aside>

    <!-- 右列：画布舞台（InfiniteCanvas 承载世界层 transform 与平移缩放手势） -->
    <section class="canvas-stage" data-test="canvas-stage">
      <div v-if="!store.project" class="canvas-hint" data-test="canvas-stage-empty">
        选择左侧项目，或新建一个画布开始
      </div>
      <div v-else class="canvas-mount" data-test="canvas-mount">
        <!-- 受控视口：手势结束/滚轮时 emit，store.setViewport 走 800ms 防抖落盘 -->
        <InfiniteCanvas
          :viewport="store.project.viewport"
          :background-mode="store.project.backgroundMode"
          @update:viewport="store.setViewport"
        >
          <!-- 世界坐标内容层：按 project.nodes 逐节点渲染。
               节点只 emit action（不 import store、不写 project），落库统一回到本组件的
               onNodeAction —— M3-T01 先占位，M3-T02 起在这里翻译成 store action -->
          <NodeShell
            v-for="node in store.project.nodes"
            :key="node.id"
            :node="node"
            :selected="store.selectedNodeIds.has(node.id)"
            :interactive="true"
            :scale="store.project.viewport.k"
            :style="nodeStyle(node)"
            @action="onNodeAction"
          >
            <component :is="NODE_COMPONENTS[node.type]" :node="node" @action="onNodeAction" />
          </NodeShell>
        </InfiniteCanvas>
        <!-- 空画布占位：浮在画布之上，不吃指针事件（不挡平移缩放） -->
        <div v-if="!store.project.nodes.length" class="canvas-empty-hint" data-test="canvas-empty-hint">
          拖入或生成素材开始
        </div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, type CSSProperties } from 'vue';

import { useCanvasStore } from '../stores/canvas';
import type { ProjectMeta } from '../canvas/bridge';
import InfiniteCanvas from './render/InfiniteCanvas.vue';
import NodeShell from './nodes/NodeShell.vue';
import { NODE_COMPONENTS } from './nodes';
import type { NodeActionPayload } from './nodes/actions';
import { CanvasNodeType, type CanvasNodeData } from './types';

const props = withDefaults(defineProps<{ active?: boolean }>(), { active: false });

const store = useCanvasStore();
const creating = ref(false);

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
      selectNode(nodeId, args?.additive === true);
      return;
    default:
      // update-title / open-director 等：M3-T05（历史栈）、M7（导演台）再接线，先不吞掉证据
      console.debug('[canvas] node action（未接线）', payload);
  }
}

/**
 * 选中落点。M3-T03 会换成 store.setSelection(nodeIds, connectionIds, additive)；
 * 在此之前先直接读写 selectedNodeIds（store 目前没有正式选中 action）。
 */
function selectNode(nodeId: string, additive: boolean): void {
  if (!store.project) return;
  const next = new Set(additive ? store.selectedNodeIds : []);
  if (additive && next.has(nodeId)) next.delete(nodeId);
  else next.add(nodeId);
  store.selectedNodeIds = next;
  if (!additive) store.selectedConnectionIds = new Set<string>();
}

/** 只刷新列表、不动当前打开的项目：切页签回来不应把编辑中的画布重置掉 */
function refreshList(): void {
  void store.refreshProjectList();
}

async function createProject(): Promise<void> {
  if (creating.value) return;
  creating.value = true;
  try {
    await store.createProject();
  } finally {
    creating.value = false;
  }
}

function openProject(meta: ProjectMeta): void {
  if (!meta.supported || store.project?.id === meta.id) return;
  void store.openProject(meta.id);
}

function formatTime(ts: number): string {
  if (!ts) return '';
  try {
    return new Date(ts).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
  } catch {
    return '';
  }
}

// 面板是 v-show 常驻挂载：只在「首次激活」与「再次激活」时拉列表，
// 未激活时不打桥（启动阶段右栏默认不显示画布，不给桥加无谓调用）
watch(
  () => props.active,
  (active) => {
    if (active) refreshList();
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
</style>
