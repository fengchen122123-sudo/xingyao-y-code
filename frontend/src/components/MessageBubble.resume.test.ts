import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import MessageBubble from './MessageBubble.vue';
import { useAppStore, type ChatMessage } from '../stores/appStore';

function errorMessage(): ChatMessage {
  return {
    role: 'assistant',
    content: '',
    reasoning_content: '',
    tool_calls: [],
    legacy_diffs: [],
    execution_state: 'error',
    error_note: 'APIError: OpenAIException - 余额不足',
  };
}

function planSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    session_name: 's1',
    parent_turn_id: 'turn-parent',
    resume_from_step_ordinal: 2,
    anchor: { complete: true, message_count: 3 },
    decisions: [
      { call_ordinal: 0, tool_name: 'read_file', klass: 'replay_with_result', reason: '' },
    ],
    blockers: [],
    replayable_count: 1,
    fresh_count: 0,
    blocked_count: 0,
    aborts: [],
    ...overrides,
  };
}

function mountBubble() {
  return mount(MessageBubble, { props: { msg: errorMessage() } });
}

let store: ReturnType<typeof useAppStore>;

beforeEach(() => {
  setActivePinia(createPinia());
  store = useAppStore();
  store.currentSessionName = 's1';
  (window as any).pywebview = undefined;
});

describe('MessageBubble 断点续跑入口（S4）', () => {
  it('开关关闭（无可用快照）时不渲染续跑按钮', () => {
    const wrapper = mountBubble();
    expect(wrapper.find('[data-test="resume-continue-btn"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it('resume_plan 返回可续跑时错误条旁出现「从断点继续」', async () => {
    const resumePlan = vi.fn(async () => ({
      ok: true,
      resumable: true,
      plan: planSnapshot(),
      aborts: [],
    }));
    (window as any).pywebview = { api: { resume_plan: resumePlan } };

    await store.loadResumePlan('s1');
    const wrapper = mountBubble();

    const btn = wrapper.get('[data-test="resume-continue-btn"]');
    expect(btn.text()).toContain('从断点继续');
    expect(resumePlan).toHaveBeenCalledWith('s1');
    wrapper.unmount();
  });

  it('aborts 非空（降级场景）时即使 ok 也不渲染按钮', async () => {
    const resumePlan = vi.fn(async () => ({
      ok: true,
      resumable: false,
      plan: planSnapshot({ aborts: ['session_archived'] }),
      aborts: ['session_archived'],
    }));
    (window as any).pywebview = { api: { resume_plan: resumePlan } };

    await store.loadResumePlan('s1');
    const wrapper = mountBubble();

    expect(wrapper.find('[data-test="resume-continue-btn"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it('点击续跑：confirmed 传空数组，confirm_required 时列出待确认写操作', async () => {
    const resumePlan = vi.fn(async () => ({
      ok: true,
      resumable: true,
      plan: planSnapshot(),
      aborts: [],
    }));
    const resumeExecute = vi.fn(async () => ({
      ok: false,
      reason: 'confirm_required',
      blockers: [{ call_ordinal: 1, tool_name: 'write_file', arguments_sha256: 'w'.repeat(64) }],
    }));
    (window as any).pywebview = { api: { resume_plan: resumePlan, resume_execute: resumeExecute } };

    await store.loadResumePlan('s1');
    const wrapper = mountBubble();
    await wrapper.get('[data-test="resume-continue-btn"]').trigger('click');
    await flushPromises();

    expect(resumeExecute).toHaveBeenCalledWith('s1', []);
    const blockers = wrapper.get('[data-test="resume-blockers"]');
    expect(blockers.text()).toContain('第 1 个调用 write_file 需手动重做');
    // 按钮仍在（未成功续跑），错误条与清单都在同一个气泡里
    expect(wrapper.find('[data-test="resume-continue-btn"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it('续跑成功则收起入口（快照清空）', async () => {
    const resumePlan = vi.fn(async () => ({
      ok: true,
      resumable: true,
      plan: planSnapshot(),
      aborts: [],
    }));
    const resumeExecute = vi.fn(async () => ({
      ok: true,
      turn_id: 'resume-abc12345',
      parent_turn_id: 'turn-parent',
      dry_run: true,
      replayed: 1,
    }));
    (window as any).pywebview = { api: { resume_plan: resumePlan, resume_execute: resumeExecute } };

    await store.loadResumePlan('s1');
    const wrapper = mountBubble();
    await wrapper.get('[data-test="resume-continue-btn"]').trigger('click');
    await flushPromises();

    expect(store.resumePlan).toBeNull();
    expect(wrapper.find('[data-test="resume-continue-btn"]').exists()).toBe(false);
    wrapper.unmount();
  });
});