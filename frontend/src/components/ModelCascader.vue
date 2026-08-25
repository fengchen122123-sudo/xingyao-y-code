<template>
  <!-- Teleport 到 body：fixed 浮层必须逃离所在分区的 isolation 层叠上下文
       （composer 分区 z=5，否则往上弹时被 workspace(6)/会话工具栏(10) 盖住） -->
  <Teleport to="body">
    <div id="model-cascader" :class="{ show: visible, 'opens-up': opensUp }" :style="styleObj" @click.stop>
    <!-- 二级子面板：具体模型列表 -->
    <div ref="subPanelEl" class="cascader-sub-panel" :class="{ show: activeProviderName }" :style="subPanelStyle">
      <div
        v-for="m in currentSubModels"
        :key="m.id || m.name"
        class="cascader-item"
        :class="{ active: m.id === store.activeModelId }"
        @click="selectModel(m.id)"
      >
        <span class="c-label">{{ m.name }}</span>
        <span v-if="m.supports_vision" class="c-vision" title="支持多模态"><AppIcon name="eye" :size="12" /></span>
        <span v-if="m.id === store.activeModelId" class="c-check checked"><AppIcon name="check" :size="13" :stroke-width="2.25" /></span>
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
          @mouseenter="activateProvider(provider as string, $event)"
        >
          <span class="c-provider-icon"><AppIcon name="hexagon" :size="12" /></span>
          <span class="c-label">{{ provider }}</span>
          <span v-if="hasCurrentModel(modelsInGroup)" class="c-check"><AppIcon name="check" :size="13" :stroke-width="2.25" /></span>
          <span class="c-arrow"><AppIcon name="chevron-right" :size="13" /></span>
        </div>
        <div class="cascader-sep"></div>
      </template>
      <div v-else class="cascader-empty">暂无模型，去添加</div>
      <div class="cascader-item cascader-manage" title="添加/管理供应商与模型" @click="manageModels">
        <span class="c-label"><AppIcon name="settings" :size="12" /> 管理模型</span>
      </div>
      <template v-if="hasAnyModel">
        <div class="cascader-sep"></div>
        <div class="effort-row" title="当前模型的推理强度（按模型保存，下一轮请求生效）">
          <span class="effort-label">推理强度</span>
          <span
            v-for="opt in effortOptions"
            :key="opt.v"
            class="effort-opt"
            :class="{ active: store.reasoning.choice === opt.v }"
            @click.stop="selectEffort(opt.v)"
          >{{ opt.label }}</span>
        </div>
      </template>
    </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';
import type { ModelInfo, ReasoningChoice } from '../types/pywebview';

const props = defineProps<{
  visible: boolean;
  anchorEl: HTMLElement | null;
}>();

const emit = defineEmits(['close']);
const store = useAppStore();

const activeProviderName = ref<string | null>(null);
const styleObj = ref<Record<string, string>>({});
const subPanelStyle = ref<Record<string, string>>({});
const subPanelEl = ref<HTMLElement | null>(null);
const opensUp = ref(false);
// 记住上次悬停的供应商行：主面板跟随锚点移动时同步重算二级面板位置
const lastProviderRow = ref<HTMLElement | null>(null);
let rafId = 0;

const MENU_GAP = 8;
const VIEWPORT_PADDING = 12;
const SUB_PANEL_MIN_WIDTH = 240;
// 1px border + 6px panel padding: aligns the first model row with the provider row.
const SUB_PANEL_ITEM_INSET = 7;

const groupedModels = computed(() => store.modelsByProvider);
const hasAnyModel = computed(() => store.models.length > 0);

const currentSubModels = computed(() => {
  if (!activeProviderName.value) return [];
  return groupedModels.value[activeProviderName.value] || [];
});

function hasCurrentModel(modelsList: ModelInfo[]) {
  return modelsList.some(m => m.id === store.activeModelId);
}

// 按当前供应商最长模型名估算子面板宽度（中文按全角 2 倍计），
// 用于右側放不下时的左翻判断；实测宽度渲染后由 offsetWidth 校正
function estimateSubPanelWidth(): number {
  let maxUnits = 0;
  for (const m of currentSubModels.value) {
    let units = 0;
    for (const ch of m.name || '') units += ch.charCodeAt(0) > 255 ? 2 : 1;
    maxUnits = Math.max(maxUnits, units);
  }
  return Math.max(SUB_PANEL_MIN_WIDTH, Math.ceil(maxUnits * 7.2) + 70);
}

