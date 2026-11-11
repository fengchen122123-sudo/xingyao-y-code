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

  it('切回运行中会话时，快照文本不得覆盖末尾已完成气泡', async () => {
    const store = useAppStore();
    const fakeApi = {
      switch_session: async (sessionName: string) => ({
        ok: true,
        session_name: sessionName,
        messages: [
          { role: 'user', content: '第一问' },
          { role: 'assistant', content: '轮1', execution_state: 'completed' },
          { role: 'assistant', content: '', reasoning_content: '', tool_calls: [], execution_state: 'running' },
        ],
        workdir: 'GLOBAL',
        is_global: true,
        active_turn_snapshot: {
          session_name: sessionName,
          user_text: '第一问',
          display_text: '第一问',
          started_at_ms: 1700000009000,
          thinking_text: '',
          streaming_text: '轮2',
          tool_calls: [
            { id: 'call_2', name: 'read_file', label: '读取代码', started_at_ms: 1700000009500 },
          ],
        },
      }),
      get_file_tree: async () => [],
    };
    (window as any).pywebview = { api: fakeApi };

    await store.switchSession('session_snapshot_running');

    // 已完成的轮1气泡必须原样保留
    expect(store.messages[1].role).toBe('assistant');
    expect(store.messages[1].content).toBe('轮1');
    // 气泡数组里不得出现「轮1轮2」这类整回合拼接
    expect(store.messages.some(m => (m.content || '').includes('轮1轮2'))).toBe(false);

    // 快照文本只落在运行中气泡上
    const running = store.messages.at(-1)!;
    expect(running.role).toBe('assistant');
    expect(running.execution_state).toBe('running');
    expect(running.content).toBe('轮2');
    expect(running.tool_calls).toHaveLength(1);
    expect(running.tool_calls![0].id).toBe('call_2');
    expect(store.currentStreamingText).toBe('轮2');

    // 后续 token_delta 继续写进运行中气泡，不会回填到轮1
    store.handleBridgeEvent(event('token_delta', {
      session_name: 'session_snapshot_running',
      text: '续写',
    }));
    expect(store.messages[1].content).toBe('轮1');
    expect(store.messages.at(-1)!.content).toBe('轮2续写');
  });

  it('轮次边界一律清零：尾部混入非 assistant 消息时新气泡不继承上一轮正文', () => {
    const store = useAppStore();
    store.currentSessionName = 'session_round_boundary';
    store.messages = [
      { role: 'user', content: '第一问' },
      { role: 'assistant', content: '轮1正文', execution_state: 'completed' },
    ];
    // 上一轮残留的流式累积文本（真实场景里由快照恢复/流式吐字留下）
    store.currentStreamingText = '轮1正文';
    store.currentThinkingText = '轮1思考';
    // 轮次之间尾部混入非 assistant 消息：末尾不再是 assistant 气泡
    store.messages.push({ role: 'error', content: 'boom' });

    store.handleBridgeEvent(event('round_started', {
      session_name: 'session_round_boundary',
    }));
    expect(store.currentStreamingText).toBe('');
    expect(store.currentThinkingText).toBe('');

    store.handleBridgeEvent(event('token_delta', {
      session_name: 'session_round_boundary',
      text: '轮2',
    }));

    const lastAssistant = store.messages.filter(m => m.role === 'assistant').at(-1)!;
    expect(lastAssistant.content).toBe('轮2');
    expect(store.messages[1].content).toBe('轮1正文');
  });

  it('末尾是已完成气泡时，整回合快照必须新建 running 气泡承载', async () => {
    const store = useAppStore();
    const fakeApi = {
      switch_session: async (sessionName: string) => ({
        ok: true,
        session_name: sessionName,
        // 库里只有上一轮的已完成气泡（当前进行中轮次还没有本地气泡）
        messages: [
          { role: 'user', content: '第一问' },
          { role: 'assistant', content: '轮1', execution_state: 'completed' },
        ],
        workdir: 'GLOBAL',
        is_global: true,
        active_turn_snapshot: {
          session_name: sessionName,
          user_text: '第一问',
          display_text: '第一问',
          started_at_ms: 1700000011000,
          thinking_text: '整回合思考',
          streaming_text: '轮1轮2整回合文本',
          tool_calls: [{ id: 'call_3', name: 'read_file', label: '读取代码', started_at_ms: 1700000011500 }],
        },
      }),
      get_file_tree: async () => [],
    };
    (window as any).pywebview = { api: fakeApi };

    await store.switchSession('session_snapshot_completed_tail');

    // 已完成的轮1气泡绝不能被整回合快照覆盖
    expect(store.messages[1].content).toBe('轮1');
    // 快照内容落在新建的 running 气泡上
    expect(store.messages).toHaveLength(3);
    const running = store.messages.at(-1)!;
    expect(running.role).toBe('assistant');
    expect(running.execution_state).toBe('running');
    expect(running.content).toBe('轮1轮2整回合文本');
    expect(running.reasoning_content).toBe('整回合思考');
    expect(running.tool_calls).toHaveLength(1);

    // 后续流式吐字继续写进新建气泡，不回填到已完成气泡
    store.handleBridgeEvent(event('token_delta', {
      session_name: 'session_snapshot_completed_tail',
      text: '追加',
    }));
    expect(store.messages[1].content).toBe('轮1');
    expect(store.messages.at(-1)!.content).toBe('轮1轮2整回合文本追加');
  });
});
