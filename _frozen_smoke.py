# -*- coding: utf-8 -*-
"""冻结环境冒烟测试：在打包后的 exe 里真跑一遍处理，证明依赖齐全。

用法：smoke.exe [待测PDF路径]
退出码 0 = 全部通过。
"""
import os
import sys
import tempfile
import traceback


def step(name, fn):
    try:
        msg = fn()
        print("  OK  %-22s %s" % (name, msg or ""))
        return True
    except Exception:
        print("  !!  %-22s" % name)
        traceback.print_exc()
        return False


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else None
    print("frozen =", getattr(sys, "frozen", False))
    print("exe    =", sys.executable)
    print("test   =", src)

    ok = True

    def imports():
        import pymupdf, numpy, cv2  # noqa
        from PIL import Image, ImageDraw  # noqa
        import pystray  # noqa
        import tkinter as tk  # noqa
        from PIL import ImageTk  # noqa
        from tkinterdnd2 import TkinterDnD  # noqa
        from pdf_page_remover import core, scaler, rotator, scan_cleaner, logo_remover, gui  # noqa
        return "pymupdf %s / cv2 %s / numpy %s" % (
            pymupdf.__version__, cv2.__version__.split()[0], numpy.__version__)
    ok &= step("模块导入", imports)

    def dnd():
        from tkinterdnd2 import TkinterDnD, DND_FILES
        r = TkinterDnD.Tk()
        r.drop_target_register(DND_FILES)
        r.withdraw()
        r.destroy()
        return "tkdnd 扩展可加载，可注册 DND_FILES"
    ok &= step("拖拽环境", dnd)

    def icons():
        from pdf_page_remover import gui
        d = os.path.dirname(os.path.abspath(gui.__file__))
        ico = os.path.join(d, "icon.ico")
        png = os.path.join(d, "icon.png")
        assert os.path.exists(ico), "缺 icon.ico: " + ico
        assert os.path.exists(png), "缺 icon.png: " + png
        return "ico/png 随包就位"
    ok &= step("图标资源", icons)

    def cv2_ops():
        import numpy, cv2
        img = (numpy.random.rand(120, 160, 3) * 255).astype(numpy.uint8)
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 15, 8)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, kernel)
        th = cv2.dilate(th, kernel)
        n, lab, stats, _ = cv2.connectedComponentsWithStats(th)
        res = cv2.resize(img, (80, 60), interpolation=cv2.INTER_AREA)
        tpl = img[10:30, 10:50]
        tm = cv2.matchTemplate(gray, cv2.cvtColor(tpl, cv2.COLOR_RGB2GRAY), cv2.TM_CCOEFF_NORMED)
        _v, _mx, _l, _p = cv2.minMaxLoc(tm)
        mask = (lab > 0).astype(numpy.uint8)
        outp = cv2.inpaint(img, mask, 3, cv2.INPAINT_TELEA)
        return "连通域 %d 个，inpaint/resize/matchTemplate 全通" % n
    ok &= step("cv2 算子", cv2_ops)

    if not src or not os.path.exists(src):
        print("  --  未提供可读的测试 PDF，跳过实际处理")
        return 1 if not ok else 0

    work = tempfile.mkdtemp(prefix="pdf_smoke_")

    def page_removal():
        from pdf_page_remover import core
        out = os.path.join(work, "nopage")
        stats = core.batch_process([src], out, core.PageNumberConfig())
        s = stats[0]
        f = s["output_file"]
        assert s.get("status") == "success", "status=%s %s" % (s.get("status"), s.get("error"))
        assert os.path.exists(f) and os.path.getsize(f) > 1000, "没产出文件"
        a, b = _counters(src), _counters(f)
        assert a["pages"] == b["pages"], "页数变了 %s -> %s" % (a["pages"], b["pages"])
        assert b["text"] > 0, "文字层没了"
        assert a["images"] == b["images"], "图片(含章)数变了 %s -> %s" % (a["images"], b["images"])
        return "页 %d 图 %d 字 %d；文本页 %s，删掉 %s 处" % (
            b["pages"], b["images"], b["text"], s.get("text_pages"), s.get("removed_count"))
    ok &= step("去页码", page_removal)

    def resizing():
        from pdf_page_remover import scaler
        out = os.path.join(work, "a4.pdf")
        r = scaler.resize_pdf(src, out, target_size="A4")
        assert os.path.exists(out) and os.path.getsize(out) > 1000, r
        b = _counters(out)
        return "A4 后页 %d，尺寸 %s" % (b["pages"], b["size"])
    ok &= step("改纸张尺寸", resizing)

    def scaling():
        from pdf_page_remover import scaler
        out = os.path.join(work, "scale")
        stats = scaler.batch_scale([src], out, scale_ratio=0.95)
        f = stats[0].get("output_file")
        assert stats[0].get("status") == "success" and f and os.path.exists(f), \
            "status=%s %s" % (stats[0].get("status"), stats[0].get("error"))
        return "内容缩放 0.95 产出成功"
    ok &= step("内容缩放", scaling)

    def logo_pass():
        from pdf_page_remover import logo_remover
        out = os.path.join(work, "nologo")
        stats = logo_remover.batch_remove_logos([src], out, logo_remover.LogoConfig())
        s = stats[0]
        assert s.get("status") != "error", "去Logo 出错: %s" % s.get("error")
        f = s.get("output_file")
        if f:
            assert os.path.exists(f), "声明产出但文件不在"
        return "跑通，status=%s，删除 %s 处" % (s.get("status"), s.get("removed_count", 0))
    ok &= step("去页眉Logo", logo_pass)

    def render():
        import pymupdf
        d = pymupdf.open(src)
        pm = d[0].get_pixmap(dpi=100)
        assert pm.width > 300, "渲染异常"
        d.close()
        return "%d x %d 像素渲染正常" % (pm.width, pm.height)
    ok &= step("页面渲染", render)

    print("\n结论：", "全部通过" if ok else "有失败项")
    return 0 if ok else 2


def _counters(path):
    import pymupdf
    d = pymupdf.open(path)
    imgs = sum(len(p.get_images(full=True)) for p in d)
    text = sum(len(p.get_text("words")) for p in d)
    size = "%.0fx%.0f" % (d[0].rect.width, d[0].rect.height)
    out = {"pages": d.page_count, "images": imgs, "text": text, "size": size}
    d.close()
    return out


if __name__ == "__main__":
    sys.exit(main())
