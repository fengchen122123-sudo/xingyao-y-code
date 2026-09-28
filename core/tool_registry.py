"""Capability registry for the built-in tools."""

from __future__ import annotations

import logging
from typing import Any, Iterable

from core.computer.tool_adapter import RESERVED_COMPUTER_TOOL_NAMES
from core.tool_protocol import ToolResult, ToolSpec

_LOGGER = logging.getLogger(__name__)

# 调度层（core/llm.py）会向 arguments 注入 schema 之外的内部上下文参数，
# 过滤未知参数时必须放行这些键，否则会破坏取消/权限链路
_INTERNAL_ARG_KEYS = {
    "cancel_event",
    "event_callback",
    "permission_owner",
    "permission_source",
    "parent_turn_id",
}


class ToolRegistry:
    def __init__(self, specs: Iterable[ToolSpec] = ()):
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if not spec.name:
            raise ValueError("tool name must not be empty")
        if spec.name in RESERVED_COMPUTER_TOOL_NAMES:
            # Harness 保留名（设计第 6 节规则 8）：computer_action /
            # computer_select_target 由固定适配器执行，插件注册同名工具
            # 直接拒绝；插件加载器会把该异常记为插件 failed 状态。
            raise ValueError(f"reserved computer tool name: {spec.name}")
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._specs[spec.name] = spec

    def unregister(self, name: str) -> None:
        """卸载指定工具（插件热切换用）；工具不存在时抛 KeyError。"""
        del self._specs[name]

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def all(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def api_schemas(self) -> list[dict[str, Any]]:
        return [spec.api_schema() for spec in self._specs.values()]

    @staticmethod
    def _filtered_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
        """Drop model-invented arguments while preserving scheduler context."""
        props = (spec.input_schema or {}).get("properties")
        if isinstance(props, dict) and props:
            return {
                key: value for key, value in arguments.items()
                if key in props or key in _INTERNAL_ARG_KEYS
            }
        return arguments

    def execute_strict(self, tool_name: str, **arguments: Any) -> ToolResult:
        """Execute a registered tool while preserving execution exceptions.

        首个形参刻意命名为 ``tool_name``：工具自身的参数里可能包含名叫 ``name`` 的
        字段（如 mcp_add_server / automation_create），若形参叫 ``name`` 会与
        ``**arguments`` 冲突（TypeError: multiple values for argument 'name'）。
        """
        spec = self.get(tool_name)
        result = spec.execute(**self._filtered_arguments(spec, arguments))
        if isinstance(result, ToolResult):
            return result
        return ToolResult(content=str(result) if result is not None else "")

    def execute(self, tool_name: str, **arguments: Any) -> ToolResult:
        spec = self._specs.get(tool_name)
        if spec is None:
            return ToolResult(content=f"Unknown tool: {tool_name}", is_error=True)
        try:
            return self.execute_strict(tool_name, **arguments)
        except Exception as exc:
            return ToolResult(content=f"Tool execution error: {exc}", is_error=True)


def _schema(properties: dict[str, Any], required: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    # 支持 anyOf / oneOf / allOf 等 JSON Schema 顶层约束
    for key, value in extra.items():
        if value is not None:
            result[key] = value
    return result


def filter_reserved_tool_schemas(schemas: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """MCP schema 合并时的保留名过滤（设计 19.2）。

    MCP server 暴露 computer_action / computer_select_target 同名工具时，
    明确跳过该扩展并记录失败状态（warning 日志）；无保留名时返回与输入
    逐项相同的列表，tools 字节保持不变。
    """
    blocked: list[str] = []
    allowed: list[dict[str, Any]] = []
    for schema in schemas:
        name = str(schema.get("function", {}).get("name", "") or "")
        if name in RESERVED_COMPUTER_TOOL_NAMES:
            blocked.append(name)
        else:
            allowed.append(schema)
    if blocked:
        _LOGGER.warning(
            "MCP 提供了电脑操控保留名工具，已拒绝合并: %s",
            ", ".join(sorted(set(blocked))),
        )
    return allowed


def build_default_registry() -> ToolRegistry:
    from core import tools
    from core import lsp as _lsp
    from core.ask_user import ask_user
    from core.plan import (
        plan_finish,
        plan_read,
        plan_resume_takeover,
        plan_retry,
        plan_update,
        plan_write,
    )

    # side_effect_scope 归类依据《03-工具调度拓扑化设计规格》3.2 取值表
    # （none / workspace / git / network / system）；表未覆盖的工具按语义归类，
    # 行尾注释说明理由。该字段 v1 不参与调度决策，仅为契约元数据。
    read_specs = [
        ToolSpec(
            name="read_file",
            description="读取指定文件的文本内容。默认仅限当前工作目录内（用户可通过 /sandbox on 开放）；默认最多返回 2000 行，大文件（>50KB）自动只读前 500 行并提示分页；compress=False 默认返回完全原始内容，设 compress=True 可压缩（去行尾空白、合并空行）以节省 token 但会与原文不完全一致（编辑前需 compress=False 重读以保证 edit_file 匹配）；with_line_numbers=True 时返回 \"N: line\" 格式（1-based），便于配合 edit_file 的 start_line/end_line 精准定位大文件或重复代码段。",
            input_schema=_schema({
                "file_path": {"type": "string", "description": "当前工作目录内的文件路径"},
                "offset": {"type": "integer", "default": 0, "description": "从第几行开始读取（0 起）"},
                "limit": {"type": "integer", "description": "最多读取的行数（上限 2000）"},
                "compress": {"type": "boolean", "default": False, "description": "是否压缩文本（去行尾空白、合并空行）。默认 False 保留原始内容；浏览理解时可设 True 省 token，但编辑前需用 compress=False 重读"},
                "with_line_numbers": {"type": "boolean", "default": False, "description": "是否在每行前添加 1-based 行号前缀（如 \"1: code\\n2: code\\n\"）。默认 False。设 True 时 compress 被忽略；建议与 edit_file 的 start_line/end_line 参数配合使用，适用于大文件、重复代码段、字符串匹配易失败的场景"},
            }, ["file_path"]),
            execute=tools.read_file,
            read_only=True,
            requires_permission=False,
            parallel_safe=True,
            activity_description="读取文件",
            side_effect_scope="none",
        ),
        ToolSpec(
            name="list_directory",
            description="列出目录内容。默认仅限当前工作目录内（用户可通过 /sandbox on 开放）。超过 max_entries 时自动截断。",
            input_schema=_schema({
                "dir_path": {"type": "string", "default": "."},
                "max_entries": {"type": "integer", "default": 200, "minimum": 1, "maximum": 1000},
            }),
            execute=tools.list_directory,
            read_only=True,
            requires_permission=False,
            parallel_safe=True,
            activity_description="查看目录",
            side_effect_scope="none",
        ),
        ToolSpec(
            name="search_code",
            description="在项目中搜索代码内容。",
            input_schema=_schema({
                "query": {"type": "string"},
                "dir_path": {"type": "string", "default": "."},
            }, ["query"]),
            execute=tools.search_code,
            read_only=True,
            requires_permission=False,
            parallel_safe=True,
            activity_description="搜索代码",
            side_effect_scope="none",
        ),
        ToolSpec(
            name="find_definition",
            description=(
                "查找符号（函数/类/方法）的定义位置。比 search_code 更精确——"
                "只匹配定义位置（def/class/function/func 等关键字），不匹配调用或字符串字面量。"
                "支持 Python/JS/TS/Go/Rust/Java/C/C++/C#。返回 file:line 格式，可直接用 read_file 跳转。"
            ),
            input_schema=_schema({
                "symbol": {"type": "string", "description": "要查找的符号名（函数名/类名/方法名）"},
                "dir_path": {"type": "string", "default": "."},
                "file_extensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "限定文件扩展名（如 ['.py', '.ts']），不传则全语言",
                },
            }, ["symbol"]),
            execute=_lsp.find_definition,
            read_only=True,
            requires_permission=False,
            parallel_safe=True,
            activity_description="查找符号定义",
            side_effect_scope="none",
        ),
        ToolSpec(
            name="recall_history",
            description=(
                "回溯当前会话更早被压缩归档的原始对话消息。before_msg_id 取自记忆摘要块"
                "尾部[更早摘要回溯]索引中的 msg_id（或上一批回溯结果里最早一条的 msg_id），"
                "返回该边界及其之前、摘要水位覆盖范围内的原始消息（时间正序，含角色）。"
                "仅用于恢复更早对话的细节；当前活跃上下文里已有的内容无需回溯。"
            ),
            input_schema=_schema({
                "before_msg_id": {"type": "integer", "description": "读取此 msg_id 之前的已压缩消息", "minimum": 1},
                "limit": {"type": "integer", "default": 16, "minimum": 1, "maximum": 50, "description": "最多返回的消息条数"},
            }, ["before_msg_id"]),
            execute=tools.recall_history,
            read_only=True,
            requires_permission=False,
            parallel_safe=True,
            activity_description="回溯已压缩的历史消息",
            side_effect_scope="none",
        ),
        ToolSpec(
            name="search_news",
            description="搜索互联网新闻。",
            input_schema=_schema({
                "query": {"type": "string"},
                "num_results": {"type": "integer", "default": 5},
            }, ["query"]),
            execute=tools.search_news,
            read_only=True,
            requires_permission=False,
            parallel_safe=True,
            activity_description="联网搜索",
            side_effect_scope="network",
        ),
        ToolSpec(
            name="fetch_url",
            description="使用本机 HTTP 客户端获取网页或 JSON。需要实时网页数据时使用，不要用它代替项目代码搜索。",
            input_schema=_schema({
                "url": {"type": "string", "description": "完整的 http 或 https 地址"},
                "timeout_seconds": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 60,
                },
            }, ["url"]),
            execute=tools.fetch_url,
            read_only=True,
            requires_permission=False,
            parallel_safe=True,
            activity_description="访问网址",
            side_effect_scope="network",
        ),
        ToolSpec(
            name="get_weather",
            description="查询指定城市的实时天气和短期预报。必须明确传入城市；不要使用 search_news 查询天气。",
            input_schema=_schema({
                "city": {"type": "string", "description": "城市名，例如北京、上海或 Shanghai"},
                "timeout_seconds": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 60,
                },
            }, ["city"]),
            execute=tools.get_weather,
            read_only=True,
            requires_permission=False,
            parallel_safe=True,
            activity_description="查询天气",
            side_effect_scope="none",
        ),
        ToolSpec(
            name="glob_files",
            description="按 glob 模式查找文件。",
            input_schema=_schema({
                "pattern": {"type": "string"},
                "dir_path": {"type": "string", "default": "."},
            }, ["pattern"]),
            execute=tools.glob_files,
            read_only=True,
            requires_permission=False,
            parallel_safe=True,
            activity_description="匹配文件",
            side_effect_scope="none",
        ),
        ToolSpec(
            name="automation_list",
            description=(
                "查看星轨自动化任务：列出全部任务的 id/名称/启用状态/触发器摘要/"
                "下次触发时间/目标会话/权限档位/上次运行时间；传入 task_id 时查看单个任务"
                "的完整详情与最近 10 条运行记录（状态/触发方式/起止时间/错误）。"
                "仅 GUI 运行期间可用（CLI/headless 下会提示不可用）。"
            ),
            input_schema=_schema({
                "task_id": {
                    "type": "string",
                    "description": "可选，查看某个星轨自动化任务的详情与最近运行记录",
                },
            }),
            execute=_automation_list_wrapper,
            read_only=True,
            requires_permission=False,
            parallel_safe=True,
            activity_description="查看星轨自动化任务",
            side_effect_scope="none",  # 表未覆盖：只读查询，无持久副作用
        ),
    ]

    mutating_specs = [
        ToolSpec(
            name="ask_user",
            description="向用户提出一个需要选择的澄清问题。必须提供 1 到 3 个选项；有计划时只能绑定独立的用户输入准备项。",
            input_schema=_schema({
                "question": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 3,
                },
                "plan_item_id": {"type": "string", "default": "", "description": "只能填写独立的用户输入准备项 ID；不得填写真正执行查询或修改的计划项 ID"},
            }, ["question", "options"]),
            execute=ask_user,
            requires_permission=False,
            activity_description="等待用户回答",
            side_effect_scope="none",
        ),
        ToolSpec(
            name="plan_write",
            description="创建或替换当前任务计划；新项目必须是 pending。用户输入准备项使用 kind=input_preparation，其他项使用 kind=execution；运行状态由调度器真实回写。",
            input_schema=_schema({
                "items": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "subject": {"type": "string"},
                        "status": {"type": "string"},
                        "depends_on": {"type": "array", "items": {"type": "string"}, "default": []},
                        "owner": {"type": "string", "default": ""},
                        "files": {"type": "array", "items": {"type": "string"}, "default": []},
                        "verification": {"type": "string", "default": "", "description": "verification_type=command 时必须填纯英文/命令字符的可执行 shell 命令（不能包含中文描述），且命令退出码 0 才算通过；result 时填自然语言验收说明"},
                        "verification_type": {"type": "string", "enum": ["result", "command"], "default": "result", "description": "信息查询/文件生成类用 result；只有真实可执行 shell 命令验证才用 command"},
                        "stage": {"type": "string", "enum": ["plan", "build", "review"], "default": "build"},
                        "kind": {"type": "string", "enum": ["execution", "input_preparation"], "default": "execution"},
                    },
                    "required": ["id", "subject", "status"],
                }},
            }, ["items"]),
            execute=plan_write,
            activity_description="更新任务计划",
            side_effect_scope="workspace",  # 表未覆盖：持久化修改任务计划状态（.ycode_tasks/plans）
        ),
        ToolSpec(
            name="plan_update",
            description="更新当前计划项的元数据；不要伪造运行状态，completed/failed 由真实执行器回写。",
            input_schema=_schema({
                "item_id": {"type": "string"},
                "status": {"type": "string"},
                "subject": {"type": "string"},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "owner": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "verification": {"type": "string"},
                "verification_type": {"type": "string", "enum": ["result", "command"]},
                "stage": {"type": "string", "enum": ["plan", "build", "review"]},
                "kind": {"type": "string", "enum": ["execution", "input_preparation"]},
            }, ["item_id"]),
            execute=plan_update,
            activity_description="更新计划项",
            side_effect_scope="workspace",  # 表未覆盖：持久化修改计划项元数据
        ),
        ToolSpec(
            name="plan_read",
            description="读取当前计划状态，查看每个计划项的 id/subject/status/depends_on。用于在 plan_write/plan_update/run_subagent 之前检查计划状态。可选传 item_id 查看单条详情。",
            input_schema=_schema({
                "item_id": {"type": "string", "description": "可选，查看特定计划项的详情"},
            }, []),
            execute=plan_read,
            read_only=True,
            activity_description="查看计划状态",
            side_effect_scope="none",
        ),
        ToolSpec(
            name="plan_retry",
            description="将 failed/blocked/cancelled 状态的计划项重置为 pending，以便重新通过 run_subagent 执行。已失败至少两次且保留错误时，必须显式传 force=true 以确认强制重试。",
            input_schema=_schema({
                "item_id": {"type": "string", "description": "要重置的计划项 id"},
                "force": {"type": "boolean", "description": "失败至少两次且仍有错误时，确认已分析根因并强制重试"},
            }, ["item_id"]),
            execute=plan_retry,
            activity_description="重试计划项",
            side_effect_scope="workspace",  # 表未覆盖：修改计划项状态
        ),
        ToolSpec(
            name="plan_resume_takeover",
            description=(
                "恢复一个曾经因子 Agent 超时而移交主 Agent、随后验证失败/阻塞/取消的计划项。"
                "恢复属于原尝试的延续，不增加 attempts，也绝不会重新创建子 Agent。"
                "恢复后由当前主 Agent 直接继续，完成时调用 plan_finish。"
            ),
            input_schema=_schema({
                "item_id": {"type": "string", "description": "要恢复的超时接管计划项 id"},
            }, ["item_id"]),
            execute=plan_resume_takeover,
            activity_description="恢复主 Agent 接管",
            side_effect_scope="workspace",  # 表未覆盖：修改计划项接管状态
        ),
        ToolSpec(
            name="plan_finish",
            description=(
                "完成主 Agent 正在接管的计划项（子代理超时接管收尾专用）。"
                "接管项的直接工具工作全部完成后必须调用本工具显式收尾："
                "verification_type=command 的计划项会真实执行 verification 命令验证，"
                "通过则标记 completed 并自动提升下一个排队接管项；"
                "验证失败则标记 failed，不重新委派子代理。"
                "只有当前回合 active 接管中的计划项允许收尾。"
            ),
            input_schema=_schema({
                "item_id": {"type": "string", "description": "要完成的接管计划项 id"},
                "result": {"type": "string", "description": "工作成果摘要/完成说明"},
            }, ["item_id", "result"]),
            execute=plan_finish,
            activity_description="完成接管计划项",
            side_effect_scope="system",  # 表未覆盖：会真实执行 verification shell 命令
        ),
        ToolSpec(
            name="write_file",
            description="创建新文件或完整重写小文件。默认仅限当前工作目录内（用户可通过 /sandbox on 开放）。重要限制：如果目标文件已存在且超过 500 行 / 50KB，write_file 会拒绝覆盖；修改已有大文件必须使用 edit_file 进行局部片段修改。",
            input_schema=_schema({
                "file_path": {"type": "string", "description": "当前工作目录内的文件路径"},
                "content": {"type": "string"},
            }, ["file_path", "content"]),
            execute=tools.write_file,
            mutates_files=True,
            activity_description="写入文件",
            side_effect_scope="workspace",
        ),
        ToolSpec(
            name="edit_file",
            description=(
                "局部修改已有文件（首选工具）。默认仅限当前工作目录内（用户可通过 /sandbox on 开放）。\n"
                "⚠ 参数硬约束（二选一，必须完整提供，缺一即报错）：\n"
                "  路径A 字符串匹配：必传 target_content + replacement_content（target_content 为空或缺失会直接失败）\n"
                "  路径B 行号路径：必传 start_line + end_line + replacement_content（只传一个行号会失败）\n"
                "两条路径互斥：传了 start_line/end_line 就跳过 target_content 匹配。\n"
                "路径A 适合小文件/唯一代码段，target_content 应包含唯一上下文（≤1500 字符），多次出现需补上下文或 replace_all=True。\n"
                "路径B 适合大文件/重复代码段，建议先 read_file(with_line_numbers=True) 获取行号。\n"
                "⚠ 编辑前必须先读取该文件：路径A 的 target_content 必须与文件实际内容一致，"
                "路径B 的行号必须来自真实文件——不要凭记忆或旧快照改文件。\n"
                "若文件在本次编辑的「读取→写入」窗口内被外部修改，写入会中止并提示重新读取（防并发覆盖）。\n"
                "严禁用 write_file 覆盖已有大文件来替代 edit_file。"
            ),
            input_schema=_schema({
                "file_path": {"type": "string", "description": "当前工作目录内的文件路径"},
                "target_content": {"type": "string", "description": "路径A 必传：要被替换的旧代码段（须与文件实际内容完全一致，含缩进/空白）。路径B 下可省略"},
                "replacement_content": {"type": "string", "description": "两条路径都必传：要换上的新代码段"},
                "replace_all": {"type": "boolean", "default": False, "description": "路径A 下是否替换全部出现位置；路径B 下忽略"},
                "start_line": {"type": "integer", "description": "路径B 必传：1-based 起始行号（inclusive）。必须与 end_line 同时提供"},
                "end_line": {"type": "integer", "description": "路径B 必传：1-based 结束行号（inclusive）。必须与 start_line 同时提供。超出文件总行数时自动收敛不报错"},
            }, ["file_path", "replacement_content"],
            allOf=[{
                "anyOf": [
                    {"type": "object", "required": ["target_content"]},
                    {"type": "object", "required": ["start_line", "end_line"]},
                ],
            }]),
            execute=tools.edit_file,
            mutates_files=True,
            activity_description="修改文件",
            side_effect_scope="workspace",
        ),
        ToolSpec(
            name="fetch_image",
            description="下载网页中的图片到本地并返回本地路径。仅当 fetch_url / search_news 结果中含与主题直接相关的图片、需要图文并茂展示时使用；广告/图标/头像等无关图片不要下载。成功后按界面形态展示：GUI 会话用 Markdown 图片语法 ![](本地路径) 内嵌（远程 URL 前端不渲染，必须下载成本地路径）；CLI 终端直接给出本地路径。图片落到 AI 生成内容目录（设置页可配置，默认 ~/.ycode/tmp）。仅支持 http/https，仅 png/jpg/jpeg/gif/webp，上限 10MB。",
            input_schema=_schema({
                "url": {"type": "string", "description": "完整的 http 或 https 图片地址"},
                "timeout_seconds": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 60,
                },
            }, ["url"]),
            execute=tools.fetch_image,
            read_only=False,
            mutates_files=True,
            requires_permission=False,
            cancellable=True,
            activity_description="下载图片",
            side_effect_scope="network",
        ),
        ToolSpec(
            name="undo_turn",
            description=(
                "撤销某一次回合的全部受控文件修改（用户明确要求“撤销/回滚这轮文件改动”时使用）。"
                "撤销前会先预检每个文件的实际状态：任一文件在回合结束后被修改过，整轮拒绝、零文件写入。"
                "聊天历史保留，模型会通过上下文得知该回合的文件结果已失效。"
                "终端命令等其他副作用不在撤销范围内。"
            ),
            input_schema=_schema({
                "turn_id": {
                    "type": "string",
                    "description": (
                        "要撤销的回合 ID（可使用 8 位短 ID）。省略时撤销最近一个可撤销回合。"
                        "不接受文件路径或其他参数。"
                    ),
                },
            }),
            execute=tools.undo_turn,
            activity_description="撤销回合文件修改",
            side_effect_scope="workspace",
        ),
        ToolSpec(
            name="execute_command",
            description=(
                "执行本地命令。前台执行默认 30 秒超时，可用 timeout_seconds 调整（上限 600 秒）。"
                "PowerShell 注意事项：\n"
                "1) $env:XXX 这类变量在单引号字符串内不会扩展，应使用双引号包裹"
                "（例如 -OutFile \"$env:TEMP\\pkg.zip\"）\n"
                "2) 命令串联必须用 ;（分号）或换行，禁止用 & 作为分隔符。"
                "PowerShell 中 & 是调用运算符（用于执行字符串命令），非 Bash 风格的命令串联符。"
                "错误示例：cd path & javac Foo.java（会抛 ParserError，退出码 2）"
                "正确示例：cd path; javac Foo.java; java Foo\n"
                "3) 条件串联用 if ($LASTEXITCODE -eq 0) { next }，PowerShell 7+ 才支持 && 运算符\n"
                "单次执行输出总量上限 1MB（stdout+stderr）：超限会终止进程树并返回截断结果；"
                "可能长时间刷屏的命令请自行收敛输出（重定向到文件、加 head 或限制范围）。\n"
                "后台模式（run_in_background=true）禁止在命令末尾接 "
                "| Select-Object -Last、| tail、| head 等缓冲/截断管道："
                "输出会实时写入任务日志，用户在界面「后台任务」面板实时查看执行过程，"
                "完成后系统自动取日志尾部汇总，无需自行截尾；"
                "这类管道（尤其 Select-Object -Last）必须收齐全部输入才开始输出，"
                "接上去整个运行期日志都只有启动标记一行，用户全程看不到执行进度。"
            ),
            input_schema=_schema({
                "command": {"type": "string"},
                "run_in_background": {"type": "boolean", "default": False},
                "timeout_seconds": {
                    "type": "integer",
                    "default": 30,
                    "minimum": 1,
                    "maximum": 600,
                },
                "shell_id": {
                    "type": "string",
                    "enum": ["auto", "cmd", "powershell", "pwsh", "bash", "zsh", "sh"],
                    "description": "可选的单次解释器覆盖；仅在需要根据报错调整时显式传入，且目标必须已安装。",
                },
            }, ["command"]),
            execute=tools.execute_command,
            activity_description="执行命令",
            side_effect_scope="system",
        ),
        ToolSpec(
            name="manage_task",
            description=(
                "查看、等待或终止后台任务，同时管理两类任务：shell 后台命令与子代理任务"
                "（task_id 形如 subagent-xxxxxxxx）。list/status/wait 为只读操作不弹确认；"
                "仅 kill 操作会请求用户确认。"
                "shell 任务用 wait 条件等待：传入上次快照的 revision（缺省视为 0，即首次等待），"
                "任务状态或输出变化时立即返回。"
                "子代理任务用 wait 等待其进入终态（忽略 after_revision），"
                "终态返回含 result 摘要，可在回合内直接收割后台子代理结果；"
                "子代理结果也会在回合结束后的下一回合自动汇总注入，无需为收结果而拖延回合。"
            ),
            input_schema=_schema({
                "action": {"type": "string", "enum": ["list", "status", "wait", "kill"]},
                "task_id": {
                    "type": "string",
                    "description": "目标任务 ID：shell 任务为 8 位十六进制，子代理任务形如 subagent-xxxxxxxx。",
                },
                "after_revision": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "仅 shell 任务生效，缺省视为 0（首次等待）；子代理任务忽略此参数。",
                },
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 30},
            }, ["action"]),
            execute=tools.manage_task,
            requires_permission=False,
            activity_description="管理后台任务",
            side_effect_scope="none",
        ),
        ToolSpec(
            name="run_subagent",
            description=(
                "启动辅助 Agent；默认前台等待结果，仅独立任务使用后台模式。"
                "后台模式（run_in_background=true）立即返回 task_id，结果有两条回流路径："
                "回合结束后的下一回合自动注入「📦 后台子代理任务结果汇总」；"
                "回合内可用 manage_task(status/wait, task_id=\"subagent-…\") 主动查询进度或等待终态，"
                "不要拖延回合空等。"
                "子代理状态非 completed（failed/timed_out/cancelled）一律视为失败："
                "读完失败原因与失败工具列表后由主 Agent 用自己的工具兜底完成，"
                "同一子任务最多重试 1 次且必须改变任务描述（换工具/换方法），禁止原样重试。"
            ),
            input_schema=_schema({
                "task_description": {"type": "string"},
                "subagent_type": {
                    "type": "string",
                    "default": "general",
                    "description": (
                        "子 Agent 类型。内置：explore（只读探索）/ implement（修改代码）/ "
                        "test（运行测试）/ review（只读审查）/ general（综合）。"
                        "也可传 ~/.ycode/agents/*.md 中定义的自定义子 agent name；"
                        "自定义子 agent 加载失败时自动回退为 general。"
                    ),
                },
                "plan_item_id": {
                    "type": "string",
                    "default": "",
                    "description": "与当前计划项关联的 ID；有计划时必须填写。",
                },
                "run_in_background": {"type": "boolean", "default": False},
            }, ["task_description"]),
            execute=tools.run_subagent,
            requires_permission=False,
            parallel_safe=True,
            activity_description="启动子 Agent",
            side_effect_scope="system",  # 表未覆盖：子代理可执行任意工具，副作用范围不可预知（保守归 system）
        ),
        # ── MCP 服务配置工具（让 AI 能自己配置/启用/禁用 MCP server）─────
        ToolSpec(
            name="mcp_add_server",
            description=(
                "新增一个 MCP server 配置并立即重连生效。用于让 AI 帮用户配置 MCP（如 Firecrawl、wigolo 等）。"
                "会写入 ~/.ycode/mcp.json 并触发重连。若已存在同名 server 会失败，需先调用 mcp_remove_server。"
            ),
            input_schema=_schema({
                "name": {"type": "string", "description": "server 名称（用作工具名前缀，如 'firecrawl'）"},
                "command": {"type": "string", "description": "启动命令，通常为 'npx'"},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "命令参数数组，如 ['-y', 'firecrawl-mcp']",
                },
                "env": {
                    "type": "object",
                    "description": "环境变量字典（可选），如 {\"FIRECRAWL_API_KEY\": \"fc-xxx\"}",
                },
                "enabled": {"type": "boolean", "default": True},
            }, ["name", "command", "args"]),
            execute=lambda name, command, args, env=None, enabled=True: _mcp_add_server_wrapper(name, command, args, env, enabled),
            requires_permission=True,
            parallel_safe=False,
            activity_description="新增 MCP server",
            side_effect_scope="system",  # 表未覆盖：写 mcp.json 且以任意 command/args 启动外部进程
        ),
        ToolSpec(
            name="mcp_remove_server",
            description=(
                "删除一个 MCP server 配置并立即重连生效。"
                "会从 ~/.ycode/mcp.json 移除该 server 并关闭其子进程。"
            ),
            input_schema=_schema({
                "name": {"type": "string", "description": "要删除的 server 名称"},
            }, ["name"]),
            execute=lambda name: _mcp_remove_server_wrapper(name),
            requires_permission=True,
            parallel_safe=False,
            activity_description="删除 MCP server",
            side_effect_scope="system",  # 表未覆盖：关闭外部进程并写 mcp.json
        ),
        ToolSpec(
            name="mcp_set_enabled",
            description=(
                "启用或禁用某个 MCP server（写回 mcp.json + 重连）。"
                "禁用时配置保留，随时可重新启用。"
            ),
            input_schema=_schema({
                "name": {"type": "string", "description": "server 名称"},
                "enabled": {"type": "boolean", "description": "true=启用, false=禁用"},
            }, ["name", "enabled"]),
            execute=lambda name, enabled: _mcp_set_enabled_wrapper(name, enabled),
            requires_permission=True,
            parallel_safe=False,
            activity_description="切换 MCP server 开关",
            side_effect_scope="system",  # 表未覆盖：可能启动外部进程并写 mcp.json
        ),
        ToolSpec(
            name="mcp_list_servers",
            description=(
                "列出所有已配置的 MCP server（含 enabled/command/args/env/connected 字段）。"
                "env 中的 API Key 会做掩码处理（前4位+****+后4位），避免敏感信息泄露。"
                "用于 AI 在配置前查看现状。"
            ),
            input_schema=_schema({}),
            execute=lambda **kwargs: _mcp_list_servers_wrapper(),
            read_only=True,
            requires_permission=False,
            parallel_safe=True,
            activity_description="查看 MCP 服务列表",
            side_effect_scope="none",  # 表未覆盖：只读查询
        ),
        # ── 项目浏览器分辨率（星瑶按项目自设视口）─────────────────────────
        ToolSpec(
            name="browser_set_viewport",
            description=(
                "设置当前项目的浏览器分辨率（逻辑视口宽×高，单位像素），按项目持久化。"
                "星瑶做浏览器操作时页面按该分辨率渲染：GUI 内置浏览器以适应窗口缩放呈现，"
                "外部浏览器作为窗口视口。适配移动端页面用 375×667，桌面页面用 1440×900。"
                "当前会话未关联项目时无法设置。"
            ),
            input_schema=_schema({
                "width": {"type": "integer", "description": "视口宽度（像素），120-8192"},
                "height": {"type": "integer", "description": "视口高度（像素），120-8192"},
            }, ["width", "height"]),
            execute=lambda width, height: _browser_set_viewport_wrapper(width, height),
            requires_permission=True,
            parallel_safe=False,
            activity_description="设置项目浏览器分辨率",
            side_effect_scope="workspace",
        ),
        # ── 星轨自动化工具（对话里创建/修改/删除/立即执行定时任务）───────
        ToolSpec(
            name="automation_create",
            description=(
                "创建一个星轨自动化任务（定时在某个会话里自动执行一段提示词）。"
                "用户说「每天/每周/每隔多久执行某任务」时用本工具创建。\n"
                "触发器参数形态（trigger_type）：\n"
                "1) interval=每隔固定分钟数：必须传 interval_minutes（整数分钟，1..10080，即最多 7 天）；\n"
                "2) daily=每天定点：必须传 time（零填充 24 小时制 HH:MM，例如 09:05、20:00）；\n"
                "3) weekly=每周若干天定点：必须传 time 与 weekdays（整数数组，1=周一、2=周二、"
                "3=周三、4=周四、5=周五、6=周六、7=周日，例如 [1,5] 表示周一和周五）；\n"
                "4) startup=应用启动时执行一次：不需要任何触发器参数。\n"
                "permission_mode：无人值守权限档位，只能是 confirm（默认，需要确认）或 "
                "accept_edits（自动接受文件编辑）；plan/full 等更高档位不允许在对话里设置，"
                "需要时请让用户在 GUI 星轨自动化面板里手动设置。\n"
                "target_session 省略时默认使用当前对话所属会话。"
            ),
            input_schema=_schema({
                "name": {"type": "string", "description": "任务名称（不超过 50 字）"},
                "prompt": {"type": "string", "description": "触发的每个回合要执行的提示词"},
                "trigger_type": {
                    "type": "string",
                    "enum": ["interval", "daily", "weekly", "startup"],
                    "description": "触发器类型：interval/daily/weekly/startup",
                },
                "interval_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10080,
                    "description": "interval 触发器：间隔分钟数（1..10080）",
                },
                "time": {
                    "type": "string",
                    "description": "daily/weekly 触发器：零填充 24 小时制 HH:MM，如 09:05、20:00",
                },
                "weekdays": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1, "maximum": 7},
                    "description": "weekly 触发器：星期数组，1=周一 … 7=周日，如 [1,5] 表示周一、周五",
                },
                "target_session": {
                    "type": "string",
                    "description": "任务运行的目标会话名；省略时默认当前会话",
                },
                "permission_mode": {
                    "type": "string",
                    "enum": ["confirm", "accept_edits"],
                    "default": "confirm",
                    "description": "无人值守权限档位，仅支持 confirm / accept_edits",
                },
                "missed_run_policy": {
                    "type": "string",
                    "enum": ["skip", "run_once"],
                    "default": "skip",
                    "description": "应用未运行而错过触发时的策略：skip=跳过（默认）/ run_once=补跑一次",
                },
                "notify_on_success": {
                    "type": "boolean",
                    "default": False,
                    "description": "任务成功完成后是否通知用户，默认 false",
                },
                "workdir": {
                    "type": "string",
                    "description": "可选，任务运行的工作目录；留空则跟随目标会话",
                },
                "model_id": {
                    "type": "string",
                    "description": "可选，指定模型 id；留空跟随会话绑定/全局默认",
                },
            }, ["name", "prompt", "trigger_type"]),
            execute=_automation_create_wrapper,
            requires_permission=True,
            parallel_safe=False,
            activity_description="创建星轨自动化任务",
            side_effect_scope="workspace",
        ),
        ToolSpec(
            name="automation_update",
            description=(
                "修改一个已存在的星轨自动化任务：只传需要改的字段，其余字段保持原值。"
                "可改 name/prompt/触发器（trigger_type 及 interval_minutes/time/weekdays）、"
                "target_session/permission_mode/missed_run_policy/notify_on_success/workdir/model_id，"
                "并可用 enabled=true/false 启用或停用任务。\n"
                "触发器参数形态同 automation_create：interval 需 interval_minutes（1..10080）；"
                "daily 需 time（零填充 HH:MM）；weekly 需 time 与 weekdays（1=周一 … 7=周日）；"
                "startup 无参数。只传 time/weekdays 时会沿用任务原有的触发器类型。\n"
                "permission_mode：无人值守权限档位只能是 confirm 或 accept_edits，"
                "plan/full 等更高档位请在 GUI 星轨自动化面板里手动设置。"
            ),
            input_schema=_schema({
                "task_id": {"type": "string", "description": "要修改的任务 id"},
                "name": {"type": "string", "description": "新的任务名称"},
                "prompt": {"type": "string", "description": "新的提示词"},
                "trigger_type": {
                    "type": "string",
                    "enum": ["interval", "daily", "weekly", "startup"],
                    "description": "新的触发器类型",
                },
                "interval_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10080,
                    "description": "interval 触发器：间隔分钟数（1..10080）",
                },
                "time": {
                    "type": "string",
                    "description": "daily/weekly 触发器：零填充 24 小时制 HH:MM",
                },
                "weekdays": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1, "maximum": 7},
                    "description": "weekly 触发器：星期数组，1=周一 … 7=周日",
                },
                "target_session": {"type": "string", "description": "新的目标会话名"},
                "permission_mode": {
                    "type": "string",
                    "enum": ["confirm", "accept_edits"],
                    "description": "无人值守权限档位，仅支持 confirm / accept_edits",
                },
                "missed_run_policy": {
                    "type": "string",
                    "enum": ["skip", "run_once"],
                    "description": "错过触发的处理策略",
                },
                "notify_on_success": {
                    "type": "boolean",
                    "description": "成功完成后是否通知用户",
                },
                "workdir": {"type": "string", "description": "新的工作目录；传空字符串表示跟随目标会话"},
                "model_id": {"type": "string", "description": "新的模型 id；传空字符串表示跟随默认"},
                "enabled": {"type": "boolean", "description": "true=启用，false=停用该任务"},
            }, ["task_id"]),
            execute=_automation_update_wrapper,
            requires_permission=True,
            parallel_safe=False,
            activity_description="修改星轨自动化任务",
            side_effect_scope="workspace",
        ),
        ToolSpec(
            name="automation_delete",
            description=(
                "删除一个星轨自动化任务（连同它的运行记录）。"
                "删除前应先用 automation_list 确认 task_id 与任务名称。"
            ),
            input_schema=_schema({
                "task_id": {"type": "string", "description": "要删除的任务 id"},
            }, ["task_id"]),
            execute=_automation_delete_wrapper,
            requires_permission=True,
            parallel_safe=False,
            activity_description="删除星轨自动化任务",
            side_effect_scope="workspace",
        ),
        ToolSpec(
            name="automation_run_now",
            description=(
                "立即手动执行一次某个星轨自动化任务（不影响它原有的定时计划）。"
                "任务上一次运行尚未结束时会被拒绝。"
            ),
            input_schema=_schema({
                "task_id": {"type": "string", "description": "要立即执行的任务 id"},
            }, ["task_id"]),
            execute=_automation_run_now_wrapper,
            requires_permission=True,
            parallel_safe=False,
            activity_description="立即执行星轨自动化任务",
            side_effect_scope="workspace",
        ),
    ]
    registry = ToolRegistry([*read_specs, *mutating_specs])
    # 加载进程内插件（plugins/ 与 ~/.ycode/plugins/）。插件加载全程隔离，
    # 任何失败只记录日志，绝不影响内置工具的可用性。
    try:
        from core import plugin_loader
        plugin_loader.load_and_register(registry)
    except Exception:
        pass
    _apply_managed_tool_filter(registry)
    return registry


