import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';


const settings = {
  command_shell: 'powershell',
  command_shell_available: true,
  command_shell_error: '',
  remote_token_ttl: 86400,
  agent_turn_step_limit: 100,
  agent_turn_step_limit_min: 10,
  agent_turn_step_limit_max: 1000,
  command_shell_options: [
    {
      id: 'auto',
      label: '自动（系统默认）',
      description: '保持兼容旧行为',
      available: true,
      executable: 'C:\\Windows\\System32\\cmd.exe',
      unavailable_reason: '',
    },
    {
      id: 'powershell',
      label: 'Windows PowerShell 5.1',
      description: 'Windows 自带',
      available: true,
      executable: 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe',
      unavailable_reason: '',
    },
  ],
};


describe('system settings store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('refreshes the extensible system settings snapshot', async () => {
    const getSystemSettings = vi.fn().mockResolvedValue(settings);
    (window as any).pywebview = { api: { get_system_settings: getSystemSettings } };
    const store = useAppStore();

    await store.refreshSystemSettings();

    expect(store.systemSettings.command_shell).toBe('powershell');
    expect(store.systemSettings.command_shell_options[1].available).toBe(true);
  });

  it('persists a command shell and applies the returned snapshot', async () => {
    const next = structuredClone(settings);
    next.command_shell = 'auto';
    const setCommandShell = vi.fn().mockResolvedValue({ ok: true, settings: next });
    (window as any).pywebview = { api: { set_command_shell: setCommandShell } };
    const store = useAppStore();
    store.systemSettings = structuredClone(settings);

    const ok = await store.setCommandShell('auto');

    expect(ok).toBe(true);
    expect(setCommandShell).toHaveBeenCalledWith('auto');
    expect(store.systemSettings.command_shell).toBe('auto');
  });

  it('persists a turn step limit and applies the returned snapshot', async () => {
    const next = structuredClone(settings);
    next.agent_turn_step_limit = 300;
    const setLimit = vi.fn().mockResolvedValue({ ok: true, settings: next });
    (window as any).pywebview = { api: { set_agent_turn_step_limit: setLimit } };
    const store = useAppStore();
    store.systemSettings = structuredClone(settings);

    const ok = await store.setAgentTurnStepLimit(300);

    expect(ok).toBe(true);
    expect(setLimit).toHaveBeenCalledWith(300);
    expect(store.systemSettings.agent_turn_step_limit).toBe(300);
  });

  it('restores the previous snapshot when saving the limit fails', async () => {
    const setLimit = vi.fn().mockResolvedValue({ ok: false, settings, msg: '范围错误' });
    (window as any).pywebview = { api: { set_agent_turn_step_limit: setLimit } };
    const store = useAppStore();
    store.systemSettings = structuredClone(settings);

    const ok = await store.setAgentTurnStepLimit(1001);

    expect(ok).toBe(false);
    expect(store.systemSettings.agent_turn_step_limit).toBe(100);
  });
});
