import re
import os
import base64

# 附件大小上限：防止大文件撑爆内存 / token
MAX_IMAGE_BYTES = 5 * 1024 * 1024   # 图片 5MB
MAX_TEXT_BYTES = 200 * 1024         # 文本 200KB（超出部分截断）

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
        # 不再逐字符截断缩短，避免误匹配无关文件并将其注入对话上下文。
        # 若路径末尾紧贴标点（如逗号、句号），先尝试去掉最后一个字符。
        if os.path.exists(candidate) and os.path.isfile(candidate):
            extracted_files.append(candidate)
            text = text.replace(candidate, "", 1).strip()
        elif len(candidate) > 1:
            trimmed = candidate[:-1]
            if os.path.exists(trimmed) and os.path.isfile(trimmed):
                extracted_files.append(trimmed)
                text = text.replace(candidate, "", 1).strip()

    # 保序去重
    extracted_files = list(dict.fromkeys(extracted_files))

    final_content = []
    text_content = text if text else "请分析附带的文件。"

    attached_msgs = []

    for file_path in extracted_files:
        if _is_image_file(file_path):
            try:
                # 图片大小上限 5MB，超限跳过
                if os.path.getsize(file_path) > MAX_IMAGE_BYTES:
                    attached_msgs.append(f"⚠️ 图片超过 5MB 上限，已跳过: {os.path.basename(file_path)}")
                    continue
                with open(file_path, "rb") as image_file:
                    encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                    ext = os.path.splitext(file_path)[1].lower().strip('.')
                    if ext == 'jpg': ext = 'jpeg'

                    final_content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/{ext};base64,{encoded_string}"
                        }
                    })
                    attached_msgs.append(f"🖼️ 已附加图片: {os.path.basename(file_path)}")
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
