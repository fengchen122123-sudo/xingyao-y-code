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

/**
 * 变量树的一层：`reference` 是该层的 DAP variablesReference，`name` 是面包屑上显示的名字
 * （作用域名 / 变量名）。索引 0 恒为作用域根，`length > 1` 表示已展开到子层级。
 */
export interface DapVarPathNode {
  reference: number;
  name: string;
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
    /** 当前展示的变量层级（随 varPath 一起变），保留扁平字段供面板直接渲染 */
    variables: [] as DapVariable[],
    /** 当前正在看哪个作用域（该作用域的 variablesReference；0 = 无） */
    activeScopeRef: 0,
    /** 当前层级路径：`varPath[0]` 是作用域，往后每展开一层追加一项 */
    varPath: [] as DapVarPathNode[],
    /**
     * 已加载层级的缓存（键为 variablesReference）。
     *
     * 只用于「暂停期间」在层与层之间来回走：每次暂停现场清空时一并丢弃，
     * 所以不会拿上一次暂停的旧值糊弄用户。
     */
    varCache: {} as Record<number, DapVariable[]>,
    /**
     * 已经结束的会话 id（S3-T10 评审）。
     *
     * `ide_debug_stop` 只丢引用、适配器最坏 ≈10s 后才死，这段时间里旧会话仍会推
     * `dap_continued` / `dap_exited` / `dap_terminated`。记下「结过账」的 id，
     * 迟到事件才能被丢掉，而不是把新会话刷回 running / 清空 sessionId。
     */
    staleSessionIds: [] as string[],
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
    /**
     * 事件是否属于当前会话（S3-T10 评审）。
     *
     * 后端 `_emit_dap_event` 已按同一口径丢残留事件，这里再做一层：桥的投递是异步的
     * （入队 → 派发线程），事件到达时前端可能已经换了会话，只靠后端把关不够。
     *
     * 四段，顺序有讲究：
     * 1. 事件不带 `session_id`（老桥 / 手工构造）→ 放行，不改变既有语义；
     * 2. 与当前会话 id 相同 → 放行；
     * 3. 已在 `staleSessionIds` 里（用户停过 / 结束过）→ **丢弃**：这是「停止后又被
     *    旧适配器的 dap_continued 刷回 running」那条 bug 的堵点；
     * 4. 当前会话 id 还是空的 → 放行：`startDebug` 的会话 id 来自 `ide_debug_start`
     *    的**返回**，而适配器事件在启动窗口里就可能先到（后端 `dap_status` 先于
     *    响应返回），严格比对会把合法事件一起丢掉；反之当前会话 id 已知时，外来 id
     *    一律丢弃（重启后旧会话的 exited/terminated 就是这么被挡掉的）。
     */
    _eventBelongsToCurrentSession(data: DapEventPayload): boolean {
      const incoming = String(data.session_id ?? '');
      if (!incoming) return true;
      if (incoming === this.sessionId) return true;
      if (this.staleSessionIds.includes(incoming)) return false;
      return this.sessionId === '';
    },

    /** 把当前会话「结账」：id 记进 staleSessionIds 并清空当前会话 id（幂等）。 */
    _forgetCurrentSession() {
      const id = this.sessionId;
      if (id && !this.staleSessionIds.includes(id)) this.staleSessionIds.push(id);
      this.sessionId = '';
    },

    /** 记下新会话 id，并把它替换掉的上一个会话结账（重启调试时旧 id 必须立即失效）。 */
    _rememberSession(id: string) {
      const next = String(id ?? '');
      if (next !== this.sessionId) this._forgetCurrentSession();
      this.sessionId = next;
    },

    /** DAP 事件统一归约入口：由 appStore.handleBridgeEvent 分流调用。 */
    reduceDapEvent(type: string, data: DapEventPayload = {}) {
      // dap_status 例外：停止与启动失败就是靠 `session_id: ''` 的 dap_status 送到
      // 前端的，按会话 id 比对会把它们一起丢掉
      if (type !== 'dap_status' && !this._eventBelongsToCurrentSession(data)) return;
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
        // 通过 _forgetCurrentSession 清：这个 id 要进 staleSessionIds，旧适配器随后
        // 推来的 exited/terminated（同一会话，最坏 ≈10s）才不会把新会话当成已结束
        this._forgetCurrentSession();
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
      // 层级路径与层级缓存同属「暂停现场」：不清就会把上一次暂停的变量树留给下一次
      if (this.varPath.length) this.varPath = [];
      if (this.activeScopeRef) this.activeScopeRef = 0;
      if (Object.keys(this.varCache).length) this.varCache = {};
    },

