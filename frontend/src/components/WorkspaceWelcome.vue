<template>
  <!-- 编辑器区欢迎页（设计稿 .welcome）：未打开任何标签且没有工作区时占据编辑器区。
       卡片里的每个动作都经过后端能力核查（见脚本里的逐项注释）：
       有真实接口的接真实调用，没有的一律 disabled + 说明，不做"看着能点却没反应"的按钮。 -->
  <div class="welcome" data-test="workspace-welcome">
    <div class="card">
      <button
        v-for="action in actions"
        :key="action.label"
        type="button"
        class="welcome-action"
        :data-test="action.test"
        :disabled="action.disabled"
        :title="action.title || action.label"
        @click="action.run && action.run()"
      >
        <AppIcon :name="action.icon" :size="16" />
        <span class="label">{{ action.label }}</span>
        <span v-if="action.disabled" class="soon">暂未接入</span>
        <span v-else class="arrow">→</span>
      </button>
    </div>

    <!-- 快捷键提示只写真实存在的绑定：Ctrl+F（App.vue 全局会话检索）、
         Ctrl+Alt+I（App.vue 全局 IDE/对话模式切换）、Ctrl+S（EditorPane 内保存）。
         设计稿里的 Ctrl+P / Ctrl+` / Ctrl+B 前端从未实现，一律不写。 -->
    <div class="hint">
      <span><kbd>Ctrl</kbd> + <kbd>F</kbd> 搜索会话</span>
      <span><kbd>Ctrl</kbd> + <kbd>Alt</kbd> + <kbd>I</kbd> 切换对话模式</span>
      <span><kbd>Ctrl</kbd> + <kbd>S</kbd> 保存当前文件</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();

type WelcomeAction = {
  label: string;
  icon: string;
  test: string;
  /** 无能力时置 true：渲染成禁用态并在按钮上标出「暂未接入」 */
  disabled?: boolean;
  /** 禁用原因（鼠标悬浮可见）；启用时是动作名 */
  title?: string;
  run?: () => void;
};

const actions = computed<WelcomeAction[]>(() => [
  {
    label: '打开文件夹',
    icon: 'folder-open',
    test: 'welcome-open-folder',
    title: '选择一个项目文件夹作为工作区',
    // appStore.openWorkspaceFolder 走原生文件夹对话框并登记工作区（已有能力，直接复用）
    run: () => { void store.openWorkspaceFolder(); },
  },
  {
    label: '新建项目',
    icon: 'plus',
    test: 'welcome-new-project',
    // 已核查 gui_bridge.py：没有新建目录 / 新建项目的桥方法（只有 ide_write_file 写文件，
    // 无法建目录），故只能禁用
    disabled: true,
    title: '暂未接入：后端没有新建项目（创建目录）的接口',
  },
  {
    label: '克隆 Git 仓库',
    icon: 'git-branch',
    test: 'welcome-clone-repo',
    // 已核查 gui_bridge.py / gui_git_mixin.py：只有对**已存在**仓库的 status/stage/commit/
    // fetch/pull/push/history，没有 git clone；前端也搜不到任何 clone 调用
    disabled: true,
    title: '暂未接入：后端没有 git clone 接口',
  },
  {
    label: '连接远程主机',
    icon: 'globe',
    test: 'welcome-remote-host',
    // 已核查：全仓没有 SSH / 远程开发相关桥方法（core/ 与 gui_bridge.py 均无）
    disabled: true,
    title: '暂未接入：当前版本不支持连接远程主机',
  },
  {
    label: '新建文件',
    icon: 'file',
    test: 'welcome-new-file',
    // 后端确实能用 ide_write_file(path, '', '') 建空文件，但这里没有工作目录可落盘
    // （欢迎页只在未打开文件夹时出现），故禁用并说明前置条件；有工作区时用
    // 资源管理器头部的「新建文件」按钮（那条路径是真实可用的）。
    disabled: true,
    title: '请先打开文件夹：新建文件需要一个工作目录',
  },
]);
</script>

<style scoped>
.welcome {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 22px;
  padding: 20px;
  background: var(--bg0);
}

.card {
  width: 340px;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: var(--theme-radius-medium, 8px);
  overflow: hidden;
}

.welcome-action {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 11px 14px;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: transparent;
  color: var(--text);
  font-size: 12.5px;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
}

.welcome-action:last-child {
  border-bottom: 0;
}

.welcome-action:hover:not(:disabled) {
  background: var(--bg2);
  color: var(--text-bright);
}

/* 禁用态：降透明度 + 默认光标，配合按钮上的「暂未接入」标记，
   避免它看起来和可用项一样 */
.welcome-action:disabled {
  color: var(--text-faint);
  cursor: not-allowed;
  opacity: 0.7;
}

.welcome-action .label {
  flex: 1;
}

/* 图标跟随强调色（设计稿 .welcome .card button svg）；禁用项随文字一起变暗 */
.welcome-action svg {
  color: var(--accent);
}

.welcome-action:disabled svg {
  color: var(--text-faint);
}

.welcome-action .arrow {
  color: var(--text-faint);
}

.welcome-action .soon {
  color: var(--text-faint);
  font-size: 11px;
}

.hint {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 14px;
  color: var(--text-faint);
  font-size: 12px;
}

.hint kbd {
  font-family: var(--mono);
  font-size: 11px;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-bottom-width: 2px;
  border-radius: 4px;
  padding: 1px 5px;
  color: var(--text-dim);
}
</style>
