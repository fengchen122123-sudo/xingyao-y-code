// CodeMirror 6 断点 gutter 与当前执行行高亮。
//
// 只做“显示 + 点击切换”两件事：断点的唯一真源在 ideStore（规格 5.2），调试会话的
// 停止位置在 ideDebugStore；本模块不持有业务状态，也不自己发桥调用。
// 点击切换的行号用 doc.lineAt 还原为 1-based 行号（gutter 回调给的是文档位置）。

import {
  EditorView,
  Decoration,
  GutterMarker,
  gutter,
  type DecorationSet,
} from '@codemirror/view';
import { StateEffect, StateField, type Extension } from '@codemirror/state';

/** 与 `ideStore.breakpoints` 的键形状一致（`${path}:${line}`）。 */
export function breakpointKey(path: string, line: number): string {
  return `${path}:${line}`;
}

/** 断点标记：一个实心圆点，样式类由主题着色。 */
export class BreakpointMarker extends GutterMarker {
  toDOM(): HTMLElement {
    const dot = document.createElement('span');
    dot.className = 'cm-ide-breakpoint';
    return dot;
  }

  /**
   * 同类标记一律相等。
   *
   * 必须显式覆盖：`GutterMarker.eq` 的默认实现是 `return false`，而 gutter 更新时
   * 用 `compare()`（同类 + `eq`）判断标记是否需要重建 DOM——不覆盖的话每次光标移动、
   * 每次重绘都会把所有断点圆点的 DOM 拆掉重建。
   */
  eq(other: GutterMarker): boolean {
    return other instanceof BreakpointMarker;
  }
}

export const breakpointMarker = new BreakpointMarker();

/** 当前执行行的整行装饰（背景高亮）。 */
export const execLineDecoration = Decoration.line({ class: 'cm-ide-exec-line' });

/** 设置/清除当前执行行（1-based 行号；null 或越界表示清除）。 */
export const setExecLineEffect = StateEffect.define<number | null>();

export const execLineField = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(decorations, transaction) {
    // 先按文档变更重定位已有装饰：文件被外部改动（重载 / 星瑶改盘）时高亮要跟着行走，
    // 否则它会停在旧行号上指着一行无关代码
    let next = decorations.map(transaction.changes);
    for (const effect of transaction.effects) {
      if (!effect.is(setExecLineEffect)) continue;
      const line = effect.value;
      if (line === null || line < 1 || line > transaction.state.doc.lines) {
        next = Decoration.none;
        continue;
      }
      next = Decoration.set([execLineDecoration.range(transaction.state.doc.line(line).from)]);
    }
    return next;
  },
  provide: (field) => EditorView.decorations.from(field),
});

/**
 * 断点 gutter。
 *
 * `lines()` 每次渲染现取（数据真源在 ideStore，本模块不缓存），`onToggle` 把 1-based
 * 行号交给调用方切换。`domEventHandlers.mousedown` 返回 true 表示事件已处理：
 * 点 gutter 不应该顺带把光标移到那一行之外的地方。
 */
export function breakpointGutter(options: {
  lines: () => number[];
  onToggle: (line: number) => void;
}): Extension {
  return gutter({
    class: 'cm-ide-breakpoint-gutter',
    lineMarker: (view, line) => {
      const number = view.state.doc.lineAt(line.from).number;
      return options.lines().includes(number) ? breakpointMarker : null;
    },
    domEventHandlers: {
      mousedown: (view, line) => {
        options.onToggle(view.state.doc.lineAt(line.from).number);
        return true;
      },
    },
  });
}
