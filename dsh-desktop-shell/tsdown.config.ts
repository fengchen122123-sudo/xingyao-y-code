import { defineConfig } from "tsdown";

/**
 * 打包配置说明（ModuleLoader 包裹对齐方式）：
 *
 * 官方 client.js（如 @deepseek-ai/dsh-client-ui-sidebar/lib/client.js）格式为：
 *
 *   window.__ModuleLoader__.load({
 *   	id: "<包名>",
 *   	factory: (require) => {
 *   		var module = { exports: {} };
 *   		var exports = module.exports;
 *   		... // CJS body：let react = require("react"); 等 external require
 *   		exports.apply = apply;
 *   		exports.inject = inject;
 *   		return module.exports;
 *   	}
 *   });
 *
 * tsdown 没有内置该包裹格式，因此采用 banner/footer 方案对齐：
 * - 先让 tsdown 产出 format: "cjs" 的 body（external 由 require(...) 引入，
 *   导出走 exports.apply / exports.inject），
 * - banner 负责打开包裹并在 factory 内自建 module / exports 两个 CJS 变量
 *  （与官方产物一致，cjs 输出里的 module/exports 引用会落到这两个变量上），
 * - footer 负责 return module.exports 并闭合包裹。
 *
 * 这样产物的头部/尾部结构与官方 client.js 完全一致，
 * react / react-dom / react/jsx-runtime / @deepseek-ai/* 全部保持 external。
 * 注意：tsdown 内置的 dts 构建会对声明产物做 fake-js 处理，
 * 若 banner/footer 全局生效会把 export 包进函数体导致解析失败，
 * 因此 dts 不在 tsdown 内产出，改由 `tsc -p tsconfig.json` 单独生成
 * （见 package.json 的 build:client 脚本，tsconfig 已配置
 *  emitDeclarationOnly + declarationDir: client-dist/types）。
 */

const banner = [
	"window.__ModuleLoader__.load({",
	'\tid: "dsh-desktop-shell",',
	"\tfactory: (require) => {",
	"\t\tvar module = { exports: {} };",
	"\t\tvar exports = module.exports;",
].join("\n");

const footer = ["\t\treturn module.exports;", "\t}", "});", ""].join("\n");

export default defineConfig({
	// 入口键名 client → 产物 client-dist/client.js（对齐官方产物文件名）
	entry: { client: "client/index.tsx" },
	outDir: "client-dist",
	format: ["cjs"],
	// 对齐官方产物文件名：client.js（而非 client.cjs）
	outExtensions: () => ({ js: ".js" }),
	// dts 由 tsc 单独产出（原因见上方注释）
	dts: false,
	external: [/^react($|\/)/, /^react-dom($|\/)/, /^@deepseek-ai\//],
	platform: "browser",
	target: "es2020",
	sourcemap: false,
	minify: false,
	outputOptions: {
		banner,
		footer,
	},
});
