# 云端浏览器接管（noVNC）设计文档

> 目标：让服务器上的星瑶在 headless 环境下也能「把人机验证交给你操作」。
> 约束：**桌面版（Windows）行为零变化**，所有新增能力只在 `server` 档位生效。

---

## 1. 背景与问题

### 现状

`core/computer/browser_backend.py:1249` 的分支决定了浏览器形态：

```python
def _persistent_context_kwargs(self, user_data_dir: str) -> dict:
    """Windows 保持有界面、不附加容器参数。Linux 恒无头。"""
    kwargs = {
        "user_data_dir": user_data_dir,
        "headless": sys.platform != "win32",   # ← Linux 恒为 True
        ...
    }
```

- **桌面版（Windows）**：`headless=False`，浏览器是真实窗口，用户可以直接点。
- **服务器版（Linux 容器）**：`headless=True`，只有 Playwright 截图，**没有任何输入通道**。

### 问题

`request_user_takeover`（`core/computer/controller.py:1077`）的实现只是把 run 状态改成 `waiting_for_user`：

```python
if action.kind == "request_user_takeover":
    self._transition(run, ComputerRunStatus.WAITING_FOR_USER.value)
    self._emit_state(run, reason=action.reason_code or "")
```

它**假设存在一个用户可以操作的浏览器窗口**。桌面版成立，服务器版不成立——于是遇到滑块、点选图片、短信验证码时，run 卡在「等待用户」，用户无处可点。

---

## 2. 目标与非目标

### 目标

1. 服务器上运行一个**非 headless** 的 Chromium，跑在虚拟显示器上。
2. 用户通过网页（noVNC）**看到并操作同一个浏览器实例**。
3. 用户完成人机验证后，星瑶从同一页面继续执行。
4. 全程需要鉴权，不能把浏览器画面裸露到公网。

### 非目标

- 不改变桌面版的任何行为（Windows 分支代码保持原样）。
- 不做多用户共享同一浏览器（每账号 worker 独立容器，天然隔离）。
- 不追求高帧率远程桌面体验（验证码场景够用即可）。

---

## 3. 方案总览

### 3.1 架构

```
                    ┌──────────────────────────── worker 容器 ────────────────────────────┐
                    │                                                                      │
  浏览器 ──HTTPS──▶ gateway(18080) ──relay──▶ LanServer(8766)                            │
                    │      │                          │                                    │
                    │      └── /novnc/<account>/──────┼──▶ websockify(6080) ──▶ x11vnc(5900)
                    │                                 │                          │         │
                    │                                 │                     Xvfb :99       │
                    │                                 │                          │         │
                    │                                 └── /computer-preview/ ──▶ Playwright ─┘
                    │                                                          Chromium
                    │                                                       (非 headless,
                    │                                                        DISPLAY=:99)
                    └──────────────────────────────────────────────────────────────────────┘
```

### 3.2 组件清单

| 组件 | 作用 | 端口 | 暴露方式 |
|---|---|---|---|
| **Xvfb** | 虚拟 X 显示器 | — | 容器内 |
| **Chromium** | 非 headless 浏览器 | — | `DISPLAY=:99` |
| **x11vnc** | X → VNC | 5900 | 仅 127.0.0.1 |
| **websockify + noVNC** | VNC → WebSocket + 静态页面 | 6080 | 仅 127.0.0.1 |
| **gateway 反代** | `/novnc/<account>/` 转发 | — | 公网（带鉴权） |

### 3.3 为什么是 noVNC 而不是别的方式

| 候选 | 结论 |
|---|---|
| CDP 远程调试 | 只能看 DOM，不能像人一样拖滑块 |
| 截图 + 坐标回传 | 需要自研输入通道，且手感差、易封 |
| **noVNC** | 标准方案，浏览器里直接操作，键鼠/滚动都能传 |

---

## 4. 关键设计决策

