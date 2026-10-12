# 电脑操控可靠性修复实施文档

> 交付对象：由你或接手开发者逐项修复。本文是修复方案，不是已完成的修复记录。
> 基于 2026-09-05 工作树：跨平台接入点已经建立，原有操控缺陷尚未修复。
> 文中“拟新增”的类型、字段、文件和错误码是明确的设计提议，不能当成当前仓库已有接口调用。

**Goal:** 修复浏览器与 Windows 桌面的动作准确性、停止语义和状态观察问题，补齐微信托盘定位，并保留 macOS/Linux 后续接入边界。

**Architecture:** 保留固定 Harness、ComputerControlService、现有消息库和事件通道。优先修复现有协议内的缺陷；需要增加局部观察、软件/控件定位等动作的部分作为明确的协议扩展实施，不能暗中改变既有动作语义。

**Tech Stack:** Python、现有 Playwright/Pillow/Win32、Vue；系统扩展经 core/computer/platforms。Windows 语义定位涉及 UI Automation，新依赖需单独核对 requirements.txt 与 ycode.spec。

## 1. 当前状态：哪些已做，哪些未做

### 1.1 已完成的工作

- 已有代码审计、模拟失败复现、真实 Edge/Chrome 本地页面探针。
- 已有 `core/computer/platforms/`：Windows/macOS/Linux 独立接入位置。
- 公共设置、Controller 默认后端工厂、runtime 热键工厂、GUI preflight 已走平台层。
- macOS/Linux 仍显式不可用，没有真机实现。

### 1.2 修复基线

本次写文档前再次执行审计文件：**9 failed，1 passed**。它不是普通测试发现模式下自动执行的文件，不能只跑 `pytest tests` 就认定缺陷消失。

| 检查 | 最近已有证据 | 适用范围 |
|---|---|---|
| 电脑操控相关回归 | 697 passed | 跨平台接口预留完成后的结果 |
| 平台/设置/GUI 定向 | 96 passed | 与上项部分重叠，不累加 |
| prompt byte-stability / Provider golden | 19 passed | 平台预留未改变请求字节 |
| 审计文件 | 9 failed、1 passed | 本文编写时重新确认，原缺陷仍存在 |
| 真实 Edge、Chrome | 各 15 成功、3 失败 | Ctrl 点击、Shift 点击、旧标签页焦点跟踪失败 |
| 桌面截图 | 3840×2160 → 1920×1080 | 本机一次真实观察 |
| Windows 真实键鼠 | 尚未完成 | 自建测试窗口未通过前台校验，第一次输入前终止 |

上次后端全量为 2620 passed、1 failed、2 skipped；唯一失败是图片插件启用状态断言。该数字是历史全量结果，不能替代修复后的全量回归。历史 LAN 线程关闭警告也需在最终报告中单列。

资料位置：

- 审计报告：`docs/computer-control-audit-20260905.md`。
- 失败复现：`tests/audits/computer_control_cases.py`。
- 真实探针：`scripts/audit_computer_live.py`。
- 原始结果：`docs/computer-audit-20260905-{edge,chrome,desktop,desktop-input}.json` 和 `docs/computer-audit-20260905-regressions.xml`。
- 平台接入：`core/computer/platforms/README.md`。

### 1.3 不得写成既定结论的事项

- 微信失败的唯一根因尚未确认；托盘能力缺失、图片缩小、误停止是已知影响因素。
- Playwright 本地点击快，不代表整个模型任务快；没有真实 Provider 分段耗时。
- UI Automation 不保证能识别所有版本微信或所有托盘控件。
- 后台命令设置了取消标记，不代表已发给浏览器/系统的动作可以撤回。
- 本文代码片段是实施核心逻辑或测试示例，只有接入生产调用链并通过测试才算修复。

## 2. 全局约束

1. 在 dev 工作；合并其他分支由用户明确要求。保留当前工作树中的平台预留和审计产物。
2. 一项缺陷先复现，再最小修复，再运行相关测试。不要把这些任务合成一个难以审查的大提交。
3. 不吞执行错误，不自动重放点击/输入，不替换浏览器，不在未知状态下猜坐标，不用固定 sleep 掩盖条件缺失。
4. 默认关闭电脑操控时，Provider 请求、system prompt、普通 tools 顺序和字节保持不变。
5. 修改动作 schema、Provider 解析或提示词前，先运行 byte-golden 与 prompt-stability。现有黄金数据不得为了通过而覆盖；新增能力需要独立、明确版本的样例和变更记录。
6. 原生 computer_call 的动作必须按原序执行，任一失败后余项零执行；pending safety 保持零执行，不回退 generic。
7. Journal 只存状态、ID、序号、时间、耗时、哈希；不存截图、文本、URL 凭据、原生 response id 或工具正文。
8. Playwright 对象只能在其所属执行线程/进程内调用；GUI evaluate_js 仍只能走原有 dispatcher。
9. pytest 只用模拟输入、模拟截图或自建假服务；真实键鼠/浏览器验收与自动测试分开。
10. Windows 专用软件定位、DPI、热键实现不能直接导入公共 Controller/Provider；通过平台接口接入。

## 3. 建议顺序与修改文件

