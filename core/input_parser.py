import re
import os
from pathlib import Path
import base64
import io
from dataclasses import dataclass

# 附件大小上限：防止大文件撑爆内存 / token
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 图片 20MB
MAX_DECODED_IMAGE_PIXELS = 40_000_000
MAX_TEXT_BYTES = 200 * 1024         # 文本 200KB（超出部分截断）

# 默认图片压缩参数：在视觉质量和 token 成本之间取平衡
DEFAULT_MAX_IMAGE_EDGE = 1536       # 最大边长（像素）
DEFAULT_IMAGE_QUALITY = 90          # JPEG/WebP 质量
DEFAULT_MAX_IMAGE_OUTPUT_BYTES = 1024 * 1024  # 压缩后目标上限（字节）

# @{会话名} 引用标记：} 定界，不允许跨行，标题上限 80 字符
SESSION_REF_PATTERN = re.compile(r"@\{([^}\n]{1,80})\}")


@dataclass(frozen=True)
class CompressedImage:
    data: bytes
    mime_type: str
    extension: str


def _quality_steps(quality):
    first_quality = max(50, min(int(quality), 100))
    steps = list(range(first_quality, 49, -5))
    if steps[-1] != 50:
        steps.append(50)
    return steps


def _is_image_file(path):
    ext = os.path.splitext(path)[1].lower()
    return ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']


def _looks_binary(data):
    """简单的二进制探测：头部包含 NUL 字节即视为二进制文件。

    取舍说明：UTF-16 编码的文本文件天然含大量 NUL 字节，会被这里误判为二进制
    而跳过。相比引入 chardet 之类的编码探测依赖，宁可让 UTF-16 用户手动转码，
    保证检测逻辑零依赖、零误放进真二进制。
    """
    return b"\x00" in data[:8192]


def _load_image_compression_config():
    """读取 config.json 中的图片压缩配置，失败时返回默认值。"""
    try:
        from core.model_manager import load_config
        cfg = load_config()
        img_cfg = cfg.get("image_compression", {})
        return {
            "max_edge": int(img_cfg.get("max_edge", DEFAULT_MAX_IMAGE_EDGE)),
            "quality": int(img_cfg.get("quality", DEFAULT_IMAGE_QUALITY)),
            "max_output_bytes": int(img_cfg.get("max_output_bytes", DEFAULT_MAX_IMAGE_OUTPUT_BYTES)),
        }
    except Exception:
        return {
            "max_edge": DEFAULT_MAX_IMAGE_EDGE,
            "quality": DEFAULT_IMAGE_QUALITY,
            "max_output_bytes": DEFAULT_MAX_IMAGE_OUTPUT_BYTES,
        }


