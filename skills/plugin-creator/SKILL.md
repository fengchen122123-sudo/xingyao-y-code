---
name: plugin-creator
description: 为 星瑶 开发并安装插件（Plugin）的标准流程。当用户要求"做一个插件"、"写一个 星瑶 插件"、"给 星瑶 加一个新工具"、"自己实现并安装插件"时使用。插件是 Python 代码包（plugin.json + plugin.py），直接注册新的 Agent 工具，与技能（SKILL.md 纯提示词）和 MCP（外部进程）是三套不同机制。
triggers: 做插件,写插件,开发插件,安装插件,新工具,星瑶插件,plugin
---

# 星瑶 插件开发技能

插件 = 装进 星瑶 进程的 Python 代码包，加载后其 `register(registry)` 被调用，
工具直接进入统一工具注册表（自动获得权限拦截、参数过滤、结果截断）。

## 一、安装即写文件

没有独立安装器。**把符合规范的目录写入插件目录即完成安装**：

```
~/.ycode/plugins/<插件名>/
    plugin.json   # 必需：元数据
    plugin.py     # 默认入口（plugin.json 的 entry 字段可改名）
    helper.py     # 可选：兄弟模块
```

- 用户目录 `~/.ycode/plugins/` 优先；同名插件会覆盖内置目录的同名插件。
- 每个插件必须是**独立子目录**（文件直接丢在 plugins/ 根下不会被扫描）。
- 装完**立即生效**：星瑶 会热加载新插件（旧版本需重启）。验证见第四节。

## 二、plugin.json 规范

```json
{
  "name": "my_plugin",
  "version": "1.0.0",
  "description": "一句话说明这个插件提供什么工具",
  "entry": "plugin.py",
  "enabled": true
}
```

- `name` 缺省取目录名；`entry` 缺省 `plugin.py`；`enabled: false` 表示安装但禁用。
- 缺 `plugin.json` 的目录会被**静默跳过**（装了也不显示的常见原因）。

## 三、plugin.py 模板

入口模块**必须暴露 `register(registry)` 函数**，否则加载失败（status=failed）。

```python
"""my_plugin：示例插件，提供一个 hello 工具。"""

from core.tool_protocol import ToolSpec


def _hello(name: str = "") -> str:
    """工具执行体：返回 str 或 ToolResult。异常会被框架捕获并标记为错误。"""
    return f"hello {name}"


def register(registry):
    registry.register(ToolSpec(
        name="my_hello",                # 工具名：全局唯一，带插件前缀避免冲突
        description="向指定名字打招呼",  # 模型靠它决定何时调用，写清楚
        input_schema={                  # JSON Schema，框架会按它过滤非法参数
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要打招呼的名字"}
            },
        },
        execute=_hello,
        read_only=True,                 # 只读工具置 True（不修改任何状态）
        mutates_files=False,            # 会写/改文件置 True
        requires_permission=False,      # 只读工具通常 False；写操作必须 True
        activity_description="打招呼中",
    ))
```

ToolSpec 关键字段（都有默认值，按工具性质正确设置）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `read_only` | False | 纯查询/读取置 True |
| `mutates_files` | False | 会修改文件系统置 True |
| `requires_permission` | True | 写操作/执行命令保持 True（走用户审批）；只读可 False |
| `cancellable` | True | 长任务保持 True 允许用户中断 |
| `parallel_safe` | False | 可与其他工具并行执行才置 True |

**兄弟模块导入**（插件目录内多个 .py 时）必须用 `import_sibling`，
**不要**用普通 `import helper`（不同插件的同名模块会在 sys.modules 里互相覆盖）：

```python
from core.plugin_loader import import_sibling
helper = import_sibling(__file__, "helper")   # 加载同目录 helper.py
```

## 四、安装后自检（必须做）

写完文件后**必须验证加载结果**，不要直接宣称成功：

1. 让用户打开 星瑶 侧边栏"插件"页查看；或运行 CLI `/plugins` 命令；
2. 确认状态为 **loaded** 且工具列表包含你的工具名；
3. 若为 **failed**：检查入口文件是否存在、`register` 是否暴露、import 是否报错
   （插件页会显示 error 详情）；修复后重新触发热加载（切换插件开关或重启）；
4. 若列表里**根本没有**：检查是否在 plugins/ 下的独立子目录、`plugin.json` 是否存在且为合法 JSON。

## 五、安全红线

- **插件代码在 星瑶 进程内直接执行**，能力等同于主程序本身。
- 安装/启用**第三方来源**的插件前，必须把完整代码展示给用户并获得明确同意。
- 写操作类工具必须 `requires_permission=True`，不得为了"用着方便"关掉审批。
- 插件不显示、加载失败时如实报告原因，禁止假装成功。

## 六、参考实现

- 框架源码：`core/plugin_loader.py`（docstring 有完整约定）
- 可运行的最小示例：本机 `~/.ycode/plugins/` 下已有插件的目录结构
