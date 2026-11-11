"""斜杠命令解析：把输入文本切成命令名与参数，不做执行与权限判断。

解析层是 GUI / CLI / LAN 等入口共用的第一道分流：命中白名单才返回 ParsedCommand，
否则返回 None 由调用方按普通消息处理。这样 `/usr/bin 是什么` 这类提问不会被误吞。
"""

import re
from dataclasses import dataclass
from typing import Optional

# 首个 token = 一段连续非空白字符。\S 天然不跨行，所以多行粘贴时只有首行首个
# token 参与命令名匹配，后续行一律当参数内容。
_FIRST_TOKEN = re.compile(r"\S+")


@dataclass(frozen=True)
class ParsedCommand:
    """一条命中白名单的斜杠命令。"""

    name: str  # 小写命令名，含前导斜杠，例如 "/fix"
    args: str  # 参数原文：保留大小写、内部空格与换行，仅去掉首尾空白
    raw: str  # 调用方传入的原始文本，回显与日志需要


def _normalize_names(known_names) -> frozenset:
    """把白名单归一化为小写集合，使配置写法（大小写、空白）不影响匹配。"""
    if not known_names:
        return frozenset()
    if isinstance(known_names, (set, frozenset)):
        names = known_names
    else:
        # known_names 可能是 dict 视图或生成器，逐个消费
        names = tuple(known_names)
    return frozenset(str(item).strip().lower() for item in names if str(item).strip())


def parse_slash_command(text, known_names) -> Optional[ParsedCommand]:
    """解析斜杠命令；不是命令或未命中白名单时返回 None。

    规则（设计文档 §4）：
      - 首个非空白字符必须是 "/"，首行以 "//" 开头视为转义，按普通文本发送；
      - 首个 token 转小写后必须命中 known_names，未命中绝不报错；
      - 参数为命令名之后的整段原文，跨行内容整体保留，不按空格拆分。
    """
    raw = "" if text is None else str(text)
    stripped = raw.lstrip()
    if not stripped.startswith("/") or stripped.startswith("//"):
        return None

    match = _FIRST_TOKEN.match(stripped)
    if match is None:  # 理论上不可达（首字符已是 "/"），保守返回 None
        return None
    token = match.group(0)
    name = token.lower()
    if name not in _normalize_names(known_names):
        return None

    # 用切掉 token 的方式取参数而不是再按空格切分：这样多行参数、连续空格
    # 都能原样保留，只由 strip() 去掉首尾空白。
    args = stripped[len(token):].strip()
    return ParsedCommand(name=name, args=args, raw=raw)
