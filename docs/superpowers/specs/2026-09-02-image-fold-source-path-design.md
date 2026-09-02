# 折叠占位符记录原图来源路径 — 设计

日期：2026-09-02
状态：已获用户批准（2026-09-02 会话内确认）

## 背景

GUI 聊天中，用户发送的图片以 base64 dataURL 存入消息 content（`image_url` 块）。
当会话中写入新的含媒体 user 消息时，`core/memory.py::_fold_stale_media_locked`
会把更早的图片块折叠为固定占位文本
`[图片/媒体内容已折叠以节省 Token；如需再次查看请重新提供]`，base64 字节永久删除。

用户诉求：占位符应携带**原图的来源路径**（从哪拖进来/粘贴落盘的路径），
需要再次查看时按路径自行找回原图。不复制、不归档图片。

## 设计

图片来源路径在入库时（`core/input_parser.py::parse_user_input`）是已知的，
当前构建 `image_url` 块时被丢弃。方案：

1. **入库时记录**：`input_parser.py` 构建图片块时增加顶层字段
   `source_path: os.path.abspath(file_path)`。
2. **发往模型前剥离**：`core/llm.py::_strip_media_and_merge` 的保留媒体分支
   （最近一条带图消息原样透传）构造 content 时剔除 block 顶层 `source_path`，
   防止严格校验的 provider 因未知字段报 400。
   Responses API 路径（`core/harness/request_envelope.py`）重建块时只取已知键，
   天然丢弃该字段，无需改动。
3. **折叠占位符带路径**：`memory.py::_fold_stale_media_locked` 中，
   图片块有 `source_path` 时占位文本写为
   `[图片已折叠以节省 Token；原图路径: <路径>]`；缺失时（老数据）回退原
   `_FOLDED_MEDIA_TEXT` 文案。

前端零改动：折叠后的占位文本自然显示在用户气泡里，路径可直接复制。

## 边界

- 改动前已折叠的历史图片无法补回路径（字节已删），仅对新发送的图片生效。
- 剪贴板粘贴的图记的是临时目录路径（`temp/ycode_paste/paste_*.png`），
  系统清理临时目录后失效；磁盘拖入/选择的图片记原始路径。
- 折叠时机不变：写入新的含媒体消息时折叠旧图，最新一张图始终保留。

## 验证

- 新增 pytest 用例：
  - input_parser 生成的图片块带 `source_path`（绝对路径）
  - `_fold_stale_media_locked` 折叠后占位文本含来源路径；无 `source_path` 的老块回退原文案
  - `_strip_media_and_merge` 保留媒体分支的输出负载不含 `source_path`
- 跑相关既有测试确认无回归。
