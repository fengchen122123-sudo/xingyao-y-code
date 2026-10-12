# 电脑操控代码审计与测试报告（2026-09-05）

结论：现有实现具有真实输入能力，但尚不能以原有测试全绿作为可靠性验收。新增审计复现了 9 个失败用例；Edge、Chrome 的真实本地页面测试各复现 3 个问题。当前只增加审计材料和测试，没有修改生产代码、现有断言、golden、用户配置或提交 Git。

## 范围与执行结果

工作分支：dev；开始时工作树干净。检查范围包括 core/computer 的动作解析、两个后端、Win32 适配、控制器、两种 Provider、工具适配、能力/设置、热键/预览/运行时，以及 Harness、GUI 和前端接线。

| 测试层 | 实际结果 | 解释 |
|---|---|---|
| 原有电脑操控相关 pytest（`-k computer`） | 672 通过，20.24 秒 | 模拟输入/浏览器/截图为主，包含集成接缝，不能替代真实操作验收 |
| 原有后端全量 pytest | 2620 通过、1 失败、2 跳过，287.07 秒 | 失败为图片插件默认启用状态；另有 LAN 服务器线程关闭警告 |
| 前端全量 Vitest | 70 文件、546 测试全部通过 | 包含电脑面板、控制条、设置、事件归约等 |
| 新增审计回归 | 9 失败、1 通过 | 正常断言，未使用 xfail/skip；有意保留缺陷复现 |
| 真实 Edge 本地页面 | 18 检查，15 通过、3 失败 | Ctrl 点击、Shift 点击、切回旧标签页后跟踪失败 |
| 真实 Chrome 本地页面 | 18 检查，15 通过、3 失败 | 与 Edge 相同 |
| 真实 Windows 后端预检/截图 | 通过 | 实测物理桌面 3840×2160，模型图 1920×1080，原点 (0,0) |
| 真实 Windows 键鼠输入 | 未执行 | 自建测试窗口未通过前台标题校验，探针在第一次输入前拒绝执行 |

后端全量唯一失败：`tests/test_tokenrhythm_image.py::PluginMetadataTest::test_plugin_json_disabled_by_default`，仓库 `plugins/tokenrhythm_image/plugin.json` 实际为 `enabled: true`，测试期望 false。此项与本次电脑操控发现无关，未改动。警告来自 `test_issue_token_kicks_connected_clients` 的 LAN 线程 `WinError 10038`，未在本次扩展排查。

环境说明：初次虚拟环境启动失败是执行沙箱拒绝访问其引用的本机 Python；在获自动审查授权的执行环境中同一个 venv 正常运行。没有重建环境或修改依赖。

## 微信托盘场景

目前桌面后端提供整屏截图和坐标输入，动作白名单中没有托盘枚举、按应用名激活窗口、UI Automation 元素查询、局部放大观察等能力。它不会自动查到“微信”这个托盘对象，必须由模型从画面找图标，再逐步点击隐藏图标箭头、识别图标并打开窗口。

本机截图确实从 3840×2160 缩至 1920×1080，每个方向缩小一半。一个原本 32×32 的图标只剩约 16×16；多屏会把同一 1920 最长边额度分给所有屏幕。隐藏托盘中的图标在展开前根本不在截图里。当前提示词只有通用“观察→动作→观察”，没有托盘任务说明。

另一个已复现的问题是正常移动鼠标也进入“三次无变化就停止”的计数：在画面不变时连续移动三次会停止，可能妨碍寻找托盘图标、等待悬浮提示等操作。

这些是明确的能力缺口和可复现机制问题，但本次没有重放用户当时的微信任务、没有取得那次模型请求和动作轨迹，因此不能认定为那次失败的唯一根因，也没有声称已通过微信端到端验收。

## 确认的问题与最小修复方向

### 1. P1：超时只是通知调用者，排队动作仍会稍后执行

位置：`core/computer/browser_backend.py:595`（`_call`）、`:332`（`_command_loop`）。

`command.done.wait` 超时后直接抛错，命令没有撤销标记；worker 取到它后仍直接执行 `command.fn()`。复现方法是先阻塞 worker，再投递一个模拟输入命令，等两者都返回超时后释放阻塞，输入副作用仍然发生。

后果：用户以为动作已失败或已停止，页面仍可能发生迟到点击。停止时的 release_inputs 也排在同一个串行队列中，无法直接打断卡住的命令。

最小修复方向：给命令建立明确生命周期与取消/截止状态，worker 在执行前检查；让“已排队”和“已经开始”的超时分别处理，保证已超时排队命令零执行。不要通过自动重试输入补救。对在执行的 Playwright 操作设计有界中断/关闭流程，并验证停止期间不再接受新输入。

证据：`test_timed_out_queued_command_never_executes_later` 失败。

### 2. P1：Ctrl/Shift 鼠标修饰键被浏览器后端忽略

位置：`core/computer/browser_backend.py:499`、`:540`。

