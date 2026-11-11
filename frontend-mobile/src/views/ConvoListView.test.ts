import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { KeepAlive, defineComponent, h, nextTick, ref } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';
import ConvoListView from './ConvoListView.vue';
import { useAppStore, type SessionGroup } from '../stores/app';

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/convo', component: ConvoListView },
      { path: '/convo/:id', component: { template: '<div/>' }, props: true },
    ],
  });
}

/** 两个会话的「自由对话」分组，供搜索/滚动用例复用 */
function twoSessions(): SessionGroup[] {
  return [
    {
      folder_path: 'GLOBAL',
      folder_name: '对话',
      is_global: true,
      is_current: true,
      sessions: [
        { name: 'a', display_name: '修复登录', count: 1, workdir: 'GLOBAL', is_global: true, updated_at: '' },
        { name: 'b', display_name: '重构接口', count: 1, workdir: 'GLOBAL', is_global: true, updated_at: '' },
      ],
    },
  ];
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
  get_sessions: vi.fn(async (): Promise<SessionGroup[]> => []),
};

beforeEach(() => {
  (window as any).pywebview = { api };
  api.switch_session.mockClear();
  // onActivated 会重拉会话列表，默认返回空，需要列表数据的用例自行 mockImplementation
  api.get_sessions.mockReset();
  api.get_sessions.mockImplementation(async () => []);
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
    store.sessionGroups = twoSessions();

    const router = makeRouter();
    await router.push('/convo');
    await router.isReady();
    const wrapper = mount(ConvoListView, { global: { plugins: [pinia, router] } });
    expect(wrapper.findAll('.conv-card')).toHaveLength(2);

    (wrapper.vm as any).search = '登录';
    await new Promise((r) => setTimeout(r, 0));
    expect(wrapper.findAll('.conv-card')).toHaveLength(1);
  });

  it('搜索无结果时给出「清除搜索」入口，点击后恢复全量列表', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useAppStore();
    store.sessionGroups = twoSessions();

    const router = makeRouter();
    await router.push('/convo');
    await router.isReady();
    const wrapper = mount(ConvoListView, { global: { plugins: [pinia, router] } });

    (wrapper.vm as any).search = '不存在的会话';
    await nextTick();
    expect(wrapper.findAll('.conv-card')).toHaveLength(0);

    const clear = wrapper.find('.empty-clear');
    expect(clear.exists()).toBe(true);
    await clear.trigger('click');
    await nextTick();
    expect((wrapper.vm as any).search).toBe('');
    expect(wrapper.findAll('.conv-card')).toHaveLength(2);
  });

  it('离开列表页清空搜索词：从会话返回后不再停在过滤结果上', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useAppStore();
    const groups = twoSessions();
    store.sessionGroups = groups;
    // 返回列表会触发 onActivated → refreshSessions，这里让后端返回同一份列表
    api.get_sessions.mockImplementation(async () => groups);

    const router = makeRouter();
    await router.push('/convo');
    await router.isReady();

    // onActivated/onDeactivated 只在被 keep-alive 缓存时触发，故用宿主组件包一层
    const show = ref(true);
    const Host = defineComponent({
      setup: () => () => h(KeepAlive, null, { default: () => (show.value ? h(ConvoListView) : null) }),
    });
    const wrapper = mount(Host, { global: { plugins: [pinia, router] } });
    const list = () => wrapper.findComponent(ConvoListView);
    const search = () => (list().vm as any).search;
    const vmBefore = list().vm as any;

    (list().vm as any).search = '登录';
    await nextTick();
    expect(wrapper.findAll('.conv-card')).toHaveLength(1);

    show.value = false; // 去详情页（列表被缓存，触发 onDeactivated）
    await nextTick();
    show.value = true; // 返回列表
    await nextTick();
    await nextTick();

    // 必须仍是同一实例：否则组件重建也会得到空 search，本用例就假通过了
    expect((list().vm as any).$.uid).toBe(vmBefore.$.uid);
    expect(search()).toBe('');
    expect(wrapper.findAll('.conv-card')).toHaveLength(2);
  });
});
