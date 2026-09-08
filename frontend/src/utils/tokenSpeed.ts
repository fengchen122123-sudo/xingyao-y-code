/**
 * Token 速度估算与平滑测速工具
 */

/**
 * 粗略估算增量文本包含的 Token 数（用于流式过程中的实时测速平滑滤波）
 * CJK 字符约 1.4 字符 / token，其他 ASCII / 代码约 3.5 字符 / token
 */
export function estimateDeltaTokens(text: string): number {
  if (!text) return 0;
  let cjkCount = 0;
  let otherCount = 0;
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    // CJK 常见汉字及常用符号范围
    if (
      (code >= 0x4e00 && code <= 0x9fff) ||
      (code >= 0x3400 && code <= 0x4dbf) ||
      (code >= 0xf900 && code <= 0xfaff)
    ) {
      cjkCount++;
    } else {
      otherCount++;
    }
  }
  const est = cjkCount / 1.4 + otherCount / 3.5;
  return Math.max(1, Math.round(est));
}

export interface SpeedSample {
  time: number; // ms
  tokens: number;
}

/**
 * 流式生成测速追踪器：
 * - 采用 1.2 秒滑动窗口聚合采样
 * - 采用指数移动平均（EMA）平滑滤波，过滤网络突发带来的数值剧烈抖动
 * - 断流超时（> 1.5 秒无新 chunk）时平滑归零
 */
export class StreamingSpeedTracker {
  private samples: SpeedSample[] = [];
  private windowMs: number;
  private currentSpeed = 0;
  private totalStreamedTokens = 0;
  private streamStartTime = 0;

  constructor(windowMs = 1200) {
    this.windowMs = windowMs;
  }

  reset(): void {
    this.samples = [];
    this.currentSpeed = 0;
    this.totalStreamedTokens = 0;
    this.streamStartTime = 0;
  }

  addChunk(text: string, now = (typeof performance !== 'undefined' ? performance.now() : Date.now())): number {
    if (!text) return this.getSpeed(now);
    if (!this.streamStartTime) {
      this.streamStartTime = now;
    }
    const tokens = estimateDeltaTokens(text);
    this.totalStreamedTokens += tokens;
    this.samples.push({ time: now, tokens });

    // 清理滑动窗口之外的历史采样
    const cutoff = now - this.windowMs;
    while (this.samples.length > 0 && this.samples[0].time < cutoff) {
      this.samples.shift();
    }

    if (this.samples.length >= 2) {
      const windowTokens = this.samples.reduce((sum, s) => sum + s.tokens, 0);
      const windowDuration = (now - this.samples[0].time) / 1000;
      if (windowDuration > 0.05) {
        const rawSpeed = windowTokens / windowDuration;
        // EMA 指数移动平均：80% 新速度 + 20% 历史，平滑抖动
        this.currentSpeed = this.currentSpeed > 0 ? (this.currentSpeed * 0.2 + rawSpeed * 0.8) : rawSpeed;
      }
    } else if (this.samples.length === 1) {
      const duration = (now - this.streamStartTime) / 1000;
      if (duration > 0.1) {
        this.currentSpeed = this.totalStreamedTokens / duration;
      }
    }

    return Math.round(this.currentSpeed * 10) / 10;
  }

  getSpeed(now = (typeof performance !== 'undefined' ? performance.now() : Date.now())): number {
    if (this.samples.length > 0 && (now - this.samples[this.samples.length - 1].time) > 1500) {
      this.currentSpeed = 0;
    }
    return Math.round(this.currentSpeed * 10) / 10;
  }

  getTotalTokens(): number {
    return this.totalStreamedTokens;
  }
}

/**
 * 格式化 Token 速率：保留 1 位小数，若为整十/整数且末尾为 0 时简化为整数字符串
 */
export function formatTokenSpeed(speed: number | undefined | null): string {
  const v = Number(speed) || 0;
  if (v <= 0) return '0 t/s';
  const rounded = Math.round(v * 10) / 10;
  return Number.isInteger(rounded) ? `${rounded} t/s` : `${rounded.toFixed(1)} t/s`;
}

/**
 * 格式化耗时（秒）
 */
export function formatDuration(seconds: number | undefined | null): string {
  const v = Number(seconds) || 0;
  if (v <= 0) return '0s';
  if (v < 10) return `${v.toFixed(1)}s`;
  return `${Math.round(v)}s`;
}
