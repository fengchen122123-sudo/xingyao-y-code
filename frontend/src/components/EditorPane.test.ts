import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { nextTick } from 'vue';
import { beforeEach, expect, it, vi } from 'vitest';

vi.mock('../editor/setup', async () => {
  const actual = await vi.importActual<typeof import('../editor/setup')>('../editor/setup');

  // CodeMirror 的 DOM 挂载依赖真实布局测量，jsdom 下会抛
  // `state.facet is not a function`。按计划 Step 1 的意图，这里只做「接线」验证：
  // 文档内容、updateListener、dispatch、selection、destroy 全部记录在替身上。
  function makeDoc(text: string) {
    const lines = text.split('\n');
    const starts: number[] = [];
    let offset = 0;
    for (const line of lines) {
      starts.push(offset);
      offset += line.length + 1;
    }
    return {
      toString: () => text,
      length: text.length,
      lines: lines.length,
      line: (n: number) => ({ from: starts[n - 1] ?? 0, to: (starts[n - 1] ?? 0) + (lines[n - 1] ?? '').length }),
    };
  }

  interface FakeDoc {
    toString(): string;
    length: number;
    lines: number;
    line(n: number): { from: number; to: number };
  }
  interface FakeState {
    doc: FakeDoc;
  }
  type Listener = (update: { docChanged: boolean; state: FakeState }) => void;

  class FakeEditorView {
    static __instances: FakeEditorView[] = [];
    static updateListener = { of: (listener: Listener) => ({ __listener: listener }) };

    state: FakeState;
    focused = false;
    destroyed = false;
    lastSelection: { anchor: number } | null = null;
    listeners: Listener[] = [];

    constructor(config: { state: FakeState; extensions?: unknown[] }) {
      this.state = config.state;
      for (const ext of config.extensions ?? []) {
        const listener = (ext as { __listener?: Listener }).__listener;
        if (listener) this.listeners.push(listener);
      }
      FakeEditorView.__instances.push(this);
    }

    dispatch(spec: { changes?: { insert: string }; selection?: { anchor: number } }) {
      if (spec.changes) {
        this.state = { doc: makeDoc(spec.changes.insert) };
        for (const listener of this.listeners) listener({ docChanged: true, state: this.state });
      } else if (spec.selection) {
        this.lastSelection = spec.selection;
        for (const listener of this.listeners) listener({ docChanged: false, state: this.state });
      }
    }

    focus() {
      this.focused = true;
    }

    destroy() {
      this.destroyed = true;
    }
  }

  return {
    ...actual,
    createEditorState: vi.fn(async (doc: string) => ({ doc: makeDoc(doc) })),
    EditorView: FakeEditorView,
  };
});

// Task 10 导航扩展的接线探针：记录 EditorPane 实际传进去的选项，既能断言「导航扩展
// 真的挂进了编辑器状态」，也能直接驱动 onJump / requestNewName 验证回调接线（
// jsdom 下无法真的按 F12/F2 键去戳 CodeMirror 的 keymap）。
const navCalls = vi.hoisted(() => [] as Array<{
  path: string;
  onJump: (locations: { path: string; line: number; character: number }[]) => void;
  requestNewName: (oldName: string) => string | null;
}>);

vi.mock('../editor/lspNavigation', async () => {
  const actual = await vi.importActual<typeof import('../editor/lspNavigation')>('../editor/lspNavigation');
  return {
    ...actual,
    lspNavigationExtensions: (options: (typeof navCalls)[number]) => {
      navCalls.push(options);
      return actual.lspNavigationExtensions(options);
    },
  };
});

import EditorPane from './EditorPane.vue';
import { buildEditorTheme } from '../editor/theme';
import { createEditorState, EditorView } from '../editor/setup';
import { useIdeStore } from '../stores/ideStore';

interface FakeView {
  state: { doc: { toString(): string } };
  focused: boolean;
  destroyed: boolean;
  lastSelection: { anchor: number } | null;
}

function lastView(): FakeView {
  const instances = (EditorView as unknown as { __instances: FakeView[] }).__instances;
  return instances[instances.length - 1];
}

function api() {
  return (window as any).pywebview.api;
}

