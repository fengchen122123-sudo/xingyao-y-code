import { describe, expect, it } from 'vitest';
import { parseModelTestError } from './errorParser';

describe('parseModelTestError', () => {
  it('识别 AuthenticationError 并提取友好中文提示', () => {
    const raw = 'litellm.AuthenticationError: AuthenticationError: OpenAIException - Invalid Authentication';
    const parsed = parseModelTestError(raw);
    expect(parsed.title).toBe('API Key 无效或未授权');
    expect(parsed.detail).toContain('请检查该厂商配置的 API Key');
    expect(parsed.raw).toBe(raw);
  });

  it('识别 401 Unauthorized 错误', () => {
    const raw = 'Error code: 401 - Incorrect API key provided: sk-xxx';
    const parsed = parseModelTestError(raw);
    expect(parsed.title).toBe('API Key 无效或未授权');
  });

  it('识别余额不足 / insufficient_quota', () => {
    const raw = 'litellm.APIError: APIError: OpenAIException - 余额不足，请充值';
    const parsed = parseModelTestError(raw);
    expect(parsed.title).toBe('账号余额不足');
    expect(parsed.detail).toContain('额度已耗尽或已欠费');
  });

  it('识别速率限制 429 RateLimitError', () => {
    const raw = 'litellm.RateLimitError: RateLimitError: OpenAIException - Rate limit reached for requests';
    const parsed = parseModelTestError(raw);
    expect(parsed.title).toBe('触发频次限制 (429)');
    expect(parsed.detail).toContain('TPM/RPM');
  });

  it('识别超时错误', () => {
    const raw = '连接超时（超过 10 秒）';
    const parsed = parseModelTestError(raw);
    expect(parsed.title).toBe('请求连接超时');
  });

  it('识别网络连接与域名解析失败', () => {
    const raw = 'ConnectionError: Failed to establish a new connection: [Errno 11001] getaddrinfo failed';
    const parsed = parseModelTestError(raw);
    expect(parsed.title).toBe('网络连接失败');
    expect(parsed.detail).toContain('Base URL');
  });

  it('识别模型不存在 404', () => {
    const raw = 'NotFoundError: The model moonshot-v99 does not exist';
    const parsed = parseModelTestError(raw);
    expect(parsed.title).toBe('模型不存在或路径错误 (404)');
  });

  it('识别服务端 500 / 502 / 503 异常', () => {
    const raw = 'InternalServerError: 502 Bad Gateway';
    const parsed = parseModelTestError(raw);
    expect(parsed.title).toBe('模型服务端异常 (5xx)');
  });

  it('兜底清洗未知异常技术前缀', () => {
    const raw = 'litellm.APIError: APIError: OpenAIException - Some custom server message';
    const parsed = parseModelTestError(raw);
    expect(parsed.title).toBe('连接测试失败');
    expect(parsed.detail).toBe('Some custom server message');
  });
});
