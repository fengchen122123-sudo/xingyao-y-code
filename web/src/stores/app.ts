import { defineStore } from "pinia";
import { markRaw } from "vue";
import { isNativeApp } from "../transport/secrets";
import { GatewayShim } from "../transport/gateway-shim";
import { useAuthStore } from "./auth";

export interface ChatMessage {
  id: string;
  role: "user" | "agent" | "tool";
  text: string;
  thinking?: string;
  tools?: { id: string; title: string; detail: string }[];
}

export interface ModelOption {
  id: string;
  name: string;
}

interface ActiveTurn {
  turn_id?: string;
  session_name?: string;
  user_text?: string;
  display_text?: string;
  thinking_text?: string;
  streaming_text?: string;
  tool_calls?: { id?: string; name?: string; label?: string; summary?: string; result?: string; error?: string }[];
}

interface ModelState {
  models?: { id?: unknown; name?: string; model_name?: string }[];
  active_model_id?: unknown;
  active_model_name?: unknown;
  model_id?: unknown;
  model_name?: unknown;
}

function textOf(data: Record<string, unknown>): string {
  return String(data.text || data.delta || "");
}

function flattenSessions(value: unknown): { name: string }[] {
  if (!Array.isArray(value)) return [];
  const names: { name: string }[] = [];
  value.forEach((item) => {
    const group = item as { name?: string; sessions?: { name?: string }[] };
    if (Array.isArray(group.sessions)) {
      group.sessions.forEach((session) => {
        if (session.name) names.push({ name: session.name });
      });
      return;
    }
    if (group.name) names.push({ name: group.name });
  });
  return names;
}

function mapMessages(value: unknown): ChatMessage[] {
  if (!Array.isArray(value)) return [];
  return value.map((item, index) => {
    const message = item as { role?: string; content?: string; reasoning_content?: string; name?: string };
    const role = message.role === "user" ? "user" : message.role === "tool" ? "tool" : "agent";
    return {
      id: String(index),
      role,
      text: String(message.content || ""),
      thinking: String(message.reasoning_content || ""),
      tools: role === "tool" ? [{ id: String(index), title: String(message.name || "工具"), detail: String(message.content || "") }] : [],
    };
  });
}