def _compress_image_bytes(
    raw_bytes,
    ext,
    max_edge=DEFAULT_MAX_IMAGE_EDGE,
    quality=DEFAULT_IMAGE_QUALITY,
    max_output_bytes=DEFAULT_MAX_IMAGE_OUTPUT_BYTES,
    max_decoded_pixels=MAX_DECODED_IMAGE_PIXELS,
):
    """规范化并压缩静态图片，返回数据及其真实媒体类型。"""
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("图片处理需要 Pillow，请先安装 pillow") from exc

    with Image.open(io.BytesIO(raw_bytes)) as opened:
        source_format = str(opened.format or ext).lower()
        if source_format == "jpg":
            source_format = "jpeg"
        mime_types = {
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
            "gif": "image/gif",
        }
        if source_format not in mime_types:
            raise ValueError(f"不支持的图片格式: {source_format or ext}")

        width, height = opened.size
        decoded_pixels = width * height
        if decoded_pixels > max_decoded_pixels:
            raise ValueError(
                f"图片解码像素 {decoded_pixels:,} 超过 {max_decoded_pixels:,} 像素上限"
            )
        opened.load()

        # GIF 保留原始动画和编码；仍执行格式识别与像素上限检查。
        if source_format == "gif":
            return CompressedImage(raw_bytes, mime_types[source_format], source_format)

        orientation = opened.getexif().get(274, 1)
        image = ImageOps.exif_transpose(opened)
        transformed = orientation not in (None, 1)

        width, height = image.size
        if max(width, height) > max_edge:
            ratio = max_edge / max(width, height)
            new_size = (
                max(1, round(width * ratio)),
                max(1, round(height * ratio)),
            )
            image = image.resize(new_size, Image.Resampling.LANCZOS)
            transformed = True

        def encode_png():
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()

        def encode_lossy(candidate_quality):
            buffer = io.BytesIO()
            if source_format == "webp":
                image.save(
                    buffer,
                    format="WEBP",
                    quality=candidate_quality,
                    method=4,
                )
            else:
                jpeg_image = image
                if jpeg_image.mode not in ("RGB", "L"):
                    jpeg_image = jpeg_image.convert("RGB")
                jpeg_image.save(
                    buffer,
                    format="JPEG",
                    quality=candidate_quality,
                    optimize=True,
                    progressive=True,
                )
            return buffer.getvalue()

        if source_format == "png":
            encoded = encode_png()
        else:
            candidates = []
            encoded = None
            for candidate_quality in _quality_steps(quality):
                candidate = encode_lossy(candidate_quality)
                candidates.append(candidate)
                if len(candidate) <= max_output_bytes:
                    encoded = candidate
                    break
            if encoded is None:
                encoded = min(candidates, key=len)

        # 未发生旋转或缩放时，体积没有收益就保留原始编码，避免无谓重编码。
        if not transformed and len(encoded) >= len(raw_bytes):
            encoded = raw_bytes
        return CompressedImage(encoded, mime_types[source_format], source_format)


def _is_within_sandbox(file_path, session_id=None):
    """M38 修复：@ 挂载/绝对路径挂载必须与 core/tools.py 走同一套沙盒边界校验。

    只读复用 tools.py 的 _resolve_workspace_path（不修改 tools.py）：
    默认仅允许当前工作目录内的路径；/sandbox on（或会话级/线程级放行）时与
    read_file 一样放行工作目录外路径。越界返回 False，由调用方跳过该附件，
    避免 ../.env、C:\\...\\id_rsa 之类的敏感文件被直接内联进 LLM 上下文并持久化。
    用户显式上传/选择的附件和粘贴板临时图片属于用户授权操作，直接放行。
    """
    try:
        from core.tools import is_trusted_paste_path, is_session_user_attached_path
        if is_trusted_paste_path(file_path) or is_session_user_attached_path(file_path, session_id):
            return True
    except Exception:
        pass
    try:
        from core.tools import _resolve_workspace_path
        _resolve_workspace_path(file_path, allow_trusted_read=True)
        return True
    except Exception:
        return False


