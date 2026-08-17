/**
 * Token 数字格式化与状态栏摘要组装：
 * - formatTokenCount：精确数值，千分位分隔（803,834），不再用 K/M 缩写
 * - buildTokenSummary：「输入 X · 输出 Y · 命中 Z (P%)」，
 *   命中率分母用输入 tokens（缓存只可能命中输入部分，用 total 会稀释失真）
 */

export function formatTokenCount(n: number): string {
  const v = Number(n) || 0;
  return Math.round(v).toLocaleString('en-US');
}

export interface CliStyleTotals {
  total_prompt: number;
  total_completion: number;
  total_cached: number;
}

export function buildTokenSummary(totals: CliStyleTotals): string {
  const prompt = totals.total_prompt || 0;
  const completion = totals.total_completion || 0;
  const cached = totals.total_cached || 0;
  const hitRate = prompt > 0 ? Math.round((cached / prompt) * 100) : 0;
  return (
    `输入 ${formatTokenCount(prompt)} · 输出 ${formatTokenCount(completion)} · ` +
    `命中 ${formatTokenCount(cached)} (${hitRate}%)`
  );
}
