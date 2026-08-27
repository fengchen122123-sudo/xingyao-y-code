import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi, afterEach } from 'vitest';
import InputArea from './InputArea.vue';
import KimiEmptyState from './KimiEmptyState.vue';
import { useAppStore } from '../stores/appStore';

function mockTouch(matches: boolean) {
  (window as unknown as { matchMedia: unknown }).matchMedia = vi
    .fn()
    .mockImplementation((query: string) => ({
      matches,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
}

describe.each([
  ['InputArea', InputArea],
  ['KimiEmptyState', KimiEmptyState],
])('%s 附件按钮触摸/桌面分流', (_name, Comp) => {
  beforeEach(() => setActivePinia(createPinia()));
  afterEach(() => vi.restoreAllMocks());

  it('触摸设备点回形针弹出拍照/相册/文件菜单', async () => {
    mockTouch(true);
    const wrapper = mount(Comp as never, { global: { stubs: { teleport: true } } });
    await wrapper.get('[data-test="attachment-button"]').trigger('click');
    expect(wrapper.find('[data-test="attach-menu"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="attach-camera"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="attach-album"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="attach-file"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it('桌面设备点回形针走 pickFiles，不弹菜单', async () => {
    mockTouch(false);
    const store = useAppStore();
    const pickSpy = vi.spyOn(store, 'pickFiles').mockResolvedValue([]);
    const wrapper = mount(Comp as never, { global: { stubs: { teleport: true } } });
    await wrapper.get('[data-test="attachment-button"]').trigger('click');
    expect(pickSpy).toHaveBeenCalled();
    expect(wrapper.find('[data-test="attach-menu"]').exists()).toBe(false);
    wrapper.unmount();
  });
});
