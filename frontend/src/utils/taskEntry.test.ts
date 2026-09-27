import { describe, expect, it } from 'vitest';
import { taskEntryState } from './taskEntry';
import type { PlanData } from '../types/pywebview';

const plan = (items: Array<{ id: string; status: 'todo' | 'in_progress' | 'done' }>): PlanData => ({
  title: '任务计划',
  progress: 50,
  items: items.map(i => ({ id: i.id, text: 'item ' + i.id, status: i.status })),
});

describe('taskEntryState', () => {
  it('没有计划时入口不可见', () => {
    expect(taskEntryState({})).toEqual({ visible: false, activeCount: 0 });
    expect(taskEntryState({ plan: { title: '', items: [], progress: 0 } })).toEqual({
      visible: false,
      activeCount: 0,
    });
  });

  it('计划存在即显示入口，角标只统计 in_progress 条目', () => {
    const state = taskEntryState({
      plan: plan([{ id: '1', status: 'done' }, { id: '2', status: 'in_progress' }, { id: '3', status: 'todo' }]),
    });
    expect(state.visible).toBe(true);
    expect(state.activeCount).toBe(1);
  });

  it('全部完成时入口仍可见（可回看计划），但无角标', () => {
    const state = taskEntryState({ plan: plan([{ id: '1', status: 'done' }]) });
    expect(state.visible).toBe(true);
    expect(state.activeCount).toBe(0);
  });

  it('多个进行中条目时角标累加', () => {
    const state = taskEntryState({
      plan: plan([{ id: '1', status: 'in_progress' }, { id: '2', status: 'in_progress' }]),
    });
    expect(state.activeCount).toBe(2);
  });
});
