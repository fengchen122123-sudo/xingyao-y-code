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
    expect(refKindOf('/gzh-design')).toBe('skill');
    expect(refKindOf('/goal', ['/goal'])).toBe('command');
    expect(refDisplayName('@{缓存改造}')).toBe('缓存改造');
    expect(refDisplayName('@src/a.ts')).toBe('src/a.ts');
    expect(refDisplayName('/gzh-design')).toBe('gzh-design');
    expect(refDisplayName('/goal')).toBe('goal');
  });

  it('传入已启用技能与斜杠命令列表时按优先级识别，不误伤路径与转义斜杠', () => {
    const skills = ['gzh-design', 'office-format', 'goal'];
    const commands = ['/goal', '/fix'];
    const text = '/goal @{关于 /gzh-design 的讨论} /gzh-design /fix 帮我排版 @a.md //goal /usr/bin http://a.com/goal path/goal /goal/sub /office-format，收尾';
    const segs = splitRefSegments(text, skills, commands);

    expect(
      segs
        .filter((s): s is Extract<typeof s, { kind: 'ref' }> => s.kind === 'ref')
        .map(s => [s.refKind, s.text]),
    ).toEqual([
      ['command', '/goal'],
      ['session', '@{关于 /gzh-design 的讨论}'],
      ['skill', '/gzh-design'],
      ['command', '/fix'],
      ['file', '@a.md'],
      ['skill', '/office-format'],
    ]);
    expect(segs.map(s => s.text).join('')).toBe(text);
  });

  it('技能与命令短 ID 匹配大小写不敏感且保留原文 token', () => {
    const segs = splitRefSegments('/Goal /GZH-Design 排版', ['gzh-design'], ['/goal']);
    expect(segs).toEqual([
      { kind: 'ref', text: '/Goal', refKind: 'command' },
      { kind: 'text', text: ' ' },
      { kind: 'ref', text: '/GZH-Design', refKind: 'skill' },
      { kind: 'text', text: ' 排版' },
    ]);
  });
});

describe('composerRefs 退格整删的边界判定', () => {
  it('光标紧贴引用末尾时识别出整个 token', () => {
    expect(refTokenEndingAt('看下 @{缓存改造}', 10)).toBe('@{缓存改造}');
    expect(refTokenEndingAt('看下 @src/a.ts', 12)).toBe('@src/a.ts');
    expect(refTokenEndingAt('@{对话} /gzh-design', 17, ['gzh-design'])).toBe('/gzh-design');
    expect(refTokenEndingAt('/goal', 5, [], ['/goal'])).toBe('/goal');
    expect(refTokenEndingAt('看下 @dir/gzh-design', 18, ['gzh-design'])).toBe('@dir/gzh-design');
    expect(refTokenEndingAt('看下 /gzh-design/sub', 18, ['gzh-design'])).toBeNull();
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