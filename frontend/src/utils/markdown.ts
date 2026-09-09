// 极简 Markdown 渲染器（手写实现，无第三方依赖）
//
// 安全约定：本文件采用"先转义、后加标记"的管线。任何来自模型输出的原始文本
// 在生成 <strong>/<h2>/<a> 等真实标签之前，必须先经过 escapeHtml 转义。
// 这样即使模型输出包含 <img onerror=...> 之类的内容，也只会被当作纯文本显示，
// 不会被 v-html 当作可执行的 HTML/JS 处理。

import { isLocalFileLink } from './localPath';
import type { MediaContext, MediaItem } from '../types/media';

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// 代码块模式：renderMarkdown 提取与 findCodeBlockRanges 共用同一 pattern，
// 保证切分器与渲染器对「代码块范围」的判定完全一致。
const CODE_BLOCK_PATTERN = '```(\\w*)\\n([\\s\\S]*?)```';

/** 返回所有已闭合代码块的 [start, end) 字符范围。 */
export function findCodeBlockRanges(source: string): Array<[number, number]> {
  if (!source) return [];
  const ranges: Array<[number, number]> = [];
  const re = new RegExp(CODE_BLOCK_PATTERN, 'g');
  let match: RegExpExecArray | null;
  while ((match = re.exec(source)) !== null) {
    ranges.push([match.index, match.index + match[0].length]);
  }
  return ranges;
}

// 仅允许 http / https / mailto 协议的链接，其余一律拦截为 #，防止 javascript: 等注入
export function sanitizeHref(href: string): string {
  if (!href) return '#';
  const trimmed = href.trim();
  const lower = trimmed.toLowerCase();
  if (lower.startsWith('http://') || lower.startsWith('https://') || lower.startsWith('mailto:')) {
    return trimmed;
  }
  return '#';
}

// 本地文件路径链接：href 用 # 阻止默认跳转，真实路径放 data-file-link 属性，
// 由 MessageBubble 的点击委托捕获后走内嵌预览。
function fileLinkHtml(path: string): string {
  return `<a href="#" data-file-link="${escapeHtml(path)}" data-file-local="1">${escapeHtml(path)}</a>`;
}

// ---------------------------------------------------------------------------
// 媒体语法（图片/视频）：与 core/media_refs.py 保持同构，前后端 ordinal 对齐
// 图片 ![caption](path)；视频 [video: caption](path) / [视频：caption](path)。
// 组 1/2 = 图片 caption/path；组 3/4 = 视频 caption/path。
// ---------------------------------------------------------------------------
const MEDIA_SYNTAX_RE = /!\[([^\]]*)\]\(([^)\s]+)\)|\[(?:video|视频)[ \t]*[:：][ \t]*([^\]]*)\]\(([^)\s]+)\)/gi;

const REMOTE_LINK_RE = /^[A-Za-z][A-Za-z0-9+.-]*:\/\//;

