
import { renderMarkdown } from './markdown';
import { toAbsoluteLocalPath, toFileUrl } from './localPath';

const PREVIEW_CSS = `
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 20px 26px 64px;
  background: #ffffff; color: #1f2328;
  font-family: "Segoe UI", "Microsoft YaHei", system-ui, -apple-system, sans-serif;
  font-size: 14.5px; line-height: 1.75;
}
.md-preview { max-width: 880px; margin: 0 auto; }
.md-preview-head {
  max-width: 880px; margin: 0 auto 18px; padding-bottom: 10px;
  border-bottom: 1px solid #e4e7eb; display: flex; gap: 10px;
  align-items: baseline; flex-wrap: wrap;
}
.md-preview-head .file { font-size: 13px; font-weight: 600; color: #424a53; }
.md-preview-head .hint { font-size: 11.5px; color: #8c959f; }
.md-preview h1, .md-preview h2, .md-preview h3, .md-preview h4 {
  line-height: 1.35; margin: 1.5em 0 .6em; font-weight: 650;
}
.md-preview h1 { font-size: 1.7em; border-bottom: 1px solid #e4e7eb; padding-bottom: .3em; }
.md-preview h2 { font-size: 1.4em; border-bottom: 1px solid #eceff2; padding-bottom: .25em; }
.md-preview h3 { font-size: 1.18em; }
.md-preview h4 { font-size: 1.03em; }
.md-preview p { margin: .8em 0; }
.md-preview a { color: #0969da; text-decoration: none; }
.md-preview a:hover { text-decoration: underline; }
.md-preview ul, .md-preview ol { margin: .8em 0; padding-left: 1.7em; }
.md-preview li { margin: .28em 0; }
.md-preview blockquote {
  margin: .9em 0; padding: .2em 1em; color: #59636e;
  border-left: 3px solid #d0d7de; background: #f6f8fa;
}
.md-preview code {
  font-family: Consolas, "Cascadia Code", "Courier New", monospace;
  font-size: .9em; background: #f0f2f4; border-radius: 5px; padding: .12em .35em;
}
.md-preview pre {
  margin: .9em 0; padding: 12px 14px; overflow-x: auto;
  background: #f6f8fa; border: 1px solid #e4e7eb; border-radius: 8px;
}
.md-preview pre code { background: none; padding: 0; font-size: 12.8px; line-height: 1.6; }
.md-preview table { border-collapse: collapse; margin: .9em 0; width: 100%; font-size: .95em; }
.md-preview th, .md-preview td { border: 1px solid #dfe3e8; padding: 6px 10px; text-align: left; }
.md-preview th { background: #f6f8fa; font-weight: 600; }
.md-preview img { max-width: 100%; height: auto; border-radius: 6px; }
.md-preview hr { border: none; border-top: 1px solid #e4e7eb; margin: 1.6em 0; }
/* mermaid 在纯静态页里没有渲染器：源码按等宽兜底展示，不静默留白 */
.md-preview pre.mermaid, .md-preview .mermaid-src {
  white-space: pre-wrap; word-break: break-word;
  font-family: Consolas, "Cascadia Code", "Courier New", monospace; font-size: 12.5px;
}
@media (prefers-color-scheme: dark) {
  body { background: #0d1117; color: #e6edf3; }
  .md-preview-head { border-bottom-color: #21262d; }
  .md-preview-head .file { color: #c9d1d9; }
  .md-preview h1, .md-preview h2 { border-bottom-color: #21262d; }
  .md-preview blockquote { background: #161b22; border-left-color: #30363d; color: #9198a1; }
  .md-preview code { background: #161b22; }
  .md-preview pre { background: #161b22; border-color: #30363d; }
  .md-preview th { background: #161b22; }
  .md-preview th, .md-preview td { border-color: #30363d; }
  .md-preview a { color: #4493f8; }
  .md-preview hr { border-top-color: #21262d; }
}
`;

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function dirOf(filePath: string): string {
  const index = Math.max(filePath.lastIndexOf('\\'), filePath.lastIndexOf('/'));
  return index > 0 ? filePath.slice(0, index) : '';
}

export function absolutizeResourceUrl(raw: string, baseDir: string): string {
  const value = (raw || '').trim();
  if (!value) return raw;
  if (value.startsWith('#') || value.startsWith('//') || /^[a-z][a-z0-9+.-]*:/i.test(value)) {
    return raw;
  }
  const absolute = toAbsoluteLocalPath(value, baseDir);
  if (!absolute) return raw;
  return toFileUrl(absolute) || raw;
}

const IMAGE_EXTENSIONS = new Set([
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico', 'avif',
]);

function isImagePath(path: string): boolean {
  const name = (path || '').trim().replace(/[?#].*$/, '');
  const dot = name.lastIndexOf('.');
  return dot >= 0 && IMAGE_EXTENSIONS.has(name.slice(dot + 1).toLowerCase());
}

function baseName(path: string): string {
  const index = Math.max(path.lastIndexOf('\\'), path.lastIndexOf('/'));
  return index >= 0 ? path.slice(index + 1) : path;
}

function rewriteLocalResources(html: string, baseDir: string): string {
  if (typeof DOMParser === 'undefined') return html;
  try {
    const doc = new DOMParser().parseFromString(`<div id="preview-root">${html}</div>`, 'text/html');
    const root = doc.getElementById('preview-root');
    if (!root) return html;
    root.querySelectorAll('img[src]').forEach((el) => {
      el.setAttribute('src', absolutizeResourceUrl(el.getAttribute('src') || '', baseDir));
    });
    root.querySelectorAll('a[data-file-link]').forEach((el) => {
      const raw = el.getAttribute('data-file-link') || '';
      const url = absolutizeResourceUrl(raw, baseDir);
      if (isImagePath(raw)) {
        const img = doc.createElement('img');
        img.setAttribute('src', url);
        img.setAttribute('alt', baseName(raw));
        el.replaceWith(img);
        return;
      }
      el.setAttribute('href', url);
      el.removeAttribute('data-file-link');
      el.removeAttribute('data-file-local');
    });
    root.querySelectorAll('a[href]').forEach((el) => {
      el.setAttribute('href', absolutizeResourceUrl(el.getAttribute('href') || '', baseDir));
    });
    return root.innerHTML;
  } catch {
    return html;
  }
}

export function buildMarkdownPreviewHtml(
  markdownText: string,
  sourcePath: string,
  title = 'Markdown 预览',
): string {
  const normalized = String(markdownText || '').replace(/\r\n/g, '\n');
  const baseDir = dirOf(sourcePath);
  const body = rewriteLocalResources(renderMarkdown(normalized), baseDir);
  const safeTitle = escapeHtml(title);
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${safeTitle}</title>
<style>${PREVIEW_CSS}</style>
</head>
<body>
<header class="md-preview-head">
  <span class="file">${safeTitle}</span>
  <span class="hint">由 星瑶Code 本地渲染 · 不经过网络</span>
</header>
<article class="md-preview">${body}</article>
</body>
</html>
`;
}
