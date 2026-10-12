# config.toml 只读兼容层设计

- 日期：2026-08-28
- 范围：`config.py`（配置读取/写入模块）
- 状态：已确认采用

## 1. 背景与动机

用户希望 Y-code 像 Codex / Kimi CLI 一样支持读取 TOML 格式的配置文件（`~/.ycode/config.toml`），以便沿用社区通用的 TOML 配置习惯，或从其他工具迁移部分配置片段。

经与用户确认，本次采取**只读兼容**方案：TOML 仅作为可选的读取源覆盖已有配置，**配置的持久化写回仍然只使用 JSON**（`config.json`）。也就是说 TOML 是"叠加层（overlay）"，JSON 仍是唯一的事实存储格式；不引入任何 TOML 写出、迁移或双向同步逻辑。

## 2. 目标 / 非目标

### 目标

1. **读取生效**：读取 `~/.ycode/config.toml`（由 `YCODE_DATA_DIR` 决定的 `YCODE_DIR` 目录下）与 `config.json` 深合并后生效，TOML 键优先级高于 JSON。
2. **JSON 永远唯一写回格式**：`update_config` / `save_config_value` 等写路径仍只写 `config.json`，绝不生成或改写 TOML。
3. **无 TOML 时 100% 现状**：不存在 `config.toml` 时行为与改动前完全一致（回归零风险）。
4. **零依赖**：使用 Python 3.11+ 标准库 `tomllib` 解析，不引入第三方依赖；Python < 3.11 环境优雅降级为忽略 TOML。

### 非目标

1. 不写 TOML：不提供任何把配置导出/写回 TOML 的能力。
2. 不迁移配置：不做 JSON → TOML 的一次性迁移工具。
3. 不处理 `mcp.toml` / `active_skills.toml`：仅覆盖 `config.toml` 这一个文件。
4. 不做 include 语法：不支持 TOML 内引用其他配置文件的语法糖。

## 3. 架构

改动全部集中在 `config.py`，不触碰其他模块（尤其不改 `core/mcp_client.py`）。

### 新增常量（路径常量区）

放在 `YCODE_DIR` 之后的路径常量区（`MCP_FILE` 附近）：

```python
_TOML_CONFIG_FILE = os.path.join(YCODE_DIR, "config.toml")
_TOML_CACHE: dict[str, dict] = {}
```

### 新增函数（`_load()` 之前）

**`_read_toml_overlay(path)`** —— 读取 TOML overlay：

- 文件不存在（`OSError`，含 `FileNotFoundError`）→ 返回 `None`，主流程不受影响。
- 用 `tomllib.load(二进制文件对象)` 解析。
- `tomllib.TOMLDecodeError` / `OSError` → `_log_warning` 记录警告后返回 `None`（容错，绝不崩溃）。
- 顶层不是 TOML 表（非 dict）→ 警告并返回 `None`。
- 独立的 `_TOML_CACHE` 做 mtime 缓存（与 `_CONFIG_CACHE` 结构一致），命中时复用 `_copy_cached` 返回浅拷贝，避免调用方原地修改污染缓存。

**`_deep_merge(base, overlay)`** —— 深合并，overlay 优先：

- 两边都是 dict → 递归合并，返回新对象，不污染入参。
- TOML 胜出：同名键以 overlay 的值为准。
- list / 标量：整体替换，不做元素级合并。

### `_load()` 返回合并视图

```python
def _load():
    data = _read_json_with_backup(_CONFIG_FILE, default=None)
    result = data if isinstance(data, dict) else {}
    overlay = _read_toml_overlay(_TOML_CONFIG_FILE)
    if overlay:
        result = _deep_merge(result, overlay)
    return result
```

`_load()` 是所有读取路径（含 `__getattr__` 动态常量、`update_config`、`save_config_value`）的单一入口，因此合并视图对全模块透明生效。

### DPAPI 路径判断

`_prepare_for_storage` / `_restore_after_read` / `_read_json_with_backup` 内部的 DPAPI 加解密路径判断均按 `os.path.abspath(path) == os.path.abspath(_CONFIG_FILE)` 只命中 `config.json`。TOML 文件从不进入这些函数（`_read_toml_overlay` 自行读取），天然不参与 DPAPI 加解密，无冲突。

### 优先级

最终生效优先级（高 → 低）：

```
命令行参数 > config.toml > config.json > 内置默认
```

## 4. 数据流与已知取舍

### 读路径

`_load()` 在内存中把 JSON 与 TOML 深合并，返回合并视图，**不落盘**。每次读配置都实时计算，多窗口/高频刷新场景由 mtime 缓存兜底性能。

### 写路径

`update_config` / `save_config_value` 仍然：`_load()`（拿到合并视图）→ mutator 修改 → `_atomic_write_json(_CONFIG_FILE, ...)` 只写 `config.json`。写出的 JSON 内容是"当时合并视图 + 本次修改"的快照。

