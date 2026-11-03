<template>
  <!-- 图片节点（视觉稿 .node[data-node=cat]）：IMG 徽章 + 标题 + 尺寸，
       中部 178px 图区（改图中盖遮罩），底部「时间 + 改图 ▾」（点开就地改图
       浮层），右侧输出端口 -->
  <div class="cv-node" :class="{ 'is-selected': selected }" data-test="canvas-node-image">
    <div class="cv-head">
      <span class="cv-kind cv-kind--img">IMG</span>
      <span class="cv-title" :title="title">{{ title }}</span>
      <span v-if="sizeText" class="cv-size">{{ sizeText }}</span>
    </div>

    <div class="cv-thumb">
      <!-- 有 url 才画 img：本项不做 URL 解析，空值直接走占位，绝不出现破图图标 -->
      <img v-if="node.url" class="cv-art" :src="node.url" :alt="title" />
      <div v-else class="cv-ph" data-test="canvas-image-placeholder">
        <svg class="cv-ph-ico" viewBox="0 0 24 24" aria-hidden="true">
          <rect x="3.5" y="5" width="17" height="14" rx="2" />
          <circle cx="9" cy="10" r="1.4" />
          <path d="M4.5 17l4.6-4.4 3.4 3.2 2.6-2.3 4.4 3.9" />
        </svg>
        <span class="cv-ph-name">{{ placeholderName }}</span>
        <span class="cv-ph-kind">image</span>
      </div>
      <div class="cv-grain"></div>
      <!-- 左下角模型名徽章 -->
      <span v-if="modelText" class="cv-badge">{{ modelText }}</span>
      <!-- 改图中：盖在图区上的半透明遮罩。为什么不只在按钮上禁用：
           就地改图要跑几十秒，用户盯的就是图区，遮罩是最直接的「在动」信号 -->
      <div v-if="editPending" class="cv-mask" data-test="canvas-edit-mask">
        <span>改图中…</span>
      </div>
    </div>

    <div class="cv-foot">
      <!-- 溯源优先于时间：派生节点（改图产物）最要紧的信息是「我从哪来」，
          两段等宽文字挤在 264px 里必然断行，所以有来源时就把时间位让出来 -->
      <span v-if="sourceText" class="cv-from cv-origin" :title="sourceTitle">
        改自 {{ sourceText }}
      </span>
      <span v-else-if="timeText" class="cv-from">{{ timeText }}</span>
      <span class="cv-spacer"></span>
      <!-- nodrag：按钮落在可拖拽节点里，不标 nodrag 会被当成拖拽起点 -->
      <button
        ref="triggerRef"
        class="cv-act nodrag"
        data-test="canvas-edit-trigger"
        :disabled="editPending"
        :aria-expanded="panelOpen"
        :title="editPending ? '这张图正在改，等它回来' : '写一句话就地改图'"
        @click.stop="togglePanel"
      >
        改图 ▾
      </button>
    </div>

    <!-- 就地改图浮层：贴在节点底部上方（底边抬到 foot 之上），左右各留 12px，
         264px 定宽下正好 240px，永远不会把面板撑出节点外被画布裁掉。
         nodrag nopan 一起标：拖拽与平移都要放过这块，否则点输入框会拖动节点 -->
    <div
      v-if="panelOpen"
      ref="panelRef"
      class="cv-edit-pop nodrag nopan"
      data-test="canvas-edit-panel"
      @click.stop
      @dblclick.stop
      @pointerdown.stop
    >
      <textarea
        v-model="promptText"
        class="cv-edit-input nodrag nopan"
        data-test="canvas-edit-prompt"
        rows="2"
        placeholder="把月亮换成一轮落日"
      ></textarea>
      <div class="cv-edit-row">
        <!-- 画笔标注：打开居中标注浮层（底图上画圈 + 一句指令），
             走的是同一个后端改图入口，只是多带一层透明标注 PNG -->
        <button class="cv-act" data-test="canvas-annotate" @click.stop="onAnnotate">
          画笔标注
        </button>
        <span class="cv-edit-spacer"></span>
        <!-- 价格常显：上游按张计费，每点一次「生成」就是一次真实开销 -->
        <span class="cv-edit-price">0.20 元/张</span>
        <button
          class="cv-act cv-act--primary"
          data-test="canvas-edit-generate"
          :disabled="editPending"
          @click.stop="onGenerate"
        >
          生成
        </button>
      </div>
      <!-- 提示放在浮层内部而不是全局 tip：空指令是最常见的误点，就地提示不打断 -->
      <p v-if="promptHint" class="cv-edit-hint" data-test="canvas-edit-hint">{{ promptHint }}</p>
      <!-- 以这张图为首帧生成视频：复用当前指令作为运动描述。计费口径
           （720P/5秒约¥3 + 本地图换公网地址¥0.2）写在 title 里常显给用户 -->
      <button
        class="cv-edit-link"
        data-test="canvas-generate-video"
        title="以这张图为首帧生成 5 秒视频（720P，约 ¥3/次；本地图换公网地址另计 ¥0.2）"
        @click.stop="onGenerateVideo"
      >
        以此图为首帧生成视频 →
      </button>
      <!-- P2 角色库入口：把这张图存成某个角色的参考（弹窗选角色/类型/主参考） -->
      <button
        class="cv-edit-link"
        data-test="canvas-save-character-ref"
        @click.stop="onSaveCharacterRef"
      >
        存为角色参考 →
      </button>
      <!-- 绑定过角色才出现：用该角色的主参考作垫图、档案拼装 prompt 出定妆图 -->
      <button
        v-if="boundCharacterCount === 1"
        class="cv-edit-link"
        data-test="canvas-generate-character-image"
        title="用该角色的主参考图作垫图，自动拼入固定外貌句与画风（0.20 元/张）"
        @click.stop="onGenerateCharacterImage"
      >
        用角色出定妆图 →
      </button>
      <!-- 绑定角色状态提示：单角色 = 注入预告；多角色 = 硬限制明示（多人同框
           没有稳定方案，不假装能一键稳定双人，生成视频会被拒绝） -->
      <p
        v-if="boundCharacterCount === 1"
        class="cv-edit-hint"
        data-test="canvas-character-bound-hint"
      >
        已绑定角色：生成视频 / 定妆图会自动带入该角色的外貌与画风
      </p>
      <p
        v-if="boundCharacterCount > 1"
        class="cv-edit-hint is-warn"
        data-test="canvas-character-multi-hint"
      >
        这张图绑定了 {{ boundCharacterCount }} 个角色：多人同框暂不支持一键稳定生成，
        以它为首帧生成视频会被拒绝
      </p>
      <!-- 次要链接：需要模型先读图、先想 prompt 的场景仍然走对话，行为与原来一字不改 -->
      <button
        class="cv-edit-link"
        data-test="canvas-send-to-agent"
        @click.stop="onSendToAgent"
      >
        送去对话修改
      </button>
    </div>

    <!-- 左侧输入端口：改图/参考这类关系从左进。缺了它图片节点只能是链条起点，
         手动也接不上「谁改的它」 -->
    <Handle id="in" type="target" :position="Position.Left" :connectable="connectable" />
    <!-- 右侧输出端口：图片作为下游节点的输入 -->
    <Handle id="out" type="source" :position="Position.Right" :connectable="connectable" />
  </div>
