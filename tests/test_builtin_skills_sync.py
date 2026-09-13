"""core/skills.py 内置技能释放（sync_builtin_skills）的单元测试。"""

import pytest

import config
from core import skills


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    """隔离内置技能源目录、用户技能目录与激活列表文件。"""
    src = tmp_path / "builtin"
    src.mkdir()
    user_base = tmp_path / "user_home" / ".ycode" / "skills"
    active_file = tmp_path / "active_skills.json"

    monkeypatch.setattr(skills, "_BUILTIN_SYNC_DONE", False)
    monkeypatch.setattr(skills, "_builtin_skills_dir", lambda: str(src))
    monkeypatch.setattr(skills, "ACTIVE_SKILLS_FILE", str(active_file))
    # 技能释放目录/发布标记统一读 config.YCODE_DIR（D4 修复后不再硬编码 ~/.ycode）
    monkeypatch.setattr(config, "YCODE_DIR", str(tmp_path / "user_home" / ".ycode"))
    return src, user_base, active_file


def _make_builtin_skill(src, name, body="# 技能正文"):
    d = src / name
    d.mkdir()
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}\n", encoding="utf-8")
    return d


def test_first_sync_copies_and_enables(sync_env):
    src, user_base, active_file = sync_env
    _make_builtin_skill(src, "plugin-creator")

    skills.sync_builtin_skills()

    assert (user_base / "plugin-creator" / "SKILL.md").is_file()
    ids = skills.get_active_skill_ids()
    assert len(ids) == 1
    assert ids[0].endswith("SKILL.md")
    assert "plugin-creator" in ids[0]


def test_sync_does_not_overwrite_user_modifications(sync_env):
    src, user_base, _ = sync_env
    _make_builtin_skill(src, "plugin-creator")
    skills.sync_builtin_skills()
    # 用户修改了已释放的技能
    (user_base / "plugin-creator" / "SKILL.md").write_text("用户自定义内容", encoding="utf-8")

    monkeypatch_done = skills._BUILTIN_SYNC_DONE
    skills._BUILTIN_SYNC_DONE = False
    try:
        skills.sync_builtin_skills()
    finally:
        skills._BUILTIN_SYNC_DONE = monkeypatch_done

    assert (user_base / "plugin-creator" / "SKILL.md").read_text(encoding="utf-8") == "用户自定义内容"


def test_sync_does_not_resurrect_deleted_skill(sync_env):
    """用户主动删除已释放的内置技能后，重启（再次同步）不会复活"""
    src, user_base, _ = sync_env
    _make_builtin_skill(src, "plugin-creator")
    skills.sync_builtin_skills()
    # 用户主动删除了技能目录
    import shutil
    shutil.rmtree(user_base / "plugin-creator")
    # 模拟重启：解除一次性守卫，再次同步
    skills._BUILTIN_SYNC_DONE = False
    skills.sync_builtin_skills()
    assert not (user_base / "plugin-creator").exists()


def test_sync_runs_only_once(sync_env):
    src, user_base, _ = sync_env
    _make_builtin_skill(src, "plugin-creator")
    skills.sync_builtin_skills()
    first_ids = skills.get_active_skill_ids()
    # 第二次调用因 _BUILTIN_SYNC_DONE 守卫直接返回，激活列表不变
    skills.sync_builtin_skills()
    assert skills.get_active_skill_ids() == first_ids


def test_sync_ignores_dirs_without_skill_md(sync_env):
    src, user_base, _ = sync_env
    (src / "not-a-skill").mkdir()
    (src / "not-a-skill" / "hello.py").write_text("x = 1\n", encoding="utf-8")

    skills.sync_builtin_skills()

    assert not (user_base / "not-a-skill").exists()
    assert skills.get_active_skill_ids() == []


# ------------------------------------------------------------------
# 已释放内置技能的跟随升级：用户没改过 → 跟随新版；改过 → 绝不覆盖
# ------------------------------------------------------------------

def _rewrite_builtin_skill(src, name, body):
    """模拟新版发布：改写内置源中的技能正文（目录已存在）。"""
    (src / name / "SKILL.md").write_text(
        f"---\nname: {name}\n---\n{body}\n", encoding="utf-8"
    )


def _resync():
    """模拟应用重启：解除一次性守卫后再次同步。"""
    skills._BUILTIN_SYNC_DONE = False
    skills.sync_builtin_skills()


def test_sync_upgrades_unmodified_builtin_skill(sync_env):
    """用户没改动过的内置技能，应跟随新版本自动升级"""
    src, user_base, _ = sync_env
    _make_builtin_skill(src, "plugin-creator", body="# 旧版正文")
    skills.sync_builtin_skills()
    assert "旧版正文" in (user_base / "plugin-creator" / "SKILL.md").read_text(encoding="utf-8")

    _rewrite_builtin_skill(src, "plugin-creator", "# 新版正文")
    _resync()

    assert "新版正文" in (user_base / "plugin-creator" / "SKILL.md").read_text(encoding="utf-8")


