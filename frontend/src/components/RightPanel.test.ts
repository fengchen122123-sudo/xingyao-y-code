import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import { mount } from '@vue/test-utils';

import RightPanel from './RightPanel.vue';
import { useAppStore } from '../stores/appStore';
import { useGitStore } from '../stores/gitStore';

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('桌面端根元素内联宽度跟随 store.rightPanelWidth，折叠时负 margin 跟随实际宽度', async () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  store.rightPanelWidth = 400;
  const wrapper = mount(RightPanel, { global: { plugins: [pinia] } });

  const root = wrapper.find('#rightbar');
  expect(root.attributes('style')).toContain('width: 400px');
  expect(root.attributes('style')).toContain('--rp-width: 400px');
  expect(root.attributes('style')).not.toContain('margin-right');

  store.rightPanelCollapsed = true;
  await nextTick();
  expect(root.attributes('style')).toContain('margin-right: -400px');
  wrapper.unmount();
});

it('浏览器操控不自动弹出操控页签（右侧只留可围观的浏览器）', async () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel, { global: { plugins: [pinia] } });

  store.computerRun = {
    ...store.computerRun,
    run_id: 'run-1',
    status: 'running',
    target: 'browser',
    browser: 'embedded',
    browser_mode: 'embedded',
  };
  await nextTick();
  await nextTick();

  expect(store.rightPanelTabs).not.toContain('computer');
  wrapper.unmount();
});

it('桌面操控仍自动登记操控页签（回归：只对浏览器目标收口）', async () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel, { global: { plugins: [pinia] } });

  store.computerRun = {
    ...store.computerRun,
    run_id: 'run-1',
    status: 'running',
    target: 'desktop',
  };
  await nextTick();
  await nextTick();

  expect(store.rightPanelTabs).toContain('computer');
  wrapper.unmount();
});

it('图片预览：kind=image 时弹窗正文渲染 <img>，文本代码块不出现', async () => {
  const store = useAppStore();
  store.filePreview = {
    show: true,
    path: 'D:\\proj\\pic.png',
    kind: 'image',
    content: '',
    dataUrl: 'data:image/png;base64,iVBORw0KGgo=',
    truncated: false,
    loading: false,
  };
  const wrapper = mount(RightPanel, { global: { stubs: { teleport: true } } });

  const img = wrapper.find('.file-preview-modal img.fp-image');
  expect(img.exists()).toBe(true);
  expect(img.attributes('src')).toBe('data:image/png;base64,iVBORw0KGgo=');
  expect(wrapper.find('.file-preview-modal pre.fp-pre').exists()).toBe(false);
  wrapper.unmount();
});

it('文本预览：kind=text 时仍渲染代码块，不出现 <img>', async () => {
  const store = useAppStore();
  store.filePreview = {
    show: true,
    path: 'D:\\proj\\foo.txt',
    kind: 'text',
    content: 'hello',
    dataUrl: '',
    truncated: false,
    loading: false,
  };
  const wrapper = mount(RightPanel, { global: { stubs: { teleport: true } } });

  expect(wrapper.find('.file-preview-modal pre.fp-pre').exists()).toBe(true);
  expect(wrapper.find('.file-preview-modal img.fp-image').exists()).toBe(false);
  wrapper.unmount();
});

it('中性入口展开面板：无页签先显示选择页卡片，点卡片开出对应页签', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel);
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(false);

  store.toggleRightPanel();
  await nextTick();
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(true);
  expect(wrapper.findAll('.tp-card').map(c => c.text().trim()))
    .toEqual(['文件', 'Git', '文档', '星眼 · 浏览器', '星港 · 后台任务', '星舰 · 子代理', '小说']);

  await wrapper.find('[data-test="picker-card-files"]').trigger('click');
  await nextTick();
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(false);
  const tab = wrapper.find('[data-test="tab-files"]');
  expect(tab.exists()).toBe(true);
  expect(tab.classes()).toContain('active');
  expect(wrapper.find('[data-test="tabbar-add"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="tabbar-collapse"]').exists()).toBe(true);
  wrapper.unmount();
});

