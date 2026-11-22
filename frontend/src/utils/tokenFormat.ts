
export function formatTokenCount(n: number): string {
  const v = Number(n) || 0;
  return Math.round(v).toLocaleString('en-US');
}

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

export function sessionHitRate(totals: {
  cached: number;
  cache_prompt: number;
}): number | null {
  const denom = Number(totals.cache_prompt) || 0;
  if (denom <= 0) return null;
  return (Number(totals.cached) / denom) * 100;
}

export function buildSessionTokenSummary(totals: SessionTokenTotals): string {
  const rate = sessionHitRate(totals);
  const rateText = rate === null ? '' : ` (${Math.round(rate)}%)`;
  return (
    `本会话 输入 ${formatTokenCount(totals.prompt)} · ` +
    `输出 ${formatTokenCount(totals.completion)} · ` +
    `命中 ${formatTokenCount(totals.cached)}${rateText}`
  );
}
