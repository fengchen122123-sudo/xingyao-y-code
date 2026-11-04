import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { captureVideoFrame, captureVideoFrameByKind, captureVideoPosterFromUrl } from '../canvas-video-frame';

type VideoStubOptions = {
  currentTime?: number;
  duration?: number;
  readyState?: number;
  videoWidth?: number;
  videoHeight?: number;
  /** 赋值 currentTime 后是否派发 seeked（默认派发） */
  autoSeek?: boolean;
  /** 赋值 currentTime 是否真的改变读值（默认改变） */
  ignoreSeekTime?: boolean;
};

function createVideoStub(options: VideoStubOptions = {}) {
  const listeners = new Map<string, Set<() => void>>();
  let currentTime = options.currentTime ?? 0;
  const emit = (type: string) => {
    for (const listener of listeners.get(type) ?? []) listener();
  };
  const video: Record<string, unknown> = {
    duration: options.duration ?? 10,
    readyState: options.readyState ?? 2,
    videoWidth: options.videoWidth ?? 640,
    videoHeight: options.videoHeight ?? 360,
    ended: false,
    pause: vi.fn(),
    removeAttribute: vi.fn(),
    load: vi.fn(),
    requestVideoFrameCallback: (callback: () => void) => {
      queueMicrotask(() => callback());
      return 1;
    },
    addEventListener: (type: string, callback: () => void) => {
      const set = listeners.get(type) ?? new Set<() => void>();
      set.add(callback);
      listeners.set(type, set);
    },
    removeEventListener: (type: string, callback: () => void) => {
      listeners.get(type)?.delete(callback);
    },
  };
  Object.defineProperty(video, 'currentTime', {
    get: () => currentTime,
    set: (value: number) => {
      if (!options.ignoreSeekTime) currentTime = value;
      if (options.autoSeek !== false) queueMicrotask(() => emit('seeked'));
    },
    enumerable: true,
  });
  return { video: video as unknown as HTMLVideoElement, currentTime: () => currentTime };
}

const drawImage = vi.fn();

beforeEach(() => {
  drawImage.mockClear();
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    (() => ({ drawImage })) as unknown as typeof HTMLCanvasElement.prototype.getContext,
  );
  vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockImplementation(function (this: HTMLCanvasElement) {
    return `data:image/png;base64,${this.width}x${this.height}`;
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('canvas-video-frame', () => {
  it('captureVideoFrame：不传时间按当前帧截图，使用 videoWidth/videoHeight', async () => {
    const { video } = createVideoStub({ currentTime: 2, readyState: 2 });

    const frame = await captureVideoFrame(video);

    expect(frame).toEqual({ dataUrl: 'data:image/png;base64,640x360', width: 640, height: 360 });
    expect(drawImage).toHaveBeenCalledWith(video, 0, 0, 640, 360);
  });

  it('captureVideoFrame：videoWidth/videoHeight 为 0 时回退 1280x720', async () => {
    const { video } = createVideoStub({ videoWidth: 0, videoHeight: 0 });

    const frame = await captureVideoFrame(video);

    expect([frame.width, frame.height]).toEqual([1280, 720]);
  });

  it('captureVideoFrame：超出时长的时间被钳到 duration-0.001 再 seek', async () => {
    const { video, currentTime } = createVideoStub({ currentTime: 0, duration: 10 });

    const frame = await captureVideoFrame(video, 99);

    expect(currentTime()).toBeCloseTo(9.999, 5);
    expect(frame.width).toBe(640);
  });

  it('captureVideoFrameByKind：first 走 0 秒，last 在无时长时抛错', async () => {
    const { video, currentTime } = createVideoStub({ currentTime: 5, duration: 10 });

    await captureVideoFrameByKind(video, 'first');
    expect(currentTime()).toBe(0);

    const unknown = createVideoStub({ duration: Number.NaN });
    await expect(captureVideoFrameByKind(unknown.video, 'last')).rejects.toThrow('无法获取视频时长');
  });

  it('captureVideoFrame：seek 超时（5s）后拒绝并给出定位提示', async () => {
    vi.useFakeTimers();
    const { video } = createVideoStub({ currentTime: 0, duration: 10, autoSeek: false, ignoreSeekTime: true });

    const pending = captureVideoFrame(video, 3);
    const assertion = expect(pending).rejects.toThrow('视频定位超时');
    await vi.advanceTimersByTimeAsync(5000);
    await assertion;
  });

  it('captureVideoPosterFromUrl：空地址与 asset:// 直接返回 null', async () => {
    await expect(captureVideoPosterFromUrl('')).resolves.toBeNull();
    await expect(captureVideoPosterFromUrl('asset://some-asset')).resolves.toBeNull();
  });
});
