import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import PluginSettingsPanel from './PluginSettingsPanel.vue';
import { useAppStore } from '../stores/appStore';

// 第六类 section：table（账号池）——渲染、缺列空串、单元格换行、右对齐、badge 上色、行内动作、空态
const TABLE_PAGE = {
  title: 'WorkBuddy',
  sections: [
    {
      type: 'table',
      title: '账号池',
      empty: '还没有账号',
      columns: [
        { key: 'account', label: '账号' },
        { key: 'credit', label: '积分', align: 'right' },
        { key: 'state', label: '状态', badge: true },
        { key: 'source', label: '来源' },
      ],
      rows: [
        {
          cells: { account: '木偶\nuid 0011', credit: '6692 / 8981', state: '使用中' },
          badges: { state: { tone: 'ok' } },
          actions: [
            { label: '签到', action: 'checkin', params: { uid: 'u1' } },
            { label: '移除', action: 'remove_account', params: { uid: 'u1' }, style: 'danger' },
          ],
        },
        {
          cells: { account: '小号', credit: '10 / 20', state: '已禁用' },
          badges: { state: { tone: 'err' } },
        },
      ],
    },
  ],
};

const EMPTY_PAGE = {
  title: 'WorkBuddy',
  sections: [{ type: 'table', title: '账号池', empty: '还没有账号', columns: [{ key: 'account', label: '账号' }], rows: [] }],
};

const NO_EMPTY_PAGE = {
  title: 'WorkBuddy',
  sections: [{ type: 'table', columns: [{ key: 'account', label: '账号' }] }],
};

async function mountPanel(page: unknown) {
  const store = useAppStore();
  vi.spyOn(store, 'loadPluginSettings').mockResolvedValue({ ok: true, page });
  const wrapper = mount(PluginSettingsPanel, { props: { name: 'workbuddy_connect' } });
  await flushPromises();
  return { wrapper, store };
}

describe('PluginSettingsPanel table 控件', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    delete (window as any).pywebview;
  });

  it('渲染两行数据、列头、标题，单元格 \n 拆成两行，缺列显示空串', async () => {
    const { wrapper } = await mountPanel(TABLE_PAGE);

    // section.title 渲染成 .field-label 小标题
    const label = wrapper.findAll('.field-label').find((el) => el.text() === '账号池');
    expect(label).toBeTruthy();
    expect(wrapper.find('[data-test="plugin-table"]').exists()).toBe(true);

    // 列头
    expect(wrapper.get('[data-test="col-account"]').text()).toBe('账号');
    expect(wrapper.get('[data-test="col-credit"]').text()).toBe('积分');
    expect(wrapper.get('[data-test="col-state"]').text()).toBe('状态');

    // 两行
    expect(wrapper.find('[data-test="table-row-0"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="table-row-1"]').exists()).toBe(true);
    expect(wrapper.findAll('[data-test^="table-row-"]').length).toBe(2);

    // 单元格换行渲染成两行
    const accountCell = wrapper.get('[data-test="table-row-0"] [data-test="cell-account"]');
    const lines = accountCell.findAll('.plugin-table-line');
    expect(lines.length).toBe(2);
    expect(lines[0].text()).toBe('木偶');
    expect(lines[1].text()).toBe('uid 0011');

    // 缺列（第二行没给 source）显示空串，不抛错
    const missing = wrapper.get('[data-test="table-row-1"] [data-test="cell-source"]');
    expect(missing.text()).toBe('');

    // 第二行没有 actions，行内动作列仍占位但不渲染按钮
    expect(wrapper.get('[data-test="table-row-1"]').findAll('button').length).toBe(0);
  });

  it('columns[].align = right 的列带右对齐类', async () => {
    const { wrapper } = await mountPanel(TABLE_PAGE);

    expect(wrapper.get('[data-test="col-credit"]').classes()).toContain('align-right');
    expect(wrapper.get('[data-test="table-row-0"] [data-test="cell-credit"]').classes()).toContain('align-right');
    // 未声明 align 的列按左对齐
    expect(wrapper.get('[data-test="col-account"]').classes()).toContain('align-left');
  });

  it('columns[].badge = true 的列按 rows[].badges[key].tone 上色', async () => {
    const { wrapper } = await mountPanel(TABLE_PAGE);

    const first = wrapper.get('[data-test="table-row-0"] [data-test="cell-state"] .plugin-badge');
    expect(first.text()).toBe('使用中');
    expect(first.classes()).toContain('tone-ok');

    const second = wrapper.get('[data-test="table-row-1"] [data-test="cell-state"] .plugin-badge');
    expect(second.text()).toBe('已禁用');
    expect(second.classes()).toContain('tone-err');

    // 非 badge 列不渲染徽标
    expect(wrapper.find('[data-test="cell-account"] .plugin-badge').exists()).toBe(false);
  });

  it('行内普通动作点击调用 runPluginSettingsAction，参数按 params 透传', async () => {
    const { wrapper, store } = await mountPanel(TABLE_PAGE);
    const runSpy = vi.spyOn(store, 'runPluginSettingsAction').mockResolvedValue({ ok: true, msg: '已签到' });

    await wrapper.get('[data-test="row-action-checkin"]').trigger('click');
    await flushPromises();

    expect(runSpy).toHaveBeenCalledWith('workbuddy_connect', 'checkin', { uid: 'u1' });
  });

  it('danger 动作先弹确认；取消则不执行，确认后才执行', async () => {
    const { wrapper, store } = await mountPanel(TABLE_PAGE);
    const runSpy = vi.spyOn(store, 'runPluginSettingsAction').mockResolvedValue({ ok: true });

    const confirmSpy = vi.spyOn(store, 'askConfirm').mockResolvedValue(false);
    await wrapper.get('[data-test="row-action-remove_account"]').trigger('click');
    await flushPromises();
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(runSpy).not.toHaveBeenCalled();

    confirmSpy.mockResolvedValue(true);
    await wrapper.get('[data-test="row-action-remove_account"]').trigger('click');
    await flushPromises();
    expect(confirmSpy).toHaveBeenCalledTimes(2);
    expect(runSpy).toHaveBeenCalledWith('workbuddy_connect', 'remove_account', { uid: 'u1' });
  });

  it('空 rows / rows 缺失时显示 section.empty，没有 empty 则显示「暂无数据」', async () => {
    const { wrapper } = await mountPanel(EMPTY_PAGE);
    expect(wrapper.get('[data-test="table-empty"]').text()).toBe('还没有账号');
    expect(wrapper.find('[data-test="plugin-table"]').exists()).toBe(false);

    const empty = await mountPanel(NO_EMPTY_PAGE);
    expect(empty.wrapper.get('[data-test="table-empty"]').text()).toBe('暂无数据');
  });
});
