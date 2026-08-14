import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { YcodeSidebar } from "./YcodeSidebar";

afterEach(cleanup);

describe("YcodeSidebar", () => {
	it("宽态渲染 logo、菜单、会话区标题、底部按钮", () => {
		render(<YcodeSidebar wide={true} />);
		expect(screen.getByText("Y-CODE")).toBeTruthy();
		expect(screen.getByText("新会话")).toBeTruthy();
		expect(screen.getByText("技能 Skills")).toBeTruthy();
		expect(screen.getByText("MCP 服务")).toBeTruthy();
		expect(screen.getByText("插件")).toBeTruthy();
		expect(screen.getByText("项目会话")).toBeTruthy();
		expect(screen.getByText("设置")).toBeTruthy();
	});

	it("点击新会话按钮触发 startSession", () => {
		let called = false;
		render(<YcodeSidebar wide={true} startSession={() => { called = true; }} />);
		screen.getByText("新会话").closest("button")!.click();
		expect(called).toBe(true);
	});

	it("有 renderSlot 时委托渲染 sidebar.workspaces 子槽", () => {
		const renders: string[] = [];
		render(
			<YcodeSidebar
				wide={true}
				renderSlot={(key) => {
					renders.push(key);
					return <div data-testid="ws-slot">会话列表占位</div>;
				}}
			/>,
		);
		expect(renders).toContain("sidebar.workspaces");
		expect(screen.getByTestId("ws-slot")).toBeTruthy();
	});

	it("无 renderSlot 时显示空态文案", () => {
		render(<YcodeSidebar wide={true} />);
		expect(screen.getByText("暂无历史会话")).toBeTruthy();
	});

	it("窄态渲染图标栏（56px），含展开按钮", () => {
		let expanded = false;
		render(<YcodeSidebar wide={false} expandSidebar={() => { expanded = true; }} />);
		expect(screen.queryByText("新会话")).toBeNull();
		const expandBtn = screen.getByTitle("展开侧栏");
		expandBtn.click();
		expect(expanded).toBe(true);
	});
});
