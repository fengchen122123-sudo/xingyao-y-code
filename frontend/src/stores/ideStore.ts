import { defineStore } from 'pinia';

import { closeDocument, resetClientState } from '../editor/lsp';
import { languageIdForPath } from '../editor/setup';
import type { IdeReadFileResult } from '../types/pywebview';

export interface IdeTab {
  path: string;
  content: string;
  encoding: 'utf-8' | 'gbk';
  bom: boolean;
  eol: 'lf' | 'crlf' | 'cr' | 'mixed';
  hash: string;
  dirty: boolean;
  /** 磁盘内容被外部改动且本地有未保存内容时为 true */
  externalChange: boolean;
  /** 保存时检测到冲突 */
  conflict: boolean;
  /**
   * 曾经用哪些「调用方原始路径」openFile 到本标签。
   *
   * 记的不是猜出来的路径，而是 openFile 时后端 realpath 的对答记录：调用方给 P、
   * 后端答 realpath R，则 P 记在这里。写类动作（updateContent / saveTab / closeTab…）
   * 用调用方原路径查表时靠它命中标签，避免 Task 8 评审发现的「saveTab 静默 return」。
   * 标签的权威路径永远是 `path`（realpath），本字段只作查表别名，不参与持久化。
   */
  requestedPaths?: string[];
}

function ideApi() {
  const api = (window as any).pywebview?.api;
  if (!api || typeof api.ide_read_file !== 'function') return null;
  return api;
}

/**
 * 路径比对键：全仓「按路径查表」的统一口径，**只统一分隔符**（`\` → `/`）。
 *
 * 刻意不做两件事，因为那是在猜路径而不是读路径：
 * - 不做 `..` 词法折叠：中间段是 junction / 符号链接时，`C:\p\sub\..\a.py` 的
 *   realpath 可能与 `C:\p\a.py` 指向不同文件；词法折叠会把两者误判为同一文件，
 *   于是 openFile 不再读第二个文件、saveTab 又写回第一个文件的路径 → 静默数据不一致。
 * - 不做大小写折叠：工作区若位于 Windows 上挂载的大小写敏感目录，`A.py` 与 `a.py`
 *   是两个文件，无差别 `toLowerCase()` 会把它们误合并。
 *
 * realpath 的权威判定只来自后端 `ide_read_file` 的返回（见 `openFile`），
 * 本函数只产出比对键，**绝不写回标签**——标签路径始终保存 realpath。
 */
export function pathKey(path: string): string {
  return (path || '').replace(/\\/g, '/');
}

/** 两个路径是否指同一处：与查表同口径（精确 + 分隔符统一，不折叠大小写与 `..`） */
export function samePath(a: string, b: string): boolean {
  return pathKey(a) === pathKey(b);
}

// ── 布局快照写回的防抖 ─────────────────────────────────────────────
// 计划 Task 12 要求「带防抖的持久化动作」：打开 / 切换 / 关闭标签与增删断点都会
// 触发写回，恢复 N 个标签时逐次落盘约 2N+1 次（activate 与 openFile 各写一次，
// 每次 toggleBreakpoint 也写一次），频繁切标签同样逐次落盘。这里把写回合并到
// 静默窗口末尾，末次写出的是**当时**的完整标签列表与真实断点（payload 在 flush
// 时才从 state 计算），不会落盘半截状态。
export const LAYOUT_PERSIST_DEBOUNCE_MS = 300;

let layoutPersistTimer: ReturnType<typeof setTimeout> | undefined;

function scheduleLayoutPersist(store: { persistLayout: () => Promise<unknown> }) {
  if (layoutPersistTimer !== undefined) clearTimeout(layoutPersistTimer);
  layoutPersistTimer = setTimeout(() => {
    layoutPersistTimer = undefined;
    // 快照是尽力而为的恢复数据：落盘失败不阻断编辑，下一次变更会重新调度
    void store.persistLayout().catch(() => {});
  }, LAYOUT_PERSIST_DEBOUNCE_MS);
}

/** 结束防抖窗口并立刻落盘（会话切换前调用，见 setSession 的注释） */
function flushLayoutPersist(store: { persistLayout: () => Promise<unknown> }) {
  if (layoutPersistTimer === undefined) return;
  clearTimeout(layoutPersistTimer);
  layoutPersistTimer = undefined;
  void store.persistLayout().catch(() => {});
}

