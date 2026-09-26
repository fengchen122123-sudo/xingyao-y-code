<template>
  <div class="settings-page" :class="{ 'settings-page--mobile': isMobile }">
    <aside class="settings-nav">
      <button class="back-workspace" @click="attemptCloseSettings"><AppIcon name="arrow-left" :size="14" /> 返回工作区</button>
      <div class="nav-section">基础设置</div>
      <button class="nav-item" :class="{ active: activeSection === 'models' }" @click="activeSection = 'models'"><span class="nav-ico"><AppIcon name="file" :size="14" /></span>模型配置</button>
      <button data-test="token-statistics-nav" class="nav-item" :class="{ active: activeSection === 'tokens' }" @click="activeSection = 'tokens'"><span class="nav-ico"><AppIcon name="chart" :size="14" /></span>Token 统计</button>
      <button data-test="token-statistics-legacy-nav" class="nav-item" :class="{ active: activeSection === 'tokens-legacy' }" @click="activeSection = 'tokens-legacy'"><span class="nav-ico"><AppIcon name="chart" :size="14" /></span>Token 统计旧版</button>
      <button data-test="permission-nav" class="nav-item" :class="{ active: activeSection === 'permissions' }" @click="activeSection = 'permissions'"><span class="nav-ico"><AppIcon name="shield" :size="14" /></span>权限与沙盒</button>
      <button data-test="hooks-nav" class="nav-item" :class="{ active: activeSection === 'hooks' }" @click="activeSection = 'hooks'"><span class="nav-ico"><AppIcon name="zap" :size="14" /></span>钩子 Hooks</button>
      <button data-test="appearance-nav" class="nav-item" :class="{ active: activeSection === 'appearance' }" @click="activeSection = 'appearance'"><span class="nav-ico"><AppIcon name="settings" :size="14" /></span>外观与主题</button>
      <button data-test="system-settings-nav" class="nav-item" :class="{ active: activeSection === 'system' }" @click="activeSection = 'system'"><span class="nav-ico"><AppIcon name="settings" :size="14" /></span>系统设置</button>
      <button data-test="persona-nav" class="nav-item" :class="{ active: activeSection === 'persona' }" @click="activeSection = 'persona'"><span class="nav-ico"><AppIcon name="message-square-plus" :size="14" /></span>人设设置</button>
      <div class="nav-section">关于</div>
      <button data-test="about-nav" class="nav-item" :class="{ active: activeSection === 'about' }" @click="activeSection = 'about'"><span class="nav-ico"><AppIcon name="info" :size="14" /></span>关于</button>
      <button data-test="contact-nav" class="nav-item" :class="{ active: activeSection === 'contact' }" @click="activeSection = 'contact'"><span class="nav-ico"><AppIcon name="message-square-plus" :size="14" /></span>联系我们</button>
      <div class="nav-section">法律</div>
      <button data-test="disclaimer-nav" class="nav-item" :class="{ active: activeSection === 'disclaimer' }" @click="activeSection = 'disclaimer'"><span class="nav-ico"><AppIcon name="shield" :size="14" /></span>免责条款</button>
      <div class="nav-spacer"></div>
      <div class="settings-user"><span class="avatar">Y</span><span>星瑶Code</span><AppIcon name="settings" :size="13" /></div>
    </aside>

    <main class="settings-main">
      <template v-if="activeSection === 'models'">
      <header class="settings-header">
        <div>
          <h1>模型配置</h1>
          <p>CLI 与 GUI 共用同一份厂商、模型和 API 配置。</p>
        </div>
        <button class="icon-button" title="刷新模型配置" @click="store.openModelSettings()"><AppIcon name="refresh" :size="14" /></button>
      </header>

      <section class="model-settings-card">
        <aside class="provider-list" v-if="!isMobile || !mobileDetail">
          <div class="list-label">已配置厂商</div>
          <button
            v-for="provider in store.providers"
            :key="provider.id"
            :data-test="`provider-${provider.id}`"
            class="provider-item"
            :class="{ active: selectedKind === 'provider' && selectedId === provider.id }"
            @click="selectProvider(provider.id)"
          >
            <span class="provider-logo cube"><AppIcon name="hexagon" :size="13" /></span>
            <span class="provider-name">{{ provider.name }}</span>
            <span class="provider-state ready"></span>
          </button>
          <div v-if="!store.providers.length" class="provider-empty">暂无已配置厂商</div>

          <button
            data-test="add-provider-toggle"
            class="add-provider-row"
            :class="{ active: showAddProviders }"
            @click="showAddProviders = !showAddProviders"
          >
            <span class="add-provider-label"><AppIcon name="plus" :size="13" /> 添加厂商</span><span><AppIcon :name="showAddProviders ? 'chevron-up' : 'chevron-down'" :size="13" /></span>
          </button>

          <div v-if="showAddProviders" class="add-provider-choices">
            <div class="list-label">预置厂商</div>
            <button
              v-for="preset in store.modelPresets"
              :key="preset.id"
              :data-test="`preset-${preset.id}`"
              class="provider-item"
              :class="{ active: selectedKind === 'preset' && selectedId === preset.id }"
              @click="selectPreset(preset.id)"
            >
              <span class="provider-logo">{{ preset.name.slice(0, 1) }}</span>
              <span class="provider-name">{{ preset.name }}</span>
              <span class="provider-state" :class="{ ready: configuredPresetIds.has(preset.id) }"></span>
            </button>
            <button
              data-test="add-custom-provider"
              class="add-provider-row custom-provider-row"
              :class="{ active: selectedKind === 'custom' }"
              @click="startCustomProvider"
            ><span class="add-provider-label"><AppIcon name="plus" :size="13" /> 添加自定义厂商</span></button>
          </div>

          <!-- 副模型入口（红框位置）：点击后在右侧设置副模型 -->
          <button
            data-test="secondary-entry"
            class="secondary-entry-row"
            :class="{ active: selectedKind === 'secondary' }"
            @click="selectSecondary"
          >
            <span class="provider-logo"><AppIcon name="layers" :size="13" /></span>
            <span class="provider-name">副模型</span>
            <span class="provider-state" :class="{ ready: !!secondaryModelId }"></span>
          </button>
        </aside>

        <div class="provider-detail" v-if="!isMobile || mobileDetail">
          <!-- 手机端钻取：详情顶栏返回列表（桌面端不渲染该按钮） -->
          <button v-if="isMobile" class="mobile-detail-back" @click="closeMobileDetail"><AppIcon name="arrow-left" :size="14" /> 返回列表</button>
          <template v-if="selectedPreset">
            <div class="detail-title-row">
              <div>
                <h2>{{ selectedPreset.name }}</h2>
                <p>预置厂商信息由 CLI 共享目录维护，只需填写 API Key。</p>
              </div>
              <span class="protocol-badge">{{ protocolLabel(selectedPreset.protocol) }}</span>
            </div>
            <label class="field-label">Base URL</label>
            <div class="readonly-field">{{ selectedPreset.base_url }}</div>
            <label class="field-label">API Key</label>
            <div class="key-field">
              <input
                v-model="presetApiKey"
                data-test="preset-api-key"
                :type="showPresetKey ? 'text' : 'password'"
                placeholder="输入 API Key"
              />
              <button @click="showPresetKey = !showPresetKey">{{ showPresetKey ? '隐藏' : '显示' }}</button>
            </div>
            <p class="modal-field-hint">预置厂商仅预填名称与接口地址；添加后通过「获取模型列表」选择模型并自动填充参数。</p>
            <button class="primary-button" :disabled="saving" @click="savePreset">
              {{ configuredPresetIds.has(selectedPreset.id) ? '更新 API Key' : '保存并添加厂商' }}
            </button>
          </template>

          <template v-else-if="selectedProvider">
            <div class="detail-title-row">
              <div>
                <h2>{{ selectedProvider.name }}</h2>
                <p>{{ selectedProvider.preset_id ? '预置厂商配置' : '自定义模型供应商' }}</p>
              </div>
              <button class="danger-icon" title="删除厂商" @click="store.deleteProvider(selectedProvider)"><AppIcon name="trash" :size="16" /></button>
            </div>

            <template v-if="selectedProvider.preset_id">
              <label class="field-label">Base URL</label>
              <div class="readonly-field">{{ selectedProvider.base_url }}</div>
              <label class="field-label">API 格式</label>
              <div class="readonly-field">{{ protocolLabel(selectedProvider.protocol || 'openai') }}</div>
            </template>
            <template v-else>
              <label class="field-label">名称</label>
              <input v-model="providerForm.name" class="text-field" />
              <label class="field-label">Base URL</label>
              <input v-model="providerForm.base_url" class="text-field mono" />
              <label class="field-label">API 格式</label>
              <select v-model="providerForm.protocol" class="text-field">
                <option v-for="protocol in store.modelProtocols" :key="protocol.id" :value="protocol.id">
                  {{ protocol.label }}
                </option>
              </select>
            </template>

            <label class="field-label">API Key</label>
            <div class="key-field">
              <input
                v-model="providerForm.api_key"
                data-test="provider-api-key"
                :type="showProviderKey ? 'text' : 'password'"
                :placeholder="selectedProvider.has_api_key ? '已配置；留空表示不修改' : '输入 API Key'"
              />
              <button
                class="icon-button"
                title="复制 API Key"
                data-test="copy-provider-key"
                :disabled="!selectedProvider.has_api_key && !providerForm.api_key"
                @click="copyProviderKey"
              ><AppIcon :name="keyCopied ? 'check' : 'copy'" :size="14" /></button>
              <button data-test="toggle-provider-key" @click="toggleProviderKey">{{ showProviderKey ? '隐藏' : '显示' }}</button>
            </div>
            <button class="secondary-button" :disabled="saving" @click="saveProvider">保存厂商配置</button>

            <div class="model-list-header">
              <div>
                <h3>模型列表</h3>
                <span>{{ selectedProvider.models?.length || 0 }} 个模型</span>
              </div>
              <div class="model-list-actions">
                <button data-test="add-model" class="secondary-button compact" @click="openModelModal()"><AppIcon name="plus" :size="12" /> 添加模型</button>
              </div>
            </div>
            <div v-if="!selectedProvider.models?.length" class="empty-models">该厂商暂无模型</div>
            <div v-else class="configured-models">
              <div v-for="model in selectedProvider.models" :key="model.id" class="configured-model-item">
                <div class="configured-model-row">
                  <div class="model-main">
                    <span class="mono model-id" :title="model.name">{{ model.name }}</span>
                    <span class="model-meta-ctx">{{ formatContext(model.context_window) }}</span>
                    <span :class="['vision-tag', { on: model.supports_vision }]">
                      {{ model.supports_vision ? '多模态' : '纯文本' }}
                    </span>
                  </div>
                  <span
                    v-if="testResults[model.id] && testResults[model.id].status !== 'fail'"
                    class="test-result"
                    :class="testResults[model.id].status"
                  >
                    <AppIcon
                      v-if="testResults[model.id].status === 'ok'"
                      name="check"
                      :size="12"
                      :stroke-width="2.25"
                    />
                    <AppIcon
                      v-else-if="testResults[model.id].status === 'testing'"
                      name="loader"
                      :size="12"
                      class="icon-spin"
                    />
                    {{ testResults[model.id].msg }}
                  </span>
                  <div class="row-actions">
                    <button :disabled="testingId === model.id" @click="testModel(model)">
                      {{ testingId === model.id ? '测试中…' : '测试' }}
                    </button>
                    <button @click="openModelModal(model)">编辑</button>
                    <button class="danger" @click="store.deleteModel(model)">删除</button>
                  </div>
                </div>

                <!-- 方案 B：行内展开微型警告条 -->
                <div
                  v-if="testResults[model.id]?.status === 'fail'"
                  class="model-error-banner"
                  data-test="model-error-banner"
                >
                  <div class="error-banner-content">
                    <AppIcon name="triangle-alert" :size="13" class="error-banner-icon" />
                    <span class="error-banner-title">{{ testResults[model.id].friendly?.title || '连接失败' }}</span>
                    <span
                      v-if="testResults[model.id].friendly?.detail"
                      class="error-banner-desc"
                      :title="testResults[model.id].friendly?.detail"
                    >
                      — {{ testResults[model.id].friendly?.detail }}
                    </span>
                  </div>
                  <div class="error-banner-actions">
                    <button
                      class="banner-action-btn"
                      data-test="copy-test-error"
                      title="复制完整报错信息"
                      @click="copyTestError(model.id)"
                    >
                      <AppIcon :name="copiedErrorId === model.id ? 'check' : 'copy'" :size="11" />
                      <span>{{ copiedErrorId === model.id ? '已复制' : '复制报错' }}</span>
                    </button>
                    <button
                      class="banner-action-btn close-btn"
                      data-test="dismiss-test-error"
                      title="关闭此条提示"
                      @click="dismissTestResult(model.id)"
                    >
                      <AppIcon name="x" :size="12" />
                    </button>
                  </div>
                </div>
              </div>
            </div>

          </template>

          <template v-else-if="selectedKind === 'secondary'">
            <div class="detail-title-row">
              <div>
                <h2>副模型</h2>
                <p>用于识图 / 记忆压缩 / 子 Agent 等后台任务，独立于主模型。</p>
              </div>
            </div>
            <label class="field-label">副模型</label>
            <select v-model="secondaryModelId" class="text-field" data-test="secondary-model" @change="onSecondaryModelChange">
              <option value="">无（不启用副模型）</option>
              <option v-for="m in store.models" :key="m.id" :value="m.id">
                {{ m.name }}（{{ m.provider_name }}）
              </option>
            </select>
            <div class="role-row">
              <label class="role-choice">
                <input v-model="secondaryRoles.vision" type="checkbox" data-test="secondary-role-vision" @change="onSecondaryRolesChange" />
                <span><b>识图</b><small>用于需要理解图片的任务</small></span>
              </label>
              <label class="role-choice">
                <input v-model="secondaryRoles.compression" type="checkbox" data-test="secondary-role-compression" @change="onSecondaryRolesChange" />
                <span><b>压缩</b><small>用于对话记忆压缩</small></span>
              </label>
              <label class="role-choice">
                <input v-model="secondaryRoles.subagent" type="checkbox" data-test="secondary-role-subagent" @change="onSecondaryRolesChange" />
                <span><b>子 Agent</b><small>用于子 Agent 调度</small></span>
              </label>
            </div>
          </template>

          <template v-else-if="selectedKind === 'custom'">
            <div class="detail-title-row">
              <div><h2>添加自定义厂商</h2><p>配置 API endpoint、密钥和真实请求格式。</p></div>
            </div>
            <label class="field-label">名称</label>
            <input v-model="customForm.name" data-test="custom-name" class="text-field" placeholder="如：团队网关" />
            <label class="field-label">Base URL</label>
            <input v-model="customForm.base_url" class="text-field mono" placeholder="https://api.example.com/v1" />
            <label class="field-label">API Key</label>
            <input v-model="customForm.api_key" type="password" class="text-field" placeholder="输入 API Key" />
            <label class="field-label">API 格式</label>
            <select v-model="customForm.protocol" class="text-field">
              <option v-for="protocol in store.modelProtocols" :key="protocol.id" :value="protocol.id">
                {{ protocol.label }}
              </option>
            </select>
            <button class="primary-button" :disabled="saving" @click="saveCustomProvider">添加厂商</button>
          </template>

          <div v-else class="empty-detail">从左侧选择一个厂商开始配置</div>
        </div>
      </section>
      </template>
      <template v-else-if="activeSection === 'permissions'">
        <header class="settings-header settings-header--narrow">
          <div>
            <h1>权限与沙盒</h1>
            <p>与 CLI 的 /sandbox、/whitelist 共享同一份配置与存储。</p>
          </div>
          <button class="icon-button" title="刷新白名单" @click="loadPermRules"><AppIcon name="refresh" :size="14" /></button>
        </header>

        <section class="perm-settings-card">
          <div class="perm-row">
            <div>
              <h3>沙盒开关</h3>
              <p>{{ store.sandboxEnabled ? '已解除：工作目录外的文件读写直接放行，不再询问（凭据文件除外）' : '已开启：仅允许访问当前工作目录内的文件' }}</p>
            </div>
            <button
              data-test="sandbox-toggle"
              class="primary-button perm-action"
              @click="store.setSandbox(!store.sandboxEnabled)"
            >{{ store.sandboxEnabled ? '恢复沙盒限制' : '解除沙盒限制' }}</button>
          </div>

          <div class="perm-row">
            <div>
              <h3>当前权限模式</h3>
              <p>{{ formatPermMode(store.permissionMode) }}（可在输入框旁的权限按钮切换）</p>
            </div>
          </div>

          <div class="perm-whitelist">
            <div class="whitelist-head">
              <h3>项目持久化白名单</h3>
              <div class="whitelist-actions">
                <button
                  data-test="add-permission-rule"
                  class="secondary-button compact"
                  @click="showPermRuleForm = !showPermRuleForm"
                >手动添加</button>
                <button
                  data-test="clear-permission-rules"
                  class="secondary-button compact"
                  :disabled="permClearing"
                  @click="clearPermRules"
                >清空本项目规则</button>
              </div>
            </div>
            <p class="whitelist-tip">用户手动添加或审批时选择“加入项目白名单”的规则会按项目目录保存到 permissions.json，重启后仍生效。命令按 token 前缀匹配；文件只放行规范化后的精确路径。任务级授权切换界面不清除，删除任务或重启后失效。</p>
            <div v-if="showPermRuleForm" class="wl-form">
              <select v-model="permRuleType" data-test="permission-rule-type" class="text-field">
                <option value="command">命令前缀</option>
                <option value="write">写入文件</option>
                <option value="edit">修改文件</option>
              </select>
              <input
                v-model="permRuleValue"
                data-test="permission-rule-value"
                class="text-field wl-value-input"
                :placeholder="permRuleType === 'command' ? '例如 git status' : '输入精确文件路径'"
                @keydown.enter.prevent="savePermRule"
              >
              <button
                data-test="save-permission-rule"
                class="primary-button compact"
                :disabled="permSaving || !permRuleValue.trim()"
                @click="savePermRule"
              >保存</button>
            </div>
            <div v-if="permRules && (permRules.commands.length || permRules.write_files.length || permRules.edit_files.length)">
              <div v-if="permRules.commands.length" class="wl-label">命令前缀规则</div>
              <div v-for="(c, i) in permRules.commands" :key="'c' + i" class="wl-item">
                <span class="mono">{{ c }}</span>
                <button :data-test="`remove-permission-command-${i}`" class="wl-remove" @click="removePermRule('command', c)">删除</button>
              </div>
              <div v-if="permRules.write_files.length" class="wl-label">放行写入的文件</div>
              <div v-for="(p, i) in permRules.write_files" :key="'w' + i" class="wl-item">
                <span class="mono">{{ p }}</span>
                <button :data-test="`remove-permission-write-${i}`" class="wl-remove" @click="removePermRule('write', p)">删除</button>
              </div>
              <div v-if="permRules.edit_files.length" class="wl-label">放行修改的文件</div>
              <div v-for="(p, i) in permRules.edit_files" :key="'e' + i" class="wl-item">
                <span class="mono">{{ p }}</span>
                <button :data-test="`remove-permission-edit-${i}`" class="wl-remove" @click="removePermRule('edit', p)">删除</button>
              </div>
            </div>
            <div v-else class="wl-empty">暂无持久化规则</div>
          </div>
        </section>
      </template>
      <template v-else-if="activeSection === 'hooks'">
        <header class="settings-header settings-header--narrow">
          <div>
            <h1>钩子 Hooks</h1>
            <p>用户级与项目级生命周期钩子（支持手动配置，信任确认后生效）。</p>
          </div>
        </header>

        <HooksSettings />
      </template>
      <template v-else-if="activeSection === 'contact'">
        <header class="settings-header">
          <div>
            <h1>联系我们</h1>
            <p>有任何问题或建议，欢迎通过以下方式与我们取得联系。</p>
          </div>
        </header>

        <div class="contact-cards">
          <section class="contact-card">
            <div class="contact-card-icon">
              <AppIcon name="mail" :size="22" />
            </div>
            <h3>邮箱</h3>
            <p class="contact-desc">发送邮件与我们沟通</p>
            <div class="contact-value mono" data-test="contact-email">{{ contactEmail }}</div>
            <button
              data-test="copy-email"
              class="primary-button contact-copy-btn"
              :class="{ copied: emailCopied }"
              @click="copyEmail"
            >
              <AppIcon :name="emailCopied ? 'check' : 'pencil'" :size="13" />
              {{ emailCopied ? '已复制' : '复制邮箱' }}
            </button>
          </section>

          <section class="contact-card">
            <div class="contact-card-icon">
              <AppIcon name="qr" :size="22" />
            </div>
            <h3>微信群</h3>
            <p class="contact-desc">扫码加入官方微信群</p>
            <div class="contact-qr-wrap" @click="enlargedQr = { src: qrcodeWechat, alt: '微信群二维码' }">
              <img :src="qrcodeWechat" alt="微信群二维码" class="contact-qr" data-test="contact-wechat-qr" />
            </div>
            <p class="contact-expiry">该二维码 7 天内（9月28日前）有效，过期后请重新获取</p>
          </section>

          <section class="contact-card">
            <div class="contact-card-icon">
              <AppIcon name="qr" :size="22" />
            </div>
            <h3>飞书群</h3>
            <p class="contact-desc">扫码加入官方飞书群</p>
            <div class="contact-qr-wrap" @click="enlargedQr = { src: qrcodeFeishu, alt: '飞书群二维码' }">
              <img :src="qrcodeFeishu" alt="飞书群二维码" class="contact-qr" data-test="contact-feishu-qr" />
            </div>
            <p class="contact-expiry">该二维码 1 年内有效</p>
          </section>
        </div>
      </template>
      <template v-else-if="activeSection === 'about'">
        <header class="settings-header settings-header--narrow">
          <div>
            <h1>关于</h1>
            <p>{{ store.agentName }} —— 你的 AI 编程搭档。</p>
          </div>
        </header>

        <section class="perm-settings-card">
          <div class="perm-row">
            <div>
              <h3>版本</h3>
              <p>当前版本：<span class="mono" data-test="about-version">{{ store.version || '未知' }}</span></p>
            </div>
            <button
              data-test="check-update"
              class="primary-button perm-action"
              :class="{
                'install-action-btn': store.updateState.status === 'downloaded'
              }"
              :disabled="store.updateState.status === 'checking' || store.updateState.status === 'installing'"
              @click="handleAboutUpdateClick"
            >{{ aboutUpdateBtnText }}</button>
          </div>
        </section>

        <section class="perm-settings-card" data-test="about-changelog">
          <div class="changelog-head">
            <h3>更新内容</h3>
            <button
              v-if="changelogEntries.length > 1"
              data-test="changelog-history-btn"
              class="secondary-button compact"
              @click="showChangelogHistory = true"
            >查看历史更新内容</button>
          </div>
          <div v-if="currentEntry" class="changelog-current">
            <div class="changelog-version">
              <span class="mono">v{{ currentEntry.version }}</span>
              <span v-if="currentEntry.date" class="changelog-date">{{ currentEntry.date }}</span>
            </div>
            <ul class="changelog-notes">
              <li v-for="(note, i) in currentEntry.notes" :key="i">{{ note }}</li>
            </ul>
          </div>
          <p v-else-if="changelogLoaded" class="changelog-empty">本版本暂无更新说明。</p>
          <p v-else class="changelog-empty">更新内容加载中…</p>
        </section>
      </template>
      <template v-else-if="activeSection === 'disclaimer'">
        <header class="settings-header settings-header--narrow">
          <div>
            <h1>免责条款</h1>
            <p>使用本软件前，请阅读并理解以下条款。继续使用本软件，即视为你已阅读并接受本条款。</p>
          </div>
        </header>

        <section class="perm-settings-card disclaimer-card" data-test="disclaimer-doc">
          <div class="disclaimer-meta">
            <span class="mono" data-test="disclaimer-version">{{ disclaimerVersion }}</span>
            <span>更新日期 {{ disclaimerUpdatedAt }}</span>
            <span>适用于本软件全部发行版本</span>
            <span
              class="disclaimer-status"
              :class="isDisclaimerVersionAccepted(store.disclaimerAcceptedVersion, disclaimerVersion) ? 'disclaimer-status--accepted' : 'disclaimer-status--pending'"
            >
              {{ isDisclaimerVersionAccepted(store.disclaimerAcceptedVersion, disclaimerVersion) ? `已同意本版本 (${store.disclaimerAcceptedVersion})` : '待确认最新条款' }}
            </span>
          </div>

          <article v-for="(clause, i) in disclaimerClauses" :key="i" class="disclaimer-clause">
            <h3>{{ clause.title }}</h3>
            <ol class="disclaimer-list">
              <li v-for="(item, j) in clause.items" :key="j">{{ item }}</li>
            </ol>
          </article>

          <footer class="disclaimer-foot">
            <p>本条款是软件使用条款的组成部分。若你不同意其中任何内容，请停止使用本软件。</p>
            <p>如对条款有疑问，可通过「联系我们」中的邮箱与我们沟通。</p>
          </footer>
        </section>
      </template>
      <TokenUsageSettings v-else-if="activeSection === 'tokens'" />
      <TokenUsageSettingsLegacy v-else-if="activeSection === 'tokens-legacy'" />
      <SystemSettings v-else-if="activeSection === 'system'" />
      <PersonaSettings v-else-if="activeSection === 'persona'" />

      <!-- 外观与主题常驻挂载（v-show）：切换设置分区不丢失未保存的主题草稿 -->
      <div v-show="activeSection === 'appearance'" class="appearance-section">
        <header class="settings-header">
          <div>
            <h1>外观与主题</h1>
            <p>为 GUI 创建、编辑和切换可视化主题；CLI 的 /theme 配置保持独立、互不影响。</p>
          </div>
        </header>

        <section class="appearance-settings-card">
          <GuiThemeSettings ref="themeEditorRef" />
        </section>
      </div>
    </main>

    <!-- 二维码放大查看 -->
    <div v-if="enlargedQr" class="qr-enlarge-mask" @click="enlargedQr = null">
      <img :src="enlargedQr.src" :alt="enlargedQr.alt" class="qr-enlarge-img" />
    </div>

    <div v-if="modelModal.show" class="modal-mask">
      <form data-test="model-modal" class="model-modal" @submit.prevent="saveModel">
        <div class="modal-head"><h3>{{ modelModal.model_id ? '编辑模型' : '添加模型' }}</h3><button type="button" @click="closeModelModal">×</button></div>
        <label class="field-label">模型 ID</label>
        <input v-model="modelModal.name" class="text-field mono" placeholder="模型 ID" />
        <button type="button" class="secondary-button" data-test="fetch-models" :disabled="fetchingModels" @click="fetchRemoteModels">
          {{ fetchingModels ? '获取中…' : (remoteModels && remoteModels.length ? `重新获取（${remoteModels.length} 个）` : '获取模型列表') }}
        </button>
        <select v-if="remoteModels && remoteModels.length" class="text-field" data-test="remote-model-select" @change="pickRemoteModel">
          <option value="">共 {{ remoteModels.length }} 个模型，点选自动填充</option>
          <option v-for="entry in remoteModels" :key="entry.id" :value="entry.id">
            {{ entry.id }}{{ entry.model_type ? `（${entry.model_type}）` : '' }}
          </option>
        </select>
        <div class="modal-field-row">
          <div class="modal-field">
            <label class="field-label">上下文窗口</label>
            <input v-model.number="modelModal.context_window" class="text-field" type="number" min="1" />
          </div>
          <div class="modal-field">
            <label class="field-label">最大输出</label>
            <input v-model.number="modelModal.max_tokens" class="text-field" type="number" min="0" placeholder="留空使用默认" />
          </div>
        </div>
        <p class="modal-field-hint">最大输出留空时使用按模型家族内置的默认输出上限（如 DeepSeek 384K、GLM-5.3 131K、其余 32K）。</p>
        <label class="vision-choice">
          <input v-model="modelModal.supports_vision" data-test="model-supports-vision" type="checkbox" />
          <span><b>是否支持多模态</b><small>启用后允许向该模型发送图片内容</small></span>
        </label>
        <label class="vision-choice">
          <input v-model="modelModal.supports_function_calling" data-test="model-supports-function-calling" type="checkbox" />
          <span><b>是否支持工具调用</b><small>默认视为支持（Agent 模型本就以工具调用驱动）；显式关闭会把该模型排除在电脑操控之外。电脑操控还要求开启多模态（视觉识图）</small></span>
        </label>
        <label class="field-label">电脑操控协议</label>
        <select v-model="modelModal.computer_protocol" data-test="model-computer-protocol" class="text-field">
          <option value="">自动（多模态与工具调用都开启即可操控）</option>
          <option value="none">无（不具备电脑操控）</option>
          <option value="generic">通用视觉工具协议（generic）</option>
          <option value="native_openai">OpenAI 原生 Computer Use（native_openai）</option>
        </select>
        <p class="modal-field-hint">电脑操控要求模型支持视觉（识图）；工具调用默认视为支持（显式关闭的模型除外）。选「自动」时，视觉已勾选的模型在开关开启后即可直接操控。</p>
        <p v-if="computerProtocolError" data-test="model-computer-protocol-error" class="modal-field-error">
          {{ computerProtocolError }}
        </p>
        <div class="modal-actions">
          <button type="button" class="secondary-button" @click="closeModelModal">取消</button>
          <button type="submit" class="primary-button" :disabled="saving">保存</button>
        </div>
      </form>
    </div>

    <!-- 历史更新内容弹窗：随安装包发布的 CHANGELOG.md 全量条目 -->
    <div v-if="showChangelogHistory" class="modal-mask" @click.self="showChangelogHistory = false">
      <div data-test="changelog-history-modal" class="changelog-modal">
        <div class="modal-head"><h3>历史更新内容</h3><button type="button" @click="showChangelogHistory = false">×</button></div>
        <div class="changelog-history-list">
          <div v-for="entry in changelogEntries" :key="entry.version" class="changelog-history-item">
            <div class="changelog-version">
              <span class="mono">v{{ entry.version }}</span>
              <span v-if="entry.date" class="changelog-date">{{ entry.date }}</span>
            </div>
            <ul class="changelog-notes">
              <li v-for="(note, i) in entry.notes" :key="i">{{ note }}</li>
            </ul>
          </div>
          <p v-if="!changelogEntries.length" class="changelog-empty">暂无历史更新记录。</p>
        </div>
        <div class="modal-actions">
          <button type="button" class="primary-button" @click="showChangelogHistory = false">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';