### D1：显示服务器用 Xvfb，Chromium 非 headless

- 容器启动时：`Xvfb :99 -screen 0 1440x900x24 -nolisten tcp`
- Chromium 通过环境变量 `DISPLAY=:99` 找到它
- 视口大小与现有 `computer_viewport` 配置保持一致，避免坐标换算

### D2：默认关闭，显式开启才生效

新增配置项（`config.json`，默认值保证与当前行为一致）：

```json
{
  "computer_remote_display_enabled": false,
  "computer_remote_display_port": 6080
}
```

- `false`：完全走现状（headless），不启动 Xvfb / x11vnc / websockify
- `true`：仅 `_server_profile()` 下生效

**这样即使代码上线，未开启配置的服务器行为与今天完全一致。**

### D3：代码隔离——Windows 分支一行不动

所有改动落在三个位置，且都有平台/档位守卫：

| 文件 | 改动 | 隔离方式 |
|---|---|---|
| `core/computer/settings.py` | 新增 `remote_display_enabled()` 读取函数 | 只在 `_server_profile()` 返回 True 时被调用 |
| `core/computer/browser_backend.py` | `headless` 改为按配置计算 | `headless = _resolve_headless()`，Windows 下恒为 `False`（与现在相同） |
| `serve_main.py` | 启动/停止 Xvfb 与 noVNC 子进程 | 只在 `YCODE_TOOL_PROFILE=server` 的入口执行 |

**判定函数（放在 settings.py）**：

```python
def resolve_headless() -> bool:
    """桌面版恒 False（有界面）；服务端看配置，默认 True（保持 headless）。"""
    if sys.platform == "win32":
        return False                       # 与今天完全一致
    return not remote_display_enabled()     # 默认 True，开启远程显示后才 False
```

**桌面版路径的可验证性**：Windows 上 `resolve_headless()` 直接返回 `False`，不读取任何新配置，不可能因为配置缺失而改变行为。

### D4：鉴权复用 worker token

- noVNC 不设密码（`x11vnc -nopw`），但**只监听 127.0.0.1**
- 公网入口只有 gateway 的 `/novnc/<account_id>/`
- gateway 校验用户 JWT + 该账号的 worker 归属后，才建立 WebSocket 隧道
- 与现有 `/computer-preview/` 的鉴权模型保持一致

### D5：失败必须降级，不能阻塞 agent

启动顺序与降级规则：

```
Xvfb 启动失败        → 记 warn，回退 headless（agent 仍可用，只是不能接管）
x11vnc/websockify 失败 → 记 warn，浏览器仍可操作（截图通道正常），只是看不到
Chromium 非 headless 启动失败 → 回退 headless 重试一次
```

**任何一环失败都不应该让 `computer_runtime` 起不来。**

### D6：资源上限

非 headless Chromium 比 headless 多占约 150–250 MB 内存。worker 当前是 `mem_limit: 2g`、`cpus: 2`，需要：

- `computer_remote_display_enabled=true` 时，容器内存上限建议提到 `3g`
- Xvfb 用 `-screen 0 1440x900x24`（24 位色，约 15 MB 显存占用）

---

## 5. 数据流（一次人机验证的完整过程）

```
1. 星瑶执行 computer_control → navigate 到需要验证的页面
2. 检测到验证元素（滑块/图形验证码）
3. 调用 request_user_takeover(reason_code="captcha")
   → run 状态变 waiting_for_user
   → 推送 computer_takeover_required 事件
4. 前端收到事件，弹出「需要你操作」卡片 + 一个「打开远程桌面」按钮
5. 用户点击 → 新标签打开 /novnc/<account>/（gateway 鉴权后反代）
6. 用户在 noVNC 里拖动滑块 → 验证通过
7. 用户点「我已完成」→ 前端调 rpc computer_resume(run_id)
   → run 恢复，强制下一次动作是 observe（旧坐标作废）
8. 星瑶从当前页面继续
```

