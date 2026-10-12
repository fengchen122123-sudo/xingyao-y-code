# AI 生成内容存储目录 — 设计

日期：2026-09-20
状态：待评审

## 1. 背景与问题

GUI 多会话架构用线程本地 workdir（`core/workspace_context.py`）替代进程级 `os.chdir`，
进程 cwd 永远停在启动目录——打包版即**安装目录**。受控工具（`read_file` /
`write_file` / `edit_file` / `execute_command` 等）经 `_resolve_against_workdir`
正确落到会话项目目录，但三类"自动落盘"仍直接使用进程 cwd，导致 AI 产物堆积在安装目录：

| 落盘点 | 现状取址 | 代码位置 |
|---|---|---|
| `hunyuan_generate_image` | `os.path.abspath(save_dir or ".")` → 进程 cwd | `core/hunyuan_image.py:131` |
| `tokenrhythm_generate_image` / `tokenrhythm_edit_image` | `os.path.abspath(save_dir or ".")` → 进程 cwd | `plugins/tokenrhythm_image/plugin.py:101,139` |
| `fetch_image` | `Path.cwd()` → 进程 cwd | `core/tools.py:1443` |

另有一批文件落在系统 `%TEMP%` 子目录（`ycode_paste`、`ycode_updates`、
`ycode-computer-*`），以及 AI 用 `write_file` 主动写进项目目录的探测脚本（`tmp/`、
`mytemp/` 等）。用户诉求：**给 AI 生成的内容一个统一、可配置的落点**，用户自己
提供的文件（粘贴图、手机上传、截图）不归该目录管。

## 2. 目标与非目标

**目标**

1. 设置页新增"AI 生成内容目录"配置项，默认 `~/.ycode/tmp`。
2. 三处自动落盘默认改到该目录；**显式指定位置的调用照旧尊重原参数**。
3. AI 主动写的临时脚本、中间产物也能落到该目录，且不被沙盒越界拦截。
4. AI 生成到该目录的文件**纳入按回合撤销**（撤销回合即回滚）。
5. 当前会话工作目录若已在该目录内（或为其子目录），直接放当前目录，不再套一层。

**非目标**

- 不接管用户提供的文件：`%TEMP%\ycode_paste`（粘贴/上传）、CLI 截图、
  `ycode_updates`（更新包）、`ycode-computer-*`（电脑操控 profile）保持现状。
- 不做 `write_file` 的"魔法重定向"：不按文件名/目录名特征猜测哪些是临时文件。
- 不改动 `~/.ycode/media/blobs` 永久媒体仓库语义。

## 3. 配置项

- 键名：`ai_output_dir`，持久化在 `~/.ycode/config.json`（走既有
  `config.save_config_value` / `config.update_config`，原子写 + 进程锁）。
- 默认值：`~/.ycode/tmp`（未配置、空串、`None` 均视为未配置）。
- 取值语义：
  - 支持 `~` 与 `%VAR%` / `$VAR` 展开；
  - 目录不存在则自动 `mkdir(parents=True, exist_ok=True)`；
  - 路径非法或不可写 → 回退默认目录，并写运行时告警日志（不静默丢文件）。
- 配置读取点集中在新模块，不允许各调用点自行读 config，避免行为分叉。

## 4. 解析入口：`core/ai_output_paths.py`

单模块、单职责，只暴露两个函数：

```python
def default_ai_output_dir() -> Path:
    """默认目录：<YCODE_DIR>/tmp（尊重 YCODE_DATA_DIR 覆盖）。"""

def resolve_ai_output_dir() -> Path:
    """当前生效的 AI 生成内容目录（绝对路径，保证已存在）。

    规则：
    1. 读 config.ai_output_dir；未配置 → default_ai_output_dir()
    2. 展开 ~ / 环境变量 → 规范化绝对路径
    3. 当前线程 workdir 命中该目录（相等或为其子目录）→ 直接返回 workdir
    4. mkdir(parents=True, exist_ok=True)；失败 → 回退默认目录 + 告警
    """
```

第 3 条即用户要求的"已在指定目录下则放当前目录"，用 `core.workspace_context.get_workdir()`
判定，与沙盒同一套工作目录语义。

## 5. 自动落盘改造（三处）

| 文件 | 改法 |
|---|---|
| `core/hunyuan_image.py` | `generate_image(save_dir=None)`：`None` → `resolve_ai_output_dir()`；显式传值原样使用 |
| `plugins/tokenrhythm_image/plugin.py` | `_save_image_bytes` / `_download_image` / 两个工具入口同样处理 |
| `core/tools.py` `fetch_image` | `Path.cwd()` → `resolve_ai_output_dir()`（该工具无落点参数，无条件走新目录） |

