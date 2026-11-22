import type { PlanData } from '../types/pywebview';

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
