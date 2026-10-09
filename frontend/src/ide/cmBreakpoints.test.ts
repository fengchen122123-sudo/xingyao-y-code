import { describe, expect, it } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';

import {
  BreakpointMarker,
  breakpointGutter,
  breakpointKey,
  execLineField,
  refreshBreakpointsEffect,
  setExecLineEffect,
} from './cmBreakpoints';

const DOC = 'a\nb\nc\nd\n';

function makeState() {
  return EditorState.create({ doc: DOC, extensions: [execLineField] });
}

/**
 * 真实 EditorView（jsdom 下可创建）：断点标记「什么时候重算」是 CM6 gutter 的内部
 * 时序，只有真的视图才跑得起来，光造 EditorState 证不了（S3-T11 评审 F3）。
 */
function mountGutter(lines: number[], onToggle: (line: number) => void = () => {}) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const view = new EditorView({
    state: EditorState.create({
      doc: DOC,
      // 只装 breakpointGutter + execLineField：样式必须是扩展自带的（评审 F1）
      extensions: [breakpointGutter({ lines: () => lines, onToggle }), execLineField],
    }),
    parent: host,
  });
  return { view, host };
}

/** gutter 里真画出来的断点圆点（initialSpacer 那个隐藏占位不算）。 */
function dots(view: EditorView): HTMLElement[] {
  return [...view.dom.querySelectorAll('.cm-ide-breakpoint')].filter((el) => {
    const cell = el.closest('.cm-gutterElement') as HTMLElement | null;
    return !(cell && cell.style.visibility === 'hidden');
  }) as HTMLElement[];
}

/** 取挂在 document.head 里的某条 CSS 规则原文（style-mod 每条规则输出一行）。 */
function cssRule(selector: string): string {
  const css = document.head.textContent ?? '';
  return css.split('\n').find((line) => line.includes(selector + ' {')) ?? '';
}

describe('breakpointKey', () => {
  it('builds the same key shape the store uses', () => {
    expect(breakpointKey('E:/p/app.py', 7)).toBe('E:/p/app.py:7');
  });

  it('与 ideStore 同口径：只统一分隔符，不折叠大小写与 ..（评审 F4）', () => {
    // 反斜杠 realpath 与正斜杠调用方路径必须拼出同一个键，否则
    // ideDebugStore.toggleBreakpoint 判「断点是否还在」会漏，verified 状态残留
    expect(breakpointKey('E:\\p\\app.py', 7)).toBe('E:/p/app.py:7');
    expect(breakpointKey('E:\\p\\app.py', 7)).toBe(breakpointKey('E:/p/app.py', 7));
    expect(breakpointKey('E:/p/A.py', 1)).not.toBe(breakpointKey('E:/p/a.py', 1));
    expect(breakpointKey('E:/p/sub/../a.py', 1)).not.toBe(breakpointKey('E:/p/a.py', 1));
  });
});

describe('BreakpointMarker', () => {
  it('renders a dot element with the breakpoint class', () => {
    const marker = new BreakpointMarker();

    expect(marker.toDOM().className).toBe('cm-ide-breakpoint');
  });

  it('treats markers of the same class as equal so gutters can diff them', () => {
    expect(new BreakpointMarker().eq(new BreakpointMarker())).toBe(true);
  });
});

describe('execLineField', () => {
  it('starts empty', () => {
    expect(makeState().field(execLineField).size).toBe(0);
  });

  it('decorates the line set by setExecLineEffect', () => {
    const state = makeState();

    const updated = state.update({ effects: setExecLineEffect.of(3) }).state;
    const decorations = updated.field(execLineField);
    let found: any = null;
    decorations.between(0, updated.doc.length, (from, _to, value) => {
      found = { from, value };
    });

    expect(decorations.size).toBe(1);
    expect(found.from).toBe(updated.doc.line(3).from);
    expect(found.value.spec.class).toBe('cm-ide-exec-line');
  });

  it('clears the decoration when the line is null or out of range', () => {
    const state = makeState();

    const cleared = state.update({ effects: setExecLineEffect.of(null) }).state;
    expect(cleared.field(execLineField).size).toBe(0);

    const outOfRange = state.update({ effects: setExecLineEffect.of(999) }).state;
    expect(outOfRange.field(execLineField).size).toBe(0);
  });

  it('keeps the decoration anchored to its line when the document changes above it', () => {
    // 执行行高亮活在被调试文件里：文件被外部改动（重载/星瑶改盘）时装饰必须跟着行走，
    // 否则高亮会停在旧行号上，指着一行无关代码
    const state = makeState();
    const decorated = state.update({ effects: setExecLineEffect.of(3) }).state;

    const inserted = decorated.update({ changes: { from: 0, insert: 'zzz\n' } }).state;

    expect(inserted.field(execLineField).size).toBe(1);
    let from = -1;
    inserted.field(execLineField).between(0, inserted.doc.length, (at) => {
      from = at;
    });
    expect(from).toBe(inserted.doc.line(4).from);
  });

  it('moves the highlight when a new line is set and drops it when cleared', () => {
    const state = makeState();

    const first = state.update({ effects: setExecLineEffect.of(2) }).state;
    const second = first.update({ effects: setExecLineEffect.of(4) }).state;

    let from = -1;
    second.field(execLineField).between(0, second.doc.length, (at) => {
      from = at;
    });
    expect(second.field(execLineField).size).toBe(1);
    expect(from).toBe(second.doc.line(4).from);

    const cleared = second.update({ effects: setExecLineEffect.of(null) }).state;
    expect(cleared.field(execLineField).size).toBe(0);
  });
});