it('浏览器卡片开出浏览器页签并挂载 BrowserPane', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel, { global: { stubs: { BrowserPane: true } } });

  store.toggleRightPanel();
  await nextTick();
  await wrapper.find('[data-test="picker-card-browser"]').trigger('click');
  await nextTick();

  const pane = wrapper.find('[data-test="browser-pane"]');
  expect(pane.exists()).toBe(true);
  expect(pane.isVisible()).toBe(true);
  expect(pane.find('browser-pane-stub').exists()).toBe(true);
expect(wrapper.find('[data-test="tab-browser"]').classes()).toContain('active');
  wrapper.unmount();
});

it('文档卡片开出文档页签并挂载 DocumentPane（不挂浏览器表面）', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel, { global: { stubs: { BrowserPane: true } } });

  store.toggleRightPanel();
  await nextTick();
  await wrapper.find('[data-test="picker-card-doc"]').trigger('click');
  await nextTick();

  const pane = wrapper.find('[data-test="doc-pane"]');
  expect(pane.exists()).toBe(true);
  expect(pane.isVisible()).toBe(true);
  expect(pane.find('[data-test="doc-pane-root"]').exists()).toBe(true);
  expect(pane.find('[data-test="dc-empty"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="tab-doc"]').classes()).toContain('active');
  expect(wrapper.find('[data-test="browser-pane"]').attributes('style')).toContain('display: none');
  wrapper.unmount();
});

it('小说卡片开出小说页签并挂载 NovelPane', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel, { global: { stubs: { NovelPane: true } } });

  store.toggleRightPanel();
  await nextTick();
  await wrapper.find('[data-test="picker-card-novel"]').trigger('click');
  await nextTick();

  const wrap = wrapper.find('[data-test="novel-pane-wrap"]');
  expect(wrap.exists()).toBe(true);
  expect(wrap.isVisible()).toBe(true);
  expect(wrapper.find('[data-test="tab-novel"]').classes()).toContain('active');
  wrapper.unmount();
});

it('多页签并存：开浏览器再开 Git 互不覆盖，点页签切回', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel, { global: { stubs: { BrowserPane: true } } });

  store.openRightPanelTab('browser');
  await nextTick();
  expect(wrapper.find('[data-test="browser-pane"]').isVisible()).toBe(true);

  store.openRightPanelTab('git');
  await nextTick();
  expect(store.rightPanelTabs).toEqual(['browser', 'git']);
  expect(store.rightPanelActiveTab).toBe('git');
  expect(wrapper.find('[data-test="git-pane"]').isVisible()).toBe(true);
  expect(wrapper.find('[data-test="browser-pane"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="browser-pane"]').attributes('style')).toContain('display: none');

  await wrapper.find('[data-test="tab-browser"]').trigger('click');
  await nextTick();
  expect(wrapper.find('[data-test="browser-pane"]').isVisible()).toBe(true);
  expect(wrapper.find('[data-test="git-pane"]').attributes('style')).toContain('display: none');
  expect(store.rightPanelTabs).toEqual(['browser', 'git']);
  wrapper.unmount();
});

it('页签可单独关闭：关闭激活页签切到邻居，全部关完回选择页', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel);

  store.openRightPanelTab('browser');
  store.openRightPanelTab('git');
  await nextTick();
  expect(store.rightPanelActiveTab).toBe('git');

  await wrapper.find('[data-test="tab-close-git"]').trigger('click');
  await nextTick();
  expect(store.rightPanelTabs).toEqual(['browser']);
  expect(store.rightPanelActiveTab).toBe('browser');
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(false);

  await wrapper.find('[data-test="tab-close-browser"]').trigger('click');
  await nextTick();
  expect(store.rightPanelTabs).toEqual([]);
  expect(store.rightPanelActiveTab).toBe('');
  expect(store.rightPanelCollapsed).toBe(false);
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(true);
  wrapper.unmount();
});

