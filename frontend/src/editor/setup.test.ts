import { expect, it } from 'vitest';

import { createEditorState, LANGUAGE_LOADERS } from './setup';

it('createEditorState 保留文档内容并设置语言扩展', async () => {
  const state = await createEditorState('x = 1\n', 'python');
  expect(state.doc.toString()).toBe('x = 1\n');
  // python 语言扩展已挂载：语言数据里能查到 python 的行注释标记（纯文本为 0 条）
  expect(state.languageDataAt('commentTokens', 0).length).toBeGreaterThan(0);
});

it('未登记的语言回落到纯文本，不抛异常', async () => {
  const state = await createEditorState('plain', 'no-such-language');
  expect(state.doc.toString()).toBe('plain');
});

it('语言加载表登记了 Python 与 JavaScript', () => {
  expect(Object.keys(LANGUAGE_LOADERS)).toContain('python');
  expect(Object.keys(LANGUAGE_LOADERS)).toContain('javascript');
});
