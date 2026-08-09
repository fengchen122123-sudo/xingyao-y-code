<template>
  <div id="model-cascader" :class="{ show: visible, 'opens-up': opensUp }" :style="styleObj" @click.stop>
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
        <span v-if="m.supports_vision" class="c-vision" title="支持多模态">◉</span>
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
          <span class="c-provider-icon">◇</span>
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
      <template v-if="hasAnyModel">
        <div class="cascader-sep"></div>
        <div class="effort-row" title="当前模型的推理强度（按模型保存，下一轮请求生效）">
          <span class="effort-label">推理强度</span>
          <span
            v-for="opt in effortOptions"
            :key="opt.v"
            class="effort-opt"
            :class="{ active: store.reasoningEffort === opt.v }"
            @click.stop="selectEffort(opt.v)"
          >{{ opt.label }}</span>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import type { ModelInfo, ReasoningEffort } from '../types/pywebview';

const props = defineProps<{
  visible: boolean;
  anchorEl: HTMLElement | null;
}>();

const emit = defineEmits(['close']);
const store = useAppStore();

const activeProviderName = ref<string | null>(null);
const styleObj = ref<Record<string, string>>({});
const opensUp = ref(false);

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
  store.openModelSettings();
  emit('close');
}

// 推理强度档位：default = 模型条目未配置（请求层内置默认）
const effortOptions: Array<{ v: ReasoningEffort; label: string }> = [
  { v: 'default', label: '默认' },
  { v: 'low', label: '低' },
  { v: 'high', label: '高' },
  { v: 'max', label: '最深' },
];

function selectEffort(level: ReasoningEffort) {
  store.setReasoningEffort(level);
}

watch(() => props.visible, (newVal) => {
  if (newVal && props.anchorEl) {
    const r = props.anchorEl.getBoundingClientRect();
    const isBottomHalf = r.top > window.innerHeight / 2;
    opensUp.value = isBottomHalf;

    if (isBottomHalf) {
      const left = Math.max(10, Math.min(r.left, window.innerWidth - 520));
      styleObj.value = {
        position: 'fixed',
        bottom: `${window.innerHeight - r.top + 8}px`,
        left: `${left}px`,
        right: 'auto',
        top: 'auto',
      };
    } else {
      const left = Math.max(10, Math.min(r.left, window.innerWidth - 520));
      styleObj.value = {
        position: 'fixed',
        top: `${r.bottom + 8}px`,
        left: `${left}px`,
        right: 'auto',
        bottom: 'auto',
      };
    }

    // 二级模型列表只在悬停供应商后展开，保持一级列表简洁。
    activeProviderName.value = null;
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
  flex-direction: row;
  align-items: flex-start;
  gap: 8px;
  animation: fadein 0.15s cubic-bezier(0.16, 1, 0.3, 1);
}
#model-cascader.show {
  display: flex;
}
#model-cascader.opens-up {
  align-items: flex-end;
}
.cascader-main-panel,
.cascader-sub-panel {
  background: var(--float-bg);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.55);
  padding: 6px;
  min-width: 240px;
  max-height: min(520px, calc(100vh - 90px));
  overflow-y: auto;
  backdrop-filter: blur(14px);
}
.cascader-main-panel {
  order: 1;
}
.cascader-sub-panel {
  order: 2;
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
.c-provider-icon {
  color: var(--text-faint);
  margin-right: 8px;
}
.c-vision {
  color: var(--accent);
  font-size: 10px;
  margin-left: 8px;
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
.effort-row {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 10px 4px;
  user-select: none;
}
.effort-label {
  font-size: 11px;
  color: var(--text-faint);
  margin-right: 4px;
  white-space: nowrap;
}
.effort-opt {
  font-size: 11px;
  color: var(--text-dim);
  padding: 2px 8px;
  border-radius: 6px;
  border: 1px solid var(--border);
  cursor: pointer;
  transition: 0.15s;
  white-space: nowrap;
}
.effort-opt:hover {
  background: var(--bg3);
  color: var(--text);
}
.effort-opt.active {
  color: var(--accent);
  border-color: var(--accent);
  background: var(--bg3);
}
</style>
