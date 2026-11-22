export interface AttachmentItem {
  path: string;
  name: string;
  ext: string;
  size: number | null;
  loading?: boolean;
  thumb?: string;
}
