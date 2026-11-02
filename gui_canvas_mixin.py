"""GUI 画布桥接混入（JSON Canvas v1 的读 / 写 / 上行）。

ApiBridge（前端与测试里也叫 GuiBridge）继承本混入；pywebview 反射暴露
public snake_case 方法，前端只需 js_api.canvas_list / canvas_load /
canvas_save / canvas_send_to_agent / canvas_resolve_media / canvas_edit_image /
canvas_edit_image_annotated 七个入口即可完成画布的全部持久化、"选中节点发到
对话"、"本地路径换预览 URL"与"画布就地改图"（含画笔标注合成）。

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

import base64
import io
import json
import logging
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 画布目录（相对会话工作目录），与 mockups/aigc-canvas.html 里的
# ".ycode/canvas/cat-series.json" 口径一致
CANVAS_SUBDIR: tuple = (".ycode", "canvas")
# 画布就地改图的产物目录（相对工作目录）。刻意不落 AI 生成内容临时目录：
# 那里会被撤销回合 / 启动清理，节点一旦引用就变成破图；放这里能随项目一起走。
CANVAS_MEDIA_SUBDIR: tuple = (*CANVAS_SUBDIR, "media")
# 前端/后端共同支持的画布文档版本
CANVAS_SCHEMA_VERSION = 1
# canvas_id 白名单：只允许字母数字下划线短横线，1~64 位。
# 既挡住路径分隔符与 ".."（路径穿越），也挡住 Windows 特殊字符。
_CANVAS_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# 就地改图走的插件工具名（tokenrhythm_image 插件提供）
_CANVAS_EDIT_TOOL = "tokenrhythm_edit_image"
# 与插件 edit_image 的限制同口径：上游只接受 1 张 jpg/jpeg/png、≤10MB。
# 在入口先挡一次，省掉一次 0.2 元的无效请求
_CANVAS_EDIT_MAX_BYTES = 10 * 1024 * 1024
_CANVAS_EDIT_ALLOWED_EXTS = (".jpg", ".jpeg", ".png")
_CANVAS_EDIT_MISSING_TOOL_MSG = (
    "改图插件未启用，请在设置页启用 tokenrhythm_image 并配置 API Key"
)
# 改图并发闸门：canvas_id -> job_id。改图是同步阻塞调用（最长 180s、按张计费），
# 连点会同时烧钱又互相覆盖节点状态，所以单画布同时只放 1 个任务进来。
_CANVAS_EDIT_JOBS: Dict[str, str] = {}
_CANVAS_EDIT_JOBS_LOCK = threading.Lock()


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

    def _canvas_media_dir(self) -> Path:
        """画布媒体目录（<workdir>/.ycode/canvas/media/），按需创建。

        改图产物与节点都要按相对路径回写，所以目录必须真实存在；创建失败
        直接抛给调用方，由它转成 ok=False（写不了盘就没必要再花 0.2 元）。
        """
        path = Path(self._canvas_workdir()).joinpath(*CANVAS_MEDIA_SUBDIR)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _canvas_media_relpath(self, abs_path: str) -> str:
        """绝对路径 → 相对工作目录的 posix 风格路径（节点里就存这个）。

        画布节点存相对路径才能随项目目录一起搬走；分隔符统一成 "/"，
        避免 Windows 反斜杠进 JSON 后再被前端当成转义。
        """
        try:
            rel = os.path.relpath(abs_path, self._canvas_workdir())
        except ValueError:
            # 跨盘符无法求相对路径（Windows）：原样返回绝对路径，前端仍能用
            return abs_path
        return str(rel).replace("\\", "/")

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
    # 画布就地改图（不经过对话，直接调改图插件）
    # ------------------------------------------------------------------
    def canvas_edit_image(
        self,
        canvas_id: Any,
        node_file: Any,
        prompt: Any,
        source_node_id: Any,
        model: Any = "",
        confirmed: Any = False,
    ) -> Dict[str, Any]:
        """在画布图片节点上就地改图：校验入参后丢给守护线程，立刻返回 job_id。

        为什么必须异步：改图是同步阻塞调用（最长 180s 超时），js_api 调用
        挂三分钟会让整个 WebView 界面冻结。这里只做校验 + 起线程，结果通过
        canvas_edit_result 事件回推。

        成功: {"ok": True, "job_id": ...}
        拒绝/异常: {"ok": False, "msg": ...}
        """
        try:
            # 本路径直接执行插件工具，绕过了调度层的权限判定，因此用
            # confirmed=True 把"用户点了生成"这个授权动作显式带进来
            if confirmed is not True:
                return {"ok": False, "msg": "改图需要用户确认"}
            cid = str(canvas_id or "").strip()
            if not _CANVAS_ID_RE.match(cid):
                return {"ok": False, "msg": "非法画布 ID"}
            text = str(prompt or "").strip()
            if not text:
                return {"ok": False, "msg": "请先写一句改图要求"}
            source = self._canvas_edit_validate_source(node_file)
            if not source["ok"]:
                return source
            return self._canvas_edit_start_job(
                cid,
                source_node_id,
                source["path"],
                text,
                str(model or "").strip(),
            )
        except Exception as e:
            logger.exception("canvas_edit_image 失败 canvas_id=%s", canvas_id)
            return {"ok": False, "msg": f"改图请求失败：{e}"}

    def _run_canvas_edit_job(
        self,
        job_id: str,
        canvas_id: str,
        source_node_id: str,
        image_path: str,
        prompt: str,
        model: str,
        save_dir: str,
        annotation_path: str = "",
    ) -> None:
        """改图线程体：调插件工具 → 归档取预览 URL → 推事件 → 清闸门。

        线程里绝不动界面：所有回推都走 _emit_raw（它自己入 _js_queue，由
        gui-js-dispatcher 单消费者线程调 evaluate_js），否则会撞上 pywebview
        的线程限制。任何异常都必须在这里消化成 error 事件，绝不外抛——守护
        线程里抛出去的异常没人接，前端会一直停在 pending。
        """
        data: Dict[str, Any] = {
            "ok": False,
            "job_id": job_id,
            "canvas_id": canvas_id,
            "source_node_id": source_node_id,
            "path": "",
            "abs_path": "",
            "url": "",
            "width": None,
            "height": None,
            "annotation_path": "",
            "annotation_abs_path": "",
            "error": "",
        }
        try:
            if annotation_path:
                # 画笔标注链路才有的字段：把"喂给模型的合成图"回传一份，用户
                # 抱怨"改错地方了"时能直接打开它对齐笔迹位置。合成图是中间
                # 产物，刻意不清理。
                data["annotation_abs_path"] = annotation_path
                data["annotation_path"] = self._canvas_media_relpath(annotation_path)
            # 懒单例：首次访问才装配插件工具表，这里不能提前 import 顶到模块层
            from core import llm

            registry = getattr(llm, "tool_registry")
            kwargs: Dict[str, Any] = {
                "prompt": prompt,
                "image": image_path,
                "save_dir": save_dir,
            }
            if model:
                # 空 model 交给工具默认值（qwen-image-2.0），别传空串覆盖它
                kwargs["model"] = model
            try:
                result = registry.execute_strict(_CANVAS_EDIT_TOOL, **kwargs)
            except KeyError:
                # 插件未启用 / 加载失败时注册表里没有这个工具
                data["error"] = _CANVAS_EDIT_MISSING_TOOL_MSG
                return
            if getattr(result, "is_error", False):
                data["error"] = str(getattr(result, "content", "") or "").strip() or "改图失败"
                return
            changed = list(getattr(result, "changed_files", None) or [])
            new_path = str(changed[0] if changed else "").strip()
            if not new_path:
                data["error"] = "改图已完成但没有拿到产物路径，请重试"
                return
            if not os.path.isfile(new_path):
                data["error"] = "改图产物文件不存在，请重试"
                return
            # abs_path 只给排查用；节点里存相对路径才能随项目搬走
            data["abs_path"] = new_path
            data["path"] = self._canvas_media_relpath(new_path)
            # 预览 URL 与 canvas_resolve_media 同口径：内容寻址仓库 + 本机媒体服务。
            # 这一步失败不该推翻"图已经出好了"的事实，url 留空前端画占位图即可
            try:
                from core.media_server import media_server
                from core.media_store import archive_media_file

                item = archive_media_file(new_path)
                data["url"] = media_server.public_url(
                    item.get("digest") or "", item.get("ext") or ""
                ) or ""
                data["width"] = item.get("width")
                data["height"] = item.get("height")
            except Exception:
                logger.debug("canvas_edit_image：预览 URL 生成失败", exc_info=True)
            data["ok"] = True
        except Exception as e:
            logger.exception("canvas_edit_image 任务失败 job_id=%s", job_id)
            data["ok"] = False
            data["error"] = f"改图失败：{e}"
        finally:
            try:
                self._emit_raw("canvas_edit_result", data)
            except Exception:
                # 事件送不出去也要保证闸门被清，否则这张画布再也改不了图
                logger.exception("canvas_edit_image：事件投递失败 job_id=%s", job_id)
            with _CANVAS_EDIT_JOBS_LOCK:
                # 只清自己的占位：极端串行下已可能被新任务接手，不能误删别人
                if _CANVAS_EDIT_JOBS.get(canvas_id) == job_id:
                    _CANVAS_EDIT_JOBS.pop(canvas_id, None)

    def canvas_edit_image_annotated(
        self,
        canvas_id: Any,
        node_file: Any,
        prompt: Any,
        source_node_id: Any,
        annotation_png_b64: Any,
        model: Any = "",
        confirmed: Any = False,
    ) -> Dict[str, Any]:
        """带画笔标注的就地改图：先把标注层合成进源图，再走同一条改图链路。

        与 canvas_edit_image 的差别只有"垫图从哪来"：这里用合成图顶替源图，
        其余（并发闸门、线程体、事件格式）完全复用，前端不需要第二套事件。
        合成是本地 CPU 动作（毫秒级），所以放在同步段做——失败能在起线程之前
        就拒绝，不占用闸门、也不会白花一次 0.2 元。

        成功: {"ok": True, "job_id": ...}
        拒绝/异常: {"ok": False, "msg": ...}
        """
        try:
            # 授权口径与普通改图一致：本路径绕过调度层权限判定，靠 confirmed
            if confirmed is not True:
                return {"ok": False, "msg": "改图需要用户确认"}
            cid = str(canvas_id or "").strip()
            if not _CANVAS_ID_RE.match(cid):
                return {"ok": False, "msg": "非法画布 ID"}
            text = str(prompt or "").strip()
            if not text:
                return {"ok": False, "msg": "请先写一句改图要求"}
            source = self._canvas_edit_validate_source(node_file)
            if not source["ok"]:
                return source
            try:
                save_dir = str(self._canvas_media_dir())
            except Exception as e:
                logger.exception("canvas_edit_image_annotated：媒体目录创建失败")
                return {"ok": False, "msg": f"创建画布媒体目录失败：{e}"}
            composed = self._canvas_edit_compose_annotation(
                source["path"], annotation_png_b64, save_dir
            )
            if not composed["ok"]:
                # 合成失败不进线程、不占闸门：坏数据没有理由挡住下一次正常改图
                return composed
            # 补一句固定说明：上游 edits 接口没有 mask 参数（见设计 §5.2），
            # 标注只是视觉提示，必须用文字把"只改这里"再讲一遍
            annotated_prompt = (
                f"{text}\n图中红色标注处需要按上述要求修改，画面其他部分保持原样。"
            )
            return self._canvas_edit_start_job(
                cid,
                source_node_id,
                composed["path"],
                annotated_prompt,
                str(model or "").strip(),
                save_dir=save_dir,
                annotation_path=composed["path"],
            )
        except Exception as e:
            logger.exception("canvas_edit_image_annotated 失败 canvas_id=%s", canvas_id)
            return {"ok": False, "msg": f"改图请求失败：{e}"}

    def _canvas_edit_validate_source(self, node_file: Any) -> Dict[str, Any]:
        """源图校验：存在 / ≤10MB / jpg-jpeg-png（两条改图入口共用）。

        为什么抽出来：普通改图与画笔标注如果各写一份，判定顺序和文案迟早
        走偏，同一张图在两条链路上一个放行一个拒绝，用户只会以为是标注坏了。

        成功: {"ok": True, "path": 绝对路径}；拒绝: {"ok": False, "msg": ...}
        """
        candidate = str(node_file or "").strip()
        if not candidate:
            return {"ok": False, "msg": "缺少源图路径"}
        # 节点存相对项目根的路径，绝对路径（用户手填 / 外部图）原样用；
        # normpath 把前端带来的 "/" 与 "a/../b" 归一，日志和工具入参才干净
        image_path = os.path.normpath(
            candidate
            if os.path.isabs(candidate)
            else os.path.join(self._canvas_workdir(), candidate)
        )
        if not os.path.isfile(image_path):
            return {"ok": False, "msg": "源图已不存在，请重新选择"}
        try:
            size = os.path.getsize(image_path)
        except OSError as e:
            return {"ok": False, "msg": f"读取源图信息失败：{e}"}
        if size > _CANVAS_EDIT_MAX_BYTES:
            return {"ok": False, "msg": "源图超过 10MB 限制，请压缩后再试"}
        ext = os.path.splitext(image_path)[1].lower()
        if ext not in _CANVAS_EDIT_ALLOWED_EXTS:
            return {"ok": False, "msg": f"源图格式不支持：{ext or '无扩展名'}，仅支持 jpg/jpeg/png"}
        return {"ok": True, "path": image_path}

    def _canvas_edit_start_job(
        self,
        canvas_id: str,
        source_node_id: Any,
        image_path: str,
        prompt: str,
        model: str,
        save_dir: str = "",
        annotation_path: str = "",
    ) -> Dict[str, Any]:
        """过并发闸门 + 起守护线程（两条改图入口共用）。

        闸门占位与"线程起不来就回滚"这两处最容易写漏，漏一次就是"这张画布
        再也改不了图"，所以只允许存在一份实现。
        """
        try:
            if not save_dir:
                save_dir = str(self._canvas_media_dir())
        except Exception as e:
            logger.exception("canvas_edit_image：媒体目录创建失败")
            return {"ok": False, "msg": f"创建画布媒体目录失败：{e}"}

        job_id = uuid.uuid4().hex
        with _CANVAS_EDIT_JOBS_LOCK:
            if _CANVAS_EDIT_JOBS.get(canvas_id):
                return {"ok": False, "msg": "这张图正在改，等它回来"}
            _CANVAS_EDIT_JOBS[canvas_id] = job_id

        worker = threading.Thread(
            target=self._run_canvas_edit_job,
            args=(job_id, canvas_id, str(source_node_id or ""), image_path, prompt,
                  model, save_dir),
            kwargs={"annotation_path": annotation_path},
            name=f"canvas-edit-{canvas_id}",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as e:
            logger.exception("canvas_edit_image：线程启动失败")
            with _CANVAS_EDIT_JOBS_LOCK:
                if _CANVAS_EDIT_JOBS.get(canvas_id) == job_id:
                    _CANVAS_EDIT_JOBS.pop(canvas_id, None)
            return {"ok": False, "msg": f"改图任务启动失败：{e}"}
        return {"ok": True, "job_id": job_id}

    def _canvas_edit_compose_annotation(
        self, image_path: str, annotation_png_b64: Any, save_dir: str
    ) -> Dict[str, Any]:
        """把前端导出的透明标注层合成到源图上，返回合成图绝对路径。

        为什么合成放后端：前端 canvas 一旦 drawImage 原图（媒体 URL 属跨源）
        就被标脏，toBlob 会直接抛安全错误；前端只画笔迹、后端再把原图补上，
        导出才永远合法（见设计 §5.2）。

        成功: {"ok": True, "path", "width", "height"}；失败: {"ok": False, "msg": ...}
        """
        raw = "".join(str(annotation_png_b64 or "").split())
        if not raw:
            return {"ok": False, "msg": "缺少画笔标注数据"}
        # 前端 canvas.toDataURL 天然带 "data:image/png;base64," 前缀，顺手剥掉，
        # 免得把前缀当成非法 base64 报给用户
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        try:
            # validate=True：宁可明确拒绝，也不让 b64decode 悄悄丢掉非法字符
            # 后把一张"半个笔迹"的图送进模型
            payload = base64.b64decode(raw, validate=True)
        except Exception:
            return {"ok": False, "msg": "标注数据不是合法的 base64，请重新标注"}
        if not payload:
            return {"ok": False, "msg": "标注数据为空，请重新标注"}
        # PNG 魔数先挡一次：非 PNG 的 base64 不必等 Pillow 解码就能给出原因
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return {"ok": False, "msg": "标注层必须是 PNG 图片"}

        # Pillow 惰性导入：与 core/media_store.py 同口径，缺依赖不该让整个画布
        # 模块 import 失败，只让标注这条路径明确报错
        try:
            from PIL import Image  # type: ignore
        except Exception:
            logger.exception("画笔标注：Pillow 不可用")
            return {"ok": False, "msg": "缺少 Pillow 依赖，无法合成画笔标注"}

        try:
            with Image.open(io.BytesIO(payload)) as ann_img:
                # 主动解码：坏 PNG 在这里就炸，别拖到 alpha_composite 才炸
                ann_img.load()
                if str(getattr(ann_img, "format", "") or "").upper() != "PNG":
                    return {"ok": False, "msg": "标注层必须是 PNG 图片"}
                if "A" not in ann_img.getbands():
                    # 没有 alpha 就没有"透明底"，笔迹会把整张图盖住；仍按不透明
                    # 合成（至少还能改成功），但留条日志便于排查异常笔迹
                    logger.debug(
                        "画笔标注：标注层无 alpha 通道（mode=%s），按不透明合成",
                        ann_img.mode,
                    )
                annotation = ann_img.convert("RGBA")
        except Exception as e:
            return {"ok": False, "msg": f"标注层图片无法解析：{e}"}

        try:
            with Image.open(image_path) as src_img:
                base = src_img.convert("RGBA")
        except Exception as e:
            return {"ok": False, "msg": f"源图无法打开（可能已损坏）：{e}"}

        if annotation.size != base.size:
            # 前端应把画布内部像素尺寸设成原图尺寸；不一致说明换算走偏了。
            # 这里按源图缩放兜底：宁可轻微错位，也不要整条链路失败
            logger.debug(
                "画笔标注：标注层尺寸 %s 与源图 %s 不一致，按源图缩放兜底；"
                "前端应导出原图尺寸的标注层",
                annotation.size,
                base.size,
            )
            resample = getattr(Image, "LANCZOS", None)
            if resample is None:  # Pillow ≥ 9.1 把重采样常量挪进了 Image.Resampling
                resample = Image.Resampling.LANCZOS
            annotation = annotation.resize(base.size, resample)

        try:
            base.alpha_composite(annotation)
            # 文件名带时间戳 + 短随机串：同一秒内连点两次也不会互相覆盖源图
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            out_path = Path(save_dir) / f"annotation-{stamp}-{uuid.uuid4().hex[:6]}.png"
            base.save(out_path, format="PNG")
        except Exception as e:
            logger.exception("画笔标注：合成失败 image=%s", image_path)
            return {"ok": False, "msg": f"合成标注图失败：{e}"}

        try:
            out_size = out_path.stat().st_size
        except OSError as e:
            return {"ok": False, "msg": f"读取合成图大小失败：{e}"}
        if out_size > _CANVAS_EDIT_MAX_BYTES:
            # 明确报错而不是压质量截断：静默降质会让模型看到的图与用户看到的
            # 不一样，事后极难定位；合成图本身留在盘上供人工比对
            return {"ok": False, "msg": "标注合成图超过 10MB 限制，请缩小源图后重试"}
        return {
            "ok": True,
            "path": str(out_path),
            "width": base.size[0],
            "height": base.size[1],
        }

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
