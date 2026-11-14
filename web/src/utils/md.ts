/** 先转义再渲染。模型输出里的 HTML 标签只能当文本显示。 */

export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function renderMd(value: string): string {
  const escaped = escapeHtml(String(value || ""));
  return escaped
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\n/g, "<br>");
}
