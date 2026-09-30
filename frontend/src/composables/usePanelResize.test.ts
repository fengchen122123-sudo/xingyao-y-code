import { beforeEach, describe, expect, it } from 'vitest';
import {
  RIGHT_PANEL_WIDTH_KEY,
  SIDEBAR_WIDTH_KEY,
  clampPanelWidth,
  computeRestoredRatioWidth,
  computeRestoredWidth,
  dragTargetWidth,
  panelMaxWidth,
  parseStoredRatio,
  ratioToWidth,
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

  it('小窗口下单侧面板不超过窗口宽度的 50%', () => {
    expect(panelMaxWidth(480, 800)).toBe(400);
    // 用户可把右面板拖到窗口一半：比例守卫不得低于 50%
    expect(panelMaxWidth(1280, 1000)).toBe(500);
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

describe('parseStoredRatio', () => {
  it('比例存储（<= 1）直接作为视口比例', () => {
    expect(parseStoredRatio('0.5', 1000)).toBe(0.5);
  });

  it('旧版像素存储按视口宽度换算成比例', () => {
    expect(parseStoredRatio('400', 1000)).toBe(0.4);
  });

  it('超过视口 50% 上限时收敛到 0.5', () => {
    expect(parseStoredRatio('0.8', 1000)).toBe(0.5);
    // 旧版像素存储的越界值同样收敛到 50%
    expect(parseStoredRatio('9999', 1000)).toBe(0.5);
  });

  it('无存储或非法值返回 null（视为未自定义）', () => {
    expect(parseStoredRatio(null, 1000)).toBeNull();
    expect(parseStoredRatio('abc', 1000)).toBeNull();
    expect(parseStoredRatio('0', 1000)).toBeNull();
    expect(parseStoredRatio('-1', 1000)).toBeNull();
  });
});

describe('ratioToWidth', () => {
  it('按视口比例换算像素宽度', () => {
    expect(ratioToWidth(0.5, 260, 1280, 1000)).toBe(500);
    expect(ratioToWidth(0.25, 260, 1280, 1920)).toBe(480);
  });

  it('换算结果夹取到 [min, min(配置上限, 视口 50%)]', () => {
    // 小视口：50% 守卫优先于配置上限
    expect(ratioToWidth(0.5, 260, 1280, 800)).toBe(400);
    // 超宽视口：固定上限 1280 兜底
    expect(ratioToWidth(0.5, 260, 1280, 4000)).toBe(1280);
    // 低于最小宽度时抬到 min
    expect(ratioToWidth(0.1, 260, 1280, 1000)).toBe(260);
  });
});

describe('computeRestoredRatioWidth', () => {
  it('无存储或非法值返回 null（用默认宽度）', () => {
    expect(computeRestoredRatioWidth(null, 260, 1280, 1000)).toBeNull();
    expect(computeRestoredRatioWidth('abc', 260, 1280, 1000)).toBeNull();
  });

  it('比例存储按当前视口换算，兼容旧版像素存储', () => {
    expect(computeRestoredRatioWidth('0.5', 260, 1280, 1000)).toBe(500);
    expect(computeRestoredRatioWidth('400', 260, 1280, 1000)).toBe(400);
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

// 视口宽度控制（jsdom 默认 1024）：比例联动测试需要模拟窗口缩放
function setViewportWidth(width: number) {
  Object.defineProperty(window, 'innerWidth', { value: width, writable: true, configurable: true });
}

describe('usePanelResize 比例联动（viewportRatio）', () => {
  function makeRatioApi(startWidth: number) {
    let width = startWidth;
    const api = usePanelResize({
      direction: 'right',
      storageKey: RIGHT_PANEL_WIDTH_KEY,
      minWidth: 260,
      maxWidth: 1280,
      defaultWidth: 330,
      viewportRatio: true,
      getWidth: () => width,
      setWidth: (w) => { width = w; },
    });
    return { api, getWidth: () => width };
  }

  it('拖拽结束按视口比例写入记忆，而不是像素', () => {
    setViewportWidth(1000);
    const { api, getWidth } = makeRatioApi(330);
    api.onHandlePointerdown(fakePointer(500));
    fakeWindowPointer('pointermove', 400);
    fakeWindowPointer('pointerup', 400);
    expect(getWidth()).toBe(430);
    expect(localStorage.getItem(RIGHT_PANEL_WIDTH_KEY)).toBe('0.43');
  });

  it('窗口放大后按同一比例跟随（拖到 50% 就一直是 50%）', () => {
    setViewportWidth(1000);
    const { api, getWidth } = makeRatioApi(330);
    api.onHandlePointerdown(fakePointer(500));
    fakeWindowPointer('pointermove', 0);
    fakeWindowPointer('pointerup', 0);
    expect(getWidth()).toBe(500);

    setViewportWidth(1920);
    window.dispatchEvent(new Event('resize'));
    expect(getWidth()).toBe(960);
  });

  it('窗口缩小时按比例收窄，不再挤压中间对话列', () => {
    setViewportWidth(1920);
    const { api, getWidth } = makeRatioApi(330);
    api.onHandlePointerdown(fakePointer(1920));
    fakeWindowPointer('pointermove', 0);
    fakeWindowPointer('pointerup', 0);
    expect(getWidth()).toBe(960);

    setViewportWidth(1200);
    window.dispatchEvent(new Event('resize'));
    expect(getWidth()).toBe(600);
  });

  it('未自定义过宽度时窗口缩放不改动默认宽度', () => {
    setViewportWidth(1000);
    const { getWidth } = makeRatioApi(330);
    setViewportWidth(1920);
    window.dispatchEvent(new Event('resize'));
    expect(getWidth()).toBe(330);
  });

  it('拖回默认宽度清除记忆后不再联动，双击重置同理', () => {
    setViewportWidth(1000);
    const { api, getWidth } = makeRatioApi(330);
    api.onHandlePointerdown(fakePointer(500));
    fakeWindowPointer('pointermove', 400);
    fakeWindowPointer('pointerup', 400);
    api.onHandlePointerdown(fakePointer(400));
    fakeWindowPointer('pointermove', 500);
    fakeWindowPointer('pointerup', 500);
    expect(getWidth()).toBe(330);
    expect(localStorage.getItem(RIGHT_PANEL_WIDTH_KEY)).toBeNull();

    setViewportWidth(2000);
    window.dispatchEvent(new Event('resize'));
    expect(getWidth()).toBe(330);

    api.onHandlePointerdown(fakePointer(500));
    fakeWindowPointer('pointermove', 400);
    fakeWindowPointer('pointerup', 400);
    api.resetWidth();
    setViewportWidth(1000);
    window.dispatchEvent(new Event('resize'));
    expect(getWidth()).toBe(330);
  });
});