| 任务 | 优先级 | 修改重点 | 前置 |
|---|---|---|---|
| R0 固化基线和可自动发现的复现 | 必做 | tests/audits、正式测试文件 | 无 |
| R1 修复取消后的成功提交 | P1 | controller、Provider/LLM 接缝 | R0 |
| R2 排队超时、执行中断与停止 | P1 | browser_backend、runtime、controller | R1 |
| R3 浏览器修饰键生命周期 | P1 | browser_backend、computer_fakes | R2 |
| R4 留存实例身份与清理 | P1 | controller、backend 身份 | R2/R3 |
| R5 浏览器受控页与坐标时效 | P1 | browser_backend、controller、GUI | R1/R2 |
| R6 无进展计数 | P2 | controller | R5 |
| R7 滚动单位/方向契约 | P2 | actions、两个 backend、provider_openai | R0 |
| R8 就绪条件与可取消等待 | P2 | 两个 backend、controller | R2/R5 |
| R9 分段耗时与性能验收 | P2 | controller、backend、GUI、前端 | 可早做，验收在 R8 后 |
| R10 软件/托盘语义定位与局部观察 | 能力补齐 | platforms、actions、models、固定适配器 | R1–R8 |
| R11 真机、协议和跨平台边界验收 | 必做 | tests、scripts、文档 | 全部 |

以下用函数名定位，行号会随修复移动。不要机械照抄旧审计报告中的行号。

## 4. R0：基线和复现整理

**文件：**现有 `tests/audits/computer_control_cases.py`；对应正式文件 `tests/test_computer_controller.py`、`tests/test_computer_browser_backend.py`。

- [ ] 保存 `git status --short` 和 `git diff --stat`，区分既有平台预留与本轮修复。
- [ ] 跑本节命令，保存初始结果，确认 9 个失败的具体名称与原因。
- [ ] 每修一项，将对应复现迁入自动发现的 `test_*.py`，或在全部缺陷处理后一次重命名审计文件。不得让修复用例最终仍只靠人工记得运行。
- [ ] 对并发测试使用 Event/Barrier 确定执行阶段；不要仅靠 50 ms 睡眠猜测已进入 worker。

```powershell
$env:PYTHONIOENCODING='utf-8'
venv\Scripts\python.exe -m pytest tests/audits/computer_control_cases.py -q --tb=short
venv\Scripts\python.exe -m pytest tests -k computer -q --tb=short
venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py -q
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q
```

**原始复现限制：**超时用例当前在超时后向同一实例发送 drain barrier。R2 若规定超时后实例永久失效，应把 drain 改为测试夹具直接等待 worker 的已知完成事件，并额外断言后续输入被拒绝；保留“超时队列输入副作用为零”这一核心要求。不能为了照顾旧测试而继续允许污染实例执行。

## 5. R1：取消后不得发布新成功结果

**现状：**`ComputerControlService.execute` 只在动作前检查 cancel_event；动作后检查 run/status/generation，却遗漏 owner.cancel_event，且预览发布发生在这次复核之前。

**文件与接缝：**

- 修改 `core/computer/controller.py`：execute、stop、complete_run。
- 核对 `core/computer/provider_generic.py:execute_reserved_computer_tool`、`core/computer/provider_openai.py:execute_native_computer_call`。
- 核对 `core/llm.py` 中 `_finalize_computer_run` 与取消结果补齐路径；确有接线缺口才修改。

**实施：**

- [ ] 提取状态锁内的提交检查：活动 run/owner、cancel、状态、generation 一并检查。
- [ ] backend.execute 返回后、开始可能耗时的 observe 前检查取消。
- [ ] observe 返回后、publish 预览前检查；最后提交状态和分配成功事件序号时再检查。
- [ ] 将成功事件的“提交点”定义为最后一次状态锁内验证与序号分配。取消先发生则不提交；提交先完成、随后取消则保留此前已成功动作，不能要求撤销历史事实。
- [ ] 外部 listener 和预览服务不能在 Controller 状态锁内调用；迟到事件由 run_id/event_seq 和终态规则拒绝覆盖新状态。
- [ ] 已 action_started 的动作最终恰好产生一条 finished，取消时 ok=False；不同时发成功和失败。
- [ ] cancel_event 置位的 generic 结果可以按现有稳定错误结构回填工具结果，但不能附带新截图或再发模型续推请求。

核心条件示意，放入既有临界区，不是另建第二个取消系统：

```python
if run.owner.cancel_event.is_set():
    raise ComputerControlError("computer_cancelled", "操控回合已取消")
if self._run is not run or run.status != "running":
    raise ComputerControlError("computer_cancelled", "操控回合已停止或失去执行权")
```

**必须补测：**execute 中取消、observe 中取消、预览发布期间取消、generation 撤销、暂停中完成当前输入、native 批次第二项前取消。暂停与取消分别断言，不得把暂停强行转换成永久取消。

现有复现：`test_cancel_during_action_must_not_publish_success`。命令：

```powershell
venv\Scripts\python.exe -m pytest tests/audits/computer_control_cases.py -k cancel -q
venv\Scripts\python.exe -m pytest tests/test_computer_controller.py tests/test_computer_provider_generic.py tests/test_computer_provider_openai.py tests/test_computer_llm_generic.py -q
```