</template>

<script setup lang="ts">
// 图片节点：只负责画，不改数据。选中态完全由 vue-flow 传入的 selected 决定，
// 「改图 / 送去修改」都只发事件，真正的后续动作（调桥、落节点）由父级
// （CanvasPane）决定——节点自己不碰 store，也不自己发请求。
import { computed, onBeforeUnmount, ref } from 'vue';
import { Handle, Position, type NodeProps } from '@vue-flow/core';
import {
  fileBaseName,
  metaNumber,
  metaString,
  nodeTimeText,
  nodeTitle,
  type CanvasNodeData,
  type NodeActionPayload,
} from './types';

const props = withDefaults(
  defineProps<{
    id: string;
    selected?: boolean;
    connectable?: boolean;
    data: CanvasNodeData;
    /**
     * 是否正在就地改图。由 CanvasPane 从 store.pendingEdits 映射后经 bridgeNode
     * 透传进来：节点只画遮罩与禁用按钮，不关心任务号从哪来。
     */
    editPending?: boolean;
  }>(),
  { selected: false, connectable: undefined, editPending: false },
);

// 官方 NodeProps 形状在此做一次编译期对齐（字段名/类型写错会直接报错）。
// 不能写成 defineProps<NodeProps<CanvasNodeData>>()：@vue/compiler-sfc 解析不了
// 外部泛型的实参，会抛 Unresolvable type reference，props 退化成空声明、
// selected 恒为 undefined，选中态就失效了。
void (props as unknown as Pick<NodeProps<CanvasNodeData>, 'id' | 'selected' | 'connectable' | 'data'>);

