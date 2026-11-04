// M3-T02：直译自参考仓 web/src/app/(user)/canvas/utils/canvas-material-title.ts（§0.6）。
// 唯一改动：isCanvasImageNodeType 的 import 指向本仓已交付的 ./canvas-panorama（M2 批）。
import { CanvasNodeType, type CanvasNodeData } from "../types";
import { isCanvasImageNodeType } from "./canvas-panorama";

const MATERIAL_RE = /^素材(\d+)(?:_.+)?$/;
const RESOURCE_TITLE_RE = /^(图片|视频|音频|文本)\s*(\d+)$/;

/** Scan existing titles and return next `素材N`. */
export function nextMaterialTitle(nodes: CanvasNodeData[]): string {
    let max = 0;
    for (const node of nodes) {
        const match = MATERIAL_RE.exec((node.title || "").trim());
        if (!match) continue;
        const n = Number(match[1]);
        if (Number.isFinite(n) && n > max) max = n;
    }
    return `素材${max + 1}`;
}

/** 按节点类型生成「图片 1 / 视频 2」类标题，复制节点时保持类型编号。 */
export function nextResourceTitle(type: CanvasNodeType, nodes: CanvasNodeData[]): string {
    let prefix: string | null = null;
    if (type === CanvasNodeType.Video) prefix = "视频";
    else if (type === CanvasNodeType.Audio) prefix = "音频";
    else if (type === CanvasNodeType.Text) prefix = "文本";
    else if (isCanvasImageNodeType(type)) prefix = "图片";
    if (!prefix) return nextMaterialTitle(nodes);

    let max = 0;
    for (const node of nodes) {
        const match = RESOURCE_TITLE_RE.exec((node.title || "").trim());
        if (!match || match[1] !== prefix) continue;
        const n = Number(match[2]);
        if (Number.isFinite(n) && n > max) max = n;
    }
    return `${prefix} ${max + 1}`;
}

export function materialDerivedTitle(baseTitle: string, suffix: string): string {
    const base = (baseTitle || "").trim() || "素材";
    const root = base.replace(/_(首帧|尾帧|当前帧|音轨|无声|片段\d*)$/u, "");
    return `${root}_${suffix}`;
}