def parse_user_input(text, explicit_paths=None, session_id=None):
    """
    解析用户输入中的文件路径（包括 @文件 和绝对路径）。
    返回 (处理后的最终 content, 附加的系统提示字符串列表)
    content 如果包含图片，将返回 LiteLLM/OpenAI 兼容的 list 结构。
    explicit_paths: 来自 UI 显式选择或拖拽的附件列表（用户明确授权，不受沙盒越界限制）。
    """
    if explicit_paths:
        try:
            from core.tools import register_session_user_attached_paths
            register_session_user_attached_paths(explicit_paths, session_id=session_id)
        except Exception:
            pass

    extracted_files = []

    # 0. 优先加入显式传入的附件路径
    if explicit_paths:
        for ep in explicit_paths:
            if ep and os.path.exists(ep) and os.path.isfile(ep):
                extracted_files.append(str(Path(ep).resolve()))

    # 1. 提取 @文件名 格式
    at_matches = re.findall(r'@([a-zA-Z0-9_\-\.\/\\]+)', text)
    for match in at_matches:
        if os.path.exists(match):
            extracted_files.append(match)
            # 把 @xxx 从原始文本中删掉（只替换首个匹配，避免误删正文相同字串）
            text = text.replace(f"@{match}", "", 1).strip()

    # 2. 提取盘符绝对路径 (例如 D:\wechatHistory\...\temp.png 或 D:/path/temp.png)
    # 匹配带引号或不带引号的绝对路径，反斜杠与正斜杠两种写法都认
    abs_matches = re.finditer(r'(?:[\'"]?)([a-zA-Z]:[\\/][^\s\'"]+)(?:[\'"]?)', text)
    for m in abs_matches:
        candidate = m.group(1)
        # 只接受原始候选路径精确匹配到真实文件的情况；
        # 若路径末尾紧贴常见标点（如逗号、句号），循环去掉尾部标点直到找到真实文件或不再是标点。
        # 注意：仅对标点字符做裁剪，避免把有效路径（如 D:data.txt）末尾字母切掉。
        _TRAILING_PUNCTUATION = {',', '.', ';', '!', '?', ':', ')', ']', '}', '"', "'"}
        trimmed = candidate
        while len(trimmed) > 1 and trimmed[-1] in _TRAILING_PUNCTUATION:
            if os.path.exists(trimmed) and os.path.isfile(trimmed):
                break
            trimmed = trimmed[:-1]
        if os.path.exists(trimmed) and os.path.isfile(trimmed):
            extracted_files.append(trimmed)
            text = text.replace(candidate, "", 1).strip()

    # 保序去重
    extracted_files = list(dict.fromkeys(extracted_files))

    final_content = []
    text_content = text if text else "请分析附带的文件。"

    attached_msgs = []
    img_cfg = _load_image_compression_config()

    for file_path in extracted_files:
        # 用户显式指定的附件与粘贴图片直接放行；其余未受托的工作目录外路径拦截跳过
        try:
            allowed = _is_within_sandbox(file_path, session_id=session_id)
        except TypeError:
            allowed = _is_within_sandbox(file_path)
        if not allowed:
            attached_msgs.append(
                f"⚠️ 已跳过工作目录外的文件（可通过 /sandbox on 开放）: {os.path.basename(file_path)}"
            )
            continue
        try:
            from core.tools import register_session_user_attached_paths
            register_session_user_attached_paths([file_path], session_id=session_id)
        except Exception:
            pass
        if _is_image_file(file_path):
            try:
                # 原始文件限制保护读取内存，解码后另有像素数量限制。
                if os.path.getsize(file_path) > MAX_IMAGE_BYTES:
                    limit_mb = MAX_IMAGE_BYTES // (1024 * 1024)
                    attached_msgs.append(
                        f"⚠️ 图片超过 {limit_mb}MB 上限，已跳过: {os.path.basename(file_path)}"
                    )
                    continue
                ext = os.path.splitext(file_path)[1].lower().strip('.')
                if ext == 'jpg': ext = 'jpeg'

                with open(file_path, "rb") as image_file:
                    raw_bytes = image_file.read()

                compressed = _compress_image_bytes(
                    raw_bytes, ext,
                    max_edge=img_cfg["max_edge"],
                    quality=img_cfg["quality"],
                    max_output_bytes=img_cfg["max_output_bytes"],
                )
                if len(compressed.data) < len(raw_bytes):
                    saved = len(raw_bytes) - len(compressed.data)
                    attached_msgs.append(
                        f"🖼️ 已附加图片: {os.path.basename(file_path)}"
                        f"（压缩后 {len(compressed.data)//1024}KB，节省 {saved//1024}KB）"
                    )
                else:
                    attached_msgs.append(f"🖼️ 已附加图片: {os.path.basename(file_path)}")

                # 图片成功附加（压缩/未压缩分支均覆盖）：把原图路径写进文本，
                # 供模型需要改图/垫图时把该路径传给生图插件的 image 参数
                text_content += f"\n\n[附带图片文件: {os.path.basename(file_path)}]\n完整路径: {os.path.abspath(file_path)}（需要改图/垫图时，把此路径传给生图插件的 image 参数）\n"

                encoded_string = base64.b64encode(compressed.data).decode('utf-8')
                final_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{compressed.mime_type};base64,{encoded_string}"
                    },
                    # 折叠占位符引用的原图来源路径；发往模型前由 _strip_media_and_merge 剥离
                    "source_path": os.path.abspath(file_path),
                })
            except Exception as e:
                attached_msgs.append(f"⚠️ 读取图片失败 {file_path}: {e}")
        else:
            try:
                with open(file_path, "rb") as bin_file:
                    raw = bin_file.read(MAX_TEXT_BYTES + 1)
                truncated = len(raw) > MAX_TEXT_BYTES
                raw = raw[:MAX_TEXT_BYTES]

                # 二进制非图片文件：无法直接内联内容，但把文件路径告知模型，
                # 由模型自行用工具（Python 库/命令行）解析，而不是静默跳过
                if _looks_binary(raw):
                    text_content += (
                        f"\n\n[附带文件: {os.path.basename(file_path)}]\n"
                        f"路径: {os.path.abspath(file_path)}\n"
                        "（该文件为二进制格式（如 PDF/Word/Excel/zip），无法直接内联内容。"
                        "如需读取内容，请使用可用工具（如运行 Python 脚本调用相应解析库）解析该文件。）\n"
                    )
                    attached_msgs.append(f"📎 已附加文件路径（二进制，模型可用工具读取）: {os.path.basename(file_path)}")
                    continue

                try:
                    content = raw.decode("utf-8")
                    enc_tag = ""
                except UnicodeDecodeError:
                    try:
                        content = raw.decode("gbk")
                        enc_tag = "(GBK)"
                    except UnicodeDecodeError:
                        if not truncated:
                            attached_msgs.append(f"⚠️ 无法解码的文本文件（疑似二进制），已跳过: {os.path.basename(file_path)}")
                            continue
                        # 截断可能正好切在多字节字符中间：用 errors="replace" 替换
                        # 损坏字节，而不是把正常文本误判为二进制跳过；取乱码更少的结果
                        utf8_guess = raw.decode("utf-8", errors="replace")
                        gbk_guess = raw.decode("gbk", errors="replace")
                        if utf8_guess.count("\ufffd") <= gbk_guess.count("\ufffd"):
                            content, enc_tag = utf8_guess, ""
                        else:
                            content, enc_tag = gbk_guess, "(GBK)"

                if truncated:
                    content += "\n... (⚠️ 文件超过 200KB，内容已截断)"
                text_content += f"\n\n[附带上下文: {os.path.basename(file_path)}]\n```\n{content}\n```\n"
                attached_msgs.append(f"📄 已附加代码/文本{enc_tag}: {os.path.basename(file_path)}")
            except Exception as e:
                attached_msgs.append(f"⚠️ 读取文本文件失败 {file_path}: {e}")

    if final_content:
        # 如果有图片，必须转为数组结构
        final_content.insert(0, {"type": "text", "text": text_content})
        return final_content, attached_msgs
    else:
        # 如果只有纯文本文件，直接返回字符串
        return text_content, attached_msgs


def extract_session_refs(text):
    """提取文本中的 @{会话名} 引用标记，返回 (清理后的文本, 会话名列表)。

    会话名去重保序；所有 @{...} 标记均从文本中移除。
    与 @文件 解析互不干扰：文件正则字符集不含 { }。
    须在 parse_user_input 之前调用。
    """
    if not text:
        return text, []
    refs = []

    def _sub(m):
        name = m.group(1).strip()
        if name and name not in refs:
            refs.append(name)
        return ""

    cleaned = SESSION_REF_PATTERN.sub(_sub, text)
    return cleaned.strip(), refs
