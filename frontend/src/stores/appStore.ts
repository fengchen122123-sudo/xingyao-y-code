import { defineStore } from 'pinia';
import type { ModelInfo, SessionGroup, SkillInfo, McpInfo, FileNode, BridgeEvent, PlanData, BackgroundTask } from '../types/pywebview';
import { formatPermMode } from '../utils/markdown';

export interface ChatMessage {
  id?: string;
  role: 'user' | 'assistant' | 'tool';
  content?: string;
  reasoning_content?: string;
  tool_calls?: any[];
  tool_call_id?: string;
  name?: string;
}

export interface ConfirmState {
  show: boolean;
  title: string;
  sub: string;
  message: string;
  resolve?: (ok: boolean) => void;
}

export interface PermReqState {
  show: boolean;
  reqId: string;
  toolName: string;
  details: string;
}

export const useAppStore = defineStore('app', {
  state: () => ({
    currentModel: 'step-3.7-flash',
    permissionMode: '默认模式',
    models: [] as ModelInfo[],
    sessionGroups: [] as SessionGroup[],
    skills: [] as SkillInfo[],
    mcps: [] as McpInfo[],
    currentSessionName: '',
    currentSessionDisplayName: '新对话',
    currentWorkdirLabel: '对话',
    fileTree: [] as FileNode[],
    messages: [] as ChatMessage[],
    running: false,
    
    // 侧栏与面板开合状态
    sidebarCollapsed: false,
    rightPanelCollapsed: false,
    themeLight: false,

    // 计划与后台任务数据
    planData: { title: '', items: [], progress: 0 } as PlanData,
    backgroundTasks: [] as BackgroundTask[],

    // Toast
    toast: { show: false, msg: '', type: 'ok' as 'ok' | 'warn' },
    toastTimer: null as any,

    // 确认弹窗
    confirmModal: { show: false, title: '', sub: '', message: '' } as ConfirmState,

    // 权限请求弹窗
    permModal: { show: false, reqId: '', toolName: '', details: '' } as PermReqState,

    // 当前流式生成临时对象
    currentStreamingText: '',
    currentThinkingText: '',
    toolResults: {} as Record<string, { content: string; name?: string }>,
  }),

  actions: {
    toggleSidebar() {
      this.sidebarCollapsed = !this.sidebarCollapsed;
    },

    toggleRightPanel() {
      this.rightPanelCollapsed = !this.rightPanelCollapsed;
    },

    toggleTheme() {
      this.themeLight = !this.themeLight;
      document.body.classList.toggle('light-theme', this.themeLight);
    },

    showToast(msg: string, type: 'ok' | 'warn' = 'ok') {
      this.toast.msg = msg;
      this.toast.type = type;
      this.toast.show = true;
      if (this.toastTimer) clearTimeout(this.toastTimer);
      this.toastTimer = setTimeout(() => {
        this.toast.show = false;
      }, 2200);
    },

    askConfirm(title: string, sub: string, message: string): Promise<boolean> {
      return new Promise((resolve) => {
        this.confirmModal = {
          show: true,
          title,
          sub,
          message,
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
      if (!window.pywebview || !window.pywebview.api) return;
      try {
        const init = await window.pywebview.api.get_initial_state();
        if (init.current_model) this.currentModel = init.current_model;
        if (init.models) this.models = init.models;
        if (init.permission_mode) this.permissionMode = formatPermMode(init.permission_mode);
        if (init.sessions) this.sessionGroups = init.sessions;
        if (init.skills) this.skills = init.skills;
        if (init.mcps) this.mcps = init.mcps;
        if (init.current_session) this.currentSessionName = init.current_session;

        this.currentWorkdirLabel = '对话';

        this.showToast('与 Python 后端实时连接成功！', 'ok');
      } catch (err) {
        console.error('初始化状态失败:', err);
      }

      // 绑定后端 Bridge 事件广播
      window.onBridgeEvent = (event: BridgeEvent) => {
        this.handleBridgeEvent(event);
      };
    },

    async setModel(modelName: string) {
      this.currentModel = modelName;
      if (window.pywebview && window.pywebview.api) {
        await window.pywebview.api.set_model(modelName);
      }
      this.showToast('已切换活动模型: ' + modelName, 'ok');
    },

    async setPermissionMode(modeDisplay: string, modeVal: string) {
      this.permissionMode = modeDisplay;
      if (window.pywebview && window.pywebview.api) {
        await window.pywebview.api.set_permission_mode(modeDisplay, modeVal);
      }
      this.showToast('权限模式已调整: ' + modeDisplay, 'ok');
    },

    async switchSession(sessionName: string, displayName?: string, targetWorkdir?: string) {
      this.currentSessionName = sessionName;
      if (displayName) this.currentSessionDisplayName = displayName;

      if (!window.pywebview || !window.pywebview.api) return;
      try {
        const res = await window.pywebview.api.switch_session(sessionName, targetWorkdir);
        this.messages = res.messages || [];

        if (res.is_global || res.workdir === 'GLOBAL') {
          this.currentWorkdirLabel = '对话';
          this.fileTree = [];
        } else if (res.workdir) {
          this.currentWorkdirLabel = res.workdir.split('\\').pop() || res.workdir;
          if (res.file_tree) this.fileTree = res.file_tree;
        }

        if (res.sessions) this.sessionGroups = res.sessions;
      } catch (err) {
        this.showToast('切换会话失败: ' + err, 'warn');
      }
    },

    async createGlobalSession() {
      if (!window.pywebview || !window.pywebview.api) return;
      try {
        const res = await window.pywebview.api.create_global_session('新对话');
        if (res && res.session_name) {
          this.currentSessionName = res.session_name;
          const sessions = await window.pywebview.api.get_sessions();
          this.sessionGroups = sessions;
          await this.switchSession(res.session_name, '新对话', 'GLOBAL');
          this.showToast('已开启独立通用对话 (跳出项目文件夹)', 'ok');
        }
      } catch (err) {
        this.showToast('创建通用对话失败: ' + err, 'warn');
      }
    },

    async createSessionInGroup(targetWorkdir: string) {
      if (!window.pywebview || !window.pywebview.api) return;
      try {
        const res = await window.pywebview.api.create_session('新会话', targetWorkdir);
        if (res && res.session_name) {
          this.currentSessionName = res.session_name;
          await this.switchSession(res.session_name, '新会话', targetWorkdir);
          this.showToast('已在指定项目新建会话', 'ok');
        }
      } catch (err) {
        this.showToast('在项目创建会话失败: ' + err, 'warn');
      }
    },

    async deleteFolderGroup(workdir: string, folderTitle: string) {
      const ok = await this.askConfirm(
        '移出项目分组',
        '此操作仅清理软件中的历史会话，绝对不会删除磁盘文件',
        `确定要在程序中移出项目分组「${folderTitle}」的所有会话记录吗？`
      );
      if (!ok) return;

      if (!window.pywebview || !window.pywebview.api) return;
      try {
        const res = await window.pywebview.api.delete_folder_group(workdir);
        if (res && res.success) {
          this.sessionGroups = res.sessions;
          this.showToast(`已移出项目分组: ${folderTitle} (磁盘文件完好)`, 'ok');
        }
      } catch (err) {
        this.showToast('移除项目分组失败: ' + err, 'warn');
      }
    },

    async deleteSingleSession(sessionName: string) {
      const ok = await this.askConfirm(
        '删除此对话记录',
        '删除后该对话的历史记录将无法恢复',
        '确定要删除该会话记录吗？'
      );
      if (!ok) return;

      if (!window.pywebview || !window.pywebview.api) return;
      try {
        const res = await window.pywebview.api.delete_session(sessionName);
        if (res && res.success) {
          this.sessionGroups = res.sessions;
          if (sessionName === this.currentSessionName && res.current_session) {
            await this.switchSession(res.current_session, '新会话');
          }
          this.showToast('会话记录已删除', 'ok');
        }
      } catch (err) {
        this.showToast('删除会话失败: ' + err, 'warn');
      }
    },

    async openWorkspaceFolder() {
      if (!window.pywebview || !window.pywebview.api) return;
      try {
        const res = await window.pywebview.api.select_workspace_folder();
        if (res && res.workdir) {
          this.currentWorkdirLabel = res.workdir.split('\\').pop() || res.workdir;
          if (res.file_tree) this.fileTree = res.file_tree;
          if (res.sessions) this.sessionGroups = res.sessions;
          if (res.skills) this.skills = res.skills;
          this.showToast('已成功引入项目文件夹: ' + res.workdir, 'ok');
        }
      } catch (err) {
        this.showToast('选择文件夹失败: ' + err, 'warn');
      }
    },

    stopCurrentTurn() {
      if (window.pywebview && window.pywebview.api) {
        window.pywebview.api.stop_current_turn();
      }
      this.running = false;
      this.showToast('已请求中断当前回合 (Esc)', 'warn');
    },

    async sendMessage(text: string) {
      if (!text.trim()) return;
      this.running = true;
      if (window.pywebview && window.pywebview.api) {
        await window.pywebview.api.send_user_message(text.trim(), this.currentSessionName);
      } else {
        // 本地无后端离线模拟
        this.messages.push({ role: 'user', content: text.trim() });
        this.showToast('无后端连接，仅处于开发离线预览视图', 'warn');
        this.running = false;
      }
    },

    handleBridgeEvent(event: BridgeEvent) {
      const type = event.type;
      const data = event.data;

      if (type === 'turn_start') {
        this.running = true;
        this.currentStreamingText = '';
        this.currentThinkingText = '';
        this.messages.push({ role: 'user', content: data.user_text });
        this.messages.push({ role: 'assistant', content: '', reasoning_content: '' });
      } else if (type === 'thinking_delta') {
        this.currentThinkingText += data.text;
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          last.reasoning_content = this.currentThinkingText;
        }
      } else if (type === 'token_delta') {
        this.currentStreamingText += data.text;
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          last.content = this.currentStreamingText;
        }
      } else if (type === 'tool_start') {
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === 'assistant') {
          if (!last.tool_calls) last.tool_calls = [];
          const idx = last.tool_calls.findIndex((tc: any) => tc.id === data.id);
          if (idx >= 0) {
            last.tool_calls[idx].function = { name: data.tool_name, arguments: data.args };
            last.tool_calls[idx].summary = data.summary;
            last.tool_calls[idx].name = data.tool_name;
          } else {
            last.tool_calls.push({
              id: data.id,
              name: data.tool_name,
              function: { name: data.tool_name, arguments: data.args },
              summary: data.summary,
            });
          }
        }
      } else if (type === 'tool_finish') {
        this.toolResults[data.id] = { content: data.result, name: data.tool_name };
      } else if (type === 'plan_updated') {
        // 动态同步计划
        if (data.items) {
          const items = data.items.map((it: any) => ({
            id: it.id || String(Math.random()),
            text: it.text || it.title || '',
            status: it.status === 'completed' || it.status === 'done' || it.status === 'x' ? 'done' : (it.status === 'in_progress' || it.status === '/' ? 'in_progress' : 'todo')
          }));
          const doneCount = items.filter((i: any) => i.status === 'done').length;
          const progress = items.length > 0 ? Math.round((doneCount / items.length) * 100) : 0;
          this.planData = {
            title: data.title || '当前任务计划',
            items,
            progress
          };
        }
      } else if (type === 'permission_request') {
        this.permModal = {
          show: true,
          reqId: data.request_id,
          toolName: data.tool_name,
          details: JSON.stringify(data.details, null, 2),
        };
      } else if (type === 'session_renamed') {
        this.currentSessionName = data.new_name;
        this.currentSessionDisplayName = data.new_name;
        if (data.sessions) this.sessionGroups = data.sessions;
      } else if (type === 'turn_finish' || type === 'turn_idle') {
        this.running = false;
        this.currentStreamingText = '';
        this.currentThinkingText = '';
      } else if (type === 'error') {
        this.showToast(data.message || '运行错误', 'warn');
      }
    },

    respondPermission(allowed: boolean, always: boolean) {
      if (window.pywebview && window.pywebview.api && this.permModal.reqId) {
        window.pywebview.api.respond_permission(this.permModal.reqId, allowed, always);
      }
      this.permModal.show = false;
    },
  },
});
