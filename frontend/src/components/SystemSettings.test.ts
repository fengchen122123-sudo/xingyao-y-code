import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import SystemSettings from './SystemSettings.vue';
import { useAppStore } from '../stores/appStore';


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
});
