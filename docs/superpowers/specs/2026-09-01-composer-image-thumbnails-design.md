# 输入框图片附件缩略图与灯箱预览 — 设计

日期：2026-09-01
状态：方案已与用户交互确认（后端归档 URL + 复用 ImageLightbox）

## 背景与目标

用户在输入框粘贴/拖拽/选择图片后，附件 chip 仅显示 emoji 图标 + 文件名，无法预览内容。

目标：
1. 图片附件 chip 直接显示缩略图（约 40px 圆角，object-fit: cover）。
2. 点击 chip 用现有 ImageLightbox 打开大图（滚轮缩放 / 多图切换 / Esc 关闭）。
3. × 按钮仍是移除附件；加载中或失败回退 emoji 图标，绝不阻断发送。

## 方案

后端把图片附件归档进内容寻址媒体仓库并返回受控 URL，前端 chip 渲染缩略图并复用灯箱。

### 后端（gui_bridge.py + core/lan_server.py）

- 新增桥接方法 `get_attachment_thumbnails(paths)`：
  - 逐个 `archive_media_file(_resolve_real_path(p))` 归档（SHA-256 内容寻址去重、
    MIME 魔数白名单、原子落盘，复用 core/media_store）。
  - 归档成功且 media_server 可用时返回 `{"path", "url"}`（带令牌 URL，与助手
    消息媒体渲染同一套服务）。
  - 单项失败（文件不存在 / 非白名单格式 / 仓库异常）→ 该项无 url；仅全部输入
    为空或内部异常时 ok=False。任何情况都不抛出阻断前端。
- `core/lan_server.py` 的 FORBIDDEN_METHODS 黑名单不动：该方法不在桌面专属
  封禁清单里，手机端经 WS RPC 可复用同一缩略图能力。

### 前端

- `types/pywebview.d.ts`：声明 `get_attachment_thumbnails(paths): Promise<any>`。
- `stores/appStore.ts`：新增 `fetchAttachmentThumbnails(paths)`：
  - 返回 `Map<path, url>`；api 缺失 / 异常 / ok=False / 空列表返回空 Map。
- `composables/useAttachments.ts`：
  - `addPaths` 在 `get_files_info` 归并后，对扩展名为图片（png/jpg/jpeg/webp/
    gif/bmp/avif）且仍有 url 能力的路径调 `fetchAttachmentThumbnails`，
    把 url 写入对应附件项（`thumb?: string`）。失败静默（缩略图是增强能力）。
  - 新增 `openAttachmentImage(path)`：收集当前附件列表中带 thumb 的 url 数组，
    `store.openLightbox(urls, indexOf(path))`；无任何 thumb 时 no-op。
- `components/AttachmentChips.vue`：
  - `a.thumb` 存在时渲染 `<img class="ac-thumb">`（40px、圆角、cover、加载
    失败 onerror 回退 emoji）；chip 本体可点击 → emit `open`；× 按钮
    `@click.stop` 仍是移除。
  - loading 占位 chip 不显示缩略图（路径未取回，无 url 可用）。
- `InputArea.vue` / `KimiEmptyState.vue`：绑定 `@open="openAttachmentImage"`。

### 数据契约

AttachmentItem 增加 `thumb?: string`（受控媒体 URL，约百字符，内存占用可忽略；
同一图片重复添加经内容寻址自动去重）。

## 备选方案（已否决）

- 前端 FileReader 转 dataURL：＋按钮选择的文件走 pywebview 原生对话框，
  前端拿不到 File 对象，覆盖不了全部来源；且 base64 常驻 pinia 内存。
- 静态 <img src="file:///">：现代 WebView 禁止 http 页面加载本地 file 资源，
  pywebview 环境不可靠。

## 测试

- 后端 pytest：`tests/test_gui_attachment_thumbnails.py`——png 项有 url（mock
  media_server）、txt 项无 url、混合列表 ok=True、归档抛错不冒泡。
- 前端 vitest：`AttachmentChips.test.ts`——thumb 渲染 img / 无 thumb 回退
  emoji / 点击 chip emit open / × 仍 emit remove、onerror 回退；
  `useAttachments.test.ts` 补 addPaths 写入 thumb 与失败静默断言。

## 验收

1. 粘贴图片 → chip 显示缩略图；点击 → 灯箱大图；× → 移除。
2. 拖拽 / ＋选择 / 手机上传同样生效。
3. 非图片附件仍显示 emoji；缩略图服务不可用时一切照旧可用。
4. `pytest tests/test_gui_attachment_thumbnails.py` 与 `npm test`（相关文件）全绿。
