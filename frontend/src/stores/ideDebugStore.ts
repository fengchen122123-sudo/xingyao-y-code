import { defineStore } from 'pinia';

import { useIdeStore } from './ideStore';
import { emitDapOutput } from '../utils/dapBus';

export type DebugState = 'idle' | 'starting' | 'running' | 'paused' | 'stopped';

export interface DapStackFrame {
  id: number;
  name: string;
  path: string;
  line: number;
  column: number;
}

export interface DapVariable {
  name: string;
  value: string;
  type: string;
  variablesReference: number;
}

export interface DapScopeItem {
  name: string;
  variablesReference: number;
  expensive: boolean;
}

export interface DapWatchResult {
  expression: string;
  result: string;
  type: string;
  error: string;
}

export interface DapEventPayload {
  session_id?: string;
  state?: string;
  msg?: string;
  reason?: string;
  thread_id?: number;
  frame_id?: number;
  path?: string;
  line?: number;
  exit_code?: number;
  verified?: boolean;
  category?: string;
  text?: string;
}

/** 调试桥句柄：与 appStore._ptyApi 同口径，桥未就绪时返回 null。 */
function debugApi(): any {
  return (window as any).pywebview?.api ?? null;
}

function breakpointKey(path: string, line: number): string {
  return `${path}:${line}`;
}