def _apply_managed_tool_filter(registry: ToolRegistry) -> None:
    """受管策略 ``disabled_tools``：注册（含插件加载）完成后过滤禁用工具。

    无策略/无禁用项时不做任何事（零行为变化）；管理端未列出的工具照常可用。
    禁用了本发行版未安装的工具名时静默忽略（管理文件可能面向多版本分发）。
    """
    try:
        from core.managed_policy import disabled_tool_names, load_managed_policy

        disabled = disabled_tool_names(load_managed_policy())
    except Exception:
        return
    if not disabled:
        return
    removed: list[str] = []
    for name in disabled:
        try:
            registry.unregister(name)
        except KeyError:
            continue
        removed.append(name)
    if removed:
        _LOGGER.warning("受管策略已禁用工具: %s", ", ".join(sorted(removed)))


# ── MCP 配置工具的执行包装函数 ──────────────────────────────────────
def _mcp_add_server_wrapper(name, command, args, env, enabled):
    from core.mcp_client import mcp_manager
    ok, msg = mcp_manager.add_server(name, command, args, env, enabled)
    return ToolResult(content=msg, is_error=not ok)


def _mcp_remove_server_wrapper(name):
    from core.mcp_client import mcp_manager
    ok, msg = mcp_manager.remove_server(name)
    return ToolResult(content=msg, is_error=not ok)


