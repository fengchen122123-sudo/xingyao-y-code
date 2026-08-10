import { describe, it, expect } from 'vitest';
import { renderMarkdown } from '../utils/markdown';

describe('renderMarkdown 表格渲染', () => {
  it('GFM 表格渲染为 table 标签', () => {
    const html = renderMarkdown('| 项目 | 状态 |\n|---|---|\n| README | 不存在 |\n| docs | 空文件 |');
    expect(html).toContain('<table>');
    expect(html).toContain('<th>项目</th>');
    expect(html).toContain('<th>状态</th>');
    expect(html).toContain('<td>不存在</td>');
    expect(html).toContain('</table>');
  });

  it('支持对齐标记分隔行', () => {
    const html = renderMarkdown('| a | b |\n| :--- | ---: |\n| 1 | 2 |');
    expect(html).toContain('<td>1</td>');
  });

  it('表格行内的行内代码不破坏表格', () => {
    const html = renderMarkdown('| 文件 | 说明 |\n|---|---|\n| `pom.xml` | 无配置 |');
    expect(html).toContain('<table>');
    expect(html).toContain('<code>pom.xml</code>');
  });

  it('表格前后文本仍正常成段', () => {
    const html = renderMarkdown('前文\n\n| a |\n|---|\n| b |\n\n后文');
    expect(html).toContain('<p>前文</p>');
    expect(html).toContain('<table>');
    expect(html).toContain('<p>后文</p>');
  });

  it('单元格内的 HTML 被转义', () => {
    const html = renderMarkdown('| a |\n|---|\n| <img src=x onerror=alert(1)> |');
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
  });
});
