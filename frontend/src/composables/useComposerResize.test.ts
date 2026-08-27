import { beforeEach, describe, expect, it } from 'vitest';
import {
  COMPOSER_HEIGHT_KEY,
  clampComposerHeight,
  composerMaxHeight,
  computeRestoredHeight,
  dragTargetHeight,
  useComposerResize,
} from './useComposerResize';

beforeEach(() => localStorage.removeItem(COMPOSER_HEIGHT_KEY));

describe('clampComposerHeight', () => {
  it('把高度限制在 [min, max] 区间内并取整', () => {
    expect(clampComposerHeight(45.4, 46, 461)).toBe(46);
    expect(clampComposerHeight(106.6, 46, 461)).toBe(107);
    expect(clampComposerHeight(1000.6, 46, 461)).toBe(461);
  });
});

describe('dragTargetHeight', () => {
  it('向上拖（clientY 变小）抬高输入框', () => {
    expect(dragTargetHeight(100, 40, 46, 46, 461)).toBe(106);
  });

  it('向下拖以默认高度为下限，不再更矮', () => {
    expect(dragTargetHeight(100, 500, 46, 46, 461)).toBe(46);
  });

  it('向上拖超过上限时停在上限', () => {
    expect(dragTargetHeight(100, -1000, 46, 46, 461)).toBe(461);
  });
});

describe('composerMaxHeight', () => {
  it('正常窗口取窗口高度的 60%', () => {
    expect(composerMaxHeight(46, 768)).toBe(461);
  });

  it('小窗口下保底默认高度 + 70px', () => {
    expect(composerMaxHeight(46, 100)).toBe(116);
    expect(composerMaxHeight(200, 300)).toBe(270);
  });
});

describe('computeRestoredHeight', () => {
  it('无存储、低于默认或非数字时视为未拉伸', () => {
    expect(computeRestoredHeight(null, 46, 768)).toBeNull();
    expect(computeRestoredHeight('30', 46, 768)).toBeNull();
    expect(computeRestoredHeight('abc', 46, 768)).toBeNull();
  });

  it('恢复记忆高度并按当前窗口夹取上限', () => {
    expect(computeRestoredHeight('200', 46, 768)).toBe(200);
    expect(computeRestoredHeight('99999', 46, 768)).toBe(461);
  });
});

// 直接驱动 composable 的拖拽流程（无组件实例，onMounted 恢复钩子不注册）
function fakePointer(clientY: number): PointerEvent {
  return { clientY, pointerId: 1, currentTarget: {}, preventDefault() {} } as unknown as PointerEvent;
}
function fakeWindowPointer(type: string, clientY: number) {
  const ev = new Event(type, { bubbles: true });
  Object.assign(ev, { clientY, pointerId: 1 });
  window.dispatchEvent(ev);
}

describe('useComposerResize 拖拽下限', () => {
  it('下限只按默认高度锁定一次，拉高后仍能拖回默认（回归：拉上去拖不下来）', () => {
    // 真实浏览器里拉伸会改变 offsetHeight；首次测量到的 60px 才是默认下限
    let el = { offsetHeight: 60 };
    const api = useComposerResize(() => el as unknown as HTMLTextAreaElement);

    // 第一次拖拽：从默认 60px 拉高到 160px 并松手（写入记忆）
    api.onHandlePointerdown(fakePointer(100));
    fakeWindowPointer('pointermove', 0);
    expect(api.textareaHeight.value).toBe(160);
    fakeWindowPointer('pointerup', 0);
    expect(localStorage.getItem(COMPOSER_HEIGHT_KEY)).toBe('160');

    // 拉伸后 offsetHeight 变成 160，再次按下向下拖必须能一路拖回默认 60px
    el = { offsetHeight: 160 };
    api.onHandlePointerdown(fakePointer(100));
    fakeWindowPointer('pointermove', 400);
    expect(api.textareaHeight.value).toBe(60);
    fakeWindowPointer('pointerup', 400);
    // 拖回默认视为未拉伸，清除记忆
    expect(localStorage.getItem(COMPOSER_HEIGHT_KEY)).toBeNull();
  });

  it('双击重置后再次拖拽仍以最初默认高度为下限', () => {
    let el = { offsetHeight: 50 };
    const api = useComposerResize(() => el as unknown as HTMLTextAreaElement);

    api.onHandlePointerdown(fakePointer(100));
    fakeWindowPointer('pointermove', 40);
    fakeWindowPointer('pointerup', 40);
    expect(api.textareaHeight.value).toBe(110);

    api.resetHeight();
    expect(api.textareaHeight.value).toBeNull();

    el = { offsetHeight: 110 };
    api.onHandlePointerdown(fakePointer(100));
    fakeWindowPointer('pointermove', 300);
    expect(api.textareaHeight.value).toBe(50);
    fakeWindowPointer('pointerup', 300);
  });
});
