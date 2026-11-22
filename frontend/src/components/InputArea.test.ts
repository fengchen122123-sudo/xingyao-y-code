import { mount, flushPromises, type VueWrapper } from '@vue/test-utils';
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

function firePointer(el: EventTarget, type: string, clientY: number) {
  el.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true, clientY, pointerId: 1 }));
}

// 富文本编辑器是 contenteditable，没有 setValue：把文本写进 DOM 再派发 input 事件，
// 走的正是真实输入路径（组件在 input 时序列化并写回 composerText）
async function typeInto(wrapper: VueWrapper<any>, text: string) {
  const editor = wrapper.get('.ce-input');
  editor.element.textContent = text;
  await editor.trigger('input');
  return editor;
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
  // Esc 不再承担中断任务的语义，提示里也不应再出现（防止误触杀回合）
  expect(hints.text()).not.toContain('Esc');
  wrapper.unmount();
});

it('运行中按 Esc 不会中断当前任务（防误触）', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-b';
  store.runningSessions = ['session-b'];
  const stopSpy = vi.spyOn(store, 'stopCurrentTurn').mockResolvedValue(undefined);

  const wrapper = mount(InputArea);
  const editor = await typeInto(wrapper, '改到一半的想法');
  await editor.trigger('keydown', { key: 'Escape' });
  await editor.trigger('keydown', { key: 'Escape' });

  expect(stopSpy).not.toHaveBeenCalled();
  // 输入内容原样保留，不会因为按 Esc 被清空或提交
  expect(editor.element.textContent).toBe('改到一半的想法');
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
  const editor = await typeInto(wrapper, '下一条指令');
  expect(wrapper.get('#send-btn').classes()).toContain('active');
  await editor.trigger('keydown', { key: 'Enter' });

  expect(sendSpy).toHaveBeenCalledWith('下一条指令', []);
  expect(stopSpy).not.toHaveBeenCalled();
  expect(editor.element.textContent).toBe('');
  expect(store._getSessionRuntime().composerText).toBe('');
  expect(wrapper.get('#send-btn').classes()).toContain('stop');
  wrapper.unmount();
});

it('空闲时按 Enter 发送消息后清空输入框与 composerText', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-b';
  const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined);

  const wrapper = mount(InputArea);
  const editor = await typeInto(wrapper, '为什么会被拦住');
  await editor.trigger('keydown', { key: 'Enter' });

  expect(sendSpy).toHaveBeenCalledWith('为什么会被拦住', []);
  expect(editor.element.textContent).toBe('');
  expect(editor.classes()).toContain('is-empty');
  expect(store._getSessionRuntime().composerText).toBe('');
  wrapper.unmount();
});

it('打开斜杠补全面板时 ArrowDown 高亮不被 input 事件重置，Enter 选中后不被回滚', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-b';
  store.skills = [
    { id: 's1', name: 'A', short_id: 'alpha', description: 'desc1', enabled: true } as any,
    { id: 's2', name: 'B', short_id: 'beta', description: 'desc2', enabled: true } as any,
  ];
  const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined);

  const wrapper = mount(InputArea, { attachTo: document.body });
  const editor = await typeInto(wrapper, '/');
  expect(wrapper.findAll('.sg-item')).toHaveLength(2);

  await editor.trigger('keydown', { key: 'ArrowDown' });
  await editor.trigger('keyup', { key: 'ArrowDown' });
  const items = wrapper.findAll('.sg-item');
  expect(items[1].classes()).toContain('active');

  await editor.trigger('keydown', { key: 'Enter' });
  expect(sendSpy).not.toHaveBeenCalled();
  expect(editor.find('.ce-chip-skill').exists()).toBe(true);
  expect(editor.find('.ce-chip-skill .ce-chip-label').text()).toBe('beta');
  expect(editor.find('.ce-chip-skill .ce-chip-icon polygon').exists()).toBe(true);
  expect(editor.element.textContent).toBe('beta ');
  expect(store._getSessionRuntime().composerText).toBe('/beta ');
  wrapper.unmount();
});

