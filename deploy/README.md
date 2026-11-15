# 部署：网关 + 工人容器

完整部署、升级、回滚和故障排查见 [DEPLOYMENT.md](DEPLOYMENT.md)。

目标机器只跑一个网关进程（或一个网关容器），工人容器由网关按账号创建。
所有平台模型 Key 都存在网关侧的加密钥匙串里，工人的卷里只留 `keyring:` 引用。

## 1. 钥匙串口令（必做）

口令只以 secret 文件形式注入，不写进环境变量明文、不进 git：

```bash
sudo install -d -m 0700 /etc/xingyao
sudo sh -c 'printf "%s" "换成你自己的强口令" > /etc/xingyao/keyring_password'
sudo chown root:10001 /etc/xingyao/keyring_password
sudo chmod 0440 /etc/xingyao/keyring_password
```

网关以 root 读取，工人以 UID 10001 读取，因此使用 `root:10001 0440`。
不要让其他用户读取；口令泄露等于所有模型 Key 泄露。

## 2. docker compose 部署

```bash
export XINGYAO_JWT_SECRET="$(openssl rand -hex 32)"
export XINGYAO_KEYRING_PASSWORD_FILE=/etc/xingyao/keyring_password
docker compose -f deploy/docker-compose.yml config   # 先校验变量和 secret 都注入了
docker compose -f deploy/docker-compose.yml up -d
```

没有设置 `XINGYAO_KEYRING_PASSWORD_FILE` 时 `docker compose config` 会直接报错停下来，
不会启动一个没有钥匙串口令的网关。

要点：

- `keyring-data` 卷（命名 `xingyao-keyring`）挂在 `/srv/xingyao-state/gateway/keyring`，
  里面是 `crypted_pass.cfg`，即网关的钥匙串数据库。
- 工人容器只读挂载同一个卷到 `/run/keyring`（`YCODE_KEYRING_FILE=/run/keyring/crypted_pass.cfg`，
  `YCODE_KEYRING_READ_ONLY=1`），读同一把口令，所以工人的 `HOME`／数据目录怎么换都能解密。
- 口令文件以 `mode: 0400` 挂进容器 `/run/secrets/keyring_password`。

## 3. systemd 部署（不开容器跑网关）

```bash
sudo cp deploy/gateway.service /etc/systemd/system/xingyao-gateway.service
sudo systemctl daemon-reload
sudo systemctl enable --now xingyao-gateway
```

unit 用 `LoadCredential=keyring_password:/etc/xingyao/keyring_password` 把口令交给服务，
进程里只看到 `$CREDENTIALS_DIRECTORY/keyring_password`。缺这个文件时服务会以
“缺少钥匙串口令”直接失败，而不是静默退回系统凭据存储。

## 4. 工人镜像

如果无法拉取 Playwright 官方基础镜像，使用 slim 版（国内可加镜像源参数）：

```bash
docker build -f deploy/worker.slim.Dockerfile \
  --build-arg PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
  -t xingyao-worker:local .
```

`deploy/worker.Dockerfile` 在启动 `gui_main --serve` 之前先跑
`python -m gateway.worker_bootstrap`：口令或共享钥匙串数据库不存在就退出，
避免工人起来后才发现模型 Key 解不开。

## 5. 自查

```bash
docker compose -f deploy/docker-compose.yml config | grep -A2 secrets
curl -s http://127.0.0.1:18080/health
```
