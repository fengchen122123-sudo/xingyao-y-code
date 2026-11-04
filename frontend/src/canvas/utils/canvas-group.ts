// M3-T02：直译自参考仓 web/src/app/(user)/canvas/utils/canvas-group.ts（§0.6）。
// 导出常量与函数签名逐字保留：GROUP_PADDING / getNodeBounds / getSelectionPlusWorld /
// findGroupDropTarget / snapNodesIntoGroup / findContainingGroupId。
// 参考仓的 CanvasNodeType 是 enum，本仓是 const 对象 + 同名类型别名（tsconfig erasableSyntaxOnly），
// 用法（CanvasNodeType.Group 既作值又作类型）两边一致，故调用方无需改动。
import { CanvasNodeType, type CanvasNodeData, type Position } from "../types";

export const GROUP_PADDING = 24;

export function getNodeBounds(nodes: CanvasNodeData[]) {
    return nodes.reduce(
        (bounds, node) => ({
            left: Math.min(bounds.left, node.position.x),
            top: Math.min(bounds.top, node.position.y),
            right: Math.max(bounds.right, node.position.x + node.width),
            bottom: Math.max(bounds.bottom, node.position.y + node.height),
        }),
        { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity },
    );
}

/** 多选工具栏右侧「+」十字的画布坐标（与 CanvasSelectionToolbar 对齐） */
export function getSelectionPlusWorld(nodes: CanvasNodeData[], scale = 1): Position | null {
    if (nodes.length < 2) return null;
    const bounds = getNodeBounds(nodes);
    return {
        x: bounds.right + 28 / Math.max(scale, 0.05),
        y: (bounds.top + bounds.bottom) / 2,
    };
}

export function findGroupDropTarget(movedIds: Set<string>, nodes: CanvasNodeData[]) {
    if (nodes.some((node) => movedIds.has(node.id) && node.type === CanvasNodeType.Group)) return null;

    const movingNodes = nodes.filter((node) => movedIds.has(node.id) && node.type !== CanvasNodeType.Group);
    return [...nodes].reverse().find((group) => {
        if (group.type !== CanvasNodeType.Group || movedIds.has(group.id)) return false;
        return movingNodes.some((node) => {
            const centerX = node.position.x + node.width / 2;
            const centerY = node.position.y + node.height / 2;
            return centerX >= group.position.x && centerX <= group.position.x + group.width && centerY >= group.position.y && centerY <= group.position.y + group.height;
        });
    }) || null;
}

export function snapNodesIntoGroup(movedIds: Set<string>, nodes: CanvasNodeData[], group: CanvasNodeData) {
    const movingNodes = nodes.filter((node) => movedIds.has(node.id) && node.type !== CanvasNodeType.Group);
    if (!movingNodes.length) return nodes;

    const bounds = getNodeBounds(movingNodes);
    const left = group.position.x + GROUP_PADDING;
    const top = group.position.y + GROUP_PADDING;
    const right = group.position.x + group.width - GROUP_PADDING;
    const bottom = group.position.y + group.height - GROUP_PADDING;
    const dx = bounds.left < left ? left - bounds.left : bounds.right > right ? right - bounds.right : 0;
    const dy = bounds.top < top ? top - bounds.top : bounds.bottom > bottom ? bottom - bounds.bottom : 0;

    return nodes.map((node) => {
        if (!movedIds.has(node.id) || node.type === CanvasNodeType.Group) return node;
        return {
            ...node,
            position: { x: node.position.x + dx, y: node.position.y + dy },
            metadata: { ...node.metadata, groupId: group.id },
        };
    });
}

export function findContainingGroupId(node: CanvasNodeData, nodes: CanvasNodeData[]) {
    const centerX = node.position.x + node.width / 2;
    const centerY = node.position.y + node.height / 2;
    return [...nodes].reverse().find((group) => group.type === CanvasNodeType.Group && centerX >= group.position.x && centerX <= group.position.x + group.width && centerY >= group.position.y && centerY <= group.position.y + group.height)?.id;
}
