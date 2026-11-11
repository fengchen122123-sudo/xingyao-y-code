import type { PlanData } from '../types/pywebview';

// 会话头部“计划”入口的可见性与角标：星瑶创建了任务计划时显示，
// 角标统计进行中的计划条目；计划数据由 plan_updated 事件流实时驱动。

export interface TaskEntryState {
  visible: boolean;
  activeCount: number;
}

export function taskEntryState(input: { plan?: PlanData }): TaskEntryState {
  const planItems = input.plan?.items ?? [];
  return {
    visible: planItems.length > 0,
    activeCount: planItems.filter((item) => item.status === 'in_progress').length,
  };
}
