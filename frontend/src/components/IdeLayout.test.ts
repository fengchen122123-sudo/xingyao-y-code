import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { nextTick } from 'vue';
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
import { emitDapOutput } from '../utils/dapBus';
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
    // P6 布局用例要用到的两个工作区接口：文件树拉取与原生「打开文件夹」对话框
    get_file_tree: vi.fn(async () => ({ ok: true, file_tree: [] })),
    select_workspace_folder: vi.fn(async () => ({ ok: false })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// 挂载真 App 时子组件全部 stub（照搬 App.historyPaged.test.ts），但 IdeLayout 保持真身——
// A 用例要断言的正是 App 模板与 IdeLayout / BottomPanel 的终端挂载关系。
// teleport 一并 stub：App 模板已不再用 Teleport 搬对话区（改用 CSS 停靠到 #main
// 右侧），留这个 stub 是防止将来别处再引入 Teleport 时，在分离容器里取不到 target。
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

it('App 不再挂载 IDE 布局：终端只挂 App 自带那一个实例', async () => {
  const app = useAppStore();
  app.terminalPanelOpen = true;

  const wrapper = mountApp();
  await flushPromises();
  // Phase 1 摘掉前端 IDE 入口后 App 不再渲染 IdeLayout，因此 App 只挂自己那一个终端；
  // 不可能出现「App 的终端 + 底部面板的终端」两个 xterm / 两个 PTY 的情况
  expect(terminal.live).toBe(1);
  expect(wrapper.findAllComponents({ name: 'TerminalPane' }).length).toBe(1);
  expect(wrapper.find('.bottom-panel').exists()).toBe(false);
  wrapper.unmount();
  expect(terminal.live).toBe(0);
});

it('收起底部面板时也只有底部面板那一个终端实例，且面板只是隐藏', async () => {
  const ide = useIdeStore();
  ide.setSession('');
  ide.toggleBottomPanel(); // 收起底部面板

  // Phase 1 之后 App 不再渲染 IdeLayout（IDE 入口已摘），本用例直接挂布局组件本身
  const wrapper = mount(IdeLayout);
  await flushPromises();

  // S3-T12：收起改用 v-show 控制可见性——调试控制台的行缓冲活在 DebugPane 里，
  // v-if 卸载会把它连同输出一起丢掉，与「收起期间调试输出不丢」冲突。
  // 这里唯一不变式仍是「不能出现第二个终端实例」：剩下的恰好是底部面板这一个
  //（收起期间只是隐藏，不销毁）。
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

// ── S3-T12：调试控制台页签在 IDE 模式里可达 ────────────────────────────────

it('切到底部面板的「调试控制台」页签能看到调用栈 / 变量 / 监视区域', async () => {
  // 验收前置：DebugPane（S3-T10）交付时全仓没有挂载点，Task 13 的「调试控制台可见」
  // 一直执行不了。这里从真实布局点击页签，验证它真的可达。
  const ide = useIdeStore();
  const wrapper = mount(IdeLayout);
  await flushPromises();

  await wrapper.get('[data-test="bottom-tab-debug"]').trigger('click');

  expect(ide.bottomTab).toBe('debug');
  const pane = wrapper.get('[data-test="debug-pane"]');
  expect(pane.isVisible()).toBe(true);
  expect(pane.text()).toContain('调用栈');
  expect(pane.text()).toContain('变量');
  expect(pane.text()).toContain('监视');
  expect(pane.find('[data-test="debug-console-input"]').exists()).toBe(true);
  wrapper.unmount();
});

it('收起再展开底部面板：调试控制台输出不丢，页签选择也保留', async () => {
  const ide = useIdeStore();
  ide.setBottomTab('debug');
  const wrapper = mount(IdeLayout);
  await flushPromises();

  emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'before close\n' });
  await nextTick();

  ide.toggleBottomPanel();
  await nextTick();
  // 面板收起期间适配器仍在推输出（后端不管前端可不可见）
  emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'while closed\n' });
  await nextTick();

  ide.toggleBottomPanel();
  await nextTick();

  const out = wrapper.get('[data-test="debug-console-output"]').element.textContent ?? '';
  expect(out).toContain('before close');
  expect(out).toContain('while closed');
  expect(ide.bottomTab).toBe('debug');
  wrapper.unmount();
});

