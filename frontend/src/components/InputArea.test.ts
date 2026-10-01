import { mount, flushPromises } from '@vue/test-utils';
import { nextTick } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import InputArea from './InputArea.vue';
import { useAppStore } from '../stores/appStore';
import { COMPOSER_HEIGHT_KEY } from '../composables/useComposerResize';

beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.removeItem(COMPOSER_HEIGHT_KEY);
});

// jsdom 的 PointerEvent 属性是 getter-only，VTU trigger 直接赋值 clientY 会报错，
// 统一用原生构造器派发（bubbles 保证 window 上的 pointermove/up 监听能收到）
function firePointer(el: EventTarget, type: string, clientY: number) {
  el.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true, clientY, pointerId: 1 }));
}

it('uses the current session state for the send/stop button', () => {
  const store = useAppStore();
  store.currentSessionName = 'session-b';
  store.running = true;
  store.sessionRuntimeByName['session-a'] = {
    messages: [], streamingText: '', thinkingText: '', running: true, subagentTasks: [],
  };
  store.sessionRuntimeByName['session-b'] = {
    messages: [], streamingText: '', thinkingText: '', running: false, subagentTasks: [],
  };

  const wrapper = mount(InputArea);

  // 发送图标已统一为 AppIcon SVG：断言按钮内渲染了内联 svg 且 title 语义不变
  expect(wrapper.get('#send-btn').find('svg').exists()).toBe(true);
  expect(wrapper.get('#send-btn').attributes('title')).toContain('发送');
  wrapper.unmount();
});

it('为键盘快捷键提示提供独立的手机端隐藏标记', () => {
  const wrapper = mount(InputArea);
  const hints = wrapper.get('[data-test="keyboard-hints"]');

  expect(hints.classes()).toContain('keyboard-hints');
  expect(hints.text()).toContain('Enter');
  expect(hints.text()).toContain('Shift+Enter');
  expect(hints.text()).toContain('Esc');
  wrapper.unmount();
});

it('右侧栏展开时根节点带 right-panel-open 标记（CSS 据此隐藏快捷键提示）', async () => {
  const store = useAppStore();
  store.rightPanelCollapsed = true;
  const wrapper = mount(InputArea);

  expect(wrapper.get('#input-area').classes()).not.toContain('right-panel-open');
  store.rightPanelCollapsed = false;
  await wrapper.vm.$nextTick();
  expect(wrapper.get('#input-area').classes()).toContain('right-panel-open');
  wrapper.unmount();
});

it('排队消息显示在输入框上方，提供取消与插队按钮', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-b';
  store.messageQueues['session-b'] = [
    { id: 'q1', text: '排队消息一', attachments: [] },
    { id: 'q2', text: '排队消息二', attachments: [] },
  ];

  const wrapper = mount(InputArea);
  const items = wrapper.findAll('[data-test="queue-item"]');
  expect(items).toHaveLength(2);
  expect(items[0].text()).toContain('排队消息一');
  expect(items[0].find('[data-test="queue-cancel-btn"]').exists()).toBe(true);
  expect(items[0].find('[data-test="queue-jump-btn"]').exists()).toBe(true);

  // 队列清空后排队条整体消失
  store.messageQueues['session-b'] = [];
  await wrapper.vm.$nextTick();
  expect(wrapper.find('[data-test="queue-item"]').exists()).toBe(false);
  wrapper.unmount();
});

it('运行中输入内容按 Enter 排队发送，不再打断当前任务', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-b';
  store.runningSessions = ['session-b'];
  const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined);
  const stopSpy = vi.spyOn(store, 'stopCurrentTurn').mockResolvedValue(undefined);

  const wrapper = mount(InputArea);
  const textarea = wrapper.get('textarea');
  await textarea.setValue('下一条指令');
  await textarea.trigger('keydown', { key: 'Enter' });

  expect(sendSpy).toHaveBeenCalledWith('下一条指令', []);
  expect(stopSpy).not.toHaveBeenCalled();
  wrapper.unmount();
});

