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
          <button class="chip-btn" title="附加文件" @click="store.showToast('附件加载功能')">📎 文件</button>
          <button class="chip-btn" title="深度思考" @click="store.showToast('深度思考已开启')">🧠 思考</button>
          <button class="chip-btn" title="计划模式" @click="store.setPermissionMode('计划模式', 'plan')">📋 计划</button>
          <button class="chip-btn" title="斜杠命令" @click="store.showToast('常用斜杠命令')">/ 命令</button>
          
          <button 
            id="send-btn" 
            :class="{ stop: store.running }"
            @click="handleSend"
          >
            <template v-if="store.running">■ 停止 (Esc)</template>
            <template v-else>发送 ⏎</template>
          </button>
        </div>
      </div>
    </div>

    <div id="input-meta">
      <div class="l">
        <span>Token: 0 / 128,000</span>
      </div>
      <div class="r">
        <span class="kbd">Enter</span> 发送 · <span class="kbd">Shift+Enter</span> 换行 · <span class="kbd">Esc</span> 中断
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();
const inputVal = ref('');

function handleSend() {
  if (store.running && !inputVal.value.trim()) {
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
  border-radius: 12px;
  padding: 10px;
  transition: 0.15s;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
}
#input-box:focus-within {
  border-color: var(--accent);
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
  border-color: var(--accent);
  color: var(--text);
}

#send-btn {
  margin-left: auto;
  border-radius: 8px;
  padding: 5px 14px;
  font-size: 12.5px;
  font-weight: 600;
  border: none;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  color: #fff;
  cursor: pointer;
  transition: 0.15s;
}
#send-btn:hover {
  opacity: 0.9;
  transform: translateY(-1px);
}
#send-btn.stop {
  background: linear-gradient(135deg, var(--red), #c0392b);
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
</style>
