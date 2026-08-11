import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import KimiEmptyState from './KimiEmptyState.vue';
import { useAppStore } from '../stores/appStore';

beforeEach(() => setActivePinia(createPinia()));

it('keeps the new conversation send button enabled while another session runs', () => {
  const store = useAppStore();
  store.currentSessionName = '';
  store.running = true;
  store.pendingWorkdir = 'GLOBAL';
  store.sessionRuntimeByName['session-a'] = {
    messages: [], streamingText: '', thinkingText: '', running: true, subagentTasks: [],
  };
  store.sessionRuntimeByName['__pending__:GLOBAL'] = {
    messages: [], streamingText: '', thinkingText: '', running: false, subagentTasks: [],
  };

  const wrapper = mount(KimiEmptyState);

  expect(wrapper.get('.kimi-send-btn').attributes('disabled')).toBeUndefined();
  wrapper.unmount();
});