import { formatPermMode } from '../utils/markdown';
import type { ChangelogEntry, ComputerProtocolChoice, ModelInfo } from '../types/pywebview';
import TokenUsageSettings from './TokenUsageSettings.vue';
import TokenUsageSettingsLegacy from './TokenUsageSettingsLegacy.vue';
import GuiThemeSettings from './GuiThemeSettings.vue';
import SystemSettings from './SystemSettings.vue';
import PersonaSettings from './PersonaSettings.vue';
import HooksSettings from './HooksSettings.vue';
import { useIsMobile } from '../composables/useViewport';
import qrcodeWechat from '../assets/qrcode-wechat.png';
import qrcodeFeishu from '../assets/qrcode-feishu.png';
// 免责条款正文与版本号：设置页展示与首次使用确认弹窗共用同一事实源
import {
  DISCLAIMER_CLAUSES as disclaimerClauses,
  DISCLAIMER_UPDATED_AT as disclaimerUpdatedAt,
  DISCLAIMER_VERSION as disclaimerVersion,
  isDisclaimerVersionAccepted,
} from '../data/disclaimer';
import { parseModelTestError, type ParsedModelError } from '../utils/errorParser';

const store = useAppStore();
// 初始分区跟随 store.settingsSection（预览返回编辑器时跳到外观与主题）；
// 组件常驻，返回编辑器时通过 watch 同步
const activeSection = ref<'models' | 'tokens' | 'tokens-legacy' | 'permissions' | 'hooks' | 'appearance' | 'system' | 'persona' | 'about' | 'contact' | 'disclaimer'>(store.settingsSection);
watch(() => store.settingsSection, (section) => { activeSection.value = section; });
// ── 手机端钻取式布局（≤768px）────────────────────────────────
// 桌面：分区侧栏 + 厂商列表 + 详情三栏并排（isMobile=false，模板不变）。
// 手机：厂商列表全宽；点选后列表/详情互斥显示，详情顶栏带"返回列表"。
const isMobile = useIsMobile();
const mobileDetail = ref(false);
function openMobileDetail() { if (isMobile.value) mobileDetail.value = true; }
function closeMobileDetail() { mobileDetail.value = false; }

