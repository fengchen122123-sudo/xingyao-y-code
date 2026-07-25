export function renderMarkdown(md: string): string {
  if (!md) return '';
  let text = md;

  // 1. 保护代码块 ```code```
  const codeBlocks: string[] = [];
  text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const idx = codeBlocks.length;
    const escaped = code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    codeBlocks.push(`<pre><code class="language-${lang}">${escaped}</code></pre>`);
    return `___CODE_BLOCK_${idx}___`;
  });

  // 2. 保护行内代码 `code`
  const inlineCodes: string[] = [];
  text = text.replace(/`([^`]+)`/g, (_, code) => {
    const idx = inlineCodes.length;
    const escaped = code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    inlineCodes.push(`<code>${escaped}</code>`);
    return `___INLINE_CODE_${idx}___`;
  });

  // 3. 处理标题
  text = text.replace(/^### (.*$)/gim, '<h4>$1</h4>');
  text = text.replace(/^## (.*$)/gim, '<h3>$1</h3>');
  text = text.replace(/^# (.*$)/gim, '<h2>$1</h2>');

  // 4. 粗体、斜体
  text = text.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/__(.*?)__/g, '<strong>$1</strong>');
  text = text.replace(/\*(.*?)\*/g, '<em>$1</em>');

  // 5. 处理列表与段落
  const lines = text.split('\n');
  let inList = false;
  let listType: 'ul' | 'ol' | null = null;
  const resultLines: string[] = [];

  lines.forEach(line => {
    const ulMatch = line.match(/^(\s*)([-*+])\s+(.*)$/);
    const olMatch = line.match(/^(\s*)(\d+\.)\s+(.*)$/);

    if (ulMatch) {
      if (!inList || listType !== 'ul') {
        if (inList) resultLines.push(listType === 'ul' ? '</ul>' : '</ol>');
        resultLines.push('<ul>');
        inList = true;
        listType = 'ul';
      }
      resultLines.push(`<li>${ulMatch[3]}</li>`);
    } else if (olMatch) {
      if (!inList || listType !== 'ol') {
        if (inList) resultLines.push(listType === 'ul' ? '</ul>' : '</ol>');
        resultLines.push('<ol>');
        inList = true;
        listType = 'ol';
      }
      resultLines.push(`<li>${olMatch[3]}</li>`);
    } else {
      if (inList) {
        resultLines.push(listType === 'ul' ? '</ul>' : '</ol>');
        inList = false;
        listType = null;
      }
      const trimmed = line.trim();
      if (trimmed === '') {
        // 空行忽略
      } else if (!trimmed.startsWith('<h') && !trimmed.startsWith('___CODE_BLOCK_')) {
        resultLines.push(`<p>${line}</p>`);
      } else {
        resultLines.push(line);
      }
    }
  });

  if (inList) {
    resultLines.push(listType === 'ul' ? '</ul>' : '</ol>');
  }

  let html = resultLines.join('');

  // 还原代码块与行内代码
  codeBlocks.forEach((cb, idx) => {
    html = html.replace(`___CODE_BLOCK_${idx}___`, cb);
  });
  inlineCodes.forEach((ic, idx) => {
    html = html.replace(`___INLINE_CODE_${idx}___`, ic);
  });

  return html;
}

export function formatPermMode(mode: string): string {
  if (!mode || mode === 'confirm' || mode === 'default') return '默认模式';
  if (mode === 'auto') return '自动接受编辑';
  if (mode === 'plan') return '计划模式';
  if (mode === 'yolo') return 'YOLO (全部允许)';
  return mode;
}
