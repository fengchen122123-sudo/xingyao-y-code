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
  // 面板默认收起（appStore.rightPanelDefault 测试锁定）；本用例验证展开态宽度
  store.rightPanelCollapsed = false;
  store.rightPanelWidth = 400;
  const wrapper = mount(RightPanel, { global: { plugins: [pinia] } });

  const root = wrapper.find('#rightbar');
  expect(root.attributes('style')).toContain('width: 400px');
  expect(root.attributes('style')).not.toContain('margin-right');

  store.rightPanelCollapsed = true;
  await nextTick();
  expect(root.attributes('style')).toContain('margin-right: -400px');
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
  // 弹窗 Teleport 到 body：stub 掉 Teleport 让内容留在 wrapper 内可查
  const wrapper = mount(RightPanel, { global: { stubs: { teleport: true } } });

  const img = wrapper.find('.file-preview-modal img.fp-image');
  expect(img.exists()).toBe(true);
  expect(img.attributes('src')).toBe('data:image/png;base64,iVBORw0KGgo=');
  // 图片分支下不应渲染文本代码块
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

// ── 打开标签页选择页（ZCode 式侧板，页签栏已移除） ──

function paneTitle(wrapper: ReturnType<typeof mount>) {
  return wrapper.find('[data-test="pane-title"]').text().trim();
}

it('中性入口展开面板时先显示选择页三卡，点卡片进入对应工作台', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel);
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(false);

  store.toggleRightPanel();
  await nextTick();
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(true);
  // 页签栏已移除：选择页期间不渲染工台头
  expect(wrapper.find('.pane-head').exists()).toBe(false);
  expect(wrapper.findAll('.tp-card').map(c => c.text().trim())).toEqual(['文件', 'Git', '浏览器']);

  await wrapper.find('[data-test="picker-card-files"]').trigger('click');
  await nextTick();
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(false);
  expect(paneTitle(wrapper)).toBe('文件');
  expect(wrapper.find('[data-test="pane-close"]').exists()).toBe(true);
  wrapper.unmount();
});

it('浏览器卡片进入浏览器工作台，无操控会话时呈现引导态', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel);

  store.toggleRightPanel();
  await nextTick();
  await wrapper.find('[data-test="picker-card-browser"]').trigger('click');
  await nextTick();

  const pane = wrapper.find('[data-test="browser-pane"]');
  expect(pane.exists()).toBe(true);
  expect(pane.isVisible()).toBe(true);
  expect(pane.find('[data-test="computer-empty-state"]').exists()).toBe(true);
  expect(paneTitle(wrapper)).toBe('浏览器');
  wrapper.unmount();
});

it('工台头 × 返回选择页，可再次选卡切换工作台', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel);

  store.toggleRightPanel();
  await nextTick();
  await wrapper.find('[data-test="picker-card-files"]').trigger('click');
  await nextTick();
  expect(paneTitle(wrapper)).toBe('文件');

  await wrapper.find('[data-test="pane-close"]').trigger('click');
  await nextTick();
  expect(store.rightPanelPicker).toBe(true);
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(true);

  await wrapper.find('[data-test="picker-card-git"]').trigger('click');
  await nextTick();
  expect(paneTitle(wrapper)).toBe('Git');
  expect(wrapper.find('[data-test="git-pane"]').isVisible()).toBe(true);
  wrapper.unmount();
});

it('定向入口 expandRightPanelDirect 跳过选择页，Git 分支项直达 Git 工作台', async () => {
  const store = useAppStore();
  const gitStore = useGitStore();
  const wrapper = mount(RightPanel);

  gitStore.requestOpenPanel();
  store.expandRightPanelDirect();
  await nextTick();

  expect(store.rightPanelPicker).toBe(false);
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="git-pane"]').isVisible()).toBe(true);
  wrapper.unmount();
});

