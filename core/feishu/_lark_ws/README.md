# `_lark_ws` · 飞书长连接传输层（第三方搬运）

本目录是**第三方代码的搬运副本**，不是星瑶原创实现。搬运目的见
`待开发/飞书集成/设计规格与实施计划.md` §3.2。

## 来源

| 项 | 值 |
|---|---|
| 上游仓库 | https://github.com/larksuite/oapi-sdk-python |
| 上游分支 | `v2_main` |
| 上游版本 | `lark-oapi` **1.7.3**（wheel：`lark_oapi-1.7.3-py3-none-any.whl`） |
| 许可证 | MIT（Copyright (c) 2023 Lark Technologies Pte. Ltd.），全文见同目录 `LICENSE` |

搬运的文件与上游路径一一对应：

| 本目录 | 上游路径 | 改动 |
|---|---|---|
| `client.py` | `lark_oapi/ws/client.py` | 见下「改造清单」 |
| `const.py` | `lark_oapi/ws/const.py` + `lark_oapi/core/const.py` 的相关常量 | 合并到一处 |
| `enum.py` | `lark_oapi/ws/enum.py` + `lark_oapi/core/enum.py` 的 `LogLevel` | 合并到一处 |
| `exception.py` | `lark_oapi/ws/exception.py` | 原样 |
| `model.py` | `lark_oapi/ws/model.py` | 内联 `lark_oapi/core/construct.py` 的 `init()` |
| `frame.py` | —（新增） | 替代 `lark_oapi/ws/pb/**` |

**未搬运**：上游 `lark_oapi/ws/pb/google/protobuf/**`（上游夹带的一整份 protobuf
运行时副本，约 50 个文件）与 `lark_oapi/core/**`、`lark_oapi/event/**`。

## 改造清单

1. **事件回调**：上游 `EventDispatcherHandler._do_without_validation(payload)` →
   本层注入回调 `on_event(payload: bytes) -> Any | None`，语义一致（返回对象会被
   JSON 序列化后作为 ACK 回给服务端）。
2. **帧编解码自写**（`frame.py`）：上游用 protoc 3.19 之前的老式生成代码并夹带
   旧 protobuf 副本；官方 protobuf ≥4.21 拒绝那种生成代码（实测报
   `Descriptors cannot be created directly`）。本协议只有 2 个消息、9 个字段，
   手写更小、零依赖、可完整单测。
3. **去掉 client_assertion 分支**：星瑶固定用 `app_id + app_secret` 鉴权。
4. **HTTP 客户端**：`requests` → 标准库 `urllib.request`（避免依赖未在
   `requirements.txt` 声明的包）。
5. **本地内联**：`JSON`（marshal/unmarshal 语义照搬，含 bytes→base64 回退）、
   日志（`logging` 到 `ycode.feishu.ws`）、`ExpiringCache`、`build_user_agent`、
   `Strings.is_empty`。
6. **新增 `stop()`**：上游没有停止入口（其宿主进程自行管理生命周期）；星瑶需要
   在应用退出时优雅关闭长连接，故 `start()` 改为在专用线程内跑独立事件循环、
   `stop()` 可跨线程请求退出。
7. **新增观察钩子** `on_connected`（`on_reconnecting` / `on_reconnected` 为上游原有），
   供服务层上报连接状态。

改造点在代码中以 `# [星瑶改造]` 标注。

## 维护约定

飞书若升级长连接协议，请按上表回上游对应版本对照 `client.py` 与 `frame.py`，
并把新版本号写回本文件与 `client.py` 的 `_SDK_VERSION`。
