import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createMemoryHistory, createRouter } from 'vue-router';
import ConvoListView from './ConvoListView.vue';
import { useAppStore } from '../stores/app';

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/convo', component: ConvoListView },
      { path: '/convo/:id', component: { template: '<div/>' }, props: true },
    ],
  });
}

const api = {
  switch_session: vi.fn(async (name: string) => ({
    ok: true,
    session_name: name,
    messages: [],
    history_has_more: false,
    history_oldest_id: null,
    workdir: 'D:/proj',
    session_model: {},
    active_sessions: [],
  })),
  get_sessions: vi.fn(async () => []),
};

beforeEach(() => {
  (window as any).pywebview = { api };
  api.switch_session.mockClear();
  api.get_sessions.mockClear();
});

describe('ConvoListView', () => {
  it('按分组渲染会话卡片，点击切换会话并跳转详情', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useAppStore();
    store.sessionGroups = [
      {
        folder_path: 'GLOBAL',
        folder_name: '对话',
        is_global: true,
        is_current: true,
        sessions: [{ name: 'g1', display_name: '自由对话', count: 3, workdir: 'GLOBAL', is_global: true, updated_at: '' }],
      },
      {
        folder_path: 'D:/proj',
        folder_name: 'proj',
        is_current: false,
        sessions: [{ name: 'p1', display_name: '项目会话', count: 9, workdir: 'D:/proj', is_global: false, updated_at: '2026-09-21T00:00:00' }],
      },
    ];
    store.currentSession = 'g1';

    const router = makeRouter();
    await router.push('/convo');
    await router.isReady();

    const wrapper = mount(ConvoListView, { global: { plugins: [pinia, router] } });
    const cards = wrapper.findAll('.conv-card');
    expect(cards).toHaveLength(2);
    expect(wrapper.text()).toContain('自由对话');
    expect(wrapper.text()).toContain('项目会话');

    await cards[1].trigger('click');
    await new Promise((r) => setTimeout(r, 0));
    expect(store.currentSession).toBe('p1');
    expect(router.currentRoute.value.path).toBe('/convo/p1');
  });

  it('搜索过滤会话', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useAppStore();
    store.sessionGroups = [
      {
        folder_path: 'GLOBAL',
        folder_name: '对话',
        is_global: true,
        is_current: false,
        sessions: [
          { name: 'a', display_name: '修复登录', count: 1, workdir: 'GLOBAL', is_global: true, updated_at: '' },
          { name: 'b', display_name: '重构接口', count: 1, workdir: 'GLOBAL', is_global: true, updated_at: '' },
        ],
      },
    ];

    const router = makeRouter();
    await router.push('/convo');
    await router.isReady();
    const wrapper = mount(ConvoListView, { global: { plugins: [pinia, router] } });
    expect(wrapper.findAll('.conv-card')).toHaveLength(2);

    (wrapper.vm as any).search = '登录';
    await new Promise((r) => setTimeout(r, 0));
    expect(wrapper.findAll('.conv-card')).toHaveLength(1);
  });
});
