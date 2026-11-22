import re
from dataclasses import dataclass
from typing import Optional

_FIRST_TOKEN = re.compile(r"\S+")


@dataclass(frozen=True)
class ParsedCommand:
    """一条命中白名单的斜杠命令。"""

    name: str
    args: str
    raw: str


def _normalize_names(known_names) -> frozenset:
    if not known_names:
        return frozenset()
    if isinstance(known_names, (set, frozenset)):
        names = known_names
    else:
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

    args = stripped[len(token):].strip()
    return ParsedCommand(name=name, args=args, raw=raw)
