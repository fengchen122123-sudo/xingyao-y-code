# 计划：co-team 移动端界面移植到星瑶（独立子应用 + 独立入口按钮）

日期：2026-09-21　分支：`dev-mobile`

## 目标

1. 把 co-team mobile（`D:\pxx\co-team\mobile`，Vite + Vue3 + vant，暗色终端 IDE 主题）
   的界面壳搬到星瑶：主题 token、van-tabbar 底栏、卡片列表、ChatStream 聊天流、
   骨架屏、iOS 防误触缩放三件套。
2. 数据层重写为星瑶 bridge（壳子照抄，数据映射星瑶实体）。
3. 与现有 mobile 完全隔离：新增 `frontend-mobile/` 独立子应用，由 lan_server 在
   `/m/` 路径托管；现有 `/` 路由（桌面 dist + mobile-shim 注入）零改动。
4. 桌面侧栏底部 `phone-btn` 旁新增按钮，点击弹新移动端专属二维码。

## Tab 映射（5 个）

| co-team 原页面 | 星瑶新页面 | 数据来源 |
|---|---|---|
| 协作 ConvoList/ConvoDetail | 会话（会话列表 + 聊天流） | `get_sessions`/`get_initial_state`/`load_older_messages`/`send_user_message`/`stop_current_turn` + `assistant_delta`/`message_final` |
| 任务 TaskList | 任务（计划项状态卡片流，简化） | `plan_updated`/`plan_item_handoff` 事件快照 |
| 审批 ApprovalsView | 审批（权限请求队列） | `permission_request` + `respond_permission*` |
| 指标 MetricsView | 指标（token 用量概览） | `get_token_usage` |
| 设置 SettingsView | 设置（模型/权限模式只读 + 主题切换 + 连接状态） | `get_initial_state` |

不抄：沟通/成员/项目（星瑶无对应实体）。

## 实施步骤

- Phase 1 脚手架：`frontend-mobile/`（package.json、vite.config base=/m/、index.html 骨架屏、router、App.vue tabbar）
- Phase 2 主题与组件壳：style.css token 体系、StatusTag/MdView/utils、断线重连提示层（替代 co-team 的 REST token 门禁）
- Phase 3 数据层：bridge.ts（window.pywebview.api Promise 封装 + onBridgeEvent）+ pinia stores
- Phase 4 页面：会话/任务/审批/指标/设置（ChatStream 为核心移植）
- Phase 5 lan_server：`_serve_http` 增 `/m/` 路由（index 注入同款 shim + 静态穿越防护）；`bind_url` 加 path 参数；`gui_bridge.get_remote_bind_qr` 加 mobile 参数
- Phase 6 Sidebar：phone-btn 旁新增按钮 + 复用二维码弹窗结构，标题"新版移动端"
- Phase 7 打包与回归：ycode.spec datas、build_release.py 构建步骤、新增测试、py_compile + npm test + 双端 build

## 风险与边界

- 现有 mobile 与桌面 UI 零改动（`/` 路由、shim 注入、REPLAYABLE 白名单不变）
- 同一 WS 广播模型下 /m 页面与桌面页并存各自收事件，无冲突
- vant 只进 frontend-mobile 依赖，桌面 dist 体积不受影响
