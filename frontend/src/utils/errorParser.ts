/**
 * 模型连接测试及 API 调用的错误解析与友好文案归一化。
 *
 * 作用：将 LiteLLM / Provider 抛出的深层嵌套技术堆栈（如 AuthenticationError /
 * OpenAIException / ConnectionError）转换为用户易懂的人性化中文提示与排查建议，
 * 避免在设置界面或调用链中直接暴露晦涩冗长、破坏布局的原始技术堆栈。
 */

export interface ParsedModelError {
  /** 简短核心原因（例如："API Key 无效或未授权"、"账号余额不足"） */
  title: string;
  /** 详细排查指引或简短说明 */
  detail: string;
  /** 原始错误信息（供开发者复制或查看完整堆栈） */
  raw: string;
}

export function parseModelTestError(rawMsg: string): ParsedModelError {
  const raw = (rawMsg || '').trim();
  const lower = raw.toLowerCase();

  // 1. 认证 / Key 无效 / 401 Unauthorized
  if (
    lower.includes('authenticationerror') ||
    lower.includes('invalid authentication') ||
    lower.includes('invalid_api_key') ||
    lower.includes('incorrect api key') ||
    lower.includes('unauthorized') ||
    /\b401\b/.test(lower)
  ) {
    return {
      title: 'API Key 无效或未授权',
      detail: '请检查该厂商配置的 API Key 是否正确且未过期',
      raw,
    };
  }

  // 2. 欠费 / 额度耗尽 / 余额不足
  if (
    lower.includes('insufficient_quota') ||
    lower.includes('quota') ||
    lower.includes('余额不足') ||
    lower.includes('insufficient balance') ||
    lower.includes('billing') ||
    lower.includes('欠费')
  ) {
    return {
      title: '账号余额不足',
      detail: '当前厂商账户额度已耗尽或已欠费，请前往服务商控制台充值',
      raw,
    };
  }

  // 3. 频次限制 / 429 Too Many Requests
  if (
    lower.includes('ratelimiterror') ||
    lower.includes('too many requests') ||
    lower.includes('rate_limit') ||
    /\b429\b/.test(lower)
  ) {
    return {
      title: '触发频次限制 (429)',
      detail: '请求过于频繁或达到 TPM/RPM 速率上限，请稍后重试',
      raw,
    };
  }

  // 4. 超时 (Timeout)
  if (
    lower.includes('timeout') ||
    lower.includes('timed out') ||
    lower.includes('连接超时')
  ) {
    return {
      title: '请求连接超时',
      detail: '连接模型服务超时，请检查网络连通性或代理配置',
      raw,
    };
  }

  // 5. 网络连通性 (ConnectionError / DNS 解析失败)
  if (
    lower.includes('connectionerror') ||
    lower.includes('failed to establish') ||
    lower.includes('nameresolutionerror') ||
    lower.includes('getaddrinfo') ||
    lower.includes('connection refused')
  ) {
    return {
      title: '网络连接失败',
      detail: '无法连接到目标服务器，请检查 Base URL 是否正确或网络是否通畅',
      raw,
    };
  }

  // 6. 找不到模型 / 404 Not Found
  if (
    lower.includes('notfounderror') ||
    lower.includes('does not exist') ||
    lower.includes('model_not_found') ||
    lower.includes('未找到模型') ||
    /\b404\b/.test(lower)
  ) {
    return {
      title: '模型不存在或路径错误 (404)',
      detail: '请核对模型 ID 是否正确，或 Base URL 是否缺少/多写了后缀',
      raw,
    };
  }

  // 7. 403 权限不足
  if (
    lower.includes('permissiondenied') ||
    lower.includes('forbidden') ||
    /\b403\b/.test(lower)
  ) {
    return {
      title: '访问被拒绝 (403)',
      detail: '当前 API Key 无权访问该模型或操作受限',
      raw,
    };
  }

  // 8. 5xx 服务端错误
  if (
    lower.includes('internalservererror') ||
    lower.includes('bad gateway') ||
    lower.includes('service unavailable') ||
    /\b50[0234]\b/.test(lower)
  ) {
    return {
      title: '模型服务端异常 (5xx)',
      detail: '服务提供方暂时不可用或网关错误，请稍后重试',
      raw,
    };
  }

  // 兜底清洗：剥离 litellm. / Exception: / Error: 等冗余技术包裹
  let cleaned = raw
    .replace(/^(litellm\.[a-zA-Z0-9_]+:\s*)+/i, '')
    .replace(/^([a-zA-Z0-9_]*Exception:\s*)+/i, '')
    .replace(/^([a-zA-Z0-9_]*Error:\s*)+/i, '')
    .trim();

  if (cleaned.startsWith('OpenAIException - ')) {
    cleaned = cleaned.replace(/^OpenAIException -\s*/, '');
  }

  return {
    title: '连接测试失败',
    detail: cleaned.length > 90 ? `${cleaned.slice(0, 90)}…` : (cleaned || '未知异常'),
    raw,
  };
}
