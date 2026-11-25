import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

import App from './App.vue';
import { useAppStore } from './stores/appStore';
import { useGitStore } from './stores/gitStore';

const stubs = {
  Topbar: true, Sidebar: true, KimiEmptyState: true, TurnGroup: true,
  InputArea: true, RightPanel: true, ConfirmModal: true, UpdateModal: true,
  PermCard: true, ImageLightbox: true, Subpage: true, SettingsPage: true,
  AppIcon: true, MessageNavRail: true, RemoteDirBrowser: true,
};

function mockDecisionApi(overrides: Record<string, unknown> = {}) {
  const api = {
    get_decision_audit_summary: vi.fn(async () => ({ ok: true, has_business_records: true })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('空新会话页隐藏「导出」按钮；进入会话后渲染导出且不渲染「清空」', () => {
  const store = useAppStore();
  store.initialized = true;
  const emptyWrapper = mount(App, { global: { stubs } });
  expect(emptyWrapper.find('#chat-header .export-wrap').exists()).toBe(false);
  emptyWrapper.unmount();

  store.currentSessionName = 'session-1';
  const wrapper = mount(App, { global: { stubs } });
  const labels = wrapper.findAll('#chat-header .chip-btn').map((b) => b.text());

  expect(labels).not.toContain('清空');
  expect(labels.some((t) => t.includes('导出'))).toBe(true);
  wrapper.unmount();
});

it('对话标题栏按钮文字收进 .chip-label，窄栏由容器查询换成纯图标', () => {
  const store = useAppStore();
  store.initialized = true;
  store.currentSessionName = 'session-1';
  const wrapper = mount(App, { global: { stubs } });

  const exportBtn = wrapper.get('#chat-header .export-wrap .chip-btn');
  // 文字必须在 .chip-label 内，@container(chat-header) 才能整块隐去收成图标
  expect(exportBtn.get('.chip-label').text()).toBe('导出');
  // 图标档没有文字，语义靠 title 兜底，所以 title 不能缺
  expect(exportBtn.attributes('title')).toBe('导出会话');
  wrapper.unmount();
});

it('对话标题栏渲染 Git 分支 chip，点击展开 Git 面板', async () => {
  const store = useAppStore();
  store.initialized = true;
  const gitStore = useGitStore();
  gitStore.snapshot = {
    branch_name: 'dev', head_state: 'clean', head_oid: 'abcd1234',
    ahead: 0, behind: 0, staged: [], unstaged: [], untracked: [],
    conflicts: [], operation_kind: 'idle',
  } as any;

  const wrapper = mount(App, { global: { stubs } });
  const chip = wrapper.get('[data-test="chat-header-git-branch"]');
  expect(chip.text()).toContain('dev');

  store.rightPanelCollapsed = true;
  const nonceBefore = gitStore.panelOpenNonce;
  await chip.trigger('click');
  expect(gitStore.panelOpenNonce).toBe(nonceBefore + 1);
  expect(store.rightPanelCollapsed).toBe(false);
  wrapper.unmount();
});

it('未产生过决策记录时，头部不渲染「决策」入口', () => {
  const store = useAppStore();
  store.initialized = true;
  const wrapper = mount(App, { global: { stubs } });

  expect(wrapper.find('[data-test="decision-entry"]').exists()).toBe(false);
  wrapper.unmount();
});

it('对话标题栏渲染「决策」入口，点击展开/收起决策记录面板', async () => {
  const store = useAppStore();
  store.initialized = true;
  store.decisionHasRecords = true;
  const wrapper = mount(App, { global: { stubs } });

  const entry = wrapper.get('[data-test="decision-entry"]');
  expect(entry.text()).toContain('决策');
  expect(wrapper.find('[data-test="decision-dialog"]').exists()).toBe(false);

  await entry.trigger('click');
  expect(wrapper.find('[data-test="decision-dialog"]').exists()).toBe(true);

  await entry.trigger('click');
  expect(wrapper.find('[data-test="decision-dialog"]').exists()).toBe(false);
  wrapper.unmount();
});

it('决策未读数字徽章已移除：本会话决策只点亮入口，不再挂数字', async () => {
  const store = useAppStore();
  store.initialized = true;
  store.currentSessionName = 'sess-A';
  const wrapper = mount(App, { global: { stubs } });

  expect(wrapper.find('[data-test="decision-entry"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="decision-entry-badge"]').exists()).toBe(false);

  store.handleBridgeEvent({ type: 'decision_recorded', data: { scene: 'router', session_id: 'sess-A' } } as any);
  store.handleBridgeEvent({ type: 'decision_recorded', data: { scene: 'router', session_id: 'sess-A' } } as any);
  await wrapper.vm.$nextTick();

  // 入口出现，但数字徽章不再渲染
  expect(wrapper.find('[data-test="decision-entry"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="decision-entry-badge"]').exists()).toBe(false);

  await wrapper.find('[data-test="decision-entry"]').trigger('click');
  await flushPromises();
  expect(store.decisionUnread).toBe(0);
  wrapper.unmount();
});

it('空白新会话不渲染「决策」入口：会话还没开始就不能被上一个会话的决策点亮', async () => {
  const store = useAppStore();
  store.initialized = true;
  store.currentSessionName = 'sess-A';
  store.decisionHasRecords = true;
  store.decisionUnread = 5;

  await store.createGlobalSession();
  const wrapper = mount(App, { global: { stubs } });

  expect(store.decisionHasRecords).toBe(false);
  expect(store.decisionUnread).toBe(0);
  expect(wrapper.find('[data-test="decision-entry"]').exists()).toBe(false);
  wrapper.unmount();
});

it('refreshDecisionAvailability 按会话名探测：has_business_records=false 时入口保持隐藏', async () => {
  const api = mockDecisionApi({
    get_decision_audit_summary: vi.fn(async () => ({ ok: true, has_business_records: false })),
  });
  const store = useAppStore();

  await store.refreshDecisionAvailability('sess-A');

  // 口径：limit=1 + 目标会话名（不能退化成全局口径）
  expect(api.get_decision_audit_summary).toHaveBeenCalledTimes(1);
  expect(api.get_decision_audit_summary).toHaveBeenCalledWith(1, 'sess-A');
  expect(store.decisionHasRecords).toBe(false);
});

it('refreshDecisionAvailability 连续探测：第二次先复位，不残留上一次的 true', async () => {
  const summary = vi.fn()
    .mockResolvedValueOnce({ ok: true, has_business_records: true })
    .mockResolvedValueOnce({ ok: true, has_business_records: false });
  const api = mockDecisionApi({ get_decision_audit_summary: summary });
  const store = useAppStore();

  await store.refreshDecisionAvailability('sess-A');
  expect(store.decisionHasRecords).toBe(true);
  expect(api.get_decision_audit_summary).toHaveBeenLastCalledWith(1, 'sess-A');

  // 切到没有决策记录的会话：入口必须熄灭
  await store.refreshDecisionAvailability('sess-B');
  expect(api.get_decision_audit_summary).toHaveBeenLastCalledWith(1, 'sess-B');
  expect(store.decisionHasRecords).toBe(false);
});

it('refreshDecisionAvailability 会话名为空时不请求后端，且入口保持隐藏', async () => {
  const api = mockDecisionApi();
  const store = useAppStore();
  // 上一会话留下的 true 必须被复位
  store.decisionHasRecords = true;

  await store.refreshDecisionAvailability('   ');

  expect(api.get_decision_audit_summary).not.toHaveBeenCalled();
  expect(store.decisionHasRecords).toBe(false);

  await store.refreshDecisionAvailability();
  expect(api.get_decision_audit_summary).not.toHaveBeenCalled();
  expect(store.decisionHasRecords).toBe(false);
});

it('decision_recorded 带其他会话 session_id：只推进 tick，不累加未读也不点亮入口', () => {
  const store = useAppStore();
  store.currentSessionName = 'sess-A';
  store.decisionUnread = 0;
  store.decisionHasRecords = false;
  const tickBefore = store.decisionEventTick;

  store.handleBridgeEvent({ type: 'decision_recorded', data: { session_id: 'sess-B' } } as any);

  expect(store.decisionEventTick).toBe(tickBefore + 1);
  expect(store.decisionUnread).toBe(0);
  expect(store.decisionHasRecords).toBe(false);

  store.handleBridgeEvent({ type: 'decision_recorded', data: { session_id: 'sess-A' } } as any);

  expect(store.decisionEventTick).toBe(tickBefore + 2);
  expect(store.decisionUnread).toBe(1);
  expect(store.decisionHasRecords).toBe(true);
});

it('打开「星链」与「决策」悬浮面板互斥，不会同时叠层显示', async () => {
  const store = useAppStore();
  store.initialized = true;
  store.decisionHasRecords = true;
  store.planData = {
    title: '任务',
    progress: 0,
    items: [{ id: '1', text: '第一步', status: 'in_progress' }],
  };
  const wrapper = mount(App, { global: { stubs } });

  await wrapper.get('[data-test="task-entry"]').trigger('click');
  expect(wrapper.find('[data-test="task-dialog"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="decision-dialog"]').exists()).toBe(false);

  await wrapper.get('[data-test="decision-entry"]').trigger('click');
  expect(wrapper.find('[data-test="task-dialog"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="decision-dialog"]').exists()).toBe(true);

  await wrapper.get('[data-test="task-entry"]').trigger('click');
  expect(wrapper.find('[data-test="task-dialog"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="decision-dialog"]').exists()).toBe(false);
  wrapper.unmount();
});

it('后台任务入口收进会话头部，点击向下展开卡片且与「星链 / 决策」互斥', async () => {
  const store = useAppStore();
  store.initialized = true;
  store.currentSessionName = 'sess-A';
  store.backgroundTasks = [
    { task_id: 't1', command: 'pytest -q', status: 'running', running_time: '10s', session_id: 'sess-A' },
  ];
  store.planData = {
    title: '任务',
    progress: 0,
    items: [{ id: '1', text: '第一步', status: 'in_progress' }],
  };
  const wrapper = mount(App, { global: { stubs } });

  const header = wrapper.get('#chat-header');
  expect(header.find('[data-test="bg-bar"]').exists()).toBe(true);
  expect(header.find('[data-test="bg-bar-count"]').text()).toBe('1');

  await header.get('[data-test="bg-bar-details"]').trigger('click');
  expect(wrapper.find('[data-test="bg-card"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="task-dialog"]').exists()).toBe(false);

  await header.get('[data-test="task-entry"]').trigger('click');
  expect(wrapper.find('[data-test="task-dialog"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="bg-card"]').exists()).toBe(false);

  await header.get('[data-test="bg-bar-details"]').trigger('click');
  expect(wrapper.find('[data-test="bg-card"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="task-dialog"]').exists()).toBe(false);
  wrapper.unmount();
});