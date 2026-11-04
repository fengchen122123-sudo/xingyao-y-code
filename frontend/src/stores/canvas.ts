// 新画布 Pinia store（设计 §3.1：store 仍名 frontend/src/stores/canvas.ts）。
//
// 分工：组件只读 store、只调 action；一切落盘经 scheduleSave 防抖后走 bridge；
// 生成类 job（M4/M6）在这一层维护 job 表，UI 只认 jobId → 节点状态。
import { defineStore } from 'pinia';

import { canvasBridge, type ProjectMeta } from '../canvas/bridge';
import type {
  CanvasConnection,
  CanvasNodeData,
  CanvasProject,
  InsertAssetPayload,
  ViewportTransform,
} from '../canvas/types';
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