export const useAppStore = defineStore("app", {
  state: () => ({
    messages: [] as ChatMessage[],
    /** 本地先显示、等待服务端 turn_start 认领的用户消息，避免回显重复。 */
    pendingUsers: [] as { id: string; text: string }[],
    sessions: [] as { name: string }[],
    currentSession: "",
    activeTurn: "",
    activeTurns: {} as Record<string, string>,
    approvals: [] as { id: string; title: string; path: string }[],
    answering: [] as string[],
    previewUrl: "",
    previewRun: "",
    status: "未连接",
    profile: "agent" as "agent" | "desktop",
    deviceId: "",
    models: [] as ModelOption[],
    activeModelId: "",
    activeModelName: "",
    modelStatus: "idle" as "idle" | "loading" | "ready" | "error",
    modelError: "",
    shim: null as GatewayShim | null,
  }),
  getters: {
    connectionTarget: (state) => state.profile === "desktop" ? `电脑 ${state.deviceId || "未指定"}` : "云端 Agent",
  },
  actions: {
    connect() {
      const auth = useAuthStore();
      if (!auth.signedIn || this.shim) return;
      this.open(auth, this.profile, this.deviceId);
    },
    connectDesktop(deviceId: string) {
      const auth = useAuthStore();
      this.open(auth, "desktop", deviceId);
    },
    open(auth: ReturnType<typeof useAuthStore>, profile: "agent" | "desktop", deviceId = "") {
      if (this.shim && this.profile === profile && this.deviceId === deviceId) return;
      this.disconnect();
      this.profile = profile;
      this.deviceId = deviceId;
      const shim = new GatewayShim({
        server: auth.server,
        accountId: auth.accountId,
        profile,
        credential: auth.credential,
        refreshCredential: () => useAuthStore().credential,
        deviceId,
        capacitor: isNativeApp(),
        onUnauthorized: async () => {
          const ok = await useAuthStore().refresh();
          if (!ok) {
            await useAuthStore().logout();
            const { default: router } = await import("../router");
            await router.push("/login");
          }
          return ok;
        },
      });
      shim.onEvent((frame) => {
        if (this.shim === shim) this.apply(frame);
      });
      this.shim = markRaw(shim);
      this.status = profile === "desktop" ? "正在连接这台电脑" : "正在连接";
      shim.connect();
      // 连上没有不能只看「socket 打开了」：要等 shim 拿到 hello/整态才算可用。
      void shim.whenReady().then(async () => {
        if (this.shim !== shim) return;
        // 初次连接偶尔会错过 yc_snapshot；就绪后再主动取一次整态兜底，
        // 否则刷新页面会出现“有旧消息但侧栏没有会话”的错位。
        if (!this.sessions.length) {
          try {
            const state = await shim.rpc("get_initial_state", []) as Record<string, unknown>;
            if (this.shim === shim && state && typeof state.current_session !== "undefined") {
              this.replaceSnapshot(state);
            }
          } catch {
            // 交由正常重连/事件流恢复，不把首次加载卡在兜底请求上。
          }
        }
        if (this.shim === shim && this.status !== "已同步") this.status = "已连接";
      }).catch(() => undefined);
    },
    apply(frame: { type: string; data?: unknown }) {
      const data = (frame.data || {}) as Record<string, unknown>;
      if (frame.type === "hello") return;
      if (frame.type === "worker_starting") {
        this.status = "正在启动";
        return;
      }
      if (frame.type === "worker_lost") {
        this.status = "星瑶没有连上";
        return;
      }
      if (frame.type === "yc_snapshot") {
        this.replaceSnapshot(data);
        // 重连/整态之后的未决审批不会重播事件，必须主动拉一次快照，否则回合会一直挂着。
        void this.syncApprovals();
        return;
      }
      const session = String(data.session_name || "");
      if (session && this.currentSession && session !== this.currentSession) return;
      if (frame.type === "model_changed") {
        this.syncModel(data);
        return;
      }
      if (frame.type === "turn_start") {
        const turn = String(data.turn_id || "");
        if (session && turn && this.activeTurns[session] === turn) return;
        if (session) {
          this.activeTurns[session] = turn;
          this.activeTurn = turn;
        }
        const userText = String(data.display_text || data.user_text || "");
        // 服务端回显的 display_text 可能带上下文/空白，不能靠文本相等去重。
        // 优先认领本地乐观气泡；只有没有待认领消息时才从事件创建。
        const pending = this.pendingUsers.shift();
        if (pending) {
          const local = this.messages.find((item) => item.id === pending.id);
          if (local && userText) local.text = userText;
        } else if (userText) {
          this.messages.push({ id: `event-user-${Date.now()}`, role: "user", text: userText });
        }
        this.messages.push({ id: `event-agent-${Date.now()}`, role: "agent", text: "", thinking: "", tools: [] });
        return;
      }
      const turn = String(data.turn_id || "");
      const expectedTurn = session ? this.activeTurns[session] : this.activeTurn;
      if (turn && expectedTurn && turn !== expectedTurn) return;
      if ((frame.type === "thinking_delta" || frame.type === "token_delta") && turn && !expectedTurn) return;
      if (frame.type === "thinking_delta" || frame.type === "token_delta") {
        const last = this.ensureAgent();
        if (frame.type === "thinking_delta") last.thinking = `${last.thinking || ""}${textOf(data)}`;
        else last.text += textOf(data);
        return;
      }
      if (frame.type === "message_final") {
        const last = this.ensureAgent();
        const finalText = String(data.content || "");
        last.text = finalText;
        this.activeTurn = "";
        if (session) this.activeTurns[session] = "";
        return;
      }
      if (frame.type === "tool_start" || frame.type === "tool_finish") {
        const last = this.ensureAgent();
        const tools = last.tools || (last.tools = []);
        const id = String(data.id || tools.length + 1);
        const title = String(data.label || data.name || "工具");
        const detail = String(data.summary || data.result || data.error || "");
        const found = tools.find((item) => item.id === id);
        if (found) {
          found.title = title;
          if (detail) found.detail = detail;
        } else {
          tools.push({ id, title, detail });
        }
        return;
      }
      if (frame.type === "permission_request") {
        const id = String(data.id || data.request_id || "");
        // 补发/重放会把同一条请求再送一遍，重复卡片会让人误以为有两件事要批。
        if (id && this.approvals.some((item) => item.id === id)) return;
        this.approvals.push({
          id,
          title: String(data.title || data.summary || "需要确认"),
          path: String(data.path || ""),
        });
        return;
      }
      if (frame.type === "permission_dismiss") {
        const id = String(data.id || data.request_id || "");
        this.approvals = this.approvals.filter((item) => !id || item.id !== id);
        return;
      }
      if (frame.type === "computer_observation" && typeof data.preview_url === "string") {
        this.previewUrl = data.preview_url;
        this.previewRun = String(data.run_id || "");
      }
    },
    syncModel(data: ModelState) {
      if (typeof data.active_model_id !== "undefined" || typeof data.model_id !== "undefined") {
        this.activeModelId = String(data.active_model_id || data.model_id || "");
      }
      if (typeof data.active_model_name !== "undefined" || typeof data.model_name !== "undefined") {
        this.activeModelName = String(data.active_model_name || data.model_name || "");
      }
    },
    async loadModels() {
      const shim = this.shim;
      if (!shim || this.modelStatus === "loading") return;
      this.modelStatus = "loading";
      this.modelError = "";
      try {
        await shim.whenReady();
        if (this.shim !== shim) return;
        const state = await shim.rpc("get_initial_state", []) as ModelState;
        if (this.shim !== shim) return;
        this.models = (state.models || []).map((model) => ({
          id: String(model.id || ""),
          name: model.name || model.model_name || "未命名模型",
        })).filter((model) => model.id);
        this.syncModel(state);
        this.modelStatus = "ready";
      } catch (cause) {
        if (this.shim !== shim) return;
        this.modelStatus = "error";
        this.modelError = cause instanceof Error ? cause.message : "模型加载失败，请重试";
      }
    },
    replaceSnapshot(data: Record<string, unknown>) {
      this.sessions = flattenSessions(data.sessions);
      this.currentSession = String(data.current_session || "");
      this.messages = mapMessages(data.messages);
      this.syncModel(data);
      const snapshots = (data.active_turn_snapshots || {}) as Record<string, ActiveTurn>;
      this.activeTurns = Object.fromEntries(Object.entries(snapshots).map(([name, turn]) => [name, String(turn.turn_id || "")]));
      this.activeTurn = this.activeTurns[this.currentSession] || "";
      const active = snapshots[this.currentSession] || data.active_turn_snapshot as ActiveTurn | undefined;
      if (active) {
        this.restoreActiveTurn(active);
        if (this.currentSession) this.activeTurns[this.currentSession] = this.activeTurn;
      }
      this.status = "已同步";
      // 后端重启后 current_session 可能为空，但历史会话仍在；自动打开最近一条，
      // 不能让用户刷新后看到“新对话”而侧栏却有旧会话。
      if (!this.currentSession && this.sessions.length) {
        void this.chooseSession(this.sessions[0].name).catch(() => undefined);
      }
    },
    restoreActiveTurn(turn: ActiveTurn) {
      this.activeTurn = String(turn.turn_id || "");
      const userText = String(turn.display_text || turn.user_text || "");
      if (userText) this.messages.push({ id: `active-user-${this.activeTurn}`, role: "user", text: userText });
      this.messages.push({
        id: `active-agent-${this.activeTurn}`,
        role: "agent",
        text: String(turn.streaming_text || ""),
        thinking: String(turn.thinking_text || ""),
        tools: (turn.tool_calls || []).map((tool) => ({
          id: String(tool.id || ""),
          title: String(tool.label || tool.name || "工具"),
          detail: String(tool.summary || tool.result || tool.error || ""),
        })),
      });
    },
    ensureAgent(): ChatMessage {
      const last = this.messages[this.messages.length - 1];
      if (last && last.role === "agent") return last;
      const created = { id: String(Date.now()), role: "agent" as const, text: "", thinking: "", tools: [] };
      this.messages.push(created);
      return created;
    },
    async send(text: string) {
      const id = `local-user-${Date.now()}-${this.pendingUsers.length}`;
      this.messages.push({ id, role: "user", text });
      this.pendingUsers.push({ id, text });
      const shim = this.shim;
      if (!shim) {
        this.removePendingUser(id);
        this.status = "还没有接上星瑶";
        return;
      }
      try {
        const result = await shim.rpc("send_user_message", { text, session_name: this.currentSession || undefined }) as
          { ok?: boolean; msg?: string } | undefined;
        if (result && result.ok === false) throw new Error(result.msg || "消息没有发出去，请再试一次");
      } catch (error) {
        // 没被服务端接受就不能留一条看起来已经发出去的本地消息。
        this.removePendingUser(id);
        throw error;
      }
    },
    removePendingUser(id: string) {
      this.pendingUsers = this.pendingUsers.filter((item) => item.id !== id);
      this.messages = this.messages.filter((item) => item.id !== id);
    },
    async newSession() {
      const shim = this.shim;
      if (!shim) throw new Error("还没有接上星瑶");
      const created = await shim.rpc("create_session", { title: "新对话", workdir: "GLOBAL" }) as
        { ok?: boolean; msg?: string; session_name?: string } | undefined;
      if (created && created.ok === false) throw new Error(created.msg || "新对话创建失败");
      const state = await shim.rpc("get_initial_state", []) as Record<string, unknown>;
      if (this.shim === shim) this.replaceSnapshot(state);
    },
    async chooseSession(name: string) {
      const shim = this.shim;
      if (!shim) return;
      const result = await shim.rpc("switch_session", { session_name: name }) as
        ({ messages?: unknown; session_name?: string; active_turn_snapshot?: ActiveTurn; ok?: boolean; msg?: string } & ModelState) | undefined;
      if (result && result.ok === false) throw new Error(result.msg || "会话没有切换成功");
      this.currentSession = String(result?.session_name || name);
      this.pendingUsers = [];
      this.messages = mapMessages(result?.messages);
      this.syncModel(result || {});
      // 围观画面和它的 run 属于上一个会话，切过去就必须清掉，否则会拿旧截图冒充当前会话。
      this.previewUrl = "";
      this.previewRun = "";
      this.activeTurn = this.activeTurns[this.currentSession] || "";
      if (result?.active_turn_snapshot) this.restoreActiveTurn(result.active_turn_snapshot);
      void this.syncApprovals();
    },
    /** 主动对齐未决审批：重连整态与会话切换都不会重播 permission_request 事件。 */
    async syncApprovals() {
      const shim = this.shim;
      if (!shim) return;
      try {
        const result = await shim.rpc("get_active_permission", []) as
          { ok?: boolean; permission?: { request_id?: string; id?: string; summary?: string; title?: string; path?: string } | null } | undefined;
        if (this.shim !== shim || !result || result.ok === false) return;
        const permission = result.permission;
        if (!permission) return;
        const id = String(permission.request_id || permission.id || "");
        if (!id || this.approvals.some((item) => item.id === id)) return;
        this.approvals.push({
          id,
          title: String(permission.title || permission.summary || "需要确认"),
          path: String(permission.path || ""),
        });
      } catch {
        // 拉不到就保持现状：卡片仍在列表里，用户可以重试或直接回对话。
        return;
      }
    },
    async respondApproval(id: string, allow: boolean) {
      if (!id || this.answering.includes(id)) return;
      const shim = this.shim;
      this.answering.push(id);
      try {
        const result = await shim?.rpc("respond_permission", { request_id: id, allow }) as { ok?: boolean; msg?: string } | undefined;
        // 服务端明确拒绝（请求已失效/已在别处处理）时不能把卡片抹掉，否则用户以为批过了。
        if (result && result.ok === false) throw new Error(result.msg || "这条确认已经失效，请刷新后再试");
        this.approvals = this.approvals.filter((item) => item.id !== id);
      } finally {
        this.answering = this.answering.filter((item) => item !== id);
      }
    },
    async stop() {
      const shim = this.shim;
      if (!shim) throw new Error("还没有接上星瑶");
      const result = await shim.rpc("stop_current_turn", { session_name: this.currentSession || undefined }) as
        { ok?: boolean; msg?: string } | undefined;
      if (result && result.ok === false) throw new Error(result.msg || "暂时无法停止，请检查连接");
      // 回合已停：清掉本地活动回合，之后迟到的增量会被当作旧回合丢掉。
      if (this.currentSession) this.activeTurns[this.currentSession] = "";
      this.activeTurn = "";
      this.status = "已停下";
    },
    async stopWatching() {
      if (!this.previewRun) return;
      await this.shim?.rpc("stop_computer_control", { run_id: this.previewRun });
    },
    disconnect() {
      this.shim?.close();
      this.shim = null;
      this.messages = [];
      this.pendingUsers = [];
      this.sessions = [];
      this.currentSession = "";
      this.activeTurn = "";
      this.activeTurns = {};
      this.approvals = [];
      this.answering = [];
      this.previewUrl = "";
      this.previewRun = "";
      this.models = [];
      this.activeModelId = "";
      this.activeModelName = "";
      this.modelStatus = "idle";
      this.modelError = "";
      this.status = "未连接";
    },
  },
});
