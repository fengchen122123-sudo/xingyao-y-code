/**
 * ycode 主题作用域容器。
 *
 * 用法：
 *   <ThemeScope theme="dark">...ycode 风格子树...</ThemeScope>
 *
 * - 通过 data-ycode-theme 属性激活 tokens.css 中对应的 --yc-* 变量组；
 * - 自身设 height:100% + 背景/文字/字体，作为 ycode 风格子树的基底。
 *
 * CSS 注入方式说明（实测结论）：
 * tsdown 0.11 对 `import "./tokens.css"` 的处理是把 CSS **抽取为独立产物
 * client-dist/client.css**，而不会打进 client.js 内联注入；但本插件的
 * client.js 由 window.__ModuleLoader__ 在运行时直接 eval 加载（见
 * tsdown.config.ts 的 banner/footer 说明），DSH 宿主并不会替我们加载
 * client.css，运行时也不会去读 client/theme/tokens.css 源文件。
 * 因此为确保 token 真正进入 DOM，这里采用 <style> 内联兜底：
 * tokensCss 字符串与 tokens.css 内容保持一致，组件首次挂载时注入
 * document.head（幂等，只注入一次）。tokens.css 文件仍保留，
 * 供测试（jsdom 解析）、类型检查与人工核对使用，两者需同步维护。
 */
export interface ThemeScopeProps {
    theme: "dark" | "light";
    children: React.ReactNode;
}
export declare function ThemeScope({ theme, children }: ThemeScopeProps): import("react").JSX.Element;
export default ThemeScope;
