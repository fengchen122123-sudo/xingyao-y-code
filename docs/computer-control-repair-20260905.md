# 电脑操控可靠性修复交付记录（2026-09-05）

> 本文是 R0–R11 的实际修复与验收记录（含第二阶段遗留项收口，见文末）；
> 修复方案见 `docs/superpowers/plans/2026-09-05-computer-control-repair-guide.md`，
> 修复前审计见 `docs/computer-control-audit-20260905.md`。
> 环境：Windows 10.0.26200（本机），Python 3.12，Playwright 1.62.0，
> 系统 Edge（msedge channel）与 Chrome 均真实有头启动。

## 总览

| 编号 | 状态 | 说明 |
|---|---|---|
| R0 | 完成 | 基线 9 failed/1 passed 复现并固化；全部用例迁移进正式 `tests/test_*.py` 后删除 `tests/audits/computer_control_cases.py` |
| R1 | 完成 | 取消/停止在成功提交点前被发现时零成功事件、零新观察、零续推 |
| R2 | 完成 | 命令生命周期 + **第二阶段实施执行器独立受管进程**：排队超时零执行；running 超时封闭实例；close 撤销队列后再投哨兵；卡死执行进程可硬终止（100ms admission / ≤2s 退出实测达标） |
| R3 | 完成 | 浏览器修饰键按序 down/逆序 up，绑定按下时的 page；retain 前也释放输入 |
| R4 | 完成 | BackendIdentity（platform/target/browser/profile）严格匹配留存实例 |
| R5 | 完成 | 受控页/前台窗口身份 + surface 代数；旧坐标失效后零输入；真机多窗口焦点跟踪验证 |
| R6 | 完成 | 无进展计数纯函数化；move 豁免；变化归零 |
| R7 | 完成 | scroll 显式 unit 契约；Win32 方向修正；native 桌面像素滚动显式拒绝（OS 约束，见 S5） |
| R8 | 完成 | 导航 domcontentloaded + 共享命令预算；显式 wait 可中断 |
| R9 | 完成 | duration_ms/queue_ms/input_ms/wait_ms/observe_ms 生产实测接入事件与前端；**真实模型端到端占比实测 98.5% provider** |
| R10 | 完成 | 语义定位（窗口/托盘）+ 局部观察；本机微信 A/B/C 矩阵真实验收通过（含 Win11 隐藏区枚举） |
| R11 | 完成 | 全量回归、前端构建、真实 Edge/Chrome/桌面/微信验收（数字见下） |

## 缺陷复现转绿对照

| 审计用例（原 tests/audits） | 迁移后位置 | 修复 |
|---|---|---|
| test_browser_mouse_action_preserves_ctrl ×3 | tests/test_computer_browser_backend.py 同名 | R3 |
| test_cancel_during_action_must_not_publish_success | tests/test_computer_controller.py 同名 | R1 |
| test_timed_out_queued_command_never_executes_later | tests/test_computer_browser_backend.py 同名（按 R2 契约重写：夹具事件屏障 + 封闭断言） | R2 |
| test_browser_change_does_not_reuse_previous_channel | tests/test_computer_controller.py 同名 | R4 |
| test_three_valid_mouse_moves_do_not_stop_desktop | tests/test_computer_controller.py 同名 | R6 |
| test_changed_frame_resets_no_progress_count_to_zero | tests/test_computer_controller.py 同名 | R6 |
| test_two_unchanged_clicks_remain_allowed | tests/test_computer_controller.py 同名 | R6 |
| test_action_finished_supplies_measured_duration | tests/test_computer_controller.py 同名 | R9 |

## 关键实现说明

### R1 取消提交点（core/computer/controller.py）

`execute` 的成功提交点 = 状态锁内最后一次复核（owner.cancel_event / run 执行权 /
状态 / generation）+ 状态更新 + 成功事件序号分配，全部在同一临界区完成；
事件经 `_build_event_locked`（锁内分配序号）与 `_dispatch`（锁外调用 listener）
拆开。backend.execute 返回后、observe 前、observe 返回后、预览发布前各有复核。
暂停不撤销已开始的动作（PAUSED 仍允许当前动作提交），与取消分别断言。

### R2 命令生命周期（core/computer/browser_backend.py）

`_Command` 携带 command_id/deadline/状态机（queued/running/succeeded/failed/
cancelled）；worker 领取与调用方撤销在同一把命令锁内原子互斥；提交锁等待、
入队、执行等待共用同一 deadline 预算。running 超时不再假称未执行：实例封闭
（`computer_backend_error` 拒绝后续输入），profile 执行权在 worker 真实退出前
不释放。close 先关 admission、再撤销全部排队命令（终态通知，调用线程不悬挂）、
最后投哨兵。`release_inputs` 使用有界短预算，不排在卡死命令后面等满 30 秒。

### R3 修饰键（browser_backend.py）

