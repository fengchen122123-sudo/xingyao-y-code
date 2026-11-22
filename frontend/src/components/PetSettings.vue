<template>
  <div class="pet-settings" data-test="pet-settings-page">
    <header class="pet-header">
      <div>
        <h1>桌面宠物</h1>
        <p>在桌面陪伴你的本机桌宠，支持自定义形象与人设。</p>
      </div>
      <button class="refresh-button" type="button" title="重新读取桌宠状态" @click="refreshAll">
        <AppIcon name="refresh" :size="14" />
      </button>
    </header>

    <p v-if="!bridgeReady" data-test="pet-native-only" class="pet-banner pet-banner-info">
      仅在桌面应用内可用：浏览器里打开设置页时无法控制桌宠。
    </p>

    <p
      v-if="message"
      data-test="pet-message"
      class="pet-banner"
      :class="messageOk ? 'pet-banner-ok' : 'pet-banner-err'"
    >{{ message }}</p>

    <section class="pet-card pet-hero">
      <div class="pet-hero-left">
        <div class="pet-hero-info">
          <div class="pet-hero-title-row">
            <span class="pet-hero-name">{{ activeSkinName }}</span>
            <span class="pet-status-pill" :class="{ on: running && bridgeReady }" data-test="pet-status">
              <span class="pet-dot" :class="{ on: running && bridgeReady }"></span>
              <span>{{ statusText }}</span>
            </span>
          </div>
          <p class="pet-hero-sub">
            {{ activeSkinObj?.persona || activeSkinObj?.description || '点击下方卡片切换形象，或导入你自己的桌宠。' }}
          </p>
        </div>
      </div>

      <div class="pet-hero-actions">
        <button
          v-if="running && bridgeReady"
          data-test="pet-test-bubble"
          class="ghost-button"
          type="button"
          title="在桌面上显示一条测试气泡"
          :disabled="busy || testBubbleBusy"
          @click="sendTestBubble"
        >打个招呼</button>
        <button
          data-test="pet-restart"
          class="ghost-button"
          type="button"
          :disabled="busy || loading || !bridgeReady || !running"
          @click="restart"
        >重新打开</button>
        <label class="switch" title="开启或隐藏桌面宠物">
          <input
            data-test="pet-enabled"
            type="checkbox"
            :checked="enabled"
            :disabled="busy || loading || installing || !bridgeReady"
            @change="onToggleChange"
          />
          <span class="switch-track"></span>
        </label>
      </div>
    </section>

    <section v-if="bridgeReady && !available" class="pet-warn" data-test="pet-unavailable">
      <div class="pet-warn-head">
        <strong>需要 Electron 运行时（约 151 MB）</strong>
        <button
          class="secondary-button pet-install-button"
          data-test="pet-install-runtime"
          type="button"
          :disabled="installing || loading || !bridgeReady"
          @click="ensureRuntimeAndEnable"
        >{{ installing ? '正在准备…' : '一键下载并启用' }}</button>
      </div>
      <p class="pet-warn-sub">
        按需安装到 <code>~/.ycode/pet-runtime</code>（可在底部高级设置指定本地 electron.exe）。
      </p>
    </section>

    <section
      v-if="bridgeReady && (installing || runtimeFailed)"
      class="pet-runtime"
      :class="{ 'pet-runtime-failed': runtimeFailed }"
      data-test="pet-runtime-progress"
    >
      <p class="pet-runtime-text" data-test="pet-runtime-text">{{ runtimeText || '正在准备运行时…' }}</p>
      <div
        v-if="runtimeShowProgress"
        class="pet-progress"
        role="progressbar"
        aria-label="Electron 运行时下载进度"
        aria-valuemin="0"
        aria-valuemax="100"
        :aria-valuenow="runtimePercent"
      >
        <span class="pet-progress-fill" :style="{ width: `${runtimePercent}%` }"></span>
      </div>
      <button
        v-if="runtimeFailed"
        class="secondary-button pet-runtime-retry"
        data-test="pet-runtime-retry"
        type="button"
        :disabled="installing"
        @click="ensureRuntimeAndEnable"
      >重试</button>
    </section>

    <section class="pet-card">
      <div class="pet-section-head">
        <div>
          <h2>宠物形象</h2>
          <p class="pet-section-desc">点选即可切换；支持图片、GIF 或 ZIP 宠物包。</p>
        </div>
        <div class="pet-section-actions">
          <button
            class="ghost-button"
            data-test="pet-skins-open-dir"
            type="button"
            :disabled="!skinDir"
            @click="openSkinDir"
          >打开目录</button>
          <button
            class="primary-button"
            data-test="pet-import-open"
            type="button"
            :disabled="!bridgeReady || skinBusy"
            @click="openImportModal"
          >+ 导入桌宠</button>
        </div>
      </div>

      <div class="pet-skin-grid" data-test="pet-skins">
        <div
          v-for="s in skins"
          :key="s.id"
          class="pet-skin-item"
          :class="{ 'is-active': s.id === activeSkin }"
        >
          <button
            class="pet-skin-card"
            :disabled="skinBusy || s.id === activeSkin"
            :data-test="`pet-skin-${s.id}`"
            type="button"
            @click="selectSkin(s.id)"
          >
            <img v-if="s.thumbnail" class="pet-skin-thumb" :src="s.thumbnail" :alt="s.name" />
            <span v-else class="pet-skin-thumb pet-skin-thumb-empty">{{ skinInitial(s.name) }}</span>
            <span class="pet-skin-name">{{ s.name }}</span>
            <span v-if="s.persona" class="pet-skin-persona" :title="s.persona">{{ s.persona }}</span>
            <span class="pet-skin-meta">
              <span class="pet-skin-badge">{{ skinSourceLabel(s.source) }}</span>
              <span v-if="s.id === activeSkin" class="pet-skin-badge pet-skin-badge-active">使用中</span>
            </span>
          </button>

          <div v-if="s.source === 'user'" class="pet-skin-ops">
            <button
              class="pet-skin-op-btn"
              type="button"
              title="编辑名称与性格人设"
              :data-test="`pet-skin-edit-${s.id}`"
              @click.stop="openEditModal(s)"
            >人设</button>
            <button
              class="pet-skin-op-btn pet-skin-op-danger"
              type="button"
              title="删除此桌宠"
              :data-test="`pet-skin-delete-${s.id}`"
              @click.stop="removeSkin(s)"
            >删除</button>
          </div>
        </div>
      </div>
      <p v-if="!skins.length" class="pet-note" data-test="pet-skins-empty">还没有可用的宠物形象。</p>
    </section>

    <section class="pet-card">
      <div class="pet-section-head">
        <div>
          <h2>互动与气泡</h2>
          <p class="pet-section-desc">控制桌宠在桌面冒泡提醒与 AI 陪伴说话的时机。</p>
        </div>
      </div>

      <div class="pet-setting-list">
        <div class="pet-setting-row">
          <div class="pet-setting-copy">
            <div class="pet-setting-title">任务提示</div>
            <p class="pet-setting-sub" data-test="pet-notify-note">
              仅在需要确认、提问、完成或出错时冒泡提醒。
            </p>
          </div>
          <label class="switch">
            <input
              data-test="pet-notify-enabled"
              type="checkbox"
              :checked="notifyEnabled"
              :disabled="notifyBusy || loading || !bridgeReady"
              @change="onNotifyChange"
            />
            <span class="switch-track"></span>
          </label>
        </div>

        <div class="pet-setting-row">
          <div class="pet-setting-copy">
            <div class="pet-setting-title">无聊时自己说话</div>
            <p class="pet-setting-sub">
              空闲达标后按人设随口说话（每日上限 40 句，不读文件内容）。
            </p>
          </div>
          <label class="switch">
            <input
              data-test="pet-chat-enabled"
              type="checkbox"
              :checked="chatEnabled"
              :disabled="chatBusy || loading || !bridgeReady"
              @change="onChatToggle"
            />
            <span class="switch-track"></span>
          </label>
        </div>

        <div v-if="chatEnabled" class="pet-chat-subpanel">
          <label class="pet-chat-field">
            <span class="pet-chat-field-label">空闲多久算无聊</span>
            <select
              class="pet-chat-select"
              data-test="pet-chat-idle"
              :value="String(chatIdleSeconds)"
              :disabled="chatBusy || !bridgeReady"
              @change="onChatIdleChange"
            >
              <option v-if="!chatIdlePresets.includes(chatIdleSeconds)" :value="String(chatIdleSeconds)">
                {{ chatIdleSeconds >= 60 ? `${Math.round(chatIdleSeconds / 60)} 分钟` : `${chatIdleSeconds} 秒` }}
              </option>
              <option value="300">5 分钟</option>
              <option value="600">10 分钟</option>
              <option value="1800">30 分钟</option>
            </select>
          </label>
          <label class="pet-chat-field pet-chat-field-grow">
            <span class="pet-chat-field-label">对话模型</span>
            <select
              class="pet-chat-select"
              data-test="pet-chat-model"
              :value="chatModelId"
              :disabled="chatBusy || !bridgeReady"
              @change="onChatModelChange"
            >
              <option value="">自动（副模型 → 主模型）</option>
              <option v-for="m in chatModels" :key="m.id" :value="m.id">
                {{ m.name }}{{ m.provider_name ? `（${m.provider_name}）` : '' }}
              </option>
            </select>
          </label>
        </div>
      </div>
    </section>

    <details class="pet-card pet-advanced">
      <summary class="pet-advanced-summary">
        <span>高级设置与运行环境</span>
        <span class="pet-advanced-hint">Electron 路径、宠物目录与快捷交互说明</span>
      </summary>
      <div class="pet-advanced-body">
        <div class="pet-adv-block">
          <label class="field-label" for="pet-electron-path">自定义 Electron 路径（可选）</label>
          <div class="pet-path-row">
            <input
              id="pet-electron-path"
              v-model="electronPath"
              data-test="pet-electron-path"
              class="text-field mono"
              type="text"
              placeholder="留空使用内置：pets/xingyao_pet/node_modules/electron/dist/electron.exe"
              :disabled="savingPath"
            />
            <button
              data-test="pet-path-save"
              class="secondary-button"
              type="button"
              :disabled="savingPath || !bridgeReady"
              @click="savePath"
            >{{ savingPath ? '保存中…' : '保存' }}</button>
          </div>
        </div>

        <div v-if="skinDir" class="pet-adv-block">
          <div class="field-label">本地宠物包目录</div>
          <code class="mono pet-skin-dir" data-test="pet-skins-dir">{{ skinDir }}</code>
        </div>

        <p class="pet-note">
          交互技巧：拖动桌宠可移动位置；双击可让它睡觉或唤醒；右键弹出菜单（可隐藏或退出）。
        </p>
      </div>
    </details>

    <div v-if="importModal.open" class="pet-modal-backdrop" @click.self="closeImportModal">
      <div class="pet-modal" data-test="pet-import-modal">
        <header class="pet-modal-head">
          <h3>导入自定义桌宠</h3>
          <button class="pet-modal-close" type="button" @click="closeImportModal">×</button>
        </header>

        <div class="pet-modal-tabs">
          <button
            type="button"
            class="pet-tab"
            :class="{ active: importModal.mode === 'images' }"
            @click="importModal.mode = 'images'"
          >图片 / GIF 创建</button>
          <button
            type="button"
            class="pet-tab"
            :class="{ active: importModal.mode === 'zip' }"
            @click="importModal.mode = 'zip'"
          >导入 ZIP 宠物包</button>
        </div>

        <div class="pet-modal-body">
          <label class="pet-modal-field">
            <span>桌宠名称</span>
            <input
              v-model="importModal.name"
              type="text"
              class="text-field"
              :placeholder="importModal.mode === 'zip' ? '留空则自动读取压缩包内名称' : '例如：小橘猫、初音…'"
            />
          </label>

          <label class="pet-modal-field">
            <span>性格与说话语气（接入星瑶 AI 对话）</span>
            <textarea
              v-model="importModal.persona"
              rows="2"
              class="text-area"
              placeholder="例如：傲娇毒舌但很关心主人的代码进度，喜欢用“哼”开头"
            ></textarea>
          </label>

          <div class="pet-modal-field">
            <span>{{ importModal.mode === 'zip' ? '选择 .zip 压缩包' : '选择图片或 GIF 动图（支持多张序列帧）' }}</span>
            <label
              class="pet-dropzone"
              @dragover.prevent
              @drop.prevent="onImportDrop"
            >
              <input
                type="file"
                :accept="importModal.mode === 'zip' ? '.zip' : 'image/png,image/jpeg,image/webp,image/gif'"
                :multiple="importModal.mode === 'images'"
                @change="onImportFilesChange"
              />
              <span v-if="!importModal.files.length">
                点击选择或拖拽{{ importModal.mode === 'zip' ? ' ZIP 文件' : ' PNG / GIF / JPG 图片' }}到这里
              </span>
              <span v-else class="pet-dropzone-files">
                已选择 {{ importModal.files.length }} 个文件：{{ importModal.files.map(f => f.name).join('、') }}
              </span>
            </label>
            <p class="pet-modal-tip">
              {{
                importModal.mode === 'zip'
                  ? '支持标准星瑶宠物包，也支持直接把图片打包成 zip，系统会自动转换格式并集成。'
                  : '上传 GIF 会自动拆解成动画帧；上传多张 PNG 会按顺序组成呼吸动作。'
              }}
            </p>
          </div>

          <label class="pet-modal-check">
            <input v-model="importModal.activate" type="checkbox" />
            <span>导入后立即设为当前桌宠</span>
          </label>

          <p v-if="importModal.error" class="pet-modal-error">{{ importModal.error }}</p>
        </div>

        <footer class="pet-modal-foot">
          <button type="button" class="ghost-button" :disabled="importModal.submitting" @click="closeImportModal">取消</button>
          <button type="button" class="primary-button" :disabled="importModal.submitting" @click="submitImport">
            {{ importModal.submitting ? '正在集成…' : '确认导入' }}
          </button>
        </footer>
      </div>
    </div>

    <div v-if="editModal.open" class="pet-modal-backdrop" @click.self="closeEditModal">
      <div class="pet-modal" data-test="pet-edit-modal">
        <header class="pet-modal-head">
          <h3>编辑桌宠人设</h3>
          <button class="pet-modal-close" type="button" @click="closeEditModal">×</button>
        </header>
        <div class="pet-modal-body">
          <label class="pet-modal-field">
            <span>桌宠名称</span>
            <input v-model="editModal.name" type="text" class="text-field" />
          </label>
          <label class="pet-modal-field">
            <span>性格与说话语气</span>
            <textarea
              v-model="editModal.persona"
              rows="3"
              class="text-area"
              placeholder="描述它的性格、口癖或对主人的称呼，开启「无聊时自己说话」时会按此人设生成台词。"
            ></textarea>
          </label>
          <p v-if="editModal.error" class="pet-modal-error">{{ editModal.error }}</p>
        </div>
        <footer class="pet-modal-foot">
          <button type="button" class="ghost-button" :disabled="editModal.submitting" @click="closeEditModal">取消</button>
          <button type="button" class="primary-button" :disabled="editModal.submitting" @click="submitEdit">
            {{ editModal.submitting ? '保存中…' : '保存人设' }}
          </button>
        </footer>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue';

