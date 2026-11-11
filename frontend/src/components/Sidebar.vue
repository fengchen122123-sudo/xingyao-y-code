<template>
  <div id="sidebar" data-theme-region="sidebar" :class="{ collapsed: store.sidebarCollapsed }" :style="panelStyle">
      <!-- QQ2009 皮肤专属：主面板用户卡（装饰性，仅内置 QQ2009 主题激活时渲染；
           样式在 themes/qq2009.css 的 §2 小节，本组件不加样式） -->
      <div v-if="store.guiTheme?.id === 'builtin.qq2009'" class="qq09-user-card">
        <!-- 头像：点击换自定义头像（隐藏 input 转交原生文件框，见 qq09PickAvatar）。
             img 盖住默认企鹅底图（z-index 1，状态点 2）；GIF 的 DataURL 放进
             img.src 即按帧自动播放，无需额外处理。状态点保持原样叠在右下角。 -->
        <div class="qq09-avatar" title="点击更换头像" @click="qq09PickAvatar">
          <img v-if="qq09AvatarData" class="qq09-avatar-img" :src="qq09AvatarData" alt="用户头像">
          <button
            v-if="qq09AvatarData"
            type="button"
            class="qq09-avatar-reset"
            data-test="qq09-avatar-reset"
            title="恢复默认头像"
            @click.stop="qq09ResetAvatar"
          >×</button>
          <!-- 状态点颜色由 :data-status 驱动（六色见 qq2009.css，与下方状态行同源） -->
          <span class="qq09-status-dot" :data-status="qq09Status" />
        </div>
        <!-- 隐藏文件框：仅由头像点击触发（hidden 不占布局，也不参与用户卡 flex 排布） -->
        <input
          ref="qq09AvatarInputRef"
          type="file"
          hidden
          accept="image/*"
          data-test="qq09-avatar-input"
          @change="qq09OnAvatarFile"
        >
        <div class="qq09-user-meta">
          <div class="qq09-user-name">
            <template v-if="!qq09EditingName">
              <!-- 不再用 :title（浏览器默认 tooltip），超长时改用自定义气泡 qq09Tip -->
              <span
                class="qq09-user-name-text"
                @mouseenter="qq09ShowTip($event, qq09UserName)"
                @mouseleave="qq09HideTip"
              >{{ qq09UserName }}</span>
              <span class="qq09-user-no">(260711)</span>
              <button type="button" class="qq09-name-edit" data-test="qq09-name-edit" title="修改昵称" @click="qq09StartEdit">
                <AppIcon name="pencil" :size="11" :stroke-width="2" />
              </button>
            </template>
            <input
              v-else
              ref="qq09NameInputRef"
              v-model="qq09NameDraft"
              class="qq09-name-input"
              data-test="qq09-name-input"
              maxlength="24"
              @keyup.enter="qq09SaveName"
              @keyup.esc="qq09CancelEdit"
              @blur="qq09SaveName"
            >
          </div>
          <!-- 个性签名：与昵称同款就地编辑（铅笔 → 输入框 → 保存） -->
          <div class="qq09-user-sign">
            <template v-if="!qq09EditingSign">
              <span
                class="qq09-user-sign-text"
                @mouseenter="qq09ShowTip($event, qq09UserSign)"
                @mouseleave="qq09HideTip"
              >{{ qq09UserSign }}</span>
              <button type="button" class="qq09-sign-edit" data-test="qq09-sign-edit" title="修改签名" @click="qq09StartSignEdit">
                <AppIcon name="pencil" :size="10" :stroke-width="2" />
              </button>
            </template>
            <input
              v-else
              ref="qq09SignInputRef"
              v-model="qq09SignDraft"
              class="qq09-sign-input"
              data-test="qq09-sign-input"
              maxlength="40"
              @keyup.enter="qq09SaveSign"
              @keyup.esc="qq09CancelSign"
              @blur="qq09SaveSign"
            >
          </div>
          <!-- 在线状态：触发器 + 就地下拉（非原生 select）；图标为纯 CSS 圆点，无 emoji/图片 -->
          <div ref="qq09StatusRef" class="qq09-user-status">
            <button
              type="button"
              class="qq09-status-trigger"
              data-test="qq09-status-trigger"
              :aria-expanded="qq09StatusMenuOpen"
              title="切换在线状态"
              @click="qq09ToggleStatusMenu"
            >
              <span class="qq09-st-icon" :data-st="qq09Status" aria-hidden="true" />
              <span class="qq09-status-label">{{ qq09StatusLabel }}</span>
              <span class="qq09-status-caret">▾</span>
            </button>
            <div v-if="qq09StatusMenuOpen" class="qq09-status-menu" data-test="qq09-status-menu" role="menu" aria-label="在线状态">
              <button
                v-for="st in QQ09_STATUSES"
                :key="st.id"
                type="button"
                role="menuitemradio"
                class="qq09-status-item"
                :class="{ active: st.id === qq09Status }"
                :aria-checked="st.id === qq09Status"
                :data-test="`qq09-status-item-${st.id}`"
                @click="qq09SelectStatus(st.id)"
              >
                <span class="qq09-st-icon" :data-st="st.id" aria-hidden="true" />
                <span class="qq09-status-item-name">{{ st.label }}</span>
              </button>
            </div>
          </div>
          <!-- QQ 等级：图标为纯 CSS 绘制（太阳/月亮/星星，无 emoji/图片），
               数量与种类由 QQ09_LEVEL_ICONS 按 16/4/1 换算；与状态胶囊同一行排布 -->
          <div class="qq09-user-level" data-test="qq09-user-level" :title="`QQ等级 ${QQ09_LEVEL} 级`">
            <span
              v-for="(icon, i) in QQ09_LEVEL_ICONS"
              :key="i"
              class="qq09-lv-icon"
              :data-lv="icon"
              aria-hidden="true"
            />
          </div>
        </div>
        <!-- 自定义气泡提示（网名 / 签名超长时显示完整文本）：fixed 定位，不参与用户卡
             弹性布局；作为 hover 元素的兄弟节点渲染，状态不落在元素内部，编辑态切换不残留 -->
        <span
          v-if="qq09Tip.show"
          class="qq09-tip"
          :class="{ flipped: qq09Tip.flipped }"
          data-test="qq09-user-tip"
          :style="{ left: `${qq09Tip.left}px`, top: `${qq09Tip.top}px` }"
        >{{ qq09Tip.text }}</span>
      </div>

      <!-- QQ2009 皮肤专属：搜索框，点击打开既有的会话搜索弹窗 -->
      <div v-if="store.guiTheme?.id === 'builtin.qq2009'" class="qq09-search" data-test="qq09-search" @click="store.openSessionSearchModal()">
        🔍 搜索会话、联系人…
      </div>

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
        <span class="mi-label">星盘 · 技能</span>
        <span class="mi-count">{{ store.skills.length }}</span>
        <span class="mi-arrow">›</span>
      </div>
      <div class="menu-item" @click="openMcp()">
        <span class="mi-ico">⧉</span>
        <span class="mi-label">星桥 · MCP</span>
        <span class="mi-count">{{ connectedMcpCount }} / {{ store.mcps.length }} 在线</span>
        <span class="mi-arrow">›</span>
      </div>
      <div class="menu-item" @click="openPlugins()">
        <span class="mi-ico">⬡</span>
        <span class="mi-label">星匣 · 插件</span>
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
            <!-- 秘书分组是虚拟分组（folder_path='SECRETARY' 不是磁盘目录）：不能打开
                 目录，也不能整组删除，否则会把分组标识当路径发给后端 -->
            <span
              v-if="!group.is_global && group.folder_path !== 'GLOBAL' && !group.is_secretary"
              class="g-folder-ico"
              title="在资源管理器中打开此目录"
              @click.stop="openFolder(group.folder_path)"
            >📁</span>
            <span class="g-title" :style="{ color: group.is_current ? 'var(--accent)' : 'inherit' }">
              {{ getFolderTitle(group) }}
              <span v-if="group.is_current" style="font-size:10px;color:var(--green)"> [当前]</span>
            </span>
            <span class="g-count">{{ group.sessions ? group.sessions.length : 0 }} 个会话</span>

            <!-- 「对话」（自由通用对话）分组的 ＋：原先挂在「项目会话」标题栏，
                 现移到行内，与「秘书」分组的入口位置保持一致 -->
            <button
              v-if="isGlobalGroup(group)"
              class="g-add"
              title="新建自由通用对话(跳出文件夹)"
              @click.stop="store.createGlobalSession()"
            >＋</button>

            <!-- 秘书分组保留 ＋（新建归秘书的会话），但不能把 'SECRETARY' 当
                 workdir 传给 createSessionInGroup，所以点击走专门的桥方法。
                 秘书必须先起名（名字是它的身份标识），故先弹起名弹窗，
                 提交后才由 store.createSecretarySession(name, persona) 真正建会话 -->
            <button
              v-if="!group.is_global && group.folder_path !== 'GLOBAL'"
              class="g-add"
              :data-test="group.is_secretary ? 'secretary-add' : 'group-add'"
              :title="group.is_secretary ? '新建秘书会话' : '在项目新建会话'"
              @click.stop="group.is_secretary ? openSecretaryNameModal() : store.createSessionInGroup(group.folder_path)"
            >＋</button>
            <button
              v-if="!group.is_global && group.folder_path !== 'GLOBAL' && !group.is_secretary"
              class="g-del"
              title="删除此项目分组及组内对话（会二次确认）"
              @click.stop="askDeleteGroup(group)"
            >✕</button>
          </div>

          <div v-show="!collapsedGroups[group.folder_path]" class="group-sessions">
            <div
              v-for="s in visibleSessions(group)"
              :key="s.name"
              class="session-item"
              :class="[sessionDropClass(group, s), { active: s.name === store.currentSessionName, confirming: confirmDeleteName === s.name || confirmArchiveName === s.name }]"
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
              <!-- 归档：可逆操作但误点会把会话挪出列表，故与删除一致走行内二次确认。
                   当前活跃会话不可归档，按钮置灰并给出原因 -->
              <button
                v-if="confirmArchiveName !== s.name"
                class="s-archive"
                data-test="session-archive-btn"
                :class="{ disabled: s.name === store.currentSessionName }"
                :disabled="s.name === store.currentSessionName"
                :title="s.name === store.currentSessionName ? '当前会话不能归档' : '归档此对话'"
                @click.stop="askArchive(s.name)"
              >⤓</button>
              <button
                v-else
                class="s-confirm archive"
                data-test="session-archive-confirm"
                title="点击确认归档"
                @click.stop="confirmArchive(s.name)"
              >确认</button>
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

    <!-- 底部容器：只为账号浮层提供定位基准（浮层 absolute 到 .side-bottom 的
         上边缘），不改变内部 .side-footer 的网格布局 -->
    <div class="side-bottom">
      <div class="side-footer">
        <!-- 左下角用户卡片：未登录点开基元律动（TokenRhythm）登录窗（登录成功后
             后端自动绑定 API Key）；已登录则切换上方的账号浮层。
             设置入口仍是右侧齿轮，手机访问是它右边的手机按钮 -->
        <button
          class="side-user"
          data-test="side-user-card"
          :title="store.trAuth.status === 'loggedIn'
            ? '查看基元律动账号'
            : '点击登录基元律动账号，自动配置 API Key'"
          @click="onUserCardClick()"
        >
          <span class="side-avatar">{{ trAvatarText }}</span>
          <span class="footer-copy"><b :title="trUserLabelTitle">{{ trUserLabelShort }}</b></span>
        </button>
        <button class="icon-btn" data-test="settings-button" title="打开设置" @click="store.openModelSettings()">
          <AppIcon name="settings" :size="16" />
        </button>
        <button class="icon-btn phone-btn" data-test="remote-bind-button" title="手机访问（移动端 / web远端）" @click="openRemoteBind()">
          <AppIcon name="smartphone" :size="16" />
        </button>
      </div>
      <!-- 基元律动账号浮层：登录成功后点左下角卡片，在卡片上方弹出 -->
      <TokenRhythmAccountPanel />
      <!-- 账号中心原生弹窗（充值 / 用量明细 / 个人信息），Teleport 到 body -->
      <TokenRhythmAccountDialog />
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
    <div
      v-if="!ctxMenu.secretary"
      class="ctx-item"
      data-test="ctx-move-to"
      @click="openMoveMenu()"
    >移动到…</div>
    <div class="ctx-item" data-test="ctx-rename" @click="startRename()">重命名</div>
    <!-- 只有秘书会话有专属人设：每个秘书是用户自己创建的独立 agent -->
    <div
      v-if="ctxMenu.secretary"
      class="ctx-item"
      data-test="ctx-secretary-persona"
      @click="openSecretaryPersonaModal()"
    >编辑人设</div>
    <div class="ctx-item danger" data-test="ctx-delete" @click="startDeleteFromMenu()">删除</div>
  </div>

  <!-- 会话「移动到…」的目标分组选择：沿用右键菜单外观，不另造一套弹层样式。
       只列已存在的项目分组（含「对话」GLOBAL），排除会话当前所在分组。 -->
  <div
    v-if="moveMenu.show"
    ref="moveMenuEl"
    class="session-ctx-menu"
    data-test="session-move-menu"
    :style="{ left: `${moveMenu.x}px`, top: `${moveMenu.y}px` }"
    @contextmenu.prevent
    @click.stop
  >
    <div
      v-if="!moveMenuFromGlobal"
      class="ctx-item"
      data-test="move-target-global"
      @click="chooseMoveTarget('GLOBAL')"
    >对话</div>
    <div
      v-for="g in moveTargets"
      :key="g.folder_path"
      class="ctx-item"
      data-test="move-target"
      :title="g.folder_path"
      @click="chooseMoveTarget(g.folder_path)"
    >{{ getFolderTitle(g) }}</div>
    <div v-if="!moveTargets.length && moveMenuFromGlobal" class="ctx-item disabled">
      没有其他分组
    </div>
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
          <h3>{{ remoteBindMode === 'mobile' ? '移动端' : 'web远端' }}</h3>
          <p>{{ remoteBindMode === 'mobile'
            ? '新版独立移动端界面，与经典界面互不影响'
            : '经典响应式网页，扫码即可作为第二个前端控制本机' }}</p>
        </div>
      </div>
      <!-- 版本切换：两个前端共用同一配对 token，只切换 URL 路径 -->
      <div class="bind-switch" role="tablist">
        <button
          role="tab"
          data-test="bind-mode-mobile"
          :class="{ active: remoteBindMode === 'mobile' }"
          @click="switchRemoteBindMode('mobile')"
        >移动端</button>
        <button
          role="tab"
          data-test="bind-mode-desktop"
          :class="{ active: remoteBindMode === 'desktop' }"
          @click="switchRemoteBindMode('desktop')"
        >web远端</button>
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

  <!-- 秘书弹窗：新建时先起名（名字是它的身份来源），并可选填专属人设；
       编辑时只改人设。每个秘书都是独立的 agent，人设只属于这个会话，
       与设置页的星瑶人设无关。人设留空 = 内置默认秘书人设。
       Enter 提交、Esc 取消、点遮罩关闭；新建时名字去首尾空白后为空或
       超 20 字符则创建按钮禁用（与后端校验同口径） -->
  <div
    v-if="secretaryModal.show"
    class="modal-mask"
    data-test="secretary-name-modal"
    @click.self="closeSecretaryModal()"
  >
    <div class="modal">
      <div class="modal-head">
        <div class="mh-ico"><AppIcon name="pencil" :size="16" /></div>
        <div>
          <h3>{{ secretaryModal.mode === 'create' ? '给秘书起个名字' : `编辑「${secretaryModal.display}」的人设` }}</h3>
          <p v-if="secretaryModal.mode === 'create'">名字只属于这个会话：界面显示与它的自称都用它，与设置页人设无关</p>
          <p v-else>人设只属于这位秘书：它是个独立 agent，与设置页的星瑶人设无关</p>
        </div>
      </div>
      <div class="modal-body">
        <input
          v-if="secretaryModal.mode === 'create'"
          ref="secretaryNameInput"
          v-model="secretaryNameDraft"
          class="s-name-input"
          data-test="secretary-name-input"
          placeholder="秘书名字（1-20 个字符）"
          @keydown.enter="onSecretaryNameEnter($event)"
          @keydown.esc="onSecretaryNameEsc($event)"
        >
        <!-- 校验失败原因就地说明：按钮禁用时用户能看到为什么不能创建 -->
        <div
          v-if="secretaryModal.mode === 'create'"
          class="s-name-hint"
          data-test="secretary-name-hint"
        >{{ secretaryNameHint }}</div>
        <label class="s-persona-label" for="secretary-persona-input">专属人设（可选）</label>
        <textarea
          id="secretary-persona-input"
          ref="secretaryPersonaInput"
          v-model="secretaryPersonaDraft"
          class="s-persona-input"
          data-test="secretary-persona-input"
          rows="8"
          placeholder="留空就使用内置默认秘书人设。想让她多大程度上像「你自己造出来的人」，就把身份、性格、说话风格、做事偏好写清楚。例如：你是做事讲究先后的行政秘书，说话简短直接，涉及日程和邮件时先给结论再给依据。"
          @keydown.esc="onSecretaryNameEsc($event)"
        ></textarea>
        <div class="s-name-hint" data-test="secretary-persona-hint">{{ secretaryPersonaHint }}</div>
      </div>
      <div class="modal-foot">
        <button class="btn" data-test="secretary-name-cancel" @click="closeSecretaryModal()">取消</button>
        <button
          class="btn primary"
          data-test="secretary-name-create"
          :disabled="!secretarySubmitValid"
          @click="submitSecretaryModal()"
        >{{ secretaryModal.mode === 'create' ? '创建' : '保存' }}</button>
      </div>
    </div>
  </div>

  <!-- 基元律动账号登录窗：左下角用户卡片点击后打开，登录成功自动关窗 -->
  <TokenRhythmLoginModal />