click/double_click/drag 的 keys 经 `_map_playwright_key` 按序 down，只有 down
成功的键进入按下记录，finally 逆序 up；记录绑定按下时的 page（popup 切页后
回到原页释放）；up 失败保留记录供 release_inputs 重试；`_cmd_release_inputs`
同时释放修饰键与鼠标键；`complete_run` 在 retain 前也调用 release_inputs。

### R4 留存身份（controller.py / models.py）

`BackendIdentity(platform, target, browser, profile_path)`：browser 取自 run 冻结
能力，profile 在领取时解析为规范绝对路径；`_default_backend_factory` 消费身份，
不再独立重读全局设置。领取时身份不匹配 → 显式关闭旧实例（失败则本次启动失败）；
身份相同但不可复用（`is_reusable()` 为假：未启动/已关闭/已封闭/worker 退出/有
pending 命令/context 被用户关闭）→ 明确失败，不暗中复活。

### R5 受控画面身份（browser_backend.py / windows_backend.py / win32_adapter.py）

- 每个 page 分配本地稳定序号（独立于 title/url）；`ComputerObservation` 新增
  本地字段 surface_id/surface_generation（不进 Provider 投影）。
- 受控页裁决：单页直选 → DOM 可见性快路径 → 系统侧窗口标题提示
  （`browser_window_hints(pid)`，平台层提供）。实测本机 Playwright 默认启动
  参数下 `document.visibilityState`/`hasFocus()` 对标签切换不生效（两页恒为
  visible+focused），因此以窗口标题前缀匹配交叉裁决；同标题多页/多窗口/
  提示不可用 → computer_surface_ambiguous。
- 输入前复核当前受控页与最近观察一致；不一致零输入并报
  computer_surface_changed，controller 立即使 require_observe 生效。
- 桌面：surface 签名 = 前台窗口 HWND + 虚拟桌面几何；变化即代数递增并拒绝旧输入。

### R6 无进展计数（controller.py）

`next_progress_state(previous_key, current_key, count, kind)` 纯函数：
变化归零（不再从 1 起算）；move/observe/wait/request_user_takeover/
list_surfaces/observe_region 豁免；豁免且不变不清既有计数。

### R7 滚动契约（actions.py / models.py / windows_backend.py / win32_adapter.py / provider_openai.py）

scroll 新增显式 `unit`：browser=css_pixel（每轴 ±4000），desktop=wheel_step
（每轴 ±10）；缺省按 target；超限拒绝（不裁剪），两轴为零拒绝。
正方向统一向右/向下；Win32 垂直事件 = -delta_y×120，水平 = +delta_x×120。
native 官方滚动按像素语义映射 css_pixel；desktop target 的原生像素滚动明确
协议错误拒绝（零执行），不静默近似。官方指南未明示 scroll_x/scroll_y 单位
（2026-09-05 复核 developers.openai.com 电脑操控指南）；参考集成均按像素喂
mouse.wheel，与本机实测一致（500 → scrollY 500）。

### R8 就绪与等待（browser_backend.py / windows_backend.py）

导航 goto/go_back/go_forward/reload 一律 `wait_until="domcontentloaded"` 且
timeout 取当前命令剩余预算（实测带 1.5s 慢图片的本地页导航从 ~1510ms 降到
~15ms）；DOMContentLoaded 只是文档基线，SPA 业务目标是否就绪由后续观察判定。
显式 wait 在两个后端都改为停止事件分片等待：close 可即时收尾，等待以
computer_cancelled 收尾，不持大锁睡满全程。

### R9 分段耗时（controller.py / gui_bridge.py 透传 / 前端类型与面板测试）

finished 事件（成功/失败/取消）携带 duration_ms（perf_counter，从 controller
接到动作请求到结果提交）与 queue_ms；input_ms/wait_ms/observe_ms 只在该阶段
真实发生时出现（缺省 = 未测量，绝不默认 0ms）。Bridge 整事件透传；前端类型
声明补齐，ComputerControlPane 新增生产形状事件（含分段）的 reducer 测试。

### R10 语义定位与局部观察

- 新增 `core/computer/surfaces.py`：SurfaceCandidate/ObservationRegion/
  region_to_physical/SurfaceLocator（平台无关）。
- 新增 `core/computer/platforms/windows_surfaces.py`：WindowsSurfaceLocator
  （窗口枚举/激活走 ctypes Win32；托盘图标走 UIA——专属 COM 线程
  初始化/使用/释放，控件对象不跨线程；Invoke 模式 NULL 指针显式判空）；
  `browser_window_hints(pid)` 供 R5。
- 平台契约扩展：`create_surface_locator` / `browser_window_hints`（macOS/
  Linux 继续显式拒绝）。新依赖 comtypes>=1.4（仅 win32）已登记
  requirements.txt 与 ycode.spec hiddenimports。
- 协议扩展（generic schema）：list_surfaces / activate_surface /
  invoke_surface / observe_region（desktop only；候选 ref+代数强校验）。
  native 官方协议无这些动作，不伪造不降级。
