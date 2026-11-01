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
  // 滑入/滑出动画的位移量由这个变量提供（Transition 的 enter-from 用 calc 取用）
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

// ── 多页签容器：页签条 + 单独关闭 + 选择页追加 ──

it('中性入口展开面板：无页签先显示选择页六卡，点卡片开出对应页签', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel);
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(false);

  store.toggleRightPanel();
  await nextTick();
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(true);
  expect(wrapper.findAll('.tp-card').map(c => c.text().trim()))
    .toEqual(['文件', 'Git', '星眼 · 浏览器', '星港 · 后台任务', '星舰 · 子代理', '小说', '画布']);

  await wrapper.find('[data-test="picker-card-files"]').trigger('click');
  await nextTick();
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(false);
  // 页签条出现 files 页签且处于激活态
  const tab = wrapper.find('[data-test="tab-files"]');
  expect(tab.exists()).toBe(true);
  expect(tab.classes()).toContain('active');
  expect(wrapper.find('[data-test="tabbar-add"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="tabbar-collapse"]').exists()).toBe(true);
  wrapper.unmount();
});

it('浏览器卡片开出浏览器页签并挂载 BrowserPane', async () => {
  const store = useAppStore();
  // 浏览器工作台依赖宿主 WebView2 桥，测试中以 stub 替换其内部实现
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

it('小说卡片开出小说页签并挂载 NovelPane', async () => {
  const store = useAppStore();
  // NovelPane 激活时会向宿主桥拉取项目状态，测试中以 stub 替换（只验证入口链路）
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

  // Git 定向入口：追加页签，不覆盖浏览器页签
  store.openRightPanelTab('git');
  await nextTick();
  expect(store.rightPanelTabs).toEqual(['browser', 'git']);
  expect(store.rightPanelActiveTab).toBe('git');
  expect(wrapper.find('[data-test="git-pane"]').isVisible()).toBe(true);
  expect(wrapper.find('[data-test="browser-pane"]').exists()).toBe(true);
  // jsdom 的 getComputedStyle 不读内联 style，v-show 隐藏只能查内联样式断言
  expect(wrapper.find('[data-test="browser-pane"]').attributes('style')).toContain('display: none');

  // 点浏览器页签切回，Git 页签保留
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

  // 关闭激活页签 git → 切到左邻 browser
  await wrapper.find('[data-test="tab-close-git"]').trigger('click');
  await nextTick();
  expect(store.rightPanelTabs).toEqual(['browser']);
  expect(store.rightPanelActiveTab).toBe('browser');
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(false);

  // 关闭最后一个页签 → 回选择页（面板保持展开）
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
  // jsdom 里脱离文档树的元素 getComputedStyle 拿不到 display，isVisible() 恒为 true，
  // 所以这里直接看 v-show 写进 style 的 display
  const paneStyle = () => wrapper.find('[data-test="git-pane"]').attributes('style') ?? '';

  store.openRightPanelTab('git');
  await nextTick();
  expect(paneStyle()).not.toContain('display: none');

  // 收起瞬间 activeTab 已转空（原生表面 hide、轮询停止都靠它），但渲染用的
  // visualTab 还要留一个滑出时长，滑走的才是刚看到的那一屏
  store.toggleRightPanel();
  await nextTick();
  expect(store.rightPanelCollapsed).toBe(true);
  expect(paneStyle()).not.toContain('display: none');

  // 滑出动画走完再清空
  await new Promise(resolve => setTimeout(resolve, 300));
  await nextTick();
  expect(paneStyle()).toContain('display: none');

  wrapper.unmount();
});

// ── 后台任务执行过程页签 ──

it('执行条详情打开后台任务页签并显示命令', async () => {
  const store = useAppStore();
  const wrapper = mount(RightPanel);

  // 后台任务带会话归属：详情头部与列表统一走 currentSessionBackgroundTasks，
  // 无归属的旧记录不显示（本用例给任务一个与当前会话一致的 session_id）
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

// ── 子代理执行过程页签 ──

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
  // 方案 B：工具开始/结束配对成一行（开始时刻锚定），模板行（调用模型）弱化保留
  const rows = wrapper.findAll('[data-test="subagent-flow"] .suba-row');
  expect(rows).toHaveLength(2);
  expect(rows[1].text()).toContain('read_file');
  expect(rows[1].text()).toContain('✓ 1.2s');
  // 头部统计：轮次与工具成败计数
  expect(wrapper.text()).toContain('轮次 1');
  expect(wrapper.text()).toContain('工具 1 成功');
  // 任务切换条：会话内两个子代理 → 两个 chip，点击第二个切换查看对象
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

// ── 操控页签（设计 16.2：run 出现自动切入，不进选择页） ──

it('run 出现后自动切入操控页签并校准快照，关闭页签后事件不抢回，新 run 再开一次', async () => {
  const store = useAppStore();
  // 快照回当前 run（真实桥行为）：校准按 store 里最新的 run_id 返回
  const getStatus = vi.fn(async () => ({
    run_id: store.computerRun.run_id,
    status: 'paused',
    pause_reason: 'user_pause',
  }));
  (window as any).pywebview = { api: { get_computer_control_status: getStatus } };
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel);

  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'starting' });
  // 事件不带归属（旧后端）：开页签前先做一次快照校准，等页签真正落地再断言
  await vi.waitFor(() => expect(store.computerLastAutoOpenedRunId).toBe('run-1'));
  await nextTick();

  expect(wrapper.find('[data-test="computer-pane"]').isVisible()).toBe(true);
  expect(store.rightPanelPicker).toBe(false);
  expect(store.rightPanelActiveTab).toBe('computer');
  expect(store.computerLastAutoOpenedRunId).toBe('run-1');
  // 进入操控页签触发快照校准
  await vi.waitFor(() => expect(store.computerRun.status).toBe('paused'));
  expect(store.computerRun.pause_reason).toBe('user_pause');

  // 用户关掉操控页签：run 不终止，同一 run 后续事件不把人拽回去
  await wrapper.find('[data-test="tab-close-computer"]').trigger('click');
  await nextTick();
  expect(store.rightPanelPicker).toBe(true);
  expect(store.computerRun.run_id).toBe('run-1');

  store.reduceComputerEvent({ type: 'computer_observation', run_id: 'run-1', event_seq: 2, action_ordinal: 1, preview_url: '/computer-preview/t/1.png', origin_or_window: 'example.com' });
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 3, status: 'stopped' });
  await nextTick();
  expect(store.rightPanelPicker).toBe(true);

  // 新 run 开始：再自动开一次（同样先校准归属再落地页签）
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
  // 用户关掉操控页签（不终止 run），run 随后进入终态
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
  // 收场后无操控页签，停在选择页
  expect(store.rightPanelPicker).toBe(true);

  wrapper.unmount();
  vi.useRealTimers();
});

