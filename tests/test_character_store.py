"""角色库（core/character_store.py）的数据层测试。

覆盖：
1) 建表幂等（重复 init 不炸）与进程级句柄注入；
2) 角色建档/查改删/项目隔离（workdir 过滤 + 同项目重名拒绝）；
3) 参考图：类型白名单、主参考唯一（DB 部分唯一索引兜底）、替换提示、
   digest 快照；
4) 生成台账：成功/失败都落库、JSON 字段往返、费用统计可查；
5) prompt 拼装纯函数：镜头描述置顶、外貌句/画风块缺失降级、seed 注入；
6) OSS 直传时效判定：https 前置、缺时间戳按过期、时钟回拨容忍。
"""

import time

import pytest

from core.character_store import (
    CHARACTER_REF_TYPES,
    CharacterStorage,
    compose_character_prompt,
    get_character_storage,
    init_character_storage,
    oss_url_still_fresh,
)
from core.memory import MemoryManager


@pytest.fixture
def manager(tmp_path):
    return MemoryManager(str(tmp_path / "ycode.db"))


@pytest.fixture
def storage(manager):
    return CharacterStorage(manager)


WORKDIR = "D:/proj/demo"


class TestTablesAndHandle:
    def test_create_tables_idempotent(self, manager):
        CharacterStorage(manager)
        CharacterStorage(manager)  # 重复建表不炸（幂等 DDL）

    def test_process_handle_roundtrip(self, manager):
        assert get_character_storage() is None or True  # 未注入时不抛
        store = init_character_storage(manager)
        assert get_character_storage() is store


class TestCharacterCrud:
    def test_create_and_get(self, storage):
        c = storage.create_character(
            "小满",
            description="短发少女",
            prompt_fragment="黑色齐肩短发，琥珀色眼睛，左脸颊有小痣",
            style_tags="写实摄影, 柔光",
            seed=42,
            workdir=WORKDIR,
        )
        assert c["id"] > 0
        assert c["name"] == "小满"
        assert c["seed"] == 42
        assert c["refs"] == []

    def test_duplicate_name_rejected(self, storage):
        storage.create_character("小满", workdir=WORKDIR)
        with pytest.raises(ValueError, match="已存在"):
            storage.create_character("小满", workdir=WORKDIR)

    def test_list_filters_by_workdir(self, storage):
        storage.create_character("A", workdir=WORKDIR)
        storage.create_character("B", workdir="D:/other")
        names = [c["name"] for c in storage.list_characters(WORKDIR)]
        assert names == ["A"]
        # workdir 归一：正反斜杠同一路径
        storage.create_character("C", workdir=WORKDIR.replace("/", "\\"))
        assert [c["name"] for c in storage.list_characters(WORKDIR)] == ["A", "C"]

    def test_update_and_delete(self, storage):
        c = storage.create_character("小满", workdir=WORKDIR)
        storage.update_character(c["id"], prompt_fragment="新外貌句")
        assert storage.get_character(c["id"])["prompt_fragment"] == "新外貌句"
        # seed 哨兵：None 是清除而不是不改
        storage.update_character(c["id"], seed=None)
        assert storage.get_character(c["id"])["seed"] is None
        assert storage.delete_character(c["id"]) == 1
        assert storage.get_character(c["id"]) is None

    def test_create_requires_name(self, storage):
        with pytest.raises(ValueError, match="角色名"):
            storage.create_character("   ", workdir=WORKDIR)


class TestRefs:
    def _character(self, storage):
        return storage.create_character("小满", workdir=WORKDIR)

    def test_add_ref_and_types(self, storage):
        c = self._character(storage)
        out = storage.add_ref(
            c["id"], ref_type="front", media_digest="a" * 64,
            rel_path=".ycode/canvas/media/refs/a.png", is_primary=True,
        )
        assert out["ref"]["is_primary"] is True
        assert out["replaced_primary"] is False
        for bad in ("back", "", None):
            with pytest.raises(ValueError, match="非法参考类型"):
                storage.add_ref(c["id"], ref_type=bad, media_digest="b" * 64, rel_path="x.png")

    def test_primary_unique_and_replace(self, storage):
        c = self._character(storage)
        storage.add_ref(c["id"], ref_type="front", media_digest="a" * 64, rel_path="a.png", is_primary=True)
        out = storage.add_ref(c["id"], ref_type="front", media_digest="b" * 64, rel_path="b.png", is_primary=True)
        # 替换提示 + 唯一性：只有一张主参考
        assert out["replaced_primary"] is True
        primaries = [r for r in storage.list_refs(c["id"]) if r["is_primary"]]
        assert len(primaries) == 1
        assert primaries[0]["media_digest"] == "b" * 64
        # 非主参考不挤占主参考位
        storage.add_ref(c["id"], ref_type="side", media_digest="c" * 64, rel_path="c.png", is_primary=False)
        primaries = [r for r in storage.list_refs(c["id"]) if r["is_primary"]]
        assert len(primaries) == 1

    def test_db_level_primary_unique_index(self, storage, manager):
        """绕过 add_ref 直接插两行主参考：部分唯一索引必须在 DB 层拦住。"""
        c = self._character(storage)
        sql = (
            "INSERT INTO character_refs (character_id, ref_type, media_digest, rel_path,"
            " is_primary, created_at) VALUES (?, 'front', ?, ?, 1, '2026-01-01')"
        )
        with manager._lock:
            storage.conn.execute(sql, (c["id"], "a" * 64, "a.png"))
            with pytest.raises(Exception, match="UNIQUE|idx_character_refs_primary"):
                storage.conn.execute(sql, (c["id"], "b" * 64, "b.png"))
            manager.conn.rollback()

    def test_ref_snapshot(self, storage):
        c = self._character(storage)
        storage.add_ref(c["id"], ref_type="front", media_digest="a" * 64, rel_path="a.png", is_primary=True)
        storage.add_ref(c["id"], ref_type="style", media_digest="b" * 64, rel_path="b.png")
        assert storage.ref_snapshot(c["id"]) == ["a" * 64, "b" * 64]
        # 档案后续更新不影响快照口径（只是当时 digest 的列表）
        storage.add_ref(c["id"], ref_type="side", media_digest="c" * 64, rel_path="c.png")
        assert len(storage.ref_snapshot(c["id"])) == 3

    def test_add_ref_to_missing_character(self, storage):
        with pytest.raises(ValueError, match="角色不存在"):
            storage.add_ref(999, ref_type="front", media_digest="a" * 64, rel_path="a.png")

    def test_delete_character_cascades_refs(self, storage):
        c = self._character(storage)
        storage.add_ref(c["id"], ref_type="front", media_digest="a" * 64, rel_path="a.png")
        storage.delete_character(c["id"])
        assert storage.list_refs(c["id"]) == []


