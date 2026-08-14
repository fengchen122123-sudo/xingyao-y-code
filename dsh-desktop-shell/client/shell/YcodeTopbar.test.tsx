import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { YcodeTopbar } from "./YcodeTopbar";

afterEach(cleanup);

describe("YcodeTopbar", () => {
	it("渲染 logo、三个控制按钮", () => {
		render(<YcodeTopbar />);
		expect(screen.getByText("Y-CODE")).toBeTruthy();
		expect(screen.getByTitle("切换侧栏")).toBeTruthy();
		expect(screen.getByTitle("切换主题")).toBeTruthy();
		expect(screen.getByTitle("切换右栏")).toBeTruthy();
	});

	it("点击切换主题按钮调用 onToggleTheme 并切换图标", () => {
		let next: string | undefined;
		render(<YcodeTopbar onToggleTheme={(t) => { next = t; }} />);
		const btn = screen.getByTitle("切换主题");
		// 初始 dark → 点击后应为 light
		expect(btn.textContent).toContain("☾");
		fireEvent.click(btn);
		expect(next).toBe("light");
		expect(btn.textContent).toContain("☀");
		fireEvent.click(btn);
		expect(next).toBe("dark");
		expect(btn.textContent).toContain("☾");
	});

	it("点击切换侧栏按钮调用 toggleSidebar", () => {
		let called = false;
		render(<YcodeTopbar toggleSidebar={() => { called = true; }} />);
		fireEvent.click(screen.getByTitle("切换侧栏"));
		expect(called).toBe(true);
	});

	it("点击切换右栏按钮交替调用 closeDetails / openDetails", () => {
		const calls: string[] = [];
		render(
			<YcodeTopbar
				openDetails={() => calls.push("open")}
				closeDetails={() => calls.push("close")}
			/>,
		);
		const btn = screen.getByTitle("切换右栏");
		fireEvent.click(btn); // 初始 open → close
		expect(calls).toEqual(["close"]);
		fireEvent.click(btn); // close → open
		expect(calls).toEqual(["close", "open"]);
	});

	it("挂载时注入 AppFrame 让位 CSS", () => {
		render(<YcodeTopbar />);
		expect(document.getElementById("yc-topbar-offset")).toBeTruthy();
	});
});
