import { beforeEach, describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import TerminalPane from './TerminalPane.vue';
import { useAppStore } from '../stores/appStore';

beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.clear();
  // 模拟桥未注入的启动空窗期（与 pywebviewready 之前一致）
  (window as any).pywebview = undefined;
});

describe('TerminalPane bridge-ready gating', () => {
  it('never pops the connection-failure dialog while mounting without a bridge', async () => {
    const store = useAppStore();
    mount(TerminalPane, { global: { stubs: { AppIcon: true } } });
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
    await Promise.resolve();

    // 桥未就绪时终端不做任何桥调用，也就绝不触发 _api() 的连接失败弹窗
    expect(store.confirmModal.show).toBe(false);
  });

  it('does not ensure a session while the panel stays closed even after bridge ready', async () => {
    const store = useAppStore();
    let ensureCalls = 0;
    (window as any).pywebview = {
      api: {
        pty_terminal_ensure: () => {
          ensureCalls++;
          return Promise.resolve({ ok: true, pid: 1, cwd: 'C:\\x' });
        },
        pty_terminal_resize: () => Promise.resolve({ ok: true }),
      },
    };
    store.bridgeReady = true;
    mount(TerminalPane, { global: { stubs: { AppIcon: true } } });
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));

    expect(ensureCalls).toBe(0);

    store.toggleTerminalPanel();
    await vi_waitFor(() => expect(ensureCalls).toBe(1));
  });
});

async function vi_waitFor(assert: () => void, timeout = 1000): Promise<void> {
  const start = Date.now();
  for (;;) {
    try {
      assert();
      return;
    } catch {
      if (Date.now() - start > timeout) throw new Error('waitFor timeout');
      await new Promise((r) => setTimeout(r, 10));
    }
  }
}
