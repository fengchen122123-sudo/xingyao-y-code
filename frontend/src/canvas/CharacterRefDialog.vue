<template>
  <!-- 存为角色参考：居中浮层。与画笔标注同一套遮罩语言，同样遮罩点击不关闭
       （用户可能填了半张角色表，误点空白就全丢）。只能点「取消」或 × 退出 -->
  <div class="cv-char-mask" data-test="character-ref-dialog" @click.stop @pointerdown.stop>
    <div class="cv-char-card" @click.stop @pointerdown.stop>
      <div class="cv-char-head">
        <span class="cv-char-title">存为角色参考</span>
        <span class="cv-char-sub" :title="nodeTitle">{{ nodeTitle }}</span>
        <span class="cv-char-spacer"></span>
        <button
          class="cv-char-x"
          data-test="character-ref-close"
          title="取消"
          @click.stop="emit('close')"
        >×</button>
      </div>

      <!-- 多角色硬限制明示：不假装能一键稳定双人，入口就把话说清 -->
      <p class="cv-char-limit" data-test="character-ref-limit">
        同一镜头暂只支持带入一个角色；多人同框请分镜头单独生成，或先合成同框图作首帧。
      </p>

      <div v-if="loading" class="cv-char-empty" data-test="character-ref-loading">角色库载入中…</div>
      <div v-else-if="loadError" class="cv-char-empty" data-test="character-ref-error">
        {{ loadError }}
      </div>
      <template v-else>
        <!-- 选中已有角色 / 新建角色：两档切换 -->
        <div class="cv-char-mode" data-test="character-ref-mode">
          <button
            class="cv-char-btn"
            :class="{ 'is-on': mode === 'pick' }"
            :disabled="!characters.length"
            @click.stop="mode = 'pick'"
          >选角色{{ characters.length ? `（${characters.length}）` : '' }}</button>
          <button
            class="cv-char-btn"
            :class="{ 'is-on': mode === 'create' }"
            @click.stop="mode = 'create'"
          >新建角色</button>
        </div>

        <!-- 选已有角色：单选列表（radio 语义，点整行切换） -->
        <div v-if="mode === 'pick'" class="cv-char-list" data-test="character-ref-list">
          <label
            v-for="item in characters"
            :key="item.id"
            class="cv-char-row"
            :class="{ 'is-on': selectedId === item.id }"
          >
            <input
              v-model="selectedId"
              type="radio"
              name="character-ref-pick"
              :value="item.id"
            />
            <span class="cv-char-name">{{ item.name }}</span>
            <span class="cv-char-refs">{{ refCountText(item) }}</span>
          </label>
        </div>

        <!-- 新建角色：名字必填，外貌句留空则回落描述 -->
        <div v-else class="cv-char-form" data-test="character-ref-create-form">
          <input
            v-model="newName"
            class="cv-char-input"
            data-test="character-ref-new-name"
            placeholder="角色名（必填，例如「小满」）"
          />
          <input
            v-model="newFragment"
            class="cv-char-input"
            data-test="character-ref-new-fragment"
            placeholder="固定外貌句（例如「黑色齐肩短发，琥珀色眼睛，左脸颊有小痣」）"
          />
          <input
            v-model="newStyle"
            class="cv-char-input"
            data-test="character-ref-new-style"
            placeholder="画风约束（可选，例如「写实摄影, 柔光」）"
          />
          <input
            v-model="newSeed"
            class="cv-char-input"
            data-test="character-ref-new-seed"
            placeholder="随机种子（可选，整数）"
          />
        </div>

        <div class="cv-char-typeline">
          <span class="cv-char-typelabel">参考类型</span>
          <button
            v-for="item in REF_TYPES"
            :key="item.value"
            class="cv-char-btn"
            :class="{ 'is-on': refType === item.value }"
            :data-test="`character-ref-type-${item.value}`"
            @click.stop="refType = item.value"
          >{{ item.label }}</button>
          <span class="cv-char-spacer"></span>
          <label class="cv-char-check" data-test="character-ref-primary-wrap">
            <input
              v-model="isPrimary"
              type="checkbox"
              data-test="character-ref-primary"
            />
            主参考（作垫图/首帧基准，每角色仅一张）
          </label>
        </div>

        <p v-if="primaryReplaceHint" class="cv-char-hint" data-test="character-ref-replace-hint">
          {{ primaryReplaceHint }}
        </p>
        <p v-if="error" class="cv-char-hint is-err" data-test="character-ref-error-line">{{ error }}</p>

        <div class="cv-char-foot">
          <span class="cv-char-price">存参考不花钱（生成时才计费）</span>
          <span class="cv-char-spacer"></span>
          <button class="cv-char-btn" data-test="character-ref-cancel" @click.stop="emit('close')">取消</button>
          <button
            class="cv-char-btn is-primary"
            data-test="character-ref-submit"
            :disabled="submitting || !canSubmit"
            @click.stop="onSubmit"
          >{{ submitting ? '保存中…' : '存为角色参考' }}</button>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
