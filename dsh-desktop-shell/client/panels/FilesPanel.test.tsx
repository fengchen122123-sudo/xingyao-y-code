/**
 * FilesPanel 单元测试。
 * 全部使用假 useWorkspaces / 假 listDirectory Promise，不依赖真实 DSH。
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { FilesPanel, type DirectoryListing } from "./FilesPanel";

afterEach(cleanup);

/** 按 path 返回不同 listing 的假 listDirectory。 */
function fakeListDirectory(map: Record<string, DirectoryListing>) {
	return vi.fn((path?: string) => Promise.resolve(map[path ?? ""] ?? {
		path: path ?? "",
		home: "/root",
		crumbs: [],
		entries: [],
		truncated: false,
	}));
}

function fakeUseWorkspaces(items: any[]) {
	return (sel: (s: any) => any) => sel({ items });
}

/** render 并等待首次 listDirectory 的 Promise 落定（avoid act warning）。 */
async function renderPanel(ui: React.ReactElement) {
	let utils!: ReturnType<typeof render>;
	await act(async () => {
		utils = render(ui);
	});
	return utils;
}

const ROOT = "/proj";
const rootListing: DirectoryListing = {
	path: ROOT,
	home: ROOT,
	crumbs: [
		{ name: "proj", path: ROOT, hidden: false },
	],
	entries: [
		{ name: "src", path: "/proj/src", hidden: false },
		{ name: "docs", path: "/proj/docs", hidden: false },
		{ name: ".git", path: "/proj/.git", hidden: true },
	],
	truncated: false,
};

const srcListing: DirectoryListing = {
	path: "/proj/src",
	home: ROOT,
	crumbs: [
		{ name: "proj", path: ROOT, hidden: false },
		{ name: "src", path: "/proj/src", hidden: false },
	],
	entries: [{ name: "client", path: "/proj/src/client", hidden: false }],
	truncated: true,
};

describe("FilesPanel 组件", () => {
	it("初始以第一个工作区 path 为根，渲染面包屑与子目录；hidden 项不显示", async () => {
		const listDirectory = fakeListDirectory({ [ROOT]: rootListing });
		await renderPanel(
			<FilesPanel
				useWorkspaces={fakeUseWorkspaces([{ path: ROOT, title: "proj" }])}
				listDirectory={listDirectory}
			/>,
		);
		expect(listDirectory).toHaveBeenCalledWith(ROOT);
		expect(screen.getByText("项目文件")).toBeInTheDocument();
		expect(screen.getByText("proj")).toBeInTheDocument();
		expect(screen.getByText("src")).toBeInTheDocument();
		expect(screen.getByText("docs")).toBeInTheDocument();
		// hidden（.git）不显示
		expect(screen.queryByText(".git")).not.toBeInTheDocument();
	});

	it("点击目录行进入子目录：调用 listDirectory(子path)", async () => {
		const listDirectory = fakeListDirectory({
			[ROOT]: rootListing,
			"/proj/src": srcListing,
		});
		await renderPanel(
			<FilesPanel
				useWorkspaces={fakeUseWorkspaces([{ path: ROOT, title: "proj" }])}
				listDirectory={listDirectory}
			/>,
		);
		await act(async () => {
			screen.getByText("src").click();
		});
		expect(listDirectory).toHaveBeenCalledWith("/proj/src");
		// 进入后渲染子目录内容与 truncated 提示
		expect(screen.getByText("client")).toBeInTheDocument();
		expect(screen.getByText("⚠ 结果过多，已截断显示")).toBeInTheDocument();
	});

	it("点击面包屑回跳上级目录", async () => {
		const listDirectory = fakeListDirectory({
			[ROOT]: rootListing,
			"/proj/src": srcListing,
		});
		await renderPanel(
			<FilesPanel
				useWorkspaces={fakeUseWorkspaces([{ path: ROOT, title: "proj" }])}
				listDirectory={listDirectory}
			/>,
		);
		// 先进入 /proj/src
		await act(async () => {
			screen.getByText("src").click();
		});
		// 面包屑当前为 proj / src，点击 "proj" 回跳根目录
		await act(async () => {
			screen.getByText("proj").click();
		});
		expect(listDirectory).toHaveBeenCalledTimes(3);
		expect(listDirectory).toHaveBeenLastCalledWith(ROOT);
		// 回跳后重新渲染根目录内容
		expect(screen.getByText("docs")).toBeInTheDocument();
	});

	it("truncated 时显示截断提示", async () => {
		const listDirectory = fakeListDirectory({ [ROOT]: srcListing });
		await renderPanel(
			<FilesPanel
				useWorkspaces={fakeUseWorkspaces([{ path: ROOT, title: "proj" }])}
				listDirectory={listDirectory}
			/>,
		);
		expect(screen.getByText("⚠ 结果过多，已截断显示")).toBeInTheDocument();
	});

	it("无工作区时显示空态文案", async () => {
		const listDirectory = fakeListDirectory({});
		await renderPanel(
			<FilesPanel useWorkspaces={fakeUseWorkspaces([])} listDirectory={listDirectory} />,
		);
		expect(screen.getByText("当前为通用自由对话，暂未引入项目文件夹")).toBeInTheDocument();
		expect(listDirectory).not.toHaveBeenCalled();
	});

	it("缺省 props 时容错为空态", async () => {
		await renderPanel(<FilesPanel />);
		expect(screen.getByText("当前为通用自由对话，暂未引入项目文件夹")).toBeInTheDocument();
	});

	it("listDirectory 失败时显示错误文案", async () => {
		const listDirectory = vi.fn(() => Promise.reject(new Error("boom")));
		await renderPanel(
			<FilesPanel
				useWorkspaces={fakeUseWorkspaces([{ path: ROOT, title: "proj" }])}
				listDirectory={listDirectory}
			/>,
		);
		expect(screen.getByText("目录读取失败")).toBeInTheDocument();
	});

	it("点击 🗂 调 openPath，不触发进入目录", async () => {
		const listDirectory = fakeListDirectory({ [ROOT]: rootListing });
		const openPath = vi.fn();
		await renderPanel(
			<FilesPanel
				useWorkspaces={fakeUseWorkspaces([{ path: ROOT, title: "proj" }])}
				listDirectory={listDirectory}
				openPath={openPath}
			/>,
		);
		const openBtns = screen.getAllByTitle("在系统中打开");
		await act(async () => {
			openBtns[0].click();
		});
		expect(openPath).toHaveBeenCalledWith("/proj/src");
		// stopPropagation：不触发进入子目录（listDirectory 只被初始加载调用 1 次）
		expect(listDirectory).toHaveBeenCalledTimes(1);
	});
});
