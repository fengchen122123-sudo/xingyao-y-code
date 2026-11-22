
export interface DiffViewLine {
  kind: 'add' | 'del' | 'ctx';
  oldNo: string;
  newNo: string;
  content: string;
}

export interface DiffViewBlock {
  lines: DiffViewLine[];
  topHidden: number;
  bottomHidden: number;
}

const HUNK_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

interface RawLine extends DiffViewLine {
  _i: number;
}

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
    if (line.startsWith('\\')) continue;
    const prefix = line[0];
    const content = line.slice(1);
    if (prefix === '-') {
      out.push({ kind: 'del', oldNo: String(oldLn), newNo: '', content, _i: i });
      oldLn += 1;
    } else if (prefix === '+') {
      out.push({ kind: 'add', oldNo: '', newNo: String(newLn), content, _i: i });
      newLn += 1;
    } else {
      out.push({ kind: 'ctx', oldNo: String(oldLn), newNo: String(newLn), content, _i: i });
      oldLn += 1;
      newLn += 1;
    }
  }
  return out;
}

function trimHunk(lines: RawLine[], contextLines: number): DiffViewBlock {
  const changeIdx: number[] = [];
  lines.forEach((l, idx) => {
    if (l.kind !== 'ctx') changeIdx.push(idx);
  });
  if (changeIdx.length === 0) {
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