// 存为角色参考的弹窗：把画布上的一张图挂到角色档案（front/side/outfit/style，
// 可勾主参考）。桥交互都收在组件内（列角色/建角色/存参考），成功只向父级抛
// 一个 saved 事件——父级（CanvasPane）只负责把 character_ids 写回节点 meta
// 和弹提示，不掺和桥协议。
import { computed, onMounted, ref, watch } from 'vue';

/** 桥层角色档案的形状（canvas_character_list 返回项的字段子集） */
interface CharacterItem {
  id: number;
  name: string;
  prompt_fragment?: string;
  style_tags?: string;
  refs?: Array<{ ref_type?: string; is_primary?: boolean }>;
}

const props = defineProps<{
  canvasId: string;
  nodeFile: string;
  nodeId: string;
  nodeTitle: string;
}>();

const emit = defineEmits<{
  (e: 'close'): void;
  (
    e: 'saved',
    payload: { characterId: number; replacedPrimary: boolean; refType: string },
  ): void;
}>();

const REF_TYPES = [
  { value: 'front', label: '正面' },
  { value: 'side', label: '侧面' },
  { value: 'outfit', label: '服装' },
  { value: 'style', label: '风格' },
] as const;

const loading = ref(true);
const loadError = ref('');
const characters = ref<CharacterItem[]>([]);
const mode = ref<'pick' | 'create'>('pick');
const selectedId = ref<number | null>(null);
const newName = ref('');
const newFragment = ref('');
const newStyle = ref('');
const newSeed = ref('');
const refType = ref<string>('front');
const isPrimary = ref(true);
const submitting = ref(false);
const error = ref('');

type BridgeApi = Record<string, unknown> | undefined;
function bridge(): BridgeApi {
  return (window as unknown as { pywebview?: { api?: BridgeApi } }).pywebview?.api;
}

/** 主参考替换预告：选中/待建角色已有主参考时提前说一声，不让用户事后再惊讶 */
const primaryReplaceHint = computed<string>(() => {
  if (!isPrimary.value) return '';
  if (mode.value === 'create') return '';
  const target = characters.value.find((c) => c.id === selectedId.value);
  if (!target) return '';
  const hasPrimary = (target.refs ?? []).some((r) => r.is_primary);
  return hasPrimary ? `「${target.name}」已有主参考，保存将替换它` : '';
});

const canSubmit = computed(() => {
  if (mode.value === 'create') return newName.value.trim() !== '';
  return selectedId.value !== null;
});

function refCountText(item: CharacterItem): string {
  const refs = item.refs ?? [];
  const primary = refs.some((r) => r.is_primary);
  return `${refs.length} 张参考${primary ? ' · 有主参考' : ''}`;
}

onMounted(async () => {
  const api = bridge();
  const list = api?.canvas_character_list;
  if (typeof list !== 'function') {
    loading.value = false;
    loadError.value = '后端桥未就绪，无法读取角色库（请在桌面应用内重试）';
    mode.value = 'create';
    return;
  }
  try {
    const res = (await (list as () => Promise<unknown>)()) as {
      ok?: boolean;
      msg?: string;
      characters?: CharacterItem[];
    };
    if (!res?.ok || !Array.isArray(res.characters)) {
      loadError.value = String(res?.msg || '角色库读取失败');
      mode.value = 'create';
    } else {
      characters.value = res.characters;
      if (characters.value.length) {
        selectedId.value = characters.value[0].id;
      } else {
        mode.value = 'create';
      }
    }
  } catch (err) {
    loadError.value = `角色库读取失败：${err instanceof Error ? err.message : String(err)}`;
    mode.value = 'create';
  } finally {
    loading.value = false;
  }
});

// 切档/换角色时清掉上一档的错误文案；主参考替换与否交给上面的预告提示
watch([mode, selectedId], () => {
  error.value = '';
});

