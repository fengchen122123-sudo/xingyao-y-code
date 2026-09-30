<template>
  <div class="page">
    <van-nav-bar title="会话" fixed placeholder safe-area-inset-top>
      <template #left>
        <img class="nvlogo" :src="logoUrl" alt="星瑶" />
      </template>
      <template #right>
        <span class="newbtn" @click="openNew"><svg class="i"><use href="#i-plus" /></svg>新建</span>
      </template>
    </van-nav-bar>

    <van-search v-model="search" placeholder="搜索会话" clear-trigger="always" />

    <van-pull-refresh class="list" :model-value="refreshing" @refresh="onRefresh">
      <div class="groups">
        <template v-for="g in filteredGroups" :key="g.folder_path">
          <div class="group-label" @click="toggle(g.folder_path)">
            <svg class="i sm chev" :class="{ open: !collapsed.has(g.folder_path) }"><use href="#i-chev-r" /></svg>
            {{ g.is_global ? '自由对话' : g.folder_name }}
            <span class="n mono">{{ g.sessions.length }}</span>
            <span v-if="g.is_current" class="cur-mark">当前</span>
          </div>
          <template v-if="!collapsed.has(g.folder_path)">
            <div
              v-for="c in g.sessions"
              :key="c.name"
              class="conv-card"
              :class="{ active: c.name === store.currentSession }"
              @click="openSession(c)"
              @touchstart="pressStart(c)"
              @touchend="pressEnd"
              @touchmove="pressEnd"
              @contextmenu.prevent="openOps(c)"
            >
              <div class="t">
                <span class="dot" :class="{ run: store.runningSessions.includes(c.name) }" />
                <span class="title">{{ c.display_name }}</span>
              </div>
              <div class="m">
                <span>{{ c.count }} 条</span>
                <span class="sp" />
                <span v-if="!g.is_global" class="mono wdir">{{ c.workdir }}</span>
                <span>{{ relTime(c.updated_at) }}</span>
              </div>
            </div>
          </template>
        </template>
        <div v-if="!filteredGroups.length" class="empty">
          <van-empty :description="search.trim() ? `没有匹配「${search.trim()}」的会话` : '还没有会话，点右上角新建'" />
          <button v-if="search.trim()" class="empty-clear" @click="search = ''">清除搜索</button>
        </div>
      </div>
    </van-pull-refresh>

    <!-- 新建会话：绑定项目 -->
    <van-popup v-model:show="newDlg" position="bottom" round :style="{ maxHeight: '70%' }">
      <div class="sheet">
        <div class="sh-h"><span>新建会话</span><span class="x" @click="newDlg = false">✕</span></div>
        <div class="frow">
          <label>标题（留空自动命名）</label>
          <input v-model="newTitle" placeholder="例如：修复登录页遮挡" maxlength="120" />
        </div>
        <div class="frow">
          <label>绑定项目（工作区）</label>
          <div class="pick" @click="projDlg = true">
            <span>{{ newWorkdirLabel }}</span>
            <svg class="i sm"><use href="#i-chev-r" /></svg>
          </div>
        </div>
        <div class="btnrow">
          <button class="btn-g" @click="newDlg = false">取消</button>
          <button class="btn-p" :disabled="creating" @click="create">创建并切换</button>
        </div>
      </div>
    </van-popup>

    <!-- 项目选择（移动端内列表，替代系统 select） -->
    <van-popup v-model:show="projDlg" position="bottom" round :style="{ maxHeight: '70%' }">
      <div class="sheet">
        <div class="sh-h"><span>选择绑定项目</span><span class="x" @click="projDlg = false">✕</span></div>
        <div class="opt" :class="{ on: !newWorkdir }" @click="pickProject('')">
          <span>自由对话（不绑定）</span>
          <span v-if="!newWorkdir" class="cur mono"><svg class="i sm"><use href="#i-check" /></svg>已选</span>
        </div>
        <div
          v-for="g in projectGroups"
          :key="g.folder_path"
          class="opt"
          :class="{ on: newWorkdir === g.folder_path }"
          @click="pickProject(g.folder_path)"
        >
          <span class="mono">{{ g.folder_path }}</span>
          <span v-if="g.is_current" class="d">当前</span>
          <span v-if="newWorkdir === g.folder_path" class="cur mono"><svg class="i sm"><use href="#i-check" /></svg>已选</span>
        </div>
        <div class="opt browse" @click="pickProject('__browse__')"><svg class="i sm"><use href="#i-folder" /></svg>浏览服务器目录…</div>
      </div>
    </van-popup>

    <!-- 服务器目录浏览 -->
    <van-popup v-model:show="dirDlg" position="bottom" round :style="{ maxHeight: '70%' }">
      <div class="sheet">
        <div class="sh-h"><span>选择工作目录</span><span class="x" @click="dirDlg = false">✕</span></div>
        <div class="dirs">
          <div v-if="!dirRoot" class="d" @click="browseUp">
            <svg class="i sm" style="transform:rotate(180deg)"><use href="#i-chev-r" /></svg>上级目录
          </div>
          <div v-for="d in dirList" :key="d.path" class="d" @click="browseInto(d.path)">
            <svg class="i sm"><use href="#i-folder" /></svg>{{ d.path }}
          </div>
          <van-empty v-if="!dirList.length" description="此目录没有子目录" />
        </div>
        <div class="btnrow">
          <button class="btn-g" @click="dirDlg = false">取消</button>
          <button class="btn-p" :disabled="!dirCurrent" @click="pickDir">选定此目录</button>
        </div>
      </div>
    </van-popup>

    <!-- 会话操作（长按，与详情 ⋯ 共用组件） -->
    <SessionOps :show="opsDlg" :target="opsTarget" @close="opsDlg = false" @reopen="opsDlg = true" @deleted="opsTarget = null" />
  </div>
