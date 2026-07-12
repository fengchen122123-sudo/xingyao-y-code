import re
import os
import base64

def _is_image_file(path):
    ext = os.path.splitext(path)[1].lower()
    return ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']

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
            # 把 @xxx 从原始文本中删掉，避免重复干扰
            text = text.replace(f"@{match}", "").strip()
            
    # 2. 提取盘符绝对路径 (例如 D:\wechatHistory\...\temp.png)
    # 匹配带引号或不带引号的绝对路径
    abs_matches = re.finditer(r'(?:[\'"]?)([a-zA-Z]:\\[^\s\'"]+)(?:[\'"]?)', text)
    for m in abs_matches:
        candidate = m.group(1)
        # 从右往左缩小范围，解决用户紧贴着路径敲字（无空格）导致识别失败的问题
        while candidate:
            if os.path.exists(candidate) and os.path.isfile(candidate):
                extracted_files.append(candidate)
                text = text.replace(candidate, "").strip()
                break
            candidate = candidate[:-1]
            
    # 去重
    extracted_files = list(set(extracted_files))
    
    final_content = []
    text_content = text if text else "请分析附带的文件。"
    
    attached_msgs = []
    
    for file_path in extracted_files:
        if _is_image_file(file_path):
            try:
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
                with open(file_path, "r", encoding="utf-8") as text_file:
                    content = text_file.read()
                    text_content += f"\n\n[附带上下文: {os.path.basename(file_path)}]\n```\n{content}\n```\n"
                    attached_msgs.append(f"📄 已附加代码/文本: {os.path.basename(file_path)}")
            except Exception as e:
                try:
                    with open(file_path, "r", encoding="gbk") as text_file:
                        content = text_file.read()
                        text_content += f"\n\n[附带上下文: {os.path.basename(file_path)}]\n```\n{content}\n```\n"
                        attached_msgs.append(f"📄 已附加代码/文本(GBK): {os.path.basename(file_path)}")
                except:
                    attached_msgs.append(f"⚠️ 读取文本文件失败 {file_path}")
                    
    if final_content:
        # 如果有图片，必须转为数组结构
        final_content.insert(0, {"type": "text", "text": text_content})
        return final_content, attached_msgs
    else:
        # 如果只有纯文本文件，直接返回字符串
        return text_content, attached_msgs
