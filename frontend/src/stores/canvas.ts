// 无限画布的数据层。
//
// 为什么放在 Pinia 而不是 CanvasPane 组件内：p5 要让 Agent 侧（工具调用）和
// 画布 UI 读写同一张图，组件内 state 外部拿不到；而且切换页签时组件可能被
// 重建，图必须活得比组件久。
//
// 字段命名直接对齐 JSON Canvas（nodes/edges/fromNode/toNode），这样 p4 后端
// 落盘、p5 导入导出都不需要再写一层字段映射——这是本项目唯一会被长期保存的
// 结构化数据，映射层迟早会漂移。
import { defineStore } from 'pinia';

/**
 * 节点类型。前四个对齐 JSON Canvas 原生类型（导入导出时原样保留），
 * 后四个是画布自有的视觉类型，分别对应 src/canvas/nodes 下的四个组件
 * （image/video/prompt/generate，同时也是 vue-flow 的 nodeTypes key）。
 */
export type CanvasNodeType =
  | 'text'
  | 'file'
  | 'link'
  | 'group'
  | 'image'
  | 'video'
  | 'prompt'
  | 'generate';

export interface CanvasNode {
  id: string;
  type: CanvasNodeType;
  /** 世界坐标（不随缩放变），供 vue-flow 直接当 position 用 */
  x: number;
  y: number;
  width: number;
  height: number;
  /** 关联的项目内相对路径；只有 file 类节点有，其它留空 */
  file?: string;
  /**
   * 媒体 URL（图片/视频节点用）。本阶段只透传不做解析：
   * 空值由节点组件画占位（类型图标 + 文件名），不会出现破图。
   */
  url?: string;
  title?: string;
  /** 各类型自己的补充字段，避免为了一个属性反复改 schema */
  meta?: Record<string, unknown>;
}

export interface CanvasEdge {
  id: string;
  fromNode: string;
  toNode: string;
  label?: string;
}

/**
 * 「最近产出」托盘条目。
 *
 * 为什么单独一层：生图工具一次可能吐好几张图，直接铺到画布上会把用户精心
 * 摆好的构图冲散。托盘先接住，用户想要哪张再自己点进画布——画布是用户的
 * 作品集，不是工具的输出垃圾桶。
 */
export interface RecentOutput {
  /** 本地路径，托盘内的去重键（同一张图重复推送只保留一条） */
  path: string;
  /** 媒体预览地址；为空表示还没解析出来，托盘退化成显示文件名 */
  url?: string;
  /** 入托盘时间（毫秒），用于排序与「刚刚」文案 */
  addedAt: number;
  /**
   * 派生来源：这条产出是从哪个节点改出来的（只有改图产物才有）。
   * 纯生图是凭空出图，没有上游，这个字段留空。
   */
  sourceNodeId?: string;
  /** 源节点标题，仅用于展示「改自 xxx」，不参与任何查找 */
  sourceTitle?: string;
}

/**
 * 「刚刚送去修改」的源节点（内存态，不落盘）。
 *
 * 为什么必须放在 store：改图是一次跨回合的动作——用户点「送去修改」，
 * 后端把图发进对话，模型几秒到几十秒后才产出新图。发起与产出之间隔着
 * 一次完整的回合，只有 store 活得过这段间隔，组件早被重建了。
 * 只认领一次（见 clearEditSource）：否则后面无关的生图会被挂到这张源图下面。
 */
export interface EditSource {
  nodeId: string;
  file: string;
  title?: string;
}

/** 托盘容量上限：再多就不叫「最近」了，旧条目自动出栈 */
export const RECENT_OUTPUTS_LIMIT = 8;

// 新建节点的默认尺寸（世界坐标 px）。导出而非内联：导入外部 .canvas 文件时
// 缺 width/height 的节点也要落到同一个尺寸，两处各写一份迟早漂移。
// 宽度 264 与视觉稿的定宽节点一致，高度只是兜底初值（真实高度由内容撑开）
export const CANVAS_DEFAULT_NODE_WIDTH = 264;
export const CANVAS_DEFAULT_NODE_HEIGHT = 120;

