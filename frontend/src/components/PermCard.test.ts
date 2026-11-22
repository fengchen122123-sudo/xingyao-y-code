import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import PermCard from './PermCard.vue';
import { useAppStore } from '../stores/appStore';


describe('PermCard', () => {
  beforeEach(() => setActivePinia(createPinia()));

  it('renders and submits backend keyed options', async () => {
    const store = useAppStore();
    store.permRequest = {
      show: true,
      sessionName: 's1',
      reqId: 'p1',
      summary: '写入 a.py',
      source: '主代理',
      functionName: 'write_file',
      options: [],
      permissionOptions: [
        { key: 'allow_once', label: '允许本次' },
        { key: 'deny', label: '拒绝' },
        { key: 'allow_task', label: '本任务同类操作放行' },
        { key: 'allow_project', label: '加入项目白名单' },
      ],
    } as never;
    const respond = vi
      .spyOn(store, 'respondPermissionOption')
      .mockResolvedValue(undefined);
    const wrapper = mount(PermCard, {
      global: { stubs: { AppIcon: true } },
    });

    const buttons = wrapper.findAll('[data-test="permission-option"]');

    expect(buttons).toHaveLength(4);
    expect(wrapper.get('.pc-head').text()).toContain('权限请求');
    expect(wrapper.get('.pc-src').text()).toContain('主代理');
    expect(buttons.map((b) => b.attributes('data-option-key'))).toEqual([
      'deny',
      'allow_task',
      'allow_project',
      'allow_once',
    ]);
    await wrapper.get('[data-option-key="allow_task"]').trigger('click');
    expect(respond).toHaveBeenCalledWith('allow_task');
  });

  it('renders ask_user as a confirmation question without the waiting prefix and submits choice or custom text', async () => {
    const store = useAppStore();
    store.permRequest = {
      show: true,
      sessionName: 's1',
      reqId: 'q1',
      summary: '等待用户回答：要如何处理未跟踪的 app/ 目录？',
      source: '主代理',
      allowAlways: false,
      functionName: 'ask_user',
      options: ['入库并补充 .gitignore', '仅提交 frontend'],
      permissionOptions: [],
    } as never;
    const respondChoice = vi
      .spyOn(store, 'respondPermissionChoice')
      .mockResolvedValue(undefined);
    const wrapper = mount(PermCard, {
      global: { stubs: { AppIcon: true } },
    });

    expect(wrapper.get('.pc-head').text()).toContain('需要你确认');
    expect(wrapper.get('[data-test="perm-question-body"]').text()).toBe('要如何处理未跟踪的 app/ 目录？');

    const choices = wrapper.findAll('[data-test="perm-question-choice"]');
    expect(choices).toHaveLength(2);
    await choices[1].trigger('click');
    expect(respondChoice).toHaveBeenCalledWith(1);

    const input = wrapper.get('[data-test="perm-custom-input"]');
    await input.setValue('  先保留现状  ');
    await wrapper.get('[data-test="perm-custom-submit"]').trigger('click');
    expect(respondChoice).toHaveBeenCalledWith(-1, '先保留现状');
  });

  it('clears unsent ask_user custom draft when reqId changes', async () => {
    const store = useAppStore();
    store.permRequest = {
      show: true,
      sessionName: 's1',
      reqId: 'q1',
      summary: '等待用户回答：问题一',
      source: '主代理',
      allowAlways: false,
      functionName: 'ask_user',
      options: ['选项一'],
      permissionOptions: [],
    } as never;
    const wrapper = mount(PermCard, {
      global: { stubs: { AppIcon: true } },
    });

    const input = wrapper.get('[data-test="perm-custom-input"]');
    await input.setValue('草稿内容');
    expect((input.element as HTMLInputElement).value).toBe('草稿内容');

    store.permRequest = {
      ...store.permRequest,
      reqId: 'q2',
      summary: '等待用户回答：问题二',
    };
    await wrapper.vm.$nextTick();
    expect((input.element as HTMLInputElement).value).toBe('');
  });

  it('splits execute_command label and command code cleanly and handles undo_turn impact', () => {
    const store = useAppStore();
    store.permRequest = {
      show: true,
      sessionName: 's1',
      reqId: 'p2',
      summary: '执行命令：\nnpm run build',
      source: '主代理',
      allowAlways: true,
      functionName: 'execute_command',
      options: [],
      permissionOptions: [
        { key: 'allow_once', label: '允许本次' },
        { key: 'deny', label: '拒绝' },
      ],
    } as never;
    const wrapper = mount(PermCard, {
      global: { stubs: { AppIcon: true } },
    });

    expect(wrapper.get('.pc-action-tag').text()).toBe('执行命令');
    expect(wrapper.get('.pc-cmd-code').text()).toBe('npm run build');

    store.permRequest = {
      show: true,
      sessionName: 's1',
      reqId: 'p3',
      summary: '使用工具 undo_turn',
      source: '主代理',
      allowAlways: false,
      functionName: 'undo_turn',
      options: [],
      permissionOptions: [
        { key: 'allow_once', label: '允许本次' },
        { key: 'deny', label: '拒绝' },
      ],
      undoImpact: {
        available: true,
        short_id: 't1',
        file_count: 2,
        restore_content_count: 2,
        delete_created_count: 0,
        restore_deleted_count: 0,
      },
    } as never;

    return wrapper.vm.$nextTick().then(() => {
      expect(wrapper.get('.pc-head').text()).toContain('撤销确认');
      expect(wrapper.find('.pc-body').exists()).toBe(false);
      expect(wrapper.get('.pc-undo-impact').text()).toContain('回合 t1：共 2 个文件');
    });
  });
});
