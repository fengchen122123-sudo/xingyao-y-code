import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import InputArea from './InputArea.vue';
import { useAppStore } from '../stores/appStore';

beforeEach(() => setActivePinia(createPinia()));

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
