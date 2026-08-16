/**
 * Token 数字格式化与状态栏摘要组装，口径与 CLI（main.py get_status）保持一致：
 * - formatTokenCount 对应 CLI 的 format_num：>=1M 显 x.xM，>=1K 显 x.xK，整数不带小数点
 * - buildTokenSummary 对应 CLI 状态栏「输入 X · 输出 Y · 命中 Z (P%)」，
 *   命中率分母用输入 tokens（缓存只可能命中输入部分，用 total 会稀释失真）
 */

export function formatTokenCount(n: number): string {
  if (n >= 1_000_000) {
    const val = n / 1_000_000;
    return Number.isInteger(val) ? `${val}M` : `${val.toFixed(1)}M`;
  }
  if (n >= 1_000) {
    const val = n / 1_000;
    return Number.isInteger(val) ? `${val}K` : `${val.toFixed(1)}K`;
  }
  return String(Math.round(n));
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
