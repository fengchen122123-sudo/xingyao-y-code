# AUDIT-REPORT 复核与修复结果

复核日期：2026-08-12
复核基准：`dev` / `origin/dev`，提交 `2e488c05081b7f5a9a6d41bc1581e75724330945`

## 结论

原报告中的不少代码事实成立，但部分风险评级忽略了现有可达性和信任边界。逐项复核结果：

- `TRUE`：代码问题与报告核心描述一致。
- `PARTIAL`：局部事实成立，但攻击链、影响或默认可达性被高估。
- `FALSE`：按当前生产路径不成立，或属于明确的受信任扩展设计。

所有 `TRUE` 项均已修复或完成防御加固；`PARTIAL` 项已切断实际攻击链或收紧边界。`B1` 当前没有生产可达性（子代理模型 schema 和执行层均禁止嵌套）；`S16` 是受信任的进程内插件模型；`S18` 是用户手动终端能力，不等同于模型绕过授权。

## Bug 复核

| 编号 | 判定 | 处理结果 |
|---|---|---|
| B1 | PARTIAL | thread-local 深度机制本身不能跨线程继承；当前子代理调用明确使用 `allow_subagents=False`，schema 与执行层双重禁止嵌套，因此报告中的生产递归链不可达。 |
| B2 | TRUE | POSIX 终止改为进程组 `SIGTERM`，超时后 `SIGKILL`。 |
| B3 | TRUE | 前台、后台和计划验证进程统一使用独立 session。 |
| B4 | PARTIAL | Token 多字段缓存竞态成立；视觉缓存主要是重复计算风险。两类缓存均加锁。 |
| B5 | TRUE | MCP 关闭状态纳入连接锁，关闭后拒绝新连接，并停止、等待、关闭事件循环。 |
| B6 | TRUE | GUI 不再在选择/切换会话时修改 cwd；依赖进程级 cwd 的完整模型回合通过互斥锁串行化。 |
| B7 | TRUE | Vite 探测改为精确的 Y-CODE `application-name` 标记。 |
| B8 | TRUE | Dream 调用统一走 `core.llm` 懒加载包装。 |

## 安全项复核

| 编号 | 判定 | 处理结果 |
|---|---|---|
| S1 | PARTIAL | 管理工具默认不暴露给模型，但显式暴露时确有风险；修改类工具已要求权限，命令限制为裸 `npx/node/python`，危险环境变量被拒绝。 |
| S2 | TRUE | 前台、后台、验证三个入口统一应用危险命令策略。 |
| S3 | TRUE | 重写为 shell 分段和 token 级判断，覆盖参数重排、根目录/glob、Windows/PowerShell、设备覆写和下载管道执行。 |
| S4 | TRUE | PowerShell 单引号字面量统一转义。 |
| S5 | TRUE | ZIP 成员全量预验证，拒绝绝对路径、盘符、UNC、`..`、符号链接和目录联接；非法包在首字节写入前失败。 |
| S6 | TRUE | 卸载器仅重试删除自身，并只做非递归空目录删除；拒绝从目标目录外自销毁。 |
| S7 | PARTIAL | HKCU 不会自动升级为 machine，原报告链条被高估；machine 安装现强制位于 Program Files 子目录，S4/S14 注入链已切断。 |
| S8 | TRUE | MCP 参数、URL userinfo/query、认证 header 和环境变量统一脱敏。 |
| S9 | TRUE | `<memory>` 数量和长度受限；索引写入拒绝指令性描述，system prompt 只接收由代码生成的固定类别/未分类计数，不再接收任何可控标题、描述或文件名；具体记忆仅按需通过工具读取。 |
| S10 | TRUE | `cmd /k` 已禁止会话级自动放行。 |
| S11 | FALSE | 生产验证路径原本已通过授权 runner；仍在底层验证 helper 增加危险命令拒绝作为纵深防御。 |
| S12 | TRUE | 安装路径含分号时拒绝 PATH 注册。 |
| S13 | TRUE | 配置播种改用 `O_CREAT | O_EXCL` 原子创建，不跟随已存在链接。 |
| S14 | TRUE | UAC 参数使用 `subprocess.list2cmdline`，并通过 Windows `CommandLineToArgvW` round-trip 测试。 |
| S15 | TRUE | MCP 环境变量值一律对模型隐藏，不再按长度或少量键名判断。 |
| S16 | FALSE | 插件目录是明确的受信任进程内扩展边界；不是隔离沙箱。未改变既有插件架构。 |
| S17 | PARTIAL | 技能本身是用户启用的受信任提示扩展；元数据现限制长度、去除控制字符和换行，避免伪造目录结构。 |
| S18 | PARTIAL | 该接口服务于用户手动终端，不是模型工具；已通过精确前端身份、CSP、文件桥接边界和可执行文件拒绝收窄不可信 JS 面。 |
| S19 | TRUE | 文件预览限制在当前会话工作区内。 |
| S20 | FALSE | 原实现已要求存在的本地路径，不接受任意 URL；进一步禁止启动可执行文件、脚本和快捷方式。 |
| S21 | PARTIAL | 原外部工作区 guard 会重新确认，直接逃逸结论被高估；会话白名单现统一使用 `realpath + normcase`。 |
| S22 | TRUE | Windows 下 API Key 使用当前用户绑定的 DPAPI；Linux/macOS 使用系统 keyring。读取旧明文配置时自动迁移，备份同样不含明文。 |
| S23 | TRUE | JSON 分享默认移除 reasoning/system；JSON、Markdown、HTML 均脱敏已配置 API Key、Bearer、连接串和常见 token。 |
| S24 | TRUE | 前端增加严格 CSP。 |
| S25 | TRUE | MCP 子进程改为最小环境白名单；主进程 API Key 不再自动继承。 |
| S26 | TRUE | GUI 错误文本增加 Bearer、连接串、键值凭据及常见 token 脱敏。 |
| S27 | TRUE | 技能扫描按物理路径限制在扫描根/技能包内，并按物理路径去重。 |
| S28 | TRUE | MCP 状态、异常和工具错误统一经过脱敏。 |
| S29 | TRUE | MCP close 现在停止并 join 事件循环线程，再关闭 loop。 |

## Windows GUI 关闭行为

- 点击窗口右上角 `X`：隐藏主窗口并保留系统托盘图标。
- 托盘菜单“显示主窗口”：恢复并显示窗口。
- 托盘菜单“退出”：幂等取消模型回合与权限等待，终止内嵌终端，取消并有界等待子代理/活动回合，停止计划调度，关闭 MCP 与 SQLite，再销毁窗口和托盘。阻塞型 LLM/工具辅助调用使用 daemon worker，不再因 Python 线程池退出钩子把宿主拖到网络超时。
- 托盘初始化失败：`X` 按普通关闭处理，避免应用隐藏后无入口。

项目的“独立后台任务”仍保持原有持久化语义；托盘退出保证 Y-CODE GUI 宿主及其受管资源退出，不擅自终止用户显式启动、设计为跨会话运行的独立任务。

## 验证口径

验证包括：安全策略单测、MCP/配置/安装卸载/记忆/技能/GUI 生命周期回归、Windows DPAPI 实测、真实 pystray 创建/销毁冒烟、前端生产构建、Python 语法编译、完整 pytest 测试集和最终 diff 检查。
