import { mount } from '@vue/test-utils';
import { expect, it } from 'vitest';
import { nextTick } from 'vue';
import MessageNavRail from './MessageNavRail.vue';

// MessageNavRail 全量索引模式：navItems 驱动红点渲染（分页懒加载下红点不打折）。

function makeScrollEl(): HTMLElement {
  const el = document.createElement('div');
  Object.defineProperty(el, 'clientHeight', { value: 600, configurable: true });
  Object.defineProperty(el, 'scrollHeight', { value: 2000, configurable: true });
  el.scrollTop = 0;
  document.body.appendChild(el);
  return el;
}

function makeUserMsgEls(n: number): HTMLElement[] {
  return Array.from({ length: n }, (_, i) => {
    const el = document.createElement('div');
    el.className = 'msg-user';
    el.innerHTML = `<span class="user-text">第${i}条</span>`;
    el.getBoundingClientRect = () => ({ top: 100 * (i + 1), height: 40 } as DOMRect);
    return el;
  });
}

const navItems = [
  { id: 10, preview: '第 1 条' },
  { id: 20, preview: '第 2 条' },
  { id: 30, preview: '第 3 条' },
  { id: 40, preview: '第 4 条' },
  { id: 50, preview: '第 5 条' },
];

it('navItems 驱动：DOM 只有 2 条但索引 5 条时渲染 5 个红点', async () => {
  const scrollEl = makeScrollEl();
  const els = makeUserMsgEls(2);
  const wrapper = mount(MessageNavRail, {
    props: {
      getScrollEl: () => scrollEl,
      getUserMsgElements: () => els,
      navItems,
      loadedFirstId: 30,
    },
  });
  await nextTick();
  expect(wrapper.findAll('[data-test="rail-dot"]')).toHaveLength(5);
});

it('已加载分界：loadedFirstId 之前的红点带 dim 淡化态', async () => {
  const scrollEl = makeScrollEl();
  const els = makeUserMsgEls(3);
  const wrapper = mount(MessageNavRail, {
    props: {
      getScrollEl: () => scrollEl,
      getUserMsgElements: () => els,
      navItems,
      loadedFirstId: 30, // 索引第 3 条起已加载 → 前 2 个淡化
    },
  });
  await nextTick();
  const dots = wrapper.findAll('[data-test="rail-dot"]');
  expect(dots[0].classes()).toContain('dim');
  expect(dots[1].classes()).toContain('dim');
  expect(dots[2].classes()).not.toContain('dim');
  expect(dots[4].classes()).not.toContain('dim');
});

it('悬停预览取 navItems 的 preview（未加载消息也有预览）', async () => {
  const scrollEl = makeScrollEl();
  const els = makeUserMsgEls(1);
  const wrapper = mount(MessageNavRail, {
    props: {
      getScrollEl: () => scrollEl,
      getUserMsgElements: () => els,
      navItems,
      loadedFirstId: 50,
    },
    global: { stubs: { teleport: true } },
  });
  await nextTick();
  await wrapper.findAll('[data-test="rail-dot"]')[0].trigger('mouseenter');
  expect(wrapper.find('[data-test="rail-tip"]').text()).toBe('第 1 条');
});

it('点击未加载红点：emit jump 事件交父组件跨页，组件内不 scrollTo', async () => {
  const scrollEl = makeScrollEl();
  scrollEl.scrollTo = () => { throw new Error('不应调用 scrollTo'); };
  const els = makeUserMsgEls(2);
  const wrapper = mount(MessageNavRail, {
    props: {
      getScrollEl: () => scrollEl,
      getUserMsgElements: () => els,
      navItems,
      loadedFirstId: 30,
    },
  });
  await nextTick();
  await wrapper.findAll('[data-test="rail-dot"]')[0].trigger('click');
  const emitted = wrapper.emitted('jump');
  expect(emitted).toHaveLength(1);
  expect(emitted![0][0]).toEqual({ index: 0, id: 10 });
});

it('点击已加载红点：按局部索引组件内 scrollTo，不 emit', async () => {
  const scrollEl = makeScrollEl();
  let scrolled = -1;
  scrollEl.scrollTo = (opts: any) => { scrolled = opts?.top ?? 0; };
  scrollEl.getBoundingClientRect = () => ({ top: 0, height: 600 } as DOMRect);
  const els = makeUserMsgEls(3);
  const wrapper = mount(MessageNavRail, {
    props: {
      getScrollEl: () => scrollEl,
      getUserMsgElements: () => els,
      navItems,
      loadedFirstId: 30, // 已加载局部：index 2/3/4 → local 0/1/2
    },
  });
  await nextTick();
  await wrapper.findAll('[data-test="rail-dot"]')[3].trigger('click');
  expect(wrapper.emitted('jump')).toBeUndefined();
  expect(scrolled).toBeGreaterThanOrEqual(0); // 局部第 1 条（top=200）- 16
  expect(scrolled).toBe(200 - 16);
});

it('无 navItems 时完全回退 DOM 驱动（旧行为）', async () => {
  const scrollEl = makeScrollEl();
  const els = makeUserMsgEls(2);
  const wrapper = mount(MessageNavRail, {
    props: { getScrollEl: () => scrollEl, getUserMsgElements: () => els },
  });
  await nextTick();
  expect(wrapper.findAll('[data-test="rail-dot"]')).toHaveLength(2);
  expect(wrapper.findAll('.dim')).toHaveLength(0);
});
