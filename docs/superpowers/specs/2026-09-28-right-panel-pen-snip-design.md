# 右侧面板「画笔截图」设计（2026-09-28）

## 目标

右面板页签条新增截图入口：进入后整块右面板冻结为一张静态帧，用户用画笔自由
圈选范围（对齐参考交互：手绘笔画 → 包围盒红框 → 浮动工具条），把圈出的区域
生成为 PNG，支持「插入输入框 / 复制到剪贴板 / 保存到文件 / 重画 / 取消」。

覆盖范围是右面板全部内容——包括 DOM 工作台（文件/Git/后台任务/子代理）和
原生 WebView2 内嵌浏览器表面。

## 方案选型

**冻结帧 + 画笔圈选 + 前端裁剪**（freeze-then-crop），一次屏幕捕获：

1. 前端测量 `#rightbar` 矩形（CSS px，与浏览器表面上报同一坐标系），调桥
   `capture_right_panel_region(rect)`；
2. 宿主在**原生表面仍可见时**按 DPI 缩放换算物理 bbox，`PIL.ImageGrab.grab`
   捕获屏幕区域 → PNG dataURL 返回；
3. 前端置 `rightPanelSnipActive=true` → 浏览器表面经既有
   `useEmbeddedBrowserSurface` 状态机 hide（`shouldShowSurface` 增加 snip 门控）；
4. 覆盖层以 1:1 尺寸显示冻结帧，其上叠画笔 canvas；笔画结束求所有笔画的
   并集包围盒，显示红框与浮动工具条；
5. 确认动作时在前端按快照物理分辨率裁剪出 PNG。

否决的替代方案：

- **html2canvas 栅格化 DOM**：拍不到原生 WebView2 表面（它不在 DOM 里），
  渲染结果与真实像素有偏差，额外重依赖。
- **先画框再二次捕获**：画笔覆盖层会被原生表面盖住（原生控件永远绘制在
  DOM 之上，见 useEmbeddedBrowserSurface 头注释），无法在浏览器内容上画；
  两次捕获还有对齐接缝。

一次捕获天然拿到全部像素（含原生表面）、覆盖层与捕获区域逐像素对齐，
且收起原生表面后画笔交互不受遮挡限制。

## 后端（gui_snip_mixin.py，新 Mixin 挂 ApiBridge）

- `capture_right_panel_region(rect)`：
  - rect 校验（x/y/width/height 数字）→ 沿用 browser mixin 的
    `unsupported_platform` 约定；
  - 复用 `EmbeddedBrowserController._compute_scale` 的 DPI 换算
    （GetDpiForWindow → DeviceDpi → 1.0）。pywebview winforms 启动时已
    `SetProcessDPIAware()`，`ClientToScreen`/`GetClientRect`/`ImageGrab`
    同处物理像素空间（与内嵌浏览器定位一致的前提）；
  - bbox = 客户区原点 + css×scale，夹紧到窗口客户区（负坐标/越界防护）；
  - 捕获入口收口为模块级 `default_panel_grab(bbox)`（惰性 ImageGrab），
    测试 monkeypatch 注入；
  - PNG 编码 → base64 dataURL；base64 > 6MB 时按最长边 1920 等比缩小重编
    （桥回传 payload 防御，对齐 windows_backend 的 1920 上限惯例）；
  - 返回 `{'ok': True, 'data_url', 'width', 'height'}`；所有异常吞掉归一
    为 `{'ok': False, 'msg'}`，绝不抛穿 js_api。
- `snip_copy_clipboard(data_url)`：PNG 解码 → PIL 转 24 位底向上 DIB →
  ctypes 写剪贴板（CF_DIB）。GlobalAlloc/SetClipboardData 显式设
  `restype = ctypes.c_void_p`（64 位句柄截断教训，同 win32_adapter 注释）。
- `snip_save_image(data_url, suggested_name)`：pywebview SAVE_DIALOG
  （默认目录 `~/Pictures`，回退家目录）→ 写文件 → 返回路径；取消返回
  `{'ok': False, 'msg': '已取消选择'}`（与 pick_files 约定一致，前端不提示）。
- 纯函数拆出便于测试：`physical_bbox_for_css`（坐标换算+夹紧）、
  `decode_png_data_url`（MIME 白名单 + 大小上限）、`dib_bytes_from_image`
  （DIB 打包，头字段可断言）。

## 前端

- `appStore.rightPanelSnipActive`（不持久化、不会话隔离——模态瞬态）；
- `RightPanel.vue`：页签条「+」左侧新增画笔入口（data-test=tabbar-snip）；
  点击后先 `capture_right_panel_region`（表面仍可见）→ 成功才置
  snipActive 并挂载覆盖层；快照 dataURL 走组件 prop，不进 store；
- `RightPanelSnip.vue`（新，绝对定位 inset:0 盖住整个 #rightbar）：
  - 分层：冻结帧 img（w/h 100% 与捕获矩形 1:1）→ 半透明压暗 → 画笔
    canvas → 红框 div → 浮动工具条 → 顶部提示条；
  - 笔画：pointer 事件 + setPointerCapture，实时线段绘制；笔画结束求
    并集包围盒（<6×6 视为误触丢弃）；可继续叠加笔画扩大选区；
  - 工具条：发送到输入框 / 复制 / 保存 / 重画（清笔画保留模式）/ 取消；
    画笔颜色红/蓝/黑三档（只影响笔迹，不影响裁剪）；
  - Esc 取消；window resize 时自动取消（冻结帧与布局错位）；
  - 裁剪：`ratio = img.naturalWidth / 覆盖层 clientWidth`，源矩形 =
    包围盒×ratio，输出物理分辨率 PNG dataURL；
  - 动作：插入输入框 = `save_clipboard_image(data_url)` 落盘受信附件 →
    `addPaths` 当前会话附件 → 关闭；复制 = 优先
    `navigator.clipboard.write`（image/png），失败回退
    `snip_copy_clipboard`；保存 = `snip_save_image`。
- `BrowserPane.vue`：`shouldShowSurface` 最前面加
  `!store.rightPanelSnipActive`——冻结帧已含浏览器像素，原生表面必须让位；
  门控放最前，星瑶自动化视口（automationViewport）也不能越过 snip 模态；
  snip 关闭后既有 watch 自动 re-show 表面，无需额外桥调用。
- `pywebview.d.ts`：补三个 api 方法签名。

## 边界与错误处理

- 桥不可用 / 非 Windows / 捕获失败：toast 提示，不进入 snip 模式，表面不动；
- 会话切换：覆盖层 watch 当前会话 id，切换即关闭（附件插入目标始终是
  用户看得见的当前会话）；
- 面板收起 / 选择页状态：入口按钮仍在页签条（页签条在选择页也存在），
  捕获的是整块右面板当前显示内容，行为一致。

## 测试

- Python `tests/test_gui_snip_mixin.py`：bbox 换算/夹紧/负坐标、dataURL
  解码拒绝、DIB 头字段、capture 编排（fake form + monkeypatch grab 与
  DeviceDpi=120）、save 落盘与取消分支；
- 前端：BrowserPane 表面门控（snipActive 时 dispatch browser_pane_hide）、
  store 标志默认值；沿用既有 vitest 模式。
