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
import { cloneNodes, deserializeNodes, serializeNodes } from '../canvas/utils/clipboard';
import { findGroupDropTarget, snapNodesIntoGroup } from '../canvas/utils/canvas-group';
import { validateConnection } from '../canvas/utils/canvas-reference-guard';
import { HistoryStack, type CanvasHistorySnapshot } from '../canvas/utils/history';
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

/**
 * 连线 id：`c_` + 递增序号（取现有最大序号 +1，并跳过被占用的号）。
 * 递增而不是纯随机：一是同一次会话里 id 可预期、调试好认；二是删了再连不会和旧 id 撞车。
 */
function newConnectionId(connections: CanvasConnection[]): string {
  let max = 0;
  for (const connection of connections) {
    const match = /^c_(\d+)$/.exec(connection.id || '');
    if (match) max = Math.max(max, Number(match[1]));
  }
  const used = new Set(connections.map((connection) => connection.id));
  let next = max + 1;
  while (used.has(`c_${next}`)) next += 1;
  return `c_${next}`;
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

// ── M3-T05 历史栈 / 剪贴板 ────────────────────────────────────
// 放模块级而不是 state：HistoryStack 是带内部队列的类，塞进 Pinia state 会被 reactive
// 代理包一层（每次读写都过 Proxy，纯浪费）；且它只服务「当前打开的项目」，换项目即清。
const history = new HistoryStack();
/** 剪贴板内容（带 `xingyao-canvas:nodes:` 标记的字符串）；放模块级 = 跨项目可粘贴 */
let clipboardRaw = '';
/** 历史栈归属的项目 id：换项目懒清栈（免得在新建/打开/删除每条路径上都写一遍 clear） */
let historyProjectId = '';

/** 取「当前状态」快照：HistoryStack.push/undo 内部会深拷贝，这里给引用即可 */
function currentSnapshot(project: CanvasProject): CanvasHistorySnapshot {
  return { nodes: project.nodes, connections: project.connections };
}

/** 项目换了就清栈：跨项目撤销毫无意义，还可能把 A 的节点还到 B 里 */
function ensureHistoryProject(project: CanvasProject): void {
  if (historyProjectId === project.id) return;
  historyProjectId = project.id;
  history.clear();
}

/** 同步 nodeCount / connectionCount（删除/粘贴后必然变，sanitize 只在落盘前才跑） */
function syncProjectCounts(project: CanvasProject): void {
  project.nodeCount = project.nodes.length;
  project.connectionCount = project.connections.length;
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

    // ── M3-T05 撤销/重做 · 复制粘贴 · 删除 ────────────────────────
    // 约定：动作在真正改 project【之前】调 pushHistory()，栈里存的是「变更前的状态」；
    // undo 时把当前状态作为 current 交给栈，它压进 redo 侧，redo 才能回到撤销前的样子。
    // 选中态与视口不进历史（纯 UI 状态，撤销不该把它们也倒回去）。

    /** 记录一次「变更前」的快照（合并窗口 300ms / 容量 50 由 HistoryStack 管） */
    pushHistory(): void {
      const project = this.project;
      if (!project) return;
      ensureHistoryProject(project);
      history.push(currentSnapshot(project));
    },

    /** 撤销一步：恢复节点与连线，摘掉已失效的选中，走防抖落盘。栈空返回 false */
    undo(): boolean {
      const project = this.project;
      if (!project) return false;
      ensureHistoryProject(project);
      const snapshot = history.undo(currentSnapshot(project));
      if (!snapshot) return false;
      project.nodes = snapshot.nodes;
      project.connections = snapshot.connections;
      syncProjectCounts(project);
      this.pruneSelection();
      this.scheduleSave();
      return true;
    },

    /** 重做一步：与 undo 对称（栈空返回 false，调用方据此不拦默认按键行为） */
    redo(): boolean {
      const project = this.project;
      if (!project) return false;
      ensureHistoryProject(project);
      const snapshot = history.redo(currentSnapshot(project));
      if (!snapshot) return false;
      project.nodes = snapshot.nodes;
      project.connections = snapshot.connections;
      syncProjectCounts(project);
      this.pruneSelection();
      this.scheduleSave();
      return true;
    },

    /** 把选中集里已经不存在的 id 摘掉（撤销/删除后指针还停在老 id 上会点不中任何东西） */
    pruneSelection(): void {
      const project = this.project;
      if (!project) return;
      const nodeIds = new Set(project.nodes.map((node) => node.id));
      const connectionIds = new Set(project.connections.map((connection) => connection.id));
      this.selectedNodeIds = new Set([...this.selectedNodeIds].filter((id) => nodeIds.has(id)));
      this.selectedConnectionIds = new Set(
        [...this.selectedConnectionIds].filter((id) => connectionIds.has(id)),
      );
    },

    /**
     * 复制选中节点到内部剪贴板（含它们之间的内部连线）。
     * 不走系统剪贴板：浏览器 clipboard API 要用户手势 + 权限，且画布是结构化数据；
     * 模块级字符串即可满足「跨项目粘贴」，也不会被别的程序误读。
     */
    copySelection(): boolean {
      const project = this.project;
      if (!project || !this.selectedNodeIds.size) return false;
      const nodes = project.nodes.filter((node) => this.selectedNodeIds.has(node.id));
      if (!nodes.length) return false;
      const raw = serializeNodes(nodes, project.connections);
      if (!raw) return false;
      clipboardRaw = raw;
      return true;
    },

    /**
     * 粘贴：id 全量重生成 + 位置偏移 24px。
     * raw 显式传入时优先用它（外部来源/跨项目数据走这条），否则用最近一次复制的内部内容。
     */
    pasteClipboard(raw?: string): boolean {
      const project = this.project;
      if (!project) return false;
      const source = typeof raw === 'string' && raw ? raw : clipboardRaw;
      const payload = deserializeNodes(source);
      if (!payload) return false;
      const cloned = cloneNodes(payload);
      if (!cloned.nodes.length) return false;

      this.pushHistory();
      project.nodes = [...project.nodes, ...cloned.nodes];
      project.connections = [...project.connections, ...cloned.connections];
      syncProjectCounts(project);
      // 粘完把选中换成新节点：接着拖/复制都是刚粘出来的那一批
      this.selectedNodeIds = new Set(cloned.nodes.map((node) => node.id));
      this.selectedConnectionIds = new Set<string>();
      this.scheduleSave();
      return true;
    },

    /**
     * 删除选中：节点连带挂在它们身上的连线一起走；被删分组下的残余子节点解除 groupId
     * （否则会留下指向不存在分组的悬空归属）；只选了连线时只删连线。
     */
    deleteSelection(): boolean {
      const project = this.project;
      if (!project) return false;
      const selectedNodes = this.selectedNodeIds;
      const selectedConnections = this.selectedConnectionIds;
      if (!selectedNodes.size && !selectedConnections.size) return false;

      const removedNodes = new Set(
        [...selectedNodes].filter((id) => project.nodes.some((node) => node.id === id)),
      );
      const removedConnections = new Set(
        [...selectedConnections].filter((id) => project.connections.some((c) => c.id === id)),
      );
      if (!removedNodes.size && !removedConnections.size) return false;

      this.pushHistory();
      if (removedNodes.size) {
        const kept: CanvasNodeData[] = [];
        for (const node of project.nodes) {
          if (removedNodes.has(node.id)) continue;
          const groupId = node.metadata?.groupId;
          if (groupId && removedNodes.has(groupId)) {
            // 用 delete 而不是置 undefined：落盘 JSON 里不留空 groupId 这类脏字段
            const nextMeta = { ...node.metadata };
            delete nextMeta.groupId;
            kept.push({ ...node, metadata: nextMeta });
            continue;
          }
          kept.push(node);
        }
        project.nodes = kept;
      }
      project.connections = project.connections.filter(
        (connection) =>
          !removedConnections.has(connection.id) &&
          !removedNodes.has(connection.fromNodeId) &&
          !removedNodes.has(connection.toNodeId),
      );
      syncProjectCounts(project);
      this.selectedNodeIds = new Set<string>();
      this.selectedConnectionIds = new Set<string>();
      this.scheduleSave();
      return true;
    },

    /** Ctrl+D：复制 + 粘贴一次（复制失败就整体失败，不留半截状态） */
    duplicateSelection(): boolean {
      if (!this.copySelection()) return false;
      return this.pasteClipboard();
    },

    /** Ctrl+A：全选节点与连线（选中态不落盘） */
    selectAll(): void {
      const project = this.project;
      if (!project) return;
      this.selectedNodeIds = new Set(project.nodes.map((node) => node.id));
      this.selectedConnectionIds = new Set(project.connections.map((connection) => connection.id));
    },

    // ── M3-T04 连线（资源引用）─────────────────────────────────────
    // 合法性全在 canvas-reference-guard 里判（类型兼容表 + 自连 + 重复 + 环），
    // store 只负责「过了就建、并落盘」，绝不在两处各写一套规则。

    /**
     * 建立一条 from → to 的引用连线。
     * 失败不落盘、不改内存，直接把 reason 交给调用方（CanvasPane 弹 toast）。
     * 成功即写 connectionCount 并走 800ms 防抖落盘（拉线是单次操作，不必立即 save）。
     */
    connect(fromNodeId: string, toNodeId: string): { ok: boolean; reason?: string } {
      const project = this.project;
      if (!project) return { ok: false, reason: '没有打开的画布' };
      const check = validateConnection({
        fromNodeId,
        toNodeId,
        nodes: project.nodes,
        connections: project.connections,
      });
      if (!check.ok) return { ok: false, reason: check.reason || '连线不可用' };

      this.pushHistory();
      const connection: CanvasConnection = {
        id: newConnectionId(project.connections),
        fromNodeId,
        toNodeId,
      };
      project.connections.push(connection);
      project.connectionCount = project.connections.length;
      this.scheduleSave();
      return { ok: true };
    },

    /** 删除一条连线；连带把它从选中集里摘掉（否则工具条会显示已删除的连线） */
    disconnect(connectionId: string): void {
      const project = this.project;
      if (!project || typeof connectionId !== 'string' || !connectionId) return;
      const next = project.connections.filter((connection) => connection.id !== connectionId);
      if (next.length === project.connections.length) return;
      this.pushHistory();
      project.connections = next;
      project.connectionCount = next.length;
      if (this.selectedConnectionIds.has(connectionId)) {
        const selected = new Set(this.selectedConnectionIds);
        selected.delete(connectionId);
        this.selectedConnectionIds = selected;
      }
      this.scheduleSave();
    },

    // ── M3-T02 拖拽 / 缩放 / 分组吸附 ─────────────────────────────
    // 一律「先 pushHistory 打点（变更前），再改内存，最后 scheduleSave」：
    // 打点顺序反了会把改完的状态当成撤销目标，撤销等于没撤。

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

      // 没命中任何节点就别打点（避免空转污染历史栈）
      if (!project.nodes.some((node) => moving.has(node.id))) return;
      // 变更前打点：拖拽会连打几十次，300ms 合并窗口收成一步
      this.pushHistory();
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

      this.pushHistory();
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
      // 既不在组里、也没落到任何组上 → 无变更，不打历史点
      if (!target && !node.metadata?.groupId) return;
      this.pushHistory();
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
      this.pushHistory();
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
