import { describe, it, expect } from 'vitest';
import {
  estimateDeltaTokens,
  StreamingSpeedTracker,
  formatTokenSpeed,
  formatDuration,
} from './tokenSpeed';

describe('tokenSpeed', () => {
  describe('estimateDeltaTokens', () => {
    it('returns 0 for empty text', () => {
      expect(estimateDeltaTokens('')).toBe(0);
    });

    it('estimates tokens for Chinese characters', () => {
      const tokens = estimateDeltaTokens('这是一段测试中文文本，用于检验速度');
      expect(tokens).toBeGreaterThanOrEqual(10);
      expect(tokens).toBeLessThanOrEqual(15);
    });

    it('estimates tokens for ASCII code/words', () => {
      const tokens = estimateDeltaTokens('const a = 123; function test() {}');
      expect(tokens).toBeGreaterThanOrEqual(8);
      expect(tokens).toBeLessThanOrEqual(14);
    });
  });

  describe('StreamingSpeedTracker', () => {
    it('tracks streaming speed across chunks', () => {
      const tracker = new StreamingSpeedTracker(1000);
      expect(tracker.getSpeed()).toBe(0);

      tracker.addChunk('你好，世界！这是一段用于测试流式生成速度的文本', 1000);
      const speed = tracker.addChunk('紧接着是第二段用于测速的文字内容', 1500);

      expect(speed).toBeGreaterThan(0);
      expect(tracker.getTotalTokens()).toBeGreaterThan(0);

      expect(tracker.getSpeed(3500)).toBe(0);

      tracker.reset();
      expect(tracker.getSpeed(3600)).toBe(0);
      expect(tracker.getTotalTokens()).toBe(0);
    });
  });

  describe('formatting utilities', () => {
    it('formats token speed correctly', () => {
      expect(formatTokenSpeed(0)).toBe('0 t/s');
      expect(formatTokenSpeed(50)).toBe('50 t/s');
      expect(formatTokenSpeed(58.2)).toBe('58.2 t/s');
      expect(formatTokenSpeed(58.24)).toBe('58.2 t/s');
    });

    it('formats duration correctly', () => {
      expect(formatDuration(0)).toBe('0s');
      expect(formatDuration(2.45)).toBe('2.5s');
      expect(formatDuration(15.2)).toBe('15s');
    });
  });
});
