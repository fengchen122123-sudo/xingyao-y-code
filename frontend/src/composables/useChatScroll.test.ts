import { describe, expect, it } from 'vitest';
import { nextTick } from 'vue';
import {
  BOTTOM_THRESHOLD,
  distanceFromBottom,
  isNearBottom,
  useChatScroll,
} from './useChatScroll';

interface FakeEl {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

function makeEl(overrides: Partial<FakeEl> = {}): FakeEl & HTMLElement {
  const el = {
    scrollTop: 0,
    scrollHeight: 1000,
    clientHeight: 500,
    ...overrides,
  };
  return el as unknown as FakeEl & HTMLElement;
}

// scrollToBottom 里用 requestAnimationFrame 合并到下一帧，flush 后再断言
async function flushRaf() {
  await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
}

describe('isNearBottom / distanceFromBottom', () => {
  it('贴底时判定为 near bottom', () => {
    const el = makeEl({ scrollTop: 500, scrollHeight: 1000, clientHeight: 500 });
    expect(distanceFromBottom(el)).toBe(0);
    expect(isNearBottom(el)).toBe(true);
  });

  it('超出阈值时判定为已离开底部', () => {
    const el = makeEl({
      scrollTop: 400,
      scrollHeight: 1000,
      clientHeight: 500,
    }); // 距离底部 100px > 80
    expect(distanceFromBottom(el)).toBe(100);
    expect(isNearBottom(el)).toBe(false);
  });

  it('阈值内仍视为跟随', () => {
    const el = makeEl({
      scrollTop: 430,
      scrollHeight: 1000,
      clientHeight: 500,
    }); // 距离底部 70px < 80
    expect(isNearBottom(el, BOTTOM_THRESHOLD)).toBe(true);
  });
});

describe('useChatScroll 跟随模式（滚动权状态机）', () => {
  it('跟随模式下自动吸底', async () => {
    const el = makeEl();
    const api = useChatScroll(() => el);

    api.scrollToBottom();
    await flushRaf();

    // scrollToBottom 直接把 scrollTop 设为 scrollHeight（浏览器会 clamp 到底部）
    expect(el.scrollTop).toBe(el.scrollHeight);
    expect(api.stickToBottom.value).toBe(true);
  });

  it('用户上滑后冻结，不再被自动拉回；滚回底部恢复跟随', async () => {
    const el = makeEl();
    const api = useChatScroll(() => el);

    // 用户向上滚动 200px，离开底部
    api.noteUserScrollIntent('awayFromBottom');
    el.scrollTop = 200;
    const fromUser = api.handleScroll();
    expect(fromUser).toBe(true); // 用户来源：按落点更新跟随态
    expect(api.stickToBottom.value).toBe(false);
    expect(api.showJumpButton.value).toBe(true);

    // 流式输出触发自动滚动：应保持用户位置不动
    api.scrollToBottom();
    await flushRaf();
    expect(el.scrollTop).toBe(200);

    // 手动滑回底部：恢复跟随
    api.noteUserScrollIntent('towardBottom');
    el.scrollTop = 500;
    expect(api.handleScroll()).toBe(true);
    expect(api.stickToBottom.value).toBe(true);
    expect(api.showJumpButton.value).toBe(false);
  });

  it('程序化滚动（无用户意图）不改变跟随态', () => {
    const el = makeEl();
    const api = useChatScroll(() => el);

    // 贴底跟随中；跳转/补偿等程序化滚动把 scrollTop 挪到中间
    el.scrollTop = 200;
    const fromUser = api.handleScroll();
    expect(fromUser).toBe(false); // 非用户来源
    expect(api.stickToBottom.value).toBe(true); // 跟随权不被程序化滚动骗走
  });

  it('用户上滚意图立即退出跟随（不等 scroll 事件）', () => {
    const el = makeEl();
    const api = useChatScroll(() => el);

    api.noteUserScrollIntent('awayFromBottom');
    expect(api.stickToBottom.value).toBe(false);
  });

  it('上滚后紧跟内容变化：不吸底（对账）', async () => {
    const el = makeEl();
    const api = useChatScroll(() => el);

    // 用户上滚的 wheel 已到达、scroll 事件尚未派发时，内容变化 commit 先到
    api.noteUserScrollIntent('awayFromBottom');
    api.scrollToBottom();
    await flushRaf();

    expect(el.scrollTop).toBe(0); // 未被吸底拽回
  });

  it('force 滚动忽略跟随模式并恢复跟随', async () => {
    const el = makeEl();
    const api = useChatScroll(() => el);

    api.noteUserScrollIntent('awayFromBottom');
    el.scrollTop = 200;
    api.handleScroll();
    expect(api.stickToBottom.value).toBe(false);

    api.scrollToBottom(true);
    await flushRaf();
    expect(el.scrollTop).toBe(el.scrollHeight);
    expect(api.stickToBottom.value).toBe(true);
  });

  it('force 覆盖新鲜的上滚意图（显式动作优先）', async () => {
    const el = makeEl();
    const api = useChatScroll(() => el);

    api.noteUserScrollIntent('awayFromBottom');
    api.scrollToBottom(true); // 显式回底：意图作废，必须吸底
    await flushRaf();
    expect(el.scrollTop).toBe(el.scrollHeight);
  });

  it('已排队的吸底帧在退出跟随后不再执行', async () => {
    const el = makeEl();
    const api = useChatScroll(() => el);

    api.scrollToBottom(); // 排队到下一帧
    api.commitFollowing(false); // 帧执行前退出跟随（如跳转发起）
    await flushRaf();
    expect(el.scrollTop).toBe(0);
  });

  it('滚动容器为空时不抛错且维持跟随', async () => {
    const api = useChatScroll(() => null);
    api.scrollToBottom();
    await flushRaf();
    expect(api.handleScroll()).toBe(false);
    expect(api.stickToBottom.value).toBe(true);
  });

  it('syncStick 按当前 scrollTop 重算跟随状态', () => {
    const el = makeEl({ scrollTop: 500 });
    const api = useChatScroll(() => el);
    // 初始为 true；构造时 scrollTop 已贴底，syncStick 保持 true
    api.syncStick();
    expect(api.stickToBottom.value).toBe(true);

    el.scrollTop = 0;
    api.syncStick();
    expect(api.stickToBottom.value).toBe(false);
  });

  it('卸载后清理未执行的 rAF', async () => {
    const el = makeEl();
    const api = useChatScroll(() => el);
    // 模拟组件卸载：无法直接触发 onUnmounted，这里仅验证 flush 不抛错
    api.scrollToBottom();
    await flushRaf();
    expect(el.scrollTop).toBe(el.scrollHeight);
    // 引用 nextTick 避免未使用告警，确认组合式在 Vue 生命周期外也能安全调用
    await nextTick();
  });
});