it('斜杠面板技能项渲染星盘图标，选中技能后像对话一样显示为胶囊且删除一次全删', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-b';
  store.slashCommands = [
    { name: '/goal', usage: '/goal <目标>', summary: '目标模式', kind: 'hybrid' } as any,
  ];
  store.skills = [
    { id: 's1', name: '公众号排版', short_id: 'gzh-design', description: '排版引擎', enabled: true } as any,
  ];
  const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined);
  (window as any).pywebview = {
    api: {
      search_sessions: vi.fn().mockResolvedValue({
        ok: true,
        sessions: [{ name: '缓存改造', count: 3, updated_at: '', workdir: '' }],
      }),
      search_workspace_files: vi.fn().mockResolvedValue({ files: [] }),
    },
  };

  try {
    const wrapper = mount(InputArea, { attachTo: document.body });
    const editor = await typeInto(wrapper, '@缓存');
    await flushPromises();
    await editor.trigger('keydown', { key: 'Tab' });
    expect(editor.find('.ce-chip-session').exists()).toBe(true);

    const el = editor.element as HTMLElement;
    el.appendChild(document.createTextNode('/g'));
    const range = document.createRange();
    range.selectNodeContents(el);
    range.collapse(false);
    const sel = document.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);
    await editor.trigger('input');
    const sgItems = wrapper.findAll('.sg-item');
    expect(sgItems).toHaveLength(2);
    expect(sgItems[0].find('.sg-icon').text()).toBe('⌘');
    expect(sgItems[1].find('.sg-icon svg polygon').exists()).toBe(true);

    await editor.trigger('keydown', { key: 'ArrowDown' });
    await editor.trigger('keydown', { key: 'Enter' });
    expect(sendSpy).not.toHaveBeenCalled();
    expect(editor.find('.ce-chip-session').exists()).toBe(true);
    expect(editor.find('.ce-chip-skill').exists()).toBe(true);
    expect(editor.find('.ce-chip-skill .ce-chip-label').text()).toBe('gzh-design');
    expect(store._getSessionRuntime().composerText).toBe('@{缓存改造} /gzh-design ');

    const skillChip = el.querySelector('.ce-chip-skill') as HTMLElement;
    skillChip.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
    await editor.trigger('keydown', { key: 'Backspace' });
    expect(editor.find('.ce-chip-skill').exists()).toBe(false);
    expect(editor.find('.ce-chip-session').exists()).toBe(true);
    expect(store._getSessionRuntime().composerText).toBe('@{缓存改造}  ');
    wrapper.unmount();
  } finally {
    delete (window as any).pywebview;
  }
});

it('斜杠面板选中内置命令（如 /goal）后渲染为胶囊，删除时整块删除，发送仍保留原始 /goal', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-b';
  store.slashCommands = [
    { name: '/goal', usage: '/goal <目标>', summary: '目标模式', kind: 'hybrid' } as any,
  ];
  const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined);

  const wrapper = mount(InputArea, { attachTo: document.body });
  const editor = await typeInto(wrapper, '/go');
  expect(wrapper.findAll('.sg-item')).toHaveLength(1);

  await editor.trigger('keydown', { key: 'Enter' });
  expect(sendSpy).not.toHaveBeenCalled();
  expect(editor.find('.ce-chip-command').exists()).toBe(true);
  expect(editor.find('.ce-chip-command .ce-chip-label').text()).toBe('goal');
  expect(store._getSessionRuntime().composerText).toBe('/goal ');

  const el = editor.element as HTMLElement;
  const range = document.createRange();
  range.selectNodeContents(el);
  range.collapse(false);
  const sel = document.getSelection();
  sel?.removeAllRanges();
  sel?.addRange(range);
  el.lastChild!.textContent = '';
  await editor.trigger('input');
  await editor.trigger('keydown', { key: 'Backspace' });
  expect(editor.find('.ce-chip-command').exists()).toBe(false);
  expect(store._getSessionRuntime().composerText).toBe('');

  el.textContent = '/go';
  const r2 = document.createRange();
  r2.selectNodeContents(el);
  r2.collapse(false);
  sel?.removeAllRanges();
  sel?.addRange(r2);
  await editor.trigger('input');
  await editor.trigger('keydown', { key: 'Enter' });
  expect(editor.find('.ce-chip-command').exists()).toBe(true);
  await editor.trigger('keydown', { key: 'Enter' });
  expect(sendSpy).toHaveBeenCalledWith('/goal ', []);
  expect(editor.find('.ce-chip-command').exists()).toBe(false);
  wrapper.unmount();
});

it('@ 引用补全后渲染为胶囊，随后按 Enter 发送可完整清空输入框', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-b';
  const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined);
  (window as any).pywebview = {
    api: {
      search_sessions: vi.fn().mockResolvedValue({
        ok: true,
        sessions: [{ name: '缓存改造', count: 3, updated_at: '', workdir: '' }],
      }),
      search_workspace_files: vi.fn().mockResolvedValue({ files: [] }),
    },
  };

  try {
    const wrapper = mount(InputArea, { attachTo: document.body });
    const editor = await typeInto(wrapper, '@缓存');
    await flushPromises();
    expect(wrapper.findAll('.sg-item')).toHaveLength(1);

    await editor.trigger('keydown', { key: 'Tab' });
    expect(sendSpy).not.toHaveBeenCalled();
    expect(store._getSessionRuntime().composerText).toBe('@{缓存改造} ');
    expect(editor.find('.ce-chip-session').exists()).toBe(true);

    await editor.trigger('keydown', { key: 'Enter' });
    expect(sendSpy).toHaveBeenCalledWith('@{缓存改造} ', []);
    expect(store._getSessionRuntime().composerText).toBe('');
    expect(editor.element.textContent).toBe('');
    expect(editor.find('.ce-chip').exists()).toBe(false);
    wrapper.unmount();
  } finally {
    delete (window as any).pywebview;
  }
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

  expect(wrapper.get('.ce-input').element.textContent).toBe('要撤销的消息');
  expect(cancelSpy).toHaveBeenCalledWith('session-b', 'q1');
  expect(wrapper.findAll('.attach-chip')).toHaveLength(2);
  wrapper.unmount();
});

