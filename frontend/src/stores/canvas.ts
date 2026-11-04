// 新画布 Pinia store（设计 §3.1：store 仍名 frontend/src/stores/canvas.ts）。
//
// 分工：组件只读 store、只调 action；一切落盘经 scheduleSave 防抖后走 bridge；
// 生成类 job（M4/M6）在这一层维护 job 表，UI 只认 jobId → 节点状态。
import { defineStore } from 'pinia';

import { canvasBridge, type ProjectMeta } from '../canvas/bridge';
import { CanvasNodeType } from '../canvas/types';
import type {
  CanvasConnection,
  CanvasNodeData,
  CanvasProject,
  InsertAssetPayload,
  ViewportTransform,
} from '../canvas/types';
import { findGroupDropTarget, snapNodesIntoGroup } from '../canvas/utils/canvas-group';
// 只借类型、不引入运行期依赖：appStore 反向 import 本 store（value import），
// 这里若用 value import 会成环；`import type` 编译期擦除，安全。
import type { CanvasEditResultData, CanvasVideoResultData } from './appStore';

/** 进行中的生成 job（图片/视频/音频同构，M4-T05 起驱动节点状态与取消按钮） */
export interface CanvasJob {
  jobId: string;
  nodeId: string;
  kind: 'image' | 'video' | 'audio';
  status: 'loading' | 'success' | 'error' | 'cancelled';
}

/** 「最近产出」托盘上限：旧画布同口径，超出后挤掉最旧的 */
const RECENT_OUTPUT_LIMIT = 24;
/** 落盘防抖窗口（拖拽/缩放期间保持高频改内存，不炸桥） */
const SAVE_DEBOUNCE_MS = 800;
/** 节点缩放下限：与 M2 fitNodeSize / NodeShell 手柄同口径 */
const NODE_MIN_SIZE = 48;

type SanitizeTarget = Pick<CanvasProject, 'nodes' | 'connections' | 'nodeCount' | 'connectionCount'>;
type Sanitizer = <T extends SanitizeTarget>(project: T) => T;

// ── sanitize 惰性加载（M2-T05 尚未交付 utils，此刻不能静态 import）──
// 不用裸 `import('../canvas/utils/canvas-sanitize')`：目标文件还不存在时
// Rollup 会在构建期直接报「Could not resolve」，整个前端打不出来。
// import.meta.glob 在构建期按文件是否存在决定命中，未命中就是空对象；
// M2-T05 交付该文件后这里自动生效，无需回来改代码。
const sanitizeModules = import.meta.glob<{ sanitizeCanvasProject?: Sanitizer }>(
  '../canvas/utils/canvas-sanitize.ts',
);
let sanitizerPromise: Promise<Sanitizer | null> | null = null;

function loadSanitizer(): Promise<Sanitizer | null> {
  if (sanitizerPromise) return sanitizerPromise;
  const key = '../canvas/utils/canvas-sanitize.ts';
  const loader = sanitizeModules[key];
  if (!loader) {
    sanitizerPromise = Promise.resolve(null);
    return sanitizerPromise;
  }
  sanitizerPromise = loader()
    .then((mod) => (typeof mod?.sanitizeCanvasProject === 'function' ? mod.sanitizeCanvasProject : null))
    .catch(() => null);
  return sanitizerPromise;
}

/**
 * 加载/保存前统一清洗（悬空连线剔除、计数同步）。
 * M2-T05 未交付时清洗函数缺失，退化为原样返回——绝不让「工具没到」变成「存不了档」。
 */
async function sanitizeProject<T extends SanitizeTarget>(project: T): Promise<T> {
  const sanitize = await loadSanitizer();
  if (!sanitize) return project;
  try {
    return sanitize(project);
  } catch {
    // 清洗本身出错时按原样存：宁可存一份可疑快照，也不丢用户正在编辑的数据
    return project;
  }
}

function randomSuffix(): string {
  return Math.random().toString(36).slice(2, 6).padEnd(4, '0');
}

/** 项目 id：'p' + 时间戳 36 进制 + 4 位随机（后端白名单 [A-Za-z0-9_-]，这里只产字母数字） */
function newProjectId(): string {
  return `p${Date.now().toString(36)}${randomSuffix()}`;
}

function cloneProject(project: CanvasProject): CanvasProject {
  return {
    ...project,
    nodes: project.nodes.map((n) => ({ ...n })),
    connections: project.connections.map((c) => ({ ...c })),
    viewport: { ...project.viewport },
    sidePanel: { ...project.sidePanel },
  };
}

