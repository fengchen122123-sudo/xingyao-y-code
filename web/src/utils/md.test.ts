import { describe, expect, it } from "vitest";
import { renderMd } from "./md";

describe("renderMd", () => {
  it("shows markup as text", () => {
    const html = renderMd("<script>alert(1)</script>");
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });
});