import AppIcon from './AppIcon.vue';
import type {
  PetChatModelOption,
  PetChatSettingsResult,
  PetMutationResult,
  PetRuntimeStatus,
  PetSkinDto,
  PetSkinResult,
  PetStatusDto,
} from '../types/pywebview';

const bridgeReady = ref(true);
const loading = ref(false);
const busy = ref(false);
const savingPath = ref(false);
const testBubbleBusy = ref(false);
const running = ref(false);
const pid = ref<number | null>(null);
const available = ref(true);
const enabled = ref(false);
const notifyEnabled = ref(true);
const notifyBusy = ref(false);
const electronPath = ref('');
const pendingText = ref('');
const message = ref('');
const messageOk = ref(true);

const DEFAULT_BUILTIN_SKIN: PetSkinDto = {
  id: 'xingyao',
  name: '星瑶',
  description: '星瑶的默认形象：白裙长发、会呼吸也会走动的少女，随对话做出拖拽与互动的动作。',
  persona: '星瑶默认形象，温柔陪伴编程',
  author: '星瑶',
  source: 'builtin',
  dir: '',
  assets_dir: '',
  config_path: '',
  active: true,
  thumbnail: '/pet-xingyao-preview.png',
};

const skins = ref<PetSkinDto[]>([DEFAULT_BUILTIN_SKIN]);
const activeSkin = ref('xingyao');
const skinDir = ref('');
const skinBusy = ref(false);

