---
name: baoyu-post-to-wechat
description: 通过 API 或 Chrome CDP 将内容发布到微信公众号（WeChat Official Account）。支持文章发布（文章），输入可为 HTML、markdown 或纯文本；也支持多图贴图（贴图，原「图文」）。Markdown 文章流程默认把普通外部链接转换为文末引用，以获得更适合公众号的输出。当用户提到「发布公众号」「post to wechat」「微信公众号」或「贴图/图文/文章」时使用本技能（baoyu-post-to-wechat）。
triggers: 发布公众号,公众号发布,微信公众号,贴图,图文,文章发布
version: 1.118.2
metadata:
  openclaw:
    homepage: https://github.com/JimLiu/baoyu-skills#baoyu-post-to-wechat
    requires:
      anyBins:
        - bun
        - npx
---

# 发布到微信公众号

## 用户输入工具

当本技能需要向用户提问时，请遵循以下工具选择规则（按优先级顺序）：

1. **优先使用当前 agent 运行时提供的内置用户输入工具** —— 例如 `AskUserQuestion`、`request_user_input`、`clarify`、`ask_user` 或任何等价工具。
2. **兜底方案**：若不存在此类工具，则输出一条带编号的纯文本消息，请用户针对每个问题回复所选的编号/答案。
3. **批量提问**：若该工具支持一次调用多个问题，则把所有适用问题合并到一次调用中；若只支持单个问题，则按优先级顺序逐条询问。

下文出现的 `AskUserQuestion` 具体示例仅为示范 —— 在其他运行时中请替换为本地等价的工具。

## 语言

用用户的语言回复。用户用中文写就用中文回，用英文写就用英文回。技术性记号（路径、参数、字段名）保持英文。

## 脚本目录

`{baseDir}` = 本 SKILL.md 所在目录。解析 `${BUN_X}`：优先用 `bun`；否则用 `npx -y bun`；再否则建议 `brew install oven-sh/bun/bun`。

| 脚本 | 用途 |
|--------|---------|
| `scripts/wechat-browser.ts` | 贴图发布（图文） |
| `scripts/wechat-article.ts` | 通过浏览器发布文章（文章） |
| `scripts/wechat-api.ts` | 通过 API 发布文章（文章） |
| `scripts/md-to-wechat.ts` | Markdown → 带图片占位的公众号就绪 HTML |
| `scripts/check-permissions.ts` | 校验环境与权限 |

## 偏好设置（EXTEND.md）

按顺序检查以下路径；先命中者生效：

| 路径 | 作用范围 |
|------|-------|
| `.baoyu-skills/baoyu-post-to-wechat/EXTEND.md` | 项目 |
| `${XDG_CONFIG_HOME:-$HOME/.config}/baoyu-skills/baoyu-post-to-wechat/EXTEND.md` | XDG |
| `$HOME/.baoyu-skills/baoyu-post-to-wechat/EXTEND.md` | 用户主目录 |

找到 → 读取、解析并应用。未找到 → 先执行首次配置（`references/config/first-time-setup.md`），再做其他任何事。

**最小键集**（大小写不敏感，接受 `1/0` 或 `true/false`）：

| 键 | 默认值 | 映射 |
|-----|---------|---------|
| `default_author` | 空 | 当 CLI/frontmatter 未提供 `author` 时的回退值 |
| `need_open_comment` | `1` | `draft/add` 中的 `articles[].need_open_comment` |
| `only_fans_can_comment` | `0` | `draft/add` 中的 `articles[].only_fans_can_comment` |

**推荐的 EXTEND.md**：

```md
default_theme: default
default_color: blue
default_publish_method: browser
default_author: 宝玉
need_open_comment: 1
only_fans_can_comment: 0
chrome_profile_path: /path/to/chrome/profile

# Remote API publishing (optional) — only set if WeChat's IP allowlist
# excludes your local machine. See "Remote API Method" below.
# remote_publish_host: server.example.com
# remote_publish_user: deploy
# remote_publish_port: 22
# remote_publish_identity_file: ~/.ssh/id_ed25519
# remote_publish_known_hosts_file: ~/.ssh/known_hosts
# remote_publish_strict_host_key_checking: accept-new
# remote_publish_connect_timeout: 10
# remote_publish_proxy_jump: bastion.example.com
```

有意不支持原始的 `ssh` / `scp` 选项；只认上述带类型前缀的键。认证仅支持 SSH key（不支持密码）。

