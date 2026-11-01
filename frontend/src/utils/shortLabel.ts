/**
 * 超长名称的固定长度截断（账号名、厂商名等展示位）：
 * 只保留前 max 个字符，超出部分用省略号替代；完整值由调用方挂在 title 上
 * 作悬停气泡展示。按码点切分，避免把 emoji / 代理对劈成乱码。
 */

export const SHORT_LABEL_MAX = 6;

export function shortLabel(value: unknown, max: number = SHORT_LABEL_MAX): string {
  const text = String(value ?? '').trim();
  const limit = Math.max(1, Math.floor(max));
  const chars = Array.from(text);
  if (chars.length <= limit) return text;
  return `${chars.slice(0, limit).join('')}…`;
}
