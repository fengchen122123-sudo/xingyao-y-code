/** Markdown 渲染：GFM（表格/任务列表/删除线）+ 软换行，输出统一过 DOMPurify 白名单。 */
import { marked } from "marked";
import DOMPurify from "dompurify";

marked.setOptions({ gfm: true, breaks: true });

/** 保留给需要纯文本转义的调用方。 */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function renderMd(value: string): string {
  const html = marked.parse(String(value || ""), { async: false }) as string;
  // 模型输出可能夹带 HTML 片段，统一过一遍白名单，只保留排版标签。
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
}
