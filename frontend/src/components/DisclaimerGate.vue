<template>
  <div class="gate-mask" role="dialog" aria-modal="true" aria-labelledby="disclaimer-gate-title">
    <div class="gate-card" data-test="disclaimer-gate">
      <header class="gate-head">
        <div class="gate-title-row">
          <h2 id="disclaimer-gate-title">免责条款</h2>
          <span class="gate-version mono">{{ DISCLAIMER_VERSION }}</span>
        </div>
        <p class="gate-meta">更新日期 {{ DISCLAIMER_UPDATED_AT }} · 适用于本软件全部发行版本</p>
      </header>

      <p class="gate-intro">
        使用本软件前，请阅读并理解以下条款。其中
        <b>涉及责任限制与免责的内容与你有重大利害关系</b>，已在下方显著标出，请特别注意。
      </p>

      <div class="gate-scroll" data-test="disclaimer-gate-scroll">
        <article
          v-for="(clause, i) in DISCLAIMER_CLAUSES"
          :key="i"
          class="gate-clause"
          :class="{ 'gate-clause--key': isKeyClause(clause.title) }"
        >
          <h3>
            {{ clause.title }}
            <span
              v-if="isKeyClause(clause.title)"
              data-test="disclaimer-key-tag"
              class="gate-key-tag"
            >重大利害条款</span>
          </h3>
          <ol>
            <li v-for="(item, j) in clause.items" :key="j">{{ item }}</li>
          </ol>
        </article>
      </div>

      <p v-if="error" data-test="disclaimer-error" class="gate-error">{{ error }}</p>

      <footer class="gate-actions">
        <button
          data-test="disclaimer-decline"
          class="gate-btn gate-btn--ghost"
          :disabled="submitting"
          @click="decline"
        >不同意，退出软件</button>
        <button
          data-test="disclaimer-accept"
          class="gate-btn gate-btn--primary"
          :disabled="submitting"
          @click="accept"
        >{{ submitting ? '处理中…' : '我已阅读并同意' }}</button>
      </footer>

      <p class="gate-note">
        不同意本条款将无法使用本软件。同意记录仅写入你的本地设备（~/.ycode），不会上传。
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import {
  DISCLAIMER_CLAUSES,
  DISCLAIMER_UPDATED_AT,
  DISCLAIMER_VERSION,
} from '../data/disclaimer';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();

const submitting = ref(false);
const error = ref('');

// 《民法典》第 496 条要求对「免除或减轻自身责任等与对方有重大利害关系的条款」
// 作显著提示。这两个章节正是免责/限责内容，必须单独强调——藏在正文里等于没提示。
const KEY_CLAUSE_TITLES = ['二、AI 生成内容', '七、责任限制'];
function isKeyClause(title: string): boolean {
  return KEY_CLAUSE_TITLES.includes(title);
}

function api(): any {
  return (window as any).pywebview?.api;
}

async function accept() {
  if (submitting.value) return;
  submitting.value = true;
  error.value = '';
  // 成功后 store.disclaimerAcceptedVersion 就地更新，父级 v-if 失效，
  // 本组件随之卸载——无需向上 emit 事件。
  error.value = await store.acceptDisclaimer();
  submitting.value = false;
}

async function decline() {
  if (submitting.value) return;
  submitting.value = true;
  const bridge = api();
  // 未接受条款则不提供服务：走完整退出序列（取消回合 → drain → 销毁窗口）
  try {
    if (bridge?.window_confirm_close) {
      await bridge.window_confirm_close('exit');
      return;
    }
  } catch {
    // 退出调用失败也要兜底关闭，不把用户困在弹窗里
  }
  window.close();
}
</script>

<style scoped>
.gate-mask {
  position: fixed;
  inset: 0;
  z-index: 6000;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(0, 0, 0, .72);
  backdrop-filter: blur(8px);
}
.gate-card {
  width: min(680px, calc(100vw - 40px));
  max-height: min(88vh, 860px);
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  padding: 24px 26px 18px;
  border: 1px solid var(--border);
  border-radius: 15px;
  background: var(--bg2);
  color: var(--text);
  box-shadow: 0 24px 80px rgba(0, 0, 0, .55);
}
.gate-head { display: flex; flex-direction: column; gap: 6px; }
.gate-title-row { display: flex; align-items: center; gap: 10px; }
.gate-title-row h2 { margin: 0; font-size: 20px; }
.gate-version { color: var(--text-dim); font-size: 12px; }
.gate-meta { margin: 0; color: var(--text-faint); font-size: 12px; }
.gate-intro { margin: 14px 0 12px; color: var(--text-dim); font-size: 13px; line-height: 1.7; }
.gate-intro b { color: var(--text); }
.gate-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg0);
}
.gate-clause + .gate-clause { padding-top: 12px; border-top: 1px solid var(--border); }
.gate-clause { padding-bottom: 12px; }
.gate-clause h3 { margin: 0 0 8px; font-size: 13.5px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.gate-clause ol { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 6px; }
.gate-clause li { color: var(--text-dim); font-size: 12.5px; line-height: 1.8; }
.gate-clause--key { border-left: 2px solid var(--danger); padding-left: 12px; }
.gate-clause--key h3 { color: var(--danger); }
.gate-key-tag {
  padding: 1px 7px;
  border: 1px solid var(--danger);
  border-radius: 999px;
  color: var(--danger);
  font-size: 11px;
  font-weight: 400;
}
.gate-error { margin: 10px 0 0; color: var(--danger); font-size: 12.5px; }
.gate-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; }
.gate-btn {
  min-height: 38px;
  padding: 0 18px;
  border: 1px solid var(--border);
  border-radius: 9px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
}
.gate-btn:disabled { opacity: .6; cursor: default; }
.gate-btn--ghost { background: var(--bg3); color: var(--text-dim); }
.gate-btn--primary { background: var(--text); color: var(--bg0); }
.gate-note { margin: 10px 0 0; color: var(--text-faint); font-size: 11.5px; line-height: 1.6; }
</style>
