import { describe, it, expect } from 'vitest';
import { renderMarkdown } from '../utils/markdown';

describe('renderMarkdown 代码块复制按钮', () => {
  it('代码块渲染为带复制按钮的 code-block 结构', () => {
    const html = renderMarkdown('```python\nprint("hi")\n```');
    expect(html).toContain('class="code-block"');
    expect(html).toContain('class="code-copy"');
    expect(html).toContain('<code class="language-python">');
  });

  it('代码内容里的 <button> 被转义，不会被当成真实按钮', () => {
    const html = renderMarkdown('```\n<button>fake</button>\n```');
    expect(html).not.toContain('<button>fake</button>');
    expect(html).toContain('&lt;button&gt;');
  });
});