describe('断点 gutter（S3-T11 评审 F1/F2/F3 的可观测用例）', () => {
  it('样式随扩展自带：圆点有尺寸与颜色、执行行有背景（F1）', () => {
    const { view, host } = mountGutter([]);

    // 三个类名此前全仓没有样式定义 → 圆点是无尺寸无颜色的空 span、执行行只挂类名
    const dot = cssRule('.cm-ide-breakpoint');
    expect(dot).toContain('display: inline-block');
    expect(dot).toContain('width: 9px');
    expect(dot).toContain('height: 9px');
    expect(dot).toContain('border-radius: 50%');
    expect(dot).toContain('background: var(--red');

    expect(cssRule('.cm-ide-breakpoint-gutter')).toContain('width: 14px');
    expect(cssRule('.cm-ide-exec-line')).toContain('background: var(--accent-soft');

    view.destroy();
    host.remove();
  });

  it('一个断点都没有时，gutter 里也有隐藏占位撑出基础宽度（F2）', () => {
    const { view, host } = mountGutter([]);

    const cells = [...view.dom.querySelectorAll('.cm-ide-breakpoint-gutter .cm-gutterElement')];
    // initialSpacer 提供的占位元素：没有它 gutter 宽度为 0，
    // domEventHandlers.mousedown 永不触发，用户点不出第一个断点
    expect(cells).toHaveLength(1);
    expect((cells[0] as HTMLElement).style.visibility).toBe('hidden');
    expect((cells[0] as HTMLElement).querySelector('.cm-ide-breakpoint')).toBeTruthy();
    expect(dots(view)).toHaveLength(0);

    view.destroy();
    host.remove();
  });

  it('空事务不重算标记：设断点后必须发 refreshBreakpointsEffect（F3）', () => {
    const lines: number[] = [];
    const { view, host } = mountGutter(lines);

    lines.push(2);
    view.dispatch({});
    const afterEmpty = dots(view).length;

    view.dispatch({ effects: refreshBreakpointsEffect.of(null) });
    expect(dots(view)).toHaveLength(1);

    view.destroy();
    host.remove();

    // 实测结论：`view.dispatch({})` 不会让 CM6 重算 gutter 的 lineMarker，
    // 所以 EditorPane 里那次空 dispatch 是无效的（本断言失败说明 CM 语义已变）
    expect(afterEmpty).toBe(0);
  });

  it('删断点后同样靠 refreshBreakpointsEffect 收起圆点', () => {
    const lines: number[] = [2, 3];
    const { view, host } = mountGutter(lines);

    view.dispatch({ effects: refreshBreakpointsEffect.of(null) });
    expect(dots(view)).toHaveLength(2);

    lines.length = 0;
    view.dispatch({ effects: refreshBreakpointsEffect.of(null) });
    expect(dots(view)).toHaveLength(0);

    view.destroy();
    host.remove();
  });

  it('点击 gutter 会触发 onToggle（jsdom 无布局，行号只断言纳正整数的行号）', () => {
    const seen: number[] = [];
    const { view, host } = mountGutter([], (line) => seen.push(line));

    const gutterDom = view.dom.querySelector('.cm-ide-breakpoint-gutter') as HTMLElement;
    gutterDom.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientY: 0 }));

    expect(seen).toHaveLength(1);
    expect(Number.isInteger(seen[0])).toBe(true);
    expect(seen[0]).toBeGreaterThanOrEqual(1);

    view.destroy();
    host.remove();
  });
});
