import { createPinia, setActivePinia } from 'pinia';
import { mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it } from 'vitest';
import ToolExecutionRow from './ToolExecutionRow.vue';
import { useAppStore } from '../stores/appStore';
import type { ToolDisplay, ToolTraceRow } from '../utils/toolTrace';

// 组件内 openSubagentDetail 依赖 pinia store
beforeEach(() => {
  setActivePinia(createPinia());
});


function failedCommandRow(): ToolTraceRow {
  return {
    id: 'bad', kind: 'command', status: 'failed', action: '运行命令',
    detail: 'pytest -q', command: 'pytest -q',
    errorSummary: 'ModuleNotFoundError: pytest', count: 1, durationMs: 3200,
    metrics: {},
    calls: [{
      id: 'bad', name: 'execute_command', label: '运行命令',
      result: { ok: false, summary: '失败', result: '命令执行失败（退出码 1）' },
    }],
  };
}


function mutationRowWithDiff(): ToolTraceRow {
  return {
    id: 'edit-1', kind: 'mutation', status: 'completed', action: '修改文件',
    detail: 'a.py', command: '', errorSummary: '', count: 1, durationMs: 100,
    metrics: {},
    calls: [{
      id: 'edit-1', name: 'edit_file', label: '修改文件 a.py',
      result: { ok: true, summary: '完成', result: '' },
      diff: { file_path: 'a.py', diff_text: '--- a.py\n+++ a.py\n-old\n+new' },
    }],
  };
}


function groupedReadRow(): ToolTraceRow {
  const readDisplay = (path: string): ToolDisplay => ({
    category: 'read',
    action: '读取文件',
    target: path,
    command: '',
    metrics: { line_count: 60 },
    error_summary: '',
    is_skill: false,
  });
  const readCall = (id: string, path: string) => ({
    id,
    name: 'read_file',
    label: `读取文件 ${path}`,
    display: readDisplay(path),
    result: { ok: true, summary: '读取完成', result: '', duration_ms: 20 },
  });
  return {
    id: 'read-group:r1', kind: 'read-group', status: 'completed', action: '读取文件',
    detail: '', command: '', errorSummary: '', count: 2, durationMs: 40,
    metrics: { line_count: 120 },
    calls: [readCall('r1', 'alpha.ts'), readCall('r2', 'beta.ts')],
  };
}


function runningRowWithoutDuration(): ToolTraceRow {
  return {
    id: 'run-1', kind: 'command', status: 'running', action: '运行命令',
    detail: 'npm run build', command: 'npm run build', errorSummary: '', count: 1,
    metrics: {},
    calls: [{ id: 'run-1', name: 'execute_command', label: '运行命令 npm run build' }],
  };
}


function subagentRow(): ToolTraceRow {
  return {
    id: 'sub-1', kind: 'tool', status: 'running', action: '运行子代理',
    detail: '', command: '', errorSummary: '', count: 1,
    metrics: {},
    calls: [{
      id: 'sub-1', name: 'run_subagent', label: '运行子代理',
      subagent_tasks: [
        { id: 't1', description: '调研代码结构', status: 'running', stage: 'tool', elapsed: 12 },
        { id: 't2', description: '整理测试报告', status: 'completed', stage: 'completed', elapsed: 30 },
      ],
    }],
  };
}


function resultRow(results: string[]): ToolTraceRow {
  return {
    id: 'result-row', kind: 'tool', status: 'failed', action: '运行子代理',
    detail: '', command: '', errorSummary: '', count: results.length,
    metrics: {},
    calls: results.map((result, index) => ({
      id: `call-${index}`, name: 'run_subagent', label: '运行子代理',
      result: { ok: false, summary: '完成', result },
    })),
  };
}


