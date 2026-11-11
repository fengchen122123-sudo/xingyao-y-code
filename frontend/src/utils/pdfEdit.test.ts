import { describe, expect, it } from 'vitest';
import {
  EditHistory,
  HISTORY_LIMIT,
  clampRect,
  displayToPdf,
  normalizeRect,
  opsForPage,
  pageSizeOf,
  pdfToDisplay,
  summarizeOps,
  validateOps,
} from './pdfEdit';
import type { AnnotOp, EditOp, TextOp } from './pdfEdit';

const rect = (x: number, y: number, w: number, h: number) => ({ x, y, w, h });

const annot = (page: number, extra: Partial<AnnotOp> = {}): AnnotOp => ({
  kind: 'annot',
  type: 'highlight',
  page,
  color: '#ffcc00',
  rect: rect(10, 10, 40, 12),
  ...extra,
});

const text = (page: number, extra: Partial<TextOp> = {}): TextOp => ({
  kind: 'text',
  page,
  rect: rect(20, 30, 120, 24),
  text: 'hello',
  fontSize: 12,
  color: '#000000',
  ...extra,
});

describe('normalizeRect', () => {
  it('正向拖拽直接得到左上角 + 宽高', () => {
    expect(normalizeRect({ x: 10, y: 20 }, { x: 110, y: 220 })).toEqual(rect(10, 20, 100, 200));
  });

  it('反方向拖拽（右下 -> 左上）仍返回左上角为正的矩形', () => {
    expect(normalizeRect({ x: 110, y: 220 }, { x: 10, y: 20 })).toEqual(rect(10, 20, 100, 200));
  });

  it('跨对角拖拽（右上 -> 左下）同样归一化', () => {
    expect(normalizeRect({ x: 100, y: 50 }, { x: 20, y: 180 })).toEqual(rect(20, 50, 80, 130));
  });

  it('单击（起止点相同）得到零尺寸矩形', () => {
    expect(normalizeRect({ x: 5, y: 5 }, { x: 5, y: 5 })).toEqual(rect(5, 5, 0, 0));
  });
});

describe('clampRect', () => {
  const page = { w: 600, h: 800 };

  it('页面内的矩形原样返回', () => {
    expect(clampRect(rect(10, 20, 100, 50), page)).toEqual(rect(10, 20, 100, 50));
  });

  it('左上越界时裁掉负坐标部分', () => {
    expect(clampRect(rect(-20, -10, 100, 50), page)).toEqual(rect(0, 0, 80, 40));
  });

  it('右下越界时裁到页面边界', () => {
    expect(clampRect(rect(550, 790, 100, 100), page)).toEqual(rect(550, 790, 50, 10));
  });

  it('裁剪后任一边 < 1pt 视作退化，返回 w/h 为 0', () => {
    expect(clampRect(rect(599.5, 10, 10, 10), page)).toEqual(rect(599.5, 10, 0, 0));
    expect(clampRect(rect(10, 10, 10, 0.4), page)).toEqual(rect(10, 10, 0, 0));
  });

  it('完全落在页面外返回 w/h 为 0', () => {
    expect(clampRect(rect(700, 900, 10, 10), page)).toEqual(rect(600, 800, 0, 0));
    expect(clampRect(rect(-100, -100, 50, 50), page)).toEqual(rect(0, 0, 0, 0));
  });

  it('容忍反向尺寸输入', () => {
    expect(clampRect(rect(100, 100, -40, -20), page)).toEqual(rect(60, 80, 40, 20));
  });
});

