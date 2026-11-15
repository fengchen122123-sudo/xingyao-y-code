import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { PreviewLoader } from "./preview";
import { useAuthStore } from "../stores/auth";

describe("PreviewLoader", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    const auth = useAuthStore();
    auth.server = "https://example.test";
    auth.credential = "jwt";
  });

  it("loads an authorized blob, revokes replaced URLs and ignores stale responses", async () => {
    let resolveFirst!: (value: { ok: boolean; status: number; blob: () => Promise<Blob> }) => void;
    const first = new Promise<{ ok: boolean; status: number; blob: () => Promise<Blob> }>((resolve) => { resolveFirst = resolve; });
    const fetchMock = vi.fn().mockReturnValueOnce(first).mockResolvedValueOnce({ ok: true, status: 200, blob: async () => new Blob(["new"]) });
    vi.stubGlobal("fetch", fetchMock);
    const create = vi.spyOn(URL, "createObjectURL").mockReturnValueOnce("blob:second").mockReturnValueOnce("blob:first");
    const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    const loader = new PreviewLoader("https://example.test", () => "jwt");
    const loadingFirst = loader.load("/one");
    const loadingSecond = loader.load("/two");
    expect(await loadingSecond).toBe("blob:second");
    resolveFirst({ ok: true, status: 200, blob: async () => new Blob(["old"]) });
    expect(await loadingFirst).toBe("");
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe("Bearer jwt");
    expect(loader.url).toBe("blob:second");
    loader.dispose();
    expect(revoke).toHaveBeenCalledWith("blob:second");
    expect(create).toHaveBeenCalledTimes(2);
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("returns visible errors for failed previews", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 404 }));
    const loader = new PreviewLoader("https://example.test", () => "jwt");
    await expect(loader.load("/missing")).rejects.toThrow("404");
    loader.dispose();
    vi.unstubAllGlobals();
  });
});
