# Y-CODE 浏览器与 Windows 电脑操控设计

## 状态

- 日期：2026-09-02

- 目标分支：`dev`

- 设计状态：用户已批准总体方案与推荐界面，尚未开始实现

- 首发平台：Windows 10 / Windows 11 当前交互式用户桌面

- 浏览器：Microsoft Edge、Google Chrome

- 浏览器配置目录：Y-CODE 专用持久化目录，禁止复用用户日常浏览器配置目录

- 默认状态：浏览器操控关闭、完整电脑操控关闭

- 授权语义：用户在设置页显式开启后，Y-CODE 不再对每一个鼠标、键盘或浏览器动作重复弹出自身权限确认

- 核心架构：OpenAI 原生 Computer Use 协议与通用视觉工具协议双通道，共用同一个本地执行器、会话控制器、停止链路和 UI 状态模型

- UI 方案：顶部常驻安全控制条 + 右侧“操控”轨迹页签

- CLI 方案：支持启动电脑操控任务、查看状态、暂停、恢复和停止；实际画面显示在 Windows 桌面，CLI 不伪造内嵌桌面

## 1. 背景

Y-CODE 已具备多 Provider、多模态输入、工具注册、严格 Harness 生命周期、权限与沙盒、取消、GUI/CLI 双入口、媒体管线、DeepSeek Prompt Ledger、Kimi/GLM 缓存亲和以及完整的前后端测试体系。电脑操控不是简单增加一个鼠标点击函数，而是同时涉及：

1. 模型能力识别；
2. OpenAI 原生 `computer_call` 协议；
3. DeepSeek、Kimi、GLM 等模型的视觉 + function calling 通用协议；
4. 浏览器专用配置目录和可见浏览器生命周期；
5. Windows 截屏、坐标映射、键鼠注入与安全桌面边界；
6. GUI/CLI 可见状态、暂停、停止和操作轨迹；
7. Harness Step、取消、消息持久化、缓存字节稳定性；
8. 打包版依赖与真实 Windows 环境验证。

如果把这些逻辑直接堆进 `core/llm.py` 或普通插件，会破坏现有固定 Harness 边界，也很难证明默认关闭时没有回归。本设计采用隔离的 `core/computer/` 领域模块，并只在明确的 Provider、Harness、GUI 和 CLI 接缝处接入。

## 2. 用户已确认的产品决策

以下决策已经固定，实施者不得自行改写：

1. Edge 和 Chrome 都必须支持。
2. 浏览器选择位于系统设置页。
3. “浏览器操控”和“完整电脑操控”是两个独立开关。
4. 两个开关都默认关闭，不做首次启动自动开启。
5. 开关只能由用户在设置页修改；CLI 不提供永久开启或关闭配置的命令。
6. 用户开启后，Y-CODE 自身不再逐动作请求授权。
7. 浏览器使用 Y-CODE 专用配置目录；需要登录时，由用户在可见浏览器中手动登录。
8. 禁止复制、挂载或复用用户日常 Edge/Chrome 的 profile、Cookie、历史和扩展目录。
9. OpenAI 原生 Computer Use 模型走原生协议。
10. DeepSeek、Kimi、GLM 以及其他明确具备视觉和工具调用能力的模型走通用协议；不是“尝试一下”，而是在能力契约满足时作为正式支持路径执行。
11. 不支持的模型、平台、浏览器或协议必须明确拒绝，不静默切换模型、浏览器、协议或执行后端。
12. 电脑操控进行时采用“顶部安全条 + 右侧操作轨迹”的推荐布局。
13. CLI 必须支持电脑操控的状态与控制命令。
14. 实施允许较长，但必须按小步 TDD 推进，每个部分单独测试，阶段性执行回归，最终执行全量验证。
15. 不能为实现电脑操控破坏现有聊天、工具、计划、子代理、MCP、插件、权限、缓存、媒体、GUI、CLI、取消和打包功能。

## 3. 目标

### 3.1 功能目标

1. 当浏览器操控已开启且模型能力满足时，模型能在可见 Edge/Chrome 页面中观察、点击、输入、滚动、导航、前进、后退、刷新和等待。
2. 当完整电脑操控已开启且模型能力满足时，模型能在当前 Windows 用户桌面中观察、移动鼠标、点击、双击、拖拽、滚动、输入 Unicode 文本、发送受支持的组合键和等待。
3. OpenAI 原生 Computer Use 输出被严格转换成统一动作，执行后按原生 Responses 协议返回 `computer_call_output`。
4. 通用视觉模型通过一个严格的 `computer_action` function tool 获取观察并执行动作，动作结果后自动获得最新截图。
5. GUI 和 CLI 使用同一个进程级控制器，任何入口发出的暂停或停止都作用于同一个活动操控会话。
6. 用户关闭任一设置开关时，对应能力立即失效，活动会话被取消，后续动作全部拒绝。

### 3.2 稳定性目标

1. 两个开关均关闭时，现有 Provider 请求、稳定 system prompt、tools schema、工具顺序和缓存 lane 字节完全不变。
2. 电脑操控代码不在未启用时启动浏览器、截屏线程、键盘钩子、HTTP 预览服务或轮询器。
3. 一个电脑动作对应一个真实 Harness Step 内的一次模型决定和一次本地动作；Provider 内部重试仍不得新增 Step。
4. 取消、崩溃和进程退出不得自动重放鼠标、键盘、导航或其他副作用动作。
5. 所有动作验证、状态转换和错误分类均为确定性规则，不增加“猜测成功”、自动换浏览器、自动换协议或吞异常的后处理。

### 3.3 可控性目标

1. 操控状态始终可见。
2. 始终提供暂停和醒目的立即停止。
3. `Ctrl+Alt+Esc` 作为 Windows 全局紧急停止快捷键；如果无法注册，电脑操控开关不得启用。
4. 操作轨迹至少显示动作序号、动作类型、目标、结果和耗时，但不得显示完整输入文本、凭据或截图原始数据。
5. 同一进程任何时刻只允许一个活动电脑操控会话，避免不同会话或模型争夺鼠标与键盘。

## 4. 非目标

首版明确不实现：

- macOS、Linux、WSL 图形桌面；

- Windows 锁屏、登录界面、UAC 安全桌面；

- 向更高完整性级别进程自动提权；

- SSH、服务进程、计划任务或无交互桌面环境中的电脑操控；

- 无头浏览器；

- 复用用户日常浏览器 profile；

- 自动读取系统密码库、浏览器保存密码或验证码；

- 绕过 CAPTCHA、Provider 安全检查或网站风控；

- 以 DOM selector、页面脚本注入或隐藏浏览器作为主要操控方式；

- 通用文件选择器、下载管理器或上传专用 API；需要操作系统文件对话框时必须开启完整电脑操控；

- 多个并发桌面操控会话；

- 手机/LAN 客户端开启或恢复电脑操控；手机端只允许查看状态和执行停止；

- 自动把原生协议失败降级成通用协议；

- 自动把 Edge 失败降级成 Chrome，或反向降级；

- 自动恢复崩溃前未完成的电脑操控任务。

## 5. 已选方案与未选方案

### 5.1 已选：双协议适配器 + 共享执行核心

```text
模型请求层
  ├─ OpenAI native computer
  │    └─ NativeComputerProtocolAdapter
  └─ Generic vision + function calling
       └─ GenericComputerProtocolAdapter
                         │
                         v
                 ComputerAction（统一动作）
                         │
                         v
              ComputerControlService（唯一租约）
                  ├─ BrowserBackend
                  └─ WindowsDesktopBackend
                         │
                         v
               ComputerObservation（统一观察）
                         │
              ┌──────────┴──────────┐
              v                     v
       原生 call output       tool result + 图片消息
```

