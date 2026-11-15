import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useAppStore } from "./app";
import { useAuthStore } from "./auth";

vi.stubGlobal("window", { sessionStorage: { getItem: vi.fn(), setItem: vi.fn(), removeItem: vi.fn() } });

const shims: MockShim[] = [];
class MockShim {
  /** 允许单个用例把「何时就绪」握在手里，验证连接状态确实跟着就绪翻转。 */
  static readyFactory: (() => Promise<void>) | null = null;
  listener: ((frame: { type: string; data?: unknown }) => void) | null = null;
  closed = false;
  lastSeq = 0;
  bootId = "";
  rpc = vi.fn().mockResolvedValue({ messages: [] });
  whenReady = vi.fn(() => (MockShim.readyFactory ? MockShim.readyFactory() : Promise.resolve()));
  constructor(_options: unknown) { shims.push(this); }
  onEvent(listener: (frame: { type: string; data?: unknown }) => void) { this.listener = listener; }
  connect() {}
  close() { this.closed = true; }
}
vi.mock("../transport/gateway-shim", () => ({ GatewayShim: class { constructor(options: unknown) { return new MockShim(options); } } }));

function signedInAuth() {
  const auth = useAuthStore();
  auth.server = "https://example.test";
  auth.credential = "jwt";
  return auth;
}