</template>

<script setup lang="ts">
import { computed, onActivated, onDeactivated, ref } from 'vue';
import { useRouter } from 'vue-router';
import { showFailToast } from 'vant';
import { useAppStore, type SessionGroup, type SessionItem } from '../stores/app';
import SessionOps from '../components/SessionOps.vue';
import { relTime } from '../utils/time';

const store = useAppStore();
const router = useRouter();

// 星瑶品牌 logo（public 静态资源，运行时引用；/m/ 前缀与 lan_server 托管路径一致）
const logoUrl = '/m/icon.png';

const search = ref('');
const refreshing = ref(false);
const collapsed = ref(new Set<string>());
const newDlg = ref(false);
const projDlg = ref(false);
const creating = ref(false);
const newTitle = ref('');
const newWorkdir = ref('');
const dirDlg = ref(false);
const dirRoot = ref(true);
const dirCurrent = ref('');
const dirList = ref<{ name: string; path: string }[]>([]);
const opsDlg = ref(false);
const opsTarget = ref<SessionItem | null>(null);
let pressTimer: ReturnType<typeof setTimeout> | null = null;

const projectGroups = computed(() => store.sessionGroups.filter((g) => !g.is_global));
const newWorkdirLabel = computed(() => newWorkdir.value || '自由对话（不绑定）');

const filteredGroups = computed<SessionGroup[]>(() => {
  const q = search.value.trim().toLowerCase();
  const out: SessionGroup[] = [];
  for (const g of store.sessionGroups) {
    const items = q
      ? g.sessions.filter((s) => s.display_name.toLowerCase().includes(q))
      : g.sessions;
    if (items.length) out.push({ ...g, sessions: items });
  }
  return out;
});

function toggle(path: string) {
  const next = new Set(collapsed.value);
  if (next.has(path)) next.delete(path);
  else next.add(path);
  collapsed.value = next;
}

