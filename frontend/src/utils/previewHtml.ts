// markdown 文件的「右栏浏览器预览页」构建：把渲染器产出的正文包成一份自包含
// HTML（内联阅读样式），交内嵌浏览器按 file:// 打开。
//
// 为什么放在前端做：实测内嵌 WebView2 直接打开 file:// 的 .md 只显示源码文本；
// 而 markdown 渲染器是 TS 侧实现（与消息气泡共用同一套），因此由前端渲染好
// 再经桥落盘（core.web_preview），最后把那份 HTML 交给浏览器打开。

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

/** 文件所在目录（与路径分隔符无关）；无分隔符时返回空串 */
function dirOf(filePath: string): string {
  const index = Math.max(filePath.lastIndexOf('\\'), filePath.lastIndexOf('/'));
  return index > 0 ? filePath.slice(0, index) : '';
}

/**
 * 相对资源地址 → file:// 绝对 URI；已是绝对 URL（http/https/data/file/协议相对）
 * 或页内锚点的原样保留。这样 md 里 `![](img/a.png)` 这种相对图片才能在预览页里
 * 正常显示（预览页落在 ~/.ycode/exports/preview，与源文件不同目录）。
 */
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

/**
 * 重写正文里的图片/链接地址（DOMParser 不可用时原样返回，不影响主流程）。
 *
 * 两件事：
 * 1. 相对地址 → file:// 绝对地址（预览页落在导出目录，与源文件不同目录）；
 * 2. 渲染器在「没有媒体上下文」时会把 `![alt](a.png)` 降级成 `<a data-file-link>`
 *    （消息气泡里靠点击协议处理），预览页是普通网页、点了没反应——这里把指向
 *    图片的这类链接还原成 <img>（浏览器能直接显示本地图片），其余改成可点击的
 *    file:// 链接。
 */
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

/**
 * 构建 markdown 预览页 HTML：正文用与消息气泡同一个渲染器，样式内联，
 * 相对图片/链接重写为本地 file:// 地址。
 */
export function buildMarkdownPreviewHtml(
  markdownText: string,
  sourcePath: string,
  title = 'Markdown 预览',
): string {
  // Windows 文件多为 CRLF，\r 会让标题/代码块等行级语法失配（与弹窗预览同处理）
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
