import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import SystemSettings from './SystemSettings.vue';
import { useAppStore } from '../stores/appStore';


const turnStepLimitFields = {
  agent_turn_step_limit: 200,
  agent_turn_step_limit_min: 10,
  agent_turn_step_limit_max: 1000,
};

// AI 生成内容目录设置（Task 5）：setting 为原始配置值（未配置为空串），
// default 为默认目录（占位符与说明文案展示用）
const aiOutputDirFields = {
  ai_output_dir: 'C:\\ycode-data\\tmp',
  ai_output_dir_setting: '',
  ai_output_dir_default: 'C:\\ycode-data\\tmp',
};

// 电脑操控设置快照（Task 12）：两个开关默认关闭，浏览器选项独立探测
const computerSettings = {
  browser_control_enabled: false,
  computer_control_enabled: false,
  computer_browser: 'edge',
  computer_browser_options: [
    {
      browser: 'edge', label: 'Microsoft Edge', available: true,
      executable: 'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe', unavailable_reason: '',
    },
    {
      browser: 'chrome', label: 'Google Chrome', available: false,
      executable: '', unavailable_reason: '未安装或不在 星瑶Code 的 PATH 中',
    },
  ],
  computer_profile_path: 'C:\\ycode-data\\browser-profiles\\edge',
  computer_platform_supported: true,
  computer_hotkey_available: true,
  computer_hotkey: 'Ctrl+Alt+Esc',
  computer_active_model_capability: {
    model: 'glm-5.3-flash', eligible: true, protocol: 'generic', error_code: '', reason: '',
  },
  computer_run: {
    run_id: '', owner_session_id: '', owner_turn_id: '', status: 'idle', target: '',
    browser: '', model_name: '', protocol: 'none', started_at: 0, action_count: 0,
    current_action: '', current_surface: '', preview_url: '', pause_reason: '',
    stop_reason: '', error_code: '', hotkey_available: false,
  },
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
      ...aiOutputDirFields,
      ...computerSettings,
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
      ...aiOutputDirFields,
      ...computerSettings,
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
      ...aiOutputDirFields,
      ...computerSettings,
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
      ...aiOutputDirFields,
      ...computerSettings,
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
      ...aiOutputDirFields,
      ...computerSettings,
      command_shell_options: [],
    };
    const save = vi.spyOn(store, 'setAgentTurnStepLimit').mockResolvedValue(true);
    const wrapper = mount(SystemSettings);

    await wrapper.get('[data-test="turn-step-limit-input"]').setValue('1001');

    expect(wrapper.get('[data-test="turn-step-limit-error"]').text()).toContain('10–1000');
    expect(wrapper.get('[data-test="save-turn-step-limit"]').attributes('disabled')).toBeDefined();
    expect(save).not.toHaveBeenCalled();
  });

  // ── AI 生成内容目录（Task 5）──

  it('saves the AI output directory through the store', async () => {
    const store = useAppStore();
    store.systemSettings = {
      command_shell: 'auto',
      command_shell_available: true,
      command_shell_error: '',
      remote_token_ttl: 86400,
      ...turnStepLimitFields,
      ...aiOutputDirFields,
      ...computerSettings,
      command_shell_options: [],
    };
    const save = vi.spyOn(store, 'setAiOutputDir').mockResolvedValue(true);
    const wrapper = mount(SystemSettings);

    // 占位符显示后端给出的默认目录，用户不填即恢复默认
    const input = wrapper.get('[data-test="ai-output-dir-input"]');
    expect(input.attributes('placeholder')).toBe('C:\\ycode-data\\tmp');

    await input.setValue('D:/ai_out');
    await wrapper.get('[data-test="save-ai-output-dir"]').trigger('click');

    expect(save).toHaveBeenCalledWith('D:/ai_out');
  });

  it('shows the error and keeps the draft when saving the AI output directory fails', async () => {
    const store = useAppStore();
    store.systemSettings = {
      command_shell: 'auto',
      command_shell_available: true,
      command_shell_error: '',
      remote_token_ttl: 86400,
      ...turnStepLimitFields,
      ...aiOutputDirFields,
      ...computerSettings,
      command_shell_options: [],
    };
    vi.spyOn(store, 'setAiOutputDir').mockResolvedValue(false);
    const wrapper = mount(SystemSettings);

    await wrapper.get('[data-test="ai-output-dir-input"]').setValue('D:/ai_out');
    await wrapper.get('[data-test="save-ai-output-dir"]').trigger('click');
    await flushPromises();

    expect(wrapper.get('[data-test="ai-output-dir-error"]').text()).toContain('保存失败');
    expect(wrapper.get<HTMLInputElement>('[data-test="ai-output-dir-input"]').element.value).toBe('D:/ai_out');
  });

  it('picks the AI output directory from the folder dialog, fills the draft and saves it', async () => {
    const store = useAppStore();
    store.systemSettings = {
      command_shell: 'auto',
      command_shell_available: true,
      command_shell_error: '',
      remote_token_ttl: 86400,
      ...turnStepLimitFields,
      ...aiOutputDirFields,
      ...computerSettings,
      command_shell_options: [],
    };
    const save = vi.spyOn(store, 'setAiOutputDir').mockResolvedValue(true);
    // 选择目录走 store.pickFolderPlain（桌面原生对话框 / 触屏内置浏览器统一入口）
    const pick = vi.spyOn(store, 'pickFolderPlain').mockImplementation(async (cb: (path: string) => void) => {
      cb('D:\\ai_out');
    });
    const wrapper = mount(SystemSettings);

    await wrapper.get('[data-test="pick-ai-output-dir"]').trigger('click');
    await flushPromises();

    expect(pick).toHaveBeenCalled();
    expect(wrapper.get<HTMLInputElement>('[data-test="ai-output-dir-input"]').element.value).toBe('D:\\ai_out');
    expect(save).toHaveBeenCalledWith('D:\\ai_out');
  });

  it('keeps the draft and saves nothing when the folder dialog is cancelled', async () => {
    const store = useAppStore();
    store.systemSettings = {
      command_shell: 'auto',
      command_shell_available: true,
      command_shell_error: '',
      remote_token_ttl: 86400,
      ...turnStepLimitFields,
      ...aiOutputDirFields,
      ...computerSettings,
      command_shell_options: [],
    };
    const save = vi.spyOn(store, 'setAiOutputDir').mockResolvedValue(true);
    // 用户取消：pickFolderPlain 的回调不触发
    vi.spyOn(store, 'pickFolderPlain').mockImplementation(async () => {});
    const wrapper = mount(SystemSettings);

    await wrapper.get('[data-test="pick-ai-output-dir"]').trigger('click');
    await flushPromises();

    expect(wrapper.get<HTMLInputElement>('[data-test="ai-output-dir-input"]').element.value).toBe('');
    expect(save).not.toHaveBeenCalled();
  });

  // ── 电脑操控区（Task 12）──

  function mountWithComputerSettings(overrides: Record<string, unknown> = {}) {
    const store = useAppStore();
    store.systemSettings = {
      command_shell: 'auto',
      command_shell_available: true,
      command_shell_error: '',
      remote_token_ttl: 86400,
      ...turnStepLimitFields,
      ...aiOutputDirFields,
      ...computerSettings,
      ...overrides,
    } as typeof store.systemSettings;
    return { store, wrapper: mount(SystemSettings) };
  }

  it('renders the computer control section with both switches off and unavailable browser disabled', () => {
    const { wrapper } = mountWithComputerSettings();

    expect(wrapper.get('[data-test="browser-control-switch"]').attributes('aria-checked')).toBe('false');
    expect(wrapper.get('[data-test="computer-control-switch"]').attributes('aria-checked')).toBe('false');
    const chromeOption = wrapper.get('[data-test="browser-option-chrome"]');
    expect(chromeOption.attributes('disabled')).toBeDefined();
    expect(chromeOption.text()).toContain('未安装或不在 星瑶Code 的 PATH 中');
    expect(wrapper.get('[data-test="browser-option-edge"]').attributes('disabled')).toBeUndefined();
  });

  it('shows the vision requirement hint in the computer control section', () => {
    const { wrapper } = mountWithComputerSettings();

    const section = wrapper.get('[data-test="computer-settings-section"]');
    // 用户要求：明确提示电脑操控模型必须支持视觉（识图）与工具调用
    expect(section.text()).toContain('视觉');
    expect(section.text()).toContain('工具调用');
    expect(section.text()).toContain('自动');
  });

  it('asks the store to enable browser control when the switch is toggled', async () => {
    const { store, wrapper } = mountWithComputerSettings();
    const enable = vi.spyOn(store, 'setBrowserControlEnabled').mockResolvedValue(true);

    await wrapper.get('[data-test="browser-control-switch"]').trigger('click');

    expect(enable).toHaveBeenCalledWith(true);
  });

  it('asks the store to save the selected browser', async () => {
    const { store, wrapper } = mountWithComputerSettings({
      computer_browser_options: [
        ...computerSettings.computer_browser_options.slice(0, 1),
        {
          browser: 'chrome', label: 'Google Chrome', available: true,
          executable: 'C:\\chrome\\chrome.exe', unavailable_reason: '',
        },
      ],
    });
    const select = vi.spyOn(store, 'setComputerBrowser').mockResolvedValue(true);

    await wrapper.get<HTMLSelectElement>('[data-test="computer-browser-select"]').setValue('chrome');

    expect(select).toHaveBeenCalledWith('chrome');
  });

  it('disables the browser select while a computer run is active', () => {
    const { wrapper } = mountWithComputerSettings({
      computer_run: {
        ...computerSettings.computer_run,
        run_id: 'run-1',
        status: 'running',
        target: 'browser',
      },
    });

    expect(wrapper.get('[data-test="computer-browser-select"]').attributes('disabled')).toBeDefined();
  });

  it('shows the dedicated profile path and the exact privacy notice', () => {
    const { wrapper } = mountWithComputerSettings();

    expect(wrapper.get('[data-test="computer-profile-path"]').text()).toContain('browser-profiles\\edge');
    expect(wrapper.get('[data-test="computer-privacy-notice"]').text()).toBe(
      '开启后，当前页面或桌面截图会发送给当前模型 Provider；星瑶Code 不会逐动作询问。登录、验证码和凭据请在专用浏览器中由你手动完成。紧急停止：Ctrl+Alt+Esc。',
    );
  });

  it('shows the platform, browser and hotkey preflight results', () => {
    const { wrapper } = mountWithComputerSettings();

    const preflight = wrapper.get('[data-test="computer-preflight-list"]').text();
    expect(preflight).toContain('平台');
    expect(preflight).toContain('Microsoft Edge');
    expect(preflight).toContain('Ctrl+Alt+Esc');
    // 浏览器不可用必须显示原因，绝不静默换浏览器
    expect(preflight).toContain('未安装或不在 星瑶Code 的 PATH 中');
  });

  it('hides the clickable enable controls on the mobile viewport', async () => {
    vi.stubGlobal('matchMedia', vi.fn().mockImplementation((query: string) => ({
      matches: query.includes('768'),
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })));
    try {
      const { wrapper } = mountWithComputerSettings();

      // 手机端不显示可点击启用控件（后端边界由 Task 15 实施）
      expect(wrapper.find('[data-test="browser-control-switch"]').exists()).toBe(false);
      expect(wrapper.find('[data-test="computer-control-switch"]').exists()).toBe(false);
      expect(wrapper.find('[data-test="computer-browser-select"]').exists()).toBe(false);
      expect(wrapper.get('[data-test="computer-mobile-note"]').text()).toContain('桌面端');
      // 只读信息（隐私提示）仍可见
      expect(wrapper.find('[data-test="computer-privacy-notice"]').exists()).toBe(true);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe('computer active model capability hint', () => {
  function mountWith(overrides: Record<string, unknown>) {
    setActivePinia(createPinia());
    const store = useAppStore();
    store.systemSettings = {
      command_shell: 'auto',
      command_shell_available: true,
      command_shell_error: '',
      remote_token_ttl: 86400,
      ...turnStepLimitFields,
      ...aiOutputDirFields,
      ...computerSettings,
      ...overrides,
    } as typeof store.systemSettings;
    return mount(SystemSettings);
  }

  it('shows actionable hint when switches on but model ineligible', () => {
    const wrapper = mountWith({
      browser_control_enabled: true,
      computer_active_model_capability: {
        model: 'glm-5.3-flash', eligible: false,
        error_code: 'computer_model_not_capable',
        reason: '模型未声明视觉与工具调用能力（电脑操控要求两者齐备）',
      },
    });
    const hint = wrapper.get('[data-test="computer-model-capability-hint"]');
    expect(hint.text()).toContain('glm-5.3-flash');
    expect(hint.text()).toContain('不具备电脑操控资格');
    expect(hint.text()).toContain('视觉');
    expect(hint.classes()).toContain('blocked');
  });

  it('shows eligible summary when switches on and model qualifies', () => {
    const wrapper = mountWith({ browser_control_enabled: true });
    const hint = wrapper.get('[data-test="computer-model-capability-hint"]');
    expect(hint.text()).toContain('已具备电脑操控资格');
    expect(hint.classes()).not.toContain('blocked');
  });

  it('hides the hint when both switches are off', () => {
    const wrapper = mountWith({});
    expect(wrapper.find('[data-test="computer-model-capability-hint"]').exists()).toBe(false);
  });
});
