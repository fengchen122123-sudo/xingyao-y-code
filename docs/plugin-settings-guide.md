# 插件设置页开发说明

面向**第三方插件作者**：只要你的插件实现一个 `plugin_settings(payload)` 函数，星瑶设置页的"插件"标签就会自动出现你的设置界面——不需要写任何前端代码、不需要注册、不需要改星瑶源码。

> 本文件只描述**通用能力**。星瑶仓库不包含任何具体插件代码；插件资产全部放在你自己的目录里（默认 `~/.ycode/plugins/<插件名>/`），开发、分发、更新完全由插件作者自理。

---

## 1. 机制一句话

插件入口模块实现可选函数：

```python
def plugin_settings(payload: dict) -> dict
```

星瑶设置页打开"插件"标签时，会对**已加载**的插件调用它（`payload = {"action": "render"}`），把你的返回值（"界面快照"）用星瑶统一风格渲染出来；用户在页面上的每次点击，都会以 `{"action": ..., "params": {...}}` 的形式再次回调这个函数。

- 插件没实现该函数 → 设置页显示"该插件暂不可配置"，其他功能不受影响。
- 插件没**加载**（未信任 / 未启用 / 加载失败）→ 星瑶**绝不执行**插件代码，只显示状态引导（"信任并启用" / "启用该插件"）。
- 插件安装 → 条目出现；插件目录删除 → 条目消失。**零注册、零持久化注册表**。

---

## 2. 最小可跑示例

### 2.1 目录结构

```
~/.ycode/plugins/hello_settings/     # Windows: C:\Users\<你>\.ycode\plugins\hello_settings\
├── plugin.json
└── plugin.py
```

### 2.2 `plugin.json`

```json
{
  "name": "hello_settings",
  "version": "1.0.0",
  "description": "最小插件设置页示例",
  "entry": "plugin.py",
  "enabled": true
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 否 | 插件名（设置页显示、同名去重）。缺省取目录名 |
| `entry` | 否 | 入口文件名，默认 `plugin.py` |
| `version` | 否 | 版本号，设置页可读到（缺省 `?`） |
| `description` | 否 | 一句话描述 |
| `enabled` | 否 | 默认 `true`；显式 `false` 表示"装了但先不启用" |

### 2.3 `plugin.py`

```python
"""最小示例：一个只有设置页、不注册任何工具的插件。

协议要点：
- 入口必须暴露 register(registry)（可以不注册任何工具，留空实现即可）；
- 设置页实现 plugin_settings(payload) -> dict（见下文 envelope 约定）；
- 可选实现 cleanup()：插件被禁用或目录被删除时由星瑶调用，用来回收自己写的持久化数据。
"""

import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_STATE = _HERE / "state.json"          # 插件自己的数据，放在插件目录内


def _load() -> dict:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(state: dict) -> None:
    _STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def register(registry):
    """星瑶加载插件时调用；本插件不提供工具。"""
    return None


def cleanup() -> None:
    """插件被禁用 / 目录被删除时调用：清掉自己写下的持久化数据。"""
    try:
        _STATE.unlink()
    except FileNotFoundError:
        pass


def _render(state: dict) -> dict:
    token = str(state.get("token") or "")
    return {
        "title": "Hello 设置页",
        "description": "最小可跑示例",
        "sections": [
            {"type": "info", "items": [
                {"label": "令牌状态", "value": "已配置" if token else "未配置",
                 "badge": "就绪" if token else "待配置",
                 "badge_tone": "ok" if token else "warn"},
            ]},
            {"type": "text", "content": "令牌只保存在插件自己的目录里，不会写进星瑶日志。"},
            {"type": "form",
             "fields": [
                 {"type": "password", "key": "token", "label": "访问令牌",
                  "value": token, "placeholder": "粘贴令牌"},
                 {"type": "switch", "key": "notify", "label": "保存后弹提示",
                  "value": bool(state.get("notify"))},
             ],
             "submit": {"label": "保存", "action": "save"}},
            {"type": "actions", "items": [
                {"label": "清除本地数据", "action": "clear", "style": "danger"},
            ]},
        ],
    }


def plugin_settings(payload):
    action = str((payload or {}).get("action") or "render")
    params = (payload or {}).get("params") or {}
    state = _load()

    if action == "render":
        return {"ok": True, "page": _render(state)}

    if action == "save":
        # form 提交固定形如 params = {"values": {字段 key: 值}}
        values = params.get("values") or {}
        state["token"] = str(values.get("token") or "").strip()
        state["notify"] = bool(values.get("notify"))
        _save(state)
        return {"ok": True, "message": "已保存", "page": _render(state)}

    if action == "clear":
        state.pop("token", None)
        _save(state)
        return {"ok": True, "message": "已清除本地数据", "page": _render(state)}

    return {"ok": False, "message": f"未知动作: {action}"}
