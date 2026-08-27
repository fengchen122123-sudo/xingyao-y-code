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
