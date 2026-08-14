/**
 * TermPanel 单元测试。
 * 全部使用假 useSessions，不依赖真实 DSH。
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { TermPanel, classifyLine, jobToLines } from "./TermPanel";

afterEach(cleanup);

describe("classifyLine", () => {
	it("'$ ' 开头 → t-cmd", () => {
		expect(classifyLine("$ npm test")).toBe("t-cmd");
	});

	it("'[进程已退出，代码 0]' → t-ok", () => {
		expect(classifyLine("[进程已退出，代码 0]")).toBe("t-ok");
	});

	it("异常/截断行 → t-warn", () => {
		expect(classifyLine("[进程已退出，代码 1]")).toBe("t-warn");
		expect(classifyLine("[进程已退出，代码 127]")).toBe("t-warn");
		expect(classifyLine("[启动失败：找不到命令]")).toBe("t-warn");
		expect(classifyLine("…[输出过长，已截断]")).toBe("t-warn");
	});

	it("普通输出 → ''", () => {
		expect(classifyLine("hello world")).toBe("");
		expect(classifyLine("[运行中…]")).toBe("");
	});
});

describe("jobToLines", () => {
	it("running → 命令行 + [运行中…]", () => {
		expect(jobToLines({ kind: "bash", label: "npm run build", status: "running" })).toEqual([
			"$ npm run build",
			"[运行中…]",
		]);
	});

	it("completed → 代码 0（默认）", () => {
		expect(jobToLines({ kind: "pwsh", label: "ls", status: "completed" })).toEqual([
			"$ ls",
			"[进程已退出，代码 0]",
		]);
	});

	it("completed 且 detail 含 exit code → 用之", () => {
		expect(
			jobToLines({ kind: "bash", label: "t", status: "completed", detail: "exit code 0" }),
		).toEqual(["$ t", "[进程已退出，代码 0]"]);
	});

	it("failed → detail 中的退出码", () => {
		expect(
			jobToLines({ kind: "bash", label: "make", status: "failed", detail: "exit code 2" }),
		).toEqual(["$ make", "[进程已退出，代码 2]"]);
	});

	it("killed 且无 detail → 代码 1", () => {
		expect(jobToLines({ kind: "bash", label: "sleep 99", status: "killed" })).toEqual([
			"$ sleep 99",
			"[进程已退出，代码 1]",
		]);
	});
});

interface FakeJob {
	id: string;
	kind: string;
	label: string;
	status: string;
	detail?: string;
	startedAt: number;
	finishedAt?: number;
}

function fakeUseSessions(jobs: FakeJob[]) {
	return (sel: (s: any) => any) => sel({ current: "s1", jobsBySession: { s1: jobs } });
}

describe("TermPanel 组件", () => {
	it("只显示 bash/pwsh 类 job，命令带 $ 前缀，退出行着色类正确", () => {
		const jobs: FakeJob[] = [
			{ id: "j1", kind: "bash", label: "npm test", status: "completed", startedAt: 0 },
			{
				id: "j2",
				kind: "pwsh",
				label: "bad-cmd",
				status: "failed",
				detail: "exit code 127",
				startedAt: 0,
			},
			{ id: "j3", kind: "file-watch", label: "watch src", status: "running", startedAt: 0 },
		];
		const { container } = render(<TermPanel useSessions={fakeUseSessions(jobs)} />);
		expect(screen.getByText("终端")).toBeInTheDocument();
		// 终端类 job 显示，非终端类不显示
		expect(screen.getByText("$ npm test")).toBeInTheDocument();
		expect(screen.getByText("$ bad-cmd")).toBeInTheDocument();
		expect(screen.queryByText("$ watch src")).not.toBeInTheDocument();
		// 着色类：命令行 t-cmd、退出 0 → t-ok、退出 127 → t-warn
		expect(screen.getByText("$ npm test").className).toContain("t-cmd");
		expect(screen.getByText("[进程已退出，代码 0]").className).toContain("t-ok");
		expect(screen.getByText("[进程已退出，代码 127]").className).toContain("t-warn");
		expect(container.querySelectorAll(".yc-term-line")).toHaveLength(4);
	});

	it("空 jobs 显示就绪提示（t-cmd 色）", () => {
		render(<TermPanel useSessions={fakeUseSessions([])} />);
		const hint = screen.getByText("$ # 终端已准备就绪，支持执行 Shell 命令");
		expect(hint).toBeInTheDocument();
		expect(hint.className).toContain("t-cmd");
	});

	it("缺省 useSessions 时容错为空态", () => {
		render(<TermPanel />);
		expect(screen.getByText("$ # 终端已准备就绪，支持执行 Shell 命令")).toBeInTheDocument();
	});

	it("输入框 disabled 且 placeholder 诚实说明", () => {
		render(<TermPanel useSessions={fakeUseSessions([])} />);
		const input = screen.getByPlaceholderText("终端执行由对话内工具完成");
		expect(input).toBeDisabled();
	});
});
