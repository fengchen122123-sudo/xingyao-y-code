// 解析 unified diff 文本为可视化行（含真实文件行号），供"文件更改"面板展开查看。
//
// 输入为后端 core.diff_viewer.generate_diff 产出的标准 unified diff：
//   --- a/path
//   +++ b/path
//   @@ -oldStart,oldLen +newStart,newLen @@
//    context（前导空格）
//   -removed
//   +added
// 行号与后端 render_diff_table 同一套口径：@@ 头给出起始行号，context/removed
// 消耗旧行号，context/added 消耗新行号；删除行只有旧行号，新增行只有新行号。

export interface DiffViewLine {
  kind: 'add' | 'del' | 'ctx';
  /** 旧文件行号（删除行/上下文行有值，新增行为空串） */
  oldNo: string;
  /** 新文件行号（新增行/上下文行有值，删除行为空串） */
  newNo: string;
  /** 已去掉 +/-/空格 前缀的纯内容 */
  content: string;
}

export interface DiffViewBlock {
  /** 该 hunk 裁剪后的可见行（含上下 contextLines 行） */
  lines: DiffViewLine[];
  /** 顶部被裁剪掉的上下文行数（>0 说明上方还有内容） */
  topHidden: number;
  /** 底部被裁剪掉的上下文行数 */
  bottomHidden: number;
}

const HUNK_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

interface RawLine extends DiffViewLine {
  /** 在 hunk 内的原始下标，便于裁剪后定位 */
  _i: number;
}

/**
 * 解析一个 hunk（首行为 @@ 头）为带行号的行数组。
 * @param hunkLines 以 @@ 开头、到下一个 @@ 或文件尾的行（含 @@ 头行本身）
 */
function parseHunk(hunkLines: string[]): RawLine[] {
  const head = hunkLines[0].match(HUNK_RE);
  if (!head) return [];
  let oldLn = parseInt(head[1], 10);
  let newLn = parseInt(head[2], 10);
  const out: RawLine[] = [];
  for (let i = 1; i < hunkLines.length; i++) {
    const line = hunkLines[i];
    if (line === '') continue;
    if (line.startsWith('--- ') || line.startsWith('+++ ')) continue;
    if (line.startsWith('\\')) continue; // "\ No newline at end of file"
    const prefix = line[0];
    const content = line.slice(1);
    if (prefix === '-') {
      out.push({ kind: 'del', oldNo: String(oldLn), newNo: '', content, _i: i });
      oldLn += 1;
    } else if (prefix === '+') {
      out.push({ kind: 'add', oldNo: '', newNo: String(newLn), content, _i: i });
      newLn += 1;
    } else {
      // 上下文行（前缀是空格）
      out.push({ kind: 'ctx', oldNo: String(oldLn), newNo: String(newLn), content, _i: i });
      oldLn += 1;
      newLn += 1;
    }
  }
  return out;
}

/** 把 hunk 内连续上下文裁剪到改动行上下 contextLines 行 */
function trimHunk(lines: RawLine[], contextLines: number): DiffViewBlock {
  const changeIdx: number[] = [];
  lines.forEach((l, idx) => {
    if (l.kind !== 'ctx') changeIdx.push(idx);
  });
  if (changeIdx.length === 0) {
    // 整个 hunk 都是上下文（理论上不会发生），原样返回
    return {
      lines: lines.map(({ _i, ...rest }) => rest),
      topHidden: 0,
      bottomHidden: 0,
    };
  }
  const firstChange = changeIdx[0];
  const lastChange = changeIdx[changeIdx.length - 1];
  const start = Math.max(0, firstChange - contextLines);
  const end = Math.min(lines.length - 1, lastChange + contextLines);
  const visible = lines.slice(start, end + 1).map(({ _i, ...rest }) => rest);
  return {
    lines: visible,
    topHidden: start,
    bottomHidden: lines.length - 1 - end,
  };
}

/**
 * 解析完整 diff 文本为若干可视化块，每块只保留改动行上下 contextLines 行上下文。
 * @param diffText 标准 unified diff（可含多个 hunk；多段文件改动已在外层拼接）
 * @param contextLines 改动行上下各保留的上下文行数（默认 5）
 */
export function buildDiffBlocks(diffText: string, contextLines = 5): DiffViewBlock[] {
  if (!diffText) return [];
  const lines = diffText.split('\n');
  const blocks: DiffViewBlock[] = [];
  let hunkStart = -1;
  for (let i = 0; i < lines.length; i++) {
    if (HUNK_RE.test(lines[i])) {
      if (hunkStart >= 0) {
        blocks.push(trimHunk(parseHunk(lines.slice(hunkStart, i)), contextLines));
      }
      hunkStart = i;
    }
  }
  if (hunkStart >= 0) {
    blocks.push(trimHunk(parseHunk(lines.slice(hunkStart)), contextLines));
  }
  // 无 @@ 头的退化 diff（如纯手工拼接的 +/- 行）：按整段当上下文无法定位行号，
  // 退化为逐行展示且不计行号，保证至少能看到增删内容。
  if (blocks.length === 0) {
    const fallback: DiffViewLine[] = [];
    for (const line of lines) {
      if (!line || line.startsWith('--- ') || line.startsWith('+++ ') || line.startsWith('\\')) continue;
      const prefix = line[0];
      const content = line.slice(1);
      if (prefix === '-') fallback.push({ kind: 'del', oldNo: '', newNo: '', content });
      else if (prefix === '+') fallback.push({ kind: 'add', oldNo: '', newNo: '', content });
      else fallback.push({ kind: 'ctx', oldNo: '', newNo: '', content });
    }
    if (fallback.length) blocks.push({ lines: fallback, topHidden: 0, bottomHidden: 0 });
  }
  return blocks;
}