function pressStart(c: SessionItem) {
  pressEnd();
  pressTimer = setTimeout(() => { opsTarget.value = c; opsDlg.value = true; }, 550);
}
function pressEnd() {
  if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
}
function openOps(c: SessionItem) {
  pressEnd();
  opsTarget.value = c;
  opsDlg.value = true;
}

async function openSession(c: SessionItem) {
  if (opsDlg.value) { opsDlg.value = false; return; }
  try {
    await store.switchSession(c.name);
    router.push(`/convo/${encodeURIComponent(c.name)}`);
  } catch (e: any) {
    showFailToast(e?.message || '切换会话失败');
  }
}

async function onRefresh() {
  refreshing.value = true;
  await store.refreshSessions();
  refreshing.value = false;
}

// ---- 新建（绑定项目）----

function openNew() {
  newTitle.value = '';
  newWorkdir.value = '';
  projDlg.value = false;
  newDlg.value = true;
}

/** 选择绑定项目；选“浏览服务器目录…”则打开目录弹层 */
function pickProject(v: string) {
  projDlg.value = false;
  if (v === '__browse__') {
    dirRoot.value = true;
    dirCurrent.value = '';
    dirDlg.value = true;
    void browseInto('');
    return;
  }
  newWorkdir.value = v;
}

async function create() {
  creating.value = true;
  try {
    const workdir = newWorkdir.value || undefined;
    const name = await store.createSession(newTitle.value.trim(), workdir);
    newDlg.value = false;
    if (name) router.push(`/convo/${encodeURIComponent(name)}`);
  } catch (e: any) {
    showFailToast(e?.message || '创建失败');
  } finally {
    creating.value = false;
  }
}

async function browseInto(path: string) {
  try {
    const r = await store.browseDirs(path);
    dirRoot.value = r.isRoot;
    dirCurrent.value = r.current;
    dirList.value = r.dirs;
  } catch (e: any) {
    showFailToast(e?.message || '目录读取失败');
  }
}
async function browseUp() {
  const parent = dirCurrent.value.replace(/[\\/][^\\/]+[\\/]?$/, '') || '';
  await browseInto(parent);
}
function pickDir() {
  newWorkdir.value = dirCurrent.value;
  dirDlg.value = false;
}

onActivated(() => { void store.refreshSessions(); });
// tabbar 可点在弹层之上，离开本页（keep-alive 不卸载）时主动收起所有弹层。
// 搜索词同样必须清掉：keep-alive 复用同一实例，若留着搜索词，从详情返回时列表
// 仍停在过滤结果上（关键词不命中时还会显示空态），看起来就是「卡在搜索界面」，
// 而 Vant 的清除按钮只在输入框聚焦时才出现，用户找不到退出口只能整页刷新。
onDeactivated(() => {
  newDlg.value = false;
  projDlg.value = false;
  dirDlg.value = false;
  opsDlg.value = false;
  search.value = '';
});
</script>

<style scoped>
/* 页面高度由 flex 分配（app-root 已扣掉 tabbar/banner），滚动容器见 .list。
   全局 html/body 是 overflow:hidden，本页必须自己提供滚动容器，否则内容超屏
   只能被裁掉（曾经 .page 只有 min-height:100vh，整页无法滑动）。 */
