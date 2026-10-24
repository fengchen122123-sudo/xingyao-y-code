import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import DecisionRecordPopup from './DecisionRecordPopup.vue';
import { useAppStore } from '../stores/appStore';

// mock 决策桥方法：默认未启用 + 无记录；用例按需覆盖
function mockApi(overrides: Record<string, unknown> = {}) {
  const api = {
    get_decision_audit_summary: vi.fn(async () => ({
      ok: true,
      today_count: 0,
      today_shadow_count: 0,
      today_adopted_count: 0,
      today_fallback_count: 0,
      recent: [],
    })),
    get_decision_settings: vi.fn(async () => ({
      ok: true,
      settings: { enabled: false, model_id: 'NeoHorse-Jev-4B' },
      runtime: {},
    })),
    open_decision_audit_dir: vi.fn(async () => ({ ok: true })),
    get_decision_record_detail: vi.fn(async () => ({ ok: false, msg: '未找到这条决策记录' })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

describe('DecisionRecordPopup', () => {
  it('未启用且无记录：显示未启用状态与空态占位', async () => {
    mockApi();
    useAppStore().currentSessionName = 'sess-A';
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();

    expect(wrapper.find('[data-test="decision-pop-status"]').text()).toContain('未启用');
    expect(wrapper.find('[data-test="decision-pop-empty"]').text()).toContain('本会话还没有决策记录');
  });

  it('今天没有决策但历史有记录：列表照常展示（门禁不看 today_count）', async () => {
    mockApi({
      get_decision_audit_summary: vi.fn(async () => ({
        ok: true,
        today_count: 0,
        today_shadow_count: 0,
        today_adopted_count: 0,
        today_fallback_count: 0,
        recent: [
          { ts: '2026-10-19T23:50:00', scene: 'router', shadow: false, adopted: true, latency_ms: 300, fallback_reason: '', model_id: 'NeoHorse-Jev-4B', answers_digest: '', state_digest: '昨天那次判断', answer: 'main' },
        ],
      })),
    });
    useAppStore().currentSessionName = 'sess-A';
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();

    // 昨天有决策、今天没有：仍要能看到最近记录，不能退化成空态
    expect(wrapper.find('[data-test="decision-pop-empty"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="decision-pop-list"]').text()).toContain('昨天那次判断');
    // 今日计数照旧单独展示，明确是「本会话今日」
    expect(wrapper.find('[data-test="decision-pop-stats"]').text()).toContain('本会话今日 0 次决策');
  });

  it('有记录：渲染统计行与最近记录列表（含影子标记与延迟）', async () => {
    const api = mockApi({
      get_decision_audit_summary: vi.fn(async () => ({
        ok: true,
        today_count: 2,
        today_shadow_count: 1,
        today_adopted_count: 1,
        today_fallback_count: 0,
        recent: [
          { ts: '2026-10-16T10:00:00', scene: 'probe', shadow: false, adopted: true, latency_ms: 326, fallback_reason: '', model_id: 'NeoHorse-Jev-4B', answers_digest: '' },
          { ts: '2026-10-16T10:01:00', scene: 'router', shadow: true, adopted: false, latency_ms: 210, fallback_reason: '', model_id: 'NeoHorse-Jev-4B', answers_digest: '', state_digest: '帮我把登录模块重构成 async/await', answer: 'main' },
        ],
      })),
      get_decision_settings: vi.fn(async () => ({
        ok: true,
        settings: { enabled: true, model_id: 'NeoHorse-Jev-4B' },
        runtime: {},
      })),
    });
    useAppStore().currentSessionName = 'sess-A';
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();

    // 记录按会话隔离：limit=10 + 当前会话名 + session_only=true
    // （today_* 与 recent 都只看本会话，全局口径只留给设置页面板）
    expect(api.get_decision_audit_summary).toHaveBeenCalledTimes(1);
    expect(api.get_decision_audit_summary).toHaveBeenCalledWith(10, 'sess-A', true);

    expect(wrapper.find('[data-test="decision-pop-status"]').text()).toContain('已启用');
    expect(wrapper.find('[data-test="decision-pop-stats"]').text()).toContain('今日 2 次决策');
    const list = wrapper.find('[data-test="decision-pop-list"]').text();
    // 场景显示中文标签：一眼看出这条记录在判什么
    expect(list).toContain('探针（连通性）');
    expect(list).toContain('路由（选模型）');
    expect(list).toContain('影子');
    expect(list).toContain('326ms');
    // 业务记录必须带上「判定的事由」与「模型结论」
    expect(list).toContain('帮我把登录模块重构成 async/await');
    expect(list).toContain('→ main');
    // 采纳状态与回退原因互斥展示
    expect(list).toContain('已采纳');
  });

  it('会话名为空（新会话懒创建阶段）时不请求审计汇总，直接显示空态', async () => {
    const api = mockApi();
    const store = useAppStore();
    expect(store.currentSessionName).toBe('');

    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();

    expect(api.get_decision_audit_summary).not.toHaveBeenCalled();
    expect(wrapper.find('[data-test="decision-pop-empty"]').exists()).toBe(true);

    // 空白会话名同样不发请求
    await wrapper.unmount();
    store.currentSessionName = '   ';
    const wrapper2 = mount(DecisionRecordPopup);
    await flushPromises();
    expect(api.get_decision_audit_summary).not.toHaveBeenCalled();
    wrapper2.unmount();
  });

  it('切换当前会话后，按新会话名重新拉取一次记录', async () => {
    const api = mockApi();
    const store = useAppStore();
    store.currentSessionName = 'sess-A';
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();
    expect(api.get_decision_audit_summary).toHaveBeenCalledTimes(1);
    expect(api.get_decision_audit_summary).toHaveBeenLastCalledWith(10, 'sess-A', true);

    store.currentSessionName = 'sess-B';
    await flushPromises();

    expect(api.get_decision_audit_summary).toHaveBeenCalledTimes(2);
    expect(api.get_decision_audit_summary).toHaveBeenLastCalledWith(10, 'sess-B', true);
    wrapper.unmount();
  });

  it('点开一条记录：展示发给 JEV 的请求原文与 JEV 的判定原文', async () => {
    const api = mockApi({
      get_decision_audit_summary: vi.fn(async () => ({
        ok: true,
        today_count: 1,
        today_shadow_count: 0,
        today_adopted_count: 1,
        today_fallback_count: 0,
        recent: [
          {
            ts: '2026-10-20T10:50:25', scene: 'router', shadow: false, adopted: true,
            latency_ms: 804, fallback_reason: '', model_id: 'NeoHorse-Jev-4B',
            answers_digest: '', state_digest: '1+1 等于几？', answer: 'secondary',
            trace_id: 'abc123',
          },
        ],
      })),
      get_decision_record_detail: vi.fn(async () => ({
        ok: true,
        record: {
          ts: '2026-10-20T10:50:25',
          scene: 'router',
          model_id: 'NeoHorse-Jev-4B',
          request: {
            model: 'NeoHorse-Jev-4B',
            state: '1+1 等于几？',
            questions: { q: { type: 'choice', criteria: { main: '更强', secondary: '更便宜' } } },
          },
          answers: {
            q: { type: 'choice', choice: 'secondary', probabilities: { main: 0.001, secondary: 0.999 } },
          },
        },
      })),
    });
    useAppStore().currentSessionName = 'sess-A';
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();

    // 默认收起
    expect(wrapper.find('[data-test="decision-pop-detail"]').exists()).toBe(false);

    await wrapper.find('[data-test="decision-pop-item"]').trigger('click');
    await flushPromises();

    expect(api.get_decision_record_detail).toHaveBeenCalledWith('abc123');
    const request = wrapper.find('[data-test="decision-pop-request"]').text();
    expect(request).toContain('"state": "1+1 等于几？"');
    expect(request).toContain('"type": "choice"');
    expect(request).toContain('"secondary": "更便宜"');
    const answers = wrapper.find('[data-test="decision-pop-answers"]').text();
    expect(answers).toContain('"choice": "secondary"');
    expect(answers).toContain('0.999');

    // 再点一次收起
    await wrapper.find('[data-test="decision-pop-item"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-test="decision-pop-detail"]').exists()).toBe(false);
  });

  it('连点两条记录：先发出的详情请求后返回也不能覆盖后一条', async () => {
    // 用对象持有 resolve：直接 let 变量会被 TS 收窄成 null（赋值发生在 Promise executor 里）
    const first = { resolve: null as ((value: unknown) => void) | null };
    const api = mockApi({
      get_decision_audit_summary: vi.fn(async () => ({
        ok: true,
        today_count: 2,
        today_shadow_count: 0,
        today_adopted_count: 2,
        today_fallback_count: 0,
        recent: [
          { ts: '2026-10-20T10:00:00', scene: 'router', shadow: false, adopted: true, latency_ms: 300, fallback_reason: '', model_id: 'NeoHorse-Jev-4B', answers_digest: '', state_digest: '第一条', answer: 'main', trace_id: 'trace-A' },
          { ts: '2026-10-20T10:01:00', scene: 'router', shadow: false, adopted: true, latency_ms: 320, fallback_reason: '', model_id: 'NeoHorse-Jev-4B', answers_digest: '', state_digest: '第二条', answer: 'secondary', trace_id: 'trace-B' },
        ],
      })),
      get_decision_record_detail: vi.fn((traceId: string) => {
        if (traceId === 'trace-A') {
          return new Promise((resolve) => { first.resolve = resolve; });
        }
        return Promise.resolve({
          ok: true,
          record: { trace_id: 'trace-B', request: { state: '第二条' }, answers: { q: { choice: 'secondary' } } },
        });
      }),
    });
    useAppStore().currentSessionName = 'sess-A';
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();

    const items = wrapper.findAll('[data-test="decision-pop-item"]');
    await items[0]?.trigger('click'); // 先点第一条：请求挂起不返回
    await items[1]?.trigger('click'); // 立刻点第二条：应以后者为准
    await flushPromises();
    expect(api.get_decision_record_detail).toHaveBeenCalledTimes(2);

    // 第一条的迟到响应此刻才回来，必须被丢弃（不能覆盖第二条的详情）
    first.resolve?.({
      ok: true,
      record: { trace_id: 'trace-A', request: { state: '第一条' }, answers: { q: { choice: 'main' } } },
    });
    await flushPromises();

    const detail = wrapper.find('[data-test="decision-pop-detail"]').text();
    expect(detail).toContain('第二条');
    expect(detail).not.toContain('第一条');
  });

  it('去设置：切到决策分区并关闭面板', async () => {
    mockApi();
    const store = useAppStore();
    store.currentSessionName = 'sess-A';
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();

    await wrapper.find('[data-test="decision-pop-open-settings"]').trigger('click');
    expect(store.settingsSection).toBe('decision');
    expect(store.settingsOpen).toBe(true);
    expect(wrapper.emitted('close')).toBeTruthy();
  });

  it('打开审计目录：调用桥方法', async () => {
    const api = mockApi();
    useAppStore().currentSessionName = 'sess-A';
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();

    await wrapper.find('[data-test="decision-pop-open-audit"]').trigger('click');
    await flushPromises();
    expect(api.open_decision_audit_dir).toHaveBeenCalledTimes(1);
  });

  it('面板开启期间收到新决策事件：自动刷新记录并保持已读', async () => {
    const api = mockApi();
    const store = useAppStore();
    store.currentSessionName = 'sess-A';
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();
    expect(api.get_decision_audit_summary).toHaveBeenCalledTimes(1);
    expect(api.get_decision_audit_summary).toHaveBeenLastCalledWith(10, 'sess-A', true);

    // 模拟后端 decision_recorded 事件（store 侧 tick 递增）
    store.decisionUnread = 3;
    store.decisionEventTick += 1;
    await flushPromises();

    expect(api.get_decision_audit_summary).toHaveBeenCalledTimes(2);
    expect(store.decisionUnread).toBe(0);
    wrapper.unmount();
  });
});