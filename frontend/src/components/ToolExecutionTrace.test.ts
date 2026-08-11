import { mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ToolExecutionTrace from './ToolExecutionTrace.vue';
import type { ToolDisplay, ToolExecutionCall } from '../utils/toolTrace';


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
  it('titles a running trace with the raw call count', () => {
    const wrapper = mount(ToolExecutionTrace, {
      props: {
        calls: [finishedCall('a'), runningCall('b', Date.now())],
        legacyDiffs: [],
        state: 'running',
      },
    });
    expect(wrapper.get('.trace-title').text()).toBe('执行过程 · 2 项 · 进行中');
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
      expect(wrapper.get('.trace-title').text()).toBe(title);
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

    expect(wrapper.find('.trace-icon.spin').exists()).toBe(false);
    expect(wrapper.get('.trace-row').classes()).toContain('status-completed');
  });

  it('toggles aria-expanded from the header button and defaults to collapsed', async () => {
    const wrapper = mount(ToolExecutionTrace, {
      props: { calls: [finishedCall('a')], legacyDiffs: [], state: 'completed' },
    });
    const button = wrapper.get('button.trace-head');
    // 默认收起（图二样式）：只显示标题行，详情折叠
    expect(button.attributes('aria-expanded')).toBe('false');
    expect(wrapper.find('.trace-body').attributes('style')).toContain('display: none');
    await button.trigger('click');
    expect(button.attributes('aria-expanded')).toBe('true');
    expect(wrapper.find('.trace-body').attributes('style') || '').not.toContain('display: none');
    await button.trigger('click');
    expect(button.attributes('aria-expanded')).toBe('false');
    expect(wrapper.find('.trace-body').attributes('style')).toContain('display: none');
  });

  it('runs exactly one interval while running and clears it on terminal state and unmount', async () => {
    vi.useFakeTimers();
    const setSpy = vi.spyOn(window, 'setInterval');
    const clearSpy = vi.spyOn(window, 'clearInterval');

    const wrapper = mount(ToolExecutionTrace, {
      props: { calls: [runningCall('b', Date.now() - 2000)], legacyDiffs: [], state: 'running' },
    });
    expect(setSpy).toHaveBeenCalledTimes(1);

    // 实时秒表驱动运行中行的时长徽标
    vi.advanceTimersByTime(3000);
    await wrapper.vm.$nextTick();
    expect(wrapper.get('.trace-duration').text()).toBe('5.0s');

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
    expect(wrapper.get('.trace-title').attributes('aria-live')).toBe('polite');
    expect(wrapper.get('.trace-duration').attributes('aria-live')).toBeUndefined();
  });

  it('groups consecutive reads and appends a legacy diff row', () => {
    const wrapper = mount(ToolExecutionTrace, {
      props: {
        calls: [readCall('r1', 'a.ts'), readCall('r2', 'b.ts')],
        legacyDiffs: [{ file_path: 'old.py', diff_text: '--- old.py\n+++ old.py\n-x\n+y' }],
        state: 'completed',
      },
    });
    const rows = wrapper.findAll('.trace-row');
    expect(rows).toHaveLength(2);
    expect(rows[0].text()).toContain('读取文件');
    expect(rows[0].text()).toContain('a.ts');
    expect(rows[0].text()).toContain('b.ts');
    expect(rows[1].text()).toContain('未绑定的历史文件变更');
    expect(rows[1].text()).toContain('old.py');
  });
});
