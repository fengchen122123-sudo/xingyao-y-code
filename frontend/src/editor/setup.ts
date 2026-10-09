import { EditorState, type Extension } from '@codemirror/state';
import { EditorView, lineNumbers, highlightActiveLine, highlightActiveLineGutter } from '@codemirror/view';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { searchKeymap, highlightSelectionMatches } from '@codemirror/search';
import { bracketMatching, indentOnInput, syntaxHighlighting, defaultHighlightStyle } from '@codemirror/language';
import { keymap } from '@codemirror/view';

/**
 * 语言扩展按需动态加载：与 RightPanel.vue 里 highlight.js 的懒加载同一思路，
 * 语言包不进首屏 chunk。
 */
export const LANGUAGE_LOADERS: Record<string, () => Promise<Extension>> = {
  python: () => import('@codemirror/lang-python').then((m) => m.python()),
  javascript: () => import('@codemirror/lang-javascript').then((m) => m.javascript()),
  typescript: () => import('@codemirror/lang-javascript').then((m) => m.javascript({ typescript: true })),
  json: () => import('@codemirror/lang-json').then((m) => m.json()),
  markdown: () => import('@codemirror/lang-markdown').then((m) => m.markdown()),
  html: () => import('@codemirror/lang-html').then((m) => m.html()),
  css: () => import('@codemirror/lang-css').then((m) => m.css()),
};

/** 扩展名到语言 id 的映射；未命中的语言一律走纯文本。 */
const EXT_LANGUAGE: Record<string, string> = {
  py: 'python',
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  jsx: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  json: 'json',
  md: 'markdown',
  html: 'html',
  htm: 'html',
  css: 'css',
};

export function languageIdForPath(path: string): string {
  const name = path.split(/[\\/]/).pop() ?? '';
  const dot = name.lastIndexOf('.');
  if (dot < 0) return '';
  return EXT_LANGUAGE[name.slice(dot + 1).toLowerCase()] ?? '';
}

export function baseExtensions(): Extension[] {
  return [
    lineNumbers(),
    highlightActiveLine(),
    highlightActiveLineGutter(),
    history(),
    bracketMatching(),
    indentOnInput(),
    highlightSelectionMatches(),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    keymap.of([...defaultKeymap, ...historyKeymap, ...searchKeymap, indentWithTab]),
  ];
}

/** 语言加载失败时静默回落到纯文本：编辑器可用性优先于高亮。 */
export async function languageExtension(languageId: string): Promise<Extension[]> {
  const loader = LANGUAGE_LOADERS[languageId];
  if (!loader) return [];
  try {
    return [await loader()];
  } catch {
    return [];
  }
}

/** extraExtensions 用于注入 LSP 等按文件实例化的扩展（补全、lint、悬停）。 */
export async function createEditorState(
  doc: string,
  languageId: string,
  extraExtensions: Extension[] = [],
): Promise<EditorState> {
  return EditorState.create({
    doc,
    extensions: [...baseExtensions(), ...(await languageExtension(languageId)), ...extraExtensions],
  });
}

export { EditorView };