it('“+”回选择页追加页签，已开页签保留在页签条上', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel);

  store.openRightPanelTab('files');
  await nextTick();
  await wrapper.find('[data-test="tabbar-add"]').trigger('click');
  await nextTick();
  expect(store.rightPanelPicker).toBe(true);
  expect(store.rightPanelTabs).toEqual(['files']);

  await wrapper.find('[data-test="picker-card-git"]').trigger('click');
  await nextTick();
  expect(store.rightPanelTabs).toEqual(['files', 'git']);
  expect(store.rightPanelActiveTab).toBe('git');
  expect(wrapper.find('[data-test="tab-files"]').exists()).toBe(true);
  wrapper.unmount();
});

it('页签条 × 收起整个面板，再次中性展开回到激活页签', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel);

  store.openRightPanelTab('git');
  await nextTick();
  await wrapper.find('[data-test="tabbar-collapse"]').trigger('click');
  await nextTick();
  expect(store.rightPanelCollapsed).toBe(true);
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(false);

  store.toggleRightPanel();
  await nextTick();
  expect(store.rightPanelCollapsed).toBe(false);
  expect(store.rightPanelPicker).toBe(false);
  expect(wrapper.find('[data-test="git-pane"]').isVisible()).toBe(true);
  wrapper.unmount();
});

it('收起时保留收起前的页签画面到滑出动画结束，避免空面板平移', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel);
  const paneStyle = () => wrapper.find('[data-test="git-pane"]').attributes('style') ?? '';

  store.openRightPanelTab('git');
  await nextTick();
  expect(paneStyle()).not.toContain('display: none');

  store.toggleRightPanel();
  await nextTick();
  expect(store.rightPanelCollapsed).toBe(true);
  expect(paneStyle()).not.toContain('display: none');

  await new Promise(resolve => setTimeout(resolve, 300));
  await nextTick();
  expect(paneStyle()).toContain('display: none');

  wrapper.unmount();
});

it('执行条详情打开后台任务页签并显示命令', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel);

  store.currentSessionName = 's1';
  store.backgroundTasks = [{ task_id: 't9', command: 'pytest -q', status: 'running', session_id: 's1' }];
  store.openBackgroundTaskDetail('t9');
  await nextTick();

  expect(store.bgTaskOpenNonce).toBe(1);
  expect(store.bgTaskDetailId).toBe('t9');
  expect(wrapper.find('[data-test="bg-task-pane"]').isVisible()).toBe(true);
  expect(wrapper.find('[data-test="bg-task-log"]').text()).toContain('正在读取任务输出');
  wrapper.unmount();
});

function subagentDetailPayload() {
  return {
    ok: true,
    task: {
      id: 'sub-1', description: '调研代码结构', type: 'explore', status: 'running',
      stage: 'tool', elapsed: 12, background: false, started_at: 1758000000,
      stages: [
        { name: 'model', at: 1758000000, detail: '' },
        { name: 'tool', at: 1758000005, detail: 'read_file' },
        { name: 'tool', at: 1758000006, detail: 'read_file ✓ 1.2s' },
      ],
    },
    tasks: [
      { id: 'sub-1', description: '调研代码结构', type: 'explore', status: 'running', stage: 'tool', elapsed: 12, background: false },
      { id: 'sub-2', description: '整理测试报告', type: 'general', status: 'completed', stage: 'completed', elapsed: 30, background: true },
    ],
  };
}

