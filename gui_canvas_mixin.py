"""GUI 画布桥接混入（无限画布 v2 的项目持久化段）。

ApiBridge（前端与测试里也叫 GuiBridge）继承本混入；pywebview 反射暴露
public snake_case 方法，本任务（M2-T02）只提供持久化四方法：

- ``canvas_list_projects``   列出工作目录下的画布项目元信息
- ``canvas_load_project``    读单个画布项目
- ``canvas_save_project``    原子写单个画布项目
- ``canvas_delete_project``  删除单个画布项目（幂等）

媒体落盘（M2-T03）与生成类入口在后续任务里追加到本文件，本任务不实现。

铁律（与 gui_git_mixin / gui_snip_mixin 同一套）：
- 所有方法返回可 JSON 序列化的 dict，错误为 {"ok": False, "msg": ...}；
  异常只记日志（logger.exception）并吞掉，绝不穿过 js_api（否则前端
  Promise 直接 reject，画布界面会卡在 loading）。
- 画布文件落盘位置固定为 <会话工作目录>/.ycode/canvas/<project_id>.json：
  跟着项目走，用户把项目目录拷走时画布一起带走；不塞进全局数据目录。

四个容易踩坑的点，实现里都单独处理了（骨架照抄旧 canvas_save/canvas_list 口径）：
1) project_id 直接参与拼文件名，若不做白名单校验，前端传入 "../../x"
   就能把文件写到工作目录之外（路径穿越）。这里只放行
   [A-Za-z0-9_-]{1,64}，并在拼出路径后再做一次 resolve 落域检查
   （双保险：白名单被放宽时仍挡得住越界写）。
2) 保存用"同目录临时文件 + fsync + os.replace"原子替换。画布是频繁自动
   保存的，直接 open(path, "w") 在写完前被切页 / 进程被杀，就会留下半截
   JSON，下次打开整张画布都读不回来；os.replace 在同一分区上是原子的，
   保证读到的永远是完整的旧版本或完整的新版本。失败路径一定清掉 tmp。
3) schema_version 必须是 2：保存入口拒绝未知版本（宁可拒存也不要把读不
   回来的数据写坏用户已有画布）；读取/列表对 v1 旧数据只做"标不支持"，
   不报错——用户还能在侧栏看到它并自行迁移或删除。
4) 列表里单个文件坏掉不该让整个列表失败：退化成"标题=文件名、
   时间=文件 mtime、supported=False"，用户可以照样看到并手动删掉它。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 画布目录（相对会话工作目录）。前端 store 与导出文件名口径同此
CANVAS_SUBDIR: tuple = (".ycode", "canvas")
# 前端/后端共同支持的画布文档版本（无限画布 v2；v1 = 已删除的 JSON Canvas 旧版）
CANVAS_SCHEMA_VERSION = 2
# project_id 白名单：只允许字母数字下划线短横线，1~64 位。
# 既挡住路径分隔符与 ".."（路径穿越），也挡住 Windows 特殊字符与保留名。
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# 旧版数据的统一话术（前端按这串文本提示"该画布是旧版，无法打开"）
_LEGACY_SCHEMA_MSG = "旧版数据，不支持"
# 非法 ID 的统一话术
_INVALID_ID_MSG = "非法画布 ID"


class CanvasBridgeMixin:
    """画布桥接方法集（ApiBridge 的继承混入）。"""

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _canvas_workdir(self) -> str:
        """当前会话的工作目录；宿主没提供解析器时回落进程 cwd。

        不自己发明全局变量：直接复用宿主已有的口径（_hook_workspace_root /
        _session_workdir 内部都已含"查不到就回落 cwd"的兜底），保证画布文件
        与回合运行时的 workspace_identity 指向同一个目录。
        """
        for name in ("_hook_workspace_root", "_session_workdir"):
            getter = getattr(self, name, None)
            if not callable(getter):
                continue
            try:
                # _session_workdir 需要会话名，_hook_workspace_root 不需要
                if name == "_session_workdir":
                    root = getter(getattr(self, "_current_session", "") or "")
                else:
                    root = getter()
            except Exception:
                logger.debug("画布：宿主工作目录解析失败，尝试下一个入口", exc_info=True)
                continue
            if root and str(root) != "GLOBAL":
                return str(root)
        return os.getcwd()

    def _canvas_dir(self) -> Path:
        """<workdir>/.ycode/canvas；一切画布路径 resolve 后必须落在其内。"""
        return Path(self._canvas_workdir()).joinpath(*CANVAS_SUBDIR)

    def _canvas_inside(self, path: Path) -> bool:
        """路径 resolve 后是否仍落在 .ycode/canvas 内（越界 = 拒绝）。"""
        try:
            root = self._canvas_dir().resolve()
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            return False
        return True

    def _canvas_path(self, project_id: Any) -> Optional[Path]:
        """project_id → 画布文件路径；非法 ID 返回 None（调用方直接拒绝）。"""
        pid = str(project_id or "").strip()
        if not _PROJECT_ID_RE.match(pid):
            logger.warning("画布：拒绝非法 project_id %r", project_id)
            return None
        path = self._canvas_dir() / f"{pid}.json"
        # 白名单已经挡掉分隔符与 ".."；这里再按 resolve 结果复核一次落域，
        # 万一将来白名单放宽（或平台上有奇怪的路径折叠）也不会写到目录外
        if not self._canvas_inside(path):
            logger.warning("画布：project_id %r 解析后落在画布目录之外，拒绝", project_id)
            return None
        return path

    # ------------------------------------------------------------------
    # 对外桥方法：持久化四方法
    # ------------------------------------------------------------------
    def canvas_list_projects(self) -> Dict[str, Any]:
        """列出工作目录下的全部画布项目（最近更新的排前面）。

        成功: {"ok": True, "projects": [{id, title, updatedAt, schema_version, supported}]}
        异常: {"ok": False, "msg": ...}

        ``supported = (schema_version == CANVAS_SCHEMA_VERSION)``：旧版（v1）
        文件照样列出来但标 supported=False，不在这里报错——前端据此提示
        "旧版数据，不支持"，用户还能手动清理。
        """
        try:
            root = self._canvas_dir()
            projects: List[Dict[str, Any]] = []
            if root.is_dir():
                for path in root.glob("*.json"):
                    # 目录里可能有前端临时文件 / 备份；只认合法的 project_id
                    if not _PROJECT_ID_RE.match(path.stem):
                        continue
                    title = path.stem
                    updated_at: Optional[int] = None
                    schema_version = 0
                    try:
                        doc = json.loads(path.read_text(encoding="utf-8"))
                        if isinstance(doc, dict):
                            raw_title = doc.get("title")
                            if isinstance(raw_title, str) and raw_title.strip():
                                title = raw_title.strip()
                            raw_updated = doc.get("updatedAt")
                            if isinstance(raw_updated, (int, float)) and not isinstance(
                                raw_updated, bool
                            ):
                                updated_at = int(raw_updated)
                            try:
                                schema_version = int(doc.get("schema_version"))
                            except (TypeError, ValueError):
                                schema_version = 0
                    except Exception:
                        # 单个画布坏掉不该让整个列表失败：退化成"标题=文件名、
                        # 时间=文件 mtime、不支持"，用户还能看到并手动删掉它
                        logger.warning("画布文件解析失败，按文件名兜底：%s", path, exc_info=True)
                        schema_version = 0
                    if updated_at is None:
                        try:
                            updated_at = int(path.stat().st_mtime * 1000)
                        except OSError:
                            updated_at = 0
                    projects.append(
                        {
                            "id": path.stem,
                            "title": title,
                            "updatedAt": int(updated_at),
                            "schema_version": schema_version,
                            "supported": schema_version == CANVAS_SCHEMA_VERSION,
                        }
                    )
            projects.sort(key=lambda item: item.get("updatedAt") or 0, reverse=True)
            return {"ok": True, "projects": projects}
        except Exception as e:
            logger.exception("canvas_list_projects 失败")
            return {"ok": False, "msg": f"读取画布列表失败：{e}"}

    def canvas_load_project(self, project_id: Any) -> Dict[str, Any]:
        """读取单个画布项目。

        成功: {"ok": True, "project": {...完整 CanvasProject dict}}
        失败: {"ok": False, "msg": ...}；schema_version != 2 → msg="旧版数据，不支持"

        只做结构兜底（nodes/connections 非 list 置空数组），不做前端 sanitize：
        脏数据清理由前端加载后的 ``sanitizeCanvasProject`` 负责，桥侧改一遍等于
        两处口径分叉。
        """
        try:
            path = self._canvas_path(project_id)
            if path is None:
                return {"ok": False, "msg": _INVALID_ID_MSG}
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                # 也覆盖"检查后被删"的竞态，统一话术
                return {"ok": False, "msg": "画布不存在"}
            try:
                doc = json.loads(text)
            except Exception:
                logger.warning("画布文件 JSON 解析失败：%s", path, exc_info=True)
                return {"ok": False, "msg": "画布文件格式非法"}
            if not isinstance(doc, dict):
                return {"ok": False, "msg": "画布文件格式非法"}
            try:
                version = int(doc.get("schema_version"))
            except (TypeError, ValueError):
                version = -1
            if version != CANVAS_SCHEMA_VERSION:
                return {"ok": False, "msg": _LEGACY_SCHEMA_MSG}
            project = dict(doc)
            project.setdefault("id", path.stem)
            if not isinstance(project.get("nodes"), list):
                project["nodes"] = []
            if not isinstance(project.get("connections"), list):
                project["connections"] = []
            return {"ok": True, "project": project}
        except Exception as e:
            logger.exception("canvas_load_project 失败 project_id=%s", project_id)
            return {"ok": False, "msg": f"读取画布失败：{e}"}

    def canvas_save_project(self, project: Any) -> Dict[str, Any]:
        """保存画布项目（原子写）。

        成功: {"ok": True, "updatedAt": int}；失败: {"ok": False, "msg": ...}

        入参是完整 CanvasProject dict（含 id / schema_version），updatedAt 由
        桥侧统一写回 epoch ms——前端时钟不可信，落盘时间以桥为准。
        """
        tmp_name: Optional[str] = None
        try:
            if not isinstance(project, dict):
                return {"ok": False, "msg": "画布数据必须是对象"}
            path = self._canvas_path(project.get("id"))
            if path is None:
                # 路径穿越防线：非法 ID 绝不落盘
                return {"ok": False, "msg": _INVALID_ID_MSG}
            try:
                version = int(project.get("schema_version"))
            except (TypeError, ValueError):
                version = -1
            if version != CANVAS_SCHEMA_VERSION:
                # 前端送来未知版本：宁可在入口拒绝，也不要把读不回来的数据写坏已有画布
                return {"ok": False, "msg": _LEGACY_SCHEMA_MSG}
            if not isinstance(project.get("nodes"), list):
                return {"ok": False, "msg": "画布数据格式非法：nodes 必须是数组"}
            if not isinstance(project.get("connections"), list):
                return {"ok": False, "msg": "画布数据格式非法：connections 必须是数组"}
            updated_at = int(time.time() * 1000)
            # 不改调用方传入的对象：复制一份再补字段
            payload = dict(project)
            payload["id"] = path.stem
            payload["updatedAt"] = updated_at
            payload["schema_version"] = CANVAS_SCHEMA_VERSION
            # 先序列化再动磁盘：对象里有不可 JSON 化的值时，原文件保持原样
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent)
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                # fsync：断电场景下 os.replace 的元数据可能先落盘而数据没落盘，
                # 会留出一个 0 字节画布
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            tmp_name = None
            return {"ok": True, "updatedAt": updated_at}
        except Exception as e:
            logger.exception("canvas_save_project 失败 project_id=%s", (project or {}).get("id") if isinstance(project, dict) else project)
            return {"ok": False, "msg": f"保存画布失败：{e}"}
        finally:
            # 只有中途失败才会留下临时文件；成功路径上 tmp_name 已被 replace 掉
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def canvas_delete_project(self, project_id: Any) -> Dict[str, Any]:
        """删除单个画布项目（幂等：文件不存在也返回 ok）。

        成功: {"ok": True}；失败: {"ok": False, "msg": ...}

        媒体文件不级联删除：画布里的图片 / 视频可能还在内容寻址仓库或其他画布
        里被引用，按"删项目不动媒体"的口径留待后续清理任务统一回收。
        """
        try:
            path = self._canvas_path(project_id)
            if path is None:
                return {"ok": False, "msg": _INVALID_ID_MSG}
            try:
                path.unlink()
            except FileNotFoundError:
                # 幂等：前端重试 / 双端删同一张画布都不该报错
                return {"ok": True}
            return {"ok": True}
        except Exception as e:
            logger.exception("canvas_delete_project 失败 project_id=%s", project_id)
            return {"ok": False, "msg": f"删除画布失败：{e}"}
