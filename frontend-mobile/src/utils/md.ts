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

const COLLAPSE_LINES = 4;

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
      const terminal = /^(bash|sh|shell|zsh|powershell|ps1|cmd|bat|sql)$/.test(l);
      const lines = raw.replace(/\n+$/, '').split('\n').length;
      const collapsible = lines > COLLAPSE_LINES;
      const toggle = collapsible
        ? `<button type="button" class="md-toggle" data-lines="${lines}">展开</button>`
        : '';
      const fade = collapsible ? '<div class="md-fade"></div>' : '';
      return `<div class="md-code${terminal ? ' term' : ''}" data-kind="${terminal ? 'term' : 'code'}" data-lines="${lines}"><div class="md-code-bar"><span class="md-code-lang mono">${label}</span><span class="md-bar-r">${toggle}<button type="button" class="md-copy" data-code="${encodeURIComponent(raw)}">复制</button></span></div><pre><code class="hljs">${body}</code></pre>${fade}</div>`;
    },
    codespan({ text }: { text: string }): string {
      return `<code>${escapeHtml(unescapeOnce(text))}</code>`;
    },
  },
});

export function renderMd(text: string): string {
  const escaped = String(text || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  let html = marked.parse(escaped, { async: false }) as string;
  html = html.replace(/<table>/g, '<div class="md-twrap"><table>').replace(/<\/table>/g, '</table></div>');
  return html;
}