it('子代理查看打开子代理页签：头部信息 + 执行行 + 任务切换条', async () => {
  const getDetail = vi.fn().mockResolvedValue(subagentDetailPayload());
  (window as any).pywebview = { api: { get_subagent_task_detail: getDetail } };
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel);

  store.openSubagentDetail('sub-1');
  await nextTick();

  expect(store.subagentOpenNonce).toBe(1);
  expect(wrapper.find('[data-test="subagent-pane"]').isVisible()).toBe(true);

  await vi.waitFor(() => {
    expect(wrapper.findAll('[data-test="subagent-flow"] .suba-row').length).toBeGreaterThan(0);
  });
  expect(getDetail).toHaveBeenCalledWith('sub-1', store.currentSessionName);
  expect(wrapper.text()).toContain('调研代码结构');
  const rows = wrapper.findAll('[data-test="subagent-flow"] .suba-row');
  expect(rows).toHaveLength(2);
  expect(rows[1].text()).toContain('read_file');
  expect(rows[1].text()).toContain('✓ 1.2s');
  expect(wrapper.text()).toContain('轮次 1');
  expect(wrapper.text()).toContain('工具 1 成功');
  await wrapper.get('[data-test="subagent-switcher-toggle"]').trigger('click');
  const chips = wrapper.findAll('.suba-chip');
  expect(chips).toHaveLength(2);
  await chips[1].trigger('click');
  expect(store.subagentDetailId).toBe('sub-2');
  wrapper.unmount();
});

it('目标任务已被清理（ok=false）时显示未找到提示而非空白', async () => {
  const getDetail = vi.fn().mockResolvedValue({
    ok: false, task: null, tasks: [], msg: '未找到该子代理任务（可能已被清理）',
  });
  (window as any).pywebview = { api: { get_subagent_task_detail: getDetail } };
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel);

  store.openSubagentDetail('gone-1');
  await nextTick();

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="subagent-missing"]').text()).toContain('未找到');
  });
  wrapper.unmount();
});

it('run 出现后自动切入操控页签并校准快照，关闭页签后事件不抢回，新 run 再开一次', async () => {
  const store = useAppStore();
  const getStatus = vi.fn(async () => ({
    run_id: store.computerRun.run_id,
    status: 'paused',
    pause_reason: 'user_pause',
  }));
  (window as any).pywebview = { api: { get_computer_control_status: getStatus } };
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel);

  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'starting' });
  await vi.waitFor(() => expect(store.computerLastAutoOpenedRunId).toBe('run-1'));
  await nextTick();

  expect(wrapper.find('[data-test="computer-pane"]').isVisible()).toBe(true);
  expect(store.rightPanelPicker).toBe(false);
  expect(store.rightPanelActiveTab).toBe('computer');
  expect(store.computerLastAutoOpenedRunId).toBe('run-1');
  await vi.waitFor(() => expect(store.computerRun.status).toBe('paused'));
  expect(store.computerRun.pause_reason).toBe('user_pause');

  await wrapper.find('[data-test="tab-close-computer"]').trigger('click');
  await nextTick();
  expect(store.rightPanelPicker).toBe(true);
  expect(store.computerRun.run_id).toBe('run-1');

  store.reduceComputerEvent({ type: 'computer_observation', run_id: 'run-1', event_seq: 2, action_ordinal: 1, preview_url: '/computer-preview/t/1.png', origin_or_window: 'example.com' });
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 3, status: 'stopped' });
  await nextTick();
  expect(store.rightPanelPicker).toBe(true);

  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-2', event_seq: 1, status: 'starting' });
  await vi.waitFor(() => expect(store.computerLastAutoOpenedRunId).toBe('run-2'));
  await nextTick();
  expect(store.rightPanelPicker).toBe(false);
  expect(wrapper.find('[data-test="computer-pane"]').isVisible()).toBe(true);
  expect(store.computerLastAutoOpenedRunId).toBe('run-2');
  wrapper.unmount();
});

