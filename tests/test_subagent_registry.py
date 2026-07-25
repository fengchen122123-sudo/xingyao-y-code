"""自定义子 Agent 注册中心测试。

覆盖：
- AGENT.md frontmatter 解析（完整字段/最小配置/无效字段回退）
- name 与内置类型重名跳过
- body 为空跳过
- allowed_tools 逗号分隔解析
- mtime 指纹缓存
- get_custom_subagent 按名称查找
- get_custom_subagents_catalog 清单格式
- 加载失败容错
"""

from pathlib import Path

import core.subagent_registry as reg


def _reset_cache():
    """每个测试前重置模块级缓存，避免互相干扰。"""
    with reg._cache_lock:
        reg._cache["fingerprint"] = None
        reg._cache["subagents"] = []


def _write_agent(path: Path, frontmatter: str, body: str) -> None:
    """写入一个 AGENT.md 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def test_parse_full_frontmatter(tmp_path, monkeypatch):
    """完整字段解析：name/description/when_to_use/base_type/model/allowed_tools/color"""
    _reset_cache()
    agent_file = tmp_path / "code-reviewer.md"
    _write_agent(
        agent_file,
        (
            'name: code-reviewer\n'
            'description: 审查代码质量\n'
            'when_to_use: 需要代码审查时\n'
            'base_type: review\n'
            'model: secondary\n'
            'allowed_tools: read_file, search_code, glob_files\n'
            'color: "#E67E22"'
        ),
        "你是专业代码审查员。",
    )
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(tmp_path)])

    subagents = reg.scan_custom_subagents()

    assert len(subagents) == 1
    s = subagents[0]
    assert s["name"] == "code-reviewer"
    assert s["description"] == "审查代码质量"
    assert s["when_to_use"] == "需要代码审查时"
    assert s["base_type"] == "review"
    assert s["model"] == "secondary"
    assert s["allowed_tools"] == {"read_file", "search_code", "glob_files"}
    assert s["color"] == "#E67E22"
    assert s["system_prompt"] == "你是专业代码审查员。"
    assert s["source"] == str(agent_file)


def test_parse_minimal_frontmatter(tmp_path, monkeypatch):
    """最小配置：只有 name 和正文，其他字段用默认值"""
    _reset_cache()
    agent_file = tmp_path / "helper.md"
    _write_agent(agent_file, "name: helper", "你是一个通用助手。")
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(tmp_path)])

    subagents = reg.scan_custom_subagents()

    assert len(subagents) == 1
    s = subagents[0]
    assert s["name"] == "helper"
    assert s["description"] == "无描述"
    assert s["when_to_use"] == "无描述"  # 未配置时回退到 description
    assert s["base_type"] == "general"
    assert s["model"] == "inherit"
    assert s["allowed_tools"] is None  # 未配置，继承 base_type 默认
    assert s["color"] is None


def test_builtin_name_collision_skipped(tmp_path, monkeypatch):
    """name 与内置 5 种类型重名时跳过"""
    _reset_cache()
    _write_agent(tmp_path / "explore.md", "name: explore", "覆盖内置 explore？不行。")
    _write_agent(tmp_path / "general.md", "name: General", "覆盖内置 general？不行。")
    _write_agent(tmp_path / "valid.md", "name: custom-explorer", "这个可以。")
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(tmp_path)])

    subagents = reg.scan_custom_subagents()

    names = [s["name"] for s in subagents]
    assert "custom-explorer" in names
    assert "explore" not in names
    assert "General" not in names


def test_empty_body_skipped(tmp_path, monkeypatch):
    """body 为空时跳过"""
    _reset_cache()
    _write_agent(tmp_path / "empty.md", "name: empty-agent", "")
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(tmp_path)])

    subagents = reg.scan_custom_subagents()

    assert len(subagents) == 0


def test_invalid_base_type_falls_back_to_general(tmp_path, monkeypatch):
    """base_type 无效时回退 general 并记录警告"""
    _reset_cache()
    _write_agent(
        tmp_path / "weird.md",
        "name: weird-agent\nbase_type: unknown_type",
        "正文。",
    )
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(tmp_path)])

    subagents = reg.scan_custom_subagents()

    assert len(subagents) == 1
    assert subagents[0]["base_type"] == "general"


def test_invalid_model_falls_back_to_inherit(tmp_path, monkeypatch):
    """model 无效时回退 inherit"""
    _reset_cache()
    _write_agent(
        tmp_path / "bad-model.md",
        "name: bad-model-agent\nmodel: gpt-4",
        "正文。",
    )
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(tmp_path)])

    subagents = reg.scan_custom_subagents()

    assert len(subagents) == 1
    assert subagents[0]["model"] == "inherit"


def test_malformed_file_skipped(tmp_path, monkeypatch):
    """文件格式损坏（无 frontmatter）时跳过，不影响其他文件"""
    _reset_cache()
    bad_file = tmp_path / "broken.md"
    bad_file.parent.mkdir(parents=True, exist_ok=True)
    bad_file.write_text("这是纯文本，没有 frontmatter。", encoding="utf-8")
    _write_agent(tmp_path / "good.md", "name: good-agent", "正文。")
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(tmp_path)])

    subagents = reg.scan_custom_subagents()

    names = [s["name"] for s in subagents]
    assert "good-agent" in names
    assert len(subagents) == 1


def test_mtime_fingerprint_cache(tmp_path, monkeypatch):
    """文件未变更时返回缓存结果，变更后重新扫描"""
    _reset_cache()
    agent_file = tmp_path / "cached.md"
    _write_agent(agent_file, "name: cached-agent", "初始正文。")
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(tmp_path)])

    first = reg.scan_custom_subagents()
    assert len(first) == 1
    assert first[0]["system_prompt"] == "初始正文。"

    # 第二次调用应命中缓存（返回新列表，但内容相同）
    second = reg.scan_custom_subagents()
    assert len(second) == 1
    assert second[0]["system_prompt"] == "初始正文。"

    # 修改文件后应重新扫描
    _write_agent(agent_file, "name: cached-agent", "更新后的正文。")
    third = reg.scan_custom_subagents()
    assert len(third) == 1
    assert third[0]["system_prompt"] == "更新后的正文。"


def test_get_custom_subagent_by_name(tmp_path, monkeypatch):
    """按名称查找单个自定义子 agent"""
    _reset_cache()
    _write_agent(tmp_path / "finder.md", "name: finder\ndescription: 查找器", "正文。")
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(tmp_path)])

    found = reg.get_custom_subagent("finder")
    assert found is not None
    assert found["name"] == "finder"

    assert reg.get_custom_subagent("nonexistent") is None
    assert reg.get_custom_subagent("") is None
    assert reg.is_custom_subagent("finder") is True
    assert reg.is_custom_subagent("nonexistent") is False


def test_get_custom_subagent_case_insensitive(tmp_path, monkeypatch):
    """名称查找大小写不敏感"""
    _reset_cache()
    _write_agent(tmp_path / "Reviewer.md", "name: Code-Reviewer", "正文。")
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(tmp_path)])

    assert reg.get_custom_subagent("code-reviewer") is not None
    assert reg.get_custom_subagent("CODE-REVIEWER") is not None
    assert reg.get_custom_subagent("Code-Reviewer") is not None


def test_catalog_format(tmp_path, monkeypatch):
    """get_custom_subagents_catalog 清单格式正确"""
    _reset_cache()
    _write_agent(
        tmp_path / "a.md",
        "name: alpha\ndescription: Alpha agent\nwhen_to_use: A 场景\nbase_type: review",
        "正文 A。",
    )
    _write_agent(
        tmp_path / "b.md",
        "name: beta\ndescription: Beta agent\nmodel: secondary\nallowed_tools: read_file",
        "正文 B。",
    )
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(tmp_path)])

    catalog = reg.get_custom_subagents_catalog()

    assert "【可用自定义子 Agent】" in catalog
    assert "alpha" in catalog
    assert "Alpha agent" in catalog
    assert "A 场景" in catalog
    assert "继承 review 工具权限" in catalog
    assert "beta" in catalog
    assert "Beta agent" in catalog
    assert "read_file" in catalog
    assert "副模型" in catalog
    # 正文不应出现在清单中
    assert "正文 A。" not in catalog
    assert "正文 B。" not in catalog


def test_catalog_empty_when_no_agents(tmp_path, monkeypatch):
    """无自定义子 agent 时清单为空字符串"""
    _reset_cache()
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(tmp_path)])

    assert reg.get_custom_subagents_catalog() == ""


def test_duplicate_name_skipped(tmp_path, monkeypatch):
    """同名自定义子 agent 后加载的跳过"""
    _reset_cache()
    dir1 = tmp_path / "dir1"
    dir2 = tmp_path / "dir2"
    _write_agent(dir1 / "dup.md", "name: dup-agent", "第一个。")
    _write_agent(dir2 / "dup.md", "name: dup-agent", "第二个。")
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(dir1), str(dir2)])

    subagents = reg.scan_custom_subagents()

    assert len(subagents) == 1
    assert subagents[0]["system_prompt"] == "第一个。"


def test_allowed_tools_with_whitespace(tmp_path, monkeypatch):
    """allowed_tools 字段含空白时正确解析"""
    _reset_cache()
    _write_agent(
        tmp_path / "ws.md",
        "name: ws-agent\nallowed_tools: read_file ,  search_code , glob_files",
        "正文。",
    )
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(tmp_path)])

    subagents = reg.scan_custom_subagents()

    assert len(subagents) == 1
    assert subagents[0]["allowed_tools"] == {"read_file", "search_code", "glob_files"}


def test_scan_handles_nonexistent_dir_gracefully(tmp_path, monkeypatch):
    """扫描不存在的目录时不报错"""
    _reset_cache()
    nonexistent = tmp_path / "does-not-exist"
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(nonexistent)])

    subagents = reg.scan_custom_subagents()
    assert subagents == []