// ── P5：活动栏 7 项与左侧面板路由 ────────────────────────────────────────

// 活动栏按钮没有 data-test，按 title 找（title 本身就是设计稿里的中文名，读起来也直观）
function activityBtn(wrapper: ReturnType<typeof mount>, title: string) {
  const btn = wrapper.findAll('.activity-btn').find((b) => b.attributes('title') === title);
  expect(btn, `活动栏缺少「${title}」按钮`).toBeTruthy();
  return btn!;
}

it('切到「运行和调试」挂载 DebugPane，不再出现「将在 S3 接入」的过时占位', async () => {
  // 旧实现里 debug 落进 v-else 分支，只显示「断点与调用栈将在 S3 接入后显示」——
  // DebugPane 早已交付，那句文案会把「点调试没反应」伪装成已完成。
  const wrapper = mount(IdeLayout);
  await flushPromises();

  await activityBtn(wrapper, '运行和调试').trigger('click');
  await flushPromises();

  expect(wrapper.find('[data-test="debug-pane"]').exists()).toBe(true);
  expect(wrapper.text()).not.toContain('将在 S3 接入');
  expect(wrapper.text()).not.toContain('断点与调用栈将在');
  wrapper.unmount();
});

it('切到「搜索」「扩展」显示诚实空态：只说明能力边界，不放伪造的功能控件', async () => {
  const wrapper = mount(IdeLayout);
  await flushPromises();

  await activityBtn(wrapper, '搜索').trigger('click');
  expect(wrapper.find('[data-test="search-empty"]').exists()).toBe(true);
  expect(wrapper.text()).toContain('文件搜索将在后续版本接入');
  // 空态里不得出现输入框：后端没有文件搜索接口（已核查 core/ide/ 与 gui_bridge.py），
  // 放个搜不出东西的输入框比空态更误导
  expect(wrapper.find('.ide-side input').exists()).toBe(false);
  expect(wrapper.find('.ide-side textarea').exists()).toBe(false);

  await activityBtn(wrapper, '扩展').trigger('click');
  expect(wrapper.find('[data-test="extensions-empty"]').exists()).toBe(true);
  expect(wrapper.text()).toContain('扩展市场尚未接入');
  wrapper.unmount();
});

it('活动栏动作项：「设置」打开设置页，「星瑶」聚焦对话输入框，且都不改变当前面板', async () => {
  const store = useAppStore();
  // 设置入口走 openModelSettings（与 Sidebar 一致），它会拉取模型配置；桩掉避免真请求
  installBridge({ get_model_settings: vi.fn(async () => ({})) });
  // attachTo：星瑶按钮靠 document.querySelector 找对话输入框并 focus()，
  // 未挂到 document 上的元素 focus 不会成为 activeElement（jsdom 语义），
  // 这里必须真挂进 body 才能验证"聚焦"这一行为
  const wrapper = mount(IdeLayout, { attachTo: document.body });
  await flushPromises();

  await activityBtn(wrapper, '设置').trigger('click');
  expect(store.settingsOpen).toBe(true);

  // 对话区由 App.vue 渲染、CSS 停靠在 #main 右侧，不在本组件树内。这里在 body 上
  // 手工放一个 #center（含 textarea），模拟它在真实 DOM 里的样子
  const host = document.createElement('div');
  host.id = 'center';
  const textarea = document.createElement('textarea');
  host.appendChild(textarea);
  document.body.appendChild(host);
  await activityBtn(wrapper, '星瑶').trigger('click');
  expect(document.activeElement).toBe(textarea);

  // 动作型项不是面板：左侧仍是资源管理器（文件树），没有被顶成空白分支
  expect(wrapper.text()).toContain('文件');
  expect(wrapper.findAll('.activity-btn.active').map((b) => b.attributes('title'))).toEqual(['资源管理器']);
  host.remove();
  wrapper.unmount();
});

// ── P6：IDE 布局与视觉对齐设计稿（mockups/ide-mode-redesign.html） ──────────

