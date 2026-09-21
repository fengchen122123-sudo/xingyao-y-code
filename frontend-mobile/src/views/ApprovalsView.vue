<template>
  <div class="page">
    <van-nav-bar title="审批" fixed placeholder safe-area-inset-top>
      <template #left>
        <img class="nvlogo" :src="logoUrl" alt="星瑶" />
      </template>
      <template #right>
        <span class="cnt mono">{{ store.permRequests.length }} 项待处理</span>
      </template>
    </van-nav-bar>
    <div class="body">
      <div class="conn mono" :class="{ ok: store.connected }">{{ store.connected ? '实时同步中' : '离线——重连后自动刷新' }}</div>
      <van-empty v-if="store.connected && !store.permRequests.length" description="当前没有等你处理的事项" />
      <van-empty v-else-if="!store.connected" description="连接中断，等待重连…" />
      <div v-for="it in store.permRequests" :key="it.requestId" class="ap" :class="{ ask: isAsk(it) }">
        <div class="l1">
          <svg class="i"><use :href="'#' + l1Icon(it)" /></svg>{{ kindLabel(it) }}
          <span class="ss mono">{{ it.sessionName || '当前会话' }} ›</span>
        </div>
        <div class="cmd">{{ it.summary || it.functionName || '权限请求' }}</div>
        <div v-if="it.functionName && it.summary" class="detail mono">{{ it.functionName }}</div>
        <!-- undo_turn 撤销影响摘要 -->
        <div v-if="it.undoImpact" class="imp">{{ impactText(it.undoImpact) }}</div>

        <!-- ask_user 提问：选项列表 + 自由回答（对齐桌面 PermCard） -->
        <template v-if="isAsk(it)">
          <div v-if="it.options.length" class="choices">
            <button
              v-for="(o, oi) in it.options"
              :key="oi"
              :disabled="busyKey === it.requestId"
              @click="decideChoice(it, oi)"
            >{{ o }}</button>
          </div>
          <div class="acts">
            <input v-model="answers[it.requestId]" placeholder="输入回答…" @keydown.enter.prevent="submitCustom(it)" />
            <button class="p" style="flex:none;padding:8px 14px" :disabled="busyKey === it.requestId" @click="submitCustom(it)">回答</button>
          </div>
        </template>

        <!-- 权限档位选项（permission_options） -->
        <div v-else-if="it.permissionOptions.length" class="acts">
          <van-button
            v-for="o in it.permissionOptions"
            :key="o.key"
            size="small"
            plain
            type="primary"
            :loading="busyKey === it.requestId"
            @click="decideOption(it, o.key)"
          >{{ o.label }}</van-button>
          <van-button size="small" class="reject-btn" :loading="busyKey === it.requestId" @click="decide(it, false)">拒绝</van-button>
        </div>

        <!-- 普通命令/操作：允许 / 总是允许 / 拒绝 -->
        <div v-else class="acts">
          <van-button size="small" type="primary" :loading="busyKey === it.requestId" @click="decide(it, true)">允许</van-button>
          <van-button v-if="it.allowAlways" size="small" plain type="primary" :loading="busyKey === it.requestId" @click="decide(it, true, true)">总是允许</van-button>
          <van-button size="small" class="reject-btn" :loading="busyKey === it.requestId" @click="decide(it, false)">拒绝</van-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue';
import { showConfirmDialog, showToast } from 'vant';
import { useAppStore, type PermRequest } from '../stores/app';

// 星瑶品牌 logo（public 静态资源，运行时引用；/m/ 前缀与 lan_server 托管路径一致）
const logoUrl = '/m/icon.png';

const store = useAppStore();
const busyKey = ref('');
const answers = reactive<Record<string, string>>({});

function isAsk(it: PermRequest): boolean {
  return it.functionName === 'ask_user';
}
function kindLabel(it: PermRequest): string {
  if (isAsk(it)) return `${store.agentName} 提问`;
  if (it.functionName === 'undo_turn') return '撤销此回合';
  if (it.permissionOptions.length) return '权限请求';
  return '命令待审批';
}
function l1Icon(it: PermRequest): string {
  if (isAsk(it)) return 'i-chat';
  if (it.functionName === 'undo_turn') return 'i-undo';
  if (it.permissionOptions.length) return 'i-shield';
  return 'i-warn';
}
/** undo_impact 服务预检摘要（结构由 core/turn_undo service 提供，宽松取字段） */
function impactText(imp: any): string {
  if (!imp || typeof imp !== 'object') return '';
  if (typeof imp.summary === 'string' && imp.summary) return imp.summary;
  const files = Number(imp.file_count ?? (Array.isArray(imp.files) ? imp.files.length : 0) ?? 0);
  const msgs = Number(imp.message_count ?? 0);
  const parts: string[] = [];
  if (files) parts.push(`${files} 个文件改动`);
  if (msgs) parts.push(`${msgs} 条消息`);
  return parts.length ? `将撤销 ${parts.join('与')}` : '确认后撤销该回合的文件改动';
}

