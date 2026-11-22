import { createPinia, setActivePinia } from 'pinia';
import { mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it } from 'vitest';
import ToolExecutionRow from './ToolExecutionRow.vue';
import { useAppStore } from '../stores/appStore';
import type { ToolDisplay, ToolTraceRow } from '../utils/toolTrace';

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
    id: 'sub-1', kind: 'subagent', status: 'running', action: '运行子代理',
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
    expect(wrapper.get('.trace-kind').text()).toBe('运行');
    expect(wrapper.get('.trace-target').text()).toBe('pytest -q');
    expect(wrapper.get('.trace-meta').text()).toBe('失败');
    expect(wrapper.get('.trace-meta').attributes('title')).toContain('ModuleNotFoundError');
    const toggle = wrapper.get('[data-row-toggle]');
    expect(toggle.attributes('aria-expanded')).toBe('false');
    await toggle.trigger('click');
    expect(toggle.attributes('aria-expanded')).toBe('true');
    expect(wrapper.get('pre').text()).toContain('退出码 1');
  });

  it('expands a mutation diff in the same row', async () => {
    const wrapper = mount(ToolExecutionRow, { props: { row: mutationRowWithDiff() } });
    expect(wrapper.get('.trace-kind').text()).toBe('编辑');
    expect(wrapper.get('.trace-diff-badge').text()).toContain('+1');
    expect(wrapper.find('.diff-line').exists()).toBe(false);
    await wrapper.get('[data-row-toggle]').trigger('click');
    expect(wrapper.findAll('.diff-line.add')).toHaveLength(1);
    expect(wrapper.findAll('.diff-line.del')).toHaveLength(1);
  });

  it('lists every file of a grouped read row after expanding', async () => {
    const wrapper = mount(ToolExecutionRow, { props: { row: groupedReadRow() } });
    expect(wrapper.get('.trace-kind').text()).toBe('读取');
    expect(wrapper.get('.trace-target').text()).toBe('2 个文件');
    await wrapper.get('[data-row-toggle]').trigger('click');
    const files = wrapper.findAll('.trace-files li');
    expect(files).toHaveLength(2);
    expect(files[0].text()).toContain('alpha.ts');
    expect(files[1].text()).toContain('beta.ts');
  });

  it('shows the running state word when no duration is known yet', () => {
    const wrapper = mount(ToolExecutionRow, { props: { row: runningRowWithoutDuration() } });
    expect(wrapper.get('.trace-meta').text()).toBe('运行中');
  });

  it('shows a live duration from elapsedMs while running', () => {
    const wrapper = mount(ToolExecutionRow, {
      props: { row: runningRowWithoutDuration(), elapsedMs: 5000 },
    });
    expect(wrapper.get('.trace-meta').text()).toBe('5.0s');
  });