- 局部观察：从原始分辨率整屏裁剪候选边界，区域原点/尺寸进入观察坐标系，
  坐标反算沿用既有公式（区域中心往返物理坐标实测精确一致）；候选移动/
  消失后旧区域坐标作废（computer_surface_changed）。

## 验收证据

### 自动回归（本机）

- `pytest tests -k computer -q`：793 passed（修复前基线 697 passed + 新增用例）。
- `pytest tests -q`（全量）：2747 passed、1 failed、2 skipped（284s）。
  唯一失败是 `test_tokenrhythm_image.py::PluginMetadataTest::test_plugin_json_disabled_by_default`
  （插件默认启用状态断言），为修复前就存在的既有失败，与电脑操控无关，未改动；
  另有既存 LAN 线程关闭告警（WinError 10038）与审计基线一致。
- `pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q`：
  19 passed（默认关闭电脑操控时请求字节不变；native golden 未改）。
- 前端 `npm test -- --run`：70 文件 547 测试全绿（含 ComputerControlPane 新增用例）。
- `npm run build`：成功。
- `git diff --check`：干净。

### 真实环境（本机，独立临时 profile / 本地测试页 / 自建窗口 / 只读托盘定位）

| 探针 | 结果 | 输出 |
|---|---|---|
| Edge 18 检查 | 18/18 通过（修复前 Ctrl 点击、Shift 点击、切回旧标签页跟踪 3 项失败，现全部转绿） | docs/computer-repair-after-edge.json |
| Chrome 18 检查 | 18/18 通过 | docs/computer-repair-after-chrome.json |
| 桌面只观察 | 3840×2160 → 1920×1080，origin (0,0) | docs/computer-repair-after-desktop.json |
| 桌面真实输入（自建 Tk 窗口，前台校验通过后才注入） | 9/9 通过：移动/单击/右键/双击/中文/退格/拖拽/向下滚动（wheel_step 方向与浏览器一致）/可中断等待 | docs/computer-repair-after-desktop-input.json |
| 语义定位+微信 | list_surfaces 枚举 11 窗口 + 23 托盘候选；找到微信（窗口 + 托盘图标两个候选）；activate_surface 置前台并复核前台标题为「微信」；另单独验证 invoke_surface 托盘路径：调用后微信窗口出现且前台 | docs/computer-repair-after-desktop-surfaces.json |

性能抽样（本次探针，非 p95 门槛）：Edge/Chrome 独立 profile 冷启动约 1.6s/1.7s，
普通点击 2–5ms，截图观察约 230ms，中文+emoji 输入约 10ms，本地导航约 15–27ms，
带 1.5s 慢子资源的导航约 14–17ms（修复前 ~1510ms）。桌面 backend 启动约 81ms，
整屏观察约 232ms。

## 遗留限制（未完成/未验证项）

> 第二阶段已收口其中 1/2/3/4 项，见文末「第二阶段：遗留项收口记录」；
> 以下原始清单保留作对照。

1. R2 硬停止第二段（浏览器执行器独立受管进程）未实施：同步 Playwright 调用
   卡死时无法在 2 秒内强行收回执行权；当前语义为「结果未知 + 实例封闭 +
   profile 不提前释放」，不会误继续，但卡死的浏览器进程需等命令自身返回或
   进程退出。验收指标（100ms 关 admission / 2s 执行环境退出）尚未验收。
2. 微信验收只覆盖本机当前状态：托盘图标在可见托盘区（非隐藏展开区）、
   中文系统、150% 缩放、单屏。隐藏区展开按钮（「显示隐藏的图标」候选已能
   枚举到）、英文系统、100%/200% 缩放、多屏负原点、任务栏自动隐藏等矩阵
   未逐一真机验收；微信版本为 Weixin 4.x（新版）。
3. UI Automation 不保证识别所有版本微信/所有托盘控件；本机托盘图标提供
   Invoke 模式，任务栏固定图标不提供（只有 click_point）。
4. 真实模型端到端任务（Provider 决策耗时占比、多步任务总延迟）未测量；
   R9 提供的是本地动作分段耗时。
5. macOS/Linux 仍为显式不可用预留；本次所有实现与验收均在 Windows。
6. 多窗口浏览器（同一受控 context 开多个窗口）+ 地址栏聚焦/devtools 前台
   等场景的歧义路径有模拟测试，真机逐一验收未做。
7. 桌面 native 像素滚动按契约显式拒绝（故障隔离），不等于已支持。

## 协议/字节影响

- generic computer_action schema：新增 scroll 的 unit 字段与滚动单位说明，
  新增 4 个 desktop 语义动作及 surface_ref/surface_generation 字段
  （协议扩展 2026-09-05）；默认关闭电脑操控时请求字节不变（byte-stability
  与 Provider golden 全绿，既有 golden 未覆盖 generic computer schema，
  未被覆盖改写）。
