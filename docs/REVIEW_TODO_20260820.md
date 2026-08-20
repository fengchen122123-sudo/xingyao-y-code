# 代码审查遗留待办（2026-08-20）

本分支（dev-review-fixes）已完成当天应修级问题及以下第 1–16 项遗留项。为保证审查可追溯，本文保留审查时的原始背景、位置、现状和建议；各项标题下新增的“完成状态”代表当前实现结果。

## 一、测试缺口（建议优先补）

### 1. CLI 自定义回答路径无测试

- 完成状态：已完成。CLI Enter 已抽取为可测试控制边界；失效请求不清空输入，也不回落为选项回答。
- 位置：`main.py` `_resolve_confirm_custom`（约 801-814 行）与 Enter 提交逻辑（约 2641-2657 行）
- 现状：GUI 侧 ask_user 自由文本回答有等价测试（`tests/test_permission_broker.py:457-513`），
  CLI 是主要入口却完全无覆盖
- 建议：补一条走真实 `ask_user` 轮询 + `_resolve_confirm_custom` 的端到端测试，
  重点断言「发送失败时文字保留在输入区、不 fall-through 到选项回答」

### 2. 凭据写入询问链路无测试

- 完成状态：已完成。真实工具管线已覆盖顶层敏感配置询问、普通数据文件/数据库放行、自定义 YCODE_DATA_DIR、尾点、插件子目录与 Windows 短路径；分类异常失败关闭。
- 位置：`core/harness/tool_adapters.py:192-224`（本次加固后的 sandbox guard）
- 现状：现有测试只覆盖 `_resolve_workspace_path` 层，未覆盖
  「credential_write=True → permission_checker 弹询问 → 批准后写入成功」完整链路；
  「沙盒开关打开时凭据写入仍询问」也无测试
- 建议：用现有 sandbox_guard 注入点构造 fake permission_checker，断言被调用/未放行。
  顺带补三条新加固路径的测试：
  - `confi~1.json` 8.3 短名写入 → 触发询问（依赖 Windows，可条件跳过）
  - `config.json.`（尾点）写入 → 触发询问
  - `~/.ycode/plugins/x/config.json` 子目录同名文件 → 不弹询问（本次已改为顶层限定）

### 3. 会话模型：switchSession 恢复与竞态无测试

- 完成状态：已完成。已覆盖 switchSession 恢复、版本竞态和在途响应不得覆盖新选择。
- 位置：`frontend/src/stores/appStore.ts` switchSession 模型恢复分支（约 886-896 行）
  与 `_sessionModelVersion` 版本守卫
- 现状：`appStore.modelSession.test.ts` 未 mock `switch_session`，恢复分支与
  竞态跳过分支（版本号变化）均无覆盖
- 建议：补 mock `switch_session` 的恢复用例 + 「在途期间 setModel 递增版本后
  switchSession 响应不覆盖本地选择」用例

### 4. 卸载器旧版 TUI 清理断言

- 完成状态：已完成。fixture 会真实创建旧版 tui 目录，删除断言不再恒真。
- 位置：`tests/test_uninstall_engine.py:73-75`（本次已更新三命名断言）
- 现状：已改，但 `tui/` 目录清理依赖 `_fake_install_dir` 是否创建该目录；
  若 fixture 未建 tui 目录，`not (install_dir / "tui").exists()` 恒真，断言无 teeth
- 建议：fixture 里补建 `tui/` 目录使断言真实生效

## 二、后端遗留

### 5. tiktoken 加载 failed 状态永久化

- 完成状态：已完成。编码器加载使用线程安全状态机，失败冷却后可重试且并发只加载一次。
- 位置：`core/memory_compressor.py:64-99` `_get_encoding`
- 现状：首次加载失败（如临时网络故障）后整个进程生命周期退回字符估算，不重试；
  另两线程同时见 idle 会各起一个加载线程（双下载，幂等无害）
- 建议：failed 后按指数退避允许重试（如 5 分钟冷却），或记录失败时间下次调用重新尝试

