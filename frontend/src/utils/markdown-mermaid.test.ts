import { describe, it, expect } from 'vitest';
import { renderMarkdown, mermaidCacheKey } from './markdown';

describe('renderMarkdown mermaid 块', () => {
  it('mermaid 代码块渲染为占位容器：含缓存 key、隐藏源码与复制按钮', () => {
    const md = '```mermaid\nflowchart TD\n    A[开始] --> B[结束]\n```';
    const html = renderMarkdown(md);
    expect(html).toContain('class="mermaid-block"');
    expect(html).toMatch(/data-mermaid-key="[^"]+"/);
    expect(html).toMatch(/data-state="pending"/);
    expect(html).toContain('<pre class="mermaid-src">flowchart TD');
    expect(html).toContain('mermaid-copy');
    // 占位容器不进入普通代码块分支
    expect(html).not.toContain('class="code-block"');
  });

  it('普通代码块不受影响，仍走 code-block 分支', () => {
    const html = renderMarkdown('```python\nprint("hi")\n```');
    expect(html).toContain('class="code-block"');
    expect(html).toContain('language-python');
    expect(html).not.toContain('mermaid-block');
  });

  it('mermaid 源码按不可信输入转义：<script> 与事件属性不得原样出现', () => {
    const md = '```mermaid\nflowchart TD\nA["<img src=x onerror=alert(1)>"] --> B\n```';
    const html = renderMarkdown(md);
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
    expect(html).not.toContain('<script>');
  });

  it('两个 mermaid 块生成两个占位容器，源码相同则 key 相同', () => {
    const src = 'graph TD\nA-->B';
    const md = '```mermaid\n' + src + '\n```\n\n```mermaid\n' + src + '\n```';
    const html = renderMarkdown(md);
    expect((html.match(/mermaid-block/g) || []).length).toBeGreaterThanOrEqual(2);
    const keys = html.match(/data-mermaid-key="([^"]+)"/g) || [];
    expect(keys.length).toBe(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it('源码不同则缓存 key 不同', () => {
    const a = mermaidCacheKey('graph TD\nA-->B');
    const b = mermaidCacheKey('graph TD\nA-->C');
    expect(a).not.toBe(b);
  });

  it('同一源码的 key 稳定（流式重建 DOM 后缓存命中依赖这一点）', () => {
    const src = 'flowchart TD\n    A --> B';
    expect(mermaidCacheKey(src)).toBe(mermaidCacheKey(src));
  });

  it('mermaid 块不被段落 <p> 包裹', () => {
    const html = renderMarkdown('```mermaid\ngraph TD\nA-->B\n```');
    expect(html).not.toMatch(/<p>\s*\uE000CB\d+\uE000/);
  });

  it('语法残缺的中间态（未闭合栅栏）不产出占位容器', () => {
    const html = renderMarkdown('```mermaid\nflowchart TD\nA-->B\n');
    expect(html).not.toContain('mermaid-block');
  });
});