export const useIdeDebugStore = defineStore('ideDebug', {
  state: () => ({
    sessionId: '',
    adapter: '',
    debugState: 'idle' as DebugState,
    stopReason: '',
    threadId: 0,
    stopPath: '',
    stopLine: 0,
    frames: [] as DapStackFrame[],
    selectedFrameId: 0,
    scopes: [] as DapScopeItem[],
    variables: [] as DapVariable[],
    watchExpressions: [] as string[],
    watchResults: [] as DapWatchResult[],
    breakpointStates: {} as Record<string, boolean>,
    lastError: '',
  }),

  getters: {
    active(state): boolean {
      return state.sessionId !== '' && state.debugState !== 'idle' && state.debugState !== 'stopped';
    },
    paused(state): boolean {
      return state.debugState === 'paused';
    },
    /** 断点按文件分组：启动调试时一次性下发（后端 DAP 用 setBreakpoints 逐文件设置）。 */
    breakpointsByPath(): { path: string; lines: number[] }[] {
      const ide = useIdeStore();
      const groups = new Map<string, number[]>();
      for (const key of ide.breakpoints) {
        const index = key.lastIndexOf(':');
        if (index <= 0) continue;
        const path = key.slice(0, index);
        const line = Number(key.slice(index + 1));
        if (!path || !Number.isFinite(line)) continue;
        groups.set(path, [...(groups.get(path) ?? []), line]);
      }
      return [...groups.entries()].map(([path, lines]) => ({ path, lines: lines.sort((a, b) => a - b) }));
    },
  },

  actions: {
    /** DAP 事件统一归约入口：由 appStore.handleBridgeEvent 分流调用。 */
    reduceDapEvent(type: string, data: DapEventPayload = {}) {
      if (type === 'dap_output') {
        // 高频原始输出：走旁路总线，绝不进响应式状态（否则每行输出都会触发组件更新）
        emitDapOutput({
          session_id: String(data.session_id ?? ''),
          category: String(data.category ?? 'stdout'),
          text: String(data.text ?? ''),
        });
        return;
      }
      if (type === 'dap_status') {
        const state = String(data.state ?? '');
        if (state) this.debugState = state as DebugState;
        if (data.msg) this.lastError = String(data.msg);
        return;
      }
      if (type === 'dap_stopped') {
        this.debugState = 'paused';
        this.stopReason = String(data.reason ?? 'breakpoint');
        this.threadId = Number(data.thread_id ?? 0);
        this.selectedFrameId = Number(data.frame_id ?? 0);
        this.stopPath = String(data.path ?? '');
        this.stopLine = Number(data.line ?? 0);
        this._clearPauseDetail();
        return;
      }
      if (type === 'dap_continued') {
        this.debugState = 'running';
        this.selectedFrameId = 0;
        this._clearPauseDetail();
        return;
      }
      if (type === 'dap_exited' || type === 'dap_terminated') {
        // 程序自然结束：会话 id 一并清掉，界面据此放行「再次开始调试」
        // （后端 _emit_dap_event 收到 terminated 也会丢弃会话引用，两边对齐）
        this.debugState = 'stopped';
        this.sessionId = '';
        this.selectedFrameId = 0;
        this._clearPauseDetail();
        return;
      }
      if (type === 'dap_breakpoint_changed') {
        const path = String(data.path ?? '');
        const line = Number(data.line ?? 0);
        if (path && line > 0) this.breakpointStates[breakpointKey(path, line)] = Boolean(data.verified);
      }
      // 其余 dap_*（含未知事件）静默忽略：桥可能先于前端加新事件，不能让未知类型抛错
    },

    /**
     * 清空「暂停现场」（栈帧 / 作用域 / 变量）。
     *
     * 幂等要求：只在非空时换新数组，重复到达的同一条 dap_stopped / dap_continued /
     * dap_terminated 不产生新的数组引用——否则每次重复事件都换一次引用，白触发
     * 一批依赖这些数组的组件更新（事件分流必须幂等）。
     */
    _clearPauseDetail() {
      if (this.frames.length) this.frames = [];
      if (this.scopes.length) this.scopes = [];
      if (this.variables.length) this.variables = [];
    },

    reset() {
      this.sessionId = '';
      this.adapter = '';
      this.debugState = 'idle';
      this.stopReason = '';
      this.threadId = 0;
      this.stopPath = '';
      this.stopLine = 0;
      this.frames = [];
      this.selectedFrameId = 0;
      this.scopes = [];
      this.variables = [];
      this.watchResults = [];
      this.breakpointStates = {};
      this.lastError = '';
    },

    async runFile(path: string, extraArgs = '') {
      const api = debugApi();
      if (!api?.ide_run_file) return { ok: false, msg: '后端桥未就绪' };
      const res = await api.ide_run_file(path, extraArgs);
      if (!res?.ok) this.lastError = String(res?.msg ?? '运行失败');
      return res ?? { ok: false };
    },

    async startDebug(path: string, program = '', args: string[] = []) {
      const api = debugApi();
      if (!api?.ide_debug_start) return { ok: false, msg: '后端桥未就绪' };
      this.debugState = 'starting';
      this.lastError = '';
      const res = await api.ide_debug_start(path, this.breakpointsByPath, program, args);
      if (res?.ok) {
        this.sessionId = String(res.session_id ?? '');
        this.adapter = String(res.adapter ?? '');
        this.debugState = 'running';
      } else {
        this.debugState = 'idle';
        this.lastError = String(res?.msg ?? '启动调试失败');
      }
      return res ?? { ok: false };
    },

    async stopDebug() {
      const api = debugApi();
      if (api?.ide_debug_stop) await api.ide_debug_stop();
      this.reset();
      this.debugState = 'stopped';
    },

    async sendCommand(command: string) {
      const api = debugApi();
      if (!api?.ide_debug_command) return { ok: false, msg: '后端桥未就绪' };
      const res = await api.ide_debug_command(command);
      if (!res?.ok) this.lastError = String(res?.msg ?? '调试命令失败');
      return res ?? { ok: false };
    },

    async setBreakpointsFor(path: string) {
      const api = debugApi();
      if (!api?.ide_debug_set_breakpoints) return { ok: false };
      const ide = useIdeStore();
      const lines = ide.breakpointsFor(path);
      const res = await api.ide_debug_set_breakpoints(path, lines);
      for (const item of res?.verified ?? []) {
        if (item?.line) this.breakpointStates[breakpointKey(path, Number(item.line))] = Boolean(item.verified);
      }
      return res ?? { ok: false };
    },

    /** 断点切换：先落前端状态（无会话时也能设），有会话时同步给适配器。 */
    async toggleBreakpoint(path: string, line: number) {
      const ide = useIdeStore();
      ide.toggleBreakpoint(path, line);
      const key = breakpointKey(path, line);
      if (!ide.breakpoints.has(key)) {
        delete this.breakpointStates[key];
      }
      if (this.sessionId) await this.setBreakpointsFor(path);
    },

    async refreshFrames() {
      const api = debugApi();
      if (!api?.ide_debug_stack_trace || !this.threadId) return [];
      const res = await api.ide_debug_stack_trace(this.threadId, 20);
      this.frames = (res?.frames ?? []) as DapStackFrame[];
      return this.frames;
    },

    async selectFrame(frameId: number) {
      this.selectedFrameId = frameId;
      await this.loadScopes(frameId);
    },

    async loadScopes(frameId: number) {
      const api = debugApi();
      if (!api?.ide_debug_scopes) return [];
      const res = await api.ide_debug_scopes(frameId);
      this.scopes = (res?.scopes ?? []) as DapScopeItem[];
      const first = this.scopes[0];
      this.variables = first ? await this.expandVariables(first.variablesReference) : [];
      return this.scopes;
    },

    async expandVariables(variablesReference: number) {
      const api = debugApi();
      if (!api?.ide_debug_variables) return [];
      const res = await api.ide_debug_variables(variablesReference);
      this.variables = (res?.variables ?? []) as DapVariable[];
      return this.variables;
    },

    async evaluate(expression: string, frameId = 0) {
      const api = debugApi();
      if (!api?.ide_debug_evaluate) return { ok: false, msg: '后端桥未就绪' };
      const target = frameId || this.selectedFrameId;
      const res = await api.ide_debug_evaluate(expression, target);
      return res ?? { ok: false };
    },

    async addWatch(expression: string) {
      const text = expression.trim();
      if (!text) return;
      if (!this.watchExpressions.includes(text)) this.watchExpressions.push(text);
      const res = await this.evaluate(text);
      this.watchResults = [
        ...this.watchResults.filter((item) => item.expression !== text),
        {
          expression: text,
          result: String(res?.result ?? ''),
          type: String(res?.type ?? ''),
          error: res?.ok ? '' : String(res?.msg ?? '求值失败'),
        },
      ];
    },

    removeWatch(expression: string) {
      this.watchExpressions = this.watchExpressions.filter((item) => item !== expression);
      this.watchResults = this.watchResults.filter((item) => item.expression !== expression);
    },

    async refreshWatch() {
      const expressions = [...this.watchExpressions];
      this.watchResults = [];
      for (const expression of expressions) await this.addWatch(expression);
    },
  },
});
