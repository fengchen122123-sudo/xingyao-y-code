# 星瑶 Personal Agent 部署手册

本文描述如何在一台 Linux 服务器上部署星瑶 Personal Agent：网关、Web 前端、账号体系和按账号启动的 worker 容器。

文中所有服务器地址、域名、账号、口令、令牌和密钥都使用占位符。真实值只放在服务器本地 secret 文件或密码管理器中，**不要提交到 Git，不要写进截图或聊天记录**。

---

## 1. 部署结构

```text
浏览器 / 手机
      │ HTTPS（可选，推荐）
      ▼
Caddy 或 SSH 隧道
      │
      ▼
gateway:8080（宿主机 127.0.0.1:18080）
      │ Docker socket
      ├── xingyao-worker-<account_id>:8766
      └── /srv/xingyao-state/accounts/<account_id>/
              ├── data
              └── workspace
```

- 网关负责登录、账号、设备、管理接口和 WebSocket 路由。
- 每个账号按需启动一个 worker 容器。
- 平台模型 Key 只存在网关的加密钥匙串中；worker 配置只保存 `keyring:` 引用。
- worker 容器不暴露端口到宿主机，由网关通过 Docker 网络访问。
- worker 以 UID `10001` 运行，账号数据目录和钥匙串文件必须允许该 UID 读取。

---

## 2. 前置条件

服务器建议：

- Linux x86_64（Ubuntu 22.04/24.04 或同类发行版）
- Docker 24+，Docker Compose v2
- 2 GB 以上内存
- 能访问 Python 包源、Playwright 浏览器下载源
- 域名和 HTTPS 证书（公网正式访问时）
- SSH 访问方式（没有域名时用于建立本地隧道）

本地开发机：

- Node.js 20+
- npm
- 能构建 `web/` 前端
- 如需打包 Android：JDK 21、Android SDK（platform 35、build-tools、platform-tools）

---

## 3. 获取代码和构建 Web

将仓库放到服务器，例如：

```bash
git clone <REPOSITORY_URL> /srv/xingyao
cd /srv/xingyao
```

构建前端。网关会直接提供 `web/dist`：

```bash
cd web
npm ci
npm run build
cd ..
```

确认：

```bash
test -f web/dist/index.html && echo "web dist ok"
```

---

## 4. 准备 secret 文件

### 4.1 JWT secret

生成一次随机值，后续重启必须保持一致，否则所有登录令牌都会失效：

```bash
sudo install -d -m 0700 /etc/xingyao
sudo sh -c 'umask 077; openssl rand -hex 32 > /etc/xingyao/jwt_secret'
sudo chmod 0400 /etc/xingyao/jwt_secret
```

### 4.2 钥匙串口令

钥匙串口令用于加密平台模型 Key。它也必须持久化：

```bash
sudo sh -c 'umask 077; openssl rand -base64 48 > /etc/xingyao/keyring_password'
sudo chown root:10001 /etc/xingyao/keyring_password
sudo chmod 0440 /etc/xingyao/keyring_password
```

`root:10001 0440` 的原因：网关以 root 写入钥匙串，worker 以 UID 10001 只读读取同一份口令。不要把它设成所有人可读。

### 4.3 systemd 环境文件（使用 systemd 部署时）

```bash
sudo sh -c 'umask 077; printf "XINGYAO_JWT_SECRET=%s\n" "$(cat /etc/xingyao/jwt_secret)" > /etc/xingyao/gateway.env'
sudo chmod 0400 /etc/xingyao/gateway.env
```

Docker Compose 部署不需要这个文件，启动前把值 export 到当前 shell 即可。

---

## 5. 构建镜像

### 5.1 网关镜像

```bash
docker build -t xingyao-gateway:local -f gateway/Dockerfile gateway
```

### 5.2 worker 镜像

优先使用 slim 版本，避免拉取体积很大的 Playwright 基础镜像：

```bash
docker build \
  -f deploy/worker.slim.Dockerfile \
  --build-arg PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
  -t xingyao-worker:local .
```

如果服务器能直接访问 Playwright 官方源，可以去掉 `--build-arg`。

确认镜像：

```bash
docker image inspect xingyao-worker:local --format '{{.Id}} {{.Size}}'
```

worker 镜像必须包含：

- Python 运行依赖
- Playwright Python 包
- Chromium 浏览器
- `gateway/worker_bootstrap.py`
- `gui_main.py` 及其 GUI mixin 模块

---

## 6. 启动网关

### 6.1 Docker Compose（推荐）

