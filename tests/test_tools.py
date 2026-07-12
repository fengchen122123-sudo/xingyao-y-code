import pytest
from pathlib import Path
from core.tools import read_file, write_file, list_directory, execute_command

def test_read_and_write_file(tmp_path):
    """测试 Agent 是否能正确读取和写入文件"""
    test_file = tmp_path / "hello.py"
    content_to_write = "print('Hello, Y-code!')"
    
    write_file(test_file, content_to_write)
    read_content = read_file(test_file)
    
    assert read_content == content_to_write

def test_list_directory(tmp_path):
    """测试雷达功能：能否看清文件夹里有什么"""
    # 1. 准备：在隔离区造一个假项目，里面有两个文件
    (tmp_path / "main.py").touch()
    (tmp_path / "readme.md").touch()
    
    # 2. 执行：启动雷达扫描
    files = list_directory(str(tmp_path))
    
    # 3. 断言验证：检查雷达有没有扫出这两个文件
    assert "main.py" in files
    assert "readme.md" in files

def test_execute_command():
    """测试引擎功能：能否在终端里执行命令"""
    # 1. 执行：让 Python 自己在终端里跑一句打印命令
    # 注意：为了跨平台兼容，我们用 python -c 来执行一句简单的代码
    result = execute_command('python -c "print(\'hello engine\')"')
    
    # 2. 断言验证：终端里是不是真的输出了这句话
    assert "hello engine" in result