describe("app connection and event isolation", () => {
  beforeEach(() => { shims.length = 0; MockShim.readyFactory = null; setActivePinia(createPinia()); });

  it("reuses the current target and closes the old shim only for a different target", () => {
    const app = useAppStore();
    signedInAuth();
    app.connect();
    app.connect();
    expect(shims).toHaveLength(1);
    expect(app.profile).toBe("agent");
    expect(app.connectionTarget).toBe("云端 Agent");

    app.connectDesktop("device-1");
    expect(shims).toHaveLength(2);
    expect(shims[0].closed).toBe(true);
    expect(app.shim).toStrictEqual(shims[1]);
    shims[0].listener?.({ type: "worker_starting" });
    expect(app.status).not.toBe("正在启动");
  });

  it("keeps the selected desktop target when chat reconnects and does not duplicate it", () => {
    const app = useAppStore();
    signedInAuth();
    app.connectDesktop("device-1");
    app.connect();
    app.connectDesktop("device-1");
    expect(shims).toHaveLength(1);
    expect(shims[0].closed).toBe(false);
    expect(app.profile).toBe("desktop");
    expect(app.deviceId).toBe("device-1");
    expect(app.connectionTarget).toBe("电脑 device-1");

    app.disconnect();
    app.connect();
    expect(shims).toHaveLength(2);
    expect(app.profile).toBe("desktop");
    expect(app.deviceId).toBe("device-1");
  });

  it("filters sessions and stale turns and restores the current active snapshot", () => {
    const app = useAppStore();
    app.currentSession = "current";
    app.apply({ type: "turn_start", data: { session_name: "other", turn_id: "other-1", user_text: "no" } });
    expect(app.messages).toEqual([]);
    app.apply({ type: "turn_start", data: { session_name: "current", turn_id: "turn-1", user_text: "yes" } });
    app.apply({ type: "token_delta", data: { session_name: "current", turn_id: "old-turn", text: "stale" } });
    app.apply({ type: "token_delta", data: { session_name: "current", turn_id: "turn-1", text: "live" } });
    app.apply({ type: "message_final", data: { session_name: "current", turn_id: "turn-1", content: "done" } });
    app.apply({ type: "token_delta", data: { session_name: "current", turn_id: "turn-1", text: "late" } });
    expect(app.messages.at(-1)?.text).toBe("done");

    app.replaceSnapshot({
      sessions: [], current_session: "current", messages: [],
      active_model_id: "model-1", active_model_name: "模型一",
      active_turn_snapshots: {
        other: { streaming_text: "wrong" },
        current: { turn_id: "turn-2", display_text: "working", thinking_text: "think", streaming_text: "partial", tool_calls: [{ id: "t", label: "tool", result: "ok" }] },
      },
    });
    expect(app.messages.map((message) => [message.role, message.text])).toEqual([["user", "working"], ["agent", "partial"]]);
    expect(app.messages[1].thinking).toBe("think");
    expect(app.messages[1].tools).toEqual([{ id: "t", title: "tool", detail: "ok" }]);
    expect(app.activeModelId).toBe("model-1");
    expect(app.activeModelName).toBe("模型一");
  });

  it("synchronizes the selected model after session switches and model_changed events", async () => {
    const app = useAppStore();
    signedInAuth();
    app.connect();
    shims[0].rpc.mockResolvedValue({
      messages: [], session_name: "work", active_model_id: "model-2", active_model_name: "模型二",
    });
    await app.chooseSession("work");
    expect(app.activeModelId).toBe("model-2");
    expect(app.activeModelName).toBe("模型二");

    app.apply({ type: "model_changed", data: { session_name: "work", active_model_id: "model-3", active_model_name: "模型三" } });
    expect(app.activeModelId).toBe("model-3");
    expect(app.activeModelName).toBe("模型三");
  });

  it("waits for readiness before loading models and keeps failure distinct from an empty retry result", async () => {
    const app = useAppStore();
    signedInAuth();
    app.connect();
    let releaseReady!: () => void;
    shims[0].whenReady.mockReturnValueOnce(new Promise<void>((resolve) => { releaseReady = resolve; }));
    // 连接就绪后的初始整态兜底请求先消耗一次响应，再轮到 loadModels。
    shims[0].rpc.mockResolvedValueOnce({ current_session: "", sessions: [], messages: [] });
    shims[0].rpc.mockRejectedValueOnce(new Error("模型服务暂不可用"));
    const loading = app.loadModels();
    expect(shims[0].rpc).not.toHaveBeenCalled();
    releaseReady();
    await loading;
    expect(shims[0].rpc).toHaveBeenCalledWith("get_initial_state", []);
    expect(app.modelStatus).toBe("error");
    expect(app.modelError).toBe("模型服务暂不可用");

    shims[0].rpc.mockResolvedValueOnce({ models: [], active_model_id: "", active_model_name: "" });
    await app.loadModels();
    expect(app.modelStatus).toBe("ready");
    expect(app.modelError).toBe("");
    expect(app.models).toEqual([]);
  });

  it("clears all session and preview state on disconnect", () => {
    const app = useAppStore();
    signedInAuth();
    app.connect();
    app.sessions = [{ name: "s" }];
    app.currentSession = "s";
    app.previewUrl = "/preview";
    app.previewRun = "run";
    app.apply({ type: "turn_start", data: { session_name: "s", turn_id: "t" } });
    app.disconnect();
    expect(shims[0].closed).toBe(true);
    expect(app.shim).toBeNull();
    expect(app.sessions).toEqual([]);
    expect(app.currentSession).toBe("");
    expect(app.messages).toEqual([]);
    expect(app.approvals).toEqual([]);
    expect(app.previewUrl).toBe("");
    expect(app.previewRun).toBe("");
  });

  it("claims the optimistic user bubble instead of duplicating a turn echo", async () => {
    const app = useAppStore();
    signedInAuth();
    app.connect();
    app.currentSession = "s";
    shims[0].rpc.mockResolvedValueOnce({ ok: true });
    await app.send("你多大了");
    expect(app.messages.filter((item) => item.role === "user")).toHaveLength(1);
    app.apply({ type: "turn_start", data: { session_name: "s", turn_id: "t1", user_text: "你多大了\n" } });
    expect(app.messages.filter((item) => item.role === "user")).toHaveLength(1);
    expect(app.pendingUsers).toEqual([]);
  });

  it("removes the optimistic bubble when the server rejects the send", async () => {
    const app = useAppStore();
    signedInAuth();
    app.connect();
    app.currentSession = "s";
    shims[0].rpc.mockResolvedValueOnce({ ok: false, msg: "发送失败" });
    await expect(app.send("hi")).rejects.toThrow("发送失败");
    expect(app.messages).toEqual([]);
    expect(app.pendingUsers).toEqual([]);
  });

  it("creates a real new server session and refreshes the sidebar snapshot", async () => {
    const app = useAppStore();
    signedInAuth();
    app.connect();
    shims[0].rpc.mockImplementation(async (method: string) => {
      if (method === "create_session") return { ok: true, session_name: "session_new" };
      if (method === "get_initial_state") {
        return {
          current_session: "session_new",
          sessions: [{ folder_name: "对话", sessions: [{ name: "session_new" }] }],
          messages: [],
        };
      }
      return { ok: true };
    });
    await app.newSession();
    expect(shims[0].rpc).toHaveBeenCalledWith("create_session", { title: "新对话", workdir: "GLOBAL" });
    expect(app.currentSession).toBe("session_new");
    expect(app.sessions).toEqual([{ name: "session_new" }]);
    expect(app.messages).toEqual([]);
  });

  it("reports connected once the transport is really ready", async () => {
    const app = useAppStore();
    signedInAuth();
    app.connect();
    expect(app.status).toBe("正在连接");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(app.status).toBe("已连接");
    expect(shims[0].whenReady).toHaveBeenCalled();
  });

  it("surfaces rejected sends and cancelled turns instead of pretending success", async () => {
    const app = useAppStore();
    signedInAuth();
    app.connect();
    app.currentSession = "s";
    shims[0].rpc.mockResolvedValueOnce({ ok: false, msg: "消息没有被接受" });
    await expect(app.send("hi")).rejects.toThrow("消息没有被接受");
    shims[0].rpc.mockResolvedValueOnce({ ok: true });
    await app.send("hi");

    app.apply({ type: "turn_start", data: { session_name: "s", turn_id: "t1" } });
    shims[0].rpc.mockResolvedValueOnce({ ok: true, idle: false });
    await app.stop();
    expect(shims[0].rpc).toHaveBeenLastCalledWith("stop_current_turn", { session_name: "s" });
    expect(app.activeTurn).toBe("");
    app.apply({ type: "token_delta", data: { session_name: "s", turn_id: "t1", text: "late" } });
    expect(app.messages.at(-1)?.text).toBe("");

    app.apply({ type: "turn_start", data: { session_name: "s", turn_id: "t2" } });
    const statusBefore = app.status;
    shims[0].rpc.mockResolvedValueOnce({ ok: false, msg: "该会话没有在跑的回合" });
    await expect(app.stop()).rejects.toThrow("没有在跑的回合");
    expect(app.activeTurn).toBe("t2");
    expect(app.status).toBe(statusBefore);
  });

  it("deduplicates replayed permission requests and keeps the card when the bridge refuses the answer", async () => {
    const app = useAppStore();
    signedInAuth();
    app.connect();
    app.apply({ type: "permission_request", data: { request_id: "r1", summary: "写入文件" } });
    app.apply({ type: "permission_request", data: { request_id: "r1", summary: "写入文件" } });
    expect(app.approvals).toEqual([{ id: "r1", title: "写入文件", path: "" }]);

    shims[0].rpc.mockResolvedValueOnce({ ok: false, msg: "该权限请求已失效或已被处理" });
    await expect(app.respondApproval("r1", true)).rejects.toThrow("已失效");
    expect(app.approvals).toHaveLength(1);

    shims[0].rpc.mockResolvedValue({ ok: true });
    const before = shims[0].rpc.mock.calls.length;
    await Promise.all([app.respondApproval("r1", false), app.respondApproval("r1", false)]);
    expect(shims[0].rpc.mock.calls.length - before).toBe(1);
    expect(shims[0].rpc).toHaveBeenLastCalledWith("respond_permission", { request_id: "r1", allow: false });
    expect(app.approvals).toEqual([]);
  });

  it("clears the watch panel and refills pending approvals when the session switches", async () => {
    const app = useAppStore();
    signedInAuth();
    app.connect();
    app.currentSession = "old";
    app.previewUrl = "/computer-preview/1";
    app.previewRun = "run-1";
    shims[0].rpc.mockImplementation(async (method: string) => {
      if (method === "switch_session") return { ok: true, session_name: "work", messages: [] };
      if (method === "get_active_permission") return { ok: true, permission: { request_id: "r9", summary: "运行命令" } };
      return { ok: true };
    });
    await app.chooseSession("work");
    expect(app.previewUrl).toBe("");
    expect(app.previewRun).toBe("");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(app.approvals.map((item) => item.id)).toContain("r9");

    shims[0].rpc.mockResolvedValueOnce({ ok: false, msg: "会话不存在" });
    app.previewUrl = "/computer-preview/2";
    await expect(app.chooseSession("gone")).rejects.toThrow("会话不存在");
    expect(app.currentSession).toBe("work");
    expect(app.previewUrl).toBe("/computer-preview/2");
  });
});
