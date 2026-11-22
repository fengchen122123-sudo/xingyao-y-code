
export const SHORT_LABEL_MAX = 6;

export function shortLabel(value: unknown, max: number = SHORT_LABEL_MAX): string {
  const text = String(value ?? '').trim();
  const limit = Math.max(1, Math.floor(max));
  const chars = Array.from(text);
  if (chars.length <= limit) return text;
  return `${chars.slice(0, limit).join('')}…`;
}
