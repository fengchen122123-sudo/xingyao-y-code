// 旧版 Token 统计页专用的环图工具（改版前的 modelColor / donutGradient）。
// 仅供 TokenUsageSettingsLegacy.vue 使用；新旧版式去留确定后可整体删除。
const MODEL_COLORS = [
  '#6D9CFF', '#7BDCB5', '#B79CFF', '#FF9F7A', '#FFD166',
  '#62D0E8', '#F38BA8', '#A6E3A1', '#CBA6F7', '#89B4FA',
];

export interface DonutModel {
  key: string;
  total: number;
}

export function modelColor(key: string): string {
  let hash = 2166136261;
  for (let index = 0; index < key.length; index += 1) {
    hash ^= key.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return MODEL_COLORS[(hash >>> 0) % MODEL_COLORS.length];
}

export function donutGradient(models: DonutModel[]): string {
  const visible = models.filter(model => Number(model.total) > 0);
  const total = visible.reduce((sum, model) => sum + Number(model.total), 0);
  if (total <= 0) return 'conic-gradient(var(--bg3) 0% 100%)';

  let cursor = 0;
  const segments = visible.map((model, index) => {
    const start = cursor;
    cursor = index === visible.length - 1
      ? 100
      : cursor + (Number(model.total) / total) * 100;
    return `${modelColor(model.key)} ${start.toFixed(3)}% ${cursor.toFixed(3)}%`;
  });
  return `conic-gradient(${segments.join(', ')})`;
}