选择理由：

- Provider 协议差异被限制在适配器，动作执行只实现一次。

- GUI、CLI、停止、热键、专用 profile 和错误处理不依赖具体模型。

- 新增其他视觉工具模型时只需要配置能力，不复制执行器。

- 原生 OpenAI 路径仍保留原生语义，不把它伪装成普通 function tool。

### 5.2 未选：所有模型统一模拟 OpenAI 原生协议

DeepSeek、Kimi、GLM 不会自动理解 OpenAI 的私有原生事件。伪造协议会把供应商差异推入不可测试的兼容层，并导致模型请求失败，因此不采用。

### 5.3 未选：所有模型都只使用通用 function tool

该方案实现较快，但会放弃 OpenAI 原生 Computer Use 的动作、安全检查和续传语义，不符合已经确认的“原生模型走原生协议”。

### 5.4 未选：把电脑操控做成普通插件

插件可以增加 function tool，但不能完整处理 Responses 原生 `computer_call`、`computer_call_output`、Provider response continuation、Harness 消息投影和全局停止状态。因此电脑操控属于固定 Harness 能力，不由插件替换。

## 6. 依赖方向和模块边界

新增固定领域包：

```text
core/computer/
  models.py              值对象、枚举、状态转换、错误码
  settings.py            配置键、严格校验、浏览器探测、设置快照
  capability.py          模型能力契约与协议选择
  actions.py             模型动作解析、坐标和参数校验
  controller.py          进程级唯一会话、租约、暂停、停止、事件
  browser_backend.py     专用 worker 线程持有 Playwright persistent context 与页面级动作
  windows_backend.py     Windows 截屏、坐标转换、SendInput
  preview_server.py      本机令牌化最新截图预览服务
  hotkey.py              Ctrl+Alt+Esc 注册、消息循环、注销
  provider_generic.py    通用 function tool、观察消息与动态提示投影
  provider_openai.py     原生 Responses computer 事件、续传与持久化投影
  tool_adapter.py        computer_action 的固定 Harness 适配器
```

依赖规则：

1. `models.py` 不导入 GUI、CLI、LiteLLM、Playwright 或全局配置。
2. 两个 backend 只实现 `ComputerBackend` 接口，不接触 Provider 消息。
3. `controller.py` 只接收规范化动作并返回规范化观察，不读取模型名称。
4. `capability.py` 只读取显式模型配置并返回协议决策，不探测 API 行为。
5. `provider_generic.py` 和 `provider_openai.py` 只负责 Provider 消息和统一动作/观察之间的转换，不执行鼠标或键盘。
6. GUI/CLI 只调用 controller 的公开方法并消费事件，不直接引用 Playwright 或 Win32。
7. `core/llm.py` 保留兼容入口，只在请求构造、流事件标准化、工具执行接缝和持久化投影处调用新模块。
8. 插件和 MCP 不能注册同名 `computer_action`；该名称由固定 Harness 保留。

## 7. 核心领域模型

### 7.1 枚举

```python
ComputerProtocol = Literal["none", "generic", "native_openai"]
ComputerTarget = Literal["browser", "desktop"]
BrowserKind = Literal["edge", "chrome"]
ComputerRunStatus = Literal[
    "idle", "starting", "running", "paused", "waiting_for_user",
    "provider_safety_required", "completing", "completed",
    "stopping", "stopped", "failed"
]
ComputerActionKind = Literal[
    "observe", "click", "double_click", "move", "drag", "scroll",
    "type_text", "keypress", "wait", "navigate", "back", "forward", "reload",
    "request_user_takeover"
]
```

### 7.2 `ComputerAction`

动作值对象至少包含：

- `kind`；

- `target`；

- 可选截图坐标 `x`、`y`；

- 可选拖拽路径 `path`，由至少 2 个、至多 256 个整数坐标点组成；generic 兼容输入可使用起点和终点，解析后必须立即规范化为 `path`；

- 鼠标键 `left | right | middle`；

- 滚动量 `delta_x`、`delta_y`；

- 输入文本 `text`；

- 组合键数组 `keys`，既可用于键盘组合键，也可作为受支持鼠标动作的 modifier；

- 导航地址 `url`；

- 等待毫秒 `duration_ms`。

动作对象在构造时严格校验：

- `observe` 不接受额外动作字段；

- 点击、移动必须同时提供整数 `x/y`；

- 拖拽必须提供合法 `path`，相邻重复点可保留但路径不得为空；

- 文本最大 8000 个 Unicode code point；

- `wait` 范围为 100–10000 ms；

- 组合键最多 8 个键，只允许登记过的键名；

- URL 只接受 `http://` 或 `https://`；

- `navigate/back/forward/reload` 只允许浏览器 target；

- `request_user_takeover` 只接受稳定 `reason_code`，不得携带凭据、验证码或输入框正文；

- 任何未知字段、缺失字段、布尔值伪装整数、NaN 或越界值均明确拒绝。

### 7.3 `ComputerObservation`

观察值对象包含：

- `run_id`；

- `target`；

- `screen_sha256`；

- PNG 文件的短期本地路径；

- 发给模型的宽高；

- 原始桌面宽高和虚拟桌面原点；

- 坐标缩放比例；

- 当前窗口标题的受控摘要；

- 浏览器页面 URL 的 origin 与页面标题；

- 动作结果状态和稳定错误码；

- `before_fingerprint`、`after_fingerprint`；

- Provider 可见的精简 JSON；

- GUI 可见的令牌化预览 URL。

模型可见结果不得包含时间戳、随机序号或本地绝对路径，避免相同画面被误判为“有进展”。UI 事件可以包含序号和耗时。

### 7.4 `ComputerRunSnapshot`

UI/CLI 快照包含：

- run id；

- owner session / turn id；

- 状态；

- target 和 browser；

- 模型显示名与 computer protocol；

- 开始时间、动作数、当前动作；

- 当前窗口或页面摘要；

- 最新预览 URL；

- 暂停原因、停止原因、错误码；

- 全局热键是否可用。

快照不得包含输入文本、截图 base64、API Key、Cookie 或浏览器 profile 内容。

## 8. 设置与配置

### 8.1 配置键

在现有 `config.json` 中增加：

```json
{
  "browser_control_enabled": false,
  "computer_control_enabled": false,
  "computer_browser": "edge"
}
```

规则：

- 缺失布尔键等价于 `false`，兼容旧配置。

- 显式非布尔值属于损坏配置，必须报错，不使用 Python truthy 转换。

- `computer_browser` 只接受 `edge` 或 `chrome`；缺失时为 `edge`。

- 浏览器选择在活动会话中不可修改，必须先停止当前操控。

- 开启开关前执行对应 preflight；失败则不写入 `true`。

- 只要任一开关为 `true`，`Ctrl+Alt+Esc` hotkey 必须由进程持续持有；设置保存时“注册成功后立即注销”只能作为探测，不能满足运行期安全要求。应用启动读取到已开启配置时先注册热键，注册失败则电脑能力不可用并显示明确错误，但不得静默改写用户配置。两个开关都关闭或应用退出时才注销。

- 关闭开关先原子写入 `false` 并递增内存设置 generation，再通知 controller 停止；因此即使 backend 清理较慢，也不会接受新动作。

- 运行中的 Turn 冻结启用能力和浏览器选择。开启只影响后续 Turn；关闭是例外，必须立即取消当前 Turn 的操控。

### 8.2 系统设置快照

