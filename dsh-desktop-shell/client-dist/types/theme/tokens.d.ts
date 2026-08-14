/**
 * tokens.css 的字符串内联版本 —— 与 tokens.css 同步维护。
 *
 * 原因：tsdown(rolldown) 在 Windows 下无法解析 `import "./tokens.css?inline"`
 *（?inline 查询串触发 os error 123），导致 build:client 失败；
 * 故将 CSS 内容以字符串常量内联在此，ThemeScope 直接从本文件导入。
 * tokens.css 文件保留不动，供测试（jsdom 解析）、类型检查与人工核对使用。
 */
export declare const tokensCss: string;
