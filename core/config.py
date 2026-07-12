import json

def load_config(file_path):
    """
    读取 JSON 格式的配置文件并返回 Python 字典
    """
    # 使用 with 语句安全地打开文件
    with open(file_path, 'r', encoding='utf-8') as f:
        # 将 JSON 文本解析为 Python 字典数据
        config_data = json.load(f)
        
    return config_data