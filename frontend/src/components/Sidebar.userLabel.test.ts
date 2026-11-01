import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import Sidebar from './Sidebar.vue';
import { useAppStore } from '../stores/appStore';

beforeEach(() => {
  localStorage.clear();
});

// 左下角用户卡片：账号名可任意长，卡片只渲染前 6 个字符，完整名字走 title 气泡；
// 关键回归点是右侧设置/手机图标必须仍在（曾被超长名字挤出网格）。
it('账号名超长时只显示前 6 个字符，完整名字在 title 气泡里，右侧图标不被挤走', () => {
  (window as any).pywebview = { api: {} };
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.trAuth.status = 'loggedIn';
  store.trAuth.user = { name: '基元律动请送我token我想要不限量的token' };

  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  const label = wrapper.get('[data-test="side-user-card"] .footer-copy b');
  expect(label.text()).toBe('基元律动请送…');
  expect(label.attributes('title')).toBe('基元律动请送我token我想要不限量的token');
  expect(wrapper.find('[data-test="settings-button"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="remote-bind-button"]').exists()).toBe(true);
});

it('未登录时卡片显示产品名，且不额外挂悬停气泡', () => {
  (window as any).pywebview = { api: {} };
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.trAuth.status = 'anonymous';
  store.trAuth.user = null;

  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  const label = wrapper.get('[data-test="side-user-card"] .footer-copy b');
  expect(label.text()).toBe('星瑶Code');
  expect(label.attributes('title')).toBeUndefined();
});