const selectedKind = ref<'preset' | 'provider' | 'custom' | 'secondary'>('provider');
const selectedId = ref(store.providers[0]?.id || '');
const showAddProviders = ref(false);
const presetApiKey = ref('');
const showPresetKey = ref(false);
const showProviderKey = ref(false);
const keyCopied = ref(false);
let keyCopyTimer: ReturnType<typeof setTimeout> | null = null;
const saving = ref(false);
const testingId = ref('');
interface TestResultState {
  status: 'testing' | 'ok' | 'fail';
  msg: string;
  friendly?: ParsedModelError;
}
const testResults = reactive<Record<string, TestResultState>>({});
const copiedErrorId = ref('');
let copiedErrorTimer: ReturnType<typeof setTimeout> | null = null;

const providerForm = reactive({ name: '', base_url: '', api_key: '', protocol: 'openai' });
const customForm = reactive({ name: '', base_url: '', api_key: '', protocol: 'openai' });

// M33 修复：副模型状态不再在 setup 时一次性快照（会与 store 脱节，
// 后端/其他入口变更后设置页显示陈旧值）。改为 computed 双向绑定，
// 读始终取 store 当前值，写直接同步回 store（与 setSecondaryRoles 乐观更新语义一致）。
const secondaryModelId = computed({
  get: () => store.secondaryModelId,
  set: (v: string) => { store.secondaryModelId = v; },
});
const secondaryRoles = computed({
  get: () => store.secondaryModelRoles,
  set: (v: { vision: boolean; compression: boolean; subagent: boolean }) => { store.secondaryModelRoles = v; },
});
const modelModal = reactive({
  show: false,
  model_id: '',
  name: '',
  context_window: 128000,
  // 最大输出：'' = 留空（请求时走内置默认表）
  max_tokens: '' as number | '',
  supports_vision: false,
  // 电脑操控能力契约（设计 9.1）：generic 必须同时具备视觉与工具调用；
  // 缺省 ''（自动）= 视觉+工具调用双勾即可操控，无需逐个配置协议。
  // 工具调用新模型默认勾选（Agent 模型本就以工具调用驱动，用户要求默认支持）
  supports_function_calling: true,
  computer_protocol: '' as ComputerProtocolChoice,
});
// 「获取模型列表」：拉取厂商 /v1/models 的一键填充候选
const remoteModels = ref<{ id: string; model_type?: string; context_window?: number | null; supports_vision?: boolean | null; max_tokens?: number | null }[] | null>(null);
const fetchingModels = ref(false);

