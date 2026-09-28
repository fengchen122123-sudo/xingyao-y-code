import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createMemoryHistory, createRouter } from 'vue-router';
import ConvoDetailView from './ConvoDetailView.vue';
import { useAppStore } from '../stores/app';

// 确认框是「输入框已有草稿」分支的判定点，必须可控：其余 vant 导出保持真实实现
const { confirmMock } = vi.hoisted(() => ({ confirmMock: vi.fn() }));
vi.mock('vant', async (importOriginal) => {
  const actual = await importOriginal<typeof import('vant')>();
  return { ...actual, showConfirmDialog: confirmMock };
});

const api = {
  cancel_queued_message: vi.fn(async () => ({ ok: true, removed: 1 })),
  get_files_info: vi.fn(async (paths: string[]) => paths.map((p) => ({ path: p, name: p.split('/').pop(), ok: true }))),
};

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/convo', component: { template: '<div/>' } },
      { path: '/convo/:id', component: ConvoDetailView, props: true },
    ],
  });
}

/** 等异步 action 链（RPC → 回填 → nextTick）走完 */
function flush() {
  return new Promise((r) => setTimeout(r, 0));
}

beforeEach(() => {
  (window as any).pywebview = { api };
  api.cancel_queued_message.mockClear();
  api.get_files_info.mockClear();
  confirmMock.mockReset();
});

describe('ConvoDetailView 排队条', () => {
  async function mountView() {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useAppStore();
    store.currentSession = 's1';
    store.queues = {
      s1: [
        { id: 'q1', text: '第一条排队', attachments: [] },
        { id: 'q2', text: '第二条排队', attachments: ['/tmp/a.png'] },
      ],
    };
    const router = makeRouter();
    await router.push('/convo/s1');
    await router.isReady();
    const wrapper = mount(ConvoDetailView, { props: { id: 's1' }, global: { plugins: [pinia, router] } });
    return { wrapper, store };
  }

  const editBtn = (wrapper: ReturnType<typeof mount>) => wrapper.findAll('.q-row')[1].findAll('.q-act')[0];

  it('逐条渲染排队消息，每条都有编辑/插队/取消入口', async () => {
    const { wrapper } = await mountView();

    const rows = wrapper.findAll('.q-row');
    expect(rows).toHaveLength(2);
    expect(rows[0].text()).toContain('第一条排队');
    expect(rows[1].text()).toContain('第二条排队');
    // 旧实现只渲染队首一条，第 2 条没有任何操作入口
    for (const row of rows) {
      expect(row.findAll('.q-act').map((a) => a.text())).toEqual(['编辑', '插队', '取消']);
    }
  });

  it('点第 2 条的「编辑」：撤出队列并把文本与附件回填输入框', async () => {
    const { wrapper, store } = await mountView();

    await editBtn(wrapper).trigger('click');
    await flush();

    expect(api.cancel_queued_message).toHaveBeenCalledWith('s1', 'q2');
    expect(store.currentQueue.map((m) => m.id)).toEqual(['q1']);
    expect((wrapper.vm as any).draft).toBe('第二条排队');
    expect(wrapper.find('.chip').text()).toContain('a.png');
  });

  it('输入框已有草稿：用户确认后覆盖草稿并撤回', async () => {
    const { wrapper, store } = await mountView();
    (wrapper.vm as any).draft = '我正在输入的新内容';
    confirmMock.mockResolvedValue(undefined);

    await editBtn(wrapper).trigger('click');
    await flush();

    expect(confirmMock).toHaveBeenCalled();
    expect((wrapper.vm as any).draft).toBe('第二条排队');
    expect(store.currentQueue.map((m) => m.id)).toEqual(['q1']);
  });

  it('输入框已有草稿：用户放弃则草稿与队列都不动', async () => {
    const { wrapper, store } = await mountView();
    (wrapper.vm as any).draft = '我正在输入的新内容';
    confirmMock.mockRejectedValue(new Error('cancel'));

    await editBtn(wrapper).trigger('click');
    await flush();

    expect((wrapper.vm as any).draft).toBe('我正在输入的新内容');
    expect(api.cancel_queued_message).not.toHaveBeenCalled();
    expect(store.currentQueue.map((m) => m.id)).toEqual(['q1', 'q2']);
  });
});
