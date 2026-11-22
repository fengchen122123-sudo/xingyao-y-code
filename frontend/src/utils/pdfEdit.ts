
export interface Point {
  x: number;
  y: number;
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export type AnnotType =
  | 'highlight'
  | 'underline'
  | 'strike'
  | 'rect'
  | 'arrow'
  | 'ink'
  | 'note'
  | 'stamp';

export interface PageOpDelete {
  kind: 'page';
  op: 'delete';
  page: number;
}

export interface PageOpRotate {
  kind: 'page';
  op: 'rotate';
  page: number;
  degrees: 90 | 180 | 270;
}

export interface PageOpMove {
  kind: 'page';
  op: 'move';
  page: number;
  to: number;
}

export interface PageOpInsertBlank {
  kind: 'page';
  op: 'insertBlank';
  after: number;
  width: number;
  height: number;
}

export type PageOp = PageOpDelete | PageOpRotate | PageOpMove | PageOpInsertBlank;

export interface AnnotOp {
  kind: 'annot';
  type: AnnotType;
  page: number;
  color: string;
  opacity?: number;
  rect?: Rect;
  rects?: Rect[];
  from?: Point;
  to?: Point;
  points?: Point[];
  width?: number;
  text?: string;
  fontSize?: number;
}

export interface RedactOp {
  kind: 'redact';
  page: number;
  rect: Rect;
  fill?: string;
}

export interface TextOp {
  kind: 'text';
  page: number;
  rect: Rect;
  text: string;
  fontSize: number;
  color: string;
  bg?: string | null;
}

export type EditOp = PageOp | AnnotOp | RedactOp | TextOp;

/** 单次编辑的操作总数上限（与后端保持一致，防止一次提交过大） */
export const MAX_OPS = 2000;
export const MAX_TEXT_LENGTH = 4000;
export const HISTORY_LIMIT = 200;
export const MAX_RECTS_PER_ANNOT = 500;
export const MAX_INK_POINTS = 5000;
export const MAX_RECT_DIM = 20000;
export const MIN_STROKE_WIDTH = 0.1;
export const MAX_STROKE_WIDTH = 50;
export const MIN_FONT_SIZE = 1;
export const MAX_FONT_SIZE = 300;

const HEX_COLOR_RE = /^#[0-9a-fA-F]{6}$/;
const ROTATE_DEGREES: readonly number[] = [90, 180, 270];

/** round 到 0.01，避免浮点噪声（-0 归一为 0） */
function round2(v: number): number {
  const r = Math.round(v * 100) / 100;
  return Object.is(r, -0) ? 0 : r;
}

export function normalizeRect(a: Point, b: Point): Rect {
  return {
    x: Math.min(a.x, b.x),
    y: Math.min(a.y, b.y),
    w: Math.abs(a.x - b.x),
    h: Math.abs(a.y - b.y),
  };
}

export function clampRect(r: Rect, page: { w: number; h: number }): Rect {
  const x0 = Math.min(r.x, r.x + r.w);
  const y0 = Math.min(r.y, r.y + r.h);
  const x1 = Math.max(r.x, r.x + r.w);
  const y1 = Math.max(r.y, r.y + r.h);

  const cx0 = Math.max(0, Math.min(x0, page.w));
  const cy0 = Math.max(0, Math.min(y0, page.h));
  const cx1 = Math.max(0, Math.min(x1, page.w));
  const cy1 = Math.max(0, Math.min(y1, page.h));

  const w = cx1 - cx0;
  const h = cy1 - cy0;
  if (!(w >= 1) || !(h >= 1)) return { x: cx0, y: cy0, w: 0, h: 0 };
  return { x: cx0, y: cy0, w, h };
}

export function displayToPdf(p: Point, scale: number): Point {
  const s = Number.isFinite(scale) && scale > 0 ? scale : 1;
  return { x: round2(p.x / s), y: round2(p.y / s) };
}

export function pdfToDisplay(p: Point, scale: number): Point {
  const s = Number.isFinite(scale) && scale > 0 ? scale : 1;
  return { x: round2(p.x * s), y: round2(p.y * s) };
}

export function pageSizeOf(
  sizes: Array<{ w: number; h: number }>,
  page: number,
): { w: number; h: number } | null {
  if (!Number.isInteger(page) || page < 1 || page > sizes.length) return null;
  const s = sizes[page - 1];
  if (!s) return null;
  return { w: s.w, h: s.h };
}

export function opsForPage(ops: readonly EditOp[], page: number): EditOp[] {
  return ops.filter((op) => op.kind !== 'page' && op.page === page);
}

export function summarizeOps(ops: readonly EditOp[]): {
  annotations: number;
  texts: number;
  redactions: number;
  pageOps: number;
  total: number;
} {
  let annotations = 0;
  let texts = 0;
  let redactions = 0;
  let pageOps = 0;
  for (const op of ops) {
    if (op.kind === 'annot') annotations += 1;
    else if (op.kind === 'text') texts += 1;
    else if (op.kind === 'redact') redactions += 1;
    else pageOps += 1;
  }
  return { annotations, texts, redactions, pageOps, total: ops.length };
}

function isIntAtLeast(v: unknown, min: number): boolean {
  return typeof v === 'number' && Number.isFinite(v) && Number.isInteger(v) && v >= min;
}

function checkColor(v: unknown, label: string, issues: string[]): void {
  if (typeof v !== 'string' || !HEX_COLOR_RE.test(v)) {
    issues.push(`${label}：颜色 ${String(v)} 不是 #RRGGBB 格式`);
  }
}

function checkOptionalColor(v: unknown, label: string, issues: string[]): void {
  if (v === undefined || v === null || v === '') return;
  checkColor(v, label, issues);
}

function checkRect(v: Rect | undefined, label: string, issues: string[]): void {
  if (!v) return;
  if (!Number.isFinite(v.x) || !Number.isFinite(v.y)) {
    issues.push(`${label}：坐标必须是有限数`);
  }
  if (!(Number.isFinite(v.w) && v.w > 0) || !(Number.isFinite(v.h) && v.h > 0)) {
    issues.push(`${label}：宽高必须大于 0（当前 ${v.w} × ${v.h}）`);
  } else if (v.w > MAX_RECT_DIM || v.h > MAX_RECT_DIM) {
    issues.push(`${label}：尺寸超出上限 ${MAX_RECT_DIM}pt（当前 ${v.w} × ${v.h}）`);
  }
}

function checkText(v: unknown, label: string, issues: string[]): void {
  if (typeof v !== 'string') return;
  if (v.length > MAX_TEXT_LENGTH) {
    issues.push(`${label}：文本长度 ${v.length} 超过上限 ${MAX_TEXT_LENGTH}`);
  }
}

export function validateOps(ops: readonly EditOp[]): string[] {
  const issues: string[] = [];
  if (ops.length > MAX_OPS) {
    issues.push(`操作数 ${ops.length} 超过上限 ${MAX_OPS}`);
    return issues;
  }

  ops.forEach((op, i) => {
    const at = `第 ${i + 1} 个操作`;

    if (op.kind === 'page') {
      if (op.op === 'insertBlank') {
        if (!isIntAtLeast(op.after, 0)) {
          issues.push(`${at}：after 必须是 ≥0 的整数（当前 ${String(op.after)}）`);
        }
        if (!(Number.isFinite(op.width) && op.width > 0) || !(Number.isFinite(op.height) && op.height > 0)) {
          issues.push(`${at}：插入页的宽高必须大于 0（当前 ${op.width} × ${op.height}）`);
        }
        return;
      }
      if (!isIntAtLeast(op.page, 1)) {
        issues.push(`${at}：page 必须是 ≥1 的整数（当前 ${String(op.page)}）`);
      }
      if (op.op === 'rotate' && !ROTATE_DEGREES.includes(op.degrees)) {
        issues.push(`${at}：rotate 的 degrees 只能是 90/180/270（当前 ${String(op.degrees)}）`);
      }
      if (op.op === 'move' && !isIntAtLeast(op.to, 1)) {
        issues.push(`${at}：move 的 to 必须是 ≥1 的整数（当前 ${String(op.to)}）`);
      }
      return;
    }

    if (!isIntAtLeast(op.page, 1)) {
      issues.push(`${at}：page 必须是 ≥1 的整数（当前 ${String(op.page)}）`);
    }

    if (op.kind === 'annot') {
      checkColor(op.color, at, issues);
      if (op.opacity !== undefined) {
        const o = op.opacity;
        if (typeof o !== 'number' || !Number.isFinite(o) || o < 0 || o > 1) {
          issues.push(`${at}：opacity 必须在 0..1 之间（当前 ${String(o)}）`);
        }
      }
      if (
        op.width !== undefined
        && !(Number.isFinite(op.width)
          && op.width >= MIN_STROKE_WIDTH
          && op.width <= MAX_STROKE_WIDTH)
      ) {
        issues.push(
          `${at}：线宽必须在 ${MIN_STROKE_WIDTH}..${MAX_STROKE_WIDTH} 之间（当前 ${String(op.width)}）`,
        );
      }
      if (
        op.fontSize !== undefined
        && !(Number.isFinite(op.fontSize)
          && op.fontSize >= MIN_FONT_SIZE
          && op.fontSize <= MAX_FONT_SIZE)
      ) {
        issues.push(
          `${at}：fontSize 必须在 ${MIN_FONT_SIZE}..${MAX_FONT_SIZE} 之间（当前 ${String(op.fontSize)}）`,
        );
      }
      checkText(op.text, at, issues);
      checkRect(op.rect, `${at} 的 rect`, issues);
      if (op.rects) {
        if (op.rects.length > MAX_RECTS_PER_ANNOT) {
          issues.push(`${at}：rects 数量 ${op.rects.length} 超过上限 ${MAX_RECTS_PER_ANNOT}`);
        }
        op.rects.forEach((r, j) => checkRect(r, `${at} 的第 ${j + 1} 个 rect`, issues));
      }
      if (op.type === 'ink' && (!op.points || op.points.length < 2)) {
        issues.push(`${at}：ink 至少需要 2 个点（当前 ${op.points ? op.points.length : 0} 个）`);
      }
      if (op.type === 'ink' && op.points && op.points.length > MAX_INK_POINTS) {
        issues.push(`${at}：ink 的 points 数量 ${op.points.length} 超过上限 ${MAX_INK_POINTS}`);
      }
      if (op.type === 'arrow' && (!op.from || !op.to)) {
        issues.push(`${at}：arrow 必须同时提供 from 与 to`);
      }
      return;
    }

    if (op.kind === 'redact') {
      checkOptionalColor(op.fill, `${at} 的 fill`, issues);
      checkRect(op.rect, `${at} 的 rect`, issues);
      return;
    }

    checkColor(op.color, at, issues);
    checkOptionalColor(op.bg, `${at} 的 bg`, issues);
    checkText(op.text, at, issues);
    if (
      !(Number.isFinite(op.fontSize)
        && op.fontSize >= MIN_FONT_SIZE
        && op.fontSize <= MAX_FONT_SIZE)
    ) {
      issues.push(
        `${at}：fontSize 必须在 ${MIN_FONT_SIZE}..${MAX_FONT_SIZE} 之间（当前 ${String(op.fontSize)}）`,
      );
    }
    checkRect(op.rect, `${at} 的 rect`, issues);
  });

  return issues;
}

/** 结构比较（元素视作不可变的纯数据） */
function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (a === null || b === null || typeof a !== 'object') return false;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((v, i) => deepEqual(v, b[i]));
  }
  const ao = a as Record<string, unknown>;
  const bo = b as Record<string, unknown>;
  const ak = Object.keys(ao);
  const bk = Object.keys(bo);
  if (ak.length !== bk.length) return false;
  return ak.every((k) => Object.prototype.hasOwnProperty.call(bo, k) && deepEqual(ao[k], bo[k]));
}

