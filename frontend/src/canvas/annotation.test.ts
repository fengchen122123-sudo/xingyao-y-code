// 标注层坐标换算的单测。
//
// 只测纯算术：jsdom 没有 canvas 实现（getContext 返回 null），凡是需要真实
// 画布才有意义的断言（笔迹像素、导出 PNG）在这里一律不写——那种测试会在
// CI 上假过或者干脆抛错，反而给「标注没问题」一个假证据。
import { describe, expect, it } from 'vitest';
import {
  ANNOTATION_COLORS,
  ANNOTATION_WIDTHS,
  annotationScale,
  displayToPixel,
  fitInto,
} from './annotation';

describe('annotationScale', () => {
  it('等比缩小：1024×768 的图显示成 512×384 时系数为 2', () => {
    expect(annotationScale(512, 384, 1024, 768)).toEqual({ sx: 2, sy: 2 });
  });

  it('非等比（显示尺寸被压扁）时横纵系数各自独立', () => {
    // 横纵必须分开算：共用 sx 会让纵向坐标整体偏上，标注落到圈外
    const scale = annotationScale(400, 300, 800, 900);
    expect(scale.sx).toBeCloseTo(2);
    expect(scale.sy).toBeCloseTo(3);
  });

  it('像素尺寸为 0（图未解码 / 坏图）时兜底成 1:1', () => {
    expect(annotationScale(512, 384, 0, 0)).toEqual({ sx: 1, sy: 1 });
    // 只坏一个方向也整体退回 1:1：混着用一个 2、一个 1 更难看懂
    expect(annotationScale(512, 384, 0, 384)).toEqual({ sx: 1, sy: 1 });
  });

  it('显示尺寸为 0 或 NaN 时同样兜底，绝不产生 Infinity', () => {
    expect(annotationScale(0, 0, 1024, 768)).toEqual({ sx: 1, sy: 1 });
    expect(annotationScale(Number.NaN, 100, 1024, 768)).toEqual({ sx: 1, sy: 1 });
  });
});

describe('displayToPixel', () => {
  const size = { width: 1000, height: 800 };
  const scale = { sx: 2, sy: 2 };

  it('四角与中心按比例换算', () => {
    expect(displayToPixel({ x: 0, y: 0 }, scale, size)).toEqual({ x: 0, y: 0 });
    // 中心点按 2 倍反推正好等于 width/height，夹取到 width-1/height-1 是设计行为
    expect(displayToPixel({ x: 500, y: 400 }, scale, size)).toEqual({ x: 999, y: 799 });
    expect(displayToPixel({ x: 250, y: 200 }, scale, size)).toEqual({ x: 500, y: 400 });
  });

  it('四舍五入取整：.5 向上、.499 向下', () => {
    // 显示坐标常带小数（指针在子像素上、scale 是浮点比值），canvas 只认整数
    expect(displayToPixel({ x: 10.25, y: 10.24 }, { sx: 2, sy: 2 }, size)).toEqual({
      x: 21,
      y: 20,
    });
    expect(displayToPixel({ x: 3.2, y: 3.7 }, { sx: 1, sy: 1 }, size)).toEqual({ x: 3, y: 4 });
  });

  it('越界夹取到 [0, width-1] / [0, height-1]', () => {
    // 右上角顶到浮层边缘时，scale 反推会得到 1000（== width），直接写进 canvas
    // 就是画到画布外——那部分笔迹在导出的 PNG 里会消失
    expect(displayToPixel({ x: 500, y: 0 }, scale, size)).toEqual({ x: 999, y: 0 });
    expect(displayToPixel({ x: 0, y: 400 }, scale, size)).toEqual({ x: 0, y: 799 });
    // 负坐标（指针拖出底图左边）同样夹回 0
    expect(displayToPixel({ x: -30, y: -12 }, scale, size)).toEqual({ x: 0, y: 0 });
    // 远远超出右下角
    expect(displayToPixel({ x: 9999, y: 9999 }, scale, size)).toEqual({ x: 999, y: 799 });
  });

  it('scale 非法时按 1:1 处理，不产出 NaN 坐标', () => {
    expect(displayToPixel({ x: 10, y: 20 }, { sx: 0, sy: Number.NaN }, size)).toEqual({
      x: 10,
      y: 20,
    });
  });

  it('像素尺寸为 0 时坐标退化成 0（不会写出负数上界）', () => {
    expect(displayToPixel({ x: 10, y: 20 }, scale, { width: 0, height: 0 })).toEqual({
      x: 0,
      y: 0,
    });
  });
});

describe('fitInto', () => {
  it('宽图按宽度贴边、高度等比缩下来', () => {
    expect(fitInto(1600, 900, 800, 560)).toEqual({ width: 800, height: 450 });
  });

  it('高图按高度贴边', () => {
    expect(fitInto(900, 1800, 800, 560)).toEqual({ width: 280, height: 560 });
  });

  it('小图放大有 2 倍上限；可用框非法时退回原图尺寸', () => {
    // 80px 的图拉到 800 宽只会更糊，且对标注精度无益
    expect(fitInto(80, 40, 800, 560)).toEqual({ width: 160, height: 80 });
    expect(fitInto(320, 200, 0, 0)).toEqual({ width: 320, height: 200 });
  });
});

describe('工具栏常量', () => {
  it('颜色三档且红色在首位（后端 prompt 只提「红色标注处」）', () => {
    expect(ANNOTATION_COLORS).toHaveLength(3);
    expect(ANNOTATION_COLORS[0].label).toBe('红');
    // 每档都是合法十六进制色，直接塞给 canvas 的 strokeStyle
    for (const color of ANNOTATION_COLORS) {
      expect(color.value).toMatch(/^#[0-9a-f]{6}$/i);
    }
  });

  it('笔宽三档且由细到粗严格递增', () => {
    expect(ANNOTATION_WIDTHS).toHaveLength(3);
    const widths = ANNOTATION_WIDTHS.map((w) => w.value);
    expect(widths).toEqual([...widths].sort((a, b) => a - b));
    expect(new Set(widths).size).toBe(3);
  });
});
