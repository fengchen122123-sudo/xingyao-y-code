export interface AttachmentItem {
  path: string;
  name: string;
  ext: string;
  size: number | null;
  // 拖拽后立即上屏的加载态：路径尚未从 Python 侧取回。
  loading?: boolean;
}
