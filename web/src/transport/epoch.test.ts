import { describe, expect, it } from "vitest";
import { decideFrame, shouldPersistWatermark, watermarkKey } from "./epoch";

describe("epoch", () => {
  it("keeps hello and seq_reset out of the business stream", () => {
    expect(decideFrame({ type: "hello", data: { boot_id: "aa" } }, false).kind).toBe("hello");
    const reset = decideFrame(
      { type: "seq_reset", data: { boot_id: "aa", resync_required: true } },
      true,
    );
    expect(reset.kind).toBe("reset");
    expect(reset.resync).toBe(true);
  });

  it("treats a server without hello as legacy", () => {
    expect(decideFrame({ type: "notice" }, false).kind).toBe("legacy");
  });

  it("scopes the watermark and refuses to persist it on Capacitor", () => {
    expect(watermarkKey("https://a", "acc", "agent")).toBe("yc-watermark:https://a|acc|agent");
    expect(shouldPersistWatermark(true)).toBe(false);
    expect(shouldPersistWatermark(false)).toBe(true);
  });
});
