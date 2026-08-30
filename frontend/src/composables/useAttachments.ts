import { computed, ref, onUnmounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import { fileExt } from '../utils/fileIcon';
import type { AttachmentItem } from '../types/composer';

export type { AttachmentItem } from '../types/composer';

// ------------------------------------------------------------------
// 拖拽路径的等待者注册表（模块级，两个输入区共用）：
// Python 侧 dom.on('drop') 收到完整路径后通过 window.__ycode_on_drop_paths 推送，
// 前端按文件名集合匹配等待中的 drop，避免连续拖拽串单。
// ------------------------------------------------------------------
interface DropWaiter {
  names: string[];
  // M29：该次拖拽的占位 chip key；removeAttachment 删除占位时同步移除，
  // 全部被删除时取消等待者，路径到达后不再复活已删附件
  keys: string[];
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
      // M28 修复：从尾部向前找"最新"的同名等待者（同名文件跨目录连续拖拽时，
      // 路径到达顺序与拖拽顺序一致，最新等待者才是本次拖拽）；
      // 超时的等待者已被其 timer 自行摘除，数组中存在的即未超时
      let idx = -1;
      for (let i = dropWaiters.length - 1; i >= 0; i--) {
        if (sameNames(dropWaiters[i].names, names)) { idx = i; break; }
      }
      // 名字对不上时（如拖拽被部分过滤）仅在唯一等待者时才兜底，
      // 多等待者场景下盲目兜底最早等待者会错配串单
      if (idx < 0 && dropWaiters.length === 1) idx = 0;
      if (idx < 0) return;
      const waiter = dropWaiters.splice(idx, 1)[0];
      clearTimeout(waiter.timer);
      waiter.resolve(valid.map(f => String(f.path)));
    };
}