class TestGenerationRecords:
    def test_record_success_and_failure(self, storage):
        rid1 = storage.record_generation({
            "kind": "image", "status": "success",
            "character_ids": [7], "prompt": "全身定妆照",
            "final_prompt": "全身定妆照\n【角色】小满：…", "params": {"model": "qwen-image-2.0"},
            "ref_snapshot": ["a" * 64], "cost_cny": 0.2,
            "result_path": ".ycode/canvas/media/x.png",
            "canvas_id": "c1", "source_node_id": "n1", "workdir": WORKDIR,
        })
        rid2 = storage.record_generation({
            "kind": "video", "status": "failed", "error": "余额不足",
            "character_ids": [7], "prompt": "推近", "cost_cny": None,
            "workdir": WORKDIR,
        })
        records = storage.list_generation_records(WORKDIR)
        assert [r["id"] for r in records] == [rid2, rid1]  # 倒序
        ok = records[1]
        assert ok["params"] == {"model": "qwen-image-2.0"}
        assert ok["ref_snapshot"] == ["a" * 64]
        assert ok["cost_cny"] == 0.2
        failed = records[0]
        assert failed["status"] == "failed" and failed["cost_cny"] is None

    def test_rejects_bad_kind(self, storage):
        with pytest.raises(ValueError, match="非法生成类型"):
            storage.record_generation({"kind": "audio"})

    def test_cost_stats(self, storage):
        for cost in (0.2, 0.6):
            storage.record_generation({"kind": "video", "cost_cny": cost, "workdir": WORKDIR})
        records = storage.list_generation_records(WORKDIR)
        total = sum(r["cost_cny"] or 0 for r in records if r["status"] == "success")
        assert total == pytest.approx(0.8)


class TestComposePrompt:
    def test_full_compose(self):
        final = compose_character_prompt("她站在天台上看落日", {
            "name": "小满", "prompt_fragment": "黑色齐肩短发，琥珀色眼睛",
            "style_tags": "写实摄影, 柔光", "seed": 42,
        })
        lines = final.split("\n")
        assert lines[0] == "她站在天台上看落日"  # 镜头描述置顶
        assert "【角色】小满：黑色齐肩短发，琥珀色眼睛" in lines
        assert "【随机种子】42" in lines
        assert "【画风】写实摄影, 柔光" in lines

    def test_fallback_and_omission(self):
        # prompt_fragment 缺失回落 description；style/seed 为空不拼对应块
        final = compose_character_prompt("镜头", {"name": "阿七", "description": "高个子少年"})
        assert "【角色】阿七：高个子少年" in final
        assert "【画风】" not in final and "【随机种子】" not in final
        # 完全空档案：只有镜头描述
        assert compose_character_prompt("镜头", {}) == "镜头"

    def test_shot_missing_keeps_blocks(self):
        final = compose_character_prompt("", {"name": "小满", "prompt_fragment": "黑发"})
        assert final == "【角色】小满：黑发"


class TestOssFreshness:
    def test_https_and_timestamp_required(self):
        now_ms = time.time() * 1000
        assert oss_url_still_fresh("https://oss/x.png", now_ms) is True
        assert oss_url_still_fresh("http://oss/x.png", now_ms) is False  # 必须 https
        assert oss_url_still_fresh("https://oss/x.png", None) is False  # 缺时间戳按过期
        assert oss_url_still_fresh("", now_ms) is False
        assert oss_url_still_fresh("https://oss/x.png", "abc") is False

    def test_age_boundary(self):
        fresh_ms = (time.time() - 49 * 60) * 1000
        stale_ms = (time.time() - 51 * 60) * 1000
        assert oss_url_still_fresh("https://oss/x.png", fresh_ms) is True
        assert oss_url_still_fresh("https://oss/x.png", stale_ms) is False
        # 时钟回拨（saved_at 比现在还新）容忍为可用
        assert oss_url_still_fresh("https://oss/x.png", (time.time() + 60) * 1000) is True


def test_ref_types_contract():
    assert CHARACTER_REF_TYPES == ("front", "side", "outfit", "style")
