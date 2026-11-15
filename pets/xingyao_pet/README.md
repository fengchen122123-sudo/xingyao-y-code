# 星瑶 · 桌面宠物（xingyao_pet）

这是星瑶的桌面宠物：一个 Electron 应用，透明置顶、不抢焦点，会按台词冒泡。平时不用手动开——在星瑶里打开 **设置 → 桌面宠物 → 显示桌宠**，星瑶会通过 `core/pet_manager.py` 把桌宠拉起来；关掉开关进程随之退出。

星瑶执行任务时（turn_start / tool_start / turn_finish 等）会往桌宠子进程的 stdin 写一行 JSON，桌宠立刻冒泡；不想被打扰就在同一页关掉「任务提示」。

---

## 换电脑 / 新克隆仓库后让它跑起来

克隆下来的仓库里只有源码和资源（`src/`、`assets/`、`scripts/`、`package.json`、`electron-builder.config.js`、本文件），**`node_modules` 不在 git 里**。所以第一次要让桌宠能启动，需要先让它找到 Electron 运行时。三条路，按推荐顺序选一条：

### 路线 a（推荐）：在本目录 `npm install`

```bash
cd pets/xingyao_pet
npm install
```

会按 `package.json` 的 `devDependencies` 装上 `electron ^44`（附带 electron-builder）。首次约下载 100MB+，装完 `node_modules/electron/dist/` 里就有 `electron.exe`（Windows）或 `electron`（macOS/Linux），`pet_manager` 会自动按这个默认路径找。

国内网络下载慢或卡住时，先设镜像再装：

```bash
npm config set ELECTRON_MIRROR https://npmmirror.com/mirrors/electron/
npm install
```

### 路线 b：机器上已有别处的 Electron

如果这台机器别的项目里已经装过 Electron，不必重复下载：在星瑶 **设置 → 桌面宠物 → Electron 路径** 里填那个可执行文件的**绝对路径**（Windows 填 `...\node_modules\electron\dist\electron.exe`），配置会写进 `~/.ycode/config.json` 的 `pet_electron_path`，优先级高于默认路径。

### 路线 c：全局安装，走 PATH

```bash
npm install -g electron
```

装完后 `electron` 在 PATH 里，`pet_manager` 的最后一级兜底就会找到它。

### 验证

以上任一条完成后：打开星瑶 → 设置 → 桌面宠物 → 勾选「显示桌宠」。桌宠出现在屏幕右下角即成功。

启动失败时不要猜，直接看设置页返回的错误文案——它会带上尝试过的查找顺序（配置路径 → 本地 node_modules → PATH）以及具体失败原因。

---

## 为什么 `node_modules` 不进 git

`node_modules` 有约 369MB（基本全是 Electron 运行时），提交进 git 只会让仓库膨胀、每次拉取都变慢，而且跨平台/跨架构的二进制本来就不能复用。仓库根 `.gitignore` 里已有 `pets/xingyao_pet/node_modules/` 规则，它在本地装、不进版本库。

随代码提交的应该只有约 41 个文件 / 0.5MB。克隆后确认资源齐全：

```bash
git ls-files pets/xingyao_pet | wc -l        # macOS / Linux / Git Bash
git ls-files pets/xingyao_pet                 # Windows PowerShell（看完整清单）
```

重点看 `assets/` 是否完整：7 组帧动画目录 + 台词 txt + 托盘图标，少一组资源动画就会缺帧。

---

## 开发调试

单独运行桌宠（不经过星瑶，用于改代码时快速看效果）：

```bash
cd pets/xingyao_pet
node scripts/start.js
```

`scripts/start.js` 会先清掉宿主注入的 `ELECTRON_RUN_AS_NODE` 环境变量，再用装好的 Electron 拉起本目录，等价于 `npm start`。渲染层改动重启即可；注意此时没有父进程写 stdin，气泡不会自动冒。

自检日志在 `~/.ycode/xingyao-pet-data/.gate.log`，每次启动都会追加：

- `[gate] G1 PASS/FAIL frames=...` —— 帧动画资源是否加载完整；
- `[gate] G3 PASS/FAIL {...}` —— 窗口尺寸/置顶/气泡带高度是否正常；
- `[gate] BUBBLE ...` —— 每收到一条气泡消息记一条，用来确认星瑶 → 桌宠的通道是否通。

桌宠自己的运行数据也在同一个目录（`~/.ycode/xingyao-pet-data/`）：窗口位置尺寸存 `pet-state.json`，另有台词语言等偏好。想恢复默认位置/大小，关掉桌宠后删掉 `pet-state.json` 再开即可。

---

## 常见问题

**启动后立刻退出。**
桌宠用了单实例锁（`app.requestSingleInstanceLock()`）。如果已经有一个同名实例在跑（比如上一次星瑶没退干净，或者你手动 `node scripts/start.js` 起过一个），新进程拿不到锁会立刻退出，表现为「开了又没了」。先确认并结束残留的 electron 进程，再在设置页重新勾一次开关。

**`ELECTRON_RUN_AS_NODE` 相关报错（一启动就像在跑 node 脚本）。**
这个环境变量会让 Electron 以纯 Node 模式运行，窗口根本不会创建。`pet_manager` 拉起桌宠时已经自动清掉它，所以从星瑶开关启动一般不会踩到；如果你是在某些 IDE / 终端里手动 `node scripts/start.js`，注意先清掉这个变量（`scripts/start.js` 内部也做了清理），否则行为会异常。

**看不到气泡 / 收不到星瑶的任务提示。**
先看 `.gate.log` 有没有 `BUBBLE` 记录：有记录说明桌宠侧正常，检查设置页「任务提示」是否被关掉；没有记录说明父进程没写进来。Windows 上 Electron 主进程的 `process.stdin` 一挂上就 EOF（Chromium 收走了这条流），所以桌宠改用 `fd 0` 包了一层 `net.Socket` 来读；如果是从终端手动启动、没有真正的管道 stdin，这条通道会静默降级——这是预期行为，不是故障。

**装好了但设置页仍报找不到 Electron。**
确认三处至少一处成立：填写的「Electron 路径」指向真实存在的可执行文件；`pets/xingyao_pet/node_modules/electron/dist/` 下有可执行文件（`npm install` 中途失败常常只剩半个目录，删掉 `node_modules` 重装）；PATH 里 `electron` 能直接执行。