```

### 2.4 跑起来 / 自检

1. 把上面的目录放进 `~/.ycode/plugins/hello_settings/`；
2. 重启星瑶（或打开设置页点右上角刷新）→ 左侧"插件"标签里出现 `hello_settings`；
3. 首次使用需**信任**：点条目 → 界面显示"信任并启用" → 确认后即可看到你的设置页。

不想开星瑶、只想在命令行里快速验证钩子（用环境变量 `YCODE_DATA_DIR` 把数据目录指到隔离位置，避免动到真实插件）：

```powershell
$env:YCODE_DATA_DIR = "$env:TEMP\ycode-demo"     # 该目录下放 plugins\hello_settings\
venv\Scripts\python.exe -c "import core.plugin_loader as pl, core.tool_registry as tr, json; print(pl.trust_plugin('hello_settings')); pl.load_and_register(tr.build_default_registry()); print(json.dumps(pl.call_plugin_hook('hello_settings', 'plugin_settings', {'action': 'render'}), ensure_ascii=False))"
```

`call_plugin_hook` 的返回固定是 `{"ok": True, "data": <你的钩子返回值>}` 或 `{"ok": False, "message": ..., "needs": ...}`。

---

## 3. 界面快照（`page`）schema

`page` 是插件声明的**数据**，渲染完全交给星瑶——你描述"有哪些信息、表单和按钮"，不写 HTML/CSS/JS。

```json
{
  "title": "WorkBuddy",
  "description": "可选的一句话",
  "sections": [ ... ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `title` | string | 页面标题；缺省用插件名 |
| `description` | string | 标题下的补充说明，可选 |
| `sections` | array | 有序的区块列表，见下 |

### 3.1 section 类型总览

| `type` | 用途 | 关键字段 |
| --- | --- | --- |
| `info` | 只读键值行（状态、账号、额度等） | `items[]`：`label` / `value` / `badge` / `badge_tone`，可带 `title` |
| `text` | 一段纯文本说明 | `content` |
| `form` | 输入表单 + 提交按钮 | `fields[]` / `submit` |
| `list` | 条目列表（每行可带按钮） | `items[]`：`title` / `subtitle` / `badge` / `badge_tone` / `actions[]` |
| `actions` | 一排动作按钮 | `items[]`：`label` / `action` / `params` / `style` |

> **向前兼容**：星瑶版本的控件集是 v1。出现它不认识的 `section.type` 或字段类型时会被**忽略**（不报错），所以请只用本文列出的类型；需要新控件请提需求，别指望"未知类型自动降级成文本"。

**`badge_tone`**（徽标配色）：`ok`（绿，正常）/ `warn`（黄，待处理）/ `err`（红，异常）/ `muted`（灰，中性，默认）。

### 3.2 `info`

```json
{"type": "info", "title": "账号", "items": [
  {"label": "当前账号", "value": "木偶", "badge": "使用中", "badge_tone": "ok"}
]}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `label` | string | 是 | 左侧标签 |
| `value` | string | 是 | 右侧值（请自行格式化成字符串） |
| `badge` | string | 否 | 徽标文字；不填则不显示徽标 |
| `badge_tone` | string | 否 | 见上，默认 `muted` |

### 3.3 `text`

```json
{"type": "text", "content": "第一行\n第二行"}
```

### 3.4 `form`

```json
{"type": "form",
 "fields": [
   {"type": "text", "key": "endpoint", "label": "接口地址", "value": "", "placeholder": "https://..."},
   {"type": "password", "key": "token", "label": "令牌", "value": ""},
   {"type": "switch", "key": "notify", "label": "完成后弹提示", "value": true},
   {"type": "select", "key": "realm", "label": "版本", "value": "cn",
    "options": [{"value": "cn", "label": "国内版"}, {"value": "global", "label": "国际版"}]}
 ],
 "submit": {"label": "保存", "action": "save_form"}}
```

| 字段属性 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | `text` / `password` / `switch` / `select` |
| `key` | string | 是 | 提交时的键名，同一 form 内唯一 |
| `label` | string | 是 | 显示标签 |
| `value` | 任意 | 否 | **初始值**（每次渲染都会重置输入框为该值，故请在 render 里回填当前值） |
| `placeholder` | string | 否 | 仅 `text` / `password` |
| `options` | array | `select` 必填 | `[{"value": "...", "label": "..."}]` |

`submit`：`{"label": "保存", "action": "save_form"}`。点击后星瑶回调你的钩子：

```python
{"action": "save_form", "params": {"values": {"endpoint": "...", "token": "...", "notify": True, "realm": "cn"}}}
```

要点：

- `values` 里的键就是各个 field 的 `key`；`switch` 是布尔值，其余是字符串。
- 提交后**是否关闭/刷新页面由你决定**：返回 `page` 就整页重渲染（推荐，让界面立刻反映新状态），返回 `message` 就只弹提示。
- 表单没有"校验失败自动回填"机制：校验不通过就直接返回 `{"ok": False, "message": "..."}`，并用 `page` 把你想要的界面重新渲染出去。

### 3.5 `list`

```json
{"type": "list", "items": [
  {"title": "木偶", "subtitle": "uid 123 · 国内版", "badge": "使用中", "badge_tone": "ok",
   "actions": [
     {"label": "切换", "action": "switch_account", "params": {"uid": "123"}, "style": "default"},
     {"label": "删除", "action": "remove_account", "params": {"uid": "123"}, "style": "danger"}
   ]}
]}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `title` | string | 是 | 主标题 |
| `subtitle` | string | 否 | 副标题（小字） |
| `badge` / `badge_tone` | string | 否 | 同行右侧徽标 |
| `actions` | array | 否 | 每行的按钮，见 3.6 |

### 3.6 `actions` 与按钮

```json
{"type": "actions", "items": [
  {"label": "添加账号", "action": "add_account", "params": {"realm": "cn"}, "style": "primary"},
  {"label": "刷新状态", "action": "refresh", "style": "ghost"}
]}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `label` | string | 是 | 按钮文字 |
| `action` | string | 是 | 回调的动作名（`payload["action"]`） |
| `params` | object | 否 | 原样透传到 `payload["params"]`（默认为 `{}`） |
| `style` | string | 否 | `default` / `primary` / `danger` / `ghost` |

- `danger` 按钮：星瑶会先弹一个确认框，用户确认后才回调你——**别拿它做不可逆的批量删除**（仍然要做自己的二次校验）。
- 按钮点击期间星瑶会短暂禁用（防连点）；钩子里别做长耗时操作。
- 同一动作名在不同位置出现是允许的，靠 `params` 区分。

---

## 4. 动作与异步

### 4.1 调用约定

| 场景 | 星瑶发来的 `payload` |
| --- | --- |
| 打开设置页 | `{"action": "render"}` |
| 点按钮 | `{"action": "<按钮的 action>", "params": <按钮的 params 或 {}>}` |
| 提交表单 | `{"action": "<submit.action>", "params": {"values": {key: 值}}}` |
| 轮询（见 4.3） | `{"action": "<poll.action>", "params": <poll.params>}` |

### 4.2 返回 envelope

必须返回 `dict`（**返回非 dict 会被判为失败**），字段除 `ok` 外全部可选：

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `ok` | bool | 动作是否成功；`false` 时星瑶弹 `message` 且不做其他处理 |
| `message` | string | 给用户看的一句话（成功/失败提示） |
| `page` | object | 新的界面快照 → **整页重渲染**（界面即真相，不要自己维护前端状态） |
| `auth_url` | string | 需要用户在浏览器完成的操作（OAuth 等）→ 渲染"授权链接 + 在浏览器中打开授权页 / 复制链接" |
| `poll` | object | 启动自动轮询，见 4.3 |
| `done` | bool | 与 `poll` 配对：轮询动作返回 `done: true` 表示流程结束（前端停止轮询并收起授权链接） |

要点：

- `render` 动作**必须**在 `page` 里返回快照；不返回 `page`（或不是 dict）时设置页显示"插件未返回有效的设置页内容"。
- 钩子里的任何异常都会被星瑶捕获成 `{"ok": False, "message": "插件 X 的 plugin_settings 执行失败: ..."}`，不会崩掉星瑶——但用户看到的就是这句报错，请自己兜住异常并给中文提示。
- 钩子内不要阻塞超过约 3 秒（星瑶界面等待中）；长流程一律拆成"启动 + 轮询"。

### 4.3 轮询（`poll`）约定

```json
{"ok": True, "auth_url": "https://...", "message": "请在浏览器完成登录",
 "poll": {"action": "poll_login", "params": {"state": "abc"}, "interval_ms": 3000}}
```

前端行为（v1，已实现）：

- 收到 `poll` 就按 `interval_ms` 定时回调 `poll.action`（带 `poll.params`），每次都用返回的 envelope 继续处理（`page` 重渲染、`message` 提示等）。
- 间隔下限 **1500ms**（写更小会被抬到 1500），缺省 **3000ms**。
- 结束条件：某次轮询返回 `done: true`、用户离开该插件的设置页、或达到上限 **200 次 tick**（3 秒间隔约 **10 分钟**）——超时会提示"等待超时或已切换插件，已停止自动刷新"。
- 每次轮询回调前若用户点了别的按钮，轮询会被打断（按钮/manual 动作优先）。
- 因此轮询动作要**幂等且无副作用累加**：每 tick 只做一次"查询远端状态"，超时未完成就继续返回 `{"ok": True, "done": false}`（可带 `message` 但不建议每 tick 都弹）。

---

## 5. 安全边界与工程约定

1. **未信任 / 未启用不可执行**：只有"已加载（= 已信任 + 已启用）"的插件才会被调用。未加载时星瑶返回 `needs: "trust" | "enable"`，设置页改成引导按钮（"信任并启用" / "启用该插件"）——**这是硬边界，插件写什么都绕不过**。
2. **首次使用需信任**：用户目录插件默认不可信。信任按**内容指纹**记录（`plugin_trust.json`）；你改了插件代码，用户需重新确认一次。
3. **凭证自管**：令牌、私钥等只落插件自己的目录（插件目录内，或用环境变量 `YCODE_DATA_DIR` 定位的数据目录），**绝不写日志、绝不打进 `message`**——`message` 会直接显示给用户。
4. **`cleanup()` 回收**：插件被禁用或目录被删除时星瑶会调用它。你自己创建的持久化数据、后台线程、临时文件都在这里收掉；`cleanup` 抛异常会被吞掉并记日志（不会阻塞用户操作），但残留数据会留下来。
5. **`danger` 按钮前端会确认**，但这只是 UI 兜底；真正的删除/注销动作请在自己的钩子里做参数校验（例如参与文件路径的 uid 要按白名单 `[A-Za-z0-9_-]` 过滤，防路径穿越）。
6. **授权链接走系统浏览器**：星瑶只允许用默认浏览器打开 `http/https` 链接（`open_external_url`），不内嵌登录页——不要要求用户在你的界面里输 OAuth 密码。
7. **只用标准库 + 你自己的依赖**（服从星瑶既有插件约定），注释与用户可见文案用中文；兄弟模块用 `core.plugin_loader.import_sibling(__file__, "wb_login")` 这类方式导入，模块名带插件目录前缀，避免与其他插件冲突。
8. 钩子名固定 `plugin_settings`；`render` 是保留动作名（打开页面时由星瑶触发），别把它当业务动作用。

---

## 6. 安装、信任与删除

| 事项 | 说明 |
| --- | --- |
| 安装位置 | `~/.ycode/plugins/<插件名>/`（Windows：`C:\Users\<你>\.ycode\plugins\<插件名>\`）。目录名即缺省插件名 |
| 数据目录 | 默认 `~/.ycode`；可用环境变量 `YCODE_DATA_DIR` 覆盖（调试、隔离测试时用） |
| 扫描顺序 | 用户目录优先于内置目录（仓库 `plugins/`）；同名时用户目录的胜出，内置的记为 `overridden` |
| 生效方式 | 打开设置页 / 点刷新即增量扫盘，**安装即出现，不需要重启、不需要注册** |
| 首次信任 | 插件未信任 → 条目显示"未信任" + "信任并启用" 按钮（确认弹窗后写入指纹） |
| 修改代码 | 指纹变化 → 回到"未信任"状态，需重新确认；已加载的旧代码会继续跑到刷新/重启 |
| 删除 | 删掉整个插件目录 → 设置页刷新后条目消失；**已加载**的插件会被完整卸载（注销它注册的工具 + 调用 `cleanup()` + 清掉模块引用），无需重启 |
| 禁用 | 改用"启用/禁用"开关或把 `plugin.json` 的 `enabled` 设为 `false`；禁用时会走 `cleanup()` 回收 |

---

## 附：常见问题

- **设置页显示"该插件暂不可配置"**：插件没实现 `plugin_settings`，或钩子抛异常/返回非 dict。看星瑶日志里 `插件 <名> 的 plugin_settings ...` 的报错。
- **点了按钮没反应**：星瑶在按钮回调期间会禁用按钮，且 `params` 必须是自己声明的动作名；确认前端请求真的到了你的钩子（可临时 `message` 回显）。
- **表单值没变**：`value` 只是初始值，每次渲染都会重置为它返回的值；请在 `render` 时读盘回填当前状态。
- **改了代码界面没变**：指纹变化后插件处于未信任状态（设置页会提示）；重新信任并启用即可。
- **轮询一直转**：检查轮询动作是否返回 `done: true`；上限 200 次约 10 分钟后自动停止。
