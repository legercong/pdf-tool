# -*- coding: utf-8 -*-
"""验证冻结适配改动：拦截 PowerShell，不真的写快捷方式。"""
import os
import sys
import subprocess
import tempfile

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from pdf_page_remover import gui

ROOT = os.path.dirname(os.path.abspath(__file__))
print("1) _app_home() 源码运行 =", gui._app_home())
assert os.path.normcase(gui._app_home()) == os.path.normcase(ROOT), "源码运行时 _app_home 应是项目根目录"

calls = []
real_run = subprocess.run


def fake_run(cmd, *a, **k):
    calls.append(cmd[-1])
    return subprocess.CompletedProcess(cmd, 0, b"", b"")


class StubRoot:
    def after(self, *_):
        pass


subprocess.run = fake_run
try:
    print("\n2) 源码分支（非冻结）:")
    gui._create_start_menu_shortcut()
    for c in calls:
        print("   ", c[:200])
    assert calls and "main.py" in calls[0], "非冻结应指向 main.py"
    assert "pythonw" in calls[0], "非冻结应用 pythonw 起，避免黑窗口"

    calls.clear()
    fake_home = os.path.join(tempfile.gettempdir(), "fake_pdf_tool")
    fake_exe = os.path.join(fake_home, "PDF Tool.exe")
    print("\n3) 冻结分支（假装 exe 在 %s）:" % fake_home)
    real_exec = sys.executable
    sys.frozen = True
    sys.executable = fake_exe
    try:
        gui._create_start_menu_shortcut()
        for c in calls:
            print("   ", c[:200])
        assert len(calls) == 2, "开始菜单 + 桌面 各一个"
        assert "PDF Tool.exe" in calls[0], "冻结后应直指 exe"
        assert "main.py" not in calls[0], "冻结后不该再引用 main.py"
        assert "Arguments" not in calls[0], "冻结后不该带脚本参数"

        calls.clear()
        print("\n4) 冻结时 backup 目录:")
        app = gui.PDFPageNumberRemoverGUI.__new__(gui.PDFPageNumberRemoverGUI)
        app.root = StubRoot()
        bd = app._backup_dir()
        print("    backup ->", bd)
        assert bd and os.path.normcase(bd) == os.path.normcase(os.path.join(fake_home, "backup")), \
            "backup 应在 exe 同级，不能落进 _internal"
    finally:
        sys.executable = real_exec
        del sys.frozen
finally:
    subprocess.run = real_run

print("\n全部通过（拦截式，未改动桌面上现有快捷方式）")