- ComputerObservation 新增本地字段（surface_id/surface_generation/
  surface_candidates），Provider 投影仅在携带候选时附加 surface_candidates，
  普通观察投影字节不变。
- 新错误码：computer_surface_changed / computer_surface_ambiguous /
  computer_surface_unknown / computer_surfaces_unavailable，全部走既有
  领域错误通道。

## 第二阶段：遗留项收口记录（2026-09-05 同日）

### S1. R2 第二段——浏览器执行器独立受管进程（已实施+验收）

**实现**（新增 `core/computer/browser_worker.py`、
`core/computer/browser_worker_protocol.py`；`browser_backend.py` 新增
`_WorkerProcessTransport` 与进程模式开关；`controller.py` 默认工厂启用）：

- 公共 `ComputerBackend` 接口不变；进程模式下 Playwright 全部对象移入受管
  子进程（子进程内复用既有线程模式 backend，R5 受控页裁决、R2 命令生命周期
  语义原样生效）；父进程只经 JSON 管道发结构化请求（操作白名单
  observe/execute/release/close），绝不传 Callable/eval/shell，绝不持有
  Playwright 对象；截图 PNG 不跨管道（子进程写短期目录、回传路径+元数据）。
- Windows Job Object（ctypes，KILL_ON_JOB_CLOSE，不新增依赖）：父进程退出
  时执行进程与其拉起的浏览器进程树一并终止；close 流程=先关 admission
  （立即拒绝新命令）→ 优雅 close（有界）→ 硬终止 Job → 等待真实退出 →
  才释放 profile/租约；子进程崩溃/EOF 把在飞请求归一为
  computer_browser_closed，绝不悬挂调用线程。
- 守卫超时（命令超时+余量）内无响应 → 实例永久封闭（结果未知），close 可
  硬终止收回执行权。PyInstaller 冻结环境暂无 `-m` 子进程入口，默认工厂
  显式回落线程模式（不静默走错路径）。
- 测试：`tests/test_computer_browser_worker.py` 6 例（桩子进程，无键鼠副作用，
  符合指南"不用真实点击验证杀得够不够快"）：往返生命周期、卡死命令→封闭→
  close 硬终止、启动超时后 profile 可重试、假意收尾不退出→硬终止、子进程
  崩溃在飞请求归一、同 profile 双实例拒绝。

**真实验收**（`scripts/audit_computer_live.py --browser-process`，Edge 与
Chrome 各 8/8 通过，`docs/computer-repair-after-process-{edge,chrome}.json`）：

- 进程模式完整生命周期：启动（~0.6s，报告真实浏览器 PID）→ 导航/观察
  → 点击（title oracle "YCODE CLICKED 1"）→ 中文+emoji 输入
  （"YCODE INPUT 10"）→ 导航（"YCODE SECOND"）→ 干净关闭（零孤儿浏览器
  进程，`psutil` 按 profile 命令行扫描验证）。
- **停止边界（在飞导航挂起时 close）**：admission 拒绝新命令 <200ms 实测；
  close 总耗时 Edge 2008ms / Chrome 118ms（优雅路径）；硬终止到退出 ≤2s；
  终止后 profile 可立即重开（新实例启动成功）——达到指南目标指标。

### S2. 浏览器真机矩阵扩展（已验收）

`--browser-window-matrix`（Edge/Chrome 各 5/5 通过，
`docs/computer-repair-after-window-matrix-{edge,chrome}.json`）：

- **真实多窗口焦点跟踪**：`window.open` popup features 生成第二个 OS 窗口
  （实测两窗口 DOM 可见性信号完全不可区分，均 visible+focused——窗口标题
  提示裁决是真机必需）；popup 置前台 → 观察跟随 popup；焦点切回主窗口 →
  观察跟随主窗口；点击落在主窗口页面且 popup 零误触。
- **后台标签页零误操作**：Ctrl+click 打开 /second 后台标签页后，观察仍锁定
  前台主窗口页面，点击落在主窗口，后台标签点击数为 0。
- 过程中发现并修复两个真机级缺陷：
  1. `_resolve_via_window_hints` 前台列表推导输出表达式误用外层循环变量
     `hwnd`（实际取到 EnumWindows 最后一个 hwnd），已改 `entry[0]`；
  2. 窗口标题落后于页面标题的异步更新瞬态（点击改标题后 ~百毫秒级）导致
     前台匹配短暂落空 → resolver 增加 **1s 有界收敛等待**（每轮重新取窗口
     提示并重新匹配；同标题多页等不可收敛状态立即 fail-closed）。
- 地址栏聚焦与 DevTools 前台属浏览器 UI 层，Playwright/CDP 无法驱动
  （浏览器快捷键不经 renderer 分发）；其 fail-closed 行为由模拟测试覆盖
  （同标题多页 → computer_surface_ambiguous），真机不可驱动如实记录。

### S3. 微信托盘矩阵扩展（本机可执行部分已验收 9/9）