it('拖拽顶边手柄抬高输入框，向下拖以默认高度为下限，松手记忆', async () => {
  const wrapper = mount(InputArea);
  const textareaEl = () => (wrapper.get('.ce-input').element as HTMLElement).style.height;

  // jsdom 无布局：默认高度走 46px 兜底常量
  const handleEl = wrapper.get('[data-test="composer-resize-handle"]').element;
  firePointer(handleEl, 'pointerdown', 100);
  await nextTick();
  expect(wrapper.get('#input-area').classes()).toContain('is-resizing');

  firePointer(window, 'pointermove', 40);
  await nextTick();
  expect(textareaEl()).toBe('106px');

  firePointer(window, 'pointermove', -400);
  await nextTick();
  expect(textareaEl()).toBe('461px');

  firePointer(window, 'pointermove', 500);
  await nextTick();
  expect(textareaEl()).toBe('46px');

  firePointer(window, 'pointerup', 500);
  await nextTick();
  expect(wrapper.get('#input-area').classes()).not.toContain('is-resizing');
  expect(localStorage.getItem(COMPOSER_HEIGHT_KEY)).toBeNull();

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
  expect((wrapper.get('.ce-input').element as HTMLElement).style.height).toBe('200px');
  wrapper.unmount();

  localStorage.setItem(COMPOSER_HEIGHT_KEY, '99999');
  wrapper = mount(InputArea);
  await nextTick();
  expect((wrapper.get('.ce-input').element as HTMLElement).style.height).toBe('461px');
  wrapper.unmount();

  localStorage.setItem(COMPOSER_HEIGHT_KEY, '30');
  wrapper = mount(InputArea);
  await nextTick();
  expect((wrapper.get('.ce-input').element as HTMLElement).style.height).toBe('');
  wrapper.unmount();
});

it('双击手柄恢复默认高度并清除记忆', async () => {
  localStorage.setItem(COMPOSER_HEIGHT_KEY, '200');
  const wrapper = mount(InputArea);
  await nextTick();
  expect((wrapper.get('.ce-input').element as HTMLElement).style.height).toBe('200px');

  await wrapper.get('[data-test="composer-resize-handle"]').trigger('dblclick');

  expect((wrapper.get('.ce-input').element as HTMLElement).style.height).toBe('');
  expect(localStorage.getItem(COMPOSER_HEIGHT_KEY)).toBeNull();
  wrapper.unmount();
});

it('切换会话时分别显示各自尚未发送的文字草稿', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  const wrapper = mount(InputArea);
  await typeInto(wrapper, '会话 A 草稿');

  store.currentSessionName = 'session-b';
  await nextTick();
  expect(wrapper.get('.ce-input').element.textContent).toBe('');

  await typeInto(wrapper, '会话 B 草稿');
  store.currentSessionName = 'session-a';
  await nextTick();
  expect(wrapper.get('.ce-input').element.textContent).toBe('会话 A 草稿');
  wrapper.unmount();
});

it('输入组件因设置页卸载后重新挂载仍恢复当前会话草稿', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  let wrapper = mount(InputArea);
  await typeInto(wrapper, '设置往返后仍保留');
  wrapper.unmount();

  wrapper = mount(InputArea);
  expect(wrapper.get('.ce-input').element.textContent).toBe('设置往返后仍保留');
  wrapper.unmount();
});

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

    expect(ring.attributes('title')).toBeUndefined();

    await ring.trigger('mouseenter');
    expect(wrapper.get('.ctx-usage-panel').classes()).toContain('show');
    expect(wrapper.get('[data-test="ctx-usage-summary"]').text())
      .toBe('12,345 / 128,000（9.6%）');

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

it('默认主题下不渲染 QQ2009 工具栏行', () => {
  const wrapper = mount(InputArea);
  expect(wrapper.find('[data-test="qq09-toolbar"]').exists()).toBe(false);
  wrapper.unmount();
});

it('QQ2009 主题下渲染工具栏行，两个附件按钮都调用真实的附件入口', async () => {
  const store = useAppStore();
  store.guiTheme = { id: 'builtin.qq2009' } as any;
  const pickSpy = vi.spyOn(store, 'pickFiles').mockResolvedValue([]);

  const wrapper = mount(InputArea);
  const toolbar = wrapper.get('[data-test="qq09-toolbar"]');
  const buttons = toolbar.findAll('.qq09-tb-btn');
  expect(buttons).toHaveLength(2);
  expect(buttons[0].attributes('title')).toContain('图片');
  expect(buttons[1].attributes('title')).toContain('文件');
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