it('运行中输入为空时按钮为停止态，点击中断当前回合', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-b';
  store.runningSessions = ['session-b'];
  const stopSpy = vi.spyOn(store, 'stopCurrentTurn').mockResolvedValue(undefined);

  const wrapper = mount(InputArea);
  const btn = wrapper.get('#send-btn');
  expect(btn.attributes('title')).toContain('停止');
  await btn.trigger('click');
  expect(stopSpy).toHaveBeenCalled();
  wrapper.unmount();
});

it('点击撤销按钮把排队消息文本和附件恢复到输入框并取消排队', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-b';
  store.messageQueues['session-b'] = [
    { id: 'q1', text: '要撤销的消息', attachments: ['d:/a.png', 'd:/b.txt'] },
  ];
  const cancelSpy = vi.spyOn(store, 'cancelQueuedMessage').mockResolvedValue(undefined);
  store.getFilesInfo = vi.fn(async (paths: string[]) =>
    paths.map(p => ({ path: p, name: p.split(/[\\/]/).pop()!, ext: 'txt', size: 1, ok: true })));

  const wrapper = mount(InputArea);
  const undoBtn = wrapper.find('[data-test="queue-undo-btn"]');
  expect(undoBtn.exists()).toBe(true);
  await undoBtn.trigger('click');
  await flushPromises();

  // 文本回到输入框
  expect(wrapper.get('textarea').element.value).toBe('要撤销的消息');
  // 从队列移除（复用取消排队）
  expect(cancelSpy).toHaveBeenCalledWith('session-b', 'q1');
  // 附件（图片+文件）一并回到输入框
  expect(wrapper.findAll('.attach-chip')).toHaveLength(2);
  wrapper.unmount();
});

it('拖拽顶边手柄抬高输入框，向下拖以默认高度为下限，松手记忆', async () => {
  const wrapper = mount(InputArea);
  const textareaEl = () => (wrapper.get('textarea').element as HTMLTextAreaElement).style.height;

  // jsdom 无布局：默认高度走 46px 兜底常量
  const handleEl = wrapper.get('[data-test="composer-resize-handle"]').element;
  firePointer(handleEl, 'pointerdown', 100);
  await nextTick();
  expect(wrapper.get('#input-area').classes()).toContain('is-resizing');

  // 向上拖 60px → 默认 46px + 60px
  firePointer(window, 'pointermove', 40);
  await nextTick();
  expect(textareaEl()).toBe('106px');

  // 继续向上拖超过窗口 60% 上限（jsdom innerHeight=768 → 上限 461px）
  firePointer(window, 'pointermove', -400);
  await nextTick();
  expect(textareaEl()).toBe('461px');

  // 向下拖过头 → 回到默认高度即触底
  firePointer(window, 'pointermove', 500);
  await nextTick();
  expect(textareaEl()).toBe('46px');

  // 松手：拖回默认高度视为未拉伸，不写入记忆
  firePointer(window, 'pointerup', 500);
  await nextTick();
  expect(wrapper.get('#input-area').classes()).not.toContain('is-resizing');
  expect(localStorage.getItem(COMPOSER_HEIGHT_KEY)).toBeNull();

  // 再拖高并松手 → 写入 localStorage
  firePointer(handleEl, 'pointerdown', 100);
  firePointer(window, 'pointermove', 40);
  await nextTick();
  firePointer(window, 'pointerup', 40);
  expect(localStorage.getItem(COMPOSER_HEIGHT_KEY)).toBe('106');
  wrapper.unmount();
});

it('挂载时恢复上次记忆的高度，并按当前窗口重新夹取', async () => {
  localStorage.setItem(COMPOSER_HEIGHT_KEY, '200');
  let wrapper = mount(InputArea);
  await nextTick();
  expect((wrapper.get('textarea').element as HTMLTextAreaElement).style.height).toBe('200px');
  wrapper.unmount();

  // 存储的上限按当前窗口夹取（461px），低于默认高度则视为未拉伸
  localStorage.setItem(COMPOSER_HEIGHT_KEY, '99999');
  wrapper = mount(InputArea);
  await nextTick();
  expect((wrapper.get('textarea').element as HTMLTextAreaElement).style.height).toBe('461px');
  wrapper.unmount();

  localStorage.setItem(COMPOSER_HEIGHT_KEY, '30');
  wrapper = mount(InputArea);
  await nextTick();
  expect((wrapper.get('textarea').element as HTMLTextAreaElement).style.height).toBe('');
  wrapper.unmount();
});

