import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { nextTick } from 'vue';

import SettingsPage from './SettingsPage.vue';
import { useAppStore } from '../stores/appStore';

// 「会话归档」从侧栏底部搬到设置页：数据源仍是 store.archivedSessions（后端 get_archived_sessions），
// 故沿用 Sidebar.archive.test.ts 的写法——不 mock store 数据，只 spy 会走桥接的 action。
function ts(minutesAgo: number) {
  const d = new Date(Date.now() - minutesAgo * 60000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 归档列表故意按「旧的在前」给，用于验证前端自行按 archived_at 倒序渲染 */
function archivedList() {
  return [
    {
      name: 'session-old-1',
      display_name: '旧会话一',
      count: 3,
      workdir: 'GLOBAL',
      is_global: true,
      updated_at: ts(300),
      archived_at: ts(120),
    },
    {
      name: 'session-old-2',
      display_name: '旧会话二',
      count: 1,
      workdir: 'D:/proj/a',
      is_global: false,
      updated_at: ts(60),
      archived_at: ts(30),
    },
  ];
}

function setup(archived: any[] = archivedList()) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.archivedSessions = archived as any;
  const wrapper = mount(SettingsPage, { global: { plugins: [pinia] } });
  return { wrapper, store };
}

async function openArchiveSection(wrapper: VueWrapper) {
  await wrapper.get('[data-test="session-archive-nav"]').trigger('click');
  await flushPromises();
}

function archivedNames(wrapper: VueWrapper) {
  return wrapper.findAll('[data-test="session-archive-item"]').map((n) => n.attributes('data-archived-name'));
}

describe('SettingsPage 会话归档分区', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    delete (window as any).pywebview;
  });

  it('左侧导航「基础设置」分组内存在「会话归档」入口，点击后切到该分区', async () => {
    const { wrapper } = setup();

    const nav = wrapper.get('[data-test="session-archive-nav"]');
    expect(nav.text()).toContain('会话归档');
    // 切走前不渲染归档页
    expect(wrapper.find('[data-test="session-archive-page"]').exists()).toBe(false);

    await openArchiveSection(wrapper);

    expect(wrapper.get('[data-test="session-archive-page"]').isVisible()).toBe(true);
    expect(nav.classes()).toContain('active');
    expect(wrapper.find('.model-settings-card').exists()).toBe(false);
  });

  it('进入分区时拉取归档列表', async () => {
    const { wrapper, store } = setup([]);
    const loadSpy = vi.spyOn(store, 'loadArchivedSessions').mockResolvedValue(undefined);

    await openArchiveSection(wrapper);

    expect(loadSpy).toHaveBeenCalledTimes(1);
  });

  it('列表按 archived_at 倒序渲染展示名、归档时间、消息数与归属目录', async () => {
    const { wrapper } = setup();

    await openArchiveSection(wrapper);

    expect(archivedNames(wrapper)).toEqual(['session-old-2', 'session-old-1']);

    const first = wrapper.findAll('[data-test="session-archive-item"]')[0];
    expect(first.get('.archive-title').text()).toBe('旧会话二');
    expect(first.get('[data-test="session-archive-time"]').text()).toBe('30分钟');
    expect(first.get('[data-test="session-archive-count"]').text()).toContain('1');
    expect(first.get('[data-test="session-archive-workdir"]').text()).toBe('D:/proj/a');

    const second = wrapper.findAll('[data-test="session-archive-item"]')[1];
    expect(second.get('[data-test="session-archive-time"]').text()).toBe('2小时');
    expect(second.get('[data-test="session-archive-workdir"]').text()).toBe('GLOBAL');
  });

  it('归档为空时只显示空态文案，不渲染任何归档行', async () => {
    const { wrapper } = setup([]);

    await openArchiveSection(wrapper);

    expect(wrapper.get('[data-test="session-archive-empty"]').text()).toContain('暂无归档会话');
    expect(wrapper.findAll('[data-test="session-archive-item"]').length).toBe(0);
  });

  it('点「恢复」调用 store.unarchiveSession（走桥接取消归档）', async () => {
    const { wrapper, store } = setup();
    const unarchiveSpy = vi.spyOn(store, 'unarchiveSession').mockResolvedValue(true);

    await openArchiveSection(wrapper);
    await wrapper.findAll('[data-test="session-archive-item"]')[0]
      .get('[data-test="session-archive-restore-btn"]').trigger('click');
    await flushPromises();

    expect(unarchiveSpy).toHaveBeenCalledTimes(1);
    expect(unarchiveSpy).toHaveBeenCalledWith('session-old-2');
  });

  it('删除沿用两段确认：先点 ✕ 只进确认态，点「确认」才真正删除', async () => {
    const { wrapper, store } = setup();
    const deleteSpy = vi.spyOn(store, 'deleteSingleSession').mockResolvedValue(undefined);

    await openArchiveSection(wrapper);
    const firstItem = () => wrapper.findAll('[data-test="session-archive-item"]')[0];

    // 第一段：仅进入确认态，不落盘
    await firstItem().get('[data-test="session-archive-delete-btn"]').trigger('click');
    await nextTick();

    expect(deleteSpy).not.toHaveBeenCalled();
    expect(firstItem().find('[data-test="session-archive-delete-confirm"]').exists()).toBe(true);

    // 第二段：确认才删除，用的是该归档会话的名字
    await firstItem().get('[data-test="session-archive-delete-confirm"]').trigger('click');
    await flushPromises();

    expect(deleteSpy).toHaveBeenCalledTimes(1);
    expect(deleteSpy).toHaveBeenCalledWith('session-old-2');
  });
});

