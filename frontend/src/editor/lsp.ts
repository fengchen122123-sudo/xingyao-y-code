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

export interface LspServerStatus {
  server: string;
  language: string;
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

const openDocs = new Map<string, { languageId: string; version: number }>();
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

export async function openDocument(path: string, languageId: string, text: string): Promise<void> {
  const api = bridgeApi();
  if (!api) return;
  if (openDocs.has(normKey(path))) {
    changeDocument(path, text);
    return;
  }
  openDocs.set(normKey(path), { languageId, version: 1 });
  docPaths.set(normKey(path), path);
  await api.lsp_notify(path, 'textDocument/didOpen', {
    textDocument: { uri: fileUri(path), languageId, version: 1, text },
  });
}

export function changeDocument(path: string, text: string): void {
  const key = normKey(path);
  if (!openDocs.has(key)) return;
  const existing = pendingChanges.get(key);
  if (existing) clearTimeout(existing.timer);
  const timer = setTimeout(() => {
    void flushChange(key);
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
  await api.lsp_notify(pendingPath(key), 'textDocument/didChange', {
    textDocument: { uri: fileUri(pendingPath(key)), version: doc.version },
    contentChanges: [{ text: pending.text }],
  });
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
  openDocs.delete(key);
  docPaths.delete(key);
  diagnosticsByPath.delete(key);
  const api = bridgeApi();
  if (!api) return;
  await api.lsp_notify(path, 'textDocument/didClose', {
    textDocument: { uri: fileUri(path) },
  });
}

export async function lspRequest<T = unknown>(
  path: string,
  method: string,
  params: Record<string, unknown>,
): Promise<T | null> {
  const api = bridgeApi();
  if (!api) return null;
  const res = await api.lsp_request(path, method, params);
  if (!res.ok) return null;
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

/** 事件入口：由 App.vue 的桥事件分发处转发 lsp_diagnostics / lsp_status。 */
export function dispatchLspEvent(name: string, payload: any): void {
  if (name === 'lsp_diagnostics') {
    const path = String(payload?.path ?? '');
    if (!path) return;
    const diagnostics = (payload?.diagnostics ?? []) as LspDiagnostic[];
    diagnosticsByPath.set(normKey(path), diagnostics);
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
}
