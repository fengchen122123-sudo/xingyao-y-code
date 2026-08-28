import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { useAppStore } from './appStore';

// message_final 自愈校正：后端在 turn_finish 前补发最后一条助手消息全文，
// 修复流式 delta 在 evaluate_js 投递环节丢失导致的「回复尾部缺一段」。

const event = (type: string, data: any) => ({ type, data });

beforeEach(() => setActivePinia(createPinia()));

it('repairs truncated tail when final text is longer', () => {
  const store = useAppStore();
  store.handleBridgeEvent(event('turn_start', { user_text: '复盘一下' }));
  // 模拟流式尾部丢失：全文应是「后面 30 多分钟是空等」，UI 只收到前半截
  store.handleBridgeEvent(event('token_delta', { text: '一句话总结：后面 30' }));
  const message = store.messages.at(-1)!;
  expect(message.content).toBe('一句话总结：后面 30');

  store.handleBridgeEvent(event('message_final', {
    content: '一句话总结：后面 30 多分钟是空等',
    reasoning_content: '',
  }));

  expect(message.content).toBe('一句话总结：后面 30 多分钟是空等');
  expect(store.currentStreamingText).toBe('一句话总结：后面 30 多分钟是空等');
});

it('keeps content untouched when final text is equal or shorter', () => {
  const store = useAppStore();
  store.handleBridgeEvent(event('turn_start', { user_text: '你好' }));
  store.handleBridgeEvent(event('token_delta', { text: '完整回复' }));
  const message = store.messages.at(-1)!;

  // 等长：正常路径 no-op
  store.handleBridgeEvent(event('message_final', { content: '完整回复', reasoning_content: '' }));
  expect(message.content).toBe('完整回复');

  // 更短：绝不回退（防异常时序下旧消息全文覆盖新气泡）
  store.handleBridgeEvent(event('message_final', { content: '完', reasoning_content: '' }));
  expect(message.content).toBe('完整回复');
});

it('repairs reasoning_content tail as well', () => {
  const store = useAppStore();
  store.handleBridgeEvent(event('turn_start', { user_text: '思考一下' }));
  store.handleBridgeEvent(event('thinking_delta', { text: '推理前半' }));
  const message = store.messages.at(-1)!;

  store.handleBridgeEvent(event('message_final', {
    content: '',
    reasoning_content: '推理前半 + 推理后半',
  }));

  expect(message.reasoning_content).toBe('推理前半 + 推理后半');
});

it('ignores message_final when there is no assistant message', () => {
  const store = useAppStore();
  // 无任何消息时收到 message_final：不得抛异常、不得创建消息
  store.handleBridgeEvent(event('message_final', { content: '孤儿全文', reasoning_content: '' }));
  expect(store.messages).toHaveLength(0);
});
