import { ref } from 'vue';
import { useAppStore } from '../stores/appStore';
import { fileExt } from '../utils/fileIcon';

export interface AttachmentItem {
  path: string;
  name: string;
  ext: string;
  size: number | null;
}

// 输入框附件列表的共享逻辑：选择文件 → 查询文件信息 → 去重维护列表。
// InputArea 与 KimiEmptyState 两处输入区共用。
export function useAttachments() {
  const store = useAppStore();
  const attachments = ref<AttachmentItem[]>([]);

  function baseName(p: string): string {
    const idx = Math.max(p.lastIndexOf('\\'), p.lastIndexOf('/'));
    return idx >= 0 ? p.slice(idx + 1) : p;
  }

  async function pickAttachments() {
    const paths = await store.pickFiles();
    if (!paths.length) return;
    await addPaths(paths);
  }

  async function addPaths(paths: string[]) {
    let infos: AttachmentItem[] = [];
    try {
      infos = await store.getFilesInfo(paths);
    } catch {
      // 后端不可用时退化为仅展示文件名，不影响发送
      infos = [];
    }
    const byPath = new Map(infos.map(i => [i.path, i]));
    for (const p of paths) {
      if (attachments.value.some(a => a.path === p)) continue;
      const info = byPath.get(p);
      const name = info?.name || baseName(p);
      attachments.value.push({
        path: p,
        name,
        ext: info?.ext ?? fileExt(name),
        size: info?.size ?? null,
      });
    }
  }

  // 输入框粘贴事件：剪贴板中的图片落盘为临时文件后加入附件列表；
  // 没有图片时不拦截，走默认文本粘贴
  function handlePaste(e: ClipboardEvent) {
    const items = e.clipboardData?.items;
    if (!items) return;
    const images: File[] = [];
    for (const item of Array.from(items)) {
      if (item.kind === 'file' && item.type.startsWith('image/')) {
        const f = item.getAsFile();
        if (f) images.push(f);
      }
    }
    if (!images.length) return;
    e.preventDefault();
    for (const file of images) {
      const reader = new FileReader();
      reader.onload = async () => {
        const dataUrl = String(reader.result || '');
        if (!dataUrl) return;
        const path = await store.saveClipboardImage(dataUrl);
        if (path) await addPaths([path]);
      };
      reader.readAsDataURL(file);
    }
  }

  function removeAttachment(path: string) {
    attachments.value = attachments.value.filter(a => a.path !== path);
  }

  function clearAttachments() {
    attachments.value = [];
  }

  return { attachments, pickAttachments, removeAttachment, clearAttachments, handlePaste };
}
