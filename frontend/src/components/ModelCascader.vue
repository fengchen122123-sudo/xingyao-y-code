<template>
  <div id="model-cascader" :class="{ show: visible }" :style="styleObj" @click.stop>
    <!-- 二级子面板：具体模型列表 -->
    <div class="cascader-sub-panel" :class="{ show: activeProviderName }">
      <div
        v-for="m in currentSubModels"
        :key="m.id || m.name"
        class="cascader-item"
        :class="{ active: m.id === store.activeModelId }"
        @click="selectModel(m.id)"
      >
        <span class="c-label">{{ m.name }}</span>
        <span v-if="m.id === store.activeModelId" class="c-check checked">✓</span>
      </div>
    </div>

    <!-- 一级主面板：供应商列表 -->
    <div class="cascader-main-panel">
      <template v-if="hasAnyModel">
        <div
          v-for="(modelsInGroup, provider) in groupedModels"
          :key="provider"
          class="cascader-item"
          :class="{ active: activeProviderName === provider }"
          @mouseenter="activeProviderName = provider as string"
        >
          <span class="c-label">{{ provider }}</span>
          <span v-if="hasCurrentModel(modelsInGroup)" class="c-check">✓</span>
          <span class="c-arrow">›</span>
        </div>
        <div class="cascader-sep"></div>
      </template>
      <div v-else class="cascader-empty">暂无模型，点击 ⚙ 添加</div>
      <div class="cascader-item cascader-manage" title="添加/管理供应商与模型" @click="manageModels">
        <span class="c-label">⚙ 管理模型</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import type { ModelInfo } from '../types/pywebview';

const props = defineProps<{
  visible: boolean;
  anchorEl: HTMLElement | null;
}>();

const emit = defineEmits(['close']);
const store = useAppStore();

const activeProviderName = ref<string | null>(null);
const styleObj = ref<Record<string, string>>({});

const groupedModels = computed(() => store.modelsByProvider);
const hasAnyModel = computed(() => store.models.length > 0);

const currentSubModels = computed(() => {
  if (!activeProviderName.value) return [];
  return groupedModels.value[activeProviderName.value] || [];
});

function hasCurrentModel(modelsList: ModelInfo[]) {
  return modelsList.some(m => m.id === store.activeModelId);
}

function selectModel(modelId: string) {
  store.setModel(modelId);
  emit('close');
}

function manageModels() {
  store.openSubpage('models');
  emit('close');
}

watch(() => props.visible, (newVal) => {
  if (newVal && props.anchorEl) {
    const r = props.anchorEl.getBoundingClientRect();
    const isBottomHalf = r.top > window.innerHeight / 2;

    if (isBottomHalf) {
      styleObj.value = {
        position: 'fixed',
        bottom: `${window.innerHeight - r.top + 8}px`,
        right: `${Math.max(10, window.innerWidth - r.right)}px`,
        top: 'auto',
        left: 'auto',
      };
    } else {
      styleObj.value = {
        position: 'fixed',
        top: `${r.bottom + 8}px`,
        right: `${Math.max(10, window.innerWidth - r.right)}px`,
        bottom: 'auto',
        left: 'auto',
      };
    }

    // 默认高亮并展开当前模型的供应商
    activeProviderName.value = null;
    for (const [provider, list] of Object.entries(groupedModels.value)) {
      if (hasCurrentModel(list)) {
        activeProviderName.value = provider;
        break;
      }
    }
  }
});

function onGlobalClick(e: MouseEvent) {
  if (props.visible && props.anchorEl && !props.anchorEl.contains(e.target as Node)) {
    emit('close');
  }
}

onMounted(() => {
  document.addEventListener('click', onGlobalClick);
});
onUnmounted(() => {
  document.removeEventListener('click', onGlobalClick);
});
</script>

<style scoped>
#model-cascader {
  position: fixed;
  z-index: 999;
  display: none;
  flex-direction: row-reverse;
  align-items: flex-start;
  gap: 8px;
  animation: fadein 0.15s cubic-bezier(0.16, 1, 0.3, 1);
}
#model-cascader.show {
  display: flex;
}
.cascader-main-panel,
.cascader-sub-panel {
  background: var(--float-bg);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.55);
  padding: 6px;
  min-width: 180px;
  backdrop-filter: blur(14px);
}
.cascader-sub-panel {
  display: none;
  animation: fadein 0.15s;
}
.cascader-sub-panel.show {
  display: block;
}
.cascader-item {
  padding: 9px 12px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--text-dim);
  transition: 0.15s;
  user-select: none;
}
.cascader-item:hover,
.cascader-item.active {
  background: var(--bg3);
  color: var(--text);
}
.cascader-item .c-label {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cascader-item .c-check {
  color: var(--text-faint);
  font-size: 12px;
  margin-left: 8px;
}
.cascader-item.active .c-check,
.cascader-item .c-check.checked {
  color: var(--accent);
}
.cascader-item .c-arrow {
  color: var(--text-faint);
  font-size: 12px;
  margin-left: 6px;
}
.cascader-sep {
  height: 1px;
  background: var(--border);
  margin: 5px 6px;
}
.cascader-manage {
  color: var(--text-dim);
}
.cascader-manage:hover {
  color: var(--accent);
}
.cascader-empty {
  padding: 14px 12px;
  font-size: 12px;
  color: var(--text-faint);
  text-align: center;
}
</style>