const activeSkinObj = computed(() =>
  skins.value.find((s) => s.id === activeSkin.value) || skins.value[0] || null,
);
const activeSkinName = computed(() => activeSkinObj.value?.name || '星瑶');

const runtimeInstalled = ref(true);
const runtimeState = ref('idle');
const runtimeMsg = ref('');
const runtimeDownloaded = ref(0);
const runtimeTotal = ref(0);
const runtimePercent = ref(0);
const installing = ref(false);
const runtimeFailed = ref(false);
const RUNTIME_POLL_MS = 2000;
let runtimeTimer: ReturnType<typeof setInterval> | null = null;
let disposed = false;

function formatMb(bytes: number): string {
  if (!bytes || bytes <= 0) return '0';
  return String(Math.max(1, Math.round(bytes / (1024 * 1024))));
}

const runtimeText = computed(() => {
  if (runtimeFailed.value) return runtimeMsg.value || '运行时安装失败，请重试';
  if (!installing.value) return '';
  switch (runtimeState.value) {
    case 'downloading': {
      if (runtimeTotal.value <= 0) return '正在准备运行时…';
      return `正在下载 Electron 运行时 ${runtimePercent.value}%（${formatMb(runtimeDownloaded.value)} / ${formatMb(runtimeTotal.value)} MB）`;
    }
    case 'verifying':
      return '校验安装包…';
    case 'extracting':
      return '正在解压…';
    case 'done':
      return '运行时已就绪，正在启用桌宠…';
    case 'failed':
      return runtimeMsg.value || '运行时安装失败，请重试';
    default:
      return '正在准备运行时…';
  }
});

const runtimeShowProgress = computed(() => {
  if (!installing.value || runtimeFailed.value) return false;
  return runtimeState.value === 'downloading';
});

const needsRuntime = computed(() => !available.value);

const statusText = computed(() => {
  if (!bridgeReady.value) return '仅桌面端可用';
  if (installing.value) return runtimeText.value || '正在准备运行时…';
  if (pendingText.value) return pendingText.value;
  if (loading.value) return '读取中…';
  if (running.value) return `运行中 · PID ${pid.value ?? '未知'}`;
  return '未运行';
});

function getApi() {
  return typeof window === 'undefined' ? undefined : window.pywebview?.api;
}

function report(ok: boolean, text: string) {
  messageOk.value = ok;
  message.value = text;
}

