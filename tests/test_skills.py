from pathlib import Path

import config
import core.skills as skills


def _skill(path: Path, name: str, description: str, body: str) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    parsed = skills.parse_skill_md(str(path))
    assert parsed is not None
    return parsed


def test_active_skill_catalog_exposes_metadata_without_body(tmp_path, monkeypatch):
    skill_path = tmp_path / "skills" / "demo" / "SKILL.md"
    parsed = _skill(skill_path, "demo", "Demo skill", "SECRET BODY")
    monkeypatch.setattr(skills, "get_active_skill_ids", lambda: [str(skill_path)])
    monkeypatch.setattr(skills, "scan_all_skills", lambda: [parsed])

    catalog = skills.get_active_skills_catalog()

    assert "demo" in catalog
    assert "Demo skill" in catalog
    assert str(skill_path) in catalog
    assert "SECRET BODY" not in catalog


def test_default_skill_scan_uses_configured_ycode_dir(tmp_path, monkeypatch):
    """YCODE_DATA_DIR 覆盖必须同时约束内置释放与默认扫描目录。"""
    configured_dir = tmp_path / "isolated-ycode"
    configured_skills = configured_dir / "skills"
    configured_skills.mkdir(parents=True)
    legacy_home = tmp_path / "legacy-home"
    (legacy_home / ".ycode" / "skills").mkdir(parents=True)

    monkeypatch.setattr(config, "YCODE_DIR", str(configured_dir))
    monkeypatch.setattr(skills, "sync_builtin_skills", lambda: None)
    monkeypatch.setattr(skills.os.path, "expanduser", lambda _value: str(legacy_home))

    paths = [Path(path).resolve() for path in skills.get_skill_dirs()]

    assert configured_skills.resolve() in paths
    assert (legacy_home / ".ycode" / "skills").resolve() not in paths


def test_active_skill_catalog_is_stable_and_compatibility_api_is_lazy(
    tmp_path, monkeypatch
):
    z_path = tmp_path / "skills" / "zeta" / "SKILL.md"
    a_path = tmp_path / "skills" / "alpha" / "SKILL.md"
    available = [
        _skill(z_path, "zeta", "Z skill", "Z BODY"),
        _skill(a_path, "alpha", "A skill", "A BODY"),
    ]
    monkeypatch.setattr(
        skills,
        "get_active_skill_ids",
        lambda: [str(z_path), str(a_path)],
    )
    monkeypatch.setattr(skills, "scan_all_skills", lambda: available)

    catalog = skills.get_active_skills_catalog()

    assert catalog.index("alpha") < catalog.index("zeta")
    assert "A BODY" not in catalog
    assert "Z BODY" not in catalog
    assert skills.get_active_skills_markdown() == catalog


def test_active_skill_ids_atomic_write_and_backup_recovery(tmp_path, monkeypatch):
    """active_skills.json 原子写可回读；主文件损坏时从 .bak 恢复而不是静默返回 []"""
    active_file = tmp_path / "active_skills.json"
    monkeypatch.setattr(skills, "ACTIVE_SKILLS_FILE", str(active_file))

    ids = ["C:/skills/demo/SKILL.md", "C:/skills/alpha/SKILL.md"]
    skills.save_active_skill_ids(ids)
    assert skills.get_active_skill_ids() == ids

    # 再写一次以生成 .bak，然后破坏主文件
    skills.save_active_skill_ids(ids)
    assert (tmp_path / "active_skills.json.bak").exists()
    active_file.write_text("[broken", encoding="utf-8")

    assert skills.get_active_skill_ids() == ids


def test_catalog_header_is_action_directive_not_passive_note(tmp_path, monkeypatch):
    """目录头部必须是行动指令（先扫目录、轻微相关即读），而非被动的状态陈述"""
    skill_path = tmp_path / "skills" / "demo" / "SKILL.md"
    parsed = _skill(skill_path, "demo", "Demo skill", "BODY")
    monkeypatch.setattr(skills, "get_active_skill_ids", lambda: [str(skill_path)])
    monkeypatch.setattr(skills, "scan_all_skills", lambda: [parsed])

    catalog = skills.get_active_skills_catalog()

    assert "先扫一遍本目录" in catalog
    assert "哪怕轻微相关" in catalog
    assert "不要为了确认是否相关而加载无关技能" not in catalog


