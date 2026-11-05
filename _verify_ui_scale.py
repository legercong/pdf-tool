# -*- coding: utf-8 -*-
"""界面缩放/适配回归：四种屏幕组合下窗口、预览、字号都该落在什么范围。

本机是 2560x1440 @100%，改不了他的显示设置，所以用 ui_dpi 的两个测试钩子
（PDF_UI_SCALE 模拟系统缩放、PDF_UI_WORK 模拟可用区域）把 1080p 和 2K 各档都过一遍。

用法：python _verify_ui_scale.py
"""
import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pdf_page_remover import ui_dpi  # noqa: E402
from pdf_page_remover import gui  # noqa: E402

# (缩放, 可用区域, 说明)
COMBOS = [
    (1.00, "1920x1040", "1080p 桌面，任务栏占 40"),
    (1.25, "1920x1040", "1080p 笔记本 125%"),
    (1.50, "1920x1040", "1080p 笔记本 150%（逻辑只有 1280x720）"),
    (1.00, "2560x1400", "2K 桌面 100%（本机）"),
    (1.50, "2560x1400", "2K 笔记本 150%"),
]

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("  ✗", msg)
    return cond


root = tk.Tk()
root.withdraw()
for scale, work, label in COMBOS:
    os.environ["PDF_UI_SCALE"] = str(scale)
    os.environ["PDF_UI_WORK"] = work
    got = ui_dpi.init(root)
    check(abs(got - scale) < 0.01, "%s: 缩放算成 %.2f，应为 %.2f" % (label, got, scale))

    ww, wh = (int(v) for v in work.split("x"))
    w, h = gui._window_size(920, 1050)
    mw, mh = gui._window_size(780, 850, floor_w=560, floor_h=460)
    pw, ph = gui._preview_size(False)
    pw_l, ph_l = gui._preview_size(True)
    body = ui_dpi.F(10)
    small = ui_dpi.F(9)
    print("%-34s 窗口 %dx%d 最小 %dx%d 预览 纵%dx%d 横%dx%d 正文 %s 次要 %s"
          % (label, w, h, mw, mh, pw, ph, pw_l, ph_l, body, small))

    check(h <= wh - ui_dpi.S(20), "%s: 窗口高 %d 超出可用高 %d，会被任务栏切掉" % (label, h, wh))
    check(w <= ww - ui_dpi.S(20), "%s: 窗口宽 %d 超出可用宽 %d" % (label, w, ww))
    check(mh <= h and mw <= w, "%s: minsize %dx%d 比窗口 %dx%d 还大" % (label, mw, mh, w, h))
    # 预览卡是整行卡片里的画布，只要塞得进窗口宽度就行（不是左右分栏）
    check(pw <= w - ui_dpi.S(80), "%s: 纵向预览 %d 宽，窗口 %d 放不下" % (label, pw, w))
    check(pw_l <= w - ui_dpi.S(80), "%s: 横向预览 %d 宽，窗口 %d 放不下" % (label, pw_l, w))
    check(isinstance(body[1], int) and body[1] < 0, "%s: 字号必须是负整数字（像素），现在是 %r" % (label, body))
    check(abs(body[1]) == max(ui_dpi._MIN_PX, int(round(ui_dpi._POINT_TO_PX[10] * scale))),
          "%s: 正文像素高算错 %r" % (label, body))
    check(abs(small[1]) < abs(body[1]), "%s: 次要文字不该比正文大" % label)
    check(body[0] == ui_dpi.family(), "%s: 字体族没落到 %s" % (label, ui_dpi.family()))

os.environ.pop("PDF_UI_SCALE", None)
os.environ.pop("PDF_UI_WORK", None)
root.destroy()

print("字体族:", ui_dpi.family(), "| awareness:", ui_dpi.info())
print("结论:", "全部通过" if not fails else "%d 条不通过" % len(fails))
sys.exit(1 if fails else 0)