</template>

<script setup lang="ts">
import { ref, computed, reactive, nextTick, onMounted, onUnmounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';
import TokenRhythmLoginModal from './TokenRhythmLoginModal.vue';
import TokenRhythmAccountPanel from './TokenRhythmAccountPanel.vue';
import TokenRhythmAccountDialog from './TokenRhythmAccountDialog.vue';
import type { SessionGroup, SessionInfo } from '../types/pywebview';
import { useIsMobile } from '../composables/useViewport';
import { shortLabel } from '../utils/shortLabel';

const store = useAppStore();

// 左下角卡片点击：已登录 → 切换账号浮层；未登录 → 打开登录弹窗
function onUserCardClick() {
  if (store.trAuth.status === 'loggedIn') store.toggleTrAccountPanel();
  else store.openTrLoginModal();
}

// 基元律动账号名（左下角用户卡片）：user 字段结构由后端决定，前端按常见字段
// 兜底，全都拿不到就回落到产品名，避免登录后卡片显示空白
const trUserLabel = computed(() => {
  if (store.trAuth.status !== 'loggedIn') return '星瑶Code';
  const user = (store.trAuth.user || {}) as Record<string, any>;
  return String(user.name || user.nickname || user.phone || user.account || '星瑶Code');
});
// 头像文字：已登录取账号名首字，未登录沿用原来的产品首字母 Y
const trAvatarText = computed(() => (
  store.trAuth.status === 'loggedIn' ? (trUserLabel.value.slice(0, 1).toUpperCase() || 'Y') : 'Y'
));
// 卡片上只显示前 6 个字符：账号名可以任意长，全量渲染会把右侧设置/手机图标
// 挤出侧栏；完整名字挂在 <b> 的 title 上作悬停气泡。
const trUserLabelShort = computed(() => shortLabel(trUserLabel.value));
// 悬停气泡只对已登录账号给出完整账号名；未登录时按钮自身的 title 已是引导文案，
// 这里返回 undefined 让属性不渲染，避免气泡内容重复。
const trUserLabelTitle = computed(() => (
  store.trAuth.status === 'loggedIn' ? trUserLabel.value : undefined
));

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

/** 展示顺序：秘书分组与「对话」恒定在前，项目分组按自定义顺序，未记录的排在最后 */
const orderedGroups = computed<SessionGroup[]>(() => {
  const groups = store.sessionGroups || [];
  // 秘书分组由后端固定排在首个，产品要求它显示在「对话」之上，故与 GLOBAL 一样
  // 从项目排序里摘出来单独置顶：它是虚拟分组，混进自定义顺序会与后端顺序打架
  const secretaries = groups.filter((g) => !!g.is_secretary);
  const globals = groups.filter((g) => isGlobalGroup(g));
  const projects = groups.filter((g) => !isGlobalGroup(g) && !g.is_secretary);
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
  return [...secretaries, ...globals, ...ranked];
});

/** 当前展示顺序下的项目路径列表（作为写回持久化的基准；秘书分组不可排序，故排除） */
function currentProjectOrder(): string[] {
  return orderedGroups.value
    .filter((g) => !isGlobalGroup(g) && !g.is_secretary)
    .map((g) => g.folder_path);
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
  // 归档会话列表同样必须等桥就绪后再拉：api 对象先注入、方法后注入，
  // 抢跑会拿到「方法不存在」的空结果，表现为归档区永远是 0
  void store.loadArchivedSessions();
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
// 正在等待二次确认归档的会话名（同处时间位置，确认按钮用主色区分可逆操作）
const confirmArchiveName = ref('');
// 手机访问绑定信息（点击手机图标后弹出二维码，弹窗内可切换移动端 / web远端）
// permanent=true 表示永久有效（后端 expires_in=0），不做倒计时自动刷新
const remoteBind = ref<null | { url: string; qr: string; expires_in: number; permanent?: boolean }>(null);
// 配对链接刷新中标记：刷新期间禁用按钮避免重复点击
const remoteBindRefreshing = ref(false);
// 绑定目标：mobile=新版独立移动端（/m/）/ desktop=经典响应式网页（/）。
// 默认移动端；用 localStorage 记住上次选择（沿用 'ycode-*' 键前缀约定）。
type RemoteBindMode = 'desktop' | 'mobile';
const REMOTE_BIND_MODE_KEY = 'ycode-remote-bind-mode';
function loadRemoteBindMode(): RemoteBindMode {
  try {
    return localStorage.getItem(REMOTE_BIND_MODE_KEY) === 'desktop' ? 'desktop' : 'mobile';
  } catch {
    // 隐私模式等 localStorage 不可用时退回默认值
    return 'mobile';
  }
}
const remoteBindMode = ref<RemoteBindMode>(loadRemoteBindMode());
// 过期检查定时器：弹窗打开期间每秒检测，过期自动刷新 token
let remoteBindExpiryTimer: ReturnType<typeof setInterval> | null = null;

// 会话右键菜单：show 为浮层开关，x/y 为鼠标坐标（渲染后按视口边缘收拢）
const MENU_EDGE = 8;
const ctxMenu = reactive({ show: false, x: 0, y: 0, name: '', display: '', path: '', secretary: false });
const ctxMenuEl = ref<HTMLElement | null>(null);
// 就地重命名态：editingName 为会话名（唯一定位条目），editingValue 为输入框内容
const editingName = ref('');
const editingValue = ref('');
// 进入编辑态时的原始显示名，用于判断"未改动"
let editingOriginal = '';
const renameInputEl = ref<HTMLInputElement | null>(null);

// 秘书弹窗：mode='create' 由秘书分组 ＋ 打开（起名 + 可选人设并建会话），
// mode='edit' 由右键「编辑人设」打开（只改该秘书的人设）。名字池随机预填一个推荐名
const SECRETARY_NAME_POOL = ['星河', '小满', '望舒', '清和', '云疏', '拾光'];
// 与后端 core.secretary.validate_secretary_name 同口径：去首尾空白后 1-20 字符
const SECRETARY_NAME_MAX_LEN = 20;
// 与后端 core.secretary.MAX_PERSONA_LEN 同口径；留空合法（= 内置默认秘书人设）
const SECRETARY_PERSONA_MAX_LEN = 20000;
const secretaryModal = reactive({
  show: false,
  mode: 'create' as 'create' | 'edit',
  session: '',
  display: '',
});
const secretaryNameDraft = ref('');
const secretaryPersonaDraft = ref('');
const secretaryNameInput = ref<HTMLInputElement | null>(null);
const secretaryPersonaInput = ref<HTMLTextAreaElement | null>(null);
// 规范化名字：去首尾空白后按码点计数与后端 len() 对齐（emoji 不算两个字符）
const secretaryNameNormalized = computed(() => secretaryNameDraft.value.trim());
const secretaryNameValid = computed(() => {
  const name = secretaryNameNormalized.value;
  return name.length > 0 && [...name].length <= SECRETARY_NAME_MAX_LEN;
});
const secretaryNameHint = computed(() => {
  const name = secretaryNameNormalized.value;
  if (!name) return '请输入名字：它会成为这位秘书的称呼';
  if ([...name].length > SECRETARY_NAME_MAX_LEN) {
    return `最多 ${SECRETARY_NAME_MAX_LEN} 个字符，当前 ${[...name].length} 个`;
  }
  return '';
});
// 人设留空视为"没填"，直接放行（走内置默认秘书人设），只有超长才拦
const secretaryPersonaNormalized = computed(() => secretaryPersonaDraft.value.trim());
const secretaryPersonaHint = computed(() => {
  const len = secretaryPersonaNormalized.value.length;
  if (len > SECRETARY_PERSONA_MAX_LEN) {
    return `最多 ${SECRETARY_PERSONA_MAX_LEN} 个字符，当前 ${len} 个`;
  }
  return secretaryModal.mode === 'edit' ? '留空即恢复内置默认秘书人设' : '';
});
const secretarySubmitValid = computed(() => {
  if (secretaryPersonaNormalized.value.length > SECRETARY_PERSONA_MAX_LEN) return false;
  // 编辑态只改人设，没有名字输入框，不必校验名字
  return secretaryModal.mode === 'edit' ? true : secretaryNameValid.value;
});

// QQ2009 皮肤：用户卡昵称可就地编辑（localStorage 持久化，默认「木偶」）
const QQ09_NAME_KEY = 'qq09.sidebarUserName';
const qq09UserName = ref(localStorage.getItem(QQ09_NAME_KEY)?.trim() || '木偶');
const qq09EditingName = ref(false);
const qq09NameDraft = ref('');
const qq09NameInputRef = ref<HTMLInputElement | null>(null);
function qq09StartEdit() {
  qq09HideTip();
  qq09NameDraft.value = qq09UserName.value;
  qq09EditingName.value = true;
  nextTick(() => qq09NameInputRef.value?.focus());
}
function qq09SaveName() {
  const v = qq09NameDraft.value.trim();
  if (v) {
    qq09UserName.value = v;
    localStorage.setItem(QQ09_NAME_KEY, v);
  }
  qq09EditingName.value = false;
}
function qq09CancelEdit() { qq09EditingName.value = false; }

// QQ2009 皮肤：用户卡个性签名可就地编辑（与昵称同款，localStorage 持久化）。
// 签名允许改短但不允许空：trim 后为空即回退默认签名，同时也写回存储。
const QQ09_SIGN_KEY = 'qq09.sidebarSign';
const QQ09_SIGN_DEFAULT = '代码写完了吗，就刷 QQ？';
const qq09UserSign = ref(localStorage.getItem(QQ09_SIGN_KEY)?.trim() || QQ09_SIGN_DEFAULT);
const qq09EditingSign = ref(false);
const qq09SignDraft = ref('');
const qq09SignInputRef = ref<HTMLInputElement | null>(null);
function qq09StartSignEdit() {
  qq09HideTip();
  qq09SignDraft.value = qq09UserSign.value;
  qq09EditingSign.value = true;
  nextTick(() => qq09SignInputRef.value?.focus());
}
function qq09SaveSign() {
  const next = qq09SignDraft.value.trim() || QQ09_SIGN_DEFAULT;
  qq09UserSign.value = next;
  localStorage.setItem(QQ09_SIGN_KEY, next);
  qq09EditingSign.value = false;
}
function qq09CancelSign() { qq09EditingSign.value = false; }

// QQ2009 皮肤：用户卡自定义气泡提示（网名 / 签名），替代浏览器默认 title。
// 悬停约 350ms 后、且文本确实被省略号截断（scrollWidth > clientWidth）才显示；
// 未截断时弹气泡纯属冗余。移出 / 点击 / 滚动 / 进入编辑态立即隐藏。
const QQ09_TIP_DELAY = 350;
// 元素离视口顶部小于此值时气泡改落到下方（并用 .flipped 翻转小三角）
const QQ09_TIP_FLIP_TOP = 60;
const qq09Tip = reactive({ show: false, text: '', top: 0, left: 0, flipped: false });
let qq09TipTimer: ReturnType<typeof setTimeout> | null = null;
function qq09HideTip() {
  if (qq09TipTimer !== null) {
    clearTimeout(qq09TipTimer);
    qq09TipTimer = null;
  }
  if (qq09Tip.show) qq09Tip.show = false;
}
/** 悬停延迟后在元素上方（空间不足则下方）弹出完整文本气泡 */
function qq09ShowTip(e: MouseEvent, text: string) {
  const el = e.currentTarget as HTMLElement | null;
  if (!el) return;
  qq09HideTip();
  qq09TipTimer = setTimeout(() => {
    qq09TipTimer = null;
    // 文本完整可见时不弹（scrollWidth 等于 clientWidth 即未截断）
    if (el.scrollWidth <= el.clientWidth) return;
    const rect = el.getBoundingClientRect();
    qq09Tip.text = text;
    // 水平居中于元素；left/top 用视口坐标，配合 .qq09-tip 的 fixed 定位
    qq09Tip.left = rect.left + rect.width / 2;
    qq09Tip.flipped = rect.top < QQ09_TIP_FLIP_TOP;
    qq09Tip.top = qq09Tip.flipped ? rect.bottom + 5 : rect.top - 5;
    qq09Tip.show = true;
  }, QQ09_TIP_DELAY);
}

// QQ2009 皮肤：用户卡在线状态可切换（localStorage 持久化，缺省 online）
// 六个状态与 qq2009.css 一一对应：.qq09-st-icon[data-st=*] 是菜单/状态行图标，
// .qq09-status-dot[data-status=*] 是头像右下角的状态点。
const QQ09_STATUS_KEY = 'qq09.sidebarStatus';
const QQ09_STATUSES = [
  { id: 'online', label: '我在线上' },
  { id: 'busy', label: '忙碌中' },
  { id: 'away', label: '离开' },
  { id: 'dnd', label: '请勿打扰' },
  { id: 'qme', label: 'Q我吧' },
  { id: 'offline', label: '隐身' },
] as const;
type Qq09StatusId = (typeof QQ09_STATUSES)[number]['id'];
/** 读 localStorage；非法/缺失值一律退回 online（与昵称同款就地持久化做法） */
function loadQq09Status(): Qq09StatusId {
  const saved = localStorage.getItem(QQ09_STATUS_KEY);
  return QQ09_STATUSES.some((s) => s.id === saved) ? (saved as Qq09StatusId) : 'online';
}
const qq09Status = ref<Qq09StatusId>(loadQq09Status());
const qq09StatusMenuOpen = ref(false);
// 触发器 + 下拉菜单的共同包裹层：外点判定用它，避免误伤菜单内部点击
const qq09StatusRef = ref<HTMLElement | null>(null);
const qq09StatusLabel = computed(
  () => QQ09_STATUSES.find((s) => s.id === qq09Status.value)?.label ?? '我在线上',
);
function qq09ToggleStatusMenu() { qq09StatusMenuOpen.value = !qq09StatusMenuOpen.value; }
function qq09CloseStatusMenu() { qq09StatusMenuOpen.value = false; }
function qq09SelectStatus(id: Qq09StatusId) {
  qq09Status.value = id;
  localStorage.setItem(QQ09_STATUS_KEY, id);
  qq09CloseStatusMenu();
}

// QQ2009 皮肤：用户卡 QQ 等级展示（装饰性，图标纯 CSS 绘制见 qq2009.css）。
// QQ 等级换算：1 太阳 = 16 级，1 月亮 = 4 级，1 星星 = 1 级（皇冠 64 级暂用不到，
// 真到那天再说）。图标顺序与 QQ 客户端一致：太阳在前，月亮居中，星星收尾。
const QQ09_LEVEL = 5;
const QQ09_LEVEL_ICONS: ReadonlyArray<'sun' | 'moon' | 'star'> = [
  ...Array.from({ length: Math.floor(QQ09_LEVEL / 16) }, () => 'sun' as const),
  ...Array.from({ length: Math.floor((QQ09_LEVEL % 16) / 4) }, () => 'moon' as const),
  ...Array.from({ length: QQ09_LEVEL % 4 }, () => 'star' as const),
];

// QQ2009 皮肤：用户卡头像可点击更换（DataURL 持久化，默认企鹅底图）。
// 2MB 上限的理由：localStorage 配额通常只有 5MB，DataURL 还要 base64 膨胀约 1/3，
// 超过这个体积既会撑爆配额、也让同步读写明显变慢；2MB 以内的 GIF 动图能完整落盘。
// 动图支持：GIF 的 DataURL 仍是标准 image/gif 资源，塞进 <img src> 后浏览器按帧
// 自动播放，无需 CSS 精灵图或 JS 逐帧控制。
const QQ09_AVATAR_KEY = 'qq09.sidebarAvatar';
const QQ09_AVATAR_MAX_BYTES = 2 * 1024 * 1024;
/** 读已存头像 DataURL；存储不可用 / 内容损坏（非 data:image/* 前缀）一律按无自定义头像处理 */
function loadQq09Avatar(): string {
  try {
    const raw = localStorage.getItem(QQ09_AVATAR_KEY);
    return raw && raw.startsWith('data:image/') ? raw : '';
  } catch {
    // localStorage 不可用（隐私模式/配额满）时退回默认企鹅头像
    return '';
  }
}
const qq09AvatarData = ref(loadQq09Avatar());
const qq09AvatarInputRef = ref<HTMLInputElement | null>(null);

/** 点头像：转交隐藏文件框；先清空 value 以便连续两次选同一个文件也能触发 change */
function qq09PickAvatar() {
  qq09HideTip();
  const input = qq09AvatarInputRef.value;
  if (!input) return;
  input.value = '';
  input.click();
}

/** 选文件后：校验类型/体积 → FileReader 读 DataURL → 写内存态 + localStorage */
function qq09OnAvatarFile(e: Event) {
  const input = e.target as HTMLInputElement | null;
  const file = input?.files?.[0];
  if (!file) return;
  // 非图片直接忽略（accept 只是过滤器，用户仍可切到「所有文件」）
  if (!file.type.startsWith('image/')) return;
  if (file.size > QQ09_AVATAR_MAX_BYTES) {
    alert('图片不能超过 2MB（localStorage 容量有限，建议压缩后再传）');
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    const data = typeof reader.result === 'string' ? reader.result : '';
    if (!data.startsWith('data:image/')) return;
    qq09AvatarData.value = data;
    try {
      localStorage.setItem(QQ09_AVATAR_KEY, data);
    } catch {
      /* 配额满时仅本次会话生效：头像已显示，只是不持久化，不打扰用户 */
    }
  };
  // 读取失败（文件被移走/无权限）静默忽略，头像保持原样
  reader.onerror = () => {};
  reader.readAsDataURL(file);
}

/** 恢复默认头像：清内存态并删键，回到 CSS 画的企鹅底图 */
function qq09ResetAvatar() {
  qq09AvatarData.value = '';
  try {
    localStorage.removeItem(QQ09_AVATAR_KEY);
  } catch {
    /* 存储不可用时仅清掉本次会话的状态 */
  }
}

function openCtxMenu(e: MouseEvent, s: SessionInfo, group: SessionGroup) {
  cancelRename();
  ctxMenu.name = s.name;
  ctxMenu.display = s.display_name || s.name;
  // 记下所属分组：菜单里的上移 / 下移 / 置顶要按组内顺序计算
  ctxMenu.path = group.folder_path;
  // 秘书会话不提供「移动到…」：它的归属由 source 决定，改 workdir 不会离开「秘书」分组
  ctxMenu.secretary = !!s.secretary_name;
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
  ctxMenu.secretary = false;
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

// 点秘书分组的 ＋：打开起名弹窗（先把菜单收掉，避免两个浮层叠着）。
// 预填推荐名并全选：用户想沿用推荐名可直接回车，想换名直接键入即可覆盖。
// 人设预填为空 = 默认秘书人设
function openSecretaryNameModal() {
  closeCtxMenu();
  closeGroupMenu();
  secretaryModal.mode = 'create';
  secretaryModal.session = '';
  secretaryModal.display = '';
  secretaryNameDraft.value = SECRETARY_NAME_POOL[Math.floor(Math.random() * SECRETARY_NAME_POOL.length)];
  secretaryPersonaDraft.value = '';
  secretaryModal.show = true;
  nextTick(() => {
    secretaryNameInput.value?.focus();
    secretaryNameInput.value?.select();
  });
}

// 右键「编辑人设」：只对秘书会话出现，用侧栏条目里已下发的人设原文回填
function openSecretaryPersonaModal() {
  const { name, display } = ctxMenu;
  closeCtxMenu();
  if (!name) return;
  const entry = store.sessionGroups
    .flatMap((g) => g.sessions || [])
    .find((s) => s.name === name);
  secretaryModal.mode = 'edit';
  secretaryModal.session = name;
  secretaryModal.display = display;
  secretaryNameDraft.value = '';
  secretaryPersonaDraft.value = String((entry && entry.secretary_persona) || '');
  secretaryModal.show = true;
  nextTick(() => {
    secretaryPersonaInput.value?.focus();
    // 光标落到末尾：编辑现有人设时接着写，而不是把原文全选覆盖掉
    const len = secretaryPersonaDraft.value.length;
    secretaryPersonaInput.value?.setSelectionRange(len, len);
  });
}

function closeSecretaryModal() {
  secretaryModal.show = false;
  secretaryModal.mode = 'create';
  secretaryModal.session = '';
  secretaryModal.display = '';
  secretaryNameDraft.value = '';
  secretaryPersonaDraft.value = '';
}

// 提交：先校验（非法值不发请求，也不静默截断），关弹窗后交给 store。
// 不 await：新建 / 保存失败由 store 弹可见提示，弹窗不必卡在加载态里
function submitSecretaryModal() {
  if (!secretarySubmitValid.value) return;
  const persona = secretaryPersonaNormalized.value;
  if (secretaryModal.mode === 'edit') {
    const session = secretaryModal.session;
    closeSecretaryModal();
    void store.updateSecretaryPersona(session, persona);
    return;
  }
  const name = secretaryNameNormalized.value;
  closeSecretaryModal();
  void store.createSecretarySession(name, persona);
}

// Enter 提交（仅名字输入框）：沿用重命名输入框的 IME 守卫——组合态下这次
// Enter 是确认候选词，此刻文字还没上屏，提交会把中间态当名字发出去。
// 人设是多行文本域，Enter 保留换行语义，不在这里提交
function onSecretaryNameEnter(e: KeyboardEvent) {
  if (isImeComposing(e)) return;
  e.preventDefault();
  submitSecretaryModal();
}

// Esc 取消：组合态是取消候选词，不应关弹窗
function onSecretaryNameEsc(e: KeyboardEvent) {
  if (isImeComposing(e)) return;
  e.preventDefault();
  closeSecretaryModal();
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

// 会话「移动到…」：跨文件夹改归属（后端只改 sessions.workdir，消息与模型绑定不动）。
// 目标列表只列已存在的项目分组，并排除会话当前所在分组；「对话」(GLOBAL) 作为反向
// 目标单独提供。秘书分组（folder_path='SECRETARY'）不是磁盘目录，不能作为移动目标。
const moveMenu = reactive({ show: false, x: 0, y: 0, name: '', fromPath: '' });
const moveMenuEl = ref<HTMLElement | null>(null);

// 当前会话是否已在「对话」分组：在的话不需要再给「对话」这一项
const moveMenuFromGlobal = computed(() => !moveMenu.fromPath || moveMenu.fromPath === 'GLOBAL');
const moveTargets = computed(() =>
  (store.sessionGroups || []).filter(
    (g) => !isGlobalGroup(g) && !g.is_secretary && g.folder_path !== moveMenu.fromPath,
  ),
);

function openMoveMenu() {
  const { path, name, x, y } = ctxMenu;
  closeCtxMenu();
  if (!name) return;
  moveMenu.name = name;
  moveMenu.fromPath = path || 'GLOBAL';
  moveMenu.x = x;
  moveMenu.y = y;
  moveMenu.show = true;
  nextTick(clampMoveMenu);
}

function clampMoveMenu() {
  const { x, y } = clampToViewport(moveMenuEl.value, moveMenu.x, moveMenu.y);
  moveMenu.x = x;
  moveMenu.y = y;
}

function closeMoveMenu() {
  moveMenu.show = false;
  moveMenu.name = '';
  moveMenu.fromPath = '';
}

async function chooseMoveTarget(target: string) {
  const name = moveMenu.name;
  closeMoveMenu();
  if (!name || !target) return;
  await store.moveSessionToWorkdir(name, target);
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

// 会话菜单、分组菜单、移动目标菜单互斥，统一收口关闭（滚动 / 缩放时顺带收掉 QQ2009 气泡）
function closeSidebarMenus() {
  closeCtxMenu();
  closeGroupMenu();
  closeMoveMenu();
  qq09HideTip();
}

// 菜单关闭时机：点击菜单外任意位置（点在菜单内不关，避免抢在 click 之前移除节点）
function onDocMouseDown(e: MouseEvent) {
  // 气泡是纯悬停提示：任何点击都立即隐藏，避免残留
  qq09HideTip();
  // QQ2009 状态菜单同款外点关闭：点在「触发器 + 下拉」包裹层外才关
  // （层内的 mousedown 一律放行，交给触发器的 click 做开合、菜单项的 click 做选中）
  if (qq09StatusMenuOpen.value) {
    const wrap = qq09StatusRef.value;
    if (!(wrap && e.target instanceof Node && wrap.contains(e.target))) qq09CloseStatusMenu();
  }
  if (!ctxMenu.show && !groupMenu.show && !moveMenu.show) return;
  const el = ctxMenu.show ? ctxMenuEl.value : (groupMenu.show ? groupMenuEl.value : moveMenuEl.value);
  if (el && e.target instanceof Node && el.contains(e.target)) return;
  closeSidebarMenus();
}

function onDocKeyDown(e: KeyboardEvent) {
  // 组合态 Esc 属于「取消候选词」，冒泡到这里时不能顺带退出编辑态 / 关菜单
  if (isImeComposing(e)) return;
  if (e.key === 'Escape') {
    closeSidebarMenus();
    qq09CloseStatusMenu();
    cancelRename();
    // 秘书弹窗也吃 Esc（输入框已聚焦时由它自己的 keydown.esc 先关，
    // 这里兜住焦点不在输入框上的情况）
    closeSecretaryModal();
  }
}

onMounted(() => {
  // 捕获阶段监听 scroll：侧栏内部滚动也能关闭菜单
  window.addEventListener('scroll', closeSidebarMenus, true);
  window.addEventListener('resize', closeSidebarMenus);
  document.addEventListener('mousedown', onDocMouseDown);
  document.addEventListener('keydown', onDocKeyDown);

  // 账号状态是左下角用户卡片的数据源：启动后静默探测一次（未登录不弹提示）
  if (store.trAuth.status === 'unknown') void store.refreshTrAuth();

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

// 删除项目分组：先弹确认框（带分组名与组内对话数），确认后才真正移出分组。
// 后端 delete_folder_group 只删分组内的对话记录，不动磁盘上的项目文件。
async function askDeleteGroup(group: SessionGroup) {
  const title = getFolderTitle(group);
  const count = group.sessions ? group.sessions.length : 0;
  const detail = count > 0 ? `组内 ${count} 个对话记录会一并删除，` : '该分组内暂无对话记录，';
  const ok = await store.askConfirm(
    '删除项目分组',
    '仅移出侧栏分组，磁盘文件不受影响',
    `确定要删除项目分组「${title}」吗？${detail}磁盘上的项目文件不会被删除。`,
  );
  if (!ok) return;
  await store.deleteFolderGroup(group.folder_path, title);
}

function askDelete(name: string) {
  confirmDeleteName.value = name;
}

function confirmDelete(name: string) {
  confirmDeleteName.value = '';
  store.deleteSingleSession(name);
}

// 归档（行内 ⤓）：与删除同一套行内二次确认——首击只进入确认态，点确认才真正归档。
// 归档可逆但会把会话挪出列表，误点后要找回来得进设置页，故不直接执行。
// 确认后由 store 静默完成刷新；当前活跃会话在按钮上已置灰，store 里还有一道同源保护。
function askArchive(name: string) {
  confirmDeleteName.value = '';
  confirmArchiveName.value = name;
}

function confirmArchive(name: string) {
  confirmArchiveName.value = '';
  void store.archiveSession(name);
}

function onSessionClick(s: { name: string; display_name?: string; workdir?: string }) {
  // 处于二次确认态（删除 / 归档）时，点击条目其它区域视为取消，不切换会话
  if (confirmDeleteName.value === s.name || confirmArchiveName.value === s.name) {
    confirmDeleteName.value = '';
    confirmArchiveName.value = '';
    return;
  }
  confirmDeleteName.value = '';
  confirmArchiveName.value = '';
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

// 打开手机访问绑定弹窗：向后端请求二维码与配对链接。
// 后端 token 固定：未过期直接复用，应用重启也不变；过期由后端自动重签。
async function fetchRemoteBind(forceNew: boolean) {
  const api = window.pywebview && window.pywebview.api;
  if (!api) {
    store.notifyModal('手机访问', '后端连接失败：pywebview API 不可用');
    return;
  }
  try {
    // get_remote_bind_qr 为后端桥接方法，前端类型契约尚未收录，故此处按返回结构取用；
    // mobile 模式追加第二参数绑定独立移动端（/m/），web远端保持单参签名不变
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
      store.notifyModal('手机访问', (result && result.msg) || '获取绑定二维码失败');
    } else {
      // 手动刷新失败时保留旧弹窗内容，仅提示
      store.notifyModal('手机访问', (result && result.msg) || '刷新配对链接失败');
    }
  } catch (err) {
    store.notifyModal('手机访问', String(err));
  } finally {
    remoteBindRefreshing.value = false;
  }
}

// 单一入口：按当前（或上次记住的）版本打开绑定弹窗
async function openRemoteBind() {
  await fetchRemoteBind(false);
}

// 弹窗内切换版本：只换 URL 路径，复用同一 token（后端未过期直接复用，
// 不会重签 token，也不会踢掉已扫码连接的手机）
async function switchRemoteBindMode(mode: RemoteBindMode) {
  if (remoteBindMode.value === mode) return;
  remoteBindMode.value = mode;
  try {
    localStorage.setItem(REMOTE_BIND_MODE_KEY, mode);
  } catch {
    // localStorage 不可用时仅本次生效
  }
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
  /* 右侧操作区（归档 ⤓ right:26px / 删除 ✕ right:6px 均为 absolute 定位）需要约 44px，
     这里给时间预留等宽并右对齐，避免 hover 时按钮压在时间文字上 */
  min-width: 44px;
  text-align: right;
}
/* hover 时时间让位给操作按钮：两层文字不再叠在一起 */
.session-item:hover .m {
  visibility: hidden;
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
/* 归档按钮与删除按钮同处 hover 操作区：删除贴最右，归档紧邻其左 */
.session-item .s-archive {
  position: absolute;
  right: 26px;
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
.session-item:hover .s-archive {
  opacity: 1;
}
.session-item .s-archive:hover {
  color: var(--accent);
  background: var(--bg3);
}
/* 当前活跃会话不可归档：置灰且不可点（tooltip 说明原因） */
.session-item .s-archive.disabled,
.session-item:hover .s-archive.disabled {
  opacity: 0.3;
  cursor: not-allowed;
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
/* 归档的确认按钮与删除同处操作区，但用主色表示这是可逆操作 */
.session-item .s-confirm.archive {
  right: 26px;
  background: var(--accent);
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
  /* 左侧用户卡片占满剩余宽度，右侧两个图标按钮各自固定 32px */
  grid-template-columns: 1fr auto auto;
  gap: 3px;
}
/* 底部容器：账号浮层（TokenRhythmAccountPanel）以它为定位基准，
   absolute + bottom: calc(100% + 8px) 即贴在用户卡片正上方。
   浮层不放在 .side-footer 内部：主题 CSS 里的 `#sidebar .side-footer button`
   会连带改掉浮层按钮外观，这里避让开。 */
.side-bottom {
  position: relative;
  flex: 0 0 auto;
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
/* 底部用户卡片：可点击打开基元律动登录窗（圆形头像 + 名称），hover 反馈
   沿用 .side-footer button 的统一样式 */
.side-footer .side-user {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 5px 7px;
  color: var(--text-dim);
  /* 名字已按 6 字符截断，这里再允许收缩一次：即使出现超长/不可断文本，
     也只让名字自己省略，不把右侧设置与手机图标挤出网格 */
  min-width: 0;
  overflow: hidden;
}
.side-footer .side-avatar {
  flex: 0 0 26px;
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: var(--bg3);
  color: var(--text);
  font-size: 12px;
  font-weight: 600;
}
/* 底部右侧图标按钮：设置与手机访问共用同一形态（32×32 圆形热区，与顶栏图标按钮同口径） */
.side-footer .icon-btn {
  width: 32px;
  justify-content: center;
  padding: 7px;
  gap: 0;
  border-radius: 50%;
}
.footer-copy { display: flex; flex-direction: column; min-width: 0; }
.footer-copy b { font-size: 12.5px; color: inherit; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 手机访问绑定弹窗（结构参照 ConfirmModal 的 modal-mask 风格） */
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
/* 版本切换分段控件（移动端 / web远端，共用同一配对 token） */
.bind-switch {
  display: flex;
  gap: 4px;
  margin: 12px 18px 0;
  padding: 3px;
  background: var(--bg3, rgba(255, 255, 255, 0.04));
  border: 1px solid var(--border);
  border-radius: 9px;
}
.bind-switch button {
  flex: 1;
  padding: 7px 0;
  border: none;
  background: none;
  color: var(--text-dim);
  font-size: 12.5px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}
.bind-switch button.active {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
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

/* 秘书起名弹窗的输入框：样式沿用侧栏就地重命名输入框（.s-rename-input）的观感，
   只是占满弹窗正文宽度（.modal-body 是居中列布局，需要显式 100%） */
.s-name-input {
  width: 100%;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
  padding: 8px 10px;
  outline: none;
}
.s-name-input:focus {
  border-color: var(--accent);
}
.s-name-hint {
  min-height: 15px;
  font-size: 11.5px;
  color: var(--text-faint);
  text-align: left;
  align-self: stretch;
}

/* 秘书人设输入区：与名字输入框同一套观感，纵向可拉伸但限制最小高度，
   让多行人设有足够书写空间又不把弹窗撑出屏幕 */
.s-persona-label {
  font-size: 11.5px;
  color: var(--text-faint);
  text-align: left;
  align-self: stretch;
}
.s-persona-input {
  width: 100%;
  min-height: 130px;
  max-height: 320px;
  resize: vertical;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
  line-height: 1.55;
  padding: 8px 10px;
  outline: none;
}
.s-persona-input:focus {
  border-color: var(--accent);
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