现有 `get_system_settings()` 作为聚合边界增加：

- 三个配置值；

- `computer_platform_supported`；

- Edge/Chrome 的 `available`、`executable` 显示名和不可用原因；

- 专用 profile 的只读展示路径；

- 全局停止热键是否可注册；

- 当前控制器状态摘要。

快照不得包含 profile 文件清单、Cookie 或凭据。

`core/computer/settings.py` 只能返回静态 `ComputerSettingsSnapshot`（配置值、generation、浏览器探测和 profile 展示路径），不得导入 controller 或 hotkey。`core/command_shell.py:get_system_settings()` 通过惰性导入聚合静态设置、hotkey snapshot 和 controller snapshot，避免设置模块与运行时模块形成循环依赖。

### 8.3 设置页布局

在现有 `SystemSettings.vue` 紧凑列表中新增“电脑操控”区，而不是重做整个设置页：

1. 浏览器操控开关；
2. 默认浏览器选择；
3. 完整电脑操控开关；
4. 专用配置目录说明；
5. 明确提示“开启后截图会发送给当前模型 Provider，Y-CODE 不逐动作询问”；
6. 当前平台、浏览器和紧急停止热键的 preflight 结果。

每个保存动作使用现有“失败回滚 + 明确 modal”模式。前端乐观状态不能覆盖后端返回的真实快照。

### 8.4 两个开关的独立语义

- `browser_control_enabled` 授予专用 Playwright 浏览器 backend，只能操作页面内容和显式浏览器导航动作。

- `computer_control_enabled` 授予整个 Windows 桌面 backend。桌面能力天然可以点击任何可见应用，其中也包括普通浏览器窗口；浏览器开关不可能反向沙盒完整桌面能力。

- 两个开关同时开启不是“双重授权”，而是允许模型在一个新 run 开始时从两个 backend 中选择一个。

- 一个 run 绑定一个 target，运行中禁止从 browser 切到 desktop 或反向切换；需要切换时先结束当前 run，再以新 target 建立新 run。

- 只有一个开关开启时，该 target 在 run 创建时直接确定。

- 两个开关同时开启时不得通过用户文字关键词猜测 target：generic 协议以第一次合法 `observe` 的显式 `target` 建立租约；native 协议先调用受控 function tool `computer_select_target(target=browser|desktop)`，之后才允许执行原生 computer call。

- `computer_select_target` 从 Turn 第一个请求起就与 native computer tool 一起存在，避免同一 Turn 中途改变 tools contract。target 已选后重复选择或选择未开启能力均明确拒绝。

## 9. 模型能力契约

### 9.1 新增显式字段

模型配置扩展：

```json
{
  "supports_vision": true,
  "supports_function_calling": true,
  "computer_protocol": "generic"
}
```

规则：

- `computer_protocol=native_openai`：只用于明确支持 OpenAI Computer Use 的模型。

- `computer_protocol=generic`：必须同时满足 `supports_vision=true` 和 `supports_function_calling=true`。

- `computer_protocol=none` 或缺失：不具备电脑操控能力。

- 旧配置缺少 `supports_function_calling` 时，现有普通工具行为保持不变，但不得因此自动获得电脑操控资格。

- 自定义模型由用户在模型设置页显式选择能力；不按模型名字符串猜测。

- 预置模型由 `core/model_catalog.py` 提供明确能力元数据。

### 9.2 初始能力范围

首版预置表至少覆盖已经确认的模型家族：

- OpenAI 官方且文档明确支持 Computer Use 的 Responses 模型：标记 `native_openai`；

- `deepseek-v4-flash-vision-exp`：视觉和工具能力确认后标记 `generic`；

- Kimi 预置中的 `k3`、`k3-256k`、`kimi-k3` 等实际声明视觉且支持工具的条目：标记 `generic`；

- GLM 预置中实际声明视觉且支持工具的条目，例如当前目录中的 `glm-5.3-flash`：标记 `generic`；

- 其他模型只有在预置元数据或用户自定义配置明确给出两个能力和协议时才启用。

实施时不得维护“模型名包含 glm/kimi/deepseek 就开启”的猜测列表。

### 9.3 运行时决策

`resolve_computer_capability(model, provider, settings, turn_source)` 返回不可变决策：

- `enabled`；

- target 集合；

- computer protocol；

- 失败原因和稳定错误码；

- 选定 browser；

- 设置 generation。

额外约束：

- `native_openai` 必须是 Responses 协议、OpenAI 官方 API Base 和显式原生能力；否则拒绝。

- `generic` 必须满足视觉 + function calling；否则拒绝。

- LAN/mobile 发起的 Turn 固定 `enabled=false`；手机端仍可查看和停止已有会话。

- 决策只在 Turn 开始时计算一次，避免同一 Turn 中 tools schema 漂移。

## 10. 统一动作协议

### 10.1 通用 function tool

模型可见名称固定为 `computer_action`，由 Harness 保留，不进入普通插件 registry。工具说明必须要求：

- 第一次先调用 `observe`；

- 每次只调用一个电脑动作；

- 每个动作后等待新截图再决定下一步；

- 坐标以最新截图左上角为 `(0,0)`；

- 不猜测动作成功，必须检查下一观察；

- 遇到登录、验证码或需要用户输入凭据时停止并请求用户手动接管；

- 完成任务后输出正常最终结论，不继续无意义观察。

一个 assistant 响应如果同时包含多个 `computer_action`，或电脑动作与其他工具混用，本 Step 不执行任何电脑动作，并为相关 call 返回明确错误，要求模型下一 Step 一次只调用一个动作。这样不会在缺少中间观察时盲目连续点击。

第一次合法 `observe` 同时建立 target 租约；后续 action 的 `target` 必须完全一致。模型不能借单个 run 在 browser 和 desktop 之间来回切换。

### 10.2 动作结果

普通 tool result 为稳定 JSON 文本，只包含：

- `ok`；

- `target`；

- `action`；

- `screen_sha256`；

- `width/height`；

- 页面 origin 或窗口摘要；

- 可选稳定错误码和错误信息。

随后追加一条自动生成的 user 多模态消息：一段固定格式观察说明 + 最新 `image_url` PNG。该消息进入正常消息和媒体维护链路，使 Chat Completions、Anthropic 和 Responses function calling 路由都能在下一 Step 看见画面。

旧截图由现有 DB 媒体折叠事务处理；请求路径不得临时改写历史。为保留 `_computer_v1` 元数据，`core/memory.py` 的媒体检测和折叠必须同时识别 content 列表与包含 `content` 列表的完整消息 dict；折叠只替换媒体块，不删除私有元数据。必须用幂等测试证明旧截图不会无限累积。电脑观察私有字段在发 Provider 前剥离。

### 10.3 动态提示词

通用电脑操控说明只在 capability 决策为 enabled 时加入动态运行上下文。不得修改 `build_stable_system_message()`，不得改变默认关闭时的 system prompt hash。

## 11. OpenAI 原生 Computer Use 协议

本节已按 2026-09-02 的 OpenAI 官方 Computer use 指南复核。新实现使用 GA Responses 形状 `tools: [{"type": "computer"}]`；`computer_use_preview`、单个 `action` 和 `truncation: "auto"` 属于旧 preview 集成，本项目不为新实现采用该旧路径。官方指南：<https://developers.openai.com/api/docs/guides/tools-computer-use>。

### 11.1 请求

当能力为 `native_openai` 时，Responses 请求 tools 在现有 function tools 之后追加精确的 GA 原生定义 `{"type":"computer"}`；如果 browser 和 desktop 同时可用，还要追加固定 function tool `computer_select_target`。默认关闭或非原生模型不得出现这些定义。

