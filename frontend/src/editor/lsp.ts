/**
 * LSP 前端客户端。
 *
 * 分工：本模块负责「怎么发请求、怎么同步文档、诊断怎么缓存、事件怎么分发」，
 * 并在 Task 9 / Task 10 里把结果映射成 CodeMirror 的补全、诊断、悬停、
 * 跳转、格式化、重命名。后端进程管理与事件派发全部在 Python 侧，前端不接触子进程。
 *
 * 路径键：诊断缓存与视图索引统一用 normKey() 归一化，因为后端把 LSP 的
 * file:// URI 还原成本地路径后，盘符大小写与斜杠方向可能与编辑器侧不完全一致。
 */

import {
  autocompletion,
  type Completion,
  type CompletionContext,
  type CompletionResult,
} from '@codemirror/autocomplete';
import { forceLinting, linter, type Diagnostic } from '@codemirror/lint';
import type { Extension } from '@codemirror/state';
import { EditorView, ViewPlugin, type PluginValue } from '@codemirror/view';

export interface LspError {
  code: number;
  message: string;
}

export interface LspResponse<T = unknown> {
  ok: boolean;
  result?: T;
  error?: LspError;
  msg?: string;
}

/**
 * `lsp_status` **事件**的 payload。后端 `LspManager._emit_status` 输出
 * `server/language/workspace/state/msg`，因此事件一定带 `workspace`。
 * 注意与 `lsp_server_status` **响应**里的 `servers[]` 条目区分：那里不含
 * `workspace`（它在响应顶层），两者是不同形状，不要合并。
 */
export interface LspServerStatus {
  server: string;
  language: string;
  workspace?: string;
  state: 'missing' | 'stopped' | 'starting' | 'running' | 'restarting' | 'failed';
  executable?: string;
  source?: 'override' | 'bundled' | 'path' | 'missing';
  pid?: number | null;
  msg?: string;
}

export interface LspPosition {
  line: number;
  character: number;
}

export interface LspRange {
  start: LspPosition;
  end: LspPosition;
}

export interface LspDiagnostic {
  range: LspRange;
  severity?: 1 | 2 | 3 | 4;
  message: string;
  source?: string;
  code?: string | number;
}

export interface LspLocation {
  path: string;
  line: number;
  character: number;
}

export interface LspCompletionItem {
  label: string;
  kind?: number;
  detail?: string;
  documentation?: string | { value: string };
  insertText?: string;
}

export interface LspBridgeApi {
  lsp_request(path: string, method: string, params: Record<string, unknown>): Promise<LspResponse>;
  lsp_notify(path: string, method: string, params: Record<string, unknown>): Promise<LspResponse>;
  lsp_server_status(workspace?: string): Promise<{
    ok: boolean;
    workspace: string;
    servers: LspServerStatus[];
    msg?: string;
  }>;
  lsp_set_server_path(serverId: string, path: string): Promise<{ ok: boolean; msg?: string }>;
  lsp_sync_workspace(): Promise<{
    ok: boolean;
    workspace: string;
    recycled: string[];
    msg?: string;
  }>;
}

/** didChange 防抖窗口：比后端 200ms 诊断合批略小，保证一次停击键只发一次变更。 */
export const CHANGE_DEBOUNCE_MS = 150;

/**
 * 本地文档登记表。`text` 保存最近一次已知文本（openDocument 的全文、或最近一次
 * changeDocument 的内容）：语言服务器崩溃重启后新进程文档表为空，重发 didOpen
 * 必须带全文，而「只发过 didChange」的文档在服务端是无从还原的。
 */
const openDocs = new Map<
  string,
  { languageId: string; version: number; text: string; workspace?: string }
>();
const pendingChanges = new Map<string, { text: string; timer: ReturnType<typeof setTimeout> }>();
const diagnosticsByPath = new Map<string, LspDiagnostic[]>();
/** normKey 会改写盘符大小写与斜杠，回传后端时必须用原样路径，这里按 key 反查*/
const docPaths = new Map<string, string>();
const diagnosticListeners = new Set<(path: string, diagnostics: LspDiagnostic[]) => void>();
const statusListeners = new Set<(status: LspServerStatus) => void>();

