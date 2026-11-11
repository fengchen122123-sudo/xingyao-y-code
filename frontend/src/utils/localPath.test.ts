import { describe, expect, it } from 'vitest';
import {
  OFFICE_EXTENSIONS,
  canOpenInRightPanel,
  isHtmlFilePath,
  isLocalFileLink,
  isMarkdownFilePath,
  isOfficeFilePath,
  toAbsoluteLocalPath,
  toFileUrl,
} from './localPath';

describe('isLocalFileLink', () => {
  it('识别相对路径（含分隔符 + 已知扩展名）', () => {
    expect(isLocalFileLink('docs/foo.md')).toBe(true);
    expect(isLocalFileLink('src\\app.py')).toBe(true);
  });
  it('办公文档的相对路径也算本地文件链接（相对路径白名单已含 office 扩展名）', () => {
    expect(isLocalFileLink('docs/方案.pptx')).toBe(true);
    expect(isLocalFileLink('报告.docx')).toBe(true);
    expect(isLocalFileLink('assets/表格.xlsx')).toBe(true);
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

describe('isHtmlFilePath', () => {
  it('只认 .html / .htm（不分大小写）', () => {
    expect(isHtmlFilePath('D:\\proj\\index.html')).toBe(true);
    expect(isHtmlFilePath('index.HTM')).toBe(true);
    expect(isHtmlFilePath('a.html?x=1')).toBe(true);
  });
  it('其它文件与目录不算', () => {
    expect(isHtmlFilePath('a.md')).toBe(false);
    expect(isHtmlFilePath('a.html.bak')).toBe(false);
    expect(isHtmlFilePath('')).toBe(false);
  });
});

describe('isMarkdownFilePath', () => {
  it('只认 .md / .markdown（不分大小写）', () => {
    expect(isMarkdownFilePath('docs/notes.md')).toBe(true);
    expect(isMarkdownFilePath('a.MARKDOWN')).toBe(true);
    expect(isMarkdownFilePath('a.mdx')).toBe(false);
    expect(isMarkdownFilePath('a.html')).toBe(false);
  });
});

describe('isOfficeFilePath', () => {
  it('识别 Word / Excel / PowerPoint 及 WPS 另存格式（不分大小写）', () => {
    expect(isOfficeFilePath('D:\\proj\\报告.docx')).toBe(true);
    expect(isOfficeFilePath('a.DOC')).toBe(true);
    expect(isOfficeFilePath('表格.xlsx')).toBe(true);
    expect(isOfficeFilePath('a.XLS')).toBe(true);
    expect(isOfficeFilePath('docs/方案.pptx')).toBe(true);
    expect(isOfficeFilePath('a.PPT')).toBe(true);
    // WPS / LibreOffice 另存格式
    expect(isOfficeFilePath('a.rtf')).toBe(true);
    expect(isOfficeFilePath('a.odt')).toBe(true);
    expect(isOfficeFilePath('a.ods')).toBe(true);
    expect(isOfficeFilePath('a.odp')).toBe(true);
  });
  it('忽略查询串与锚点（前端链接常带 ?x=1）', () => {
    expect(isOfficeFilePath('report.docx?x=1')).toBe(true);
    expect(isOfficeFilePath('report.PPTX#page=2')).toBe(true);
    expect(isOfficeFilePath('  a.xls?download=1  ')).toBe(true);
  });
  it('非办公文档不算', () => {
    expect(isOfficeFilePath('pic.png')).toBe(false);
    expect(isOfficeFilePath('a.txt')).toBe(false);
    expect(isOfficeFilePath('a.docx.bak')).toBe(false);
    expect(isOfficeFilePath('a.pdf')).toBe(false);
    expect(isOfficeFilePath('')).toBe(false);
  });
  it('OFFICE_EXTENSIONS 覆盖三类办公文档', () => {
    for (const ext of ['doc', 'docx', 'rtf', 'odt', 'xls', 'xlsx', 'ods', 'ppt', 'pptx', 'odp']) {
      expect(OFFICE_EXTENSIONS.has(ext)).toBe(true);
    }
    expect(OFFICE_EXTENSIONS.has('pdf')).toBe(false);
  });
});

describe('canOpenInRightPanel', () => {
  it('html / pdf / md / office 四类交给右栏', () => {
    expect(canOpenInRightPanel('D:\\proj\\index.html')).toBe(true);
    expect(canOpenInRightPanel('D:\\proj\\doc.pdf')).toBe(true);
    expect(canOpenInRightPanel('docs/notes.md')).toBe(true);
    expect(canOpenInRightPanel('a.MARKDOWN')).toBe(true);
    expect(canOpenInRightPanel('report.pdf?x=1')).toBe(true);
  });
  it('office 文档也交给右栏（docx / pptx / xlsx）', () => {
    expect(canOpenInRightPanel('D:\\proj\\报告.docx')).toBe(true);
    expect(canOpenInRightPanel('docs/方案.pptx')).toBe(true);
    expect(canOpenInRightPanel('表格.XLSX?x=1')).toBe(true);
  });
  it('其它类型仍走源码预览弹窗', () => {
    expect(canOpenInRightPanel('a.py')).toBe(false);
    expect(canOpenInRightPanel('pic.png')).toBe(false);
    expect(canOpenInRightPanel('a.txt')).toBe(false);
    expect(canOpenInRightPanel('')).toBe(false);
  });
});

describe('toFileUrl', () => {
  it('盘符路径转 file:// URL（统一正斜杠）', () => {
    expect(toFileUrl('D:\\proj\\a.html')).toBe('file:///D:/proj/a.html');
    expect(toFileUrl('C:/x/index.htm')).toBe('file:///C:/x/index.htm');
  });
  it('空格与中文按段百分号编码（与后端 quote(safe="") 同口径）', () => {
    expect(toFileUrl('D:\\My Docs\\a b.html')).toBe('file:///D:/My%20Docs/a%20b.html');
    expect(toFileUrl('D:\\文档\\a(1).html')).toBe(
      'file:///D:/%E6%96%87%E6%A1%A3/a%281%29.html',
    );
  });
  it('UNC 路径转 file:// 主机形式', () => {
    expect(toFileUrl('\\\\srv\\share\\a.html')).toBe('file://srv/share/a.html');
  });
  it('相对路径 / 空路径 / 含 .. 的路径返回空串', () => {
    expect(toFileUrl('docs/a.html')).toBe('');
    expect(toFileUrl('')).toBe('');
    expect(toFileUrl('D:\\a\\..\\b.html')).toBe('');
  });
});
