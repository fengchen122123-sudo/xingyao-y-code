import type { GitDiffDto } from '../types/pywebview.d';

/**
 * 将相对文件路径拆分为文件名与父目录前缀，供列表项分层展示。
 * 兼容正斜杠与反斜杠路径，无目录时 dir 为空串。
 */
export function splitGitPath(rawPath: string): { name: string; dir: string } {
  const path = String(rawPath ?? '');
  const idx = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
  if (idx < 0) return { name: path, dir: '' };
  return {
    name: path.slice(idx + 1),
    dir: path.slice(0, idx + 1),
  };
}

export function formatRelativeCommitTime(
  unixSeconds: number,
  nowSeconds: number = Math.floor(Date.now() / 1000),
): string {
  if (!unixSeconds || !Number.isFinite(unixSeconds) || unixSeconds <= 0) return '';
  const delta = Math.max(0, Math.floor(nowSeconds - unixSeconds));
  if (delta < 60) return '刚刚';
  if (delta < 3600) return `${Math.floor(delta / 60)}分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}小时前`;
  if (delta < 86400 * 30) return `${Math.floor(delta / 86400)}天前`;
  const d = new Date(unixSeconds * 1000);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

export function countDiffStats(diff: GitDiffDto | null): { added: number; deleted: number } {
  if (!diff || diff.is_binary || !Array.isArray(diff.hunks)) {
    return { added: 0, deleted: 0 };
  }
  let added = 0;
  let deleted = 0;
  for (const hunk of diff.hunks) {
    for (const line of hunk.lines) {
      if (line.startsWith('+') && !line.startsWith('+++')) added += 1;
      else if (line.startsWith('-') && !line.startsWith('---')) deleted += 1;
    }
  }
  return { added, deleted };
}