### 6. 小窗口突破 40% 预算线 + 测试口径不一致

- 完成状态：已完成。初次统一为 40%；后续针对大段摘要固定 60 秒超时问题，将预算进一步收紧为 `max(512, context_window * 0.25)`，并按本段 prompt token 使用 90–600 秒动态超时。
- 位置：`core/memory_compressor.py:356` `chunk_token_budget = max(2000, int(compress_window * 0.4))`
- 现状：4096 窗口时预算取 2000 而非 1638，加上指令+累积摘要+输出固定开销（~1200）
  可能贴近窗口；`tests/test_memory_compressor_chunks.py:80、186` 断言用 `int(4096*0.4)=1638`，
  与实现（2000）口径不一致，断言碰巧宽松才通过
- 建议：统一口径——实现去掉 max(2000,...) 改为 `max(512, window*0.4)`，或测试改为
  读取实现的实际预算值；两者取一

### 7. 每回合无条件写库

- 完成状态：已完成。首次回合先建会话；仅空绑定初始化模型，不做无变化 UPDATE，也不覆盖并发产生的新绑定。
- 位置：`gui_bridge.py:1886`（`_run_turn_worker` 内 `set_session_model_id`）
- 现状：model_id 未变也每回合 UPDATE+commit，写放大轻微
- 建议：先 `get_session_model_id` 比较再写。注意此写发生在 `ensure_session` 之前，
  非 pending 路径的新会话首回合 UPDATE 影响 0 行（静默丢失，第二回合才落库）——
  修复时把该调用移到 `ensure_session` 之后可同时解决

### 8. `min(real, estimated)` 切到更贵分词模型时选错边

- 完成状态：已完成。真实 token 用量携带模型身份，切换模型后作废旧模型基线。
- 位置：`core/agent_service.py:213-229` `get_session_token_baseline`
- 现状：切到分词密度更高的模型时 real（旧模型口径，偏小）与 estimated（新模型口径，
  偏大）取 min → 低估占用 → 延迟触发压缩（有 llm.py:3978 超窗重试兜底，不阻断）
- 建议：检测最近用量所属模型 ≠ 当前模型时直接作废 real 用 estimated

### 9. `is_ycode_data_path` 相对路径基准不一致

- 完成状态：已完成。相对路径按线程 workdir 解析，以 realpath + commonpath 判定数据目录边界；根目录统一来自 config.YCODE_DIR。
- 位置：`core/memory_kairos.py:41` 用 `os.path.abspath`（进程 cwd），
  而 `core/tools.py:191-205` `_resolve_against_workdir` 按线程 workdir
- 现状：现实构造场景（线程 workdir 恰好位于 ~/.ycode 内）几乎不存在
- 建议：如要彻底一致，`_is_within_dir` 复用 `_resolve_against_workdir`

### 10. 二分截断后缀未计入 token

- 完成状态：已完成。二分截断预留提示后缀 token，并对最终文本重新计数。
- 位置：`core/memory_compressor.py:373-374`，"...[该条消息过长，中段已截断]..." 约 15 token
- 建议：误差可忽略，可在 token_count 上加常量 20 严谨化

### 11. GUI 内已无途径修改全局默认模型

- 完成状态：已完成。新建自由/项目会话继承当前窗口模型，首条消息携带模型 ID 持久化。
- 位置：`gui_bridge.py` `set_model` 只写会话绑定（用户要求的行为）
- 现状：新会话未显式选模型时继承全局 `active_model_id`，但 GUI 界面已无任何入口
  修改它（CLI `/models` 可以）；设置页「激活模型」语义现在是「当前会话模型」
- 建议：产品决策——设置页模型管理里加「设为全局默认」按钮，或明确只用会话级

## 三、前端遗留

### 12. 行数统计基于 25 行截断文本