export const useIdeStore = defineStore('ide', {
  state: () => ({
    session: '',
    tabsBySession: {} as Record<string, IdeTab[]>,
    activeBySession: {} as Record<string, string>,
    /** 当前活动标签对象：S3 的调试界面与断点 gutter 直接读 activeTab.path */
    activeTab: null as IdeTab | null,
    /** 断点按会话隔离，元素形状与 core/ide/layout_store.py 的校验结构一致 */
    breakpointsBySession: {} as Record<string, { path: string; line: number }[]>,
    bottomTabBySession: {} as Record<string, string>,
    bottomPanelOpenBySession: {} as Record<string, boolean>,
    /** 待执行的跳转请求：S2 的「跳转定义」把目标行列写在这里，EditorPane 消费后清空 */
    pendingJump: null as { path: string; line: number; character: number } | null,
  }),

  getters: {
    /** `${path}:${line}` 集合，供断点 gutter 做 O(1) 判定 */
    breakpoints(state): Set<string> {
      return new Set(
        (state.breakpointsBySession[state.session] ?? []).map((b) => `${b.path}:${b.line}`),
      );
    },

    bottomTab(state): string {
      return state.bottomTabBySession[state.session] ?? 'terminal';
    },

    bottomPanelOpen(state): boolean {
      return state.bottomPanelOpenBySession[state.session] ?? true;
    },
  },

  actions: {
    setSession(session: string) {
      const next = session || '';
      const prev = this.session;
      // 切会话前先把上一个会话挂起的防抖写回落盘：persistLayout 的 payload 在 flush
      // 时才从 state 计算，若留到定时器到点，写回的会是新会话（甚至空会话）的列表，
      // 上一个会话的最新标签就丢了。
      flushLayoutPersist(this);
      // `''` 桶是「界面挂载前还没拿到会话名」时打开文件的临时归属地。离开它去绑定
      // 真实会话时把桶里已有的标签迁移过去，否则先打开的文件会随 `''` 桶变成孤儿、
      // 在新会话下静默消失（Task 8 评审 Important）。
      // 只在目标会话桶为空时迁移：目标桶已有标签说明那是它自己的状态，不搬运覆盖。
      if (prev === '' && next) {
        const orphans = this.tabsBySession[''];
        if (orphans?.length && !(this.tabsBySession[next]?.length)) {
          this.tabsBySession[next] = orphans;
          this.tabsBySession[''] = [];
          const active = this.activeBySession[''];
          if (active) {
            this.activeBySession[next] = active;
            this.activeBySession[''] = '';
          }
        }
      }
      this.session = next;
      if (!this.tabsBySession[this.session]) this.tabsBySession[this.session] = [];
      this._syncActiveTab();
    },

    /**
     * 会话 / 工作区切换后调用：后端回收全部语言服务器，前端丢弃 LSP 缓存。
     *
     * 本地状态先清、再调后端：`resetClientState()` 必须无条件执行，否则桥调用失败
     * （窗口重载期 / 后端不可用）会把上一个工作区的文档登记与诊断留在内存里，之后
     * 语言服务器的重启重发就可能落到已经切走的文档上（Task 12 评审 B）。
     * 后端那条腿是 `lsp_sync_workspace` → `LspManager.recycle_all()`；即使前端漏调
     * 本动作，`_lsp_workspace_guard` 也会在下一次 LSP 请求前兜底回收。
     */
    async syncWorkspace() {
      resetClientState();
      const api = ideApi();
      if (!api || typeof api.lsp_sync_workspace !== 'function') return;
      await api.lsp_sync_workspace();
    },

    /** 打开目标文件并把光标定位到指定行列（S2 的跳转定义 / 诊断跳转复用） */
    async jumpTo(path: string, line: number, character = 1) {
      await this.openFile(path);
      // 跳转请求里的路径统一记 realpath，EditorPane 用同一查表口径比对即可消费
      this.pendingJump = { path: this._resolvePath(path), line, character };
    },

    /** EditorPane 消费完跳转请求后调用，避免重复定位 */
    clearPendingJump() {
      this.pendingJump = null;
    },

    setBottomTab(tab: string) {
      this.bottomTabBySession[this.session] = tab;
    },

    toggleBottomPanel() {
      this.bottomPanelOpenBySession[this.session] = !this.bottomPanelOpen;
    },

    breakpointsFor(path: string): number[] {
      // 断点按标签 realpath 落库，查询前先经查表把调用方路径解析成 realpath
      const key = pathKey(this._resolvePath(path));
      return (this.breakpointsBySession[this.session] ?? [])
        .filter((b) => pathKey(b.path) === key)
        .map((b) => b.line)
        .sort((a, b) => a - b);
    },

    toggleBreakpoint(path: string, line: number) {
      if (!this.breakpointsBySession[this.session]) this.breakpointsBySession[this.session] = [];
      const list = this.breakpointsBySession[this.session];
      const key = pathKey(this._resolvePath(path));
      const idx = list.findIndex((b) => pathKey(b.path) === key && b.line === line);
      if (idx >= 0) list.splice(idx, 1);
      // 落库统一用 realpath（标签已打开时取标签路径），这样 S3 的断点 gutter
      // 用 `${tab.path}:${line}` 查 `breakpoints` 集合才能命中
      else list.push({ path: this._resolvePath(path), line });
      scheduleLayoutPersist(this);
    },

    /** 写回布局快照（标签 / 活动标签 / 断点），供重开软件后恢复。
     *  实现取自计划 Task 12 的权威片段；此处提前落地以满足 Task 8 冻结契约
     *  （toggleBreakpoint 已调用本动作，未定义会在运行期抛 TypeError）。
     *
     *  本动作是**立即写**语义（显式调用即落盘）；标签 / 断点变更路径走
     *  `scheduleLayoutPersist`，合并到防抖窗口末尾再调用它。 */
    async persistLayout() {
      const api = ideApi();
      if (!api?.ide_save_layout || !this.session) return;
      const tabs = this.tabs();
      await api.ide_save_layout(this.session, {
        version: 1,
        tabs: tabs.map((t, index) => ({ path: t.path, line: index + 1 })),
        active: this.activePath(),
        // 断点由 S3 写入 ideStore，这里必须持久化真实值，不能写死空数组
        breakpoints: this.breakpointsBySession[this.session] ?? [],
      });
    },

    /** 活动标签对象与 activeBySession 保持同步，避免两处状态漂移 */
    _syncActiveTab() {
      this.activeTab = this.tab(this.activePath()) ?? null;
    },

    tabs(): IdeTab[] {
      return this.tabsBySession[this.session] ?? [];
    },

    /**
     * 按路径查标签：**全仓唯一口径**（openFile 判重、EditorPane 跳转比对、写类动作都用它）。
     *
     * 两步，都不猜路径：
     * 1. 精确比对标签路径——只统一分隔符，不折叠大小写、不折叠 `..`；
     * 2. 再比对标签「曾经用哪些调用方路径打开」（`requestedPaths`，来自 openFile 时
     *    后端 realpath 的真实对答），保证调用方拿原路径查表也不会静默落空。
     */
    tab(path: string): IdeTab | undefined {
      const key = pathKey(path);
      if (!key) return undefined;
      const exact = this.tabs().find((t) => pathKey(t.path) === key);
      if (exact) return exact;
      return this.tabs().find((t) => (t.requestedPaths ?? []).some((p) => pathKey(p) === key));
    },

    /** 把调用方路径解析成标签的权威路径（realpath）；查不到就原样返回 */
    _resolvePath(path: string): string {
      return this.tab(path)?.path ?? path;
    },

    activePath(): string {
      return this.activeBySession[this.session] ?? '';
    },

    activate(path: string) {
      // 活动路径统一记标签的 realpath：activeBySession 与标签路径两处状态不漂移
      this.activeBySession[this.session] = this._resolvePath(path);
      this._syncActiveTab();
      // 活动标签变化也要写回快照，否则重开软件后恢复的 active 是陈旧值
      // （防抖：openFile → activate 后紧跟的补写会合并成一次落盘）
      scheduleLayoutPersist(this);
    },

    languageId(path: string): string {
      return languageIdForPath(path);
    },

    async _syncOpenFiles() {
      const api = ideApi();
      if (!api) return;
      await api.ide_set_open_files(this.tabs().map((t) => t.path));
    },

    async openFile(path: string) {
      const api = ideApi();
      if (!api) return;
      // 当前会话桶可能尚未建立（界面在挂载前也会直接打开文件）：
      // 必须先确保它存在，否则 tabs() 的 `?? []` 会返回一个被丢弃的临时数组，
      // push 进去的标签立即丢失。
      if (!this.tabsBySession[this.session]) this.tabsBySession[this.session] = [];
      // 第一次精确比对：调用方路径与已有标签（含其记录过的调用方路径）逐字相同则直接用
      const opened = this.tab(path);
      if (opened) {
        this.activate(opened.path);
        return;
      }
      const res: IdeReadFileResult = await api.ide_read_file(path);
      if (!res.ok) {
        throw new Error(res.msg || '打开文件失败');
      }
      // 后端 realpath 是唯一判重权威：拿它再精确比对一次已有标签，命中就丢弃本次读取结果。
      // 判重只依据 realpath 的精确相等（最多统一分隔符）——不折叠大小写、不折叠 `..`:
      // 那两种折叠在 junction/符号链接或大小写敏感目录上会把不同文件误判成同一个。
      // 代价是形态不同的路径会多读一次，可以接受（文件早已打开时才付）。
      const realPath = res.path || path;
      const existing = this.tab(realPath);
      if (existing) {
        // 记下这次的对答，后续用同一调用方路径查表（saveTab/closeTab…）才能命中
        if (!(existing.requestedPaths ?? []).some((p) => pathKey(p) === pathKey(path))) {
          existing.requestedPaths = [...(existing.requestedPaths ?? []), path];
        }
        this.activate(existing.path);
        return;
      }
      const tab: IdeTab = {
        path: realPath,
        content: res.content ?? '',
        encoding: (res.encoding as 'utf-8' | 'gbk') ?? 'utf-8',
        bom: !!res.bom,
        eol: (res.eol as IdeTab['eol']) ?? 'lf',
        hash: res.hash ?? '',
        dirty: false,
        externalChange: false,
        conflict: false,
        requestedPaths: samePath(path, realPath) ? [] : [path],
      };
      this.tabs().push(tab);
      this.activate(tab.path);
      await this._syncOpenFiles();
      // 标签集合有变化：写回布局快照（activate 已调度一次，防抖会与这次合并）
      scheduleLayoutPersist(this);
    },

    closeTab(path: string) {
      const list = this.tabs();
      const target = this.tab(path);
      if (!target) return;
      const key = pathKey(target.path);
      const idx = list.indexOf(target);
      if (idx < 0) return;
      list.splice(idx, 1);
      if (pathKey(this.activePath()) === key) {
        this.activeBySession[this.session] = list[Math.min(idx, list.length - 1)]?.path ?? '';
      }
      this._syncActiveTab();
      void this._syncOpenFiles();
      // 关标签 = 该文档退出编辑：通知语言服务器 didClose（EditorPane 只按活动标签挂载，
      // 切标签不会卸载文档，所以关闭点只能在这里）。传输失败由 lsp 层静默吞掉。
      void closeDocument(target.path);
      // 关标签后活动标签可能换人：写回快照，重开软件时不会恢复已关闭的标签
      scheduleLayoutPersist(this);
    },

    updateContent(path: string, content: string) {
      const tab = this.tab(path);
      if (!tab) return;
      tab.content = content;
      tab.dirty = true;
    },

    async saveTab(path: string) {
      const api = ideApi();
      const tab = this.tab(path);
      if (!api || !tab) return;
      const res = await api.ide_write_file(
        tab.path,
        tab.content,
        tab.hash,
        tab.encoding,
        tab.bom,
        tab.eol,
      );
      if (res.ok) {
        tab.hash = res.hash ?? tab.hash;
        tab.dirty = false;
        tab.conflict = false;
        tab.externalChange = false;
        return;
      }
      // 冲突：保留本地内容，交由界面让用户选择
      if (res.conflict) {
        tab.conflict = true;
        tab.externalChange = true;
        return;
      }
      throw new Error(res.msg || '保存失败');
    },

    /** 用磁盘内容覆盖当前标签（冲突时用户选择「以磁盘为准」） */
    async reloadFromDisk(path: string) {
      const api = ideApi();
      const tab = this.tab(path);
      if (!api || !tab) return;
      const res: IdeReadFileResult = await api.ide_read_file(tab.path);
      if (!res.ok) return;
      tab.content = res.content ?? '';
      tab.encoding = (res.encoding as 'utf-8' | 'gbk') ?? tab.encoding;
      tab.bom = !!res.bom;
      tab.eol = (res.eol as IdeTab['eol']) ?? tab.eol;
      tab.hash = res.hash ?? tab.hash;
      tab.dirty = false;
      tab.conflict = false;
      tab.externalChange = false;
    },

    /** 外部（星瑶或其他程序）改动了磁盘文件 */
    async handleExternalChange(path: string) {
      const tab = this.tab(path);
      if (!tab) return;
      if (tab.dirty) {
        // 有未保存内容：绝不静默覆盖，只标记
        tab.externalChange = true;
        return;
      }
      await this.reloadFromDisk(path);
    },
  },
});
