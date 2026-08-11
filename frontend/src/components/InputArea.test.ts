import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
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

  expect(wrapper.get('#send-btn').text()).toBe('↑');
  expect(wrapper.get('#send-btn').attributes('title')).toContain('发送');
  wrapper.unmount();
});
