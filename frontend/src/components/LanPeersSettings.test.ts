import { describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import LanPeersSettings from './LanPeersSettings.vue';

function flush() {
  return new Promise((r) => setTimeout(r, 0));
}

describe('LanPeersSettings', () => {
  it('加载并展示本机接入信息与远程节点列表', async () => {
    const get_lan_peers = vi.fn(async () => ({
      ok: true,
      peers: [
        {
          id: 'office-pc',
          name: '公司台式机',
          url: 'http://192.168.1.25:8765/a2a',
          protocol: 'a2a',
          has_token: true,
          token_plaintext: false,
          default_session: '',
          default_workdir: '',
          description: 'GPU 主机',
          enabled: true,
          last_status: 'online',
          last_agent_name: '星瑶 (Y-code)',
          last_checked_at: '2026-11-27 22:00:00',
          last_error: '',
        },
      ],
    }));
    const get_lan_peer_local_info = vi.fn(async () => ({
      ok: true,
      running: true,
      lan_ip: '192.168.1.10',
      lan_port: 54321,
      a2a_port: 8765,
      a2a_url: 'http://192.168.1.10:8765/a2a',
      ws_url: 'ws://192.168.1.10:54321/',
      token: 'secret-tok',
      import_json: '{"name":"星瑶 (192.168.1.10)","url":"http://192.168.1.10:8765/a2a","token":"secret-tok"}',
    }));
    (window as any).pywebview = {
      api: { get_lan_peers, get_lan_peer_local_info },
    };

    const wrapper = mount(LanPeersSettings, { global: { stubs: { AppIcon: true } } });
    await flush();

    expect(wrapper.find('[data-test="lan-peers-local-card"]').text()).toContain('http://192.168.1.10:8765/a2a');
    expect(wrapper.find('[data-test="lan-peer-card-office-pc"]').text()).toContain('公司台式机');
    expect(wrapper.find('[data-test="lan-peer-card-office-pc"]').text()).toContain('已加密保存');
    wrapper.unmount();
  });

  it('支持解析一键导入 JSON、保存节点、测试连接并删除节点', async () => {
    const peersState: any[] = [];
    const get_lan_peers = vi.fn(async () => ({ ok: true, peers: [...peersState] }));
    const get_lan_peer_local_info = vi.fn(async () => ({ ok: true, running: false }));
    const save_lan_peer = vi.fn(async (payload) => {
      const item = {
        id: payload.id || 'peer-b',
        name: payload.name,
        url: payload.url,
        protocol: payload.protocol || 'auto',
        has_token: true,
        token_plaintext: false,
        default_session: payload.default_session || '',
        default_workdir: payload.default_workdir || '',
        description: payload.description || '',
        enabled: payload.enabled ?? true,
        last_status: '',
        last_agent_name: '',
        last_checked_at: '',
        last_error: '',
      };
      peersState.splice(0, peersState.length, item);
      return { ok: true, peer: item, peers: [...peersState] };
    });
    const test_lan_peer = vi.fn(async () => ({
      ok: true,
      online: true,
      msg: '连接正常（星瑶，12ms）',
      peers: [{ ...peersState[0], last_status: 'online' }],
    }));
    const delete_lan_peer = vi.fn(async () => {
      peersState.splice(0, peersState.length);
      return { ok: true, peers: [] };
    });

    (window as any).pywebview = {
      api: {
        get_lan_peers,
        get_lan_peer_local_info,
        save_lan_peer,
        test_lan_peer,
        delete_lan_peer,
      },
    };

    const wrapper = mount(LanPeersSettings, { global: { stubs: { AppIcon: true } } });
    await flush();

    await wrapper.find('[data-test="lan-peers-add-btn"]').trigger('click');
    await wrapper.find('[data-test="lan-peer-import-input"]').setValue(
      JSON.stringify({
        id: 'peer-b',
        name: 'B电脑星瑶',
        url: 'http://192.168.1.99:8765/a2a',
        protocol: 'a2a',
        token: 'tok-xyz',
      }),
    );
    await wrapper.find('[data-test="lan-peer-apply-import"]').trigger('click');
    await wrapper.find('[data-test="lan-peer-form-save"]').trigger('click');
    await flush();

    expect(save_lan_peer).toHaveBeenCalledWith(
      expect.objectContaining({
        id: 'peer-b',
        name: 'B电脑星瑶',
        url: 'http://192.168.1.99:8765/a2a',
        token: 'tok-xyz',
      }),
    );
    expect(wrapper.find('[data-test="lan-peer-card-peer-b"]').exists()).toBe(true);

    await wrapper.find('[data-test="lan-peer-test-peer-b"]').trigger('click');
    await flush();
    expect(test_lan_peer).toHaveBeenCalledWith('peer-b');
    expect(wrapper.find('[data-test="lan-peers-notice"]').text()).toContain('探测成功');

    await wrapper.find('[data-test="lan-peer-delete-peer-b"]').trigger('click');
    await flush();
    expect(delete_lan_peer).toHaveBeenCalledWith('peer-b');
    expect(wrapper.find('[data-test="lan-peers-empty"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it('服务监听中但令牌未签发时按钮显示签发配对令牌并禁用一键导入，保存节点后刷新斜杠命令', async () => {
    setActivePinia(createPinia());
    const get_lan_peers = vi.fn(async () => ({ ok: true, peers: [] }));
    const get_lan_peer_local_info = vi.fn(async () => ({
      ok: true,
      running: true,
      lan_ip: '192.168.1.13',
      a2a_port: 8765,
      a2a_url: 'http://192.168.1.13:8765/a2a',
      token: '',
      import_json: '{}',
    }));
    const save_lan_peer = vi.fn(async (payload) => ({
      ok: true,
      peer: { id: 'nb', name: payload.name, url: payload.url, protocol: 'a2a', has_token: true, enabled: true },
      peers: [{ id: 'nb', name: payload.name, url: payload.url, protocol: 'a2a', has_token: true, enabled: true }],
    }));
    const list_slash_commands = vi.fn(async () => ({
      ok: true,
      commands: [{ name: '/笔记本目录', usage: '/笔记本目录', summary: '目录', kind: 'action' }],
    }));
    (window as any).pywebview = {
      api: {
        get_lan_peers,
        get_lan_peer_local_info,
        save_lan_peer,
        list_slash_commands,
      },
    };

    const wrapper = mount(LanPeersSettings, { global: { stubs: { AppIcon: true } } });
    await flush();

    expect(wrapper.find('[data-test="lan-peers-start-local"]').text()).toContain('签发配对令牌');
    expect((wrapper.find('[data-test="lan-peers-copy-import"]').element as HTMLButtonElement).disabled).toBe(true);

    await wrapper.find('[data-test="lan-peers-add-btn"]').trigger('click');
    await wrapper.find('[data-test="lan-peer-form-name"]').setValue('笔记本');
    await wrapper.find('[data-test="lan-peer-form-url"]').setValue('http://192.168.1.22:8765/a2a');
    await wrapper.find('[data-test="lan-peer-form-save"]').trigger('click');
    await flush();

    expect(list_slash_commands).toHaveBeenCalled();
    wrapper.unmount();
  });
});
