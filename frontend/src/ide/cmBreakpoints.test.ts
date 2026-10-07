import { describe, expect, it } from 'vitest';
import { EditorState } from '@codemirror/state';

import {
  BreakpointMarker,
  breakpointKey,
  execLineField,
  setExecLineEffect,
} from './cmBreakpoints';

const DOC = 'a\nb\nc\nd\n';

function makeState() {
  return EditorState.create({ doc: DOC, extensions: [execLineField] });
}

describe('breakpointKey', () => {
  it('builds the same key shape the store uses', () => {
    expect(breakpointKey('E:/p/app.py', 7)).toBe('E:/p/app.py:7');
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
