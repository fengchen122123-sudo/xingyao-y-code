import { describe, expect, it } from 'vitest';
import { buildDiffBlocks } from './diffView';

describe('buildDiffBlocks', () => {
  it('解析 @@ 行号头：删除行用旧行号、新增行用新行号、上下文行两者都有', () => {
    const diff = [
      '--- a/f.py',
      '+++ b/f.py',
      '@@ -10,4 +10,5 @@',
      ' ctx10',
      '-old11',
      '+new11',
      '+new12',
      ' ctx13',
    ].join('\n');
    const blocks = buildDiffBlocks(diff, 0);
    expect(blocks).toHaveLength(1);
    const lines = blocks[0].lines;
    // contextLines=0：只保留改动行
    expect(lines.map((l) => l.kind)).toEqual(['del', 'add', 'add']);
    expect(lines[0]).toMatchObject({ oldNo: '11', newNo: '', content: 'old11' });
    expect(lines[1]).toMatchObject({ oldNo: '', newNo: '11', content: 'new11' });
    expect(lines[2]).toMatchObject({ oldNo: '', newNo: '12', content: 'new12' });
  });

  it('contextLines=5 保留改动上下各 5 行上下文，行号连续正确', () => {
    // 构造 8 行上文 + 1 改动 + 8 行下文的 hunk
    const before = Array.from({ length: 8 }, (_, i) => ` b${i + 1}`);
    const after = Array.from({ length: 8 }, (_, i) => ` a${i + 1}`);
    const diff = [
      '--- a/f.py',
      '+++ b/f.py',
      '@@ -1,18 +1,18 @@',
      ...before, // b1..b8 = 行 1..8
      '-old', // 旧行 9
      '+new', // 新行 9
      ...after, // 旧/新行 10..17
    ].join('\n');
    const blocks = buildDiffBlocks(diff, 5);
    const lines = blocks[0].lines;
    // 上：b4..b8（行5..9 上文5行），下：a1..a5（行10..14 下文5行）
    // 顶部隐藏 3 行（b1..b3），底部隐藏 3 行（a6..a8）
    expect(blocks[0].topHidden).toBe(3);
    expect(blocks[0].bottomHidden).toBe(3);
    // 首行可见上下文 b4 = 旧/新行 4
    expect(lines[0]).toMatchObject({ kind: 'ctx', oldNo: '4', newNo: '4' });
    const del = lines.find((l) => l.kind === 'del')!;
    const add = lines.find((l) => l.kind === 'add')!;
    expect(del.oldNo).toBe('9');
    expect(add.newNo).toBe('9');
    // 末行可见上下文 a5 = 行 14
    expect(lines[lines.length - 1]).toMatchObject({ kind: 'ctx', oldNo: '14', newNo: '14' });
  });

  it('多 hunk 拆成多块，各自行号独立', () => {
    const diff = [
      '@@ -1,2 +1,2 @@',
      '-a',
      '+b',
      '@@ -50,2 +50,2 @@',
      '-x',
      '+y',
    ].join('\n');
    const blocks = buildDiffBlocks(diff, 0);
    expect(blocks).toHaveLength(2);
    expect(blocks[0].lines[0].oldNo).toBe('1');
    expect(blocks[1].lines[0].oldNo).toBe('50');
  });

  it('跳过 ---/+++ 文件头与 \\ 注释行', () => {
    const diff = [
      '--- a/f.py',
      '+++ b/f.py',
      '@@ -1,2 +1,2 @@',
      '-a',
      '+b',
      '\\ No newline at end of file',
    ].join('\n');
    const blocks = buildDiffBlocks(diff, 0);
    expect(blocks[0].lines).toHaveLength(2);
  });

  it('无 @@ 头的退化 diff：逐行展示增删，不计行号', () => {
    const blocks = buildDiffBlocks('-x\n+y\n ctx', 5);
    expect(blocks).toHaveLength(1);
    expect(blocks[0].lines.map((l) => l.kind)).toEqual(['del', 'add', 'ctx']);
    expect(blocks[0].lines.every((l) => l.oldNo === '' && l.newNo === '')).toBe(true);
  });

  it('空文本返回空数组', () => {
    expect(buildDiffBlocks('')).toEqual([]);
  });
});
