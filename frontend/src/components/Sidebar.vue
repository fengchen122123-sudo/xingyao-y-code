<template>
  <div id="sidebar" data-theme-region="sidebar" :class="{ collapsed: store.sidebarCollapsed }" :style="panelStyle">
    <div class="side-menu">
      <div class="menu-item" @click="newSession()">
        <span class="mi-ico">✎</span>
        <span class="mi-label">新会话</span>
        <span
          class="mi-count"
          :title="store.currentWorkdirLabel"
        >{{ store.currentWorkdirLabel === '对话' ? '对话' : store.currentWorkdirLabel }}</span>
        <span class="mi-arrow">＋</span>
      </div>
      <div class="menu-item" @click="openSkills()">
        <span class="mi-ico">❖</span>
        <span class="mi-label">技能 Skills</span>
        <span class="mi-count">{{ store.skills.length }}</span>
        <span class="mi-arrow">›</span>
      </div>
      <div class="menu-item" @click="openMcp()">
        <span class="mi-ico">⧉</span>
        <span class="mi-label">MCP 服务</span>
        <span class="mi-count">{{ connectedMcpCount }} / {{ store.mcps.length }} 在线</span>
        <span class="mi-arrow">›</span>
      </div>
      <div class="menu-item" @click="openPlugins()">
        <span class="mi-ico">⬡</span>
        <span class="mi-label">插件</span>
        <span class="mi-count">{{ enabledPluginCount }} / {{ store.plugins.length }}</span>
        <span class="mi-arrow">›</span>
      </div>
      <div class="menu-item" @click="openAutomations()">
        <span class="mi-ico"><AppIcon name="orbit" :size="15" /></span>
        <span class="mi-label">星轨</span>
        <span class="mi-count">{{ enabledAutomationCount }} / {{ store.automationTasks.length }}</span>
        <span class="mi-arrow">›</span>
      </div>
    </div>

    <div class="side-section">
      项目会话
      <span>
        <button class="add-btn" title="搜索会话 (Ctrl+F)" style="margin-right:4px" data-test="btn-search-sessions" @click="store.openSessionSearchModal()"><AppIcon name="search" :size="13" /></button>
        <button class="add-btn" title="添加/引入项目文件夹" style="margin-right:4px" @click="store.openWorkspaceFolder()"><AppIcon name="folder-plus" :size="13" /></button>
        <button class="add-btn" title="新建自由通用对话(跳出文件夹)" @click="store.createGlobalSession()">＋</button>
      </span>
    </div>

    <div class="side-scroll">
      <div id="session-list">
        <div v-if="!store.sessionGroups || store.sessionGroups.length === 0" style="padding:10px;color:var(--text-faint);font-size:12px">
          暂无历史会话
        </div>

        <div
          v-for="group in orderedGroups"
          :key="group.folder_path || group.folder_name"
          class="session-group"
          :class="groupDropClass(group)"
          @dragover.prevent="onGroupDragOver($event, group)"
          @drop.prevent="onGroupDrop(group)"
        >
          <div
            class="group-header"
            :draggable="canDragGroup(group)"
            :data-group-path="group.folder_path"
            :title="canDragGroup(group) ? '拖动可调整项目顺序，右键可上移 / 下移 / 置顶' : ''"
            @click="toggleGroupCollapsed(group.folder_path)"
            @contextmenu.prevent.stop="openGroupCtxMenu($event, group)"
            @dragstart="onGroupDragStart($event, group)"
            @dragend="onGroupDragEnd()"
          >
            <span class="g-arrow">{{ collapsedGroups[group.folder_path] ? '▸' : '▾' }}</span>
            <span
              v-if="!group.is_global && group.folder_path !== 'GLOBAL'"
              class="g-folder-ico"
              title="在资源管理器中打开此目录"
              @click.stop="openFolder(group.folder_path)"
            >📁</span>
            <span class="g-title" :style="{ color: group.is_current ? 'var(--accent)' : 'inherit' }">
              {{ getFolderTitle(group) }}
              <span v-if="group.is_current" style="font-size:10px;color:var(--green)"> [当前]</span>
            </span>
            <span class="g-count">{{ group.sessions ? group.sessions.length : 0 }} 个会话</span>
            
            <button 
              v-if="!group.is_global && group.folder_path !== 'GLOBAL'" 
              class="g-add" 
              title="在项目新建会话" 
              @click.stop="store.createSessionInGroup(group.folder_path)"
            >＋</button>
            <button
              v-if="!group.is_global && group.folder_path !== 'GLOBAL' && confirmDeleteGroupPath !== group.folder_path"
              class="g-del"
              title="删除此项目分组及组内对话"
              @click.stop="askDeleteGroup(group.folder_path)"
            >✕</button>
            <button
              v-if="!group.is_global && group.folder_path !== 'GLOBAL' && confirmDeleteGroupPath === group.folder_path"
              class="g-confirm"
              title="点击确认删除分组及组内对话"
              @click.stop="confirmDeleteGroup(group.folder_path, getFolderTitle(group))"
            >确认</button>
          </div>

          <div v-show="!collapsedGroups[group.folder_path]" class="group-sessions">
            <div
              v-for="s in visibleSessions(group)"
              :key="s.name"
              class="session-item"
              :class="[sessionDropClass(group, s), { active: s.name === store.currentSessionName, confirming: confirmDeleteName === s.name }]"
              :draggable="true"
              :data-session-name="s.name"
              title="拖动可调整会话顺序，右键可上移 / 下移 / 置顶"
              @click="onSessionClick(s)"
              @contextmenu.prevent.stop="openCtxMenu($event, s, group)"
              @dragstart="onSessionDragStart($event, group, s)"
              @dragend="onSessionDragEnd()"
              @dragover.prevent="onSessionDragOver($event, group, s)"
              @drop.prevent="onSessionDrop(group, s)"
            >
              <!-- 重命名编辑态：名称位置换成输入框；@click.stop 防止触发会话切换 -->
              <input
                v-if="editingName === s.name"
                :ref="bindRenameInput"
                v-model="editingValue"
                class="s-rename-input"
                data-test="session-rename-input"
                @click.stop
                @mousedown.stop
                @dragstart.stop.prevent
                @keydown.enter="onRenameEnter($event)"
                @keydown.esc="onRenameEsc($event)"
                @blur="commitRename()"
              />
              <div v-else class="t">{{ s.display_name || s.name }}</div>
              <span
                v-if="store.sessionStatusDots[s.name]"
                class="s-dot"
                :class="`s-dot-${store.sessionStatusDots[s.name]}`"
                :title="dotTitle(store.sessionStatusDots[s.name])"
              ></span>
              <span
                v-if="store.runningSessions.includes(s.name)"
                class="s-spinner"
                title="正在执行回合"
              ></span>
              <div class="m">{{ relativeTime(s.updated_at) }}</div>
              <button
                v-if="confirmDeleteName !== s.name"
                class="s-del"
                title="删除此对话"
                @click.stop="askDelete(s.name)"
              >✕</button>
              <button
                v-else
                class="s-confirm"
                title="点击确认删除"
                @click.stop="confirmDelete(s.name)"
              >确认</button>
            </div>
            <div
              v-if="group.sessions && group.sessions.length > 5"
              class="more-toggle"
              @click.stop="toggleShowMore(group.folder_path)"
            >{{ expandedGroups[group.folder_path] ? '收起' : `显示更多 (${group.sessions.length - 5})` }}</div>
          </div>
        </div>
      </div>
    </div>

    <div class="side-footer">
      <button data-test="settings-button" title="打开设置" @click="store.openModelSettings()">
        <span>⚙</span><span class="footer-copy"><b>设置</b></span>
      </button>
      <button class="phone-btn" data-test="remote-bind-button" title="手机远程控制" @click="openRemoteBind()">
        <AppIcon name="smartphone" :size="16" />
      </button>
      <button class="phone-btn" data-test="mobile-ui-button" title="新版移动端界面" @click="openMobileBind()">
        <AppIcon name="gem" :size="16" />
      </button>
    </div>
  </div>

  <!-- 会话右键菜单：fixed 浮层，跟随鼠标坐标并做视口边缘收拢 -->
  <div
    v-if="ctxMenu.show"
    ref="ctxMenuEl"
    class="session-ctx-menu"
    data-test="session-ctx-menu"
    :style="{ left: `${ctxMenu.x}px`, top: `${ctxMenu.y}px` }"
    @contextmenu.prevent
    @click.stop
  >
    <div
      class="ctx-item"
      :class="{ disabled: ctxSessionIndex <= 0 }"
      data-test="ctx-move-up"
      @click="runCtxMenu('up')"
    >上移</div>
    <div
      class="ctx-item"
      :class="{ disabled: ctxSessionIndex < 0 || ctxSessionIndex >= ctxSessionCount - 1 }"
      data-test="ctx-move-down"
      @click="runCtxMenu('down')"
    >下移</div>
    <div
      class="ctx-item"
      :class="{ disabled: ctxSessionIndex <= 0 }"
      data-test="ctx-move-top"
      @click="runCtxMenu('top')"
    >置顶</div>
    <div class="ctx-sep"></div>
    <div class="ctx-item" data-test="ctx-rename" @click="startRename()">重命名</div>
    <div class="ctx-item danger" data-test="ctx-delete" @click="startDeleteFromMenu()">删除</div>
  </div>

  <!-- 项目分组右键菜单：排序入口（拖拽之外的确定性操作，首/末项自动置灰） -->
  <div
    v-if="groupMenu.show"
    ref="groupMenuEl"
    class="session-ctx-menu"
    data-test="group-ctx-menu"
    :style="{ left: `${groupMenu.x}px`, top: `${groupMenu.y}px` }"
    @contextmenu.prevent
    @click.stop
  >
    <div
      class="ctx-item"
      :class="{ disabled: groupMenuIndex <= 0 }"
      data-test="group-move-up"
      @click="runGroupMenu('up')"
    >上移</div>
    <div
      class="ctx-item"
      :class="{ disabled: groupMenuIndex < 0 || groupMenuIndex >= groupMenuCount - 1 }"
      data-test="group-move-down"
      @click="runGroupMenu('down')"
    >下移</div>
    <div
      class="ctx-item"
      :class="{ disabled: groupMenuIndex <= 0 }"
      data-test="group-move-top"
      @click="runGroupMenu('top')"
    >置顶</div>
  </div>

  <!-- 移动端侧栏遮罩：侧栏展开时盖住主区，点击收起（桌面端由 CSS 隐藏） -->
  <div
    v-if="!store.sidebarCollapsed"
    class="sidebar-backdrop"
    data-test="sidebar-backdrop"
    @click="store.toggleSidebar()"
  ></div>

  <div v-if="remoteBind" class="modal-mask" data-test="remote-bind-modal">
    <div class="modal">
      <div class="modal-head">
        <div class="mh-ico"><AppIcon name="smartphone" :size="18" /></div>
        <div>
          <h3>{{ remoteBindMode === 'mobile' ? '新版移动端界面' : '手机远程控制' }}</h3>
          <p>{{ remoteBindMode === 'mobile' ? 'co-team 风格独立移动端，与经典界面互不影响' : '用手机扫码即可作为第二个前端控制本机' }}</p>
        </div>
      </div>
      <div class="modal-body">
        <img class="qr-img" :src="remoteBind.qr" alt="绑定二维码" />
        <div class="bind-url">{{ remoteBind.url }}</div>
        <div class="bind-hint" data-test="remote-bind-hint">
          {{ remoteBindRefreshing ? '正在刷新配对链接…' : `链接有效期：${formatExpiry(remoteBind.expires_in)}，请在过期前扫码` }}
        </div>
      </div>
      <div class="modal-foot">
        <button class="btn" data-test="remote-bind-refresh" :disabled="remoteBindRefreshing" @click="refreshRemoteBind()">刷新</button>
        <button class="btn primary" data-test="remote-bind-close" @click="closeRemoteBind()">关闭</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, reactive, nextTick, onMounted, onUnmounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';
