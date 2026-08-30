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

it('重复打开弹窗直接复用同一 token（不强制重签）', async () => {
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
  // 关闭后再打开：仍应不带 force_new 调用，由后端决定复用
  await wrapper.find('[data-test="remote-bind-close"]').trigger('click');
  await wrapper.find('[data-test="remote-bind-button"]').trigger('click');
  await new Promise((r) => setTimeout(r, 0));

  expect(get_remote_bind_qr).toHaveBeenCalledTimes(2);
  expect(get_remote_bind_qr).toHaveBeenNthCalledWith(1, false);
  expect(get_remote_bind_qr).toHaveBeenNthCalledWith(2, false);
});

it('点击刷新按钮以 force_new 重新获取 token', async () => {
  const get_remote_bind_qr = vi
    .fn()
    .mockResolvedValueOnce({
      ok: true,
      url: 'http://192.168.1.5:8765/?token=old',
      token: 'old',
      qr: 'data:image/png;base64,OLD',
      expires_in: 86400,
    })
    .mockResolvedValueOnce({
      ok: true,
      url: 'http://192.168.1.5:8765/?token=new',
      token: 'new',
      qr: 'data:image/png;base64,NEW',
      expires_in: 86400,
    });
  (window as any).pywebview = { api: { get_remote_bind_qr } };

  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  await wrapper.find('[data-test="remote-bind-button"]').trigger('click');
  await new Promise((r) => setTimeout(r, 0));
  expect(wrapper.find('[data-test="remote-bind-modal"]').exists()).toBe(true);

  const refreshBtn = wrapper.find('[data-test="remote-bind-refresh"]');
  expect(refreshBtn.exists()).toBe(true);
  await refreshBtn.trigger('click');
  await new Promise((r) => setTimeout(r, 0));

  expect(get_remote_bind_qr).toHaveBeenLastCalledWith(true);
  const modal = wrapper.find('[data-test="remote-bind-modal"]');
  expect(modal.find('img').attributes('src')).toBe('data:image/png;base64,NEW');
});

it('永久有效（expires_in=0）时不做倒计时自动刷新', async () => {
  vi.useFakeTimers();
  try {
    const get_remote_bind_qr = vi.fn(async () => ({
      ok: true,
      url: 'http://192.168.1.5:8765/?token=forever',
      token: 'forever',
      qr: 'data:image/png;base64,FOREVER',
      expires_in: 0,
    }));
    (window as any).pywebview = { api: { get_remote_bind_qr } };

    const pinia = createPinia();
    setActivePinia(pinia);
    const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

    await wrapper.find('[data-test="remote-bind-button"]').trigger('click');
    await vi.advanceTimersByTimeAsync(0);

    // 文案显示“永久有效”
    expect(wrapper.find('[data-test="remote-bind-hint"]').text()).toContain('永久有效');

    // 推进 3 秒：永久 token 不应触发自动刷新（调用次数保持 1）
    await vi.advanceTimersByTimeAsync(3000);
    expect(get_remote_bind_qr).toHaveBeenCalledTimes(1);
  } finally {
    vi.useRealTimers();
  }
});

it('子页面打开时点击新会话会立即返回对话页面', async () => {
  delete (window as any).pywebview;
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.openSubpage('skills');
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  await wrapper.findAll('.menu-item')[0].trigger('click');

  expect(store.subpage.show).toBe(false);
  expect(store.pendingWorkdir).toBe('GLOBAL');
  wrapper.unmount();
});

it('子页面打开时点击历史会话会立即返回对话页面', async () => {
  delete (window as any).pywebview;
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.sessionGroups = [{
    folder_path: 'GLOBAL',
    folder_name: '对话',
    is_global: true,
    is_current: true,
    sessions: [{ name: 'session-a', display_name: '历史会话 A', updated_at: '' }],
  }] as any;
  store.openSubpage('plugins');
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  await wrapper.get('.session-item').trigger('click');

  expect(store.subpage.show).toBe(false);
  expect(store.currentSessionName).toBe('session-a');
  wrapper.unmount();
});
