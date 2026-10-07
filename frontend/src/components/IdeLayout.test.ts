import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';

import { LAYOUT_PERSIST_DEBOUNCE_MS } from '../stores/ideStore';

// 终端实例计数：IDE 模式下终端必须**恰好一个**实例——两个 xterm 就等于两个 PTY 会话、
// 两套终端状态。所以这里把它 mock 成带挂载 / 卸载计数的 stub，让 A 用例能直接数实例。
const terminal = vi.hoisted(() => ({ live: 0, mounts: 0 }));

vi.mock('./EditorPane.vue', () => ({
  default: { name: 'EditorPane', template: '<div class="editor-pane-stub" />' },
}));
vi.mock('./TerminalPane.vue', () => ({
  default: {
    name: 'TerminalPane',
    template: '<div class="terminal-stub" />',
    mounted() {
      terminal.live += 1;
      terminal.mounts += 1;
    },
    unmounted() {
      terminal.live -= 1;
    },
  },
}));

import App from '../App.vue';
import IdeLayout from './IdeLayout.vue';
import { useAppStore } from '../stores/appStore';
import { useIdeStore } from '../stores/ideStore';

function installBridge(overrides: Record<string, unknown> = {}) {
  const api = {
    ide_read_file: vi.fn(async (path: string) => ({
      ok: true, path, content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
    })),
    ide_set_open_files: vi.fn(async () => ({ ok: true, count: 1 })),
    ide_write_file: vi.fn(async () => ({ ok: true, hash: 'h2' })),
    ide_get_layout: vi.fn(async () => ({ ok: true, layout: null })),
    ide_save_layout: vi.fn(async (_s: string, layout: unknown) => ({ ok: true, layout })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// 挂载真 App 时子组件全部 stub（照搬 App.historyPaged.test.ts），但 IdeLayout 保持真身——
// A 用例要断言的正是 App 模板与 IdeLayout / BottomPanel 的终端挂载关系。
// Teleport 一并 stub：目标 #ide-chat-host 在分离容器里挂载时还不在 document 中，
// 真 Teleport 会取不到 target；本用例只数终端实例，不关心搬 DOM。
const appStubs = {
  Topbar: true, Sidebar: true, KimiEmptyState: true, TurnGroup: true,
  InputArea: true, RightPanel: true, ConfirmModal: true, UpdateModal: true,
  PermCard: true, ImageLightbox: true, Subpage: true, SettingsPage: true,
  AppIcon: true, MessageNavRail: { template: '<div />', methods: { refresh() {}, jumpToLoaded() {} } },
  RemoteDirBrowser: true, teleport: true,
};

function mountApp() {
  const store = useAppStore();
  store.initialized = true;
  return mount(App, { global: { stubs: appStubs } });
}

beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.clear();
  installBridge();
  terminal.live = 0;
  terminal.mounts = 0;
});

it('没有打开标签时显示空状态，不挂载编辑器', () => {
  const wrapper = mount(IdeLayout);
  expect(wrapper.find('.editor-pane-stub').exists()).toBe(false);
  expect(wrapper.text()).toContain('打开文件');
  wrapper.unmount();
});

it('打开文件后挂载编辑器，底部面板可切换到终端', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  const wrapper = mount(IdeLayout);

  expect(wrapper.find('.editor-pane-stub').exists()).toBe(true);
  expect(wrapper.text()).toContain('终端');

  await wrapper.find('.bottom-tab.terminal').trigger('click');
  expect(wrapper.find('.terminal-stub').exists()).toBe(true);
  wrapper.unmount();
});

it('IDE 布局渲染出调试工具条（运行/调试的唯一入口必须可达）', async () => {
  // S3-T9 评审：DebugToolbar 交付时全仓没有任何挂载点，计划自己的验收条款
  //（Task 13 Step 5「点调试工具条『运行』」）在真实 IDE 模式里执行不了。
  const wrapper = mount(IdeLayout);

  const toolbar = wrapper.find('[data-test="debug-toolbar"]');
  expect(toolbar.exists()).toBe(true);
  // 「运行 / 调试」是工具条上必须可达的两个动作（无活动文件时 disabled，但要在）
  expect(wrapper.find('[data-test="debug-run"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="debug-start"]').exists()).toBe(true);
  // 工具条常驻：没有打开任何标签时也必须在（按钮自己 disabled）
  expect(wrapper.find('.editor-pane-stub').exists()).toBe(false);
  wrapper.unmount();
});