def _mcp_set_enabled_wrapper(name, enabled):
    from core.mcp_client import mcp_manager
    ok, msg = mcp_manager.set_server_enabled(name, enabled)
    return ToolResult(content=msg, is_error=not ok)


def _mcp_list_servers_wrapper():
    from core.mcp_client import mcp_manager
    return ToolResult(content=mcp_manager.list_servers(), is_error=False)


# ── 项目浏览器分辨率工具的执行包装函数 ──────────────────────────────
def _browser_set_viewport_wrapper(width, height):
    from core.computer.models import ComputerControlError
    from core.computer.settings import set_project_browser_viewport
    from core.workspace_context import get_workdir

    workdir = get_workdir()
    try:
        viewport = set_project_browser_viewport(workdir, width, height)
    except ComputerControlError as error:
        return ToolResult(content=f"{error.code}: {error.message}", is_error=True)
    except Exception as error:
        return ToolResult(content=f"browser_set_viewport 执行失败：{error}", is_error=True)
    return ToolResult(
        content=(
            f"已设置当前项目的浏览器分辨率：{viewport[0]}×{viewport[1]}"
            "（逻辑像素，按项目持久化）。GUI 内置浏览器已即时按适应窗口缩放"
            "呈现该分辨率；之后的浏览器自动化也会使用该视口。"
        ),
        is_error=False,
    )