import type { SessionGroup, SessionInfo } from '../types/pywebview';
import { useIsMobile } from '../composables/useViewport';

const store = useAppStore();

// 桌面端宽度由拖拽手柄写入 store，此处内联生效；折叠负 margin 跟随实际
// 宽度。移动端（<=768px）不输出内联样式，布局交给 style.css 媒体查询。
const isMobile = useIsMobile();
const panelStyle = computed(() => {
  if (isMobile.value) return {};
  const style: Record<string, string> = { width: `${store.sidebarWidth}px` };
  if (store.sidebarCollapsed) style.marginLeft = `-${store.sidebarWidth}px`;
  return style;
});

// 分组折叠态 / 「显示更多」展开态持久化：以 folder_path 为键存 localStorage，
// 沿用 appStore 与 usePanelResize 的 'ycode-*' 前缀约定。只记录与默认状态
// 不同的目录，恢复默认即删键，避免项目移除后旧路径在存储里无限残留。
const COLLAPSED_GROUPS_KEY = 'ycode-sidebar-collapsed-groups';
const EXPANDED_GROUPS_KEY = 'ycode-sidebar-expanded-groups';

/** 读取一组被标记的路径（[{path}] 形态）；内容缺失/损坏时按空处理 */
function loadFlagPaths(key: string): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return {};
    return Object.fromEntries(
      parsed.filter((p): p is string => typeof p === 'string').map((p) => [p, true]),
    );
  } catch {
    return {};
  }
}

/** 写回被标记的路径；全部恢复默认时删键（存储不可用时仅本次会话生效） */
function saveFlagPaths(key: string, flags: Record<string, boolean>) {
  try {
    const paths = Object.keys(flags).filter((p) => flags[p]);
    if (paths.length === 0) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(paths));
  } catch {
    /* localStorage 不可用（隐私模式/配额满）时静默降级 */
  }
}

const collapsedGroups = ref<Record<string, boolean>>(loadFlagPaths(COLLAPSED_GROUPS_KEY));
const expandedGroups = ref<Record<string, boolean>>(loadFlagPaths(EXPANDED_GROUPS_KEY));

// 分组自定义顺序持久化：只存非 GLOBAL 的 folder_path 数组。顺序是纯展示层
// 关注点，故不写回后端 sessionGroups——KimiEmptyState / AutomationPane 等
// 消费方拿到的事实数据保持不变。未记录的目录（新引入的项目）追加到末尾，
// 并保持后端给出的字母序，避免引入新项目时列表整体跳动。
const GROUP_ORDER_KEY = 'ycode-sidebar-group-order';