**关键点**：用户操作的是**同一个 Chromium 实例**，不是另开一个浏览器。所以 cookie、登录态、验证结果全部延续。

---

## 6. 实施阶段

### P1：容器内跑通非 headless（不接公网）

- `deploy/worker.slim.Dockerfile` 增加 `xvfb`、`x11vnc`、`novnc`、`websockify`
- `serve_main.py` 增加 `_start_remote_display()`：
  - 启动 Xvfb
  - 设置 `os.environ["DISPLAY"] = ":99"`
  - 启动 x11vnc + websockify
- `browser_backend.py` 改用 `resolve_headless()`
- **验收**：容器内 `curl 127.0.0.1:6080` 返回 noVNC 页面；Chromium 进程带 `--display=:99`

### P2：gateway 反代

- `gateway/app.py` 增加 `/novnc/{account_id}/` 路由
- 校验 JWT → 找到该账号的 worker → 建 WebSocket/HTTP 隧道
- **验收**：从公网 `https://xingyaocode.cn/xy/bot/novnc/<账号>/` 能打开 noVNC

### P3：前端入口

- 聊天页收到 `computer_takeover_required` 事件 → 显示卡片
- 卡片上一个按钮 → 新标签打开远程桌面
- 一个「我已完成，继续」按钮 → 调 `computer_resume`
- **验收**：完整走通第 5 节的数据流

### P4：自动检测验证码

- 在 `core/computer/` 里加入验证页特征识别（常见滑块容器、`captcha` 关键字）
- 命中时自动触发 `request_user_takeover`
- **验收**：访问带验证码的页面时，无需人工提示就能弹出接管卡片

---

## 7. 验收标准

| 编号 | 场景 | 期望 |
|---|---|---|
| A1 | Windows 桌面版启动、操作浏览器 | 与改动前**完全一致**（回归测试） |
| A2 | 服务器 `remote_display_enabled=false` | 与改动前完全一致（headless 截图） |
| A3 | 服务器开启后打开 `/novnc/<account>/` | 看到 Chromium 画面 |
| A4 | 在 noVNC 里拖动滑块 | 容器内页面真的变化 |
| A5 | 完成验证后点「继续」 | 星瑶从当前页面继续执行 |
| A6 | Xvfb 启动失败 | agent 仍可用（降级 headless），日志有 warn |
| A7 | 未鉴权访问 `/novnc/` | 401，不泄露画面 |

---

## 8. 回滚方案

1. **配置回滚**：`computer_remote_display_enabled=false` + 重启 worker → 立即回到 headless。
2. **代码回滚**：改动集中在 3 个文件 + 2 个 Dockerfile，`git revert` 即可。
3. **镜像回滚**：worker 镜像保留旧 tag，gateway 的 `worker_image` 配置切回旧镜像。

---

## 9. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 内存超限 | 容器 OOM 被杀 | 开启时把 `mem_limit` 提到 3g；Xvfb 用 24 位色 |
| 带宽 | noVNC 画面流消耗流量 | 默认 8fps；只在接管时连接，其余时间不断开也行（画面静止几乎无流量） |
| 安全 | 远程操作入口被滥用 | 强制 JWT + worker 归属校验；noVNC 只监听 127.0.0.1 |
| 桌面版被波及 | 用户本机行为变化 | `resolve_headless()` 在 Windows 分支直接返回 False，不读新配置 |
| 多 worker 混淆 | 看到别人的浏览器 | 每个账号独立容器 + 独立 noVNC 端口，gateway 按 account 路由 |

---

## 10. 待确认事项

1. **视口分辨率**：沿用现有 `computer_viewport` 还是固定 1440×900？
2. **接管超时**：用户打开远程桌面后长时间不操作，run 是否自动取消？（建议 10 分钟）
3. **前端入口位置**：「围观操作」面板旁边加一个「远程接管」标签，还是独立页面？
