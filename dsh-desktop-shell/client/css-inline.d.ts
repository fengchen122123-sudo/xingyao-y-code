/** css?inline 导入（vite/rolldown 风格，返回 CSS 文本内容） */
declare module "*.css?inline" {
	const css: string;
	export default css;
}

/** 普通 css 副作用导入 */
declare module "*.css" {
	const css: string;
	export default css;
}
