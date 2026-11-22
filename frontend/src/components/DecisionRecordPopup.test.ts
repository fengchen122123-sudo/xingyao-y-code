import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import DecisionRecordPopup from './DecisionRecordPopup.vue';
import { useAppStore } from '../stores/appStore';

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
    expect(wrapper.find('[data-test="decision-pop-status"]').text()).toContain('当前会话');
    expect(wrapper.find('[data-test="decision-pop-stats"]').text()).toContain('今日0');
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

    expect(api.get_decision_audit_summary).toHaveBeenCalledTimes(1);
    expect(api.get_decision_audit_summary).toHaveBeenCalledWith(10, 'sess-A', true);

    expect(wrapper.find('[data-test="decision-pop-status"]').text()).toContain('已启用');
    const statsText = wrapper.find('[data-test="decision-pop-stats"]').text();
    expect(statsText).toContain('今日2');
    expect(statsText).toContain('影子1');
    expect(statsText).toContain('采纳1');
    expect(statsText).toContain('回退0');
    const list = wrapper.find('[data-test="decision-pop-list"]').text();
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

  it('任务级场景展示中文名称，长事由摘要完整展示不截断，且关闭按钮可触发 close', async () => {
    const longDigest = '角色：explore；计划项：P1；工具成功：15；遗留报错工具：无；任务：【只读扫描盘点】检查 E 盘大体积目录与镜像分布情况完整保留';
    mockApi({
      get_decision_audit_summary: vi.fn(async () => ({
        ok: true,
        today_count: 4,
        today_shadow_count: 2,
        today_adopted_count: 1,
        today_fallback_count: 1,
        recent: [
          { ts: '2026-10-20T21:25:29', scene: 'subagent_review', shadow: true, adopted: false, latency_ms: 625, fallback_reason: '', model_id: 'NeoHorse-Jev-4B', answers_digest: '', state_digest: longDigest, answer: 'accepted', trace_id: 't1' },
          { ts: '2026-10-20T21:49:07', scene: 'loop_guard', shadow: false, adopted: true, latency_ms: 765, fallback_reason: '', model_id: 'NeoHorse-Jev-4B', answers_digest: '', state_digest: '步数：4/200', answer: 'pivot', trace_id: 't2' },
          { ts: '2026-10-20T21:55:00', scene: 'plan_recovery', shadow: false, adopted: false, latency_ms: 510, fallback_reason: 'timeout', model_id: 'NeoHorse-Jev-4B', answers_digest: '', state_digest: '计划项 P2 重试判断', answer: '', trace_id: 't3' },
          { ts: '2026-10-20T21:58:00', scene: 'memory_filter', shadow: true, adopted: false, latency_ms: 480, fallback_reason: '', model_id: 'NeoHorse-Jev-4B', answers_digest: '', state_digest: '待沉淀记忆候选项', answer: 'keep_facts_only', trace_id: 't4' },
        ],
      })),
    });
    useAppStore().currentSessionName = 'sess-A';
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();

    const listText = wrapper.find('[data-test="decision-pop-list"]').text();
    expect(listText).toContain('子代理验收');
    expect(listText).toContain('验收通过');
    expect(listText).toContain('循环止损');
    expect(listText).toContain('改换思路');
    expect(listText).toContain('计划恢复');
    expect(listText).toContain('回退：timeout');
    expect(listText).toContain('记忆筛选');
    expect(listText).toContain('仅留事实');
    expect(listText).toContain(longDigest);

    await wrapper.find('[data-test="decision-pop-close"]').trigger('click');
    expect(wrapper.emitted('close')).toBeTruthy();
  });

  it('会话名为空（新会话懒创建阶段）时不请求审计汇总，直接显示空态', async () => {
    const api = mockApi();
    const store = useAppStore();
    expect(store.currentSessionName).toBe('');

    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();

    expect(api.get_decision_audit_summary).not.toHaveBeenCalled();
    expect(wrapper.find('[data-test="decision-pop-empty"]').exists()).toBe(true);

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

    await wrapper.find('[data-test="decision-pop-item"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-test="decision-pop-detail"]').exists()).toBe(false);
  });

  it('连点两条记录：先发出的详情请求后返回也不能覆盖后一条', async () => {
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
    await items[0]?.trigger('click');
    await items[1]?.trigger('click');
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

    store.decisionUnread = 3;
    store.decisionEventTick += 1;
    await flushPromises();

    expect(api.get_decision_audit_summary).toHaveBeenCalledTimes(2);
    expect(store.decisionUnread).toBe(0);
    wrapper.unmount();
  });

  it('明确展示当前会话累计决策数量与全局今日计数，超过 10 条时支持查看全部', async () => {
    const api = mockApi({
      get_decision_audit_summary: vi.fn(async (limit?: number) => ({
        ok: true,
        today_count: 12,
        today_shadow_count: 0,
        today_adopted_count: 12,
        today_fallback_count: 0,
        session_total_count: 15,
        global_today_count: 29,
        recent: Array.from({ length: limit === 10 ? 10 : 15 }, (_, idx) => ({
          ts: `2026-11-20T16:00:${String(idx).padStart(2, '0')}`,
          scene: 'auto_permission',
          shadow: false,
          adopted: true,
          latency_ms: 500,
          fallback_reason: '',
          model_id: 'NeoHorse-Jev-4B',
          answers_digest: '',
          state_digest: `命令 ${idx + 1}`,
          answer: 'allow',
          trace_id: `t-${idx}`,
        })),
      })),
    });
    useAppStore().currentSessionName = 'sess-A';
    const wrapper = mount(DecisionRecordPopup);
    await flushPromises();

    expect(wrapper.find('[data-test="decision-pop-scope"]').text()).toContain('当前会话 · 共 15 次');
    expect(wrapper.find('[data-test="decision-pop-session-total"]').text()).toContain('15');
    expect(wrapper.find('[data-test="decision-pop-stats"]').text()).toContain('本会话今日12');
    expect(wrapper.find('[data-test="decision-pop-global-hint"]').text()).toContain('全部会话今日共 29 次');
    expect(wrapper.find('.foot-hint').text()).toContain('本会话共 15 条 · 显示最近 10 条');

    const loadMoreBtn = wrapper.find('[data-test="decision-pop-load-more"]');
    expect(loadMoreBtn.exists()).toBe(true);
    await loadMoreBtn.trigger('click');
    await flushPromises();

    expect(api.get_decision_audit_summary).toHaveBeenLastCalledWith(50, 'sess-A', true);
    expect(wrapper.findAll('[data-test="decision-pop-item"]')).toHaveLength(15);
    expect(wrapper.find('[data-test="decision-pop-load-more"]').exists()).toBe(false);
    wrapper.unmount();
  });
});