/** 读取用户自定义顺序；内容缺失/损坏时按"未自定义"处理 */
function loadGroupOrder(): string[] {
  try {
    const raw = localStorage.getItem(GROUP_ORDER_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((p): p is string => typeof p === 'string' && !!p);
  } catch {
    return [];
  }
}

/** 写回自定义顺序；清空即删键（存储不可用时仅本次会话生效） */
function saveGroupOrder(paths: string[]) {
  try {
    if (paths.length === 0) localStorage.removeItem(GROUP_ORDER_KEY);
    else localStorage.setItem(GROUP_ORDER_KEY, JSON.stringify(paths));
  } catch {
    /* localStorage 不可用（隐私模式/配额满）时静默降级 */
  }
}

/** 「对话」分组固定最顶部且不可排序（与后端 get_sessions 的语义保持一致） */
function isGlobalGroup(g: SessionGroup | null | undefined): boolean {
  return !!g && (!!g.is_global || g.folder_path === 'GLOBAL');
}

const groupOrder = ref<string[]>(loadGroupOrder());

/** 展示顺序：GLOBAL 恒定在前，项目分组按自定义顺序，未记录的排在最后 */
const orderedGroups = computed<SessionGroup[]>(() => {
  const groups = store.sessionGroups || [];
  const globals = groups.filter((g) => isGlobalGroup(g));
  const projects = groups.filter((g) => !isGlobalGroup(g));
  const rank = new Map<string, number>();
  groupOrder.value.forEach((p, i) => {
    if (!rank.has(p)) rank.set(p, i);
  });
  // Array.sort 稳定：两个未记录的分组比较结果为 0，保持后端字母序
  const ranked = projects.slice().sort((a, b) => {
    const ra = rank.get(a.folder_path);
    const rb = rank.get(b.folder_path);
    if (ra === undefined && rb === undefined) return 0;
    if (ra === undefined) return 1;
    if (rb === undefined) return -1;
    return ra - rb;
  });
  return [...globals, ...ranked];
});

/** 当前展示顺序下的项目路径列表（作为写回持久化的基准） */
function currentProjectOrder(): string[] {
  return orderedGroups.value.filter((g) => !isGlobalGroup(g)).map((g) => g.folder_path);
}

function commitGroupOrder(paths: string[]) {
  groupOrder.value = paths.slice();
  saveGroupOrder(paths);
  pushLayoutToBackend();
}

/** 把 fromKey 移到 toKey 前/后：先摘除再按目标重新定位，天然吸收前后偏移。
 *  分组与会话两处排序共用；无法移动（缺键 / 移到自己身上）时返回 null。 */
function moveBeforeOrAfter(
  list: string[],
  fromKey: string,
  toKey: string,
  after: boolean,
): string[] | null {
  if (!fromKey || !toKey || fromKey === toKey) return null;
  const from = list.indexOf(fromKey);
  if (from < 0 || list.indexOf(toKey) < 0) return null;
  const next = list.slice();
  const [moved] = next.splice(from, 1);
  const target = next.indexOf(toKey);
  if (target < 0) return null;
  next.splice(after ? target + 1 : target, 0, moved);
  return next;
}

function moveGroupTo(fromPath: string, toPath: string, after: boolean) {
  const next = moveBeforeOrAfter(currentProjectOrder(), fromPath, toPath, after);
  if (!next) return;
  commitGroupOrder(next);
}

/** 相邻移动（delta=±1）与置顶：右键菜单的确定性操作 */
function moveGroupStep(path: string, delta: number) {
  const paths = currentProjectOrder();
  const i = paths.indexOf(path);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= paths.length) return;
  [paths[i], paths[j]] = [paths[j], paths[i]];
  commitGroupOrder(paths);
}

function moveGroupTop(path: string) {
  const paths = currentProjectOrder();
  const i = paths.indexOf(path);
  if (i <= 0) return;
  const [moved] = paths.splice(i, 1);
  paths.unshift(moved);
  commitGroupOrder(paths);
}

// 分组内会话的自定义顺序：按 folder_path 分桶存会话名数组。已记录的按用户
// 顺序排列，未记录的（新会话）保持后端时间序排在最前——分组内默认只展示 5
// 条，新会话若沉到末尾会直接看不见。会话名是稳定标识：重命名只改
// display_name，不换 name，所以顺序记录不会因改名失效。
const SESSION_ORDER_KEY = 'ycode-sidebar-session-order';

/** 读取会话顺序分桶；内容缺失/损坏时按"未自定义"处理 */
function loadSessionOrder(): Record<string, string[]> {
  try {
    const raw = localStorage.getItem(SESSION_ORDER_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    const out: Record<string, string[]> = {};
    for (const [path, names] of Object.entries(parsed as Record<string, unknown>)) {
      if (!path || !Array.isArray(names)) continue;
      const kept = names.filter((n): n is string => typeof n === 'string' && !!n);
      if (kept.length) out[path] = kept;
    }
    return out;
  } catch {
    return {};
  }
}

/** 写回会话顺序分桶；分组无自定义顺序即删桶（存储不可用时仅本次会话生效） */
function saveSessionOrder(map: Record<string, string[]>) {
  try {
    const paths = Object.keys(map).filter((p) => map[p] && map[p].length);
    if (paths.length === 0) {
      localStorage.removeItem(SESSION_ORDER_KEY);
      return;
    }
    const out: Record<string, string[]> = {};
    for (const p of paths) out[p] = map[p];
    localStorage.setItem(SESSION_ORDER_KEY, JSON.stringify(out));
  } catch {
    /* localStorage 不可用（隐私模式/配额满）时静默降级 */
  }
}

const sessionOrder = ref<Record<string, string[]>>(loadSessionOrder());

/** 把当前排序推给后端（权威副本）。
 *
 * localStorage 只是即时缓存：WebView2 在「拖完排序立刻重启」时会丢写——
 * 写前日志尾部留下 checksum mismatch，leveldb 恢复时整段丢弃，表现为
 * "数据写进文件了但应用读不到、重启就复原"。后端走原子写落盘才可靠。
 * 失败仅静默降级，不影响交互。
 */
function pushLayoutToBackend() {
  const api = (window.pywebview && window.pywebview.api) as any;
  if (!api || typeof api.save_ui_layout !== 'function') return;
  try {
    const ret = api.save_ui_layout({
      group_order: groupOrder.value.slice(),
      session_order: { ...sessionOrder.value },
    });
    if (ret && typeof ret.catch === 'function') ret.catch(() => {});
  } catch {
    /* 后端不可用时保持 localStorage 单通道 */
  }
}

/** 启动时用后端快照校正本地缓存。
 *
 * 后端为空快照（首次升级到带后端持久化的版本）时反向补写一次，把用户此前
 * 存在 localStorage 里的排序带过去，避免升级即丢历史。
 */
async function syncLayoutFromBackend() {
  const api = (window.pywebview && window.pywebview.api) as any;
  if (!api || typeof api.get_ui_layout !== 'function') return;
  try {
    const res = await api.get_ui_layout();
    if (!res || res.ok === false) return;
    const groups = Array.isArray(res.group_order) ? res.group_order : [];
    const sessions =
      res.session_order && typeof res.session_order === 'object' ? res.session_order : {};
    if (groups.length === 0 && Object.keys(sessions).length === 0) {
      if (groupOrder.value.length || Object.keys(sessionOrder.value).length) {
        pushLayoutToBackend();
      }
      return;
    }
    if (groups.length) {
      groupOrder.value = groups;
      saveGroupOrder(groups);
    }
    if (Object.keys(sessions).length) {
      sessionOrder.value = sessions;
      saveSessionOrder(sessions);
    }
  } catch {
    /* 静默降级：本地缓存仍可用 */
  }
}

// pywebview 的 JS 桥分两步注入：先给 window.pywebview.api 一个空对象，再由
// finish.js 填入方法并触发 pywebviewready。若在空对象阶段抢跑，get_ui_layout
// 还不存在会被静默跳过——表现为「后端快照明明写好了，重启后却没被应用」。
// 因此沿用 App.vue 的同一套模式：就绪探测 + 事件 + 轮询兜底 + 互斥标志。
let layoutSyncStarted = false;
let layoutSyncTimer: number | undefined;

function pywebviewLayoutApiReady(): boolean {
  const api = window.pywebview && window.pywebview.api;
  return !!(api && typeof (api as any).get_ui_layout === 'function');
}

function startLayoutSync() {
  if (layoutSyncStarted) return;
  layoutSyncStarted = true;
  window.removeEventListener('pywebviewready', onLayoutBridgeReady);
  if (layoutSyncTimer !== undefined) {
    clearInterval(layoutSyncTimer);
    layoutSyncTimer = undefined;
  }
  syncLayoutFromBackend();
}

function onLayoutBridgeReady() {
  startLayoutSync();
}

/** 分组内展示顺序：已记录的按用户顺序，未记录的（新会话）保持原顺序排最前 */
function orderedSessions(group: SessionGroup): SessionInfo[] {
  const list = group.sessions || [];
  const saved = sessionOrder.value[group.folder_path];
  if (!saved || saved.length === 0) return list;
  const rank = new Map<string, number>();
  saved.forEach((n, i) => {
    if (!rank.has(n)) rank.set(n, i);
  });
  const fresh = list.filter((s) => !rank.has(s.name));
  const ranked = list
    .filter((s) => rank.has(s.name))
    .sort((a, b) => rank.get(a.name)! - rank.get(b.name)!);
  return [...fresh, ...ranked];
}

/** 当前分组内的会话名顺序（作为写回持久化的基准） */
function currentSessionOrder(path: string): string[] {
  const group = (store.sessionGroups || []).find((g) => g.folder_path === path);
  return group ? orderedSessions(group).map((s) => s.name) : [];
}

function commitSessionOrder(path: string, names: string[]) {
  if (!path) return;
  const next = { ...sessionOrder.value };
  if (names.length) next[path] = names.slice();
  else delete next[path];
  sessionOrder.value = next;
  saveSessionOrder(next);
  pushLayoutToBackend();
}

function moveSessionTo(path: string, fromName: string, toName: string, after: boolean) {
  const next = moveBeforeOrAfter(currentSessionOrder(path), fromName, toName, after);
  if (!next) return;
  commitSessionOrder(path, next);
}

function moveSessionStep(path: string, name: string, delta: number) {
  const names = currentSessionOrder(path);
  const i = names.indexOf(name);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= names.length) return;
  [names[i], names[j]] = [names[j], names[i]];
  commitSessionOrder(path, names);
}