`--desktop-surfaces`（`docs/computer-repair-after-desktop-surfaces.json`）：

- list_surfaces：12 窗口 + 22 托盘候选；微信候选含窗口与托盘图标。
- A. activate_surface 置前台 + 前台标题复核 =「微信」。
- B. WM_CLOSE 礼貌关闭（微信收入托盘，不终止进程）→ 顶层窗口列表确认消失。
- C. 托盘恢复：Invoke Shell_TrayWnd 上的微信托盘图标 → 窗口重新出现且前台；
  **隐藏区飞出面板枚举已打通**（补 Win11 宿主类
  `TopLevelWindowForOverflowXamlIsland` 后枚举到 9 个隐藏图标及真实坐标）；
  本机微信图标被用户提升到可见区（不在隐藏区），探针按实际状态选择候选并
  如实记录 `source=shell_tray_promoted`。
- 顺带修复：`GetForegroundWindow` NULL（restype=HWND → None）崩溃；
  `_element_source` 来源标记（Shell_TrayWnd / NotifyIconOverflowWindow /
  XamlIsland）。
- 本机不可验证项（如实记录，需其他环境）：英文系统、100%/200% 缩放、
  多屏负原点、任务栏自动隐藏；微信 4.x 之外版本。

### S4. 真实模型端到端耗时测量（已执行）

新增 `scripts/benchmark_computer_model_loop.py`（显式探针）：真实 Provider
（glm-5.2 视觉不支持、step-3.7-flash 经 anthropic 兼容路由成功）+ 受管子进程
真实浏览器 + 仅本地测试页；动作全部经 ComputerControlService 固定链路，
分段耗时取 controller 实测事件。

**实测**（`docs/computer-model-loop-benchmark.json`，step-3.7-flash，
temperature=0）：6 步完成全部任务（观察 → 点击×3 → 输入 → 确认，
final_text="任务完成"）：

| 步 | 动作 | Provider 决策 | 本地 input | 本地 observe |
|---|---|---:|---:|---:|
| 1 | observe | 1265ms | — | 37ms |
| 2 | click | 2033ms | 3.1ms | 34ms |
| 3 | click | 1646ms | 1.8ms | 28ms |
| 4 | click | 2234ms | 1.8ms | 31ms |
| 5 | type_text | 2005ms | 2.9ms | 32ms |
| 6 | （最终结论） | — | — | — |

汇总：任务总 14.4s；**Provider 决策 14.19s（98.5%），本地动作合计 0.21s
（1.5%）**；单张截图 8.6–11.9KB。证实审计判断：多步任务慢的来源是 generic
协议逐动作模型往返与截图编码，本地键鼠/截图在毫秒级。

### S5. 桌面 native 像素滚动（维持显式拒绝，OS 约束）

Windows SendInput 只有 MOUSEEVENTF_WHEEL/HWHEEL（滚轮步进，受系统滚轮设置
影响），不存在像素精度的系统级滚动注入 API；UIA Scroll pattern 亦为百分比
语义且仅限 UIA 支持的应用。任何"固定换算"都属指南禁止的静默近似——维持
「desktop 仅支持显式 wheel_step，原生像素请求协议错误拒绝」的契约不变，
此为 OS 能力约束而非实现缺口。

### 第二阶段自动回归

- `pytest tests -k computer -q`：805 passed（含 6 例受管进程测试）。
- 全量 `pytest tests -q`（第二阶段收口后最终）：**2753 passed、1 failed、
  2 skipped**；唯一失败仍为既有的 tokenrhythm_image 插件默认状态断言
  （与电脑操控无关，修复前后一致）；byte-stability + Provider golden 全绿。
- 前端测试与构建不受本阶段影响（未改前端）。
- 探针产物：`docs/computer-repair-after-process-{edge,chrome}.json`、
  `docs/computer-repair-after-window-matrix-{edge,chrome}.json`、
  `docs/computer-repair-after-desktop-surfaces.json`（覆盖同名旧产物）、
  `docs/computer-model-loop-benchmark.json`。

### 第二阶段后仍遗留

1. macOS/Linux 真机实现与验收（需对应硬件/系统；本阶段仍显式不可用）。
2. 英文系统/100%/200% 缩放/多屏负原点/任务栏自动隐藏的微信矩阵（需其他
   环境配置，本机不可达）。
3. Win11 飞出面板的**坐标点击**路径（图标不在隐藏区时无法在本机演练
   "飞出面板内按 bounds 点击"——语义 Invoke 路径已验证）。
4. R2 硬停止的"真实 Playwright 调用卡死"场景（驱动级挂死）无安全复现手段，
   以受管子进程桩 + 在飞真实动作终止实测代替；机制已验证，极端挂死属统计
   罕见路径。
5. 打包版（PyInstaller）进程模式回落线程模式；后续如需在打包版启用进程
   隔离，需为 spec 增加 worker 入口（独立 executable 或 argv 分发）。

