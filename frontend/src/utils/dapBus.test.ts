import { describe, expect, it, vi } from 'vitest';
import { emitDapOutput, onDapOutput } from './dapBus';

describe('dapBus', () => {
  it('fans debug adapter output out to every subscriber', () => {
    const a = vi.fn();
    const b = vi.fn();
    const offA = onDapOutput(a);
    const offB = onDapOutput(b);

    emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'hello\n' });

    expect(a).toHaveBeenCalledWith({ session_id: 'dap-1', category: 'stdout', text: 'hello\n' });
    expect(b).toHaveBeenCalledTimes(1);
    offA();
    offB();
  });

  it('stops delivering after unsubscribe', () => {
    const handler = vi.fn();
    const off = onDapOutput(handler);
    off();

    emitDapOutput({ session_id: 'dap-1', category: 'stderr', text: 'x' });

    expect(handler).not.toHaveBeenCalled();
  });

  it('一个订阅者抛错不拖垮广播与其他订阅者', () => {
    const seen: string[] = [];
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
    const offBad = onDapOutput(() => {
      throw new Error('订阅者自己炸了');
    });
    const offGood = onDapOutput((payload) => seen.push(payload.text));

    // 广播发生在 appStore.handleBridgeEvent 的事件泵里：异常若漏出去会打断
    // 桥事件分发，所以这里必须吞掉并记录（调试输出是尽力而为的原始流）
    expect(() => emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'a\n' })).not.toThrow();
    expect(seen).toEqual(['a\n']);

    errors.mockRestore();
    offBad();
    offGood();
  });

  it('取消订阅后再广播不重复投递（幂等）', () => {
    const handler = vi.fn();
    const off = onDapOutput(handler);
    off();
    off();

    emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'x' });

    expect(handler).not.toHaveBeenCalled();
  });
});
