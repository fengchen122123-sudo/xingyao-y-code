
import { findCodeBlockRanges } from './markdown';

export function splitStableBlocks(source: string): { blocks: string[]; tail: string } {
  if (!source) return { blocks: [], tail: '' };

  const ranges = findCodeBlockRanges(source);
  const inCode = (offset: number): boolean =>
    ranges.some(([start, end]) => offset >= start && offset < end);

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
