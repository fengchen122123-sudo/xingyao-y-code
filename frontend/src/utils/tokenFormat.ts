/**
 * Token 数字格式化与状态栏摘要组装：
 * - formatTokenCount：精确数值，千分位分隔（803,834），不再用 K/M 缩写
 * - buildSessionTokenSummary：「本会话 输入 X · 输出 Y · 命中 Z (P%)」，
 *   命中率分母只取已报告缓存明细的轮次 prompt（未报告 ≠ 0）
 */

export function formatTokenCount(n: number): string {
  const v = Number(n) || 0;
  return Math.round(v).toLocaleString('en-US');
}

/** 本会话 token 用量聚合（token_usage 落库；与后端 SessionTokenTotals 对齐） */
export interface SessionTokenTotals {
  session_id: string;
  prompt: number;
  completion: number;
  cached: number;
  cache_prompt: number;
  total: number;
  requests: number;
  last_ts: number | null;
  keepalive_prompt: number;
  keepalive_cached: number;
  keepalive_requests: number;
}

/** 会话命中率：分母只含已报告缓存明细的轮次（未报告 ≠ 0）；无样本返回 null */
export function sessionHitRate(totals: {
  cached: number;
  cache_prompt: number;
}): number | null {
  const denom = Number(totals.cache_prompt) || 0;
  if (denom <= 0) return null;
  return (Number(totals.cached) / denom) * 100;
}

/** 状态行本会话摘要：「本会话 输入 X · 输出 Y · 命中 Z (P%)」；无缓存样本时省略百分比 */
export function buildSessionTokenSummary(totals: SessionTokenTotals): string {
  const rate = sessionHitRate(totals);
  const rateText = rate === null ? '' : ` (${Math.round(rate)}%)`;
  return (
    `本会话 输入 ${formatTokenCount(totals.prompt)} · ` +
    `输出 ${formatTokenCount(totals.completion)} · ` +
    `命中 ${formatTokenCount(totals.cached)}${rateText}`
  );
}