describe('displayToPdf / pdfToDisplay', () => {
  it('scale=1.5 时显示像素除以 scale', () => {
    expect(displayToPdf({ x: 150, y: 300 }, 1.5)).toEqual({ x: 100, y: 200 });
    expect(pdfToDisplay({ x: 100, y: 200 }, 1.5)).toEqual({ x: 150, y: 300 });
  });

  it('scale=0.8 时显示像素按比例换算', () => {
    expect(displayToPdf({ x: 100, y: 200 }, 0.8)).toEqual({ x: 125, y: 250 });
    expect(pdfToDisplay({ x: 125, y: 250 }, 0.8)).toEqual({ x: 100, y: 200 });
  });

  it('往返一致（误差 <= 0.01）', () => {
    const points = [
      { x: 0, y: 0 },
      { x: 120, y: 340 },
      { x: 33.33, y: 78.91 },
    ];
    for (const scale of [1.5, 0.8]) {
      for (const p of points) {
        const back = pdfToDisplay(displayToPdf(p, scale), scale);
        expect(Math.abs(back.x - p.x)).toBeLessThanOrEqual(0.01);
        expect(Math.abs(back.y - p.y)).toBeLessThanOrEqual(0.01);
      }
    }
  });

  it('结果 round 到 0.01', () => {
    expect(displayToPdf({ x: 10, y: 10 }, 3)).toEqual({ x: 3.33, y: 3.33 });
    expect(pdfToDisplay({ x: 3.3333, y: 6.6666 }, 1)).toEqual({ x: 3.33, y: 6.67 });
  });

  it('非法 scale 退化为 1，不产生 NaN/Infinity', () => {
    expect(displayToPdf({ x: 42, y: 7 }, 0)).toEqual({ x: 42, y: 7 });
    expect(pdfToDisplay({ x: 42, y: 7 }, Number.NaN)).toEqual({ x: 42, y: 7 });
  });
});

describe('pageSizeOf', () => {
  const sizes = [
    { w: 612, h: 792 },
    { w: 595, h: 842 },
  ];

  it('按 1 基页序取尺寸', () => {
    expect(pageSizeOf(sizes, 1)).toEqual({ w: 612, h: 792 });
    expect(pageSizeOf(sizes, 2)).toEqual({ w: 595, h: 842 });
  });

  it('越界返回 null', () => {
    expect(pageSizeOf(sizes, 0)).toBeNull();
    expect(pageSizeOf(sizes, 3)).toBeNull();
    expect(pageSizeOf(sizes, -1)).toBeNull();
    expect(pageSizeOf(sizes, 1.5)).toBeNull();
    expect(pageSizeOf([], 1)).toBeNull();
  });
});

describe('opsForPage', () => {
  it('只挑本页的 annot/redact/text，跳过 page 操作', () => {
    const ops: EditOp[] = [
      annot(1),
      annot(2),
      { kind: 'redact', page: 1, rect: rect(0, 0, 20, 20) },
      text(2),
      { kind: 'page', op: 'delete', page: 1 },
      { kind: 'page', op: 'rotate', page: 1, degrees: 90 },
      { kind: 'page', op: 'insertBlank', after: 1, width: 600, height: 800 },
    ];
    const page1 = opsForPage(ops, 1);
    expect(page1).toHaveLength(2);
    expect(page1.every((o) => o.kind !== 'page')).toBe(true);
    expect(page1.map((o) => o.kind)).toEqual(['annot', 'redact']);
    expect(opsForPage(ops, 2).map((o) => o.kind)).toEqual(['annot', 'text']);
    expect(opsForPage(ops, 3)).toEqual([]);
  });
});

describe('summarizeOps', () => {
  it('分类计数正确', () => {
    const ops: EditOp[] = [
      annot(1),
      annot(1, { type: 'ink', points: [{ x: 1, y: 1 }, { x: 2, y: 2 }] }),
      text(1),
      { kind: 'redact', page: 2, rect: rect(0, 0, 10, 10) },
      { kind: 'page', op: 'delete', page: 1 },
      { kind: 'page', op: 'move', page: 1, to: 2 },
    ];
    expect(summarizeOps(ops)).toEqual({
      annotations: 2,
      texts: 1,
      redactions: 1,
      pageOps: 2,
      total: 6,
    });
  });

  it('空列表全为 0', () => {
    expect(summarizeOps([])).toEqual({
      annotations: 0,
      texts: 0,
      redactions: 0,
      pageOps: 0,
      total: 0,
    });
  });
});

