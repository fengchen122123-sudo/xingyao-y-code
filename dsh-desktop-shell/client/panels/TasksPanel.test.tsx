/**
 * TasksPanel 单元测试。
 * 全部使用假 useSessions，不依赖真实 DSH。
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import {
	TasksPanel,
	formatElapsed,
	statusLabel,
	type JobView,
} from "./TasksPanel";

afterEach(cleanup);

describe("formatElapsed", () => {
	it("35 秒 → 35s", () => {
		const job: JobView = { id: "1", kind: "bash", label: "a", status: "running", startedAt: 1000 };
		expect(formatElapsed(job, 36000)).toBe("35s");
	});

	it("125 秒 → 2m5s", () => {
		const job: JobView = { id: "1", kind: "bash", label: "a", status: "running", startedAt: 0 };
		expect(formatElapsed(job, 125000)).toBe("2m5s");
	});

	it("有 finishedAt 时用 finishedAt", () => {
		const job: JobView = {
			id: "1",
			kind: "bash",
			label: "a",
			status: "completed",
			startedAt: 0,
			finishedAt: 65000,
		};
		expect(formatElapsed(job, 999999999)).toBe("1m5s");
	});
});

describe("statusLabel", () => {
	it("五态映射", () => {
		expect(statusLabel("running")).toBe("运行中");
		expect(statusLabel("stopping")).toBe("停止中");
		expect(statusLabel("completed")).toBe("已完成");
		expect(statusLabel("killed")).toBe("已终止");
		expect(statusLabel("failed")).toBe("失败");
	});
});

function fakeUseSessions(jobs: JobView[]) {
	return (sel: (s: any) => any) =>
		sel({ current: "s1", jobsBySession: { s1: jobs } });
}

describe("TasksPanel 组件", () => {
	it("渲染任务卡片：running 有终止按钮，completed 无", () => {
		const jobs: JobView[] = [
			{ id: "j1", kind: "bash", label: "npm run build", status: "running", startedAt: 0 },
			{
				id: "j2",
				kind: "bash",
				label: "npm test",
				status: "completed",
				startedAt: 0,
				finishedAt: 5000,
			},
		];
		render(<TasksPanel useSessions={fakeUseSessions(jobs)} />);
		expect(screen.getByText("后台运行任务")).toBeInTheDocument();
		expect(screen.getByText("npm run build")).toBeInTheDocument();
		expect(screen.getByText("npm test")).toBeInTheDocument();
		expect(screen.getByText("运行中")).toBeInTheDocument();
		expect(screen.getByText("已完成")).toBeInTheDocument();
		// 仅 running 的任务显示终止按钮
		expect(screen.getAllByText("终止")).toHaveLength(1);
	});

	it("空 jobs 渲染空态文案", () => {
		render(<TasksPanel useSessions={fakeUseSessions([])} />);
		expect(screen.getByText("暂无后台活跃任务")).toBeInTheDocument();
	});

	it("缺省 useSessions 时容错为空态", () => {
		render(<TasksPanel />);
		expect(screen.getByText("暂无后台活跃任务")).toBeInTheDocument();
	});
});
