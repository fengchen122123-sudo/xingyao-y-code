"""core/skills.py 内置技能释放（sync_builtin_skills）的单元测试。"""

import os

import pytest

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
    monkeypatch.setattr(
        skills, "_builtin_released_file",
        lambda: str(tmp_path / "user_home" / ".ycode" / "builtin_skills_released.json"),
    )
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "user_home") if p == "~" else p)
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
