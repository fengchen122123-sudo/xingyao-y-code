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

  it('点击红点滚动到对应消息并发出 jumpStart', async () => {
    const els = [makeEl(100, 40), makeEl(500, 40)];
    const scrollEl = makeScrollEl({ scrollHeight: 5000 });

    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => scrollEl,
        getUserMsgElements: () => els,
      },
    });
    await nextTick();

    await wrapper.findAll('[data-test="rail-dot"]')[1].trigger('click');
    expect(scrollEl.scrollTo).toHaveBeenCalledWith({ top: 500 - 16, behavior: 'smooth' });
    // 跳转发起事件：父组件据此显式退出「跟随底部」
    expect(wrapper.emitted('jumpStart')).toBeTruthy();
    wrapper.unmount();
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
    wrapper.unmount();
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

  // —— 定位校正（有界）闭环：真实 DOM 语义脚手架 ——
  // msgEl 的视口 top = 内容坐标(msgTop) - 当前 scrollTop：滚动会让目标向视口上方移动，
  // 目标上方内容异步撑高等价于把内容坐标整体下移。
  function makeJumpHarness() {
    const scrollEl = document.createElement('div');
    Object.defineProperty(scrollEl, 'clientHeight', { value: 600, configurable: true });
    Object.defineProperty(scrollEl, 'scrollHeight', { value: 5000, configurable: true });
    scrollEl.scrollTop = 0;
    scrollEl.getBoundingClientRect = () => ({ top: 0, height: 600 } as DOMRect);
    const scrollCalls: Array<{ top: number; behavior: string }> = [];
    scrollEl.scrollTo = ((opts: any) => {
      scrollCalls.push({ top: opts?.top, behavior: opts?.behavior });
      scrollEl.scrollTop = opts?.top ?? 0;
    }) as unknown as typeof scrollEl.scrollTo;
    document.body.appendChild(scrollEl);

    const msgEl = document.createElement('div');
    msgEl.className = 'msg-user';
    msgEl.innerHTML = '<span class="user-text">目标</span>';
    let msgTop = 3000; // 目标消息在内容坐标系中的位置，撑高时上调它
    msgEl.getBoundingClientRect = () => ({ top: msgTop - scrollEl.scrollTop, height: 40 } as DOMRect);
    document.body.appendChild(msgEl);

    return {
      scrollEl,
      msgEl,
      scrollCalls,
      setMsgTop: (v: number) => { msgTop = v; },
    };
  }

  function cleanup(harness: { scrollEl: HTMLElement; msgEl: HTMLElement }) {
    harness.scrollEl.remove();
    harness.msgEl.remove();
  }

  it('定位后目标漂移：检查点有限校正重新钉回', async () => {
    vi.useFakeTimers();
    const h = makeJumpHarness();
    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => h.scrollEl,
        getUserMsgElements: () => [h.msgEl],
      },
    });
    await nextTick();
    await wrapper.find('[data-test="rail-dot"]').trigger('click');
    // 初始：一次性平滑定位到 3000 - 16
    expect(h.scrollCalls.length).toBe(1);
    expect(h.scrollCalls[0]).toEqual({ top: 3000 - 16, behavior: 'smooth' });

    // 平滑动画期间/之后目标上方内容异步撑高 600px：终点已过时，需有限校正
    h.setMsgTop(3600);
    vi.advanceTimersByTime(800); // 第一个检查点（滚动已停止）
    await nextTick();
    expect(h.scrollCalls.length).toBe(2);
    expect(h.scrollCalls[1]).toEqual({ top: 3600 - 16, behavior: 'auto' });

    // 再推进到兜底：已对齐，不再产生任何校正
    vi.advanceTimersByTime(2000);
    await nextTick();
    expect(h.scrollCalls.length).toBe(2);

    wrapper.unmount();
    cleanup(h);
    vi.useRealTimers();
  });

  it('检查点到达时滚动仍在进行：本次不校正（不打断平滑动画）', async () => {
    vi.useFakeTimers();
    const h = makeJumpHarness();
    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => h.scrollEl,
        getUserMsgElements: () => [h.msgEl],
      },
    });
    await nextTick();
    await wrapper.find('[data-test="rail-dot"]').trigger('click');
    expect(h.scrollCalls.length).toBe(1);

    h.setMsgTop(3600);
    vi.advanceTimersByTime(700);
    // 模拟动画仍在滚动：检查点前 100ms 仍有 scroll 事件
    h.scrollEl.dispatchEvent(new Event('scroll'));
    vi.advanceTimersByTime(100); // 到达 800ms 检查点：距最后 scroll 仅 100ms
    await nextTick();
    // 滚动进行中：不校正（旧实现会在动画启动初期误判"稳定"而打断动画）
    expect(h.scrollCalls.length).toBe(1);

    wrapper.unmount();
    cleanup(h);
    vi.useRealTimers();
  });

  it('目标持续漂移时校正次数有上限（绝不无限校正）', async () => {
    vi.useFakeTimers();
    const h = makeJumpHarness();
    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => h.scrollEl,
        getUserMsgElements: () => [h.msgEl],
      },
    });
    await nextTick();
    await wrapper.find('[data-test="rail-dot"]').trigger('click');
    expect(h.scrollCalls.length).toBe(1);

    h.setMsgTop(3600);
    vi.advanceTimersByTime(800); // 检查点1 → 校正1
    await nextTick();
    h.setMsgTop(4200);
    vi.advanceTimersByTime(700); // 检查点2 → 校正2（达到上限）
    await nextTick();
    h.setMsgTop(4800);
    vi.advanceTimersByTime(3000); // 兜底及以后：不再校正
    await nextTick();

    expect(h.scrollCalls.filter(c => c.behavior === 'auto').length).toBe(2);
    expect(h.scrollCalls.length).toBe(3); // 初始 smooth 1 次 + auto 校正 2 次，到此为止

    wrapper.unmount();
    cleanup(h);
    vi.useRealTimers();
  });

  it('内容不可滚时目标 clamp 到边界：不产生无效校正（防死循环回归）', async () => {
    vi.useFakeTimers();
    const scrollEl = document.createElement('div');
    Object.defineProperty(scrollEl, 'clientHeight', { value: 600, configurable: true });
    Object.defineProperty(scrollEl, 'scrollHeight', { value: 600, configurable: true }); // 不可滚
    scrollEl.scrollTop = 0;
    scrollEl.getBoundingClientRect = () => ({ top: 0, height: 600 } as DOMRect);
    const scrollCalls: Array<{ top: number; behavior: string }> = [];
    scrollEl.scrollTo = ((opts: any) => {
      scrollCalls.push({ top: opts?.top, behavior: opts?.behavior });
      scrollEl.scrollTop = opts?.top ?? 0;
    }) as unknown as typeof scrollEl.scrollTo;
    document.body.appendChild(scrollEl);

    const msgEl = document.createElement('div');
    msgEl.className = 'msg-user';
    msgEl.innerHTML = '<span class="user-text">目标</span>';
    msgEl.getBoundingClientRect = () => ({ top: 20 - scrollEl.scrollTop, height: 40 } as DOMRect);
    document.body.appendChild(msgEl);

    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => scrollEl,
        getUserMsgElements: () => [msgEl],
      },
    });
    await nextTick();
    await wrapper.find('[data-test="rail-dot"]').trigger('click');
    // 目标消息在内容坐标 20 处：raw=4，超出可滚动范围 → clamp 到 0
    expect(scrollCalls.length).toBe(1);
    expect(scrollCalls[0].top).toBe(0);

    // 所有检查点跑完：delta=0 已对齐，无任何额外校正
    //（旧实现在此每帧校正一次、持续到 2 秒超时——"一直闪"的直接根因）
    vi.advanceTimersByTime(3000);
    await nextTick();
    expect(scrollCalls.length).toBe(1);

    wrapper.unmount();
    scrollEl.remove();
    msgEl.remove();
    vi.useRealTimers();
  });

  it('用户滚轮接管：取消后续校正', async () => {
    vi.useFakeTimers();
    const h = makeJumpHarness();
    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => h.scrollEl,
        getUserMsgElements: () => [h.msgEl],
      },
    });
    await nextTick();
    await wrapper.find('[data-test="rail-dot"]').trigger('click');
    expect(h.scrollCalls.length).toBe(1);

    h.setMsgTop(3600);
    // 用户主动滚轮：立即让位，取消跳转检查
    h.scrollEl.dispatchEvent(new Event('wheel'));
    vi.advanceTimersByTime(3000);
    await nextTick();
    expect(h.scrollCalls.length).toBe(1);

    wrapper.unmount();
    cleanup(h);
    vi.useRealTimers();
  });

  it('检查点全部因滚动跳过时：兜底做最后一次校正后收尾', async () => {
    vi.useFakeTimers();
    const h = makeJumpHarness();
    const wrapper = mount(MessageNavRail, {
      props: {
        getScrollEl: () => h.scrollEl,
        getUserMsgElements: () => [h.msgEl],
      },
    });
    await nextTick();
    await wrapper.find('[data-test="rail-dot"]').trigger('click');
    expect(h.scrollCalls.length).toBe(1);

    h.setMsgTop(3600);
    // 800ms 检查点：滚动仍在进行 → 跳过（不打断）
    vi.advanceTimersByTime(700);
    h.scrollEl.dispatchEvent(new Event('scroll'));
    vi.advanceTimersByTime(100);
    await nextTick();
    expect(h.scrollCalls.length).toBe(1);
    // 1500ms 检查点：仍在滚动 → 跳过
    vi.advanceTimersByTime(600);
    h.scrollEl.dispatchEvent(new Event('scroll'));
    vi.advanceTimersByTime(100);
    await nextTick();
    expect(h.scrollCalls.length).toBe(1);
    // 2500ms 兜底：最终校正一次并收尾（动画期撑高的目标被重钉回）
    vi.advanceTimersByTime(1000);
    await nextTick();
    expect(h.scrollCalls.length).toBe(2);
    expect(h.scrollCalls[1]).toEqual({ top: 3600 - 16, behavior: 'auto' });
    // 之后不再有任何校正
    vi.advanceTimersByTime(2000);
    await nextTick();
    expect(h.scrollCalls.length).toBe(2);

    wrapper.unmount();
    cleanup(h);
    vi.useRealTimers();
  });
});
