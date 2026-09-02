import { beforeEach, describe, expect, it } from 'vitest';
import {
  RIGHT_PANEL_WIDTH_KEY,
  SIDEBAR_WIDTH_KEY,
  clampPanelWidth,
  computeRestoredWidth,
  dragTargetWidth,
  panelMaxWidth,
  usePanelResize,
} from './usePanelResize';

beforeEach(() => {
  localStorage.removeItem(SIDEBAR_WIDTH_KEY);
  localStorage.removeItem(RIGHT_PANEL_WIDTH_KEY);
});

describe('clampPanelWidth', () => {
  it('把宽度限制在 [min, max] 区间内并取整', () => {
    expect(clampPanelWidth(100.4, 180, 480)).toBe(180);
    expect(clampPanelWidth(240.6, 180, 480)).toBe(241);
    expect(clampPanelWidth(999, 180, 480)).toBe(480);
  });

  it('上限小于下限时按交换后的区间夹取（极端小窗口兜底）', () => {
    expect(clampPanelWidth(100, 180, 120)).toBe(120);
  });
});

describe('panelMaxWidth', () => {
  it('窗口够宽时取配置上限', () => {
    expect(panelMaxWidth(480, 2400)).toBe(480);
  });

  it('小窗口下单侧面板不超过窗口宽度的 45%', () => {
    expect(panelMaxWidth(480, 800)).toBe(360);
  });
});

describe('dragTargetWidth', () => {
  it('左侧栏：向右拖（clientX 变大）变宽', () => {
    expect(dragTargetWidth('left', 100, 160, 240, 180, 480)).toBe(300);
  });

  it('左侧栏：向左拖收窄且不越过下限', () => {
    expect(dragTargetWidth('left', 300, 0, 240, 180, 480)).toBe(180);
  });

  it('右侧面板：向左拖（clientX 变小）变宽', () => {
    expect(dragTargetWidth('right', 500, 420, 330, 260, 640)).toBe(410);
  });

  it('右侧面板：向右拖收窄且不越过下限', () => {
    expect(dragTargetWidth('right', 100, 500, 330, 260, 640)).toBe(260);
  });

  it('变宽不越过上限', () => {
    expect(dragTargetWidth('left', 0, 9999, 240, 180, 480)).toBe(480);
  });
});

describe('computeRestoredWidth', () => {
  it('无存储或非数字时返回 null（使用默认宽度）', () => {
    expect(computeRestoredWidth(null, 180, 480)).toBeNull();
    expect(computeRestoredWidth('abc', 180, 480)).toBeNull();
  });

  it('恢复记忆宽度并夹取到 [min, max]', () => {
    expect(computeRestoredWidth('300', 180, 480)).toBe(300);
    expect(computeRestoredWidth('99999', 180, 480)).toBe(480);
    expect(computeRestoredWidth('10', 180, 480)).toBe(180);
  });
});

// 直接驱动 composable 的拖拽流程（无组件实例，仅使用 window 监听部分，
// 模式同 useComposerResize.test.ts）
function fakePointer(clientX: number): PointerEvent {
  return { clientX, pointerId: 1, currentTarget: {}, preventDefault() {} } as unknown as PointerEvent;
}
function fakeWindowPointer(type: string, clientX: number) {
  const ev = new Event(type, { bubbles: true });
  Object.assign(ev, { clientX, pointerId: 1 });
  window.dispatchEvent(ev);
}

describe('usePanelResize 拖拽流程', () => {
  function makeApi(direction: 'left' | 'right', startWidth: number) {
    let width = startWidth;
    const api = usePanelResize({
      direction,
      storageKey: SIDEBAR_WIDTH_KEY,
      minWidth: 180,
      maxWidth: 480,
      defaultWidth: 240,
      getWidth: () => width,
      setWidth: (w) => { width = w; },
    });
    return { api, getWidth: () => width };
  }

  it('拖拽实时更新宽度，松手写入 localStorage', () => {
    const { api, getWidth } = makeApi('left', 240);
    api.onHandlePointerdown(fakePointer(100));
    expect(api.isResizing.value).toBe(true);
    fakeWindowPointer('pointermove', 150);
    expect(getWidth()).toBe(290);
    fakeWindowPointer('pointerup', 150);
    expect(api.isResizing.value).toBe(false);
    expect(localStorage.getItem(SIDEBAR_WIDTH_KEY)).toBe('290');
  });

  it('direction=right 时向左拖变宽', () => {
    const { api, getWidth } = makeApi('right', 330);
    api.onHandlePointerdown(fakePointer(500));
    fakeWindowPointer('pointermove', 430);
    expect(getWidth()).toBe(400);
    fakeWindowPointer('pointerup', 430);
    expect(localStorage.getItem(SIDEBAR_WIDTH_KEY)).toBe('400');
  });

  it('拖回默认宽度视为未自定义，清除记忆', () => {
    const { api } = makeApi('left', 240);
    api.onHandlePointerdown(fakePointer(100));
    fakeWindowPointer('pointermove', 160);
    fakeWindowPointer('pointerup', 160);
    expect(localStorage.getItem(SIDEBAR_WIDTH_KEY)).toBe('300');

    api.onHandlePointerdown(fakePointer(160));
    fakeWindowPointer('pointermove', 100);
    fakeWindowPointer('pointerup', 100);
    expect(localStorage.getItem(SIDEBAR_WIDTH_KEY)).toBeNull();
  });

  it('双击重置：恢复默认宽度并清除记忆', () => {
    const { api, getWidth } = makeApi('left', 240);
    api.onHandlePointerdown(fakePointer(100));
    fakeWindowPointer('pointermove', 200);
    fakeWindowPointer('pointerup', 200);
    expect(getWidth()).toBe(340);

    api.resetWidth();
    expect(getWidth()).toBe(240);
    expect(localStorage.getItem(SIDEBAR_WIDTH_KEY)).toBeNull();
  });

  it('小窗口下拖拽上限按视口 45% 夹取', () => {
    const { api, getWidth } = makeApi('left', 240);
    api.onHandlePointerdown(fakePointer(0));
    fakeWindowPointer('pointermove', 9999);
    // 上限取 panelMaxWidth(maxWidth) 的实时计算值，而非裸配置上限
    expect(getWidth()).toBe(panelMaxWidth(480));
    expect(getWidth()).toBeLessThanOrEqual(480);
    fakeWindowPointer('pointerup', 9999);
  });
});
