// @vitest-environment jsdom
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import EditorLanguageSettings from './EditorLanguageSettings.vue';
import { dispatchLspEvent, resetClientState } from '../editor/lsp';

const STATUS = {
  ok: true,
  workspace: 'E:\\ws',
  servers: [
    {
      server: 'jedi',
      language: 'python',
      state: 'running',
      executable: 'E:\\app\\lsp_bin\\jedi-language-server.exe',
      source: 'bundled',
    },
    {
      server: 'gopls',
      language: 'go',
      state: 'missing',
      executable: '',
      source: 'missing',
      msg: '未找到 gopls：请安装它并加入 PATH',
    },
  ],
};

function installApi(overrides: Record<string, unknown> = {}) {
  const api = {
    lsp_server_status: vi.fn(async () => STATUS),
    lsp_set_server_path: vi.fn(async () => ({ ok: true })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

beforeEach(() => {
  (window as any).pywebview = undefined;
  (window as any).prompt = vi.fn(() => null);
  resetClientState();
});

// 组件在 onMounted 里订阅 lsp_status 事件：用例之间必须卸载，否则监听器会跨用例
// 累积（组件的订阅是模块级 Set），后面「检查刷新次数」的用例会被前面的组件干扰。
const mounted: VueWrapper[] = [];

function mountSettings(): VueWrapper {
  const wrapper = mount(EditorLanguageSettings) as VueWrapper;
  mounted.push(wrapper);
  return wrapper;
}

afterEach(() => {
  for (const wrapper of mounted.splice(0)) {
    try {
      wrapper.unmount();
    } catch {
      // 用例内部已经卸载过，忽略
    }
  }
});

it('挂载后列出各语言服务器状态与路径', async () => {
  installApi();
  const wrapper = mountSettings();
  await flushPromises();
  const text = wrapper.text();
  expect(text).toContain('编辑器与语言服务');
  expect(text).toContain('jedi');
  expect(text).toContain('python');
  expect(text).toContain('运行中');
  expect(text).toContain('go');
  expect(text).toContain('未安装');
  expect(text).toContain('未找到 gopls');
});

it('手动指定路径后调用桥并刷新状态', async () => {
  const api = installApi();
  (window as any).prompt = vi.fn(() => 'E:\\tools\\gopls.exe');
  const wrapper = mountSettings();
  await flushPromises();
  await wrapper.find('[data-testid="pick-gopls"]').trigger('click');
  await flushPromises();
  expect(api.lsp_set_server_path).toHaveBeenCalledWith('gopls', 'E:\\tools\\gopls.exe');
  // 设置后必定重新拉一次状态，用户能看到生效结果
  expect(api.lsp_server_status).toHaveBeenCalledTimes(2);
});

it('用户取消输入时不调用桥', async () => {
  const api = installApi();
  (window as any).prompt = vi.fn(() => null);
  const wrapper = mountSettings();
  await flushPromises();
  await wrapper.find('[data-testid="pick-gopls"]').trigger('click');
  await flushPromises();
  expect(api.lsp_set_server_path).not.toHaveBeenCalled();
});

it('已被手动覆盖的服务器提供恢复默认入口', async () => {
  installApi({
    lsp_server_status: vi.fn(async () => ({
      ok: true,
      workspace: 'E:\\ws',
      servers: [
        {
          server: 'jedi',
          language: 'python',
          state: 'stopped',
          executable: 'E:\\custom\\jedi.exe',
          source: 'override',
        },
      ],
    })),
  });
  const wrapper = mountSettings();
  await flushPromises();
  await wrapper.find('[data-testid="clear-jedi"]').trigger('click');
  await flushPromises();
  expect((window as any).pywebview.api.lsp_set_server_path).toHaveBeenCalledWith('jedi', '');
}, 20000);

it('桥不可用时给出明确提示且不抛异常', async () => {
  const wrapper = mountSettings();
  await flushPromises();
  expect(wrapper.text()).toContain('桥未就绪');
});

// ---------------------------------------------------------------------------
// 比计划更强的补充断言：两种「用不了」的下一步动作完全不同，必须有区分
// ---------------------------------------------------------------------------

it('未安装（missing）与未启动（stopped）给出不同的下一步指引', async () => {
  installApi({
    lsp_server_status: vi.fn(async () => ({
      ok: true,
      workspace: 'E:\\ws',
      servers: [
        {
          server: 'jedi',
          language: 'python',
          state: 'stopped',
          executable: 'E:\\custom\\jedi.exe',
          source: 'override',
          msg: 'jedi 未启动',
        },
        {
          server: 'gopls',
          language: 'go',
          state: 'missing',
          executable: '',
          source: 'missing',
          msg: '未找到 gopls：请安装它并加入 PATH',
        },
      ],
    })),
  });
  const wrapper = mountSettings();
  await flushPromises();

  const stopped = wrapper.find('[data-testid="row-jedi"]').text();
  const missing = wrapper.find('[data-testid="row-gopls"]').text();
  expect(stopped).toContain('未启动');
  expect(stopped).not.toContain('未安装');
  expect(missing).toContain('未安装');

  // 两种情况的下一步指引必须不同：一个「先装」，一个「已经装了，去启动」
  const stoppedHint = wrapper.find('[data-testid="hint-jedi"]').text();
  const missingHint = wrapper.find('[data-testid="hint-gopls"]').text();
  expect(stoppedHint).not.toBe(missingHint);
  expect(stoppedHint).toContain('未启动');
  expect(missingHint).toContain('未找到');
});

it('指定路径失败时把后端原因显示出来，不被随后的刷新清掉', async () => {
  const api = installApi({
    lsp_set_server_path: vi.fn(async () => ({
      ok: false,
      msg: '可执行文件不存在或不可执行：E:\\tools\\gopls.exe',
    })),
  });
  (window as any).prompt = vi.fn(() => 'E:\\tools\\gopls.exe');
  const wrapper = mountSettings();
  await flushPromises();
  await wrapper.find('[data-testid="pick-gopls"]').trigger('click');
  await flushPromises();

  const error = wrapper.find('[data-testid="error"]');
  expect(error.exists()).toBe(true);
  expect(error.text()).toContain('可执行文件不存在或不可执行');
  // 失败时不必刷新（状态没变），但错误提示必须在
  expect(api.lsp_server_status).toHaveBeenCalledTimes(1);
});

it('读取状态失败时显示后端原因', async () => {
  installApi({
    lsp_server_status: vi.fn(async () => ({ ok: false, msg: '工作区未就绪' })),
  });
  const wrapper = mountSettings();
  await flushPromises();
  expect(wrapper.find('[data-testid="error"]').text()).toContain('工作区未就绪');
});

it('状态事件到达时自动刷新（语言服务器崩了/起来了，面板要跟上）', async () => {
  const api = installApi();
  const wrapper = mountSettings();
  await flushPromises();
  expect(api.lsp_server_status).toHaveBeenCalledTimes(1);
  dispatchLspEvent('lsp_status', {
    server: 'gopls',
    language: 'go',
    workspace: 'E:\\ws',
    state: 'running',
  });
  await flushPromises();
  expect(api.lsp_server_status).toHaveBeenCalledTimes(2);
  wrapper.unmount();
});

it('卸载后不再响应状态事件（订阅必须撤销，否则设置页反复开关会累积监听器）', async () => {
  const api = installApi();
  const wrapper = mountSettings();
  await flushPromises();
  wrapper.unmount();
  dispatchLspEvent('lsp_status', {
    server: 'jedi',
    language: 'python',
    workspace: 'E:\\ws',
    state: 'running',
  });
  await flushPromises();
  expect(api.lsp_server_status).toHaveBeenCalledTimes(1);
});