# ── 星轨自动化（automation_*）工具的执行包装函数 ──────────────────────
#
# 全部经 core.automation.service 句柄访问桥侧能力：工具层不读写数据库、不
# 发事件、不唤醒引擎；CLI/headless 未注册句柄时统一提示能力不可用。
_AUTOMATION_SERVICE_UNAVAILABLE = "星轨自动化仅在 GUI 运行期间可用（当前为 CLI/headless）"
# 无人值守权限档位：对话创建的星轨自动化任务只允许 confirm / accept_edits，
# 更高档位必须由用户在 GUI 星轨自动化面板里手动设置（design：无人值守安全边界）。
_AUTOMATION_ALLOWED_PERMISSION_MODES = ("confirm", "accept_edits")
_AUTOMATION_PERMISSION_MODE_MESSAGE = (
    "无人值守权限档位只支持 confirm 或 accept_edits；"
    "需要更高档位请在 GUI 星轨自动化面板里手动设置"
)


def _automation_service_or_error():
    """取服务句柄；未注册（CLI/headless）时返回 (None, 错误结果)。"""
    from core.automation import service as automation_service

    svc = automation_service.get()
    if svc is None:
        return None, ToolResult(
            content=_AUTOMATION_SERVICE_UNAVAILABLE, is_error=True
        )
    return svc, None