- 完成状态：已完成。后端基于完整 diff 计算 added/removed，25 行限制只影响展示；GUI 桥接和前端聚合保留完整统计。
- 位置：`frontend/src/utils/turnGroups.ts` `countDiffChanges`
- 现状：diff_ready 推送的 diff_text 是截断到 25 行的 display_text，大改动统计偏小；
  与后端 `_count_diff_changes` 口径一致（turn_renderer.py 也统计截断版）
- 建议：在 `collectFileChanges` 注释注明截断口径即可，或后端随事件附完整统计数

### 13. ChangedFilesPanel diff 解析无缓存

- 完成状态：已完成。ChangedFilesPanel 使用 computed Map 按路径缓存解析结果。
- 位置：`frontend/src/components/ChangedFilesPanel.vue:54` 模板直接调 `diffBlocks(c.diffText)`
- 现状：每次重渲染重新解析 diff 文本
- 建议：按 path 缓存为 computed，影响轻微

### 14. ChangedFilesPanel 硬编码颜色

- 完成状态：已完成。增删背景色改用 color-mix 与主题 --green/--red 变量。
- 位置：`frontend/src/components/ChangedFilesPanel.vue:281、288`
  `rgba(78,169,106,.13)` / `rgba(217,99,91,.13)`
- 现状：取深色主题 --green/--red 当前值，浅色主题不跟随（与 MessageBubble.vue:242
  既有先例一致）
- 建议：变量化为 `color-mix` 或独立 CSS 变量

### 15. turnGroups.test.ts 风格

- 完成状态：已完成。测试闭合符号已按文件风格格式化。
- 位置：`frontend/src/utils/turnGroups.test.ts:199` `});});` 两段闭合挤一行
- 建议：格式化与文件其余风格一致

## 四、架构级（记录在案，暂不动）

### 16. active_model_tuple 是进程级单值

- 完成状态：已完成。子代理启动快照与 custom inherit 均按 parent_session 解析；显式会话解析失败时禁止回退进程级 tuple。
- 位置：`core/app_context.py:131-139`
- 现状：多会话并行回合时 `_sync_active_model_context` 最后写入者胜出；本次已改为
  显式传 session_name（单会话场景正确），并行场景子代理 fallback 仍可能串会话
- 根治方案：per-session 快照（thread-local 或随回合传递），改动面大，需专项

---

## 当天已修复项（备查，勿重复修）

1. 会话模型读取断链：`get_token_usage` / `_recalc_token_usage` / `_ensure_fits_active_window`
   / `_run_turn_worker._sync_active_model_context` / `memory_compressor` 压缩窗口解析
   → 全部改 `get_active_model(session_name)`
2. pending 草稿会话切模型污染旧会话：前端 `draftModelId` + 首条消息随
   `send_user_message(model_id)` 落库（后端在 worker 启动前绑定）
3. PermCard 自定义回答草稿残留：watch `permRequest.reqId` 清空
4. setModel/switchSession 竞态：入口捕获 sessionName + `_sessionModelVersion` 版本守卫
5. 沙盒守卫绕过：realpath + `GetLongPathNameW` 8.3 还原 + 尾点/空格归一化 +
   ~/.ycode 顶层限定 + broker `active_function_name` 纵深防御
6. ChangedFilesPanel 相对路径打开失败：前端按 `store.workdir` 拼绝对路径
7. 会话绑定模型被删后死绑定：`model_manager.get_active_model` 回退全局
8. CLI ask_user 自定义回答失败 fall-through：改为直接 return 保留文字
9. 卸载器恢复旧版 TUI 清理项（注册表/快捷方式/tui 目录），修复自销毁卡死
10. 编辑事故：memory.py 重复 return/注释、chevron-up 图标路径、BUILD.md/README 措辞、
    根目录 `_h2.txt`/`_hist.txt`/`_tmp_verify2.py` 垃圾文件

---

## 最终收口

第 1–16 项现均有实现和回归测试。完整验证命令与结果记录在 `docs/superpowers/plans/2026-08-20-review-findings-remediation.md`。
