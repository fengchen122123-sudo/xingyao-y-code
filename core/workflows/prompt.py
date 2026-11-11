"""工作流转译执行指令构建器（ZCode 导入设计 §3.4）。

星瑶不执行 TypeScript：``workflow_run`` 工具把「固定映射契约 + 脚本原文 +
生效参数」组装成执行指令返回，主代理在同一回合按契约把脚本的编排语义映射
到星瑶工具上完成执行。契约写死在代码里（单一事实源），不靠模型即兴发挥。
"""

from __future__ import annotations

import json

from core.workflows.frontmatter import ParsedWorkflow

WORKFLOW_EXECUTION_CONTRACT = """\
## 工作流转译执行契约

你收到的是一段工作流编排脚本（.dwf.ts，TypeScript 子集）。星瑶不直接执行 TS，
由你按下面的固定映射把编排语义落到星瑶工具上，忠实执行：

| 脚本构造 | 星瑶执行方式 |
|---|---|
| `agent(名字).ask<类型>(指令)` | 调用 run_subagent 派子代理：任务描述 = 指令原文；把脚本中 `类型` 的 interface 定义连同每个字段的注释（那就是子代理读到的字段说明）转述为「最终输出必须是符合以下结构的 JSON」追加进子代理指令 |
| `phase("名字")` | 用 plan_write 建同名计划项；进入该阶段置 in_progress、完成置 completed。阶段是脚本的骨架，一个都不能少 |
| `world.run(cmd, argv, {timeoutMs})` | 调用 execute_command 执行同一条命令（可执行文件 + 参数原样拼接）。**非零退出码是脚本用来分支的值，不是执行失败**，照常把 stdout/stderr/退出码交给后续逻辑 |
| `files.glob/read/grep`、`git.changedFiles/diff/status/log` | 用 glob_files / read_file / search_code 完成同等只读查询 |
| `report(条目)` | 在回复的「工作流产物」小节逐条输出，保持脚本给出的顺序 |
| `artifact.file/markdown(...)` | 用 write_file 把产物写入 `.ycode/workflow-artifacts/` 目录（文件名取脚本里的 id） |
| `artifact.chart/table/metrics/board` | 星瑶没有看板：降级为 Markdown 文本（表格用 Markdown 表），并在产物小节注明「看板产物已降级为文本」 |
| `log(消息)` | 一行简短进度说明 |
| `channel` / `future` / `Promise.all` | 并行执行：互不依赖的子代理或命令用并行工具调用同时发出 |
| `hole<类型>()` 留白 | 用 ask_user 向用户收集该输入后继续 |
| `model(id)` / persona 模型指定 | 子代理不逐个换模型，忽略并在开始时说明 |

不变量：

1. 忠实转译：不改写脚本意图、不增删阶段、不跳过任何 ask / world.run；
2. 一切副作用（命令、写文件、子代理）都走星瑶工具的正常权限流程，不做任何绕过；
3. 脚本顶部的 interface 定义是子代理输出契约，转述时逐字段保留注释；
4. **渐进呈现**：每进入一个阶段立即输出一行「▶ 阶段 (i/N)：名字」；每条
   report 在产出当下立即输出「📦 [工作流产物 #k] …」——不要攒到最后汇总，
   用户要实时看到进展；阶段与产物之间穿插的 log() 各占一行；
5. 结束时如实汇总已产出的全部条目与未覆盖部分——不虚报完成。"""


def build_execution_directive(
    name: str, parsed: ParsedWorkflow, effective_args: dict, run: dict | None = None
) -> str:
    """组装执行指令：任务框架 + 运行信息 + 生效参数 + 固定契约 + 脚本原文。

    ``run`` 是运行台账记录（workflow_run 已登记）；为 None 表示台账不可用，
    指令相应去掉收尾要求。
    """
    parts: list[str] = [
        f"执行星瑶工作流「{name}」：{parsed.description.strip()}",
    ]
    if parsed.when_to_use.strip():
        parts.append(f"（适用场景：{parsed.when_to_use.strip()}）")
    parts.append("")
    if run is not None:
        parts.append("## 运行信息")
        parts.append(f"- run_id：{run['id']}（收尾时传给 workflow_finish）")
        if run.get("artifact_dir"):
            parts.append(
                f"- 产物目录：{run['artifact_dir']}/（artifact.file/markdown 的文件都写这里）"
            )
        parts.append(
            "- 收尾：全部阶段结束后（无论成败）**必须**调用一次 workflow_finish："
            f"run_id=\"{run['id']}\"、ok=是否顺利完成、summary=一句话结论；"
            "失败也要如实调用（ok=false），绝不静默结束。"
        )
        parts.append("")
    parts.append("生效参数（已按声明校验并填充默认值）：")
    parts.append("```json")
    parts.append(json.dumps(effective_args, ensure_ascii=False, indent=2))
    parts.append("```")
    parts.append("")
    parts.append(WORKFLOW_EXECUTION_CONTRACT)
    parts.append("")
    parts.append("## 工作流脚本原文（转译的唯一依据）")
    parts.append("")
    parts.append("```typescript")
    parts.append(parsed.body.rstrip("\n"))
    parts.append("```")
    parts.append("")
    if run is not None:
        parts.append(
            "现在开始：先按脚本里的 phase 结构建计划，再逐阶段执行；"
            "过程中把 ▶ 阶段行与 📦 产物行实时输出，最后如实调用 workflow_finish 收尾。"
        )
    else:
        parts.append(
            "现在开始：先按脚本里的 phase 结构建计划，再逐阶段执行；"
            "过程中把 ▶ 阶段行与 📦 产物行实时输出，最后给出「工作流产物」汇总。"
        )
    return "\n".join(parts)
