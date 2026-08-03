<template>
  <div id="input-area">
    <div id="input-wrap">
      <div id="input-box">
        <textarea 
          v-model="inputVal" 
          rows="2" 
          placeholder="向星瑶提问… 输入 / 查看命令，@ 引用文件"
          @keydown.enter.exact.prevent="handleSend"
          @keydown.esc="store.stopCurrentTurn()"
        ></textarea>

        <div id="input-bar">
          <button class="chip-btn" title="附加文件" @click="handlePickFiles">附件</button>
          <button
            class="chip-btn"
            :class="{ active: store.showThinking }"
            title="显示/收起思考过程"
            @click="store.toggleShowThinking()"
          >思考</button>
          <button
            ref="permBtnRef"
            class="chip-btn"
            :class="{ active: store.permissionMode === 'plan' || store.permissionMode === 'full' }"
            title="切换权限模式"
            @click.stop="togglePermDropdown"
          >☑ {{ formatPermMode(store.permissionMode) }} <span style="font-size:9px;color:var(--text-faint)">▼</span></button>
          <button class="chip-btn" title="撤销星瑶上一次的文件修改" @click="store.undoLastEdit()">↩ 撤销</button>

          <button
            ref="modelBtnRef"
            class="chip-btn model-chip"
            title="切换模型"
            @click.stop="toggleModelCascader"
          >{{ store.activeModelName || '选择模型' }} <span style="font-size:9px;color:var(--text-faint)">▼</span></button>
          <button
            id="send-btn"
            :class="{ stop: store.running }"
            :title="store.running ? '停止生成 (Esc)' : '发送消息 (Enter)'"
            @click="handleSend"
          >
            <template v-if="store.running">■ 停止 (Esc)</template>
            <template v-else>发送 ⏎</template>
          </button>
        </div>
      </div>
    </div>

    <!-- 模型 / 权限下拉（与空状态页共用组件） -->
    <ModelCascader
      :visible="showModelCascader"
      :anchor-el="modelBtnRef"
      @close="showModelCascader = false"
    />
    <PermDropdown
      :visible="showPermDropdown"
      :anchor-el="permBtnRef"
      @close="showPermDropdown = false"
    />

    <div id="input-meta">
      <div class="l">
        <span :class="tokenClass">Token: {{ tokenUsedDisplay }} / {{ tokenWindowDisplay }}</span>
      </div>
      <div class="r">
        <span class="kbd">Enter</span> 发送 · <span class="kbd">Shift+Enter</span> 换行 · <span class="kbd">Esc</span> 中断
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue';
import { useAppStore } from '../stores/appStore';
import { formatPermMode } from '../utils/markdown';
import ModelCascader from './ModelCascader.vue';
import PermDropdown from './PermDropdown.vue';

const store = useAppStore();
const inputVal = ref('');

const showModelCascader = ref(false);
const showPermDropdown = ref(false);
const modelBtnRef = ref<HTMLElement | null>(null);
const permBtnRef = ref<HTMLElement | null>(null);

function toggleModelCascader() {
  showPermDropdown.value = false;
  showModelCascader.value = !showModelCascader.value;
}

function togglePermDropdown() {
  showModelCascader.value = false;
  showPermDropdown.value = !showPermDropdown.value;
}

const tokenUsedDisplay = computed(() => (store.tokenUsage.used || 0).toLocaleString('zh-CN'));
const tokenWindowDisplay = computed(() => (store.tokenUsage.window || 0).toLocaleString('zh-CN'));
const tokenPct = computed(() => {
  const w = store.tokenUsage.window || 0;
  if (!w) return 0;
  return (store.tokenUsage.used / w) * 100;
});
const tokenClass = computed(() => {
  if (tokenPct.value > 90) return 'tok-red';
  if (tokenPct.value > 70) return 'tok-orange';
  return '';
});

async function handlePickFiles() {
  const paths = await store.pickFiles();
  if (paths.length) {
    inputVal.value += paths.map(p => ` @${p}`).join('');
  }
}

function handleSend() {
  if (store.running) {
    store.stopCurrentTurn();
    return;
  }

  if (!inputVal.value.trim()) return;
  const text = inputVal.value;
  inputVal.value = '';
  store.sendMessage(text);
}
</script>

<style scoped>
#input-area {
  border-top: 1px solid var(--border);
  background: var(--bg1);
  padding: 12px 28px 10px;
  flex-shrink: 0;
}
#input-wrap {
  max-width: 880px;
  margin: 0 auto;
}
#input-box {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px;
  transition: border-color 0.15s, box-shadow 0.15s;
}
#input-box:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
textarea {
  width: 100%;
  background: none;
  border: none;
  outline: none;
  color: var(--text);
  font-family: var(--sans);
  font-size: 13.5px;
  resize: none;
  line-height: 1.6;
}
#input-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px solid var(--border);
}
.chip-btn {
  background: var(--bg3);
  border: 1px solid var(--border);
  color: var(--text-dim);
  border-radius: 7px;
  padding: 4px 10px;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 5px;
  cursor: pointer;
  transition: 0.15s;
}
.chip-btn:hover {
  border-color: var(--text-faint);
  color: var(--text);
}
.chip-btn.active {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-soft);
}

.model-chip {
  margin-left: auto;
  max-width: 180px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: inline-block;
}

#send-btn {
  border-radius: 8px;
  padding: 5px 14px;
  font-size: 12.5px;
  font-weight: 600;
  border: none;
  background: var(--accent);
  color: #fff;
  cursor: pointer;
  transition: 0.15s;
}
#send-btn:hover {
  filter: brightness(1.1);
}
#send-btn.stop {
  background: var(--red);
}

#input-meta {
  max-width: 880px;
  margin: 6px auto 0;
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--text-faint);
}
.kbd {
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 10px;
}
.tok-orange {
  color: var(--orange);
}
.tok-red {
  color: var(--red);
}
</style>