async function onSubmit(): Promise<void> {
  if (submitting.value || !canSubmit.value) return;
  const api = bridge();
  const create = api?.canvas_character_create;
  const addRef = api?.canvas_character_add_ref;
  if (typeof addRef !== 'function') {
    error.value = '后端桥未就绪，无法保存（请在桌面应用内重试）';
    return;
  }
  submitting.value = true;
  error.value = '';
  try {
    let characterId = selectedId.value;
    if (mode.value === 'create') {
      if (typeof create !== 'function') {
        error.value = '后端桥未就绪，无法新建角色';
        return;
      }
      const seedRaw = newSeed.value.trim();
      const seed = seedRaw === '' ? null : Number(seedRaw);
      if (seed !== null && (!Number.isFinite(seed) || !Number.isInteger(seed))) {
        error.value = '随机种子必须是整数（或留空）';
        return;
      }
      const res = (await (
        create as (
          name: string,
          description: string,
          fragment: string,
          style: string,
          seed: number | null,
        ) => Promise<unknown>
      )(newName.value.trim(), '', newFragment.value.trim(), newStyle.value.trim(), seed)) as {
        ok?: boolean;
        msg?: string;
        character?: { id?: number };
      };
      if (!res?.ok || !res.character?.id) {
        error.value = String(res?.msg || '新建角色失败');
        return;
      }
      characterId = Number(res.character.id);
    }
    const res = (await (
      addRef as (
        canvasId: string,
        nodeFile: string,
        characterId: number,
        refType: string,
        isPrimary: boolean,
        sourceNodeId: string,
      ) => Promise<unknown>
    )(
      props.canvasId,
      props.nodeFile,
      Number(characterId),
      refType.value,
      isPrimary.value,
      props.nodeId,
    )) as { ok?: boolean; msg?: string; replaced_primary?: boolean };
    if (!res?.ok) {
      error.value = String(res?.msg || '存为角色参考失败');
      return;
    }
    emit('saved', {
      characterId: Number(characterId),
      replacedPrimary: res.replaced_primary === true,
      refType: refType.value,
    });
    emit('close');
  } catch (err) {
    error.value = `保存失败：${err instanceof Error ? err.message : String(err)}`;
  } finally {
    submitting.value = false;
  }
}
</script>

<style scoped>
/* 与 AnnotationDialog 同一套浮层语言：全屏半透明遮罩 + 居中卡片 */
.cv-char-mask {
  position: absolute;
  inset: 0;
  z-index: 30;
  display: flex;
  align-items: center;
  justify-content: center;
  background: color-mix(in srgb, var(--bg0) 55%, transparent);
  backdrop-filter: blur(3px);
}
.cv-char-card {
  width: min(420px, calc(100% - 32px));
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg1);
  box-shadow: 0 16px 36px color-mix(in srgb, var(--border) 55%, transparent);
}
.cv-char-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.cv-char-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.cv-char-sub {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-faint);
  font-size: 11px;
}
.cv-char-spacer { flex: 1; }
.cv-char-x {
  width: 22px;
  height: 22px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--text-faint);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
}
.cv-char-x:hover { border-color: var(--border); background: var(--bg3); color: var(--text); }

/* 硬限制明示：放在最上面而不是藏在帮助里——用户在把图挂给第二个角色前就该知道 */
.cv-char-limit {
  margin: 0 0 10px;
  padding: 6px 9px;
  border: 1px solid color-mix(in srgb, var(--accent) 30%, transparent);
  border-radius: 8px;
  background: var(--accent-soft);
  color: var(--text-dim);
  font-size: 11.5px;
  line-height: 1.5;
}
.cv-char-empty {
  padding: 18px 0;
  color: var(--text-faint);
  font-size: 12px;
  text-align: center;
}
.cv-char-mode {
  display: flex;
  gap: 6px;
  margin-bottom: 8px;
}
.cv-char-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 168px;
  overflow-y: auto;
  margin-bottom: 8px;
  padding: 4px;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.cv-char-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 8px;
  border-radius: 6px;
  cursor: pointer;
}
.cv-char-row:hover { background: var(--bg2); }
.cv-char-row.is-on { background: var(--accent-soft); }
.cv-char-row input { accent-color: var(--accent); }
.cv-char-name { color: var(--text); font-size: 12px; }
.cv-char-refs { margin-left: auto; color: var(--text-faint); font-size: 10.5px; }

.cv-char-form { display: flex; flex-direction: column; gap: 6px; margin-bottom: 8px; }
.cv-char-input {
  padding: 6px 9px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--bg2);
  color: var(--text);
  font-family: inherit;
  font-size: 12px;
}
.cv-char-input:focus { outline: none; border-color: var(--accent); }

.cv-char-typeline { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.cv-char-typelabel { color: var(--text-faint); font-size: 11px; }
.cv-char-btn {
  padding: 4px 9px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg3);
  color: var(--text-dim);
  font-family: inherit;
  font-size: 11.5px;
  cursor: pointer;
  transition: 0.15s;
}
.cv-char-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.cv-char-btn.is-on {
  border-color: color-mix(in srgb, var(--accent) 55%, transparent);
  background: var(--accent-soft);
  color: var(--accent);
}
.cv-char-btn.is-primary {
  border-color: color-mix(in srgb, var(--accent) 55%, transparent);
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}
.cv-char-btn:disabled { opacity: 0.55; cursor: not-allowed; }
.cv-char-check {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--text-dim);
  font-size: 11.5px;
  cursor: pointer;
}
.cv-char-check input { accent-color: var(--accent); }

.cv-char-hint {
  margin: 8px 0 0;
  color: var(--text-faint);
  font-size: 11px;
}
.cv-char-hint.is-err { color: #e06c5f; }
.cv-char-foot {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 12px;
}
.cv-char-price {
  color: var(--text-faint);
  font-family: var(--mono);
  font-size: 10.5px;
}
</style>
