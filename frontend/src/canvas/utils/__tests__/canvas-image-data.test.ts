import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  MAX_UPSCALE_LONG_EDGE,
  cropDataUrl,
  resolveUpscaleSize,
  splitDataUrl,
} from '../canvas-image-data';

// jsdom 不自带 canvas 实现（getContext 返回 null），这里注入最小 2D 上下文 stub，
// 只校验 drawImage 的裁剪参数与 toDataURL 调用，不做真实像素比对（计划 §0.7.3）。
type DrawCall = unknown[];

const drawCalls: DrawCall[] = [];

class FakeImage {
  static nextWidth = 0;
  static nextHeight = 0;

  width = FakeImage.nextWidth;
  height = FakeImage.nextHeight;
  crossOrigin: string | null = null;
  onload: (() => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  private currentSrc = '';

  get src(): string {
    return this.currentSrc;
  }

  set src(value: string) {
    this.currentSrc = value;
    queueMicrotask(() => this.onload?.());
  }
}

const contextStub = {
  drawImage: vi.fn((...args: unknown[]) => {
    drawCalls.push(args);
  }),
  save: vi.fn(),
  restore: vi.fn(),
  clearRect: vi.fn(),
  setTransform: vi.fn(),
  fillRect: vi.fn(),
  createRadialGradient: vi.fn(() => ({ addColorStop: vi.fn() })),
  imageSmoothingEnabled: true,
  imageSmoothingQuality: 'low' as ImageSmoothingQuality,
  fillStyle: '',
};

beforeEach(() => {
  drawCalls.length = 0;
  FakeImage.nextWidth = 400;
  FakeImage.nextHeight = 200;
  vi.stubGlobal('Image', FakeImage);
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    (() => contextStub) as unknown as typeof HTMLCanvasElement.prototype.getContext,
  );
  vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockImplementation(function (this: HTMLCanvasElement) {
    return `data:image/png;base64,${this.width}x${this.height}`;
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('canvas-image-data', () => {
  it('MAX_UPSCALE_LONG_EDGE 固定 4096', () => {
    expect(MAX_UPSCALE_LONG_EDGE).toBe(4096);
  });

  it('resolveUpscaleSize：目标长边超过 4096 时钳到 4096', () => {
    expect(resolveUpscaleSize(2048, 1024, 8192)).toEqual({ width: 4096, height: 2048 });
    expect(resolveUpscaleSize(1000, 500, 5000)).toEqual({ width: 4096, height: 2048 });
  });

  it('resolveUpscaleSize：缩小到目标长边，极小目标下宽高至少 1', () => {
    expect(resolveUpscaleSize(2000, 1000, 500)).toEqual({ width: 500, height: 250 });
    expect(resolveUpscaleSize(4000, 10, 1)).toEqual({ width: 1, height: 1 });
  });

  it('cropDataUrl：未给 crop 时取中心最大正方形', async () => {
    const dataUrl = await cropDataUrl('data:image/png;base64,AAAA');

    // 400x200 → 200x200，水平居中 sx=100 / sy=0
    expect(drawCalls).toHaveLength(1);
    expect(drawCalls[0].slice(1)).toEqual([100, 0, 200, 200, 0, 0, 200, 200]);
    expect(dataUrl).toBe('data:image/png;base64,200x200');
  });

  it('splitDataUrl：按行优先顺序切块，行/列号与 drawImage 参数一致', async () => {
    const pieces = await splitDataUrl('data:image/png;base64,AAAA', {
      horizontalLines: [0.5],
      verticalLines: [0.5],
    });

    // 400x200 切 2x2：[(0,0),(0,1),(1,0),(1,1)]
    expect(pieces.map((piece) => [piece.row, piece.column])).toEqual([
      [0, 0],
      [0, 1],
      [1, 0],
      [1, 1],
    ]);
    expect(drawCalls.map((call) => call.slice(1, 5))).toEqual([
      [0, 0, 200, 100],
      [200, 0, 200, 100],
      [0, 100, 200, 100],
      [200, 100, 200, 100],
    ]);
    expect(pieces.map((piece) => piece.dataUrl)).toEqual([
      'data:image/png;base64,200x100',
      'data:image/png;base64,200x100',
      'data:image/png;base64,200x100',
      'data:image/png;base64,200x100',
    ]);
  });

  it('splitDataUrl：切线先排序再切，界外切线被剔除', async () => {
    const pieces = await splitDataUrl('data:image/png;base64,AAAA', {
      horizontalLines: [],
      verticalLines: [0.8, 0.2, 1.5, -0.5],
    });

    // 400 宽：合法切线 0.2→80、0.8→320；1.5→600 与 -0.5→-200 越界剔除
    expect(pieces).toHaveLength(3);
    expect(drawCalls.map((call) => call.slice(1, 5))).toEqual([
      [0, 0, 80, 200],
      [80, 0, 240, 200],
      [320, 0, 80, 200],
    ]);
  });
});