it('操控终态 60 秒且用户已关闭页签时自动收场（页签随收场关闭）', async () => {
  vi.useFakeTimers();
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel);

  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running' });
  await nextTick();
  await wrapper.find('[data-test="tab-close-computer"]').trigger('click');
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 2, status: 'completed' });
  await nextTick();
  expect(store.computerRun.run_id).toBe('run-1');

  vi.advanceTimersByTime(59999);
  await nextTick();
  expect(store.computerRun.run_id).toBe('run-1');

  vi.advanceTimersByTime(1);
  await nextTick();
  expect(store.computerRun.run_id).toBe('');
  expect(store.rightPanelPicker).toBe(true);

  wrapper.unmount();
  vi.useRealTimers();
});

it('用户停留在操控页签时到点顺延，不被自动关闭', async () => {
  vi.useFakeTimers();
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel);
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running' });
  await nextTick();
  expect(wrapper.find('[data-test="computer-pane"]').isVisible()).toBe(true);
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 2, status: 'stopped' });
  await nextTick();

  vi.advanceTimersByTime(60000);
  await nextTick();
  expect(store.computerRun.run_id).toBe('run-1');
  vi.advanceTimersByTime(60000);
  await nextTick();
  expect(store.computerRun.run_id).toBe('run-1');

  wrapper.unmount();
  vi.useRealTimers();
});

it('鼠标悬停在右面板内时到点顺延，移开后下一个周期关闭', async () => {
  vi.useFakeTimers();
  const store = useAppStore();
  const wrapper = mount(RightPanel);
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running' });
  await nextTick();
  await wrapper.find('#rightbar').trigger('mouseenter');
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 2, status: 'failed' });
  await nextTick();

  vi.advanceTimersByTime(60000);
  await nextTick();
  expect(store.computerRun.run_id).toBe('run-1');

  await wrapper.find('#rightbar').trigger('mouseleave');
  vi.advanceTimersByTime(60000);
  await nextTick();
  expect(store.computerRun.run_id).toBe('');

  wrapper.unmount();
  vi.useRealTimers();
});

it('Git 分支项直达 Git 页签，浏览器页签并存不覆盖', async () => {
  const store = useAppStore();
  const gitStore = useGitStore();
  const wrapper = mount(RightPanel, { global: { stubs: { BrowserPane: true } } });

  store.openRightPanelTab('browser');
  await nextTick();

  gitStore.requestOpenPanel();
  store.openRightPanelTab('git');
  await nextTick();

  expect(store.rightPanelPicker).toBe(false);
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="git-pane"]').isVisible()).toBe(true);
  expect(store.rightPanelTabs).toEqual(['browser', 'git']);
  wrapper.unmount();
});

it('画笔截图：无桥时不进入截图模式并提示；桥正常时先捕获再挂覆盖层', async () => {
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel, { global: { stubs: { BrowserPane: true } } });

  expect(wrapper.find('[data-test="tabbar-snip"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="snip-overlay"]').exists()).toBe(false);

  await wrapper.find('[data-test="tabbar-snip"]').trigger('click');
  await nextTick();
  expect(store.rightPanelSnipActive).toBe(false);
  expect(wrapper.find('[data-test="snip-overlay"]').exists()).toBe(false);

  let captureCalled = false;
  (window as any).pywebview = {
    api: {
      capture_right_panel_region: vi.fn(async () => {
        captureCalled = true;
        return { ok: true, data_url: 'data:image/png;base64,AAAA', width: 10, height: 10 };
      }),
    },
  };
  await wrapper.find('[data-test="tabbar-snip"]').trigger('click');
  await nextTick();
  await nextTick();
  expect(captureCalled).toBe(true);
  expect(store.rightPanelSnipActive).toBe(true);
  expect(wrapper.find('[data-test="snip-overlay"]').exists()).toBe(true);

  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  await nextTick();
  expect(store.rightPanelSnipActive).toBe(false);
  wrapper.unmount();
});

