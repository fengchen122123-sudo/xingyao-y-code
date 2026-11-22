import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';
import TurnGroup from './TurnGroup.vue';
import type { TurnGroupInfo } from '../utils/turnGroups';
import { useAppStore, type ChatMessage } from '../stores/appStore';

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
    expect(wrapper.find('.fold-body').exists()).toBe(true);
    expect((wrapper.find('.fold-body').element as HTMLElement).style.display).toBe('none');
    expect(wrapper.text()).toContain('修复完成');
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

    expect(wrapper.findAll('.assistant-name')).toHaveLength(1);
    expect(wrapper.find('.fold-body .assistant-name').exists()).toBe(false);
  });

  it('展开折叠区后身份行仍固定在顶部且只有一处', async () => {
    const wrapper = mount(TurnGroup, { props: { group: finishedGroup() } });

    await wrapper.get('.fold-head').trigger('click');

    expect(wrapper.findAll('.assistant-name')).toHaveLength(1);
    expect(wrapper.find('.fold-body .assistant-name').exists()).toBe(false);
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

  it('中间过程助手消息不重复渲染性能指标栏，仅末尾结果保留', () => {
    const metrics = { duration_s: 2.34, token_speed: 58.2, completion_tokens: 142 };
    const group: TurnGroupInfo = {
      userMsg: user('跑个任务'),
      middle: [
        assistant({ content: '中间轮 1', metrics: { duration_s: 1.2, token_speed: 40.0, completion_tokens: 50 } }),
        assistant({ content: '中间轮 2', metrics: { duration_s: 1.8, token_speed: 45.0, completion_tokens: 80 } }),
      ],
      tail: [assistant({ content: '最终回复', metrics })],
      running: true,
      state: 'completed',
      turnId: '',
    };
    const wrapper = mount(TurnGroup, { props: { group } });

    const bars = wrapper.findAll('.msg-metrics-bar');
    expect(bars).toHaveLength(1);
    expect(bars[0].text()).toContain('58.2 t/s');
    wrapper.unmount();
  });

  it('回合执行中在过程头和底部显示当前 token 速度', async () => {
    const store = useAppStore();
    store.streamingSpeed = 184.1;
    const group: TurnGroupInfo = {
      userMsg: user('跑个任务'),
      middle: [assistant({ content: '先查一下', tool_calls: [{ id: 't1' } as any], execution_state: 'running' })],
      tail: [assistant({ content: '正在写', execution_state: 'running' })],
      running: true,
      state: 'completed',
      turnId: '',
    };
    const wrapper = mount(TurnGroup, {
      props: { group, runningElapsedText: '19s' },
    });

    expect(wrapper.get('[data-test="running-process-meta"]').text()).toContain('184.1 t/s');
    expect(wrapper.get('[data-test="running-process-meta"]').text()).toContain('19s');
    expect(wrapper.get('[data-test="live-token-speed"]').text()).toBe('184.1 t/s');

    await wrapper.get('.xy-process-head').trigger('click');
    expect(wrapper.get('.xy-shimmer-text').text()).toContain('184.1 t/s');

    store.streamingSpeed = 0;
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="live-token-speed"]').exists()).toBe(false);
    expect(wrapper.get('.xy-shimmer-text').text()).not.toContain('t/s');
    wrapper.unmount();
  });

  it('没有执行过程时，生成中的速度仍显示在回合底部', async () => {
    const store = useAppStore();
    store.streamingSpeed = 50;
    const group: TurnGroupInfo = {
      userMsg: user('你好'),
      middle: [],
      tail: [assistant({ content: '正在回答', execution_state: 'running' })],
      running: true,
      state: 'completed',
      turnId: '',
    };
    const wrapper = mount(TurnGroup, { props: { group } });

    expect(wrapper.find('[data-test="running-process-meta"]').exists()).toBe(false);
    expect(wrapper.get('[data-test="live-token-speed"]').text()).toBe('50 t/s');
    wrapper.unmount();
  });

  it('回合结束后显示任务总时长，右下角时间不用最后一次模型耗时', () => {
    const metrics = { duration_s: 19, token_speed: 184.1, completion_tokens: 2694 };
    const group: TurnGroupInfo = {
      userMsg: user('跑个任务'),
      middle: [assistant({ content: '先编译', tool_calls: [{ id: 't1' } as any] })],
      tail: [assistant({ content: '完成', metrics })],
      running: false,
      state: 'completed',
      turnId: '',
    };
    group.userMsg!.turnElapsedS = 25;
    const wrapper = mount(TurnGroup, { props: { group } });

    expect(wrapper.get('[data-test="turn-total-duration"]').text()).toContain('本轮工作 25s');
    const bar = wrapper.get('[data-test="msg-metrics"]');
    expect(bar.text()).toContain('184.1 t/s');
    expect(bar.text()).toContain('25s');
    expect(bar.text()).not.toContain('19s');
    expect(bar.attributes('title')).toContain('任务总时长: 25s');
    expect(bar.attributes('title')).toContain('模型耗时: 19s');
    wrapper.unmount();
  });
});
