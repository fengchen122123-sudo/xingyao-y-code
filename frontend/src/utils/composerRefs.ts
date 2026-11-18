/**
 * 输入框里 @ 引用的切分口径（纯函数，便于单测）。
 *
 * 与后端 core/input_parser.py 保持一致：
 *   会话引用 @{会话名}：} 定界、不跨行、最长 80 字符（SESSION_REF_PATTERN）
 *   文件引用 @路径    ：空白、@、花括号与中文句读为边界（AT_PATH_TOKEN_PATTERN）
 * 前端把它们渲染成胶囊，后端按同一口径解析成真实的会话/附件引用，
 * 两边口径一旦不同，就会出现「看起来是引用但后端没收到」这类问题。
 */
export type RefKind = 'session' | 'file';

export type RefSegment =
  | { kind: 'text'; text: string }
  | { kind: 'ref'; text: string; refKind: RefKind };

export const REF_TOKEN_PATTERN = /@\{[^}\n]{1,80}\}|@[^\s@{}，。；！？：、）」】》]+/g;

/** 引用 token 的类型：`@{...}` 是会话，其余按文件路径处理 */
export function refKindOf(token: string): RefKind {
  return token.startsWith('@{') ? 'session' : 'file';
}

/** 胶囊上显示的名字（去掉语法记号） */
export function refDisplayName(token: string): string {
  return refKindOf(token) === 'session' ? token.slice(2, -1) : token.slice(1);
}

/** 把输入框文本切成「普通文本段 + 引用段」序列；拼回去必须与原文逐字符相等 */
export function splitRefSegments(text: string): RefSegment[] {
  const src = text || '';
  const out: RefSegment[] = [];
  let last = 0;
  REF_TOKEN_PATTERN.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = REF_TOKEN_PATTERN.exec(src)) !== null) {
    if (m.index > last) out.push({ kind: 'text', text: src.slice(last, m.index) });
    out.push({ kind: 'ref', text: m[0], refKind: refKindOf(m[0]) });
    last = m.index + m[0].length;
  }
  if (last < src.length) out.push({ kind: 'text', text: src.slice(last) });
  return out;
}

/**
 * 光标前一个「字符」是引用时，返回该引用 token 的长度（0 表示不是），
 * 供编辑器实现「退格一次删掉整个引用」。token 边界判定与上方正则同口径：
 * 从光标往左截取，命中正则且恰好贴到光标位置才算。
 */
export function refTokenEndingAt(text: string, caret: number): string | null {
  const before = (text || '').slice(0, Math.max(0, Math.min(caret, (text || '').length)));
  if (!before) return null;
  // 会话引用先看：以 } 结尾且能整段匹配
  const session = /@\{([^}\n]{1,80})\}$/.exec(before);
  if (session) return session[0];
  const file = /@([^\s@{}，。；！？：、）」】》]+)$/.exec(before);
  // 文件 token 不能是另一个引用的尾巴（例如 @{a} 里的 @）
  if (file) return file[0];
  return null;
}