    reset() {
      // 会话 id 走 _forgetCurrentSession：先把它记进 staleSessionIds 再清空，
      // 否则「停止后旧适配器迟到的事件」会拿到一个空 sessionId 的空档挤进来
      this._forgetCurrentSession();
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
      this.varPath = [];
      this.activeScopeRef = 0;
      this.varCache = {};
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
        // 会话 id 从启动响应来：走 _rememberSession 顺带把上一个会话结账
        this._rememberSession(String(res.session_id ?? ''));
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

    async sendCommand(command: string, threadId = 0) {
      const api = debugApi();
      if (!api?.ide_debug_command) return { ok: false, msg: '后端桥未就绪' };
      // 只在明确拿到线程号时才带第二个参数：不改变既有单参调用口径（回退逻辑在后端）
      const res = threadId
        ? await api.ide_debug_command(command, threadId)
        : await api.ide_debug_command(command);
      if (!res?.ok) this.lastError = String(res?.msg ?? '调试命令失败');
      return res ?? { ok: false };
    },

    /**
     * 暂停：运行中还没有 `dap_stopped` 记录，`threadId` 可能是 0，而适配器会把
     * `threadId: 0` 的 pause 拒成 "thread not found"。先取线程表拿一个真实线程号，
     * 取不到才退回 0，由后端按既有回退逻辑处理（S3-T9 评审补的缺口）。
     */
    async pauseDebug() {
      const api = debugApi();
      let threadId = this.threadId;
      if (!threadId && api?.ide_debug_threads) {
        const res = await api.ide_debug_threads();
        threadId = Number(res?.threads?.[0]?.id ?? 0);
      }
      return this.sendCommand('pause', threadId);
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
      // 换帧＝换一套作用域：层级路径与缓存必须一起重来，否则会把上一帧的变量挂在新帧下
      this.varPath = [];
      this.activeScopeRef = 0;
      this.varCache = {};
      const first = this.scopes[0];
      if (!first) {
        this.variables = [];
        return this.scopes;
      }
      // 只加载首个作用域的子节点：Python(debugpy) 的 scopes 常态是 Locals + Globals，
      // 一次 IPC 就把 Globals 也拉下来既慢又可能巨大；其余作用域由用户点页签时再取
      // （S3-T10 评审：变量必须归属到各自的作用域，不能拿首个作用域的子节点去顶）
      await this.selectScope(first.variablesReference);
      return this.scopes;
    },

    /** 切到某个作用域的根层级（点变量面板上的作用域页签） */
    async selectScope(reference: number) {
      const scope = this.scopes.find((item) => item.variablesReference === reference);
      if (!scope) return this.variables;
      this.activeScopeRef = reference;
      this.varPath = [{ reference, name: scope.name }];
      return this._loadLevel(reference);
    },

    /**
     * 载入某一层的变量（命中缓存则直接用）。
     *
     * 缓存只服务「暂停期间来回走层级」：`_clearPauseDetail` 在每次暂停/继续/结束时清空。
     */
    async _loadLevel(reference: number) {
      const cached = this.varCache[reference];
      if (cached) {
        this.variables = cached;
        return cached;
      }
      const api = debugApi();
      if (!api?.ide_debug_variables) return [];
      const res = await api.ide_debug_variables(reference);
      const items = (res?.variables ?? []) as DapVariable[];
      this.varCache = { ...this.varCache, [reference]: items };
      this.variables = items;
      return items;
    },

    /**
     * 展开一个变量：进入它的子层级。
     *
     * 旧实现是「用子节点整体替换 variables」，展开 `self` 或一个大 dict 之后用户既看不到
     * 兄弟变量、也没有任何回退入口（唯一的回退是回调用栈再点一次帧）。现在把层级路径
     * 记进 `varPath`，配合 `backVariables` 面包屑回退（S3-T10 评审）。
     */
    async expandVariables(variablesReference: number, name = '') {
      const items = await this._loadLevel(variablesReference);
      this.varPath = [
        ...this.varPath,
        { reference: variablesReference, name: name || `#${variablesReference}` },
      ];
      return items;
    },

    /** 返回上一层（面包屑左侧的「返回上一层」）；已在作用域根时不动作 */
    async backVariables() {
      if (this.varPath.length <= 1) return this.variables;
      this.varPath = this.varPath.slice(0, -1);
      return this._loadLevel(this.varPath[this.varPath.length - 1].reference);
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
