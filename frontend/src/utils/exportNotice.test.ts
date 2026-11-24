import { describe, expect, it } from 'vitest';
import { extractExportSuccessPath } from './exportNotice';

describe('extractExportSuccessPath', () => {
  it('提取 Windows 与 POSIX 导出成功路径', () => {
    expect(
      extractExportSuccessPath(
        '导出成功: C:\\Users\\冯晨昊\\.ycode\\exports\\检查一下我现在代码中修改_2__20261122_203911.html',
      ),
    ).toBe('C:\\Users\\冯晨昊\\.ycode\\exports\\检查一下我现在代码中修改_2__20261122_203911.html');
    expect(
      extractExportSuccessPath('导出成功：/home/alice/.ycode/exports/my chat 2026.md'),
    ).toBe('/home/alice/.ycode/exports/my chat 2026.md');
  });

  it('无路径、导出失败或普通提示返回空串', () => {
    expect(extractExportSuccessPath('导出成功')).toBe('');
    expect(extractExportSuccessPath('导出成功:   ')).toBe('');
    expect(extractExportSuccessPath('导出失败: 权限不足')).toBe('');
    expect(extractExportSuccessPath('已切换活动模型: k3-256k')).toBe('');
    expect(extractExportSuccessPath('导出成功: C:\\a\\b.md\n附加文本')).toBe('');
    expect(extractExportSuccessPath(undefined)).toBe('');
    expect(extractExportSuccessPath(null)).toBe('');
  });
});
