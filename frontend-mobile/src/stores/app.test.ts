import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useAppStore } from './app';

function fresh() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return useAppStore();
}

describe('mobile app store 事件语义', () => {
  let store: ReturnType<typeof useAppStore>;

  beforeEach(() => {
    store = fresh();
    store.currentSession = 's1';
    store.sessionGroups = [
      { folder_path: 'GLOBAL', folder_name: '对话', is_global: true, is_current: true, sessions: [{ name: 's1', display_name: 's1', count: 1, workdir: 'GLOBAL', is_global: true, updated_at: '' }] },
    ];
  });

  it('turn_start 去重用户消息并补 assistant 占位，绑定 turnId', () => {
    store.applyEvent('turn_start', { session_name: 's1', user_text: '你好', turn_id: 't9' });
    expect(store.messages.map((m) => m.role)).toEqual(['user', 'assistant']);
    expect(store.messages[0].turnId).toBe('t9');
    expect(store.isRunning).toBe(true);

    // 重复 turn_start（同文本）不重复插入用户气泡
    store.applyEvent('turn_start', { session_name: 's1', user_text: '你好', turn_id: 't9' });
    expect(store.messages.filter((m) => m.role === 'user')).toHaveLength(1);
  });

  it('token/thinking delta 累积到流式文本；非当前会话被忽略', () => {
    store.applyEvent('turn_start', { session_name: 's1', user_text: 'q' });
    store.applyEvent('thinking_delta', { session_name: 's1', text: '想' });
    store.applyEvent('token_delta', { session_name: 's1', text: '答' });
    store.applyEvent('token_delta', { session_name: 'other', text: 'X' });
    expect(store.currentThinkingText).toBe('想');
    expect(store.currentStreamingText).toBe('答');
    const last = store.messages[store.messages.length - 1];
    expect(last.content).toBe('答');
    expect(last.reasoning_content).toBe('想');
  });

  it('step_metrics 挂到最后一条 assistant', () => {
    store.applyEvent('turn_start', { session_name: 's1', user_text: 'q' });
    store.applyEvent('step_metrics', { session_name: 's1', duration_s: 1.5, completion_tokens: 120, token_speed: 38, ttft_ms: 900 });
    const last = store.messages[store.messages.length - 1];
    expect(last.metrics?.completion_tokens).toBe(120);
    expect(last.metrics?.ttft_ms).toBe(900);
  });

  it('message_final 终态全文自愈：仅在更长时覆盖', () => {
    store.applyEvent('turn_start', { session_name: 's1', user_text: 'q' });
    store.applyEvent('token_delta', { session_name: 's1', text: '短' });
    store.applyEvent('message_final', { session_name: 's1', content: '完整回复', reasoning_content: '' });
    expect(store.messages[store.messages.length - 1].content).toBe('完整回复');
    // 更短的 final 不回退
    store.applyEvent('message_final', { session_name: 's1', content: '短', reasoning_content: '' });
    expect(store.messages[store.messages.length - 1].content).toBe('完整回复');
  });

  it('turn_finish 收敛运行态并清空流式缓冲', () => {
    store.applyEvent('turn_start', { session_name: 's1', user_text: 'q' });
    store.applyEvent('token_delta', { session_name: 's1', text: 'a' });
    store.applyEvent('turn_finish', { session_name: 's1' });
    expect(store.isRunning).toBe(false);
    expect(store.currentStreamingText).toBe('');
    expect(store.messages[store.messages.length - 1].execution_state).toBe('completed');
  });

  it('message_queue 快照幂等替换当前会话队列', () => {
    store.applyEvent('message_queue', { session_name: 's1', queue: [{ id: 'q1', text: 'a', attachments: [] }, { id: 'q2', text: 'b', attachments: ['p1'] }] });
    expect(store.currentQueue).toHaveLength(2);
    store.applyEvent('message_queue', { session_name: 's1', queue: [{ id: 'q1', text: 'a', attachments: [] }] });
    expect(store.currentQueue).toHaveLength(1);
    store.applyEvent('message_queue', { session_name: 's1', queue: [] });
    expect(store.currentQueue).toHaveLength(0);
  });

  it('plan_updated 快照映射 subject/status', () => {
    store.applyEvent('plan_updated', {
      title: '计划',
      items: [
        { id: '1', subject: '第一步', status: 'completed' },
        { id: '2', subject: '第二步', status: 'in_progress' },
      ],
    });
    expect(store.planItems).toHaveLength(2);
    expect(store.planItems[0].subject).toBe('第一步');
    expect(store.planItems[0].status).toBe('completed');
  });

  it('permission_request 聚合去重；permission_dismiss 整体清空', () => {
    store.applyEvent('permission_request', { session_name: 's1', request_id: 'r1', summary: '执行 ls' });
    store.applyEvent('permission_request', { session_name: 's1', request_id: 'r1', summary: '执行 ls' });
    expect(store.permRequests).toHaveLength(1);
    expect(store.pendingPermCount).toBe(1);
    store.applyEvent('permission_dismiss', {});
    expect(store.permRequests).toHaveLength(0);
  });

  it('notice 写入会话通知流', () => {
    store.applyEvent('notice', { session_name: 's1', message: '提示', kind: 'notice' });
    expect(store.notices).toHaveLength(1);
    expect(store.notices[0].text).toBe('提示');
  });

  it('user_message_images 补挂到最近一条用户气泡', () => {
    store.applyEvent('turn_start', { session_name: 's1', user_text: '看图' });
    store.applyEvent('user_message_images', { session_name: 's1', images: ['data:image/png;base64,AAA'] });
    const user = store.messages.find((m) => m.role === 'user');
    expect(user?.media).toHaveLength(1);
    expect(user?.media?.[0].url).toBe('data:image/png;base64,AAA');
  });

  it('file_tree_loaded / skills_loaded 更新补全数据源', () => {
    store.applyEvent('file_tree_loaded', { tree: [{ name: 'src', children: [{ name: 'a.ts' }] }] });
    store.applyEvent('skills_loaded', { skills: [{ name: 'tdd' }] });
    expect(store.fileTree[0].name).toBe('src');
    expect(store.skills[0].name).toBe('tdd');
  });
});

