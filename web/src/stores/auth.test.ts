import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useAppStore } from "./app";
import { useAuthStore } from "./auth";
import { useDeviceStore } from "./devices";
import { secretGet, secretClear, secretSet, serverAddressGet, serverAddressRemove, serverAddressSet } from "../transport/secrets";

vi.mock("../transport/secrets", () => ({
  secretGet: vi.fn().mockResolvedValue("refresh"),
  secretSet: vi.fn(),
  secretRemove: vi.fn().mockResolvedValue(undefined),
  secretClear: vi.fn().mockResolvedValue(undefined),
  serverAddressGet: vi.fn().mockResolvedValue(""),
  serverAddressSet: vi.fn().mockResolvedValue(undefined),
  serverAddressRemove: vi.fn().mockResolvedValue(undefined),
}));

describe("logout", () => {
  beforeEach(() => setActivePinia(createPinia()));
  it("closes both sockets and clears session and preview state", async () => {
    const auth = useAuthStore();
    const app = useAppStore();
    const devices = useDeviceStore();
    auth.server = "https://example.test";
    auth.accountId = "a";
    auth.credential = "jwt";
    app.currentSession = "s";
    app.previewUrl = "/preview";
    const close = vi.fn();
    app.shim = { close } as never;
    devices.socket = { close } as never;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true }));
    await auth.logout();
    expect(close).toHaveBeenCalledTimes(2);
    expect(app.currentSession).toBe("");
    expect(app.previewUrl).toBe("");
    expect(devices.socket).toBeNull();
    expect(auth.credential).toBe("");
    expect(auth.server).toBe("");
    // 退出时凭据和服务器地址都要清：Keystore 里不能留旧账号的东西。
    expect(vi.mocked(secretClear)).toHaveBeenCalled();
    expect(vi.mocked(serverAddressRemove)).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});

describe("hydrate", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("takes the server address from preferences and the credentials from the keystore", async () => {
    vi.mocked(serverAddressGet).mockResolvedValue("https://phone.example");
    const stored: Record<string, string> = {
      "yc-session-account": "u1",
      "yc-session-credential": "jwt-1",
      "yc-session-role": "admin",
      "yc-refresh": "r1",
    };
    vi.mocked(secretGet).mockImplementation(async (key: string) => stored[key] || "");
    const auth = useAuthStore();
    await auth.hydrate();
    expect(auth.server).toBe("https://phone.example");
    expect(auth.accountId).toBe("u1");
    expect(auth.credential).toBe("jwt-1");
    expect(auth.role).toBe("admin");
    expect(auth.signedIn).toBe(true);
  });

  it("stays signed out when the keystore lost the access token", async () => {
    vi.mocked(serverAddressGet).mockResolvedValue("https://phone.example");
    vi.mocked(secretGet).mockResolvedValue("");
    const auth = useAuthStore();
    await auth.hydrate();
    expect(auth.server).toBe("https://phone.example");
    expect(auth.credential).toBe("");
    expect(auth.signedIn).toBe(false);
  });
});

describe("login and refresh", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.mocked(secretSet).mockClear();
    vi.mocked(secretGet).mockResolvedValue("refresh");
  });

  it("remembers the access token, refresh token and role after a successful login", async () => {
    const auth = useAuthStore();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ access_token: "a1", refresh_token: "r1", role: "admin" }),
    }));
    await auth.login("https://example.test", "user", "pw");
    expect(auth.credential).toBe("a1");
    expect(auth.accountId).toBe("user");
    expect(auth.server).toBe("https://example.test");
    expect(auth.role).toBe("admin");
    expect(auth.signedIn).toBe(true);
    expect(vi.mocked(secretSet)).toHaveBeenCalledWith("yc-refresh", "r1");
    vi.unstubAllGlobals();
  });

  it("refuses a login answer without an access token and keeps the caller signed out", async () => {
    const auth = useAuthStore();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ role: "user" }) }));
    await expect(auth.login("https://example.test", "user", "pw")).rejects.toThrow("登录没有通过");
    expect(auth.signedIn).toBe(false);
    vi.unstubAllGlobals();
  });

  it("rotates the refresh token and reports failure so the caller can log out", async () => {
    const auth = useAuthStore();
    auth.server = "https://example.test";
    auth.credential = "a1";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ access_token: "a2", refresh_token: "r2", role: "user" }),
    }));
    expect(await auth.refresh()).toBe(true);
    expect(auth.credential).toBe("a2");
    expect(vi.mocked(secretSet)).toHaveBeenCalledWith("yc-session-credential", "a2");
    expect(vi.mocked(secretSet)).toHaveBeenCalledWith("yc-refresh", "r2");

    vi.mocked(fetch).mockResolvedValue({ ok: false, status: 401 } as never);
    expect(await auth.refresh()).toBe(false);
    expect(auth.credential).toBe("a2");
    vi.unstubAllGlobals();
  });
});
