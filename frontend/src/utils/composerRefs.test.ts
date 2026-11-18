import { describe, expect, it } from 'vitest';
import { refTokenEndingAt, refDisplayName, refKindOf, splitRefSegments } from './composerRefs';

// 切分口径必须与后端 core/input_parser.py 一致，否则会出现「前端显示成引用、
// 后端却没当引用」的错位。用例直接对着后端两条正则的行为断言。

describe('composerRefs @ 引用切分', () => {
  it('会话引用与文件引用分别识别，拼回去与原文逐字符相等', () => {
    const text = '先 @{对话A} 再 @docs/接口文档.md 然后结束\n第二行 @b.py';
    const segs = splitRefSegments(text);

    expect(segs.filter(s => s.kind === 'ref').map(s => (s.kind === 'ref' ? s.text : ''))).toEqual([
      '@{对话A}',
      '@docs/接口文档.md',
      '@b.py',
    ]);
    expect(segs.map(s => s.text).join('')).toBe(text);
  });

  it('中文句读切断文件 token：@接口文档.md，然后 → 胶囊不含逗号', () => {
    const segs = splitRefSegments('看下 @接口文档.md，然后说结论');
    expect(segs.filter(s => s.kind === 'ref').map(s => (s.kind === 'ref' ? s.text : ''))).toEqual([
      '@接口文档.md',
    ]);
  });

  it('会话引用不跨行且最长 80 字符，超长或未闭合都不算', () => {
    expect(splitRefSegments('@{' + '长'.repeat(90) + '}').some(s => s.kind === 'ref')).toBe(false);
    expect(splitRefSegments('@{没闭合\n下一行').some(s => s.kind === 'ref')).toBe(false);
  });

  it('胶囊标签去掉语法记号，类型判断只看前缀', () => {
    expect(refKindOf('@{缓存改造}')).toBe('session');
    expect(refKindOf('@src/a.ts')).toBe('file');
    expect(refDisplayName('@{缓存改造}')).toBe('缓存改造');
    expect(refDisplayName('@src/a.ts')).toBe('src/a.ts');
  });
});

describe('composerRefs 退格整删的边界判定', () => {
  it('光标紧贴引用末尾时识别出整个 token', () => {
    expect(refTokenEndingAt('看下 @{缓存改造}', 10)).toBe('@{缓存改造}');
    expect(refTokenEndingAt('看下 @src/a.ts', 12)).toBe('@src/a.ts');
  });

  it('光标在引用中间或不在引用上时返回 null（照常逐字删除）', () => {
    expect(refTokenEndingAt('看下 @{缓存改造}', 6)).toBeNull();
    expect(refTokenEndingAt('普通文字', 4)).toBeNull();
    expect(refTokenEndingAt('', 0)).toBeNull();
  });

  it('越界光标位置被夹住，不抛异常', () => {
    expect(refTokenEndingAt('@a.ts', 999)).toBe('@a.ts');
    expect(refTokenEndingAt('@a.ts', -5)).toBeNull();
  });
});