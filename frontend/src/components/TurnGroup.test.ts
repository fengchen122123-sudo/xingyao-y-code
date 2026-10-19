import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';
import TurnGroup from './TurnGroup.vue';
import type { TurnGroupInfo } from '../utils/turnGroups';
import type { ChatMessage } from '../stores/appStore';

beforeEach(() => setActivePinia(createPinia()));

function user(text: string): ChatMessage {
  return { role: 'user', content: text };
}

function assistant(opts: Partial<ChatMessage> = {}): ChatMessage {
  return {
    role: 'assistant',
    content: '',
    reasoning_content: '',
    tool_calls: [],
    legacy_diffs: [],
    execution_state: 'completed',
    ...opts,
  };
}

function finishedGroup(): TurnGroupInfo {
  return {
    userMsg: user('帮我修个 bug'),
    middle: [
      assistant({ content: '先看代码', tool_calls: [{ id: 't1' } as any] }),
      assistant({ content: '再跑测试', tool_calls: [{ id: 't2' } as any] }),
    ],
    tail: [assistant({ content: '修复完成' })],
    running: false,
    state: 'completed',
    turnId: '',
  };
}

describe('TurnGroup', () => {
  it('回合结束后中间过程默认折叠，只显示摘要头与最终总结', () => {
    const wrapper = mount(TurnGroup, { props: { group: finishedGroup() } });

    const head = wrapper.get('.fold-head');
    expect(head.attributes('aria-expanded')).toBe('false');
    expect(head.text()).toContain('2 个工具调用');
    expect(head.text()).toContain('已完成');
    // 折叠体存在但不显示
    expect(wrapper.find('.fold-body').exists()).toBe(true);
    expect((wrapper.find('.fold-body').element as HTMLElement).style.display).toBe('none');
    // 最终总结始终可见
    expect(wrapper.text()).toContain('修复完成');
    // 中间过程文字不出现在可见 DOM 文本之外的位置（v-show 隐藏但仍在 DOM，
    // 这里验证总结与摘要头都在，中间内容被 fold-body 包裹）
    expect(wrapper.find('.fold-body').text()).toContain('先看代码');
  });

  it('点击摘要头展开中间过程，再点收起', async () => {
    const wrapper = mount(TurnGroup, { props: { group: finishedGroup() } });

    await wrapper.get('.fold-head').trigger('click');
    expect(wrapper.get('.fold-head').attributes('aria-expanded')).toBe('true');
    expect((wrapper.find('.fold-body').element as HTMLElement).style.display).not.toBe('none');

    await wrapper.get('.fold-head').trigger('click');
    expect(wrapper.get('.fold-head').attributes('aria-expanded')).toBe('false');
  });

  it('回合运行中：无折叠头，中间过程逐条实时显示', () => {
    const group: TurnGroupInfo = {
      userMsg: user('跑个任务'),
      middle: [assistant({ content: '中间轮', execution_state: 'completed' })],
      tail: [assistant({ content: '', execution_state: 'running' })],
      running: true,
      state: 'completed',
      turnId: '',
    };
    const wrapper = mount(TurnGroup, { props: { group } });

    expect(wrapper.find('.fold-head').exists()).toBe(false);
    expect(wrapper.text()).toContain('中间轮');
  });

  it('单轮简单问答（无中间过程）：不显示折叠头', () => {
    const group: TurnGroupInfo = {
      userMsg: user('你好'),
      middle: [],
      tail: [assistant({ content: '你好！' })],
      running: false,
      state: 'completed',
      turnId: '',
    };
    const wrapper = mount(TurnGroup, { props: { group } });

    expect(wrapper.find('.fold-head').exists()).toBe(false);
    expect(wrapper.text()).toContain('你好！');
  });

  it('取消的回合摘要头显示已取消', () => {
    const group = finishedGroup();
    group.state = 'cancelled';
    const wrapper = mount(TurnGroup, { props: { group } });

    expect(wrapper.get('.fold-head').text()).toContain('已取消');
  });

  it('总结轮之后的 error 消息挂在折叠区外始终可见', () => {
    const group = finishedGroup();
    group.tail.push({ role: 'error', content: '导出失败' });
    const wrapper = mount(TurnGroup, { props: { group } });

    expect(wrapper.text()).toContain('导出失败');
    expect(wrapper.find('.fold-body').text()).not.toContain('导出失败');
  });

  it('折叠收起时名字行只落在组尾总结上', () => {
    const wrapper = mount(TurnGroup, { props: { group: finishedGroup() } });

    // 整个回合只有一处「星瑶 · 模型」名字行，且不在折叠体内
    expect(wrapper.findAll('.assistant-name')).toHaveLength(1);
    expect(wrapper.find('.fold-body .assistant-name').exists()).toBe(false);
  });

  it('展开折叠区后名字行移到中间过程首条，仍只有一处', async () => {
    const wrapper = mount(TurnGroup, { props: { group: finishedGroup() } });

    await wrapper.get('.fold-head').trigger('click');

    expect(wrapper.findAll('.assistant-name')).toHaveLength(1);
    expect(wrapper.find('.fold-body .assistant-name').exists()).toBe(true);
  });

  it('回合运行中名字行只出现在中间过程首条', () => {
    const group: TurnGroupInfo = {
      userMsg: user('跑个任务'),
      middle: [
        assistant({ content: '中间轮' }),
        assistant({ content: '第二中间轮' }),
      ],
      tail: [assistant({ content: '', execution_state: 'running' })],
      running: true,
      state: 'completed',
      turnId: '',
    };
    const wrapper = mount(TurnGroup, { props: { group } });

    expect(wrapper.findAll('.assistant-name')).toHaveLength(1);
  });
});