export function bridgeApi(): LspBridgeApi | null {
  const api = (window as any).pywebview?.api;
  if (!api || typeof api.lsp_request !== 'function') return null;
  return api as LspBridgeApi;
}

export function normKey(path: string): string {
  return path.replace(/\//g, '\\').toLowerCase();
}

/**
 * 工作区比较键：斜杠方向与大小写都可能因来源不同而不同（后端 `status()` 报的是
 * `os.path.normcase` 归一化后的路径，`_emit_status` 报的是原始路径），统一归一化
 * 后再比，末尾斜杠也去掉。
 */
export function workspaceKey(workspace: string | undefined | null): string {
  return String(workspace ?? '')
    .replace(/\//g, '\\')
    .replace(/\\+$/, '')
    .toLowerCase();
}

/**
 * 当前工作区（后端 `lsp_sync_workspace` 的响应口径）。`openDocument` 打开的文档
 * 记在自己名下：语言服务器按 (workspace, server) 记账，重发 didOpen 必须按工作区
 * 过滤，否则会把 A 区的文档重发给 B 区的同语言服务器（LSP 规范下重复 didOpen
 * 是协议错误，且会让 B 区服务器持有不属于它的文档）。
 */
let activeWorkspace: string | undefined;

export function setActiveWorkspace(workspace: string | undefined | null): void {
  activeWorkspace = workspace ? String(workspace) : undefined;
}

export function fileUri(path: string): string {
  const normalized = path.replace(/\\/g, '/');
  const withRoot = normalized.startsWith('/') ? normalized : `/${normalized}`;
  return `file://${encodeURI(withRoot)}`;
}

export function uriToPath(uri: string): string {
  if (!uri.startsWith('file://')) return '';
  let rest = uri.slice('file://'.length);
  if (/^\/[A-Za-z]:/.test(rest)) rest = rest.slice(1);
  return decodeURIComponent(rest).replace(/\//g, '\\');
}

/**
 * 传输层兜底：pywebview 桥调用可能 reject（窗口重载期、传输异常），
 * 任何一条通知都不允许把异常抛给编辑器输入链路（Task 8 评审 I-3）。
 * 失败时返回 null，调用方按「未送达」处理。
 */
async function notifySafe(
  api: LspBridgeApi,
  path: string,
  method: string,
  params: Record<string, unknown>,
): Promise<LspResponse | null> {
  try {
    return await api.lsp_notify(path, method, params);
  } catch (err) {
    console.warn(`[lsp] ${method} 传输失败（已忽略，编辑器不受影响）`, path, err);
    return null;
  }
}

export async function openDocument(
  path: string,
  languageId: string,
  text: string,
  workspace?: string,
): Promise<void> {
  const api = bridgeApi();
  if (!api) return;
  const key = normKey(path);
  if (openDocs.has(key)) {
    changeDocument(path, text);
    return;
  }
  // 先发 didOpen、拿到 ok 再登记：后端在路径越权（-32003）或该类型未配置语言
  // 服务器（-32000，已降级为纯文本编辑）时返回 ok:false，若此时仍登记进 openDocs，
  // 之后每次击键都会向一个从未打开的文档发 didChange —— 白跑桥调用且用户零反馈。
  // 传输异常（reject）同样按「未送达」处理，绝不登记幽灵文档（评审 I-4）。
  const res = await notifySafe(api, path, 'textDocument/didOpen', {
    textDocument: { uri: fileUri(path), languageId, version: 1, text },
  });
  if (!res || !res.ok) {
    console.warn(
      '[lsp] didOpen 未生效，该文件按纯文本降级（不再发送 didChange）',
      path,
      res ? (res.error?.code ?? '') : 'transport',
      res ? (res.error?.message ?? res.msg ?? '') : '',
    );
    return;
  }
  // 归属工作区：调用方没给就用当前工作区（后端 sync 响应口径），都没有时留 undefined
  // ——「归属未知」按参与处理（见 reopenOpenDocuments）。
  openDocs.set(key, { languageId, version: 1, text, workspace: workspace ?? activeWorkspace });
  docPaths.set(key, path);
}

export function changeDocument(path: string, text: string): void {
  const key = normKey(path);
  const doc = openDocs.get(key);
  if (!doc) return;
  // 留一份最新文本：服务器重启后重发 didOpen 需要全文
  doc.text = text;
  const existing = pendingChanges.get(key);
  if (existing) clearTimeout(existing.timer);
  const timer = setTimeout(() => {
    // 桥调用 reject 时不让 promise 逃逸（unhandled rejection），否则防抖定时器已被
    // 消费 → 该次变更彻底丢失（评审 I-3）
    void flushChange(key).catch(() => {
      /* flushChange 内部已兜底；这里只保证不改动编辑器输入链路 */
    });
  }, CHANGE_DEBOUNCE_MS);
  pendingChanges.set(key, { text, timer });
}

async function flushChange(key: string): Promise<void> {
  const doc = openDocs.get(key);
  const pending = pendingChanges.get(key);
  if (!doc || !pending) return;
  pendingChanges.delete(key);
  doc.version += 1;
  const api = bridgeApi();
  if (!api) return;
  const path = pendingPath(key);
  const res = await notifySafe(api, path, 'textDocument/didChange', {
    textDocument: { uri: fileUri(path), version: doc.version },
    contentChanges: [{ text: pending.text }],
  });
  if (res && !res.ok) {
    console.warn('[lsp] didChange 未送达', path, res.error?.code ?? '', res.error?.message ?? res.msg ?? '');
  }
}

/** normKey 会改写盘符大小写，回传后端时必须用原样路径，这里从登记表反查。 */
function pendingPath(key: string): string {
  return docPaths.get(key) ?? key;
}

export async function closeDocument(path: string): Promise<void> {
  const key = normKey(path);
  if (!openDocs.has(key)) return;
  const pending = pendingChanges.get(key);
  if (pending) {
    clearTimeout(pending.timer);
    pendingChanges.delete(key);
  }
  // 本地登记先撤销：即使 didClose 送不出去，也不能让已关闭的文件继续留在登记表里
  openDocs.delete(key);
  docPaths.delete(key);
  diagnosticsByPath.delete(key);
  const api = bridgeApi();
  if (!api) return;
  const res = await notifySafe(api, path, 'textDocument/didClose', {
    textDocument: { uri: fileUri(path) },
  });
  if (res && !res.ok) {
    console.warn('[lsp] didClose 未送达', path, res.error?.code ?? '', res.error?.message ?? res.msg ?? '');
  }
}

export async function lspRequest<T = unknown>(
  path: string,
  method: string,
  params: Record<string, unknown>,
): Promise<T | null> {
  const api = bridgeApi();
  // 桥整体缺失（浏览器预览 / 单测环境）不算失败分支，不打日志：否则每次触发
  // 补全都会刷一行警告。真正的失败（服务器缺失 / 方法不支持 / 越权）在下面记日志。
  if (!api) return null;
  let res: LspResponse;
  try {
    res = await api.lsp_request(path, method, params);
  } catch (err) {
    // 传输异常一律静默回落为「没有结果」：补全/悬停拿不到结果时不打断输入（评审 I-3）
    console.warn(`[lsp] ${method} 传输失败（静默回落）`, path, err);
    return null;
  }
  if (!res.ok) {
    // 失败原因必须可见（评审 Minor-1）：服务器缺失 / 不含该方法 / 越权 / 内部错误
    console.warn(
      `[lsp] ${method} 失败`,
      path,
      res.error?.code ?? '',
      res.error?.message ?? res.msg ?? '',
    );
    return null;
  }
  return (res.result ?? null) as T | null;
}

export function onDiagnostics(
  fn: (path: string, diagnostics: LspDiagnostic[]) => void,
): () => void {
  diagnosticListeners.add(fn);
  return () => {
    diagnosticListeners.delete(fn);
  };
}

export function onServerStatus(fn: (status: LspServerStatus) => void): () => void {
  statusListeners.add(fn);
  return () => {
    statusListeners.delete(fn);
  };
}

export function diagnosticsFor(path: string): LspDiagnostic[] {
  return diagnosticsByPath.get(normKey(path)) ?? [];
}

// ---------------------------------------------------------------------------
// 服务器崩溃重启：新进程的文档表是空的，前端必须重发 didOpen
// ---------------------------------------------------------------------------

/** 状态机按 (workspace, server) 记账：servers 是每工作区每语言一个进程。 */
const serverStateByKey = new Map<string, string>();
/** 服务器离开 running（崩溃重启 / 被回收）后置位，直到它再次 running。 */
const reopenPending = new Set<string>();

function serverKeyOf(status: LspServerStatus): string {
  return `${status.workspace ?? ''}|${status.server}`;
}

/**
 * 服务器重启后对「仍登记为已打开」的文档重发 didOpen（带最新文本）。
 *
 * 前提（Task 12 评审 B 收口）：切换工作区时 `resetClientState()` 会同时清空
 * `openDocs` 与按 (workspace, server) 记账的 `serverStateByKey` / `reopenPending`，
 * 生产入口是 `ideStore.syncWorkspace()`（切换点调用）。三条状态必须一起清：
 * 只清文档、留下旧工作区的 `reopenPending` 键时，旧键上一次迟到的 `running`
 * 会把新工作区的文档当成「自己刚重启的服务器仍打开的文档」重发 didOpen
 * —— 那正是跨工作区误发（LSP 规范下重复 didOpen 是协议错误）。
 *
 * 只按 `languageId` 过滤本身不足以判定归属：同一语言在多个工作区各有一个进程，
 * 真正区分它们的是 `serverKeyOf` 的 (workspace, server) 口径。因此重发时按
 * 「文档登记的工作区 == 事件里的工作区」二次过滤（S2 验收欠账 4）：
 * - 文档记过工作区：只有与事件工作区一致才重发；
 * - 文档没记工作区（登记时前端还不知道当前工作区，例如挂载早期的文件）：
 *   按参与处理，交回 `resetClientState()` 的三态同清兜底，绝不漏发。
 */
async function reopenOpenDocuments(languageId: string, workspace?: string): Promise<void> {
  const api = bridgeApi();
  const jobs: Promise<void>[] = [];
  const target = workspaceKey(workspace);
  openDocs.forEach((doc, key) => {
    if (languageId && doc.languageId !== languageId) return;
    if (doc.workspace !== undefined && target && workspaceKey(doc.workspace) !== target) return;
    const path = pendingPath(key);
    // 待发变更的文本并入 didOpen 全文：那次 didChange 已经没有意义了
    const pending = pendingChanges.get(key);
    if (pending) {
      clearTimeout(pending.timer);
      pendingChanges.delete(key);
    }
    if (pending) doc.text = pending.text;
    doc.version += 1;
    jobs.push(
      (async () => {
        if (!api) return;
        const res = await notifySafe(api, path, 'textDocument/didOpen', {
          textDocument: {
            uri: fileUri(path),
            languageId: doc.languageId,
            version: doc.version,
            text: doc.text,
          },
        });
        if (!res || !res.ok) {
          // 重发失败（服务器仍不可用 / 越权）：撤销登记，避免继续向空文档发 didChange
          console.warn('[lsp] 重启后重发 didOpen 未生效，撤销本地登记', path);
          if (openDocs.get(key) === doc) openDocs.delete(key);
          docPaths.delete(key);
        }
      })(),
    );
  });
  await Promise.all(jobs);
}

/**
 * 状态观察器：`restarting`（崩溃重启）或 running → stopped/failed（进程被回收或
 * 重启次数用尽）说明旧进程的文档表连带丢失；再次 `running` 时重发 didOpen。
 * 刻意不在「首次 starting → running」时重发——那是新服务器首次启动，didOpen
 * 正由 openDocument 发出，重复发送会被服务器视为协议错误。
 */
function handleServerStatus(status: LspServerStatus): void {
  const key = serverKeyOf(status);
  const prev = serverStateByKey.get(key);
  serverStateByKey.set(key, status.state);
  const leftRunning =
    status.state === 'restarting' ||
    ((status.state === 'stopped' || status.state === 'failed') && prev === 'running');
  if (leftRunning) {
    reopenPending.add(key);
    return;
  }
  if (status.state === 'running' && reopenPending.has(key)) {
    reopenPending.delete(key);
    void reopenOpenDocuments(status.language, status.workspace);
  }
}

// 模块加载即挂上：重启可能发生在 IDE 面板未挂载时，重发逻辑不能依赖组件生命周期
onServerStatus(handleServerStatus);

/**
 * 诊断落地前先深拷贝并冻结（评审 Minor-2）：payload 数组/range 对象是事件的引用，
 * 若被下游原地排序或裁剪，会污染本缓存与后续每一帧的 lint 结果。
 */
function freezeDiagnostics(items: LspDiagnostic[]): LspDiagnostic[] {
  const copies = (items ?? []).map((item) => {
    const range = item?.range;
    if (!range) return Object.freeze({ ...item });
    return Object.freeze({
      ...item,
      range: Object.freeze({
        start: Object.freeze({ ...range.start }),
        end: Object.freeze({ ...range.end }),
      }),
    });
  });
  return Object.freeze(copies) as unknown as LspDiagnostic[];
}

/**
 * 事件入口：由 appStore.handleBridgeEvent（window.onBridgeEvent 的唯一赋值点）转发
 * lsp_diagnostics / lsp_status —— 与 ide_file_changed 同一条既有 type-if 链，
 * 不再另开分发通道。
 */
export function dispatchLspEvent(name: string, payload: any): void {
  if (name === 'lsp_diagnostics') {
    const path = String(payload?.path ?? '');
    if (!path) return;
    const diagnostics = freezeDiagnostics((payload?.diagnostics ?? []) as LspDiagnostic[]);
    diagnosticsByPath.set(normKey(path), diagnostics);
    // 诊断是后端推送的，不等修完再等 300ms：立刻让 lint 重算，改动立刻可见
    const view = viewsByPath.get(normKey(path));
    if (view) forceLinting(view);
    diagnosticListeners.forEach((fn) => fn(path, diagnostics));
    return;
  }
  if (name === 'lsp_status') {
    const status = payload as LspServerStatus;
    if (!status || typeof status.server !== 'string') return;
    statusListeners.forEach((fn) => fn(status));
  }
}

/** 切换工作区 / 关闭标签页时清空本地状态，避免把旧工作区的诊断挂到新文件上。 */
export function resetClientState(): void {
  pendingChanges.forEach((entry) => clearTimeout(entry.timer));
  pendingChanges.clear();
  openDocs.clear();
  docPaths.clear();
  diagnosticsByPath.clear();
  // 按 (workspace, server) 的重启记账同样属于「上一个工作区的状态」：`openDocs`
  // 清了、这两个表还留着旧工作区的键时，旧键上一次迟到的 `running` 会在新工作区的
  // 文档上触发一次重启重发（跨工作区误发 didOpen，评审 B）。三条状态必须一起清。
  serverStateByKey.clear();
  reopenPending.clear();
  // 当前工作区也一起清：留着旧值会让「切换后新打开」的文档被打上旧工作区的归属，
  // 之后它自己的服务器重启时反而被过滤掉（宁可「归属未知」也不能记错归属）。
  // 生产入口 `ideStore.syncWorkspace()` 紧接着会用后端响应把它设回当前工作区。
  activeWorkspace = undefined;
}

// ---------------------------------------------------------------------------
// 位置换算：LSP 用「行 + 字符」定位，CodeMirror 用文档绝对偏移
// ---------------------------------------------------------------------------

export function lineOffsets(text: string): number[] {
  const offsets = [0];
  for (let i = 0; i < text.length; i += 1) {
    if (text.charCodeAt(i) === 10) offsets.push(i + 1);
  }
  return offsets;
}

export function positionToOffset(
  offsets: number[],
  line: number,
  character: number,
  docLength: number,
): number {
  if (line < 0 || line >= offsets.length) return docLength;
  return Math.min(docLength, offsets[line] + Math.max(0, character));
}

export function offsetToPosition(offsets: number[], offset: number): LspPosition {
  let lo = 0;
  let hi = offsets.length - 1;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (offsets[mid] <= offset) lo = mid;
    else hi = mid - 1;
  }
  return { line: lo, character: Math.max(0, offset - offsets[lo]) };
}

export function severityOf(severity?: number): 'hint' | 'info' | 'warning' | 'error' {
  switch (severity) {
    case 2:
      return 'warning';
    case 3:
      return 'info';
    case 4:
      return 'hint';
    default:
      return 'error';
  }
}

export function completionType(kind?: number): string {
  switch (kind) {
    case 2:
      return 'method';
    case 3:
      return 'function';
    case 4:
    case 7:
      return 'class';
    case 5:
    case 10:
      return 'property';
    case 6:
      return 'variable';
    case 8:
      return 'interface';
    case 9:
      return 'namespace';
    case 14:
      return 'keyword';
    default:
      return 'text';
  }
}

function documentationText(doc: LspCompletionItem['documentation']): string | undefined {
  if (!doc) return undefined;
  if (typeof doc === 'string') return doc;
  return doc.value;
}

export function toCompletions(items: LspCompletionItem[]): Completion[] {
  return items.map((item) => ({
    label: item.label,
    type: completionType(item.kind),
    detail: item.detail,
    info: documentationText(item.documentation),
    apply: item.insertText ?? item.label,
  }));
}

export function toDiagnostics(
  items: LspDiagnostic[],
  offsets: number[],
  docLength: number,
): Diagnostic[] {
  const out: Diagnostic[] = [];
  // 诊断来自跨进程的 JSON，range 缺失时跳过而不是让 lint 源抛异常
  // （lint 源抛错会把编辑器的诊断与输入链路一起打断）
  for (const item of items ?? []) {
    const range = item?.range;
    if (!range) continue;
    out.push({
      from: positionToOffset(offsets, range.start.line, range.start.character, docLength),
      to: positionToOffset(offsets, range.end.line, range.end.character, docLength),
      severity: severityOf(item.severity),
      message: item.message,
      source: item.source,
    });
  }
  return out;
}

export function lspDiagnosticsFor(view: EditorView, path: string): Diagnostic[] {
  const text = view.state.doc.toString();
  return toDiagnostics(diagnosticsFor(path), lineOffsets(text), text.length);
}

export function lspCompletionSource(
  path: string,
): (context: CompletionContext) => Promise<CompletionResult | null> {
  return async (context) => {
    const word = context.matchBefore(/[\w.$]+/);
    if (!word) return null;
    // 非显式触发时，光标前没有标识符字符就不要打扰语言服务器
    if (word.from === word.to && !context.explicit) return null;
    const text = context.state.doc.toString();
    const offsets = lineOffsets(text);
    const position = offsetToPosition(offsets, word.from);
    // lspRequest 内部已把所有失败（服务器缺失 / 越权 / 传输异常）归一成 null：
    // 这里拿不到结果就静默回落（返回 null，不弹任何提示，不打断输入）
    const result = await lspRequest<{ items?: LspCompletionItem[] }>(
      path,
      'textDocument/completion',
      {
        textDocument: { uri: fileUri(path) },
        position,
        context: { triggerKind: context.explicit ? 1 : 2 },
      },
    );
    const items = result?.items ?? [];
    if (!items.length) return null;
    return { from: word.from, options: toCompletions(items), validFor: /^[\w.$]*$/ };
  };
}

const viewsByPath = new Map<string, EditorView>();

function viewRegistry(path: string): Extension {
  const key = normKey(path);
  return ViewPlugin.define((view) => {
    viewsByPath.set(key, view);
    return {
      update() {
        /* 视图已登记，无需在每次更新时做额外工作 */
      },
      destroy() {
        if (viewsByPath.get(key) === view) viewsByPath.delete(key);
      },
    } as PluginValue;
  });
}

/**
 * 挂到编辑器上的 LSP 扩展：补全源、诊断 lint 源、视图注册。
 * 诊断到达时（dispatchLspEvent）会主动 forceLinting，不需要额外的轮询。
 */
export function lspExtensions(options: { path: string; languageId: string }): Extension[] {
  const { path } = options;
  return [
    autocompletion({ override: [lspCompletionSource(path)] }),
    linter((view) => lspDiagnosticsFor(view, path), { delay: 300 }),
    viewRegistry(path),
  ];
}