describe('ToolExecutionRow', () => {
  it('shows a failed command and continues to expose details', async () => {
    const wrapper = mount(ToolExecutionRow, { props: { row: failedCommandRow() } });
    expect(wrapper.text()).toContain('运行命令失败');
    expect(wrapper.text()).toContain('pytest -q');
    expect(wrapper.text()).toContain('ModuleNotFoundError: pytest');
    const button = wrapper.get('button[data-detail-toggle]');
    expect(button.attributes('aria-expanded')).toBe('false');
    await button.trigger('click');
    expect(button.attributes('aria-expanded')).toBe('true');
    expect(wrapper.get('pre').text()).toContain('退出码 1');
  });

  it('expands a mutation diff in the same row', async () => {
    const wrapper = mount(ToolExecutionRow, { props: { row: mutationRowWithDiff() } });
    expect(wrapper.text()).toContain('+1 / -1 行');
    await wrapper.get('button[data-diff-toggle]').trigger('click');
    expect(wrapper.findAll('.diff-line.add')).toHaveLength(1);
    expect(wrapper.findAll('.diff-line.del')).toHaveLength(1);
  });

  it('lists every file of a grouped read row', () => {
    const wrapper = mount(ToolExecutionRow, { props: { row: groupedReadRow() } });
    expect(wrapper.text()).toContain('读取文件');
    const files = wrapper.findAll('.trace-files li');
    expect(files).toHaveLength(2);
    expect(files[0].text()).toContain('alpha.ts');
    expect(files[1].text()).toContain('beta.ts');
  });

  it('omits the duration badge when durationMs is missing', () => {
    const wrapper = mount(ToolExecutionRow, { props: { row: runningRowWithoutDuration() } });
    expect(wrapper.find('.trace-duration').exists()).toBe(false);
  });

  it('shows a live duration badge from elapsedMs while running', () => {
    const wrapper = mount(ToolExecutionRow, {
      props: { row: runningRowWithoutDuration(), elapsedMs: 5000 },
    });
    expect(wrapper.get('.trace-duration').text()).toBe('5.0s');
  });

  it('renders run_subagent task snapshots', () => {
    const wrapper = mount(ToolExecutionRow, { props: { row: subagentRow() } });
    const tasks = wrapper.findAll('.subagent-task');
    expect(tasks).toHaveLength(2);
    expect(wrapper.text()).toContain('调研代码结构');
    expect(wrapper.text()).toContain('整理测试报告');
    expect(wrapper.text()).toContain('12s');
    expect(wrapper.text()).toContain('30s');
  });

  it('查看按钮记录目标任务 id 并自增打开 nonce（右面板切入子代理工作台）', async () => {
    const wrapper = mount(ToolExecutionRow, { props: { row: subagentRow() } });
    const store = useAppStore();
    expect(store.subagentDetailId).toBe('');

    await wrapper.get('button[data-test="subagent-view-t1"]').trigger('click');
    expect(store.subagentDetailId).toBe('t1');
    expect(store.subagentOpenNonce).toBe(1);

    await wrapper.get('button[data-test="subagent-view-t2"]').trigger('click');
    expect(store.subagentDetailId).toBe('t2');
    expect(store.subagentOpenNonce).toBe(2);
  });

  it.each([
    ['main_takeover', '主 Agent 已接管'],
    ['takeover_queued', '等待主 Agent 接管'],
  ])('shows the %s handoff badge', (handoff, expected) => {
    const result = JSON.stringify({ status: 'timed_out', handoff });
    const wrapper = mount(ToolExecutionRow, { props: { row: resultRow([result]) } });
    expect(wrapper.get('.trace-takeover-badge').text()).toBe(expected);
    expect(wrapper.classes()).toContain('status-failed');
  });

  it.each([
    JSON.stringify({ status: 'failed', handoff: 'main_takeover' }),
    'ordinary non-json failure',
  ])('does not show a handoff badge for %s', (result) => {
    const wrapper = mount(ToolExecutionRow, { props: { row: resultRow([result]) } });
    expect(wrapper.find('.trace-takeover-badge').exists()).toBe(false);
  });

  it('parses each grouped call result and selects the first timed-out handoff', () => {
    const wrapper = mount(ToolExecutionRow, {
      props: {
        row: resultRow([
          'ordinary result',
          JSON.stringify({ status: 'timed_out', handoff: 'takeover_queued' }),
          JSON.stringify({ status: 'timed_out', handoff: 'main_takeover' }),
        ]),
      },
    });
    expect(wrapper.get('.trace-takeover-badge').text()).toBe('等待主 Agent 接管');
  });
});
