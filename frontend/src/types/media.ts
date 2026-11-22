
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

export interface MediaContext {
  items: MediaItem[];
}
