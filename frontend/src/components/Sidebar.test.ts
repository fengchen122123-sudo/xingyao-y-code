import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { expect, it, vi } from 'vitest';
import Sidebar from './Sidebar.vue';
import { useAppStore } from '../stores/appStore';

it('点击手机图标后调用 get_remote_bind_qr 并渲染二维码 img', async () => {
  const get_remote_bind_qr = vi.fn(async () => ({
    ok: true,
    url: 'http://192.168.1.5:8765/?token=abc123',
    token: 'abc123',
    qr: 'data:image/png;base64,QUJD',
    expires_in: 86400,
  }));
  (window as any).pywebview = { api: { get_remote_bind_qr } };

  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  const btn = wrapper.find('[data-test="remote-bind-button"]');
  expect(btn.exists()).toBe(true);
  await btn.trigger('click');
  // 等待异步 api 调用与响应式更新落定（宏任务 tick）
  await new Promise((r) => setTimeout(r, 0));

  expect(get_remote_bind_qr).toHaveBeenCalledTimes(1);
  const modal = wrapper.find('[data-test="remote-bind-modal"]');
  expect(modal.exists()).toBe(true);
  const img = modal.find('img');
  expect(img.exists()).toBe(true);
  expect(img.attributes('src')).toBe('data:image/png;base64,QUJD');
});

it('获取二维码失败时不显示弹窗，并弹出错误提示', async () => {
  const get_remote_bind_qr = vi.fn(async () => ({ ok: false, msg: '局域网服务未就绪' }));
  (window as any).pywebview = { api: { get_remote_bind_qr } };

  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });
  const store = useAppStore();

  await wrapper.find('[data-test="remote-bind-button"]').trigger('click');
  await new Promise((r) => setTimeout(r, 0));

  expect(wrapper.find('[data-test="remote-bind-modal"]').exists()).toBe(false);
  expect(store.confirmModal.show).toBe(true);
  expect(store.confirmModal.notice).toBe(true);
  expect(store.confirmModal.title).toBe('手机远程控制');
});

it('点击弹窗遮罩不关闭，仅点击关闭按钮才关闭', async () => {
  const get_remote_bind_qr = vi.fn(async () => ({
    ok: true,
    url: 'http://192.168.1.5:8765/?token=abc123',
    token: 'abc123',
    qr: 'data:image/png;base64,QUJD',
    expires_in: 86400,
  }));
  (window as any).pywebview = { api: { get_remote_bind_qr } };

  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  await wrapper.find('[data-test="remote-bind-button"]').trigger('click');
  await new Promise((r) => setTimeout(r, 0));
  expect(wrapper.find('[data-test="remote-bind-modal"]').exists()).toBe(true);

  // 点击遮罩（弹窗外区域）不应触发关闭
  await wrapper.find('[data-test="remote-bind-modal"]').trigger('click');
  expect(wrapper.find('[data-test="remote-bind-modal"]').exists()).toBe(true);

  // 仅点击“关闭”按钮才关闭
  await wrapper.find('[data-test="remote-bind-close"]').trigger('click');
  expect(wrapper.find('[data-test="remote-bind-modal"]').exists()).toBe(false);
});
