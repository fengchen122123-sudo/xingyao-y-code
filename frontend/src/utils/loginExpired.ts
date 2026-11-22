const EXPIRED_HINTS = /(失效|过期|超时|未登录|请先登录|重新登录|未认证)/;

export function isLoginExpiredError(text?: string | null): boolean {
  const s = String(text ?? '');
  if (!s || !s.includes('登录')) return false;
  return EXPIRED_HINTS.test(s);
}
