import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import SystemSettings from './SystemSettings.vue';
import { useAppStore } from '../stores/appStore';


describe('SystemSettings', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('disables unavailable interpreters and keeps the selected interpreter checked', () => {
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

    expect(wrapper.get('[data-test="shell-pwsh"] input').attributes('disabled')).toBeDefined();
    expect(wrapper.get('[data-test="shell-pwsh"]').text()).toContain('未安装');
    expect((wrapper.get('[data-test="shell-powershell"] input').element as HTMLInputElement).checked).toBe(true);
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

    await wrapper.get('[data-test="shell-cmd"] input').setValue(true);

    expect(save).toHaveBeenCalledWith('cmd');
  });
});
