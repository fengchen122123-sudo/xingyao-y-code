# -*- coding: utf-8 -*-
"""IDE 模式的 Python 侧支撑模块。

与 core/lsp.py 的区别：core/lsp.py 是 find_definition 工具用的符号查找实现，
本包服务于 GUI 编辑器的文件读写与语言服务，两者互不依赖。
"""

__all__ = ["file_service", "layout_store"]
