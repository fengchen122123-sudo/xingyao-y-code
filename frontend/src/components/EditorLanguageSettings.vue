<template>
  <section class="editor-language-settings">
    <header class="editor-language-settings__header">
      <h3>编辑器与语言服务</h3>
      <button type="button" data-testid="refresh" @click="refresh">刷新状态</button>
    </header>
    <p class="editor-language-settings__hint">
      语言服务器是可执行文件，由安装包内置件或系统 PATH 提供；未找到的语言不影响编辑器基本编辑，可在此手动指定可执行文件路径。
    </p>
    <p v-if="errorMsg" class="editor-language-settings__error" data-testid="error">{{ errorMsg }}</p>
    <table class="editor-language-settings__table">
      <thead>
        <tr>
          <th>语言</th>
          <th>服务器</th>
          <th>状态</th>
          <th>可执行文件</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in servers" :key="row.language + ':' + row.server" :data-testid="'row-' + row.server">
          <td>{{ row.language }}</td>
          <td>{{ row.server }}</td>
          <td>
            <span class="editor-language-settings__state" :data-state="row.state">{{ stateText(row.state) }}</span>
            <span class="editor-language-settings__hint-cell" :data-testid="'hint-' + row.server">
              {{ stateHint(row.state) }}
            </span>
          </td>
          <td class="editor-language-settings__path">
            <span>{{ row.executable || '未找到' }}</span>
            <span class="editor-language-settings__source">{{ sourceText(row.source) }}</span>
            <span v-if="row.msg" class="editor-language-settings__msg" :data-testid="'msg-' + row.server">
              {{ row.msg }}
            </span>
          </td>
          <td>
            <button type="button" :data-testid="'pick-' + row.server" @click="pick(row)">指定路径</button>
            <button
              v-if="row.source === 'override'"
              type="button"
              :data-testid="'clear-' + row.server"
              @click="restore(row)"
            >
              恢复默认
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';

import { onServerStatus, type LspServerStatus } from '../editor/lsp';

const servers = ref<LspServerStatus[]>([]);
const errorMsg = ref('');

/** 状态文案：missing（没装）与 stopped（装了没起）必须一眼可辨。 */
const STATE_TEXT: Record<string, string> = {
  missing: '未安装',
  stopped: '未启动',
  starting: '启动中',
  running: '运行中',
  restarting: '重启中',
  failed: '不可用',
};

/** 与状态绑定的下一步动作：两种「用不了」的处置方式完全不同，文案不能雷同。 */
const STATE_HINT: Record<string, string> = {
  missing: '未安装：未找到可执行文件，请安装该语言服务器并加入 PATH，或点右侧「指定路径」手动指定',
  stopped: '未启动：可执行文件已就绪，打开对应语言的文件即会自动启动，无需额外操作',
  starting: '正在启动语言服务器，稍候刷新',
  running: '补全、诊断与导航能力可用',
  restarting: '进程异常退出，正在自动重启',
  failed: '不可用：点「刷新状态」重试，或重新「指定路径」换一个可执行文件',
};

const SOURCE_TEXT: Record<string, string> = {
  override: '手动指定',
  bundled: '内置',
  path: 'PATH 探测',
  missing: '',
};

function stateText(state: string): string {
  return STATE_TEXT[state] ?? state;
}

function stateHint(state: string): string {
  return STATE_HINT[state] ?? '';
}

function sourceText(source?: string): string {
  if (!source) return '';
  return SOURCE_TEXT[source] ?? '';
}

interface LspSettingsBridge {
  lsp_server_status(workspace?: string): Promise<{
    ok: boolean;
    workspace?: string;
    servers?: LspServerStatus[];
    msg?: string;
  }>;
  lsp_set_server_path(serverId: string, path: string): Promise<{ ok: boolean; msg?: string }>;
}

function api(): LspSettingsBridge | null {
  const bridge = (window as any).pywebview?.api;
  if (!bridge || typeof bridge.lsp_server_status !== 'function') return null;
  return bridge as LspSettingsBridge;
}

async function refresh(): Promise<void> {
  const bridge = api();
  if (!bridge) {
    errorMsg.value = '桥未就绪，无法读取语言服务器状态';
    return;
  }
  let res: Awaited<ReturnType<LspSettingsBridge['lsp_server_status']>>;
  try {
    res = await bridge.lsp_server_status();
  } catch (err) {
    // 传输层异常同样要看得见：静默的话用户只会看到一张空表
    errorMsg.value = `读取语言服务器状态失败：${err instanceof Error ? err.message : String(err)}`;
    return;
  }
  if (!res?.ok) {
    errorMsg.value = res?.msg || '读取语言服务器状态失败';
    return;
  }
  errorMsg.value = '';
  servers.value = res.servers ?? [];
}

async function applyPath(server: string, path: string): Promise<void> {
  const bridge = api();
  if (!bridge) {
    errorMsg.value = '桥未就绪，无法设置语言服务器路径';
    return;
  }
  let res: { ok: boolean; msg?: string };
  try {
    res = await bridge.lsp_set_server_path(server, path);
  } catch (err) {
    errorMsg.value = `设置语言服务器路径失败：${err instanceof Error ? err.message : String(err)}`;
    return;
  }
  if (!res?.ok) {
    // 明确反馈（文件不存在 / 不可执行由后端给原因），且**不**接着刷新——
    // refresh 成功会清空 errorMsg，用户就看不到失败原因了
    errorMsg.value = res?.msg || '设置失败';
    return;
  }
  await refresh();
}

function pick(row: LspServerStatus): void {
  const picked = window.prompt(
    `为 ${row.language} 指定 ${row.server} 可执行文件的完整路径`,
    row.executable || '',
  );
  if (picked === null) return;
  void applyPath(row.server, picked.trim());
}

function restore(row: LspServerStatus): void {
  void applyPath(row.server, '');
}

let unsubscribe: (() => void) | null = null;

onMounted(() => {
  void refresh();
  // 语言服务器起来/崩了都推事件，面板要跟着变，不能停在挂载那一刻的快照
  unsubscribe = onServerStatus(() => {
    void refresh();
  });
});

onBeforeUnmount(() => {
  // 设置页反复开关会累积监听器（每次挂载都注册而不撤销），必须退订
  unsubscribe?.();
  unsubscribe = null;
});
</script>

<style scoped>
.editor-language-settings {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.editor-language-settings__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.editor-language-settings__hint {
  color: var(--text-secondary, #888);
  font-size: 12px;
  line-height: 1.6;
}

.editor-language-settings__error {
  color: var(--danger, #d33);
}

.editor-language-settings__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.editor-language-settings__table th,
.editor-language-settings__table td {
  border-bottom: 1px solid var(--border, #333);
  padding: 4px 6px;
  text-align: left;
  vertical-align: top;
}

.editor-language-settings__state {
  display: block;
}

.editor-language-settings__hint-cell {
  display: block;
  color: var(--text-secondary, #888);
}

.editor-language-settings__path {
  word-break: break-all;
}

.editor-language-settings__source {
  margin-left: 6px;
  color: var(--text-secondary, #888);
}

.editor-language-settings__msg {
  display: block;
  margin-top: 2px;
  color: var(--danger, #d33);
}
</style>