**完成标准：**取消在成功提交点之前可被确定观察到时，成功事件、新观察 follow-up、后续批次输入和模型续推均为零；已发出的真实输入不声称回滚。

## 6. R2：命令超时与紧急停止

### 6.1 先修排队命令生命周期

**根因：**`_Command` 没有状态/截止时间；`_call` 等待超时后抛错，`_command_loop` 仍执行队列中的 fn。`_submit_lock` 等待也不计入同一个总预算。

**修改：**`core/computer/browser_backend.py` 的 _Command、_call、_command_loop、close、release_inputs；`tests/test_computer_browser_backend.py`。

**拟新增内部契约：**

| 字段/状态 | 定义 |
|---|---|
| command_id | backend 内单调递增 ID，不含工具正文 |
| deadline | monotonic 绝对截止时间；锁等待、入队、执行等待共用 |
| queued | 尚未取得执行权，可取消，零副作用 |
| running | worker 原子领取后进入，不能假称尚未执行 |
| succeeded / failed | 正常终态 |
| cancelled | 排队阶段已撤销；worker 只能跳过 |

领取和撤销使用同一个命令锁。仅在线程看到 `done=False` 后设置 cancelled 不够，必须避免“检查未运行→worker 开始→调用方仍认作未执行”的竞态。

核心领取代码可按此实现；状态与 deadline 由 _Command 构造完成：

```python
def try_start(self, now: float) -> bool:
    with self.lock:
        if self.state != "queued":
            return False
        if now >= self.deadline:
            self.state = "cancelled"
            self.done.set()
            return False
        self.state = "running"
        return True
```

- [ ] _call 入口计算 deadline，所有阻塞操作只使用剩余预算。
- [ ] worker 在 fn 调用前 try_start；不成功则不调用 fn。
- [ ] running 超时不能改成“未执行”，记录结果未知并禁止后续输入。
- [ ] close 先关闭 admission，再处理队列；不能把结束哨兵放在待执行输入之后并任其照常执行。
- [ ] 排队命令全部得到终态通知，调用线程不能在 backend 关闭后继续悬挂。
- [ ] 有未结束 worker 的实例不得进入 retained，也不得释放 profile 执行权给新实例。

**测试：**现有 `test_timed_out_queued_command_never_executes_later`，再补锁等待超时、queued/running 领取竞态、队列满时 stop、close 与 submit 并发、超时后重入被拒绝、profile 不提前释放。

### 6.2 执行中的命令：不能只靠 threading.Event