beforeEach(() => {
  (EditorView as unknown as { __instances: FakeView[] }).__instances = [];
  navCalls.length = 0;
  setActivePinia(createPinia());
  (window as any).pywebview = {
    api: {
      ide_read_file: vi.fn(async (path: string) => ({
        ok: true, path, content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
      })),
      ide_write_file: vi.fn(async () => ({ ok: true, hash: 'h2' })),
      ide_set_open_files: vi.fn(async () => ({ ok: true, count: 1 })),
    },
  };
});

it('Ctrl+S 触发保存且阻止浏览器默认行为', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  store.updateContent('a.py', 'x = 2\n');
  const wrapper = mount(EditorPane, { props: { path: 'a.py' } });

  const event = new KeyboardEvent('keydown', { key: 's', ctrlKey: true, cancelable: true });
  window.dispatchEvent(event);
  await Promise.resolve();

  expect((window as any).pywebview.api.ide_write_file).toHaveBeenCalled();
  expect(event.defaultPrevented).toBe(true);
  wrapper.unmount();
});

it('文件被外部改动且本地脏时显示提示条', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  store.updateContent('a.py', 'x = 2\n');
  await store.handleExternalChange('a.py');
  const wrapper = mount(EditorPane, { props: { path: 'a.py' } });

  expect(wrapper.text()).toContain('已被星瑶修改');
  wrapper.unmount();
});

it('挂载时用 store 内容建视图，编辑器输入写回 store 并置脏', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  const wrapper = mount(EditorPane, { props: { path: 'a.py' } });
  await flushPromises();

  const view = lastView();
  expect(view.state.doc.toString()).toBe('x = 1\n');

  (view as unknown as { dispatch: (spec: { changes: { insert: string } }) => void }).dispatch({
    changes: { insert: 'x = 9\n' },
  });
  expect(store.tab('a.py')?.content).toBe('x = 9\n');
  expect(store.tab('a.py')?.dirty).toBe(true);
  wrapper.unmount();
});

it('外部重载把磁盘内容同步进编辑器，且不回声写回 store（applyingExternal 守卫）', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  const wrapper = mount(EditorPane, { props: { path: 'a.py' } });
  await flushPromises();

  api().ide_read_file.mockResolvedValueOnce({
    ok: true, path: 'a.py', content: 'x = 99\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h3',
  });
  await store.reloadFromDisk('a.py');
  await nextTick();

  // 内容真的进了编辑器
  expect(lastView().state.doc.toString()).toBe('x = 99\n');
  // 但没有被当成用户输入回写（否则会立刻变脏 → 无限回声）
  expect(store.tab('a.py')?.dirty).toBe(false);
  expect(store.tab('a.py')?.content).toBe('x = 99\n');
  expect(api().ide_write_file).not.toHaveBeenCalled();
  wrapper.unmount();
});

it('pendingJump 落到选区后清空，其他路径的跳转请求不消费', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  const wrapper = mount(EditorPane, { props: { path: 'a.py' } });
  await flushPromises();

  store.pendingJump = { path: 'b.py', line: 1, character: 1 };
  await nextTick();
  expect(store.pendingJump).not.toBeNull();
  expect(lastView().lastSelection).toBeNull();

  store.pendingJump = { path: 'a.py', line: 2, character: 3 };
  await nextTick();
  expect(store.pendingJump).toBeNull();
  // 'x = 1\n' 第 2 行是空行，锚点被夹到行首（第 1 行末尾 +1 = 6）
  expect(lastView().lastSelection).toEqual({ anchor: 6 });
  expect(lastView().focused).toBe(true);
  wrapper.unmount();
});

it('pendingJump 写的是调用方原路径（与 props.path 形态不同）时同样被消费', async () => {
  // 后端把 E:\p\sub\..\a.py 规范化成 realpath E:\p\a.py
  api().ide_read_file = vi.fn(async () => ({
    ok: true, path: 'E:\\p\\a.py', content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
  }));
  const store = useIdeStore();
  await store.openFile('E:\\p\\sub\\..\\a.py');
  const wrapper = mount(EditorPane, { props: { path: 'E:\\p\\a.py' } });
  await flushPromises();

  // S2 用原始路径写下的跳转请求：严格 !== 比对会让它永远不被消费
  store.pendingJump = { path: 'E:\\p\\sub\\..\\a.py', line: 1, character: 1 };
  await nextTick();
  expect(store.pendingJump).toBeNull();
  expect(lastView().lastSelection).not.toBeNull();
  wrapper.unmount();
});

