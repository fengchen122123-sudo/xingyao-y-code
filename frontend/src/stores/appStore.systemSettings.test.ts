import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';


const settings = {
  command_shell: 'powershell',
  command_shell_available: true,
  command_shell_error: '',
  remote_token_ttl: 86400,
  agent_turn_step_limit: 200,
  agent_turn_step_limit_min: 10,
  agent_turn_step_limit_max: 1000,
  ai_output_dir: 'C:\\ycode-data\\tmp',
  ai_output_dir_setting: '',
  ai_output_dir_default: 'C:\\ycode-data\\tmp',
  browser_control_enabled: false,
  computer_control_enabled: false,
  computer_browser: 'edge',
  computer_browser_options: [
    {
      browser: 'edge',
      label: 'Microsoft Edge',
      available: true,
      executable: 'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
      unavailable_reason: '',
    },
    {
      browser: 'chrome',
      label: 'Google Chrome',
      available: false,
      executable: '',
      unavailable_reason: '未安装或不在 星瑶Code 的 PATH 中',
    },
  ],
  computer_profile_path: 'C:\\ycode-data\\browser-profiles\\edge',
  computer_platform_supported: true,
  computer_hotkey_available: true,
  computer_hotkey: 'Ctrl+Alt+Esc',
  computer_active_model_capability: { model: '', eligible: false },
  computer_run: {
    run_id: '',
    owner_session_id: '',
    owner_turn_id: '',
    status: 'idle',
    target: '',
    browser: '',
    model_name: '',
    protocol: 'none',
    started_at: 0,
    action_count: 0,
    current_action: '',
    current_surface: '',
    preview_url: '',
    pause_reason: '',
    stop_reason: '',
    error_code: '',
    hotkey_available: false,
  },
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
    expect(store.systemSettings.agent_turn_step_limit).toBe(200);
  });

  // ── 电脑操控设置（Task 12）：只以后端返回快照覆盖，不做乐观持久值 ──

  it('rolls back to backend snapshot when browser enable preflight fails', async () => {
    const disabledSettings = structuredClone(settings);
    disabledSettings.browser_control_enabled = false;
    const enable = vi.fn().mockResolvedValue({
      ok: false,
      error_code: 'computer_browser_unavailable',
      msg: 'Edge 未安装',
      settings: disabledSettings,
    });
    (window as any).pywebview = { api: { set_browser_control_enabled: enable } };
    const store = useAppStore();
    store.systemSettings = structuredClone(settings);
    // 本地陈旧值（如上一轮快照漂移）也必须被后端真实快照覆盖
    store.systemSettings.browser_control_enabled = true;

    await store.setBrowserControlEnabled(true);

    expect(enable).toHaveBeenCalledWith(true);
    expect(store.systemSettings.browser_control_enabled).toBe(false);
    expect(store.confirmModal.message).toContain('Edge 未安装');
  });

  it('applies the backend snapshot immediately after disabling', async () => {
    const disabled = structuredClone(settings);
    disabled.browser_control_enabled = false;
    const disable = vi.fn().mockResolvedValue({ ok: true, settings: disabled });
    (window as any).pywebview = { api: { set_browser_control_enabled: disable } };
    const store = useAppStore();
    store.systemSettings = structuredClone(settings);
    store.systemSettings.browser_control_enabled = true;

    const ok = await store.setBrowserControlEnabled(false);

    expect(ok).toBe(true);
    expect(disable).toHaveBeenCalledWith(false);
    // 关闭调用后快照立即 false（即使后端清理尚在进行）
    expect(store.systemSettings.browser_control_enabled).toBe(false);
  });

  it('keeps the browser selection and surfaces the error during an active run', async () => {
    const unchanged = structuredClone(settings);
    unchanged.computer_browser = 'edge';
    const select = vi.fn().mockResolvedValue({
      ok: false,
      error_code: 'computer_session_busy',
      msg: '当前有进行中的电脑操控会话，请先停止后再切换浏览器',
      settings: unchanged,
    });
    (window as any).pywebview = { api: { set_computer_browser: select } };
    const store = useAppStore();
    store.systemSettings = structuredClone(settings);

    const ok = await store.setComputerBrowser('chrome');

    expect(ok).toBe(false);
    expect(select).toHaveBeenCalledWith('chrome');
    expect(store.systemSettings.computer_browser).toBe('edge');
    expect(store.confirmModal.message).toContain('电脑操控会话');
  });

  it('persists computer control enable and applies the returned snapshot', async () => {
    const enabled = structuredClone(settings);
    enabled.computer_control_enabled = true;
    const enable = vi.fn().mockResolvedValue({ ok: true, settings: enabled });
    (window as any).pywebview = { api: { set_computer_control_enabled: enable } };
    const store = useAppStore();
    store.systemSettings = structuredClone(settings);

    const ok = await store.setComputerControlEnabled(true);

    expect(ok).toBe(true);
    expect(enable).toHaveBeenCalledWith(true);
    expect(store.systemSettings.computer_control_enabled).toBe(true);
  });

  // ── AI 生成内容目录（Task 5）：只以后端返回快照覆盖，失败即回滚 ──

  it('persists the AI output directory and applies the returned snapshot', async () => {
    const next = structuredClone(settings);
    next.ai_output_dir = 'D:\\ai_out';
    next.ai_output_dir_setting = 'D:\\ai_out';
    const setDir = vi.fn().mockResolvedValue({
      ok: true,
      settings: next,
      msg: 'AI 生成内容目录已更新：D:\\ai_out',
    });
    (window as any).pywebview = { api: { set_ai_output_dir: setDir } };
    const store = useAppStore();
    store.systemSettings = structuredClone(settings);

    const ok = await store.setAiOutputDir('D:\\ai_out');

    expect(ok).toBe(true);
    expect(setDir).toHaveBeenCalledWith('D:\\ai_out');
    expect(store.systemSettings.ai_output_dir).toBe('D:\\ai_out');
    expect(store.confirmModal.message).toContain('AI 生成内容目录已更新');
  });

  it('rolls back to the backend snapshot when the AI output directory is rejected', async () => {
    const unchanged = structuredClone(settings);
    const setDir = vi.fn().mockResolvedValue({
      ok: false,
      msg: '目录不可写或无法创建：D:\\blocked',
      settings: unchanged,
    });
    (window as any).pywebview = { api: { set_ai_output_dir: setDir } };
    const store = useAppStore();
    store.systemSettings = structuredClone(settings);

    const ok = await store.setAiOutputDir('D:\\blocked');

    expect(ok).toBe(false);
    expect(setDir).toHaveBeenCalledWith('D:\\blocked');
    expect(store.systemSettings.ai_output_dir_setting).toBe('');
    expect(store.confirmModal.message).toContain('目录不可写或无法创建');
  });
});
