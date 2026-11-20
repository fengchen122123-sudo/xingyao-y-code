import { describe, expect, it } from 'vitest';
import { countDiffStats, formatRelativeCommitTime, splitGitPath } from './gitFormat';

describe('gitFormat', () => {
  it('splitGitPath 正确拆分 POSIX、Windows 路径与纯文件名', () => {
    expect(splitGitPath('frontend/src/components/GitPanel.vue')).toEqual({
      name: 'GitPanel.vue',
      dir: 'frontend/src/components/',
    });
    expect(splitGitPath('core\\git\\service.py')).toEqual({
      name: 'service.py',
      dir: 'core\\git\\',
    });
    expect(splitGitPath('README.md')).toEqual({
      name: 'README.md',
      dir: '',
    });
  });

  it('formatRelativeCommitTime 按时间跨度格式化相对时间或日期', () => {
    const now = 1_700_000_000;
    expect(formatRelativeCommitTime(0, now)).toBe('');
    expect(formatRelativeCommitTime(now - 20, now)).toBe('刚刚');
    expect(formatRelativeCommitTime(now - 15 * 60, now)).toBe('15分钟前');
    expect(formatRelativeCommitTime(now - 3 * 3600, now)).toBe('3小时前');
    expect(formatRelativeCommitTime(now - 5 * 86400, now)).toBe('5天前');
    const oldTs = now - 40 * 86400;
    const d = new Date(oldTs * 1000);
    const expected = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    expect(formatRelativeCommitTime(oldTs, now)).toBe(expected);
  });

  it('countDiffStats 统计新增与删除行数并跳过二进制文件', () => {
    expect(countDiffStats(null)).toEqual({ added: 0, deleted: 0 });
    expect(countDiffStats({ raw_text: '', is_binary: true, hunks: [] })).toEqual({ added: 0, deleted: 0 });
    expect(
      countDiffStats({
        raw_text: '',
        is_binary: false,
        hunks: [
          {
            old_start: 1,
            old_lines: 3,
            new_start: 1,
            new_lines: 4,
            header: '',
            lines: [' line1', '-old1', '+new1', '+new2', ' line2'],
          },
        ],
      }),
    ).toEqual({ added: 2, deleted: 1 });
  });
});
