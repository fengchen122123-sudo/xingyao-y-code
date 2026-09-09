import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import MessageBubble from './MessageBubble.vue';
import { useAppStore, type ChatMessage } from '../stores/appStore';

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

beforeEach(() => {
  setActivePinia(createPinia());
  // 文件链接后置校验会请求后端，测试里短路掉
  vi.spyOn(useAppStore(), 'checkPathsKind').mockResolvedValue([]);
});

describe('MessageBubble 块级渲染', () => {
  it('按稳定块 + 尾部块渲染出多个 .md-block', async () => {
    const wrapper = mount(MessageBubble, {
      props: { msg: assistantMessage({ content: 'a\n\nb\n\nc' }) },
    });
    await flushPromises();
    const blocks = wrapper.findAll('.md-block');
    expect(blocks).toHaveLength(2); // 1 个稳定块 + 1 个尾部块
    expect(wrapper.html()).toContain('a');
    expect(wrapper.html()).toContain('b');
    expect(wrapper.html()).toContain('c');
  });

  it('无空行时只有一个尾部块', async () => {
    const wrapper = mount(MessageBubble, {
      props: { msg: assistantMessage({ content: '一段没有空行的文字' }) },
    });
    await flushPromises();
    expect(wrapper.findAll('.md-block')).toHaveLength(1);
  });

  it('流式追加内容时已完成块的 DOM 节点被复用', async () => {
    const msg = assistantMessage({ content: 'a\n\nb\n\nc', execution_state: 'running' });
    const wrapper = mount(MessageBubble, { props: { msg } });
    await flushPromises();
    const firstBlock = wrapper.findAll('.md-block')[0].element;

    await wrapper.setProps({ msg: { ...msg, content: 'a\n\nb\n\nc\n\nd' } });
    await flushPromises();

    const after = wrapper.findAll('.md-block');
    expect(after[0].element).toBe(firstBlock); // 同一个 DOM 节点，未重建
    expect(wrapper.html()).toContain('d');
  });

  it('空内容不渲染 .md-body', async () => {
    const wrapper = mount(MessageBubble, {
      props: { msg: assistantMessage({ content: '' }) },
    });
    await flushPromises();
    expect(wrapper.find('.md-body').exists()).toBe(false);
  });
});
