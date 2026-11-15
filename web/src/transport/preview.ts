import { apiFetch } from "./api";

/** 管理短期预览对象 URL；旧请求即使晚返回也不能覆盖新画面或泄漏 Blob。 */
export class PreviewLoader {
  url = "";
  private generation = 0;
  private disposed = false;

  constructor(_server: string, _credential: () => string) {}

  /** server/credential 参数保留给调用点；实际令牌和刷新统一由 apiFetch 管理。 */
  async load(path: string): Promise<string> {
    if (this.disposed) return "";
    const generation = ++this.generation;
    this.revoke();
    if (!path || this.disposed) return "";
    const response = await apiFetch(path);
    if (!response.ok) throw new Error(`预览加载失败（${response.status}）`);
    const url = URL.createObjectURL(await response.blob());
    if (this.disposed || generation !== this.generation) {
      URL.revokeObjectURL(url);
      return "";
    }
    this.url = url;
    return url;
  }

  dispose(): void {
    this.disposed = true;
    this.generation += 1;
    this.revoke();
  }

  private revoke(): void {
    if (this.url) URL.revokeObjectURL(this.url);
    this.url = "";
  }
}
