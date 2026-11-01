// 本地文件路径识别与归一化工具。
// 渲染层（markdown.ts）用它判断哪些链接/文本是本地文件路径；
// 点击层（MessageBubble.vue）用它把相对路径拼成绝对路径再交后端预览。

const KNOWN_EXTENSIONS = new Set([
  'md', 'markdown', 'py', 'ts', 'tsx', 'js', 'jsx', 'vue', 'html', 'htm',
  'css', 'scss', 'less', 'json', 'yml', 'yaml', 'toml', 'ini', 'cfg', 'conf',
  'txt', 'log', 'csv', 'xml', 'sql', 'sh', 'bat', 'ps1', 'c', 'h', 'cpp',
  'hpp', 'java', 'go', 'rs', 'rb', 'php', 'kt', 'swift', 'cs', 'fs', 'm',
  'gradle', 'properties', 'env', 'gitignore',
  // 办公文档：`报告.docx`、`docs/方案.pptx` 这类相对路径也要能被识别成可点链接
  // （绝对路径本来就按盘符识别）。rtf/odt/ods/odp 覆盖 WPS/LibreOffice 另存格式。
  'doc', 'docx', 'rtf', 'odt', 'xls', 'xlsx', 'ods', 'ppt', 'pptx', 'odp',
]);

function extOf(s: string): string {
  const m = s.match(/\.([A-Za-z0-9]+)$/);
  return m ? m[1].toLowerCase() : '';
}

function isKnownExtension(s: string): boolean {
  return KNOWN_EXTENSIONS.has(extOf(s));
}