解析器接受 click/double_click/drag 的 keys，但分发只传递鼠标位置和 button，没有按下、释放对应的键。真实 Edge 和 Chrome 的测试按钮收到的 `event.ctrlKey` / `event.shiftKey` 都为 false；因此 Ctrl 点击打开新标签、Shift 多选等不能按请求执行。

最小修复方向：在鼠标动作前显式按下修饰键，finally 逆序释放；将按键状态纳入统一释放流程。补充按下失败、鼠标失败、释放失败的测试。

证据：3 个模拟用例失败；两个浏览器各 2 个真实点击用例失败。

### 3. P1：用户切换标签页后仍操作旧的内部 active_page

位置：`core/computer/browser_backend.py:371`、`:386`。

只监听创建新 page 和关闭 context；新建页被无条件设为 active。手动切回已存在的第一页时，`_ensure_usable()` 只要发现内部第二页没关闭就继续返回第二页。

真实复现：创建第二页，再 `first.bring_to_front()`，后台选择的页面仍是第二页。测试使用自己创建的两个本地页面；没有接管用户已有页面。

最小修复方向：建立明确的受控标签页身份和切换机制，切换后刷新观察；GUI 显示受控页身份。不能把“最近创建”当作“当前前台”。若产品选择固定操作指定页，也必须明确显示该页并使其可见。

### 4. P1：动作执行中取消，仍可能报告成功

位置：`core/computer/controller.py:546`、动作后状态复核段。

动作前检查 cancel_event；动作后复核 run/status/generation，但没有复核 owner.cancel_event。模拟在 backend.execute 内置位取消事件，随后仍返回成功观察并发出 ok=True。

这是“仅取消事件已置位”的竞态复现；完整 service.stop 还会改变状态，不能把两种路径混为一谈。

最小修复方向：动作后发布观察、提交成功之前再次检查取消事件；取消错误沿既有终态路径传播。

证据：`test_cancel_during_action_must_not_publish_success` 失败。

### 5. P2：无进展规则误停正常动作，计数起点还有偏差

位置：`core/computer/controller.py:75`、`:615`、`:622`。

一是 move 不在豁免集合里，三个成功移动但截图不变就停止。二是画面确实改变时计数被设置为 1，随后只需两次不变就触发“连续三次无进展”，与错误说明不符。截图哈希还容易受时钟、闪烁光标等无关变化影响，所以也可能漏掉真正无进展。

最小修复方向：先修复进展后计数归零；按动作是否预期改变画面定义规则，至少不能将普通移动直接等价为失败。进一步使用明确的动作结果/目标信息，不能仅靠整屏字节判断任务进展。

证据：2 个审计用例失败；“仅两次不变点击允许继续”的对照用例通过。

### 6. P2：设置从 Edge 改为 Chrome，仍复用留存的 Edge

位置：`core/computer/controller.py:355`、`:760`。

留存 backend 只按 target='browser' 索引。上一回合正常结束留存 Edge，设置改为 Chrome 后，新的 capability 声明 Chrome，但 `_claim_backend` 直接取出 Edge，不核对 browser/profile 身份。

最小修复方向：留存身份必须包含浏览器通道及 profile；领取时严格匹配当前能力快照，不匹配则显式结束旧实例并建立所选实例。

证据：`test_browser_change_does_not_reuse_previous_channel` 失败，实际 edge、期望 chrome。

### 7. P2：两种后端的滚动单位与垂直方向没有统一

位置：`core/computer/browser_backend.py:514`、`core/computer/windows_backend.py:358`、`core/computer/win32_adapter.py:582`。

浏览器把 delta 原样作为 CSS 像素传给 mouse.wheel；Win32 把同一个值乘以 120，当作滚轮格数。native Provider 又把 scroll_x/scroll_y 直接映射到这些 delta。相同数值在两种 target 上效果明显不同；例如 500 在浏览器是 500 像素，在桌面会发出 500 格滚轮量。垂直滚轮正负语义也不同。

最小修复方向：先明确统一动作和 Provider 映射的单位/方向契约，再分别适配；桌面滚动受系统滚轮设置影响，不能宣称一个固定倍数能保证精确 CSS 像素位移。schema 也应明确单位与上限。

证据等级：代码与官方 API 契约确认；真实浏览器 delta_y=500 得到 scrollY=500。真实桌面输入未执行，所以没有宣称已测出桌面实际位移。

