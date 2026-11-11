"""角色库（ycode.db ``characters`` / ``character_refs`` / ``character_generation_records``）。

P2「角色库与角色带入」的数据层：角色档案（外貌锚点句 + 画风约束 + 可选 seed）、
参考图（front/side/outfit/style 四类，内容寻址 digest + 项目相对路径，主参考
每角色仅一张）、生成台账（每次带角色生成的 prompt/参数/参考图快照/费用/产物
路径，保证旧镜头可复现统计）。

设计要点：
- 沿用 core/workflows/runs.py 的 DAO 模式：持有 MemoryManager，复用其连接与
  可重入锁；进程级句柄经 ``init_character_storage(manager)`` 注入
  （gui_bridge 启动时），未注入（CLI/headless/测试）时 ``get_character_storage()``
  返回 None，桥层 best-effort 降级为「角色库不可用」提示，绝不起死库。
- 新增表不算 schema_version 变更：CREATE TABLE IF NOT EXISTS 幂等建表，与
  harness/工作流等既有新增表同一口径；不触碰既有表的 schema_version。
- 主参考唯一性在 DB 层兜底：SQLite 部分唯一索引（WHERE is_primary = 1）保证
  每角色最多一行主参考；替换主参考由 add_ref 在同一事务里先清旧行再插新行。
- character_ref_snapshot 只存 digest 列表：角色档案后续增删参考图不影响旧
  镜头的复现口径（内容寻址仓库按 digest 永远能取回当时的字节）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("ycode.character")

#: 参考类型白名单：正面 / 侧面 / 服装 / 风格。桥层入口与 UI 下拉共用这一份。
CHARACTER_REF_TYPES = ("front", "side", "outfit", "style")

#: 参考类型的中文文案（UI 提示与桥层错误信息用；顺序与 CHARACTER_REF_TYPES 一致）
REF_TYPE_LABELS = {
    "front": "正面",
    "side": "侧面",
    "outfit": "服装",
    "style": "风格",
}

#: 生成类型白名单：定妆图（image）/ 镜头视频（video）
GENERATION_KINDS = ("image", "video")

#: OSS 直传 URL 的保守时效：平台签名约 60 分钟，留 10 分钟余量
OSS_URL_MAX_AGE_SECONDS = 50 * 60

_TABLE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS characters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        prompt_fragment TEXT NOT NULL DEFAULT '',
        style_tags TEXT NOT NULL DEFAULT '',
        seed INTEGER,
        workdir TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_characters_workdir_name"
    " ON characters(workdir, name)",
    """
    CREATE TABLE IF NOT EXISTS character_refs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        character_id INTEGER NOT NULL,
        ref_type TEXT NOT NULL,
        media_digest TEXT NOT NULL,
        rel_path TEXT NOT NULL,
        mime TEXT NOT NULL DEFAULT '',
        width INTEGER,
        height INTEGER,
        source_node_id TEXT NOT NULL DEFAULT '',
        is_primary INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY(character_id) REFERENCES characters(id) ON DELETE CASCADE
    )
    """,
    # 部分唯一索引：每角色最多一张主参考（替换必须走 add_ref 的事务口径）
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_character_refs_primary"
    " ON character_refs(character_id) WHERE is_primary = 1",
    "CREATE INDEX IF NOT EXISTS idx_character_refs_character"
    " ON character_refs(character_id)",
    """
    CREATE TABLE IF NOT EXISTS character_generation_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'success',
        error TEXT NOT NULL DEFAULT '',
        character_ids_json TEXT NOT NULL DEFAULT '[]',
        prompt TEXT NOT NULL DEFAULT '',
        final_prompt TEXT NOT NULL DEFAULT '',
        params_json TEXT NOT NULL DEFAULT '{}',
        ref_snapshot_json TEXT NOT NULL DEFAULT '[]',
        cost_cny REAL,
        result_path TEXT NOT NULL DEFAULT '',
        canvas_id TEXT NOT NULL DEFAULT '',
        source_node_id TEXT NOT NULL DEFAULT '',
        workdir TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_generation_records_workdir"
    " ON character_generation_records(workdir, created_at)",
)

