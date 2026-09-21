# 电脑操控元素定位修复工作包

状态：已完成（E1~E4 全部实施；自动项经 2026-09-21 独立复核核验通过，人工验收场景待用户执行——见 03-验收清单与实施进度）
设计日期：2026-09-21
触发原因：用户实测反馈「操作经常找不到要点击的位置」，而 ZCode 定位准确。

## 一句话根因

**浏览器的 `observe` 只填 `png_path` 不填 `elements`，导致浏览器场景永远走「截图 → 看图猜坐标」路径；而桌面场景走的是「元素列表 → click_element」的 a11y-first 路径。**

元素定位的全部基础设施**已经存在**（UIA 收集、ref 生成、文本投影、click_element 动作），**只差把浏览器接上同一条通路**。

## 证据链

### 证据 1：元素投影通路已完整存在

`core/computer/provider_generic.py:203` 的 `_observation_followup_message`：

```python
if observation.elements:
    # 走纯文本控件列表路径（a11y-first，请求不含截图字节）
    lines = [f"- {e['ref']} {e['kind']} name={e['name']!r} ..." for e in observation.elements]
    return {"role": "user", "content": [{"type": "text", "text": ...}, ...]}
# 否则走 PNG base64 截图路径
png_bytes = _read_observation_png(observation.png_path)
data_url = "data:image/png;base64," + ...
```

投影文本前缀（逐字）：
> 【电脑观察·控件列表】请优先用元素引用动作操作（click_element 点击 / set_value_element 写值，目标输入框优先 set_value_element）；元素都不可用时再用截图坐标。元素列表：

### 证据 2：浏览器 observe 不填 elements

`core/computer/browser_backend.py:1223` 的 `_cmd_observe`：

```python
png = page.screenshot(type="png")
...
return ComputerObservation(
    png_path=str(path),
    page_origin=origin,
    page_title=title,
    ...
)   # ← 没有 elements 参数
```

返回给模型的信息只有：**PNG + 域名 + 标题**。

### 证据 3：元素动作被限定为桌面专属

`core/computer/actions.py:47`：

```python
_BROWSER_ONLY_KINDS = frozenset({"navigate", "back", "forward", "reload"})
_DESKTOP_ONLY_KINDS = frozenset({
    "list_surfaces", "activate_surface", "invoke_surface", "observe_region",
    "list_elements", "click_element", "set_value_element",   # ← 桌面专属
})
```

### 证据 4：实测数据印证

`docs/superpowers/plans/2026-09-06-computer-control-speedup.md` 记录：

> 往返次数黑洞：逐屏滚动读消息约 45 步、**点击落空重试约 25 步**、IME 乱码绕路 8 步、RepeatCallGuard 误拦 10 次。

「点击落空重试」正是「看图猜坐标」的直接后果。

### 对照：ZCode 的做法

`packages/desktop/src/main/browserView/browserPlaywrightDomSnapshot.ts` —— 浏览器观察返回 **ARIA 文本树**，不是截图：

```
- heading "登录" [level=1]
- textbox "用户名"
- textbox "密码"
- button "提交"
```

模型读文本 → 构造 locator → 精确命中，**不需要坐标**。

## 文件说明

- [01-完整设计规格.md](01-完整设计规格.md)：根因分析、四项修复的详细设计、技术约束、风险、实施阶段，是实现的唯一需求基线。
- [02-Agent开发提示词.md](02-Agent开发提示词.md)：可直接复制给开发、评审和验收 Agent 的提示词。
- [03-验收清单.md](03-验收清单.md)：完成定义和人工验收场景。
- [实施进度.md](实施进度.md)：实施状态跟踪。

## 修复项总览

| 编号 | 项 | 优先级 | 风险 | 说明 |
|---|---|---|---|---|
| **E1** | 浏览器元素树观察 | **P0** | 中 | 核心修复：浏览器 observe 填充 elements |
| **E2** | 浏览器元素引用动作 | **P0** | 中 | click_element / set_value_element 支持 browser |
| **E3** | SoM 标注字体修复 | **P1** | 低 | 桌面截图标注字体过小，几乎免费 |
| **E4** | 提示词引导强化 | **P2** | 低 | 让能力真的被用上 |
| **E5** | 桌面 UIA 覆盖扩展 | **P3** | 高 | 自绘应用（微信等）的深层问题，工作量大 |

**E1 + E2 是核心**，两者必须一起做（只有元素列表没有点击动作 = 无用）。**E3 几乎免费**，建议同批做。

## 不可改变的产品决定

1. **复用现有通路，不新造轮子**。`ComputerObservation.elements` 字段、`_observation_followup_message` 投影、`click_element` 动作语义——这些都已存在且经过验证。浏览器侧复用同一套形状，**不引入第二套元素抽象**。

2. **ref 必须确定性**。对齐项目铁律「相同画面必须产生相同字节」（`som_annotator.py` 的确定性铁律、`provider 投影字节稳定性`约束）。同一页面状态、同一元素集合，必须产生**逐字节相同**的元素列表与 ref。

3. **ref 是观察快照，不是持久标识**。ref 生命周期与既有的 `surface_generation` 语义一致：页面切换 / 画面变化 → 旧 ref 全部失效。对齐桌面 UIA 的 `computer_element_stale` 语义，**不发明新的失效语义**。

4. **有元素就不发截图**（不加例外）。现有投影逻辑是「有 elements → 纯文本；无 elements → PNG」。浏览器接入后维持此逻辑，**不改成「元素 + 截图都给」**——那会显著增加 token 且与 ZCode 的「do not take a snapshot and screenshot together by default」原则冲突。

5. **元素不可用时回落截图**。canvas 应用、图片型界面、元素收集失败等场景，必须自然回落到现有的截图路径，**保证不回归**。

6. **不破坏现有 golden 与投影字节稳定性**。`test_prompt_byte_stability.py`、`test_provider_payload_golden.py` 的 golden 不得因本工作包改动（除非协议变更经用户明确同意并按流程重新基线）。

7. **worker 协议序列化兼容**。浏览器 observe 可能经 `browser_worker.py` 进程间传输（stdin/stdout JSON），`elements` 的 tuple 经 JSON 往返会变成 list——反序列化侧必须正确归一化。

8. **安全边界不放松**。元素列表是「可交互元素的结构信息」，**不得包含 DOM 内容值、表单填写值、Cookie、localStorage**（延续 `browser_backend.py` 的隐私铁律）。元素 `name` 只取无障碍名称（aria-label / 可见文本），**不取 value**。

9. **不引入第三方依赖**。Playwright 已在依赖中（1.62.0，支持 `aria_snapshot`）；DOM 元素提取用注入 JS 实现，不引入新库。

10. **每项独立可验收、独立可回滚**，不做 MVP 或分版交付。

## Git 分支约束

项目 `AGENTS.md` 规定日常代码拉取与提交只在 `dev` 分支进行。开发 Agent 开始修改前必须确认当前分支；若不是 `dev`，不得自行在其他分支提交，也不得擅自合并到 `master`。

实施过程必须使用持久化实施计划并按 01 规格第 7 节的阶段推进，持续更新 `待开发/电脑操控元素定位修复/实施进度.md`。
