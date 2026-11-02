"""GUI 画布桥接混入（JSON Canvas v1 的读 / 写 / 上行）。

ApiBridge（前端与测试里也叫 GuiBridge）继承本混入；pywebview 反射暴露
public snake_case 方法，前端只需 js_api.canvas_list / canvas_load /
canvas_save / canvas_send_to_agent / canvas_resolve_media 五个入口即可完成
画布的全部持久化、"选中节点发到对话"与"本地路径换预览 URL"。

铁律（与 gui_git_mixin / gui_snip_mixin 同一套）：
- 所有方法返回可 JSON 序列化的 dict，错误为 {"ok": False, "msg": ...}；
  异常只记日志（logger.exception）并吞掉，绝不穿过 js_api（否则前端
  Promise 直接 reject，画布界面会卡在 loading）。
- 画布文件落盘位置固定为 <会话工作目录>/.ycode/canvas/<canvas_id>.json：
  跟着项目走，用户把项目目录拷走时画布一起带走；不塞进全局数据目录。

三个容易踩坑的点，实现里都单独处理了：
1) canvas_id 直接参与拼文件名，若不做白名单校验，前端传入 "../../x"
   就能把文件写到工作目录之外（路径穿越）。这里只放行
   [A-Za-z0-9_-]{1,64}，既挡住分隔符与 ".."，也顺手挡掉 Windows 保留名
   里那些带点的写法。
2) 保存用"同目录临时文件 + os.replace"原子替换。画布是频繁自动保存的，
   直接 open(path, "w") 在写完前被切页 / 进程被杀，就会留下半截 JSON，
   下次打开整张画布都读不回来；os.replace 在同一分区上是原子的，
   保证读到的永远是完整的旧版本或完整的新版本。
3) doc 必须带 schema_version == 1。前端（或未来的 v2 前端）如果送来未知
   版本，宁可在入口拒绝，也不要把读不回来的数据写坏用户已有画布。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 画布目录（相对会话工作目录），与 mockups/aigc-canvas.html 里的
# ".ycode/canvas/cat-series.json" 口径一致
CANVAS_SUBDIR: tuple = (".ycode", "canvas")
# 前端/后端共同支持的画布文档版本
CANVAS_SCHEMA_VERSION = 1
# canvas_id 白名单：只允许字母数字下划线短横线，1~64 位。
# 既挡住路径分隔符与 ".."（路径穿越），也挡住 Windows 特殊字符。
_CANVAS_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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
        return Path(self._canvas_workdir()).joinpath(*CANVAS_SUBDIR)

    def _canvas_path(self, canvas_id: Any) -> Optional[Path]:
        """canvas_id → 画布文件路径；非法 ID 返回 None（调用方直接拒绝）。"""
        cid = str(canvas_id or "").strip()
        if not _CANVAS_ID_RE.match(cid):
            return None
        return self._canvas_dir() / f"{cid}.json"

    # ------------------------------------------------------------------
    # 对外桥方法
    # ------------------------------------------------------------------
    def canvas_list(self) -> Dict[str, Any]:
        """列出工作目录下的全部画布（最近修改的排前面）。

        成功: {"ok": True, "items": [{id, title, updated_at}]}
        异常: {"ok": False, "msg": ...}
        """
        try:
            root = self._canvas_dir()
            items: List[Dict[str, str]] = []
            if root.is_dir():
                for path in root.glob("*.json"):
                    # 目录里可能有前端临时文件 / 备份；只认合法的 canvas_id
                    if not _CANVAS_ID_RE.match(path.stem):
                        continue
                    title = path.stem
                    updated_at = ""
                    try:
                        doc = json.loads(path.read_text(encoding="utf-8"))
                        if isinstance(doc, dict):
                            raw_title = doc.get("title")
                            if isinstance(raw_title, str) and raw_title.strip():
                                title = raw_title.strip()
                            raw_updated = doc.get("updated_at")
                            if isinstance(raw_updated, str) and raw_updated.strip():
                                updated_at = raw_updated.strip()
                    except Exception:
                        # 单个画布坏掉不该让整个列表 500：退化成"标题=文件名、
                        # 时间=文件 mtime"，用户还能看到并手动删掉它
                        logger.warning("画布文件解析失败，按文件名兜底：%s", path, exc_info=True)
                    if not updated_at:
                        try:
                            updated_at = datetime.fromtimestamp(
                                path.stat().st_mtime
                            ).astimezone().isoformat(timespec="seconds")
                        except OSError:
                            updated_at = ""
                    items.append({"id": path.stem, "title": title, "updated_at": updated_at})
            items.sort(key=lambda it: it.get("updated_at") or "", reverse=True)
            return {"ok": True, "items": items}
        except Exception as e:
            logger.exception("canvas_list 失败")
            return {"ok": False, "msg": f"读取画布列表失败：{e}"}

    def canvas_load(self, canvas_id: Any) -> Dict[str, Any]:
        """读取单个画布文档。

        成功: {"ok": True, "doc": {...}}
        异常: {"ok": False, "msg": ...}
        """
        try:
            path = self._canvas_path(canvas_id)
            if path is None:
                return {"ok": False, "msg": "非法画布 ID"}
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                # 也覆盖"检查后被删"的竞态，统一话术
                return {"ok": False, "msg": "画布不存在"}
            doc = json.loads(text)
            if not isinstance(doc, dict):
                return {"ok": False, "msg": "画布文件格式非法"}
            return {"ok": True, "doc": doc}
        except Exception as e:
            logger.exception("canvas_load 失败 canvas_id=%s", canvas_id)
            return {"ok": False, "msg": f"读取画布失败：{e}"}

    def canvas_save(self, canvas_id: Any, doc: Any) -> Dict[str, Any]:
        """保存画布文档（原子写）。

        成功: {"ok": True}
        异常: {"ok": False, "msg": ...}
        """
        tmp_name: Optional[str] = None
        try:
            path = self._canvas_path(canvas_id)
            if path is None:
                # 路径穿越防线：非法 ID 绝不落盘
                return {"ok": False, "msg": "非法画布 ID"}
            if not isinstance(doc, dict):
                return {"ok": False, "msg": "画布数据必须是对象"}
            try:
                version = int(doc.get("schema_version"))
            except (TypeError, ValueError):
                version = -1
            if version != CANVAS_SCHEMA_VERSION:
                return {"ok": False, "msg": "画布数据版本不受支持"}
            # 不改调用方传入的对象：复制一份再补字段
            payload = dict(doc)
            payload.setdefault("id", path.stem)
            payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
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
                # 留出一个 0 字节画布
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            tmp_name = None
            return {"ok": True}
        except Exception as e:
            logger.exception("canvas_save 失败 canvas_id=%s", canvas_id)
            return {"ok": False, "msg": f"保存画布失败：{e}"}
        finally:
            # 只有中途失败才会留下临时文件；成功路径上 tmp_name 已被 replace 掉
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def canvas_resolve_media(self, paths: Any = None) -> Dict[str, Any]:
        """把画布节点引用的本地图片/视频换成 WebView 能直接加载的媒体 URL。

        为什么必须走后端：画布节点只存磁盘路径（相对项目根，画布跟着项目走），
        而 WebView 里的 <img src> 读不到本地文件（前端没有 file:// 访问权）。
        这里借既有的「内容寻址仓库 + 本机媒体服务」把文件换成一个带令牌的
        http 地址，前端直接塞给节点就出图，不需要再发一次图片。

        失败语义（与 get_attachment_thumbnails 同一套）：单条失败不整体失败。
        - 成功项：{"path", "url", "digest", "width", "height"}
        - 单条失败：{"path", "error": "中文原因"}，其余项照常返回
        - 媒体服务没起来（public_url 返回空串）：跳过该项，url 缺省即表示
          「暂不可预览」，前端继续画占位图，不算错误
        只有媒体模块整体导入失败这类基础设施问题才 ok=False。
        """
        try:
            from core.media_server import media_server
            from core.media_store import archive_media_file
        except Exception as e:
            logger.exception("canvas_resolve_media：媒体模块导入失败")
            return {"ok": False, "msg": f"媒体服务不可用：{e}"}

        items: List[Dict[str, Any]] = []
        try:
            # 容忍单个字符串：前端偶尔只解析出一个路径，没必要为此报参数错
            raw_list = paths if isinstance(paths, (list, tuple)) else [paths]
            workdir = self._canvas_workdir()
            for raw in raw_list:
                candidate = str(raw or "").strip()
                if not candidate:
                    continue
                try:
                    # 节点存的是项目内相对路径（画布跟着项目走），按画布工作目录
                    # 解析；用户手动填的绝对路径原样使用
                    resolved = (
                        candidate
                        if os.path.isabs(candidate)
                        else os.path.join(workdir, candidate)
                    )
                    item = archive_media_file(resolved)
                    # 服务未启动时 public_url 返回空串 -> 跳过该项
                    url = media_server.public_url(item.get("digest") or "", item.get("ext") or "")
                    if not url:
                        logger.debug("canvas_resolve_media：媒体服务未启动，跳过 %s", candidate)
                        continue
                    items.append({
                        "path": candidate,
                        "url": url,
                        "digest": item.get("digest") or "",
                        "width": item.get("width"),
                        "height": item.get("height"),
                    })
                except Exception as e:
                    # 文件被删 / 非白名单格式 / 仓库不可写都算单项失败：记进结果，
                    # 前端按「这张暂时预览不了」渲染，不至于整张画布不出图
                    logger.debug("canvas_resolve_media 单项失败: %s", candidate, exc_info=True)
                    items.append({"path": candidate, "error": f"无法生成预览：{e}"})
            return {"ok": True, "items": items}
        except Exception as e:
            logger.exception("canvas_resolve_media 失败")
            return {"ok": False, "msg": f"解析画布媒体失败：{e}"}

    def canvas_send_to_agent(
        self,
        canvas_id: Any,
        node_files: Any = None,
        instruction: Any = "",
        session_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把画布中选中的节点图片发到对话，交给智能体继续处理。

        成功/失败都原样返回 send_user_message 的结果（它本身就是
        {"ok": bool, ...} 契约），异常时为 {"ok": False, "msg": ...}。
        """
        try:
            files: List[str] = []
            for raw in node_files or []:
                candidate = str(raw or "").strip()
                if not candidate:
                    continue
                try:
                    # 前端缓存的路径可能已被用户/临时目录清理；不存在的直接丢弃，
                    # 否则 @路径 会带着幽灵附件进回合
                    if os.path.isfile(candidate):
                        files.append(candidate)
                except OSError:
                    continue
            if not files:
                return {"ok": False, "msg": "选中的图片已不存在"}
            text = self._canvas_instruction_text(canvas_id, files, instruction)
            sender = getattr(self, "send_user_message", None)
            if not callable(sender):
                return {"ok": False, "msg": "对话通道不可用"}
            result = sender(text, session_name, attachments=files)
            if isinstance(result, dict):
                return result
            return {"ok": False, "msg": "对话通道返回异常"}
        except Exception as e:
            logger.exception("canvas_send_to_agent 失败 canvas_id=%s", canvas_id)
            return {"ok": False, "msg": f"发送到对话失败：{e}"}

    # ------------------------------------------------------------------
    # 内部：上行指令文案
    # ------------------------------------------------------------------
    def _canvas_instruction_text(self, canvas_id: Any, files: List[str], instruction: Any) -> str:
        """拼一句中文指令文本；附件路径由 send_user_message 以 @路径 形式挂载。"""
        cid = str(canvas_id or "").strip()
        if cid and not _CANVAS_ID_RE.match(cid):
            cid = cid[:64]  # 只进文案，不参与路径，做个长度保险即可
        count = len(files)
        where = f"画布「{cid}」" if cid else "画布"
        head = f"下面是我在{where}里选中的 {count} 张图（见附件）。"
        extra = str(instruction or "").strip()
        if extra:
            return f"{head}\n{extra}"
        return f"{head}\n请先看清每张图的内容，然后等我下一步的具体要求。"