it('双击手柄恢复默认高度并清除记忆', async () => {
  localStorage.setItem(COMPOSER_HEIGHT_KEY, '200');
  const wrapper = mount(InputArea);
  await nextTick();
  expect((wrapper.get('textarea').element as HTMLTextAreaElement).style.height).toBe('200px');

  await wrapper.get('[data-test="composer-resize-handle"]').trigger('dblclick');

  expect((wrapper.get('textarea').element as HTMLTextAreaElement).style.height).toBe('');
  expect(localStorage.getItem(COMPOSER_HEIGHT_KEY)).toBeNull();
  wrapper.unmount();
});

it('切换会话时分别显示各自尚未发送的文字草稿', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  const wrapper = mount(InputArea);
  const textarea = wrapper.get('textarea');
  await textarea.setValue('会话 A 草稿');

  store.currentSessionName = 'session-b';
  await nextTick();
  expect(wrapper.get('textarea').element.value).toBe('');

  await wrapper.get('textarea').setValue('会话 B 草稿');
  store.currentSessionName = 'session-a';
  await nextTick();
  expect(wrapper.get('textarea').element.value).toBe('会话 A 草稿');
  wrapper.unmount();
});

it('输入组件因设置页卸载后重新挂载仍恢复当前会话草稿', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  let wrapper = mount(InputArea);
  await wrapper.get('textarea').setValue('设置往返后仍保留');
  wrapper.unmount();

  wrapper = mount(InputArea);
  expect(wrapper.get('textarea').element.value).toBe('设置往返后仍保留');
  wrapper.unmount();
});

// —— 上下文用量面板：悬停圆环打开 / 延迟关闭 / 面板内保持 ——

function mountInput() {
  return mount(InputArea, { global: { stubs: { teleport: true } } });
}

it('悬停上下文圆环打开用量面板，移出后延迟关闭', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  try {
    const store = useAppStore();
    store.tokenUsage = { ...store.tokenUsage, used: 12345, window: 128000 };
    const wrapper = mountInput();
    const ring = wrapper.get('[data-test="context-ring"]');

    // 原生 title 已移除，改为悬停详情面板
    expect(ring.attributes('title')).toBeUndefined();

    await ring.trigger('mouseenter');
    expect(wrapper.get('.ctx-usage-panel').classes()).toContain('show');
    expect(wrapper.get('[data-test="ctx-usage-summary"]').text())
      .toBe('12,345 / 128,000（9.6%）');

    // 移出圆环：延迟期内仍显示（留给鼠标移入面板的过渡时间）
    await ring.trigger('mouseleave');
    expect(wrapper.get('.ctx-usage-panel').classes()).toContain('show');

    vi.advanceTimersByTime(300);
    await nextTick();
    expect(wrapper.get('.ctx-usage-panel').classes()).not.toContain('show');
    wrapper.unmount();
  } finally {
    vi.useRealTimers();
  }
});

it('鼠标移入面板取消关闭，移出面板后延迟关闭', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  try {
    const wrapper = mountInput();
    const ring = wrapper.get('[data-test="context-ring"]');
    await ring.trigger('mouseenter');
    await ring.trigger('mouseleave');

    const panel = wrapper.get('.ctx-usage-panel');
    await panel.trigger('mouseenter');
    vi.advanceTimersByTime(300);
    await nextTick();
    expect(panel.classes()).toContain('show');

    await panel.trigger('mouseleave');
    vi.advanceTimersByTime(300);
    await nextTick();
    expect(wrapper.get('.ctx-usage-panel').classes()).not.toContain('show');
    wrapper.unmount();
  } finally {
    vi.useRealTimers();
  }
});

