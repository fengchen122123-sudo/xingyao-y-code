
const KNOWN_EXTENSIONS = new Set([
  'md', 'markdown', 'py', 'ts', 'tsx', 'js', 'jsx', 'vue', 'html', 'htm',
  'css', 'scss', 'less', 'json', 'yml', 'yaml', 'toml', 'ini', 'cfg', 'conf',
  'txt', 'log', 'csv', 'xml', 'sql', 'sh', 'bat', 'ps1', 'c', 'h', 'cpp',
  'hpp', 'java', 'go', 'rs', 'rb', 'php', 'kt', 'swift', 'cs', 'fs', 'm',
  'gradle', 'properties', 'env', 'gitignore',
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
  if (/[\s<>"'`]/.test(s)) return false;
  if (s.toLowerCase().startsWith('file://')) return true;
  if (/^[A-Za-z][A-Za-z0-9+.-]*:\/\//.test(s)) return false;
  if (/^[A-Za-z]:[\\/]/.test(s)) return true;
  if (s.startsWith('./') || s.startsWith('../')) return true;
  if (s.startsWith('/') && s.length > 1 && !s.startsWith('//')) return true;
  if (/[\\/]/.test(s) && isKnownExtension(s)) return true;
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
    try { p = decodeURIComponent(p); } catch { }
    if (/^\/[A-Za-z]:[\\/]/.test(p)) p = p.slice(1);
    return p;
  }
  if (/^[A-Za-z]:[\\/]/.test(s) || s.startsWith('\\\\') || s.startsWith('/')) {
    return s;
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
const PDF_EXTENSIONS = new Set(['pdf']);
// Office 文档：右栏不能直接渲染，需先由后端转成 PDF，再交「文档」页签（pdf.js）。
// rtf/odt/ods/odp 一并算进来（WPS/LibreOffice 的另存格式，后端转换器同样吃）。
export const OFFICE_EXTENSIONS = new Set([
  'doc', 'docx', 'rtf', 'odt',
  'xls', 'xlsx', 'ods',
  'ppt', 'pptx', 'odp',
]);
const RIGHT_PANEL_EXTENSIONS = new Set([
  'html', 'htm', 'pdf', 'md', 'markdown',
  ...OFFICE_EXTENSIONS,
]);

function normalizedExt(path: string): string {
  return extOf((path || '').trim().replace(/[?#].*$/, ''));
}

export function isHtmlFilePath(path: string): boolean {
  return HTML_EXTENSIONS.has(normalizedExt(path));
}

export function isMarkdownFilePath(path: string): boolean {
  return MARKDOWN_EXTENSIONS.has(normalizedExt(path));
}

export function isOfficeFilePath(path: string): boolean {
  return OFFICE_EXTENSIONS.has(normalizedExt(path));
}

export function isDocxFilePath(path: string): boolean {
  return normalizedExt(path) === 'docx';
}

export function isPdfFilePath(path: string): boolean {
  return PDF_EXTENSIONS.has(normalizedExt(path));
}

export function canOpenInRightPanel(path: string): boolean {
  return RIGHT_PANEL_EXTENSIONS.has(normalizedExt(path));
}

function encodeSeg(seg: string): string {
  return encodeURIComponent(seg).replace(/[!'()*]/g, c =>
    '%' + c.charCodeAt(0).toString(16).toUpperCase(),
  );
}

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
