import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { canvasAudioProbeUrl, probeAudioDurationMs } from '../canvas-audio-duration';
import { canvasVideoProbeUrl, probeVideoDurationMs } from '../canvas-video-duration';
import { CanvasNodeType, type CanvasNodeData } from '../../types';

type MediaStub = {
  duration: number;
  preload: string;
  src: string;
  load: () => void;
  addEventListener: (type: string, callback: () => void) => void;
  removeEventListener: (type: string, callback: () => void) => void;
};

/** 只有元数据能力的 <video>/<audio> 替身：不加载真实媒体，靠手动 emit 驱动。 */
function createMediaStub(duration: number) {
  const listeners = new Map<string, Set<() => void>>();
  const stub: MediaStub = {
    duration,
    preload: '',
    src: '',
    load: vi.fn(),
    addEventListener: (type, callback) => {
      const set = listeners.get(type) ?? new Set<() => void>();
      set.add(callback);
      listeners.set(type, set);
    },
    removeEventListener: (type, callback) => {
      listeners.get(type)?.delete(callback);
    },
  };
  return {
    stub,
    emit: (type: string) => {
      for (const listener of listeners.get(type) ?? []) listener();
    },
  };
}

/** 把 document.createElement('video'|'audio') 换成替身，其余标签仍走真实实现。 */
function stubMediaElement(stub: MediaStub) {
  const createElement = document.createElement.bind(document);
  vi.spyOn(document, 'createElement').mockImplementation(((tagName: string) => {
    if (tagName === 'video' || tagName === 'audio') return stub;
    return createElement(tagName);
  }) as typeof document.createElement);
}

function mediaNode(content: string, type: CanvasNodeType = CanvasNodeType.Video): CanvasNodeData {
  return {
    id: 'n1',
    type,
    title: 'n1',
    position: { x: 0, y: 0 },
    width: 320,
    height: 180,
    metadata: { content },
  };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('canvas-video-duration', () => {
  it('probeVideoDurationMs：loadedmetadata 后按秒换算毫秒', async () => {
    const { stub, emit } = createMediaStub(12.5);
    stubMediaElement(stub);

    const pending = probeVideoDurationMs('http://127.0.0.1:9000/media/ok.mp4');
    emit('loadedmetadata');

    await expect(pending).resolves.toBe(12500);
    expect(stub.preload).toBe('metadata');
  });

  it('probeVideoDurationMs：时长为 Infinity（流式）按失败返回 undefined', async () => {
    const { stub, emit } = createMediaStub(Number.POSITIVE_INFINITY);
    stubMediaElement(stub);

    const pending = probeVideoDurationMs('http://127.0.0.1:9000/media/stream.mp4');
    emit('loadedmetadata');

    await expect(pending).resolves.toBeUndefined();
  });

  it('probeVideoDurationMs：10s 内无元数据即超时返回 undefined', async () => {
    vi.useFakeTimers();
    const { stub } = createMediaStub(Number.NaN);
    stubMediaElement(stub);

    const pending = probeVideoDurationMs('http://127.0.0.1:9000/media/timeout.mp4');
    await vi.advanceTimersByTimeAsync(10000);

    await expect(pending).resolves.toBeUndefined();
  });

  it('probeVideoDurationMs：不可播放地址直接返回 undefined，不建元素', async () => {
    const createElement = vi.spyOn(document, 'createElement');

    await expect(probeVideoDurationMs('asset://volc-asset')).resolves.toBeUndefined();
    await expect(probeVideoDurationMs('blob:http://127.0.0.1/abc')).resolves.toBeUndefined();
    expect(createElement).not.toHaveBeenCalled();
  });

  it('canvasVideoProbeUrl：只认 metadata.content，blob:/data:/asset:// 一律留空', () => {
    expect(canvasVideoProbeUrl(mediaNode('http://127.0.0.1:9000/media/a.mp4'))).toBe('http://127.0.0.1:9000/media/a.mp4');
    expect(canvasVideoProbeUrl(mediaNode('blob:http://127.0.0.1/abc'))).toBe('');
    expect(canvasVideoProbeUrl(mediaNode('data:video/mp4;base64,AAAA'))).toBe('');
    expect(canvasVideoProbeUrl(mediaNode('asset://volc-asset'))).toBe('');
    expect(canvasVideoProbeUrl({ ...mediaNode(''), metadata: undefined })).toBe('');
  });
});

describe('canvas-audio-duration', () => {
  it('probeAudioDurationMs：loadedmetadata 后按秒换算毫秒', async () => {
    const { stub, emit } = createMediaStub(3.2);
    stubMediaElement(stub);

    const pending = probeAudioDurationMs('http://127.0.0.1:9000/media/ok.mp3');
    emit('loadedmetadata');

    await expect(pending).resolves.toBe(3200);
  });

  it('probeAudioDurationMs：8s 内无元数据即超时返回 undefined', async () => {
    vi.useFakeTimers();
    const { stub } = createMediaStub(Number.NaN);
    stubMediaElement(stub);

    const pending = probeAudioDurationMs('http://127.0.0.1:9000/media/timeout.mp3');
    await vi.advanceTimersByTimeAsync(8000);

    await expect(pending).resolves.toBeUndefined();
  });

  it('canvasAudioProbeUrl：取 metadata.content，asset:// 跳过', () => {
    expect(canvasAudioProbeUrl(mediaNode('http://127.0.0.1:9000/media/a.mp3', CanvasNodeType.Audio))).toBe('http://127.0.0.1:9000/media/a.mp3');
    expect(canvasAudioProbeUrl(mediaNode('asset://volc-asset', CanvasNodeType.Audio))).toBe('');
    expect(canvasAudioProbeUrl({ ...mediaNode('', CanvasNodeType.Audio), metadata: undefined })).toBe('');
  });
});
