import { describe, it, expect, beforeEach } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useAppStore } from './appStore';
import type { BridgeEvent } from '../types/pywebview';

function event(type: string, data: any): BridgeEvent {
  return { type, data };
}

describe('Active turn snapshot restore and streaming continuation', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('restores in-flight turn from get_initial_state and seamlessly appends new token_delta', async () => {
    const store = useAppStore();

    // 模拟手机扫码后 get_initial_state 返回了进行中回合的快照
    const fakeApi = {
      get_initial_state: async () => ({
        models: [],
        providers: [],
        active_model_id: 'm1',
        active_model_name: 'Test Model',
        permission_mode: 'confirm',
        sessions: [],
        workdir: 'GLOBAL',
        current_session: 'session_lan_test',
        // 数据库里只有更早的历史消息（或当前消息的用户输入，但没有 assistant 消息）
        messages: [{ role: 'user', content: '早先的历史消息' }],
        active_sessions: ['session_lan_test'],
        active_turn_snapshot: {
          session_name: 'session_lan_test',
          user_text: '帮我分析这段代码',
          display_text: '帮我分析这段代码',
          started_at_ms: 1700000000000,
          thinking_text: '思考中：首先分析结构...',
          streaming_text: '这是正在生成的第一段内容',
          tool_calls: [
            {
              id: 'call_1',
              name: 'read_file',
              label: '读取代码',
              started_at_ms: 1700000001000,
            },
          ],
        },
      }),
      get_system_settings: async () => ({}),
      get_token_usage: async () => ({}),
      get_sessions: async () => [],
      check_update: async () => ({}),
    };
    (window as any).pywebview = { api: fakeApi };

    await store.initFromBackend();

    // 断言当前会话处于 running 态
    expect(store.running).toBe(true);
    expect(store.runningSessions).toContain('session_lan_test');

    // 验证消息列表已还原：末尾包含用户输入及 running 助手卡片
    const msgs = store.messages;
    expect(msgs).toHaveLength(3);
    expect(msgs[1].role).toBe('user');
    expect(msgs[1].content).toBe('帮我分析这段代码');

    const assistant = msgs[2];
    expect(assistant.role).toBe('assistant');
    expect(assistant.execution_state).toBe('running');
    expect(assistant.content).toBe('这是正在生成的第一段内容');
    expect(assistant.reasoning_content).toBe('思考中：首先分析结构...');
    expect(assistant.tool_calls).toHaveLength(1);
    expect(assistant.tool_calls![0].id).toBe('call_1');

    // 验证 store 的流式追踪状态已同步还原
    expect(store.currentStreamingText).toBe('这是正在生成的第一段内容');
    expect(store.currentThinkingText).toBe('思考中：首先分析结构...');
    expect(store.turnStartedAt).toBe(1700000000000);

    // 模拟后续电脑端继续吐字，手机端收到新的 token_delta
    store.handleBridgeEvent(event('token_delta', {
      session_name: 'session_lan_test',
      text: '，这是追加的第二段。',
    }));

    // 验证无缝继续追加！
    expect(assistant.content).toBe('这是正在生成的第一段内容，这是追加的第二段。');
    expect(store.currentStreamingText).toBe('这是正在生成的第一段内容，这是追加的第二段。');

    // 模拟工具执行完毕
    store.handleBridgeEvent(event('tool_finish', {
      session_name: 'session_lan_test',
      id: 'call_1',
      name: 'read_file',
      ok: true,
      summary: '读取成功',
      result: 'file content',
    }));

    expect(assistant.tool_calls![0].result?.ok).toBe(true);
    expect(assistant.tool_calls![0].result?.summary).toBe('读取成功');
  });

  it('restores in-flight turn when switching sessions', async () => {
    const store = useAppStore();
    const fakeApi = {
      switch_session: async (sessionName: string) => ({
        ok: true,
        session_name: sessionName,
        messages: [{ role: 'user', content: '之前的问题' }],
        workdir: 'GLOBAL',
        is_global: true,
        active_turn_snapshot: {
          session_name: sessionName,
          user_text: '切进来的运行中任务',
          display_text: '切进来的运行中任务',
          started_at_ms: 1700000005000,
          thinking_text: '深度思考中...',
          streaming_text: '实时进度 50%',
          tool_calls: [],
        },
      }),
      get_file_tree: async () => [],
    };
    (window as any).pywebview = { api: fakeApi };

    await store.switchSession('session_running_target');

    expect(store.running).toBe(true);
    const last = store.messages.at(-1)!;
    expect(last.role).toBe('assistant');
    expect(last.execution_state).toBe('running');
    expect(last.content).toBe('实时进度 50%');
    expect(last.reasoning_content).toBe('深度思考中...');
  });

  it('sets yellow status dot when active_session_changed arrives for another session', () => {
    const store = useAppStore();
    store.currentSessionName = 'session_local';

    store.handleBridgeEvent(event('active_session_changed', {
      session_name: 'session_remote',
    }));

    expect(store.sessionStatusDots['session_remote']).toBe('yellow');
  });

  it('handles turn_error with error_note on existing assistant message', () => {
    const store = useAppStore();
    store.currentSessionName = 'session_err_test';
    store.messages = [
      { role: 'user', content: '测试报错' },
      { role: 'assistant', content: '部分输出...', execution_state: 'running' },
    ];
    store.running = true;
    store.runningSessions = ['session_err_test'];

    store.handleBridgeEvent(event('turn_error', {
      session_name: 'session_err_test',
      error: 'APIError: OpenAIException - 余额不足',
    }));

    const last = store.messages.at(-1)!;
    expect(last.role).toBe('assistant');
    expect(last.execution_state).toBe('error');
    expect(last.error_note).toBe('APIError: OpenAIException - 余额不足');
  });

  it('creates placeholder assistant bubble with error_note if turn_error occurs before assistant message exists', () => {
    const store = useAppStore();
    store.currentSessionName = 'session_err_test2';
    store.messages = [
      { role: 'user', content: '测试报错' },
    ];
    store.running = true;
    store.runningSessions = ['session_err_test2'];

    store.handleBridgeEvent(event('turn_error', {
      session_name: 'session_err_test2',
      error: 'litellm.APIError: APIError: OpenAIException - 余额不足',
    }));

    expect(store.messages).toHaveLength(2);
    const last = store.messages.at(-1)!;
    expect(last.role).toBe('assistant');
    expect(last.execution_state).toBe('error');
    expect(last.error_note).toBe('litellm.APIError: APIError: OpenAIException - 余额不足');
  });

  it('restores error_note and execution_state from get_initial_state messages', async () => {
    const store = useAppStore();
    const fakeApi = {
      get_initial_state: async () => ({
        models: [],
        providers: [],
        active_model_id: 'm1',
        active_model_name: 'Test Model',
        permission_mode: 'confirm',
        sessions: [],
        workdir: 'GLOBAL',
        current_session: 'session_with_error',
        messages: [
          { role: 'user', content: '发问' },
          {
            role: 'assistant',
            content: '',
            execution_state: 'error',
            error_note: 'APIError: OpenAIException - 余额不足',
          },
        ],
        active_sessions: [],
        active_turn_snapshot: null,
      }),
      get_system_settings: async () => ({}),
      get_token_usage: async () => ({}),
      get_sessions: async () => [],
      check_update: async () => ({}),
    };
    (window as any).pywebview = { api: fakeApi };

    await store.initFromBackend();

    expect(store.messages).toHaveLength(2);
    const last = store.messages.at(-1)!;
    expect(last.role).toBe('assistant');
    expect(last.execution_state).toBe('error');
    expect(last.error_note).toBe('APIError: OpenAIException - 余额不足');
  });
});