it('用户停留在操控页签时到点顺延，不被自动关闭', async () => {
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

// ── Git 定向入口（状态栏分支项 / 输入框 chip） ──

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

// ── 画笔截图入口（gui_snip_mixin.SnipBridgeMixin）──

it('画笔截图：无桥时不进入截图模式并提示；桥正常时先捕获再挂覆盖层', async () => {
  const store = useAppStore();
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel, { global: { stubs: { BrowserPane: true } } });

  expect(wrapper.find('[data-test="tabbar-snip"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="snip-overlay"]').exists()).toBe(false);

  // 无桥（jsdom 调试环境）：不置位、不挂覆盖层
  await wrapper.find('[data-test="tabbar-snip"]').trigger('click');
  await nextTick();
  expect(store.rightPanelSnipActive).toBe(false);
  expect(wrapper.find('[data-test="snip-overlay"]').exists()).toBe(false);

  // 桥就绪：捕获成功（含 data_url）→ 置位并挂覆盖层；快照经 prop 传入冻结帧
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

  // 覆盖层 Esc → 退出截图模式（原生表面由既有状态机恢复，无需桥调用；
  // 工具条仅在圈出选区后渲染，jsdom 无笔画，走键盘退出路径）
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

// ── 后台任务详情口径一致性 ──

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

  // 列表源是 currentSessionBackgroundTasks（本会话无任务 → 不渲染多任务列表）
  expect(wrapper.find('[data-test="bg-task-list"]').exists()).toBe(false);
  // 头部详情同样只认当前会话：查不到则回落到任务 id，不显示他会话命令
  expect(wrapper.find('.bgt-cmd').text()).toBe('t1');
  wrapper.unmount();
});

// ── 操控页签的会话归属 ──

it('run 首个事件带归属时操控页签直接写进归属会话，不碰当前查看的会话', async () => {
  const store = useAppStore();
  store.currentSessionName = 'B';
  store.rightPanelCollapsed = false;
  const wrapper = mount(RightPanel, { global: { stubs: { BrowserPane: true } } });

  // 事件信封自带 owner_session_id（后端每个电脑操控事件都下发）：首帧即可定归属
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
  // 旧后端事件不带归属：快照校准是唯一真值源
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

  // 2026-10-30 反馈：此前归属未知直接回落 currentSessionName，后台会话发起的
  // 操控把页签开进用户正看着的会话。校准后应落进归属会话 A
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

  // run 由 A 会话启动（owner_session_id=A），用户此刻正看 B 会话
  store._applyComputerSnapshot({ run_id: 'run-1', status: 'running', owner_session_id: 'A' });
  await nextTick();

  // B 的页签集不被写脏
  expect(store.rightPanelTabs).not.toContain('computer');
  // 归属会话 A 的 runtime 已登记操控页签（切回 A 时看到）
  const runtimeA = (store as any).sessionRuntimeByName['A'];
  expect(runtimeA.rightPanelTabs).toContain('computer');
  expect(runtimeA.rightPanelActiveTab).toBe('computer');
  wrapper.unmount();
});

// ── QQ2009 皮肤专属：动态 QQ秀（非主流动图） ──

it('QQ秀：默认主题（guiTheme 为 null）停在选择页时不渲染 QQ秀块', async () => {
  const store = useAppStore();
  // 展开面板且无页签 → 停在选择页（showPicker）
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
  // 主题构造器只用到 id 字段：测试用最小对象（GuiTheme 全量字段由主题仓库负责）
  store.guiTheme = { id: 'builtin.qq2009' } as any;
  const wrapper = mount(RightPanel);

  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(true);
  const show = wrapper.find('[data-test="qq09-show"]');
  expect(show.exists()).toBe(true);
  // 舞台双图（背景垫图 + 动图）同源引用 public 资源
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

  // 打开 files 页签：picker 让位，QQ秀随选择页一并隐藏
  store.openRightPanelTab('files');
  await nextTick();
  expect(store.rightPanelPicker).toBe(false);
  expect(wrapper.find('[data-test="tab-picker"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="qq09-show"]').exists()).toBe(false);
  wrapper.unmount();
});
