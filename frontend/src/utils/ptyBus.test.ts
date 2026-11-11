import { describe, expect, it, vi } from 'vitest';
import { emitPtyData, emitPtyExit, onPtyData, onPtyExit } from './ptyBus';

describe('ptyBus', () => {
  it('fans terminal output out to every subscriber', () => {
    const a = vi.fn();
    const b = vi.fn();
    const offA = onPtyData(a);
    const offB = onPtyData(b);

    emitPtyData('hello\r\n');

    expect(a).toHaveBeenCalledWith('hello\r\n');
    expect(b).toHaveBeenCalledWith('hello\r\n');
    offA();
    offB();
  });

  it('stops delivering after unsubscribe', () => {
    const handler = vi.fn();
    const off = onPtyData(handler);
    off();

    emitPtyData('after');

    expect(handler).not.toHaveBeenCalled();
  });

  it('delivers exit codes', () => {
    const handler = vi.fn();
    const off = onPtyExit(handler);

    emitPtyExit(0);
    emitPtyExit(3);

    expect(handler.mock.calls.map((c) => c[0])).toEqual([0, 3]);
    off();
  });
});