async function decide(it: PermRequest, approved: boolean, always = false) {
  if (!approved) {
    try {
      await showConfirmDialog({ title: '拒绝请求', message: '确定拒绝该权限请求？模型将收到拒绝结果。' });
    } catch { return; }
  }
  busyKey.value = it.requestId;
  try {
    await store.respondPermission(it.requestId, approved, always);
    showToast(approved ? '已允许' : '已拒绝');
  } catch (e: any) {
    showToast(e?.message || '操作失败');
  } finally {
    busyKey.value = '';
  }
}

async function decideOption(it: PermRequest, key: string) {
  busyKey.value = it.requestId;
  try {
    await store.respondPermissionOption(it.requestId, key);
    showToast('已响应');
  } catch (e: any) {
    showToast(e?.message || '操作失败');
  } finally {
    busyKey.value = '';
  }
}

async function decideChoice(it: PermRequest, index: number) {
  busyKey.value = it.requestId;
  try {
    await store.respondPermissionChoice(it.requestId, index);
    showToast('已响应');
  } catch (e: any) {
    showToast(e?.message || '操作失败');
  } finally {
    busyKey.value = '';
  }
}

/** ask_user 自由文本回答（对齐桌面：respond_permission_choice(-1, text)） */
async function submitCustom(it: PermRequest) {
  const text = (answers[it.requestId] || '').trim();
  if (!text) return;
  busyKey.value = it.requestId;
  try {
    await store.respondPermissionChoice(it.requestId, -1, text);
    answers[it.requestId] = '';
    showToast('已回答');
  } catch (e: any) {
    showToast(e?.message || '操作失败');
  } finally {
    busyKey.value = '';
  }
}
</script>

<style scoped>
.page { height: 100dvh; display: flex; flex-direction: column; background: var(--bg); }
.nvlogo { width: 22px; height: 22px; border-radius: 6px; object-fit: cover; box-shadow: 0 0 8px var(--accent-soft); }
.cnt { font-size: 11px; color: var(--accent); }
.body { flex: 1; min-height: 0; overflow-y: auto; padding-bottom: calc(16px + env(safe-area-inset-bottom)); }
.conn { font-size: var(--fs-xs); color: var(--text-3); padding: 10px 16px 0; }
.conn.ok { color: var(--ok); }
.ap { background: var(--bg-panel); border: 1px solid color-mix(in srgb, var(--danger) 25%, transparent); border-radius: 14px; padding: 12px 14px; margin: 10px 16px; }
.ap.ask { border-color: color-mix(in srgb, var(--accent) 30%, transparent); }
.l1 { font-size: 11px; color: var(--danger); font-weight: 600; display: flex; gap: 7px; align-items: center; }
.ap.ask .l1 { color: var(--accent); }
.l1 .i { width: 13px; height: 13px; }
.ss { margin-left: auto; font-size: 10px; color: var(--text-3); font-weight: 400; }
.cmd { font-size: 12.5px; margin: 7px 0 3px; font-family: var(--font-mono); color: var(--text-1); word-break: break-all; }
.detail { font-size: 11px; color: var(--text-3); margin-bottom: 4px; font-family: var(--font-mono); }
.imp { font-size: 11px; color: var(--text-3); margin: 6px 0 9px; line-height: 1.6; }
.choices { display: flex; flex-direction: column; gap: 7px; margin: 8px 0 4px; }
.choices button { padding: 9px 12px; border-radius: 9px; border: 1px solid var(--line-strong); background: none; color: var(--text-1); font-size: 12.5px; text-align: left; }
.choices button:active { border-color: var(--accent); }
.acts { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; align-items: center; }
.acts .van-button { height: 34px; }
.reject-btn { background: color-mix(in srgb, var(--danger) 12%, transparent); color: var(--danger); border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent); }
.acts input { flex: 1; min-width: 140px; padding: 8px 11px; border-radius: 9px; border: 1px solid var(--line-strong); background: var(--bg-inset); color: var(--text-1); font-size: 12.5px; outline: none; }
</style>
