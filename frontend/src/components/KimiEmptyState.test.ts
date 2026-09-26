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
  // jsdom 不解析 CSS 自定义属性。用手工值代入生产样式，验证最终级联结果：
  // 普通卡片底色为白，workspace 主题表面为透明。
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
  await wrapper.get('textarea').setValue('新会话尚未发送的内容');
  wrapper.unmount();

  wrapper = mount(KimiEmptyState);
  expect(wrapper.get('textarea').element.value).toBe('新会话尚未发送的内容');
  wrapper.unmount();
});

it('shows the sandbox toggle and git branch in the hero composer', async () => {
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

  const sandbox = wrapper.get('[data-test="hero-sandbox-button"]');
  expect(sandbox.text()).toContain('沙盒: 限');
  await sandbox.trigger('click');
  expect(setSandbox).toHaveBeenCalledWith(true);

  const branch = wrapper.get('[data-test="hero-git-branch"]');
  expect(branch.text()).toContain('dev');
  const nonceBefore = gitStore.panelOpenNonce;
  await branch.trigger('click');
  expect(gitStore.panelOpenNonce).toBe(nonceBefore + 1);
  expect(store.rightPanelCollapsed).toBe(false);
  wrapper.unmount();
});
