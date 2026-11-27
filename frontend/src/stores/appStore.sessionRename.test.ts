import { beforeEach, describe, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useAppStore } from './appStore';

describe('appStore session_renamed handling', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('收到 old === new 的同名 session_renamed 事件时保留消息列表与状态点，绝不退回空白新会话', () => {
    const store = useAppStore();
    store.currentSessionName = '你现在工作空间是什么';
    store.currentSessionDisplayName = '你现在工作空间是什么';
    store.messages = [
      { role: 'user', content: '你现在工作空间是什么' },
      { role: 'assistant', content: 'E:\\fch\\Y-code_tui' },
    ];
    store._captureCurrentRuntime();
    store.sessionStatusDots['你现在工作空间是什么'] = 'green';

    store.handleBridgeEvent({
      type: 'session_renamed',
      data: {
        old: '你现在工作空间是什么',
        new: '你现在工作空间是什么',
        old_name: '你现在工作空间是什么',
        new_name: '你现在工作空间是什么',
      },
    });

    expect(store.currentSessionName).toBe('你现在工作空间是什么');
    expect(store.messages).toHaveLength(2);
    expect(store.messages[1].content).toBe('E:\\fch\\Y-code_tui');
    expect(store.sessionRuntimeByName['你现在工作空间是什么']?.messages).toHaveLength(2);
    expect(store.sessionStatusDots['你现在工作空间是什么']).toBe('green');
  });

  it('收到 old !== new 的 session_renamed 事件时完整迁移 runtime、messages 与状态点', () => {
    const store = useAppStore();
    store.currentSessionName = '旧标题';
    store.currentSessionDisplayName = '旧标题';
    store.messages = [
      { role: 'user', content: '帮我查目录' },
      { role: 'assistant', content: '好的' },
    ];
    store._captureCurrentRuntime();
    store.sessionStatusDots['旧标题'] = 'green';

    store.handleBridgeEvent({
      type: 'session_renamed',
      data: {
        old: '旧标题',
        new: '新标题',
      },
    });

    expect(store.currentSessionName).toBe('新标题');
    expect(store.currentSessionDisplayName).toBe('新标题');
    expect(store.messages).toHaveLength(2);
    expect(store.sessionRuntimeByName['旧标题']).toBeUndefined();
    expect(store.sessionRuntimeByName['新标题']?.messages).toHaveLength(2);
    expect(store.sessionStatusDots['旧标题']).toBeUndefined();
    expect(store.sessionStatusDots['新标题']).toBe('green');
  });
});
