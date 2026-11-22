import { readFileSync } from 'node:fs';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import KimiEmptyState from './KimiEmptyState.vue';
import { useAppStore } from '../stores/appStore';

beforeEach(() => setActivePinia(createPinia()));

it('keeps the new conversation send button enabled while another session runs', () => {
  const store = useAppStore();
  store.currentSessionName = '';
  store.running = true;
  store.pendingWorkdir = 'GLOBAL';
  store.sessionRuntimeByName['session-a'] = {
    messages: [], streamingText: '', thinkingText: '', running: true, subagentTasks: [],
  };
  store.sessionRuntimeByName['__pending__:GLOBAL'] = {
    messages: [], streamingText: '', thinkingText: '', running: false, subagentTasks: [],
  };

  const wrapper = mount(KimiEmptyState);

  expect(wrapper.get('.kimi-send-btn').attributes('disabled')).toBeUndefined();
  wrapper.unmount();
});

it('makes the large new-conversation surfaces follow the transparent workspace surface', () => {
  const appStyles = readFileSync('src/style.css', 'utf8');
  const style = document.createElement('style');
  style.textContent = appStyles
    .replaceAll('var(--theme-region-surface-panel)', 'transparent')
    .replaceAll('var(--theme-region-surface)', 'transparent')
    .replaceAll('var(--bg1)', '#ffffff');
  document.head.append(style);

  const workspace = document.createElement('div');
  workspace.dataset.themeRegion = 'workspace';
  workspace.style.setProperty('--bg1', '#ffffff');
  workspace.style.setProperty('--theme-region-surface', 'transparent');
  workspace.style.setProperty('--theme-region-surface-panel', 'transparent');

  const card = document.createElement('div');
  card.className = 'kimi-card';
  const workdir = document.createElement('div');
  workdir.className = 'kimi-footer-workdir';
  workspace.append(card, workdir);
  document.body.append(workspace);

  expect(getComputedStyle(card).backgroundColor).toBe('rgba(0, 0, 0, 0)');
  expect(getComputedStyle(workdir).backgroundColor).toBe('rgba(0, 0, 0, 0)');

  workspace.remove();
  style.remove();
});

it('未落库新会话离开空状态页后重新挂载仍恢复文字草稿', async () => {
  const store = useAppStore();
  store.currentSessionName = '';
  store.pendingWorkdir = 'GLOBAL';

  let wrapper = mount(KimiEmptyState);
  const editor = wrapper.get('.ce-input');
  editor.element.textContent = '新会话尚未发送的内容';
  await editor.trigger('input');
  wrapper.unmount();

  wrapper = mount(KimiEmptyState);
  expect(wrapper.get('.ce-input').element.textContent).toBe('新会话尚未发送的内容');
  wrapper.unmount();
});

it('shows the sandbox toggle without duplicating the git branch in the hero composer', async () => {
  const { useGitStore } = await import('../stores/gitStore');
  const store = useAppStore();
  const gitStore = useGitStore();
  gitStore.snapshot = {
    branch_name: 'dev', head_state: 'clean', head_oid: 'abcd1234',
    ahead: 0, behind: 0, staged: [], unstaged: [], untracked: [],
    conflicts: [], operation_kind: 'idle',
  } as any;
  const setSandbox = vi.fn();
  store.setSandbox = setSandbox as any;

  const wrapper = mount(KimiEmptyState);

  expect(wrapper.find('[data-test="hero-git-branch"]').exists()).toBe(false);

  const sandbox = wrapper.get('[data-test="hero-sandbox-button"]');
  expect(sandbox.text()).toContain('沙盒限制中');
  await sandbox.trigger('click');
  expect(setSandbox).toHaveBeenCalledWith(true);
  wrapper.unmount();
});

it('空态页不显示 Beta 标签，并按项目工作区动态加载推荐提示词（点击回填）', async () => {
  const get_workspace_prompt_suggestions = vi.fn(async (workdir: string) => ({
    ok: true,
    workdir,
    suggestions: [
      { id: 's1', label: '核对 181/184 双稿', prompt: '请核对小说 181/184 双稿差异' },
      { id: 's2', label: '运行第 3 轮回归', prompt: '运行回归脚本扫字数' },
    ],
  }));
  (window as any).pywebview = { api: { get_workspace_prompt_suggestions } };

  const store = useAppStore();
  store.currentSessionName = '';
  store.workdir = 'D:\\小说';
  store.currentWorkdirLabel = '小说';

  const wrapper = mount(KimiEmptyState);
  expect(wrapper.find('.kimi-beta').exists()).toBe(false);

  await new Promise((r) => setTimeout(r, 0));
  await wrapper.vm.$nextTick();

  expect(get_workspace_prompt_suggestions).toHaveBeenCalledWith('D:\\小说', null);
  const chips = wrapper.findAll('[data-test="project-prompt-chip"]');
  expect(chips).toHaveLength(2);
  expect(chips[0].text()).toBe('核对 181/184 双稿');

  await chips[0].trigger('click');
  expect(wrapper.get('.ce-input').text()).toBe('请核对小说 181/184 双稿差异');

  store.workdir = '';
  store.currentWorkdirLabel = '对话';
  await new Promise((r) => setTimeout(r, 0));
  await wrapper.vm.$nextTick();
  expect(wrapper.find('[data-test="project-prompt-suggestions"]').exists()).toBe(false);

  delete (window as any).pywebview;
  wrapper.unmount();
});

