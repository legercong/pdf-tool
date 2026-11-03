#!/usr/bin/env pythonw
# -*- coding: utf-8 -*-
"""
PDF Tool - 启动入口
使用 pythonw.exe 运行可隐藏控制台窗口
"""

import sys
import os

# 隐藏Windows控制台窗口
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pdf_page_remover.gui import main

if __name__ == "__main__":
    main()