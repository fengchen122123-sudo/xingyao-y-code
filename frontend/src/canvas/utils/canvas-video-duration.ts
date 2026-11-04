// M2-T05：直译自参考仓 utils/canvas-video-duration.ts。
// 替换点（§0.6）：类型 import 路径改 ../types；探测地址本来就取节点 metadata.content
//（星瑶口径即 media_server URL），保持与播放器同源，不再有 ark 临时链兜底。
import type { CanvasNodeData } from "../types";

/**
 * 读取视频真实时长（毫秒）。用 <video preload="metadata"> 只取元数据，不下载整段视频。
 * 探测失败（不可播放地址、跨域被拒、超时）返回 undefined，调用方保持原判断，不要因此拦死用户。
 */
const probeCache = new Map<string, { ms?: number; at: number }>();
// 成功缓存 10 分钟；失败（跨域/不可播放）只缓存 1 分钟，避免每次配置变更都重建 <video> 元素
const PROBE_TTL_MS = 10 * 60_000;
const PROBE_FAIL_TTL_MS = 60_000;

export async function probeVideoDurationMs(url: string, timeoutMs = 10000): Promise<number | undefined> {
    const src = (url || "").trim();
    // asset:// 是火山素材库引用，浏览器读不了；其余（含桌面本地协议）都交给 <video> 试
    if (!src || typeof document === "undefined" || src.startsWith("asset://") || src.startsWith("blob:") || src.startsWith("data:")) return undefined;
    const hit = probeCache.get(src);
    if (hit && Date.now() - hit.at < (hit.ms ? PROBE_TTL_MS : PROBE_FAIL_TTL_MS)) return hit.ms;
    const value = await probeVideoDurationMsUncached(src, timeoutMs);
    probeCache.set(src, { ms: value, at: Date.now() });
    return value;
}

function probeVideoDurationMsUncached(src: string, timeoutMs: number): Promise<number | undefined> {
    return new Promise((resolve) => {
        const element = document.createElement("video");
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
            // 流式/未知长度会返回 Infinity，按失败处理
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

/**
 * 节点上可用于「播放器探测时长」的地址。
 *
 * 必须与播放器用的是同一个地址：视频节点用 `node.metadata.content` 作为 src，
 * 探测别的地址会读错时长（节点内容被替换后残留链会指向旧片，价格随之算错）。
 * 所以这里只用 content。
 */
export function canvasVideoProbeUrl(node: CanvasNodeData): string {
    const value = String(node.metadata?.content || "").trim();
    if (!value || value.startsWith("blob:") || value.startsWith("data:") || value.startsWith("asset://")) return "";
    return value;
}
