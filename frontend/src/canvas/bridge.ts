// 画布桥封装：组件/store 一律经这里访问 window.pywebview.api.canvas_*，
// 不直接触碰 window.pywebview（设计 §3.2「桥层」）。
//
// 这一层的职责只有三件：
// 1) 桥就绪等待（pywebview 两步注入竞态：api.js 先注入空对象，finish.js 之后
//    才把方法填进去并触发 pywebviewready —— 与 BrowserPane.vue 同一套双通道
//    等待口径：事件 + 100ms 兜底轮询 + 宽限期，超时按「无宿主」处理）；
// 2) 返回值归一（后端约定 {ok, ...}，异常/缺方法一律收敛成 {ok:false, msg}，
//    绝不让调用方拿到 reject —— 画布页面是常驻页签，一次抛错不该炸掉整块 UI）；
// 3) 全量签名一次定型：生成/音频类方法在 M4/M6 实装，本任务先抛
//    `canvas bridge not ready`，签名与参数类型在此固定，后续只填方法体。
import type { CanvasProject } from './types';

export interface BridgeOk { ok: boolean; msg?: string }
export interface ProjectMeta { id: string; title: string; updatedAt: number; schema_version: number; supported: boolean }
export interface SaveMediaResult extends BridgeOk {
  media_id: string;
  path: string;
  url: string;
  width: number | null;
  height: number | null;
  bytes: number;
}
export interface JobAck extends BridgeOk { pending?: true; job_id?: string }

// ── 参数类型（M4-T02 / M6-T03 使用，本任务只定型不实装）──
export interface CanvasGenerateImageParams {
  project_id: string;
  source_node_id: string;
  prompt: string;
  size?: string;
  model?: 'qwen-image-2.0' | 'wan2.7-image';
  n?: number;
}
export interface CanvasEditImageParams {
  project_id: string;
  source_node_id: string;
  prompt: string;
  image_media_id: string;
  mask_media_id?: string;
  size?: string;
  model?: 'qwen-image-2.0' | 'wan2.7-image';
}
export interface CanvasGenerateVideoParams {
  project_id: string;
  source_node_id: string;
  prompt: string;
  first_frame_media_id?: string;
  duration?: number;
  resolution?: '480P' | '720P' | '1080P';
}
export type CanvasAudioOperation = 'split-av' | 'trim' | 'tempo' | 'silence-split';
export interface CanvasAudioParams {
  start_ms?: number;
  end_ms?: number;
  rate?: number;
  silence_noise_db?: number;
  silence_min_ms?: number;
}

const BRIDGE_GRACE_MS = 5000;
const BRIDGE_POLL_MS = 100;
const NOT_READY_MSG = 'canvas bridge not ready';

type BridgeApi = Record<string, unknown>;

function readApi(): BridgeApi | null {
  try {
    return ((window as any).pywebview?.api ?? null) as BridgeApi | null;
  } catch {
    return null;
  }
}

/**
 * 桥是否已经「填好方法」。
 *
 * 不能只判 `window.pywebview.api` 存在：启动早期它是个空对象（api.js 先注入），
 * 此时任何方法调用都会抛 TypeError。用几个与本模块无关的既有方法当作
 * 「宿主已就绪」的探针——任一存在即认为 finish.js 已完成注入。
 */
function bridgeLooksReady(api: BridgeApi | null): boolean {
  if (!api) return false;
  try {
    return typeof api.canvas_list_projects === 'function'
      || typeof api.get_initial_state === 'function'
      || typeof api.browser_pane_open === 'function';
  } catch {
    return false;
  }
}

let pendingWait: Promise<BridgeApi | null> | null = null;

/**
 * 等待 pywebview 桥就绪（双通道：pywebviewready 事件 + 100ms 兜底轮询）。
 * 宽限期内始终无桥（纯浏览器调试、单测环境）返回 null，调用方按「无宿主」处理。
 * 同一时刻只会有一次等待，多个并发调用共享同一个 Promise。
 */
