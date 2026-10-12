# 手机/平板拍照与相册上传设计

日期：2026-08-27
状态：待评审

## 背景与目标

手机端（局域网远程控制）目前点附件按钮走的是桌面 `pick_files`（手机端被 `FORBIDDEN_METHODS` 禁止且本身无意义），无法把手机/平板上的照片、拍照结果或本地文件作为附件发送。本设计为触摸设备（手机 + 平板，含 iOS / 安卓 / 鸿蒙浏览器）新增「拍照 / 相册 / 文件」三个入口，选中的文件经局域网传到电脑落盘，再作为附件加入输入框。

关键约束（来自需求确认）：
- 菜单三项：拍照、相册、文件，都要。
- 上传大小上限 50MB，超过拒绝上传。
- 平板（iPad 等）屏幕可能很宽（>1024px，甚至 1366px），按宽度会被误判成"电脑端"，因此**触摸入口的显隐绝不能依赖宽度断点**。

## 关键决策：触摸判定与布局解耦

入口显隐用 `pointer: coarse`（是否有触摸能力）判定，与布局用的宽度断点（`max-width: 768px`）完全解耦：

| 设备 | 宽度 | pointer | 布局 | 拍照/相册入口 |
|------|------|---------|------|--------------|
| 手机 | 390px | coarse | 紧凑 | 显示 |
| iPad 竖/横屏 | 820~1366px | coarse | 可能按桌面排布 | 显示 |
| 桌面拖窄窗口 | 700px | fine | 紧凑 | 不显示 |

边缘情况：带触屏的 Windows 笔记本 `pointer: coarse` 可能为 true，会多出「拍照/相册」入口，但点击后 `<input type="file">` 退化为普通文件选择框，功能不坏，可接受。

新增 `useIsTouch()` composable：`matchMedia('(pointer: coarse)')`，并监听 `change` 事件实时更新。

## 架构与数据流

手机端架构：手机浏览器加载 `core/lan_server.py` 提供的页面（同一端口 HTTP 静态服务 + WebSocket RPC），`mobile-shim.js` 用 WS 模拟 `window.pywebview.api`。注意 `save_clipboard_image` 在 `FORBIDDEN_METHODS` 中，手机端无法复用该 RPC 通道。

**关键约束（已验证）**：`lan_server.py` 的 HTTP 路由基于 `websockets` 库的 `process_request` 回调，其 `Request.parse` 对 `Content-Length != 0` 的请求直接抛 `ValueError("unsupported request body")`——**该通道无法接收带 body 的 POST 上传**。因此放弃"新增 POST /upload HTTP 端点"，改为**复用现有 WebSocket 连接走 RPC 上传**。

### 上传通道：WS RPC `upload_file`
- 手机经 `FileReader.readAsDataURL` 把文件编成 base64，通过现有 WS 连接发 `{"method":"upload_file","args":{...},"id":N}`。
- `upload_file` 不得加入 `FORBIDDEN_METHODS`（手机端必须可调）。
- token 在 WS 握手首消息已校验（`_handle_ws`），上传复用该已认证连接，无需重复带 token。
- **`serve(...)` 需调大 `max_size`**：默认 1MB（1048576），50MB 文件 base64 后约 67MB，必须显式调到 ≥ 70MB（`70 * 1024 * 1024`），否则消息被拒。

```
手机浏览器
  ├─ 点回形针 → 弹菜单（拍照/相册/文件）
  ├─ <input type="file" ...> 唤起系统相册/相机/文件管理器
  ├─ File → base64 → WS RPC upload_file(name, b64)
  └─ 响应 {ok, path} → addPaths([path]) → 附件 chip
                                       ↓
电脑 lan_server.py: _route_message → bridge.upload_file
  ├─ 校验大小 ≤ 50MB（超限返回 {ok:False,msg}）
  ├─ base64 解码
  └─ 落盘临时目录 → 返回路径
```

## 组件设计

