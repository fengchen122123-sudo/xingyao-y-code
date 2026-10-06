import type { Extension } from '@codemirror/state';
import { EditorView } from '@codemirror/view';

/**
 * 从 GUI 主题的 CSS 变量构造编辑器主题。
 *
 * 变量名取自 frontend/src/style.css（深色在 :root，浅色在 body.light 覆盖），
 * 这样主题切换时编辑器自动跟随，不需要维护第二套配色。
 */
function readVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.body).getPropertyValue(name).trim();
  return value || fallback;
}

export function buildEditorTheme(isLight: boolean): Extension {
  const bg = readVar('--bg1', isLight ? '#ffffff' : '#20201e');
  const text = readVar('--text', isLight ? '#2c2c2a' : '#d9d9d4');
  const dim = readVar('--text-dim', isLight ? '#5c5c56' : '#a8a7a0');
  const faint = readVar('--text-faint', isLight ? '#7a7a73' : '#85847d');
  const border = readVar('--border', isLight ? '#e3e3de' : '#34332f');
  const accent = readVar('--accent', '#6a92d8');
  const accentSoft = readVar('--accent-soft', 'rgba(106,146,216,0.16)');

  return EditorView.theme(
    {
      '&': { backgroundColor: bg, color: text, height: '100%' },
      '.cm-content': { caretColor: accent },
      '.cm-cursor, .cm-dropCursor': { borderLeftColor: accent },
      '.cm-gutters': { backgroundColor: bg, color: faint, border: 'none', borderRight: `1px solid ${border}` },
      '.cm-activeLine': { backgroundColor: accentSoft },
      '.cm-activeLineGutter': { backgroundColor: accentSoft, color: text },
      '.cm-selectionBackground, &.cm-focused .cm-selectionBackground': { backgroundColor: accentSoft },
      '.cm-selectionMatch': { backgroundColor: accentSoft },
      '.cm-scroller': { fontFamily: 'Consolas, "Cascadia Mono", monospace', lineHeight: '1.5' },
      '.cm-placeholder': { color: dim },
      '.cm-tooltip': { backgroundColor: bg, border: `1px solid ${border}`, color: text },
      '.cm-panels': { backgroundColor: bg, color: text },
      '.cm-panels input, .cm-panels button': { backgroundColor: bg, color: text, border: `1px solid ${border}` },
    },
    { dark: !isLight },
  );
}