it('画笔截图：捕获失败不进入截图模式', async () => {
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  (window as any).pywebview = {
    api: {
      capture_right_panel_region: vi.fn(async () => ({ ok: false, msg: '屏幕捕获失败' })),
    },
  };
  const wrapper = mount(RightPanel, { global: { stubs: { BrowserPane: true } } });

  await wrapper.find('[data-test="tabbar-snip"]').trigger('click');
  await nextTick();
  await nextTick();
  expect(store.rightPanelSnipActive).toBe(false);
  expect(wrapper.find('[data-test="snip-overlay"]').exists()).toBe(false);
  wrapper.unmount();
});

it('后台任务详情头部与列表同口径：只认当前会话任务，不显示其他会话的命令', async () => {
  const store = useAppStore();
  store.currentSessionName = 's2';
  store.backgroundTasks = [
    { task_id: 't1', command: '别的会话命令', status: 'running', session_id: 's1' },
  ];
  store.bgTaskDetailId = 't1';
  const wrapper = mount(RightPanel, { global: { stubs: { BrowserPane: true } } });

  store.openRightPanelTab('tasks');
  await nextTick();

  expect(wrapper.find('[data-test="bg-task-list"]').exists()).toBe(false);
  expect(wrapper.find('.bgt-cmd').text()).toBe('t1');
  wrapper.unmount();
});

it('run 首个事件带归属时操控页签直接写进归属会话，不碰当前查看的会话', async () => {
  const store = useAppStore();
  store.currentSessionName = 'B';
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel, { global: { stubs: { BrowserPane: true } } });

  store.reduceComputerEvent({
    type: 'computer_state', run_id: 'run-1', event_seq: 1,
    status: 'running', owner_session_id: 'A',
  } as any);
  await nextTick();

  expect(store.computerRun.owner_session_id).toBe('A');
  expect(store.rightPanelTabs).not.toContain('computer');
  const runtimeA = (store as any).sessionRuntimeByName['A'];
  expect(runtimeA.rightPanelTabs).toContain('computer');
  wrapper.unmount();
});

it('事件缺归属时先校准快照再开页签，绝不回落当前查看的会话', async () => {
  const store = useAppStore();
  store.currentSessionName = 'B';
  store.rightPanelCollapsed = false;
  (window as any).pywebview = {
    api: {
      get_computer_control_status: async () => ({
        ok: true, run_id: 'run-1', owner_session_id: 'A', status: 'running',
      }),
    },
  };
  const wrapper = mount(RightPanel, { global: { stubs: { BrowserPane: true } } });

  store.reduceComputerEvent({
    type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running',
  } as any);

  await vi.waitFor(() => {
    const runtimeA = (store as any).sessionRuntimeByName?.['A'];
    expect(runtimeA?.rightPanelTabs || []).toContain('computer');
  });
  expect(store.rightPanelTabs).not.toContain('computer');
  wrapper.unmount();
});

it('run 归属其他会话时操控页签写入归属会话，不污染当前查看的会话', async () => {
  const store = useAppStore();
  store.currentSessionName = 'B';
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel, { global: { stubs: { BrowserPane: true } } });

  store._applyComputerSnapshot({ run_id: 'run-1', status: 'running', owner_session_id: 'A' });
  await nextTick();

  expect(store.rightPanelTabs).not.toContain('computer');
  const runtimeA = (store as any).sessionRuntimeByName['A'];
  expect(runtimeA.rightPanelTabs).toContain('computer');
  expect(runtimeA.rightPanelActiveTab).toBe('computer');
  wrapper.unmount();
});

it('QQ秀：默认主题（guiTheme 为 null）停在选择页时不渲染 QQ秀块', async () => {
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel);

  expect(store.guiTheme).toBeNull();
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="qq09-show"]').exists()).toBe(false);
  wrapper.unmount();
});