### 1. `useIsTouch()` composable（新增 `frontend/src/composables/useIsTouch.ts`）
- 返回 `Ref<boolean>`，`pointer: coarse` 匹配结果。
- `matchMedia` 不可用（jsdom / 老环境）时按 `false`（桌面）处理。
- 监听 media query `change` 更新。

### 2. 后端上传 RPC（`core/lan_server.py` + `gui_bridge.py`）
- `lan_server.py`：`serve(..., max_size=70 * 1024 * 1024)` 调大消息上限。`_route_message` 无需特判（走统一 `_call_bridge`），只要 `upload_file` 不在 `FORBIDDEN_METHODS`。
- `gui_bridge.py` 新增 `upload_file(name: str, data_b64: str) -> Dict`：
  - base64 解码，校验解码后大小 ≤ 50MB（超限返回 `{"ok":False,"msg":"文件超过 50MB"}`）。
  - 落盘到 `tempfile.gettempdir()/ycode_paste`，文件名沿用时间戳+随机后缀防撞名，扩展名取原始文件名扩展名（无则 `.bin`）。
  - 返回 `{"ok": True, "path": ...}`。
  - 落盘逻辑抽成 helper，`save_clipboard_image` 复用（不改变其 8MB 上限与桌面禁用现状）。

### 3. 前端附件菜单（`useAttachments.ts` + `InputArea.vue` + `KimiEmptyState.vue`）
- `useAttachments` 新增 `uploadFiles(files: File[])`：逐个 `FileReader.readAsDataURL` → 取 base64 → `store.uploadFile` RPC → 收集返回路径后 `addPaths(paths)`；失败/超限弹 `notifyChat`。
- `store` 新增 `uploadFile(name, dataB64)`，走 `this._api().upload_file(...)`（mobile-shim 的 Proxy 会路由到 WS RPC）。
- 两个输入区（InputArea、KimiEmptyState）的回形针按钮：
  - 触摸设备：点击弹出小菜单「📷 拍照 / 🖼 相册 / 📁 文件」，分别触发三个隐藏 input：
    - 拍照：`<input type="file" accept="image/*" capture="environment">`
    - 相册：`<input type="file" accept="image/*" multiple>`
    - 文件：`<input type="file" multiple>`
  - 非触摸（桌面）：保持现状走 `store.pickFiles()`，完全不变。

### 4. 平台兼容
- iOS Safari / 安卓 Chrome / 鸿蒙浏览器均支持 `<input type="file" accept>` 与 `capture`，会由系统弹出「拍照/相册」选择。`capture="environment"` 优先后置相机。
- 菜单为本应用内的简单浮层（Teleport 到 body，与 ReasoningSelector 一致），不依赖各平台原生 action sheet。

## 错误处理
- WS 未连接 / 连接中断：RPC reject，前端提示"连接未就绪，请重试"。
- 超 50MB：前端选中后先用 `file.size` 预检拦截（避免无谓 base64 传输），后端 `upload_file` 再兜底校验，提示"文件超过 50MB，无法上传"。
- base64 解码失败 / 落盘失败：后端返回 `{ok:False,msg}`，前端提示"上传失败"。
- 配对 token 失效：WS 握手即被拒（现有行为），与本功能无关但需引导重新扫码。

## 测试
- 后端：`upload_file` 的单测（50MB 上限、base64 解码、落盘返回路径、非法 base64）；`serve` 的 `max_size` 已调大（集成/冒烟验证大消息不被拒）。
- 前端：
  - `useIsTouch` 在 matchMedia mock 下的真/假/缺失三分支。
  - `useAttachments.uploadFiles`：mock store.uploadFile，验证多文件、路径收集、失败提示、50MB 预检拦截。
  - 组件级：触摸设备点回形针弹菜单三项；非触摸直接走 pickFiles（现有行为不回归）。

## 范围外（YAGNI）
- 上传进度条（单文件 ≤50MB 局域网内秒级，先不做；后续可加）。
- 视频/文档的缩略图预览。
- 分片/断点续传、二进制 WS 帧优化（base64 33% 膨胀在局域网可接受）。
- 触屏 Windows 笔记本入口文案的精细化区分。
