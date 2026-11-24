# relay_server 部署手册（2026-11-17 生产验证版）

极简纯中转：无账号、无 worker、无管理面。桌面主动外连 WSS，手机扫码经
nginx TLS 反代打开公网页面。协议与旧网关逐帧一致，桌面端零改动。

## 1. 本机构建静态资源

```powershell
cd frontend
npx vite build --base=/desktop-assets/ --outDir desktop-dist   # 桌面远端页专用
cd ../frontend-mobile
npx vite build                                                  # base 已是 /m/，输出 dist/
```

注意：frontend/dist（默认 base）供桌面 GUI 与局域网用，不要覆盖；
源码构建远端专用包推荐用 desktop-dist（资源前缀 /desktop-assets/assets/*）。
若用户通过安装包部署（仅含 `_internal/frontend/dist`、无 Node 环境），也可直接上传
`frontend/dist` 到 `/srv/relay/static/desktop`，`relay_server` 会自动同时挂载 `/assets/`。

## 2. 上传（服务器 129.204.50.227，ubuntu，免密 sudo）

/srv 归 root：先 `sudo mkdir -p /srv/relay/{static,data} && sudo chown -R ubuntu:ubuntu /srv/relay`。

| 本机 | 服务器 |
|---|---|
| relay_server/ | /srv/relay/relay_server |
| requirements-relay.txt | /srv/relay/ |
| frontend/desktop-dist | /srv/relay/static/desktop |
| frontend-mobile/dist | /srv/relay/static/mobile |
| frontend/mobile-shim.js | /srv/relay/static/mobile-shim.js |

## 3. 运行环境

> **Python 必须是 3.11 或更高。** 锁定的 `starlette==1.10.0` 的 `Requires-Python` 就是 `>=3.11`，
> Ubuntu 22.04 自带的 3.10、20.04 自带的 3.8 都不够，需先 `sudo apt install python3.11 python3.11-venv`
> 再用它创建 venv。`requirements-relay.txt` 已把所有传递依赖锁到生产实测版本，
> 升级其中任何一项都要整套重测（fastapi / starlette / pydantic 互相牵制）。

```bash
python3 -m venv /srv/relay/venv
/srv/relay/venv/bin/pip install -r /srv/relay/requirements-relay.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
sudo install -d -m 0700 /etc/relay
openssl rand -hex 32 | sudo tee /etc/relay/jwt_secret >/dev/null && sudo chmod 0400 /etc/relay/jwt_secret
```

/etc/relay/relay.env（0400）：
RELAY_DB_PATH=/srv/relay/data/relay.db、RELAY_JWT_SECRET_FILE=/etc/relay/jwt_secret、
RELAY_BIND=127.0.0.1、RELAY_PORT=18080、RELAY_DESKTOP_DIR=/srv/relay/static/desktop、
RELAY_MOBILE_DIR=/srv/relay/static/mobile、RELAY_SHIM_PATH=/srv/relay/static/mobile-shim.js

## 4. systemd 与 nginx

- 本目录 relay.service → /etc/systemd/system/relay.service；daemon-reload；enable --now relay。
- nginx：
  - 全新独立服务器（个人域名或 IP）：使用本目录 `nginx-standalone.conf` 作为站点配置（已覆盖 `/health`、`/api/`、`/relay/`、`/m/`、`/desktop/`、`/assets/`、`/desktop-assets/`、`/desktop-shim.js`、`/icon.png`）；有域名再运行 `certbot --nginx -d <域名>` 配 HTTPS/WSS。
  - 官网子路径反代（`/xy/bot/`）：把 `nginx-bot.conf` 的路由段插入 `/etc/nginx/sites-available/xingyaocode`
    （生产机可直接从备份 `/etc/nginx/bot-cleanup-backup/xingyaocode.pre-bot-cleanup` 恢复）。
  - `sudo nginx -t` 通过才 reload。官网 root/SSL/其余 location 不动。

## 5. 设备与桌面配置

> `create-device` 会用 `RELAY_DB_PATH` 指向的库写设备记录，**必须与服务端 `relay.env` 里
> `RELAY_DB_PATH` 一字不差**；漏了它命令会落到当前目录的 `relay.db`，建出一份服务读不到的
> 孤儿库，表现就是换票一直 `401 设备令牌无效`、手机连不上。库文件属 root，命令要走 `sudo`。

```bash
sudo bash -c 'set -a; . /etc/relay/relay.env; set +a; cd /srv/relay && venv/bin/python -m relay_server create-device --name <名字>'
```

明文 device_id + token 仅此次输出。桌面 设置→云中转 填：
URL=wss://xingyaocode.cn/xy/bot/relay/device、设备号、令牌，打开开关。
二维码即 https://xingyaocode.cn/xy/bot/m/<设备号>?token=<23h JWT>。
CLI 另有 list-devices / revoke / rotate（rotate 会解除吊销并踢旧连接）。

## 6. 已知边界

- 换票限流每设备 10 分钟 6 次（portal 本地缓存票据，正常扫码不触发）。
- 服务以 root 运行、仅监听 127.0.0.1（后续可改专用用户加固）。
- 出站队列 500 / 镜像 128 帧 / App 名额 4 / 45s 无心跳标离线，与旧网关一致。
- 公网页面挂在 `/xy/bot/m/<设备号>`，而移动端前端 base 是 `/m/`：nginx 必须保留
  `location /m/`（反代 `127.0.0.1:18080/m/`），否则页面运行期请求的 `/m/assets/*`
  与子路由 `/m/convo` 会落到 404。改移动端前端后要按 §1 重新构建并整套上传。
- nginx 必须开 gzip 压缩 JS/CSS（`gzip_types` 里的 `text/javascript` 要能命中：
  relay 的 StaticFiles 把 .js 声明成 `text/javascript`，实测只在 http 级写它不生效，
  需在 `/m/`、`/xy/bot/`、`/xy/bot/m/` 各 location 里显式写一次）。不压缩时
  入口 JS 221KB、CSS 212KB，窄带外网首屏要十几秒。
- 升级代码：scp 覆盖 /srv/relay/relay_server 后 sudo systemctl restart relay；
  前端改动需按 §1 重新构建上传。