function moveSessionTop(path: string, name: string) {
  const names = currentSessionOrder(path);
  const i = names.indexOf(name);
  if (i <= 0) return;
  const [moved] = names.splice(i, 1);
  names.unshift(moved);
  commitSessionOrder(path, names);
}

// 会话拖拽态：dragSessionPath/Name 为源，dropSession* 为同组内的落点
const dragSessionPath = ref('');
const dragSessionName = ref('');
const dropSessionPath = ref('');
const dropSessionName = ref('');
const dropSessionAfter = ref(false);

function sessionDropClass(group: SessionGroup, s: SessionInfo) {
  const sameGroup = dropSessionPath.value === group.folder_path;
  return {
    dragging: dragSessionName.value === s.name,
    'drop-before': sameGroup && dropSessionName.value === s.name && !dropSessionAfter.value,
    'drop-after': sameGroup && dropSessionName.value === s.name && dropSessionAfter.value,
  };
}

function onSessionDragStart(e: DragEvent, group: SessionGroup, s: SessionInfo) {
  dragSessionPath.value = group.folder_path;
  dragSessionName.value = s.name;
  dropSessionName.value = '';
  if (e.dataTransfer) {
    e.dataTransfer.effectAllowed = 'move';
    try {
      e.dataTransfer.setData('text/plain', s.name);
    } catch {
      /* 同分组头：排序不依赖数据内容，忽略即可 */
    }
  }
}

function onSessionDragOver(e: DragEvent, group: SessionGroup, s: SessionInfo) {
  if (!dragSessionName.value) return;
  // 会话归属由 workdir 决定，跨分组拖拽等于改项目归属，故不支持（给出禁止光标）
  if (group.folder_path !== dragSessionPath.value) {
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'none';
    return;
  }
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
  dropSessionPath.value = group.folder_path;
  dropSessionName.value = s.name;
  dropSessionAfter.value = e.clientY > rect.top + rect.height / 2;
}

function onSessionDrop(group: SessionGroup, s: SessionInfo) {
  const from = dragSessionName.value;
  const path = dragSessionPath.value;
  const after = dropSessionAfter.value;
  onSessionDragEnd();
  if (!from || path !== group.folder_path) return;
  moveSessionTo(path, from, s.name, after);
}

function onSessionDragEnd() {
  dragSessionPath.value = '';
  dragSessionName.value = '';
  dropSessionPath.value = '';
  dropSessionName.value = '';
  dropSessionAfter.value = false;
}

// 拖拽排序态：dragPath 为被拖分组，dropPath/dropAfter 为落点及其前后半区
const dragPath = ref('');
const dropPath = ref('');
const dropAfter = ref(false);

function canDragGroup(g: SessionGroup): boolean {
  return !isGlobalGroup(g);
}

function groupDropClass(g: SessionGroup) {
  return {
    dragging: dragPath.value === g.folder_path,
    'drop-before': dropPath.value === g.folder_path && !dropAfter.value,
    'drop-after': dropPath.value === g.folder_path && dropAfter.value,
  };
}

function onGroupDragStart(e: DragEvent, g: SessionGroup) {
  if (!canDragGroup(g)) {
    e.preventDefault();
    return;
  }
  dragPath.value = g.folder_path;
  dropPath.value = '';
  if (e.dataTransfer) {
    e.dataTransfer.effectAllowed = 'move';
    try {
      e.dataTransfer.setData('text/plain', g.folder_path);
    } catch {
      /* 个别内核下 dataTransfer 只读；排序不依赖数据内容，忽略即可 */
    }
  }
}

/** 落点判定用分组头中线：上半区插到目标前，下半区插到目标后 */
function onGroupDragOver(e: DragEvent, g: SessionGroup) {
  if (!dragPath.value || isGlobalGroup(g)) return;
  const box = e.currentTarget as HTMLElement | null;
  const header = box ? box.querySelector('.group-header') : null;
  const rect = (header || box)!.getBoundingClientRect();
  dropPath.value = g.folder_path;
  dropAfter.value = e.clientY > rect.top + rect.height / 2;
}

// drop 只消费 dragover 阶段算好的落点（dropAfter），不需要事件对象
function onGroupDrop(g: SessionGroup) {
  if (!dragPath.value || isGlobalGroup(g)) {
    onGroupDragEnd();
    return;
  }
  const from = dragPath.value;
  const after = dropAfter.value;
  onGroupDragEnd();
  moveGroupTo(from, g.folder_path, after);
}

function onGroupDragEnd() {
  dragPath.value = '';
  dropPath.value = '';
  dropAfter.value = false;
}
// 正在等待二次确认删除的会话名（在时间位置显示红色小确认按钮）
const confirmDeleteName = ref('');
// 正在等待二次确认删除的项目分组路径
const confirmDeleteGroupPath = ref('');
// 手机远程控制绑定信息（点击手机图标后弹出二维码）
// permanent=true 表示永久有效（后端 expires_in=0），不做倒计时自动刷新
const remoteBind = ref<null | { url: string; qr: string; expires_in: number; permanent?: boolean }>(null);
// 配对链接刷新中标记：刷新期间禁用按钮避免重复点击
const remoteBindRefreshing = ref(false);
// 绑定目标：desktop=经典界面（桌面 dist 响应式版）/ mobile=独立移动端（/m/ 路径）
const remoteBindMode = ref<'desktop' | 'mobile'>('desktop');
// 过期检查定时器：弹窗打开期间每秒检测，过期自动刷新 token
let remoteBindExpiryTimer: ReturnType<typeof setInterval> | null = null;

