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
      <!-- 调试工具条挂在编辑器区顶部：它是「运行 / 调试当前文件」在 IDE 模式下的唯一
           入口。Task 9 只要求 Create 组件、全仓并无挂载点，导致计划自己的验收条款
           （Task 13 Step 5「点调试工具条『运行』」）在真实 IDE 模式里执行不了——
           等于交了死代码（S3-T9 评审）。工具条对活动文件操作，因此随编辑器区常驻，
           不随标签是否存在而退场（无文件时按钮自己 disabled）。 -->
      <DebugToolbar />
      <EditorTabs v-if="ide.tabs().length" />
      <div v-if="!ide.tabs().length" class="ide-empty-main">从左侧文件树打开文件</div>
      <EditorPane v-else :key="ide.activePath()" :path="ide.activePath()" />

      <!-- v-show 而非 v-if（S3-T12 接线）：底部面板收起期间必须保持挂载——调试控制台
           的行缓冲、断点 gutter 与执行行高亮都活在组件里，v-if 卸载会把它们一并丢掉，
           与「收起期间调试输出不丢」冲突。隐藏的是外面这层定高容器，所以收起后不占高度。 -->
      <div v-show="ide.bottomPanelOpen" class="ide-bottom" :style="{ height: bottomHeight + 'px' }">
        <BottomPanel @close="ide.toggleBottomPanel()" />
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
import { onMounted, ref, watch } from 'vue';

import ActivityBar from './ActivityBar.vue';
import BottomPanel from './BottomPanel.vue';
import DebugToolbar from './DebugToolbar.vue';
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

// 先登记会话、再恢复标签：顺序不能反（Task 8 评审 Important）——
// ideStore 的标签桶按会话隔离，若反过来，先 openFile 的标签会落进 '' 桶，
// 会话绑定后成为孤儿、用户已打开的文件凭空消失。
onMounted(async () => {
  ide.setSession(store.currentSessionName);
  const api = (window as any).pywebview?.api;
  if (!api?.ide_get_layout) return;
  const res = await api.ide_get_layout(store.currentSessionName);
  if (res?.ok && res.layout) {
    for (const item of res.layout.tabs) {
      try {
        await ide.openFile(item.path);
      } catch {
        // 上次打开的文件可能已被删除或移出工作区，静默跳过
      }
    }
    // 恢复 active 前必须确认该标签真的恢复了：快照里的 active 可能指向已被删除或
    // 移出工作区的文件——上面的 openFile 读取失败会被 try/catch 静默吞掉，标签根本没建。
    // 若无条件 activate，活动标签会指向一个不存在的标签，并随后经 persistLayout 把这条
    // 陈旧 active 写回快照（重开一次脏一次）。这里用 tab() 与 openFile 相同的查表口径
    // （realpath 精确比对 + 调用方路径别名）判存在性；不存在就保持恢复过程中最后成功
    // 打开的那个标签，空快照则保持空。
    if (res.layout.active && ide.tab(res.layout.active)) ide.activate(res.layout.active);
  }
});

// IDE 布局常驻不重建：会话切换时也把标签桶重新绑定到新会话，
// 否则切换会话后新打开的文件会写进上一个会话的桶（跨会话串标签）。
watch(() => store.currentSessionName, (name) => {
  ide.setSession(name);
  // 切会话 = 切工作区（工作区跟随会话），必须回收语言服务器：进程与工作区绑定
  // （LSP initialize 的 rootUri 写死），留着上一个工作区不仅白占内存，还会让
  // 迟到的诊断/跳转指向错误目录。回收幂等，与下面的 workdir 监听重复调用无副作用。
  void ide.syncWorkspace();
});

// 工作区变了但会话没变（例如给「对话」会话引入项目文件夹，或手机上按路径选目录）：
// 同样要回收，否则旧工作区的语言服务器会一直跑着（验收 S2-3：切工作区无残留进程）。
watch(() => store.workdir, () => {
  void ide.syncWorkspace();
});
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
