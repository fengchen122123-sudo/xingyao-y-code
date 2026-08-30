// 助手消息媒体清单（media_v1）类型定义。
// 与后端 _display.media_v1.items 及 gui_bridge._media_items_for_frontend 输出对齐：
// 非 error 项带 url（受控媒体服务的带令牌地址，由桥接层拼好）；
// error 项带 error_code 与 error_label（中文展示文案）。

export interface MediaItem {
  ordinal: number;
  kind: 'image' | 'video' | 'error';
  digest?: string;
  mime?: string;
  ext?: string;
  bytes?: number;
  width?: number | null;
  height?: number | null;
  duration_ms?: number | null;
  caption?: string;
  source_name?: string;
  error_code?: string;
  error_label?: string;
  url?: string;
}

// renderMarkdown 的媒体上下文：ordinal → 清单项映射由数组下标承担
export interface MediaContext {
  items: MediaItem[];
}