describe('validateOps', () => {
  it('合法操作集合返回空数组', () => {
    const ops: EditOp[] = [
      annot(1),
      annot(2, { type: 'ink', points: [{ x: 1, y: 1 }, { x: 3, y: 4 }], opacity: 0.5 }),
      annot(2, { type: 'arrow', from: { x: 1, y: 1 }, to: { x: 10, y: 10 } }),
      text(1, { bg: '#ffffff' }),
      { kind: 'redact', page: 1, rect: rect(0, 0, 30, 12), fill: '#000000' },
      { kind: 'page', op: 'rotate', page: 1, degrees: 180 },
      { kind: 'page', op: 'move', page: 1, to: 3 },
      { kind: 'page', op: 'insertBlank', after: 0, width: 600, height: 800 },
    ];
    expect(validateOps(ops)).toEqual([]);
  });

  it('非法颜色被指出', () => {
    const issues = validateOps([annot(1, { color: 'yellow' })]);
    expect(issues).toHaveLength(1);
    expect(issues[0]).toContain('颜色');
  });

  it('rect 宽高必须 > 0', () => {
    const issues = validateOps([{ kind: 'redact', page: 1, rect: rect(10, 10, -5, 10) }]);
    expect(issues).toHaveLength(1);
    expect(issues[0]).toContain('宽高');
  });

  it('rects 里每个矩形都要校验', () => {
    const issues = validateOps([
      annot(1, { rects: [rect(0, 0, 10, 10), rect(0, 0, 10, 0)] }),
    ]);
    expect(issues).toHaveLength(1);
    expect(issues[0]).toContain('第 2 个 rect');
  });

  it('ink 少于 2 个点被指出', () => {
    const issues = validateOps([
      annot(1, { type: 'ink', points: [{ x: 1, y: 2 }] }),
    ]);
    expect(issues).toHaveLength(1);
    expect(issues[0]).toContain('ink');
  });

  it('arrow 缺 from/to 被指出', () => {
    const onlyFrom = validateOps([annot(1, { type: 'arrow', from: { x: 1, y: 1 } })]);
    expect(onlyFrom).toHaveLength(1);
    expect(onlyFrom[0]).toContain('from');
    expect(onlyFrom[0]).toContain('to');

    const onlyTo = validateOps([annot(1, { type: 'arrow', to: { x: 1, y: 1 } })]);
    expect(onlyTo).toHaveLength(1);
    expect(onlyTo[0]).toContain('arrow');
  });

  it('page 必须是 >=1 的整数', () => {
    const issues = validateOps([annot(0)]);
    expect(issues).toHaveLength(1);
    expect(issues[0]).toContain('page');

    expect(validateOps([annot(1.5)])[0]).toContain('page');
    expect(validateOps([{ kind: 'page', op: 'delete', page: 0 }])[0]).toContain('page');
    expect(validateOps([text(-2)])[0]).toContain('page');
  });

  it('text 超过 4000 字被指出', () => {
    const long = '字'.repeat(4001);
    const issues = validateOps([text(1, { text: long })]);
    expect(issues).toHaveLength(1);
    expect(issues[0]).toContain('文本长度');

    // 正好 4000 字合法
    expect(validateOps([text(1, { text: '字'.repeat(4000) })])).toEqual([]);
  });

  it('opacity 越界被指出', () => {
    const issues = validateOps([annot(1, { opacity: 1.5 })]);
    expect(issues).toHaveLength(1);
    expect(issues[0]).toContain('opacity');
  });

  it('多个问题一次性全部返回', () => {
    const issues = validateOps([
      annot(0, { color: 'nope' }),
      { kind: 'page', op: 'rotate', page: 1, degrees: 45 as unknown as 90 },
    ]);
    expect(issues.length).toBeGreaterThanOrEqual(3);
  });
});

