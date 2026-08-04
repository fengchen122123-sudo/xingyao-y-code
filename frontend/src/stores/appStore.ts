import { defineStore } from 'pinia';
import type {
  ModelInfo,
  ProviderInfo,
  SessionGroup,
  SkillInfo,
  McpInfo,
  PluginInfo,
  FileNode,
  BridgeEvent,
  PlanData,
  BackgroundTask,
  TokenUsage,
  PermissionMode,
  PyWebViewAPI,
} from '../types/pywebview';
import { formatPermMode } from '../utils/markdown';

export interface ChatMessage {
  id?: string;
  role: 'user' | 'assistant' | 'tool';
  content?: string;
  reasoning_content?: string;
  tool_calls?: any[];
  tool_call_id?: string;
  name?: string;
  cancelled?: boolean;
  diffs?: Array<{ file_path: string; diff_text: string }>;
}

export interface ConfirmState {
  show: boolean;
  title: string;
  sub: string;
  message: string;
  confirmLabel: string;
  resolve?: (ok: boolean) => void;
}

export interface PermReqState {
  show: boolean;
  reqId: string;
  toolName: string;
  details: string;
  source: string;
}

export type SubpageType = 'skills' | 'mcp' | 'plugins' | 'models';

// 从完整路径中取出所在目录（不依赖 Node path 模块，纯字符串处理，兼容 \ 与 /）
function dirName(p: string): string {
  if (!p) return p;
  const idx = Math.max(p.lastIndexOf('\\'), p.lastIndexOf('/'));
  return idx >= 0 ? p.slice(0, idx) : p;
}