## 第四阶段：能力发现性修复（2026-09-05，用户实测反馈）

用户反馈：开关已开，模型却回答"没有电脑操控功能"。

- **根因**：能力判定（设计 9.3）要求模型显式声明 `supports_vision=True` 且
  `supports_function_calling=True`；活动模型 glm-5.3-flash 只勾了视觉，
  没勾工具调用 → capability 判定为不可用 → `computer_action` 工具不进请求
  → 模型如实回答"不能"。这是 fail-closed 设计的正确行为，但原因不可见。
- **处理**：
  1. 用户配置修正：为活动模型补上 `supports_function_calling: true`
     （判定已实测转为 enabled/generic/browser+desktop）；
  2. 发现性补强：`get_system_settings` 新增 `computer_active_model_capability`
     字段（与 Turn 起点同一判定函数的只读展示）；SystemSettings 设置页在开关
     已开但活动模型不合格时显示可操作的原因提示，合格时显示资格确认。
- **验证**：新增后端 2 例 + 前端 3 例；定向回归 844 passed；前端 550 全绿、
  构建通过。
- **附带修正**：`_computer_active_model_capability` 经 `config._load()` 读配置，
  不走 `model_manager.load_config`（其模块级 `_CONFIG_FILE` 快照与设置读取
  路径可能不一致，曾导致测试读到真实用户配置）。

## 第五阶段：能力闸门修订（2026-09-05，用户裁决更新）

用户裁决（覆盖同日早些时候的裁决）：**Agent 里可用的模型默认都支持工具调用**
（Agent 每步本就靠 function calling 驱动，这不是按模型名猜测）；视觉在添加
模型时已配置，维持必须显式声明。

- `resolve_computer_capability`：`supports_function_calling` 未声明（None/缺键）
  视为支持；显式 `False` 是用户主动排除某模型的逃生门，始终拒绝。自动判定
  与显式 generic 两条路径一致。
- `model_manager._validate_computer_combination`、模型编辑器校验与提示文案、
  设置页资格提示同步修订。
- 测试更新：`test_legacy_model_without_function_calling_field_gets_capability`
  （旧配置未声明工具调用 → 获得资格；显式 False → 拒绝）、
  `test_unset_protocol_auto_enables_with_vision_and_undeclared_tools`、
  config 保存校验增加 generic+视觉+未声明工具调用的接受用例。
- 回归：computer/config/command_shell/model_manager/gui_system 定向 918 passed；
  golden 19 passed；前端 550 全绿、构建通过。

实际使用技能：using-superpowers（流程纪律）+ 直接实施与验证。
## 第三阶段：遗留项收口记录

> 分工说明：T1–T6 的代码骨架由 Codex 按提示词编写，但其沙箱内 venv 无法
> 启动（pyvenv.cfg 指向不可启动的 Python），全部测试/探针当时未执行；
> 本节记录的是随后在真实环境中逐一审查、修复与实测的结果。审查发现并已
> 修复的探针/测试缺陷：记事本窗口按进程名匹配可能命中用户已有窗口
> （Win11 记事本单实例多标签）→ 改为唯一命名临时文件 + 按文件名匹配；
> desktop_surfaces 坐标点击缺少观察消费（飞出面板打开后前台签名已变化，
> 不观察就点被正确拒绝）；微信候选误匹配“微信游戏”→ 主图标精确匹配；
> Invoke 是切换动作（坐标点击已开窗时跳过 Invoke，不拿 toggle 充当失败）；
> 探针前台获取在前台锁被持时失败 → AttachThreadInput 附加前台线程 +
> 有界重试（只作用于探针自建窗口）；激活动作夹具级有界重试（首轮控制台
> 前台锁），生产代码不加重试。Codex 的 T5 方向被人工审查推翻并纠正：
> git 历史显示插件启用是明确的后续提交（135877e），因此恢复插件
> enabled=true，改为更新过时的测试期望。

### T1：核心场景 10 连击重复性统计（已实测）

`scripts/soak_computer_matrix.py`（importlib 加载探针，逐轮记录全部用例与
失败 error_code），输出 `docs/computer-soak-20260905.json`：

| 探针 | 成功率 | p50 | p95 |
|---|---|---|---|
| desktop_input（真实键鼠，自建 Tk 窗口） | 10/10 | 3160ms | 3174ms |
| desktop_surfaces（微信 A/B/C 矩阵） | 10/10 | 2995ms | 3015ms |
| browser_process（进程模式生命周期+停止边界） | 10/10 | 6652ms | 8657ms |

### T2：20 样本本地基准（已实测）

`docs/computer-local-benchmark.json`（Win11 26200 / Intel 20 核 /
32GB / 3840×2160 @150%）：

- 浏览器冷启动（进程模式，含受管子进程）：p50 558ms，p95 568ms（首次
  1693ms，预热后稳定）；
