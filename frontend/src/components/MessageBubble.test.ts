import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';
import MessageBubble from './MessageBubble.vue';
import { useAppStore, type ChatMessage } from '../stores/appStore';
import type { ToolExecutionCall } from '../utils/toolTrace';


const finishedCommandCall = (id: string): ToolExecutionCall => ({
  id, name: 'execute_command', label: '运行命令 npm test',
  display: {
    category: 'command', action: '运行命令', target: 'npm test', command: 'npm test',
    metrics: {}, error_summary: '', is_skill: false,
  },
  result: { ok: true, summary: '完成', result: 'ok', duration_ms: 100 },
});

function assistantMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    role: 'assistant',
    content: '',
    reasoning_content: '',
    tool_calls: [],
    legacy_diffs: [],
    execution_state: 'completed',
    ...overrides,
  };
}

beforeEach(() => setActivePinia(createPinia()));

describe('MessageBubble', () => {
  it('orders thinking block, execution trace, then markdown answer', () => {
    const msg = assistantMessage({
      reasoning_content: '先想想怎么做',
      content: '最终答案',
      tool_calls: [finishedCommandCall('c1')],
      execution_state: 'completed',
    });
    const wrapper = mount(MessageBubble, { props: { msg } });

    const think = wrapper.get('.think-block').element;
    const trace = wrapper.get('.exec-trace').element;
    const md = wrapper.get('.md-body').element;
    expect(think.compareDocumentPosition(trace) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(trace.compareDocumentPosition(md) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(wrapper.get('.md-body').text()).toContain('最终答案');
    wrapper.unmount();
  });

  it('hides the empty working line after the first tool call arrives', async () => {
    const store = useAppStore();
    store.messages.push(assistantMessage({ execution_state: 'running' }));
    store.running = true;
    // 用 store 内部的响应式代理作为 props，模拟 App.vue 的 v-for 传参
    const msg = store.messages[store.messages.length - 1];
    const wrapper = mount(MessageBubble, { props: { msg } });
    expect(wrapper.find('.working-line').exists()).toBe(true);

    msg.tool_calls!.push({
      id: 'c1', name: 'execute_command', label: '运行命令 npm test',
      started_at_ms: Date.now(),
    });
    await wrapper.vm.$nextTick();
    expect(wrapper.find('.working-line').exists()).toBe(false);
    wrapper.unmount();
  });

  it('renders no legacy tool-card or standalone diff-list', () => {
    const msg = assistantMessage({
      tool_calls: [{
        ...finishedCommandCall('c1'),
        diff: { file_path: 'a.py', diff_text: '--- a.py\n+++ a.py\n-old\n+new' },
      }],
      legacy_diffs: [{ file_path: 'old.py', diff_text: '--- old.py\n+++ old.py\n-x\n+y' }],
      execution_state: 'completed',
    });
    const wrapper = mount(MessageBubble, { props: { msg } });

    expect(wrapper.find('.tool-card').exists()).toBe(false);
    expect(wrapper.find('.diff-list').exists()).toBe(false);
    const trace = wrapper.get('.exec-trace');
    expect(trace.text()).toContain('运行命令');
    expect(trace.text()).toContain('未绑定的历史文件变更');
    wrapper.unmount();
  });
});
