// 按文件扩展名映射图标，模拟 Windows 资源管理器的文件类型图标
// 文件树与附件卡片共用，保证两处图标一致
const EXT_ICONS: Record<string, string> = {
  // Python
  py: '🐍', pyc: '🐍', pyw: '🐍',
  // 前端 / 代码（按品牌配色：JS 黄、TS 蓝、Vue 绿、React、HTML 橙）
  js: '🟨', mjs: '🟨', cjs: '🟨',
  ts: '🟦', mts: '🟦', cts: '🟦',
  jsx: '⚛️', tsx: '⚛️',
  vue: '💚', v: '🔷',
  html: '🟧', htm: '🟧', css: '🎨', scss: '🎨', less: '🎨',
  // 后端 / 系统语言
  java: '☕', class: '☕', jar: '☕',
  c: '🔵', h: '🔵', cpp: '🔵', cc: '🔵', hpp: '🔵',
  cs: '🟪', go: '🔹', rs: '🦀', php: '🐘', rb: '💎', swift: '🧡',
  kt: '🟣', lua: '🌙', r: '📈',
  // 文本 / 办公文档（Word 蓝、Excel 绿、PPT 橙）
  txt: '📄', log: '📄', md: '📝', pdf: '📕',
  doc: '📘', docx: '📘',
  xls: '📗', xlsx: '📗', csv: '📊',
  ppt: '📙', pptx: '📙',
  // 配置 / 数据
  json: '🧾', yaml: '🧾', yml: '🧾', toml: '🧾', ini: '🧾', cfg: '🧾',
  xml: '🧾', sql: '🗄️', db: '🗄️', sqlite: '🗄️',
  // 压缩包
  zip: '📦', rar: '📦', '7z': '📦', tar: '📦', gz: '📦', bz2: '📦', xz: '📦',
  // 图片 / 媒体
  png: '🖼️', jpg: '🖼️', jpeg: '🖼️', gif: '🖼️', bmp: '🖼️', ico: '🖼️',
  svg: '🖼️', webp: '🖼️', mp3: '🎵', wav: '🎵', flac: '🎵',
  mp4: '🎬', mkv: '🎬', avi: '🎬', mov: '🎬',
  // 可执行 / 脚本
  exe: '⚙️', dll: '🧩', so: '🧩', lib: '🧩', ocx: '🧩',
  bat: '🖥️', cmd: '🖥️', ps1: '🖥️', sh: '🐚', bash: '🐚', zsh: '🐚',
};

export function fileExt(name: string): string {
  const idx = name.lastIndexOf('.');
  return idx > 0 ? name.slice(idx + 1).toLowerCase() : '';
}

export function extIcon(name: string): string {
  return EXT_ICONS[fileExt(name)] ?? '📄';
}

export function formatFileSize(bytes: number | null | undefined): string {
  if (bytes == null || isNaN(bytes)) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(2)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}