it('触摸设备点按上下文圆环切换面板', async () => {
  const originalMatchMedia = window.matchMedia;
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (query: string) => ({ matches: query.includes('pointer'), media: query }),
  });
  try {
    const wrapper = mountInput();
    const ring = wrapper.get('[data-test="context-ring"]');

    await ring.trigger('click');
    expect(wrapper.get('.ctx-usage-panel').classes()).toContain('show');

    await ring.trigger('click');
    expect(wrapper.get('.ctx-usage-panel').classes()).not.toContain('show');
    wrapper.unmount();
  } finally {
    Object.defineProperty(window, 'matchMedia', { configurable: true, value: originalMatchMedia });
  }
});

it('renders the git branch chip and opens the git panel on click', async () => {
  const { useGitStore } = await import('../stores/gitStore');
  const store = useAppStore();
  const gitStore = useGitStore();
  gitStore.snapshot = {
    branch_name: 'dev', head_state: 'clean', head_oid: 'abcd1234',
    ahead: 0, behind: 0, staged: [], unstaged: [], untracked: [],
    conflicts: [], operation_kind: 'idle',
  } as any;

  const wrapper = mount(InputArea);
  const chip = wrapper.get('[data-test="composer-git-branch"]');
  // 复用状态栏文案（去 ⎇ 前缀）：分支名 + 脏标记
  expect(chip.text()).toContain('dev');

  store.rightPanelCollapsed = true;
  const nonceBefore = gitStore.panelOpenNonce;
  await chip.trigger('click');
  expect(gitStore.panelOpenNonce).toBe(nonceBefore + 1);
  expect(store.rightPanelCollapsed).toBe(false);
  wrapper.unmount();
});

it('hides the git branch chip outside a git workspace', async () => {
  const { useGitStore } = await import('../stores/gitStore');
  useGitStore().snapshot = null;
  const wrapper = mount(InputArea);
  expect(wrapper.find('[data-test="composer-git-branch"]').exists()).toBe(false);
  wrapper.unmount();
});

// —— QQ2009 皮肤专属工具栏行：默认主题不渲染，仅真实功能按钮 ——

it('默认主题下不渲染 QQ2009 工具栏行', () => {
  const wrapper = mount(InputArea);
  expect(wrapper.find('[data-test="qq09-toolbar"]').exists()).toBe(false);
  wrapper.unmount();
});

it('QQ2009 主题下渲染工具栏行，两个附件按钮都调用真实的附件入口', async () => {
  const store = useAppStore();
  store.guiTheme = { id: 'builtin.qq2009' } as any;
  // 附件入口最终落点是 store.pickFiles（系统文件对话框），断言真实函数被调用
  const pickSpy = vi.spyOn(store, 'pickFiles').mockResolvedValue([]);

  const wrapper = mount(InputArea);
  const toolbar = wrapper.get('[data-test="qq09-toolbar"]');
  // 动作清单：图片 + 文件（星瑶无独立图片菜单，两者共用同一附件入口）
  const buttons = toolbar.findAll('.qq09-tb-btn');
  expect(buttons).toHaveLength(2);
  expect(buttons[0].attributes('title')).toContain('图片');
  expect(buttons[1].attributes('title')).toContain('文件');
  // 图标为 inline SVG（stroke 风格），非 emoji
  expect(buttons[0].find('svg').exists()).toBe(true);

  await buttons[0].trigger('click');
  await buttons[1].trigger('click');
  await flushPromises();
  expect(pickSpy).toHaveBeenCalledTimes(2);
  wrapper.unmount();
});

it('切换到 QQ2009 主题时工具栏行实时出现，切回默认主题后消失', async () => {
  const store = useAppStore();
  const wrapper = mount(InputArea);
  expect(wrapper.find('[data-test="qq09-toolbar"]').exists()).toBe(false);

  store.guiTheme = { id: 'builtin.qq2009' } as any;
  await nextTick();
  expect(wrapper.find('[data-test="qq09-toolbar"]').exists()).toBe(true);

  store.guiTheme = { id: 'builtin.dark' } as any;
  await nextTick();
  expect(wrapper.find('[data-test="qq09-toolbar"]').exists()).toBe(false);
  wrapper.unmount();
});
