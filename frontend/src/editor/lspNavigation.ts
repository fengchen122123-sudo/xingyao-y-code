// 悬停 / 跳转定义 / 格式化 / 重命名
//
// 与 `lsp.ts` 里的补全与诊断扩展（`lspExtensions`）的分工：那边管「编辑时」的
// 被动能力，本模块管「按需触发」的导航类能力。两者都复用 `./lsp` 的客户端，
// 不另开桥调用通道；桥调用失败一律静默回落（`lspRequest` 内部已吞掉传输异常）。
import { hoverTooltip, keymap } from '@codemirror/view';
import type { Extension } from '@codemirror/state';

import {
  fileUri,
  lineOffsets,
  lspRequest,
  normKey,
  offsetToPosition,
  positionToOffset,
  uriToPath,
  type LspLocation,
} from './lsp';

export function toHoverText(hover: unknown): string {
  if (!hover || typeof hover !== 'object') return '';
  const contents = (hover as { contents?: unknown }).contents;
  if (typeof contents === 'string') return contents;
  if (Array.isArray(contents)) {
    return contents
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object' && typeof (item as { value?: unknown }).value === 'string') {
          return (item as { value: string }).value;
        }
        return '';
      })
      .filter((item) => item !== '')
      .join('\n\n');
  }
  if (contents && typeof contents === 'object') {
    const value = (contents as { value?: unknown }).value;
    if (typeof value === 'string') return value;
  }
  return '';
}

export function buildTooltipDom(text: string): HTMLElement {
  const dom = document.createElement('div');
  dom.className = 'ycode-lsp-hover';
  dom.textContent = text;
  return dom;
}

export function toLocations(result: unknown): LspLocation[] {
  if (!result) return [];
  const raw = Array.isArray(result) ? result : [result];
  const locations: LspLocation[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const row = item as Record<string, any>;
    if (row.targetUri) {
      locations.push({
        path: uriToPath(String(row.targetUri)),
        line: row.targetRange?.start?.line ?? 0,
        character: row.targetRange?.start?.character ?? 0,
      });
      continue;
    }
    const uri = row.uri ?? row.location?.uri;
    const range = row.range ?? row.location?.range;
    if (!uri || !range) continue;
    locations.push({
      path: uriToPath(String(uri)),
      line: range.start?.line ?? 0,
      character: range.start?.character ?? 0,
    });
  }
  // 非 file:// 的 URI（http 等）经 uriToPath 变成空串，必须剔除，
  // 否则会被当成「本地文件」去打开一个根本不存在的路径。
  return locations.filter((location) => location.path !== '');
}

/**
 * LSP 的一个位置点是否可用：必须是带整数 line / character 的对象。
 *
 * 只接受整数，是为了让 `{ line: '0' }`、`NaN` 这类「结构不合法」的畸形位置
 * 走丢弃分支，而不是被 `?? 0` 兜成 (0,0)。
 */
function isPosition(value: any): value is { line: number; character: number } {
  return (
    !!value &&
    typeof value === 'object' &&
    Number.isInteger(value.line) &&
    Number.isInteger(value.character) &&
    value.line >= 0 &&
    value.character >= 0
  );
}

/** TextEdit.range 在 LSP 里是必填字段；缺失或结构不合法一律返回 null（丢弃该条编辑）。 */
function validRange(range: any): { start: { line: number; character: number }; end: { line: number; character: number } } | null {
  return isPosition(range?.start) && isPosition(range?.end) ? range : null;
}

export function toTextEdits(
  edits: unknown[],
  offsets: number[],
  docLength: number,
): { from: number; to: number; insert: string }[] {
  if (!Array.isArray(edits)) return [];
  const changes: { from: number; to: number; insert: string }[] = [];
  for (const raw of edits) {
    const edit = (raw ?? {}) as Record<string, any>;
    // 安全过滤一：缺 range / range 结构不合法 ⇒ 整体放弃这一条。
    // 绝不能用 `edit.range?.start?.line ?? 0` 兜底——那会把「没有范围」退化成
    // 位于 (0,0) 的零宽范围，它能通过下面的 `from <= to` 检查，于是变成
    // 「在文档开头插入 newText」，真的改坏用户文档。
    const range = validRange(edit.range);
    if (!range) continue;
    const change = {
      from: positionToOffset(offsets, range.start.line, range.start.character, docLength),
      to: positionToOffset(offsets, range.end.line, range.end.character, docLength),
      insert: String(edit.newText ?? ''),
    };
    // 安全过滤二：CodeMirror 对「起点在终点之后」的变更范围会直接抛错。
    // 语言服务器返回畸形 range 时宁可整体放弃这次编辑，也不能让文档半途改坏。
    if (change.from > change.to) continue;
    changes.push(change);
  }
  return changes;
}

/**
 * URI 是否指向本地 `path`：一律先经 `uriToPath` 归一到本地路径再比（与 `toLocations`
 * 同一口径）。服务器回传的 URI 在盘符大小写、百分号编码、斜杠方向上可能与本地
 * `fileUri(path)` 不完全一致，精确串匹配会静默漏掉（评审 M-2）。
 */
function sameLocalPath(uri: unknown, path: string): boolean {
  const cleaned = uriToPath(String(uri ?? ''));
  return cleaned !== '' && normKey(cleaned) === normKey(path);
}