`_responses_tools_from_chat_tools()` 不能被改成接受任意未知 built-in tool；只允许经过 capability 决策注入的受控 computer 定义。

### 11.2 流事件

Responses 事件标准化层增加对以下语义的严格支持：

- response id；

- `computer_call` output item；

- 完整、非空的 `actions[]`；

- call id；

- completed / incomplete / failed。

缺少 response id、call id、`actions[]`，或任一动作字段冲突时，当前 Step 以 Provider 协议错误失败。不得改走通用协议。GA `screenshot` action 映射为统一 `observe`。

GA 原生协议允许一个 `computer_call` 返回有序的 `actions[]`，这是 generic“一次一个 function call”规则的显式例外。Y-CODE 必须按原顺序串行执行；每个动作前重新检查暂停、停止、owner、settings generation 和安全桌面，每个动作均捕获本地 after observation、执行无进展判断并产生脱敏 trace。任一动作失败或用户停止后，后续动作全部不执行；整批成功后只把最后一次 observation 的截图发送为一个 `computer_call_output`。这里“只发送最后一图”不等于“中间动作不观察”。不得并行动作，也不得把整批当成不可中断的单次 backend 调用。

当两个 target 同时可用时，原生请求固定设置 `parallel_tool_calls=false`。`computer_select_target` 必须由固定 adapter 在独立 Step 中执行并建立 target 租约；同一个 Provider response 同时出现 selector 和 `computer_call` 时以 `provider_protocol_error` 失败，绝不按 output 数组顺序猜测。尚未建立租约时，原生 computer call 不得执行。

### 11.3 续传

原生电脑操控活动 Turn 使用 Responses `previous_response_id` 续传：

1. 首次请求发送正常 Y-CODE 会话历史和 computer tool。
2. Provider 返回含有序 `actions[]` 的 computer call 和 response id。
3. 本地逐个串行执行统一动作，整批成功后生成截图。
4. 下一 Step 不重发完整消息历史，但必须重新发送同一 model、同序 tools、必要 instructions/options、上一 response id 和新的 `computer_call_output`；`instructions` 不依赖 `previous_response_id` 自动继承。
5. Provider 返回最终文本后结束原生 continuation。

如果 Provider 未返回可用 response id，立即失败；禁止退回“重发全部截图历史”或通用 function tool。

### 11.4 持久化投影

原生中间 call/output 以 `_computer_v1` 标记写入 `messages`，供 UI 回放和取消打捞，但截图 base64 与 Provider response id 不持久化：

- action 只保存脱敏摘要；

- 输入文本只保存长度和 SHA-256，不保存正文；

- screenshot 只保存本 Step 的摘要哈希，不写 Harness Journal 正文；

- 这些原生 continuation 中间行在后续普通 Provider 请求中被确定性排除；

- Provider 最终文本作为普通 assistant 消息持久化，是后续会话上下文的正式结论。

崩溃后原生 continuation 标记 interrupted，不自动恢复。下一轮不重放任何动作，也不提交旧 response id。

### 11.5 Provider 强制安全检查

“开启后无需逐动作授权”只取消 Y-CODE 自身的逐动作权限询问，不能伪造或绕过 Provider 强制 safety check。截至本设计复核时，GA Computer use 指南没有定义 `acknowledged_checks` 请求契约，而 beta Responses 类型仍暴露 `pending_safety_checks`。因此实现不得凭旧 preview 示例发明字段：

- 原生解析器可以保存 Provider 返回的稳定 check id/code，但不得保存原文到 Journal；

- 只有在当前官方 GA 请求 schema 或所用 SDK 类型明确提供确认字段时，才允许构造对应确认 payload，并为该精确形状增加 byte-golden；

- 若 Provider 返回强制检查但当前客户端没有已验证的确认契约，controller 进入 `provider_safety_required`，不执行动作，并明确提示用户停止或升级客户端；

- GUI 和 CLI 都必须提供查看状态与停止入口；只有在确认 payload 已有官方契约并实现 byte-golden 后，才显示“继续”入口；

- 未确认不得发送虚假的 acknowledged safety checks，也不得回退 generic 绕过。

这是外部协议强制边界，不得以“完全访问”配置绕过。

## 12. 浏览器执行后端

### 12.1 技术选型

使用 Python Playwright 的 headed persistent context，并直接调用系统安装的 Edge/Chrome channel。不得下载或启动 Playwright 自带 Chromium 作为静默替代。

同步 Playwright 对象具有线程归属。`BrowserComputerBackend` 必须创建一个专用 worker 线程，该线程独占 `sync_playwright`、browser context、page 和 popup 事件；start、observe、execute、complete、close 全部通过有界命令队列 marshal 到该线程。外部 RLock 只用于保护 backend 状态，不能代替线程归属。worker 未就绪、超时或退出时明确失败，不在调用线程重建 Playwright 对象。

原因：

- persistent context 正好满足独立登录态；

- Edge/Chrome channel 有官方支持；

- 页面截图、鼠标、键盘、弹窗和多页面生命周期可测试；

- 比自建 CDP 客户端更少协议维护风险。

### 12.2 专用 profile

固定目录：

```text
%YCODE_DATA_DIR%/browser-profiles/edge/
%YCODE_DATA_DIR%/browser-profiles/chrome/
```

未设置 `YCODE_DATA_DIR` 时沿用 `config.YCODE_DIR`。实现必须使用 `Path(config.YCODE_DIR) / "browser-profiles" / browser`；当前 `config.YCODE_DIR` 是字符串，禁止直接对字符串使用 `/`。规则：

- 目录只由当前用户创建和使用；

- 不从默认浏览器目录复制文件；

- Edge 和 Chrome 互不共享目录；

- 同一 browser profile 只允许一个 Y-CODE context 持有；

- profile 被其他进程锁定时明确失败；

- context 关闭时正常 flush Cookie 和站点数据；

- Y-CODE 运行期间完成任务后可保留可见浏览器，应用退出时必须关闭其 Playwright 子进程；

- 用户下次任务重新打开时登录态由专用 profile 自然恢复。

### 12.3 页面和坐标

- 浏览器窗口必须可见，禁止 `headless=True`。

- 页面 viewport 固定为 1440×900，device scale factor 固定为 1，确保截图坐标和 page mouse 坐标一致。

- 模型只控制网页内容区，不直接点击浏览器标签栏或地址栏。

- URL 导航、前进、后退和刷新由显式 browser action 完成。

- 新 popup/page 出现时，以最后获得焦点的可见 page 作为 active page，并发出页面变化事件。

- 页面或 context 被用户关闭时，当前动作失败并停止会话，不自动重开。

- persistent context 启动后若没有 page，创建且只创建一个 `about:blank`；多个 page 时按最后获得焦点的可见 page 选择 active page。

### 12.4 登录与用户接管

遇到登录、验证码或凭据输入时：

1. 模型调用固定 `request_user_takeover(reason_code=login_required|captcha_required|credential_required)` 动作；普通助手文字不得触发状态转换；
2. controller 进入 `waiting_for_user`，保留 run 和专用浏览器，但释放当前动作执行权；
3. 用户完成后从 GUI/CLI 选择恢复；
4. 恢复后先 `observe`，不得直接继续旧坐标动作。

Y-CODE 不读取输入框内容、浏览器密码库或 Cookie 值，也不把 profile 目录作为模型工具可见路径。

## 13. Windows 桌面执行后端

### 13.1 截图