function applyRuntime(runtime?: PetRuntimeStatus) {
  if (!runtime) return;
  if (typeof runtime.installed === 'boolean') runtimeInstalled.value = runtime.installed;
  if (typeof runtime.state === 'string' && runtime.state) runtimeState.value = runtime.state;
  runtimeMsg.value = typeof runtime.msg === 'string' ? runtime.msg : '';
  if (typeof runtime.downloaded === 'number') runtimeDownloaded.value = runtime.downloaded;
  if (typeof runtime.total === 'number') runtimeTotal.value = runtime.total;
  if (typeof runtime.percent === 'number') {
    runtimePercent.value = Math.max(0, Math.min(100, Math.round(runtime.percent)));
  }
  if (!installing.value && (runtimeInstalled.value || runtimeState.value === 'done')) {
    runtimeFailed.value = false;
  }
}

function applyStatus(status?: PetStatusDto, syncPath = false) {
  if (!status) return;
  running.value = Boolean(status.running);
  pid.value = typeof status.pid === 'number' ? status.pid : null;
  available.value = status.available !== false;
  if (!installing.value) {
    enabled.value = typeof status.enabled === 'boolean' ? status.enabled : running.value;
  }
  applyRuntime(status.runtime);
  notifyEnabled.value = typeof status.notify_enabled === 'boolean' ? status.notify_enabled : true;
  if (typeof status.skin_id === 'string' && status.skin_id) activeSkin.value = status.skin_id;
  if (syncPath && typeof status.electron_path === 'string') electronPath.value = status.electron_path;
}

async function refresh() {
  const api = getApi();
  if (!api || typeof api.pet_get_status !== 'function') {
    bridgeReady.value = false;
    return;
  }
  bridgeReady.value = true;
  loading.value = true;
  try {
    const status = await api.pet_get_status();
    applyStatus(status, true);
    if (status && status.ok === false && status.msg) report(false, status.msg);
  } catch {
    report(false, '读取桌宠状态失败');
  } finally {
    loading.value = false;
  }
}

async function refreshAll() {
  await Promise.all([refresh(), loadSkins(), loadChatSettings()]);
}

function skinInitial(name: string): string {
  const chars = Array.from((name || '').trim());
  return chars.length ? chars[0] : '宠';
}

function skinSourceLabel(source: string): string {
  if (source === 'builtin') return '内置';
  if (source === 'user') return '自定义';
  return source || '宠物';
}

function applySkins(res?: PetSkinResult) {
  if (!res) return;
  if (Array.isArray(res.skins) && res.skins.length > 0) {
    skins.value = res.skins
      .filter((s) => s && typeof s.id === 'string')
      .map((s) =>
        s.id === 'xingyao' && !s.thumbnail
          ? { ...s, thumbnail: DEFAULT_BUILTIN_SKIN.thumbnail }
          : s,
      );
  }
  if (typeof res.active === 'string' && res.active) {
    activeSkin.value = res.active;
  } else {
    const flagged = skins.value.find((s) => s.active);
    if (flagged) activeSkin.value = flagged.id;
  }
  if (typeof res.user_dir === 'string') skinDir.value = res.user_dir;
}

async function loadSkins() {
  if (disposed) return;
  const api = getApi();
  if (!api || typeof api.pet_list_skins !== 'function') return;
  try {
    const res = await api.pet_list_skins();
    if (disposed) return;
    applySkins(res);
    if (res && res.ok === false) report(false, res.msg || '读取宠物列表失败');
  } catch {
    if (disposed) return;
    report(false, '读取宠物列表失败');
  }
}

async function selectSkin(id: string) {
  if (skinBusy.value || id === activeSkin.value) return;
  const api = getApi();
  if (!api || typeof api.pet_set_skin !== 'function') {
    if (!api) bridgeReady.value = false;
    report(false, '当前版本不支持切换宠物形象');
    return;
  }
  skinBusy.value = true;
  message.value = '';
  try {
    const res: PetMutationResult | undefined = await api.pet_set_skin(id);
    if (disposed) return;
    if (res?.status) applyStatus(res.status);
    await loadSkins();
    report(Boolean(res?.ok), res?.msg || (res?.ok ? '已切换' : '切换失败'));
  } catch {
    if (disposed) return;
    report(false, '切换失败：桌面桥接不可用');
  } finally {
    skinBusy.value = false;
  }
}

async function openSkinDir() {
  if (!skinDir.value) return;
  const api = getApi();
  if (!api || typeof api.reveal_in_explorer !== 'function') {
    report(false, '打开宠物目录失败');
    return;
  }
  try {
    const res = await api.reveal_in_explorer(skinDir.value);
    if (res && res.ok === false) report(false, '打开宠物目录失败');
  } catch {
    report(false, '打开宠物目录失败');
  }
}

async function sendTestBubble() {
  const api = getApi();
  if (!api || typeof api.pet_test_bubble !== 'function') return;
  testBubbleBusy.value = true;
  try {
    const res = await api.pet_test_bubble('');
    report(Boolean(res?.ok), res?.msg || (res?.ok ? '已发送测试气泡' : '发送失败'));
  } catch {
    report(false, '发送测试气泡失败');
  } finally {
    testBubbleBusy.value = false;
  }
}

const importModal = reactive({
  open: false,
  mode: 'images' as 'images' | 'zip',
  name: '',
  persona: '',
  activate: true,
  files: [] as File[],
  submitting: false,
  error: '',
});

function openImportModal() {
  importModal.open = true;
  importModal.mode = 'images';
  importModal.name = '';
  importModal.persona = '';
  importModal.activate = true;
  importModal.files = [];
  importModal.submitting = false;
  importModal.error = '';
}

function closeImportModal() {
  if (importModal.submitting) return;
  importModal.open = false;
}

function onImportFilesChange(event: Event) {
  const input = event.target as HTMLInputElement | null;
  if (!input?.files?.length) return;
  importModal.files = Array.from(input.files);
  importModal.error = '';
}

function onImportDrop(event: DragEvent) {
  const dropped = event.dataTransfer?.files;
  if (!dropped?.length) return;
  const list = Array.from(dropped);
  if (list.some((f) => f.name.toLowerCase().endsWith('.zip'))) {
    importModal.mode = 'zip';
    importModal.files = [list.find((f) => f.name.toLowerCase().endsWith('.zip'))!];
  } else {
    importModal.mode = 'images';
    importModal.files = list;
  }
  importModal.error = '';
}

function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error(`读取文件 ${file.name} 失败`));
    reader.readAsDataURL(file);
  });
}

