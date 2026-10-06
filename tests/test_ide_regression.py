# -*- coding: utf-8 -*-
"""IDE 模式的回归守卫。

核心断言：IDE 能力对模型请求路径零影响——这是设计规格 §7.3 的硬约束，
一旦有人把编辑器内容注入上下文，这里会立刻失败。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_ide_package_does_not_import_lsp_tool():
    """core/ide 不得依赖 core/lsp.py（那是 find_definition 的实现）。"""
    import core.ide.file_service as fs

    source = open(fs.__file__, encoding="utf-8").read()
    assert "core.lsp" not in source
    assert "find_definition" not in source


def test_ide_modules_do_not_touch_prompt_builder():
    """IDE 模块不得引用提示词构造，防止把编辑器状态注入请求。"""
    import core.ide.file_service as fs
    import core.ide.layout_store as ls

    for module in (fs, ls):
        source = open(module.__file__, encoding="utf-8").read()
        for forbidden in ("build_stable_prompt", "context_builder", "system_prompt"):
            assert forbidden not in source, f"{module.__name__} 引用了 {forbidden}"


def test_ide_modules_do_not_touch_request_path():
    """静态断言：IDE 模块不得出现在模型请求构造链路上。

    字节稳定黄金测试（tests/test_prompt_byte_stability.py）由本任务 Step 4 的统一
    测试一并跑，这里刻意不用 subprocess 嵌套调用 pytest——嵌套执行会重复跑一遍
    测试、拉长耗时，且失败信息二段套娃，定位成本远高于收益。
    """
    import core.ide.file_service as fs
    import core.ide.layout_store as ls

    for module in (fs, ls):
        source = open(module.__file__, encoding="utf-8").read()
        for forbidden in ("llm_transport", "llm_native_transport", "llm_dialects", "turn_controller"):
            assert forbidden not in source, f"{module.__name__} 引用了 {forbidden}"