参考：[Playwright Mouse](https://playwright.dev/python/docs/api/class-mouse)、[Microsoft MOUSEINPUT](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-mouseinput)。

### 8. P2：缺少动作后的就绪判定，慢页导航又等待整页 load

位置：`core/computer/controller.py:546`、`core/computer/browser_backend.py:527`。

坐标输入后马上截图，未等应用状态达到预期。Playwright mouse.wheel 明确不会等滚动完成。另一方面 goto/back/forward/reload 未配置等待策略，默认等待 load：本地测试页仅增加一个延迟 1.5 秒的图片资源，导航就从约 15–26 毫秒变成约 1510 毫秒。

最小修复方向：为导航与动作结果分别定义有界的可观察就绪条件；不要统一增加固定 sleep，也不要通过重放点击补救。DOM 加载完成和可交互完成必须分别验证。

参考：[Playwright Navigations](https://playwright.dev/python/docs/navigations)、[Playwright Mouse](https://playwright.dev/python/docs/api/class-mouse)。

### 9. P2：前端有耗时字段，控制器却没有发送实测耗时

位置：`core/computer/controller.py:643`；对照 `gui_bridge.py:4434`。

前端测试用自己构造的 duration_ms，控制器生产事件没有这个字段。无法从该事件区分“模型慢、排队慢、动作慢、截图慢”，也不能将 UI 模拟耗时当作生产测量证据。

最小修复方向：用单调时钟分别记录排队、输入、截图耗时，通过现有事件接线展示；Provider 决策时间与本地执行时间分开计量。

证据：`test_action_finished_supplies_measured_duration` 失败。

## 浏览器到底怎么操作，为什么感觉慢

内置 browser target 确实启动真实可见系统 Edge/Chrome：`launch_persistent_context(headless=False)`，专用用户目录、1440×900 视口。动作经 Python Playwright 的 mouse/keyboard/navigation API 执行；不是只生成一段脚本就假称完成，也不是操作用户日常浏览器的现有 profile。

“脚本驱动”和“真实浏览器”并不矛盾。当前模型主要通过截图和坐标操作页面；生产后端没有给模型暴露 DOM 定位或可访问性元素。测试探针使用 evaluate 仅作为自建页面的结果断言，不属于生产操作方式。用户以前的某次任务是否绕到 run_command 自写脚本，本次没有历史轨迹，不能据此断言。

| 本机测量 | Edge | Chrome |
|---|---:|---:|
| 独立 profile 启动 | 1773.7 ms | 466.9 ms |
| 本地页面普通点击及结果读取 | 4.5 ms | 5.3 ms |
| 截图观察 | 268.4 ms | 265.4 ms |
| 中文/emoji 输入及结果读取 | 11.7 ms | 16.3 ms |
| 本地页导航 | 25.6 ms | 23.7 ms |
| 带 1.5 秒慢资源的导航 | 1511.3 ms | 1510.4 ms |

这是各浏览器一次本地探针结果，非稳定性能排名，不包含任何模型请求。generic 协议要求每次只一个动作、每次返回整张 PNG 后再请求模型，故多步任务必然累积模型往返和图片处理成本。该结构可以解释慢的来源，但本次未调用真实 Provider，不能给出模型耗时占比或整项任务总延迟。

## 修复优先顺序

1. 先修复超时迟到动作、取消竞态、修饰键丢失、标签页选择和错误留存复用，确保动作准确且能停止。
2. 修复无进展计数与滚动契约，并补齐真实前后台切换和动作完成观察测试。
3. 针对托盘增加明确的名称/元素定位及放大观察能力，再测试隐藏托盘展开、微信打开、不同 DPI 和多屏。
4. 补齐分段耗时后再优化模型往返与页面等待，避免只调提示词或增加 sleep。

以上为修复方案，生产修复尚未实施。

## 复现命令与产物

PowerShell 下先设置 `$env:PYTHONIOENCODING='utf-8'`，随后执行：

```powershell
# 原有电脑操控相关回归
venv\Scripts\python.exe -m pytest tests -k computer -q --tb=short --durations=20
# 原有后端全量回归（不自动收集审计文件 computer_control_cases.py）
venv\Scripts\python.exe -m pytest tests -q --tb=short --durations=15
# 前端全量
npm --prefix frontend test -- --run
# 审计复现：预期当前实现 9 failed, 1 passed，非通过验收
venv\Scripts\python.exe -m pytest tests/audits/computer_control_cases.py -q --tb=short --junitxml=docs/computer-audit-20260905-regressions.xml
# 真实浏览器集成：打开临时 profile，只访问探针自己的 loopback 页面，结束关闭
venv\Scripts\python.exe scripts/audit_computer_live.py --browser edge --output docs/computer-audit-20260905-edge.json
venv\Scripts\python.exe scripts/audit_computer_live.py --browser chrome --output docs/computer-audit-20260905-chrome.json
# 真实桌面只观察；不保留截图正文
venv\Scripts\python.exe scripts/audit_computer_live.py --desktop-observe --output docs/computer-audit-20260905-desktop.json
# 自建窗口真实输入：必须通过每一步前台标题检查，否则零输入失败
venv\Scripts\python.exe scripts/audit_computer_live.py --desktop-input --output docs/computer-audit-20260905-desktop-input.json
```

审计脚本与结果均留在仓库供复查。真实截图使用 backend 自带短期目录并在关闭时清理；JSON 不含截图、凭据或用户浏览器页面。

未完成的验收：真实桌面键鼠、微信托盘端到端、真实模型决策、GUI 宿主的混合 DPI/多屏、登录验证码与系统安全桌面。不能称为“所有电脑操控场景均已测试通过”。

实际使用技能：using-superpowers、systematic-debugging、verification-before-completion。另阅读了 computer-use/playwright 的工具说明；本次真实集成探针直接调用被审计的项目后端，没有用另一套操控工具替代被测实现。