it('空态页按 Enter 发送首条消息后清空输入框与新会话 composerText', async () => {
  const send_user_message = vi.fn().mockResolvedValue({ ok: true });
  const get_sessions = vi.fn().mockResolvedValue({ ok: true, sessions: [] });
  (window as any).pywebview = { api: { send_user_message, get_sessions } };

  const store = useAppStore();
  store.currentSessionName = '';
  store.pendingWorkdir = 'GLOBAL';

  const wrapper = mount(KimiEmptyState);
  const editor = wrapper.get('.ce-input');
  editor.element.textContent = '首条提问内容';
  await editor.trigger('input');

  await editor.trigger('keydown', { key: 'Enter' });
  await new Promise((r) => setTimeout(r, 0));

  expect(send_user_message).toHaveBeenCalledTimes(1);
  expect(send_user_message.mock.calls[0][0]).toBe('首条提问内容');
  expect(store.currentSessionName).toMatch(/^session_[0-9a-f]{6}$/);
  expect(store._getSessionRuntime().composerText).toBe('');
  expect(editor.element.textContent).toBe('');

  delete (window as any).pywebview;
  wrapper.unmount();
});

it('空态页斜杠选择技能显示星盘图标与胶囊，删除时整块删除', async () => {
  const store = useAppStore();
  store.currentSessionName = '';
  store.pendingWorkdir = 'GLOBAL';
  store.skills = [
    { id: 's1', name: '排版', short_id: 'gzh-design', description: '公众号排版', enabled: true } as any,
  ];

  const wrapper = mount(KimiEmptyState, { attachTo: document.body });
  const editor = wrapper.get('.ce-input');
  editor.element.textContent = '/g';
  await editor.trigger('input');

  const sgItems = wrapper.findAll('.sg-item');
  expect(sgItems).toHaveLength(1);
  expect(sgItems[0].find('.sg-icon svg polygon').exists()).toBe(true);

  await editor.trigger('keydown', { key: 'Enter' });
  expect(editor.find('.ce-chip-skill').exists()).toBe(true);
  expect(editor.find('.ce-chip-skill .ce-chip-label').text()).toBe('gzh-design');
  expect(store._getSessionRuntime().composerText).toBe('/gzh-design ');

  const skillChip = editor.element.querySelector('.ce-chip-skill') as HTMLElement;
  skillChip.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
  await editor.trigger('keydown', { key: 'Backspace' });
  expect(editor.find('.ce-chip-skill').exists()).toBe(false);
  expect(store._getSessionRuntime().composerText).toBe(' ');
  wrapper.unmount();
});

it('空态页斜杠选择内置命令（如 /goal）显示为胶囊且删除整块删除', async () => {
  const store = useAppStore();
  store.currentSessionName = '';
  store.pendingWorkdir = 'GLOBAL';
  store.slashCommands = [
    { name: '/goal', usage: '/goal <目标>', summary: '目标模式', kind: 'hybrid' } as any,
  ];

  const wrapper = mount(KimiEmptyState, { attachTo: document.body });
  const editor = wrapper.get('.ce-input');
  editor.element.textContent = '/go';
  await editor.trigger('input');

  await editor.trigger('keydown', { key: 'Enter' });
  expect(editor.find('.ce-chip-command').exists()).toBe(true);
  expect(editor.find('.ce-chip-command .ce-chip-label').text()).toBe('goal');
  expect(store._getSessionRuntime().composerText).toBe('/goal ');

  const cmdChip = editor.element.querySelector('.ce-chip-command') as HTMLElement;
  cmdChip.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
  await editor.trigger('keydown', { key: 'Delete' });
  expect(editor.find('.ce-chip-command').exists()).toBe(false);
  expect(store._getSessionRuntime().composerText).toBe(' ');
  wrapper.unmount();
});
