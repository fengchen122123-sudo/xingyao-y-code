const EXT_ICONS: Record<string, string> = {
  py: '🐍', pyc: '🐍', pyw: '🐍',
  js: '🟨', mjs: '🟨', cjs: '🟨',
  ts: '🟦', mts: '🟦', cts: '🟦',
  jsx: '⚛️', tsx: '⚛️',
  vue: '💚', v: '🔷',
  html: '🟧', htm: '🟧', css: '🎨', scss: '🎨', less: '🎨',
  java: '☕', class: '☕', jar: '☕',
  c: '🔵', h: '🔵', cpp: '🔵', cc: '🔵', hpp: '🔵',
  cs: '🟪', go: '🔹', rs: '🦀', php: '🐘', rb: '💎', swift: '🧡',
  kt: '🟣', lua: '🌙', r: '📈',
  txt: '📄', log: '📄', md: '📝', pdf: '📕',
  doc: '📘', docx: '📘',
  xls: '📗', xlsx: '📗', csv: '📊',
  ppt: '📙', pptx: '📙',
  json: '🧾', yaml: '🧾', yml: '🧾', toml: '🧾', ini: '🧾', cfg: '🧾',
  xml: '🧾', sql: '🗄️', db: '🗄️', sqlite: '🗄️',
  zip: '📦', rar: '📦', '7z': '📦', tar: '📦', gz: '📦', bz2: '📦', xz: '📦',
  png: '🖼️', jpg: '🖼️', jpeg: '🖼️', gif: '🖼️', bmp: '🖼️', ico: '🖼️',
  svg: '🖼️', webp: '🖼️', mp3: '🎵', wav: '🎵', flac: '🎵',
  mp4: '🎬', mkv: '🎬', avi: '🎬', mov: '🎬',
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
