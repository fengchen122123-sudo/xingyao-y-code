import { defineConfig } from "vitest/config";

export default defineConfig({
	test: {
		environment: "jsdom",
		setupFiles: ["./vitest.setup.ts"],
		include: ["client/**/*.test.{ts,tsx}"],
		// 测试文件只跑 vitest，不进入 client 产物（tsdown 入口只含 client/index.tsx）
	},
});
