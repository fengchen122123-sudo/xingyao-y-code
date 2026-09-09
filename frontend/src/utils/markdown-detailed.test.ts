import { describe, it, expect } from 'vitest';
import { findCodeBlockRanges, renderMarkdown, renderMarkdownDetailed } from './markdown';
import type { MediaItem } from '../types/media';

const img = (ordinal: number): MediaItem => ({
  ordinal,
  kind: 'image',
  mime: 'image/png',
  ext: 'png',
  caption: '图',
  url: `http://127.0.0.1:50000/m/${'a'.repeat(64)}.png?t=tok`,
});

describe('findCodeBlockRanges', () => {
  it('返回已闭合代码块的字符范围', () => {
    const src = 'a\n```ts\nconst x = 1;\n```\nb';
    const ranges = findCodeBlockRanges(src);
    expect(ranges).toHaveLength(1);
    expect(src.slice(ranges[0][0], ranges[0][1])).toBe('```ts\nconst x = 1;\n```');
  });

  it('未闭合代码块不计入', () => {
    expect(findCodeBlockRanges('a\n```ts\nconst x = 1;\n')).toEqual([]);
  });

  it('空输入返回空数组', () => {
    expect(findCodeBlockRanges('')).toEqual([]);
  });

  it('多个代码块按顺序返回', () => {
    const src = '```a\n1\n```\n\n```b\n2\n```';
    expect(findCodeBlockRanges(src)).toHaveLength(2);
  });
});

describe('renderMarkdownDetailed', () => {
  it('html 与 renderMarkdown 完全一致', () => {
    const md = '# 标题\n\n正文 **加粗** 与 `code`\n\n- a\n- b';
    expect(renderMarkdownDetailed(md).html).toBe(renderMarkdown(md));
  });

  it('无媒体时 mediaCount 为 0', () => {
    expect(renderMarkdownDetailed('纯文本').mediaCount).toBe(0);
  });

  it('mediaCount 统计本地媒体语法数量（含清单越界项）', () => {
    const md = '![一](a.png)\n\n![二](b.png)';
    expect(renderMarkdownDetailed(md, { items: [] }).mediaCount).toBe(2);
  });

  it('起始序号生效：data-media-ordinal 从 startMediaOrdinal 起算', () => {
    const md = '![一](a.png)';
    const html = renderMarkdownDetailed(md, { items: [img(0), img(1)] }, 1).html;
    expect(html).toContain('data-media-ordinal="1"');
  });

  it('空输入返回空 html 与 0 计数', () => {
    expect(renderMarkdownDetailed('')).toEqual({ html: '', mediaCount: 0 });
  });
});
