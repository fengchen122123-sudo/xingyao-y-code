/**
 * PlanPanel 单元测试。
 * 全部使用假 useProjection，不依赖真实 DSH。
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { PlanPanel, planProgress, type TodoItem } from "./PlanPanel";

afterEach(cleanup);

describe("planProgress", () => {
	it("空数组返回 0", () => {
		expect(planProgress([])).toBe(0);
	});

	it("半完成返回 50", () => {
		const todos: TodoItem[] = [
			{ content: "a", status: "completed" },
			{ content: "b", status: "pending" },
		];
		expect(planProgress(todos)).toBe(50);
	});

	it("全完成返回 100", () => {
		const todos: TodoItem[] = [
			{ content: "a", status: "completed" },
			{ content: "b", status: "completed" },
		];
		expect(planProgress(todos)).toBe(100);
	});
});

function fakeUseProjection(todos: TodoItem[]) {
	return (key: string) => (key === "todos" ? todos : undefined);
}

describe("PlanPanel 组件", () => {
	it("渲染进度百分比与状态图标", () => {
		const todos: TodoItem[] = [
			{ content: "梳理架构", status: "completed" },
			{ content: "实现面板", status: "in_progress" },
			{ content: "编写测试", status: "pending" },
		];
		const { container } = render(
			<PlanPanel useProjection={fakeUseProjection(todos)} />,
		);
		// 1/3 完成 → 33%
		expect(screen.getByText("33%")).toBeInTheDocument();
		expect(screen.getByText("任务计划")).toBeInTheDocument();
		// 状态图标
		expect(screen.getByText("✓")).toBeInTheDocument();
		expect(screen.getByText("⟳")).toBeInTheDocument();
		expect(screen.getByText("○")).toBeInTheDocument();
		// todo 内容
		expect(screen.getByText("梳理架构")).toBeInTheDocument();
		expect(screen.getByText("实现面板")).toBeInTheDocument();
		expect(screen.getByText("编写测试")).toBeInTheDocument();
		// 进度条宽度
		const fill = container.querySelector(".yc-plan-bar-fill") as HTMLElement;
		expect(fill.style.width).toBe("33%");
		// in_progress 卡片带 accent 边框类
		const doing = container.querySelector(".yc-plan-item.yc-doing");
		expect(doing).not.toBeNull();
	});

	it("空数组渲染空态文案", () => {
		render(<PlanPanel useProjection={fakeUseProjection([])} />);
		expect(
			screen.getByText("暂无活动计划。当星瑶编排复杂大任务时，计划全景会自动在此呈现。"),
		).toBeInTheDocument();
		expect(screen.getByText("0%")).toBeInTheDocument();
	});

	it("缺省 useProjection 时容错为空数组（空态）", () => {
		render(<PlanPanel />);
		expect(
			screen.getByText("暂无活动计划。当星瑶编排复杂大任务时，计划全景会自动在此呈现。"),
		).toBeInTheDocument();
	});

	it("useProjection 返回 undefined 时容错为空数组（空态）", () => {
		render(<PlanPanel useProjection={() => undefined} />);
		expect(
			screen.getByText("暂无活动计划。当星瑶编排复杂大任务时，计划全景会自动在此呈现。"),
		).toBeInTheDocument();
	});
});