function cloneOps(ops: readonly EditOp[]): EditOp[] {
  return ops.slice();
}

export class EditHistory {
  private stack: EditOp[][];
  private index: number;

  constructor(initial: EditOp[] = []) {
    this.stack = [cloneOps(initial)];
    this.index = 0;
  }

  get current(): EditOp[] {
    return cloneOps(this.stack[this.index] ?? []);
  }

  get canUndo(): boolean {
    return this.index > 0;
  }

  get canRedo(): boolean {
    return this.index < this.stack.length - 1;
  }

  get size(): number {
    return this.stack.length;
  }

  push(ops: EditOp[]): void {
    if (deepEqual(ops, this.stack[this.index])) return;
    if (this.index < this.stack.length - 1) this.stack.length = this.index + 1;
    this.stack.push(cloneOps(ops));
    if (this.stack.length > HISTORY_LIMIT) {
      this.stack.splice(0, this.stack.length - HISTORY_LIMIT);
    }
    this.index = this.stack.length - 1;
  }

  undo(): EditOp[] | null {
    if (!this.canUndo) return null;
    this.index -= 1;
    return this.current;
  }

  redo(): EditOp[] | null {
    if (!this.canRedo) return null;
    this.index += 1;
    return this.current;
  }

  reset(ops: EditOp[] = []): void {
    this.stack = [cloneOps(ops)];
    this.index = 0;
  }
}