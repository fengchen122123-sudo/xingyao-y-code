import { describe, expect, it } from 'vitest';
import { absolutizeResourceUrl, buildMarkdownPreviewHtml } from './previewHtml';

describe('absolutizeResourceUrl', () => {
  it('相对路径拼到源文件目录并转 file://', () => {
    expect(absolutizeResourceUrl('img/a.png', 'D:\\proj\\docs')).toBe(
      'file:///D:/proj/docs/img/a.png',
    );
    expect(absolutizeResourceUrl('./a.png', 'D:\\proj')).toBe('file:///D:/proj/a.png');
  });

  it('中文 / 空格路径按段百分号编码', () => {
    expect(absolutizeResourceUrl('图 片/a.png', 'D:\\proj\\文档')).toBe(
      'file:///D:/proj/%E6%96%87%E6%A1%A3/%E5%9B%BE%20%E7%89%87/a.png',
    );
  });

  it('绝对 URL / 锚点 / 协议相对地址保持原样', () => {
    expect(absolutizeResourceUrl('https://x.com/a.png', 'D:\\proj')).toBe('https://x.com/a.png');
    expect(absolutizeResourceUrl('data:image/png;base64,AAA', 'D:\\proj')).toBe(
      'data:image/png;base64,AAA',
    );
    expect(absolutizeResourceUrl('#sec', 'D:\\proj')).toBe('#sec');
    expect(absolutizeResourceUrl('//cdn.x.com/a.png', 'D:\\proj')).toBe('//cdn.x.com/a.png');
  });

  it('没有基准目录时相对路径保持原样（不猜路径）', () => {
    expect(absolutizeResourceUrl('img/a.png', '')).toBe('img/a.png');
  });
});

describe('buildMarkdownPreviewHtml', () => {
  it('把渲染结果包成自包含 HTML', () => {
    const html = buildMarkdownPreviewHtml('# 标题\n\n正文', 'D:\\proj\\a.md', 'a.md');

    expect(html).toContain('<!DOCTYPE html>');
    expect(html).toContain('<title>a.md</title>');
    expect(html).toContain('<h'); // 标题已渲染（级别由渲染器决定）
    expect(html).toContain('标题');
    expect(html).toContain('.md-preview');
  });

  it('相对图片还原成 <img> 并指向源文件目录下的 file:// 地址', () => {
    const html = buildMarkdownPreviewHtml('![图](img/a.png)', 'D:\\proj\\docs\\a.md');

    // 渲染器在无媒体上下文时把图片降级成文件链接，预览页里要还原成图片
    expect(html).toContain('<img src="file:///D:/proj/docs/img/a.png"');
    expect(html).not.toContain('data-file-link');
  });

  it('本地非图片链接改成可点击的 file:// 地址', () => {
    const html = buildMarkdownPreviewHtml('[配置](config/settings.json)', 'D:\\proj\\a.md');

    expect(html).toContain('href="file:///D:/proj/config/settings.json"');
    expect(html).not.toContain('data-file-link');
  });

  it('CRLF 文件也能渲染标题（与弹窗预览同处理）', () => {
    const html = buildMarkdownPreviewHtml('# 标题\r\n\r\n正文', 'D:\\proj\\a.md');

    expect(html).toContain('<h');
    expect(html).toContain('标题');
  });

  it('标题里的 HTML 被转义（防注入）', () => {
    const html = buildMarkdownPreviewHtml('x', 'D:\\proj\\a.md', '<script>alert(1)</script>');

    expect(html).not.toContain('<script>alert(1)</script>');
    expect(html).toContain('&lt;script&gt;');
  });
});