describe('mobile app store 排队消息撤回', () => {
  function mockApi(overrides: Record<string, any> = {}) {
    const api = {
      cancel_queued_message: vi.fn(async () => ({ ok: true, removed: 1 })),
      get_files_info: vi.fn(async (paths: string[]) => paths.map((p) => ({ path: p, name: p.split('/').pop(), ok: true }))),
      ...overrides,
    };
    (window as any).pywebview = { api };
    return api;
  }

  function queuedStore() {
    const store = fresh();
    store.currentSession = 's1';
    store.queues = {
      s1: [
        { id: 'q1', text: '第一条', attachments: [] },
        { id: 'q2', text: '第二条', attachments: ['/tmp/a.png'] },
      ],
    };
    return store;
  }

  it('takeQueued 移出队列并回填文本与附件名', async () => {
    const api = mockApi();
    const store = queuedStore();

    const r = await store.takeQueued('q2');

    expect(r).toEqual({ text: '第二条', attachments: [{ path: '/tmp/a.png', name: 'a.png' }] });
    expect(store.currentQueue.map((m) => m.id)).toEqual(['q1']);
    expect(api.cancel_queued_message).toHaveBeenCalledWith('s1', 'q2');
  });

  it('id 不在队列时返回 null 且不调用取消接口', async () => {
    const api = mockApi();
    const store = queuedStore();

    expect(await store.takeQueued('missing')).toBeNull();
    expect(api.cancel_queued_message).not.toHaveBeenCalled();
    expect(store.currentQueue).toHaveLength(2);
  });

  it('取消失败时抛错且队列与草稿都不动（避免消息既在队列又在输入框）', async () => {
    mockApi({ cancel_queued_message: vi.fn(async () => { throw new Error('连接中断'); }) });
    const store = queuedStore();

    await expect(store.takeQueued('q2')).rejects.toThrow('连接中断');
    expect(store.currentQueue.map((m) => m.id)).toEqual(['q1', 'q2']);
  });

  it('附件名读取失败仍按路径兜底回填', async () => {
    mockApi({ get_files_info: vi.fn(async () => { throw new Error('读不到'); }) });
    const store = queuedStore();

    const r = await store.takeQueued('q2');

    expect(r?.attachments).toEqual([{ path: '/tmp/a.png', name: 'a.png' }]);
    expect(store.currentQueue.map((m) => m.id)).toEqual(['q1']);
  });
});