// 会话右键菜单：show 为浮层开关，x/y 为鼠标坐标（渲染后按视口边缘收拢）
const MENU_EDGE = 8;
const ctxMenu = reactive({ show: false, x: 0, y: 0, name: '', display: '', path: '' });
const ctxMenuEl = ref<HTMLElement | null>(null);
// 就地重命名态：editingName 为会话名（唯一定位条目），editingValue 为输入框内容
const editingName = ref('');
const editingValue = ref('');
// 进入编辑态时的原始显示名，用于判断"未改动"
let editingOriginal = '';
const renameInputEl = ref<HTMLInputElement | null>(null);

function openCtxMenu(e: MouseEvent, s: SessionInfo, group: SessionGroup) {
  cancelRename();
  ctxMenu.name = s.name;
  ctxMenu.display = s.display_name || s.name;
  // 记下所属分组：菜单里的上移 / 下移 / 置顶要按组内顺序计算
  ctxMenu.path = group.folder_path;
  ctxMenu.x = e.clientX;
  ctxMenu.y = e.clientY;
  ctxMenu.show = true;
  // 菜单尺寸要等渲染后才知道，故下一帧再收拢到视口内
  nextTick(clampCtxMenu);
}

/** 把浮层坐标收拢进视口（尺寸要等渲染后才知道，故调用方放在 nextTick 里） */
function clampToViewport(el: HTMLElement | null, x: number, y: number) {
  const width = el ? el.offsetWidth : 0;
  const height = el ? el.offsetHeight : 0;
  const maxX = Math.max(MENU_EDGE, window.innerWidth - width - MENU_EDGE);
  const maxY = Math.max(MENU_EDGE, window.innerHeight - height - MENU_EDGE);
  return {
    x: Math.min(Math.max(MENU_EDGE, x), maxX),
    y: Math.min(Math.max(MENU_EDGE, y), maxY),
  };
}

function clampCtxMenu() {
  const { x, y } = clampToViewport(ctxMenuEl.value, ctxMenu.x, ctxMenu.y);
  ctxMenu.x = x;
  ctxMenu.y = y;
}

function closeCtxMenu() {
  ctxMenu.show = false;
  ctxMenu.name = '';
  ctxMenu.display = '';
  ctxMenu.path = '';
}

// 菜单项「重命名」：关菜单、就地进入编辑态，并把输入框聚焦全选
function startRename() {
  if (!ctxMenu.name) return;
  editingName.value = ctxMenu.name;
  editingValue.value = ctxMenu.display;
  editingOriginal = ctxMenu.display;
  closeCtxMenu();
  nextTick(() => {
    renameInputEl.value?.focus();
    renameInputEl.value?.select();
  });
}

// 菜单项「删除」：复用现有二次确认流程（进入 confirming 态，仍需点红色确认按钮）
function startDeleteFromMenu() {
  const name = ctxMenu.name;
  cancelRename();
  closeCtxMenu();
  if (name) askDelete(name);
}

// 会话菜单里的排序项：与分组排序同一套语义（首个不能上移/置顶，末个不能下移）
const ctxSessionIndex = computed(() =>
  ctxMenu.show ? currentSessionOrder(ctxMenu.path).indexOf(ctxMenu.name) : -1,
);
const ctxSessionCount = computed(() =>
  ctxMenu.show ? currentSessionOrder(ctxMenu.path).length : 0,
);

function runCtxMenu(action: 'up' | 'down' | 'top') {
  const { path, name } = ctxMenu;
  closeCtxMenu();
  if (!path || !name) return;
  if (action === 'up') moveSessionStep(path, name, -1);
  else if (action === 'down') moveSessionStep(path, name, 1);
  else moveSessionTop(path, name);
}

// 分组右键菜单：拖拽之外的排序入口（触屏/手抖时更可靠，也便于长距离调整）
const groupMenu = reactive({ show: false, x: 0, y: 0, path: '' });
const groupMenuEl = ref<HTMLElement | null>(null);

function openGroupCtxMenu(e: MouseEvent, g: SessionGroup) {
  // 「对话」分组固定最顶，不提供排序菜单
  if (isGlobalGroup(g)) return;
  cancelRename();
  closeCtxMenu();
  groupMenu.path = g.folder_path;
  groupMenu.x = e.clientX;
  groupMenu.y = e.clientY;
  groupMenu.show = true;
  nextTick(clampGroupMenu);
}

function clampGroupMenu() {
  const { x, y } = clampToViewport(groupMenuEl.value, groupMenu.x, groupMenu.y);
  groupMenu.x = x;
  groupMenu.y = y;
}

function closeGroupMenu() {
  groupMenu.show = false;
  groupMenu.path = '';
}

// 菜单项可用性：首个项目不能上移/置顶，末个项目不能下移
const groupMenuIndex = computed(() => currentProjectOrder().indexOf(groupMenu.path));
const groupMenuCount = computed(() => currentProjectOrder().length);

function runGroupMenu(action: 'up' | 'down' | 'top') {
  const path = groupMenu.path;
  closeGroupMenu();
  if (!path) return;
  if (action === 'up') moveGroupStep(path, -1);
  else if (action === 'down') moveGroupStep(path, 1);
  else moveGroupTop(path);
}

// v-for 内若用字符串 ref，Vue 会把值收集成数组；改用函数 ref 拿到唯一输入框
function bindRenameInput(el: any) {
  renameInputEl.value = (el as HTMLInputElement) || null;
}

function cancelRename() {
  editingName.value = '';
  editingValue.value = '';
  editingOriginal = '';
}

// IME 组合态守卫：中文/日文输入法下，确认候选词的那次 Enter（或取消候选词的
// Esc）会先冒泡到 keydown，此刻文字尚未上屏——isComposing 为 true，Chrome 等
// 浏览器还会退化为 keyCode 229。若在此刻直接提交，editingValue 仍是旧值，会被
// 「与原名相同」判断静默丢弃，重命名请求根本发不出去。故组合态必须先拦下，
// 让输入法自己处理上屏 / 取消。
function isImeComposing(e: KeyboardEvent): boolean {
  return e.isComposing === true || (e as any).keyCode === 229;
}

// Enter：组合中不 preventDefault、不提交（交由输入法上屏）；非组合态才阻止
// 默认换行行为并提交重命名
function onRenameEnter(e: KeyboardEvent) {
  if (isImeComposing(e)) return;
  e.preventDefault();
  commitRename();
}

// Esc：组合中是取消候选词，不应退出编辑态；非组合态才取消重命名
function onRenameEsc(e: KeyboardEvent) {
  if (isImeComposing(e)) return;
  e.preventDefault();
  cancelRename();
}

// Enter / 失焦落到同一处：先退出编辑态再发请求，避免后续 blur 重复触发
function commitRename() {
  const oldName = editingName.value;
  if (!oldName) return;
  const next = editingValue.value.trim();
  const unchanged = next === editingOriginal;
  cancelRename();
  if (!next || unchanged) return;
  store.renameSession(oldName, next);
}

