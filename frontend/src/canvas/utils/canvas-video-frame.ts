// M2-T05：直译自参考仓 utils/canvas-video-frame.ts（§0.6 无平台依赖，原样直译）。
// 唯一差异：captureVideoPosterFromUrl 里参考仓的动态代理兜底（@/services/image-storage.getProxyUrl）
// 去掉——星瑶 media_server URL 与前端同源可直接加载，失败即返回 null。

/** Seek video to time (seconds) and capture a PNG data URL. */
export async function captureVideoFrame(video: HTMLVideoElement, timeSec?: number): Promise<{ dataUrl: string; width: number; height: number }> {
    const target = typeof timeSec === "number" && Number.isFinite(timeSec) ? Math.max(0, timeSec) : video.currentTime;
    if (Math.abs(video.currentTime - target) > 0.04) {
        await seekVideo(video, target);
    } else if (video.readyState < 2) {
        await waitForVideoReady(video);
    }
    await waitForPaint(video);

    const width = video.videoWidth || 1280;
    const height = video.videoHeight || 720;
    if (!width || !height) throw new Error("视频画面未就绪");
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("无法截取视频帧");
    try {
        ctx.drawImage(video, 0, 0, width, height);
        return { dataUrl: canvas.toDataURL("image/png"), width, height };
    } catch {
        throw new Error("截帧失败：视频跨域受限，请刷新后重试");
    }
}

function clampSeekTime(video: HTMLVideoElement, time: number) {
    const duration = video.duration;
    if (!Number.isFinite(duration) || duration <= 0) return Math.max(0, time);
    const maxSafe = Math.max(0, duration - 0.001);
    return Math.min(Math.max(0, time), maxSafe);
}

function seekVideo(video: HTMLVideoElement, time: number, timeoutMs = 5000) {
    return new Promise<void>((resolve, reject) => {
        const clamped = clampSeekTime(video, time);
        if (Math.abs(video.currentTime - clamped) < 0.04 && video.readyState >= 2) {
            resolve();
            return;
        }

        let settled = false;
        const finish = (fn: () => void) => {
            if (settled) return;
            settled = true;
            cleanup();
            fn();
        };
        const onSeeked = () => {
            const done = () => finish(() => resolve());
            if (video.readyState >= 2) done();
            else void waitForVideoReady(video).then(done).catch(() => finish(() => reject(new Error("视频定位失败"))));
        };
        const onError = () => finish(() => reject(new Error("视频定位失败")));
        const cleanup = () => {
            clearTimeout(timer);
            video.removeEventListener("seeked", onSeeked);
            video.removeEventListener("error", onError);
        };
        const timer = window.setTimeout(() => {
            if (video.readyState >= 2 && Math.abs(video.currentTime - clamped) < 0.35) {
                finish(() => resolve());
                return;
            }
            finish(() => reject(new Error("视频定位超时，请拖到接近结尾后改截取当前帧")));
        }, timeoutMs);

        video.addEventListener("seeked", onSeeked);
        video.addEventListener("error", onError);
        try {
            if (video.ended) video.pause();
            video.currentTime = clamped;
        } catch (error) {
            finish(() => reject(error instanceof Error ? error : new Error("视频定位失败")));
        }
    });
}

function waitForVideoReady(video: HTMLVideoElement, timeoutMs = 5000) {
    if (video.readyState >= 2) return Promise.resolve();
    return new Promise<void>((resolve, reject) => {
        const onReady = () => {
            cleanup();
            resolve();
        };
        const onError = () => {
            cleanup();
            reject(new Error("视频未就绪"));
        };
        const cleanup = () => {
            clearTimeout(timer);
            video.removeEventListener("loadeddata", onReady);
            video.removeEventListener("canplay", onReady);
            video.removeEventListener("error", onError);
        };
        const timer = window.setTimeout(() => {
            cleanup();
            if (video.readyState >= 2) resolve();
            else reject(new Error("视频未就绪"));
        }, timeoutMs);
        video.addEventListener("loadeddata", onReady);
        video.addEventListener("canplay", onReady);
        video.addEventListener("error", onError);
    });
}

function waitForPaint(video: HTMLVideoElement) {
    return new Promise<void>((resolve) => {
        let done = false;
        const finish = () => {
            if (done) return;
            done = true;
            resolve();
        };
        const withRvf = video as HTMLVideoElement & {
            requestVideoFrameCallback?: (cb: (now: number, meta: unknown) => void) => number;
        };
        if (typeof withRvf.requestVideoFrameCallback === "function") {
            withRvf.requestVideoFrameCallback(() => finish());
            window.setTimeout(finish, 400);
            return;
        }
        requestAnimationFrame(() => requestAnimationFrame(() => finish()));
    });
}

export type VideoFrameKind = "first" | "last" | "current";

export async function captureVideoFrameByKind(video: HTMLVideoElement, kind: VideoFrameKind) {
    if (kind === "first") return captureVideoFrame(video, 0);
    if (kind === "last") {
        const duration = Number.isFinite(video.duration) ? video.duration : 0;
        if (!(duration > 0)) throw new Error("无法获取视频时长，请先播放片刻后再截尾帧");
        const offsets = [0.12, 0.25, 0.5, 1, Math.min(2, Math.max(0.12, duration * 0.04))];
        let lastError: Error | null = null;
        for (const offset of offsets) {
            try {
                return await captureVideoFrame(video, Math.max(0, duration - offset));
            } catch (error) {
                lastError = error instanceof Error ? error : new Error("截取尾帧失败");
            }
        }
        throw lastError || new Error("截取尾帧失败");
    }
    return captureVideoFrame(video, video.currentTime);
}

/** 从未挂载的远程/本地视频 URL 截一帧作未选中预览（失败返回 null，常见于跨域污染）。 */
export async function captureVideoPosterFromUrl(url: string): Promise<{ dataUrl: string; width: number; height: number } | null> {
    if (!url || url.startsWith("asset://")) return null;
    const tryCapture = async (src: string) => {
        const video = document.createElement("video");
        video.muted = true;
        video.playsInline = true;
        // metadata 足够取时长并 seek 首帧；避免 preload=auto 拖垮多视频画布
        video.preload = "metadata";
        if (/^https?:\/\//i.test(src)) video.crossOrigin = "anonymous";
        try {
            await new Promise<void>((resolve, reject) => {
                const onReady = () => {
                    cleanup();
                    resolve();
                };
                const onError = () => {
                    cleanup();
                    reject(new Error("视频预览加载失败"));
                };
                const cleanup = () => {
                    clearTimeout(timer);
                    video.removeEventListener("loadedmetadata", onReady);
                    video.removeEventListener("loadeddata", onReady);
                    video.removeEventListener("error", onError);
                };
                const timer = window.setTimeout(() => {
                    cleanup();
                    if (video.readyState >= 1) resolve();
                    else reject(new Error("视频预览加载失败"));
                }, 12000);
                video.addEventListener("loadedmetadata", onReady);
                video.addEventListener("loadeddata", onReady);
                video.addEventListener("error", onError);
                video.src = src;
            });
            const duration = Number.isFinite(video.duration) ? video.duration : 0;
            const at = duration > 0 ? Math.min(0.12, duration * 0.02) : 0;
            return await captureVideoFrame(video, at);
        } finally {
            video.removeAttribute("src");
            video.load();
        }
    };
    try {
        return await tryCapture(url);
    } catch {
        return null;
    }
}