def test_catalog_appends_trigger_hint_for_english_description(tmp_path, monkeypatch):
    """英文描述的技能追加极简中文触发词；中文描述不追加（自身已含触发语义）"""
    en_path = tmp_path / "skills" / "systematic-debugging" / "SKILL.md"
    zh_path = tmp_path / "skills" / "mes-db" / "SKILL.md"
    en = _skill(en_path, "systematic-debugging", "Use when encountering any bug", "BODY")
    zh = _skill(zh_path, "mes-db", "连接并操作本地 SQL Server 数据库", "BODY")
    monkeypatch.setattr(skills, "get_active_skill_ids", lambda: [str(en_path), str(zh_path)])
    monkeypatch.setattr(skills, "scan_all_skills", lambda: [en, zh])

    catalog = skills.get_active_skills_catalog()

    assert "（触发:排查/报错/异常/bug）" in catalog
    zh_line = next(line for line in catalog.splitlines() if "mes-db" in line)
    assert "触发:" not in zh_line


def test_frontmatter_triggers_override_builtin_hints(tmp_path, monkeypatch):
    """SKILL.md frontmatter 里的 triggers: 字段优先于内置触发词表"""
    skill_path = tmp_path / "skills" / "systematic-debugging" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(
        "---\nname: systematic-debugging\ndescription: Use when debugging\ntriggers: 自定义触发词\n---\nBODY\n",
        encoding="utf-8",
    )
    parsed = skills.parse_skill_md(str(skill_path))
    monkeypatch.setattr(skills, "get_active_skill_ids", lambda: [str(skill_path)])
    monkeypatch.setattr(skills, "scan_all_skills", lambda: [parsed])

    catalog = skills.get_active_skills_catalog()

    assert "（触发:自定义触发词）" in catalog
    assert "排查/报错" not in catalog


def test_skill_catalog_bounds_untrusted_metadata(tmp_path, monkeypatch):
    skill_path = tmp_path / "skills" / "demo" / "SKILL.md"
    parsed = _skill(
        skill_path,
        "n" * (skills.SKILL_NAME_MAX_CHARS + 20),
        "d" * (skills.SKILL_DESCRIPTION_MAX_CHARS + 20),
        "BODY",
    )
    parsed["triggers"] = "t" * (skills.SKILL_TRIGGERS_MAX_CHARS + 20)
    monkeypatch.setattr(skills, "get_active_skill_ids", lambda: [str(skill_path)])
    monkeypatch.setattr(skills, "scan_all_skills", lambda: [parsed])

    catalog = skills.get_active_skills_catalog()

    assert "n" * (skills.SKILL_NAME_MAX_CHARS + 1) not in catalog
    assert "d" * (skills.SKILL_DESCRIPTION_MAX_CHARS + 1) not in catalog
    assert "t" * (skills.SKILL_TRIGGERS_MAX_CHARS + 1) not in catalog


def test_scan_skills_rejects_symlink_outside_search_root(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: escaped\ndescription: outside\n---\nBODY\n",
        encoding="utf-8",
    )
    try:
        (root / "escaped").symlink_to(outside, target_is_directory=True)
    except OSError:
        import pytest
        pytest.skip("当前 Windows 环境不允许创建测试符号链接")
    monkeypatch.setattr(skills, "get_skill_dirs", lambda: [str(root)])

    assert skills.scan_all_skills() == []


def _source_env(tmp_path, monkeypatch):
    """隔离内置技能源目录、用户技能目录、索引与释放记录，避免触碰真实 ~/.ycode。

    返回 (内置源目录, 用户技能目录 base, config.YCODE_DIR)。
    """
    src = tmp_path / "builtin_src"
    src.mkdir()
    ycode_dir = tmp_path / "user_home" / ".ycode"
    ycode_dir.mkdir(parents=True)
    monkeypatch.setattr(skills, "_BUILTIN_SYNC_DONE", False)
    monkeypatch.setattr(skills, "_builtin_skills_dir", lambda: str(src))
    monkeypatch.setattr(config, "YCODE_DIR", str(ycode_dir))
    monkeypatch.setattr(skills, "ACTIVE_SKILLS_FILE", str(tmp_path / "active_skills.json"))
    return src, ycode_dir / "skills", ycode_dir


