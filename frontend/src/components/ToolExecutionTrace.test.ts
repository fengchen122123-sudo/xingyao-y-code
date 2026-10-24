import { createPinia, setActivePinia } from 'pinia';
import { mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ToolExecutionTrace from './ToolExecutionTrace.vue';
import type { ToolDisplay, ToolExecutionCall } from '../utils/toolTrace';

// 行组件内 openSubagentDetail 依赖 pinia store
beforeEach(() => {
  setActivePinia(createPinia());
});


const commandDisplay = (command: string): ToolDisplay => ({
  category: 'command', action: '运行命令', target: command, command,
  metrics: {}, error_summary: '', is_skill: false,
});

const finishedCall = (id: string): ToolExecutionCall => ({
  id, name: 'execute_command', label: '运行命令 npm test',
  display: commandDisplay('npm test'),
  result: { ok: true, summary: '完成', result: 'ok', duration_ms: 800 },
});

const runningCall = (id: string, startedAt: number): ToolExecutionCall => ({
  id, name: 'execute_command', label: '运行命令 npm run build',
  started_at_ms: startedAt, display: commandDisplay('npm run build'),
});

const readCall = (id: string, path: string): ToolExecutionCall => ({
  id, name: 'read_file', label: `读取文件 ${path}`,
  display: {
    category: 'read', action: '读取文件', target: path, command: '',
    metrics: { line_count: 10 }, error_summary: '', is_skill: false,
  },
  result: { ok: true, summary: '读取完成', result: '', duration_ms: 10 },
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('ToolExecutionTrace', () => {
  it('announces the running trace via a visually hidden live region', () => {
    const wrapper = mount(ToolExecutionTrace, {
      props: {
        calls: [finishedCall('a'), runningCall('b', Date.now())],
        legacyDiffs: [],
        state: 'running',
      },
    });
    // 外壳标题已移除，但无障碍播报保留（视觉隐藏）
    expect(wrapper.get('.sr-only').text()).toBe('执行过程 · 2 项 · 进行中');
    expect(wrapper.find('.fold-head').exists()).toBe(false);
  });

  it('maps terminal states to Chinese labels', () => {
    const cases = [
      ['completed', '执行过程 · 1 项 · 已完成'],
      ['cancelled', '执行过程 · 1 项 · 已取消'],
      ['error', '执行过程 · 1 项 · 异常结束'],
    ] as const;
    for (const [state, title] of cases) {
      const wrapper = mount(ToolExecutionTrace, {
        props: { calls: [finishedCall('a')], legacyDiffs: [], state },
      });
      expect(wrapper.get('.sr-only').text()).toBe(title);
      wrapper.unmount();
    }
  });

  it('does not spin an unresolved historical call after the turn completed', () => {
    const wrapper = mount(ToolExecutionTrace, {
      props: {
        calls: [runningCall('legacy-empty-id', Date.now())],
        legacyDiffs: [],
        state: 'completed',
      },
    });

    expect(wrapper.find('.trace-icon .icon-spin').exists()).toBe(false);
    expect(wrapper.get('.trace-row').classes()).toContain('status-completed');
  });

  it('renders tool rows directly without a collapsible shell', () => {
    const wrapper = mount(ToolExecutionTrace, {
      props: { calls: [finishedCall('a')], legacyDiffs: [], state: 'completed' },
    });
    // 没有"执行过程 · N 项"折叠头，工具行直接可见
    expect(wrapper.find('.fold').exists()).toBe(false);
    expect(wrapper.get('.exec-trace').find('.trace-line').isVisible()).toBe(true);
    expect(wrapper.findAll('.trace-row')).toHaveLength(1);
  });

  it('runs exactly one interval while running and clears it on terminal state and unmount', async () => {
    vi.useFakeTimers();
    const setSpy = vi.spyOn(window, 'setInterval');
    const clearSpy = vi.spyOn(window, 'clearInterval');

    const wrapper = mount(ToolExecutionTrace, {
      props: { calls: [runningCall('b', Date.now() - 2000)], legacyDiffs: [], state: 'running' },
    });
    expect(setSpy).toHaveBeenCalledTimes(1);

    // 实时秒表驱动运行中行的耗时
    vi.advanceTimersByTime(3000);
    await wrapper.vm.$nextTick();
    expect(wrapper.get('.trace-meta').text()).toBe('5.0s');

    // 进入终态后清除定时器，且 elapsed 秒数不在 aria-live 区域内
    await wrapper.setProps({ state: 'completed' });
    expect(clearSpy).toHaveBeenCalledTimes(1);
    wrapper.unmount();

    // 再次挂载 running 轨迹：仍是恰好一个定时器，卸载即清理
    setSpy.mockClear();
    clearSpy.mockClear();
    const again = mount(ToolExecutionTrace, {
      props: { calls: [runningCall('c', Date.now())], legacyDiffs: [], state: 'running' },
    });
    expect(setSpy).toHaveBeenCalledTimes(1);
    again.unmount();
    expect(clearSpy).toHaveBeenCalledTimes(1);
  });

  it('announces state changes via aria-live while keeping elapsed outside the live region', () => {
    const wrapper = mount(ToolExecutionTrace, {
      props: { calls: [runningCall('b', Date.now())], legacyDiffs: [], state: 'running' },
    });
    expect(wrapper.get('.sr-only').attributes('aria-live')).toBe('polite');
    expect(wrapper.get('.trace-meta').attributes('aria-live')).toBeUndefined();
  });

  it('groups consecutive reads and appends a legacy diff row', async () => {
    const wrapper = mount(ToolExecutionTrace, {
      props: {
        calls: [readCall('r1', 'a.ts'), readCall('r2', 'b.ts')],
        legacyDiffs: [{ file_path: 'old.py', diff_text: '--- old.py\n+++ old.py\n-x\n+y' }],
        state: 'completed',
      },
    });
    const rows = wrapper.findAll('.trace-row');
    expect(rows).toHaveLength(2);
    // 分组行摘要只给类别与文件数，完整清单在展开区
    expect(rows[0].get('.trace-kind').text()).toBe('读取');
    expect(rows[0].get('.trace-target').text()).toBe('2 个文件');
    await rows[0].get('[data-row-toggle]').trigger('click');
    const files = rows[0].findAll('.trace-files li');
    expect(files).toHaveLength(2);
    expect(files[0].text()).toContain('a.ts');
    expect(files[1].text()).toContain('b.ts');
    // 未绑定的历史 Diff 行：行内给变更类别与文件，Diff 内容在展开区
    expect(rows[1].get('.trace-kind').text()).toBe('变更');
    expect(rows[1].get('.trace-target').text()).toContain('old.py');
    await rows[1].get('[data-row-toggle]').trigger('click');
    expect(rows[1].findAll('.diff-line.add')).toHaveLength(1);
  });
});