async function submitImport() {
  if (importModal.submitting) return;
  const api = getApi();
  if (!api || typeof api.pet_import_skin !== 'function') {
    importModal.error = '当前版本不支持界面导入桌宠';
    return;
  }
  if (!importModal.files.length) {
    importModal.error = '请先选择要导入的文件';
    return;
  }
  if (importModal.mode === 'images' && !importModal.name.trim()) {
    importModal.error = '请给新桌宠起个名字';
    return;
  }

  importModal.submitting = true;
  importModal.error = '';
  try {
    const encoded = await Promise.all(
      importModal.files.map(async (f) => ({
        name: f.name,
        data: await readFileAsBase64(f),
      })),
    );
    const res = await api.pet_import_skin({
      mode: importModal.mode,
      name: importModal.name.trim(),
      persona: importModal.persona.trim(),
      activate: importModal.activate,
      files: encoded,
    });
    if (!res?.ok) {
      importModal.error = res?.msg || '导入失败';
      return;
    }
    if (res.status) applyStatus(res.status);
    if (res.list) applySkins(res.list);
    else await loadSkins();
    importModal.open = false;
    report(true, res.msg || '导入成功');
  } catch (err) {
    importModal.error = err instanceof Error ? err.message : '导入失败';
  } finally {
    importModal.submitting = false;
  }
}

const editModal = reactive({
  open: false,
  id: '',
  name: '',
  persona: '',
  submitting: false,
  error: '',
});

function openEditModal(skin: PetSkinDto) {
  editModal.open = true;
  editModal.id = skin.id;
  editModal.name = skin.name;
  editModal.persona = skin.persona || skin.description || '';
  editModal.submitting = false;
  editModal.error = '';
}

function closeEditModal() {
  if (editModal.submitting) return;
  editModal.open = false;
}

async function submitEdit() {
  if (editModal.submitting) return;
  const api = getApi();
  if (!api || typeof api.pet_update_skin !== 'function') {
    editModal.error = '当前版本不支持编辑桌宠人设';
    return;
  }
  if (!editModal.name.trim()) {
    editModal.error = '桌宠名称不能为空';
    return;
  }
  editModal.submitting = true;
  editModal.error = '';
  try {
    const res = await api.pet_update_skin(editModal.id, {
      name: editModal.name.trim(),
      persona: editModal.persona.trim(),
    });
    if (!res?.ok) {
      editModal.error = res?.msg || '保存失败';
      return;
    }
    if (res.list) applySkins(res.list);
    else await loadSkins();
    editModal.open = false;
    report(true, res.msg || '已更新桌宠人设');
  } catch {
    editModal.error = '保存失败：桌面桥接不可用';
  } finally {
    editModal.submitting = false;
  }
}

async function removeSkin(skin: PetSkinDto) {
  const api = getApi();
  if (!api || typeof api.pet_delete_skin !== 'function') return;
  if (!window.confirm(`确定要删除自定义桌宠「${skin.name}」吗？`)) return;
  skinBusy.value = true;
  try {
    const res = await api.pet_delete_skin(skin.id);
    if (res?.status) applyStatus(res.status);
    if (res?.list) applySkins(res.list);
    else await loadSkins();
    report(Boolean(res?.ok), res?.msg || (res?.ok ? '已删除' : '删除失败'));
  } catch {
    report(false, '删除失败：桌面桥接不可用');
  } finally {
    skinBusy.value = false;
  }
}

function onToggleChange(event: Event) {
  const target = event.target as HTMLInputElement | null;
  const next = Boolean(target?.checked);
  if (next && needsRuntime.value) {
    void ensureRuntimeAndEnable();
    return;
  }
  void toggleEnabled(next);
}

async function toggleEnabled(next: boolean) {
  const api = getApi();
  if (!api || typeof api.pet_set_enabled !== 'function') {
    bridgeReady.value = false;
    return;
  }
  const previous = enabled.value;
  enabled.value = next;
  busy.value = true;
  pendingText.value = next ? '正在启动桌宠…' : '正在关闭桌宠…';
  message.value = '';
  try {
    const res: PetMutationResult | undefined = await api.pet_set_enabled(next);
    if (res?.status) applyStatus(res.status);
    else enabled.value = Boolean(res?.ok) ? next : previous;
    report(Boolean(res?.ok), res?.msg || (res?.ok ? (next ? '桌宠已启动' : '桌宠已关闭') : '操作失败'));
  } catch {
    enabled.value = previous;
    report(false, '操作失败：桌面桥接不可用');
  } finally {
    busy.value = false;
    pendingText.value = '';
  }
}

function stopPolling() {
  if (runtimeTimer !== null) {
    clearInterval(runtimeTimer);
    runtimeTimer = null;
  }
}

function startPolling() {
  stopPolling();
  runtimeTimer = setInterval(() => {
    void pollRuntime();
  }, RUNTIME_POLL_MS);
}

function failRuntime(text: string) {
  stopPolling();
  installing.value = false;
  pendingText.value = '';
  enabled.value = false;
  runtimeFailed.value = true;
  runtimeMsg.value = text;
  message.value = '';
}

async function ensureRuntimeAndEnable() {
  if (installing.value) return;
  const api = getApi();
  if (!api || typeof api.pet_install_runtime !== 'function') {
    if (!api) bridgeReady.value = false;
    else report(false, '当前版本不支持自动安装 Electron 运行时，请手动填写下方 electron.exe 路径。');
    return;
  }
  enabled.value = true;
  installing.value = true;
  runtimeFailed.value = false;
  runtimeState.value = 'idle';
  runtimeMsg.value = '';
  runtimePercent.value = 0;
  runtimeDownloaded.value = 0;
  runtimeTotal.value = 0;
  message.value = '';
  startPolling();
  try {
    const res = await api.pet_install_runtime();
    if (disposed) return;
    applyRuntime(res?.runtime);
    if (res && res.ok === false) {
      failRuntime(res.msg || '启动运行时安装失败');
      return;
    }
    if (runtimeInstalled.value) void enableAfterRuntime();
  } catch {
    if (disposed) return;
    failRuntime('启动运行时安装失败：桌面桥接不可用');
  }
}