```bash
export XINGYAO_JWT_SECRET="$(cat /etc/xingyao/jwt_secret)"
export XINGYAO_KEYRING_PASSWORD_FILE=/etc/xingyao/keyring_password
export XINGYAO_KEYRING_HOST_PASSWORD_FILE=/etc/xingyao/keyring_password

docker compose -f deploy/docker-compose.yml config
docker compose -f deploy/docker-compose.yml up -d gateway
```

先看 `docker compose config`，确认没有把真实 secret 展开成明文打印出来。

检查：

```bash
curl -fsS http://127.0.0.1:18080/health
docker ps --filter name=xingyao-gateway
```

### 6.2 systemd

把 unit 安装到系统：

```bash
sudo cp deploy/gateway.service /etc/systemd/system/xingyao-gateway.service
sudo systemctl daemon-reload
sudo systemctl enable --now xingyao-gateway
sudo systemctl status xingyao-gateway --no-pager
```

unit 使用 `/etc/xingyao/gateway.env` 和 `LoadCredential`，不要把 JWT secret 写进 unit 文件。

---

## 7. 初始化管理员

首次部署时数据库中还没有账号，只能从服务器本机执行一次初始化：

```bash
docker compose -f deploy/docker-compose.yml exec -it gateway \
  python -c "import getpass, os; from pathlib import Path; from gateway.db import connect; from gateway.auth import init_admin; db=connect(Path(os.environ['XINGYAO_GATEWAY_DIR'])/'gateway.db'); init_admin(db, input('admin username: '), getpass.getpass('admin password: '))"
```

注意：

- 只在空库执行一次。
- 口令不要写进 shell 历史。
- 初始化后从 `http://127.0.0.1:18080/` 登录，再在管理后台创建其他账号。
- 如果库里已有账号，`init_admin` 会拒绝再次执行。

---

## 8. 本机验收

```bash
curl -fsS http://127.0.0.1:18080/health
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18080/
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18080/login
```

期望：

```text
200
200
200
```

再检查：

- `/api/admin/models` 不返回明文 API Key，只返回 `api_key_configured`。
- 登录后在聊天页发送一条消息，worker 能在 60 秒内启动并完成回合。
- 用量页出现对应账号的 `usage_daily` 记录。
- 关闭 worker 后容器被回收，重新发消息能再次启动。

---

## 9. 没有域名时：SSH 隧道访问

当前网关只监听服务器本机 `127.0.0.1:18080`，公网不能直接访问。可以在本地电脑建立 SSH 隧道：

```bash
ssh -N \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -L 18080:127.0.0.1:18080 \
  <SSH_USER>@<SERVER_IP>
```

然后浏览器访问：

```text
http://127.0.0.1:18080/
```

Windows 用户可以把这个命令写成 `.bat` 或使用项目提供的隧道启动脚本。隧道未建立时，浏览器会连接失败；这不是网关故障。

---

## 10. 有域名时：Caddy HTTPS

`deploy/Caddyfile` 是网关的反向代理模板。需要自行提供：

```text
SITE_DOMAIN=你的域名
ACME_EMAIL=证书通知邮箱（可空）
```

启动 Caddy 后：

```bash
caddy validate --config deploy/Caddyfile
```

要求：

- 公网只开 443。
- 不把 18080、8766、8765 暴露到公网。
- TLS 证书由 Caddy 自动申请。
- 访问日志对 `token`、`jwt` 查询参数做脱敏。
- `/computer-preview/` 的 token 不进访问日志。

配置域名后，Web 登录地址改为：

```text
https://<DOMAIN>/
```

---

## 11. 配对本地电脑（云中转）

网页要访问本地电脑，需要建立设备配对。

### 11.1 服务器生成一次性设备令牌

1. 管理员登录 Web。
2. 进入 **管理后台 → 账号**，复制目标账号 ID。
3. 进入 **管理后台 → 设备**，填写账号 ID 和设备名称。
4. 生成令牌后立即保存；完整令牌只显示一次。
5. 在设备列表记录设备编号。

### 11.2 本地星瑶设置

本地桌面星瑶打开 **设置 → 云中转**：

```text
启用：开
网关地址：ws://127.0.0.1:18080/relay/device
设备编号：<DEVICE_ID>
设备令牌：<一次性设备令牌>
```

保存后状态应变为已连接。使用域名时应改为：

```text
wss://<DOMAIN>/relay/device
```

### 11.3 使用

网页登录同一账号，进入 **我的 → 我的电脑**，选择在线设备。之后消息会转发到本地电脑，本地电脑必须保持 GUI 运行。

