import { describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import CloudRelaySettings from './CloudRelaySettings.vue';
import { useAppStore } from '../stores/appStore';

describe('CloudRelaySettings', () => {
  it('未配置时点击「星瑶自动部署到服务器」关闭设置并发送部署提示词', async () => {
    const get_cloud_relay_settings = vi.fn(async () => ({
      ok: true,
      settings: { enabled: false, url: '', device_id: '', has_token: false, state: 'disabled' },
    }));
    (window as any).pywebview = { api: { get_cloud_relay_settings } };
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useAppStore();
    store.settingsOpen = true;
    const sendSpy = vi.spyOn(store, 'sendMessage').mockResolvedValue(undefined);

    const wrapper = mount(CloudRelaySettings, { global: { plugins: [pinia] } });
    await new Promise((r) => setTimeout(r, 0));

    await wrapper.find('[data-test="deploy-host"]').setValue('129.204.50.227');
    await wrapper.find('[data-test="deploy-user"]').setValue('ubuntu');
    await wrapper.find('[data-test="deploy-domain"]').setValue('xingyaocode.cn');
    await wrapper.find('[data-test="cloud-relay-auto-deploy"]').trigger('click');

    expect(store.settingsOpen).toBe(false);
    expect(sendSpy).toHaveBeenCalledTimes(1);
    const prompt = sendSpy.mock.calls[0][0];
    expect(prompt).toContain('_internal/');
    expect(prompt).toContain('relay_server');
    expect(prompt).toContain('129.204.50.227');
    expect(prompt).toContain('ubuntu');
    expect(prompt).toContain('xingyaocode.cn');
    wrapper.unmount();
  });

  it('未配置时打开总开关会提示先部署或手动填写，已配置时直接保存并连接', async () => {
    const save_cloud_relay_settings = vi.fn(async (payload) => ({
      ok: true,
      settings: {
        enabled: payload.enabled,
        url: payload.url,
        device_id: payload.device_id,
        has_token: true,
        state: payload.enabled ? 'connecting' : 'stopped',
      },
    }));
    const get_cloud_relay_settings = vi.fn(async () => ({
      ok: true,
      settings: { enabled: false, url: '', device_id: '', has_token: false, state: 'disabled' },
    }));
    (window as any).pywebview = { api: { get_cloud_relay_settings, save_cloud_relay_settings } };
    const pinia = createPinia();
    setActivePinia(pinia);
    const wrapper = mount(CloudRelaySettings, { global: { plugins: [pinia] } });
    await new Promise((r) => setTimeout(r, 0));

    const toggle = wrapper.find('[data-test="cloud-relay-enabled"]');
    await toggle.setValue(true);
    await new Promise((r) => setTimeout(r, 0));

    expect(save_cloud_relay_settings).not.toHaveBeenCalled();
    expect(wrapper.find('[data-test="cloud-relay-message"]').text()).toContain('星瑶自动部署');

    await wrapper.find('[data-test="cloud-relay-url"]').setValue('wss://xingyaocode.cn/xy/bot/relay/device');
    await wrapper.find('[data-test="cloud-relay-device-id"]').setValue('dev-1');
    await wrapper.find('[data-test="cloud-relay-token"]').setValue('tok-1');
    await toggle.setValue(true);
    await new Promise((r) => setTimeout(r, 0));

    expect(save_cloud_relay_settings).toHaveBeenCalledTimes(1);
    expect(save_cloud_relay_settings).toHaveBeenCalledWith({
      enabled: true,
      url: 'wss://xingyaocode.cn/xy/bot/relay/device',
      device_id: 'dev-1',
      token: 'tok-1',
    });
    wrapper.unmount();
  });
});