使用 Pillow `ImageGrab.grab(all_screens=True)` 捕获虚拟桌面，并由 Win32 adapter 使用 `GetSystemMetrics(SM_XVIRTUALSCREEN/SM_YVIRTUALSCREEN/SM_CXVIRTUALSCREEN/SM_CYVIRTUALSCREEN)` 取得真实虚拟屏幕矩形。Pillow 图像尺寸必须与 Win32 宽高一致，否则 preflight/observe 失败。观察必须记录：

- 虚拟桌面原点，允许多显示器负坐标；

- 原始物理像素宽高；

- 发给模型的缩放后宽高；

- 从截图坐标到物理坐标的精确比例。

最长边超过 1920 时等比缩小为 PNG。执行动作时按记录的比例和虚拟桌面原点反算物理坐标，并在 SendInput 前再次检查边界。

### 13.2 DPI

启动 desktop backend 前确认进程 DPI awareness 为 Per-Monitor V2。若现有 GUI 已设置则验证而不重复改变；CLI 路径显式设置。设置失败时完整电脑操控 preflight 失败，不在错误坐标系中继续。

### 13.3 输入

使用 `ctypes` 封装 Win32 `SendInput`：

- 鼠标移动使用绝对虚拟桌面坐标；

- click/double click/drag 明确发送 down/up 序列；

- scroll 使用 wheel/hwheel；

- Unicode 文本使用 `KEYEVENTF_UNICODE`；

- 组合键采用登记的虚拟键并保证 finally 中释放已按下键；

- 任何 SendInput 返回数量少于请求数量都视为 `input_rejected`。

测试环境必须通过注入 fake Win32 adapter 验证，pytest 不得真实移动开发机鼠标或发送键盘。

### 13.4 不可控桌面

每次桌面动作前检查当前 input desktop：

- 必须是交互式 `Default` desktop；

- 锁屏、登录界面或 UAC secure desktop 时停止；

- 无 foreground window 或截屏失败时停止；

- 目标应用完整性级别高于 Y-CODE 且 SendInput 被 UIPI 拒绝时明确报错；

- 不自动请求管理员权限或重启自身。

## 14. Controller、租约和状态机

### 14.1 单一控制器

进程内只有一个 `ComputerControlService`。创建活动 run 时获取排他租约：

- owner session；

- owner turn id；

- owner cancel event；

- capability snapshot；

- target/backend；

- settings generation。

第二个 Turn 请求操控时返回 `session_busy`，包含用户可识别但不含敏感数据的 owner 会话名称，不排队自动接管。

### 14.2 状态转换

```text
idle -> starting -> running -> completing -> completed
                    |  |  |
                    |  |  `-> failed
                    |  `-> waiting_for_user -> running
                    `-> paused -> running

