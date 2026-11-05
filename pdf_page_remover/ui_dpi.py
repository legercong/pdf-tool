# -*- coding: utf-8 -*-
"""界面清晰度与屏幕适配：DPI awareness、中文字体族、按缩放换算像素。

三件事必须按这个顺序做：

1. enable_dpi_awareness() —— 只能在第一个 tk.Tk() 之前调一次。晚了 Windows 已经决定
   怎么拉伸这个进程的位图，125%/150% 缩放屏上文字就会发虚；
2. init(root) —— 窗口建出来才读得到真实 DPI，据此算 SCALE；
3. F() / S() —— 界面代码从这里取字号和像素，字号一律落到整数物理像素。

根因记录：界面原先全线写 ("Segoe UI", 10)。Segoe UI 不含中文字形，Windows 会做
font linking 回落到宋体去画汉字；宋体是印刷字体，13px 时笔画极细且行高跟拉丁部分不一致，
在深色背景上被 ClearType 画出彩色边——用户说的"糊"就是这个。
"""
import ctypes
import os
import tkinter.font as tkfont

# 中文优先的 UI 字体链：雅黑UI 是 Win10/11 桌面默认 UI 字体，等线次之，宋体兜底
_FAMILY_CANDIDATES = ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
                      "DengXian", "等线", "SimSun", "宋体")
_MONO_FAMILY = "Consolas"

# 原界面里的 point 值 → 96dpi 下的物理像素高度（pt*96/72 取整）
# 用像素而不是 point：Tk 按 point 换算会得到 13.33 这种小数，取整后每档字号的行高不统一
_POINT_TO_PX = {8: 11, 9: 12, 10: 13, 11: 15, 12: 16, 14: 19, 16: 21, 20: 27, 22: 29}
_MIN_PX = 11

_state = {"scale": 1.0, "dpi": 96, "family": None, "awareness": "未声明"}
_awareness_done = [False]


def enable_dpi_awareness() -> str:
    """让进程自己按真实像素绘制，而不是被 Windows 位图拉伸。返回采用的策略。

    必须在创建任何 Tk 窗口之前调用。已经由 manifest 声明过（打包后的 exe）时这些调用
    会失败，属于正常情况，静默接受。
    """
    if _awareness_done[0]:
        return _state["awareness"]
    _awareness_done[0] = True
    user32 = ctypes.windll.user32
    try:  # Win10 1703+：每显示器 v2，多屏不同缩放也不会忽大忽小
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _state["awareness"] = "Per-Monitor-V2"
            return _state["awareness"]
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        _state["awareness"] = "Per-Monitor"
        return _state["awareness"]
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        _state["awareness"] = "System"
    except Exception:
        _state["awareness"] = "声明失败"
    return _state["awareness"]


def _real_dpi(root) -> int:
    """窗口所在显示器的真实 DPI；读不到就退回 Tk 自己看到的。"""
    override = os.environ.get("PDF_UI_SCALE")
    if override:  # 测试钩子：不改用户显示设置也能验证高缩放屏下的排版
        try:
            return max(1, int(round(96 * float(override))))
        except ValueError:
            pass
    user32 = ctypes.windll.user32
    try:
        hwnd = user32.GetAncestor(root.winfo_id(), 0) or root.winfo_id()
        dpi = user32.GetDpiForWindow(hwnd)
        if dpi >= 96:
            return int(dpi)
    except Exception:
        pass
    try:  # Win8.1：按主显示器取
        shcore = ctypes.windll.shcore
        mon = user32.GetPrimaryMonitor()
        got = ctypes.c_uint()
        if shcore.GetDpiForMonitor(mon, 0, ctypes.byref(got), ctypes.byref(got)) == 0 and got.value >= 96:
            return int(got.value)
    except Exception:
        pass
    try:
        return int(round(root.winfo_fpixels("1i") * 0.75))
    except Exception:
        return 96


def init(root):
    """窗口建好后调用：算出 SCALE，并把 Tk 的命名字体一起换掉。"""
    dpi = _real_dpi(root)
    _state["dpi"] = dpi
    _state["scale"] = max(0.75, min(3.0, dpi / 96.0))
    _state["family"] = _pick_family(root)
    _apply_named_fonts(root)
    return _state["scale"]


def _pick_family(root) -> str:
    try:
        installed = {f.lower() for f in tkfont.families(root)}
    except Exception:
        installed = set()
    for name in _FAMILY_CANDIDATES:
        if not installed or name.lower() in installed:
            return name
    return _FAMILY_CANDIDATES[0]


def scale() -> float:
    return _state["scale"]


def family() -> str:
    return _state["family"] or _FAMILY_CANDIDATES[0]


def info() -> str:
    """给日志/自检用的一行说明。"""
    return "DPI=%d 缩放=%.2f 字体=%s awareness=%s" % (
        _state["dpi"], _state["scale"], family(), _state["awareness"])


def S(value):
    """把界面上写死的像素量（间距、画布尺寸）换算成当前缩放下该占的像素。"""
    if isinstance(value, (tuple, list)):
        return type(value)(S(v) for v in value)
    return int(round(value * _state["scale"]))


def F(point_size: int, weight: str = "") -> tuple:
    """字号工厂：沿用原来代码里的 point 数值，实际按整数物理像素下发。"""
    px = _POINT_TO_PX.get(point_size)
    if px is None:
        px = max(_MIN_PX, int(round(point_size * 96 / 72)))
    px = max(_MIN_PX, int(round(px * _state["scale"])))
    spec = (family(), -px)
    return spec + (weight,) if weight else spec


def mono(point_size: int, weight: str = "") -> tuple:
    """等宽字体：只给日志区用，那里全是 ASCII，Consolas 的 ClearType 表现没问题。"""
    px = max(_MIN_PX, int(round(_POINT_TO_PX.get(point_size, point_size) * _state["scale"])))
    spec = (_MONO_FAMILY, -px)
    return spec + (weight,) if weight else spec


def work_area():
    """任务栏以外的可用屏幕像素；窗口尺寸要按它封顶。"""
    import ctypes.wintypes as wt
    override = os.environ.get("PDF_UI_WORK")
    if override:  # 测试钩子：本机是 2K，用它模拟 1080p 笔记本的可用区域
        try:
            w, h = (int(v) for v in override.lower().split("x"))
            return w, h
        except ValueError:
            pass
    try:
        r = wt.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):  # SPI_GETWORKAREA
            return r.right - r.left, r.bottom - r.top
    except Exception:
        pass
    return None, None


def _apply_named_fonts(root):
    """把 Tk 的命名字体统一改掉：菜单、消息框、ttk 默认控件、以后新增的控件都跟着走。"""
    sizes = {
        "TkDefaultFont": F(10), "TkTextFont": F(10), "TkMenuFont": F(10),
        "TkHeadingFont": F(10, "bold"), "TkCaptionFont": F(10, "bold"),
        "TkSmallCaptionFont": F(9), "TkIconFont": F(9), "TkTooltipFont": F(9),
        "TkFixedFont": mono(10),
    }
    for name, spec in sizes.items():
        try:
            font = tkfont.nametofont(name)
            font.configure(family=spec[0], size=spec[1])
            font.configure(weight=spec[2] if len(spec) > 2 else "normal")
        except Exception:
            pass