_CHARACTER_COLUMNS = (
    "id", "name", "description", "prompt_fragment", "style_tags", "seed",
    "workdir", "created_at", "updated_at",
)

_REF_COLUMNS = (
    "id", "character_id", "ref_type", "media_digest", "rel_path", "mime",
    "width", "height", "source_node_id", "is_primary", "created_at",
)

_RECORD_COLUMNS = (
    "id", "kind", "status", "error", "character_ids_json", "prompt",
    "final_prompt", "params_json", "ref_snapshot_json", "cost_cny",
    "result_path", "canvas_id", "source_node_id", "workdir", "created_at",
)


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_workdir(path: str | None) -> str:
    """工作目录归一成 realpath 文本；空值归空串（全项目可见的兜底口径）。"""
    if not path:
        return ""
    try:
        return os.path.realpath(str(path))
    except Exception:
        return str(path)


def ref_type_label(ref_type: str) -> str:
    return REF_TYPE_LABELS.get(str(ref_type or ""), str(ref_type or "?"))


def oss_url_still_fresh(url: str | None, saved_at_ms: Any = None) -> bool:
    """OSS 直传 URL 是否仍在保守时效内（约 60 分钟签名，留 10 分钟余量）。

    saved_at_ms 缺失/非法一律按过期处理：宁可回落 ¥0.2 重绘，也不把一张
    过期签名图喂给视频接口（400 白跑一趟还不退钱的话就是纯损失）。
    """
    url = str(url or "").strip()
    if not url.lower().startswith("https://"):
        return False
    try:
        age = (time.time() * 1000 - float(saved_at_ms)) / 1000.0
    except (TypeError, ValueError):
        return False
    if age < 0:
        # 时钟回拨容忍：saved_at 比当前时间还新就当刚保存
        return True
    return age <= OSS_URL_MAX_AGE_SECONDS


def compose_character_prompt(shot: str, character: dict) -> str:
    """镜头描述 + 角色固定外貌句 + 画风约束的拼装（纯函数）。

    拼装口径（图像/视频两阶段共用；真跑脚本与桥层必须用同一份，避免两处口径漂移）：
    1. 用户写的镜头描述原样置顶——模型对开头句的服从度最高；
    2. 【角色】块放 name + prompt_fragment（外貌锚点句），缺失时回落 description；
    3. 【画风】块放 style_tags（逗号分隔即可读），为空则整个块不拼。

    注意 prompt_fragment 里用户自己写的是「同一角色必须保持的固定特征」，不是
    逐镜头描述；生成记录里 prompt 与 final_prompt 都落库，两者差值即注入量。
    """
    shot = str(shot or "").strip()
    name = str((character or {}).get("name") or "").strip()
    fragment = str((character or {}).get("prompt_fragment") or "").strip()
    description = str((character or {}).get("description") or "").strip()
    style_tags = str((character or {}).get("style_tags") or "").strip()
    seed = (character or {}).get("seed")

    parts: list[str] = [shot] if shot else []
    if name or fragment or description:
        anchor = fragment or description
        line = "【角色】"
        if name:
            line += name
        if anchor:
            line += ("：" if name else "") + anchor
        parts.append(line)
        if seed is not None:
            try:
                parts.append("【随机种子】%d" % int(seed))
            except (TypeError, ValueError):
                pass
    if style_tags:
        parts.append("【画风】" + style_tags)
    return "\n".join(parts)


