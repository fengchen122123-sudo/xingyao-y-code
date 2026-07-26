<template>
  <div class="kimi-empty-container">
    <div class="kimi-hero">
      <div class="kimi-icon">✦</div>
      <div class="kimi-title-row">
        <span class="kimi-title">让 星瑶 来帮你完成任务</span>
        <span class="kimi-beta">Beta</span>
      </div>
    </div>

    <div class="kimi-card">
      <textarea 
        v-model="inputVal" 
        placeholder="尽管问，或做个任务..." 
        @keydown.enter.exact.prevent="handleSend"
      ></textarea>

      <div class="kimi-card-bar">
        <div style="display:flex;align-items:center;gap:8px">
          <button class="kimi-pill-btn" title="添加附件" @click="handlePickFiles">＋ 附件</button>
          <button
            ref="permBtnRef"
            class="kimi-pill-btn"
            title="切换权限模式"
            @click.stop="togglePermDropdown"
          >
            ☑ <span>{{ formatPermMode(store.permissionMode) }}</span>
            <span style="font-size:10px;color:var(--text-faint)">▼</span>
          </button>
        </div>

        <div style="display:flex;align-items:center;gap:10px">
          <button
            ref="modelBtnRef"
            class="kimi-pill-btn"
            title="切换模型"
            @click.stop="toggleModelCascader"
          >
            <span>{{ store.activeModelName || '选择模型' }}</span>
            <span style="font-size:10px;color:var(--text-faint)">▼</span>
          </button>

          <button
            class="kimi-send-btn"
            :class="{ active: inputVal.trim() && !store.running }"
            :disabled="store.running"
            title="发送消息 (Enter)"
            @click="handleSend"
          >↑</button>
        </div>
      </div>
    </div>

    <div class="kimi-footer-workdir" @click="store.openWorkspaceFolder()">
      📂 <span>{{ store.currentWorkdirLabel === 'GLOBAL' || store.currentWorkdirLabel === '对话' ? '进入项目工作区' : '当前工作区: ' + store.currentWorkdirLabel }}</span>
      <span style="font-size:10px;color:var(--text-faint)">▼</span>
    </div>

    <!-- 下拉弹出节点 -->
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
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
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

async function handlePickFiles() {
  const paths = await store.pickFiles();
  if (paths.length) {
    inputVal.value += paths.map(p => ` @${p}`).join('');
  }
}

function handleSend() {
  if (store.running) return;
  if (!inputVal.value.trim()) return;
  const text = inputVal.value;
  inputVal.value = '';
  store.sendMessage(text);
}
</script>
