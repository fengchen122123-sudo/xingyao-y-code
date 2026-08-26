import { describe, expect, it } from 'vitest';
import { isLocalFileLink, toAbsoluteLocalPath } from './localPath';

describe('isLocalFileLink', () => {
  it('识别相对路径（含分隔符 + 已知扩展名）', () => {
    expect(isLocalFileLink('docs/foo.md')).toBe(true);
    expect(isLocalFileLink('src\\app.py')).toBe(true);
  });
  it('识别相对前缀 ./ 与 ../', () => {
    expect(isLocalFileLink('./foo.py')).toBe(true);
    expect(isLocalFileLink('../foo.py')).toBe(true);
  });
  it('识别盘符绝对路径', () => {
    expect(isLocalFileLink('D:\\x\\y.py')).toBe(true);
    expect(isLocalFileLink('C:/x/y.py')).toBe(true);
  });
  it('识别 file:// 协议路径', () => {
    expect(isLocalFileLink('file:///C:/x/y.md')).toBe(true);
  });
  it('识别纯文件名（已知扩展名）', () => {
    expect(isLocalFileLink('app.py')).toBe(true);
    expect(isLocalFileLink('2026-08-26.md')).toBe(true);
  });
  it('拒绝 URL 与危险协议', () => {
    expect(isLocalFileLink('https://example.com')).toBe(false);
    expect(isLocalFileLink('https://example.md')).toBe(false);
    expect(isLocalFileLink('javascript:alert(1)')).toBe(false);
  });
  it('拒绝非路径普通文本', () => {
    expect(isLocalFileLink('1.2.3')).toBe(false);
    expect(isLocalFileLink('foo.bar')).toBe(false);
    expect(isLocalFileLink('import os')).toBe(false);
    expect(isLocalFileLink('hello world.md')).toBe(false);
    expect(isLocalFileLink('<img src=x.md>')).toBe(false);
  });
  it('allowBareFilename=false 时不识别纯文件名，但仍识别含分隔符的路径', () => {
    expect(isLocalFileLink('pom.xml', { allowBareFilename: false })).toBe(false);
    expect(isLocalFileLink('docs/foo.md', { allowBareFilename: false })).toBe(true);
  });
});

describe('toAbsoluteLocalPath', () => {
  it('相对路径拼 workdir 成绝对路径（统一反斜杠）', () => {
    expect(toAbsoluteLocalPath('docs/foo.md', 'D:\\proj')).toBe('D:\\proj\\docs\\foo.md');
    expect(toAbsoluteLocalPath('./foo.py', 'D:\\proj')).toBe('D:\\proj\\foo.py');
  });
  it('绝对路径原样返回', () => {
    expect(toAbsoluteLocalPath('D:\\x\\y.py', 'D:\\proj')).toBe('D:\\x\\y.py');
    expect(toAbsoluteLocalPath('C:/x/y.py', '')).toBe('C:/x/y.py');
  });
  it('file:// 转为本地路径', () => {
    expect(toAbsoluteLocalPath('file:///C:/x/y.md', '')).toBe('C:/x/y.md');
  });
  it('相对路径且 workdir 为空返回 null', () => {
    expect(toAbsoluteLocalPath('docs/foo.md', '')).toBeNull();
  });
});