注意事项：

- 设备令牌只在创建时完整显示一次；遗失后用“轮换”重新生成。
- 桌面 GUI 关闭后，设备会离线。
- SSH 隧道断开时，`ws://127.0.0.1:18080` 也会断开，需要重启隧道并重连。
- 一个电脑同时连接的 App 数有上限，旧连接需要正常释放。

---

## 11.4 独立远端页

除了把消息转发到本地电脑，Web 还支持为某台已配对电脑打开一个**独立远端页**，不混入服务器会话侧栏。该页加载的是桌面 GUI 的完整前端，通过云中转把 RPC/事件转发到本地电脑：

```text
https://<DOMAIN>/desktop/<DEVICE_ID>?token=<REMOTE_TOKEN>
```

流程：

1. 用户登录 Web。
2. 在“我的 → 我的电脑”选择设备。
3. 网关校验设备属于当前账号，签发一个 23 小时有效、绑定该设备的 `remote` 类型令牌。
4. 浏览器在新标签页打开远端页；页面用该令牌连接 `/relay/client`。
5. 生成本地二维码后，手机用同一公网域名扫码即可打开同一个远端页。

安全约束：

- 远端令牌只能用于指定设备，不能调用管理接口。
- 远端令牌放在 URL 查询参数中，必须使用 HTTPS；Caddy 日志需对 `token` 查询参数脱敏。
- 令牌过期或设备被吊销后需要重新生成。
- 如果当前浏览器地址是 `127.0.0.1` 或局域网地址，手机扫码不能公网访问；必须使用公网域名打开服务器后再生成二维码。

构建服务器侧的完整桌面 UI（独立于 `web/dist`）：

```bash
cd frontend
npx vite build --base=/desktop-assets/ --outDir desktop-dist
```

网关通过 `XINGYAO_DESKTOP_DIST` 读取该目录，默认是 `/srv/xingyao/frontend/desktop-dist`。

## 11.5 桌面二维码走公网（可选）

云中转连上后，桌面「手机访问」二维码会优先给公网地址，手机不在同一 WiFi 也能扫：

```text
https://<DOMAIN>/m/<DEVICE_ID>?token=<REMOTE_TOKEN>
```

拿不到公网入口的任何一环（未开启、未连上、换票失败）都回落原来的局域网地址，
行为与从前一致。

换票：桌面上只有长期 device token，没有账号口令，所以走一条专用接口换取只读
`remote` 令牌——`POST /api/relay/device-token`，请求体 `{device_id, device_token}`。
网关侧三重收口：只签给未吊销且账号未启用的设备、票只绑自己的 `device_id`、
同一设备 10 分钟内最多换 6 次（超限返回 429，桌面回落局域网）。

### 部署独立移动端页面

不给这台机器部署移动端包时，公网二维码指向 `/desktop/<DEVICE_ID>`（桌面响应式 UI）。
要拿到专属移动端界面，再部署一份：

```bash
cd frontend-mobile
npx vite build            # base 已是 /m/，输出 dist/
# 上传到服务器，目录结构：
#   /srv/xingyao/frontend/mobile-dist/{index.html,assets/...}
#   /srv/xingyao/frontend/mobile-shim.js   （与 desktop 共用同一份 shim）
```

网关通过 `XINGYAO_MOBILE_DIST` 读取，默认 `/srv/xingyao/frontend/mobile-dist`；
目录不存在时 `mount_mobile` 不挂任何路由。

### 反代到子路径时注意

设备拨号地址形如 `wss://<域名>/xy/bot/relay/device`（子路径反代）时，页面地址是
`https://<域名>/xy/bot/m/<DEVICE_ID>`。shim 会按 pathname 推导出 `/xy/bot` 前缀再去连
`/xy/bot/relay/client`，这一环代码已处理；但 `index.html` 里的资源引用是构建期
写死的绝对路径（`/desktop-assets/` 或 `/m/assets/`），**不带子路径前缀**。所以子路径
反代时必须在反代层把 `/desktop-assets`、`/m/assets`、`/desktop-shim.js`、
`/m/mobile-shim.js` 也一并代理到网关，否则页面会白屏。

---

## 12. 备份与恢复

### 12.1 备份

至少备份：

```text
/srv/xingyao-state/gateway/gateway.db
/srv/xingyao-state/accounts/
/etc/xingyao/keyring_password
/etc/xingyao/jwt_secret
```

数据库建议使用 SQLite backup API，不要在写入过程中直接复制 WAL 文件：