// 与后端 model_manager._validate_computer_combination 同一校验原因（Task 12，
// 2026-09-05 修订：工具调用未声明视为支持，显式关闭才是拒绝理由）
const COMPUTER_PROTOCOL_GENERIC_REASON = 'computer_protocol=generic 要求声明 supports_vision，且 supports_function_calling 未被显式关闭';

const computerProtocolError = computed(() => (
  modelModal.computer_protocol === 'generic'
  && !(modelModal.supports_vision && modelModal.supports_function_calling !== false)
)
  ? COMPUTER_PROTOCOL_GENERIC_REASON
  : '');

// 外观与主题编辑器（常驻挂载，通过 defineExpose 暴露脏检查）
const themeEditorRef = ref<InstanceType<typeof GuiThemeSettings> | null>(null);

async function attemptCloseSettings() {
  if (themeEditorRef.value?.isDirty?.()) {
    const leave = await store.askConfirm(
      '未保存的主题修改', '', '离开设置页将丢失未保存的主题修改，确定离开？', '直接离开',
    );
    if (!leave) return;
  }
  store.closeModelSettings();
}

const selectedPreset = computed(() => selectedKind.value === 'preset'
  ? store.modelPresets.find(item => item.id === selectedId.value)
  : undefined);
const selectedProvider = computed(() => selectedKind.value === 'provider'
  ? store.providers.find(item => item.id === selectedId.value)
  : undefined);
