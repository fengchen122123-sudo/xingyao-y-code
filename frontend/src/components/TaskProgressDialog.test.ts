import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

import TaskProgressDialog from './TaskProgressDialog.vue';
import { useAppStore } from '../stores/appStore';

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('渲染计划全景：标题、完成比、状态计数、完整文本与接管提示并存', () => {
  const store = useAppStore();
  const longText = '给内置文件工具加沙箱边界: Config 新增 sandboxDir, BuiltinTools 统一校验绝对路径与越界访问并补充单测覆盖';
  store.planData = {
    title: '重构登录模块',
    progress: 60,
    items: [
      { id: '1', text: longText, status: 'done' },
      { id: '2', text: '改核心代码', status: 'in_progress', handoff_note: '正在改 session.rs', execution_mode: 'main_takeover' },
      { id: '3', text: '回归验证', status: 'todo' },
    ],
  };
  const wrapper = mount(TaskProgressDialog);

  expect(wrapper.find('[data-test="task-dialog"]').exists()).toBe(true);
  const planHeader = wrapper.find('[data-test="task-dialog-plan"]').text();
  expect(planHeader).toContain('重构登录模块');
  expect(planHeader).toContain('1/3');
  expect(planHeader).toContain('60%');

  const statsText = wrapper.find('[data-test="task-dialog-stats"]').text();
  expect(statsText).toContain('全部3');
  expect(statsText).toContain('进行中1');
  expect(statsText).toContain('已完成1');
  expect(statsText).toContain('待执行1');

  // 长文本完整渲染，不被截断；接管提示单独渲染且不覆盖条目正文
  expect(wrapper.find('[data-test="plan-item-done"] .p-text').text()).toBe(longText);
  const inProgressCard = wrapper.find('[data-test="plan-item-in_progress"]');
  expect(inProgressCard.find('.p-text').text()).toBe('改核心代码');
  expect(inProgressCard.find('[data-test="plan-item-handoff"]').text()).toContain('正在改 session.rs');
  expect(inProgressCard.text()).toContain('主 Agent 接管');
  expect(wrapper.find('[data-test="plan-item-todo"]').text()).toContain('回归验证');
  wrapper.unmount();
});

it('无计划标题时进度行回退为“当前任务计划”并展示空态', () => {
  const store = useAppStore();
  store.planData = { title: '', progress: 0, items: [] };
  const wrapper = mount(TaskProgressDialog);
  expect(wrapper.find('[data-test="task-dialog-plan"]').text()).toContain('当前任务计划');
  expect(wrapper.find('[data-test="task-dialog-empty"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="task-dialog-stats"]').exists()).toBe(false);
  wrapper.unmount();
});

it('计划进度更新实时反映到面板（plan_updated 事件驱动，无需手动刷新）', async () => {
  const store = useAppStore();
  store.planData = { title: '计划', progress: 0, items: [{ id: '1', text: '第一步', status: 'in_progress' }] };
  const wrapper = mount(TaskProgressDialog);
  expect(wrapper.find('[data-test="task-dialog-plan"]').text()).toContain('0%');

  store.planData = { title: '计划', progress: 100, items: [{ id: '1', text: '第一步', status: 'done' }] };
  await wrapper.vm.$nextTick();
  expect(wrapper.find('[data-test="task-dialog-plan"]').text()).toContain('100%');
  expect(wrapper.find('[data-test="plan-item-done"]').exists()).toBe(true);
  wrapper.unmount();
});

it('点击关闭按钮或按 Esc 均可触发 close', async () => {
  const store = useAppStore();
  store.planData = { title: '计划', progress: 0, items: [] };
  const wrapper = mount(TaskProgressDialog);
  await wrapper.find('[data-test="task-pop-close"]').trigger('click');
  expect(wrapper.emitted('close')).toHaveLength(1);

  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  await wrapper.vm.$nextTick();
  expect(wrapper.emitted('close')).toHaveLength(2);
  wrapper.unmount();
});

it('支持按状态筛选条目，异常状态（failed/blocked/cancelled）单独统计与筛选', async () => {
  const store = useAppStore();
  store.planData = {
    title: '多状态计划',
    progress: 25,
    items: [
      { id: '1', text: '完成项', status: 'done' },
      { id: '2', text: '进行项', status: 'in_progress' },
      { id: '3', text: '待办项', status: 'todo' },
      { id: '4', text: '失败项', status: 'failed', error: '编译失败：缺少依赖' },
    ],
  };
  const wrapper = mount(TaskProgressDialog);

  const issueBtn = wrapper.find('[data-test="task-filter-issue"]');
  expect(issueBtn.exists()).toBe(true);
  expect(issueBtn.text()).toContain('异常1');
  // 异常原因在折叠态也直接可见
  expect(wrapper.find('[data-test="plan-item-failed"] [data-test="plan-item-error"]').text()).toContain('编译失败：缺少依赖');

  await issueBtn.trigger('click');
  expect(wrapper.findAll('li.plan-card')).toHaveLength(1);
  expect(wrapper.find('[data-test="plan-item-failed"]').exists()).toBe(true);

  // 再次点击同一筛选按钮复位为全部
  await issueBtn.trigger('click');
  expect(wrapper.findAll('li.plan-card')).toHaveLength(4);
  wrapper.unmount();
});

