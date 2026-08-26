import { describe, expect, it } from 'vitest';
import { renderMarkdown } from '../utils/markdown';

describe('renderMarkdown 本地文件路径链接', () => {
  it('裸路径渲染为 data-file-link 链接', () => {
    const html = renderMarkdown('见 docs/foo.md 文件');
    expect(html).toContain('data-file-link="docs/foo.md"');
    expect(html).toContain('data-file-local="1"');
  });
  it('反引号包裹的路径也渲染为可点击链接', () => {
    const html = renderMarkdown('`docs/foo.md`');
    expect(html).toContain('data-file-link="docs/foo.md"');
    expect(html).not.toContain('<code>docs/foo.md</code>');
  });
  it('反引号内的纯文件名保持行内代码', () => {
    const html = renderMarkdown('`pom.xml`');
    expect(html).toContain('<code>pom.xml</code>');
    expect(html).not.toContain('data-file-link');
  });
  it('显式链接的本地路径渲染为可点击链接', () => {
    const html = renderMarkdown('[查看](docs/foo.md)');
    expect(html).toContain('data-file-link="docs/foo.md"');
    expect(html).toContain('>查看</a>');
  });
  it('危险协议链接仍被拦截为 #', () => {
    const html = renderMarkdown('[x](javascript:alert(1))');
    expect(html).toContain('href="#"');
    expect(html).not.toContain('javascript:');
  });
  it('普通文本不被误识别为路径', () => {
    const html = renderMarkdown('import os 和 1.2.3 版本');
    expect(html).not.toContain('data-file-link');
  });
  it('代码块内的路径不做识别', () => {
    const html = renderMarkdown('```\ndocs/foo.md\n```');
    expect(html).not.toContain('data-file-link');
  });
  it('http 链接保持原有 target 与 rel 属性', () => {
    const html = renderMarkdown('[站点](https://example.com)');
    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('target="_blank"');
  });
  it('路径后紧跟中文标点时不吞入标点', () => {
    const html = renderMarkdown('见 E:\\proj\\dist，和 E:\\proj\\ok.md。');
    expect(html).toContain('data-file-link="E:\\proj\\dist"');
    expect(html).toContain('data-file-link="E:\\proj\\ok.md"');
  });
});
