import { describe, expect, it } from 'vitest';
import { buildMessageGroups, groupHasProcess, groupStats, msgKey } from './turnGroups';
import type { ChatMessage } from '../stores/appStore';

function user(text: string): ChatMessage {
  return { role: 'user', content: text };
}

function assistant(opts: Partial<ChatMessage> = {}): ChatMessage {
  return {
    role: 'assistant',
    content: '',
    reasoning_content: '',
    tool_calls: [],
    legacy_diffs: [],
    execution_state: 'completed',
    ...opts,
  };
}

function notice(text: string): ChatMessage {
  return { role: 'notice', content: text };
}

function error(text: string): ChatMessage {
  return { role: 'error', content: text };
}

describe('msgKey', () => {
  it('同一消息对象返回同一 key，不同对象返回不同 key', () => {
    const a = user('a');
    const b = user('b');
    expect(msgKey(a)).toBe(msgKey(a));
    expect(msgKey(a)).not.toBe(msgKey(b));
  });
});

describe('buildMessageGroups', () => {
  it('多轮回合：中间轮次进 middle，最后一轮总结留在 tail', () => {
    const groups = buildMessageGroups([
      user('帮我修个 bug'),
      assistant({ content: '先看看代码', tool_calls: [{ id: 't1' } as any] }),
      assistant({ content: '再跑下测试', tool_calls: [{ id: 't2' } as any, { id: 't3' } as any] }),
      assistant({ content: '修复完成，测试全过' }),
    ]);

    expect(groups).toHaveLength(1);
    const g = groups[0];
    expect(g.userMsg?.content).toBe('帮我修个 bug');
    expect(g.middle).toHaveLength(2);
    expect(g.tail).toHaveLength(1);
    expect(g.tail[0].content).toBe('修复完成，测试全过');
    expect(g.running).toBe(false);
    expect(g.state).toBe('completed');
  });

  it('任一轮次 running 时整组保持实时模式', () => {
    const groups = buildMessageGroups([
      user('跑个任务'),
      assistant({ content: '中间轮', execution_state: 'completed' }),
      assistant({ content: '', execution_state: 'running' }),
    ]);

    expect(groups[0].running).toBe(true);
    expect(groups[0].middle).toHaveLength(1);
    expect(groups[0].tail).toHaveLength(1);
  });

  it('回合取消：execution_state=cancelled 或 cancelled 标记归约为 cancelled', () => {
    const g1 = buildMessageGroups([
      user('x'),
      assistant({ execution_state: 'cancelled' }),
    ])[0];
    expect(g1.state).toBe('cancelled');

    const g2 = buildMessageGroups([
      user('x'),
      assistant({ cancelled: true, execution_state: 'completed' }),
    ])[0];
    expect(g2.state).toBe('cancelled');
  });

  it('回合异常：execution_state=error 归约为 error', () => {
    const g = buildMessageGroups([
      user('x'),
      assistant({ execution_state: 'error', error_note: '模型超时' }),
    ])[0];
    expect(g.state).toBe('error');
  });

  it('连续两个回合正确分开', () => {
    const groups = buildMessageGroups([
      user('问题一'),
      assistant({ content: '答一' }),
      user('问题二'),
      assistant({ content: '中间' }),
      assistant({ content: '答二' }),
    ]);

    expect(groups).toHaveLength(2);
    expect(groups[0].tail[0].content).toBe('答一');
    expect(groups[1].userMsg?.content).toBe('问题二');
    expect(groups[1].middle).toHaveLength(1);
    expect(groups[1].tail[0].content).toBe('答二');
  });

  it('会话开头无用户消息的孤儿段聚合成无 user 的组', () => {
    const groups = buildMessageGroups([
      assistant({ content: '孤儿轮' }),
      assistant({ content: '孤儿总结' }),
      user('新问题'),
      assistant({ content: '答' }),
    ]);

    expect(groups).toHaveLength(2);
    expect(groups[0].userMsg).toBeNull();
    expect(groups[0].middle).toHaveLength(1);
    expect(groups[0].tail[0].content).toBe('孤儿总结');
    expect(groups[1].userMsg?.content).toBe('新问题');
  });

  it('总结轮之后的 error 挂 tail（始终可见），之前的进 middle', () => {
    const groups = buildMessageGroups([
      user('x'),
      notice('中途提示'),
      assistant({ content: '总结' }),
      error('导出失败'),
    ]);

    const g = groups[0];
    expect(g.middle.map((m) => m.role)).toEqual(['notice']);
    expect(g.tail.map((m) => m.role)).toEqual(['assistant', 'error']);
  });

  it('单轮简单问答：middle 为空，无折叠', () => {
    const groups = buildMessageGroups([
      user('你好'),
      assistant({ content: '你好！' }),
    ]);

    const g = groups[0];
    expect(g.middle).toHaveLength(0);
    expect(groupHasProcess(g)).toBe(false);
    expect(g.tail).toHaveLength(1);
  });

  it('只有用户消息没有回复时组保持空尾', () => {
    const groups = buildMessageGroups([
      user('在吗'),
      user('另一个问题'),
      assistant({ content: '在' }),
    ]);

    expect(groups).toHaveLength(2);
    expect(groups[0].middle).toHaveLength(0);
    expect(groups[0].tail).toHaveLength(0);
    expect(groups[1].tail[0].content).toBe('在');
  });
});

describe('groupStats', () => {
  it('统计轮次数与工具调用数（含 legacy diff）', () => {
    const g = buildMessageGroups([
      user('x'),
      assistant({ tool_calls: [{ id: 'a' } as any, { id: 'b' } as any] }),
      assistant({ legacy_diffs: [{ file_path: 'f' } as any] }),
      assistant({ content: '总结' }),
    ])[0];

    expect(groupStats(g)).toEqual({ rounds: 3, toolCount: 3 });
  });
});
