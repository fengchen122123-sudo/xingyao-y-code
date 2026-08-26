import { describe, it, expect } from 'vitest';
import { renderMarkdown } from '../utils/markdown';

describe('renderMarkdown 有序列表', () => {
  it('连续编号的列表渲染为单个 ol', () => {
    const html = renderMarkdown('1. 方案A\n2. 方案B\n3. 方案C');
    expect(html).toContain('<ol');
    expect(html).toContain('<li>方案A</li>');
    expect(html).toContain('<li>方案B</li>');
    expect(html).toContain('<li>方案C</li>');
    // 连续列表只有一个 <ol>，浏览器自动递增编号
    expect(html.match(/<ol/g)?.length).toBe(1);
  });

  it('被空行打断的有序列表保留各自的原始编号', () => {
    const html = renderMarkdown('1. 方案A\n\n2. 方案B\n\n3. 方案C');
    // 每个独立列表项用 start 保留原始编号，避免都从 1 重新开始
    expect(html).toContain('<ol start="1"');
    expect(html).toContain('<ol start="2"');
    expect(html).toContain('<ol start="3"');
  });

  it('空行打断的懒编号列表按各自编号渲染', () => {
    const html = renderMarkdown('1. 方案A\n\n1. 方案B\n\n1. 方案C');
    expect(html.match(/<ol/g)?.length).toBe(3);
    expect(html).toContain('<li>方案A</li>');
    expect(html).toContain('<li>方案B</li>');
    expect(html).toContain('<li>方案C</li>');
  });
});