const configuredPresetIds = computed(() => new Set(
  store.providers.map(item => item.preset_id).filter(Boolean) as string[],
));

watch(selectedProvider, (provider) => {
  if (!provider) return;
  providerForm.name = provider.name;
  providerForm.base_url = provider.base_url || '';
  providerForm.api_key = '';
  providerForm.protocol = provider.protocol || 'openai';
  showProviderKey.value = false;
}, { immediate: true });

// 输入框为空但后端已配置 Key 时，按需从后端取回明文填入（快照不含明文）
async function revealProviderKey(): Promise<boolean> {
  const provider = selectedProvider.value;
  if (!provider) return false;
  if (providerForm.api_key) return true;
  if (!provider.has_api_key) return false;
  const key = await store.getProviderApiKey(provider.id);
  if (key === null || !key) return false;
  providerForm.api_key = key;
  return true;
}

async function toggleProviderKey() {
  if (showProviderKey.value) {
    showProviderKey.value = false;
    return;
  }
  if (await revealProviderKey()) showProviderKey.value = true;
}

async function copyProviderKey() {
  if (!(await revealProviderKey())) return;
  try {
    await navigator.clipboard.writeText(providerForm.api_key);
  } catch {
    // 剪贴板 API 不可用时静默失败
  }
  keyCopied.value = true;
  if (keyCopyTimer) clearTimeout(keyCopyTimer);
  keyCopyTimer = setTimeout(() => { keyCopied.value = false; }, 2000);
}

watch(
  () => [store.modelPresets.map(item => item.id), store.providers.map(item => item.id)],
  () => {
    if (selectedKind.value === 'custom' || selectedKind.value === 'secondary') return;
    const presetExists = selectedKind.value === 'preset'
      && store.modelPresets.some(item => item.id === selectedId.value);
    const providerExists = selectedKind.value === 'provider'
      && store.providers.some(item => item.id === selectedId.value);
    if (presetExists || providerExists) return;
    if (store.providers.length) {
      selectProvider(store.providers[0].id);
    } else {
      selectedKind.value = 'provider';
      selectedId.value = '';
    }
  },
  { immediate: true },
);

// 手机端把窗口拉宽到桌面尺寸时退出钻取态，避免详情被隐藏导致"卡"在列表
watch(isMobile, (mobile) => { if (!mobile) mobileDetail.value = false; });

function selectPreset(id: string) {
  selectedKind.value = 'preset';
  selectedId.value = id;
  presetApiKey.value = '';
  openMobileDetail();
}

function selectProvider(id: string) {
  selectedKind.value = 'provider';
  selectedId.value = id;
  showAddProviders.value = false;
  openMobileDetail();
}

// 点击左侧"副模型"入口：右侧切换到副模型设置面板
function selectSecondary() {
  selectedKind.value = 'secondary';
  selectedId.value = '';
  showAddProviders.value = false;
  openMobileDetail();
}

function startCustomProvider() {
  selectedKind.value = 'custom';
  selectedId.value = '';
  openMobileDetail();
}

function protocolLabel(id: string) {
  return store.modelProtocols.find(item => item.id === id)?.label || id;
}

function formatContext(value: number) {
  if (value >= 1000000) return `${Math.round(value / 100000) / 10}M`;
  return `${Math.round(value / 1000)}K`;
}

async function savePreset() {
  if (!selectedPreset.value || !presetApiKey.value.trim()) {
    store.notifyModal('表单校验', '请填写 API Key');
    return;
  }
  saving.value = true;
  const existing = store.providers.find(item => item.preset_id === selectedPreset.value?.id);
  const result = existing
    ? await store.updateProvider(
        existing.id,
        selectedPreset.value.name,
        selectedPreset.value.base_url,
        presetApiKey.value.trim(),
        selectedPreset.value.protocol,
      )
    : await store.addPresetProvider(selectedPreset.value.id, presetApiKey.value.trim());
  saving.value = false;
  if (result?.ok) {
    presetApiKey.value = '';
    selectProvider(existing?.id || result.provider_id);
  }
}

async function saveCustomProvider() {
  if (!customForm.name.trim() || !customForm.base_url.trim()) {
    store.notifyModal('表单校验', '请填写名称和 Base URL');
    return;
  }
  saving.value = true;
  const result = await store.addProvider(
    customForm.name.trim(), customForm.base_url.trim(), customForm.api_key.trim(), customForm.protocol,
  );
  saving.value = false;
  if (result?.ok) selectProvider(result.provider_id);
}

async function saveProvider() {
  const provider = selectedProvider.value;
  if (!provider) return;
  saving.value = true;
  await store.updateProvider(
    provider.id,
    provider.preset_id ? provider.name : providerForm.name.trim(),
    provider.preset_id ? (provider.base_url || '') : providerForm.base_url.trim(),
    providerForm.api_key.trim(),
    provider.preset_id ? (provider.protocol || 'openai') : providerForm.protocol,
  );
  providerForm.api_key = '';
  showProviderKey.value = false;
  saving.value = false;
}

function openModelModal(model?: ModelInfo) {
  modelModal.show = true;
  modelModal.model_id = model?.id || '';
  modelModal.name = model?.name || '';
  modelModal.context_window = model?.context_window || 128000;
  modelModal.max_tokens = model?.max_tokens || '';
  modelModal.supports_vision = model?.supports_vision || false;
  // 新增模型默认支持工具调用；编辑时保留该模型的既有值（显式关闭仍是关闭）
  modelModal.supports_function_calling = model ? !!model.supports_function_calling : true;
  // 后端缺键模型返回 ''（自动）；显式 none/generic/native_openai 原样回填
  modelModal.computer_protocol = model?.computer_protocol || '';
  remoteModels.value = null;
}

async function fetchRemoteModels() {
  const provider = selectedProvider.value;
  if (!provider || fetchingModels.value) return;
  fetchingModels.value = true;
  const res = await store.fetchProviderModels(provider.id);
  fetchingModels.value = false;
  if (res?.ok) {
    remoteModels.value = res.models || [];
    if (!remoteModels.value.length) {
      store.notifyModal('获取模型列表', '服务端返回了空列表');
    }
  } else {
    store.notifyModal('获取模型列表', res?.msg || '未知错误');
  }
}

function pickRemoteModel(event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  if (!value) return;
  modelModal.name = value;
  // 厂商元数据 + 本地规格表：能填的都填（未提供的字段保持表单原值）
  const entry = (remoteModels.value || []).find((m) => m.id === value);
  if (!entry) return;
  if (entry.context_window && entry.context_window > 0) {
    modelModal.context_window = entry.context_window;
  }
  if (entry.max_tokens && entry.max_tokens > 0) {
    modelModal.max_tokens = entry.max_tokens;
  } else {
    modelModal.max_tokens = '';
  }
  if (entry.supports_vision !== undefined && entry.supports_vision !== null) {
    modelModal.supports_vision = entry.supports_vision;
  }
}

function closeModelModal() {
  modelModal.show = false;
}

async function saveModel() {
  const provider = selectedProvider.value;
  if (!provider || !modelModal.name.trim() || modelModal.context_window < 1) {
    store.notifyModal('表单校验', '请填写有效的模型 ID 和上下文窗口');
    return;
  }
  // 前端先阻止半有效 generic 保存并显示校验原因；后端仍会再次校验
  if (computerProtocolError.value) {
    store.notifyModal('表单校验', computerProtocolError.value);
    return;
  }
  saving.value = true;
  // 最大输出：留空 = 0（后端语义：新增不落键 / 编辑清除键，回到内置默认）
  const maxTokens = modelModal.max_tokens === '' ? 0 : Number(modelModal.max_tokens);
  let result;
  if (modelModal.model_id) {
    result = await store.updateModel(
      provider.id, modelModal.model_id, modelModal.name.trim(),
      Number(modelModal.context_window), modelModal.supports_vision,
      modelModal.supports_function_calling, modelModal.computer_protocol,
      maxTokens,
    );
  } else {
    result = await store.addModel(
      provider.id, modelModal.name.trim(), Number(modelModal.context_window), modelModal.supports_vision,
      modelModal.supports_function_calling, modelModal.computer_protocol,
      maxTokens,
    );
  }
  saving.value = false;
  if (result?.ok) closeModelModal();
}

async function testModel(model: ModelInfo) {
  testingId.value = model.id;
  testResults[model.id] = { status: 'testing', msg: '测试中…' };
  const result = await store.testModel(model.id);
  if (result.ok) {
    testResults[model.id] = { status: 'ok', msg: '连接成功' };
  } else {
    const friendly = parseModelTestError(result.msg);
    testResults[model.id] = {
      status: 'fail',
      msg: friendly.title,
      friendly,
    };
  }
  testingId.value = '';
}

function dismissTestResult(modelId: string) {
  delete testResults[modelId];
}

async function copyTestError(modelId: string) {
  const item = testResults[modelId];
  const text = item?.friendly?.raw || item?.msg || '';
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // 剪贴板不可用时降级
  }
  copiedErrorId.value = modelId;
  if (copiedErrorTimer) clearTimeout(copiedErrorTimer);
  copiedErrorTimer = setTimeout(() => {
    if (copiedErrorId.value === modelId) copiedErrorId.value = '';
  }, 2000);
}