### 已知取舍：TOML 值固化

由于写回会把合并结果（含 TOML 覆盖值）固化进 `config.json`，一旦发生任意一次写操作：

- TOML 中的值被"烤"进了 JSON；
- 之后**删除 `config.toml` 不会回退**到旧 JSON 值——因为 JSON 已被更新为 TOML 的值。

恢复方法：

1. 手动编辑 `config.json` 改回想要的值；或
2. 删除 `config.json` 中对应键后，通过 `/config` 重新设置。

该取舍是只读兼容方案的固有语义，已向用户明示并被接受。

## 5. 错误处理

| 场景 | 行为 |
| --- | --- |
| `config.toml` 语法错误（`TOMLDecodeError`） | `_log_warning` 警告，本次忽略 overlay，回退纯 JSON，不崩溃 |
| `config.toml` 读取失败（`OSError`，含权限/占用） | 同上：警告 + 忽略 overlay，回退纯 JSON |
| `config.toml` 顶层不是 TOML 表 | 警告 + 忽略该文件 |
| TOML 与 JSON 键冲突 | TOML 胜出（深合并 overlay 优先） |
| Python < 3.11（无 `tomllib`） | import 降级为 `tomllib = None`，overlay 恒为 `None`，等同无 TOML |
| JSON 本身损坏 | 沿用现有 `.bak` 自愈逻辑，不变 |

原则：TOML 读取链路上的任何失败都只表现为"忽略 overlay"，绝不影响主配置加载流程。

## 6. 测试清单

新增 `tests/test_config_toml_overlay.py`，至少覆盖以下 7 条：

1. **无 TOML 回归**：tmp 目录只有 `config.json` 时，`config._load()` 返回值与改动前语义一致（JSON 原值），行为不回归。
2. **覆盖顶层标量**：TOML 中设置顶层标量键（如 `theme = "toml-value"`），合并后取 TOML 值。
3. **嵌套 dict 深合并**：JSON 与 TOML 各有同名嵌套表，TOML 只覆盖其中一个子键，其余子键保留 JSON 原值（证明是递归深合并而非整体替换）。
4. **list 整体替换**：TOML 中的 list 键整体替换 JSON 中的同名 list，不做元素级拼接。
5. **TOML 语法错误回退**：`config.toml` 内容非法时，不抛异常，返回 JSON 原值。
6. **`update_config` 写回落 JSON**：存在 TOML overlay 时执行 `update_config`，写回的 `config.json` 文件内容包含合并后的值，且文件格式为合法 JSON。
7. **`YCODE_DATA_DIR` 隔离**：测试环境下 `config.YCODE_DIR` 指向 `YCODE_DATA_DIR` 隔离目录，不触碰真实用户目录。

隔离策略：用例通过 monkeypatch 把 `config._CONFIG_FILE` 与 `config._TOML_CONFIG_FILE` 指向 pytest `tmp_path`，并在用例开头 `dict.clear()` 清空 `config._CONFIG_CACHE` 与 `config._TOML_CACHE`，避免 mtime 缓存串扰；不构造 `providers` 数组，避开 DPAPI 加解密路径。

## 7. 实现位置

全部改动位于 `config.py`：

1. **import 区**：`try: import tomllib / except ImportError: tomllib = None`（Python < 3.11 优雅降级）。
2. **路径常量区**（`MCP_FILE` 行附近）：新增 `_TOML_CONFIG_FILE` 与 `_TOML_CACHE`。
3. **`_load()` 之前**：插入 `_deep_merge` 与 `_read_toml_overlay` 两个函数。
4. **`_load()`**：整体替换为"读 JSON → 读 overlay → 深合并"三步。

其余函数（`_read_json_with_backup` / `_atomic_write_json` / `update_config` / `save_config_value` / `__getattr__` 等）语义一律不变。

## 8. 风险与缓解

| 风险 | 等级 | 缓解 |
| --- | --- | --- |
| TOML 值经一次写操作后固化进 JSON，删 TOML 不回退 | 中 | 已在第 4 节明示语义与恢复方法（手动改 JSON 或删键后 `/config` 重设）；文档同步告知用户 |
| 并发写 `config.toml`（外部工具同时改）读到半成品 | 低 | TOML 只读且逐次解析容错，解析失败自动回退纯 JSON；无需加锁 |
| mtime 缓存串扰（测试/多窗口） | 低 | `_TOML_CACHE` 独立于 `_CONFIG_CACHE`，键为绝对路径，命中前校验 mtime |
| Python < 3.11 环境无 `tomllib` | 低 | import 降级为 `None`，overlay 恒为空，等同现状 |

## 9. 确认

本设计已由用户于 2026-08-28 确认采用只读兼容方案。
