"""hunyuan_image：腾讯混元生图插件入口（薄壳）。

工具实现位于 core.hunyuan_image（随主程序编译进 PYZ 分发），本文件只负责
注册工具与转发调用，不含 API Key 及任何请求逻辑——安装包中插件目录即使
明文可见也不泄露秘密。
"""

from __future__ import annotations

from core.hunyuan_image import VALID_SIZES, generate_image
from core.tool_protocol import ToolSpec


def register(registry):
    registry.register(ToolSpec(
        name="hunyuan_generate_image",
        description=(
            "腾讯混元生图：根据文字描述（中英文均可，越具体越好，建议 500 字以内）"
            "生成一张图片并下载保存到本地，返回本地文件路径。生成耗时约 10~60 秒。"
            "【主动使用时机】用户明确要求生成图片/海报/封面/配图时；"
            "或需要精美演示图且 mermaid 代码块无法表达（如封面、艺术化示意图）时。"
            "【不要主动使用】架构图/流程图/时序图优先输出 mermaid 代码块而非生图；"
            "同一主题已生成过图片不重复生成；纯代码修改与问答任务不生图。"
            "支持图生图：传入 image 垫图参数后，以该图为基础按 prompt 修改画面"
            "（如换背景、改风格）。"
            "生成成功后必须在回复中用 Markdown 图片语法 ![](本地路径) 直接展示图片，"
            "不要只回复文件路径。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "图片描述，中文英文均可，越具体效果越好",
                },
                "size": {
                    "type": "string",
                    "enum": list(VALID_SIZES),
                    "description": "图片尺寸，默认 1024x1024；横幅用 1280x720，竖幅用 720x1280",
                },
                "save_dir": {
                    "type": "string",
                    "description": "图片保存目录；不传则落到 AI 生成内容目录（设置页可配置，默认 ~/.ycode/tmp）",
                },
                "footnote": {
                    "type": "string",
                    "description": "自定义水印文字，默认一个空格（等效无可见水印）；传具体文字（如：星瑶）则作为水印内容",
                },
                "image": {
                    "type": "string",
                    "description": "垫图本地路径（可选，图生图模式）：传入后以此图为基础按 prompt 修改画面；仅支持 1 张、≤10MB 的 jpg/jpeg/png",
                },
            },
            "required": ["prompt"],
        },
        execute=generate_image,
        read_only=False,
        mutates_files=True,          # 会下载图片写入本地文件
        requires_permission=True,    # 写操作走用户审批
        cancellable=True,            # 生图耗时长，允许用户中断
        parallel_safe=False,
        activity_description="混元生图中（约 10~60 秒）",
    ))