export const useAppStore = defineStore('app', {
  state: () => ({
    // 模型 / 供应商
    models: [] as ModelInfo[],
    providers: [] as ProviderInfo[],
    activeModelId: '',
    activeModelName: '',

    // 权限模式（规范值：confirm | auto | plan | full，展示一律经 formatPermMode）
    permissionMode: 'confirm' as PermissionMode,

    // 会话 / 项目工作区
    sessionGroups: [] as SessionGroup[],
    currentSessionName: '',
    currentSessionDisplayName: '新对话',
    currentWorkdirLabel: '对话',
    workdir: '',
    fileTree: [] as FileNode[],

    // 技能 / MCP / 插件
    skills: [] as SkillInfo[],
    mcps: [] as McpInfo[],
    plugins: [] as PluginInfo[],

    // 对话消息流
    messages: [] as ChatMessage[],
    running: false,
    showThinking: false,

    // 后端连接状态 / 版本号
    connected: false,
    version: '',
    // 启动初始化是否完成（控制 Y-CODE 启动画面，完成后淡出进入对话）
    initialized: false,

    // 侧栏与面板开合状态
    sidebarCollapsed: false,
    rightPanelCollapsed: false,
    themeLight: false,

    // 计划、后台任务、Token 用量
    planData: { title: '', items: [], progress: 0 } as PlanData,
    backgroundTasks: [] as BackgroundTask[],
    tokenUsage: { used: 0, window: 0, cached_pct: 0, cumulative: 0 } as TokenUsage,

    // Toast（支持可选的操作按钮，例如"打开所在文件夹"）
    toast: {
      show: false,
      msg: '',
      type: 'ok' as 'ok' | 'warn',
      action: null as { label: string; fn: () => void } | null,
    },
    toastTimer: null as any,

    // 确认弹窗
    confirmModal: { show: false, title: '', sub: '', message: '', confirmLabel: '确定删除 ⏎' } as ConfirmState,

    // 权限请求弹窗
    permModal: { show: false, reqId: '', toolName: '', details: '', source: '' } as PermReqState,

    // 二级抽屉页面：技能 / MCP / 模型管理
    subpage: { show: false, type: 'skills' as SubpageType },

    // 文件预览弹窗（RightPanel 文件树点击文件时使用）
    filePreview: {
      show: false,
      path: '',
      content: '',
      truncated: false,
      loading: false,
    },

    // 当前流式生成临时对象
    currentStreamingText: '',
    currentThinkingText: '',
    toolResults: {} as Record<string, any>,

    // 内嵌终端（右侧面板）
    terminalOutput: '',
    terminalRunning: false,

    // 子代理实时进度（后端 subagent_progress 事件推送，回合期间约 0.8s 一次）
    subagentTasks: [] as Array<{
      id: string;
      description: string;
      type: string;
      status: string;
      stage: string;
      stage_detail: string;
      elapsed: number;
      background: boolean;
    }>,
  }),

  getters: {
    // 按供应商分组的模型列表，ModelCascader 与 Subpage(models) 共用
    modelsByProvider(state): Record<string, ModelInfo[]> {
      const groups: Record<string, ModelInfo[]> = {};
      state.models.forEach(m => {
        const p = m.provider_name || '未分组';
        if (!groups[p]) groups[p] = [];
        groups[p].push(m);
      });
      return groups;
    },

    // 供 "添加模型" 表单使用的供应商下拉选项：
    // 合并显式添加过的供应商(带真实 id) 与 从现有模型列表反推出的供应商名称
    // （契约未提供 get_providers 接口，此处以 provider_name 作为兜底 id）
    providerOptions(state): Array<{ id: string; name: string }> {
      const map = new Map<string, string>();
      state.providers.forEach(p => map.set(p.id, p.name));
      state.models.forEach(m => {
        if (!map.has(m.provider_name)) map.set(m.provider_name, m.provider_name);
      });
      return Array.from(map.entries()).map(([id, name]) => ({ id, name }));
    },
  },

  actions: {
    // 统一的后端可用性检查：不可用时自动置 connected=false 并 toast 提示。
    // 返回值经 TS 收窄为局部 const，后续 await 调用不会丢失类型narrowing。
    _api(): PyWebViewAPI | null {
      const api = window.pywebview && window.pywebview.api;
      if (!api) {
        this.connected = false;
        this.showToast('后端连接失败：pywebview API 不可用', 'warn');
        return null;
      }
      return api;
    },

    toggleSidebar() {
      this.sidebarCollapsed = !this.sidebarCollapsed;
    },

    toggleRightPanel() {
      this.rightPanelCollapsed = !this.rightPanelCollapsed;
    },

    toggleTheme() {
      this.themeLight = !this.themeLight;
      // body.light 是唯一的浅色主题开关机制（对应 style.css 中的 body.light 选择器）
      document.body.classList.toggle('light', this.themeLight);
      // 同步 Windows 窗口标题栏明暗，避免深色内容配白色标题栏刺眼
      this._api()?.set_titlebar_theme(!this.themeLight)?.catch(() => {});
    },

    toggleShowThinking() {
      this.showThinking = !this.showThinking;
    },

    openSubpage(type: SubpageType) {
      this.subpage = { show: true, type };
    },

    closeSubpage() {
      this.subpage.show = false;
    },

    showToast(msg: string, type: 'ok' | 'warn' = 'ok', action: { label: string; fn: () => void } | null = null) {
      this.toast.msg = msg;
      this.toast.type = type;
      this.toast.action = action;
      this.toast.show = true;
      if (this.toastTimer) clearTimeout(this.toastTimer);
      const duration = action ? 6000 : 2200;
      this.toastTimer = setTimeout(() => {
        this.toast.show = false;
      }, duration);
    },

    askConfirm(title: string, sub: string, message: string, confirmLabel: string = '确定删除 ⏎'): Promise<boolean> {
      return new Promise((resolve) => {
        this.confirmModal = {
          show: true,
          title,
          sub,
          message,
          confirmLabel,
          resolve,
        };
      });
    },

    resolveConfirm(ok: boolean) {
      if (this.confirmModal.resolve) {
        this.confirmModal.resolve(ok);
      }
      this.confirmModal.show = false;
    },

    async initFromBackend() {
      // 无论初始状态拉取是否成功，都先挂好事件监听，保证后续事件不丢失
      window.onBridgeEvent = (event: BridgeEvent) => {
        this.handleBridgeEvent(event);
      };

      // 启动画面最少展示时长：后端太快时避免一闪而过
      const SPLASH_MIN_MS = 900;
      const splashStart = Date.now();
      const finishSplash = async () => {
        const remain = SPLASH_MIN_MS - (Date.now() - splashStart);
        if (remain > 0) await new Promise(r => setTimeout(r, remain));
        this.initialized = true;
      };

      const api = this._api();
      if (!api) {
        await finishSplash();
        return;
      }
      // 启动时按当前主题同步一次标题栏（默认深色）
      api.set_titlebar_theme(!this.themeLight)?.catch(() => {});
      try {
        const init = await api.get_initial_state();
        this.models = init.models || [];
        this.activeModelId = init.active_model_id || '';
        this.activeModelName = init.active_model_name || '';
        this.permissionMode = init.permission_mode || 'confirm';
        this.sessionGroups = init.sessions || [];
        this.skills = init.skills || [];
        this.mcps = init.mcps || [];
        this.plugins = (init as any).plugins || [];
        this.workdir = init.workdir || '';
        this.currentSessionName = (init as any).current_session || '';
        // 工作区标签以“当前会话归属”为准：GLOBAL 会话显示“对话”，
        // 不能直接用启动目录，否则会显示在工作区里、消息却发进自由对话
        const isGlobal = !!(init as any).is_global;
        this.currentWorkdirLabel = (!isGlobal && this.workdir)
          ? (this.workdir.split('\\').pop() || this.workdir)
          : '对话';
        // 启动时后端自动恢复最近一次对话：同步消息列表与标题
        this.messages = (init as any).messages || [];
        for (const g of this.sessionGroups) {
          const hit = (g.sessions || []).find((s: any) => s.name === this.currentSessionName);
          if (hit) {
            this.currentSessionDisplayName = hit.display_name || hit.name;
            break;
          }
        }
        this.fileTree = (init as any).file_tree || [];
        this.tokenUsage = init.token_usage || { used: 0, window: 0, cached_pct: 0, cumulative: 0 };
        this.version = init.version || '';
        this.connected = true;
      } catch (err) {
        this.connected = false;
        this.showToast('后端连接失败：' + err, 'warn');
      } finally {
        await finishSplash();
      }
    },

    async setModel(modelId: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.set_model(modelId);
        if (res && res.ok) {
          this.activeModelId = res.active_model_id || modelId;
          this.activeModelName = res.active_model_name || this.activeModelName;
          this.showToast('已切换活动模型: ' + this.activeModelName, 'ok');
        } else {
          this.showToast('切换模型失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('切换模型失败: ' + err, 'warn');
      }
    },

    async setPermissionMode(mode: PermissionMode) {
      const prev = this.permissionMode;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.set_permission_mode(mode);
        if (res && res.ok) {
          this.permissionMode = res.permission_mode || mode;
          this.showToast('权限模式已调整: ' + formatPermMode(this.permissionMode), 'ok');
        } else {
          this.permissionMode = prev;
          this.showToast('权限模式调整失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.permissionMode = prev;
        this.connected = false;
        this.showToast('权限模式调整失败: ' + err, 'warn');
      }
    },

    async switchSession(sessionName: string, displayName?: string, targetWorkdir?: string) {
      this.currentSessionName = sessionName;
      if (displayName) this.currentSessionDisplayName = displayName;

      const api = this._api();
      if (!api) return;
      try {
        const res = await api.switch_session(sessionName, targetWorkdir);
        this.messages = res.messages || [];
        this.toolResults = {};

        if (res.is_global || res.workdir === 'GLOBAL') {
          this.currentWorkdirLabel = '对话';
          this.workdir = '';
          this.fileTree = [];
        } else if (res.workdir) {
          this.workdir = res.workdir;
          this.currentWorkdirLabel = res.workdir.split('\\').pop() || res.workdir;
          if (res.file_tree) this.fileTree = res.file_tree;
        }

        if (res.sessions) this.sessionGroups = res.sessions;
      } catch (err) {
        this.connected = false;
        this.showToast('切换会话失败: ' + err, 'warn');
      }
    },

    async createGlobalSession() {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.create_global_session('新对话');
        if (res && res.session_name) {
          this.currentSessionName = res.session_name;
          const sessions = await api.get_sessions();
          this.sessionGroups = sessions;
          await this.switchSession(res.session_name, '新对话', 'GLOBAL');
          this.showToast('已开启独立通用对话 (跳出项目文件夹)', 'ok');
        } else {
          this.showToast('创建通用对话失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('创建通用对话失败: ' + err, 'warn');
      }
    },

    // 顶栏“新会话”按钮：跟随当前上下文——在项目中就在该项目新建，
    // 在自由对话中就新建自由对话
    async createSessionSmart() {
      if (this.workdir && this.currentWorkdirLabel !== '对话') {
        await this.createSessionInGroup(this.workdir);
      } else {
        await this.createGlobalSession();
      }
    },

    async createSessionInGroup(targetWorkdir: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.create_session('新会话', targetWorkdir);
        if (res && res.session_name) {
          this.currentSessionName = res.session_name;
          await this.switchSession(res.session_name, '新会话', targetWorkdir);
          this.showToast('已在指定项目新建会话', 'ok');
        } else {
          this.showToast('在项目创建会话失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('在项目创建会话失败: ' + err, 'warn');
      }
    },

    async deleteFolderGroup(workdir: string, folderTitle: string) {
      // 确认动作已内联在分组头的红色小按钮上，不再弹大确认窗；
      // 后端 delete_folder_group 会连同组内对话记录一起删除（不动磁盘文件）
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.delete_folder_group(workdir);
        if (res && res.success) {
          this.sessionGroups = res.sessions;
          this.showToast(`已移出项目分组: ${folderTitle} (磁盘文件完好)`, 'ok');
        } else {
          this.showToast('移除项目分组失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('移除项目分组失败: ' + err, 'warn');
      }
    },

    async deleteSingleSession(sessionName: string) {
      // 确认动作已内联在侧栏的红色小按钮上，不再弹大确认窗
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.delete_session(sessionName);
        if (res && res.success) {
          this.sessionGroups = res.sessions;
          if (sessionName === this.currentSessionName && res.current_session) {
            await this.switchSession(res.current_session, '新会话');
          }
          this.showToast('会话记录已删除', 'ok');
        } else {
          this.showToast('删除会话失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('删除会话失败: ' + err, 'warn');
      }
    },

    async openWorkspaceFolder() {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.select_workspace_folder();
        if (res && res.ok !== false && res.workdir) {
          this.workdir = res.workdir;
          this.currentWorkdirLabel = res.workdir.split('\\').pop() || res.workdir;
          if (res.file_tree) this.fileTree = res.file_tree;
          if (res.sessions) this.sessionGroups = res.sessions;
          if (res.skills) this.skills = res.skills;
          this.showToast('已成功引入项目文件夹: ' + res.workdir, 'ok');
        }
        // res.ok === false 代表用户取消了文件夹选择，静默忽略即可
      } catch (err) {
        this.connected = false;
        this.showToast('选择文件夹失败: ' + err, 'warn');
      }
    },

    async stopCurrentTurn() {
      this.running = false;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.stop_current_turn();
        if (!res || !res.ok) {
          this.showToast('中断请求未成功送达', 'warn');
        } else {
          this.showToast('已请求中断当前回合', 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('中断请求失败: ' + err, 'warn');
      }
    },

    async sendMessage(text: string, attachments?: string[]) {
      // 生成中禁止再次发送（无论调用方是否已自行判断，均在此兜底拦截）
      const attachList = (attachments || []).filter(p => p && p.trim());
      if ((!text.trim() && !attachList.length) || this.running) return;
      this.running = true;

      const api = window.pywebview && window.pywebview.api;
      if (!api) {
        // 本地无后端离线模拟，方便纯前端调试
        this.messages.push({ role: 'user', content: text.trim() });
        this.showToast('无后端连接，仅处于开发离线预览视图', 'warn');
        this.running = false;
        return;
      }
      try {
        const res = await api.send_user_message(text.trim(), this.currentSessionName, attachList);
        if (!res || !res.ok) {
          this.running = false;
          this.showToast('消息发送失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
        // 成功时不主动改 running，由后续 turn_start/turn_finish/turn_cancelled 等事件驱动
      } catch (err) {
        this.running = false;
        this.connected = false;
        this.showToast('消息发送失败: ' + err, 'warn');
      }
    },

    async refreshSessions() {
      const api = this._api();
      if (!api) return;
      try {
        const sessions = await api.get_sessions();
        if (sessions) this.sessionGroups = sessions;
      } catch (err) {
        // 静默失败：下一次切换会话/新建会话时会重新拉取
        console.error('刷新会话列表失败:', err);
      }
    },

    async refreshTokenUsage() {
      const api = window.pywebview && window.pywebview.api;
      if (!api) return;
      try {
        const usage = await api.get_token_usage();
        if (usage) this.tokenUsage = usage;
      } catch (err) {
        // 静默失败：token_usage 推送事件通常会补上，不打断用户
        console.error('刷新 Token 用量失败:', err);
      }
    },

    handleBridgeEvent(event: BridgeEvent) {
      const type = event.type;
      const data = event.data || {};
      // 只要有事件推送过来，说明桥接链路是通的
      this.connected = true;

      if (type === 'turn_start') {
        this.running = true;
        this.currentStreamingText = '';
        this.currentThinkingText = '';
        this.subagentTasks = [];
        // 内部回合（如后台任务自动汇总）：展示友好的系统提示文案，
        // 不把内部指令原文渲染成用户气泡
        this.messages.push({ role: 'user', content: data.display_text || data.user_text });
        this.messages.push({ role: 'assistant', content: '', reasoning_content: '' });
        if (data.internal === 'bg_summary') {
          this.showToast('后台子代理任务已全部结束，正在自动汇总结果', 'ok');
        }
      } else if (type === 'thinking_delta') {
        // 后端约 60ms 批量推送，data.text 可能包含多字符/多行，直接拼接即可
        this.currentThinkingText += data.text || '';
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          last.reasoning_content = this.currentThinkingText;
        }
      } else if (type === 'token_delta') {
        this.currentStreamingText += data.text || '';
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          last.content = this.currentStreamingText;
        }
      } else if (type === 'tool_start') {
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          if (!last.tool_calls) last.tool_calls = [];
          const idx = last.tool_calls.findIndex((tc: any) => tc.id === data.id);
          const entry = { id: data.id, name: data.name, label: data.label };
          if (idx >= 0) last.tool_calls[idx] = { ...last.tool_calls[idx], ...entry };
          else last.tool_calls.push(entry);
        }
      } else if (type === 'tool_finish') {
        this.toolResults[data.id] = {
          ok: data.ok,
          summary: data.summary,
          result: data.result,
          name: data.name,
          label: data.label,
        };
      } else if (type === 'plan_updated') {
        if (data.items) {
          const items = data.items.map((it: any) => ({
            id: it.id || String(Math.random()),
            // 后端计划项的正文字段是 subject（见 core/plan.py snapshot）
            text: it.subject || it.text || it.title || '',
            status: it.status === 'completed' || it.status === 'done' || it.status === 'x' ? 'done' : (it.status === 'in_progress' || it.status === '/' ? 'in_progress' : 'todo'),
          }));
          const doneCount = items.filter((i: any) => i.status === 'done').length;
          const progress = items.length > 0 ? Math.round((doneCount / items.length) * 100) : 0;
          this.planData = {
            title: data.title || '当前任务计划',
            items,
            progress,
          };
        }
      } else if (type === 'permission_request') {
        this.permModal = {
          show: true,
          reqId: data.request_id,
          toolName: data.function_name,
          details: JSON.stringify(data.arguments || {}, null, 2),
          source: data.source || '',
        };
      } else if (type === 'permission_dismiss') {
        // 后端权限队列已清空（请求被超时拒绝/取消）：收起弹窗，避免死请求残留
        this.permModal.show = false;
      } else if (type === 'notice') {
        if (data.message) this.showToast(String(data.message), 'ok');
      } else if (type === 'session_renamed') {
        if (this.currentSessionName === data.old) {
          this.currentSessionName = data.new;
          this.currentSessionDisplayName = data.new;
        }
        this.sessionGroups.forEach(g => {
          (g.sessions || []).forEach(s => {
            if (s.name === data.old) {
              s.name = data.new;
              s.display_name = data.new;
            }
          });
        });
      } else if (type === 'turn_finish' || type === 'turn_idle') {
        this.running = false;
        this.currentStreamingText = '';
        this.currentThinkingText = '';
        this.refreshTokenUsage();
        // 对话结束后消息数已变化，刷新侧栏会话列表（否则计数停留在启动快照）
        this.refreshSessions();
      } else if (type === 'turn_cancelled') {
        this.running = false;
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          last.cancelled = true;
        }
        this.currentStreamingText = '';
        this.currentThinkingText = '';
        this.refreshTokenUsage();
        this.refreshSessions();
      } else if (type === 'token_usage') {
        this.tokenUsage = {
          used: data.used || 0,
          window: data.window || 0,
          cached_pct: data.cached_pct || 0,
          cumulative: data.cumulative || 0,
        };
      } else if (type === 'mcp_status') {
        if (data.mcps) this.mcps = data.mcps;
      } else if (type === 'diff_ready') {
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          if (!last.diffs) last.diffs = [];
          last.diffs.push({ file_path: data.file_path, diff_text: data.diff_text });
        }
      } else if (type === 'subagent_progress') {
        this.subagentTasks = data.tasks || [];
      } else if (type === 'background_subagent_terminal') {
        const statusLabel: Record<string, string> = {
          completed: '已完成', failed: '已失败', timed_out: '已超时', cancelled: '已取消',
        };
        const label = statusLabel[data.status] || data.status;
        const desc = data.description ? `：${data.description}` : '';
        this.showToast(`后台子代理${label}${desc}`, data.status === 'completed' ? 'ok' : 'warn');
      } else if (type === 'terminal_output') {
        this.terminalOutput += data.text || '';
        // 输出上限 200K 字符：超出后从头部截断，避免长任务把内存拖爆
        if (this.terminalOutput.length > 200_000) {
          this.terminalOutput = '…[输出过长，头部已截断]\n' + this.terminalOutput.slice(-160_000);
        }
      } else if (type === 'terminal_finished') {
        this.terminalRunning = false;
        const code = data.code;
        this.terminalOutput += `\n[进程已退出，代码 ${code}]\n\n`;
      } else if (type === 'error') {
        this.showToast(data.message || '运行错误', 'warn');
      }
    },

    async respondPermission(allowed: boolean, always: boolean) {
      const reqId = this.permModal.reqId;
      this.permModal.show = false;
      if (!reqId) return;
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.respond_permission(reqId, allowed, always);
        if (!res || !res.ok) {
          this.showToast('权限响应发送失败', 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('权限响应发送失败: ' + err, 'warn');
      }
    },

    async toggleSkill(skillId: string, enabled: boolean) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.toggle_skill(skillId, enabled);
        if (res && res.ok) {
          this.skills = res.skills || this.skills;
        } else {
          this.showToast('切换技能状态失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('切换技能状态失败: ' + err, 'warn');
      }
    },

    async toggleMcpServer(name: string, enabled: boolean) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.toggle_mcp_server(name, enabled);
        if (res && res.ok) {
          this.mcps = res.mcps || this.mcps;
          this.showToast(res.msg || 'MCP 状态已更新', 'ok');
        } else {
          this.showToast('MCP 状态更新失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('MCP 状态更新失败: ' + err, 'warn');
      }
    },

    // 启用 MCP 服务：预置服务需带上用户填写的密钥；已配置服务等价于 toggle
    async enableMcpServer(name: string, envValues?: Record<string, string>) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.enable_mcp_server(name, envValues || {});
        if (res && res.ok) {
          this.mcps = res.mcps || this.mcps;
          this.showToast(res.msg || 'MCP 状态已更新', 'ok');
        } else {
          this.showToast('启用失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('启用 MCP 服务失败: ' + err, 'warn');
      }
    },

    async refreshPlugins() {
      const api = this._api();
      if (!api) return;
      try {
        this.plugins = (await api.get_plugins()) || [];
      } catch {
        // 静默失败，保留现有列表
      }
    },

    async togglePlugin(name: string, enabled: boolean) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.toggle_plugin(name, enabled);
        if (res && res.ok) {
          this.plugins = res.plugins || this.plugins;
          this.showToast(res.msg || '插件状态已更新', 'ok');
        } else {
          this.showToast('插件状态更新失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('插件状态更新失败: ' + err, 'warn');
      }
    },

    async reconnectMcp() {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.reconnect_mcp();
        if (res && res.ok) {
          this.mcps = res.mcps || this.mcps;
          this.showToast(res.msg || 'MCP 已重新连接', 'ok');
        } else {
          this.showToast('MCP 重连失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('MCP 重连失败: ' + err, 'warn');
      }
    },

    async refreshMcpServers() {
      const api = window.pywebview && window.pywebview.api;
      if (!api) return;
      try {
        const list = await api.get_mcp_servers();
        if (list) this.mcps = list;
      } catch (err) {
        console.error('刷新 MCP 列表失败:', err);
      }
    },

    // ---------------- 内嵌终端 ----------------
    async runTerminalCommand(command: string) {
      const cmd = (command || '').trim();
      if (!cmd || this.terminalRunning) return;
      const api = this._api();
      if (!api) return;
      this.terminalOutput += `$ ${cmd}\n`;
      this.terminalRunning = true;
      try {
        const res = await api.run_terminal_command(cmd);
        if (!res || !res.ok) {
          this.terminalRunning = false;
          this.terminalOutput += `[启动失败: ${(res && res.msg) || '未知错误'}]\n`;
        }
      } catch (err) {
        this.terminalRunning = false;
        this.terminalOutput += `[启动失败: ${err}]\n`;
      }
    },

    async killTerminalCommand() {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.kill_terminal_command();
        if (!res || !res.ok) {
          this.showToast((res && res.msg) || '终止命令失败', 'warn');
        }
      } catch (err) {
        this.showToast('终止命令失败: ' + err, 'warn');
      }
    },

    clearTerminal() {
      this.terminalOutput = '';
    },

    async exportSession(fmt: 'md' | 'html' | 'json') {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.export_session(fmt);
        if (res && res.ok) {
          const path = res.path || '';
          this.showToast(
            '导出成功: ' + path,
            'ok',
            path ? { label: '打开所在文件夹', fn: () => this.openInSystem(dirName(path)) } : null
          );
        } else {
          this.showToast('导出失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('导出失败: ' + err, 'warn');
      }
    },

    async clearSessionHistory() {
      const ok = await this.askConfirm(
        '清空会话历史',
        '此操作不可撤销',
        '清空当前会话的全部历史记录？此操作不可撤销',
        '确定清空 ⏎'
      );
      if (!ok) return;

      const api = this._api();
      if (!api) return;
      try {
        const res = await api.clear_session_history(this.currentSessionName);
        if (res && res.ok) {
          this.messages = [];
          this.toolResults = {};
          this.showToast('已清空当前会话历史', 'ok');
        } else {
          this.showToast('清空失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('清空失败: ' + err, 'warn');
      }
    },

    async undoLastEdit() {
      const ok = await this.askConfirm(
        '撤销文件修改',
        '此操作将撤销星瑶最近一次的文件改动',
        '撤销星瑶上一次的文件修改？',
        '确定撤销 ⏎'
      );
      if (!ok) return;

      const api = this._api();
      if (!api) return;
      try {
        const res = await api.undo_last_edit();
        if (res && res.ok) {
          this.showToast(res.msg || '已撤销上一次文件修改', 'ok');
        } else {
          this.showToast('撤销失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('撤销失败: ' + err, 'warn');
      }
    },

    async pickFiles(): Promise<string[]> {
      const api = this._api();
      if (!api) return [];
      try {
        const res = await api.pick_files();
        if (res && res.ok && res.paths) return res.paths;
        return [];
      } catch (err) {
        this.connected = false;
        this.showToast('选择文件失败: ' + err, 'warn');
        return [];
      }
    },

    async getFilesInfo(paths: string[]) {
      const api = this._api();
      if (!api || !paths.length) return [];
      const list = await api.get_files_info(paths);
      return (list || []) as Array<{ path: string; name: string; ext: string; size: number | null; ok: boolean }>;
    },

    // 保存剪贴板粘贴的图片，返回落盘后的文件路径（失败返回空串并 toast）
    async saveClipboardImage(dataUrl: string): Promise<string> {
      const api = this._api();
      if (!api) return '';
      try {
        const res = await api.save_clipboard_image(dataUrl);
        if (res && res.ok && res.path) return res.path;
        this.showToast('粘贴图片失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        return '';
      } catch (err) {
        this.showToast('粘贴图片失败: ' + err, 'warn');
        return '';
      }
    },

    async openFilePreview(path: string) {
      this.filePreview = { show: true, path, content: '', truncated: false, loading: true };
      const api = this._api();
      if (!api) {
        this.filePreview.show = false;
        return;
      }
      try {
        const res = await api.read_file_preview(path);
        if (res && res.ok) {
          this.filePreview.content = res.content || '';
          this.filePreview.truncated = !!res.truncated;
          this.filePreview.loading = false;
        } else {
          this.filePreview.show = false;
          this.showToast('无法预览文件: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.filePreview.show = false;
        this.connected = false;
        this.showToast('无法预览文件: ' + err, 'warn');
      }
    },

    closeFilePreview() {
      this.filePreview.show = false;
    },

    async openInSystem(path: string) {
      const api = this._api();
      if (!api) return;
      try {
        const res = await api.open_path(path);
        if (!res || !res.ok) {
          this.showToast('打开失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('打开失败: ' + err, 'warn');
      }
    },

    async addProvider(name: string, baseUrl: string, apiKey: string, protocol: string) {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.add_provider(name, baseUrl, apiKey, protocol);
        if (res && res.ok) {
          this.providers.push({ id: res.provider_id, name });
          this.showToast(res.msg || '供应商已添加', 'ok');
          return res;
        } else {
          this.showToast('添加供应商失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
          return null;
        }
      } catch (err) {
        this.connected = false;
        this.showToast('添加供应商失败: ' + err, 'warn');
        return null;
      }
    },

    async addModel(providerId: string, modelName: string, contextWindow: number) {
      const api = this._api();
      if (!api) return null;
      try {
        const res = await api.add_model(providerId, modelName, contextWindow);
        if (res && res.ok) {
          if (res.models) this.models = res.models;
          this.showToast(res.msg || '模型已添加', 'ok');
          return res;
        } else {
          this.showToast('添加模型失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
          return null;
        }
      } catch (err) {
        this.connected = false;
        this.showToast('添加模型失败: ' + err, 'warn');
        return null;
      }
    },

    async deleteModel(model: ModelInfo) {
      const ok = await this.askConfirm('删除模型', '删除后需要重新添加才能使用', `确定要删除模型「${model.name}」吗？`);
      if (!ok) return;

      const api = this._api();
      if (!api) return;
      try {
        const res = await api.delete_model(model.id);
        if (res && res.ok) {
          if (res.models) this.models = res.models;
          this.showToast('模型已删除', 'ok');
        } else {
          this.showToast('删除模型失败: ' + (res && res.msg ? res.msg : '未知错误'), 'warn');
        }
      } catch (err) {
        this.connected = false;
        this.showToast('删除模型失败: ' + err, 'warn');
      }
    },

    async testModel(modelId: string): Promise<{ ok: boolean; msg: string }> {
      const api = this._api();
      if (!api) return { ok: false, msg: '后端未连接' };
      try {
        const res = await api.test_model(modelId);
        const ok = !!(res && res.ok);
        return { ok, msg: (res && res.msg) || (ok ? '连接成功' : '连接失败') };
      } catch (err) {
        this.connected = false;
        return { ok: false, msg: String(err) };
      }
    },
  },
});