def _automation_permission_mode_error(permission_mode):
    """校验无人值守权限档位；非法（plan/full/未知）时返回错误结果。"""
    if permission_mode is None:
        return None
    mode = str(permission_mode).strip()
    if not mode:
        return None
    if mode not in _AUTOMATION_ALLOWED_PERMISSION_MODES:
        return ToolResult(
            content=_AUTOMATION_PERMISSION_MODE_MESSAGE, is_error=True
        )
    return None


def _automation_bridge_error_text(resp) -> str:
    """把桥侧返回值里的 errors/msg 拼成人类可读文本。"""
    if isinstance(resp, dict):
        errors = resp.get("errors")
        if isinstance(errors, (list, tuple)) and errors:
            return "；".join(str(item) for item in errors)
        msg = resp.get("msg")
        if msg:
            return str(msg)
        if resp.get("ok"):
            return ""
    if resp is None:
        return "自动化服务未返回结果"
    return str(resp)


def _automation_build_trigger_config(
    trigger_type, interval_minutes, time, weekdays
):
    """按触发器类型组装 trigger_config；返回 (config, 错误文本)。

    只做「参数是否齐备」的检查，取值范围/格式由桥侧 validate_task 兜底并原样
    透出错误，避免 core 层出现两套校验。
    """
    trigger_type = str(trigger_type or "").strip()
    if trigger_type == "interval":
        if interval_minutes is None:
            return None, "interval 触发器必须提供 interval_minutes（整数分钟，1..10080）"
        return {"interval_minutes": interval_minutes}, None
    if trigger_type == "daily":
        if time is None:
            return None, "daily 触发器必须提供 time（零填充 24 小时制 HH:MM，如 09:05）"
        return {"time": time}, None
    if trigger_type == "weekly":
        if time is None or weekdays is None:
            return (
                None,
                "weekly 触发器必须同时提供 time（HH:MM）与 weekdays（1=周一 … 7=周日）",
            )
        return {"time": time, "weekdays": weekdays}, None
    if trigger_type == "startup":
        return {}, None
    return (
        None,
        f"未知触发器类型: {trigger_type!r}（可用：interval / daily / weekly / startup）",
    )