def _write_skill(skill_dir: Path, name: str, body: str = "BODY") -> Path:
    """在 skill_dir 下写一个最小可用 SKILL.md，返回该文件路径。"""
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_classify_skill_source_builtin_when_fingerprint_matches_index(tmp_path, monkeypatch):
    """场景 a：目录名命中索引且当前指纹与索引记录一致 → 判为内置。"""
    src, _user, _yc = _source_env(tmp_path, monkeypatch)
    skill_dir = tmp_path / "skills" / "demo"
    _write_skill(skill_dir, "demo")
    fingerprint = skills._skill_dir_fingerprint(str(skill_dir))
    assert fingerprint

    index = {"demo": fingerprint}

    assert skills.classify_skill_source(str(skill_dir), index) == "builtin"


def test_classify_skill_source_user_when_indexed_but_content_modified(tmp_path, monkeypatch):
    """场景 b：目录名命中索引，但内容被用户改动（指纹不符）→ 判为用户。"""
    src, _user, _yc = _source_env(tmp_path, monkeypatch)
    skill_dir = tmp_path / "skills" / "demo"
    _write_skill(skill_dir, "demo")
    recorded = skills._skill_dir_fingerprint(str(skill_dir))

    # 用户改动技能内容
    (skill_dir / "SKILL.md").write_text("用户改写内容", encoding="utf-8")

    assert skills.classify_skill_source(str(skill_dir), {"demo": recorded}) == "user"


def test_classify_skill_source_user_when_name_absent_from_index(tmp_path, monkeypatch):
    """场景 c：目录名不在索引中（用户自建技能）→ 判为用户。"""
    src, _user, _yc = _source_env(tmp_path, monkeypatch)
    skill_dir = tmp_path / "skills" / "mine"
    _write_skill(skill_dir, "mine")

    assert skills.classify_skill_source(str(skill_dir), {}) == "user"
    assert skills.classify_skill_source(str(skill_dir), {"other": "x"}) == "user"


def test_classify_skill_source_builtin_when_under_builtin_dir(tmp_path, monkeypatch):
    """场景 d：技能目录直接位于 _builtin_skills_dir() 之下 → 判为内置（无需索引）。"""
    src, _user, _yc = _source_env(tmp_path, monkeypatch)
    skill_dir = src / "demo"
    _write_skill(skill_dir, "demo")

    assert skills.classify_skill_source(str(skill_dir), {}) == "builtin"


def test_scan_all_skills_each_item_exposes_valid_source(tmp_path, monkeypatch):
    """场景 e：scan_all_skills 每个技能字典都带 source 键，取值只能是 builtin/user。"""
    src, _user, _yc = _source_env(tmp_path, monkeypatch)
    skills_root = tmp_path / "skills"
    builtin_dir = skills_root / "demo"
    user_dir = skills_root / "mine"
    _write_skill(builtin_dir, "demo")
    _write_skill(user_dir, "mine")

    # 只把 "demo" 记为内置索引且指纹一致；"mine" 用户自建不入索引
    config._atomic_write_json(
        skills._builtin_skills_index_file(),
        {"demo": skills._skill_dir_fingerprint(str(builtin_dir))},
        indent=None,
    )
    monkeypatch.setattr(skills, "get_skill_dirs", lambda: [str(skills_root)])

    found = skills.scan_all_skills()

    assert len(found) == 2
    for info in found:
        assert "source" in info
        assert info["source"] in {"builtin", "user"}
    sources = {info["name"]: info["source"] for info in found}
    assert sources["demo"] == "builtin"
    assert sources["mine"] == "user"


def test_sync_indexes_only_unmodified_released_skills(tmp_path, monkeypatch):
    """场景 f：sync 后为「真正释放且内容未改」的技能补索引；同名不同内容不补。"""
    src, user_skills, _yc = _source_env(tmp_path, monkeypatch)
    # 内置源：plugin-creator（将被释放）、user-mod（用户已有同名不同内容）
    _write_skill(src / "plugin-creator", "plugin-creator", body="内置正文")
    _write_skill(src / "user-mod", "user-mod", body="内置正文")
    # 用户事先已存在同名技能，且内容与内置源不同
    _write_skill(user_skills / "user-mod", "user-mod", body="用户自建内容")

    skills.sync_builtin_skills()

    index = skills.load_builtin_skill_index()
    # 全新释放且内容未改的 plugin-creator 被写入索引
    assert "plugin-creator" in index
    assert index["plugin-creator"] == skills._skill_dir_fingerprint(
        str(user_skills / "plugin-creator")
    )
    # 用户已有同名且内容不同的 user-mod 不写入索引
    assert "user-mod" not in index

    # 来源判定：释放的技能判为内置，用户技能判为用户
    assert skills.classify_skill_source(str(user_skills / "plugin-creator")) == "builtin"
    assert skills.classify_skill_source(str(user_skills / "user-mod")) == "user"

