# -*- coding: utf-8 -*-
"""扩展名 -> languageId -> 语言服务器配置 的映射表。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ide import language_registry as reg


def test_python_extensions_map_to_python():
    assert reg.language_id_for_path("E:\\proj\\a.py") == "python"
    assert reg.language_id_for_path("/home/u/a.pyi") == "python"


def test_react_extensions_have_their_own_language_id():
    # LSP 的 languageId 里 jsx / tsx 是独立取值，不能归一成 javascript
    assert reg.language_id_for_path("a.jsx") == "javascriptreact"
    assert reg.language_id_for_path("a.tsx") == "typescriptreact"


def test_windows_and_posix_paths_are_both_handled():
    assert reg.language_id_for_path("E:\\proj\\src\\main.go") == "go"
    assert reg.language_id_for_path("/srv/src/main.go") == "go"


def test_unknown_extension_returns_empty():
    assert reg.language_id_for_path("README.txt") == ""
    assert reg.language_id_for_path("Makefile") == ""


def test_uppercase_extension_still_matches():
    assert reg.language_id_for_path("A.PY") == "python"


def test_server_id_lookup():
    assert reg.server_id_for_language("python") == "jedi"
    assert reg.server_id_for_language("typescriptreact") == "typescript"
    assert reg.server_id_for_language("cobol") == ""


def test_specs_are_internally_consistent():
    for key, spec in reg.SPECS.items():
        assert spec.server_id == key
        assert spec.language_ids, f"{key} 没有登记语言"
        assert spec.command, f"{key} 没有启动命令"
        # 一个 languageId 只能属于一个服务器，否则 didOpen 之后不知道发给谁
        for language_id in spec.language_ids:
            owners = [s.server_id for s in reg.SPECS.values() if language_id in s.language_ids]
            assert owners == [key], f"{language_id} 被 {owners} 重复登记"


def test_all_language_servers_is_sorted_and_complete():
    pairs = reg.all_language_servers()
    assert pairs == sorted(pairs)
    assert len(pairs) == sum(len(s.language_ids) for s in reg.SPECS.values())
    assert ("python", "jedi") in pairs


def test_root_markers():
    assert "pyproject.toml" in reg.root_markers_for_language("python")
    assert reg.root_markers_for_language("cobol") == ()
