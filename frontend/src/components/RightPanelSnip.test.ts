import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { nextTick } from 'vue';
import { mount } from '@vue/test-utils';

import RightPanelSnip from './RightPanelSnip.vue';
import { useAppStore } from '../stores/appStore';

// 画笔截图覆盖层：冻结帧按面板进入时的尺寸 1:1 铺回，裁剪时按
// naturalWidth/rootW 反算物理像素。面板尺寸一变（拖拽宽度手柄只改
// store.rightPanelWidth，不派发 window.resize），必须退出重拍，否则导出区域
// 与所见不符。

beforeEach(() => {
  setActivePinia(createPinia());
});

it('拖拽面板宽度（store.rightPanelWidth 变化）时退出截图，避免冻结帧错位', async () => {
  const store = useAppStore();
  store.rightPanelWidth = 360;
  const wrapper = mount(RightPanelSnip, { props: { snapshot: 'data:image/png;base64,AAAA' } });
  expect(wrapper.emitted('close')).toBeUndefined();

  store.rightPanelWidth = 420;
  await nextTick();
  expect(wrapper.emitted('close')).toBeTruthy();

  wrapper.unmount();
});

it('面板折叠态变化同样退出截图（尺寸门控与窗口 resize 同口径）', async () => {
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanelSnip, { props: { snapshot: 'data:image/png;base64,AAAA' } });
  expect(wrapper.emitted('close')).toBeUndefined();

  store.rightPanelCollapsed = true;
  await nextTick();
  expect(wrapper.emitted('close')).toBeTruthy();

  wrapper.unmount();
});

it('窗口 resize 仍退出截图（既有行为保持）', async () => {
  const wrapper = mount(RightPanelSnip, { props: { snapshot: 'data:image/png;base64,AAAA' } });
  window.dispatchEvent(new Event('resize'));
  await nextTick();
  expect(wrapper.emitted('close')).toBeTruthy();
  wrapper.unmount();
});
