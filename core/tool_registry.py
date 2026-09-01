"""Capability registry for the built-in tools."""

from __future__ import annotations

from typing import Any, Iterable

from core.tool_protocol import ToolResult, ToolSpec

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

    def execute_strict(self, name: str, **arguments: Any) -> ToolResult:
        """Execute a registered tool while preserving execution exceptions."""
        spec = self.get(name)
        result = spec.execute(**self._filtered_arguments(spec, arguments))
        if isinstance(result, ToolResult):
            return result
        return ToolResult(content=str(result) if result is not None else "")

    def execute(self, name: str, **arguments: Any) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            return ToolResult(content=f"Unknown tool: {name}", is_error=True)
        try:
            return self.execute_strict(name, **arguments)
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
        ),
        ToolSpec(
            name="fetch_image",
            description="下载网页中的图片到本地并返回本地路径。仅当 fetch_url / search_news 结果中含与主题直接相关的图片、需要图文并茂展示时使用；广告/图标/头像等无关图片不要下载。成功后按界面形态展示：GUI 会话用 Markdown 图片语法 ![](本地路径) 内嵌（远程 URL 前端不渲染，必须下载成本地路径）；CLI 终端直接给出本地路径。仅支持 http/https，仅 png/jpg/jpeg/gif/webp，上限 10MB。",
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
                "3) 条件串联用 if ($LASTEXITCODE -eq 0) { next }，PowerShell 7+ 才支持 && 运算符"
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
        ),
        ToolSpec(
            name="run_subagent",
            description=(
                "启动辅助 Agent；默认前台等待结果，仅独立任务使用后台模式。"
                "后台模式（run_in_background=true）立即返回 task_id，结果有两条回流路径："
                "回合结束后的下一回合自动注入「📦 后台子代理任务结果汇总」；"
                "回合内可用 manage_task(status/wait, task_id=\"subagent-…\") 主动查询进度或等待终态，"
                "不要拖延回合空等。"
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
    return registry


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
