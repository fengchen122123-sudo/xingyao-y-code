import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { YcodeDetailsPanel } from "./YcodeDetailsPanel";

afterEach(cleanup);

describe("YcodeDetailsPanel", () => {
	it("渲染 计划/任务/终端/文件 四个标签，默认激活计划", () => {
		render(<YcodeDetailsPanel />);
		for (const label of ["计划", "任务", "终端", "文件"]) {
			expect(screen.getByRole("button", { name: label })).toBeTruthy();
		}
		const plan = screen.getByRole("button", { name: "计划" });
		expect(plan.getAttribute("aria-current")).toBe("true");
	});

	it("点击切换到任务标签", async () => {
		render(<YcodeDetailsPanel />);
		const tasks = screen.getByRole("button", { name: "任务" });
		await userEvent.click(tasks);
		expect(tasks.getAttribute("aria-current")).toBe("true");
		expect(screen.getByText("暂无后台活跃任务")).toBeTruthy();
	});

	it("hasPlan 时计划标签显示徽标点", () => {
		render(<YcodeDetailsPanel hasPlan />);
		expect(screen.getByText("●")).toBeTruthy();
	});
});

describe("YcodeDetailsPanel 框架接线（透传 props 到子面板）", () => {
	it("useProjection 透传到 PlanPanel（以 'todos' 调用）并据此显示徽标", () => {
		const useProjection = vi.fn((key: string) =>
			key === "todos"
				? [{ content: "写代码", status: "in_progress" as const }]
				: undefined,
		);
		render(<YcodeDetailsPanel useProjection={useProjection} />);
		// PlanPanel 与徽标推导都会调用 useProjection("todos")
		expect(useProjection).toHaveBeenCalledWith("todos");
		// 有未完成计划项 → 徽标出现
		expect(screen.getByText("●")).toBeTruthy();
		// PlanPanel 渲染出真实 todo 内容
		expect(screen.getByText("写代码")).toBeTruthy();
	});

	it("useSessions 透传到 TasksPanel（任务标签渲染真实 job）", async () => {
		const useSessions = vi.fn((sel: (s: any) => any) =>
			sel({
				current: "s1",
				jobsBySession: {
					s1: [
						{
							id: "j1",
							kind: "bash",
							label: "npm test",
							status: "running",
							startedAt: Date.now() - 3000,
						},
					],
				},
			}),
		);
		render(<YcodeDetailsPanel useSessions={useSessions} />);
		await userEvent.click(screen.getByRole("button", { name: "任务" }));
		expect(useSessions).toHaveBeenCalled();
		expect(screen.getByText("npm test")).toBeTruthy();
	});

	it("useSessions 透传到 TermPanel（终端标签渲染终端类 job）", async () => {
		const useSessions = vi.fn((sel: (s: any) => any) =>
			sel({
				current: "s1",
				jobsBySession: {
					s1: [
						{
							id: "j2",
							kind: "bash",
							label: "ls -la",
							status: "completed",
							detail: "exit code 0",
							startedAt: 0,
							finishedAt: 1000,
						},
					],
				},
			}),
		);
		render(<YcodeDetailsPanel useSessions={useSessions} />);
		await userEvent.click(screen.getByRole("button", { name: "终端" }));
		expect(useSessions).toHaveBeenCalled();
		expect(screen.getByText("$ ls -la")).toBeTruthy();
	});

	it("useWorkspaces/listDirectory/openPath 透传到 FilesPanel", async () => {
		const useWorkspaces = vi.fn((sel: (s: any) => any) =>
			sel({ items: [{ path: "/proj", title: "proj" }] }),
		);
		const listDirectory = vi.fn(async (_path?: string) => ({
			path: "/proj",
			home: "/home",
			crumbs: [{ name: "proj", path: "/proj", hidden: false }],
			entries: [{ name: "src", path: "/proj/src", hidden: false }],
			truncated: false,
		}));
		const openPath = vi.fn();
		render(
			<YcodeDetailsPanel
				useWorkspaces={useWorkspaces}
				listDirectory={listDirectory}
				openPath={openPath}
			/>,
		);
		await userEvent.click(screen.getByRole("button", { name: "文件" }));
		expect(useWorkspaces).toHaveBeenCalled();
		// 初始以第一个工作区 path 为根自动列目录
		expect(listDirectory).toHaveBeenCalledWith("/proj");
		expect(await screen.findByText("src")).toBeTruthy();
	});

	it("无 hook 时四个标签均为空态容错（不崩溃）", async () => {
		render(<YcodeDetailsPanel />);
		expect(screen.getByText(/暂无活动计划/)).toBeTruthy();
		await userEvent.click(screen.getByRole("button", { name: "任务" }));
		expect(screen.getByText("暂无后台活跃任务")).toBeTruthy();
		await userEvent.click(screen.getByRole("button", { name: "终端" }));
		expect(screen.getByText(/终端已准备就绪/)).toBeTruthy();
		await userEvent.click(screen.getByRole("button", { name: "文件" }));
		expect(screen.getByText(/暂未引入项目文件夹/)).toBeTruthy();
	});
});