**主题可选值**：default、grace、simple、modern。**颜色预设**：blue、green、vermilion、yellow、purple、sky、rose、olive、black、gray、pink、red、orange（或十六进制色值）。

**取值优先级**：CLI 参数 → frontmatter → EXTEND.md（账号级 → 全局）→ 技能默认值。

## 多账号支持

EXTEND.md 支持一个 `accounts:` 块，用于管理多个公众号。当条目数 ≥ 2 时，流程会插入 Step 0.5 提示选择账号（或依据 `default: true` 或 `--account <alias>` 自动选择）。

完整细节 —— 兼容性规则、账号级键、凭据解析、按账号区分的 Chrome profile、CLI 用法 —— 见 `references/multi-account.md`。

## 预检（可选）

首次使用前，建议先做环境检查（用户可跳过）：

```bash
${BUN_X} {baseDir}/scripts/check-permissions.ts
```

检查项：Chrome、profile 隔离、Bun、辅助功能权限、剪贴板、粘贴按键、API 凭据、Chrome 冲突。

| 检查失败项 | 修复方法 |
|-------------|-----|
| Chrome | 安装 Chrome 或设置 `WECHAT_BROWSER_CHROME_PATH` |
| Profile 目录 | 使用共享 profile：`baoyu-skills/chrome-profile` |
| Bun 运行时 | `brew install oven-sh/bun/bun` 或 `npm install -g bun` |
| 辅助功能权限（macOS） | 系统设置 → 隐私与安全性 → 辅助功能 → 启用终端 App |
| 剪贴板复制 | 确保有 Swift/AppKit（macOS：`xcode-select --install`） |
| 粘贴按键（Linux） | 安装 `xdotool`（X11）或 `ydotool`（Wayland） |
| API 凭据 | 按 Step 2 的引导式配置操作，或在 `.baoyu-skills/.env` 中设置 |

## 贴图发布（图文）

带多张图片（最多 9 张）的短内容：

```bash
${BUN_X} {baseDir}/scripts/wechat-browser.ts --markdown article.md --images ./images/
${BUN_X} {baseDir}/scripts/wechat-browser.ts --title "标题" --content "内容" --image img.png --submit
```

详情见：`references/image-text-posting.md`。

## 文章发布流程（文章）

```
- [ ] Step 0: Load preferences (EXTEND.md)
- [ ] Step 0.5: Resolve account (multi-account only — see references/multi-account.md)
- [ ] Step 1: Determine input type
- [ ] Step 2: Select method and configure credentials
- [ ] Step 3: Resolve theme/color and validate metadata
- [ ] Step 4: Publish to WeChat
- [ ] Step 5: Report completion
```

### Step 0：加载偏好设置

检查并加载 EXTEND.md（见上文「偏好设置」）。若未找到，先完成首次配置，再提出其他任何问题。解析并缓存以供后续步骤使用：`default_theme`、`default_color`、`default_author`、`need_open_comment`、`only_fans_can_comment`。

### Step 1：确定输入类型

| 输入 | 判定方式 | 下一步 |
|-------|-----------|------|
| HTML 文件 | 路径以 `.html` 结尾且文件存在 | 跳到 Step 3 |
| Markdown 文件 | 路径以 `.md` 结尾且文件存在 | Step 2 |
| 纯文本 | 不是文件路径，或文件不存在 | 先保存为 markdown，再走 Step 2 |

**纯文本处理**：

1. 生成 slug（取前 2-4 个有意义的词，kebab-case；中文需转写为英文作为 slug）。
2. 保存到 `post-to-wechat/YYYY-MM-DD/<slug>.md`（目录不存在则创建）。
3. 之后按 markdown 文件继续处理。

### Step 2：选择发布方式并完成配置

除非 EXTEND.md 或 CLI 已指定，否则询问用户选择方式：

| 方式 | 速度 | 前置条件 |
|--------|-------|----------|
| `api`（推荐） | 快 | API 凭据（本地 IP 在白名单内） |
| `browser` | 慢 | Chrome + 已登录的会话 |
| `remote-api` | 快 | API 凭据 + 一台 SSH 可达、且其 IP 在公众号白名单内的服务器 |

**选择 API 但缺少凭据** → 按 `references/api-setup.md` 执行引导式配置（写入 `.baoyu-skills/.env`）。