// 会话菜单与分组菜单互斥，统一收口关闭
function closeSidebarMenus() {
  closeCtxMenu();
  closeGroupMenu();
}

// 菜单关闭时机：点击菜单外任意位置（点在菜单内不关，避免抢在 click 之前移除节点）
function onDocMouseDown(e: MouseEvent) {
  if (!ctxMenu.show && !groupMenu.show) return;
  const el = ctxMenu.show ? ctxMenuEl.value : groupMenuEl.value;
  if (el && e.target instanceof Node && el.contains(e.target)) return;
  closeSidebarMenus();
}

function onDocKeyDown(e: KeyboardEvent) {
  // 组合态 Esc 属于「取消候选词」，冒泡到这里时不能顺带退出编辑态 / 关菜单
  if (isImeComposing(e)) return;
  if (e.key === 'Escape') {
    closeSidebarMenus();
    cancelRename();
  }
}

onMounted(() => {
  // 捕获阶段监听 scroll：侧栏内部滚动也能关闭菜单
  window.addEventListener('scroll', closeSidebarMenus, true);
  window.addEventListener('resize', closeSidebarMenus);
  document.addEventListener('mousedown', onDocMouseDown);
  document.addEventListener('keydown', onDocKeyDown);

  // 用后端权威快照校正本地缓存（localStorage 在强杀场景会丢写）。
  // 必须等桥真正就绪：api 对象先注入、方法后注入，抢跑会静默失败。
  if (pywebviewLayoutApiReady()) {
    startLayoutSync();
  } else {
    window.addEventListener('pywebviewready', onLayoutBridgeReady);
    let attempts = 0;
    layoutSyncTimer = window.setInterval(() => {
      attempts += 1;
      if (pywebviewLayoutApiReady()) {
        startLayoutSync();
      } else if (attempts > 300) {
        // 30 秒仍未就绪（纯浏览器预览等无桥环境）：放弃轮询，
        // localStorage 缓存照常可用，不影响排序本身
        clearInterval(layoutSyncTimer);
        layoutSyncTimer = undefined;
        window.removeEventListener('pywebviewready', onLayoutBridgeReady);
      }
    }, 100);
  }
});

onUnmounted(() => {
  window.removeEventListener('scroll', closeSidebarMenus, true);
  window.removeEventListener('resize', closeSidebarMenus);
  document.removeEventListener('mousedown', onDocMouseDown);
  document.removeEventListener('keydown', onDocKeyDown);
  window.removeEventListener('pywebviewready', onLayoutBridgeReady);
  if (layoutSyncTimer !== undefined) {
    clearInterval(layoutSyncTimer);
    layoutSyncTimer = undefined;
  }
});

function askDeleteGroup(path: string) {
  confirmDeleteGroupPath.value = path;
}

function confirmDeleteGroup(path: string, title: string) {
  confirmDeleteGroupPath.value = '';
  store.deleteFolderGroup(path, title);
}

function askDelete(name: string) {
  confirmDeleteName.value = name;
}

function confirmDelete(name: string) {
  confirmDeleteName.value = '';
  store.deleteSingleSession(name);
}

function onSessionClick(s: { name: string; display_name?: string; workdir?: string }) {
  // 处于确认删除状态时，点击条目其它区域视为取消，不切换会话
  if (confirmDeleteName.value === s.name) {
    confirmDeleteName.value = '';
    return;
  }
  confirmDeleteName.value = '';
  // 切换会话前清理右键菜单与重命名编辑态（两者互斥，且不随会话保留）
  closeCtxMenu();
  cancelRename();
  store.closeSubpage();
  store.switchSession(s.name, s.display_name || s.name, s.workdir);
  collapseSidebarIfMobile();
}

// 移动端视口判定：<=768px 时侧栏为覆盖层，点遮罩 / 选择会话 / 点击菜单后收起
function isMobileViewport(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(max-width: 768px)').matches
  );
}

function collapseSidebarIfMobile() {
  if (isMobileViewport()) {
    store.sidebarCollapsed = true;
  }
}

// 侧栏菜单项：触发动作后，移动端自动收起覆盖层
function newSession() {
  store.closeSubpage();
  store.createSessionSmart();
  collapseSidebarIfMobile();
}
function openSkills() {
  store.openSubpage('skills');
  collapseSidebarIfMobile();
}
function openMcp() {
  store.openSubpage('mcp');
  collapseSidebarIfMobile();
}
function openPlugins() {
  store.openSubpage('plugins');
  collapseSidebarIfMobile();
}
function openAutomations() {
  store.openSubpage('automations');
  collapseSidebarIfMobile();
}

// 一个目录下超过 5 条会话时默认折叠，点“显示更多”再展开全部。
// 取前 5 条必须基于自定义顺序，否则置顶的会话会被切掉
function visibleSessions(group: SessionGroup) {
  const list = orderedSessions(group);
  if (expandedGroups.value[group.folder_path] || list.length <= 5) return list;
  return list.slice(0, 5);
}

function toggleShowMore(path: string) {
  expandedGroups.value[path] = !expandedGroups.value[path];
  saveFlagPaths(EXPANDED_GROUPS_KEY, expandedGroups.value);
}