export function isLocalFileLink(url: string, opts?: { allowBareFilename?: boolean }): boolean {
  const s = url.trim();
  if (!s) return false;
  // 含空白 / HTML 特殊字符 / 反引号的字符串一律不算路径（防注入 + 防误判）
  if (/[\s<>"'`]/.test(s)) return false;
  if (s.toLowerCase().startsWith('file://')) return true;
  // 其他带 scheme 的 URL（http/https/ftp/...）一律非本地路径
  if (/^[A-Za-z][A-Za-z0-9+.-]*:\/\//.test(s)) return false;
  if (/^[A-Za-z]:[\\/]/.test(s)) return true; // 盘符
  if (s.startsWith('./') || s.startsWith('../')) return true;
  if (s.startsWith('/') && s.length > 1 && !s.startsWith('//')) return true; // Unix 绝对路径
  if (/[\\/]/.test(s) && isKnownExtension(s)) return true; // 含分隔符 + 已知扩展名
  // 纯文件名（无分隔符）：仅当允许裸文件名时才识别。反引号内是明确的代码/字面量
  // 语境，纯文件名（如 pom.xml）更可能是字面量，不识别为链接。
  if (!/[\\/]/.test(s) && isKnownExtension(s)) {
    return opts?.allowBareFilename !== false;
  }
  return false;
}

export function toAbsoluteLocalPath(raw: string, workdir: string): string | null {
  const s = raw.trim();
  if (!s) return null;
  if (s.toLowerCase().startsWith('file://')) {
    let p = s.slice('file://'.length);
    try { p = decodeURIComponent(p); } catch { /* 保留原样 */ }
    if (/^\/[A-Za-z]:[\\/]/.test(p)) p = p.slice(1); // file:///C:/x -> C:/x
    return p;
  }
  if (/^[A-Za-z]:[\\/]/.test(s) || s.startsWith('\\\\') || s.startsWith('/')) {
    return s; // 盘符 / UNC / Unix 根，绝对路径原样返回
  }
  if (!workdir) return null;
  const base = workdir.replace(/[\\/]+$/, '');
  const baseSegs = base.split('\\');
  // 逐段归一化：跳过空段与 "."，遇到 ".." 回退一层（不越过盘符/根）
  for (const seg of s.replace(/\//g, '\\').split('\\')) {
    if (seg === '' || seg === '.') continue;
    if (seg === '..') { if (baseSegs.length > 1) baseSegs.pop(); continue; }
    baseSegs.push(seg);
  }
  return baseSegs.join('\\');
}

const HTML_EXTENSIONS = new Set(['html', 'htm']);
const MARKDOWN_EXTENSIONS = new Set(['md', 'markdown']);
// Office 文档：右栏不能直接渲染，需先由后端转成 PDF 再交 WebView2 查看器。
// rtf/odt/ods/odp 一并算进来（WPS/LibreOffice 的另存格式，后端转换器同样吃）。
export const OFFICE_EXTENSIONS = new Set([
  'doc', 'docx', 'rtf', 'odt',
  'xls', 'xlsx', 'ods',
  'ppt', 'pptx', 'odp',
]);
// 右栏内嵌浏览器能打开的文件类型：html/htm（网页）、pdf（WebView2 内置查看器）、
// md/markdown（先由前端渲染成预览 HTML 再打开）、office 文档（先由后端转 PDF）。
// 其它类型仍走源码预览弹窗。
const RIGHT_PANEL_EXTENSIONS = new Set([
  'html', 'htm', 'pdf', 'md', 'markdown',
  ...OFFICE_EXTENSIONS,
]);

function normalizedExt(path: string): string {
  return extOf((path || '').trim().replace(/[?#].*$/, ''));
}

/** 是否是可当网页渲染的 HTML 文件（.html / .htm，忽略查询串与锚点） */
export function isHtmlFilePath(path: string): boolean {
  return HTML_EXTENSIONS.has(normalizedExt(path));
}

/** 是否是 markdown 文件（.md / .markdown） */
export function isMarkdownFilePath(path: string): boolean {
  return MARKDOWN_EXTENSIONS.has(normalizedExt(path));
}

/** 是否是 Office 文档（doc/docx/rtf/odt/xls/xlsx/ods/ppt/pptx/odp，忽略查询串与锚点） */
export function isOfficeFilePath(path: string): boolean {
  return OFFICE_EXTENSIONS.has(normalizedExt(path));
}

/** 是否该交给右栏内嵌浏览器打开（html / pdf / md / office 四类） */
export function canOpenInRightPanel(path: string): boolean {
  return RIGHT_PANEL_EXTENSIONS.has(normalizedExt(path));
}

// 与 Python 的 urllib.parse.quote(s, safe="") 对齐：encodeURIComponent 额外
// 放过 ! ' ( ) *，这里补编码，保证前后端对同一路径产出同一个 file:// URI
// （前端乐观显示与后端规范化结果不会出现两种写法）
function encodeSeg(seg: string): string {
  return encodeURIComponent(seg).replace(/[!'()*]/g, c =>
    '%' + c.charCodeAt(0).toString(16).toUpperCase(),
  );
}

/**
 * 本地绝对路径 → file:// URL（正斜杠 + 百分号编码）；非绝对路径返回 ''。
 *
 * 只接受盘符路径（D:\x、D:/x）与 UNC 路径（\\server\share\x）：相对路径没有
 * 可靠基准，调用方应先经 toAbsoluteLocalPath 拼成绝对路径再传进来。口径与
 * 后端 core.embedded_browser.path_to_file_uri 保持一致（空格/中文按段编码、
 * 含 .. 的路径拒绝）。
 */
export function toFileUrl(absPath: string): string {
  const raw = (absPath || '').trim().replace(/^"|"$/g, '');
  if (!raw) return '';
  const p = raw.replace(/\\/g, '/');
  const segs = (s: string) => s.split('/').filter(x => x !== '' && x !== '.');
  if (/^[A-Za-z]:\//.test(p)) {
    const rest = segs(p.slice(2));
    if (!rest.length || rest.includes('..')) return '';
    return 'file:///' + p.slice(0, 2) + '/' + rest.map(encodeSeg).join('/');
  }
  if (p.startsWith('//')) {
    const parts = segs(p.slice(2));
    if (parts.length < 2 || parts.includes('..')) return '';
    return 'file://' + encodeSeg(parts[0]) + '/' + parts.slice(1).map(encodeSeg).join('/');
  }
  return '';
}
