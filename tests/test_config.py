import json
import pytest
from pathlib import Path
from core.config import load_config

def test_load_valid_config(tmp_path):
    """测试是否能正确读取 config.json 文件"""
    
    # 1. 准备测试数据 (Arrange)
    # 使用 pytest 内置的 tmp_path 创建一个临时的、干净的 config.json
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "default_model": "gpt-4o",
        "api_key": "sk-test-key-123"
    }))

    # 2. 执行业务代码 (Act)
    # 调用我们将要写的 load_config 函数去读取这个文件
    config = load_config(config_file)

    # 3. 断言验证 (Assert)
    # 检查读出来的数据是不是我们期望的
    assert config["default_model"] == "gpt-4o"
    assert config["api_key"] == "sk-test-key-123"

def test_load_config_file_not_found():
    """测试当配置文件不存在时，代码是否能优雅地报错或处理"""
    
    # 制造一个根本不存在的文件路径
    fake_path = Path("this_file_does_not_exist.json")
    
    # 我们期望当代码去读这个假文件时，抛出 FileNotFoundError 异常
    with pytest.raises(FileNotFoundError):
        load_config(fake_path)