import { describe, it, expect } from 'vitest';
import { renderMarkdown } from './markdown';

describe('renderMarkdown 表格渲染', () => {
  it('渲染标准三行表格', () => {
    const md = [
      '| # | 问题 | 状态 |',
      '|---|---|---|',
      '| 1 | `manage_task` 不认识 | ✅ 已修复 |',
      '| 2 | 普通问题 | ⚠️ 待办 |',
      '| 3 | 另一个问题 | ✅ 已提交 |',
    ].join('\n');
    const html = renderMarkdown(md);
    expect(html).toContain('<table>');
    expect((html.match(/<tr>/g) || []).length).toBe(4); // 1 表头 + 3 数据行
    expect(html).not.toContain('|---|');
  });

  it('某行含空的反引号对（上游占位符被清理后的残留）不应吞掉后续表格行', () => {
    // 复现 2026-08-30 的实际事故：模型输出中的 `` 空代码片段
    // （sanitize 清理占位符后的残留）使行内代码正则跨行贪婪匹配，
    // 把第 2 行到第 4 行的反引号之间整张表吞成一段行内代码。
    const md = [
      '| # | 问题 | 状态 |',
      '|---|---|---|',
      '| 1 | `manage_task` 不认识 | ✅ 已修复 |',
      '| 2 | `` 占位符泄漏到用户界面 | ⚠️ 详见下文 |',
      '| 3 | 后台任务轮询被拦截 | ✅ 已提交 |',
      '| 4 | MCP stdio errlog | ✅ `_resolve_stdio_errlog()` 在位 |',
    ].join('\n');
    const html = renderMarkdown(md);
    expect(html).toContain('<table>');
    // 表头 + 4 个数据行都必须存在
    expect((html.match(/<tr>/g) || []).length).toBe(5);
    // 空的反引号对应原样保留为文本，不应成为跨行代码片段的起点
    expect(html).toContain('占位符泄漏到用户界面');
    expect(html).toContain('_resolve_stdio_errlog()');
  });

  it('行内代码不跨行匹配：一行内的孤立反引号保持为字面量', () => {
    const md = '第一段有个孤立反引号 ` 结尾\n第二行 `code` 正常';
    const html = renderMarkdown(md);
    expect(html).toContain('<code>code</code>');
    // 孤立反引号不应把换行后的内容吞进 <code>
    expect(html).not.toMatch(/<code>[^<]*\n/);
  });

  it('表格单元格内的正常行内代码仍渲染为 <code>', () => {
    const md = [
      '| 函数 | 说明 |',
      '|---|---|',
      '| `renderMarkdown()` | 主入口 |',
    ].join('\n');
    const html = renderMarkdown(md);
    expect(html).toContain('<table>');
    expect(html).toContain('<code>renderMarkdown()</code>');
  });

  it('表格单元格内路径型行内代码按设计渲染为可点击文件链接', () => {
    const md = [
      '| 文件 | 说明 |',
      '|---|---|',
      '| `core/llm.py` | 主循环 |',
    ].join('\n');
    const html = renderMarkdown(md);
    expect(html).toContain('<table>');
    expect(html).toContain('data-file-link="core/llm.py"');
  });
});
