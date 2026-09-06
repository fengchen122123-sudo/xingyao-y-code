# 电脑操控通用化重构方案（阶段三：任意软件操控）

> 日期：2026-09-06。依据：会话 147（37 分钟/3 件事）与会话 156（21 分钟/1 件事）实测，
> 单件效率负增长的根因分析；10 个开源项目调研。
> 边界：纯内核电脑操控，**不使用任何插件**（用户裁决）；微信只是测试案例，目标
> 是操控电脑上的任意软件。

## 一、调研：10 个开源项目怎么做的

| # | 项目 | Star | 许可证 | 核心机制 | 对本方案的启发 |
|---|---|---|---|---|---|
| 1 | [microsoft/UFO](https://github.com/microsoft/ufo) | 9.6k | MIT | UIA 控件框**编号标注叠加在截图上**（SoM 变体）+ 推测式多动作（官方数据 **-51% LLM 调用**） | Windows 场景最直接的对标；标注图让模型「点编号」而不是估像素 |
| 2 | [CursorTouch/Windows-MCP](https://github.com/CursorTouch/Windows-MCP) | 6.9k | MIT | Screenshot/Snapshot 双轨：控件树文本 + 元素 ID 动作，"不依赖视觉，任何 LLM 可用" | 文本观察是省时间的大头；元素 ID 是动作目标 |
| 3 | [simular-ai/Agent-S](https://github.com/simular-ai/Agent-S) | 12.2k | Apache-2.0 | 规划与 grounding 分离；grounding 收**单张标注截图**，token 恒定 | 标注图比原始截图+坐标更省更准 |
| 4 | [microsoft/OmniParser](https://github.com/microsoft/OmniParser) | 25.4k | CC-BY-4.0 | 纯视觉屏幕解析（YOLO 检测+caption），输出结构化元素列表 | UIA 不可用时的视觉兜底路线（需本地 GPU 模型，本方案不引入，留作后续） |
| 5 | [bytedance/UI-TARS-desktop](https://github.com/bytedance/UI-TARS-desktop) | 38.9k | Apache-2.0 | 纯视觉 grounding 模型；内核基于 MCP | 专用模型路线成本高，不采纳 |
| 6 | [trycua/cua](https://github.com/trycua/cua) | 22.2k | MIT | 驱动层设计目标："click, type, verify **without stealing the cursor or focus**" | 输入前置激活的反向印证：焦点稳定性是驱动层责任，不是模型责任 |
| 7 | [xlang-ai/OpenCUA](https://github.com/xlang-ai/OpenCUA) | 833 | MIT | 截图+反思式 CoT（解释上一动作/预测下一状态） | 反思信息可塞进观察投影尾部，成本极低 |
| 8 | [browser-use](https://github.com/browser-use/browser-use) | — | MIT | DOM 抽取成带索引的元素列表，模型按索引操作 | 「元素索引动作」在浏览器域已验证是正确抽象 |
| 9 | [microsoft/SoM](https://github.com/microsoft/SoM)（原论文 arXiv:2310.11441） | 2.2k | MIT | 在图上叠加编号标注，释放 VLM 的视觉 grounding | 标注图技术的源头 |
| 10 | [anthropics/computer-use-demo](https://github.com/anthropics/anthropic-quickstarts/tree/main/computer-use-demo) | — | MIT | 纯坐标方案；可靠性设计：**同轮多调用顺序执行、首错即停**；分辨率控制防漂移 | 坐标派的上限参考：即使最强视觉模型也要靠缩放控制防漂移——坐标的脆弱是共识 |

另参照：ZCode computer-use（a11y-first，30 工具，元素目标优先）、cluic/wxauto
（UIA 应用专精的可行性与版本脆弱性，其插件路线经用户裁决不采纳）。

**调研结论**：所有「快」的项目共同点只有三条——
1. **观察不是裸截图**：要么控件树文本（Windows-MCP），要么编号标注图（UFO/Agent-S/SoM）；模型引用「元素/编号」而不是估算像素。
2. **动作有确定性兜底**：激活窗口、焦点稳定、批量执行是**驱动层**职责（cua、UFO 的 automator），绝不靠模型每步自我纠正。
3. **一次往返做多件事**：推测式批量（UFO -51%）+ 首错即停（anthropic toolset）。

## 二、ycode 两次会话的病根对照

| 症状（实测） | 病根 | 对应支柱 |
|---|---|---|
| 每步请求 ~900KB（全屏截图 base64） | 观察没有文本通道 | P1 |
| 模型肉眼估坐标，窗口一挪就点空（156 会话 26 步点击循环） | 坐标靠视觉模型猜，无标注、无元素引用 | P1+P2 |
| 「第一击只聚焦不生效」反复出现 | 点击不激活目标窗口，纯 SendInput 盲发 | P3 |
| ycode/PowerShell 窗口抢焦点 → 微信失活 | 焦点稳定无人负责 | P3+P4 |
| 一次往返只做一件事 | 批量能力有但模型缺「状态漂移自检」 | P5 |
| list_elements 做了没接线（156 会话实测"桌面后端不支持 UIA"） | 生产构造缺接线 | P1 |

## 三、五大支柱设计

### P1 元素能力生产接线（地基，`create_default()`）

`WindowsComputerBackend.create_default()` 增加 `element_locator=WindowsElementLocator()`；
`WindowsElementLocator` 惰性起 COM 线程（首次 list_elements 才 start）。设置页开关沿用
`computer_control_enabled`，不加新开关。UIA 收集失败（非交互桌面/树超时）时
`list_elements` 返回稳定错误 `computer_elements_unavailable`，模型自动退回截图坐标
路径——**任何软件都能操作**：UIA 暴露充分的应用走元素，自绘/远程/游戏界面走坐标。

### P2 SoM 编号标注截图（来自 UFO/Agent-S/SoM）

`observe` 时若本次（或紧邻上一次）`list_elements` 结果仍有效：把元素 bounds 按
编号画到截图上（PIL 确定性绘制：红框 2px + 左上角黑底白字编号标签，编号即 ref
序号），观察 followup 消息变为：

```
【电脑观察·标注图】红框编号对应控件；优先 set_value_element/click_element 按 ref
操作；需坐标时点标注框中心（不是估像素）。控件表：
- el-1 EditControl name='搜索' bounds=(…) enabled=True
- el-2 ButtonControl name='发送' …
```

关键收益：模型从「看图猜 (x,y)」变成「看编号引 ref」——**坐标漂移类失败模式整类
消灭**。绘制确定性：同一画面+同一元素集 → 同一标注图字节（无时间戳）。
无元素（UIA 不可用）时自动退回普通截图，字节与现状一致。

### P3 输入前置激活（来自 cua「不抢焦点」+ 真实用户行为模型）

backend 在执行 click/double_click/drag/type_text/keypress 前：
1. 取输入目标点的所属窗口（`WindowFromPoint`，新 adapter 方法）；
2. 若非前台 → `SetForegroundWindow`（失败用经典 Alt 键技巧解除系统限制，best-effort
   不阻断）→ 等 ≤200ms 激活完成；
3. 再发输入。observation 附 `activated=bool` + 窗口标题（模型可见确认）。

确定性消灭「第一击只聚焦」：激活是 backend 的职责，不再消耗模型往返。

### P4 焦点保护与协调点

- 动态提示加一条：「出现焦点被抢/会话失活/窗口挪位时，先 `list_elements` 或
  `activate_surface` 重建基线，不要盲目按旧坐标重试」。
- **协调点（需用户拍板，涉及在途 WIP 文件 `core/command_shell.py`）**：
  `execute_command` 在电脑操控 run 活动期间以 `CREATE_NO_WINDOW` 创建进程，
  防止控制台窗口抢前台。
- **协调点（GUI）**：电脑操控 run 活动期间 ycode 主窗口不抢前台（收到
  `computer_observation` 事件时不调用激活类 JS/置顶）。

### P5 观察经济学与自愈（UFO 批量 + anthropic 首错即停 + 反思尾部）

1. `sequence` 执行后的最终观察自动升级为**标注图**（P2 路径），一次往返交付
   「N 个动作 + 一张带编号的状态图」。
2. **no-op 点击自愈**（确定性，controller 层）：click 提交后发现 screen_sha256 与
   点击前完全一致、且本步发生了前台切换 → 判定「激活吞击」，同参数自动补击一次
   并再次观察；仍无变化才把 `no_change=true` 交给模型。防重语义：仅当本步发生了
   激活切换才允许补击（不存在双发风险——激活吞击意味着第一击根本没进目标窗口）。
3. guard 拦截文案升级：附「窗口可能已移动，请 list_elements/observe 重建坐标」。
4. 观察投影尾部加一行反思提示（OpenCUA 式，零成本）：`surface_generation=N`
   变化时显式提示「画面已切换，旧坐标作废」（复用 R5 既有机制，只是让它更显性）。

## 四、不做的事（YAGNI + 用户裁决）

- 不做任何插件（用户裁决）。
- 不引入 OmniParser/grounding 模型（要本地 GPU，且 UIA+SoM 已覆盖主流场景）。
- 不压缩截图分辨率（用户已裁决保持 1920 PNG）。
- 不换纯视觉路线（UI-TARS/OpenCUA 需要专用模型）。

## 五、实施任务分解（TDD，每任务独立提交）

| 任务 | 内容 | 涉及文件 | 预期 |
|---|---|---|---|
| Task A | P1 接线：`create_default()` 挂 `WindowsElementLocator`（惰性 COM），设置不新增开关 | `windows_backend.py`、`platforms/windows.py` | `list_elements` 生产可用 |
| Task B | P2 标注渲染器：`som_annotator.py` 纯函数（元素集+PNG→标注 PNG，确定性字节）+ observe 双路径 | 新 `core/computer/som_annotator.py`、`windows_backend.py` | 标注图替代裸图 |
| Task C | P3 前置激活：adapter 增 `window_from_point`/`set_foreground_window`，backend 输入分发前置激活 | `win32_adapter.py`、`windows_backend.py` | 消灭「第一击只聚焦」 |
| Task D | P5 自愈：no-op 点击激活吞击自动补击（一次）+ guard 文案升级 | `controller.py`、`tool_pipeline.py` | 省重试往返 |
| Task E | P2+P5 sequence 终态标注图 + P4 动态提示改革（元素优先学说、焦点恢复指引） | `provider_generic.py` | 模型行为升级 |
| Task F | 协调点落地（需批准）：`CREATE_NO_WINDOW`（`command_shell.py`，用户 WIP）+ GUI 焦点礼让 | `command_shell.py`、gui 相关 | 焦点环境稳定 |
| Task G | 全量回归 + 真机验收：微信发消息复测 ≤5 分钟；记事本/计算器各一件通识任务复测 | tests + 实测 | 单件 21 分钟 → **≤5 分钟** |

验收基线（写死在计划里）：微信发消息一件 ≤5 分钟 / ≤15 往返；任意第二件同类任务
（记事本写文件）≤3 分钟。达不到就继续按会话数据迭代，不靠感觉。

## 六、预期数字（按两次会话实测外推）

| 场景 | 147 | 156 | 本方案后 |
|---|---|---|---|
| 单件耗时 | ~12 分钟 | 21 分钟（负增长） | **2~5 分钟** |
| 往返次数 | 114/3 件 | 48/1 件 | 10~15/件 |
| 单步请求 | ~900KB | ~900KB | 文本步 ~几 KB；标注图步同尺寸但命中率质变 |
| 坐标漂移重试 | ~25 步 | ~26 步 | → 0（元素/标注引用） |
| 焦点循环 | 8 步 | ~15 步 | → 0（前置激活+保护） |