async function onSecondaryModelChange() {
  await store.setSecondaryModel(secondaryModelId.value);
}

async function onSecondaryRolesChange() {
  await store.setSecondaryRoles({ ...secondaryRoles.value });
}

// ── 权限与沙盒分区：加载/清空当前项目的持久化白名单 ──
const permRules = ref<{ commands: string[]; write_files: string[]; edit_files: string[] } | null>(null);
const permClearing = ref(false);
type PermissionRuleType = 'command' | 'write' | 'edit';
const showPermRuleForm = ref(false);
const permRuleType = ref<PermissionRuleType>('command');
const permRuleValue = ref('');
const permSaving = ref(false);

async function loadPermRules() {
  try {
    const api = (window as any).pywebview?.api;
    if (!api?.get_permission_rules) return;
    const res = await api.get_permission_rules();
    if (res && res.ok) {
      permRules.value = {
        commands: (res.commands || []).map(String),
        write_files: (res.write_files || []).map(String),
        edit_files: (res.edit_files || []).map(String),
      };
    }
  } catch {
    // 后端未就绪时静默忽略，保留上次结果
  }
}

async function clearPermRules() {
  const confirmed = await store.askConfirm(
    '清空项目白名单',
    '该操作会删除当前项目的全部持久化权限规则',
    '确定清空本项目规则吗？任务级临时授权不会受到影响。',
    '确认清空',
  );
  if (!confirmed) return;
  permClearing.value = true;
  try {
    const api = (window as any).pywebview?.api;
    if (!api?.clear_permission_rules) return;
    const res = await api.clear_permission_rules();
    if (res && res.ok) {
      store.notifyModal('权限与沙盒', `已清空 ${res.removed || 0} 条持久化规则`);
      await loadPermRules();
    } else {
      store.notifyModal('权限与沙盒', '清空失败: ' + (res && res.msg ? res.msg : '未知错误'));
    }
  } finally {
    permClearing.value = false;
  }
}

async function savePermRule() {
  const value = permRuleValue.value.trim();
  if (!value) return;
  permSaving.value = true;
  try {
    const api = (window as any).pywebview?.api;
    if (!api?.add_permission_rule) return;
    const res = await api.add_permission_rule(permRuleType.value, value);
    if (res?.ok) {
      permRuleValue.value = '';
      showPermRuleForm.value = false;
      await loadPermRules();
    } else {
      store.notifyModal('权限与沙盒', '新增失败: ' + (res?.msg || '未知错误'));
    }
  } finally {
    permSaving.value = false;
  }
}

async function removePermRule(ruleType: PermissionRuleType, value: string) {
  const confirmed = await store.askConfirm(
    '删除白名单规则',
    '删除后相关操作将重新按当前权限模式判断',
    `确定删除规则「${value}」吗？`,
    '确认删除',
  );
  if (!confirmed) return;
  const api = (window as any).pywebview?.api;
  if (!api?.remove_permission_rule) return;
  const res = await api.remove_permission_rule(ruleType, value);
  if (res?.ok) {
    await loadPermRules();
  } else {
    store.notifyModal('权限与沙盒', '删除失败: ' + (res?.msg || '未知错误'));
  }
}

watch(activeSection, (v) => {
  if (v === 'permissions') loadPermRules();
});

const aboutUpdateBtnText = computed(() => {
  if (store.updateState.status === 'checking') return '正在检查...';
  if (store.updateState.status === 'downloading') return `下载中 (${store.updateState.downloadProgress}%)`;
  if (store.updateState.status === 'downloaded') return '立即安装更新';
  if (store.updateState.status === 'available') return `更新至 v${store.updateState.latestVersion}`;
  return '检查更新';
});

function handleAboutUpdateClick() {
  if (store.updateState.status === 'downloaded') {
    store.handleInstallClick();
  } else if (store.updateState.status === 'available') {
    store.openUpdateModal();
  } else {
    store.checkUpdate(true);
  }
}

// ------------------------------------------------------------------
// 更新内容（关于页）：随安装包发布的 assets/CHANGELOG.md
// ------------------------------------------------------------------
const changelogEntries = ref<ChangelogEntry[]>([]);
const changelogLoaded = ref(false);
const showChangelogHistory = ref(false);

// 当前版本条目：优先精确匹配运行版本号，匹配不到（如源码跑未发布版本）
// 则回落到文件里的最新一条，保证关于页始终有内容可看
const currentEntry = computed<ChangelogEntry | null>(() => {
  if (!changelogEntries.value.length) return null;
  return (
    changelogEntries.value.find((e) => e.version === store.version) ??
    changelogEntries.value[0]
  );
});

// ------------------------------------------------------------------
// 免责条款页（法律声明）
// ------------------------------------------------------------------
// 条款正文与版本号统一维护在 ../data/disclaimer.ts，设置页与首次使用
// 确认弹窗共用同一事实源。修改条款只需改该文件，并同步递增后端
// core/disclaimer_consent.py 的 DISCLAIMER_VERSION（否则已同意旧版本的
// 用户不会重新看到确认弹窗）。

// ------------------------------------------------------------------
// 联系我们页
// ------------------------------------------------------------------
const contactEmail = 'fchyjy@qq.com';
const emailCopied = ref(false);
const enlargedQr = ref<{ src: string; alt: string } | null>(null);
let emailCopyTimer: ReturnType<typeof setTimeout> | null = null;

async function copyEmail() {
  try {
    await navigator.clipboard.writeText(contactEmail);
  } catch {
    // 剪贴板 API 不可用时静默失败
  }
  emailCopied.value = true;
  if (emailCopyTimer) clearTimeout(emailCopyTimer);
  emailCopyTimer = setTimeout(() => { emailCopied.value = false; }, 2000);
}

onMounted(async () => {
  // 直接读 window.pywebview 而非 store._api()：后者为空时会弹
  // "连接失败" 模态框，更新内容拉取失败不该打扰用户
  const api = window.pywebview?.api;
  if (!api) {
    changelogLoaded.value = true;
    return;
  }
  try {
    const res = await api.get_changelog();
    if (res && res.ok) changelogEntries.value = res.entries || [];
  } catch {
    // 更新内容属展示性信息，失败静默（关于页显示"暂无更新说明"）
  } finally {
    changelogLoaded.value = true;
  }
});
</script>

