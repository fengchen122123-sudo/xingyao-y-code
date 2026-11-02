// 画布「画笔标注」的坐标换算与工具栏常量（纯逻辑，不碰 DOM）。
//
// 为什么单独抽一个文件：标注浮层的硬约束是「导出的标注层 PNG 必须与原图同尺寸」——
// canvas 的内部像素设成底图的 naturalWidth/naturalHeight，笔迹坐标则从浮层的
// 显示尺寸换算过去。这套换算最容易在改了浮层布局（最大宽度、内边距、窗口尺寸）
// 之后悄悄错位，而错位只表现为「导出后标注整体偏移」，肉眼当场看不出来，要等到
// 花了 0.2 元发现模型改错地方才发现。抽成纯函数就能用 vitest 钉死。
//
// 另一个不能踩的坑：绝不能把底图 drawImage 到 canvas 上。底图 url 来自本机媒体
// 服务的另一个端口，在 WebView 里属跨源图片，画进去会把 canvas 标脏（tainted），
// toBlob 直接抛安全错误。所以 canvas 只画笔迹，透明层导出后交给后端合成。

/** 浮层显示坐标（CSS 像素，相对底图左上角） */
export interface DisplayPoint {
  x: number;
  y: number;
}

/** 原图像素坐标（写进 canvas 的坐标系） */
export interface PixelPoint {
  x: number;
  y: number;
}

/** 显示尺寸 → 原图像素尺寸的换算系数：pixel = display * sx/sy */
export interface AnnotationScale {
  sx: number;
  sy: number;
}

/** 像素尺寸（宽高都用同一形状，便于 fitInto / displayToPixel 复用） */
export interface PixelSize {
  width: number;
  height: number;
}

/**
 * 颜色三档：红在首位即默认色。
 * 为什么固定三档饱和色而不是取色器：后端 prompt 里写死了一句「图中红色标注处
 * 需要按上述要求修改」（见 gui_canvas_mixin.canvas_edit_image_annotated），
 * 颜色越少、对比越强，模型越不容易把标注当成画面内容的一部分。
 */
export const ANNOTATION_COLORS = [
  { value: '#ff3b30', label: '红' },
  { value: '#2f7bff', label: '蓝' },
  { value: '#ffcc00', label: '黄' },
] as const;

/**
 * 笔宽三档（单位：显示像素）。
 * 落进 canvas 时会乘 scale 换成原图像素宽度：同一档笔宽在小图上就是细线、
 * 在 4000px 大图上按比例变粗，导出的标注在成图里视觉粗细始终一致。
 */
export const ANNOTATION_WIDTHS = [
  { value: 3, label: '细' },
  { value: 6, label: '中' },
  { value: 11, label: '粗' },
] as const;

function isPositiveFinite(value: number): boolean {
  return Number.isFinite(value) && value > 0;
}

/**
 * 显示尺寸与原图像素尺寸的换算系数。
 *
 * 像素尺寸非法（≤0 / NaN，通常是图还没解码完或拿到坏图）时返回 1:1：
 * 宁可按显示坐标先画，也不能让 0 或 NaN 顺着乘法污染所有坐标——那会画出
 * 一片空白，而且完全看不出是哪一环算错的。
 */
export function annotationScale(
  displayW: number,
  displayH: number,
  pixelW: number,
  pixelH: number,
): AnnotationScale {
  if (
    !isPositiveFinite(displayW) ||
    !isPositiveFinite(displayH) ||
    !isPositiveFinite(pixelW) ||
    !isPositiveFinite(pixelH)
  ) {
    return { sx: 1, sy: 1 };
  }
  return { sx: pixelW / displayW, sy: pixelH / displayH };
}

/** 把值夹成 [0, limit-1] 的整数：limit 非法时退化成 0，绝不让坐标越界写出画布 */
function clampPixel(value: number, limit: number): number {
  if (!Number.isFinite(value)) return 0;
  const max = isPositiveFinite(limit) ? Math.floor(limit) - 1 : 0;
  return Math.min(max, Math.max(0, value));
}

/**
 * 浮层显示坐标 → 原图像素坐标：先按 scale 缩放、再四舍五入取整、最后夹进
 * [0, width-1] / [0, height-1]。
 *
 * 为什么要显式传 size 而不是只按 scale 反推：canvas 的合法坐标上界由内部像素
 * 尺寸决定，而 scale 是浮点比值，反推出来的边界必然差一两像素。指针一旦拖到
 * 浮层边缘（CSS 尺寸取整后可能比实际显示宽 1px），不夹取就会画到画布外面去，
 * 那部分笔迹在导出的 PNG 里直接消失——用户看到的是「我明明画了，怎么没了」。
 */
export function displayToPixel(
  value: DisplayPoint,
  scale: AnnotationScale,
  size: PixelSize,
): PixelPoint {
  const sx = Number.isFinite(scale.sx) && scale.sx > 0 ? scale.sx : 1;
  const sy = Number.isFinite(scale.sy) && scale.sy > 0 ? scale.sy : 1;
  return {
    x: clampPixel(Math.round(value.x * sx), size.width),
    y: clampPixel(Math.round(value.y * sy), size.height),
  };
}

/**
 * 等比适配：把原图尺寸装进浮层给定的可用框，返回底图的 CSS 显示尺寸。
 *
 * 上限 2 倍：太小的图（200px 图标）按原生尺寸显示时笔迹只能画在几个像素上，
 * 稍微放大一点才好落笔；但不设上限的话，一张 80px 的图会被拉到 880 宽，
 * 看着糊、对标注精度也毫无帮助。
 */
export function fitInto(
  pixelW: number,
  pixelH: number,
  boxW: number,
  boxH: number,
): PixelSize {
  if (!isPositiveFinite(pixelW) || !isPositiveFinite(pixelH)) {
    return { width: 0, height: 0 };
  }
  // 可用框非法（窗口被压得很小 / 还没测量）时退回原图尺寸，至少还能画
  const bw = isPositiveFinite(boxW) ? boxW : pixelW;
  const bh = isPositiveFinite(boxH) ? boxH : pixelH;
  const k = Math.min(bw / pixelW, bh / pixelH, 2);
  return {
    width: Math.max(1, Math.round(pixelW * k)),
    height: Math.max(1, Math.round(pixelH * k)),
  };
}