const emit = defineEmits<{
  // 旧链路：把图发进对话（行为不变）
  (e: 'send-to-agent', payload: NodeActionPayload): void;
  // 就地改图：父级直接调 canvas_edit_image，不经过对话
  (e: 'edit-inline', payload: NodeActionPayload): void;
  // 画笔标注：父级打开居中标注浮层（底图只显示，笔迹画在透明 canvas 层上）
  (e: 'annotate', payload: NodeActionPayload): void;
  // 生成视频：父级调 canvas_generate_video（以这张图为首帧）
  (e: 'generate-video', payload: NodeActionPayload): void;
  // P2 存为角色参考：父级打开 CharacterRefDialog（选角色/参考类型/主参考）
  (e: 'save-character-ref', payload: NodeActionPayload): void;
  // P2 用角色出定妆图：父级调 canvas_character_generate_image（主参考垫图）
  (e: 'generate-character-image', payload: NodeActionPayload): void;
}>();

/** 浮层开合与指令都和这一个节点绑死，用局部 ref 就够，不引入任何全局状态 */
const panelOpen = ref(false);
const promptText = ref('');
const promptHint = ref('');
const triggerRef = ref<HTMLButtonElement | null>(null);
const panelRef = ref<HTMLElement | null>(null);

const node = computed(() => props.data.node);
const title = computed(() => nodeTitle(node.value, '未命名图片'));

/** 头部右侧尺寸：优先 meta.size 文案，其次由 meta 里的宽高拼出来 */
const sizeText = computed(() => {
  const text = metaString(node.value, 'size');
  if (text) return text;
  const width = metaNumber(node.value, 'width', 0);
  const height = metaNumber(node.value, 'height', 0);
  return width > 0 && height > 0 ? `${width}×${height}` : '';
});

/** 图区左下角徽章：出图模型名 */
const modelText = computed(() => metaString(node.value, 'model'));

/** 占位文案：文件名优先，没有就回落到标题 */
const placeholderName = computed(
  () => fileBaseName(node.value.file) || title.value || '未生成图片',
);

const timeText = computed(() => nodeTimeText(node.value));

/**
 * 溯源文案：改图产物由画布层在落节点时写入 meta.source_file / source_title。
 * 只读展示，不参与任何查找——真正的溯源关系是节点之间那条「改图」边。
 */
const sourceTitle = computed(
  () => metaString(node.value, 'source_title') || metaString(node.value, 'source_file'),
);
const sourceText = computed(
  () =>
    fileBaseName(metaString(node.value, 'source_file')) || metaString(node.value, 'source_title'),
);

/**
 * 点节点/画布别处收起浮层。
 * 用 capture 阶段监听 document：浮层内部的 click 自己 stop 了，但 capture
 * 先于目标触发，所以这里还得显式放过浮层与触发器内部的指针事件——
 * 否则「点触发器」会先被关一次、再被 click 打开，看起来像点不动。
 */