<style scoped>
.settings-page { position: relative; flex: 1; min-width: 0; z-index: 80; display: flex; background: var(--bg0); color: var(--text); }
.settings-nav { width: 220px; padding: 22px 12px 14px; background: var(--bg1); border-right: 1px solid var(--border); display: flex; flex-direction: column; }
.back-workspace, .nav-item { width: 100%; border: 0; background: transparent; color: var(--text-dim); text-align: left; border-radius: 9px; padding: 10px 12px; }
.back-workspace:hover, .nav-item:hover { color: var(--text); background: var(--bg2); }
.nav-section { margin: 28px 12px 8px; color: var(--text-faint); font-size: 11px; }
.nav-item { display: flex; gap: 10px; align-items: center; font-size: 13px; }
.nav-item.active { background: var(--bg3); color: var(--text); }
.nav-spacer { flex: 1; }
.settings-user { display: flex; align-items: center; gap: 10px; padding: 10px; color: var(--text-dim); font-size: 12px; }
.settings-user span:nth-child(2) { flex: 1; font-weight: 600; }
.avatar { width: 30px; height: 30px; display: grid; place-items: center; border-radius: 50%; background: var(--bg3); color: var(--text); }
.settings-main { flex: 1; min-width: 0; overflow: auto; padding: 46px 5vw; }
.settings-header { max-width: 1120px; margin: 0 auto 26px; display: flex; align-items: flex-end; justify-content: space-between; }
/* 权限与沙盒 / 关于：正文卡片宽度 860px，标题栏收窄到同宽，保证左边缘对齐且整体居中 */
.settings-header--narrow { max-width: 860px; }
.settings-header h1 { margin: 0 0 10px; font-size: 32px; letter-spacing: -1px; }
.settings-header p, .detail-title-row p { margin: 0; color: var(--text-faint); font-size: 13px; }
.icon-button { background: transparent; color: var(--text-dim); border: 0; font-size: 20px; }
.model-settings-card { max-width: 1120px; min-height: 610px; margin: 0 auto; display: grid; grid-template-columns: 280px minmax(0, 1fr); border: 1px solid var(--border); border-radius: 16px; background: var(--bg1); overflow: hidden; }
.provider-list { padding: 16px 12px; border-right: 1px solid var(--border); background: var(--bg2); overflow-y: auto; }
.list-label { padding: 4px 10px 10px; color: var(--text-faint); font-size: 11px; font-weight: 600; }
.provider-empty { padding: 12px 10px; color: var(--text-faint); font-size: 11px; }
.provider-item { width: 100%; display: flex; align-items: center; gap: 9px; padding: 9px 10px; border: 1px solid transparent; border-radius: 9px; background: transparent; color: var(--text-dim); text-align: left; }
.provider-item:hover, .provider-item.active { background: var(--bg3); color: var(--text); }
.provider-item.active { border-color: var(--border); }
.provider-logo { width: 24px; height: 24px; display: grid; place-items: center; border-radius: 7px; background: var(--accent-soft); color: var(--accent); font-size: 11px; }
.provider-logo.cube { background: transparent; color: var(--text-dim); font-size: 18px; }
.provider-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.provider-state { width: 7px; height: 7px; border-radius: 50%; background: var(--text-faint); }
.provider-state.ready { background: var(--green); }
.add-provider-row { width: 100%; margin-top: 10px; padding: 9px; border: 1px dashed var(--border); border-radius: 9px; background: transparent; color: var(--text-dim); text-align: left; display: flex; align-items: center; justify-content: space-between; }
.add-provider-row:hover { border-color: var(--accent); color: var(--accent); }
.add-provider-row.active { border-color: var(--accent); color: var(--accent); background: var(--bg3); }
.add-provider-choices { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border); }
.custom-provider-row { margin-top: 10px; }
/* 副模型入口：位于左侧列表底部（添加厂商下方），点击后在右侧设置副模型 */
.secondary-entry-row {
  width: 100%;
  margin-top: 10px;
  padding: 9px 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: transparent;
  color: var(--text-dim);
  text-align: left;
  display: flex;
  align-items: center;
  gap: 9px;
}
.secondary-entry-row:hover, .secondary-entry-row.active {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--bg3);
}
.secondary-entry-row .provider-logo { background: var(--bg3); color: var(--text-dim); }
.secondary-entry-row.active .provider-logo { color: var(--accent); }
.secondary-entry-row .provider-name { font-size: 13px; }
.provider-detail { min-width: 0; padding: 38px; overflow-y: auto; }
.detail-title-row { display: flex; justify-content: space-between; gap: 18px; margin-bottom: 28px; }
.detail-title-row h2 { margin: 0 0 8px; font-size: 20px; }
.protocol-badge, .vision-tag { align-self: flex-start; padding: 4px 9px; border: 1px solid var(--border); border-radius: 8px; color: var(--text-faint); font-size: 11px; }
.field-label { display: block; margin: 16px 0 7px; color: var(--text-dim); font-size: 12px; }
.text-field, .readonly-field, .key-field { width: 100%; min-height: 42px; box-sizing: border-box; border: 1px solid var(--border); border-radius: 9px; background: var(--bg2); color: var(--text); font-size: 13px; }
.text-field { padding: 0 13px; }
.readonly-field { display: flex; align-items: center; padding: 0 13px; color: var(--text-dim); }
.key-field { display: flex; align-items: center; overflow: hidden; }
.key-field input { flex: 1; min-width: 0; height: 40px; padding: 0 13px; border: 0; background: transparent; color: var(--text); outline: 0; }
.key-field button, .row-actions button { border: 0; background: transparent; color: var(--text-faint); }
.key-field button { padding: 0 13px; }
.key-field .icon-button { display: flex; align-items: center; padding: 0 10px; cursor: pointer; }
.key-field .icon-button:disabled { opacity: 0.4; cursor: default; }
.mono { font-family: var(--mono); }
.model-preview-title { margin: 24px 0 9px; color: var(--text-dim); font-size: 12px; }
.preset-models, .configured-models { border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
.configured-model-item { border-bottom: 1px solid var(--border); }
.configured-model-item:last-child { border-bottom: 0; }
.preset-model-row, .configured-model-row { min-height: 48px; display: flex; align-items: center; gap: 14px; padding: 0 14px; font-size: 12px; }
.preset-model-row { border-bottom: 1px solid var(--border); }
.preset-model-row:last-child { border-bottom: 0; }
.preset-model-row .mono { flex: 1; color: var(--text); }
.vision-tag { padding: 2px 7px; flex-shrink: 0; white-space: nowrap; }
.vision-tag.on { color: var(--green); border-color: color-mix(in srgb, var(--green) 35%, var(--border)); }
.model-meta-ctx { flex-shrink: 0; white-space: nowrap; color: var(--text-dim); }
.primary-button, .secondary-button { min-height: 38px; margin-top: 20px; padding: 0 16px; border-radius: 9px; border: 1px solid var(--border); font-weight: 600; }
.primary-button { background: var(--text); color: var(--bg0); }
.secondary-button { background: var(--bg3); color: var(--text); }
.secondary-button.compact { margin: 0; min-height: 34px; }
.danger-icon { border: 0; background: transparent; color: var(--text-faint); font-size: 18px; }
.danger-icon:hover, .row-actions .danger:hover { color: var(--red); }
.model-list-header { display: flex; align-items: center; justify-content: space-between; margin: 34px 0 10px; }
.model-list-actions { display: flex; align-items: center; gap: 8px; }
.model-list-header h3 { margin: 0 0 4px; font-size: 14px; }
.model-list-header span { color: var(--text-faint); font-size: 11px; }
.model-main { flex: 1; min-width: 0; display: flex; align-items: center; gap: 12px; }
.model-id { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.test-result {
  color: var(--text-faint);
  font-size: 11px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
  white-space: nowrap;
}
.test-result.ok { color: var(--green); }
.test-result.testing { color: var(--accent); }
.row-actions { display: flex; gap: 7px; flex-shrink: 0; white-space: nowrap; }
.row-actions button { flex-shrink: 0; white-space: nowrap; }
.row-actions button:hover { color: var(--accent); }

/* ---------- 方案 B：模型测试失败行内微型警告条 ---------- */
.model-error-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 14px;
  background: color-mix(in srgb, var(--red) 7%, var(--bg1));
  border-top: 1px dashed color-mix(in srgb, var(--red) 25%, var(--border));
  font-size: 11.5px;
  animation: banner-slide-down 0.18s ease-out;
}
@keyframes banner-slide-down {
  from { opacity: 0; transform: translateY(-3px); }
  to { opacity: 1; transform: translateY(0); }
}
.error-banner-content {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex: 1;
  overflow: hidden;
}
.error-banner-icon {
  color: var(--red);
  flex-shrink: 0;
}
.error-banner-title {
  color: var(--red);
  font-weight: 600;
  flex-shrink: 0;
  white-space: nowrap;
}
.error-banner-desc {
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.error-banner-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.banner-action-btn {
  border: 1px solid color-mix(in srgb, var(--red) 28%, var(--border));
  background: color-mix(in srgb, var(--bg2) 80%, transparent);
  color: var(--text-dim);
  border-radius: 5px;
  padding: 2px 7px;
  font-size: 11px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
}
.banner-action-btn:hover {
  background: var(--bg2);
  color: var(--text);
  border-color: var(--text-faint);
}
.banner-action-btn.close-btn {
  padding: 2px 4px;
  border-color: transparent;
  background: transparent;
  color: var(--text-faint);
}
.banner-action-btn.close-btn:hover {
  color: var(--red);
  background: color-mix(in srgb, var(--red) 12%, transparent);
}
.empty-models, .empty-detail { padding: 48px 0; text-align: center; color: var(--text-faint); font-size: 12px; }
.modal-mask { position: fixed; inset: 0; z-index: 5000; display: grid; place-items: center; background: rgba(0, 0, 0, .68); backdrop-filter: blur(8px); }
.model-modal { width: min(560px, calc(100vw - 40px)); padding: 24px; box-sizing: border-box; border: 1px solid var(--border); border-radius: 15px; background: var(--bg2); box-shadow: 0 24px 80px rgba(0, 0, 0, .55); }
.modal-field-row { display: flex; gap: 12px; }
.modal-field-row .modal-field { flex: 1; min-width: 0; }
.modal-head { display: flex; align-items: center; justify-content: space-between; }
.modal-head h3 { margin: 0; }
.modal-head button { border: 0; background: transparent; color: var(--text-dim); font-size: 20px; }

/* ---------- 联系我们页 ---------- */
.contact-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
.contact-card { display: flex; flex-direction: column; align-items: center; text-align: center; padding: 24px 18px; background: var(--bg1); border: 1px solid var(--border); border-radius: 10px; transition: border-color 0.2s; }
.contact-card:hover { border-color: var(--text-faint); }
.contact-card-icon { width: 44px; height: 44px; border-radius: 10px; background: var(--bg2); display: flex; align-items: center; justify-content: center; color: var(--text-dim); margin-bottom: 14px; }
.contact-card h3 { margin: 0 0 4px; font-size: 15px; font-weight: 600; }
.contact-desc { margin: 0 0 14px; font-size: 12px; color: var(--text-dim); }
.contact-value { font-size: 14px; padding: 8px 14px; background: var(--bg2); border-radius: 6px; margin-bottom: 14px; word-break: break-all; }
.contact-copy-btn { display: inline-flex; align-items: center; gap: 6px; padding: 7px 16px; font-size: 12px; }
.contact-copy-btn.copied { opacity: 0.85; }
.contact-qr-wrap { width: 170px; aspect-ratio: 2 / 3; height: auto; border-radius: 8px; overflow: hidden; background: #fff; display: flex; align-items: center; justify-content: center; margin-bottom: 10px; padding: 6px; cursor: zoom-in; transition: transform 0.15s; }
.contact-qr-wrap:hover { transform: scale(1.03); }
.contact-qr { width: 100%; height: 100%; object-fit: contain; }
.qr-enlarge-mask { position: fixed; inset: 0; z-index: 999; background: rgba(0, 0, 0, 0.7); display: flex; align-items: center; justify-content: center; cursor: zoom-out; }
.qr-enlarge-img { max-width: 80vw; max-height: 80vh; border-radius: 12px; box-shadow: 0 8px 40px rgba(0, 0, 0, 0.5); }
.contact-expiry { margin: 0; font-size: 11px; color: var(--text-faint); }

/* ---------- 关于页 · 更新内容 ---------- */
.changelog-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.changelog-head h3 { margin: 0; }
.changelog-version { display: flex; align-items: baseline; gap: 10px; margin-bottom: 8px; }
.changelog-version .mono { font-weight: 600; color: var(--accent); }
.changelog-date { font-size: 11.5px; color: var(--text-faint); }
.changelog-notes { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 5px; }
.changelog-notes li { font-size: 12.5px; line-height: 1.55; color: var(--text-dim); }
.changelog-empty { font-size: 12.5px; color: var(--text-faint); margin: 4px 0; }
.changelog-modal { width: min(620px, calc(100vw - 40px)); max-height: min(72vh, 640px); padding: 24px; box-sizing: border-box; border: 1px solid var(--border); border-radius: 15px; background: var(--bg2); box-shadow: 0 24px 80px rgba(0, 0, 0, .55); display: flex; flex-direction: column; }
.changelog-history-list { flex: 1; min-height: 0; overflow-y: auto; margin-top: 14px; padding-right: 6px; display: flex; flex-direction: column; gap: 18px; }
.changelog-history-item { border-left: 2px solid var(--border); padding-left: 14px; }
.changelog-history-item:first-child { border-left-color: var(--accent); }
.changelog-modal .modal-actions { margin-top: 18px; display: flex; justify-content: flex-end; }
.vision-choice { margin-top: 20px; display: flex; align-items: flex-start; gap: 10px; padding: 13px; border: 1px solid var(--border); border-radius: 10px; color: var(--text); }
.vision-choice span { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.vision-choice small { color: var(--text-faint); }
.role-row { margin-top: 14px; display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
.role-choice { display: flex; align-items: flex-start; gap: 9px; padding: 11px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg1); color: var(--text); }
.role-choice span { display: flex; flex-direction: column; gap: 3px; font-size: 12px; }
.role-choice small { color: var(--text-faint); }
.modal-field-error { margin: 8px 0 0; color: var(--danger); font-size: 12px; line-height: 1.6; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; }

/* ── 权限与沙盒分区 ── */
.perm-settings-card { max-width: 860px; margin: 0 auto; border: 1px solid var(--border); border-radius: 16px; background: var(--bg1); padding: 10px 28px 28px; }

/* ── 免责条款分区（法律声明）── */
.disclaimer-card { padding: 6px 32px 30px; }
.disclaimer-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; padding: 18px 0 2px; color: var(--text-faint); font-size: 12px; }
.disclaimer-meta .mono { color: var(--text-dim); }
.disclaimer-status { padding: 1px 8px; border-radius: 999px; font-size: 11px; line-height: 1.6; }
.disclaimer-status--accepted { background: rgba(34, 197, 94, 0.12); color: #22c55e; border: 1px solid rgba(34, 197, 94, 0.28); }
.disclaimer-status--pending { background: rgba(234, 179, 8, 0.12); color: #eab308; border: 1px solid rgba(234, 179, 8, 0.28); }
.disclaimer-clause { padding-top: 22px; }
.disclaimer-clause h3 { margin: 0 0 10px; font-size: 14px; }
.disclaimer-list { margin: 0; padding-left: 20px; display: flex; flex-direction: column; gap: 8px; }
.disclaimer-list li { color: var(--text-dim); font-size: 12.5px; line-height: 1.85; }
.disclaimer-foot { margin-top: 26px; padding-top: 18px; border-top: 1px solid var(--border); }
.disclaimer-foot p { margin: 0 0 6px; color: var(--text-faint); font-size: 12px; line-height: 1.8; }

/* ── 外观与主题分区 ── */
.appearance-section { width: 100%; min-width: 0; }
.appearance-settings-card { width: 100%; min-width: 0; max-width: 1120px; margin: 0 auto; border: 1px solid var(--border); border-radius: 16px; background: var(--bg1); padding: 22px 24px; }
.perm-row { display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 20px 0; border-bottom: 1px solid var(--border); }
.perm-row h3 { margin: 0 0 6px; font-size: 14px; }
.perm-row p { margin: 0; color: var(--text-faint); font-size: 12.5px; }
.perm-action { margin-top: 0; white-space: nowrap; }
.perm-whitelist { padding-top: 20px; }
.whitelist-head { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.whitelist-actions { display: flex; align-items: center; gap: 8px; }
.whitelist-head h3 { margin: 0; font-size: 14px; }
.whitelist-tip { margin: 8px 0 14px; color: var(--text-faint); font-size: 12px; line-height: 1.7; }
.wl-form { display: flex; gap: 8px; align-items: center; margin: 12px 0 16px; }
.wl-form select { flex: 0 0 130px; }
.wl-value-input { flex: 1; min-width: 0; }
.wl-label { margin: 14px 0 6px; color: var(--text-dim); font-size: 11.5px; font-weight: 600; }
.wl-item { padding: 8px 10px 8px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg2); color: var(--text-dim); font-size: 12px; margin-bottom: 6px; display: flex; align-items: center; gap: 10px; }
.wl-item > span { flex: 1; min-width: 0; word-break: break-all; }
.wl-remove { flex: none; border: 0; background: transparent; color: var(--red); cursor: pointer; padding: 4px 6px; }
.wl-empty { padding: 26px 0; text-align: center; color: var(--text-faint); font-size: 12px; }
/* ── 手机端钻取式布局（≤768px）──────────────────────────────
   分区导航变顶部横滑条；模型配置卡片单列，列表/详情互斥显示。
   仅在窄断点生效，桌面端布局不受任何影响。 */
.mobile-detail-back {
  display: none; /* 桌面端隐藏；详情返回按钮仅手机端渲染 */
  align-items: center;
  gap: 6px;
  width: fit-content;
  margin-bottom: 14px;
  padding: 6px 12px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg2);
  color: var(--text);
}
@media (max-width: 900px) {
  .settings-nav { width: 170px; }
  .settings-main { padding: 28px 20px; }
  .model-settings-card { grid-template-columns: 220px minmax(0, 1fr); }
  .provider-detail { padding: 24px; }
}
@media (max-width: 768px) {
  /* 页面纵向布局：导航在上、内容在下 */
  .settings-page--mobile { flex-direction: column; }

  /* 分区导航 → 顶部横滑胶囊条；分组标题与底部用户区隐藏 */
  .settings-page--mobile .settings-nav {
    width: auto;
    flex-direction: row;
    align-items: center;
    gap: 7px;
    padding: 10px 10px;
    border-right: 0;
    border-bottom: 1px solid var(--border);
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    flex: none;
  }
  .settings-page--mobile .back-workspace { width: auto; flex: none; color: var(--accent); font-weight: 600; padding: 8px 4px; }
  .settings-page--mobile .nav-section,
  .settings-page--mobile .nav-spacer,
  .settings-page--mobile .settings-user { display: none; }
  .settings-page--mobile .nav-item {
    flex: none;
    /* 基础样式为纵向侧栏设计的 width:100% 必须复位，否则每个 chip 占满
       一整屏宽，7 个分区要横向拖 7 屏（width:100% + flex:none 不收缩） */
    width: auto;
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 5px 11px;
    font-size: 12.5px;
  }
  .settings-page--mobile .nav-item.active { background: var(--bg3); }

  /* 内容区：去大留白 */
  .settings-page--mobile .settings-main { padding: 16px 12px 22px; }
  .settings-page--mobile .settings-header { margin-bottom: 14px; }
  .settings-page--mobile .settings-header h1 { font-size: 24px; letter-spacing: -0.5px; }
  .settings-page--mobile .settings-header p, .settings-page--mobile .detail-title-row p { font-size: 12px; }

  /* 模型配置卡片：单列全宽；列表/详情由 v-show + isMobile 互斥显示 */
  .settings-page--mobile .model-settings-card {
    grid-template-columns: minmax(0, 1fr);
    min-height: 0;
    max-width: none;
  }
  .settings-page--mobile .provider-list { border-right: 0; border-bottom: 0; padding: 12px 12px 14px; }
  .settings-page--mobile .provider-detail { padding: 18px 16px 22px; overflow-y: visible; }
  .settings-page--mobile .detail-title-row { flex-wrap: wrap; gap: 10px; margin-bottom: 18px; }
  .settings-page--mobile .mobile-detail-back { display: inline-flex; }

  /* 其余分区的卡片跟随收紧（Token 统计等子组件自带适配，此处只调容器） */
  .settings-page--mobile .perm-settings-card { padding: 6px 16px 20px; }
  .settings-page--mobile .whitelist-head { align-items: flex-start; flex-direction: column; }
  .settings-page--mobile .wl-form { align-items: stretch; flex-direction: column; }
  .settings-page--mobile .wl-form select { flex-basis: auto; }
  .settings-page--mobile .appearance-settings-card {
    max-width: none;
    padding: 0;
    border: 0;
    border-radius: 0;
    background: transparent;
  }
}
</style>