function positionSubPanel(providerRow: HTMLElement) {
  const mainPanel = providerRow.closest('.cascader-main-panel');
  if (!(mainPanel instanceof HTMLElement)) {
    throw new Error('Model provider row must be inside the cascader main panel');
  }

  const rowRect = providerRow.getBoundingClientRect();
  const mainPanelRect = mainPanel.getBoundingClientRect();
  const top = Math.max(VIEWPORT_PADDING, Math.floor(rowRect.top - SUB_PANEL_ITEM_INSET));

  // 实测宽度优先（已渲染），否则按名字估算
  const panelWidth = subPanelEl.value?.offsetWidth || estimateSubPanelWidth();
  const rightLeft = Math.floor(mainPanelRect.right + MENU_GAP);
  const rightSpace = window.innerWidth - rightLeft - VIEWPORT_PADDING;
  const leftSpace = Math.floor(mainPanelRect.left - MENU_GAP) - VIEWPORT_PADDING;

  let left: number;
  let maxWidth = 0;
  if (panelWidth <= rightSpace) {
    // 右侧放得下：照常向右展开
    left = rightLeft;
  } else if (panelWidth <= leftSpace) {
    // 右侧放不下、左侧够宽：翻到主面板左边
    left = Math.floor(mainPanelRect.left - MENU_GAP - panelWidth);
  } else if (rightSpace >= leftSpace) {
    // 两侧都不够：放更宽的一侧并限宽，条目省略号兜底
    left = rightLeft;
    maxWidth = Math.max(160, rightSpace);
  } else {
    maxWidth = Math.max(160, leftSpace);
    left = Math.max(VIEWPORT_PADDING, Math.floor(mainPanelRect.left - MENU_GAP - maxWidth));
  }

  subPanelStyle.value = {
    position: 'fixed',
    top: `${top}px`,
    left: `${left}px`,
    right: 'auto',
    bottom: 'auto',
    maxHeight: `${Math.max(0, Math.floor(window.innerHeight - top - VIEWPORT_PADDING))}px`,
    ...(maxWidth ? { maxWidth: `${maxWidth}px` } : {}),
  };
}

function activateProvider(provider: string, event: MouseEvent) {
  activeProviderName.value = provider;
  const providerRow = event.currentTarget as HTMLElement;
  lastProviderRow.value = providerRow;
  positionSubPanel(providerRow);
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
const effortOptions: Array<{ v: ReasoningChoice; label: string }> = [
  { v: 'default', label: '默认' },
  { v: 'low', label: '低' },
  { v: 'high', label: '高' },
  { v: 'max', label: '最深' },
];

function selectEffort(level: ReasoningChoice) {
  store.setReasoningChoice(level);
}

function positionMainPanel() {
  if (!props.anchorEl) return;
  const r = props.anchorEl.getBoundingClientRect();
  const upwardHeight = Math.max(0, Math.floor(r.top - MENU_GAP - VIEWPORT_PADDING));
  const downwardHeight = Math.max(0, Math.floor(
    window.innerHeight - r.bottom - MENU_GAP - VIEWPORT_PADDING,
  ));
  const isBottomHalf = upwardHeight >= downwardHeight;
  const maxPanelHeight = isBottomHalf ? upwardHeight : downwardHeight;
  opensUp.value = isBottomHalf;

  const left = Math.max(10, Math.min(r.left, window.innerWidth - 520));
  if (isBottomHalf) {
    styleObj.value = {
      position: 'fixed',
      bottom: `${window.innerHeight - r.top + MENU_GAP}px`,
      left: `${left}px`,
      right: 'auto',
      top: 'auto',
      '--cascader-max-height': `${maxPanelHeight}px`,
    };
  } else {
    styleObj.value = {
      position: 'fixed',
      top: `${r.bottom + MENU_GAP}px`,
      left: `${left}px`,
      right: 'auto',
      bottom: 'auto',
      '--cascader-max-height': `${maxPanelHeight}px`,
    };
  }
}

// 菜单打开期间逐帧跟随锚点：收起/展开右侧面板等布局变化（含 0.25s 过渡动画）
// 会让锚点持续移动，一次性快照必然过期，导致主/子面板错位层叠
function trackAnchor() {
  positionMainPanel();
  if (activeProviderName.value && lastProviderRow.value) {
    positionSubPanel(lastProviderRow.value);
  }
  rafId = requestAnimationFrame(trackAnchor);
}

watch(() => props.visible, (newVal) => {
  cancelAnimationFrame(rafId);
  if (newVal && props.anchorEl) {
    positionMainPanel();

    // 二级模型列表只在悬停供应商后展开，保持一级列表简洁。
    activeProviderName.value = null;
    subPanelStyle.value = {};
    lastProviderRow.value = null;
    rafId = requestAnimationFrame(trackAnchor);
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
  cancelAnimationFrame(rafId);
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
  /* 入场动画在面板上而不在容器：容器动画的 transform 会创建 containing block，
     使 fixed 定位的二级面板在动画期间相对容器而非视口，表现为短暂错位 */
}
#model-cascader.show {
  display: flex;
}
#model-cascader.opens-up {
  align-items: flex-end;
}
.cascader-main-panel,
.cascader-sub-panel {
  /* 半透明毛玻璃：以主题浮窗色为基准混入 20% 透明，配合下方 blur 透出
     全局背景图/分区层（Teleport 后位于所有图层之上，可读性由模糊保证） */
  background: color-mix(in srgb, var(--theme-float-background) 80%, transparent);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.55);
  padding: 6px;
  min-width: 240px;
  max-height: var(--cascader-max-height, min(520px, calc(100vh - 90px)));
  box-sizing: border-box;
  overflow-y: auto;
  backdrop-filter: blur(14px);
}
.cascader-main-panel {
  order: 1;
  animation: fadein 0.15s cubic-bezier(0.16, 1, 0.3, 1);
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
