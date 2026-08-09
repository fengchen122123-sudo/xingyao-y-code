// 极简 Markdown 渲染器（手写实现，无第三方依赖）
//
// 安全约定：本文件采用"先转义、后加标记"的管线。任何来自模型输出的原始文本
// 在生成 <strong>/<h2>/<a> 等真实标签之前，必须先经过 escapeHtml 转义。
// 这样即使模型输出包含 <img onerror=...> 之类的内容，也只会被当作纯文本显示，
// 不会被 v-html 当作可执行的 HTML/JS 处理。

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
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

export function renderMarkdown(md: string): string {
  if (!md) return '';
  let text = md;

  // 占位符用 Unicode 私有区字符包裹：早期版本用 ___CODE_BLOCK_0___ 这种
  // 下划线格式，会被后面的粗体正则 __(.*?)__ 改写成 <strong>…</strong>，
  // 导致还原 replace 找不到占位符，正文里直接漏出 _INLINE_CODE_0_ 残片。
  const PH = '\uE000';

  // 1. 保护代码块 ```code```（块内容单独转义后直接生成安全 HTML 片段）
  const codeBlocks: string[] = [];
  text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const idx = codeBlocks.length;
    const escaped = escapeHtml(code);
    codeBlocks.push(`<pre><code class="language-${escapeHtml(lang || '')}">${escaped}</code></pre>`);
    return `${PH}CB${idx}${PH}`;
  });

  // 2. 保护行内代码 `code`
  const inlineCodes: string[] = [];
  text = text.replace(/`([^`]+)`/g, (_, code) => {
    const idx = inlineCodes.length;
    const escaped = escapeHtml(code);
    inlineCodes.push(`<code>${escaped}</code>`);
    return `${PH}IC${idx}${PH}`;
  });

  // 3. 关键修复：转义剩余的全部文本，再叠加标记。
  //    此步之后，文本中原有的 <, >, &, ", ' 均已变为安全实体，
  //    后续所有正则只会在“已转义”的文本上叠加真正的标签。
  text = escapeHtml(text);

  // 4. 标题（# / ## / ###，井号不会被转义，故可在转义后继续匹配）
  text = text.replace(/^### (.*)$/gim, '<h4>$1</h4>');
  text = text.replace(/^## (.*)$/gim, '<h3>$1</h3>');
  text = text.replace(/^# (.*)$/gim, '<h2>$1</h2>');

  // 4.5 分割线 --- 或 ***
  text = text.replace(/^(?:---|\*\*\*)$/gim, '<hr>');

  // 5. 链接 [text](url) —— href 经 sanitizeHref 白名单过滤
  text = text.replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, (_m, label, url) => {
    const safeHref = sanitizeHref(url);
    return `<a href="${safeHref}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });

  // 6. 粗体、斜体
  text = text.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/__(.*?)__/g, '<strong>$1</strong>');
  text = text.replace(/\*(.*?)\*/g, '<em>$1</em>');

  // 7. 逐行处理列表 / 引用块 / 段落
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

  lines.forEach(line => {
    const ulMatch = line.match(/^(\s*)([-*+])\s+(.*)$/);
    const olMatch = line.match(/^(\s*)(\d+\.)\s+(.*)$/);
    // 引用块 "> xxx" 转义后变为 "&gt; xxx"，因此匹配转义后的形式
    const bqMatch = line.match(/^&gt;\s?(.*)$/);

    if (ulMatch) {
      if (mode !== 'ul') { closeMode(); resultLines.push('<ul>'); mode = 'ul'; }
      resultLines.push(`<li>${ulMatch[3]}</li>`);
    } else if (olMatch) {
      if (mode !== 'ol') { closeMode(); resultLines.push('<ol>'); mode = 'ol'; }
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
  });
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

  return html;
}

// 权限模式：规范值(canonical) -> 中文展示文案。store 内一律保存规范值，
// 所有展示位置都必须经过本函数转换，不允许直接展示规范值或旧文案。
export function formatPermMode(mode: string): string {
  switch (mode) {
    case 'confirm': return '变更前确认';
    case 'auto': return '自动接受编辑';
    case 'plan': return '计划模式';
    case 'full': return '完全访问';
    default: return mode || '变更前确认';
  }
}