it('收起面板即离开选择页，再次中性展开重新显示', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel);

  store.toggleRightPanel();
  await nextTick();
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(true);

  store.toggleRightPanel();
  await nextTick();
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(false);

  store.toggleRightPanel();
  await nextTick();
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(true);
  wrapper.unmount();
});

// ── 后台任务执行过程工作台 ──

it('执行条详情 nonce 打开后台任务工作台并显示命令', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel);

  store.backgroundTasks = [{ task_id: 't9', command: 'pytest -q', status: 'running' }];
  store.bgTaskDetailId = 't9';
  store.bgTaskOpenNonce += 1;
  await nextTick();

  expect(store.rightPanelPicker).toBe(false);
  expect(wrapper.find('[data-test="bg-task-pane"]').isVisible()).toBe(true);
  expect(wrapper.find('[data-test="pane-title"]').text()).toBe('后台任务');
  expect(wrapper.find('[data-test="bg-task-log"]').text()).toContain('正在读取任务输出');
  wrapper.unmount();
});

// ── 操控工作台（设计 16.2：run 出现自动切入，不进选择页） ──

it('run 出现后自动切入操控工作台并校准快照，返回选择页后事件不抢回，新 run 再切一次', async () => {
  const getStatus = vi.fn().mockResolvedValue({ run_id: 'run-1', status: 'paused', pause_reason: 'user_pause' });
  (window as any).pywebview = { api: { get_computer_control_status: getStatus } };
  const store = useAppStore();
  const wrapper = mount(RightPanel);

  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'starting' });
  await nextTick();

  expect(wrapper.find('[data-test="computer-pane"]').isVisible()).toBe(true);
  expect(paneTitle(wrapper)).toBe('操控');
  expect(store.rightPanelPicker).toBe(false);
  expect(store.computerLastAutoOpenedRunId).toBe('run-1');
  // 进入操控工作台触发快照校准
  await vi.waitFor(() => expect(store.computerRun.status).toBe('paused'));
  expect(store.computerRun.pause_reason).toBe('user_pause');

  // 用户点工台头 × 返回选择页：run 不终止，同一 run 后续事件不把人拽回去
  await wrapper.find('[data-test="pane-close"]').trigger('click');
  await nextTick();
  expect(store.rightPanelPicker).toBe(true);
  expect(store.computerRun.run_id).toBe('run-1');

  store.reduceComputerEvent({ type: 'computer_observation', run_id: 'run-1', event_seq: 2, action_ordinal: 1, preview_url: '/computer-preview/t/1.png', origin_or_window: 'example.com' });
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 3, status: 'stopped' });
  await nextTick();
  expect(store.rightPanelPicker).toBe(true);

  // 新 run 开始：再自动切入一次
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-2', event_seq: 1, status: 'starting' });
  await nextTick();
  expect(store.rightPanelPicker).toBe(false);
  expect(wrapper.find('[data-test="computer-pane"]').isVisible()).toBe(true);
  expect(store.computerLastAutoOpenedRunId).toBe('run-2');
  wrapper.unmount();
});

it('操控终态 60 秒且用户已返回选择页时自动收场', async () => {
  vi.useFakeTimers();
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel);

  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running' });
  await nextTick();
  // 用户 × 返回选择页（不终止 run），run 随后进入终态
  await wrapper.find('[data-test="pane-close"]').trigger('click');
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 2, status: 'completed' });
  await nextTick();
  expect(store.computerRun.run_id).toBe('run-1');

  vi.advanceTimersByTime(59999);
  await nextTick();
  expect(store.computerRun.run_id).toBe('run-1');

  vi.advanceTimersByTime(1);
  await nextTick();
  expect(store.computerRun.run_id).toBe('');
  // 收场后留在选择页
  expect(store.rightPanelPicker).toBe(true);

  wrapper.unmount();
  vi.useRealTimers();
});

it('用户停留在操控工作台时到点顺延，不被自动关闭', async () => {
  vi.useFakeTimers();
  const store = useAppStore();
  // 「在看就不关」要求面板处于展开态（默认收起时谈不上在看）
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
