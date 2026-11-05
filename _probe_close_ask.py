# -*- coding: utf-8 -*-
r"""关闭按钮行为取证：第一次点 X 要问清楚"退出还是收进托盘"，勾了不再提示以后就照着办。

A 第一次点 X 弹询问框，选"收进托盘 + 不再提示"，窗口收起、设置落盘
B 重启后不再问，点 X 直接收进托盘
C 记住的是"退出"时，点 X 真的把进程关掉
D 点"取消"（Esc）什么都没发生，下次照旧要问
E 托盘起不来（缺 pystray）时不问，直接退出

每个场景单独开一个子进程，共用同一个临时 APPDATA，这样"重启"是真的重启，
也不会碰他正在用的 %APPDATA%\PDF Tool\settings.json。
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_regr", "ui")
TITLE = "关闭 PDF Tool"


# ---------- 子进程：单个场景 ----------
def run_child(appdata, scenario):
    os.environ["APPDATA"] = appdata
    sys.path.insert(0, HERE)
    import tkinter as tk
    from tkinter import ttk
    from tkinterdnd2 import TkinterDnD
    from PIL import ImageGrab
    from pdf_page_remover import gui as app_mod
    from pdf_page_remover import ui_dpi

    ui_dpi.enable_dpi_awareness()
    fails = []

    def check(name, cond, detail=""):
        print("%s %s %s" % ("  OK " if cond else "  FAIL", name, detail), flush=True)
        if not cond:
            fails.append(name)

    def settings_path():
        return os.path.join(appdata, "PDF Tool", "settings.json")

    def settings():
        if not os.path.exists(settings_path()):
            return {}
        with open(settings_path(), "r", encoding="utf-8") as f:
            return json.load(f)

    def dialogs(root):
        try:
            kids = root.winfo_children()
        except tk.TclError:      # 选"退出"之后 root 已经销毁，视作"没有询问框"
            return []
        return [w for w in kids
                if isinstance(w, tk.Toplevel) and alive(w)
                and w.title() == TITLE]

    def walk(win, kinds):
        hits, stack = [], [win]
        while stack:
            for c in stack.pop().winfo_children():
                if isinstance(c, kinds):
                    hits.append(c)
                stack.append(c)
        return hits

    def pick(win, kind, text):
        for w in walk(win, kind):
            try:
                if text in w.cget("text"):
                    return w
            except tk.TclError:
                continue
        return None

    def pump(root, ms=120):
        for _ in range(ms // 20):
            try:
                root.update()
            except tk.TclError:
                return

    def alive(widget):
        try:
            return widget.winfo_exists()
        except tk.TclError:
            return False

    def new_app():
        root = TkinterDnD.Tk()
        root.withdraw()
        app = app_mod.PDFPageNumberRemoverGUI(root)
        root.deiconify()
        pump(root)
        return root, app

    def open_win(root, app, act=None):
        """先排好"操作询问框"的队再触发 X 号回调：它会开一个嵌套事件循环，队列照跑"""
        box = {"dialog": None}

        def grab(tag, win):
            os.makedirs(OUT, exist_ok=True)
            try:
                win.update()      # 点完选项立刻截图，不重画就拍到的是上一帧
            except tk.TclError:
                return
            x, y = win.winfo_rootx(), win.winfo_rooty()
            w, h = win.winfo_width(), win.winfo_height()
            path = os.path.join(OUT, "close_dialog_%s.png" % tag)
            ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(path)
            print("  截图:", path, (w, h), flush=True)

        def interact():
            got = dialogs(root)
            if not got:
                return
            box["dialog"] = got[0]
            grab("default", got[0])
            if act:
                act(got[0], grab)

        root.after(300, interact)
        app._on_window_close()
        pump(root, 120)
        return box["dialog"]

    def close_safely(root):
        try:
            if alive(root):
                root.destroy()
        except tk.TclError:
            pass

    if scenario == "A":
        root, app = new_app()
        check("默认每次都问", app.close_ask_var.get() is True)

        def act(win, grab):
            pick(win, tk.Radiobutton, "收进托盘").invoke()
            pick(win, tk.Checkbutton, "不再提示").invoke()
            grab("A", win)
            pick(win, ttk.Button, "确定").invoke()

        dialog = open_win(root, app, act)
        check("第一次点 X 弹出询问框", dialog is not None)
        check("进程还在", alive(root))
        check("窗口收起了（看不见）", not root.winfo_viewable())
        data = settings()
        check("落盘：不再问", data.get("close_ask_var") is False, str(data.get("close_ask_var")))
        check("落盘：动作=托盘", data.get("close_action_var") == "tray",
              str(data.get("close_action_var")))
        close_safely(root)

    elif scenario == "B":
        root, app = new_app()
        check("重启后读到不再问", app.close_ask_var.get() is False)
        check("重启后读到动作=托盘", app.close_action_var.get() == "tray")
        app._on_window_close()
        pump(root, 60)
        check("这次没弹窗", not dialogs(root))
        check("窗口收起、进程还在", alive(root) and not root.winfo_viewable())
        close_safely(root)

    elif scenario == "C":
        data = settings()
        data["close_action_var"] = "quit"
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        root, app = new_app()
        check("重启后读到动作=退出", app.close_action_var.get() == "quit")
        app._on_window_close()
        pump(root, 60)
        check("没弹窗", not dialogs(root))
        check("窗口真被销毁（进程收尾）", not alive(root))

    elif scenario == "D":
        root, app = new_app()

        def act(win, grab):
            win.event_generate("<Escape>")

        dialog = open_win(root, app, act)
        check("询问框弹出来了", dialog is not None)
        check("取消后窗口还在原位", alive(root) and root.winfo_viewable())
        check("取消后仍然保持每次都问", app.close_ask_var.get() is True)
        check("取消后没有偷偷落盘", settings().get("close_ask_var", True) is True,
              str(settings().get("close_ask_var")))
        close_safely(root)

    elif scenario == "E":
        root, app = new_app()
        app._tray_icon = None
        app._on_window_close()
        pump(root, 60)
        check("没有托盘时不问", not dialogs(root))
        check("没有托盘时直接退出", not alive(root))

    else:
        raise SystemExit("unknown scenario " + scenario)

    if fails:
        print("失败项:", ", ".join(fails), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--only" in argv:
        run_child(argv[argv.index("--appdata") + 1], argv[argv.index("--only") + 1])
        sys.exit(0)

    base = tempfile.mkdtemp(prefix="close_ask_")
    shared = os.path.join(base, "shared", "roaming")   # A→B→C 连着"重启"同一份设置
    fresh = os.path.join(base, "fresh", "roaming")     # D/E 用干净设置
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    ok = True
    for scen, appdata in (("A", shared), ("B", shared), ("C", shared),
                          ("D", fresh), ("E", fresh)):
        os.makedirs(os.path.join(appdata, "PDF Tool"), exist_ok=True)
        print("=== 场景 %s ===" % scen, flush=True)
        r = subprocess.run([sys.executable, os.path.abspath(__file__),
                            "--only", scen, "--appdata", appdata],
                           env=env, cwd=HERE, timeout=120)
        ok = ok and r.returncode == 0
    print("结论:", "全部通过" if ok else "有失败项", flush=True)
    sys.exit(0 if ok else 1)
