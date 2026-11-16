// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { renderMd } from "./md";

describe("renderMd", () => {
  it("strips dangerous markup", () => {
    const html = renderMd('<script>alert(1)</script><img src=x onerror="alert(2)">');
    expect(html).not.toContain("<script");
    expect(html).not.toContain("onerror");
  });

  it("renders headings, bold and inline code", () => {
    const html = renderMd("## 系统与硬件\n\n**已安装**：`python 3.12`");
    expect(html).toContain("<h2");
    expect(html).toContain("<strong>");
    expect(html).toContain("<code>");
  });

  it("renders GFM tables", () => {
    const html = renderMd("| 项目 | 值 |\n| --- | --- |\n| CPU | 4 核 |");
    expect(html).toContain("<table>");
    expect(html).toContain("<th>");
    expect(html).toContain("4 核");
  });

  it("renders fenced code blocks and lists", () => {
    const html = renderMd("```python\nprint(1)\n```\n\n- 第一条\n- 第二条");
    expect(html).toContain("<pre>");
    expect(html).toContain("<li>");
  });

  it("keeps single newlines as line breaks", () => {
    expect(renderMd("第一行\n第二行")).toContain("<br>");
  });
});
