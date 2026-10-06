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

function isWindowsRuntime(): boolean {
  return typeof navigator !== 'undefined' && /Windows/i.test(navigator.userAgent || '');
}

/**
 * 路径规范化键：所有「按路径查表」的统一比对口径。
 *
 * 后端 `ide_read_file` 返回的是 realpath，而界面（文件树、跳转请求）传进来的是
 * 原始路径；Windows 上两者常因分隔符、盘符大小写、`..` 相对段而不等（后端还有
 * abspath 兜底）。若直接用字符串比对，会出现：同一文件打开两次开出两个标签、
 * `saveTab(原始路径)` 查不到标签而静默 return，Ctrl+S 失效。
 *
 * 注意：本函数只产出比对键，**绝不写回标签**——标签路径始终保存 realpath，
 * 它是后端与 S2/S3 的权威路径。
 */
export function normKey(path: string): string {
  if (!path) return '';
  const key = path.replace(/\\/g, '/');
  const unc = key.startsWith('//');
  const drive = /^[a-zA-Z]:\//.test(key);
  const absolute = unc || drive || key.startsWith('/');
  const segments: string[] = [];
  for (const seg of key.split('/')) {
    if (seg === '' || seg === '.') continue;
    if (seg === '..') {
      if (segments.length && segments[segments.length - 1] !== '..') segments.pop();
      else if (!absolute) segments.push('..');
      continue;
    }
    segments.push(seg);
  }
  let normalized = segments.join('/');
  if (unc) normalized = `//${normalized}`;
  else if (absolute) normalized = `/${normalized}`;
  // Windows 路径大小写不敏感：盘符/UNC 前缀自带证据，其余靠运行时判断
  if (drive || unc || isWindowsRuntime()) normalized = normalized.toLowerCase();
  return normalized;
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
      const key = normKey(path);
      return (this.breakpointsBySession[this.session] ?? [])
        .filter((b) => normKey(b.path) === key)
        .map((b) => b.line)
        .sort((a, b) => a - b);
    },

    toggleBreakpoint(path: string, line: number) {
      if (!this.breakpointsBySession[this.session]) this.breakpointsBySession[this.session] = [];
      const list = this.breakpointsBySession[this.session];
      const key = normKey(path);
      const idx = list.findIndex((b) => normKey(b.path) === key && b.line === line);
      if (idx >= 0) list.splice(idx, 1);
      // 落库统一用 realpath（标签已打开时取标签路径），这样 S3 的断点 gutter
      // 用 `${tab.path}:${line}` 查 `breakpoints` 集合才能命中
      else list.push({ path: this.tab(path)?.path ?? path, line });
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
      const key = normKey(path);
      return this.tabs().find((t) => normKey(t.path) === key);
    },

    activePath(): string {
      return this.activeBySession[this.session] ?? '';
    },

    activate(path: string) {
      // 活动路径统一记标签的 realpath：activeBySession 与标签路径两处状态不漂移
      this.activeBySession[this.session] = this.tab(path)?.path ?? path;
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
      // 判重经 normKey：后端回的是 realpath，调用方给的是原始路径
      const existing = this.tab(path);
      if (existing) {
        this.activate(existing.path);
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
      const key = normKey(path);
      const idx = list.findIndex((t) => normKey(t.path) === key);
      if (idx < 0) return;
      list.splice(idx, 1);
      if (normKey(this.activePath()) === key) {
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
