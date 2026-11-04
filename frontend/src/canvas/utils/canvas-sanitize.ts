// M2-T05：直译自参考仓 utils/canvas-sanitize.ts。
// 替换点（§0.6）：CanvasProject 类型 import 从 ../stores/use-canvas-store 改为 ../types。
import type { CanvasConnection, CanvasNodeData, CanvasProject } from "../types";

/** 修剪引用已删除节点的连线，并同步 nodeCount / connectionCount。 */
export function sanitizeCanvasProject<T extends Pick<CanvasProject, "nodes" | "connections" | "nodeCount" | "connectionCount">>(
    project: T,
): T {
    const nodes = Array.isArray(project.nodes) ? project.nodes : [];
    const nodeIds = new Set(nodes.map((node) => node.id));
    const connections = (Array.isArray(project.connections) ? project.connections : []).filter(
        (connection) => nodeIds.has(connection.fromNodeId) && nodeIds.has(connection.toNodeId),
    );
    return {
        ...project,
        nodes,
        connections,
        nodeCount: nodes.length,
        connectionCount: connections.length,
    };
}

function projectNodeCount(project: Pick<CanvasProject, "nodes" | "nodeCount">) {
    return project.nodes?.length ?? project.nodeCount ?? 0;
}

function projectConnectionCount(project: Pick<CanvasProject, "connections" | "connectionCount">) {
    return project.connections?.length ?? project.connectionCount ?? 0;
}

/** 节点被清空但仍有连线时属于异常快照，禁止写入云端。 */
export function isDestructiveCanvasSnapshot(nodes: CanvasNodeData[], connections: CanvasConnection[]) {
    return nodes.length === 0 && connections.length > 0;
}

/** 节点数量相对已有数据骤降时拒绝保存（防多标签/竞态覆盖）。 */
export function isSuspiciousCanvasNodeDrop(previousNodes: number, nextNodes: number) {
    if (previousNodes > 0 && nextNodes === 0) return true;
    if (previousNodes < 3 || nextNodes >= previousNodes) return false;
    const lost = previousNodes - nextNodes;
    if (lost >= 3) return true;
    if (previousNodes >= 8 && nextNodes < Math.ceil(previousNodes * 0.75)) return true;
    return false;
}

/** 连线骤降也属于异常快照（如 251→0）。 */
export function isSuspiciousCanvasProjectShrink(
    previous: Pick<CanvasProject, "nodes" | "connections" | "nodeCount" | "connectionCount">,
    next: Pick<CanvasProject, "nodes" | "connections" | "nodeCount" | "connectionCount">,
) {
    const prevNodes = projectNodeCount(previous);
    const nextNodes = projectNodeCount(next);
    const prevConns = projectConnectionCount(previous);
    const nextConns = projectConnectionCount(next);
    if (isSuspiciousCanvasNodeDrop(prevNodes, nextNodes)) return true;
    if (prevConns >= 10 && nextConns === 0 && nextNodes > 0) return true;
    if (prevConns >= 20 && nextConns < Math.floor(prevConns * 0.5)) return true;
    return false;
}

function canvasFilledContentCount(project: Pick<CanvasProject, "nodes">) {
    const nodes = Array.isArray(project.nodes) ? project.nodes : [];
    return nodes.filter((node) => {
        const meta = node.metadata || {};
        return Boolean(meta.content || meta.storageKey || meta.thumbnailContent || meta.thumbnailStorageKey);
    }).length;
}

/** 合并两份完整画布时，是否应保留更满的一方。 */
export function shouldPreferRicherCanvasSnapshot(
    previous: Pick<CanvasProject, "nodes" | "connections" | "nodeCount" | "connectionCount" | "updatedAt">,
    next: Pick<CanvasProject, "nodes" | "connections" | "nodeCount" | "connectionCount" | "updatedAt">,
) {
    if (isSuspiciousCanvasProjectShrink(previous, next)) return true;
    const prevNodes = projectNodeCount(previous);
    const nextNodes = projectNodeCount(next);
    if (prevNodes === nextNodes && prevNodes > 0) {
        const prevFilled = canvasFilledContentCount(previous);
        const nextFilled = canvasFilledContentCount(next);
        if (prevFilled !== nextFilled) return prevFilled > nextFilled;
    }
    return false;
}
