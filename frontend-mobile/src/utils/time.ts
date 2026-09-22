/** 时间显示工具 */

export function relTime(ts: string): string {
  const d = Date.now() - new Date(ts).getTime();
  if (!Number.isFinite(d) || d < 0) return '';
  if (d < 60_000) return '刚刚';
  if (d < 3600_000) return `${Math.floor(d / 60_000)}分钟前`;
  if (d < 86400_000) return `${Math.floor(d / 3600_000)}小时前`;
  return `${Math.floor(d / 86400_000)}天前`;
}

export function fmtTime(ts: string | number): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