it('卸载时 destroy 视图并移除 keydown 监听', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  store.updateContent('a.py', 'x = 2\n');
  const wrapper = mount(EditorPane, { props: { path: 'a.py' } });
  await flushPromises();

  const destroy = vi.spyOn(EditorView.prototype, 'destroy');
  wrapper.unmount();
  expect(destroy).toHaveBeenCalled();
  expect(lastView().destroyed).toBe(true);

  const event = new KeyboardEvent('keydown', { key: 's', ctrlKey: true, cancelable: true });
  window.dispatchEvent(event);
  await Promise.resolve();
  expect(api().ide_write_file).not.toHaveBeenCalled();
  expect(event.defaultPrevented).toBe(false);
  destroy.mockRestore();
});

it('提示条按钮：以磁盘为准重载、保留我的内容只清标记、冲突时用我的覆盖', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  store.updateContent('a.py', 'x = 2\n');
  await store.handleExternalChange('a.py');
  const wrapper = mount(EditorPane, { props: { path: 'a.py' } });
  await flushPromises();

  await wrapper.findAll('button')[1].trigger('click'); // 保留我的内容
  expect(store.tab('a.py')?.externalChange).toBe(false);
  expect(store.tab('a.py')?.content).toBe('x = 2\n');

  // 冲突态：用我的覆盖走空 hash 保存
  store.tab('a.py')!.conflict = true;
  store.tab('a.py')!.externalChange = true;
  await nextTick();
  await wrapper.findAll('button')[3].trigger('click');
  expect(api().ide_write_file).toHaveBeenCalledWith('a.py', 'x = 2\n', '', 'utf-8', false, 'lf');
  wrapper.unmount();
});

it('编辑器状态里并列挂上补全/诊断扩展与 4 个导航扩展（悬停/F12/格式化/F2）', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  const wrapper = mount(EditorPane, { props: { path: 'a.py' } });
  await flushPromises();

  // 导航扩展按当前文件实例化，参数口径与 lspExtensions 一致
  expect(navCalls).toHaveLength(1);
  expect(navCalls[0].path).toBe('a.py');

  // createEditorState 的第 3 个参数 = 3 个补全/诊断/视图扩展 + 4 个导航扩展
  const calls = (createEditorState as unknown as { mock: { calls: unknown[][] } }).mock.calls;
  expect(calls[calls.length - 1][2]).toHaveLength(7);
  wrapper.unmount();
});

it('F12 跳转回调只经 store.jumpTo 写跳转请求，不自己动编辑器选区', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  const wrapper = mount(EditorPane, { props: { path: 'a.py' } });
  await flushPromises();

  const selectionBefore = lastView().lastSelection;
  navCalls[0].onJump([{ path: 'b.py', line: 4, character: 2 }]);
  await flushPromises();

  // 目标文件被打开，跳转请求落到 store（定位由待跳转 watcher 独占负责）
  expect(store.tab('b.py')).toBeTruthy();
  expect(store.pendingJump).toEqual({ path: 'b.py', line: 4, character: 2 });
  expect(lastView().lastSelection).toBe(selectionBefore);
  wrapper.unmount();
});

it('F2 重命名的取名回调接到 window.prompt，并把旧名字带进提示', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  const wrapper = mount(EditorPane, { props: { path: 'a.py' } });
  await flushPromises();

  const promptSpy = vi.fn<() => string | null>(() => 'new_name');
  (window as any).prompt = promptSpy;
  expect(navCalls[0].requestNewName('old_name')).toBe('new_name');
  expect(promptSpy).toHaveBeenCalledWith('重命名为', 'old_name');

  // 用户取消（prompt 返回 null）时重命名不生效
  promptSpy.mockReturnValue(null);
  expect(navCalls[0].requestNewName('old_name')).toBeNull();
  wrapper.unmount();
});

it('主题从 CSS 变量构造，浅色/深色都返回可用扩展', () => {
  const dark = buildEditorTheme(false) as unknown[];
  const light = buildEditorTheme(true) as unknown[];
  expect(dark.length).toBeGreaterThan(0);
  expect(light.length).toBeGreaterThan(0);
  document.body.classList.add('light');
  expect((buildEditorTheme(true) as unknown[]).length).toBeGreaterThan(0);
  document.body.classList.remove('light');
});
