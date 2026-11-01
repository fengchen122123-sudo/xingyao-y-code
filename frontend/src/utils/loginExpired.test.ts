import { describe, expect, it } from 'vitest';
import { isLoginExpiredError } from './loginExpired';

describe('isLoginExpiredError', () => {
  it('识别登录过期 / 失效的中文错误文案', () => {
    expect(isLoginExpiredError('基元律动登录已失效，请重新登录后再使用该模型')).toBe(true);
    expect(isLoginExpiredError('登录已过期，请重新登录')).toBe(true);
    expect(isLoginExpiredError('未登录基元律动账号')).toBe(true);
    expect(isLoginExpiredError('请先登录基元律动账号（登录态已失效，请重新登录后再试）')).toBe(true);
    expect(isLoginExpiredError('HTTP 401：未认证或登录已过期')).toBe(true);
  });

  it('不把普通错误当成登录过期', () => {
    expect(isLoginExpiredError('余额不足')).toBe(false);
    expect(isLoginExpiredError('登录按钮没反应')).toBe(false);
    expect(isLoginExpiredError('登录失败，请检查手机号')).toBe(false);
    expect(isLoginExpiredError('')).toBe(false);
    expect(isLoginExpiredError(null)).toBe(false);
    expect(isLoginExpiredError(undefined)).toBe(false);
  });
});