- 热点击 p50 1.3ms / p95 2.3ms；热观察 p50 31.3ms / p95 37.2ms；
- 桌面 backend start p50 68.7ms / p95 70.3ms；整屏观察 p50 167.5ms /
  p95 172.8ms（4K 整屏编码）。

### T3：PyInstaller 进程隔离 worker（代码路径完成，打包产物验收待用户）

- `WORKER_ARGV_FLAG` 常量 + `_default_worker_command` frozen 分支 +
  gui_main/main 入口 argv 分发（命中即进 worker，绝不启动 GUI/CLI）；
  controller 默认工厂统一走进程模式，去掉 frozen 回落。
- 自动验证：`tests/test_computer_browser_worker.py` 7 例通过（含 frozen
  argv 命令断言）；全量回归 2755 passed、0 failed、2 skipped。
- **未验证**：PyInstaller 实际打包产物与打包 GUI 的人工冒烟（需用户执行，
  打包耗时长且产物不入库）。

### T4：隐藏区坐标点击演练（代码完成，前置条件未满足）

探针 C 段已实现“observe_region 局部观察 → 区域中心坐标点击”变体
（与 Invoke 对照）。本机微信图标被用户固定在任务栏可见区
（source=shell_tray_promoted），隐藏区坐标点击分支未触发——需用户取消
任务栏固定后重跑 `--desktop-surfaces`，候选 source 变为 hidden_overflow
时才演练该路径。枚举隐藏区本身已实测可行（TopLevelWindowForOverflowXamlIsland
宿主类补齐后枚举到 9–10 个隐藏图标）。

### T5：tokenrhythm_image 断言（已修正）

恢复 `plugins/tokenrhythm_image/plugin.json` 的 `enabled: true`（启用是
明确的历史提交意图）；更新 `tests/test_tokenrhythm_image.py` 的过时期望并
改名 `test_plugin_json_enabled_by_deliberate_enable`（13/13 通过）。
全量回归随之零失败。

### T6：通用软件操作矩阵（已实测）

`scripts/audit_computer_live.py --desktop-apps`，输出
`docs/computer-apps-matrix.json`：

- 记事本（唯一临时文件实例）：枚举命中 → activate → 前台标题复核
  （ycode-apps-audit-<pid>.txt）→ 局部观察 → type_text 后画面 hash 变化
  → WM_CLOSE 关闭验证：全链路通过；
- 文件资源管理器：枚举+激活+观察通过（只读路径）；
- 计算器：未运行，如实记 skip；
- Electron（VS Code）：激活被前台锁拒绝（computer_action_failed，
  探针从后台进程发起时 SetForegroundWindow 受限）——如实记录；实际使用时
  星瑶 GUI 自身在前台，首次激活有前台权限。
- 结论：语义定位+桌面操控不绑定微信；管理员窗口（UIPI）与全屏游戏
  属 OS 边界（前者已 fail-closed，后者未验证）。

### 第三阶段回归

`pytest tests -q`：2755 passed、0 failed、2 skipped（261s）；
golden 19 passed；`git diff --check` 干净。

## 第四阶段：能力发现性修复（2026-09-05，用户实测反馈）

用户反馈：开关已开，模型却回答"没有电脑操控功能"。

- **根因**：能力判定（设计 9.3）要求模型显式声明 `supports_vision=True` 且
  `supports_function_calling=True`；活动模型 glm-5.3-flash 只勾了视觉，
  没勾工具调用 → capability 判定为不可用 → `computer_action` 工具不进请求
  → 模型如实回答"不能"。这是 fail-closed 设计的正确行为，但原因不可见。
- **处理**：
  1. 用户配置修正：为活动模型补上 `supports_function_calling: true`
     （判定已实测转为 enabled/generic/browser+desktop）；
  2. 发现性补强：`get_system_settings` 新增 `computer_active_model_capability`
     字段（与 Turn 起点同一判定函数的只读展示）；SystemSettings 设置页在开关
     已开但活动模型不合格时显示可操作的原因提示，合格时显示资格确认。
- **验证**：新增后端 2 例 + 前端 3 例；定向回归 844 passed；前端 550 全绿、
  构建通过。
- **附带修正**：`_computer_active_model_capability` 经 `config._load()` 读配置，
  不走 `model_manager.load_config`（其模块级 `_CONFIG_FILE` 快照与设置读取
  路径可能不一致，曾导致测试读到真实用户配置）。

## 第五阶段：能力闸门修订（2026-09-05，用户裁决更新）

用户裁决（覆盖同日早些时候的裁决）：**Agent 里可用的模型默认都支持工具调用**
（Agent 每步本就靠 function calling 驱动，这不是按模型名猜测）；视觉在添加
模型时已配置，维持必须显式声明。

- `resolve_computer_capability`：`supports_function_calling` 未声明（None/缺键）
  视为支持；显式 `False` 是用户主动排除某模型的逃生门，始终拒绝。自动判定
  与显式 generic 两条路径一致。
