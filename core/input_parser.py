import re
import os
import base64
import io

# 附件大小上限：防止大文件撑爆内存 / token
MAX_IMAGE_BYTES = 5 * 1024 * 1024   # 图片 5MB
MAX_TEXT_BYTES = 200 * 1024         # 文本 200KB（超出部分截断）

# 默认图片压缩参数：在视觉质量和 token 成本之间取平衡
DEFAULT_MAX_IMAGE_EDGE = 1024       # 最大边长（像素）
DEFAULT_IMAGE_QUALITY = 85          # JPEG/WebP 质量
DEFAULT_MAX_IMAGE_OUTPUT_BYTES = 500 * 1024  # 压缩后目标上限（字节）


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


def _compress_image_bytes(raw_bytes, ext, max_edge=1024, quality=85, max_output_bytes=500*1024):
    """用 Pillow 压缩图片，返回压缩后的 bytes。

    策略：
    1. 限制最大边长；
    2. 对 JPEG/WebP 调整质量；
    3. 如果压缩后反而更大，回退到原图；
    4. Pillow 不可用时返回原图。

    仅处理静态图片，GIF 跳过压缩（保留动图）。
    """
    if ext == "gif":
        return raw_bytes
    try:
        from PIL import Image
    except Exception:
        return raw_bytes

    try:
        img = Image.open(io.BytesIO(raw_bytes))
        # 统一转换为 RGB（PNG 带透明通道时转白底，视觉模型兼容性更好）
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # 等比缩放，限制最大边长
        width, height = img.size
        if max(width, height) > max_edge:
            ratio = max_edge / max(width, height)
            new_size = (int(width * ratio), int(height * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        # 选择输出格式：原图是 PNG 且尺寸较大时转 JPEG 更省体积；否则保持 JPEG/WebP
        out_ext = ext if ext in ("jpeg", "jpg", "webp") else "jpeg"

        def _encode(q):
            buf = io.BytesIO()
            if out_ext == "webp":
                img.save(buf, format="WEBP", quality=q, method=3)
            else:
                img.save(buf, format="JPEG", quality=q, optimize=True)
            return buf.getvalue()

        # 先按默认质量编码
        encoded = _encode(quality)

        # 如果仍超限，逐步降低质量再试一次
        if len(encoded) > max_output_bytes and quality > 50:
            encoded = _encode(50)

        # 压缩有效才使用；否则回退原图避免画质劣化无收益
        if len(encoded) < len(raw_bytes):
            return encoded
        return raw_bytes
    except Exception:
        return raw_bytes


def parse_user_input(text):
    """
    解析用户输入中的文件路径（包括 @文件 和绝对路径）。
    返回 (处理后的最终 content, 附加的系统提示字符串列表)
    content 如果包含图片，将返回 LiteLLM/OpenAI 兼容的 list 结构。
    """
    extracted_files = []

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
        if _is_image_file(file_path):
            try:
                # 图片大小上限 5MB，超限跳过
                if os.path.getsize(file_path) > MAX_IMAGE_BYTES:
                    attached_msgs.append(f"⚠️ 图片超过 5MB 上限，已跳过: {os.path.basename(file_path)}")
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
                if len(compressed) < len(raw_bytes):
                    saved = len(raw_bytes) - len(compressed)
                    attached_msgs.append(
                        f"🖼️ 已附加图片: {os.path.basename(file_path)}"
                        f"（压缩后 {len(compressed)//1024}KB，节省 {saved//1024}KB）"
                    )
                else:
                    attached_msgs.append(f"🖼️ 已附加图片: {os.path.basename(file_path)}")

                encoded_string = base64.b64encode(compressed).decode('utf-8')
                final_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/{ext};base64,{encoded_string}"
                    }
                })
            except Exception as e:
                attached_msgs.append(f"⚠️ 读取图片失败 {file_path}: {e}")
        else:
            try:
                with open(file_path, "rb") as bin_file:
                    raw = bin_file.read(MAX_TEXT_BYTES + 1)
                truncated = len(raw) > MAX_TEXT_BYTES
                raw = raw[:MAX_TEXT_BYTES]

                # 二进制非图片文件：不尝试兜底解码，直接提示跳过
                if _looks_binary(raw):
                    attached_msgs.append(f"⚠️ 检测到二进制文件，已跳过: {os.path.basename(file_path)}")
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