```bash
docker compose -f deploy/docker-compose.yml exec gateway \
  python -c "import sqlite3, os; from pathlib import Path; p=Path(os.environ['XINGYAO_GATEWAY_DIR'])/'gateway.db'; out=Path('/tmp/gateway-backup.db'); s=sqlite3.connect(p); d=sqlite3.connect(out); s.backup(d); d.close(); s.close(); print(out)"
```

然后再把备份从容器复制到宿主机安全位置。

### 12.2 恢复

1. 停止网关。
2. 恢复 `gateway.db` 和账号目录。
3. 保持同一把 `/etc/xingyao/keyring_password`，否则已保存的模型 Key 无法解密。
4. 启动网关并检查 `/health`。
5. 登录后确认账号、模型和会话均存在。

---

## 13. 升级流程

```bash
cd /srv/xingyao
# 1. 先备份 gateway.db 和账号目录
# 2. 更新代码
git pull --ff-only

# 3. 重新构建前端
cd web && npm ci && npm run build && cd ..

# 4. 依赖或 gateway 代码变化时重建镜像
docker build -t xingyao-gateway:local -f gateway/Dockerfile gateway
docker build -f deploy/worker.slim.Dockerfile \
  --build-arg PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
  -t xingyao-worker:local .

# 5. 重建网关容器
export XINGYAO_JWT_SECRET="$(cat /etc/xingyao/jwt_secret)"
export XINGYAO_KEYRING_PASSWORD_FILE=/etc/xingyao/keyring_password
export XINGYAO_KEYRING_HOST_PASSWORD_FILE=/etc/xingyao/keyring_password
docker compose -f deploy/docker-compose.yml up -d --force-recreate gateway
```

升级后必须复验：

```bash
/health
/login
/api/admin/models（无明文 Key）
发一条真实消息
用量记录
worker 自动回收
```

不要直接在生产数据库中测试未完成的迁移。

---

## 14. 回滚

回滚代码前先确认数据库迁移是否可逆。推荐顺序：

1. 停止网关。
2. 用备份恢复 `gateway.db` 和账号目录。
3. 切回上一版代码和镜像。
4. 启动网关。
5. 检查 `/health`、登录、模型配置、历史会话。

如果数据库已经迁移到新版本，而旧代码不兼容，必须恢复旧数据库，不能只回滚镜像。

---

## 15. 常见问题

### `/health` 正常，但登录后没有模型

检查：

```bash
docker exec xingyao-gateway python -c "from pathlib import Path; import os; from gateway.db import connect; db=connect(Path(os.environ['XINGYAO_GATEWAY_DIR'])/'gateway.db'); print(db.execute('select providers_json, active_model_id from platform_models where id=1').fetchone())"
```

再用管理员接口确认 `api_key_configured=true`，但响应中不包含明文模型 Key。

### worker 启动后立即退出

检查：

```bash
docker ps -a --filter name=xingyao-worker
docker logs --tail 100 xingyao-worker-<account_id>
```

常见原因：

- `xingyao-worker:local` 镜像不存在。
- 钥匙串文件或口令权限不允许 UID 10001 读取。
- 账号数据目录由 root 创建，worker 无法写 `/data`。
- worker 启动时没有挂载到 `xingyao` Docker 网络。

### 刷新后侧栏为空或打开旧对话

清理浏览器缓存并强制刷新，确认前端资源已经更新。服务端问题应检查：

```bash
curl -fsS http://127.0.0.1:18080/ | grep index-
```

### 云中转设备一直离线

按顺序检查：

1. 本地桌面 GUI 是否运行。
2. 本地 SSH 隧道是否存在。
3. 云中转地址是否为 `ws://127.0.0.1:18080/relay/device`。
4. 设备编号和令牌是否来自同一台设备。
5. 管理后台设备是否被吊销。

---

## 16. 安全检查清单

- [ ] `/etc/xingyao/jwt_secret` 权限为 `0400`。
- [ ] `/etc/xingyao/keyring_password` 权限为 `0400` 或 `root:10001 0440`。
- [ ] 真实令牌和口令没有出现在 Git、日志、截图和聊天记录中。
- [ ] `/api/admin/models` 不返回明文模型 Key。
- [ ] 18080、8766、8765 没有直接暴露到公网。
- [ ] Caddy 访问日志对 `token`、`jwt` 做了脱敏。
- [ ] worker 数据目录和 workspace 只挂载对应账号的路径。
- [ ] 生产数据库已纳入备份计划。
- [ ] 管理员账号使用独立强口令，不与其他服务共用。