async function pollRuntime() {
  if (disposed) {
    stopPolling();
    return;
  }
  const api = getApi();
  if (!api || typeof api.pet_get_runtime_status !== 'function') {
    failRuntime('当前版本不支持查询运行时状态，请手动填写 electron.exe 路径。');
    return;
  }
  try {
    const res = await api.pet_get_runtime_status();
    if (disposed) return;
    applyRuntime(res?.runtime);
    if (res && res.ok === false) {
      failRuntime(res.msg || '查询运行时安装进度失败');
      return;
    }
    if (!res?.runtime) return;
    if (runtimeInstalled.value || runtimeState.value === 'done') {
      void enableAfterRuntime();
      return;
    }
    if (runtimeState.value === 'failed') {
      failRuntime(runtimeMsg.value || '运行时安装失败，请重试');
    }
  } catch {
    if (disposed) return;
    failRuntime('查询运行时安装进度失败：桌面桥接不可用');
  }
}

async function enableAfterRuntime() {
  if (!installing.value) return;
  stopPolling();
  runtimeState.value = 'done';
  pendingText.value = '运行时已就绪，正在启用桌宠…';
  const api = getApi();
  if (!api || typeof api.pet_set_enabled !== 'function') {
    if (!api) bridgeReady.value = false;
    failRuntime('启用桌宠失败：桌面桥接不可用');
    return;
  }
  try {
    const res: PetMutationResult | undefined = await api.pet_set_enabled(true);
    if (disposed) return;
    if (res?.status) applyStatus(res.status);
    else enabled.value = Boolean(res?.ok);
    if (res?.ok) {
      installing.value = false;
      pendingText.value = '';
      runtimeFailed.value = false;
      report(true, '桌宠已启动');
      await refresh();
      return;
    }
    failRuntime(res?.msg || '运行时已就绪，但启用桌宠失败');
  } catch {
    if (disposed) return;
    failRuntime('启用桌宠失败：桌面桥接不可用');
  }
}

function onNotifyChange(event: Event) {
  const target = event.target as HTMLInputElement | null;
  void toggleNotify(Boolean(target?.checked));
}

async function toggleNotify(next: boolean) {
  const api = getApi();
  if (!api || typeof api.pet_set_notify_enabled !== 'function') {
    bridgeReady.value = false;
    return;
  }
  const previous = notifyEnabled.value;
  notifyEnabled.value = next;
  notifyBusy.value = true;
  message.value = '';
  try {
    const res: PetMutationResult | undefined = await api.pet_set_notify_enabled(next);
    if (res?.status) applyStatus(res.status);
    else if (!res?.ok) notifyEnabled.value = previous;
    report(Boolean(res?.ok), res?.msg || (res?.ok ? (next ? '任务提示已开启' : '任务提示已关闭') : '操作失败'));
  } catch {
    notifyEnabled.value = previous;
    report(false, '操作失败：桌面桥接不可用');
  } finally {
    notifyBusy.value = false;
  }
}

const chatIdlePresets = [300, 600, 1800];
const chatEnabled = ref(false);
const chatIdleSeconds = ref(600);
const chatModelId = ref('');
const chatModels = ref<PetChatModelOption[]>([]);
const chatBusy = ref(false);

function applyChatSettings(res?: PetChatSettingsResult) {
  if (!res) return;
  if (typeof res.enabled === 'boolean') chatEnabled.value = res.enabled;
  if (typeof res.idle_seconds === 'number' && Number.isFinite(res.idle_seconds)) {
    chatIdleSeconds.value = Math.round(res.idle_seconds);
  }
  if (typeof res.model_id === 'string') chatModelId.value = res.model_id;
  chatModels.value = Array.isArray(res.models)
    ? res.models.filter((m) => m && typeof m.id === 'string')
    : [];
}

async function loadChatSettings(): Promise<boolean> {
  if (disposed) return false;
  const api = getApi();
  if (!api || typeof api.pet_chat_get_settings !== 'function') return false;
  try {
    const res = await api.pet_chat_get_settings();
    if (disposed) return false;
    applyChatSettings(res);
    if (res && res.ok === false) report(false, res.msg || '读取「自己说话」设置失败');
    return true;
  } catch {
    if (disposed) return false;
    report(false, '读取「自己说话」设置失败');
    return false;
  }
}

async function saveChat(patch: {
  enabled?: boolean;
  idle_seconds?: number;
  model_id?: string;
}): Promise<boolean> {
  if (chatBusy.value) return true;
  const api = getApi();
  if (!api || typeof api.pet_chat_set_settings !== 'function') {
    if (!api) bridgeReady.value = false;
    const synced = await loadChatSettings();
    if (synced) report(false, '当前版本不支持「自己说话」设置');
    return synced;
  }
  chatBusy.value = true;
  message.value = '';
  let synced = false;
  try {
    const res = await api.pet_chat_set_settings(patch);
    if (disposed) return false;
    synced = await loadChatSettings();
    if (disposed) return synced;
    if (res?.ok) report(true, res.msg || '已保存');
    else report(false, res?.msg || '保存失败');
  } catch {
    if (disposed) return false;
    synced = await loadChatSettings();
    if (disposed) return synced;
    report(false, '保存失败：桌面桥接不可用');
  } finally {
    chatBusy.value = false;
  }
  return synced;
}

function onChatToggle(event: Event) {
  const target = event.target as HTMLInputElement | null;
  const next = Boolean(target?.checked);
  const previous = chatEnabled.value;
  chatEnabled.value = next;
  void saveChat({ enabled: next }).then((synced) => {
    if (synced || disposed) return;
    chatEnabled.value = previous;
    if (target) target.checked = previous;
  });
}

function onChatIdleChange(event: Event) {
  const target = event.target as HTMLSelectElement | null;
  const next = Number(target?.value);
  if (!Number.isFinite(next) || next <= 0) return;
  const previous = chatIdleSeconds.value;
  chatIdleSeconds.value = next;
  void saveChat({ idle_seconds: next }).then((synced) => {
    if (synced || disposed) return;
    chatIdleSeconds.value = previous;
    if (target) target.value = String(previous);
  });
}

function onChatModelChange(event: Event) {
  const target = event.target as HTMLSelectElement | null;
  const next = String(target?.value ?? '');
  const previous = chatModelId.value;
  chatModelId.value = next;
  void saveChat({ model_id: next }).then((synced) => {
    if (synced || disposed) return;
    chatModelId.value = previous;
    if (target) target.value = previous;
  });
}

async function savePath() {
  const api = getApi();
  if (!api || typeof api.pet_set_electron_path !== 'function') {
    bridgeReady.value = false;
    return;
  }
  savingPath.value = true;
  message.value = '';
  try {
    const res: PetMutationResult | undefined = await api.pet_set_electron_path(electronPath.value.trim());
    if (res?.status) applyStatus(res.status);
    report(Boolean(res?.ok), res?.msg || (res?.ok ? '已保存' : '保存失败'));
  } catch {
    report(false, '保存失败：桌面桥接不可用');
  } finally {
    savingPath.value = false;
  }
}