默认值由 `"."` 改为 `None` 是关键：`"."` 无法区分"没传"与"显式要求进程 cwd"，
而显式传 `"."` 的调用必须保持旧行为。
同步更新三处工具的 `ToolSpec` / plugin 描述文案与**参数默认值**（`"."` → `None`），
明确"不传 save_dir 时落到 AI 生成内容目录"，让模型知道默认行为而非猜；仅改描述与
默认值、不改 schema 形状，避免既有调用方解析失败。

保留的旧行为：显式传 `"."` 仍按进程 cwd 处理——该写法等价于"就要当前目录"，
不做隐式改道，避免破坏既有调用。
AI 生成内容目录"，让模型知道默认行为而非猜。

## 6. AI 主动写的临时产物

采用"沙盒豁免 + 运行时上下文注入 + 提示词约定"三件套，不做强制拦截。

**6.1 沙盒豁免**

在 `core/tools.py` 现有豁免体系（`is_trusted_paste_path` /
`is_session_user_attached_path`，见 `_resolve_workspace_path` 第 405-445 行）中
新增 `is_ai_output_path(path)`：路径位于 `resolve_ai_output_dir()` 内即返回 True，
`allow_trusted_read` 分支同样放行。默认目录 `~/.ycode/tmp` 已在
`is_ycode_data_path` 豁免范围内，自定义目录靠本条生效。

**6.2 运行时上下文注入**

`core/context_builder.py:85` 附近的运行上下文块（"当前工作目录 / 默认命令解释器"）
增加一行 `AI 生成内容目录：<绝对路径>`，让每回合都能看到实际生效值（含用户自定义）。

**6.3 提示词约定**

`system_prompt.md` 与 `system_prompt_gui.md` 增加约定：探测脚本、中间产物、临时输出
一律写到"AI 生成内容目录"，不写进用户项目目录；临时文件需要长期保留时再显式写入
项目内目标路径。

## 7. 撤销语义

- AI 生成内容目录内的写入**纳入按回合撤销**：撤销该回合时，本回合产出的图片 /
  下载文件 / 临时脚本一并回滚。
- 依据：`core/turn_undo/recorder.py:68` `display_path_for` 已明确支持工作区外路径
  （"工作区外路径返回规范化绝对路径"），`prepare_mutation` 的 `canonicalize_path`
  也不限制路径必须在工作区内，因此无需为撤销层开新口子。
- 仅回滚**本回合产生的**文件（撤销层按检查点记录，天然满足）；用户此前已存在于
  该目录的文件不受影响。
- 锁语义：写入仍持 `workspace_lock(thread_workspace_root())`，与现有生图实现一致。

## 8. 前端设置页

- `frontend/src/components/SettingsPage.vue`：新增"AI 生成内容目录"输入框 +
  选择文件夹按钮，展示当前生效值与默认值，支持恢复默认。
- 桥接：`gui_bridge.get_system_settings()` 快照增加 `ai_output_dir` /
  `ai_output_dir_default` 字段；新增 `set_ai_output_dir(path)` 方法（校验目录可写，
  失败显式返回错误，不静默降级）。
- 设置项归类到系统设置分区，与"默认命令解释器"同级。

## 9. 错误处理

| 场景 | 行为 |
|---|---|
| 配置目录不存在 | 自动创建 |
| 配置目录不可写 | 回退默认目录 + 告警日志；工具结果中说明实际落点 |
| 配置值非法（空/相对/含非法字符） | 回退默认目录 + 告警日志 |
| 显式 `save_dir` 不可写 | 维持现有报错路径，不静默改道 |
| 前端渲染该目录图片 | 验证 `~/.ycode/tmp` 下图片可正常内嵌（media blobs 已在 `~/.ycode` 下，预期可行）；不可行则前端需补路径白名单 |

## 10. 测试

- `tests/test_ai_output_paths.py`（新建）
  - 未配置 → 默认目录；配置了 → 自定义目录
  - `~` / 环境变量展开
  - workdir 等于目录 / 为子目录 → 返回 workdir
  - 目录不存在 → 自动创建
  - 不可写 → 回退默认 + 告警
- `tests/test_tools.py`（扩展）
  - `fetch_image` 落点断言（monkeypatch 配置目录）
  - `is_ai_output_path` 豁免：写入自定义目录不触发越界询问
- 生图落点：混元 / 基元各一条，断言未传 `save_dir` 时落到配置目录、显式传值时尊重原值
- 撤销：生成后撤销回合，断言文件被回滚

## 11. 兼容性与影响面

- 默认行为变化：未传 `save_dir` 的生图不再落进程 cwd —— 这是本次要修的问题本身。
- 显式传 `save_dir` 的既有调用（含技能脚本里写死的路径）不受影响。
- 技能文档若提到"图片会存到当前目录"，需同步检查
  （`skills/baoyu-xhs-images/references/ycode-imagegen.md` 等）。