it('labels a run_subagent row as 星舰 instead of 工具', async () => {
    const wrapper = mount(ToolExecutionRow, { props: { row: subagentRow() } });
    expect(wrapper.get('.trace-kind').text()).toBe('星舰');
    await wrapper.get('[data-row-toggle]').trigger('click');
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
    await wrapper.get('[data-row-toggle]').trigger('click');

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


describe('computer action row', () => {
  const shotSha = 'a'.repeat(64);

  function computerRow(): ToolTraceRow {
    return {
      id: 'computer:c1',
      kind: 'computer',
      status: 'completed',
      action: '点击',
      detail: 'el-20',
      command: '',
      errorSummary: '',
      count: 1,
      durationMs: 320,
      metrics: {},
      calls: [{
        id: 'c1',
        name: 'computer_action',
        label: '点击控件 el-20',
        result: {
          ok: true,
          summary: '完成',
          result: JSON.stringify({ ok: true, target: 'browser', action: 'click_element', screen_sha256: shotSha }),
        },
      }],
    };
  }

  it('uses the action verb as the row label instead of 工具', () => {
    const wrapper = mount(ToolExecutionRow, { props: { row: computerRow() } });
    expect(wrapper.get('.trace-kind').text()).toBe('点击');
    expect(wrapper.get('.trace-target').text()).toBe('el-20');
    expect(wrapper.get('.trace-meta').text()).toBe('320ms');
  });

  it('expands to the screenshot matched by screenshot hash', async () => {
    const store = useAppStore();
    store.computerShots[shotSha] = '/computer-preview/tok/shot-007.png';
    const wrapper = mount(ToolExecutionRow, { props: { row: computerRow() } });

    await wrapper.get('.trace-line').trigger('click');

    expect(wrapper.get('.computer-shot').attributes('src')).toBe('/computer-preview/tok/shot-007.png');
    expect(wrapper.text()).toContain('该动作当帧');
  });

  it('explains when that frame is no longer cached', async () => {
    const wrapper = mount(ToolExecutionRow, { props: { row: computerRow() } });

    await wrapper.get('.trace-line').trigger('click');

    expect(wrapper.find('.computer-shot').exists()).toBe(false);
    expect(wrapper.text()).toContain('该帧已过期');
  });

  it('strips leading cd prefix in compact command target while preserving full command in title', () => {
    const fullCmd = 'cd D:\\repo\\project; npm test';
    const wrapper = mount(ToolExecutionRow, {
      props: {
        row: {
          id: 'cmd-cd-1',
          kind: 'tool',
          count: 1,
          action: '运行',
          detail: fullCmd,
          command: fullCmd,
          status: 'completed',
          errorSummary: '',
          metrics: {},
          calls: [
            {
              id: 'cmd-cd-1',
              name: 'execute_command',
              label: '运行',
              display: {
                category: 'command',
                action: '运行',
                target: fullCmd,
                command: fullCmd,
                metrics: {},
                error_summary: '',
                is_skill: false,
              },
              result: { ok: true, summary: '完成', result: 'ok', duration_ms: 80 },
            },
          ],
          legacyDiffs: [],
        },
      },
    });

    expect(wrapper.get('.trace-target').text()).toBe('npm test');
    expect(wrapper.get('.trace-line').attributes('title')).toContain(fullCmd);
    wrapper.unmount();
  });

  it('summarizes multi-file legacy-diffs cleanly while keeping all files visible when expanded', async () => {
    const wrapper = mount(ToolExecutionRow, {
      props: {
        row: {
          id: 'legacy-diffs-multi',
          kind: 'legacy-diffs',
          count: 2,
          action: '变更',
          detail: 'src/b.ts 等 2 个文件',
          command: '',
          status: 'completed',
          errorSummary: '',
          metrics: {},
          calls: [],
          legacyDiffs: [
            { file_path: 'src/b.ts', diff_text: '--- src/b.ts\n+++ src/b.ts\n-old\n+new' },
            { file_path: 'src/c.ts', diff_text: '--- src/c.ts\n+++ src/c.ts\n-a\n+b' },
          ],
        },
      },
    });

    expect(wrapper.get('.trace-target').text()).toBe('b.ts 等 2 个文件');
    await wrapper.get('.trace-line').trigger('click');
    expect(wrapper.text()).toContain('src/b.ts');
    expect(wrapper.text()).toContain('src/c.ts');
    wrapper.unmount();
  });

  it('draws SVG coordinate overlay only when frame_id and dimensions match, and falls back to text trace when missing', async () => {
    const store = useAppStore();
    const frameSha = 'b'.repeat(64);
    store.computerShots[frameSha] = '/computer-preview/tok/shot-frame.png';
    store.computerFrameShots = {
      ...(store.computerFrameShots || {}),
      [frameSha]: {
        url: '/computer-preview/tok/shot-frame.png',
        frame_id: 'frame-42',
        image_width: 1280,
        image_height: 800,
      },
    } as any;

    const matchedRow: ToolTraceRow = {
      id: 'computer:c-match',
      kind: 'computer',
      status: 'completed',
      action: '点击',
      detail: '640, 400',
      command: '',
      errorSummary: '',
      count: 1,
      durationMs: 120,
      metrics: {},
      calls: [{
        id: 'c-match',
        name: 'computer_action',
        label: '点击 (640, 400)',
        result: {
          ok: true,
          summary: '点击',
          result: JSON.stringify({
            ok: true,
            target: 'browser',
            action: 'click',
            screen_sha256: frameSha,
            frame_id: 'frame-42',
            image_width: 1280,
            image_height: 800,
            action_geometry: { kind: 'point', x: 640, y: 400 },
          }),
        },
      }],
    };
    const wMatch = mount(ToolExecutionRow, { props: { row: matchedRow } });
    await wMatch.get('.trace-line').trigger('click');
    const svg = wMatch.find('[data-test="computer-svg-overlay"]');
    expect(svg.exists()).toBe(true);
    expect(svg.attributes('viewBox')).toBe('0 0 1280 800');
    expect(svg.find('circle').attributes('cx')).toBe('640');
    expect(svg.find('circle').attributes('cy')).toBe('400');
    wMatch.unmount();

    // 缺少几何或 frame_id 不匹配时不猜位置，回退纯文本轨迹说明
    const noGeomRow: ToolTraceRow = {
      ...matchedRow,
      id: 'computer:c-nogeom',
      calls: [{
        id: 'c-nogeom',
        name: 'computer_action',
        label: '点击 el-3',
        result: {
          ok: true,
          summary: '点击',
          result: JSON.stringify({
            ok: true,
            target: 'browser',
            action: 'click_element',
            screen_sha256: frameSha,
            frame_id: 'frame-99',
          }),
        },
      }],
    };
    const wFallback = mount(ToolExecutionRow, { props: { row: noGeomRow } });
    await wFallback.get('.trace-line').trigger('click');
    expect(wFallback.find('[data-test="computer-svg-overlay"]').exists()).toBe(false);
    expect(wFallback.get('[data-test="computer-trace-fallback"]').text()).toContain('无匹配帧几何');
    wFallback.unmount();
  });
});