function onDocPointerDown(event: PointerEvent): void {
  const target = event.target as Node | null;
  if (!target) return;
  if (panelRef.value?.contains(target)) return;
  if (triggerRef.value?.contains(target)) return;
  setPanel(false);
}

function setPanel(open: boolean): void {
  if (panelOpen.value === open) return;
  panelOpen.value = open;
  promptHint.value = '';
  if (open) document.addEventListener('pointerdown', onDocPointerDown, true);
  else document.removeEventListener('pointerdown', onDocPointerDown, true);
}

function togglePanel(): void {
  // pending 期间不允许再开浮层：后端对同一节点也有闸门，前端先挡一层
  if (props.editPending) return;
  setPanel(!panelOpen.value);
}

/**
 * 生成：只发事件，不自己调桥。
 * 空指令在这里就地拦下——空 prompt 后端也会拒，但一次往返才告诉用户
 * 「你没写要求」体验很差，浮层内联提示足够。
 */
function onGenerate(): void {
  if (props.editPending) return;
  const prompt = promptText.value.trim();
  if (!prompt) {
    promptHint.value = '先写一句要改什么，例如「把月亮换成一轮落日」';
    return;
  }
  emit('edit-inline', { id: props.id, action: 'edit-inline', prompt });
  // 不在这里清空 promptText：请求可能失败（插件未配 Key 等），用户改一改就能重发，
  // 清掉等于让他重敲一遍
  setPanel(false);
}

/** 画笔标注：把已经写好的那句指令一并带出去，用户在标注浮层里不必重敲一遍 */
function onAnnotate(): void {
  // prompt 原样透传（可能是空串）：父级用它预填标注浮层的输入框，是否为空由那边判
  emit('annotate', { id: props.id, action: 'annotate', prompt: promptText.value.trim() });
  setPanel(false);
}

/** 送去对话修改：与原来的「送去修改」同一条链路，载荷保持 action='edit' */
function onSendToAgent(): void {
  emit('send-to-agent', { id: props.id, action: 'edit' });
  setPanel(false);
}

/**
 * 以这张图为首帧生成视频：复用改图浮层里写好的指令当运动描述。
 * 只往父级抛事件，节点不碰桥、不碰 store（单一职责，与改图同构）。
 * 视频按秒计费且要跑 1~5 分钟，所以空指令在就地拦下，别让任务白排队。
 */
function onGenerateVideo(): void {
  const prompt = promptText.value.trim();
  if (!prompt) {
    promptHint.value = '先写一句画面运动指令，例如「镜头缓缓推近，人物转头微笑」';
    return;
  }
  emit('generate-video', { id: props.id, action: 'generate-video', prompt });
  setPanel(false);
}

/**
 * 节点绑定的角色（meta.character_ids）：「存为角色参考」成功后由画布层写回。
 * 这里只读不写——节点不知道角色库，只根据绑定个数决定露出哪些入口与提示。
 */
const boundCharacterCount = computed(() => {
  const value = node.value.meta?.character_ids;
  return Array.isArray(value) ? value.length : 0;
});

/** 存为角色参考：只抛事件，弹窗（选角色/参考类型/主参考）由父级画布打开 */
function onSaveCharacterRef(): void {
  emit('save-character-ref', { id: props.id, action: 'save-character-ref' });
  setPanel(false);
}

/** 用角色出定妆图：复用当前指令作镜头描述（如「正面半身定妆照」），父级拼装 */
function onGenerateCharacterImage(): void {
  const prompt = promptText.value.trim();
  if (!prompt) {
    promptHint.value = '先写一句画面描述，例如「正面半身定妆照，浅笑」';
    return;
  }
  emit('generate-character-image', {
    id: props.id,
    action: 'generate-character-image',
    prompt,
  });
  setPanel(false);
}

// 组件被移除（节点删了/画布销毁）时要摘掉 document 监听，否则 ref 已空的
// 处理器会一直挂在 document 上
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onDocPointerDown, true);
});
</script>

<style scoped src="./node-shell.css"></style>
