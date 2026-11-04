// M2-T05：直译自参考仓 utils/canvas-audio-duration.ts。
// 替换点（§0.6）：类型 import 路径改 ../types；探测地址取节点 metadata.content
//（星瑶口径即 media_server URL）。参考仓的 arkAssetUrl 兜底随云端字段裁剪去掉。
import type { CanvasNodeData } from "../types";

/**
 * 读取音频真实时长（毫秒）。用 <audio preload="metadata"> 只取元数据，不下载整段音频。
 * 探测失败（不可播放地址、跨域被拒、超时）返回 undefined，调用方保持原判断，不要因此拦死用户。
 */
const probeCache = new Map<string, { ms?: number; at: number }>();
// 成功缓存 10 分钟；失败（跨域/不可播放）只缓存 1 分钟，避免每次配置变更都重建 <audio> 元素
const PROBE_TTL_MS = 10 * 60_000;
const PROBE_FAIL_TTL_MS = 60_000;

export async function probeAudioDurationMs(url: string, timeoutMs = 8000): Promise<number | undefined> {
    const src = (url || "").trim();
    if (!src || typeof document === "undefined" || src.startsWith("asset://")) return undefined;
    const hit = probeCache.get(src);
    if (hit && Date.now() - hit.at < (hit.ms ? PROBE_TTL_MS : PROBE_FAIL_TTL_MS)) return hit.ms;
    const value = await probeAudioDurationMsUncached(src, timeoutMs);
    probeCache.set(src, { ms: value, at: Date.now() });
    return value;
}

function probeAudioDurationMsUncached(src: string, timeoutMs: number): Promise<number | undefined> {
    return new Promise((resolve) => {
        const element = document.createElement("audio");
        let settled = false;
        const finish = (value?: number) => {
            if (settled) return;
            settled = true;
            window.clearTimeout(timer);
            element.removeEventListener("loadedmetadata", onLoaded);
            element.removeEventListener("error", onError);
            element.removeEventListener("stalled", onError);
            try {
                element.src = "";
            } catch {
                /* ignore */
            }
            resolve(value);
        };
        const onLoaded = () => {
            const seconds = element.duration;
            finish(Number.isFinite(seconds) && seconds > 0 ? Math.round(seconds * 1000) : undefined);
        };
        const onError = () => finish(undefined);
        const timer = window.setTimeout(() => finish(undefined), timeoutMs);
        element.addEventListener("loadedmetadata", onLoaded);
        element.addEventListener("error", onError);
        element.addEventListener("stalled", onError);
        element.preload = "metadata";
        element.src = src;
        try {
            element.load();
        } catch {
            finish(undefined);
        }
    });
}

/** 节点上可播放的地址：星瑶统一用 metadata.content（asset:// 无法用播放器探测，跳过）。 */
export function canvasAudioProbeUrl(node: CanvasNodeData): string {
    const meta = node.metadata;
    return (meta?.content || "").trim().startsWith("asset://") ? "" : (meta?.content || "").trim();
}
