export function extractExportSuccessPath(content?: string | null): string {
  const s = String(content ?? '').trim();
  if (!s) return '';
  const match = s.match(/^导出成功[:：]\s*(.+)$/);
  return match ? match[1].trim() : '';
}