def test_scan_all_skills_classifies_builtin_on_first_scan_before_index_exists(
    tmp_path, monkeypatch
):
    """进程内首扫即触发 sync 补齐索引：已释放的内置技能必须判为 builtin（回归）。

    复现缺陷：scan_all_skills 若先 load 索引再调 get_skill_dirs()，首扫会读到空索引，
    把已释放的内置技能误标为用户。修复后顺序为 get_skill_dirs() -> load 索引 -> 遍历。

    隔离方式说明：不直接替换 get_skill_dirs（那会绕过其内部触发的 sync_builtin_skills()），
    而是保留真实 get_skill_dirs 链路，仅替换更内层的目录来源——让 config.YCODE_DIR/skills
    恰好等于被扫描的用户技能目录 dest，并屏蔽自定义搜索路径与 $HOME/.agents，使
    get_skill_dirs() 只返回 [dest]，且首次调用仍会触发 sync 并补齐索引。
    """
    import os
    import shutil

    src, dest, _ycode_dir = _source_env(tmp_path, monkeypatch)
    skill_name = "plugin-creator"
    _write_skill(src / skill_name, skill_name, body="builtin body")

    # 模拟"已释放"：把内置技能目录内容原样复制到用户技能目录
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src / skill_name, dest / skill_name)
    # released 记录该技能名；索引文件故意不存在
    config._atomic_write_json(skills._builtin_released_file(), [skill_name], indent=None)
    index_file = skills._builtin_skills_index_file()
    assert not os.path.exists(index_file)

    # 屏蔽真实目录来源，保证扫描目录仅为 dest，同时保留 get_skill_dirs 内部的 sync 链路
    monkeypatch.setattr(skills, "get_user_skill_search_paths", lambda: [])
    monkeypatch.setattr(skills.os.path, "expanduser", lambda _value: str(tmp_path / "fake_home"))

    found = skills.scan_all_skills()

    by_name = {info["name"]: info for info in found}
    assert skill_name in skills.load_builtin_skill_index()


def test_skill_source_label_normalizes_unknown_to_user():
    """来源标签归一：仅 source == "builtin" 显示"内置"，缺失/非法/非字典一律回落"用户"。

    该函数供 CLI 三处展示入口共用（/skills list、技能快捷指令下拉、全屏选择器），
    保守回落保证未知来源不会被误标为内置。
    """
    assert skills.skill_source_label({"source": "builtin"}) == "内置"
    assert skills.skill_source_label({"source": "user"}) == "用户"
    assert skills.skill_source_label({}) == "用户"
    assert skills.skill_source_label({"source": None}) == "用户"
    # 大小写敏感：后端契约只产出小写 "builtin"，其余取值不算内置
    assert skills.skill_source_label({"source": "BUILTIN"}) == "用户"
    assert skills.skill_source_label(None) == "用户"


def test_gui_bridge_refresh_skills(tmp_path, monkeypatch):
    """GUI ApiBridge.refresh_skills 必须能重新扫描并返回最新技能列表。"""
    import gui_bridge
    from gui_bridge import ApiBridge

    skill_path = tmp_path / "skills" / "refreshed_demo" / "SKILL.md"
    parsed = _skill(skill_path, "refreshed_demo", "Refreshed demo", "BODY")
    monkeypatch.setattr(gui_bridge, "get_active_skill_ids", lambda: [str(skill_path)])
    monkeypatch.setattr(gui_bridge, "scan_all_skills", lambda: [parsed])

    bridge = ApiBridge.__new__(ApiBridge)
    res = bridge.refresh_skills()

    assert res["ok"] is True
    assert len(res["skills"]) == 1
    assert res["skills"][0]["name"] == "refreshed_demo"
    assert res["skills"][0]["enabled"] is True

    direct = bridge.get_skills()
    assert len(direct) == 1
    assert direct[0]["name"] == "refreshed_demo"

