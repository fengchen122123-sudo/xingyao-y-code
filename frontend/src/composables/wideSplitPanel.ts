import { clampPanelWidth, RIGHT_PANEL_DEFAULT_WIDTH } from './usePanelResize';
import type { RightPanelTabId } from '../stores/appStore';

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

export function pickWideDefaultTab(ctx: WideTabContext): RightPanelTabId | null {
  if (ctx.bgTaskCount > 0) return 'tasks';
  if (ctx.subagentTaskCount > 0) return 'subagent';
  if (ctx.workdir && ctx.workdir.trim().toUpperCase() !== 'GLOBAL') return 'files';
  return null;
}