it('无工作区：编辑器区渲染欢迎卡片，且不自动弹目录选择器 / 不拉文件树', async () => {
  const api = installBridge();
  const wrapper = mount(IdeLayout);
  await flushPromises();

  // 欢迎卡片（WorkspaceWelcome）用 data-test 锚点断言存在
  expect(wrapper.find('[data-test="workspace-welcome"]').exists()).toBe(true);
  // ensureIdeWorkspace 的语义：无工作区直接返回——既不弹原生对话框，也不打后端拉树
  expect(api.get_file_tree).not.toHaveBeenCalled();
  expect(api.select_workspace_folder).not.toHaveBeenCalled();
  // 没有工作区时不得渲染文件树/空状态之外的假控件：欢迎卡片里没有可用的「新建项目」等
  expect(wrapper.get('[data-test="welcome-new-project"]').attributes('disabled')).toBeDefined();
  wrapper.unmount();
});

it('有工作区：资源管理器渲染文件树与大纲/时间线区段，不渲染欢迎卡片，也不重复拉树', async () => {
  const store = useAppStore();
  store.workdir = 'D:\\proj';
  store.fileTree = [{ n: 'a.py', d: 0 }, { n: 'core', d: 1 }] as any;
  const api = installBridge();
  const wrapper = mount(IdeLayout);
  await flushPromises();

  expect(wrapper.find('[data-test="explorer-tree"]').exists()).toBe(true);
  expect(wrapper.text()).toContain('a.py');
  expect(wrapper.find('[data-test="workspace-welcome"]').exists()).toBe(false);
  // 树已在内存里：ensureIdeWorkspace 不重复打后端（模式切换是高频操作）
  expect(api.get_file_tree).not.toHaveBeenCalled();

  // 大纲 / 时间线：数据源核查为「没有」，因此只允许出现折叠区段 + 诚实空态
  expect(wrapper.get('[data-test="explorer-outline-empty"]').text()).toContain('语言服务器提供符号后显示');
  expect(wrapper.find('[data-test="explorer-timeline-empty"]').exists()).toBe(false); // 默认折叠
  await wrapper.get('[data-test="explorer-timeline"] .sect-head').trigger('click');
  expect(wrapper.get('[data-test="explorer-timeline-empty"]').text()).toContain('Git 提交历史接入后显示');
  wrapper.unmount();
});

it('资源管理器空状态的「打开文件夹」点击后调用 select_workspace_folder', async () => {
  const api = installBridge();
  const wrapper = mount(IdeLayout);
  await flushPromises();

  const btn = wrapper.get('[data-test="explorer-open-folder"]');
  expect(btn.text()).toContain('打开文件夹');
  await btn.trigger('click');
  await flushPromises();

  expect(api.select_workspace_folder).toHaveBeenCalledTimes(1);
  wrapper.unmount();
});

it('收起对话区后活动栏「星瑶」能恢复，占位节点始终留在 DOM 里', async () => {
  const store = useAppStore();
  const wrapper = mount(IdeLayout);
  await flushPromises();

  // 收起状态在 appStore（收起按钮在 App.vue 的对话头部、恢复入口是本组件的活动栏动作
  // 与右缘浮动按钮），所以这里直接置 store 状态模拟「已收起」
  store.ideChatCollapsed = true;
  await nextTick();

  // 占位节点决定编辑器区右边让出多少宽度，用 v-show 隐藏而不是 v-if 卸载（避免布局抖动）。
  // 这里断言内联 style 而不是 isVisible()——该 helper 在 jsdom 下按元素缓存首次结果，
  // 收起前后读到的值不可靠。
  expect(wrapper.find('[data-test="ide-chat-gap"]').exists()).toBe(true);
  expect(wrapper.get('[data-test="ide-chat-gap"]').attributes('style') ?? '').toContain('display: none');
  // 收起时右缘的浮动恢复按钮出现：只留活动栏图标不够显眼，「收起」会像不可逆操作
  expect(wrapper.find('[data-test="ide-chat-restore"]').exists()).toBe(true);

  // 活动栏「星瑶」必须能恢复展开，否则「收起」是一次性不可逆操作
  await activityBtn(wrapper, '星瑶').trigger('click');
  await nextTick();
  expect(wrapper.get('[data-test="ide-chat-gap"]').attributes('style') ?? '').not.toContain('display: none');
  expect(wrapper.find('[data-test="ide-chat-restore"]').exists()).toBe(false);
  wrapper.unmount();
});

