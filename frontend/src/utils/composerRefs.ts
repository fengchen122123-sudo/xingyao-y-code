export type RefKind = 'session' | 'file' | 'skill' | 'command';

export type RefSegment =
  | { kind: 'text'; text: string }
  | { kind: 'ref'; text: string; refKind: RefKind };

export const REF_TOKEN_PATTERN =
  /@\{[^}\n]{1,80}\}|@[^\s@{}，。；！？：、）」】》]+|\/[^\s@{}，。；！？：、）」】》]+/g;

function normalizeSlashSet(ids?: Iterable<string>): Set<string> {
  const set = new Set<string>();
  if (!ids) return set;
  for (const id of ids) {
    const clean = String(id || '').trim().toLowerCase();
    const normalized = clean.startsWith('/') ? clean.slice(1) : clean;
    if (normalized) set.add(normalized);
  }
  return set;
}

/** 引用 token 的类型：`@{...}` 是会话，`/` 开头命中命令表为命令否则技能，其余按文件路径处理 */
export function refKindOf(token: string, commandIds?: Iterable<string>): RefKind {
  if (token.startsWith('@{')) return 'session';
  if (token.startsWith('/')) {
    const cmdSet = normalizeSlashSet(commandIds);
    const clean = token.slice(1).toLowerCase();
    if (clean && cmdSet.has(clean)) return 'command';
    return 'skill';
  }
  return 'file';
}

export function refDisplayName(token: string): string {
  return refKindOf(token) === 'session' ? token.slice(2, -1) : token.slice(1);
}

/** 把输入框文本切成「普通文本段 + 引用段」序列；拼回去必须与原文逐字符相等 */
export function splitRefSegments(
  text: string,
  skillIds?: Iterable<string>,
  commandIds?: Iterable<string>,
): RefSegment[] {
  const src = text || '';
  const out: RefSegment[] = [];
  const skillSet = normalizeSlashSet(skillIds);
  const cmdSet = normalizeSlashSet(commandIds);
  let last = 0;
  REF_TOKEN_PATTERN.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = REF_TOKEN_PATTERN.exec(src)) !== null) {
    const token = m[0];
    let refKind: RefKind;
    if (token.startsWith('/')) {
      const precededByBoundary = m.index === 0 || /\s/.test(src[m.index - 1]);
      const clean = token.slice(1).toLowerCase();
      if (!precededByBoundary) continue;
      if (cmdSet.has(clean)) refKind = 'command';
      else if (skillSet.has(clean)) refKind = 'skill';
      else continue;
    } else {
      refKind = refKindOf(token);
    }
    if (m.index > last) out.push({ kind: 'text', text: src.slice(last, m.index) });
    out.push({ kind: 'ref', text: token, refKind });
    last = m.index + token.length;
  }
  if (last < src.length) out.push({ kind: 'text', text: src.slice(last) });
  return out;
}

export function refTokenEndingAt(
  text: string,
  caret: number,
  skillIds?: Iterable<string>,
  commandIds?: Iterable<string>,
): string | null {
  const before = (text || '').slice(0, Math.max(0, Math.min(caret, (text || '').length)));
  if (!before) return null;
  const session = /@\{([^}\n]{1,80})\}$/.exec(before);
  if (session) return session[0];
  const file = /@([^\s@{}，。；！？：、）」】》]+)$/.exec(before);
  // 文件 token 不能是另一个引用的尾巴（例如 @{a} 里的 @）
  if (file) return file[0];
  const skillSet = normalizeSlashSet(skillIds);
  const cmdSet = normalizeSlashSet(commandIds);
  if (skillSet.size > 0 || cmdSet.size > 0) {
    const slash = /(?:^|\s)(\/[^\s@{}，。；！？：、）」】》]+)$/.exec(before);
    if (slash) {
      const clean = slash[1].slice(1).toLowerCase();
      if (cmdSet.has(clean) || skillSet.has(clean)) return slash[1];
    }
  }
  return null;
}