class CharacterStorage:
    """角色库 DAO。持有 MemoryManager，复用其连接与可重入锁。"""

    def __init__(self, manager):
        self._manager = manager
        self._create_tables()

    @property
    def conn(self):
        return self._manager.conn

    def _create_tables(self) -> None:
        with self._manager._lock:
            cursor = self.conn.cursor()
            for ddl in _TABLE_DDL:
                cursor.execute(ddl)
            self.conn.commit()

    # -- 角色 --

    def create_character(
        self,
        name: str,
        *,
        description: str = "",
        prompt_fragment: str = "",
        style_tags: str = "",
        seed: Optional[int] = None,
        workdir: str = "",
    ) -> dict:
        """建档；同名同项目会撞唯一索引，转成可读错误向上抛 ValueError。"""
        name = str(name or "").strip()
        if not name:
            raise ValueError("角色名不能为空")
        now = iso_now()
        workdir = normalize_workdir(workdir)
        with self._manager._lock:
            try:
                cursor = self.conn.execute(
                    "INSERT INTO characters (name, description, prompt_fragment,"
                    " style_tags, seed, workdir, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        name, str(description or ""), str(prompt_fragment or ""),
                        str(style_tags or ""), seed, workdir, now, now,
                    ),
                )
                self.conn.commit()
            except Exception as exc:
                self.conn.rollback()
                if "idx_characters_workdir_name" in str(exc) or "UNIQUE" in str(exc).upper():
                    raise ValueError("角色「%s」已存在，请换个名字或直接选用它" % name) from exc
                raise
            return self.get_character(int(cursor.lastrowid or 0), workdir=workdir) or {}

    def get_character(self, character_id: int, workdir: str = "") -> Optional[dict]:
        cid = int(character_id or 0)
        if cid <= 0:
            return None
        with self._manager._lock:
            row = self.conn.execute(
                "SELECT %s FROM characters WHERE id = ?" % ",".join(_CHARACTER_COLUMNS),
                (cid,),
            ).fetchone()
        return self._row_to_character(row)

    def update_character(
        self,
        character_id: int,
        *,
        description: Optional[str] = None,
        prompt_fragment: Optional[str] = None,
        style_tags: Optional[str] = None,
        seed=...,  # sentinel：None 是合法值（清除），不传才是不改
    ) -> Optional[dict]:
        """局部更新；None 表示不改该字段（seed 用 ... 哨兵区分「不改」与「清除」）。"""
        cid = int(character_id or 0)
        if cid <= 0:
            return None
        sets, args = ["updated_at = ?"], [iso_now()]
        if description is not None:
            sets.append("description = ?")
            args.append(str(description or ""))
        if prompt_fragment is not None:
            sets.append("prompt_fragment = ?")
            args.append(str(prompt_fragment or ""))
        if style_tags is not None:
            sets.append("style_tags = ?")
            args.append(str(style_tags or ""))
        if seed is not ...:
            sets.append("seed = ?")
            args.append(seed if seed is None else int(seed))
        args.append(cid)
        with self._manager._lock:
            self.conn.execute(
                "UPDATE characters SET %s WHERE id = ?" % ", ".join(sets), args
            )
            self.conn.commit()
        return self.get_character(cid)

    def delete_character(self, character_id: int) -> int:
        """删档连参考一起删（ON DELETE CASCADE）；生成台账刻意保留（历史记录）。"""
        cid = int(character_id or 0)
        if cid <= 0:
            return 0
        with self._manager._lock:
            removed = self.conn.execute(
                "DELETE FROM characters WHERE id = ?", (cid,)
            ).rowcount
            # 旧版 SQLite 默认不开 foreign_keys 时兜底手动清参考
            self.conn.execute("DELETE FROM character_refs WHERE character_id = ?", (cid,))
            self.conn.commit()
        return removed

    def list_characters(self, workdir: str = "") -> list[dict]:
        """当前项目的角色列表（含参考）；workdir 为空返回空列表而不是全部。"""
        workdir = normalize_workdir(workdir)
        if not workdir:
            return []
        with self._manager._lock:
            rows = self.conn.execute(
                "SELECT %s FROM characters WHERE workdir = ? ORDER BY id"
                % ",".join(_CHARACTER_COLUMNS),
                (workdir,),
            ).fetchall()
            ref_rows = self.conn.execute(
                "SELECT %s FROM character_refs ORDER BY id" % ",".join(_REF_COLUMNS)
            ).fetchall()
        characters = [self._row_to_character(r) for r in rows]
        by_id = {c["id"]: c for c in characters}
        for ref_row in ref_rows:
            ref = self._row_to_ref(ref_row)
            owner = by_id.get(ref.get("character_id"))
            if owner is not None:
                owner["refs"].append(ref)
        return characters

    # -- 参考图 --

    def add_ref(
        self,
        character_id: int,
        *,
        ref_type: str,
        media_digest: str,
        rel_path: str,
        mime: str = "",
        width: Optional[int] = None,
        height: Optional[int] = None,
        source_node_id: str = "",
        is_primary: bool = False,
    ) -> dict:
        """落一条参考；is_primary=True 时同一事务里先清掉旧主参考再插（部分唯一索引兜底）。

        返回 {"ref": {...}, "replaced_primary": bool}——replaced_primary 给桥层
        做「主参考图已替换」提示用。
        """
        cid = int(character_id or 0)
        ref_type = str(ref_type or "").strip()
        digest = str(media_digest or "").strip()
        rel_path = str(rel_path or "").strip()
        if cid <= 0:
            raise ValueError("角色不存在")
        if ref_type not in CHARACTER_REF_TYPES:
            raise ValueError(
                "非法参考类型 %r：可选 %s" % (ref_type, " / ".join(CHARACTER_REF_TYPES))
            )
        if not digest or not rel_path:
            raise ValueError("参考图缺少 digest 或路径")
        with self._manager._lock:
            exists = self.conn.execute(
                "SELECT 1 FROM characters WHERE id = ?", (cid,)
            ).fetchone()
            if not exists:
                raise ValueError("角色不存在（可能刚被删除）")
            replaced = False
            if is_primary:
                replaced = bool(
                    self.conn.execute(
                        "SELECT 1 FROM character_refs WHERE character_id = ?"
                        " AND is_primary = 1",
                        (cid,),
                    ).fetchone()
                )
                self.conn.execute(
                    "UPDATE character_refs SET is_primary = 0"
                    " WHERE character_id = ? AND is_primary = 1",
                    (cid,),
                )
            cursor = self.conn.execute(
                "INSERT INTO character_refs (character_id, ref_type, media_digest,"
                " rel_path, mime, width, height, source_node_id, is_primary, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cid, ref_type, digest, rel_path, str(mime or ""),
                    width, height, str(source_node_id or ""),
                    1 if is_primary else 0, iso_now(),
                ),
            )
            self.conn.commit()
            ref_id = int(cursor.lastrowid or 0)
        row = self.conn.execute(
            "SELECT %s FROM character_refs WHERE id = ?" % ",".join(_REF_COLUMNS),
            (ref_id,),
        ).fetchone()
        return {"ref": self._row_to_ref(row), "replaced_primary": replaced}

    def list_refs(self, character_id: int) -> list[dict]:
        cid = int(character_id or 0)
        with self._manager._lock:
            rows = self.conn.execute(
                "SELECT %s FROM character_refs WHERE character_id = ? ORDER BY id"
                % ",".join(_REF_COLUMNS),
                (cid,),
            ).fetchall()
        return [self._row_to_ref(r) for r in rows]

    def primary_ref(self, character_id: int) -> Optional[dict]:
        for ref in self.list_refs(character_id):
            if ref.get("is_primary"):
                return ref
        return None

    def ref_snapshot(self, character_id: int) -> list[str]:
        """生成时的参考图快照：digest 列表（内容寻址，档案后续更新不影响旧镜头）。"""
        return [str(r.get("media_digest") or "") for r in self.list_refs(character_id)]

    def resolve_ref_abs_path(self, ref: dict, workdir: str) -> str:
        """项目相对路径 → 绝对路径（只在读取侧拼，库里的 rel_path 永远随项目走）。"""
        return os.path.join(str(workdir or ""), str((ref or {}).get("rel_path") or ""))

    # -- 生成台账 --

    def record_generation(self, record: dict) -> int:
        """落一条生成记录（成功/失败都落，cost_cny 可为 NULL）。返回记录 id。"""
        kind = str(record.get("kind") or "").strip()
        if kind not in GENERATION_KINDS:
            raise ValueError("非法生成类型 %r：可选 %s" % (kind, " / ".join(GENERATION_KINDS)))
        with self._manager._lock:
            cursor = self.conn.execute(
                "INSERT INTO character_generation_records (kind, status, error,"
                " character_ids_json, prompt, final_prompt, params_json,"
                " ref_snapshot_json, cost_cny, result_path, canvas_id,"
                " source_node_id, workdir, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    str(record.get("status") or "success"),
                    str(record.get("error") or "")[:2000],
                    json.dumps(list(record.get("character_ids") or [])),
                    str(record.get("prompt") or ""),
                    str(record.get("final_prompt") or ""),
                    json.dumps(record.get("params") or {}, ensure_ascii=False),
                    json.dumps(list(record.get("ref_snapshot") or [])),
                    record.get("cost_cny"),
                    str(record.get("result_path") or ""),
                    str(record.get("canvas_id") or ""),
                    str(record.get("source_node_id") or ""),
                    normalize_workdir(record.get("workdir")),
                    iso_now(),
                ),
            )
            self.conn.commit()
            return int(cursor.lastrowid or 0)

    def list_generation_records(self, workdir: str = "", limit: int = 50) -> list[dict]:
        """按时间倒序的可复现台账（费用统计/旧镜头复现都查这里）。"""
        workdir = normalize_workdir(workdir)
        limit = max(1, min(int(limit or 50), 500))
        with self._manager._lock:
            if workdir:
                rows = self.conn.execute(
                    "SELECT %s FROM character_generation_records WHERE workdir = ?"
                    " ORDER BY id DESC LIMIT ?" % ",".join(_RECORD_COLUMNS),
                    (workdir, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT %s FROM character_generation_records"
                    " ORDER BY id DESC LIMIT ?" % ",".join(_RECORD_COLUMNS),
                    (limit,),
                ).fetchall()
        return [self._row_to_record(r) for r in rows]

    # -- 行映射 --

    @staticmethod
    def _row_to_character(row) -> Optional[dict]:
        if not row:
            return None
        values = dict(zip(_CHARACTER_COLUMNS, tuple(row)))
        values["id"] = int(values["id"])
        values["seed"] = None if values.get("seed") is None else int(values["seed"])
        values["refs"] = []
        return values

    @staticmethod
    def _row_to_ref(row) -> dict:
        values = dict(zip(_REF_COLUMNS, tuple(row)))
        values["character_id"] = int(values["character_id"])
        values["id"] = int(values["id"])
        values["is_primary"] = bool(values.get("is_primary"))
        for key in ("width", "height"):
            values[key] = None if values.get(key) is None else int(values[key])
        return values

    @staticmethod
    def _row_to_record(row) -> dict:
        values = dict(zip(_RECORD_COLUMNS, tuple(row)))
        values["id"] = int(values["id"])
        values["cost_cny"] = None if values.get("cost_cny") is None else float(values["cost_cny"])
        for key, target in (
            ("character_ids_json", "character_ids"),
            ("params_json", "params"),
            ("ref_snapshot_json", "ref_snapshot"),
        ):
            try:
                values[target] = json.loads(values.pop(key) or ("{}" if key == "params_json" else "[]"))
            except ValueError:
                values[target] = {} if key == "params_json" else []
        return values


# -- 进程级句柄（gui_bridge 启动时注入；未注入时桥层降级提示）--

_storage: Optional[CharacterStorage] = None


def init_character_storage(manager) -> CharacterStorage:
    global _storage
    _storage = CharacterStorage(manager)
    return _storage


def get_character_storage() -> Optional[CharacterStorage]:
    return _storage
