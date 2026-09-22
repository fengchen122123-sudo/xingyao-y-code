import { describe, expect, it } from 'vitest';
import { pairHistoryToolMessages } from '../stores/app';
import { buildToolTraceRows } from './toolTrace';

describe('pairHistoryToolMessages', () => {
  it('把 role=tool 消息配对进 assistant，并从 arguments 生成 label', () => {
    const msgs = [
      {
        role: 'assistant',
        content: '',
        tool_calls: [
          { id: 'a', type: 'function', function: { name: 'read_file', arguments: '{"file_path":"D:/x/reconciliation.py"}' } },
          { id: 'b', type: 'function', function: { name: 'execute_command', arguments: { command: 'npm run build' } } },
        ],
      },
      { role: 'tool', tool_call_id: 'a', content: 'file content' },
      { role: 'tool', tool_call_id: 'b', content: 'build ok' },
    ] as any;
    const out = pairHistoryToolMessages(msgs);
    expect(out.length).toBe(1);
    const calls = out[0].tool_calls!;
    expect(calls[0].label).toContain('reconciliation.py');
    expect(calls[0].result?.summary).toBe('file content');
    expect(calls[1].label).toContain('npm run build');
    expect(calls[1].result?.summary).toBe('build ok');
  });
});

describe('buildToolTraceRows', () => {
  it('连续 read 合并成一行，命令用 display.command 渲染', () => {
    const calls = [
      { id: '1', name: 'read_file', label: 'read a', display: { category: 'read', action: '读取文件', target: 'a.py' }, result: { ok: true, summary: 'a', result: 'a' } },
      { id: '2', name: 'read_file', label: 'read b', display: { category: 'read', action: '读取文件', target: 'b.py' }, result: { ok: true, summary: 'b', result: 'b' } },
      { id: '3', name: 'execute_command', label: 'run', display: { category: 'command', action: '执行命令', command: 'npm run build' }, result: { ok: true, summary: 'ok', result: 'ok' } },
    ] as any;
    const rows = buildToolTraceRows(calls, 'completed');
    expect(rows.length).toBe(2);
    expect(rows[0].kind).toBe('read-group');
    expect(rows[0].count).toBe(2);
    expect(rows[0].action).toBe('读取文件');
    expect(rows[1].kind).toBe('command');
    expect(rows[1].command).toBe('npm run build');
    expect(rows[1].status).toBe('completed');
  });
});