**`remote-api` 方式**：公众号的「公众号设置 → IP 白名单」常把 API 访问限制在一两个固定 IP。若本机 IP 不在该列表中，而某台云服务器的 IP 在，就可使用 `remote-api`：所有 markdown 渲染、图片处理、草稿组装和 HTML 改写仍在本地完成，只有对 `api.weixin.qq.com` 的出站 HTTPS 调用（token、uploadimg、add_material、draft/add）会经由 SSH SOCKS5 动态端口转发（`ssh -N -D`）走隧道，使微信看到的来源 IP 是远端服务器。远端主机上不写入任何文件；`AppSecret` 始终不离开本地进程。远端只需有 `sshd` 和外网出口 —— 不需要 Python，也不需要 agent 进程。见下文「Remote API 方式」。

### Step 3：解析主题/颜色并校验元数据

1. **主题**：CLI `--theme` → EXTEND.md `default_theme` → `default`（取第一个命中项；若已确定则不要询问）。
2. **颜色**：CLI `--color` → EXTEND.md `default_color` → 省略（使用主题默认值）。
3. **校验元数据**（markdown 用 frontmatter，HTML 用 meta 标签）：

| 字段 | 缺失时 → |
|-------|-----------|
| 标题 | 询问用户，或按回车根据正文自动生成 |
| 摘要 | frontmatter `description` → `summary` → 询问或自动生成 |
| 作者 | CLI `--author` → frontmatter `author` → EXTEND.md `default_author` |
| 原文链接 | CLI `--source-url` → frontmatter `sourceUrl`/`contentSourceUrl`/`content_source_url` |

自动生成：标题 = 第一个 H1/H2 或第一句话；摘要 = 第一段，截断至 120 字符。

4. **封面图**（API `article_type=news` 时必填）：CLI `--cover` → frontmatter（`coverImage` / `featureImage` / `cover` / `image`）→ `imgs/cover.png` → 正文第一张内嵌图片 → 若仍缺失则停下并向用户索取。

### Step 4：发布

**重要 —— 绝不要预先将 markdown 转成 HTML。** 发布脚本会在内部完成转换，且两种方式对图片的渲染方式不同：API 会渲染 `<img>` 标签用于上传，浏览器方式则使用占位符以便粘贴替换。传入预先转换好的 HTML 会破坏其中一种方式。

**Markdown 引用默认行为**：对于 markdown 输入，普通外部链接默认会被转换为文末引用。仅当用户明确想保留行内链接时才使用 `--no-cite`。已有的 HTML 输入保持原样。

**API 方式**（接受 `.md` 或 `.html`）：

```bash
${BUN_X} {baseDir}/scripts/wechat-api.ts <file> --theme <theme> [--color <color>] [--title <title>] [--summary <summary>] [--author <author>] [--cover <cover_path>] [--source-url <url>] [--no-cite]
```

即使主题是 `default` 也要始终传 `--theme`。仅当用户或 EXTEND.md 明确设置时才传 `--color`。

**Remote API 方式**（同一个脚本，额外加 `--remote`）：

```bash
${BUN_X} {baseDir}/scripts/wechat-api.ts <file> --theme <theme> --remote [--remote-host <host>] [--remote-user <user>] [--remote-port <port>] [--remote-identity-file <path>] [--remote-known-hosts-file <path>] [--remote-strict-host-key-checking yes|no|accept-new] [--remote-connect-timeout <s>] [--remote-proxy-jump <spec>]
```

任何 `--remote-*` 参数都隐含 `--remote`。CLI 取值会覆盖 EXTEND.md 中的账号级 `remote_publish_*` 键，再覆盖全局键。设置 `default_publish_method: remote-api` 也能在不用 `--remote` 的情况下启用远端模式。

**`draft/add` 请求体规则**：
- 端点：`POST https://api.weixin.qq.com/cgi-bin/draft/add?access_token=ACCESS_TOKEN`
- `article_type`：`news`（默认）或 `newspic`
- 对于 `news`，需包含 `thumb_media_id`（封面必填）
- 请求体中始终包含 `need_open_comment`（默认 `1`）与 `only_fans_can_comment`（默认 `0`），即使 CLI 未暴露这两个参数
- 对于 `news`，可选包含 `content_source_url`（原文链接，会显示为「阅读原文」链接，最大 1KB）。通过 `--source-url` CLI 参数或 frontmatter `sourceUrl`/`contentSourceUrl`/`content_source_url` 提供