def _automation_trigger_summary(task: dict) -> str:
    """取/算任务的触发器摘要（DTO 可能已带 trigger_summary）。"""
    summary = task.get("trigger_summary")
    if summary:
        return str(summary)
    try:
        from core.automation.models import trigger_summary as _summarize

        return _summarize(task.get("trigger_type"), task.get("trigger_config") or {})
    except Exception:
        return str(task.get("trigger_type") or "未知触发器")


def _automation_format_ts(value) -> str:
    if value in (None, ""):
        return "未计划"
    try:
        from datetime import datetime

        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return str(value)


def _automation_task_line(task: dict) -> str:
    enabled = "启用" if task.get("enabled") else "停用"
    return (
        f"- {task.get('name')}｜id={task.get('id')}｜{enabled}"
        f"｜{_automation_trigger_summary(task)}"
        f"｜下次触发={_automation_format_ts(task.get('next_run_at'))}"
        f"｜上次运行={_automation_format_ts(task.get('last_run_at'))}"
        f"｜目标会话={task.get('target_session')}"
        f"｜权限档位={task.get('permission_mode')}"
    )


def _automation_run_line(run: dict) -> str:
    return (
        f"- {run.get('id')}｜状态={run.get('status')}"
        f"｜触发方式={run.get('trigger_kind')}"
        f"｜开始={run.get('started_at') or run.get('queued_at')}"
        f"｜结束={run.get('finished_at')}"
        + (f"｜错误={run.get('error')}" if run.get("error") else "")
    )


