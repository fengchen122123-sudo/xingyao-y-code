import { ref, watch } from 'vue';

const STORAGE_KEY = 'ycode.chatLayoutWidth';

export type ChatLayoutWidthMode = 'wide' | 'full' | 'compact';

export const CHAT_LAYOUT_WIDTH_OPTIONS: ReadonlyArray<{
  id: ChatLayoutWidthMode;
  label: string;
  desc: string;
}> = [
  { id: 'wide', label: '宽屏分栏（推荐）', desc: '对话保持 960–1060px 舒适栏宽，最大化时联动展开右侧工作台' },
  { id: 'full', label: '单栏铺满', desc: '关闭自动分栏，对话与输入栏尽可能利用横向空间' },
  { id: 'compact', label: '经典居中', desc: '固定 820px 窄栏居中阅读，不自动展开右侧工作台' },
];

export const chatLayoutWidth = ref<ChatLayoutWidthMode>('wide');

function normalizeMode(value: unknown): ChatLayoutWidthMode {
  if (value === 'wide' || value === 'full' || value === 'compact') return value;
  return 'wide';
}

function apply() {
  document.documentElement.dataset.chatWidth = chatLayoutWidth.value;
}

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, chatLayoutWidth.value);
  } catch {
  }
}

/** 应用启动时调用一次，避免首帧闪动。 */
export function loadChatLayoutWidth() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    chatLayoutWidth.value = normalizeMode(raw);
  } catch {
    chatLayoutWidth.value = 'wide';
  }
  apply();
}

export function setChatLayoutWidth(mode: ChatLayoutWidthMode) {
  chatLayoutWidth.value = normalizeMode(mode);
}

watch(chatLayoutWidth, () => {
  apply();
  persist();
});