async function restart() {
  const api = getApi();
  if (!api || typeof api.pet_restart !== 'function') {
    bridgeReady.value = false;
    return;
  }
  busy.value = true;
  pendingText.value = '正在重新打开桌宠…';
  message.value = '';
  try {
    const res: PetMutationResult | undefined = await api.pet_restart();
    if (res?.status) applyStatus(res.status);
    report(Boolean(res?.ok), res?.msg || (res?.ok ? '桌宠已重新打开' : '重新打开失败'));
  } catch {
    report(false, '重新打开失败：桌面桥接不可用');
  } finally {
    busy.value = false;
    pendingText.value = '';
  }
}

onMounted(() => {
  void refreshAll();
});

onUnmounted(() => {
  disposed = true;
  stopPolling();
});
</script>

<style scoped>
.pet-settings {
  max-width: 880px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.pet-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.pet-header h1 { margin: 0 0 6px; font-size: 24px; font-weight: 700; }
.pet-header p { margin: 0; color: var(--text-faint); font-size: 13px; line-height: 1.6; }

.refresh-button {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg2);
  color: var(--text-dim);
  flex: none;
  cursor: pointer;
}
.refresh-button:hover { color: var(--text); background: var(--bg3); }

.pet-banner {
  margin: 0;
  padding: 10px 14px;
  border-radius: 10px;
  font-size: 12.5px;
  line-height: 1.5;
  border: 1px solid var(--border);
}
.pet-banner-ok {
  color: var(--green);
  background: color-mix(in srgb, var(--green) 8%, var(--bg1));
  border-color: color-mix(in srgb, var(--green) 30%, var(--border));
}
.pet-banner-err {
  color: var(--theme-danger);
  background: color-mix(in srgb, var(--theme-danger) 8%, var(--bg1));
  border-color: color-mix(in srgb, var(--theme-danger) 30%, var(--border));
}
.pet-banner-info {
  color: var(--text-dim);
  background: var(--bg2);
}

.pet-card {
  padding: 18px 20px;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--bg1);
}

.pet-hero {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  background: linear-gradient(135deg, color-mix(in srgb, var(--accent) 6%, var(--bg1)), var(--bg1));
}
.pet-hero-left {
  display: flex;
  align-items: center;
  min-width: 0;
  flex: 1;
}
.pet-hero-info { min-width: 0; flex: 1; }
.pet-hero-title-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.pet-hero-name { font-size: 16px; font-weight: 700; color: var(--text); }
.pet-hero-sub {
  margin: 4px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.5;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pet-hero-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: none;
}

.pet-status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 9px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text-faint);
  font-size: 11.5px;
}
.pet-status-pill.on {
  color: var(--green);
  border-color: color-mix(in srgb, var(--green) 35%, var(--border));
  background: color-mix(in srgb, var(--green) 8%, var(--bg1));
}
.pet-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text-faint); }
.pet-dot.on { background: var(--green); }

.switch { position: relative; display: inline-flex; align-items: center; cursor: pointer; flex: none; }
.switch input { position: absolute; opacity: 0; width: 0; height: 0; }
.switch-track {
  width: 40px;
  height: 22px;
  border-radius: 999px;
  background: var(--bg3);
  border: 1px solid var(--border);
  position: relative;
  transition: background .18s ease, border-color .18s ease;
}
.switch-track::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--text-dim);
  transition: transform .18s ease, background .18s ease;
}
.switch input:checked + .switch-track {
  background: var(--accent);
  border-color: var(--accent);
}
.switch input:checked + .switch-track::after {
  transform: translateX(18px);
  background: #fff;
}
.switch input:disabled + .switch-track {
  opacity: .55;
  cursor: not-allowed;
}

.primary-button, .secondary-button, .ghost-button {
  min-height: 34px;
  padding: 0 14px;
  border-radius: 9px;
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  flex: none;
  border: 1px solid var(--border);
}
.primary-button { background: var(--accent); border-color: var(--accent); color: #fff; }
.primary-button:hover:not(:disabled) { filter: brightness(1.06); }
.secondary-button { background: var(--bg3); color: var(--text); }
.secondary-button:hover:not(:disabled) { background: var(--bg2); }
.ghost-button { background: transparent; color: var(--text-dim); }
.ghost-button:hover:not(:disabled) { color: var(--text); background: var(--bg2); }
.primary-button:disabled, .secondary-button:disabled, .ghost-button:disabled { opacity: .5; cursor: not-allowed; }

.pet-warn {
  padding: 12px 16px;
  border: 1px solid color-mix(in srgb, var(--theme-warning) 35%, var(--border));
  border-radius: 12px;
  background: color-mix(in srgb, var(--theme-warning) 7%, var(--bg1));
  color: var(--theme-warning);
  font-size: 12.5px;
}
.pet-warn-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.pet-warn-sub { margin: 6px 0 0; color: var(--text-dim); font-size: 12px; line-height: 1.5; }
.pet-install-button { color: var(--theme-warning); border-color: currentColor; background: var(--bg1); }

.pet-runtime { padding: 12px 16px; border: 1px solid var(--border); border-radius: 12px; background: var(--bg2); color: var(--text-dim); font-size: 12.5px; }
.pet-runtime-failed { border-color: color-mix(in srgb, var(--theme-danger) 35%, var(--border)); background: color-mix(in srgb, var(--theme-danger) 8%, var(--bg1)); color: var(--theme-danger); }
.pet-runtime-text { margin: 0; line-height: 1.5; }
.pet-progress { margin: 8px 0 0; height: 5px; border-radius: 999px; background: var(--bg3); overflow: hidden; }
.pet-progress-fill { display: block; height: 100%; border-radius: 999px; background: var(--accent); transition: width .2s linear; }
.pet-runtime-retry { margin-top: 8px; }

.pet-section-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
.pet-section-head h2 { margin: 0 0 4px; font-size: 15px; font-weight: 700; color: var(--text); }
.pet-section-desc { margin: 0; color: var(--text-faint); font-size: 12px; line-height: 1.5; }
.pet-section-actions { display: flex; align-items: center; gap: 8px; flex: none; }

.pet-skin-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(136px, 1fr));
  gap: 12px;
}
.pet-skin-item {
  position: relative;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg2);
  overflow: hidden;
  transition: border-color .15s ease, transform .15s ease;
}
.pet-skin-item:hover { border-color: color-mix(in srgb, var(--accent) 45%, var(--border)); }
.pet-skin-item.is-active {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 16%, transparent);
}
.pet-skin-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 14px 10px 10px;
  border: none;
  background: transparent;
  color: var(--text);
  font: inherit;
  cursor: pointer;
}
.pet-skin-card:disabled { cursor: default; }
.pet-skin-thumb { width: 76px; height: 76px; object-fit: contain; border-radius: 10px; }
.pet-skin-thumb-empty {
  display: grid;
  place-items: center;
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text-faint);
  font-size: 22px;
  font-weight: 600;
}
.pet-skin-name {
  width: 100%;
  font-size: 13px;
  font-weight: 600;
  text-align: center;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.pet-skin-persona {
  width: 100%;
  font-size: 11px;
  color: var(--text-faint);
  text-align: center;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.pet-skin-meta { display: flex; align-items: center; justify-content: center; gap: 4px; flex-wrap: wrap; }
.pet-skin-badge {
  padding: 1px 6px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg3);
  color: var(--text-faint);
  font-size: 10.5px;
  line-height: 1.5;
}
.pet-skin-badge-active {
  border-color: var(--accent);
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}

.pet-skin-ops {
  display: flex;
  border-top: 1px solid var(--border);
  background: var(--bg1);
}
.pet-skin-op-btn {
  flex: 1;
  padding: 5px 0;
  border: none;
  background: transparent;
  color: var(--text-dim);
  font-size: 11px;
  cursor: pointer;
}
.pet-skin-op-btn + .pet-skin-op-btn { border-left: 1px solid var(--border); }
.pet-skin-op-btn:hover { background: var(--bg3); color: var(--text); }
.pet-skin-op-danger:hover { color: var(--theme-danger); }

.pet-setting-list { display: flex; flex-direction: column; }
.pet-setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 12px 0;
}
.pet-setting-row + .pet-setting-row { border-top: 1px solid var(--border); }
.pet-setting-copy { min-width: 0; }
.pet-setting-title { font-size: 13.5px; font-weight: 600; color: var(--text); }
.pet-setting-sub { margin: 3px 0 0; color: var(--text-faint); font-size: 12px; line-height: 1.5; }

