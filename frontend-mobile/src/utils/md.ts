/**
 * 星瑶移动端 Markdown 渲染管线（移植自 co-team mobile，全站唯一来源）：
 * 转义 → marked → highlight.js 代码高亮 → 代码块工具条（语言标签 + 复制）→ 表格横向滚动包裹。
 * 安全模型：整体预转义源文本（LLM 输出的裸 HTML 标签一律显示为文本），代码块内先还原一层再交给 hljs 高亮。
 */
import { marked } from 'marked';
import hljs from 'highlight.js/lib/core';
import javascript from 'highlight.js/lib/languages/javascript';
import typescript from 'highlight.js/lib/languages/typescript';
import python from 'highlight.js/lib/languages/python';
import json from 'highlight.js/lib/languages/json';
import bash from 'highlight.js/lib/languages/bash';
import xml from 'highlight.js/lib/languages/xml';
import css from 'highlight.js/lib/languages/css';
import sql from 'highlight.js/lib/languages/sql';
import yaml from 'highlight.js/lib/languages/yaml';
import markdown from 'highlight.js/lib/languages/markdown';
import java from 'highlight.js/lib/languages/java';
import go from 'highlight.js/lib/languages/go';

hljs.registerLanguage('javascript', javascript);
hljs.registerLanguage('js', javascript);
hljs.registerLanguage('typescript', typescript);
hljs.registerLanguage('ts', typescript);
hljs.registerLanguage('python', python);
hljs.registerLanguage('py', python);
hljs.registerLanguage('json', json);
hljs.registerLanguage('bash', bash);
hljs.registerLanguage('sh', bash);
hljs.registerLanguage('shell', bash);
hljs.registerLanguage('xml', xml);
hljs.registerLanguage('html', xml);
hljs.registerLanguage('vue', xml);
hljs.registerLanguage('css', css);
hljs.registerLanguage('sql', sql);
hljs.registerLanguage('yaml', yaml);
hljs.registerLanguage('yml', yaml);
hljs.registerLanguage('markdown', markdown);
hljs.registerLanguage('md', markdown);
hljs.registerLanguage('java', java);
hljs.registerLanguage('go', go);

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
/** 还原整体预转义的一层（还原顺序：&lt;/&gt; 先，&amp; 最后，保证字面 "&lt;" 正确还原） */
function unescapeOnce(s: string): string {
  return s.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
}

marked.use({
  breaks: true,
  renderer: {
    code({ text, lang }: { text: string; lang?: string }): string {
      const raw = unescapeOnce(text);
      const l = (lang || '').trim().split(/\s+/)[0].toLowerCase();
      const body = l && hljs.getLanguage(l)
        ? hljs.highlight(raw, { language: l, ignoreIllegals: true }).value
        : escapeHtml(raw);
      const label = l || 'text';
      return `<div class="md-code"><div class="md-code-bar"><span class="md-code-lang mono">${label}</span><button type="button" class="md-copy" data-code="${encodeURIComponent(raw)}">复制</button></div><pre><code class="hljs">${body}</code></pre></div>`;
    },
    codespan({ text }: { text: string }): string {
      // 源文本整体预转义 + marked 又转义一层 → 还原一层后单次转义显示
      return `<code>${escapeHtml(unescapeOnce(text))}</code>`;
    },
  },
});

export function renderMd(text: string): string {
  const escaped = String(text || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  let html = marked.parse(escaped, { async: false }) as string;
  // 表格横向滚动包裹（长路径/长命令单元格不再撑破弹层）
  html = html.replace(/<table>/g, '<div class="md-twrap"><table>').replace(/<\/table>/g, '</table></div>');
  return html;
}