/** 落盘/传输格式：schema_version 固定 1，后端按此版本号决定如何迁移 */
export interface CanvasDoc {
  schema_version: 1;
  nodes: CanvasNode[];
  edges: CanvasEdge[];
}



/** 派生节点（改图产物）与源图之间的水平间距：留出一条边的空间，连线才看得清 */
export const DERIVED_NODE_GAP_X = 56;

// 画布上的 id 只要求“同一张图内唯一”，不需要全局安全强度；
// crypto.randomUUID 不可用时（老 WebView2）退化成 8 位随机串即可
function newId(prefix: string): string {
  const cryptoObj = typeof crypto !== 'undefined' ? crypto : undefined;
  if (cryptoObj?.randomUUID) return `${prefix}-${cryptoObj.randomUUID().slice(0, 8)}`;
  return `${prefix}-${Math.random().toString(36).slice(2, 10)}`;
}

/** 取文件名（标题/展示用），口径与 canvas/nodes/types.ts 的 fileBaseName 一致 */
function baseName(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

export const useCanvasStore = defineStore('canvas', {
  state: () => ({
    nodes: [] as CanvasNode[],
    edges: [] as CanvasEdge[],
    selectedNodeId: null as string | null,
    /** 有未落盘的改动；persist() 成功后清掉，用来给顶栏做「未保存」提示 */
    dirty: false,
    /**
     * 当前画布 id（落盘文件名，也是 canvas_send_to_agent 的画布标识）。
     * 本阶段只有「默认画布」一个概念，固定值即可；将来做多画布切换时
     * 只改这一处，不用让每个调用点各自拼 id。
     */
    canvasId: 'default',
    /**
     * 最近产出托盘：对话里直接生成的图先入这里，不自动落节点。
     * 刻意不进 toDoc()——托盘是临时缓冲而不是画布内容，写进画布文件等于
     * 把一堆用户没要的图塞进他自己的画布。
     */
    recentOutputs: [] as RecentOutput[],
    /** 等待认领的改图源节点；null 表示当前没有进行中的改图 */
    pendingEditSource: null as EditSource | null,
  }),

  actions: {
    addNode(input: Partial<CanvasNode> = {}): CanvasNode {
      const node: CanvasNode = {
        id: input.id ?? newId('node'),
        type: input.type ?? 'text',
        x: input.x ?? 0,
        y: input.y ?? 0,
        width: input.width ?? CANVAS_DEFAULT_NODE_WIDTH,
        height: input.height ?? CANVAS_DEFAULT_NODE_HEIGHT,
        file: input.file,
        url: input.url,
        title: input.title,
        meta: input.meta,
      };
      this.nodes.push(node);
      this.dirty = true;
      return node;
    },

    /** 删节点必须连边一起删：留下指向空缺的边会让图渲染时抛错 */
    removeNode(id: string): void {
      const idx = this.nodes.findIndex((n) => n.id === id);
      if (idx < 0) return;
      this.nodes.splice(idx, 1);
      this.edges = this.edges.filter((e) => e.fromNode !== id && e.toNode !== id);
      if (this.selectedNodeId === id) this.selectedNodeId = null;
      this.dirty = true;
    },

    updateNodePosition(id: string, x: number, y: number): void {
      const node = this.nodes.find((n) => n.id === id);
      if (!node) return;
      node.x = x;
      node.y = y;
      this.dirty = true;
    },

    /** 返回 null 表示这条边不合法（端点不存在/自环/重复），调用方按失败处理 */
    addEdge(fromNode: string, toNode: string, label?: string): CanvasEdge | null {
      if (fromNode === toNode) return null;
      const has = (id: string) => this.nodes.some((n) => n.id === id);
      if (!has(fromNode) || !has(toNode)) return null;
      const dup = this.edges.some((e) => e.fromNode === fromNode && e.toNode === toNode);
      if (dup) return null;
      const edge: CanvasEdge = { id: newId('edge'), fromNode, toNode, label };
      this.edges.push(edge);
      this.dirty = true;
      return edge;
    },

    selectNode(id: string | null): void {
      this.selectedNodeId = id;
    },

    /** 整体替换：载入文档后 dirty 为 false（磁盘上的就是当前内容） */
    loadDoc(doc: CanvasDoc | null | undefined): void {
      this.nodes = (doc?.nodes ?? []).map((n) => ({ ...n }));
      this.edges = (doc?.edges ?? []).map((e) => ({ ...e }));
      this.selectedNodeId = null;
      this.dirty = false;
      // 载入后立刻解析「本地路径 -> 媒体 URL」：节点只存路径，WebView 加载
      // 不了 file://，不解析就是一片占位框。桥缺失/单项失败时内部静默跳过，
      // 所以不 await，也不影响画布先渲染出来
      void this.resolveMedia();
    },

    /** 深拷贝出去：调用方（桥/序列化）不应拿着 store 里的响应式对象继续改 */
    toDoc(): CanvasDoc {
      return {
        schema_version: 1,
        nodes: this.nodes.map((n) => ({ ...n })),
        edges: this.edges.map((e) => ({ ...e })),
      };
    },

    /**
     * 把节点的本地路径批量解析成媒体 URL（后端 canvas_resolve_media）。
     *
     * 只处理「有 file 但没 url」的项：已经解析过的和用户手动填了外链的都不碰。
     * 桥缺失（浏览器预览 / 后端还没暴露方法）或整体失败一律静默返回——
     * 画布继续画占位框，不上抛、不打断用户操作。
     */
    async resolveMedia(): Promise<void> {
      const nodeTargets = this.nodes.filter((n) => !!n.file && !n.url);
      const trayTargets = this.recentOutputs.filter((r) => !!r.path && !r.url);
      if (!nodeTargets.length && !trayTargets.length) return;

      const win = window as unknown as { pywebview?: { api?: Record<string, unknown> } };
      const bridge = win.pywebview?.api?.canvas_resolve_media;
      if (typeof bridge !== 'function') return;

      // 同一张图可能既是节点又在托盘里：去重后只问后端一次
      const paths = Array.from(
        new Set<string>([
          ...nodeTargets.map((n) => String(n.file)),
          ...trayTargets.map((r) => r.path),
        ]),
      );
      let payload: unknown;
      try {
        payload = await (bridge as (p: string[]) => Promise<unknown>)(paths);
      } catch (err) {
        console.debug('[canvas] canvas_resolve_media 调用失败，保持占位', err);
        return;
      }
      if (!payload || typeof payload !== 'object') return;
      const res = payload as { ok?: boolean; items?: unknown };
      if (!res.ok || !Array.isArray(res.items)) return;

      for (const raw of res.items as Array<Record<string, unknown>>) {
        const path = typeof raw?.path === 'string' ? raw.path : '';
        const url = typeof raw?.url === 'string' ? raw.url : '';
        // 失败项只带 error、媒体服务未启动的项根本没有 url：都保持占位
        if (!path || !url) continue;
        const node = this.nodes.find((n) => n.file === path);
        if (node && !node.url) node.url = url;
        const tray = this.recentOutputs.find((r) => r.path === path);
        if (tray && !tray.url) tray.url = url;
      }
      // 回填 url 不动 dirty：url 是「能不能预览」的派生结果，不是用户编辑，
      // 标记成未保存会莫名其妙点亮顶栏的未保存提示
    },

    /**
     * 记下这次「送去修改」的源节点。改图产出回到前端时靠它把两张图接上。
     * 同一时刻只保留最后一次：用户连点两张图送修改，产物只能归属最近那张。
     */
    markEditSource(source: EditSource): void {
      this.pendingEditSource = { ...source };
    },

    /** 认领完（产出已落画布）或放弃这次改图时清掉待认领的源 */
    clearEditSource(): void {
      this.pendingEditSource = null;
    },

    /**
     * 接收一张刚产出的图，按有没有「源图」分两条路径：
     *
     * - 改图产物（带 sourceNodeId 且源节点还在）：直接落成源图右侧的下游节点，
     *   并补一条「改图」边。用户点「送去修改」就是在源图上发起的延续动作，
     *   产物再让他去托盘里捞一次是多余的；边会随画布一起保存，重开关系还在。
     * - 纯生图：只入托盘、不落节点，交由用户点选。同路径重复推送挪到最前
     *   而不是新增一条——同一张图被两个工具各报一次是很常见的。
     */
    pushRecentOutput(item: {
      path: string;
      url?: string;
      sourceNodeId?: string;
      sourceTitle?: string;
    }): RecentOutput | null {
      const path = String(item?.path || '').trim();
      if (!path) return null;

      const sourceId = String(item?.sourceNodeId || '').trim();
      const source = sourceId ? this.nodes.find((n) => n.id === sourceId) : undefined;
      if (source) {
        const created = this.addNode({
          type: 'image',
          // 落点：源图右侧隔开一条边的间距，同一起点向右生长，链条不会叠在一起
          x: source.x + source.width + DERIVED_NODE_GAP_X,
          y: source.y,
          file: path,
          url: item.url,
          title: baseName(path),
          meta: {
            created_at: Date.now(),
            // 溯源信息同时写进 meta：边表达「谁是上游」，meta 表达「谁改的它」，
            // 节点被单独拷到别的画布时也不丢来历
            source_node_id: source.id,
            source_file: source.file ?? '',
            source_title: source.title ?? item.sourceTitle ?? '',
          },
        });
        this.addEdge(source.id, created.id, '改图');
        this.selectNode(created.id);
        // 缩略图解析与纯生图同一套：桥缺失时 resolveMedia 内部静默返回
        void this.resolveMedia();
        return {
          path,
          url: item.url,
          addedAt: Date.now(),
          sourceNodeId: source.id,
          sourceTitle: source.title,
        };
      }

      const existed = this.recentOutputs.findIndex((r) => r.path === path);
      if (existed >= 0) this.recentOutputs.splice(existed, 1);
      const entry: RecentOutput = { path, url: item.url, addedAt: Date.now() };
      this.recentOutputs.unshift(entry);
      if (this.recentOutputs.length > RECENT_OUTPUTS_LIMIT) {
        this.recentOutputs.length = RECENT_OUTPUTS_LIMIT;
      }
      // 顺手解析缩略图：桥不在时 resolveMedia 内部静默返回，托盘显示文件名
      void this.resolveMedia();
      return entry;
    },

    /** 清空托盘（用户手动「清空」；不碰画布上的节点） */
    clearRecentOutputs(): void {
      this.recentOutputs = [];
    },

    /**
     * 持久化占位：p4 才实现后端 canvas_save，这里只保证「桥还没就绪」时
     * 不抛错、不吞掉 dirty（后端接上后重试仍能看到待保存状态）。
     */
    persist(): void {
      if (!this.dirty) return;
      const win = window as unknown as { pywebview?: { api?: Record<string, unknown> } };
      const save = win.pywebview?.api?.canvas_save;
      if (typeof save !== 'function') {
        console.debug('[canvas] canvas_save 桥未就绪，跳过持久化（后端在 p4 提供）');
        return;
      }
      try {
        (save as (payload: CanvasDoc) => unknown)(this.toDoc());
        this.dirty = false;
      } catch (err) {
        // 保存失败不能丢 dirty：下次切换/关闭时还会再试一次
        console.debug('[canvas] 持久化失败，保留未保存标记', err);
      }
    },
  },
});