export function waitForCanvasBridge(timeoutMs = BRIDGE_GRACE_MS): Promise<BridgeApi | null> {
  const api = readApi();
  if (bridgeLooksReady(api)) return Promise.resolve(api);
  if (pendingWait) return pendingWait;
  if (typeof window === 'undefined') return Promise.resolve(null);

  let settled = false;
  let pollTimer: ReturnType<typeof setInterval> | undefined;
  let graceTimer: ReturnType<typeof setTimeout> | undefined;
  let resolveWait: (found: BridgeApi | null) => void = () => {};

  const finish = (found: BridgeApi | null) => {
    if (settled) return;
    settled = true;
    if (pollTimer !== undefined) { clearInterval(pollTimer); pollTimer = undefined; }
    if (graceTimer !== undefined) { clearTimeout(graceTimer); graceTimer = undefined; }
    window.removeEventListener('pywebviewready', onReady);
    resolveWait(found);
  };
  const check = () => {
    const cur = readApi();
    if (bridgeLooksReady(cur)) finish(cur);
  };
  function onReady(): void { check(); }

  const promise = new Promise<BridgeApi | null>((resolve) => { resolveWait = resolve; });
  pendingWait = promise;
  // 结算后清掉共享句柄：超时（无宿主）之后再调用应当重新等一次，而不是永远拿旧结果
  void promise.then(() => { if (pendingWait === promise) pendingWait = null; });

  // 先同步探测一次：调用方可能本就晚于桥就绪（事件已错过，不必再等）
  check();
  if (!settled) {
    // 双通道：事件可能先于监听注册触发，故再加 100ms 兜底轮询
    window.addEventListener('pywebviewready', onReady);
    pollTimer = setInterval(check, BRIDGE_POLL_MS);
    graceTimer = setTimeout(() => finish(bridgeLooksReady(readApi()) ? readApi() : null), timeoutMs);
  }
  return promise;
}

interface InvokeResult {
  ok: boolean;
  msg: string;
  raw: Record<string, unknown>;
}

function errText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  const text = String(err ?? '');
  return text && text !== '[object Object]' ? text : '画布桥调用失败';
}

/** 统一调用入口：缺方法 / 抛异常 / 返回非对象全部收敛成 ok:false，不 reject */
async function invoke(method: string, ...args: unknown[]): Promise<InvokeResult> {
  const api = await waitForCanvasBridge();
  if (!api) return { ok: false, msg: NOT_READY_MSG, raw: {} };
  const fn = api[method];
  if (typeof fn !== 'function') return { ok: false, msg: NOT_READY_MSG, raw: {} };
  try {
    const raw = await (fn as (...a: unknown[]) => unknown).apply(api, args);
    if (!raw || typeof raw !== 'object') return { ok: false, msg: '画布桥返回非法结果', raw: {} };
    const obj = raw as Record<string, unknown>;
    return {
      ok: obj.ok === true,
      msg: typeof obj.msg === 'string' ? obj.msg : '',
      raw: obj,
    };
  } catch (err) {
    return { ok: false, msg: errText(err), raw: {} };
  }
}

function numOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function strOr(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function normalizeProjectMeta(item: unknown): ProjectMeta | null {
  if (!item || typeof item !== 'object') return null;
  const obj = item as Record<string, unknown>;
  const id = strOr(obj.id);
  if (!id) return null;
  return {
    id,
    title: strOr(obj.title, id),
    updatedAt: numOr(obj.updatedAt, 0),
    schema_version: numOr(obj.schema_version, 0),
    // 后端已按 schema_version 归一 supported；这里再兜一层，防止旧桥漏字段
    supported: obj.supported === true || numOr(obj.schema_version, 0) === 2,
  };
}

function notReadyThrow(method: string): never {
  throw new Error(`${NOT_READY_MSG}: ${method} 将在后续里程碑实装`);
}

export const canvasBridge = {
  /** 列出工作目录下所有画布项目（v1 旧项目照列，supported=false 前端置灰） */
  async listProjects(): Promise<{ ok: boolean; projects: ProjectMeta[] }> {
    const res = await invoke('canvas_list_projects');
    if (!res.ok) return { ok: false, projects: [] };
    const list = Array.isArray(res.raw.projects) ? res.raw.projects : [];
    const projects: ProjectMeta[] = [];
    for (const item of list) {
      const meta = normalizeProjectMeta(item);
      if (meta) projects.push(meta);
    }
    return { ok: true, projects };
  },

  /** 加载单个项目；schema_version !== 2 时后端回 msg「旧版数据，不支持」 */
  async loadProject(projectId: string): Promise<{ ok: boolean; project?: CanvasProject; msg?: string }> {
    const res = await invoke('canvas_load_project', projectId);
    if (!res.ok) return { ok: false, msg: res.msg };
    const project = res.raw.project;
    if (!project || typeof project !== 'object') return { ok: false, msg: '画布数据缺失' };
    return { ok: true, project: project as CanvasProject };
  },

  /** 保存项目（后端原子写，updatedAt 以服务端回执为准） */
  async saveProject(project: CanvasProject): Promise<{ ok: boolean; updatedAt?: number; msg?: string }> {
    const res = await invoke('canvas_save_project', project);
    if (!res.ok) return { ok: false, msg: res.msg };
    const updatedAt = res.raw.updatedAt;
    return typeof updatedAt === 'number' && Number.isFinite(updatedAt)
      ? { ok: true, updatedAt }
      : { ok: true };
  },

  async deleteProject(projectId: string): Promise<BridgeOk> {
    const res = await invoke('canvas_delete_project', projectId);
    return res.ok ? { ok: true } : { ok: false, msg: res.msg };
  },

  /** dataURL 或纯 base64 落盘 → 归档 → 返回 media id 与预览 URL */
  async saveMedia(dataUrlOrBase64: string, hintName = ''): Promise<SaveMediaResult> {
    const res = await invoke('canvas_save_media', dataUrlOrBase64, hintName);
    if (!res.ok) {
      return {
        ok: false,
        msg: res.msg,
        media_id: '',
        path: '',
        url: '',
        width: null,
        height: null,
        bytes: 0,
      };
    }
    const width = typeof res.raw.width === 'number' ? res.raw.width : null;
    const height = typeof res.raw.height === 'number' ? res.raw.height : null;
    return {
      ok: true,
      media_id: strOr(res.raw.media_id),
      path: strOr(res.raw.path),
      url: strOr(res.raw.url),
      width,
      height,
      bytes: numOr(res.raw.bytes, 0),
    };
  },

  /** media id → media_server 预览 URL */
  async resolveMedia(mediaId: string): Promise<{ ok: boolean; url?: string; msg?: string }> {
    const res = await invoke('canvas_resolve_media', mediaId);
    if (!res.ok) return { ok: false, msg: res.msg };
    return { ok: true, url: strOr(res.raw.url) };
  },

  // ── 以下 M4/M6 实装，签名在此定型 ──
  async generateImage(_params: CanvasGenerateImageParams): Promise<JobAck> {
    return notReadyThrow('generateImage');
  },

  async editImage(_params: CanvasEditImageParams): Promise<JobAck> {
    return notReadyThrow('editImage');
  },

  async generateVideo(_params: CanvasGenerateVideoParams): Promise<JobAck> {
    return notReadyThrow('generateVideo');
  },

  async cancelJob(_jobId: string): Promise<BridgeOk> {
    return notReadyThrow('cancelJob');
  },

  async audioProcess(
    _operation: CanvasAudioOperation,
    _mediaId: string,
    _params: CanvasAudioParams,
  ): Promise<JobAck> {
    return notReadyThrow('audioProcess');
  },
};
