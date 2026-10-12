import { mount } from '@vue/test-utils';
import { expect, it } from 'vitest';

import ActivityBar from './ActivityBar.vue';

// 设计稿 mockups/ide-mode-redesign.html 的 .activitybar：自上而下 7 项，设置贴底
const EXPECTED_TITLES = ['资源管理器', '搜索', '源代码管理', '运行和调试', '扩展', '星瑶', '设置'];
const PANEL_IDS = ['files', 'search', 'git', 'debug', 'extensions'];
const ACTION_IDS = ['ai', 'settings'];

function mountBar(model = 'files') {
  return mount(ActivityBar, { props: { model } });
}

it('渲染 7 项活动栏按钮，顺序与中文 title 与设计稿一致，且图标均为已注册键', () => {
  const wrapper = mountBar();
  const buttons = wrapper.findAll('.activity-btn');

  expect(buttons).toHaveLength(7);
  expect(buttons.map((b) => b.attributes('title'))).toEqual(EXPECTED_TITLES);
  // title 是悬停提示，aria-label 供读屏——两者都要有，否则活动栏对无鼠标用户不可用
  expect(buttons.map((b) => b.attributes('aria-label'))).toEqual(EXPECTED_TITLES);
  // AppIcon 对未注册的键渲染空 svg（iconPaths.ts 是纯字符串注册表），
  // 所以这里用「svg 内有内容」反证图标名拼写正确、没用到未注册的图标
  for (const button of buttons) {
    expect(button.find('svg').exists()).toBe(true);
    expect((button.find('svg').element as unknown as HTMLElement).innerHTML).not.toBe('');
  }
});

it('点击面板型项发出 update:model（沿用既有 v-model:model 约定），model 命中时带激活类', async () => {
  const wrapper = mountBar();
  const buttons = wrapper.findAll('.activity-btn');

  // 初始高亮跟随 model
  expect(wrapper.findAll('.activity-btn.active').map((b) => b.attributes('title'))).toEqual(['资源管理器']);

  // 逐项点击：每项都要能被激活（含设计稿新增的 search / extensions）
  for (const id of PANEL_IDS) {
    const index = PANEL_IDS.indexOf(id);
    await buttons[index].trigger('click');
    expect(wrapper.emitted('update:model')?.at(-1)).toEqual([id]);
    // 动作型事件绝不能被面板型点击顺带触发
    expect(wrapper.emitted('action')).toBeUndefined();
  }

  // 切到 search 后重新挂载校验高亮：只有当前面板那一个按钮带 active 类
  const search = mountBar('search');
  expect(search.findAll('.activity-btn.active').map((b) => b.attributes('title'))).toEqual(['搜索']);
  search.unmount();
});

it('点击动作型项只发 action：不发 update:model，也不改变高亮', async () => {
  const wrapper = mountBar('files');
  const buttons = wrapper.findAll('.activity-btn');

  for (const id of ACTION_IDS) {
    const index = EXPECTED_TITLES.indexOf(id === 'ai' ? '星瑶' : '设置');
    await buttons[index].trigger('click');
    expect(wrapper.emitted('action')?.at(-1)).toEqual([id]);
  }

  // 动作型项不是"当前面板"：既不能发 update:model，也不能抢走高亮
  expect(wrapper.emitted('update:model')).toBeUndefined();
  expect(wrapper.findAll('.activity-btn.active').map((b) => b.attributes('title'))).toEqual(['资源管理器']);
  wrapper.unmount();
});
