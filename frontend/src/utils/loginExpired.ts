// 「登录过期」类错误的统一判定。
//
// 登录态失效在各条链路上最终都表现为一条错误文本（发送前守门的 need_login、
// 回合里的「登录已过期」、账号接口返回的「登录已失效」等），文案来源不止一处。
// 这里集中匹配一次，UI 只问「这条错误是不是登录过期」，命中就在错误提示旁
// 直接给「重新登录」按钮，不再让用户自己去翻登录入口。
//
// 判定刻意保守：必须同时出现「登录」与失效语义，避免把「登录按钮没反应」
// 这类普通报错误判成登录过期，给用户一个点不出结果的按钮。
const EXPIRED_HINTS = /(失效|过期|超时|未登录|请先登录|重新登录|未认证)/;

export function isLoginExpiredError(text?: string | null): boolean {
  const s = String(text ?? '');
  if (!s || !s.includes('登录')) return false;
  return EXPIRED_HINTS.test(s);
}
