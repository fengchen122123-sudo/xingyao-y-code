// 本地文件路径识别与归一化工具。
// 渲染层（markdown.ts）用它判断哪些链接/文本是本地文件路径；
// 点击层（MessageBubble.vue）用它把相对路径拼成绝对路径再交后端预览。

const KNOWN_EXTENSIONS = new Set([
  'md', 'markdown', 'py', 'ts', 'tsx', 'js', 'jsx', 'vue', 'html', 'htm',
  'css', 'scss', 'less', 'json', 'yml', 'yaml', 'toml', 'ini', 'cfg', 'conf',
  'txt', 'log', 'csv', 'xml', 'sql', 'sh', 'bat', 'ps1', 'c', 'h', 'cpp',
  'hpp', 'java', 'go', 'rs', 'rb', 'php', 'kt', 'swift', 'cs', 'fs', 'm',
  'gradle', 'properties', 'env', 'gitignore',
]);

function extOf(s: string): string {
  const m = s.match(/\.([A-Za-z0-9]+)$/);
  return m ? m[1].toLowerCase() : '';
}

function isKnownExtension(s: string): boolean {
  return KNOWN_EXTENSIONS.has(extOf(s));
}

export function isLocalFileLink(url: string): boolean {
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
  // 已知扩展名结尾（含分隔符的相对路径或纯文件名，如 docs/foo.md、README.md、
  // 2026-08-26.md）。"1.2.3" 的尾段 '3' 不在白名单，已被 isKnownExtension 排除。
  if (isKnownExtension(s)) return true;
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