// ZCode 风格的紧凑相对时间：刚刚 / 5分钟 / 3小时 / 13天 / 2个月
function relativeTime(updatedAt?: string) {
  if (!updatedAt) return '';
  const t = new Date(updatedAt.replace(' ', 'T')).getTime();
  if (isNaN(t)) return '';
  const diff = Math.max(0, Date.now() - t);
  const min = Math.floor(diff / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min}分钟`;
  const hour = Math.floor(min / 60);
  if (hour < 24) return `${hour}小时`;
  const day = Math.floor(hour / 24);
  if (day < 30) return `${day}天`;
  const mon = Math.floor(day / 30);
  if (mon < 12) return `${mon}个月`;
  return `${Math.floor(mon / 12)}年`;
}
const connectedMcpCount = computed(() => store.mcps.filter(m => m.connected).length);
const enabledPluginCount = computed(() => store.plugins.filter(p => p.enabled).length);
const enabledAutomationCount = computed(() => store.automationTasks.filter(t => t.enabled).length);

function toggleGroupCollapsed(path: string) {
  // 处于确认删除状态时，点击分组头其它区域视为取消，不折叠
  if (confirmDeleteGroupPath.value === path) {
    confirmDeleteGroupPath.value = '';
    return;
  }
  confirmDeleteGroupPath.value = '';
  collapsedGroups.value[path] = !collapsedGroups.value[path];
  saveFlagPaths(COLLAPSED_GROUPS_KEY, collapsedGroups.value);
}

// 点击分组文件夹图标：在系统资源管理器中打开该目录
async function openFolder(path: string) {
  const api = window.pywebview && window.pywebview.api;
  if (!api || !path) return;
  try {
    const res = await api.open_folder_in_explorer(path);
    if (res && res.ok === false && res.msg) {
      store.notifyModal('打开目录失败', res.msg);
    }
  } catch (err) {
    store.notifyModal('打开目录失败', String(err));
  }
}

// 打开手机远程控制绑定弹窗：向后端请求二维码与配对链接。
// 后端 token 固定：未过期直接复用，应用重启也不变；过期由后端自动重签。
async function fetchRemoteBind(forceNew: boolean) {
  const api = window.pywebview && window.pywebview.api;
  if (!api) {
    store.notifyModal('手机远程控制', '后端连接失败：pywebview API 不可用');
    return;
  }
  try {
    // get_remote_bind_qr 为后端桥接方法，前端类型契约尚未收录，故此处按返回结构取用；
    // mobile 模式追加第二参数绑定独立移动端（/m/），桌面模式保持单参签名不变
    const result = remoteBindMode.value === 'mobile'
      ? await (api as any).get_remote_bind_qr(forceNew, true)
      : await (api as any).get_remote_bind_qr(forceNew);
    if (result && result.ok && result.qr && result.url) {
      remoteBind.value = {
        url: result.url,
        qr: result.qr,
        // expires_in=0：永久有效（TTL 选“永久”时后端永不过期）
        expires_in: result.expires_in ?? 0,
        permanent: !(result.expires_in > 0),
      };
      startExpiryWatch();
    } else if (!forceNew || !remoteBind.value) {
      store.notifyModal('手机远程控制', (result && result.msg) || '获取绑定二维码失败');
    } else {
      // 手动刷新失败时保留旧弹窗内容，仅提示
      store.notifyModal('手机远程控制', (result && result.msg) || '刷新配对链接失败');
    }
  } catch (err) {
    store.notifyModal('手机远程控制', String(err));
  } finally {
    remoteBindRefreshing.value = false;
  }
}

async function openRemoteBind() {
  remoteBindMode.value = 'desktop';
  await fetchRemoteBind(false);
}

// 新版移动端入口：与经典界面并列的第二个手机端，共用配对通道
async function openMobileBind() {
  remoteBindMode.value = 'mobile';
  await fetchRemoteBind(false);
}

// 手动刷新：强制后端重签 token，已绑定的旧链接立即失效
async function refreshRemoteBind() {
  if (remoteBindRefreshing.value) return;
  remoteBindRefreshing.value = true;
  await fetchRemoteBind(true);
}

// 弹窗打开期间每秒检测剩余有效期，到期自动向后端请求新 token（未过期时后端会直接复用）
function startExpiryWatch() {
  stopExpiryWatch();
  // 永久有效（expires_in=0）无需倒计时，也就永远不会触发自动刷新
  if (remoteBind.value?.permanent) return;
  remoteBindExpiryTimer = setInterval(() => {
    if (!remoteBind.value) return;
    remoteBind.value.expires_in -= 1;
    if (remoteBind.value.expires_in <= 0) {
      // 过期：自动刷新（后端检测到过期会重签新 token）
      fetchRemoteBind(false);
    }
  }, 1000);
}

function stopExpiryWatch() {
  if (remoteBindExpiryTimer !== null) {
    clearInterval(remoteBindExpiryTimer);
    remoteBindExpiryTimer = null;
  }
}

function closeRemoteBind() {
  stopExpiryWatch();
  remoteBind.value = null;
}

// 将 expires_in（秒）转成友好文案；0 表示永久有效
function formatExpiry(seconds: number): string {
  if (!seconds || seconds <= 0) return '永久有效';
  if (seconds < 60) return `${seconds} 秒`;
  const min = Math.floor(seconds / 60);
  if (min < 60) return `${min} 分钟`;
  const hour = Math.floor(min / 60);
  if (hour < 24) return `${hour} 小时`;
  return `${Math.floor(hour / 24)} 天`;
}

// 状态提醒点 hover 文案
function dotTitle(kind: 'green' | 'yellow' | 'red') {
  if (kind === 'green') return '该会话任务执行完成';
  if (kind === 'yellow') return '该会话等待确认操作';
  return '该会话任务报错中断';
}

function getFolderTitle(g: SessionGroup) {
  let title = g.folder_name || (g.folder_path ? g.folder_path.split('\\').pop() : '项目工作区') || '项目工作区';
  title = title.replace(/^💬\s*/, '');
  return (g.is_global || g.folder_path === 'GLOBAL') ? '对话' : title;
}
</script>

<style scoped>
#sidebar {
  width: 240px;
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  transition: margin-left 0.25s;
}
#sidebar.collapsed {
  margin-left: -240px;
}
.side-menu {
  padding: 8px 8px 2px;
  border-bottom: 1px solid var(--border);
}
.menu-item {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 8px 10px;
  border-radius: 8px;
  cursor: pointer;
  color: var(--text-dim);
  margin-bottom: 3px;
  transition: 0.15s;
}
.menu-item:hover {
  background: var(--bg2);
  color: var(--text);
}
.menu-item .mi-ico {
  font-size: 14px;
  width: 18px;
  text-align: center;
}
.menu-item .mi-label {
  font-size: 13px;
  font-weight: 500;
  white-space: nowrap;
  flex-shrink: 0;
}
.menu-item .mi-count {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-faint);
  background: var(--bg3);
  border-radius: 8px;
  padding: 1px 7px;
  /* 目录名过长时单行显示并省略，不换行撑高菜单项 */
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}
.menu-item .mi-arrow {
  color: var(--text-faint);
  font-size: 14px;
}

.side-section {
  padding: 10px 10px 4px;
  color: var(--text-faint);
  font-size: 11.5px;
  letter-spacing: 1px;
  text-transform: uppercase;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.side-section .add-btn {
  background: none;
  border: none;
  color: var(--text-faint);
  font-size: 13px;
  padding: 1px 5px;
  border-radius: 4px;
  transition: 0.15s;
}
.side-section .add-btn:hover {
  color: var(--accent);
  background: var(--bg3);
}

.side-scroll {
  flex: 1;
  overflow-y: auto;
}
#session-list {
  padding: 0 6px;
}
.session-group {
  margin-bottom: 8px;
}
.group-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
  color: var(--text-dim);
  font-size: 12.5px;
  font-weight: 600;
  user-select: none;
  transition: 0.15s;
}
.group-header .g-folder-ico {
  font-size: 13px;
  flex-shrink: 0;
  cursor: pointer;
  padding: 0 2px;
  border-radius: 4px;
  transition: 0.15s;
}
.group-header .g-folder-ico:hover {
  background: var(--bg3);
  transform: scale(1.15);
}
.more-toggle {
  padding: 5px 8px 5px 12px;
  font-size: 11.5px;
  color: var(--text-faint);
  cursor: pointer;
  border-radius: 6px;
  transition: 0.15s;
  user-select: none;
}
.more-toggle:hover {
  color: var(--accent);
  background: var(--bg2);
}
.group-header:hover {
  background: var(--bg2);
  color: var(--text);
}
.group-header .g-arrow {
  font-size: 10px;
  color: var(--text-faint);
}
.group-header .g-title {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.group-header .g-count {
  font-size: 10.5px;
  color: var(--text-faint);
  background: var(--bg3);
  padding: 1px 6px;
  border-radius: 6px;
}
.group-header .g-add, .group-header .g-del {
  background: none;
  border: none;
  color: var(--text-faint);
  font-size: 12px;
  padding: 0 4px;
  border-radius: 4px;
  transition: 0.15s;
}
.group-header .g-add:hover {
  color: var(--accent);
  background: var(--bg3);
}
.group-header .g-del:hover {
  color: var(--red);
  background: rgba(248, 81, 73, 0.15);
}
/* 分组删除的二次确认：红色小确认按钮 */
.group-header .g-confirm {
  background: var(--red);
  border: none;
  color: #fff;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 5px;
  cursor: pointer;
  transition: 0.15s;
  animation: s-confirm-pop 0.15s;
}
.group-header .g-confirm:hover {
  filter: brightness(1.15);
}

.group-sessions {
  padding-left: 6px;
  margin-top: 2px;
  border-left: 1px solid var(--border);
  margin-left: 10px;
}
.session-item {
  position: relative;
  padding: 5px 9px;
  border-radius: 7px;
  cursor: pointer;
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
  border: 1px solid transparent;
}
.session-item:hover {
  background: var(--bg2);
}
.session-item.active {
  background: var(--bg3);
  border-color: var(--border);
}
.session-item .t {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.session-item .m {
  flex-shrink: 0;
  font-size: 11.5px;
  color: var(--text-faint);
}
/* 悬停时删除按钮出现在最右侧，隐藏时间避免重叠 */
.session-item:hover .m {
  visibility: hidden;
}
/* 正在执行回合的会话：名称右侧显示旋转动画图标 */
.session-item .s-spinner {
  flex-shrink: 0;
  width: 12px;
  height: 12px;
  border: 2px solid var(--border);
  border-top-color: var(--orange);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
/* 状态提醒点：非当前会话任务完成/待确认/报错时显示，进入会话后消失 */
.session-item .s-dot {
  flex-shrink: 0;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.session-item .s-dot-green {
  background: var(--green);
  box-shadow: 0 0 4px var(--green);
}
.session-item .s-dot-yellow {
  background: var(--orange);
  box-shadow: 0 0 4px var(--orange);
}
.session-item .s-dot-red {
  background: var(--red);
  box-shadow: 0 0 4px var(--red);
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
.session-item .s-del {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  background: none;
  border: none;
  color: var(--text-faint);
  font-size: 12px;
  padding: 2px 5px;
  border-radius: 4px;
  opacity: 0;
  transition: 0.15s;
}
.session-item:hover .s-del {
  opacity: 1;
}
.session-item .s-del:hover {
  color: var(--red);
  background: rgba(248, 81, 73, 0.15);
}
/* 二次确认状态：时间位置显示红色小确认按钮 */
.session-item.confirming .m {
  visibility: hidden;
}
.session-item .s-confirm {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  background: var(--red);
  border: none;
  color: #fff;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 5px;
  cursor: pointer;
  transition: 0.15s;
  animation: s-confirm-pop 0.15s;
}
.session-item .s-confirm:hover {
  filter: brightness(1.15);
}
@keyframes s-confirm-pop {
  from { transform: translateY(-50%) scale(0.85); opacity: 0; }
  to { transform: translateY(-50%) scale(1); opacity: 1; }
}

/* 就地重命名输入框：占据名称位置，样式与条目文字保持一致 */
.session-item .s-rename-input {
  flex: 1;
  min-width: 0;
  background: var(--bg);
  border: 1px solid var(--accent);
  border-radius: 4px;
  color: var(--text);
  font-size: inherit;
  font-family: inherit;
  padding: 1px 4px;
  outline: none;
}

/* 会话右键菜单：fixed 浮层贴在鼠标位置，坐标由内联 style 计算（含视口收拢） */
.session-ctx-menu {
  position: fixed;
  z-index: 3000;
  min-width: 96px;
  padding: 4px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.35);
}
.session-ctx-menu .ctx-item {
  padding: 6px 10px;
  border-radius: 5px;
  font-size: 12px;
  color: var(--text);
  cursor: pointer;
  white-space: nowrap;
}
.session-ctx-menu .ctx-item:hover {
  background: var(--bg3);
}
.session-ctx-menu .ctx-item.danger:hover {
  color: var(--red);
  background: rgba(248, 81, 73, 0.15);
}
/* 排序菜单的禁用项：首个/末个分组无可移动方向 */
.session-ctx-menu .ctx-item.disabled {
  opacity: 0.4;
  pointer-events: none;
}

/* 分组拖拽排序：源分组淡化，落点用上下插入线提示 */
.session-group.dragging {
  opacity: 0.45;
}
.session-group.drop-before,
.session-group.drop-after {
  position: relative;
}
.session-group.drop-before::before,
.session-group.drop-after::after {
  content: '';
  position: absolute;
  left: 6px;
  right: 6px;
  height: 2px;
  border-radius: 1px;
  background: var(--accent);
  pointer-events: none;
}
.session-group.drop-before::before {
  top: -1px;
}
.session-group.drop-after::after {
  bottom: -1px;
}
/* 可拖动的分组头给出抓取光标；「对话」分组不可拖，保持 pointer */
.group-header[draggable='true'] {
  cursor: grab;
}
.group-header[draggable='true']:active {
  cursor: grabbing;
}

/* 会话拖拽排序：与分组同一套视觉语言（源条目淡化 + 上下插入线） */
.session-item.dragging {
  opacity: 0.45;
}
.session-item.drop-before,
.session-item.drop-after {
  position: relative;
}
.session-item.drop-before::before,
.session-item.drop-after::after {
  content: '';
  position: absolute;
  left: 8px;
  right: 8px;
  height: 2px;
  border-radius: 1px;
  background: var(--accent);
  pointer-events: none;
}
.session-item.drop-before::before {
  top: -1px;
}
.session-item.drop-after::after {
  bottom: -1px;
}
.session-item[draggable='true'] {
  cursor: grab;
}
.session-item[draggable='true']:active {
  cursor: grabbing;
}
/* 会话菜单里的排序项与重命名/删除之间的分隔线 */
.session-ctx-menu .ctx-sep {
  height: 1px;
  margin: 4px 2px;
  background: var(--border);
}

.side-footer {
  padding: 7px;
  border-top: 1px solid var(--border);
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 3px;
}
.side-footer button {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 8px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--text-faint);
  text-align: left;
}
.side-footer button:hover {
  color: var(--text);
  background: var(--bg2);
}
/* 手机远程控制入口：与设置按钮同行右侧的小方形图标按钮 */
.side-footer .phone-btn {
  width: 32px;
  justify-content: center;
  padding: 7px;
  gap: 0;
}
.footer-copy { display: flex; flex-direction: column; min-width: 0; }
.footer-copy b { font-size: 12.5px; color: inherit; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 手机远程控制绑定弹窗（结构参照 ConfirmModal 的 modal-mask 风格） */
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  backdrop-filter: blur(2px);
}
.modal {
  width: 380px;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6);
  overflow: hidden;
  animation: remote-pop 0.2s;
}
@keyframes remote-pop {
  from { transform: scale(0.95); opacity: 0; }
  to { transform: none; opacity: 1; }
}
.modal-head {
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 10px;
}
.modal-head .mh-ico {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: var(--accent-soft);
  color: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.modal-head h3 {
  font-size: 14.5px;
}
.modal-head p {
  font-size: 12px;
  color: var(--text-faint);
}
.modal-body {
  padding: 18px;
  background: var(--bg2);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}
.qr-img {
  width: 200px;
  height: 200px;
  border-radius: 10px;
  background: #fff;
  padding: 6px;
}
.bind-url {
  font-size: 12px;
  color: var(--text-dim);
  word-break: break-all;
  text-align: center;
  max-width: 100%;
}
.bind-hint {
  font-size: 11.5px;
  color: var(--text-faint);
}
.modal-foot {
  padding: 13px 18px;
  display: flex;
  gap: 9px;
  justify-content: flex-end;
}
.btn {
  border-radius: 8px;
  padding: 7px 16px;
  font-size: 13px;
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text);
  transition: 0.15s;
}
.btn:hover {
  border-color: var(--text-faint);
}
.btn.primary {
  background: var(--accent);
  border: none;
  color: #fff;
  font-weight: 600;
}

/* 移动端侧栏遮罩：桌面端不显示，仅在 <=768px 侧栏展开时盖住主区。
   层级低于侧栏（style.css 中 #main > #sidebar z-index:400）以便侧栏可点击 */
.sidebar-backdrop {
  display: none;
}
@media (max-width: 768px) {
  .sidebar-backdrop {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 390;
    background: rgba(0, 0, 0, 0.5);
  }
}
</style>