function waitDroppedPaths(names: string[], timeoutMs: number, keys: string[] = []): Promise<string[]> {
  ensureDropPushBound();
  return new Promise(resolve => {
    const waiter: DropWaiter = {
      names,
      keys: [...keys],
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

// M29：取消包含指定占位 chip 的等待者中已删除的 key；
// 该等待者的占位全部被删除时直接取消等待（resolve([]) 走不到 addPaths）
function cancelWaiterForRemovedChip(path: string) {
  for (let i = dropWaiters.length - 1; i >= 0; i--) {
    const w = dropWaiters[i];
    const ki = w.keys.indexOf(path);
    if (ki < 0) continue;
    w.keys.splice(ki, 1);
    if (!w.keys.length) {
      dropWaiters.splice(i, 1);
      clearTimeout(w.timer);
      w.resolve([]);
    }
    return;
  }
}

// 输入框附件列表的共享逻辑：选择文件 → 查询文件信息 → 去重维护列表。
// InputArea 与 KimiEmptyState 两处输入区共用。
export function useAttachments() {
  const store = useAppStore();
  function currentAttachments(): AttachmentItem[] {
    const runtime = store._getSessionRuntime();
    if (!runtime.composerAttachments) runtime.composerAttachments = [];
    return runtime.composerAttachments;
  }
  const attachments = computed<AttachmentItem[]>({
    get: currentAttachments,
    set: value => { store._getSessionRuntime().composerAttachments = value; },
  });
  // 拖拽悬停状态：驱动输入框高亮提示
  const dragging = ref(false);
  let dragDepth = 0;
  let boundEl: HTMLElement | null = null;

  function baseName(p: string): string {
    const idx = Math.max(p.lastIndexOf('\\'), p.lastIndexOf('/'));
    return idx >= 0 ? p.slice(idx + 1) : p;
  }

  async function pickAttachments() {
    const target = currentAttachments();
    const paths = await store.pickFiles();
    if (!paths.length) return;
    await addPaths(paths, target);
  }

  const UPLOAD_MAX_BYTES = 50 * 1024 * 1024;

  function fileToDataB64(file: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const dataUrl = String(reader.result || '');
        resolve(dataUrl.slice(dataUrl.indexOf(',') + 1));
      };
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }

  // 手机/平板端：把 <input type=file> 选中的文件逐个经 WS RPC 上传到电脑，再加入附件列表
  async function uploadFiles(files: File[]) {
    const target = currentAttachments();
    const paths: string[] = [];
    for (const file of files) {
      if (file.size > UPLOAD_MAX_BYTES) {
        store.notifyChat(`「${file.name}」超过 50MB，无法上传`);
        continue;
      }
      try {
        const b64 = await fileToDataB64(file);
        const path = await store.uploadFile(file.name, b64);
        if (path) paths.push(path);
      } catch {
        store.notifyChat(`「${file.name}」上传失败，请重试`);
      }
    }
    if (paths.length) await addPaths(paths, target);
  }

  async function addPaths(paths: string[], target = currentAttachments()) {
    let infos: AttachmentItem[] = [];
    try {
      infos = await store.getFilesInfo(paths);
    } catch {
      // 后端不可用时退化为仅展示文件名，不影响发送
      infos = [];
    }
    const byPath = new Map(infos.map(i => [i.path, i]));
    for (const p of paths) {
      if (target.some(a => a.path === p)) continue;
      const info = byPath.get(p);
      const name = info?.name || baseName(p);
      target.push({
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
    const target = currentAttachments();
    for (const file of images) {
      const reader = new FileReader();
      reader.onload = async () => {
        const dataUrl = String(reader.result || '');
        if (!dataUrl) return;
        const path = await store.saveClipboardImage(dataUrl);
        if (path) await addPaths([path], target);
      };
      reader.readAsDataURL(file);
    }
  }

  function removeAttachment(path: string) {
    const target = currentAttachments();
    const index = target.findIndex(a => a.path === path);
    if (index >= 0) target.splice(index, 1);
    // M29 修复：删除拖拽占位 chip 时同步取消对应等待者，
    // 否则 4s 等待结束后路径到达会把已删除的附件复活
    cancelWaiterForRemovedChip(path);
  }

  function clearAttachments(target = currentAttachments()) {
    target.splice(0, target.length);
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
    const target = currentAttachments();

    // 1) 立即上屏：pywebviewFullPath 只注入到 Python 侧事件，JS 的 File 对象
    //    拿不到路径，但文件名/大小可同步读取——先渲染 loading 占位 chip
    const dropped = Array.from(files).filter(f => f.name);
    if (!dropped.length) return;
    const pendingKeys: string[] = [];
    for (const f of dropped) {
      const key = `__pending__${Date.now()}_${f.name}`;
      pendingKeys.push(key);
      target.push({
        path: key,
        name: f.name,
        ext: fileExt(f.name),
        size: typeof f.size === 'number' ? f.size : null,
        loading: true,
      });
    }
    const removePending = () => {
      for (let i = target.length - 1; i >= 0; i--) {
        if (pendingKeys.includes(target[i].path)) target.splice(i, 1);
      }
    };

    // 2) 等待 Python 侧 dom.on('drop') 推送完整路径（事件驱动，替代原先的
    //    "Python 存储 + 前端轮询"——那个方案有竞态：首次拖拽读不到、
    //    连续拖拽读到上一次的旧路径）
    let paths = await waitDroppedPaths(dropped.map(f => f.name), 4000, pendingKeys);

    // M29 修复：等待期间用户可能已删除全部占位 chip（等待者被 removeAttachment
    // 取消，或手动逐个删掉），此时任何通道解析出的路径都不应再复活附件
    if (!target.some(a => pendingKeys.includes(a.path))) {
      removePending();
      return;
    }

    // 3) 推送超时：兜底轮询一次旧桥接（兼容尚未升级的 Python 侧），再退化为
    //    text/uri-list 解析
    // 旧桥接只有一个全局槽位，无法区分并发拖拽。仍有其他 waiter 时禁止消费，
    // 否则先超时的会话可能拿走另一会话的路径并写入错误草稿。
    if (!paths.length && dropWaiters.length === 0) {
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
    // M29：被删除占位对应的文件不复活——按文件名从解析结果中剔除
    const removedNames = new Set(
      pendingKeys
        .filter(k => !target.some(a => a.path === k))
        .map(k => k.replace(/^__pending__\d+_/, '')),
    );
    removePending();
    paths = paths.filter(p => !removedNames.has(baseName(p)));
    if (paths.length) {
      await addPaths(paths, target);
    } else if (!removedNames.size) {
      store.notifyChat('未获取到文件路径，请重试或改用「＋」按钮选择文件');
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
    dragging, bindDrop, unbindDrop, addPaths, uploadFiles,
  };
}