it('点击条目可展开/收起详情（依赖、文件、验收标准、结果、子代理跳转），并支持一键展开全部', async () => {
  const store = useAppStore();
  store.planData = {
    title: '复杂任务编排',
    progress: 50,
    items: [
      {
        id: 'P1',
        text: '盘点接口',
        status: 'done',
        stage: 'plan',
        files: ['src/api.ts'],
        result: '已确认 4 个接口签名',
      },
      {
        id: 'P2',
        text: '实现校验逻辑',
        status: 'in_progress',
        stage: 'build',
        depends_on: ['P1'],
        files: ['src/validator.ts', 'src/validator.test.ts'],
        verification: 'npm test -- validator',
        verification_type: 'command',
        owner: 'subagent-1',
        handoff_task_id: 'subagent-1',
      },
    ],
  };
  const openSubagentSpy = vi.spyOn(store, 'openSubagentDetail');
  const wrapper = mount(TaskProgressDialog);

  expect(wrapper.find('[data-test="plan-item-detail"]').exists()).toBe(false);

  await wrapper.find('[data-test="plan-item-in_progress"]').trigger('click');
  const detail = wrapper.find('[data-test="plan-item-detail"]');
  expect(detail.exists()).toBe(true);
  expect(detail.text()).toContain('P1');
  expect(detail.text()).toContain('盘点接口');
  expect(detail.text()).toContain('src/validator.ts');
  expect(detail.text()).toContain('npm test -- validator');
  expect(detail.text()).toContain('subagent-1');

  // 点击查看子代理直达右侧子代理面板，且不误触折叠卡片
  await wrapper.find('[data-test="plan-item-subagent"]').trigger('click');
  expect(openSubagentSpy).toHaveBeenCalledWith('subagent-1');
  expect(wrapper.findAll('[data-test="plan-item-detail"]')).toHaveLength(1);

  // 底部一键展开全部
  await wrapper.find('[data-test="task-pop-toggle-all"]').trigger('click');
  expect(wrapper.findAll('[data-test="plan-item-detail"]')).toHaveLength(2);
  expect(wrapper.find('[data-test="task-pop-toggle-all"]').text()).toContain('收起详情');

  await wrapper.find('[data-test="task-pop-toggle-all"]').trigger('click');
  expect(wrapper.findAll('[data-test="plan-item-detail"]')).toHaveLength(0);
  wrapper.unmount();
});

it('点击复制清单可将 Markdown 计划写入剪贴板并显示反馈', async () => {
  const writeText = vi.fn(async () => undefined);
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
  });

  const store = useAppStore();
  store.planData = {
    title: '发布前检查',
    progress: 50,
    items: [
      { id: '1', text: '跑通单测', status: 'done' },
      { id: '2', text: '构建产物', status: 'in_progress' },
    ],
  };
  const wrapper = mount(TaskProgressDialog);
  await wrapper.find('[data-test="task-pop-copy"]').trigger('click');
  await flushPromises();

  expect(writeText).toHaveBeenCalledTimes(1);
  const copied = (writeText.mock.calls as any[][])[0][0] as string;
  expect(copied).toContain('## 发布前检查（50%，1/2）');
  expect(copied).toContain('- [x] #1 跑通单测');
  expect(copied).toContain('- [-] #2 构建产物（进行中）');
  expect(wrapper.find('[data-test="task-pop-copy"]').text()).toContain('已复制');
  wrapper.unmount();
});

it('单行省略时悬停条目弹出完整内容气泡，移出后收起', async () => {
  vi.useFakeTimers();
  try {
    const store = useAppStore();
    const fullText = 'HttpShell 会话生命周期：空闲 30 分钟淘汰、上限 200 个、同 sessionId 请求串行锁保护与异常兜底';
    store.planData = {
      title: '单行省略测试',
      progress: 0,
      items: [{ id: '3', text: fullText, status: 'in_progress' }],
    };
    const wrapper = mount(TaskProgressDialog, { attachTo: document.body });
    const card = wrapper.find('[data-test="plan-item-in_progress"]');
    const textEl = card.find('.p-text').element as HTMLElement;

    // 模拟单行内容超出容器宽度被省略
    Object.defineProperty(textEl, 'clientWidth', { value: 220, configurable: true });
    Object.defineProperty(textEl, 'scrollWidth', { value: 520, configurable: true });

    await card.trigger('mouseenter');
    const tip = document.body.querySelector('[data-test="plan-item-tooltip"]');
    expect(tip).not.toBeNull();
    expect(tip?.textContent).toContain('#3');
    expect(tip?.textContent).toContain('进行中');
    expect(tip?.textContent).toContain(fullText);

    await card.trigger('mouseleave');
    vi.advanceTimersByTime(150);
    await wrapper.vm.$nextTick();
    expect(document.body.querySelector('[data-test="plan-item-tooltip"]')).toBeNull();
    wrapper.unmount();
  } finally {
    vi.useRealTimers();
  }
});