it('QQ秀：内置 QQ2009 主题下选择页渲染动图舞台与名牌', async () => {
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  store.guiTheme = { id: 'builtin.qq2009' } as any;
  const wrapper = mount(RightPanel);

  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(true);
  const show = wrapper.find('[data-test="qq09-show"]');
  expect(show.exists()).toBe(true);
  const imgs = show.findAll('img');
  expect(imgs).toHaveLength(2);
  expect(imgs.map(i => i.attributes('src'))).toEqual(['/qqshow-fzl.gif', '/qqshow-fzl.gif']);
  expect(show.find('.qq09-show-nameplate').text()).toContain('非主流动图秀');
  wrapper.unmount();
});

it('QQ秀：QQ2009 主题下打开具体标签页（选择页隐藏）时不渲染 QQ秀块', async () => {
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  store.guiTheme = { id: 'builtin.qq2009' } as any;
  const wrapper = mount(RightPanel);

  expect(wrapper.find('[data-test="qq09-show"]').exists()).toBe(true);

  store.openRightPanelTab('files');
  await nextTick();
  expect(store.rightPanelPicker).toBe(false);
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="qq09-show"]').exists()).toBe(false);
  wrapper.unmount();
});

it('子代理开关关闭：选择页不出子代理卡片，重新打开即恢复', async () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel, { global: { plugins: [pinia] } });

  store.showRightPanelPicker();
  await nextTick();
  expect(wrapper.find('[data-test="picker-card-subagent"]').exists()).toBe(true);

  store.systemSettings.subagent_enabled = false;
  await nextTick();
  expect(wrapper.find('[data-test="picker-card-subagent"]').exists()).toBe(false);
  expect(wrapper.findAll('.tp-card').map(c => c.text().trim())).not.toContain('星舰 · 子代理');

  store.systemSettings.subagent_enabled = true;
  await nextTick();
  expect(wrapper.find('[data-test="picker-card-subagent"]').exists()).toBe(true);
  wrapper.unmount();
});

it('外部浏览器（Edge/Chrome）run 打开 owner_session 绑定的操控预览页签，不抢焦点、不改宽、不推开手动折叠', async () => {
  const store = useAppStore();
  store.currentSessionName = 'B';
  store.rightPanelWidth = 330;
  store.rightPanelCollapsed = false;
  store.toggleRightPanel();
  expect(store.rightPanelCollapsed).toBe(true);

  const btn = document.createElement('button');
  document.body.appendChild(btn);
  btn.focus();

  const wrapper = mount(RightPanel, { attachTo: document.body, global: { stubs: { BrowserPane: true } } });
  store.reduceComputerEvent({
    type: 'computer_state',
    run_id: 'run-edge-1',
    run_generation: 1,
    event_seq: 1,
    status: 'running',
    target: 'browser',
    browser: 'edge',
    browser_mode: 'external',
    owner_session_id: 'A',
  } as any);
  await nextTick();

  expect(store.currentSessionName).toBe('B');
  expect(document.activeElement).toBe(btn);
  expect(store.rightPanelCollapsed).toBe(true);
  expect(store.rightPanelWidth).toBe(330);
  const runtimeA = (store as any).sessionRuntimeByName['A'];
  expect(runtimeA.rightPanelTabs).toContain('computer');
  expect(runtimeA.rightPanelActiveTab).toBe('computer');

  wrapper.unmount();
  btn.remove();
});

it('内置浏览器（embedded）run 跳过打开操控页签，由浏览器页签承接', async () => {
  const store = useAppStore();
  store.currentSessionName = 'A';
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel, { global: { stubs: { BrowserPane: true } } });

  store.reduceComputerEvent({
    type: 'computer_state',
    run_id: 'run-emb-1',
    run_generation: 1,
    event_seq: 1,
    status: 'running',
    target: 'browser',
    browser: 'embedded',
    browser_mode: 'embedded',
    owner_session_id: 'A',
  } as any);
  await nextTick();

  expect(store.rightPanelTabs).not.toContain('computer');
  expect(store.computerRun.browser_mode).toBe('embedded');
  wrapper.unmount();
});
