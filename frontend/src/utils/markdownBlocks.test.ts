import { describe, it, expect } from 'vitest';
import { splitStableBlocks } from './markdownBlocks';
import { renderMarkdown, renderMarkdownDetailed } from './markdown';
import type { MediaItem } from '../types/media';

const img = (ordinal: number): MediaItem => ({
  ordinal,
  kind: 'image',
  mime: 'image/png',
  ext: 'png',
  caption: '图',
  url: `http://127.0.0.1:50000/m/${'a'.repeat(64)}.png?t=tok`,
});

describe('splitStableBlocks', () => {
  it('无空行时全部进 tail', () => {
    expect(splitStableBlocks('一行文字')).toEqual({ blocks: [], tail: '一行文字' });
  });

  it('只有一个安全空行时全部进 tail（滞后一块）', () => {
    const src = 'a\n\nb';
    expect(splitStableBlocks(src)).toEqual({ blocks: [], tail: src });
  });

  it('两个安全空行时第一个空行前成为稳定块', () => {
    const src = 'a\n\nb\n\nc';
    expect(splitStableBlocks(src)).toEqual({ blocks: ['a\n\n'], tail: 'b\n\nc' });
  });

  it('代码块内的空行不算切分点', () => {
    const src = '```ts\nconst a = 1;\n\nconst b = 2;\n```\n\n尾部';
    expect(splitStableBlocks(src)).toEqual({ blocks: [], tail: src });
  });

  it('空输入返回空块与空尾', () => {
    expect(splitStableBlocks('')).toEqual({ blocks: [], tail: '' });
  });

  it('黄金断言：分块渲染拼接 === 整段渲染', () => {
    const cases = [
      'a\n\nb\n\nc\n\nd',
      '# 标题\n\n正文\n\n- 列表一\n- 列表二\n\n```ts\nconst x = 1;\n```\n\n结尾',
      '| a | b |\n| --- | --- |\n| 1 | 2 |\n\n后面一段',
      '> 引用\n> 第二行\n\n普通段落\n\n![图](a.png)',
      '```js\nconsole.log(1)\n```',
      '没有空行的一段话',
      'a\n\n\n\nb\n\nc',
    ];
    for (const src of cases) {
      const { blocks, tail } = splitStableBlocks(src);
      const joined =
        blocks.map((b) => renderMarkdown(b)).join('') + renderMarkdown(tail);
      expect(joined, src).toBe(renderMarkdown(src));
    }
  });

  it('媒体序号跨块连续（黄金断言 + ordinal 校验）', () => {
    const src = '![一](a.png)\n\n![二](b.png)\n\n![三](c.png)';
    const items = [img(0), img(1), img(2)];
    const { blocks, tail } = splitStableBlocks(src);
    let ordinal = 0;
    const html =
      blocks
        .map((b) => {
          const r = renderMarkdownDetailed(b, { items }, ordinal);
          ordinal += r.mediaCount;
          return r.html;
        })
        .join('') + renderMarkdownDetailed(tail, { items }, ordinal).html;
    expect(html).toBe(renderMarkdown(src, { items }));
    expect(html).toContain('data-media-ordinal="0"');
    expect(html).toContain('data-media-ordinal="1"');
    expect(html).toContain('data-media-ordinal="2"');
  });
});
