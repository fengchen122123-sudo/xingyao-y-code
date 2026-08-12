import { ref, onUnmounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import { fileExt } from '../utils/fileIcon';

export interface AttachmentItem {
  path: string;
  name: string;
  ext: string;
  size: number | null;
  // 拖拽后立即上屏的加载态：路径尚未从 Python 侧取回，chips 上显示进度条
  loading?: boolean;
}

// ------------------------------------------------------------------
// 拖拽路径的等待者注册表（模块级，两个输入区共用）：
// Python 侧 dom.on('drop') 收到完整路径后通过 window.__ycode_on_drop_paths 推送，
// 前端按文件名集合匹配等待中的 drop，避免连续拖拽串单。
// ------------------------------------------------------------------
interface DropWaiter {
  names: string[];
  timer: number;
  resolve: (paths: string[]) => void;
}
const dropWaiters: DropWaiter[] = [];
let dropPushBound = false;

function sameNames(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const sa = [...a].sort();
  const sb = [...b].sort();
  return sa.every((v, i) => v === sb[i]);
}

function ensureDropPushBound() {
  if (dropPushBound || typeof window === 'undefined') return;
  dropPushBound = true;
  (window as unknown as { __ycode_on_drop_paths?: (p: unknown) => void }).__ycode_on_drop_paths =
    (payload: unknown) => {
      const files = (payload as { files?: { name?: string; path?: string }[] })?.files || [];
      const valid = files.filter(f => f && f.path);
      if (!valid.length) return;
      const names = valid.map(f => String(f.name || ''));
      let idx = dropWaiters.findIndex(w => sameNames(w.names, names));
      // 名字对不上时（如拖拽被部分过滤）兜底匹配最早的等待者，避免永久悬挂
      if (idx < 0) idx = dropWaiters.length ? 0 : -1;
      if (idx < 0) return;
      const waiter = dropWaiters.splice(idx, 1)[0];
      clearTimeout(waiter.timer);
      waiter.resolve(valid.map(f => String(f.path)));
    };
}

function waitDroppedPaths(names: string[], timeoutMs: number): Promise<string[]> {
  ensureDropPushBound();
  return new Promise(resolve => {
    const waiter: DropWaiter = {
      names,
      resolve,
      timer: window.setTimeout(() => {
        const i = dropWaiters.indexOf(waiter);
        if (i >= 0) dropWaiters.splice(i, 1);
        resolve([]);
      }, timeoutMs),
    };
    dropWaiters.push(waiter);
  });
}

// 输入框附件列表的共享逻辑：选择文件 → 查询文件信息 → 去重维护列表。
// InputArea 与 KimiEmptyState 两处输入区共用。
export function useAttachments() {
  const store = useAppStore();
  const attachments = ref<AttachmentItem[]>([]);
  // 拖拽悬停状态：驱动输入框高亮提示
  const dragging = ref(false);
  let dragDepth = 0;
  let boundEl: HTMLElement | null = null;

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

  // ------------------------------------------------------------------
  // 拖拽文件到输入框：复用 pywebview 的 DOM drop 桥拿到完整本地路径
  // ------------------------------------------------------------------
  function onDragOver(e: DragEvent) {
    if (!e.dataTransfer) return;
    e.dataTransfer.dropEffect = 'copy';
    e.preventDefault();
  }

  function onDragEnter(e: DragEvent) {
    if (!e.dataTransfer?.types?.includes('Files')) return;
    dragDepth += 1;
    dragging.value = true;
  }

  function onDragLeave(e: DragEvent) {
    if (!e.dataTransfer?.types?.includes('Files')) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) dragging.value = false;
  }

  async function onDrop(e: DragEvent) {
    e.preventDefault();
    dragDepth = 0;
    dragging.value = false;
    const files = e.dataTransfer?.files;
    if (!files || !files.length) return;

    // 1) 立即上屏：pywebviewFullPath 只注入到 Python 侧事件，JS 的 File 对象
    //    拿不到路径，但文件名/大小可同步读取——先渲染 loading 占位 chip
    const dropped = Array.from(files).filter(f => f.name);
    if (!dropped.length) return;
    const pendingKeys: string[] = [];
    for (const f of dropped) {
      const key = `__pending__${Date.now()}_${f.name}`;
      pendingKeys.push(key);
      attachments.value.push({
        path: key,
        name: f.name,
        ext: fileExt(f.name),
        size: typeof f.size === 'number' ? f.size : null,
        loading: true,
      });
    }
    const removePending = () => {
      attachments.value = attachments.value.filter(a => !pendingKeys.includes(a.path));
    };

    // 2) 等待 Python 侧 dom.on('drop') 推送完整路径（事件驱动，替代原先的
    //    "Python 存储 + 前端轮询"——那个方案有竞态：首次拖拽读不到、
    //    连续拖拽读到上一次的旧路径）
    let paths = await waitDroppedPaths(dropped.map(f => f.name), 4000);

    // 3) 推送超时：兜底轮询一次旧桥接（兼容尚未升级的 Python 侧），再退化为
    //    text/uri-list 解析
    if (!paths.length) {
      const api = window.pywebview?.api;
      if (api?.report_dropped_files) {
        try {
          const res = await api.report_dropped_files({});
          paths = ((res && res.paths) || []).filter((p: string) => !!p);
        } catch {
          // 桥接失败继续走 uri-list 兜底
        }
      }
    }
    if (!paths.length) {
      const uriList = e.dataTransfer?.getData('text/uri-list') || '';
      paths = uriList
        .split('\n')
        .map(s => s.trim())
        .filter(s => s.startsWith('file:///'))
        .map(s => decodeURIComponent(s.replace('file:///', '')));
    }

    // 4) 替换占位 chip（等待期间用户可能已手动删除某些占位，removePending 只清还在的）
    removePending();
    if (paths.length) {
      await addPaths(paths);
    } else {
      store.showToast('未获取到文件路径，请重试或改用「＋」按钮选择文件', 'warn');
    }
  }

  // 必须用原生 addEventListener 注册 drop：pywebview 只拦截原生监听器的 drop 事件，
  // Vue 模板 @drop 不会触发其完整路径注入逻辑
  function bindDrop(el: HTMLElement) {
    boundEl = el;
    el.addEventListener('dragover', onDragOver);
    el.addEventListener('dragenter', onDragEnter);
    el.addEventListener('dragleave', onDragLeave);
    el.addEventListener('drop', onDrop);
  }

  function unbindDrop() {
    if (!boundEl) return;
    boundEl.removeEventListener('dragover', onDragOver);
    boundEl.removeEventListener('dragenter', onDragEnter);
    boundEl.removeEventListener('dragleave', onDragLeave);
    boundEl.removeEventListener('drop', onDrop);
    boundEl = null;
    dragDepth = 0;
    dragging.value = false;
  }

  onUnmounted(unbindDrop);

  return {
    attachments, pickAttachments, removeAttachment, clearAttachments, handlePaste,
    dragging, bindDrop, unbindDrop,
  };
}
