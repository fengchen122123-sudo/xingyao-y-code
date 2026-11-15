import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { apiFetch } from "./api";
import { useAuthStore } from "../stores/auth";

vi.mock("../router", () => ({ default: { push: vi.fn().mockResolvedValue(undefined) } }));
vi.mock("./secrets", () => ({
  secretGet: vi.fn().mockResolvedValue("refresh"),
  secretSet: vi.fn(),
  secretRemove: vi.fn(),
  secretClear: vi.fn().mockResolvedValue(undefined),
  serverAddressGet: vi.fn().mockResolvedValue(""),
  serverAddressSet: vi.fn().mockResolvedValue(undefined),
  serverAddressRemove: vi.fn().mockResolvedValue(undefined),
}));

describe("authorized REST requests", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("shares one refresh and retries concurrent 401 responses with the new credential", async () => {
    const auth = useAuthStore();
    auth.server = "https://example.test";
    auth.credential = "old";
    const refresh = vi.spyOn(auth, "refresh").mockImplementation(async () => { auth.credential = "new"; return true; });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ status: 401, ok: false })
      .mockResolvedValueOnce({ status: 401, ok: false })
      .mockResolvedValueOnce({ status: 200, ok: true })
      .mockResolvedValueOnce({ status: 200, ok: true });
    vi.stubGlobal("fetch", fetchMock);
    await Promise.all([apiFetch("/api/devices"), apiFetch("/preview/1")]);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(fetchMock.mock.calls.slice(2).every((call) => call[1].headers.Authorization === "Bearer new")).toBe(true);
    vi.unstubAllGlobals();
  });

  it("does not loop when the retry is also unauthorized", async () => {
    const auth = useAuthStore();
    auth.server = "https://example.test";
    auth.credential = "";
    vi.spyOn(auth, "refresh").mockResolvedValue(false);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ status: 401, ok: false }));
    const response = await apiFetch("/api/devices");
    expect(response.status).toBe(401);
    expect(fetch).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });
});