/**
 * 从 `textDocument/rename` 的返回里取出「当前文件」的编辑。
 *
 * 两条路都要走（评审 I-2）：
 * - `documentChanges`：LSP 3.x 的推荐形态（`TextDocumentEdit`）。部分服务器
 *   （jdtls / rust-analyzer 等）优先回它，此时 `changes` 为空 —— 只读 `changes`
 *   会让 F2 静默无效果。
 * - `changes`：按 URI 分组的旧形态。
 *
 * 跨文件重命名只取当前文件的那一份；其余文件的编辑不在这里落地（避免静默改写
 * 未打开的磁盘文件）。
 */
export function toRenameEdits(result: unknown, path: string): unknown[] {
  if (!result || typeof result !== 'object') return [];
  const row = result as Record<string, any>;
  if (Array.isArray(row.documentChanges)) {
    for (const change of row.documentChanges) {
      if (!change || typeof change !== 'object') continue;
      const docUri = (change as Record<string, any>).textDocument?.uri;
      if (!sameLocalPath(docUri, path)) continue;
      const edits = (change as Record<string, any>).edits;
      if (Array.isArray(edits)) return edits;
    }
  }
  if (row.changes && typeof row.changes === 'object') {
    for (const [uri, edits] of Object.entries(row.changes as Record<string, unknown>)) {
      if (Array.isArray(edits) && sameLocalPath(uri, path)) return edits;
    }
  }
  return [];
}

export function lspHover(path: string): Extension {
  return hoverTooltip(async (view, pos) => {
    const text = view.state.doc.toString();
    const position = offsetToPosition(lineOffsets(text), pos);
    const hover = await lspRequest(path, 'textDocument/hover', {
      textDocument: { uri: fileUri(path) },
      position,
    });
    const hoverText = toHoverText(hover);
    if (!hoverText) return null;
    return {
      pos,
      create: () => ({ dom: buildTooltipDom(hoverText) }),
    };
  });
}

export function lspDefinitionKeymap(
  path: string,
  onJump: (locations: LspLocation[]) => void,
): Extension {
  return keymap.of([
    {
      key: 'F12',
      run: (view) => {
        void (async () => {
          const text = view.state.doc.toString();
          const cursor = view.state.selection.main.head;
          const result = await lspRequest(path, 'textDocument/definition', {
            textDocument: { uri: fileUri(path) },
            position: offsetToPosition(lineOffsets(text), cursor),
          });
          const locations = toLocations(result);
          if (locations.length) onJump(locations);
        })().catch(() => {
          /* 导航请求是尽力而为的：异步体内的任何异常（畸形输入 / onJump 回调）
             都不允许变成 unhandled rejection（评审 M-3）。 */
        });
        return true;
      },
    },
  ]);
}

export function lspFormatKeymap(path: string): Extension {
  return keymap.of([
    {
      key: 'Shift-Alt-f',
      run: (view) => {
        void (async () => {
          const text = view.state.doc.toString();
          const result = await lspRequest<unknown[]>(path, 'textDocument/formatting', {
            textDocument: { uri: fileUri(path) },
            options: { tabSize: 4, insertSpaces: true },
          });
          const changes = toTextEdits(result ?? [], lineOffsets(text), text.length);
          if (changes.length) view.dispatch({ changes });
        })().catch(() => {
          /* CodeMirror 对畸形编辑范围（如重叠变更）会抛错：文档因 dispatch 的原子性
             保持不变，但绝不留未处理拒绝（评审 M-3）。 */
        });
        return true;
      },
    },
  ]);
}

export function lspRenameKeymap(
  path: string,
  requestNewName: (oldName: string) => string | null,
): Extension {
  return keymap.of([
    {
      key: 'F2',
      run: (view) => {
        const text = view.state.doc.toString();
        const offsets = lineOffsets(text);
        const word = view.state.wordAt(view.state.selection.main.head);
        if (!word) return false;
        const oldName = text.slice(word.from, word.to);
        const newName = requestNewName(oldName);
        if (!newName) return true;
        void (async () => {
          const edit = await lspRequest<unknown>(path, 'textDocument/rename', {
            textDocument: { uri: fileUri(path) },
            position: offsetToPosition(offsets, word.from),
            newName,
          });
          // 同时支持 documentChanges（LSP 3.x）与 changes（旧形态），URI 经归一后比对
          const rawEdits = toRenameEdits(edit, path);
          const mapped = toTextEdits(rawEdits, offsets, text.length);
          if (mapped.length) view.dispatch({ changes: mapped });
          // 跨文件重命名只改当前文件；其余文件的编辑由用户手动保存后重新触发，
          // 避免在前端静默改写未打开的磁盘文件
        })().catch(() => {
          /* 畸形的重命名编辑让 dispatch 抛错时，文档因原子性保持不变，
             但绝不留未处理拒绝（评审 M-3）。 */
        });
        return true;
      },
    },
  ]);
}

export function lspNavigationExtensions(options: {
  path: string;
  onJump: (locations: LspLocation[]) => void;
  requestNewName: (oldName: string) => string | null;
}): Extension[] {
  const { path, onJump, requestNewName } = options;
  return [
    lspHover(path),
    lspDefinitionKeymap(path, onJump),
    lspFormatKeymap(path),
    lspRenameKeymap(path, requestNewName),
  ];
}
