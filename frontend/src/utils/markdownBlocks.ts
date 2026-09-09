// 流式块级增量渲染的源码切分器。
//
// 目标：把正在流式生长的 markdown 源码切成「已完成块」与「可变尾部」，让前端只重渲染尾部。
// 切分点选「倒数第二个安全空行」——生长中的最后一块（列表未结束、代码块未闭合）永远留在尾部，
// 不会被误判为已完成；代价是稳定边界滞后一个块，视觉无感。
//
// 安全空行 = 只含空白字符、且不在任何已闭合代码块范围内的行。
// 代码块范围复用 markdown.ts 的 findCodeBlockRanges，保证与渲染器判定一致。

import { findCodeBlockRanges } from './markdown';

export function splitStableBlocks(source: string): { blocks: string[]; tail: string } {
  if (!source) return { blocks: [], tail: '' };

  const ranges = findCodeBlockRanges(source);
  const inCode = (offset: number): boolean =>
    ranges.some(([start, end]) => offset >= start && offset < end);

  // 收集「安全空行之后」的偏移，即下一块的起点
  const cutPoints: number[] = [];
  let lineStart = 0;
  for (let i = 0; i <= source.length; i++) {
    if (i === source.length || source[i] === '\n') {
      const lineText = source.slice(lineStart, i);
      if (lineText.trim() === '' && !inCode(lineStart)) {
        cutPoints.push(i < source.length ? i + 1 : i);
      }
      lineStart = i + 1;
    }
  }

  if (cutPoints.length < 2) return { blocks: [], tail: source };

  const cut = cutPoints[cutPoints.length - 2];
  const blocks: string[] = [];
  let prev = 0;
  for (const point of cutPoints) {
    if (point >= cut) break;
    const chunk = source.slice(prev, point);
    if (chunk.trim()) blocks.push(chunk);
    prev = point;
  }
  const head = source.slice(prev, cut);
  if (head.trim()) blocks.push(head);

  return { blocks, tail: source.slice(cut) };
}
