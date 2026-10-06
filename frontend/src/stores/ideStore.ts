import { defineStore } from 'pinia';

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
}

function ideApi() {
  const api = (window as any).pywebview?.api;
  if (!api || typeof api.ide_read_file !== 'function') return null;
  return api;
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
      this.session = session || '';
      if (!this.tabsBySession[this.session]) this.tabsBySession[this.session] = [];
      this._syncActiveTab();
    },

    /** 打开目标文件并把光标定位到指定行列（S2 的跳转定义 / 诊断跳转复用） */
    async jumpTo(path: string, line: number, character = 1) {
      await this.openFile(path);
      this.pendingJump = { path: this.tab(path)?.path ?? path, line, character };
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
      return (this.breakpointsBySession[this.session] ?? [])
        .filter((b) => b.path === path)
        .map((b) => b.line)
        .sort((a, b) => a - b);
    },

    toggleBreakpoint(path: string, line: number) {
      if (!this.breakpointsBySession[this.session]) this.breakpointsBySession[this.session] = [];
      const list = this.breakpointsBySession[this.session];
      const idx = list.findIndex((b) => b.path === path && b.line === line);
      if (idx >= 0) list.splice(idx, 1);
      else list.push({ path, line });
      void this.persistLayout();
    },

    /** 写回布局快照（标签 / 活动标签 / 断点），供重开软件后恢复。
     *  实现取自计划 Task 12 的权威片段；此处提前落地以满足 Task 8 冻结契约
     *  （toggleBreakpoint 已调用本动作，未定义会在运行期抛 TypeError）。 */
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

    tab(path: string): IdeTab | undefined {
      return this.tabs().find((t) => t.path === path);
    },

    activePath(): string {
      return this.activeBySession[this.session] ?? '';
    },

    activate(path: string) {
      this.activeBySession[this.session] = path;
      this._syncActiveTab();
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
      const existing = this.tab(path);
      if (existing) {
        this.activate(path);
        return;
      }
      const res: IdeReadFileResult = await api.ide_read_file(path);
      if (!res.ok) {
        throw new Error(res.msg || '打开文件失败');
      }
      const tab: IdeTab = {
        path: res.path || path,
        content: res.content ?? '',
        encoding: (res.encoding as 'utf-8' | 'gbk') ?? 'utf-8',
        bom: !!res.bom,
        eol: (res.eol as IdeTab['eol']) ?? 'lf',
        hash: res.hash ?? '',
        dirty: false,
        externalChange: false,
        conflict: false,
      };
      this.tabs().push(tab);
      this.activate(tab.path);
      await this._syncOpenFiles();
    },

    closeTab(path: string) {
      const list = this.tabs();
      const idx = list.findIndex((t) => t.path === path);
      if (idx < 0) return;
      list.splice(idx, 1);
      if (this.activePath() === path) {
        this.activeBySession[this.session] = list[Math.min(idx, list.length - 1)]?.path ?? '';
      }
      this._syncActiveTab();
      void this._syncOpenFiles();
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
