<template>
  <div id="sidebar" :class="{ collapsed: store.sidebarCollapsed }">
    <div class="side-menu">
      <div class="menu-item" @click="store.createSessionSmart()">
        <span class="mi-ico">✎</span>
        <span class="mi-label">新会话</span>
        <span class="mi-count">{{ store.currentWorkdirLabel === '对话' ? '对话' : store.currentWorkdirLabel }}</span>
        <span class="mi-arrow">＋</span>
      </div>
      <div class="menu-item" @click="store.openSubpage('skills')">
        <span class="mi-ico">❖</span>
        <span class="mi-label">技能 Skills</span>
        <span class="mi-count">{{ store.skills.length }}</span>
        <span class="mi-arrow">›</span>
      </div>
      <div class="menu-item" @click="store.openSubpage('mcp')">
        <span class="mi-ico">⧉</span>
        <span class="mi-label">MCP 服务</span>
        <span class="mi-count">{{ connectedMcpCount }} / {{ store.mcps.length }} 在线</span>
        <span class="mi-arrow">›</span>
      </div>
      <div class="menu-item" @click="store.openSubpage('plugins')">
        <span class="mi-ico">⬡</span>
        <span class="mi-label">插件</span>
        <span class="mi-count">{{ enabledPluginCount }} / {{ store.plugins.length }}</span>
        <span class="mi-arrow">›</span>
      </div>
    </div>

    <div class="side-section">
      项目会话
      <span>
        <button class="add-btn" title="添加/引入项目文件夹" style="margin-right:4px" @click="store.openWorkspaceFolder()">▣</button>
        <button class="add-btn" title="新建自由通用对话(跳出文件夹)" @click="store.createGlobalSession()">＋</button>
      </span>
    </div>

    <div class="side-scroll">
      <div id="session-list">
        <div v-if="!store.sessionGroups || store.sessionGroups.length === 0" style="padding:10px;color:var(--text-faint);font-size:12px">
          暂无历史会话
        </div>

        <div v-for="group in store.sessionGroups" :key="group.folder_path || group.folder_name" class="session-group">
          <div class="group-header" @click="toggleGroupCollapsed(group.folder_path)">
            <span class="g-arrow">{{ collapsedGroups[group.folder_path] ? '▸' : '▾' }}</span>
            <span v-if="!group.is_global && group.folder_path !== 'GLOBAL'" class="g-folder-ico">📁</span>
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
              :class="{ active: s.name === store.currentSessionName, confirming: confirmDeleteName === s.name }"
              @click="onSessionClick(s)"
            >
              <div class="t">{{ s.display_name || s.name }}</div>
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
      <button title="点击切换项目工作区文件夹" @click="store.openWorkspaceFolder()">
        <span>▣</span>
        <span class="footer-copy">
          <b>{{ store.currentWorkdirLabel === 'GLOBAL' || store.currentWorkdirLabel === '对话' ? '对话' : store.currentWorkdirLabel }}</b>
          <small>SQLite 已连接</small>
        </span>
      </button>
      <button data-test="settings-button" title="打开设置" @click="store.openModelSettings()">
        <span>⚙</span><span class="footer-copy"><b>设置</b><small>模型配置</small></span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue';
import { useAppStore } from '../stores/appStore';
import type { SessionGroup } from '../types/pywebview';

const store = useAppStore();

const collapsedGroups = ref<Record<string, boolean>>({});
const expandedGroups = ref<Record<string, boolean>>({});
// 正在等待二次确认删除的会话名（在时间位置显示红色小确认按钮）
const confirmDeleteName = ref('');
// 正在等待二次确认删除的项目分组路径
const confirmDeleteGroupPath = ref('');

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
  store.switchSession(s.name, s.display_name || s.name, s.workdir);
}

// 一个目录下超过 5 条会话时默认折叠，点“显示更多”再展开全部
function visibleSessions(group: SessionGroup) {
  const list = group.sessions || [];
  if (expandedGroups.value[group.folder_path] || list.length <= 5) return list;
  return list.slice(0, 5);
}

function toggleShowMore(path: string) {
  expandedGroups.value[path] = !expandedGroups.value[path];
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

function toggleGroupCollapsed(path: string) {
  // 处于确认删除状态时，点击分组头其它区域视为取消，不折叠
  if (confirmDeleteGroupPath.value === path) {
    confirmDeleteGroupPath.value = '';
    return;
  }
  confirmDeleteGroupPath.value = '';
  collapsedGroups.value[path] = !collapsedGroups.value[path];
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
  background: var(--bg1);
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
}
.menu-item .mi-count {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-faint);
  background: var(--bg3);
  border-radius: 8px;
  padding: 1px 7px;
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

.side-footer {
  padding: 7px;
  border-top: 1px solid var(--border);
  display: grid;
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
.footer-copy { display: flex; flex-direction: column; min-width: 0; }
.footer-copy b { font-size: 12.5px; color: inherit; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.footer-copy small { font-size: 10px; color: var(--text-faint); }
</style>