/** 托盘去重键：图片看 storageKey/dataUrl，音视频看 storageKey/url，文本看内容 */
function assetKey(item: InsertAssetPayload): string {
  if (item.kind === 'text') return `text:${item.content}`;
  if (item.kind === 'image') return `image:${item.storageKey || item.dataUrl}`;
  return `${item.kind}:${item.storageKey || item.url}`;
}

// 防抖句柄放模块级：单 store 实例（app 全局唯一）；同一时刻只保留最后一次改动
let saveTimer: ReturnType<typeof setTimeout> | null = null;

function clearSaveTimer(): void {
  if (saveTimer !== null) {
    clearTimeout(saveTimer);
    saveTimer = null;
  }
}

export const useCanvasStore = defineStore('canvas', {
  state: () => ({
    project: null as CanvasProject | null,
    projectList: [] as ProjectMeta[],
    selectedNodeIds: new Set<string>(),
    selectedConnectionIds: new Set<string>(),
    jobs: new Map<string, CanvasJob>(),
    recentOutputs: [] as InsertAssetPayload[],
    saving: false,
    loadError: '',
  }),

  actions: {
    /** 新建空项目并立即落盘（新建后要出现在项目列表里，不能等防抖） */
    async createProject(title?: string): Promise<void> {
      const now = Date.now();
      const project: CanvasProject = {
        id: newProjectId(),
        title: (title || '').trim() || '未命名画布',
        createdAt: now,
        updatedAt: now,
        nodes: [] as CanvasNodeData[],
        connections: [] as CanvasConnection[],
        viewport: { x: 0, y: 0, k: 1 },
        backgroundMode: 'grid',
        sidePanel: { open: false, width: 320, nodeId: null },
        nodeCount: 0,
        connectionCount: 0,
        schema_version: 2,
      };
      this.project = project;
      this.selectedNodeIds = new Set<string>();
      this.selectedConnectionIds = new Set<string>();
      this.loadError = '';
      await this.saveNow();
      await this.refreshProjectList();
    },

    /** 打开项目：load → sanitize → state.project（v1 数据后端回 msg，这里落 loadError） */
    async openProject(id: string): Promise<void> {
      this.loadError = '';
      const res = await canvasBridge.loadProject(id);
      if (!res.ok || !res.project) {
        this.loadError = res.msg || '加载画布失败';
        return;
      }
      const clean = await sanitizeProject(cloneProject(res.project));
      this.project = clean;
      this.selectedNodeIds = new Set<string>();
      this.selectedConnectionIds = new Set<string>();
    },

    async refreshProjectList(): Promise<void> {
      const res = await canvasBridge.listProjects();
      if (!res.ok) {
        this.projectList = [];
        if (!this.loadError) this.loadError = '读取画布列表失败';
        return;
      }
      this.projectList = res.projects;
    },

    async deleteProject(id: string): Promise<void> {
      const res = await canvasBridge.deleteProject(id);
      if (!res.ok) {
        this.loadError = res.msg || '删除画布失败';
        return;
      }
      this.projectList = this.projectList.filter((p) => p.id !== id);
      if (this.project?.id === id) {
        this.project = null;
        this.selectedNodeIds = new Set<string>();
        this.selectedConnectionIds = new Set<string>();
      }
    },

    /** 改内存后调这里：800ms 内的多次改动合并成一次 saveProject */
    scheduleSave(): void {
      if (!this.project) return;
      clearSaveTimer();
      saveTimer = setTimeout(() => {
        saveTimer = null;
        void this.saveNow();
      }, SAVE_DEBOUNCE_MS);
    },

    /** 立即落盘（保存前跑 sanitize）。失败只记 loadError，不抛给调用方 */
    async saveNow(): Promise<void> {
      const current = this.project;
      if (!current) return;
      const payload = await sanitizeProject(current);
      this.saving = true;
      try {
        const res = await canvasBridge.saveProject(payload);
        if (!res.ok) {
          this.loadError = res.msg || '保存画布失败';
          return;
        }
        // 回执时间戳以服务端为准（列表排序靠它）；只在同项目期间回写
        if (typeof res.updatedAt === 'number' && this.project?.id === payload.id) {
          this.project.updatedAt = res.updatedAt;
        }
        const meta = this.projectList.find((p) => p.id === payload.id);
        if (meta && typeof res.updatedAt === 'number') meta.updatedAt = res.updatedAt;
      } finally {
        this.saving = false;
      }
    },

    /** 视口只改内存，随防抖落盘（拖拽/滚轮期间不逐帧写盘） */
    setViewport(v: ViewportTransform): void {
      if (!this.project) return;
      this.project.viewport = { ...v };
      this.scheduleSave();
    },

    // ── M3-T02 拖拽 / 缩放 / 分组吸附 ─────────────────────────────
    // 全部只做「内存 + scheduleSave」：历史栈（M3-T05）在这四个 action 外面接，
    // 现在若自己 push 快照会和后续 stack 重入，反而丢步骤。

    /**
     * 按世界坐标增量平移节点。
     * 组拖动带动组内子节点（metadata.groupId === group.id）；子节点若本身就出现在 ids 里，
     * 用集合去重保证只位移一次（否则「组 + 子节点」同时选中时子节点会被拖两次）。
     */
    moveNodes(ids: string[], dx: number, dy: number): void {
      const project = this.project;
      if (!project || !Array.isArray(ids) || !ids.length) return;
      if (!Number.isFinite(dx) || !Number.isFinite(dy)) return;
      if (dx === 0 && dy === 0) return;

      const moving = new Set(ids.filter((id) => typeof id === 'string' && id));
      if (!moving.size) return;

      // 先补齐「组 → 子节点」，再统一位移（不能边遍历边加，避免嵌套组连坐）
      for (const node of project.nodes) {
        if (node.type !== CanvasNodeType.Group || !moving.has(node.id)) continue;
        for (const child of project.nodes) {
          if (child.metadata?.groupId === node.id) moving.add(child.id);
        }
      }

      let changed = false;
      for (const node of project.nodes) {
        if (!moving.has(node.id)) continue;
        node.position = { x: node.position.x + dx, y: node.position.y + dy };
        changed = true;
      }
      if (changed) this.scheduleSave();
    },

    /**
     * 缩放单个节点（rect 为世界坐标绝对值，x/y 可省略即只改尺寸）。
     * - 宽高最小 48；
     * - metadata.freeResize 为真 → 完全自由；
     * - 否则锁当前纵横比（媒体节点建节点时尺寸已按 naturalWidth/Height 定，锁比例即锁媒体比例），
     *   手柄拖哪个方向就跟着放大哪个方向，另一维按比例回算。
     */
    resizeNode(id: string, rect: { x?: number; y?: number; width: number; height: number }): void {
      const project = this.project;
      if (!project || !rect) return;
      const node = project.nodes.find((n) => n.id === id);
      if (!node) return;
      if (!Number.isFinite(rect.width) || !Number.isFinite(rect.height)) return;

      let width = Math.max(NODE_MIN_SIZE, rect.width);
      let height = Math.max(NODE_MIN_SIZE, rect.height);

      if (!node.metadata?.freeResize && node.width > 0 && node.height > 0) {
        const ratio = node.width / node.height;
        const scaleW = width / node.width;
        const scaleH = height / node.height;
        // 单一方向手柄（只看其中一维）时无法判断意图，按变化更大的一维作主导
        const scale = Math.abs(scaleW - 1) >= Math.abs(scaleH - 1) ? scaleW : scaleH;
        width = Math.max(NODE_MIN_SIZE, node.width * scale);
        height = Math.max(NODE_MIN_SIZE, width / ratio);
        if (height < NODE_MIN_SIZE) {
          height = NODE_MIN_SIZE;
          width = Math.max(NODE_MIN_SIZE, height * ratio);
        }
      }

      node.width = width;
      node.height = height;
      node.position = {
        x: Number.isFinite(rect.x) ? (rect.x as number) : node.position.x,
        y: Number.isFinite(rect.y) ? (rect.y as number) : node.position.y,
      };
      this.scheduleSave();
    },

    /**
     * 拖拽/缩放结束时调用：落点组内 → 吸附到 GROUP_PADDING 并写 metadata.groupId；
     * 落点不在任何组 → 清除原 groupId（出组）。分组节点自身不参与吸附。
     */
    commitGroupSnap(id: string): void {
      const project = this.project;
      if (!project || !id) return;
      const node = project.nodes.find((n) => n.id === id);
      if (!node || node.type === CanvasNodeType.Group) return;

      const moved = new Set([id]);
      const target = findGroupDropTarget(moved, project.nodes);
      if (target) {
        project.nodes = snapNodesIntoGroup(moved, project.nodes, target);
        this.scheduleSave();
        return;
      }
      if (node.metadata?.groupId) {
        // 用 delete 而不是置 undefined：落盘 JSON 里不留 `"groupId": null` 这类脏字段
        const nextMeta = { ...node.metadata };
        delete nextMeta.groupId;
        node.metadata = nextMeta;
        this.scheduleSave();
      }
    },

    // ── M3-T03 选中集（正式取代 CanvasPane 里 T02 的临时直改 selectedNodeIds）──
    // 选中态是纯内存态、不落盘：不进 scheduleSave，也不进 M3-T05 的历史栈。

    /**
     * 整体/并集写选中集。
     * - additive=false：用新集合覆盖（框选/单击都是这个口径）；
     * - additive=true：与现有选中取并集（Shift 框选，参考仓 initialSelected* 快照 + 命中）。
     * id 过滤掉空串与重复，避免脏数据把选中集撑大。
     */
    setSelection(nodeIds: string[], connectionIds: string[], additive: boolean): void {
      if (!this.project) return;
      const nodes = Array.isArray(nodeIds) ? nodeIds.filter((id) => typeof id === 'string' && id) : [];
      const connections = Array.isArray(connectionIds)
        ? connectionIds.filter((id) => typeof id === 'string' && id)
        : [];
      if (!additive) {
        this.selectedNodeIds = new Set(nodes);
        this.selectedConnectionIds = new Set(connections);
        return;
      }
      const nextNodes = new Set(this.selectedNodeIds);
      for (const id of nodes) nextNodes.add(id);
      const nextConnections = new Set(this.selectedConnectionIds);
      for (const id of connections) nextConnections.add(id);
      this.selectedNodeIds = nextNodes;
      this.selectedConnectionIds = nextConnections;
    },

    /** 清空选中（点空白画布 / Esc 口径；连线选中一同清掉） */
    clearSelection(): void {
      this.selectedNodeIds = new Set<string>();
      this.selectedConnectionIds = new Set<string>();
    },

    /**
     * 单击选中单个节点。
     * - additive（Shift/Ctrl）：已选中则移除、未选中则加入（参考仓 shift 点选是切换）；
     * - 非 additive：只留这一个节点，并清掉连线选中；但若该节点已在多选集里，则保留多选
     *   （参考仓 `else if (!nextSelected.has(nodeId))` 同口径），否则多选后拖任一节点会掉成单选。
     */
    selectNode(nodeId: string, additive = false): void {
      if (!this.project || typeof nodeId !== 'string' || !nodeId) return;
      if (additive) {
        const next = new Set(this.selectedNodeIds);
        if (next.has(nodeId)) next.delete(nodeId);
        else next.add(nodeId);
        this.selectedNodeIds = next;
        this.selectedConnectionIds = new Set<string>();
        return;
      }
      const keepMulti = this.selectedNodeIds.has(nodeId) && this.selectedNodeIds.size > 1;
      if (!keepMulti) this.selectedNodeIds = new Set([nodeId]);
      this.selectedConnectionIds = new Set<string>();
    },

    /** NodeShell 的 update-text 落点：文本节点正文（聊天侧 collect 走 metadata.content） */
    updateNodeText(id: string, text: string): void {
      const project = this.project;
      if (!project) return;
      const node = project.nodes.find((n) => n.id === id);
      if (!node || typeof text !== 'string') return;
      if ((node.metadata?.content ?? '') === text) return;
      node.metadata = { ...node.metadata, content: text };
      this.scheduleSave();
    },

    // M4-T01 实装：按 job/节点回填改图产出（本任务只占位，保证 appStore 分发有落点）
    applyEditResult(_data: CanvasEditResultData): void {},

    // M4-T01/M6 实装：按 job_id 认领视频节点回填
    applyVideoResult(_data: CanvasVideoResultData): void {},

    /** canvas_focus_node 入口：M2 只做选中，视口居中随 InfiniteCanvas（M2-T06）补 */
    focusNode(nodeId: string): void {
      if (!nodeId) return;
      this.selectedNodeIds = new Set([nodeId]);
      this.selectedConnectionIds = new Set<string>();
    },

    /** collectGeneratedImages 改造落点：聊天生图产出进「最近产出」托盘（追加去重） */
    ingestGeneratedImages(items: InsertAssetPayload[]): void {
      if (!Array.isArray(items) || !items.length) return;
      const seen = new Set(this.recentOutputs.map(assetKey));
      for (const item of items) {
        if (!item) continue;
        const key = assetKey(item);
        if (seen.has(key)) continue;
        seen.add(key);
        this.recentOutputs.push(item);
      }
      if (this.recentOutputs.length > RECENT_OUTPUT_LIMIT) {
        this.recentOutputs.splice(0, this.recentOutputs.length - RECENT_OUTPUT_LIMIT);
      }
    },
  },
});

/** 测试辅助：清掉模块级防抖句柄（避免跨用例串味） */
export function __clearCanvasSaveTimer(): void {
  clearSaveTimer();
}