def _automation_list_wrapper(task_id=None, **_ignored):
    svc, err = _automation_service_or_error()
    if err is not None:
        return err

    task_id = str(task_id or "").strip()
    if task_id:
        task = svc.get_task(task_id)
        if not isinstance(task, dict):
            return ToolResult(content=f"未找到星轨自动化任务：{task_id}", is_error=True)
        lines = [f"星轨自动化任务详情：{task.get('name')}", _automation_task_line(task)]
        if task.get("prompt"):
            lines.append(f"提示词：{task.get('prompt')}")
        runs_resp = svc.get_runs(task_id, 10)
        if isinstance(runs_resp, dict) and runs_resp.get("ok"):
            runs = runs_resp.get("runs") or []
            lines.append(f"最近运行记录（最多 10 条，共 {len(runs)} 条）：")
            if runs:
                lines.extend(_automation_run_line(run) for run in runs)
            else:
                lines.append("（暂无运行记录）")
        else:
            lines.append(
                f"读取运行记录失败：{_automation_bridge_error_text(runs_resp)}"
            )
        return ToolResult(content="\n".join(lines))

    resp = svc.list_tasks()
    if not isinstance(resp, dict) or not resp.get("ok"):
        return ToolResult(
            content=f"读取星轨自动化任务失败：{_automation_bridge_error_text(resp)}",
            is_error=True,
        )
    tasks = resp.get("tasks") or []
    if not tasks:
        return ToolResult(content="当前没有任何星轨自动化任务。")
    lines = [f"共 {len(tasks)} 个星轨自动化任务："]
    lines.extend(_automation_task_line(task) for task in tasks)
    return ToolResult(content="\n".join(lines))