running/paused/waiting/provider_safety_required -> stopping -> stopped
starting -> failed
```

非法状态转换抛出领域不变量错误。`complete_run()` 和 `stop()` 都必须幂等；重复调用不得重复释放租约或重复关闭 backend。

Provider 返回最终文本且当前没有未完成 computer call 时，Turn 终结路径必须调用 `complete_run(run_id, owner, retain_browser_session=True)`：把 run 标记为 completed、释放 owner/活动租约并发出最终事件。浏览器 worker/context 可以作为不具备活动执行权的 retained session 保留可见，下一 run 显式复用；桌面 backend 立即释放输入和运行态。`stop()` 专用于取消、故障和用户紧急停止，会关闭 backend。任何 Turn 终态都必须在 `finally` 中二选一调用 complete 或 stop，不能遗留 running lease。

### 14.3 动作执行顺序

每个动作：

1. 检查 owner、状态、cancel event 和 settings generation；
2. 检查 action 与 backend target；
3. 捕获 before fingerprint；
4. 执行动作；
5. 捕获 after observation；
6. 更新无进展计数；
7. 发出动作和观察事件；
8. 返回 Provider 投影。

任何一步失败都不能谎报动作成功。截图失败时不得返回上一张截图。

### 14.4 无进展和预算

- 每个电脑动作由一次模型 Step 驱动，因此沿用 Turn 启动时冻结的 `agent_turn_step_limit`，不新增另一个可漂移的动作预算。

- `observe` 和 `wait` 不计入本地“画面未变化”连续计数，但仍计入 Harness Step。

- 对预期改变界面的动作，如果截图 SHA-256、active window/page fingerprint 连续 3 次完全相同，则停止并返回 `no_progress`。

- 不使用相似度猜测、OCR 猜测或模型自报成功作为进展证据。

- 现有 `TurnProgressGuard` 继续工作；电脑 tool result 不含时间戳/随机序号，确保相同状态仍会被识别为停滞。

- `RepeatCallGuard` 不为电脑动作豁免；同签名成功两次后的现有拦截仍保留。

## 15. 暂停、停止、取消和退出

### 15.1 停止入口

所有入口汇聚到同一个 `controller.stop(run_id, reason)`：

- GUI 顶部“立即停止”；

- GUI 当前 Turn 停止；

- CLI `/computer stop`；

- CLI `Ctrl+C`；

- Windows 全局 `Ctrl+Alt+Esc`；

- 设置开关关闭；

- 应用退出；

- Provider safety block 后用户停止；

- 锁屏、安全桌面、浏览器关闭、无进展或 Step 上限。

### 15.2 顺序

停止顺序固定：

1. 设置 controller cancel flag；
2. 设置 owner Turn cancel event；
3. 关闭当前 Provider response 流；
4. 阻止新动作进入；
5. 取消可轮询的 wait/screenshot/browser 操作；
6. finally 释放所有按下的键和鼠标键；
7. 打捞已形成的合法 assistant/tool 消息组；
8. 标记 run 和 Harness 终态；
9. 释放排他租约；
10. 发出最终状态事件。

不关闭 LiteLLM 共享连接池，不重放动作，不注入“自动接手完成”。

关闭 Provider 流必须先设置 `owner.cancel_event`，再调用现有 `core.llm.abort_inflight_streams(owner.cancel_event)`；不得按 turn id 发明第二套取消注册表。

### 15.3 暂停

- 暂停只阻止下一动作开始，不伪造当前动作回滚。

- `wait` 和截图等可取消操作立即响应暂停/停止。

- 恢复后强制先产生新 observation，旧截图坐标作废。

- owner Turn 已结束时不得恢复旧 run。

## 16. GUI 设计

### 16.1 顶部安全控制条

插入在现有对话标题栏下方，只在 run 非 idle/stopped/completed 时显示。包含：

- 状态点和“正在操控 / 已暂停 / 等待用户 / 正在停止”；

- Edge、Chrome 或桌面；

- 当前动作数；

- 当前窗口或页面 host；

- 暂停/恢复按钮；

- 高强调“立即停止”按钮。

控制条在对话滚动时保持可见。窄屏时折叠次要文字，但暂停和停止始终可见。

### 16.2 右侧操控页签

复用现有 `RightPanel.vue`，动态增加“操控”页签，避免新建与现有右栏争抢宽度的第二抽屉。页签内容：

1. 最新截图预览；
2. run 状态和目标；
3. 按新到旧排列的最多 200 条内存操作轨迹；
4. 登录/Provider safety/失败原因；
5. 暂停、恢复、停止操作。

run 开始时首次自动切换到操控页签；用户之后手动切走时不反复抢回。run 结束后保留本次轨迹到下一个 run 或应用退出，但不写入长期截图历史。

### 16.3 截图预览传输

禁止通过 `evaluate_js` 直接推送大段 base64。`preview_server.py`：

- 仅绑定 127.0.0.1；

- 每次进程启动生成随机令牌；

- 只服务当前 run 最近 3 张、文件名严格校验的 PNG；

- 错令牌统一 404；

- 新 observation 后事件只推送短 URL；

- 启动时清理 24 小时前的残留临时目录；

- 应用退出时关闭服务并删除当前临时截图。

GUI 事件线程只向现有 `_js_queue` 投递小型 JSON；仍由 `gui-js-dispatcher` 单消费者执行 `evaluate_js`。

### 16.4 Store 和事件

新增事件：

- `computer_state`；

- `computer_action_started`；

- `computer_action_finished`；

- `computer_observation`。

前端 Store 使用纯 reducer 更新 `computerRun` 和 `computerTrace`。每个 run 拥有独立、严格递增的 `event_seq`；action ordinal 只表示动作顺序，不承担 pause/resume/stop 等状态事件排序。事件乱序时按 run id + `event_seq` 拒绝旧事件；旧 run 的迟到事件不能覆盖新 run。由于 GUI `_js_queue` 满时允许丢新事件，每次 Bridge 控制操作的返回值必须用完整 snapshot 校准 Store，并在窗口重新聚焦、切换到操控页签时调用 `get_computer_control_status()`，确保终态不会永久丢失。

### 16.5 GUI Bridge API

本地 Bridge 暴露：

- 设置读取和三个严格 setter；

- `get_computer_control_status()`；

- `pause_computer_control()`；

- `resume_computer_control()`；

- `stop_computer_control()`。

Bridge 方法不直接触碰 Vue 状态，只调用领域服务并返回快照。

## 17. CLI 设计

### 17.1 命令

CLI 只提供运行控制，不改变永久开关：

```text
/computer status
/computer pause
/computer resume
/computer stop
```

`/computer resume` 同时用于 `waiting_for_user` 的用户接管恢复；恢复实现必须强制下一动作是 observe。`provider_safety_required` 在没有经官方 schema 和 byte-golden 验证的确认 payload 时不可 resume，只允许 status/stop。

如果设置未开启，`status` 显示关闭；模型任务无法获得 computer 能力，并提示用户到 GUI 系统设置页开启。CLI 不提供 `/computer on` 或 `/computer off`。

### 17.2 显示

- 状态栏显示 `电脑操控: 运行中 · Edge · 12 步`；

- 每个动作只打印一行脱敏摘要；

- 输入文本显示“输入 N 个字符”，不打印正文；

- 截图仍显示在实际 Windows 桌面/浏览器，不转成终端字符画；

- Provider safety、锁屏、浏览器关闭和不可用环境显示明确错误码与中文原因；

- 原始 CLI 终端不输出 Markdown 强调符号。

### 17.3 热键和线程

热键线程、backend 线程不得直接调用 `print_formatted_text` 或修改 prompt\_toolkit layout。它们通过事件队列和 `app_instance.loop.call_soon_threadsafe()` 回到主线程。

## 18. LAN / 手机边界

电脑操控具有本机键鼠权限，首版不允许 LAN 客户端扩大权限：

- 新增设置 setter、pause 和 resume 进入 `LanServer.FORBIDDEN_METHODS`；

- `get_computer_control_status` 和 `stop_computer_control` 可用；

- LAN RPC 发起的用户 Turn 带不可伪造的 `turn_source=lan`；

- capability resolver 对 LAN Turn 返回 disabled，即使本机设置已开启；

- 手机可以看到本机正在操控并紧急停止，但不能通过远程消息启动电脑操控。

不得只依赖前端隐藏按钮；后端必须实施同一边界。

## 19. Harness 和工具管线接入

### 19.1 工具 schema

- `computer_action` 不加入模块级 `tool_registry`，避免默认关闭时改变全局 schema hash。

- capability 为 generic 时，在现有 route tool selection 之后、请求指纹捕获之前追加到 tools 尾部。

- capability 为 native 时，只在 Responses provider tools 中追加原生 computer tool。

- capability 关闭时必须返回与当前 `get_tools_schema()` 字节完全相同的列表。

- 同一 Turn capability 冻结，后续 Step 的工具契约不漂移。

### 19.2 固定 Tool Adapter

`core/computer/tool_adapter.py` 在普通 registry/MCP resolver 之前识别保留名 `computer_action`。必须同时验证当前 `ToolExecutionContext` 的 capability token 和 owner lease；模型伪造工具名不能绕过设置。

ToolSpec 语义：

- `requires_permission=False`，因为设置开关已经是用户授权边界；

- `cancellable=True`；

- `parallel_safe=False`；

- `mutates_files=False`；

- 不受工作区文件沙盒控制，但受 computer target 和 session lease 控制。

### 19.3 `ToolResult` 扩展

给 `ToolResult` 增加默认空的 `followup_messages` 不可变序列。现有工具不传该字段时行为和序列化完全不变。ToolBatchExecutor 在所有 tool result 之后、下一 Provider Step 之前按 call ordinal 追加 follow-up user observation。

电脑动作与其他工具混用的响应整体拒绝电脑动作，避免消息顺序和副作用不确定。

### 19.4 Step 和消息

- assistant computer call、tool output 和 generic observation 必须在当前 Step 的既有提交边界内按序落库；

- Harness Journal 只记录工具名、参数哈希、结果哈希、耗时和状态，不记录截图、文本或 URL 原文；

- 一次动作完成并提交后当前 Step completed，下一次看图决策创建新 Step；

- cancellation salvage 为未完成 computer call 生成明确取消 tool result；

- 原生 transient rows 使用受控投影，不让后续普通 Provider 请求出现悬空 call。

## 20. 缓存与请求字节不变量

### 20.1 默认关闭

以下现有测试期望必须零修改通过：

- `tests/test_prompt_byte_stability.py` 的固定 hash；

- `tests/test_provider_payload_golden.py` 的所有现有 expected；

- DeepSeek tools 顺序；

- Kimi tools 原始顺序；

- DeepSeek Prompt Ledger CAS、追加和切代；

- Kimi/GLM reasoning、cache control、metadata 和 extra\_body；

- token 统计口径。

禁止更新现有 golden/hash 来迁就电脑操控实现。

### 20.2 启用 generic

- computer tool 只追加在现有 tools 之后；

- tool contract fingerprint 自然变化；

- DeepSeek 官方 ledger 发现 tools contract 变化时按现有规则切代，不改写旧 generation；

- Kimi cache lane 身份包含新的 tool contract；

- 关闭后再次回到原始 tools 字节，但这是后续 Turn 的新 capability 快照。

### 20.3 启用 native

- 只有 OpenAI 原生 Responses 路径出现 built-in computer tool 和 continuation 参数；

- RequestEnvelope 记录 input/tools/options 的真实最终字节；

- `previous_response_id` 只存在于原生活动 continuation Step；

- 不把 response id 写入 Journal、普通消息正文或日志。

## 21. 错误分类与无降级原则

稳定错误码至少包括：

- `computer_disabled`；

- `computer_model_not_capable`；

- `computer_protocol_mismatch`；

- `computer_unsupported_os`；

- `computer_noninteractive_session`；

- `computer_hotkey_unavailable`；

- `computer_browser_unavailable`；

- `computer_profile_locked`；

- `computer_backend_start_failed`；

- `computer_session_busy`；

- `computer_invalid_action`；

- `computer_coordinate_out_of_bounds`；

- `computer_input_rejected`；

- `computer_secure_desktop`；

- `computer_browser_closed`；

- `computer_action_timeout`；

- `computer_provider_protocol_error`；

- `computer_provider_safety_required`；

- `computer_no_progress`；

- `computer_cancelled`。

禁止行为：

- 浏览器不可用时自动换另一个浏览器；

- Playwright 失败时自动改用桌面坐标点击浏览器；

- 原生事件解析失败时自动改用 generic；

- 截图失败时复用旧截图；

- SendInput 返回异常时假装点击成功；

- 配置非法时自动纠正；

- 捕获所有异常并返回空成功结果。

## 22. 隐私和数据处理

1. 设置页必须说明：开启后，当前浏览器页面或桌面截图会发送给当前模型 Provider。
2. 浏览器 profile 留在本机 Y-CODE 数据目录，不发送其文件内容。
3. 输入动作正文不进入 GUI trace、CLI trace、Harness Journal 或 runtime log。
4. trace 只记录文本长度和摘要哈希。
5. 预览服务只绑定 loopback，并使用随机令牌。
6. 临时截图最多保留当前 run 最近 3 张，停止或退出时清理。
7. generic 最新截图按现有多模态消息进入会话 DB；旧截图由现有媒体折叠事务处理。
8. native screenshot 只用于本次 `computer_call_output`，不持久化 base64。
9. URL 日志和 UI 默认显示 origin/host，不显示 query、fragment 或可能含 token 的完整 URL。
10. 错误日志不写 Cookie、认证头、response id 或 input text。

## 23. 依赖、打包和运行环境

### 23.1 依赖

- 新增 Python Playwright 运行时依赖；

- 不下载 Playwright Chromium；

- 继续使用 Pillow 截图；

- Windows 输入使用标准库 `ctypes`，不引入 pyautogui；

- 预览服务使用标准库 HTTP server 或复用现有受控服务模式。

- `ycode.spec` 使用 `PyInstaller.utils.hooks.collect_all("playwright")` 收集 Python 子模块、driver 数据和所需二进制，再合并到项目现有 `datas/binaries/hiddenimports`；构建产物扫描不得包含 Playwright `.local-browsers`、Chromium、Firefox 或 WebKit 浏览器缓存。

实施任务必须先做独立 dependency spike，证明：

1. 开发环境能用系统 Edge/Chrome channel 启动 persistent context；
2. 打包版能找到 Playwright driver 资源；
3. 不执行 `playwright install` 也能启动系统浏览器；
4. requirements 和 PyInstaller hidden data 明确列出；
5. 依赖不可用时 preflight 明确失败，Y-CODE 其他功能正常启动。

### 23.2 生命周期

- 浏览器和预览 server 由 controller 拥有；

- GUI/CLI 正常退出时显式 shutdown；

- 非正常崩溃依赖下次启动清理临时截图和浏览器 profile lock；

- 不杀用户自行启动的 Edge/Chrome，只关闭 controller 创建并持有的进程/context。

## 24. 测试策略

### 24.1 总原则

每一个实施 Task 必须：

1. 先写具体失败测试；
2. 运行并确认因当前缺失能力 RED；
3. 写最小实现；
4. 运行定向测试 GREEN；
5. 运行该 Task 的关联回归；
6. 检查 diff 和 `git diff --check`；
7. 把真实命令、退出码、测试数量和耗时记录到实施计划 Execution Record；
8. 只有当前 Task 全绿才能进入下一 Task。

测试不得真实移动开发机鼠标、输入键盘、调用付费模型或修改用户日常浏览器 profile。Win32、Playwright 和 Provider 全部通过依赖注入 fake 测试；真实烟测单独标记并由用户在受控环境执行。

### 24.2 基线门禁

实现前记录：

```powershell
venv\Scripts\python.exe -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
venv\Scripts\python.exe -m py_compile main.py gui_main.py gui_bridge.py core\llm.py
git diff --check
git status --short --branch
```

如果基线本身失败，先记录现有失败并停止电脑操控实现，不把无关失败混入本功能。

### 24.3 单元测试范围

#### 设置和能力

- 缺失配置默认 false/edge；

- 非布尔开关、非法浏览器值拒绝；

- preflight 失败不写盘；

- 关闭触发 generation 变化和 stop；

- generic 必须 vision + function calling；

- native 必须 official OpenAI + Responses + explicit capability；

- 旧模型配置不自动获得电脑操控。

#### 动作解析

- 每种合法动作；

- 缺字段、未知字段、bool 冒充 int、坐标越界；

- Unicode 长度、wait 边界、组合键白名单；

- browser-only action 不能进入 desktop；

- 多 computer call 和混合 tool call 整批拒绝电脑动作。

#### Controller

- 全状态转换；

- 单一租约；

- pause/resume/stop 幂等；

- setting disable 竞态；

- owner Turn cancel；

- 连续三次完全无变化停止；

- late event 不覆盖新 run；

- finally 释放按键。

#### Browser backend

- Edge/Chrome channel 选择精确，不互相 fallback；

- profile 路径隔离；

- viewport 和坐标；

- page close、popup、navigation、timeout；

- 用户关闭 browser 后停止；

- fake Playwright 不触及真实浏览器。

#### Windows backend

- 多显示器负原点映射；

- 缩放后坐标反算；

- SendInput 结构与返回数量；

- Unicode、组合键释放、drag；

- secure desktop/UIPI/无前台窗口失败；

- fake adapter 保证测试不真实注入输入。

#### Provider 协议

- generic function schema 和 follow-up 图片消息；

- DeepSeek Responses generic；

- Kimi OpenAI/Anthropic generic；

- GLM OpenAI generic；

- native computer tool 请求；

- native response id、computer call、call output、continuation；

- safety required 不执行动作；

- 原生失败不 fallback；

- cancellation 关闭流和 controller。

#### UI/CLI

- 设置默认关闭；

- 保存成功和失败回滚；

- 顶部 bar 的 running/paused/waiting/stopping；

- stop 始终可见；

- 操控页签、预览、200 条 trace 上限；

- run id/ordinal 防迟到事件；

- CLI 四个命令和状态行；

- raw terminal 不出现 Markdown 强调；

- 后台线程只通过主循环更新 UI。

#### LAN

- setter/pause/resume 被拒绝；

- status/stop 可用；

- LAN Turn capability 永远 disabled；

- 前端隐藏不作为唯一安全控制。

### 24.4 字节稳定和缓存回归

每次触碰请求构造、tools、消息规范化或 system prompt 前后都运行：

```powershell
venv\Scripts\python.exe -m pytest tests\test_provider_payload_golden.py tests\test_prompt_byte_stability.py -q
```

并增加 feature-on 独立 golden，不修改现有 feature-off expected。覆盖 DeepSeek ledger tool contract 切代、Kimi cache lane 变化和恢复关闭后的原字节。

### 24.5 阶段回归

每完成一个纵向阶段执行对应集合：

- 领域/设置阶段：config、model manager、GUI settings；

- backend 阶段：computer 单元测试 + cancellation；

- generic 阶段：LLM events、tool pipeline、media、DeepSeek/Kimi/GLM、cache；

- native 阶段：Responses、RequestEnvelope、stream assembler、Harness；

- GUI/CLI 阶段：全部 GUI/CLI tests；

- 打包阶段：构建、启动、依赖探测。

### 24.6 最终全量门禁

```powershell
venv\Scripts\python.exe -m py_compile main.py gui_main.py gui_bridge.py core\llm.py
venv\Scripts\python.exe -m compileall -q core\computer
venv\Scripts\python.exe -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
venv\Scripts\python.exe smoke_release_chain.py
git diff --check
git status --short --branch
```

打包脚本若项目环境允许，还要执行项目现有发布构建和安装后 smoke；不能只以源码测试代替打包验证。

### 24.7 受控人工烟测

自动测试全绿后，使用专门测试账号和无敏感内容桌面：

1. 默认关闭时普通聊天、工具和图片对话正常；
2. Edge 开启、专用 profile 首次登录、重启后登录态保留；
3. Chrome 同样验证且 profile 不共享；
4. 通用模型观察、点击、输入、滚动、完成；
5. OpenAI native 模型完整 call/output continuation；
6. 操控中关闭设置开关立即停止；
7. GUI stop、CLI Ctrl+C、`/computer stop`、全局热键分别停止；
8. 暂停后手动改变页面，恢复先重新观察；
9. 锁屏/UAC/关闭浏览器时明确停止；
10. 两个会话并发请求，第二个收到 busy；
11. 应用退出不留下 Y-CODE 创建的浏览器子进程；
12. 打包版重复上述最小路径。

人工烟测失败必须回到对应 Task 加自动回归测试后修复，不能只做一次性补丁。

## 25. 实施顺序

详细代码步骤将在用户审核本规格后单独写入实施计划。固定顺序为：

1. 捕获基线与 dependency spike；
2. 领域模型、错误和严格状态机；
3. 设置、浏览器探测和模型能力；
4. 预览服务与全局热键；
5. Browser backend；
6. Windows desktop backend；
7. controller、租约、取消和无进展；
8. generic tool/result/media 链路；
9. OpenAI native Responses 链路；
10. GUI 系统设置；
11. GUI 顶部控制条和右侧轨迹；
12. CLI 状态与命令；
13. LAN 边界；
14. 打包和受控烟测；
15. 全量门禁与变更边界审计。

顺序不得把 GUI 开关先接到未完成的执行器，也不得在 native/generic 协议未被 fake provider 测试前进行真实模型烟测。

## 26. 预计文件影响范围

最终实施预计涉及但不限于：

### 新建

- `core/computer/__init__.py`

- `core/computer/models.py`

- `core/computer/settings.py`

- `core/computer/capability.py`

- `core/computer/actions.py`

- `core/computer/controller.py`

- `core/computer/browser_backend.py`

- `core/computer/win32_adapter.py`

- `core/computer/windows_backend.py`

- `core/computer/preview_server.py`

- `core/computer/hotkey.py`

- `core/computer/provider_generic.py`

- `core/computer/provider_openai.py`

- `core/computer/tool_adapter.py`

- `core/computer/cli.py`

- 对应 `tests/test_computer_*.py`

- `tests/computer_fakes.py`

- `scripts/smoke_computer_control.py`

- `frontend/src/components/ComputerControlBar.vue`

- `frontend/src/components/ComputerControlBar.test.ts`

- `frontend/src/components/ComputerControlPane.vue`

- `frontend/src/components/ComputerControlPane.test.ts`

### 修改

- `requirements.txt`

- `ycode.spec`

- `core/model_catalog.py`

- `core/model_manager.py`

- `core/tool_protocol.py`

- `core/harness/request_envelope.py`

- `core/harness/stream_assembler.py`

- `core/harness/tool_contracts.py`

- `core/harness/tool_pipeline.py`

- `core/llm.py`

- `core/command_shell.py`

- `core/lan_server.py`

- `config.py`

- `gui_bridge.py`

- `gui_main.py`

- `main.py`

- `frontend/src/types/pywebview.d.ts`

- `frontend/src/stores/appStore.ts`

- `frontend/src/components/SystemSettings.vue`

- `frontend/src/components/RightPanel.vue`

- `frontend/src/App.vue`

- 相关现有测试和 README/更新说明

实施计划必须再次核对实际行号和职责；如果实现证明无需修改某文件，应删除该计划项，而不是制造无意义改动。

## 27. 验收标准

全部满足才算完成：

1. 新安装和旧配置升级后两个开关均为关闭。
2. 关闭时现有 Provider golden、prompt hash、tools schema 和全量测试全部通过且无需更新 expected。
3. Edge、Chrome 均使用各自 Y-CODE 专用 profile，登录态可持久化，不访问日常 profile。
4. browser-only 模式不能越权操作桌面；desktop 模式能按统一动作操作 Windows 当前桌面。
5. generic 支持明确的 DeepSeek/Kimi/GLM 视觉工具模型；不支持模型在请求前明确拒绝。
6. OpenAI native 使用真实 computer tool、computer call output 和 response continuation；协议失败不 fallback。
7. GUI 顶部控制条和右侧操控页签符合批准方案，暂停和停止始终可用。
8. CLI 能 status/pause/resume/stop，Ctrl+C 和全局热键能停止。
9. 设置关闭、Turn 取消、浏览器关闭、锁屏、安全桌面、无进展、Step 上限和应用退出均安全停止。
10. 同时只存在一个 run，竞态测试通过。
11. 输入文本、Cookie、凭据、response id、截图 base64 不进入日志或 Harness Journal。
12. GUI JS 调度、prompt\_toolkit 线程、SQLite 事务和现有取消铁律未被破坏。
13. 每个 Task 有真实 RED → GREEN 记录，所有阶段回归和最终门禁退出码为 0。
14. 打包版无需下载 Chromium 即可使用已安装 Edge/Chrome；依赖缺失时其他功能仍正常，电脑操控明确不可用。
15. 没有 Critical 或 Important 遗留问题，没有临时测试 profile、截图、进程或意外工作树文件。

## 28. 主要风险及控制

### Playwright 打包资源

风险：源码环境通过而 PyInstaller 找不到 driver。控制：第一阶段先做 dependency spike，最后必须执行打包 smoke；失败时不继续接入业务代码。

### Provider 协议差异

风险：把 native 和 generic 混在一个消息格式里。控制：两个协议适配器、独立 golden、禁止 fallback、共享的只有动作和观察。

### 截图导致上下文和 DB 膨胀

风险：每一步都是图片。控制：generic 复用 DB 媒体折叠，只保留最新有效画面；native 使用 continuation 且不持久化 base64；预览临时文件最多 3 张。

### 鼠标键盘竞态

风险：多个会话、停止时按键未释放。控制：进程级排他租约、单动作串行、finally 释放、设置 generation、全局停止。

### 默认功能回归

风险：tools 或 prompt 改变导致 DeepSeek/Kimi 缓存失效。控制：默认关闭路径零字节变化、原 golden 不修改、feature-on 新增独立快照。

### 远程客户端扩大权限

风险：LAN token 持有者开启或启动本机电脑操控。控制：后端 turn source、RPC 黑名单、只允许 status/stop。

## 29. 对后续 K3 实施的约束

后续给 K3 的执行提示词必须要求它：

1. 完整读取 `AGENTS.md`、本规格和实施计划；
2. 保持 `dev` 分支，不擅自 merge、push 或发布；
3. 严格按 Task 顺序，不并行修改共享 Harness/LLM/GUI 文件；
4. 每个 Task 先 RED、再最小 GREEN、再关联回归；
5. 记录每条真实命令、退出码、测试数量和耗时；
6. 不更新现有 golden/hash 来迁就实现；
7. 不增加 fallback、自动换协议/浏览器、吞异常或启发式补救；
8. 发现代码现状与计划不一致时先取证并做最小调整，架构变化必须停下请求用户确认；
9. 不调用真实付费 Provider、不移动真实鼠标键盘、不访问日常浏览器 profile，除非用户明确进入受控人工烟测；
10. 每个阶段结束检查 diff 范围，最终运行全量门禁；
11. 最终报告逐 Task RED/GREEN、全部回归、字节稳定、打包、人工烟测、剩余风险和是否达到可提交状态；
12. 达到停止条件后停止，不继续开放式重构或找无关问题。