it('IDE 模式下终端只挂载一个实例，且那一个是底部面板的（App 自带的终端必须 v-if 退场）', async () => {
  const app = useAppStore();
  app.terminalPanelOpen = true;

  // 对话模式：App 自带的终端面板挂载一次
  const chat = mountApp();
  await flushPromises();
  expect(terminal.live).toBe(1);
  expect(chat.findAllComponents({ name: 'TerminalPane' }).length).toBe(1);
  chat.unmount();
  expect(terminal.live).toBe(0);

  // 切到 IDE 模式：终端让位给 IdeLayout 的 BottomPanel——同时挂两个即为两个 xterm / 两个 PTY
  app.toggleIdeMode();
  expect(app.ideMode()).toBe(true);
  const ide = mountApp();
  await flushPromises();

  expect(ide.findAllComponents({ name: 'TerminalPane' }).length).toBe(1);
  expect(terminal.live).toBe(1);
  // 唯一的那个实例必须来自 BottomPanel（若把 App 里的 v-if 改回 v-show，这里会变成 2）
  expect(ide.find('.bottom-panel .terminal-stub').exists()).toBe(true);
  ide.unmount();
  expect(terminal.live).toBe(0);
});

it('收起底部面板时也只有底部面板那一个终端实例，且面板只是隐藏', async () => {
  const app = useAppStore();
  app.terminalPanelOpen = true;
  app.toggleIdeMode();
  const ide = useIdeStore();
  ide.setSession('');
  ide.toggleBottomPanel(); // 收起底部面板

  const wrapper = mountApp();
  await flushPromises();

  // S3-T12：收起改用 v-show 控制可见性——调试控制台的行缓冲活在 DebugPane 里，
  // v-if 卸载会把它连同输出一起丢掉，与「收起期间调试输出不丢」冲突。
  // 这里唯一不变式仍是「不能出现第二个终端实例」：App 自带的那份在 IDE 模式下退场，
  // 剩下的恰好是底部面板这一个（收起期间只是隐藏，不销毁）。
  expect(terminal.live).toBe(1);
  expect(wrapper.find('.ide-bottom').isVisible()).toBe(false);
  expect(wrapper.find('.bottom-panel').exists()).toBe(true);
  wrapper.unmount();
  expect(terminal.live).toBe(0);
});

it('恢复布局时快照 active 指向已删除文件：不切到陈旧路径，也不把它写回快照', async () => {
  const gone = 'E:\\p\\gone.py';
  const alive = 'E:\\p\\ok.py';
  const api = installBridge({
    ide_read_file: vi.fn(async (path: string) => (path === gone
      ? { ok: false, msg: '文件不存在' }
      : { ok: true, path, content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1' })),
    ide_get_layout: vi.fn(async () => ({
      ok: true,
      layout: {
        version: 1,
        tabs: [{ path: alive, line: 1 }, { path: gone, line: 2 }],
        active: gone,
        breakpoints: [],
      },
    })),
  });
  // persistLayout 只在有会话名时才落盘：给个会话名才能验证「没把陈旧 active 写回」
  useAppStore().currentSessionName = 's1';
  const ide = useIdeStore();

  const wrapper = mount(IdeLayout);
  await flushPromises();
  await sleep(LAYOUT_PERSIST_DEBOUNCE_MS + 80); // 等防抖窗口结束落盘

  // 读取失败的标签没被建出来
  expect(ide.tabs().map((t) => t.path)).toEqual([alive]);
  // 活动标签落在真恢复出来的那个上，而不是快照里的陈旧值
  expect(ide.activePath()).toBe(alive);

  // 陈旧路径不得被写回快照（否则重开一次脏一次，active 永远指向不存在的文件）
  const last = api.ide_save_layout.mock.calls.at(-1)?.[1] as { active: string; tabs: { path: string }[] } | undefined;
  expect(last?.active).toBe(alive);
  expect(last?.tabs.map((t) => t.path)).toEqual([alive]);
  wrapper.unmount();
});

it('恢复 N 个标签只落盘一次，且末次是完整标签列表与真实 active', async () => {
  const api = installBridge({
    ide_get_layout: vi.fn(async () => ({
      ok: true,
      layout: {
        version: 1,
        tabs: [{ path: 'a.py', line: 1 }, { path: 'b.py', line: 2 }, { path: 'c.py', line: 3 }],
        active: 'a.py',
        breakpoints: [],
      },
    })),
  });
  useAppStore().currentSessionName = 's1';
  const ide = useIdeStore();

  const wrapper = mount(IdeLayout);
  await flushPromises();
  await sleep(LAYOUT_PERSIST_DEBOUNCE_MS + 80);

  // 未防抖时这里会是 ~2N+1 次（activate 一次 + openFile 补写一次 + 末尾 activate）
  expect(api.ide_save_layout).toHaveBeenCalledTimes(1);
  expect(api.ide_save_layout).toHaveBeenCalledWith('s1', {
    version: 1,
    tabs: [
      { path: 'a.py', line: 1 },
      { path: 'b.py', line: 2 },
      { path: 'c.py', line: 3 },
    ],
    active: 'a.py',
    breakpoints: [],
  });
  expect(ide.tabs().length).toBe(3);
  wrapper.unmount();
});

