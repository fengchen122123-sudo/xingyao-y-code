import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ThemeScope } from "./ThemeScope";

afterEach(cleanup);

describe("ThemeScope", () => {
	it("渲染带 data-ycode-theme 的容器并包含子内容", () => {
		render(
			<ThemeScope theme="dark">
				<span>hi</span>
			</ThemeScope>,
		);

		const child = screen.getByText("hi");
		const scope = child.closest(".yc-scope");
		expect(scope).not.toBeNull();
		expect(scope).toHaveAttribute("data-ycode-theme", "dark");
		expect(scope).toContainElement(child);
	});

	it("theme=light 时挂上 light 变量组", () => {
		render(
			<ThemeScope theme="light">
				<span>hi</span>
			</ThemeScope>,
		);

		const scope = screen.getByText("hi").closest(".yc-scope");
		expect(scope).toHaveAttribute("data-ycode-theme", "light");
	});
});