.page { flex: 1; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
/* 滚动容器必须是 pull-refresh 自身或其祖先：Vant 用 useScrollParent(root) 沿 DOM
   向上找 overflow-y 为 auto/scroll 的元素来判定「是否在顶部」，找不到就退化成
   window（永不滚动）→ reachTop 恒真 → 任何向下拖动都会被 preventDefault 抢去触发
   下拉刷新，列表反而滚不动。这里覆盖 Vant 的 overflow:hidden 让它命中自身。 */
.list { flex: 1; min-height: 0; overflow-y: auto; -webkit-overflow-scrolling: touch; overscroll-behavior-y: contain; }
/* Vant 默认把 track 固定成 100% 高（内容靠溢出传播撑开滚动区）。改成随内容长高、
   但至少一屏，滚动区尺寸不再依赖溢出传播规则。 */
.list :deep(.van-pull-refresh__track) { height: auto; min-height: 100%; }
.nvlogo { width: 22px; height: 22px; border-radius: 6px; object-fit: cover; box-shadow: 0 0 8px var(--accent-soft); }
.newbtn { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; font-family: var(--font-mono); color: var(--accent); }
.groups { padding: 12px 16px calc(12px + env(safe-area-inset-bottom, 0px)); display: flex; flex-direction: column; gap: 10px; }
.empty { padding-top: 40px; display: flex; flex-direction: column; align-items: center; gap: 10px; }
.empty-clear { padding: 9px 22px; border-radius: 8px; font-size: 13px; border: 1px solid var(--line-strong); background: none; color: var(--text-2); }
.group-label { font-size: 12px; color: var(--text-2); padding: 9px 2px 7px; display: flex; align-items: center; gap: 7px; cursor: pointer; }
.group-label .chev { transition: transform .15s; }
.group-label .chev.open { transform: rotate(90deg); }
.group-label .n { font-size: 10px; color: var(--text-3); }
.cur-mark { font-size: 10px; color: var(--accent); border: 1px solid var(--accent-line); border-radius: 99px; padding: 0 7px; }
.conv-card { background: var(--bg-panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column; gap: 5px; }
.conv-card:active { border-color: var(--accent); }
.conv-card.active { border-color: var(--accent-line); }
.conv-card .t { display: flex; align-items: center; gap: 7px; font-size: 13px; font-weight: 600; }
.conv-card .title { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.conv-card .m { display: flex; gap: 7px; align-items: center; font-size: 11px; color: var(--text-3); }
.conv-card .sp { flex: 1; }
.wdir { font-size: 10px; max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text-3); flex: none; }
.dot.run { background: var(--ok); animation: livepulse 1.3s infinite; }
.sheet { padding: 15px 18px 20px; }
.sh-h { display: flex; align-items: center; justify-content: space-between; padding: 0; margin-bottom: 12px; font-size: 13px; font-weight: 600; }
.sh-h .x { font-size: 12px; color: var(--text-3); font-weight: 400; padding: 4px 8px; }
.frow { display: flex; flex-direction: column; gap: 5px; margin: 10px 0 0; }
.frow label { font-size: 11px; color: var(--text-3); }
.frow input { padding: 10px 12px; border-radius: 8px; border: 1px solid var(--line-strong); background: var(--bg-inset); color: var(--text-1); font-size: 13px; outline: none; font-family: inherit; }
/* 项目选择行（替代系统 select） */
.pick { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--line-strong); background: var(--bg-inset); color: var(--text-1); font-size: 13px; cursor: pointer; }
.pick span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pick .i { color: var(--text-3); }
.opt { padding: 13px 16px; font-size: 13.5px; cursor: pointer; display: flex; gap: 10px; align-items: center; }
.opt:active { background: var(--bg-raised); }
.opt.on { color: var(--accent); }
.opt .d { font-size: 11px; color: var(--text-3); }
.opt .cur { margin-left: auto; font-size: 10px; color: var(--accent); display: inline-flex; gap: 4px; align-items: center; }
.opt.browse { color: var(--accent); border-top: 1px solid var(--line); }
.btnrow { display: flex; gap: 8px; margin: 16px 0 0; }
.btnrow button { flex: 1; padding: 10px; border-radius: 8px; font-size: 13px; }
.btn-g { border: 1px solid var(--line-strong); background: none; color: var(--text-2); }
.btn-p { border: none; background: var(--accent); color: var(--accent-text); font-weight: 600; }
.dirs { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; max-height: 220px; overflow-y: auto; margin: 4px 0 0; }
.dirs .d { padding: 9px 12px; font-size: 12.5px; font-family: var(--font-mono); cursor: pointer; display: flex; gap: 8px; align-items: center; }
.dirs .d:active { background: var(--bg-raised); }
</style>
