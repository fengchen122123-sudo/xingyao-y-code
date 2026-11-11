import { describe, it, expect } from 'vitest';
import { renderMarkdown } from './markdown';
import type { MediaItem } from '../types/media';

const img = (ordinal: number, overrides: Partial<MediaItem> = {}): MediaItem => ({
  ordinal,
  kind: 'image',
  mime: 'image/png',
  ext: 'png',
  caption: '图',
  url: `http://127.0.0.1:50000/m/${'a'.repeat(64)}.png?t=tok`,
  ...overrides,
});

const video = (ordinal: number, overrides: Partial<MediaItem> = {}): MediaItem => ({
  ordinal,
  kind: 'video',
  mime: 'video/mp4',
  ext: 'mp4',
  caption: '视频',
  url: `http://127.0.0.1:50000/m/${'b'.repeat(64)}.mp4?t=tok`,
  ...overrides,
});

describe('renderMarkdown 媒体语法', () => {
  it('图片语法 + mediaCtx 渲染为 <img>，ordinal 对齐', () => {
    const md = '![江南水乡](img1.png)';
    const html = renderMarkdown(md, { items: [img(0)] });
    expect(html).toContain('<img class="md-img"');
    expect(html).toContain(`src="${img(0).url}"`);
    expect(html).toContain('alt="江南水乡"');
    expect(html).toContain('data-media-ordinal="0"');
    expect(html).not.toContain('data-file-link');
  });

  it('图片带悬停"复制原图"按钮：data-copy-src 与图标（视频只有另存为、无复制）', () => {
    const html = renderMarkdown('![图](a.png)\n[video: v](b.mp4)', { items: [img(0), video(1)] });
    expect(html).toContain('class="md-media-wrap"');
    expect((html.match(/md-img-copy"/g) || []).length).toBe(1);
    expect(html).toContain(`data-copy-src="${img(0).url}"`);
    expect(html).toContain('<svg');
    // 复制按钮在 wrap 内、img 之后（事件委托按 button 命中，不触发灯箱）
    expect(html.indexOf('md-img-copy')).toBeGreaterThan(html.indexOf('md-img'));
  });

  it('图片带悬停"另存为"按钮：data-save-src 与建议文件名（视频按钮用独立类名）', () => {
    const html = renderMarkdown('![微信群二维码](a.png)\n[video: v](b.mp4)', { items: [img(0), video(1)] });
    expect((html.match(/md-img-save"/g) || []).length).toBe(1);
    expect(html).toContain(`data-save-src="${img(0).url}"`);
    expect(html).toContain('data-save-name="微信群二维码"');
    // 两个按钮同处一个操作区容器：各自绝对定位会互相重叠
    expect(html).toContain('class="md-img-actions"');
    expect(html.indexOf('md-img-actions')).toBeLessThan(html.indexOf('md-img-copy'));
  });

  it('视频语法 [video: x](path) 渲染为 <video controls>', () => {
    const html = renderMarkdown('[video: 演示](demo.mp4)', { items: [video(0)] });
    expect(html).toContain('<video class="md-video"');
    expect(html).toContain('controls');
    expect(html).toContain('playsinline');
    expect(html).toContain(`src="${video(0).url}"`);
  });

  it('中文视频标记 [视频：x](path) 同样渲染为播放器', () => {
    const html = renderMarkdown('[视频：产品演示](demo.mp4)', { items: [video(0)] });
    expect(html).toContain('<video class="md-video"');
  });

  it('多媒体按出现顺序与 items 对齐（图片视频混合）', () => {
    const md = '![一](a.png)\n\n[video: 二](b.mp4)\n\n![三](c.png)';
    const html = renderMarkdown(md, { items: [img(0), video(1), img(2)] });
    expect(html).toContain('data-media-ordinal="0"');
    expect(html).toContain('<video class="md-video"');
    expect((html.match(/<img class="md-img"/g) || []).length).toBe(2);
    expect((html.match(/<video class="md-video"/g) || []).length).toBe(1);
  });

  it('视频带"另存为"按钮与 nodownload，原生 ⋮ 菜单不再出现下载项', () => {
    const html = renderMarkdown('[video: 演示](demo.mp4)', { items: [video(0)] });
    expect(html).toContain('controlslist="nodownload"');
    expect(html).toContain('class="md-media-wrap md-video-wrap"');
    expect(html).toContain('class="md-video-actions"');
    expect((html.match(/md-video-save"/g) || []).length).toBe(1);
    expect(html).toContain(`data-save-src="${video(0).url}"`);
    expect(html).toContain('data-save-name="演示"');
  });

  it('视频不渲染"复制原图"按钮（剪贴板不承载视频）', () => {
    const html = renderMarkdown('[video: v](b.mp4)', { items: [video(0)] });
    expect(html).not.toContain('md-img-copy');
    expect(html).not.toContain('data-copy-src');
  });

  it('视频 caption 缺失时 data-save-name 回退为 source_name', () => {
    const item: MediaItem = { ...video(0), caption: '', source_name: 'demo.mp4' };
    const html = renderMarkdown('[video:](demo.mp4)', { items: [item] });
    expect(html).toContain('data-save-name="demo.mp4"');
  });

  it('error 项渲染为失败态提示', () => {
    const html = renderMarkdown('![坏图](gone.png)', {
      items: [{ ordinal: 0, kind: 'error', error_code: 'not_found', error_label: '源文件不存在', source_name: 'gone.png' }],
    });
    expect(html).toContain('md-media-error');
    expect(html).toContain('源文件不存在');
    expect(html).not.toContain('<img');
  });

  it('无 mediaCtx 时退回文件链接渲染（旧消息兜底）', () => {
    const html = renderMarkdown('![alt](img1.png)');
    expect(html).not.toContain('<img');
    expect(html).toContain('data-file-link');
    expect(html).toContain('data-file-local="1"');
  });

  it('ordinal 越界（清单缺项）时退回文件链接', () => {
    const html = renderMarkdown('![一](a.png)\n![二](b.png)', { items: [img(0)] });
    expect(html).toContain('data-media-ordinal="0"');
    expect(html).toContain('data-file-link');
  });

  it('远程 URL 不当媒体渲染，保持普通链接（防 SSRF）', () => {
    const md = '![远程](https://example.com/x.png)';
    const html = renderMarkdown(md, { items: [img(0)] });
    expect(html).not.toContain('<img class="md-img"');
  });

  it('代码块内的媒体语法不渲染媒体', () => {
    const md = '```\n![不是媒体](x.png)\n```';
    const html = renderMarkdown(md, { items: [img(0)] });
    expect(html).not.toContain('<img class="md-img"');
  });

  it('行内代码内的媒体语法不渲染媒体', () => {
    const md = '看这个 `![不是媒体](x.png)` 语法';
    const html = renderMarkdown(md, { items: [img(0)] });
    expect(html).not.toContain('<img class="md-img"');
  });

  it('src/alt 属性经过 HTML 转义', () => {
    const evil: MediaItem = { ...img(0), url: `http://127.0.0.1:50000/m/x.png?t=a"onerror="1` };
    const html = renderMarkdown('![x"onmouseover="](a.png)', { items: [evil] });
    // 引号必须被转义，不产生可逃逸的属性边界
    expect(html).not.toMatch(/src="[^"]*"[^>]*onerror=/);
    expect(html).toContain('&quot;');
  });
});
