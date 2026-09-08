import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

const event = (type: string, data: any) => ({ type, data });

beforeEach(() => {
  setActivePinia(createPinia());
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('appStore step_metrics and streaming speed', () => {
  it('attaches step_metrics to the last assistant message', () => {
    const store = useAppStore();
    store.handleBridgeEvent(event('turn_start', { user_text: '计算斐波那契数列' }));
    store.handleBridgeEvent(event('token_delta', { text: '斐波那契数列代码如下：' }));

    const metricsData = {
      duration_s: 1.5,
      ttft_ms: 120,
      output_duration_s: 1.38,
      token_speed: 65.2,
      completion_tokens: 90,
    };

    store.handleBridgeEvent(event('step_metrics', metricsData));

    const lastMsg = store.messages.at(-1)!;
    expect(lastMsg.metrics).toBeDefined();
    expect(lastMsg.metrics?.duration_s).toBe(1.5);
    expect(lastMsg.metrics?.ttft_ms).toBe(120);
    expect(lastMsg.metrics?.output_duration_s).toBe(1.38);
    expect(lastMsg.metrics?.token_speed).toBe(65.2);
    expect(lastMsg.metrics?.completion_tokens).toBe(90);
  });

  it('updates streamingSpeed across chunks and resets on turn_finish', () => {
    const store = useAppStore();
    store.currentSessionName = 's1';
    store.handleBridgeEvent(event('turn_start', { session_name: 's1', user_text: '测试流速' }));

    store.handleBridgeEvent(event('token_delta', { session_name: 's1', text: '你好，世界！' }));
    vi.advanceTimersByTime(200);
    store.handleBridgeEvent(event('token_delta', { session_name: 's1', text: '紧接着是第二段流式文本' }));

    expect(store.streamingSpeed).toBeGreaterThan(0);

    store.handleBridgeEvent(event('turn_finish', { session_name: 's1' }));
    expect(store.streamingSpeed).toBe(0);
  });

  it('resets streamingSpeed on turn_cancelled and turn_error', () => {
    const store = useAppStore();
    store.currentSessionName = 's1';
    store.handleBridgeEvent(event('turn_start', { session_name: 's1', user_text: '测试取消' }));
    store.handleBridgeEvent(event('token_delta', { session_name: 's1', text: '即将被取消的文本1' }));
    vi.advanceTimersByTime(200);
    store.handleBridgeEvent(event('token_delta', { session_name: 's1', text: '即将被取消的文本2' }));
    expect(store.streamingSpeed).toBeGreaterThan(0);

    store.handleBridgeEvent(event('turn_cancelled', { session_name: 's1' }));
    expect(store.streamingSpeed).toBe(0);

    // 再次开始流式，并在 turn_error 时重置
    store.handleBridgeEvent(event('turn_start', { session_name: 's1', user_text: '测试错误' }));
    store.handleBridgeEvent(event('token_delta', { session_name: 's1', text: '即将出错的文本1' }));
    vi.advanceTimersByTime(200);
    store.handleBridgeEvent(event('token_delta', { session_name: 's1', text: '即将出错的文本2' }));
    expect(store.streamingSpeed).toBeGreaterThan(0);

    store.handleBridgeEvent(event('turn_error', { session_name: 's1', error: 'Network failure' }));
    expect(store.streamingSpeed).toBe(0);
  });
});



