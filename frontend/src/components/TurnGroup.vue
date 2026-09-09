<template>
  <!-- 触发本轮的用户消息 -->
  <MessageBubble v-if="group.userMsg" :msg="group.userMsg" />

  <!-- 回合运行中：保持现状，中间过程逐条实时显示，不折叠 -->
  <template v-if="group.running">
    <MessageBubble v-for="m in group.middle" :key="msgKey(m)" :msg="m" />
  </template>
  <!-- 回合已结束：中间过程收成一块可展开的折叠区（FoldCard 统一折叠体系），
       突出最终总结 -->
  <div v-else-if="group.middle.length" class="turn-fold">
    <FoldCard v-model:open="expanded" :title="title">
      <MessageBubble v-for="m in group.middle" :key="msgKey(m)" :msg="m" />
    </FoldCard>
  </div>

  <!-- 组尾：总结轮 assistant + 其后的 error 消息，始终可见 -->
  <MessageBubble v-for="m in group.tail" :key="msgKey(m)" :msg="m" />

  <!-- 回合结束且有文件改动时：底部展示可折叠的"文件更改"面板 -->
  <ChangedFilesPanel
    v-if="!group.running && fileChanges.length"
    class="turn-changes"
    :changes="fileChanges"
  />
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import ChangedFilesPanel from './ChangedFilesPanel.vue';
import FoldCard from './FoldCard.vue';
import MessageBubble from './MessageBubble.vue';
import { useAppStore } from '../stores/appStore';
import {
  collectFileChanges,
  groupStats,
  msgKey,
  type TurnGroupInfo,
} from '../utils/turnGroups';

const props = defineProps<{
  group: TurnGroupInfo;
}>();

// 回合结束时默认收起，只留一行摘要；点击可展开回看完整过程。
// 折叠状态纯组件内 UI，不写入 store。
const expanded = ref(false);

const STATE_LABELS: Record<TurnGroupInfo['state'], string> = {
  completed: '已完成',
  cancelled: '已取消',
  error: '异常结束',
};

const stats = computed(() => groupStats(props.group));

// 回合文件更改摘要（无改动则空数组，面板不渲染）
const fileChanges = computed(() => collectFileChanges(props.group));

const title = computed(() => {
  const { rounds, toolCount } = stats.value;
  const stateLabel = STATE_LABELS[props.group.state];
  const agentName = useAppStore().agentName;
  if (toolCount > 0) {
    return `${agentName}执行过程 · ${toolCount} 个工具调用 · ${stateLabel}`;
  }
  return `${agentName}执行过程 · ${rounds} 轮 · ${stateLabel}`;
});
</script>

<style scoped>
.turn-fold {
  max-width: var(--chat-col-width);
  margin: 2px auto 6px;
}
.turn-changes {
  max-width: var(--chat-col-width);
  margin: 6px auto 10px;
}
/* 回合折叠头在统一体系上加轻量 hover 底色，与消息流的区分度更强 */
.turn-fold :deep(.fold-head) {
  padding: 3px 8px;
  border-radius: 8px;
}
.turn-fold :deep(.fold-head:hover) {
  background: var(--bg2);
}
</style>
