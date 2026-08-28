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
    await buttons[2].trigger('click');
    expect(respond).toHaveBeenCalledWith('allow_task');
  });
});