def _automation_create_wrapper(
    *,
    name,
    prompt,
    trigger_type,
    interval_minutes=None,
    time=None,
    weekdays=None,
    target_session=None,
    permission_mode=None,
    missed_run_policy=None,
    notify_on_success=None,
    workdir=None,
    model_id=None,
    **_ignored,
):
    svc, err = _automation_service_or_error()
    if err is not None:
        return err

    perm_err = _automation_permission_mode_error(permission_mode)
    if perm_err is not None:
        return perm_err

    config, config_err = _automation_build_trigger_config(
        trigger_type, interval_minutes, time, weekdays
    )
    if config_err:
        return ToolResult(content=config_err, is_error=True)

    session = str(target_session).strip() if target_session else ""
    if not session:
        from core import app_context

        session = str(app_context.get_thread_session() or "").strip()
    if not session:
        return ToolResult(
            content=(
                "未指定 target_session，且当前回合没有绑定会话；"
                "请显式指定要运行该任务的会话名（target_session）。"
            ),
            is_error=True,
        )

    task = {
        "name": name,
        "prompt": prompt,
        "trigger_type": str(trigger_type or "").strip(),
        "trigger_config": config,
        "target_session": session,
        "permission_mode": str(permission_mode).strip() if permission_mode else "confirm",
        "missed_run_policy": (
            str(missed_run_policy).strip() if missed_run_policy else "skip"
        ),
        "notify_on_success": bool(notify_on_success),
        "enabled": True,
    }
    if workdir is not None:
        task["workdir"] = str(workdir).strip()
    if model_id is not None:
        task["model_id"] = str(model_id).strip()

    resp = svc.save_task(task)
    if not isinstance(resp, dict) or not resp.get("ok"):
        return ToolResult(
            content=f"创建星轨自动化任务失败：{_automation_bridge_error_text(resp)}",
            is_error=True,
        )
    task_id = resp.get("id")
    return ToolResult(
        content=(
            f"已创建星轨自动化任务「{task.get('name')}」：id={task_id}"
            f"｜{_automation_trigger_summary(task)}"
            f"｜目标会话={task['target_session']}｜权限档位={task['permission_mode']}"
        )
    )


def _automation_update_wrapper(
    *,
    task_id,
    name=None,
    prompt=None,
    trigger_type=None,
    interval_minutes=None,
    time=None,
    weekdays=None,
    target_session=None,
    permission_mode=None,
    missed_run_policy=None,
    notify_on_success=None,
    workdir=None,
    model_id=None,
    enabled=None,
    **_ignored,
):
    svc, err = _automation_service_or_error()
    if err is not None:
        return err

    task_id = str(task_id or "").strip()
    if not task_id:
        return ToolResult(content="必须提供 task_id。", is_error=True)

    perm_err = _automation_permission_mode_error(permission_mode)
    if perm_err is not None:
        return perm_err

    old = svc.get_task(task_id)
    if not isinstance(old, dict):
        return ToolResult(content=f"未找到星轨自动化任务：{task_id}", is_error=True)

    # 桥侧 save_task 是整体覆盖：先取旧任务，只把传入（非 None）字段合并进去，
    # 未传字段必须保持原值，否则会被清空。
    task = dict(old)
    changed: list[str] = []
    if name is not None:
        task["name"] = name
        changed.append("名称")
    if prompt is not None:
        task["prompt"] = prompt
        changed.append("提示词")
    if target_session is not None:
        task["target_session"] = str(target_session).strip()
        changed.append("目标会话")
    if permission_mode is not None:
        task["permission_mode"] = str(permission_mode).strip()
        changed.append("权限档位")
    if missed_run_policy is not None:
        task["missed_run_policy"] = str(missed_run_policy).strip()
        changed.append("错过策略")
    if notify_on_success is not None:
        task["notify_on_success"] = bool(notify_on_success)
        changed.append("成功通知")
    if workdir is not None:
        task["workdir"] = str(workdir).strip()
        changed.append("工作目录")
    if model_id is not None:
        task["model_id"] = str(model_id).strip()
        changed.append("指定模型")

    trigger_touched = trigger_type is not None or any(
        value is not None for value in (interval_minutes, time, weekdays)
    )
    if trigger_touched:
        old_config = task.get("trigger_config")
        if not isinstance(old_config, dict):
            old_config = {}
        effective_type = (
            trigger_type if trigger_type is not None else task.get("trigger_type")
        )
        config, config_err = _automation_build_trigger_config(
            effective_type,
            interval_minutes if interval_minutes is not None else old_config.get("interval_minutes"),
            time if time is not None else old_config.get("time"),
            weekdays if weekdays is not None else old_config.get("weekdays"),
        )
        if config_err:
            return ToolResult(content=config_err, is_error=True)
        task["trigger_type"] = str(effective_type or "").strip()
        task["trigger_config"] = config
        changed.append("触发器")

    if not changed and enabled is None:
        return ToolResult(
            content="请至少提供一个要修改的字段（名称/提示词/触发器/目标会话/权限档位等，或 enabled）。",
            is_error=True,
        )

    if changed:
        resp = svc.save_task(task)
        if not isinstance(resp, dict) or not resp.get("ok"):
            return ToolResult(
                content=f"更新星轨自动化任务失败：{_automation_bridge_error_text(resp)}",
                is_error=True,
            )

    if enabled is not None:
        # 启停走桥侧专用方法（轻量、并发安全）；已保存的合并结果不覆盖它。
        resp = svc.set_enabled(task_id, bool(enabled))
        if not isinstance(resp, dict) or not resp.get("ok"):
            return ToolResult(
                content=f"切换星轨自动化任务启停失败：{_automation_bridge_error_text(resp)}",
                is_error=True,
            )

    parts = []
    if changed:
        parts.append(f"已更新{('、'.join(changed))}")
    if enabled is not None:
        parts.append("已" + ("启用" if enabled else "停用"))
    return ToolResult(content=f"星轨自动化任务 {task_id}：{'，'.join(parts)}。")


def _automation_delete_wrapper(*, task_id, **_ignored):
    svc, err = _automation_service_or_error()
    if err is not None:
        return err

    task_id = str(task_id or "").strip()
    if not task_id:
        return ToolResult(content="必须提供 task_id。", is_error=True)

    resp = svc.delete_task(task_id)
    if not isinstance(resp, dict) or not resp.get("ok"):
        return ToolResult(
            content=f"删除星轨自动化任务失败：{_automation_bridge_error_text(resp)}",
            is_error=True,
        )
    if not resp.get("deleted"):
        return ToolResult(content=f"未找到星轨自动化任务：{task_id}", is_error=True)
    return ToolResult(content=f"已删除星轨自动化任务 {task_id}。")


def _automation_run_now_wrapper(*, task_id, **_ignored):
    svc, err = _automation_service_or_error()
    if err is not None:
        return err

    task_id = str(task_id or "").strip()
    if not task_id:
        return ToolResult(content="必须提供 task_id。", is_error=True)

    resp = svc.run_now(task_id)
    if not isinstance(resp, dict) or not resp.get("ok"):
        return ToolResult(
            content=f"立即执行星轨自动化任务失败：{_automation_bridge_error_text(resp)}",
            is_error=True,
        )
    return ToolResult(
        content=f"已触发星轨自动化任务 {task_id} 立即执行：run_id={resp.get('run_id')}"
    )