**浏览器方式**（接受 `--markdown` 或 `--html`）：

```bash
${BUN_X} {baseDir}/scripts/wechat-article.ts --markdown <markdown_file> --theme <theme> [--color <color>] [--no-cite]
${BUN_X} {baseDir}/scripts/wechat-article.ts --html <html_file>
```

### Step 5：完成报告

```
WeChat Publishing Complete!

Input: [type] - [path]
Method: [API | Browser]
Theme: [theme] [color if set]

Article:
• Title: [title]
• Summary: [summary]
• Images: [N] inline
• Comments: [open/closed], [fans-only/all]    ← API method only

Result:
✓ Draft saved to WeChat Official Account
• media_id: [media_id]                         ← API method only

Next Steps (API):
→ Manage drafts: https://mp.weixin.qq.com (登录后进入「内容管理」→「草稿箱」)

Files created:
[• post-to-wechat/YYYY-MM-DD/slug.md (if plain text input)]
[• slug.html (converted)]
```

## 功能对比

| 功能 | 贴图 | 文章（API） | 文章（Remote API） | 文章（浏览器） |
|---------|:---:|:---:|:---:|:---:|
| 纯文本输入 | ✗ | ✓ | ✓ | ✓ |
| HTML 输入 | ✗ | ✓ | ✓ | ✓ |
| Markdown 输入 | 标题/内容 | ✓ | ✓ | ✓ |
| 多图 | ✓（最多 9 张） | ✓（内嵌） | ✓（内嵌） | ✓（内嵌） |
| 主题 | ✗ | ✓ | ✓ | ✓ |
| 自动生成元数据 | ✗ | ✓ | ✓ | ✓ |
| 默认封面回退（`imgs/cover.png`） | ✗ | ✓ | ✓ | ✗ |
| 评论控制 | ✗ | ✓ | ✓ | ✗ |
| 需要 Chrome | ✓ | ✗ | ✗ | ✓ |
| 需要 API 凭据 | ✗ | ✓ | ✓ | ✗ |
| 需要 SSH 可达且 IP 在白名单内的服务器 | ✗ | ✗ | ✓ | ✗ |
| 速度 | 中等 | 快 | 快 | 慢 |

## 故障排查

| 问题 | 修复方法 |
|-------|-----|
| 缺少 API 凭据 | 按 Step 2 的引导式配置操作 |
| Access token 报错 | 确认凭据有效且未过期 |
| 未登录（浏览器） | 首次运行会打开浏览器 —— 扫码登录。设置 `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` 即可通过 Telegram 接收二维码图片 |
| 找不到 Chrome | 设置 `WECHAT_BROWSER_CHROME_PATH` |
| 标题/摘要缺失 | 使用自动生成，或手动提供 |
| 没有封面图 | 在 frontmatter 中添加封面，或在文章目录放置 `imgs/cover.png` |
| 评论默认值不对 | 检查 EXTEND.md 中的 `need_open_comment` / `only_fans_can_comment` |
| 粘贴失败 | 检查系统剪贴板权限 |
| `Remote publish host is required` | 设置 `--remote-host`，或在 EXTEND.md 中设置 `remote_publish_host` |
| `SOCKS proxy on 127.0.0.1:… not ready` | SSH 未能建立隧道 —— 检查密钥、主机、`StrictHostKeyChecking`，或使用 `--remote-connect-timeout` |
| 远端发布时出现 `ssh exited early` | 确认该用户能非交互式地 `ssh` 到服务器；链路较慢时调大 `--remote-connect-timeout` |
| Remote API 调用返回 `errcode 40164`（invalid IP） | 远端服务器的出口 IP 不在公众号白名单内；请在 公众号设置 → IP 白名单 中添加 |

## 参考文档

| 文件 | 内容 |
|------|---------|
| `references/image-text-posting.md` | 贴图参数、自动压缩 |
| `references/article-posting.md` | 文章主题、图片处理 |
| `references/multi-account.md` | 多账号兼容性、凭据、Chrome profile、CLI |
| `references/api-setup.md` | 引导式凭据配置 |
| `references/config/first-time-setup.md` | 首次 EXTEND.md 配置 |

## 扩展支持

通过 EXTEND.md 自定义配置。路径与支持的选项见「偏好设置」。