.pet-chat-subpanel {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 4px;
  padding: 12px 14px;
  border-radius: 10px;
  background: var(--bg2);
  border: 1px solid var(--border);
}
.pet-chat-field { display: flex; flex-direction: column; gap: 5px; min-width: 150px; }
.pet-chat-field-grow { flex: 1 1 240px; min-width: 220px; }
.pet-chat-field-label { color: var(--text-dim); font-size: 11.5px; }
.pet-chat-select {
  min-height: 34px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg1);
  color: var(--text);
  font-size: 13px;
}
.pet-chat-select:disabled { opacity: .6; cursor: not-allowed; }

.pet-advanced { padding: 0; overflow: hidden; }
.pet-advanced-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 20px;
  cursor: pointer;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text-dim);
  list-style: none;
}
.pet-advanced-summary::-webkit-details-marker { display: none; }
.pet-advanced-summary:hover { color: var(--text); background: var(--bg2); }
.pet-advanced-hint { font-size: 12px; font-weight: 400; color: var(--text-faint); }
.pet-advanced-body { padding: 0 20px 18px; border-top: 1px solid var(--border); }
.pet-adv-block { margin-top: 14px; }
.field-label { display: block; margin-bottom: 6px; color: var(--text-dim); font-size: 12px; }
.pet-path-row { display: flex; align-items: center; gap: 10px; }
.text-field {
  width: 100%;
  min-width: 0;
  min-height: 38px;
  padding: 0 12px;
  box-sizing: border-box;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg2);
  color: var(--text);
  font-size: 13px;
}
.text-field:focus, .text-area:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 15%, transparent);
}
.text-area {
  width: 100%;
  padding: 9px 12px;
  box-sizing: border-box;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg2);
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
  resize: vertical;
}
.mono { font-family: var(--mono); }
.pet-skin-dir { display: block; color: var(--text-faint); font-size: 11.5px; word-break: break-all; }
.pet-note { margin: 14px 0 0; color: var(--text-faint); font-size: 12px; line-height: 1.6; }

.pet-modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1200;
  background: rgba(0, 0, 0, .48);
  display: grid;
  place-items: center;
  padding: 20px;
}
.pet-modal {
  width: 100%;
  max-width: 480px;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: 0 18px 48px rgba(0, 0, 0, .28);
  overflow: hidden;
}
.pet-modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
}
.pet-modal-head h3 { margin: 0; font-size: 15px; font-weight: 700; }
.pet-modal-close {
  border: none;
  background: transparent;
  color: var(--text-faint);
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
}
.pet-modal-close:hover { color: var(--text); }
.pet-modal-tabs {
  display: flex;
  gap: 8px;
  padding: 12px 18px 0;
}
.pet-tab {
  flex: 1;
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg2);
  color: var(--text-dim);
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
}
.pet-tab.active {
  border-color: var(--accent);
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}
.pet-modal-body {
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.pet-modal-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  color: var(--text-dim);
}
.pet-dropzone {
  position: relative;
  display: grid;
  place-items: center;
  min-height: 76px;
  padding: 12px;
  border: 1.5px dashed var(--border);
  border-radius: 10px;
  background: var(--bg2);
  color: var(--text-dim);
  font-size: 12.5px;
  text-align: center;
  cursor: pointer;
}
.pet-dropzone:hover { border-color: var(--accent); }
.pet-dropzone input { display: none; }
.pet-dropzone-files { color: var(--accent); font-weight: 600; word-break: break-all; }
.pet-modal-tip { margin: 0; font-size: 11.5px; color: var(--text-faint); line-height: 1.5; }
.pet-modal-check {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--text);
  cursor: pointer;
}
.pet-modal-check input { accent-color: var(--accent); }
.pet-modal-error { margin: 0; font-size: 12px; color: var(--theme-danger); }
.pet-modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 18px;
  border-top: 1px solid var(--border);
  background: var(--bg2);
}

@media (max-width: 768px) {
  .pet-hero { flex-direction: column; align-items: stretch; }
  .pet-hero-actions { justify-content: flex-end; }
  .pet-section-head { flex-direction: column; }
  .pet-path-row { flex-wrap: wrap; }
  .pet-skin-grid { grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); }
  .pet-chat-field, .pet-chat-field-grow { min-width: 100%; }
}
</style>
