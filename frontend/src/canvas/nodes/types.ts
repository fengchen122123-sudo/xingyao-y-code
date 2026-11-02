// 四类画布节点共用的类型与展示取值工具。
//
// 为什么单独一个文件：四个节点组件拿到的 data 形状必须一致（都由 CanvasPane
// 从 stores/canvas 的 CanvasNode 原样带下来），把形状和取值兜底集中在这里，
// 改一处就四处同步，不用在四个 SFC 里各写一遍 meta 解析。
import type { CanvasNode } from '../../stores/canvas';

/** vue-flow 透传给自定义节点的 data：整条 CanvasNode，节点只读不写 */
export interface CanvasNodeData {
  node: CanvasNode;
  /**
   * 该节点是否正在就地改图。
   *
   * 为什么走 data 而不是让节点自己读 store：vue-flow 传给节点组件的 props
   * 是固定的一组（id/type/data/selected…），自定义字段只能塞进 data 才带得下来；
   * 而节点「只负责画、不改数据」的既有原则也要求它只拿一个现成的布尔，
   * 不去反查 store.pendingEdits——那会让节点与 store 双向耦合、也难单独渲染。
   * 这个字段是纯 UI 态（不进 CanvasDoc），由 CanvasPane 从 store 映射后塞进来。
   */
  editPending?: boolean;
}

/** 节点操作按钮抛出的事件载荷：父级（CanvasPane）按 action 决定后续动作 */
export interface NodeActionPayload {
  id: string;
  /**
   * edit=送去修改（走对话）、config-shot=配置镜头、
   * edit-inline=就地改图（浮层里直接出图，不经过对话）、
   * annotate=画笔标注（阶段二的后端还没接，父级先只提示「即将支持」）。
   * 后两个是本轮新增，旧值原样保留，父级的 switch 里没命中时自然降级。
   */
  action: 'edit' | 'config-shot' | 'edit-inline' | 'annotate';
  /** 就地改图的用户指令；只有 action==='edit-inline' 时有值 */
  prompt?: string;
}

/** 读 meta 上的字符串字段：非字符串一律当作没有，避免渲染出 [object Object] */
export function metaString(node: CanvasNode, key: string): string {
  const value = node.meta?.[key];
  return typeof value === 'string' ? value : '';
}

/** 读 meta 上的数值字段：字符串数字也接受（落盘 JSON 里常见这种形态） */
export function metaNumber(node: CanvasNode, key: string, fallback = 0): number {
  const value = node.meta?.[key];
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) {
    return Number(value);
  }
  return fallback;
}

/** 读 meta 上的字符串列表（标签胶囊 / 参数行）；单个字符串包成单元素数组 */
export function metaStringList(node: CanvasNode, key: string): string[] {
  const value = node.meta?.[key];
  if (Array.isArray(value)) return value.filter((item): item is string => typeof item === 'string');
  if (typeof value === 'string' && value.trim() !== '') return [value];
  return [];
}

/** 从路径里取文件名：画布节点只展示文件名，完整路径留给右面板 */
export function fileBaseName(file?: string): string {
  if (!file) return '';
  const parts = file.split(/[\\/]/);
  return parts[parts.length - 1] || file;
}

/** 标题兜底：title → 文件名 → 调用方给的默认名 */
export function nodeTitle(node: CanvasNode, fallback: string): string {
  return node.title || fileBaseName(node.file) || fallback;
}

/**
 * 底部时间文案：优先 meta.time（可直接写「14:22 创建」这类文案）；
 * 否则用 meta.created_at（毫秒时间戳或 ISO 串）格式化成 HH:MM。
 * 都没有就返回空串，由组件决定不渲染这一格。
 */
export function nodeTimeText(node: CanvasNode): string {
  const text = metaString(node, 'time');
  if (text) return text;
  const created = node.meta?.created_at;
  const date =
    typeof created === 'number'
      ? new Date(created)
      : typeof created === 'string'
        ? new Date(created)
        : null;
  if (date && !Number.isNaN(date.getTime())) {
    const hh = String(date.getHours()).padStart(2, '0');
    const mm = String(date.getMinutes()).padStart(2, '0');
    return `${hh}:${mm} 创建`;
  }
  return '';
}

/** 进度百分比：夹到 0~100，非数字按 0 处理，避免进度条宽度算出 NaN */
export function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(100, Math.max(0, Math.round(value)));
}
