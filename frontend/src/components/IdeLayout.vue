<template>
  <div class="ide-layout">
    <!-- ActivityBar 的约定 prop/事件是 model / update:model（不是 v-model 默认的
         modelValue），所以这里必须用 v-model:model 传参，否则活动栏点击不生效 -->
    <ActivityBar v-model:model="activity" />

    <div v-if="activity === 'files'" class="ide-side">
      <div class="ide-side-head">
        <span>文件</span>
        <button type="button" class="icon-btn" title="刷新" @click="store.refreshFileTree()">⟳</button>
      </div>
      <div class="file-tree">
        <FileTreeNode
          v-for="node in store.fileTree"
          :key="node.n"
          :node="node"
          :parent-path="store.workdir"
        />
      </div>
    </div>
    <div v-else-if="activity === 'git'" class="ide-side">
      <GitPanel />
    </div>
    <div v-else class="ide-side">
      <div class="ide-empty">断点与调用栈将在 S3 接入后显示</div>
    </div>

    <div class="ide-main">
      <EditorTabs v-if="ide.tabs().length" />
      <div v-if="!ide.tabs().length" class="ide-empty-main">从左侧文件树打开文件</div>
      <EditorPane v-else :key="ide.activePath()" :path="ide.activePath()" />

      <div class="ide-bottom" :style="{ height: bottomHeight + 'px' }">
        <BottomPanel v-if="ide.bottomPanelOpen" @close="ide.toggleBottomPanel()" />
      </div>
      <div v-if="!ide.bottomPanelOpen" class="ide-bottom-open">
        <button type="button" class="icon-btn" @click="ide.toggleBottomPanel()">显示面板</button>
      </div>
    </div>

    <!-- 对话区宿主：对话模式的 #center 会 Teleport 到这里（见 App.vue），
         插槽本身留给未来需要在布局内直接嵌入对话区的场景 -->
    <div class="ide-chat" id="ide-chat-host">
      <slot name="chat" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';

import ActivityBar from './ActivityBar.vue';
import BottomPanel from './BottomPanel.vue';
import EditorPane from './EditorPane.vue';
import EditorTabs from './EditorTabs.vue';
import FileTreeNode from './FileTreeNode.vue';
import GitPanel from './GitPanel.vue';
import { useAppStore } from '../stores/appStore';
import { useIdeStore } from '../stores/ideStore';

const store = useAppStore();
const ide = useIdeStore();
// 用 ide 别名避免与 appStore 混用；文件树与工作区状态都来自 appStore，
// 标签、断点、底部面板状态都来自 ideStore
const activity = ref('files');
const bottomHeight = ref(200);
</script>

<style scoped>
.ide-layout {
  display: flex;
  flex: 1;
  min-height: 0;
  min-width: 0;
}

.ide-side {
  width: 260px;
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--bg1);
  border-right: 1px solid var(--border);
}

.ide-side-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 8px;
  font-size: 12px;
  color: var(--text-dim);
  border-bottom: 1px solid var(--border);
}

.file-tree {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.ide-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.ide-bottom {
  flex: 0 0 auto;
  min-height: 0;
  overflow: hidden;
}

.ide-bottom-open {
  flex: 0 0 auto;
  padding: 4px 8px;
  border-top: 1px solid var(--border);
}

.ide-chat {
  width: 380px;
  flex: 0 0 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--border);
}

.ide-empty,
.ide-empty-main {
  padding: 12px;
  font-size: 12px;
  color: var(--text-faint);
}

.ide-empty-main {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
