<template>
  <!-- Teleport 到 body：fixed 浮层必须逃离所在分区的 isolation 层叠上下文
       （composer 分区 z=5，否则往上弹时被 workspace(6)/会话工具栏(10) 盖住） -->
  <Teleport to="body">
    <div id="model-cascader" :class="{ show: visible, 'opens-up': opensUp }" :style="styleObj" @click.stop>
    <!-- 二级子面板：具体模型列表（窄屏为主面板左侧的独立方盒） -->
    <div ref="subPanelEl" class="cascader-sub-panel" :class="{ show: activeProviderName, 'narrow-side': narrowMode }" :style="subPanelStyle">
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

    <!-- 一级主面板：供应商列表（窄屏顶部内联当前供应商的模型，ZCode 式） -->
    <div class="cascader-main-panel" :style="mainPanelStyle">
      <template v-if="hasAnyModel">
        <template v-if="narrowMode && currentProviderName">
          <div class="cascader-provider-header">{{ currentProviderName }}</div>
          <div
            v-for="m in currentProviderModels"
            :key="'cur-' + (m.id || m.name)"
            class="cascader-item"
            :class="{ active: m.id === store.activeModelId }"
            @click="selectModel(m.id)"
          >
            <span class="c-label">{{ m.name }}</span>
            <span v-if="m.supports_vision" class="c-vision" title="支持多模态"><AppIcon name="eye" :size="12" /></span>
            <span v-if="m.id === store.activeModelId" class="c-check checked"><AppIcon name="check" :size="13" :stroke-width="2.25" /></span>
          </div>
          <div class="cascader-sep"></div>
        </template>
        <div
          v-for="(modelsInGroup, provider) in listedProviders"
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
    </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';
import AppIcon from './AppIcon.vue';
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
const mainPanelStyle = ref<Record<string, string>>({});
const subPanelStyle = ref<Record<string, string>>({});
const subPanelEl = ref<HTMLElement | null>(null);
const opensUp = ref(false);
// 窄屏（ZCode 式）：主面板窄版靠右、当前供应商模型内联，二级模型列表为
// 主面板左侧的独立方盒；打开菜单时判定一次
const narrowMode = ref(false);
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

// 移动端视口判定，与 Sidebar.vue 同一断点；matchMedia 不可用（jsdom）按桌面处理
function isNarrowViewport(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(max-width: 768px)').matches
  );
}

// 窄屏双列宽度：左方盒不超 200px（45% 视口），主面板吃掉剩余宽度；
// 10px 屏边距 x2 + 6px 列间距，两列恰好铺满且永不重叠
const NARROW_MARGIN = 10;
const NARROW_GAP = 6;
function narrowLayout(): { subW: number; mainW: number } {
  const vw = window.innerWidth;
  const subW = Math.min(200, Math.floor(vw * 0.45));
  const mainW = Math.min(220, vw - subW - NARROW_MARGIN * 2 - NARROW_GAP);
  return { subW, mainW };
}

// 当前供应商（按激活模型归属）：窄屏下其模型内联在主面板顶部
const currentProviderName = computed(() => {
  const current = store.models.find(m => m.id === store.activeModelId);
  return current?.provider_name || null;
});
const currentProviderModels = computed(() => {
  if (!currentProviderName.value) return [];
  return groupedModels.value[currentProviderName.value] || [];
});
// 窄屏供应商列表去掉当前供应商（其模型已在顶部内联，避免重复入口）
const listedProviders = computed(() => {
  if (!narrowMode.value || !currentProviderName.value) return groupedModels.value;
  const rest: Record<string, ModelInfo[]> = {};
  for (const [provider, models] of Object.entries(groupedModels.value)) {
    if (provider !== currentProviderName.value) rest[provider] = models;
  }
  return rest;
});

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

  // 窄屏：主面板左侧的独立方盒（ZCode 式），垂直方向与主面板同侧对齐
  if (narrowMode.value) {
    const { subW } = narrowLayout();
    const left = Math.max(NARROW_MARGIN, Math.floor(mainPanelRect.left - NARROW_GAP - subW));
    const maxH = Math.max(0, Math.floor(mainPanelRect.height));
    subPanelStyle.value = opensUp.value
      ? {
        position: 'fixed',
        top: 'auto',
        bottom: `${Math.floor(window.innerHeight - mainPanelRect.bottom)}px`,
        left: `${left}px`,
        right: 'auto',
        width: `${subW}px`,
        minWidth: '0',
        maxHeight: `${maxH}px`,
      }
      : {
        position: 'fixed',
        top: `${Math.floor(mainPanelRect.top)}px`,
        bottom: 'auto',
        left: `${left}px`,
        right: 'auto',
        width: `${subW}px`,
        minWidth: '0',
        maxHeight: `${maxH}px`,
      };
    return;
  }

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

  let left: number;
  if (narrowMode.value) {
    // 窄屏：主面板窄版靠右（ZCode 式，左侧留给二级方盒）
    const { mainW } = narrowLayout();
    left = Math.max(NARROW_MARGIN, Math.floor(window.innerWidth - NARROW_MARGIN - mainW));
    mainPanelStyle.value = { width: `${mainW}px`, minWidth: '0' };
  } else {
    left = Math.max(10, Math.min(r.left, window.innerWidth - 520));
    mainPanelStyle.value = {};
  }

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
    // 先定窄屏模式再定位：主面板宽度/靠右都依赖该标记
    narrowMode.value = isNarrowViewport();
    positionMainPanel();

    // 二级模型列表只在悬停/点选供应商后展开，保持一级列表简洁。
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
/* 窄屏左方盒与主面板列间距极小，显式抬高层级：两面板都有 backdrop-filter，
   Chromium 按合成层 DOM 顺序绘制（主面板在模板中靠后），会盖住 fixed 子面板 */
.cascader-sub-panel.narrow-side {
  z-index: 1;
}
/* 窄屏主面板顶部内联的当前供应商标题（ZCode 式） */
.cascader-provider-header {
  padding: 8px 12px 3px;
  font-size: 12px;
  color: var(--text-faint);
  user-select: none;
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
</style>