// 批量恢复 / 批量删除：归档区多选模式（桌面与手机端共用同一套交互）
describe('SettingsPage 会话归档·批量管理', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    delete (window as any).pywebview;
  });

  /** 进入归档分区并打开批量模式 */
  async function enterBatchMode(wrapper: VueWrapper) {
    await openArchiveSection(wrapper);
    await wrapper.get('[data-test="session-archive-batch-toggle"]').trigger('click');
    await nextTick();
  }

  /** 第 i 行的勾选框（列表按 archived_at 倒序，索引 0 是最新归档的会话） */
  function selectBox(wrapper: VueWrapper, index: number) {
    return wrapper.findAll('[data-test="session-archive-item"]')[index]
      .get('[data-test="session-archive-select"]');
  }

  function selectedCount(wrapper: VueWrapper) {
    return wrapper.get('[data-test="session-archive-selected-count"]').text();
  }

  it('「批量管理」进入多选模式：出现操作栏与勾选框，隐藏行内单项操作', async () => {
    const { wrapper } = setup();

    await openArchiveSection(wrapper);
    expect(wrapper.find('[data-test="session-archive-batch-bar"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="session-archive-select"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="session-archive-restore-btn"]').exists()).toBe(true);

    await enterBatchMode(wrapper);

    expect(wrapper.get('[data-test="session-archive-batch-bar"]').isVisible()).toBe(true);
    expect(wrapper.findAll('[data-test="session-archive-select"]').length).toBe(2);
    // 批量模式下隐藏行内单项恢复/删除，避免与批量语义冲突
    expect(wrapper.find('[data-test="session-archive-restore-btn"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="session-archive-delete-btn"]').exists()).toBe(false);
  });

  it('勾选与全选联动被选数量，退出批量模式后清空勾选', async () => {
    const { wrapper } = setup();
    await enterBatchMode(wrapper);

    expect(selectedCount(wrapper)).toContain('0');

    await selectBox(wrapper, 0).setValue(true);
    await nextTick();
    expect(selectedCount(wrapper)).toContain('1');

    await wrapper.get('[data-test="session-archive-select-all"]').setValue(true);
    await nextTick();
    expect(selectedCount(wrapper)).toContain('2');
    expect(wrapper.findAll('[data-test="session-archive-select"]')
      .every(n => (n.element as HTMLInputElement).checked)).toBe(true);

    // 全选状态下再点一次 = 取消全选
    await wrapper.get('[data-test="session-archive-select-all"]').setValue(false);
    await nextTick();
    expect(selectedCount(wrapper)).toContain('0');

    // 勾一项后退出批量模式：操作栏收起，再次进入时勾选已清空
    await selectBox(wrapper, 0).setValue(true);
    await nextTick();
    await wrapper.get('[data-test="session-archive-batch-toggle"]').trigger('click');
    await nextTick();
    expect(wrapper.find('[data-test="session-archive-batch-bar"]').exists()).toBe(false);

    await wrapper.get('[data-test="session-archive-batch-toggle"]').trigger('click');
    await nextTick();
    expect(selectedCount(wrapper)).toContain('0');
  });

  it('未勾选任何项时两个批量按钮都禁用', async () => {
    const { wrapper } = setup();
    await enterBatchMode(wrapper);

    expect(wrapper.get('[data-test="session-archive-batch-restore"]').attributes('disabled')).toBeDefined();
    expect(wrapper.get('[data-test="session-archive-batch-delete"]').attributes('disabled')).toBeDefined();
  });

  it('「恢复选中」调 store.batchUnarchiveSessions，可逆操作不弹二次确认', async () => {
    const { wrapper, store } = setup();
    const batchSpy = vi.spyOn(store, 'batchUnarchiveSessions').mockResolvedValue(['session-old-2']);
    const confirmSpy = vi.spyOn(store, 'askConfirm').mockResolvedValue(false);

    await enterBatchMode(wrapper);
    await selectBox(wrapper, 0).setValue(true);
    await nextTick();
    await wrapper.get('[data-test="session-archive-batch-restore"]').trigger('click');
    await flushPromises();

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(batchSpy).toHaveBeenCalledTimes(1);
    expect(batchSpy).toHaveBeenCalledWith(['session-old-2']);
  });

  it('「删除选中」先二次确认：取消不发请求，确认才调 batchDeleteArchivedSessions', async () => {
    const { wrapper, store } = setup();
    const batchSpy = vi.spyOn(store, 'batchDeleteArchivedSessions').mockResolvedValue(['session-old-2']);
    const confirmSpy = vi.spyOn(store, 'askConfirm').mockResolvedValue(false);

    await enterBatchMode(wrapper);
    await selectBox(wrapper, 0).setValue(true);
    await nextTick();

    await wrapper.get('[data-test="session-archive-batch-delete"]').trigger('click');
    await flushPromises();

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    // 确认文案里必须带待删数量，避免误删
    expect(String(confirmSpy.mock.calls[0][2])).toContain('1');
    expect(batchSpy).not.toHaveBeenCalled();

    confirmSpy.mockResolvedValue(true);
    await wrapper.get('[data-test="session-archive-batch-delete"]').trigger('click');
    await flushPromises();

    expect(batchSpy).toHaveBeenCalledTimes(1);
    expect(batchSpy).toHaveBeenCalledWith(['session-old-2']);
  });

  it('恢复后只清掉成功项的勾选，失败项保留以便重试', async () => {
    const { wrapper, store } = setup();
    // 列表倒序：索引 0 = session-old-2，索引 1 = session-old-1；只有后者恢复成功
    vi.spyOn(store, 'batchUnarchiveSessions').mockResolvedValue(['session-old-1']);

    await enterBatchMode(wrapper);
    await selectBox(wrapper, 0).setValue(true);
    await selectBox(wrapper, 1).setValue(true);
    await nextTick();
    await wrapper.get('[data-test="session-archive-batch-restore"]').trigger('click');
    await flushPromises();

    const checked = wrapper.findAll('[data-test="session-archive-select"]')
      .map(n => (n.element as HTMLInputElement).checked);
    expect(checked).toEqual([true, false]);
  });

  it('归档列表清空时自动退出批量模式并隐藏入口', async () => {
    const { wrapper, store } = setup();
    await enterBatchMode(wrapper);
    expect(wrapper.find('[data-test="session-archive-batch-toggle"]').exists()).toBe(true);

    store.archivedSessions = [];
    await nextTick();

    expect(wrapper.find('[data-test="session-archive-batch-bar"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="session-archive-batch-toggle"]').exists()).toBe(false);
    expect(wrapper.get('[data-test="session-archive-empty"]').text()).toContain('暂无归档会话');
  });
});
