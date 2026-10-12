<template>
  <div class="activity-bar">
    <button
      v-for="item in topItems"
      :key="item.id"
      type="button"
      class="activity-btn"
      :class="{ active: model === item.id }"
      :title="item.title"
      :aria-label="item.title"
      @click="activate(item)"
    >
      <AppIcon :name="item.icon" :size="21" />
    </button>

    <!-- 撑开剩余高度，把「设置」压到活动栏底部（对应设计稿 .act-spacer）：
         设置是全局入口而不是左侧面板，贴底才和上面的面板型按钮区分开 -->
    <div class="activity-spacer"></div>

    <button
      v-for="item in bottomItems"
      :key="item.id"
      type="button"
      class="activity-btn"
      :class="{ active: model === item.id }"
      :title="item.title"
      :aria-label="item.title"
      @click="activate(item)"
    >
      <AppIcon :name="item.icon" :size="21" />
    </button>
  </div>
</template>

<script setup lang="ts">
import AppIcon from './AppIcon.vue';

defineProps<{ model: string }>();
const emit = defineEmits<{
  (e: 'update:model', value: string): void;
  (e: 'action', id: string): void;
}>();

// 活动栏项有两种语义（对齐设计稿 mockups/ide-mode-redesign.html 的 .activitybar）：
// - panel：切换左侧面板，走既有的 update:model 约定，按 model 高亮；
// - action：一次性动作（星瑶聚焦对话输入框、设置打开设置页），只发 action、不参与高亮。
// 若把 action 也塞进 update:model，当前左侧面板会被顶掉（落到空分支），
// 按钮还会被误标成"当前面板"高亮。
type ActivityItem = {
  id: string;
  icon: string;
  title: string;
  kind: 'panel' | 'action';
};

// 图标名必须是 frontend/src/components/iconPaths.ts 里已注册的键（该文件第 2 行
// 明确要求保持纯字符串注册表以便控制体积），下面七个键均已存在，不要新增图标
const topItems: ActivityItem[] = [
  { id: 'files', icon: 'folder', title: '资源管理器', kind: 'panel' },
  { id: 'search', icon: 'search', title: '搜索', kind: 'panel' },
  { id: 'git', icon: 'git-branch', title: '源代码管理', kind: 'panel' },
  { id: 'debug', icon: 'play', title: '运行和调试', kind: 'panel' },
  { id: 'extensions', icon: 'blocks', title: '扩展', kind: 'panel' },
  { id: 'ai', icon: 'brain', title: '星瑶', kind: 'action' },
];

const bottomItems: ActivityItem[] = [
  { id: 'settings', icon: 'settings', title: '设置', kind: 'action' },
];

function activate(item: ActivityItem) {
  if (item.kind === 'panel') {
    emit('update:model', item.id);
    return;
  }
  emit('action', item.id);
}
</script>

<style scoped>
.activity-bar {
  /* 宽度按设计稿 .activitybar 定为 48px：40px 按钮 + 左右各 4px 内边距。
     只定外框宽度、按钮尺寸同步放大，内部布局仍是纵向 flex + 底部 spacer。 */
  width: 48px;
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 4px;
  background: var(--bg1);
  border-right: 1px solid var(--border);
}

.activity-spacer {
  flex: 1;
  min-height: 0;
}

.activity-btn {
  /* 设计稿 .act：40×38 的图标按钮，配 21px 图标（原 32×32/16px 在 48px 栏里显得空） */
  width: 40px;
  height: 38px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
}

.activity-btn:hover {
  background: var(--bg2);
  color: var(--text);
}

.activity-btn.active {
  background: transparent;
  color: var(--text-bright, var(--text));
  box-shadow: inset 2px 0 0 var(--accent);
}
</style>
