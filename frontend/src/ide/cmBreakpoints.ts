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

import { pathKey } from '../stores/ideStore';

/**
 * 与 `ideStore.breakpoints` 的键形状一致（`${path}:${line}`）。
 *
 * 路径段必须走 `ideStore.pathKey` 归一化（只统一分隔符）：`ideStore.breakpoints`
 * 与 `ideStore.breakpointsFor` 用同一把尺子，`ideDebugStore.toggleBreakpoint` 用
 * 本函数判「断点是否还在」。旧实现直接拼 `${path}:${line}`：调用方传反斜杠路径
 * （`E:\p\a.py`）时键与归一化后的集合对不上，删断点时 `breakpointStates[key]`
 * 清不掉，verified 状态残留（S3-T11 评审 F4）。
 */
export function breakpointKey(path: string, line: number): string {
  return `${pathKey(path)}:${line}`;
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

/**
 * 断点标记与执行行高亮的样式。
 *
 * 用 `EditorView.baseTheme` 而不是外部 CSS 文件：这三个类名
 * （`cm-ide-breakpoint` / `cm-ide-exec-line` / `cm-ide-breakpoint-gutter`）此前在
 * 全仓库没有任何样式定义——`BreakpointMarker.toDOM()` 产出的是一个无尺寸无颜色的
 * 空 `<span>`（红点不可见），执行行 decoration 只是挂了个类名（高亮不可见），直接
 * 导致计划 Task 13 的手工验收必然失败（S3-T11 评审 F1）。样式与扩展放在一起，谁引入
 * 本模块谁就自带样式，不依赖某个 CSS 文件被正确引入。
 *
 * 颜色走 GUI 主题的 CSS 变量（`frontend/src/style.css` 的 `--red` / `--accent-soft`），
 * 与 `editor/theme.ts` 同一口径，主题切换自动跟随（括号内是变量缺失时的兜底色）。
 */
export const ideDecorationTheme: Extension = EditorView.baseTheme({
  // gutter 必须有非零宽度：CM6 的 gutter 宽度由内容撑开，一个断点都没有时
  // lineMarker 每行都返回 null → gutter 里一个子元素都没有 → 宽度为 0 →
  // domEventHandlers.mousedown 永不触发 → 用户点不出第一个断点（S3-T11 评审 F2）。
  // 固定宽度 + initialSpacer（见 breakpointGutter）双保险。
  '.cm-ide-breakpoint-gutter': { width: '14px' },
  '.cm-ide-breakpoint-gutter .cm-gutterElement': {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
  },
  '.cm-ide-breakpoint': {
    display: 'inline-block',
    width: '9px',
    height: '9px',
    borderRadius: '50%',
    background: 'var(--red, #d9635b)',
  },
  '.cm-ide-exec-line': {
    background: 'var(--accent-soft, rgba(106, 146, 216, 0.28))',
  },
});

/** 设置/清除当前执行行（1-based 行号；null 或越界表示清除）。 */
export const setExecLineEffect = StateEffect.define<number | null>();

/**
 * 请求重算 gutter 的断点标记。
 *
 * 空事务（`view.dispatch({})`）**不会**让 CM6 重算 `lineMarker`（实测见
 * cmBreakpoints.test.ts「断点 gutter」组）：`GutterView.updateGutters` 只在
 * docChanged / heightChanged / viewportChanged / gutterLineClass 变化时才同步，
 * `SingleGutterView.update` 也只在 `markers` 变化或 `lineMarkerChange` 判定为
 * 「可能变了」时才返回 true。断点真源在 store（本模块不持有业务状态），所以增删断点后
 * 必须显式发这个 effect，否则点完 gutter 要等下一次输入 / 滚动才看到红点
 * （S3-T11 评审 F3）。
 */
export const refreshBreakpointsEffect = StateEffect.define<null>();

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
 *
 * 返回值里带上 `ideDecorationTheme`：样式与 gutter 一起交给 EditorView，避免「扩展装了
 * 但样式没引」这种只有肉眼才能发现的断链（S3-T11 评审 F1）。
 */
export function breakpointGutter(options: {
  lines: () => number[];
  onToggle: (line: number) => void;
}): Extension {
  return [
    ideDecorationTheme,
    gutter({
      class: 'cm-ide-breakpoint-gutter',
      // `initialSpacer` 提供 gutter 的基础宽度：即使一个断点都没有，gutter 里也有一个
      // 隐藏的占位标记，鼠标能落在它上面（评审 F2 的另一半保险，配合上面的固定宽度）
      initialSpacer: () => breakpointMarker,
      updateSpacer: (spacer) => spacer,
      lineMarker: (view, line) => {
        const number = view.state.doc.lineAt(line.from).number;
        return options.lines().includes(number) ? breakpointMarker : null;
      },
      // 断点增删只改 store、事务里没有文档变化：靠这条判定认出 refreshBreakpointsEffect，
      // gutter 才会重算标记（空事务不触发重算，见 refreshBreakpointsEffect 的注释）
      lineMarkerChange: (update) =>
        update.transactions.some((tr) => tr.effects.some((e) => e.is(refreshBreakpointsEffect))),
      domEventHandlers: {
        mousedown: (view, line) => {
          options.onToggle(view.state.doc.lineAt(line.from).number);
          return true;
        },
      },
    }),
  ];
}