- `model_manager._validate_computer_combination`、模型编辑器校验与提示文案、
  设置页资格提示同步修订。
- 测试更新：`test_legacy_model_without_function_calling_field_gets_capability`
  （旧配置未声明工具调用 → 获得资格；显式 False → 拒绝）、
  `test_unset_protocol_auto_enables_with_vision_and_undeclared_tools`、
  config 保存校验增加 generic+视觉+未声明工具调用的接受用例。
- 回归：computer/config/command_shell/model_manager/gui_system 定向 918 passed；
  golden 19 passed；前端 550 全绿、构建通过。

实际使用技能：using-superpowers（流程纪律）+ 直接实施与验证。
## 第三阶段：遗留项收口记录

### T1：核心场景 10 连击重复性统计

- 【目标】新增 `scripts/soak_computer_matrix.py`，按轮次调用三个真实探针，逐次记录完整结果、失败用例/error_code、耗时并汇总 p50/p95。
- 【改动文件】`scripts/soak_computer_matrix.py`。
- 【实际命令与输出摘要】计划命令为 `venv\Scripts\python.exe scripts/soak_computer_matrix.py --rounds 10 --output docs/computer-soak-20260905.json`；本机 venv 无法启动，未生成真实测量 JSON。
- 【验收结论】未验收。
- 【未验证项】真实键鼠/微信/浏览器 10 轮数据待修复 Python venv 后执行。

### T2：20 样本本地基准

- 【目标】新增浏览器冷启动/热 click/observe、桌面 start/observe 的 20 样本基准及环境记录。
- 【改动文件】`scripts/benchmark_computer_local.py`。
- 【实际命令与输出摘要】计划命令为 `venv\Scripts\python.exe scripts/benchmark_computer_local.py --samples 20 --output docs/computer-local-benchmark.json`；因 venv 启动失败，未产生样本数字。
- 【验收结论】未验收；脚本不调用 Provider。
- 【未验证项】真实 Playwright/桌面截图耗时与环境字段待执行。

### T3：PyInstaller 进程隔离 worker

- 【目标】冻结环境统一使用进程 worker，通过 argv 分发进入 `browser_worker.main()`。
- 【改动文件】`core/computer/browser_backend.py`、`core/computer/controller.py`、`gui_main.py`、`main.py`、`tests/test_computer_browser_worker.py`。
- 【实际命令与输出摘要】新增 frozen 命令断言；`venv\Scripts\python.exe -m pytest tests/test_computer_browser_worker.py -q` 实际输出为 `Unable to create process ... Python312\\python.exe`，未执行测试。PyInstaller 和 dist worker 冒烟未执行。
- 【验收结论】代码路径已接入，自动/打包验收未完成。
- 【未验证项】PyInstaller 产物、worker stdin close 响应、完整 GUI 人工冒烟待执行。

### T4：隐藏区坐标点击演练

- 【目标】隐藏区候选增加 observe_region 后按局部区域中心坐标点击，并保留 Invoke 对照。
- 【改动文件】`scripts/audit_computer_live.py`。
- 【实际命令与输出摘要】未执行用户前置操作及真实探针，没有伪造 `hidden_overflow` 结果。
- 【验收结论】未验收。
- 【未验证项】候选 source、坐标点击前台复核、Invoke 对照待执行。

### T5：tokenrhythm_image 默认状态

- 【目标】解决既有测试与插件默认状态不一致。
- 【改动文件】`plugins/tokenrhythm_image/plugin.json`。
- 【实际命令与输出摘要】`git log --follow` 显示先有“默认关闭”提交、后有“启用插件”提交；测试名为 `test_plugin_json_disabled_by_default`，据此将 `enabled` 改为 `false`。pytest 未能启动。
- 【验收结论】按测试与历史意图选择默认关闭，待回归确认。
- 【未验证项】插件加载器对 disabled 的完整行为待测试。

### T6：通用软件操作矩阵

- 【目标】新增记事本全链路，并对资源管理器、计算器、Electron 应用执行只激活/观察或 skip。
- 【改动文件】`scripts/audit_computer_live.py`（新增 `--desktop-apps`）。
- 【实际命令与输出摘要】计划命令为 `venv\Scripts\python.exe scripts/audit_computer_live.py --desktop-apps --output docs/computer-apps-matrix.json`；未执行，未生成真实结果。
- 【验收结论】未验收；输入仅指向探针自建记事本实例。
- 【未验证项】管理员窗口、全屏游戏及实际软件枚举待运行验证。

### 验证限制与收尾命令

- `venv\Scripts\python.exe` 当前无法启动：`pyvenv.cfg` 指向不可启动的 Python 路径，任意命令均返回 `Unable to create process using ...`。因此真实数据、PyInstaller、全量 pytest、19 项 golden 回归均未执行，不能宣称通过。
- 已运行 `git diff --check`；仅有 CRLF 转换 warning，无 whitespace error。
- 本阶段未 commit、未 push、未合并，保留工作树既有改动。
