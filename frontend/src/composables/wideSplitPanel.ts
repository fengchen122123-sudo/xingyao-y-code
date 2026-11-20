import { clampPanelWidth, RIGHT_PANEL_DEFAULT_WIDTH } from './usePanelResize';
import type { RightPanelTabId } from '../stores/appStore';

/**
 * 宽屏 / 最大化侧边联动分栏策略：
 * - 当桌面窗口达到宽屏阈值（>= 1440px，覆盖常见 1080p 125% 缩放 1536px、
 *   1080p 100% 1920px、2K/4K 等桌面最大化场景）且用户处于默认的 'wide'
 *   布局模式时，自动展开右侧工作台并分配约 28% 的比例宽度，让中间对话保持在
 *   960–1060px 舒适阅读列宽，同时充分利用右侧空间展示文件 / 后台任务 / 子代理。
 */
export const WIDE_SPLIT_MIN_VIEWPORT_WIDTH = 1440;
export const WIDE_RIGHT_PANEL_RATIO = 0.28;
export const WIDE_RIGHT_PANEL_MIN_WIDTH = 380;
export const WIDE_RIGHT_PANEL_MAX_WIDTH = 680;

export function wideRightPanelDefaultWidth(viewportWidth: number): number {
  if (!Number.isFinite(viewportWidth) || viewportWidth < WIDE_SPLIT_MIN_VIEWPORT_WIDTH) {
    return RIGHT_PANEL_DEFAULT_WIDTH;
  }
  return clampPanelWidth(
    Math.round(viewportWidth * WIDE_RIGHT_PANEL_RATIO),
    WIDE_RIGHT_PANEL_MIN_WIDTH,
    WIDE_RIGHT_PANEL_MAX_WIDTH,
  );
}

export interface WideTabContext {
  workdir: string;
  bgTaskCount: number;
  subagentTaskCount: number;
}

/**
 * 为尚未打开任何页签的会话挑选宽屏分栏缺省页签：
 * 运行中的后台任务 > 子代理过程 > 项目文件树；纯自由对话返回 null，
 * 展示右侧面板自带的标签页选择器。
 */
export function pickWideDefaultTab(ctx: WideTabContext): RightPanelTabId | null {
  if (ctx.bgTaskCount > 0) return 'tasks';
  if (ctx.subagentTaskCount > 0) return 'subagent';
  if (ctx.workdir && ctx.workdir.trim().toUpperCase() !== 'GLOBAL') return 'files';
  return null;
}
