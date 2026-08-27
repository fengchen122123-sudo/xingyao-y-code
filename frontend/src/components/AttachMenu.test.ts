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

  it('触摸设备点回形针直接触发系统文件选择，不弹自定义菜单', async () => {
    mockTouch(true);
    const wrapper = mount(Comp as never, { global: { stubs: { teleport: true } } });
    // 设计简化：不再有应用内「拍照/相册/文件」浮层，只保留一个通用隐藏文件入口
    expect(wrapper.find('[data-test="attach-menu"]').exists()).toBe(false);
    expect(wrapper.findAll('input[type="file"]')).toHaveLength(1);
    const inputEl = wrapper.find('input[type="file"]').element as HTMLInputElement;
    const clickSpy = vi.spyOn(inputEl, 'click');
    await wrapper.get('[data-test="attachment-button"]').trigger('click');
    expect(clickSpy).toHaveBeenCalledTimes(1);
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