def test_sync_does_not_upgrade_user_modified_skill(sync_env):
    """用户改过的内置技能，即使内置源发布了新版本也不能被覆盖"""
    src, user_base, _ = sync_env
    _make_builtin_skill(src, "plugin-creator", body="# 旧版正文")
    skills.sync_builtin_skills()
    (user_base / "plugin-creator" / "SKILL.md").write_text("用户自定义内容", encoding="utf-8")

    _rewrite_builtin_skill(src, "plugin-creator", "# 新版正文")
    _resync()

    assert (user_base / "plugin-creator" / "SKILL.md").read_text(encoding="utf-8") == "用户自定义内容"


def test_sync_upgrade_replaces_whole_directory(sync_env):
    """升级为整目录替换：新版本删掉的文件不应残留在用户目录"""
    src, user_base, _ = sync_env
    builtin_dir = _make_builtin_skill(src, "plugin-creator", body="# 旧版正文")
    (builtin_dir / "legacy.md").write_text("旧版遗留", encoding="utf-8")
    skills.sync_builtin_skills()
    assert (user_base / "plugin-creator" / "legacy.md").is_file()

    (builtin_dir / "legacy.md").unlink()
    _rewrite_builtin_skill(src, "plugin-creator", "# 新版正文")
    _resync()

    assert not (user_base / "plugin-creator" / "legacy.md").exists()
    assert "新版正文" in (user_base / "plugin-creator" / "SKILL.md").read_text(encoding="utf-8")


def test_sync_upgrade_keeps_builtin_source_label(sync_env):
    """升级后应刷新内容索引，该技能来源仍被识别为内置"""
    src, user_base, _ = sync_env
    _make_builtin_skill(src, "plugin-creator", body="# 旧版正文")
    skills.sync_builtin_skills()

    _rewrite_builtin_skill(src, "plugin-creator", "# 新版正文")
    _resync()

    index = skills.load_builtin_skill_index()
    assert skills.classify_skill_source(str(user_base / "plugin-creator"), index) == "builtin"


# ------------------------------------------------------------------
# 恢复内置版本（restore_builtin_skill）：用户显式发起的覆盖，不做"是否改过"判断
# ------------------------------------------------------------------

def test_get_released_builtin_names_without_file(sync_env):
    """没有任何释放记录时返回空集合，且不抛异常"""
    _ = sync_env
    assert skills.get_released_builtin_names() == set()


def test_get_released_builtin_names_after_release(sync_env):
    """释放过技能后能读出对应技能名"""
    src, _, _ = sync_env
    _make_builtin_skill(src, "plugin-creator")
    skills.sync_builtin_skills()

    assert "plugin-creator" in skills.get_released_builtin_names()


def test_restore_builtin_skill_overwrites_user_modification(sync_env):
    """恢复被用户改过的内置技能：内容回到内置源版本"""
    src, user_base, _ = sync_env
    _make_builtin_skill(src, "plugin-creator", body="# 内置正文")
    skills.sync_builtin_skills()
    (user_base / "plugin-creator" / "SKILL.md").write_text("用户自定义内容", encoding="utf-8")

    result = skills.restore_builtin_skill("plugin-creator")

    assert result.get("ok") is True
    assert "内置正文" in (user_base / "plugin-creator" / "SKILL.md").read_text(encoding="utf-8")


def test_restore_builtin_skill_recreates_deleted_skill(sync_env):
    """恢复被用户删除的内置技能：目录重新出现且内容正确"""
    import shutil
    src, user_base, _ = sync_env
    _make_builtin_skill(src, "plugin-creator", body="# 内置正文")
    skills.sync_builtin_skills()
    shutil.rmtree(user_base / "plugin-creator")
    assert not (user_base / "plugin-creator").exists()

    result = skills.restore_builtin_skill("plugin-creator")

    assert result.get("ok") is True
    assert (user_base / "plugin-creator" / "SKILL.md").is_file()
    assert "内置正文" in (user_base / "plugin-creator" / "SKILL.md").read_text(encoding="utf-8")


def test_restore_builtin_skill_missing_in_source(sync_env):
    """内置源中不存在该技能：返回 ok=False 且带提示"""
    src, _, _ = sync_env
    _make_builtin_skill(src, "plugin-creator")
    skills.sync_builtin_skills()

    result = skills.restore_builtin_skill("no-such-skill")

    assert result.get("ok") is False
    assert result.get("msg") == "内置源中不存在该技能"


def test_restore_builtin_skill_refreshes_index(sync_env):
    """恢复后应刷新内容索引，该技能重新被识别为内置"""
    src, user_base, _ = sync_env
    _make_builtin_skill(src, "plugin-creator", body="# 内置正文")
    skills.sync_builtin_skills()
    (user_base / "plugin-creator" / "SKILL.md").write_text("用户自定义内容", encoding="utf-8")
    assert skills.classify_skill_source(str(user_base / "plugin-creator")) == "user"

    skills.restore_builtin_skill("plugin-creator")

    index = skills.load_builtin_skill_index()
    assert skills.classify_skill_source(str(user_base / "plugin-creator"), index) == "builtin"
