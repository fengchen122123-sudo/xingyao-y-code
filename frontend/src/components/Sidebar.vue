<template>
  <div id="sidebar" :class="{ collapsed: store.sidebarCollapsed }">
    <div class="side-menu">
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
              v-if="!group.is_global && group.folder_path !== 'GLOBAL'" 
              class="g-del" 
              title="移出项目分组 (不删磁盘文件)" 
              @click.stop="store.deleteFolderGroup(group.folder_path, getFolderTitle(group))"
            >✕</button>
          </div>

          <div v-show="!collapsedGroups[group.folder_path]" class="group-sessions">
            <div 
              v-for="s in group.sessions" 
              :key="s.name" 
              class="session-item"
              :class="{ active: s.name === store.currentSessionName }"
              @click="store.switchSession(s.name, s.display_name || s.name, s.workdir)"
            >
              <div class="t" style="padding-right:18px">{{ s.display_name || s.name }}</div>
              <div class="m">{{ s.count || 0 }} 条消息</div>
              <button class="s-del" title="删除此对话" @click.stop="store.deleteSingleSession(s.name)">✕</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="side-footer" title="点击切换项目工作区文件夹" @click="store.openWorkspaceFolder()">
      {{ store.currentWorkdirLabel === 'GLOBAL' || store.currentWorkdirLabel === '对话' ? '对话' : store.currentWorkdirLabel }}<br>
      SQLite 已连接
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue';
import { useAppStore } from '../stores/appStore';
import type { SessionGroup } from '../types/pywebview';

const store = useAppStore();

const collapsedGroups = ref<Record<string, boolean>>({});
const connectedMcpCount = computed(() => store.mcps.filter(m => m.connected).length);

function toggleGroupCollapsed(path: string) {
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
  font-size: 10.5px;
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
  font-size: 11px;
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
  font-size: 12px;
  font-weight: 600;
  user-select: none;
  transition: 0.15s;
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
  font-size: 10px;
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

.group-sessions {
  padding-left: 6px;
  margin-top: 2px;
  border-left: 1px solid var(--border);
  margin-left: 10px;
}
.session-item {
  position: relative;
  padding: 7px 9px;
  border-radius: 7px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 2px;
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
  font-size: 12.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.session-item .m {
  font-size: 10.5px;
  color: var(--text-faint);
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

.side-footer {
  padding: 10px;
  border-top: 1px solid var(--border);
  font-size: 12px;
  color: var(--text-faint);
  cursor: pointer;
  transition: 0.15s;
}
.side-footer:hover {
  color: var(--text);
  background: var(--bg2);
}
</style>
