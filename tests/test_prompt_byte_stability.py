"""字节稳定性 Golden 测试：前缀缓存的第一道 CI 守卫。

任何让 system prompt / tools schema 字节输出变得不确定的改动
（时间戳注入、map 序遍历、随机 ID）都应在这里当场失败。
"""

import json


def test_stable_prompt_is_byte_identical_across_builds():
    from core.agent_service import load_system_prompt
    from core.context_builder import build_stable_prompt
    first = build_stable_prompt(load_system_prompt())
    second = build_stable_prompt(load_system_prompt())
    assert first == second


def test_tools_schema_serialization_is_deterministic():
    from core.llm import get_tools_schema

    def canonical():
        return json.dumps(get_tools_schema(), ensure_ascii=False, sort_keys=True)

    assert canonical() == canonical()


def test_tools_schema_order_is_stable():
    from core.llm import get_tools_schema
    names_a = [t["function"]["name"] for t in get_tools_schema()]
    names_b = [t["function"]["name"] for t in get_tools_schema()]
    assert names_a == names_b
