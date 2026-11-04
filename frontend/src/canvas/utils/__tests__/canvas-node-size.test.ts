import { describe, expect, it } from 'vitest';

import { fitNodeSize, nodeSizeFromRatio } from '../canvas-node-size';

describe('canvas-node-size', () => {
  it('fitNodeSize：超上限时按短边等比缩到 640 框内', () => {
    expect(fitNodeSize(2000, 1000)).toEqual({ width: 640, height: 320 });
    expect(fitNodeSize(1000, 2000)).toEqual({ width: 320, height: 640 });
  });

  it('fitNodeSize：小图不放大（scale 上限 1）', () => {
    expect(fitNodeSize(200, 100)).toEqual({ width: 200, height: 100 });
    expect(fitNodeSize(640, 640)).toEqual({ width: 640, height: 640 });
  });

  it('fitNodeSize：短边缩到小于 48 时钳到最小 48', () => {
    // 4000x10：宽缩到 640，高 0.64 → 取最小 48
    expect(fitNodeSize(4000, 10)).toEqual({ width: 640, height: 48 });
    // 20x10：不缩放，两边都抬到 48
    expect(fitNodeSize(20, 10)).toEqual({ width: 48, height: 48 });
  });

  it('nodeSizeFromRatio："1280x720" 与 "16:9" 两种写法解析结果一致', () => {
    expect(nodeSizeFromRatio('1280x720', 320, 240)).toEqual({ width: 320, height: 180 });
    expect(nodeSizeFromRatio('16:9', 320, 240)).toEqual({ width: 320, height: 180 });
  });

  it('nodeSizeFromRatio：比例 <0.25 或 >4 回退基准尺寸', () => {
    // 100x1000 → 0.1 超出下限
    expect(nodeSizeFromRatio('100x1000', 320, 240)).toEqual({ width: 320, height: 240 });
    // 1000x100 → 10 超出上限
    expect(nodeSizeFromRatio('1000x100', 320, 240)).toEqual({ width: 320, height: 240 });
  });

  it('nodeSizeFromRatio：比基准更窄时按高度反算宽度；非尺寸串返回 null', () => {
    // 1:1 比基准 320x240 更窄 → 以高度 240 为基准
    expect(nodeSizeFromRatio('1:1', 320, 240)).toEqual({ width: 240, height: 240 });
    expect(nodeSizeFromRatio('', 320, 240)).toBeNull();
    expect(nodeSizeFromRatio('auto', 320, 240)).toBeNull();
  });
});
