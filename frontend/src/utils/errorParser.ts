
export interface ParsedModelError {
  /** 简短核心原因（例如："API Key 无效或未授权"、"账号余额不足"） */
  title: string;
  detail: string;
  raw: string;
}

export function parseModelTestError(rawMsg: string): ParsedModelError {
  const raw = (rawMsg || '').trim();
  const lower = raw.toLowerCase();

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
