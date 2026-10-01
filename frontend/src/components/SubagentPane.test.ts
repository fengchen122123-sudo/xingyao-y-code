import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

import SubagentPane from './SubagentPane.vue';
import { useAppStore } from '../stores/appStore';

// 长任务描述：子代理面板标题默认收 5 行，hover 时用气泡展示全文
const LONG_DESC = `任务：给星瑶 GUI 侧边栏 QQ2009 皮肤用户头像加「点击更换头像」功能。${'补充说明。'.repeat(20)}`;

function detailPayload(description: string) {
  return {
    ok: true,
    task: {
      id: 'sub-1',
      description,
      type: 'implement',
      status: 'running',
      stage: 'thinking',
      elapsed: 5,
      background: false,
      started_at: 1758000000,
      stages: [{ name: 'model', at: 1758000000, detail: '' }],
    },
    tasks: [],
  };
}

async function mountPane(description: string) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const getDetail = vi.fn().mockResolvedValue(detailPayload(description));
  (window as any).pywebview = { api: { get_subagent_task_detail: getDetail } };
  const store = useAppStore();
  store.subagentDetailId = 'sub-1';
  const wrapper = mount(SubagentPane, {
    props: { active: true },
    global: { plugins: [pinia] },
  });
  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="subagent-title"]').exists()).toBe(true);
  });
  return wrapper;
}

// jsdom 不做排版：手动给 scrollHeight/clientHeight 表达“已被截断 / 未截断”
function stubOverflow(el: HTMLElement, scrollHeight: number, clientHeight: number) {
  Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true });
  Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true });
}

function tipElement() {
  return document.body.querySelector('[data-test="subagent-title-tip"]');
}

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('标题被 5 行截断时：hover 弹出气泡显示全文，移出后收起', async () => {
  const wrapper = await mountPane(LONG_DESC);
  const title = wrapper.get('[data-test="subagent-title"]');
  expect(title.text()).toBe(LONG_DESC);
  // 初始不弹气泡
  expect(tipElement()).toBeNull();

  stubOverflow(title.element as HTMLElement, 240, 100);
  await title.trigger('mouseenter');
  expect(tipElement()?.textContent).toBe(LONG_DESC);

  await title.trigger('mouseleave');
  await vi.waitFor(() => {
    expect(tipElement()).toBeNull();
  });
  wrapper.unmount();
});

it('标题未被截断（内容不足 5 行）时不弹气泡', async () => {
  const wrapper = await mountPane('整理测试报告');
  const title = wrapper.get('[data-test="subagent-title"]');
  stubOverflow(title.element as HTMLElement, 20, 20);

  await title.trigger('mouseenter');
  expect(tipElement()).toBeNull();
  wrapper.unmount();
});

it('气泡可悬停：鼠标进入气泡不收起，移出后收起', async () => {
  const wrapper = await mountPane(LONG_DESC);
  const title = wrapper.get('[data-test="subagent-title"]');
  stubOverflow(title.element as HTMLElement, 240, 100);

  await title.trigger('mouseenter');
  const tip = tipElement() as HTMLElement;
  expect(tip).not.toBeNull();

  await title.trigger('mouseleave');
  tip.dispatchEvent(new MouseEvent('mouseenter'));
  // 进入气泡后延时收起被取消：等过收起延时气泡仍在
  await new Promise((resolve) => setTimeout(resolve, 200));
  expect(tipElement()).not.toBeNull();

  tip.dispatchEvent(new MouseEvent('mouseleave'));
  await vi.waitFor(() => {
    expect(tipElement()).toBeNull();
  });
  wrapper.unmount();
});
