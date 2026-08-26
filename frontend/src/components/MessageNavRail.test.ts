import { describe, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import { mount } from '@vue/test-utils';
import MessageNavRail from './MessageNavRail.vue';

function makeEl(top: number, height: number, text?: string): HTMLElement {
  return {
    getBoundingClientRect: () => ({
      top,
      height,
      left: 0,
      right: 0,
      bottom: top + height,
      width: 0,
    }),
    querySelector: (sel: string) => {
      if (sel === '.user-text' && text !== undefined) {
        return { textContent: text } as HTMLElement;
      }
      return null;
    },
  } as unknown as HTMLElement;
}

// 轨道现在会在滚动容器上监听 scroll / 读取视口位置，mock 需补齐这些方法。
// 默认 scrollHeight=clientHeight=600（贴底状态），可通过参数覆盖为非贴底。
function makeScrollEl(overrides: Record<string, number> = {}): HTMLElement {
  return {
    scrollTop: 0,
    scrollHeight: 600,
    clientHeight: 600,
    scrollTo: vi.fn(),
    addEventListener: () => {},
    removeEventListener: () => {},
    getBoundingClientRect: () => ({ top: 0, height: 600 }),
    ...overrides,
  } as unknown as HTMLElement;
}

describe('MessageNavRail', () => {
  it('按用户消息数量渲染对应个数的红点', async () => {
    const els = [makeEl(100, 40), makeEl(300, 40), makeEl(500, 40)];
    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => makeScrollEl(),
        getUserMsgElements: () => els,
      },
    });
    // onMounted 里 refresh 更新计数，等待响应式重渲染后再断言
    await nextTick();

    expect(wrapper.findAll('[data-test="rail-dot"]').length).toBe(3);
  });

  it('悬停红点显示消息内容', async () => {
    const els = [makeEl(100, 40, '帮我查一下库存'), makeEl(300, 40, '好的谢谢')];
    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => makeScrollEl(),
        getUserMsgElements: () => els,
      },
      global: { stubs: { teleport: true } },
    });
    await nextTick();

    const dots = wrapper.findAll('[data-test="rail-dot"]');
    await dots[0].trigger('mouseenter');
    expect(wrapper.find('[data-test="rail-tip"]').text()).toBe('帮我查一下库存');
    await dots[1].trigger('mouseenter');
    expect(wrapper.find('[data-test="rail-tip"]').text()).toBe('好的谢谢');
  });

  it('消息过长时截断显示，不加完整内容', async () => {
    const long = 'x'.repeat(150);
    const els = [makeEl(100, 40, long)];
    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => makeScrollEl(),
        getUserMsgElements: () => els,
      },
      global: { stubs: { teleport: true } },
    });
    await nextTick();

    await wrapper.find('[data-test="rail-dot"]').trigger('mouseenter');
    const tip = wrapper.find('[data-test="rail-tip"]').text();
    expect(tip).toBe(`${'x'.repeat(100)}…`);
    expect(tip.length).toBeLessThan(long.length);
  });

  it('纯图片消息（无文字）退回索引标签', async () => {
    const els = [makeEl(100, 40, '')];
    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => makeScrollEl(),
        getUserMsgElements: () => els,
      },
      global: { stubs: { teleport: true } },
    });
    await nextTick();

    await wrapper.find('[data-test="rail-dot"]').trigger('mouseenter');
    expect(wrapper.find('[data-test="rail-tip"]').text()).toBe('跳转到第 1 条我的消息');
  });

  it('点击红点滚动到对应消息', async () => {
    const els = [makeEl(100, 40), makeEl(500, 40)];
    const scrollEl = makeScrollEl();

    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => scrollEl,
        getUserMsgElements: () => els,
      },
    });
    await nextTick();

    await wrapper.findAll('[data-test="rail-dot"]')[1].trigger('click');
    expect(scrollEl.scrollTo).toHaveBeenCalledWith({ top: 500 - 16, behavior: 'smooth' });
  });

  it('没有用户消息时不渲染轨道', () => {
    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => makeScrollEl(),
        getUserMsgElements: () => [],
      },
    });
    expect(wrapper.find('[data-test="nav-rail"]').exists()).toBe(false);
  });

  it('点击红点立即高亮为当前消息，其余保持浅灰', async () => {
    const els = [makeEl(100, 40), makeEl(300, 40), makeEl(500, 40)];
    const scrollEl = makeScrollEl();
    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => scrollEl,
        getUserMsgElements: () => els,
      },
    });
    await nextTick();

    const dots = wrapper.findAll('[data-test="rail-dot"]');
    await dots[2].trigger('click');
    expect(dots[2].classes()).toContain('active');
    expect(dots[0].classes()).not.toContain('active');
    expect(dots[1].classes()).not.toContain('active');
  });

  it('停在底部时高亮最后一条（顶部在中线下方也算当前）', async () => {
    // 用户滚到底部看最新一条：即使它的顶部没越过中线，也应高亮最后一条
    const els = [makeEl(100, 40), makeEl(200, 40), makeEl(400, 40)];
    const scrollEl = makeScrollEl(); // 默认贴底（scrollHeight==clientHeight）
    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => scrollEl,
        getUserMsgElements: () => els,
      },
    });
    await nextTick();
    await nextTick();

    const dots = wrapper.findAll('[data-test="rail-dot"]');
    expect(dots[2].classes()).toContain('active');
    expect(dots[0].classes()).not.toContain('active');
    expect(dots[1].classes()).not.toContain('active');
  });

  it('浏览历史中按视口中线高亮当前消息', async () => {
    // 非贴底（向上滚了）：中线在 300，第1、2条顶部(100/200)过中线，第3条(400)未过
    const els = [makeEl(100, 40), makeEl(200, 40), makeEl(400, 40)];
    const scrollEl = makeScrollEl({ scrollHeight: 2000, clientHeight: 600, scrollTop: 500 });
    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => scrollEl,
        getUserMsgElements: () => els,
      },
    });
    await nextTick();
    await nextTick();

    const dots = wrapper.findAll('[data-test="rail-dot"]');
    expect(dots[1].classes()).toContain('active');
    expect(dots[2].classes()).not.toContain('active');
  });
});