同步 Playwright 调用卡住时，Event 无法打断那个 Python 调用；release_inputs 和 close 若排在同一串行队列，也会被阻塞。禁止从另一个线程直接调用 page/context.close。Playwright 官方明确 API 非线程安全；当前官方文档也不支持直接取消正在执行 Playwright 调用的 asyncio task，不能机械换成 async 后 task.cancel 就宣布解决。[官方线程与取消说明](https://playwright.dev/python/docs/library#threading)

**本项目建议分两次交付：**

1. 第一版完成 6.1，并把超时实例封闭，准确报告“执行结果未知”；这能修迟到排队输入，但不算硬停止验收完成。
2. 若要满足浏览器卡死时仍能在有限时间内收回执行权，将浏览器执行器隔离为独立受管进程；公共 ComputerBackend 接口保持不变。此变更只涉及浏览器执行边界，不重构整个 Harness。

**独立进程版拟新增文件：**`core/computer/browser_worker.py`、`core/computer/browser_worker_protocol.py`、`tests/test_computer_browser_worker.py`。这些文件当前不存在。

**IPC 设计：**

- 父进程只发送结构化 command_id/run_id/operation/action；操作白名单为 start/observe/execute/release/close，不传 Callable、eval 或 shell 字符串。
- 单个子进程独占一个 Playwright 实例、context 和页面对象。父进程不持有 Playwright 对象。
- 子进程正常返回 command_id、明确状态及必要的观察字段；输入正文仅在本机通信内存，禁止额外记入日志。
- 父进程超时先停止接收输入，给有界清理机会；无法关闭则终止本次专用执行进程和能确认归属的浏览器进程树，等待退出后才释放 profile/租约。
- Windows 的受管进程组、未来 macOS/Linux 的进程组分别实现；不得按进程名结束所有 chrome/msedge，也不能结束用户日常浏览器。
- 只终止 Python 子进程不足以证明浏览器已退出，必须验证受控 driver/browser 子进程归属和退出结果。
- 终止失败应保留故障状态及执行权隔离，不能“清空引用后当作停止成功”。

**建议验收指标（目标，不是现有成绩）：**停止请求后 100 ms 内关闭新命令入口，专用执行环境 2 秒内退出；若目标机器不满足，记录实测并调整设计，不能改测试伪装达标。

**硬停止测试：**永不返回的假驱动、worker 启动中挂死、close 卡住、driver 留存子进程、父进程异常退出、profile 下一次显式重开。先用无键鼠副作用的受管子进程测试，不用真实点击验证“杀得够不够快”。

## 7. R3：正确执行和释放 Ctrl/Shift 等修饰键

**根因：**schema 接受 keys，但 browser 分发丢弃了它们。Windows adapter 已有 modifiers 路径，可作为行为参照，不能直接复制本机 VK 码到浏览器。

**修改：**`browser_backend.py:_dispatch_action/_exec_drag/_cmd_release_inputs`；`tests/computer_fakes.py:FakeKeyboard`；浏览器后端测试。

- [ ] 将 keys 通过既有 `_map_playwright_key` 映射，按输入顺序 keyboard.down。
- [ ] 只有 down 成功的键进入按下记录；执行 click/double_click/drag。
- [ ] finally 逆序 keyboard.up；失败保留记录供清理，执行失败仍向上传播。
- [ ] 按键记录绑定到按下时的 page，不能在 popup 导致 active_page 改变后去另一个页面释放。
- [ ] 停止和正常 complete 都释放按下状态，包括准备留存的 browser。当前 `complete_run` 在 retain 时跳过 release_inputs，需要一起修。
- [ ] 多键组合保持原序，不静默丢弃未知键；键集合合法性由严格解析统一检查。

典型动作顺序：

```text
keyboard.down(Control)
keyboard.down(Shift)
mouse.click(x, y)
keyboard.up(Shift)
keyboard.up(Control)
```

**测试：**现有 `test_browser_mouse_action_preserves_ctrl` 三个参数；新增 Ctrl+Shift 点击、第二个 down 失败、点击失败、up 失败、拖拽中取消、popup 后释放回原页、正常 retain 前释放。

```powershell
venv\Scripts\python.exe -m pytest tests/audits/computer_control_cases.py -k preserves_ctrl -q
venv\Scripts\python.exe -m pytest tests/test_computer_browser_backend.py tests/test_computer_controller.py -q
```

**真机验收：**自建页面直接读取 event.ctrlKey/event.shiftKey，并验证真实 Ctrl 点击链接行为。API 调用日志只能证明发过参数，不能替代页面收到修饰键的断言。

## 8. R4：留存浏览器必须匹配身份

**根因：**`_retained` 仅按 browser target 索引，改配置后照用旧实例。还有一处耦合：默认 factory 在创建时重读全局设置，可能与 Turn 冻结能力中的 browser 不一致。

**修改：**`controller.py:_claim_backend/start_run/execute/complete_run/stop/_default_backend_factory`；必要时新增 `BackendIdentity` 内部值对象。

**拟定内部身份：**

```python
@dataclass(frozen=True)
class BackendIdentity:
    platform: str
    target: str
    browser: str
    profile_path: str
```

desktop 的 browser/profile_path 使用空串。身份只在本地执行层存在；profile 路径不加入 Provider 或 Journal。

- [ ] 为 run 构造不可变 backend 请求身份，browser 来自已冻结 capability；profile 在本次领取时解析为规范绝对路径。
- [ ] 工厂消费该身份，不再次独立读取“当前选中浏览器”。更新工厂注入测试，不用 `getattr` 猜旧签名实现兼容兜底。
- [ ] 留存对象必须 identity 完全相同、healthy、输入已释放、未关闭、无 pending/running 命令才可领取。
- [ ] 身份不匹配，显式关闭旧实例，确认清理后创建新配置指定实例。这是配置变更生命周期，不是失败时换浏览器。
- [ ] 清理失败则本次启动失败，不能静默丢弃旧对象后启动新实例。
- [ ] 同身份但用户已关闭页面/context 时明确失败；若产品需要重新打开，应是新一轮明确启动行为，不能暗中自动复活。

**测试：**现有 `test_browser_change_does_not_reuse_previous_channel`；再测 profile 更换、配置在冻结后改变、旧 close 失败、closed context、仍有按键/命令、相同身份正常复用、退出关闭全部留存。

## 9. R5：受控标签页和旧坐标失效

### 9.1 选择规则

当前 `_on_new_page` 把最新 page 当 active，`_ensure_usable` 只检查它没关闭。推荐采用“跟随用户当前可辨认的可见受控页，切换后强制观察”的规则，并保持旧裁决：当前页被关闭但仍有其他可见页时，选最后可见页；全部关闭则停止。

**禁止做法：**假定 Playwright 有 `page.on('focus')` 这样的 API；当前代码没有，不能凭空新增事件名就当实现了跟踪。`context.pages[-1]` 也不代表前台。

**实施：**

- [ ] 为每个 page 分配本地稳定身份，独立于 title/url；同标题同域的两个页也不同。
- [ ] 在 worker 内，通过经过验证的浏览器可见性/焦点信息确定候选页。若使用 `document.visibilityState`/`document.hasFocus()`，只读取这两个状态，不读取表单或凭据。
- [ ] 不能把“visible”直接等同于系统前台：多窗口、地址栏聚焦、浏览器失焦、devtools 都要测试。无法唯一确定时明确暂停/报错，不能选择任意页继续。
- [ ] observe 确定页面并记录身份；截图后再验证仍是同一页，期间切换则观察作废。
- [ ] 点击前重新核对当前页身份与上次观察是否一致；变化则零输入、要求重新 observe。
- [ ] 新 popup 只登记，依据可观测焦点决定是否采用；每次切换都失效旧坐标。
- [ ] 页面关闭后的选择行为保留既有约定，但选中后也必须重新观察。

**拟新增错误码：**`computer_surface_changed`、`computer_surface_ambiguous`。加入现有领域错误通道，不创建第二套工具返回格式。

### 9.2 Controller 与 UI 接线

拟在 ComputerObservation 增加仅本地使用的 `surface_id`、`surface_generation`；页切换、窗口变更、DPI/桌面几何变化触发 generation 变化。随机 ID 不进入相同截图要求稳定的 Provider 投影。GUI 可使用受控页标题摘要及本地 ID 区分页面，继续按 run_id/event_seq 消费。

`run.require_observe` 必须在该错误出现后生效。不要只返回错误却保留可执行的旧坐标。native 批次中切页同样中断余项，不能拿前一页坐标点后一页。

桌面应用也需要检查“观察时的窗口身份”与“输入前的前台窗口身份”；当前只检查存在任意前台窗口，不能防止用户切到别的软件后误输入。窗口身份不能只用标题字符串，需由平台层提供稳定的本地标识及几何代数。

**修改：**browser_backend、windows_backend、models、controller；GUI 及 `frontend/src/stores/appStore.ts` 的电脑事件类型与展示。仅本地字段不得泄露进 Provider 消息和媒体持久化。

**测试：**两个同标题页互切、失焦、地址栏焦点、多窗口都 visible、popup、页关闭、用户切到其他软件、分辨率变化、native 中途切页。真实探针 `manual_tab_focus_tracking` 必须变绿。

## 10. R6：无进展计数

**修改：**`controller.py:_NO_PROGRESS_EXEMPT_KINDS` 和 execute 中 key/count 更新段。

明确规则：

1. 画面/有效目标状态改变，计数归零，不设为 1。
2. move、observe、wait、request_user_takeover 不增加失败计数。
3. 豁免动作观察到真实变化，也应更新基线和归零；否则后续动作会拿过期基线比较。
4. 豁免动作且画面不变，不清掉既有失败次数，避免靠插入 observe 掩盖重复失败。
5. 该机制只作“重复不变”的限制，不宣称等价于任务语义成功；光标闪烁/时钟变化造成的误判单列，不用任意图像相似度阈值伪装解决。

拟提取的纯函数完整核心：

```python
def next_progress_state(previous_key, current_key, count, kind):
    exempt = {"move", "observe", "wait", "request_user_takeover"}
    if current_key != previous_key:
        return current_key, 0
    if kind in exempt:
        return current_key, count
    return current_key, count + 1
```

新语义定位动作加入后，依据该动作是否预期改变状态确定是否计数，不能一概按截图是否变化判断。

**测试：**现有两个失败用例和两击允许继续的对照；新增进展后 0→1→2→3、observe 发现变化归零、move 不变不增加、clock 类画面变化只更新像素基线不声称业务完成。

```powershell
venv\Scripts\python.exe -m pytest tests/audits/computer_control_cases.py -k 'moves or progress or unchanged' -q
venv\Scripts\python.exe -m pytest tests/test_computer_controller.py -q
```

## 11. R7：滚动单位与方向

**事实：**browser mouse.wheel 的参数为 CSS 像素；Windows 当前把 delta 乘以 120 作为滚轮格数。Win32 垂直正值是滚轮向远离用户方向转动，不能原样当浏览器向下的正像素值。[Playwright Mouse](https://playwright.dev/python/docs/api/class-mouse#mouse-wheel)、[Microsoft MOUSEINPUT](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-mouseinput)

**禁止修法：**所有 target 统一乘/除 120、直接夹到某个最大值、把 500 像素默认为 500 格、对所有应用声称固定比例能得到精确像素位移。

### 11.1 推荐显式契约

不要强行宣称 Win32 滚轮与 DOM 像素完全同单位。建议新协议的 scroll 增加明确 unit：

| target | unit | 正方向 | 建议输入范围 |
|---|---|---|---|
| browser | css_pixel | 右/下为正 | 每轴 -4000 到 4000 |
| desktop Windows | wheel_step | 右/下为正 | 每轴 -10 到 10 |

范围是拟定产品限制，不是 OS 限制；超限拒绝，不裁剪。每轴都为 0 应拒绝或明确定义为无输入查询，建议拒绝。

新增协议中的 wheel_step 转 Windows event：

```python
vertical_wheel_data = -delta_y * 120
horizontal_wheel_data = delta_x * 120
```

只适用于已验证 unit='wheel_step' 的动作，不能插入当前未区分单位的通用路径后宣布所有请求兼容。

### 11.2 native Provider 不能偷换语义

`provider_openai.py:_map_official_action` 当前直接把 scroll_x/scroll_y 复制成 delta。实施前重新核对当时官方协议及本机依赖版本，不能依据旧注释猜单位。

若原生请求定义为像素而 Windows 实现只能发滚轮格数：在缺少可验证的像素滚动实现时，明确返回“该 target 不支持此单位”，不得静默近似。该能力在验收表中保留未完成；先拒绝危险误滚动是故障隔离，不等于已支持原生桌面滚动。

**需要产品确认的明确边界：**是否接受“桌面仅支持显式滚轮步进，原生像素请求暂不可用”。这不是可以靠一个通用换算公式完成的实现细节。本文提供推荐契约，实际修改 Provider schema 前需记录该决定。

**修改文件：**actions.py、models.py、windows_backend.py、win32_adapter.py、browser_backend.py、provider_openai.py；对应解析、Provider、两个后端测试。协议扩展不得覆盖既有 golden。

**测试：**browser +500 实际向下；Windows +1 生成 -120 垂直事件、水平 +1 为 +120；负方向、零、上限、bool、非法 unit、native 不支持单位零执行；真机验证多个应用及系统滚轮设置，测实际效果而非只看 SendInput 返回值。

## 12. R8：动作就绪与等待

**现状：**Controller 在输入返回后马上截图；browser wait 分支使用 time.sleep；导航默认等 load，慢子资源拖住整步。

**实施分两类，不能用统一 sleep：**

### 12.1 导航

- [ ] 将策略显式写到 goto/go_back/go_forward/reload 调用，建议以 domcontentloaded 作为文档基线，使用命令剩余预算作为 timeout。
- [ ] DOMContentLoaded 只意味着文档基线，不意味着 SPA 已加载完成。业务目标未可见时要返回新观察并明确状态，不能声称业务成功。
- [ ] 不使用 networkidle 作为所有页面的统一完成条件。
- [ ] 导航中 URL/page 身份变化按 R5 刷新基线；失败不复用旧截图。

拟定调用形式：

```python
page.goto(url, wait_until="domcontentloaded", timeout=remaining_ms)
```

remaining_ms 取 R2 的同一 deadline，不创建独立 30 秒重试预算。

### 12.2 鼠标/键盘及显式 wait

- [ ] 对可确定的目标，等待对应状态变化：焦点到目标、窗口已显示、页面滚动完成、指定可访问性属性达到预期。
- [ ] 对纯截图动作，没有可验证业务条件时只承诺“输入已派发并取得新观察”，不要发明 universal 页面稳定算法。
- [ ] explicit wait 可继续表达用户/模型要求等待的时长，但等待过程中必须服务取消与页面事件；不自动重复输入。
- [ ] 当前同步 Playwright 架构避免用 time.sleep 阻塞其消息处理；R2 进程方案中停止由父进程监督。不能把 wait_for_timeout 当业务就绪验证。[Playwright 等待说明](https://playwright.dev/python/docs/library#time-sleep-leads-to-outdated-state)
- [ ] Windows wait 不能持有 backend 大锁睡满 10 秒而让 close 无法收尾；引入 backend 停止事件、用 Event.wait 实现可中断等待，锁只用于状态更新。

**测试：**慢图片但文档已就绪、本地 SPA 延迟渲染、滚动动画、页面中途关闭、wait 期间停止、窗口打开动画、截图失败。真实探针需同时断言“更快返回”和“业务目标是否已就绪”，不能只改导航超时让测试变快。

## 13. R9：耗时与性能定位

**根因：**controller 的 action_finished 没有 duration_ms，前端测试自己构造该字段，掩盖了生产接线缺失。

**修改：**controller.py 的 action_started/finished；必要的 backend 计时结果；gui_bridge.py 的电脑事件转发；`frontend/src/types/pywebview.d.ts`、`frontend/src/stores/appStore.ts`、ComputerControlPane/Bar 及测试。

拟定计时字段全部为本地事件数据，不加入 Provider 观察投影：

| 字段 | 起止 |
|---|---|
| duration_ms | Controller 接到该次动作请求到结果提交/失败 |
| queue_ms | 等待动作锁、命令锁与执行队列的时间 |
| input_ms | 实际输入/导航执行阶段 |
| wait_ms | 明确的条件等待阶段 |
| observe_ms | 抓图、编码和构造观察 |

平台启动可单独记录 backend_start_ms。Provider 思考时间另用现有 Step 的请求起止记录；不要混进 input_ms。所有计时基于 monotonic/perf_counter，不能靠墙上时钟相减。

```python
started = time.perf_counter()
# 成功与错误出口都计算同一个动作的耗时。
duration_ms = round((time.perf_counter() - started) * 1000, 3)
```

- [ ] 成功、失败、取消均有耗时和唯一 finished。
- [ ] 不存在数据时显示“未测量”，不能默认 0 ms 暗示瞬间完成。
- [ ] UI 从真实 Controller 事件走 Bridge/reducer 验证，不只给前端塞理想化假事件。
- [ ] 本地基准至少分别测 20 次冷启动/热操作，报告样本数、p50、p95及机器环境；首次启动和后续动作分开。
- [ ] 模型任务记录请求次数、每次耗时、截图大小和模型/协议标识；不记录凭据和输入正文。

历史真实点击约 2–5 ms、截图约 265–268 ms，只是一次探针，不能直接当 p95 门槛。首次优化应消除错误等待与无谓观察；generic 当前逐动作模型往返是明确契约，批量动作优化需要另行设计，不顺手解除安全检查。

## 14. R10：微信托盘、软件定位与局部观察

这部分是能力建设，不只是给现有函数加两个 if。完成 R1–R8 后再接入，避免更强定位能力仍受错误点击和停止问题影响。

### 14.1 第一件事：采集本机可观测结构

- [ ] 在普通任务栏、隐藏图标展开区、微信显示/隐藏/最小化状态下，采集控件类型、名称、可用 pattern、屏幕边界与所属进程等最小结构。
- [ ] 不扫描或保存聊天正文、密码控件值；报告只需说明目标是否可识别及理由。
- [ ] 确认当前 Windows/微信版本是否实际提供所需 UIA 元素。若没有，明确标为不支持的语义定位路径，不能宣称 UIA 一定能解决。
- [ ] 记录英文/中文系统、缩放比例、显示器布置、任务栏自动隐藏状态。

UIA 按控件实际提供的 pattern 执行，例如可调用控件使用 Invoke pattern；不能根据一个元素名就假定它能 Invoke。[Microsoft Control Patterns](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-controlpatternsoverview)

### 14.2 建议的软件定位边界

拟新增 `core/computer/surfaces.py`，定义平台无关的发现结果与定位接口；Windows 实现放在 `core/computer/platforms/windows_surfaces.py`，由 WindowsPlatformAdapter 提供。两个新文件当前不存在。

拟定接口供设计评审，不是现有调用：

```python
@dataclass(frozen=True)
class SurfaceCandidate:
    ref: str
    kind: str                  # window / tray_icon / control
    label: str                 # 受长度限制的名称摘要
    surface_generation: int
    supported_operations: tuple[str, ...]

class SurfaceLocator(Protocol):
    def list_windows(self) -> tuple[SurfaceCandidate, ...]: ...
    def list_tray_icons(self) -> tuple[SurfaceCandidate, ...]: ...
    def activate_window(self, ref: str, generation: int) -> None: ...
    def invoke_element(self, ref: str, generation: int) -> None: ...
```

ref 只引用当前会话已观察到的对象；必须关联 run/代数、操作权限和本地真实元素，不能接受模型伪造 HWND、进程 ID 或任意脚本。应用名称有多个候选时返回候选，不自动点第一个。

**Windows 实现约束：**

- COM/UIA 在专属线程初始化、使用和释放；不能把控件对象跨线程传入模型 worker。
- Win32 ctypes 的 HWND/HDESK 参数及返回值按指针宽度声明，不能只声明 GetForegroundWindow 的 restype 而漏掉后续接收句柄的 API。
- 激活窗口后验证实际前台身份；SetForegroundWindow 受系统限制，返回/验证失败必须报告，不能通过后台盲输假装已激活。[Microsoft SetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow)
- UIA 操作失败不自动改成同位置坐标点击。语义操作和图像坐标操作是显式的两种动作，不构建失败后偷偷切换的兜底链。

### 14.3 微信打开流程

1. 观察当前桌面并取得窗口/托盘候选。
2. 已有可唯一识别的微信窗口：显式 activate，验证前台身份及新截图。
3. 需要从托盘打开：观察普通托盘；微信处于隐藏区则显式操作已识别的展开按钮，等待展开区出现，再重新观察候选。
4. 操作确切的微信候选，只执行其实际支持的调用/点击方式；单双击必须在该版本真机验证，不写死所有托盘都单击。
5. 验证微信窗口出现并可见，之后输出“已打开”。只成功调用 Invoke 或 SendInput 不足以得出此结论。
6. 名称缺失、多个候选、窗口不出现或失去前台：明确报告哪一步未完成，不重复盲点。

测试目标到“打开窗口”为止，不发送聊天消息、不修改账号设置。

### 14.4 局部观察与坐标

不建议直接把整屏截图无限加大。拟新增明确的区域观察，保留概览图，并支持针对任务栏/目标窗口取得原分辨率区域图。

**拟定区域模型：**

```python
@dataclass(frozen=True)
class ObservationRegion:
    origin_x: int
    origin_y: int
    original_width: int
    original_height: int
    model_width: int
    model_height: int
```

区域原点是虚拟桌面的物理坐标；裁剪图坐标反算：

```python
physical_x = region.origin_x + round(x * region.original_width / region.model_width)
physical_y = region.origin_y + round(y * region.original_height / region.model_height)
```

与当前 screenshot_to_physical 的公式结构一致，但 original_width/height 必须是区域尺寸，不能误用整个虚拟桌面的尺寸。验证裁剪区域在虚拟桌面内、半开区间、负原点、DPI/分辨率变化和代数失效；不要 clamp 越界坐标。

区域变更后动作必须引用该张最新观察；窗口移动或显示器几何变化使旧区域失效。随机区域身份/代数只在本地元数据中，不能漂移稳定的 Provider 观察字节。

### 14.5 接入固定 Harness

拟新增的发现/激活/区域观察动作必须在 actions/models 严格声明，经原有固定 computer 适配器和 Controller 检查 owner、target、cancel、generation、暂停状态，再到平台执行。

不要创建第二个绕过 Controller 的 `open_wechat()` 通道。generic 工具新增语义需要独立版本样例；native 官方协议没有这些动作时，不伪造原生动作名、不自动降级。新增动作不可用时应在能力范围中清楚表达。

### 14.6 其他系统

macOS 后续在独立模块接入软件/窗口/菜单栏定位、授权、Retina；Linux 在其模块中明确区分 X11/Wayland 和无图形会话。当前 supported=False 保持不变，不能因为公共接口存在就开启开关。

## 15. R11：测试矩阵与最终验收

### 15.1 自动测试

| 类别 | 必须覆盖 |
|---|---|
| 动作生命周期 | queued/running 超时、取消提交点、stop 幂等、close 失败、并发 submit |
| 输入 | 普通/双/右键、Ctrl/Shift、多键按下/释放失败、中文/emoji、拖拽取消 |
| 页面与窗口 | 相同标题、切页、popup、多窗口失焦、页面关闭、桌面前台变化 |
| 留存 | 通道/profile 匹配、配置冻结、关闭/不健康实例、输入残留、退出清理 |
| 滚动 | unit/方向/上下限、native 单位不支持零执行、不自动裁剪 |
| 观察 | 截图失败、区域坐标、4K/多屏负原点、显示配置改变 |
| 控制策略 | observe 恢复要求、无进展计数、明确等待取消、条件超时 |
| 事件/隐私 | 唯一 finished、真实耗时、晚到事件、正文不进 Journal/日志 |
| 模型接缝 | generic 取消不续推、native 中途失败余项零执行、安全检查零执行 |
| 平台 | Mac/Linux 拒绝真实执行且不加载 Windows 依赖 |

### 15.2 真实浏览器

用新文件保存修复后结果，保留旧 JSON 作为 before 证据：

```powershell
venv\Scripts\python.exe scripts/audit_computer_live.py --browser edge --output docs/computer-repair-after-edge.json
venv\Scripts\python.exe scripts/audit_computer_live.py --browser chrome --output docs/computer-repair-after-chrome.json
```

现有两个浏览器各 18 检查必须重新执行；扩展失焦、慢页、popup、受控进程停止测试。若按新的契约调整探针，保留实际要求的行为断言，不删掉原来的失败项。

### 15.3 真实桌面与微信

先解决测试窗口前台问题：记录本探针窗口身份和当前前台身份的关系、SetForegroundWindow 结果、实际截图；必要时人工把测试窗口置前。不能删掉前台校验以让输入测试执行。

```powershell
venv\Scripts\python.exe scripts/audit_computer_live.py --desktop-observe --output docs/computer-repair-after-desktop.json
venv\Scripts\python.exe scripts/audit_computer_live.py --desktop-input --output docs/computer-repair-after-desktop-input.json
```

注意现有探针的 `positive_scroll_matches_browser_down_direction` 是旧单位语义审计：R7 明确 unit 后需升级该探针，以实际方向和声明单位断言，不继续把两个 target 的数值直接等同。

微信矩阵至少：图标直接可见/隐藏区、窗口显示/最小化/隐藏、任务栏自动隐藏、中文/英文系统、100%/150%/200% 缩放、单屏/双屏负原点、任务进行中手动切窗、停止与恢复。

建议每个受支持核心场景至少连续 10 次，记录成功次数、步骤、耗时和失败阶段。**零误点、零错误窗口输入、零停止后新派发输入**是验收要求；只有截图看起来相似不算完成。

截图仅短期保存；共享验收记录中不包含聊天内容。

### 15.4 完整回归命令

```powershell
$env:PYTHONIOENCODING='utf-8'
venv\Scripts\python.exe -m pytest tests/audits/computer_control_cases.py -q --tb=short
venv\Scripts\python.exe -m pytest tests -k computer -q --tb=short
venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py tests/test_llm_responses.py tests/test_harness_runtime.py -q --tb=short
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_cache_keepalive.py tests/test_token_accounting.py -q
venv\Scripts\python.exe -m pytest tests -q --tb=short
npm --prefix frontend test -- --run
npm --prefix frontend run build
git diff --check
```

审计文件迁入正式文件后，相应更新第一条命令；新增进程/语义定位测试按正式 test_*.py 命名纳入全量。构建若产生跟踪产物，按项目既有规则处理，不将无关输出混入修复。

## 16. 每项交付记录模板

复制下面表格，一项修复一份；空白状态表示尚未验收，不填猜测数据。

| 项目 | 填写内容 |
|---|---|
| 修复编号 | R1–R11 中的编号 |
| 具体问题 | 用户触发条件、修复前结果 |
| 根因证据 | 函数名、失败测试、必要的脱敏运行证据 |
| 修改清单 | 实际文件及接口变化 |
| RED | 命令和真实失败原因 |
| GREEN | 命令、通过数、失败数、耗时 |
| 真实验收 | 系统/浏览器/应用版本、重复次数、结果 |
| 协议影响 | 是否改变 schema/字节；旧模式如何保持 |
| 遗留限制 | 尚未实现或尚未验证的行为 |

最终只有同时满足以下条件才能写“操控可靠性修复完成”：已确认缺陷的正式回归通过，相关真机验收通过，未修项逐条明确，微信打开行为得到实际确认，默认关闭的请求字节不变。不能用“697 项原有回归通过”替代这份验收。

## 17. 可直接交给接手开发者的说明

> 请基于当前 dev 工作树实施本文件。保留已完成的平台预留，不重写整个 Harness。按 R0→R1→R2 的停止语义优先顺序推进，每项先跑现有失败复现，再改生产逻辑，再补正式回归。不要自动重试输入、吞错误、换浏览器或用固定 sleep 伪装就绪。需要扩展动作 schema、滚动单位或托盘能力时，明确记录契约变化并保护旧 golden。真实键鼠测试与 pytest 分离；没有通过前台身份校验时不得输入。每项结束写清测试证据和未完成限制，全部验收前不要声称全量修好。

本文实际使用技能：writing-plans。编写期间重新确认了当前审计失败基线，没有实施上述生产修复。