describe('EditHistory', () => {
  it('初始为空快照，不能 undo/redo', () => {
    const h = new EditHistory();
    expect(h.current).toEqual([]);
    expect(h.canUndo).toBe(false);
    expect(h.canRedo).toBe(false);
    expect(h.size).toBe(1);
    expect(h.undo()).toBeNull();
    expect(h.redo()).toBeNull();
  });

  it('支持传入初始快照', () => {
    const h = new EditHistory([annot(1)]);
    expect(h.current).toHaveLength(1);
    expect(h.canUndo).toBe(false);
  });

  it('push / undo / redo 走通', () => {
    const h = new EditHistory();
    h.push([annot(1)]);
    h.push([annot(1), text(1)]);
    expect(h.size).toBe(3);
    expect(h.canUndo).toBe(true);
    expect(h.canRedo).toBe(false);

    const back1 = h.undo();
    expect(back1).toHaveLength(1);
    expect(h.canUndo).toBe(true);
    expect(h.canRedo).toBe(true);

    const back0 = h.undo();
    expect(back0).toEqual([]);
    expect(h.canUndo).toBe(false);

    const fwd1 = h.redo();
    expect(fwd1).toHaveLength(1);
    const fwd2 = h.redo();
    expect(fwd2).toHaveLength(2);
    expect(h.canRedo).toBe(false);
    expect(h.redo()).toBeNull();
  });

  it('push 后清空 redo 分支', () => {
    const h = new EditHistory();
    h.push([annot(1)]);
    h.push([annot(2)]);
    h.undo();
    expect(h.canRedo).toBe(true);

    h.push([annot(3)]);
    expect(h.canRedo).toBe(false);
    expect(h.current).toEqual([annot(3)]);
    expect(h.size).toBe(3);
    expect(h.redo()).toBeNull();
  });

  it('相同快照不重复入栈', () => {
    const h = new EditHistory();
    h.push([annot(1)]);
    const size = h.size;
    h.push([annot(1)]);
    expect(h.size).toBe(size);
    // 结构相同但对象不同，也算相同快照
    h.push([annot(1, { rect: rect(10, 10, 40, 12) })]);
    expect(h.size).toBe(size);
    expect(h.canUndo).toBe(true);
  });

  it('容量上限 200，超出丢弃最旧快照', () => {
    const h = new EditHistory();
    for (let page = 1; page <= HISTORY_LIMIT + 50; page++) {
      h.push([annot(page)]);
    }
    expect(HISTORY_LIMIT).toBe(200);
    expect(h.size).toBe(200);

    // 一路 undo 到底：最旧快照应是第 51 次 push（page=51）
    let steps = 0;
    while (h.canUndo) {
      h.undo();
      steps += 1;
    }
    expect(steps).toBe(199);
    expect(h.current).toHaveLength(1);
    expect((h.current[0] as AnnotOp).page).toBe(51);
  });

  it('undo/current 返回拷贝，外部修改不影响历史', () => {
    const h = new EditHistory();
    const pushIn: EditOp[] = [annot(1)];
    h.push(pushIn);
    // 外部改入参数组不影响已入栈快照
    pushIn.push(annot(2));
    expect(h.current).toHaveLength(1);

    const back = h.undo();
    expect(back).toEqual([]);
    back!.push(annot(9));
    expect(h.current).toEqual([]);
    expect(h.redo()).toEqual([annot(1)]);

    // 改 current 返回的数组同样不影响内部
    const cur = h.current;
    cur.push(annot(3));
    expect(h.current).toHaveLength(1);
  });

  it('reset 清空历史，可带初始快照', () => {
    const h = new EditHistory();
    h.push([annot(1)]);
    h.push([annot(2)]);
    h.reset();
    expect(h.size).toBe(1);
    expect(h.current).toEqual([]);
    expect(h.canUndo).toBe(false);
    expect(h.canRedo).toBe(false);

    h.reset([text(1)]);
    expect(h.current).toEqual([text(1)]);
    expect(h.size).toBe(1);
  });
});

describe('validateOps：数值上限与后端 core/pdf_edit.py 一致', () => {
  it('线宽必须落在 0.1..50', () => {
    expect(validateOps([annot(1, { width: 0.05 })])).toHaveLength(1);
    expect(validateOps([annot(1, { width: 60 })])).toHaveLength(1);
    expect(validateOps([annot(1, { width: 3 })])).toHaveLength(0);
  });

  it('字号必须落在 1..300', () => {
    expect(validateOps([text(1, { fontSize: 0.5 })])).toHaveLength(1);
    expect(validateOps([text(1, { fontSize: 301 })])).toHaveLength(1);
    expect(validateOps([text(1, { fontSize: 300 })])).toHaveLength(0);
  });

  it('单条批注的 rects 数量上限 500', () => {
    const many = Array.from({ length: 501 }, () => rect(0, 0, 10, 10));
    expect(validateOps([annot(1, { rects: many })])[0]).toContain('500');
  });

  it('手写笔迹点数上限 5000', () => {
    const points = Array.from({ length: 5001 }, (_, i) => ({ x: i, y: i }));
    expect(validateOps([annot(1, { type: 'ink', points })])[0]).toContain('5000');
  });

  it('矩形边长上限 20000pt', () => {
    const op = { kind: 'redact', page: 1, rect: rect(0, 0, 20001, 10) } as const;
    expect(validateOps([op as never])[0]).toContain('20000');
  });
});
