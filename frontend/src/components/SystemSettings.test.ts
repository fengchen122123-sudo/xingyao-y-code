import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import SystemSettings from './SystemSettings.vue';
import { useAppStore } from '../stores/appStore';


const turnStepLimitFields = {
  agent_turn_step_limit: 200,
  agent_turn_step_limit_min: 10,
  agent_turn_step_limit_max: 1000,
};


describe('SystemSettings', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('renders the selected interpreter in one compact select and disables unavailable options', () => {
    const store = useAppStore();
    store.systemSettings = {
      command_shell: 'powershell',
      command_shell_available: true,
      command_shell_error: '',
      remote_token_ttl: 86400,
      ...turnStepLimitFields,
      command_shell_options: [
        {
          id: 'pwsh', label: 'PowerShell 7+', description: '现代 PowerShell',
          available: false, executable: '', unavailable_reason: '未安装',
        },
        {
          id: 'powershell', label: 'Windows PowerShell 5.1', description: 'Windows 自带',
          available: true, executable: 'powershell.exe', unavailable_reason: '',
        },
      ],
    };

    const wrapper = mount(SystemSettings);

    const select = wrapper.get<HTMLSelectElement>('[data-test="command-shell-select"]');
    const unavailable = wrapper.get('[data-test="shell-pwsh"]');

    expect(select.element.value).toBe('powershell');
    expect(unavailable.attributes('disabled')).toBeDefined();
    expect(unavailable.text()).toContain('未安装');
  });

  it('saves an available interpreter selected by the user', async () => {
    const store = useAppStore();
    store.systemSettings = {
      command_shell: 'auto',
      command_shell_available: true,
      command_shell_error: '',
      remote_token_ttl: 86400,
      ...turnStepLimitFields,
      command_shell_options: [
        {
          id: 'auto', label: '自动（系统默认）', description: '兼容旧行为',
          available: true, executable: 'cmd.exe', unavailable_reason: '',
        },
        {
          id: 'cmd', label: 'CMD', description: '传统解释器',
          available: true, executable: 'cmd.exe', unavailable_reason: '',
        },
      ],
    };
    const save = vi.spyOn(store, 'setCommandShell').mockResolvedValue(true);
    const wrapper = mount(SystemSettings);

    await wrapper.get<HTMLSelectElement>('[data-test="command-shell-select"]').setValue('cmd');

    expect(save).toHaveBeenCalledWith('cmd');
  });

  it('renders the remote token ttl select and saves the chosen value', async () => {
    const store = useAppStore();
    store.systemSettings = {
      command_shell: 'auto',
      command_shell_available: true,
      command_shell_error: '',
      remote_token_ttl: 86400,
      ...turnStepLimitFields,
      command_shell_options: [],
    };
    const save = vi.spyOn(store, 'setRemoteTokenTtl').mockResolvedValue(true);
    const wrapper = mount(SystemSettings);

    const select = wrapper.get<HTMLSelectElement>('[data-test="remote-token-ttl-select"]');
    expect(select.element.value).toBe('86400');

    await select.setValue('604800');

    expect(save).toHaveBeenCalledWith(604800);
  });

  it('saves a valid custom turn step limit', async () => {
    const store = useAppStore();
    store.systemSettings = {
      command_shell: 'auto',
      command_shell_available: true,
      command_shell_error: '',
      remote_token_ttl: 86400,
      ...turnStepLimitFields,
      command_shell_options: [],
    };
    const save = vi.spyOn(store, 'setAgentTurnStepLimit').mockResolvedValue(true);
    const wrapper = mount(SystemSettings);

    await wrapper.get('[data-test="turn-step-limit-input"]').setValue('300');
    await wrapper.get('[data-test="save-turn-step-limit"]').trigger('click');

    expect(save).toHaveBeenCalledWith(300);
  });

  it('does not call the backend for an out-of-range value', async () => {
    const store = useAppStore();
    store.systemSettings = {
      command_shell: 'auto',
      command_shell_available: true,
      command_shell_error: '',
      remote_token_ttl: 86400,
      ...turnStepLimitFields,
      command_shell_options: [],
    };
    const save = vi.spyOn(store, 'setAgentTurnStepLimit').mockResolvedValue(true);
    const wrapper = mount(SystemSettings);

    await wrapper.get('[data-test="turn-step-limit-input"]').setValue('1001');

    expect(wrapper.get('[data-test="turn-step-limit-error"]').text()).toContain('10–1000');
    expect(wrapper.get('[data-test="save-turn-step-limit"]').attributes('disabled')).toBeDefined();
    expect(save).not.toHaveBeenCalled();
  });
});