// 本地媒体路径判定：与后端 media_refs._is_local_media_path 同规则。
// 远程 URL 不是媒体项、不占 ordinal（渲染为普通链接，防 SSRF）。
function isLocalMediaPath(path: string): boolean {
  const s = (path || '').trim();
  if (!s || /[\s<>"'`]/.test(s)) return false;
  if (s.toLowerCase().startsWith('file://')) return true;
  if (REMOTE_LINK_RE.test(s) || s.toLowerCase().startsWith('data:')) return false;
  return true;
}

// 复制原图按钮的内联 SVG（与 iconPaths.ts 的 copy/check 图标同源，lucide 风格；
// v-html 里不能用 Vue 组件，只能内联）。导出供 MessageBubble 点击反馈时切换。
export const COPY_ICON_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>';
export const CHECK_ICON_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';
// "另存为"按钮的下载图标（同上，v-html 内联）
export const SAVE_ICON_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/></svg>';

function mediaItemHtml(item: MediaItem, caption: string, ordinal: number): string {
  if (item.kind === 'error') {
    const label = item.error_label || '媒体不可用';
    const name = caption || item.source_name || '媒体';
    return `<span class="md-media-error">⚠ ${escapeHtml(name)}：${escapeHtml(label)}</span>`;
  }
  const src = item.url || '';
  if (!src) {
    const name = caption || item.source_name || '媒体';
    return `<span class="md-media-error">⚠ ${escapeHtml(name)}：媒体服务不可用</span>`;
  }
  if (item.kind === 'video') {
    return `<video class="md-video" controls preload="metadata" playsinline src="${escapeHtml(src)}"></video>`;
  }
  const alt = caption || item.caption || item.source_name || '图片';
  // 悬停浮现操作区：src 放 data 属性，点击经 MessageBubble 事件委托处理。
  // 两个按钮共用 .md-img-actions 容器（各自绝对定位会互相重叠）；
  // data-save-name 是另存为的默认文件名建议（后端会清理并补真实扩展名）。
  return `<span class="md-media-wrap">`
    + `<img class="md-img" src="${escapeHtml(src)}" alt="${escapeHtml(alt)}" data-media-ordinal="${ordinal}" loading="lazy">`
    + `<span class="md-img-actions">`
    + `<button class="md-img-copy" type="button" title="复制原图" aria-label="复制原图" data-copy-src="${escapeHtml(src)}">${COPY_ICON_SVG}<span class="md-img-copy-text">复制</span></button>`
    + `<button class="md-img-save" type="button" title="另存为" aria-label="另存为" data-save-src="${escapeHtml(src)}" data-save-name="${escapeHtml(alt)}">${SAVE_ICON_SVG}<span class="md-img-save-text">另存为</span></button>`
    + `</span>`
    + `</span>`;
}

// mermaid 块的渲染缓存 key：源码的 FNV-1a 哈希 + 长度。只用于缓存去重，
// 不是安全边界（源码在展示前仍会转义）。流式期间同一段源码的 key 保持
// 稳定，v-html 整体重建 DOM 后可凭 key 直接命中已渲染的 SVG。
export function mermaidCacheKey(code: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < code.length; i++) {
    hash ^= code.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return `${hash.toString(16)}-${code.length}`;
}

export function renderMarkdown(md: string, mediaCtx?: MediaContext): string {
  return renderMarkdownDetailed(md, mediaCtx).html;
}

export function renderMarkdownDetailed(
  md: string,
  mediaCtx?: MediaContext,
  startMediaOrdinal = 0,
): { html: string; mediaCount: number } {
  if (!md) return { html: '', mediaCount: 0 };
  let text = md;

  // 占位符用 Unicode 私有区字符包裹：早期版本用 ___CODE_BLOCK_0___ 这种
  // 下划线格式，会被后面的粗体正则 __(.*?)__ 改写成 <strong>…</strong>，
  // 导致还原 replace 找不到占位符，正文里直接漏出 _INLINE_CODE_0_ 残片。
  const PH = '\uE000';

  // 1. 保护代码块 ```code```（块内容单独转义后直接生成安全 HTML 片段）
  const codeBlocks: string[] = [];
  const fileLinks: string[] = [];
  const links: string[] = [];
  text = text.replace(new RegExp(CODE_BLOCK_PATTERN, 'g'), (_, lang, code) => {
    const idx = codeBlocks.length;
    // mermaid 块：renderMarkdown 保持纯同步——这里只产出带缓存 key 的占位
    // 容器 + 隐藏源码（均已转义），SVG 渲染由 mermaidRenderer 在 DOM 挂载后
    // 异步完成（与文件链接后置校验同一模式）。渲染失败时降级显示源码。
    if ((lang || '').toLowerCase() === 'mermaid') {
      const key = mermaidCacheKey(code);
      codeBlocks.push(
        `<div class="mermaid-block" data-mermaid-key="${escapeHtml(key)}" data-state="pending">`
        + '<div class="mermaid-head"><span class="mermaid-tag">mermaid</span>'
        + '<button class="mermaid-copy" type="button">复制源码</button></div>'
        + `<pre class="mermaid-src">${escapeHtml(code)}</pre>`
        + '</div>'
      );
      return `${PH}CB${idx}${PH}`;
    }
    const escaped = escapeHtml(code);
    codeBlocks.push(`<div class="code-block"><button class="code-copy" type="button">复制</button><pre><code class="language-${escapeHtml(lang || '')}">${escaped}</code></pre></div>`);
    return `${PH}CB${idx}${PH}`;
  });

  // 2. 行内代码：反引号内若是本地文件路径则渲染为可点击链接，否则保持 <code>
  // 关键约束：[^`\n] 禁止跨行匹配。行内代码语义上属于单行；若允许跨行，
  // 某行的孤立/空反引号（如上游占位符被清理后残留的 ``）会与数行之后的
  // 反引号错误配对，把中间的整张表格/列表吞进一个 <code> 里（2026-08-30 事故）。
  const inlineCodes: string[] = [];
  text = text.replace(/`([^`\n]+)`/g, (_, code) => {
    if (isLocalFileLink(code, { allowBareFilename: false })) {
      const idx = fileLinks.length;
      fileLinks.push(fileLinkHtml(code));
      return `${PH}FL${idx}${PH}`;
    }
    const idx = inlineCodes.length;
    inlineCodes.push(`<code>${escapeHtml(code)}</code>`);
    return `${PH}IC${idx}${PH}`;
  });

  // 2.5 媒体语法（图片/视频）：必须先于普通链接解析，否则 ![alt](path) 会命中
  // 第 3 步被渲染成蓝色链接（图片显示不出来一直就是这个根因）。按出现顺序维护
  // ordinal 与 mediaCtx.items 对齐；无清单（旧消息/迁移前）或 ordinal 越界时退回
  // 文件链接渲染。远程 URL 不是媒体项，原样返回走普通链接管线。
  const mediaItems: string[] = [];
  let mediaOrdinal = startMediaOrdinal;
  text = text.replace(MEDIA_SYNTAX_RE, (_m, imgCaption, imgPath, vidCaption, vidPath) => {
    const caption = String(imgCaption ?? vidCaption ?? '').trim();
    const path = String(imgPath ?? vidPath ?? '').trim();
    if (!isLocalMediaPath(path)) return _m;
    const ordinal = mediaOrdinal++;
    const item = mediaCtx?.items[ordinal];
    if (!item) {
      const idx = fileLinks.length;
      fileLinks.push(fileLinkHtml(path));
      return `${PH}FL${idx}${PH}`;
    }
    const idx = mediaItems.length;
    mediaItems.push(mediaItemHtml(item, caption, ordinal));
    return `${PH}MD${idx}${PH}`;
  });

  // 3. 显式链接 [label](url)：本地文件路径 → 可点击预览；否则走 http/https/mailto 白名单
  text = text.replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, (_m, label, url) => {
    const idx = links.length;
    if (isLocalFileLink(url)) {
      links.push(`<a href="#" data-file-link="${escapeHtml(url)}" data-file-local="1">${escapeHtml(label)}</a>`);
    } else {
      const safeHref = sanitizeHref(url);
      links.push(`<a href="${safeHref}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`);
    }
    return `${PH}L${idx}${PH}`;
  });

  // 4. 裸路径识别：此时代码块/行内代码/显式链接均已占位，只在剩余纯文本上匹配，
  //    命中后用 isLocalFileLink 二次过滤（正则尾段会匹配到 1.2 / foo.bar 等，靠白名单排除）
  // 排除字符含中文标点：中文语境下模型常在路径后紧跟「，。；」等，
  // 不排除会被贪婪匹配吞进路径，导致拼出的磁盘路径不存在
  const LOCAL_PATH_RE = /(?:file:\/\/[^\s<>\"'`()，。；：、！？（）【】《》“”‘…—·]+|[A-Za-z]:[\\/][^\s<>\"'`()，。；：、！？（）【】《》“”‘…—·]+|\.{1,2}[\\/][^\s<>\"'`()，。；：、！？（）【】《》“”‘…—·]+|[^\s<>\"'`()，。；：、！？（）【】《》“”‘…—·]+[\\/][^\s<>\"'`()，。；：、！？（）【】《》“”‘…—·]*\.[A-Za-z0-9]+|[A-Za-z0-9_\-]+\.[A-Za-z0-9]+)/g;
  text = text.replace(LOCAL_PATH_RE, (m) => {
    if (!isLocalFileLink(m)) return m;
    const idx = fileLinks.length;
    fileLinks.push(fileLinkHtml(m));
    return `${PH}FL${idx}${PH}`;
  });

  // 5. 关键修复：转义剩余的全部文本，再叠加标记。
  //    此步之后，文本中原有的 <, >, &, ", ' 均已变为安全实体，
  //    后续所有正则只会在“已转义”的文本上叠加真正的标签。
  text = escapeHtml(text);

  // 6. 标题（# / ## / ###，井号不会被转义，故可在转义后继续匹配）
  text = text.replace(/^### (.*)$/gim, '<h4>$1</h4>');
  text = text.replace(/^## (.*)$/gim, '<h3>$1</h3>');
  text = text.replace(/^# (.*)$/gim, '<h2>$1</h2>');

  // 6.5 分割线 --- 或 ***
  text = text.replace(/^(?:---|\*\*\*)$/gim, '<hr>');

  // 6. 粗体、斜体
  text = text.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/__(.*?)__/g, '<strong>$1</strong>');
  text = text.replace(/\*(.*?)\*/g, '<em>$1</em>');

  // 7. 逐行处理表格 / 列表 / 引用块 / 段落
  const lines = text.split('\n');
  type Mode = 'ul' | 'ol' | 'bq' | null;
  let mode: Mode = null;
  const resultLines: string[] = [];

  const closeMode = () => {
    if (mode === 'ul') resultLines.push('</ul>');
    else if (mode === 'ol') resultLines.push('</ol>');
    else if (mode === 'bq') resultLines.push('</blockquote>');
    mode = null;
  };

  // GFM 表格：行以 | 开始结束；第二行是分隔行 |---|---|（允许 :---: 对齐标记）
  const isTableRow = (l: string) => {
    const t = l.trim();
    return t.length > 1 && t.startsWith('|') && t.endsWith('|');
  };
  const isTableSep = (l: string) => {
    const t = l.trim();
    return isTableRow(t) && /^\|[\s:|-]+\|$/.test(t) && t.includes('-');
  };
  const splitRow = (l: string): string[] => {
    const t = l.trim();
    return t.slice(1, t.length - 1).split('|').map(c => c.trim());
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // 表格块：表头行 + 分隔行 + 若干数据行
    if (isTableRow(line) && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      closeMode();
      const headerCells = splitRow(line);
      i++; // 跳过分隔行
      const bodyRows: string[][] = [];
      while (i + 1 < lines.length && isTableRow(lines[i + 1])) {
        i++;
        bodyRows.push(splitRow(lines[i]));
      }
      const thead = `<thead><tr>${headerCells.map(c => `<th>${c}</th>`).join('')}</tr></thead>`;
      const tbody = bodyRows.length
        ? `<tbody>${bodyRows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody>`
        : '';
      resultLines.push(`<table>${thead}${tbody}</table>`);
      continue;
    }

    const ulMatch = line.match(/^(\s*)([-*+])\s+(.*)$/);
    const olMatch = line.match(/^(\s*)(\d+\.)\s+(.*)$/);
    // 引用块 "> xxx" 转义后变为 "&gt; xxx"，因此匹配转义后的形式
    const bqMatch = line.match(/^&gt;\s?(.*)$/);

    if (ulMatch) {
      if (mode !== 'ul') { closeMode(); resultLines.push('<ul>'); mode = 'ul'; }
      resultLines.push(`<li>${ulMatch[3]}</li>`);
    } else if (olMatch) {
      if (mode !== 'ol') {
        closeMode();
        const num = parseInt(olMatch[2], 10);
        resultLines.push(`<ol start="${num}">`);
        mode = 'ol';
      }
      resultLines.push(`<li>${olMatch[3]}</li>`);
    } else if (bqMatch) {
      if (mode !== 'bq') { closeMode(); resultLines.push('<blockquote>'); mode = 'bq'; }
      resultLines.push(`${bqMatch[1]}<br>`);
    } else {
      closeMode();
      const trimmed = line.trim();
      if (trimmed === '') {
        // 空行忽略
      } else if (!trimmed.startsWith('<h') && !trimmed.startsWith(`${PH}CB`)) {
        resultLines.push(`<p>${line}</p>`);
      } else {
        resultLines.push(line);
      }
    }
  }
  closeMode();

  let html = resultLines.join('');

  // 还原代码块与行内代码（已在提取时单独转义，安全）。
  // 注意：replace 的第二参数必须用函数形式——字符串形式会把代码内容里的
  // $&、$'、$` 等序列当作特殊替换模式展开，导致含 $ 的代码渲染错乱。
  codeBlocks.forEach((cb, idx) => {
    html = html.replace(`${PH}CB${idx}${PH}`, () => cb);
  });
  inlineCodes.forEach((ic, idx) => {
    html = html.replace(`${PH}IC${idx}${PH}`, () => ic);
  });
  fileLinks.forEach((fl, idx) => {
    html = html.replace(`${PH}FL${idx}${PH}`, () => fl);
  });
  links.forEach((l, idx) => {
    html = html.replace(`${PH}L${idx}${PH}`, () => l);
  });
  mediaItems.forEach((mi, idx) => {
    html = html.replace(`${PH}MD${idx}${PH}`, () => mi);
  });

  return { html, mediaCount: mediaOrdinal - startMediaOrdinal };
}

// 权限模式：规范值(canonical) -> 中文展示文案。store 内一律保存规范值，
// 所有展示位置都必须经过本函数转换，不允许直接展示规范值或旧文案。
export function formatPermMode(mode: string): string {
  switch (mode) {
    case 'confirm': return '变更前确认';
    case 'accept_edits': return '自动编辑';
    case 'plan': return '计划模式';
    case 'full': return '完全访问';
    default: return mode || '变更前确认';
  }
}
