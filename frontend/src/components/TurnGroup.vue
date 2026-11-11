<template>
  <!-- 触发本轮的用户消息。data-msg-id 经 attrs 透传到 MessageBubble 根元素
       （.msg-user）：红点导航按消息 id 精确定位用；本地新消息无 _msg_id 时不渲染 -->
  <MessageBubble v-if="group.userMsg" :msg="group.userMsg" :data-msg-id="group.userMsg?._msg_id ?? undefined" />

  <!-- 回合运行中：保持现状，中间过程逐条实时显示，不折叠 -->
  <template v-if="group.running">
    <MessageBubble
      v-for="m in group.middle"
      :key="msgKey(m)"
      :msg="m"
      :show-name="msgKey(m) === nameOwnerKey"
    />
  </template>
  <!-- 回合已结束：中间过程收成一块可展开的折叠区（FoldCard 统一折叠体系），
       突出最终总结 -->
  <div v-else-if="group.middle.length" class="turn-fold">
    <FoldCard v-model:open="expanded" :title="title">
      <MessageBubble
        v-for="m in group.middle"
        :key="msgKey(m)"
        :msg="m"
        :show-name="msgKey(m) === nameOwnerKey"
      />
    </FoldCard>
  </div>

  <!-- 组尾：总结轮 assistant + 其后的 error 消息，始终可见 -->
  <MessageBubble
    v-for="m in group.tail"
    :key="msgKey(m)"
    :msg="m"
    :show-name="msgKey(m) === nameOwnerKey"
  />

  <!-- 回合结束且有文件改动时：底部展示可折叠的"文件更改"面板
       （含按回合撤销按钮：按权威 turnId 绑定状态） -->
  <ChangedFilesPanel
    v-if="!group.running && fileChanges.length"
    class="turn-changes"
    :changes="fileChanges"
    :undo-state="undoState"
    :turn-id="group.turnId"
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

// 一个回合只在最前面显示一次「星瑶 · 模型」名字行：
// 运行中 / 折叠区展开时由中间过程第一条 assistant 承担；折叠收起（中间过程
// 不可见）时改由组尾第一条承担，保证可见区域顶部始终有且只有一个名字行。
const nameOwnerKey = computed<number | null>(() => {
  const firstMiddle = props.group.middle.find((m) => m.role === 'assistant');
  const firstTail = props.group.tail.find((m) => m.role === 'assistant');
  const middleVisible = props.group.running || expanded.value;
  const owner = (middleVisible ? firstMiddle : null) ?? firstTail ?? firstMiddle;
  return owner ? msgKey(owner) : null;
});

// 回合文件更改摘要（无改动则空数组，面板不渲染）
const fileChanges = computed(() => collectFileChanges(props.group));

// 按回合撤销：本回合的权威检查点状态（按 turnId 绑定；无关联回合为 null）
const store = useAppStore();
const undoState = computed(() => {
  const turnId = props.group.turnId;
  return turnId ? (store.turnUndoStates[turnId] ?? null) : null;
});

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
