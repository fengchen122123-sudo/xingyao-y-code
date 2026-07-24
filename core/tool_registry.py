"""Capability registry for the built-in tools."""

from __future__ import annotations

from typing import Any, Iterable

from core.tool_protocol import ToolResult, ToolSpec


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

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def all(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def api_schemas(self) -> list[dict[str, Any]]:
        return [spec.api_schema() for spec in self._specs.values()]

    def execute(self, name: str, **arguments: Any) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            return ToolResult(content=f"Unknown tool: {name}", is_error=True)
        try:
            result = spec.execute(**arguments)
            if isinstance(result, ToolResult):
                return result
            return ToolResult(content=str(result) if result is not None else "")
        except Exception as exc:
            return ToolResult(content=f"Tool execution error: {exc}", is_error=True)


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def build_default_registry() -> ToolRegistry:
    from core import tools
    from core import lsp as _lsp
    from core.ask_user import ask_user
    from core.plan import plan_read, plan_retry, plan_update, plan_write

    read_specs = [
        ToolSpec(
            name="read_file",
            description="读取指定文件的文本内容。默认仅限当前工作目录内（用户可通过 /sandbox on 开放）；默认最多返回 2000 行，大文件（>50KB）自动只读前 500 行并提示分页；compress=False 默认返回完全原始内容，设 compress=True 可压缩（去行尾空白、合并空行）以节省 token 但会与原文不完全一致（编辑前需 compress=False 重读以保证 edit_file 匹配）。",
            input_schema=_schema({
                "file_path": {"type": "string", "description": "当前工作目录内的文件路径"},
                "offset": {"type": "integer", "default": 0, "description": "从第几行开始读取（0 起）"},
                "limit": {"type": "integer", "description": "最多读取的行数（上限 2000）"},
                "compress": {"type": "boolean", "default": False, "description": "是否压缩文本（去行尾空白、合并空行）。默认 False 保留原始内容；浏览理解时可设 True 省 token，但编辑前需用 compress=False 重读"},
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
            description="将 failed/blocked/cancelled 状态的计划项重置为 pending，以便重新通过 run_subagent 执行。",
            input_schema=_schema({
                "item_id": {"type": "string", "description": "要重置的计划项 id"},
            }, ["item_id"]),
            execute=plan_retry,
            activity_description="重试计划项",
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
            description="以字符串替换的方式局部修改已有文件（首选工具）。默认仅限当前工作目录内（用户可通过 /sandbox on 开放）；系统会尝试智能定位 target_content，即使轻微截断/空白差异也能匹配。target_content 应尽量短小并包含唯一上下文；出现多次时需补充上下文或显式设置 replace_all。单次 target_content 不得超过 1500 字符，更大改动请拆分为多次小改；严禁用 write_file 覆盖已有大文件来替代 edit_file。",
            input_schema=_schema({
                "file_path": {"type": "string", "description": "当前工作目录内的文件路径"},
                "target_content": {"type": "string"},
                "replacement_content": {"type": "string"},
                "replace_all": {"type": "boolean", "default": False, "description": "是否替换全部出现位置"},
            }, ["file_path", "target_content", "replacement_content"]),
            execute=tools.edit_file,
            mutates_files=True,
            activity_description="修改文件",
        ),
        ToolSpec(
            name="execute_command",
            description=(
                "执行本地命令。前台执行默认 30 秒超时，可用 timeout_seconds 调整（上限 600 秒）。"
                "注意：PowerShell 中 $env:XXX 这类变量在单引号字符串内不会扩展，应使用双引号包裹"
                "（例如 -OutFile \"$env:TEMP\\pkg.zip\"）。"
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
            }, ["command"]),
            execute=tools.execute_command,
            activity_description="执行命令",
        ),
        ToolSpec(
            name="manage_task",
            description="查看或终止后台任务。list/status 为只读操作不弹确认；仅 kill 操作会请求用户确认。",
            input_schema=_schema({
                "action": {"type": "string"},
                "task_id": {"type": "string"},
            }, ["action"]),
            execute=tools.manage_task,
            requires_permission=False,
            activity_description="管理后台任务",
        ),
        ToolSpec(
            name="run_subagent",
            description="启动辅助 Agent；默认前台等待结果，仅独立任务使用后台模式。",
            input_schema=_schema({
                "task_description": {"type": "string"},
                "subagent_type": {
                    "type": "string",
                    "enum": ["explore", "implement", "test", "review", "general"],
                    "default": "general",
                },
                "plan_item_id": {
                    "type": "string",
                    "default": "",
                    "description": "与当前计划项关联的 ID；有计划时必须填写。",
                },
                "run_in_background": {"type": "boolean", "default": False},
                "timeout_seconds": {
                    "type": "integer",
                    "default": 600,
                    "minimum": 30,
                    "maximum": 3600,
                    "description": "默认 600 秒；简单只读任务可调小，复杂多步任务可调大。",
                },
            }, ["task_description"]),
            execute=tools.run_subagent,
            requires_permission=False,
            parallel_safe=True,
            activity_description="启动子 Agent",
        ),
    ]
    return ToolRegistry([*read_specs, *mutating_specs])
