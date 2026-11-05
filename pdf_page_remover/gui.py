"""
PDF Tool - 图形界面
基于tkinter实现，支持批量处理
UI风格：现代深色主题
支持多功能组合：去除页码、内容缩放、自动旋转、尺寸调整、扫描件去瑕疵、去除页眉Logo
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
import os
import stat
import sys
import json
import tempfile
import shutil
import logging
from typing import Dict, List, Optional, Set

# 拖拽支持（可选，不可用时降级为普通Tk）
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False

import pymupdf
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdf_page_remover.core import PageNumberConfig, PageNumberRemover, batch_process
from pdf_page_remover.scaler import PDFScaler, batch_scale
from pdf_page_remover.rotator import PDFRotator, batch_rotate, parse_page_range
from pdf_page_remover.scaler import PAGE_SIZES, resize_pdf, batch_resize
from pdf_page_remover.scaler import (batch_fit_margins, DEFAULT_SMALL_CONTENT_RATIO,
                                     FitSpec)
from pdf_page_remover.scan_cleaner import batch_clean_scan
from pdf_page_remover.logo_remover import LogoConfig, batch_remove_logos
from pdf_page_remover import ui_dpi
# 界面里所有字号和像素间距都过 F()/S()/mono()，见 ui_dpi 的说明
from pdf_page_remover.ui_dpi import F, S, mono


# ============ 输出目录 ============
# 所有功能都输出到同一个目录，不再按功能给目录起不同的名字
DEFAULT_OUTPUT_DIR_NAME = "PDF输出"
# 旧版按功能分的目录名，识别出来好一并换成统一名
LEGACY_OUTPUT_DIR_NAMES = ("PDF无页码输出", "PDF转Word输出", "PDF缩放输出",
                           "PDF旋转输出", "PDF尺寸调整输出", "PDF边距缩放输出",
                           "PDF组合处理输出")


# ============ 界面选项持久化 ============
SETTINGS_DIR_NAME = "PDF Tool"
SETTINGS_FILE_NAME = "settings.json"


def _app_home() -> str:
    """应用自己的目录：打包后是可执行文件所在处，源码运行时是项目根目录"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _settings_path() -> str:
    """设置文件位置：%APPDATA%\\PDF Tool\\settings.json，没有 APPDATA 就放主目录"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, SETTINGS_DIR_NAME, SETTINGS_FILE_NAME)


def read_saved_settings() -> dict:
    """读取上次的界面选项；文件缺失或损坏时返回空字典（用默认值启动）"""
    path = _settings_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.warning(f"读取上次的设置失败，本次使用默认值: {e}")
        return {}


def write_saved_settings(data: dict) -> bool:
    """写界面选项；先写临时文件再替换，避免写一半被杀掉留下坏文件"""
    try:
        path = _settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
        return True
    except Exception as e:
        logging.debug(f"保存界面设置失败: {e}")
        return False


# ============ 主题配色 ============
class Theme:
    BG = "#1e1e2e"           # 主背景色
    BG_CARD = "#2d2d44"      # 卡片背景色
    BG_INPUT = "#252538"     # 输入框背景
    FG = "#cdd6f4"           # 主前景文字色
    FG_DIM = "#6c7086"       # 次要文字色
    ACCENT = "#89b4fa"       # 强调色（蓝）
    ACCENT_HOVER = "#b4befe" # 强调色hover
    SUCCESS = "#a6e3a1"      # 成功绿
    WARNING = "#f9e2af"      # 警告黄
    ERROR = "#f38ba8"        # 错误红
    BORDER = "#45475a"       # 边框色
    TIP_BG = "#313244"       # 悬停气泡底色：比卡片亮一档，才看得出是浮在上面的一层


class ToolTip:
    """鼠标停留一会儿后弹出的说明气泡。

    Tkinter 没有原生 tooltip。功能按钮的名字简化成三五个字之后，详细说明只能靠悬停给出。
    延时 400ms 才弹：顺手划过一排按钮时不该被一堆窗口糊脸。
    """

    DELAY_MS = 400

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self._job = None
        self._win = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._dismiss, add="+")
        widget.bind("<Button-1>", self._dismiss, add="+")

    def _schedule(self, _event=None):
        self._dismiss()
        try:
            self._job = self.widget.after(self.DELAY_MS, self._show)
        except tk.TclError:
            self._job = None

    def _dismiss(self, _event=None):
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        if self._win is not None:
            try:
                self._win.destroy()
            except tk.TclError:
                pass
            self._win = None

    def _show(self):
        self._job = None
        try:
            top = self.widget.winfo_toplevel()
            win = tk.Toplevel(top)
            win.wm_overrideredirect(True)
            win.wm_attributes("-topmost", True)
            tk.Label(win, text=self.text, justify=tk.LEFT, bg=Theme.TIP_BG, fg=Theme.FG,
                     font=F(9), relief="solid", bd=1,
                     highlightbackground=Theme.BORDER, highlightthickness=1,
                     padx=S(10), pady=S(7)).pack()
            win.update_idletasks()
            x = self.widget.winfo_pointerx() + 14
            y = self.widget.winfo_pointery() + 20
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            if x + w > top.winfo_screenwidth() - 8:
                x = max(0, top.winfo_screenwidth() - w - 8)
            if y + h > top.winfo_screenheight() - 8:
                y = self.widget.winfo_pointery() - h - 10
            win.wm_geometry(f"+{x}+{y}")
            self._win = win
        except tk.TclError:
            self._win = None


def _window_size(base_w: int, base_h: int, floor_w: int = 640, floor_h: int = 520):
    """按当前缩放换算窗口尺寸，并封顶在任务栏以外的可用区域里。

    基准 920x1050 是在 2K 屏上定的，1080p 的可用高度只有 1040——不封顶的话窗口底部
    被任务栏切掉一截，正好切在日志区。缩放后同理，超出的部分靠内容区滚动消化。
    """
    w, h = ui_dpi.S(base_w), ui_dpi.S(base_h)
    aw, ah = ui_dpi.work_area()
    if aw:
        w = min(w, aw - ui_dpi.S(24))
        h = min(h, ah - ui_dpi.S(24))
    return max(ui_dpi.S(floor_w), w), max(ui_dpi.S(floor_h), h)


def _preview_size(landscape: bool):
    """预览画布尺寸：按缩放放大，但不超过可用屏幕的 34% 宽 / 60% 高。

    150% 缩放的笔记本上，预览再按 1.5 倍长就会把左边的设置卡片挤出窗口——宁可预览小一点。
    """
    base_w, base_h = (560, 400) if landscape else (400, 560)
    w, h = ui_dpi.S(base_w), ui_dpi.S(base_h)
    aw, ah = ui_dpi.work_area()
    if aw:
        w = min(w, int(aw * 0.34))
        h = min(h, int(ah * 0.60))
    return max(ui_dpi.S(240), w), max(ui_dpi.S(240), h)


def _setup_styles(root: tk.Tk):
    """设置ttk自定义样式"""
    style = ttk.Style(root)
    style.theme_use("clam")

    # 基础框架
    style.configure("TFrame", background=Theme.BG)
    style.configure("Card.TFrame", background=Theme.BG_CARD, relief="flat")

    # 滚动条：clam 主题下可以整条刷成深色，不再是一根亮白的 Windows 原生条
    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar",
                        background=Theme.BORDER, troughcolor=Theme.BG,
                        bordercolor=Theme.BG, arrowcolor=Theme.FG_DIM,
                        lightcolor=Theme.BG, darkcolor=Theme.BG,
                        relief="flat", width=12)
        style.map(f"{orient}.TScrollbar",
                  background=[("active", Theme.ACCENT), ("pressed", Theme.ACCENT_HOVER)])

    # 下拉框：不配就是 clam 自带的浅色，跟深色面板对不上；弹出的列表也要一起刷
    style.configure("TCombobox", fieldbackground=Theme.BG_INPUT, background=Theme.BORDER,
                    foreground=Theme.FG, arrowcolor=Theme.FG, bordercolor=Theme.BORDER,
                    lightcolor=Theme.BG_INPUT, darkcolor=Theme.BG_INPUT, arrowsize=14)
    style.map("TCombobox",
              fieldbackground=[("readonly", Theme.BG_INPUT)],
              foreground=[("readonly", Theme.FG)],
              background=[("readonly", Theme.BORDER), ("active", Theme.ACCENT)],
              arrowcolor=[("disabled", Theme.FG_DIM)])
    for opt, color in (("*TCombobox*Listbox*Background", Theme.BG_INPUT),
                       ("*TCombobox*Listbox*Foreground", Theme.FG),
                       ("*TCombobox*Listbox*selectBackground", Theme.ACCENT),
                       ("*TCombobox*Listbox*selectForeground", "#11111b")):
        root.option_add(opt, color)

    # 标签
    style.configure("TLabel", background=Theme.BG, foreground=Theme.FG, font=F(10))
    style.configure("CardLabel.TLabel", background=Theme.BG_CARD, foreground=Theme.FG, font=F(10))
    style.configure("Title.TLabel", background=Theme.BG, foreground=Theme.ACCENT, font=F(16, "bold"))
    style.configure("Subtitle.TLabel", background=Theme.BG, foreground=Theme.FG_DIM, font=F(10))

    # 按钮 - 主按钮
    style.configure("Accent.TButton",
                    background=Theme.ACCENT,
                    foreground="white",
                    font=F(10, "bold"),
                    padding=(16, 6))
    style.map("Accent.TButton",
              background=[("active", Theme.ACCENT_HOVER), ("disabled", Theme.BORDER)],
              foreground=[("disabled", Theme.FG_DIM)])

    # 按钮 - 次按钮
    style.configure("Secondary.TButton",
                    background=Theme.BG_CARD,
                    foreground=Theme.FG,
                    font=F(10),
                    padding=(12, 5))
    style.map("Secondary.TButton",
              background=[("active", Theme.BORDER)])

    # 危险按钮
    style.configure("Danger.TButton",
                    background=Theme.ERROR,
                    foreground="white",
                    font=F(10, "bold"),
                    padding=(12, 5))
    style.map("Danger.TButton",
              background=[("active", "#ff6b8a")])

    # 单选框
    style.configure("TRadiobutton", background=Theme.BG, foreground=Theme.FG, font=F(10))

    # 检查框
    style.configure("TCheckbutton", background=Theme.BG, foreground=Theme.FG, font=F(10))

    # 进度条
    style.configure("TProgressbar",
                    background=Theme.ACCENT,
                    troughcolor=Theme.BG_CARD,
                    thickness=16)

    # Scale
    style.configure("TScale", background=Theme.BG, troughcolor=Theme.BG_CARD)

    # Spinbox
    style.configure("TSpinbox", fieldbackground=Theme.BG_INPUT, foreground=Theme.FG,
                    selectbackground=Theme.ACCENT)

    # 滚动条
    style.configure("Custom.Vertical.TScrollbar",
                    background=Theme.BG_CARD,
                    troughcolor=Theme.BG,
                    arrowcolor=Theme.FG)


class PDFPageNumberRemoverGUI:
    """PDF页码去除与转换工具GUI"""

    # 关闭应用后要记住的界面选项：属性名就是存储键，加新控件时把变量名填进来即可
    # 页面范围、跳过页码这类"每次任务都不一样"的输入不记住，
    # 免得上一批文件的范围悄悄作用于下一批
    PERSISTED_VARS = (
        "func_remove_pages_var", "func_scale_var",
        "func_rotate_var", "func_resize_var", "func_clean_var",
        "func_logo_var", "logo_anchor_var", "logo_zone_w_var", "logo_zone_h_var",
        "logo_presence_var", "logo_whole_row_var",
        "replace_source_var", "mode_var", "settings_expanded",
        "bottom_ratio_var", "top_ratio_var", "dpi_var",
        "check_side_var", "check_seal_var",
        "close_ask_var", "close_action_var",
        "scale_mode_var", "scale_ratio_var", "offset_x_var", "offset_y_var",
        "fit_top_var", "fit_bottom_var", "fit_mode_var", "fit_side_var",
        "fit_side_cm_var", "fit_pre_var", "fit_auto_skip_var", "fit_small_var",
        "uniform_axis_var", "stretch_axis_var", "stretch_x_var", "stretch_y_var",
        "rotate_direction_var", "rotate_auto_var", "resize_size_var",
        "output_var",
    )

    def __init__(self, root: tk.Tk):
        self.root = root
        # 先量屏幕再摆控件：字号和间距都按这里的缩放换算（_gui_shot 之类的入口也走这条）
        ui_dpi.init(root)
        self.root.configure(bg=Theme.BG)
        self.root.title("PDF Tool - 去页码 · 改比例 · 改尺寸 · 旋转 · 去瑕疵 · 去页眉Logo")
        self.root.geometry("%dx%d" % _window_size(920, 1050))
        self.root.minsize(*_window_size(780, 850, floor_w=560, floor_h=460))

        # 设置落盘的防抖任务
        self._settings_save_job = None

        # "最初的原文件"快照：覆盖过源文件后，再次生成都从这份原始内容出发
        self._originals_dir = None
        self._pristine_snapshots = {}
        self._snapshot_originals = {}
        self._snapshot_seq = 0
        # 本次实际写回了几个源文件，结果弹窗要按这个说实话
        self._replace_summary = None
        self._sweep_stale_snapshots()

        # 设置窗口图标
        self._set_window_icon()

        # 处理状态
        self.is_processing = False
        self.process_thread = None
        self._quit_waiting = False
        self.input_files: List[str] = []

        # 系统托盘
        self._tray_icon = None
        self._setup_tray_icon()

        # 窗口关闭：问一句"退出还是收进托盘"（见 _on_window_close）
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        _setup_styles(root)
        self._create_widgets()
        self._layout_widgets()
        self._restore_settings()
        self._watch_settings_changes()
        self._check_dependencies()

    # ========== 创建控件 ==========
    def _create_widgets(self):
        # --- 顶部标题栏 ---
        self.header = tk.Frame(self.root, bg=Theme.BG, height=S(56))
        self.header_label = tk.Label(self.header, text="PDF Tool", bg=Theme.BG,
                                     fg=Theme.ACCENT, font=F(20, "bold"))
        self.header_sub = tk.Label(self.header,
            text="去页码 · 改比例 · 改尺寸 · 旋转 · 去扫描瑕疵 · 去页眉Logo　|　批量处理，电子签章原样保留",
            bg=Theme.BG, fg=Theme.FG_DIM, font=F(10))

        # --- 可滚动内容区域 ---
        self.scroll_canvas = tk.Canvas(self.root, bg=Theme.BG, highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(self.root, orient=tk.VERTICAL, command=self.scroll_canvas.yview)
        self.scroll_frame = tk.Frame(self.scroll_canvas, bg=Theme.BG)
        self.scroll_frame.bind("<Configure>",
            lambda e: self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all")))
        self._scroll_window_id = self.scroll_canvas.create_window((0, 0), window=self.scroll_frame, anchor=tk.NW)
        self.scroll_canvas.configure(yscrollcommand=self.scrollbar.set)
        # Canvas宽度变化时，同步内部frame宽度
        self.scroll_canvas.bind("<Configure>", self._on_scroll_canvas_configure)
        # 鼠标滚轮绑定
        self.scroll_canvas.bind("<Enter>", self._bind_mousewheel)
        self.scroll_canvas.bind("<Leave>", self._unbind_mousewheel)

        # --- 文件选择卡片 ---
        self.card_files = tk.Frame(self.scroll_frame, bg=Theme.BG_CARD, highlightthickness=1,
                                   highlightbackground=Theme.BORDER)
        self.card_files_title = tk.Label(self.card_files, text="文件选择", bg=Theme.BG_CARD,
                                          fg=Theme.ACCENT, font=F(12, "bold"))

        self.file_listbox = tk.Listbox(self.card_files, height=5, selectmode=tk.EXTENDED,
                                        bg=Theme.BG_INPUT, fg=Theme.FG,
                                        highlightthickness=1, highlightcolor=Theme.ACCENT,
                                        selectbackground=Theme.ACCENT, selectforeground="white",
                                        borderwidth=0, font=F(10),
                                        relief="flat")
        # 拖拽支持
        if HAS_DND:
            self.file_listbox.drop_target_register(DND_FILES)
            self.file_listbox.dnd_bind('<<Drop>>', self._on_drop_files)
        self.file_scrollbar = ttk.Scrollbar(self.card_files, orient=tk.VERTICAL,
                                             command=self.file_listbox.yview)
        self.file_listbox.configure(yscrollcommand=self.file_scrollbar.set)

        self.btn_add_files = tk.Button(self.card_files, text="+ 添加文件", bg=Theme.ACCENT,
                                        fg="white", font=F(10, "bold"),
                                        activebackground=Theme.ACCENT_HOVER,
                                        activeforeground="white",
                                        relief="flat", cursor="hand2", padx=S(16), pady=S(5),
                                        command=self._add_files)
        self.btn_add_folder = tk.Button(self.card_files, text="+ 添加文件夹", bg=Theme.BG_CARD,
                                         fg=Theme.FG, font=F(10),
                                         activebackground=Theme.BORDER,
                                         activeforeground=Theme.FG,
                                         relief="flat", cursor="hand2", padx=S(12), pady=S(5),
                                         command=self._add_folder)
        self.btn_remove = tk.Button(self.card_files, text="移除选中", bg=Theme.BG_CARD,
                                     fg=Theme.WARNING, font=F(10),
                                     activebackground=Theme.BORDER,
                                     activeforeground=Theme.WARNING,
                                     relief="flat", cursor="hand2", padx=S(12), pady=S(5),
                                     command=self._remove_selected)
        self.btn_clear = tk.Button(self.card_files, text="清空", bg=Theme.BG_CARD,
                                    fg=Theme.ERROR, font=F(10),
                                    activebackground=Theme.BORDER,
                                    activeforeground=Theme.ERROR,
                                    relief="flat", cursor="hand2", padx=S(12), pady=S(5),
                                    command=self._clear_all)
        self.file_count_label = tk.Label(self.card_files, text="0 个文件", bg=Theme.BG_CARD,
                                          fg=Theme.FG_DIM, font=F(9))

        # --- 输出设置卡片 ---
        self.card_output = tk.Frame(self.scroll_frame, bg=Theme.BG_CARD, highlightthickness=1,
                                    highlightbackground=Theme.BORDER)
        self.card_output_title = tk.Label(self.card_output, text="输出设置", bg=Theme.BG_CARD,
                                           fg=Theme.ACCENT, font=F(12, "bold"))
        self.output_var = tk.StringVar(value=self._default_output_dir())
        self.output_entry = tk.Entry(self.card_output, bg=Theme.BG_INPUT, fg=Theme.FG,
                                      insertbackground=Theme.FG, font=F(10),
                                      relief="flat", highlightthickness=1,
                                      highlightcolor=Theme.ACCENT,
                                      highlightbackground=Theme.BORDER)
        self.btn_browse = tk.Button(self.card_output, text="浏览...", bg=Theme.BG_CARD,
                                     fg=Theme.FG, font=F(10),
                                     activebackground=Theme.BORDER,
                                     relief="flat", cursor="hand2", padx=S(14), pady=S(4),
                                     command=self._browse_output)
        self.replace_source_var = tk.BooleanVar(value=True)
        self.cb_replace_source = self._make_check(self.card_output, "替换源文件",
                                                  self.replace_source_var,
                                                  command=self._on_replace_source_changed)
        self._render_output_display()

        # --- 功能模式卡片 ---
        self.card_func_mode = tk.Frame(self.scroll_frame, bg=Theme.BG_CARD, highlightthickness=1,
                                        highlightbackground=Theme.BORDER)
        self.card_func_mode_title = tk.Label(self.card_func_mode, text="功能模式",
                                              bg=Theme.BG_CARD, fg=Theme.ACCENT,
                                              font=F(12, "bold"))
        # --- 功能模式（多选Checkbutton） ---
        self.func_remove_pages_var = tk.BooleanVar(value=False)
        self.func_scale_var = tk.BooleanVar(value=False)
        self.func_rotate_var = tk.BooleanVar(value=False)
        self.func_resize_var = tk.BooleanVar(value=False)
        self.func_clean_var = tk.BooleanVar(value=False)
        self.func_logo_var = tk.BooleanVar(value=False)

        # 功能定义：(按钮短名, BooleanVar, 内部key, 悬停说明)
        # 按钮上只摆三五个字，详细说明放进悬停气泡，一排摆得下也不用换行
        self.func_checks_def = [
            ("去页码", self.func_remove_pages_var, "remove_page_numbers",
             "识别并涂掉页脚的页码（- 3 -、3/12 这类），文本型和扫描件都能处理。\n"
             "红头页、印发说明页原样保留，正文不会被吃掉。"),
            ("改比例", self.func_scale_var, "pdf_scale",
             "缩放页面内容，页面尺寸不变。三种方式：\n"
             "· 对齐缩放——按上下左右的距离把正文摆正，超出就等比缩小\n"
             "· 等比缩放——满足某一方向的边距\n"
             "· 根据边距缩放——单向拉伸到指定边距"),
            ("旋转", self.func_rotate_var, "pdf_rotate",
             "把横着的页面转成竖版。自动识别文字朝向，也可指定顺时针/逆时针。"),
            ("改尺寸", self.func_resize_var, "pdf_resize",
             "统一整本页面尺寸（如 A4），内容按比例适配、居中摆放。"),
            ("去扫描瑕疵", self.func_clean_var, "clean_scan",
             "只涂白扫描页四条边上的黑框、黑线（扫描进光、走纸脏污留下的）。\n"
             "不做整页降噪，也不碰正文；红印章、红头标题不受影响。\n"
             "按边距缩放前先用它，否则纸边黑框会被当成内容边界。"),
            ("去页眉Logo", self.func_logo_var, "remove_logo",
             "批量去掉每页同一角上重复出现的单位标识（图形+公司全称+编号/横线）。\n"
             "靠跨页比对认定哪块墨迹页页相同，只重画标识自己的墨迹，\n"
             "正文、页眉横线、红印章都不会被动到。"),
        ]
        self.func_mode_vars = {key: var for _, var, key, _ in self.func_checks_def}
        self.func_mode_descriptions = {key: desc for _, _, key, desc in self.func_checks_def}
        # 卡片里只留一排短名，长说明交给悬停气泡；这一行只报"选了哪些、按什么顺序跑"
        self.func_mode_labels = {key: text for text, _, key, _ in self.func_checks_def}
        self.func_mode_checks = self._make_choice_group(
            self.card_func_mode,
            [(text, key) for text, _, key, _ in self.func_checks_def],
            self.func_mode_vars,
            command=self._on_func_mode_change,
            multi=True,
            tooltips=self.func_mode_descriptions,
        )

        self.func_mode_desc_label = tk.Label(self.card_func_mode,
                                              text="选择一项或多项功能组合处理",
                                              bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                                              font=F(9))

        # --- 处理模式卡片（页码去除模式，仅去除页码相关功能时显示） ---
        self.card_mode = tk.Frame(self.scroll_frame, bg=Theme.BG_CARD, highlightthickness=1,
                                  highlightbackground=Theme.BORDER)
        self.card_mode_title = tk.Label(self.card_mode, text="页码去除模式", bg=Theme.BG_CARD,
                                         fg=Theme.ACCENT, font=F(12, "bold"))
        self.mode_var = tk.StringVar(value="auto")
        self.mode_desc_var = tk.StringVar()
        modes = [
            ("自动检测", "auto", "自动判断PDF类型，选择最佳处理方式"),
            ("仅文本PDF", "text_only", "只处理文本型PDF的页码"),
            ("仅扫描件PDF", "scanned_only", "只处理扫描件PDF的页码（需要Tesseract OCR）"),
            ("两种都处理", "both", "同时使用两种方式检测页码"),
        ]
        self.mode_radios = self._make_choice_group(
            self.card_mode,
            [(text, value) for text, value, _ in modes],
            self.mode_var,
        )
        self.mode_descriptions = {value: desc for _, value, desc in modes}
        self.mode_desc_label = tk.Label(self.card_mode, textvariable=self.mode_desc_var,
                                         bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                                         font=F(9))
        self.mode_var.trace_add("write", self._update_mode_description)
        self._update_mode_description()

        # --- 高级设置卡片（可展开/折叠） ---
        self.card_settings = tk.Frame(self.scroll_frame, bg=Theme.BG_CARD, highlightthickness=1,
                                       highlightbackground=Theme.BORDER)
        self.settings_expanded = tk.BooleanVar(value=False)
        self.btn_toggle_settings = tk.Button(self.card_settings, text="▶ 高级设置",
                                              bg=Theme.BG_CARD, fg=Theme.ACCENT,
                                              font=F(12, "bold"),
                                              activebackground=Theme.BG_CARD,
                                              activeforeground=Theme.ACCENT_HOVER,
                                              relief="flat", cursor="hand2",
                                              anchor=tk.W, padx=S(4),
                                              command=self._toggle_settings)

        self.settings_content = tk.Frame(self.card_settings, bg=Theme.BG_CARD)
        # 底部比例
        self.lbl_bottom = tk.Label(self.settings_content, text="底部页码区域高度占比",
                                    bg=Theme.BG_CARD, fg=Theme.FG, font=F(10))
        self.bottom_ratio_var = tk.DoubleVar(value=0.12)
        self.scale_bottom = tk.Scale(self.settings_content, from_=0.02, to=0.20,
                                      resolution=0.01, orient=tk.HORIZONTAL,
                                      variable=self.bottom_ratio_var,
                                      bg=Theme.BG_CARD, fg=Theme.FG,
                                      troughcolor=Theme.BG_INPUT, highlightthickness=0,
                                      activebackground=Theme.ACCENT, cursor="sb_h_double_arrow")
        self.lbl_bottom_val = tk.Label(self.settings_content, text="0.12",
                                        bg=Theme.BG_CARD, fg=Theme.ACCENT,
                                        font=F(10, "bold"), width=5)
        # 顶部比例
        self.lbl_top = tk.Label(self.settings_content, text="顶部页码区域高度占比",
                                 bg=Theme.BG_CARD, fg=Theme.FG, font=F(10))
        self.top_ratio_var = tk.DoubleVar(value=0.08)
        self.scale_top = tk.Scale(self.settings_content, from_=0.02, to=0.20,
                                   resolution=0.01, orient=tk.HORIZONTAL,
                                   variable=self.top_ratio_var,
                                   bg=Theme.BG_CARD, fg=Theme.FG,
                                   troughcolor=Theme.BG_INPUT, highlightthickness=0,
                                   activebackground=Theme.ACCENT, cursor="sb_h_double_arrow")
        self.lbl_top_val = tk.Label(self.settings_content, text="0.08",
                                     bg=Theme.BG_CARD, fg=Theme.ACCENT,
                                     font=F(10, "bold"), width=5)
        # DPI
        self.lbl_dpi = tk.Label(self.settings_content, text="扫描件处理DPI",
                                 bg=Theme.BG_CARD, fg=Theme.FG, font=F(10))
        self.dpi_var = tk.IntVar(value=200)
        self.spin_dpi = tk.Spinbox(self.settings_content, from_=100, to=400, increment=50,
                                    textvariable=self.dpi_var, width=8,
                                    bg=Theme.BG_INPUT, fg=Theme.FG,
                                    insertbackground=Theme.FG, font=F(10),
                                    buttonbackground=Theme.BG_CARD, relief="flat",
                                    highlightthickness=1, highlightcolor=Theme.ACCENT,
                                    highlightbackground=Theme.BORDER)
        # 侧边检测
        self.check_side_var = tk.BooleanVar(value=True)
        self.cb_side = self._make_check(self.settings_content, "检测侧边页码",
                                        self.check_side_var)

        # 印章保护
        self.check_seal_var = tk.BooleanVar(value=True)
        self.cb_seal = self._make_check(self.settings_content,
                                        "保护印章（不去印章上的编号/日期）",
                                        self.check_seal_var)

        # 点 X 号时先问一句"退出还是收进托盘"。朋友那台机器攒了一堆小图标：
        # 他以为点 X 就是关掉程序，实际全缩进托盘了，下次打开又叠一个。
        # 勾掉"关闭时先问我"以后就按上次选的那条直接执行，想反悔回这里勾上。
        self.close_ask_var = tk.BooleanVar(value=True)
        self.cb_close_ask = self._make_check(self.settings_content,
                                            "关闭时先问我（退出 / 收进托盘）",
                                            self.close_ask_var)
        # 记住的那一次选择："quit" 或 "tray"
        self.close_action_var = tk.StringVar(value="quit")

        self.bottom_ratio_var.trace_add("write",
            lambda *a: self.lbl_bottom_val.config(text=f"{self.bottom_ratio_var.get():.2f}"))
        self.top_ratio_var.trace_add("write",
            lambda *a: self.lbl_top_val.config(text=f"{self.top_ratio_var.get():.2f}"))

        # --- 缩放控制卡片（PDF修改比例模式时显示） ---
        self.card_scale = tk.Frame(self.scroll_frame, bg=Theme.BG_CARD, highlightthickness=1,
                                    highlightbackground=Theme.BORDER)
        self.card_scale_title = tk.Label(self.card_scale, text="内容缩放设置",
                                          bg=Theme.BG_CARD, fg=Theme.ACCENT,
                                          font=F(12, "bold"))

        # 缩放方式：对齐缩放 / 等比缩放 / 根据边距缩放
        self.scale_mode_frame = tk.Frame(self.card_scale, bg=Theme.BG_CARD)
        self.scale_mode_var = tk.StringVar(value="align")
        (self.rb_scale_align_mode,
         self.rb_scale_uniform_mode,
         self.rb_scale_stretch_mode) = self._make_choice_group(
            self.scale_mode_frame,
            [("对齐缩放", "align"), ("等比缩放", "uniform"), ("根据边距缩放", "stretch")],
            self.scale_mode_var,
            command=self._on_scale_mode_change,
        )
        # 程序化改变量（不只是点击）时说明文字也要跟上
        self.scale_mode_var.trace_add("write", lambda *a: self._update_scale_mode_hint())
        self.scale_mode_hint = tk.Label(self.card_scale,
                                        text=self.SCALE_HINT_ALIGN,
                                        bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                                        font=F(9), justify=tk.LEFT, anchor=tk.W)

        # 缩放比例控制
        self.scale_ratio_var = tk.IntVar(value=100)  # 百分比 10-150
        self.scale_ratio_display_var = tk.StringVar(value="100%")

        # 比例控制行：-按钮 + 滑块 + +按钮 + 百分比显示
        self.scale_control_frame = tk.Frame(self.card_scale, bg=Theme.BG_CARD)

        self.btn_scale_minus = tk.Button(self.scale_control_frame, text=" − ",
                                          bg=Theme.BG_INPUT, fg=Theme.ACCENT,
                                          font=F(14, "bold"),
                                          activebackground=Theme.BORDER,
                                          activeforeground=Theme.ACCENT_HOVER,
                                          relief="flat", cursor="hand2",
                                          width=3, command=self._scale_decrease)

        self.scale_slider = tk.Scale(self.scale_control_frame, from_=10, to=150,
                                      resolution=1, orient=tk.HORIZONTAL,
                                      variable=self.scale_ratio_var,
                                      bg=Theme.BG_CARD, fg=Theme.FG,
                                      troughcolor=Theme.BG_INPUT, highlightthickness=0,
                                      activebackground=Theme.ACCENT,
                                      cursor="sb_h_double_arrow", length=350,
                                      command=self._on_scale_change)

        self.btn_scale_plus = tk.Button(self.scale_control_frame, text=" + ",
                                         bg=Theme.BG_INPUT, fg=Theme.ACCENT,
                                         font=F(14, "bold"),
                                         activebackground=Theme.BORDER,
                                         activeforeground=Theme.ACCENT_HOVER,
                                         relief="flat", cursor="hand2",
                                         width=3, command=self._scale_increase)

        self.scale_ratio_label = tk.Label(self.scale_control_frame, textvariable=self.scale_ratio_display_var,
                                           bg=Theme.BG_CARD, fg=Theme.ACCENT,
                                           font=F(16, "bold"), width=5)

        # 预设比例快捷按钮
        self.scale_presets_frame = tk.Frame(self.card_scale, bg=Theme.BG_CARD)
        self.scale_preset_buttons = []
        presets = [("50%", 50), ("60%", 60), ("70%", 70), ("80%", 80), ("90%", 90), ("100%", 100), ("120%", 120), ("150%", 150)]
        for text, value in presets:
            btn = tk.Button(self.scale_presets_frame, text=text,
                            bg=Theme.BG_INPUT, fg=Theme.FG,
                            font=F(9),
                            activebackground=Theme.BORDER,
                            activeforeground=Theme.ACCENT,
                            relief="flat", cursor="hand2", padx=S(8), pady=S(2),
                            command=lambda v=value: self._set_scale_preset(v))
            self.scale_preset_buttons.append(btn)

        # 内容偏移控制
        self.offset_frame = tk.Frame(self.card_scale, bg=Theme.BG_CARD)
        self.offset_title_label = tk.Label(self.offset_frame, text="内容偏移",
                                            bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                                            font=F(9))
        self.offset_x_var = tk.IntVar(value=0)
        self.offset_y_var = tk.IntVar(value=0)

        # 水平偏移行
        self.offset_x_frame = tk.Frame(self.offset_frame, bg=Theme.BG_CARD)
        self.offset_x_label = tk.Label(self.offset_x_frame, text="水平",
                                         bg=Theme.BG_CARD, fg=Theme.FG,
                                         font=F(9), width=4)
        self.btn_offset_x_minus = tk.Button(self.offset_x_frame, text=" ← ",
                                             bg=Theme.BG_INPUT, fg=Theme.ACCENT,
                                             font=F(9, "bold"),
                                             activebackground=Theme.BORDER,
                                             relief="flat", cursor="hand2",
                                             width=3, command=lambda: self._adjust_offset("x", -10))
        self.offset_x_spinbox = tk.Spinbox(self.offset_x_frame, from_=-500, to=500,
                                            increment=5, textvariable=self.offset_x_var,
                                            width=6, font=F(9),
                                            bg=Theme.BG_INPUT, fg=Theme.FG,
                                            buttonbackground=Theme.BORDER,
                                            command=lambda: self._on_offset_change())
        self.btn_offset_x_plus = tk.Button(self.offset_x_frame, text=" → ",
                                            bg=Theme.BG_INPUT, fg=Theme.ACCENT,
                                            font=F(9, "bold"),
                                            activebackground=Theme.BORDER,
                                            relief="flat", cursor="hand2",
                                            width=3, command=lambda: self._adjust_offset("x", 10))
        self.offset_x_unit = tk.Label(self.offset_x_frame, text="pt",
                                       bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                                       font=F(9))

        # 垂直偏移行
        self.offset_y_frame = tk.Frame(self.offset_frame, bg=Theme.BG_CARD)
        self.offset_y_label = tk.Label(self.offset_y_frame, text="垂直",
                                         bg=Theme.BG_CARD, fg=Theme.FG,
                                         font=F(9), width=4)
        self.btn_offset_y_minus = tk.Button(self.offset_y_frame, text=" ↑ ",
                                             bg=Theme.BG_INPUT, fg=Theme.ACCENT,
                                             font=F(9, "bold"),
                                             activebackground=Theme.BORDER,
                                             relief="flat", cursor="hand2",
                                             width=3, command=lambda: self._adjust_offset("y", -10))
        self.offset_y_spinbox = tk.Spinbox(self.offset_y_frame, from_=-500, to=500,
                                            increment=5, textvariable=self.offset_y_var,
                                            width=6, font=F(9),
                                            bg=Theme.BG_INPUT, fg=Theme.FG,
                                            buttonbackground=Theme.BORDER,
                                            command=lambda: self._on_offset_change())
        self.btn_offset_y_plus = tk.Button(self.offset_y_frame, text=" ↓ ",
                                            bg=Theme.BG_INPUT, fg=Theme.ACCENT,
                                            font=F(9, "bold"),
                                            activebackground=Theme.BORDER,
                                            relief="flat", cursor="hand2",
                                            width=3, command=lambda: self._adjust_offset("y", 10))
        self.offset_y_unit = tk.Label(self.offset_y_frame, text="pt",
                                       bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                                       font=F(9))

        # 偏移重置按钮
        self.btn_offset_reset = tk.Button(self.offset_frame, text="重置偏移",
                                           bg=Theme.BG_INPUT, fg=Theme.FG,
                                           font=F(8),
                                           activebackground=Theme.BORDER,
                                           relief="flat", cursor="hand2",
                                           padx=S(8), pady=S(1),
                                           command=self._reset_offset)

        # Spinbox手动输入绑定
        self.offset_x_spinbox.bind("<FocusOut>", lambda e: self._on_offset_change())
        self.offset_x_spinbox.bind("<Return>", lambda e: self._on_offset_change())
        self.offset_y_spinbox.bind("<FocusOut>", lambda e: self._on_offset_change())
        self.offset_y_spinbox.bind("<Return>", lambda e: self._on_offset_change())

        # 预览区域
        self.preview_frame = tk.Frame(self.card_scale, bg=Theme.BG_CARD)
        self.preview_title_frame = tk.Frame(self.preview_frame, bg=Theme.BG_CARD)
        self.preview_label_title = tk.Label(self.preview_title_frame, text="实时预览",
                                             bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                                             font=F(9))
        # 预览标题行与画布行（作为属性，便于切换缩放方式时反复pack/forget）
        self.preview_nav_frame = tk.Frame(self.preview_frame, bg=Theme.BG_CARD)
        self.preview_canvas_frame = tk.Frame(self.preview_frame, bg=Theme.BG_CARD)
        # 预览边距说明（按边距模式时显示）
        self.preview_fit_note = tk.Label(self.preview_frame,
                                         text="红色虚线为要求的上/下边距位置，内容边缘应正好贴在线上",
                                         bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                                         font=F(9))
        # 预览Canvas尺寸（根据页面横竖动态调整）
        self._preview_canvas_w, self._preview_canvas_h = _preview_size(False)
        self._preview_page_is_landscape = False  # 跟踪当前预览页面方向
        self.preview_canvas = tk.Canvas(self.preview_canvas_frame, width=self._preview_canvas_w, height=self._preview_canvas_h,
                                         bg=Theme.BG_INPUT, highlightthickness=1,
                                         highlightbackground=Theme.BORDER)
        self.preview_placeholder = tk.Label(self.preview_frame,
                                             text="请添加PDF文件以预览缩放效果",
                                             bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                                             font=F(10))
        # 预览页面控制
        self.preview_page_var = tk.IntVar(value=1)
        self.preview_page_input_var = tk.StringVar(value="1")
        self.preview_page_label = tk.Label(self.preview_nav_frame, text="预览页：",
                                            bg=Theme.BG_CARD, fg=Theme.FG,
                                            font=F(9))
        self.btn_preview_prev = tk.Button(self.preview_nav_frame, text="◀",
                                           bg=Theme.BG_INPUT, fg=Theme.FG,
                                           font=F(10),
                                           activebackground=Theme.BORDER,
                                           relief="flat", cursor="hand2",
                                           padx=S(6), command=self._preview_prev_page)
        self.preview_page_entry = tk.Entry(self.preview_nav_frame, textvariable=self.preview_page_input_var,
                                           bg=Theme.BG_INPUT, fg=Theme.ACCENT,
                                           insertbackground=Theme.ACCENT,
                                           font=F(10, "bold"), width=5,
                                           justify=tk.CENTER, relief="flat", highlightthickness=1,
                                           highlightcolor=Theme.ACCENT, highlightbackground=Theme.BORDER)
        self.preview_page_entry.bind("<Return>", self._preview_jump_to_input)
        self.preview_page_entry.bind("<KP_Enter>", self._preview_jump_to_input)
        self.preview_page_entry.bind("<FocusOut>", lambda e: self._sync_preview_page_input())
        self.btn_preview_goto = tk.Button(self.preview_nav_frame, text="跳转",
                                           bg=Theme.BG_INPUT, fg=Theme.FG,
                                           font=F(9),
                                           activebackground=Theme.BORDER,
                                           relief="flat", cursor="hand2",
                                           padx=S(6), command=self._preview_jump_to_input)
        self.btn_preview_next = tk.Button(self.preview_nav_frame, text="▶",
                                           bg=Theme.BG_INPUT, fg=Theme.FG,
                                           font=F(10),
                                           activebackground=Theme.BORDER,
                                           relief="flat", cursor="hand2",
                                           padx=S(6), command=self._preview_next_page)
        self.preview_total_label = tk.Label(self.preview_nav_frame, text="",
                                             bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                                             font=F(9))
        # 页码显示与输入框保持同步（翻页按钮、载入文件等都会改 preview_page_var）
        self.preview_page_var.trace_add("write", lambda *_: self._sync_preview_page_input())

        # 预览相关状态（不再需要手动刷新按钮，操作时自动更新）
        self._preview_doc = None
        self._preview_pdf_path = None
        self._preview_total_pages = 0
        self._preview_photo = None  # 保持引用防止GC
        self._preview_update_job = None  # 防抖定时器

        # --- 页面范围选择卡片 ---
        self.card_page_range = tk.Frame(self.scroll_frame, bg=Theme.BG_CARD, highlightthickness=1,
                                         highlightbackground=Theme.BORDER)
        self.card_page_range_title = tk.Label(self.card_page_range, text="页面范围",
                                               bg=Theme.BG_CARD, fg=Theme.ACCENT,
                                               font=F(12, "bold"))
        self.page_range_var = tk.StringVar(value="")
        self.page_range_entry = tk.Entry(self.card_page_range, textvariable=self.page_range_var,
                                          bg=Theme.BG_INPUT, fg=Theme.FG,
                                          insertbackground=Theme.FG, font=F(10),
                                          relief="flat", highlightthickness=1,
                                          highlightcolor=Theme.ACCENT,
                                          highlightbackground=Theme.BORDER, width=25)
        self.page_range_hint = tk.Label(self.card_page_range,
                                         text="留空=所有页面，示例：3,5-7 表示第3页和第5-7页",
                                         bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                                         font=F(9))

        # --- 自动旋转设置卡片 ---
        self.card_rotate = tk.Frame(self.scroll_frame, bg=Theme.BG_CARD, highlightthickness=1,
                                     highlightbackground=Theme.BORDER)
        self.card_rotate_title = tk.Label(self.card_rotate, text="旋转设置",
                                           bg=Theme.BG_CARD, fg=Theme.ACCENT,
                                           font=F(12, "bold"))
        self.rotate_direction_var = tk.StringVar(value="counterclockwise")
        (self.rb_rotate_cw,
         self.rb_rotate_ccw) = self._make_choice_group(
            self.card_rotate,
            [("顺时针90°", "clockwise"), ("逆时针90°", "counterclockwise")],
            self.rotate_direction_var,
        )
        self.rotate_auto_var = tk.BooleanVar(value=True)
        self.cb_rotate_auto = self._make_check(self.card_rotate, "仅自动旋转横向页面",
                                               self.rotate_auto_var)

        # --- 页面尺寸调整卡片 ---
        self.card_resize = tk.Frame(self.scroll_frame, bg=Theme.BG_CARD, highlightthickness=1,
                                     highlightbackground=Theme.BORDER)
        self.card_resize_title = tk.Label(self.card_resize, text="页面尺寸设置",
                                           bg=Theme.BG_CARD, fg=Theme.ACCENT,
                                           font=F(12, "bold"))

        # 目标尺寸选择
        self.resize_size_frame = tk.Frame(self.card_resize, bg=Theme.BG_CARD)
        self.resize_size_label = tk.Label(self.resize_size_frame, text="目标尺寸：",
                                           bg=Theme.BG_CARD, fg=Theme.FG,
                                           font=F(10))
        self.resize_size_var = tk.StringVar(value="A4")
        self.resize_size_combo = ttk.Combobox(self.resize_size_frame, textvariable=self.resize_size_var,
                                               values=list(PAGE_SIZES.keys()),
                                               state="readonly", width=10,
                                               font=F(10))
        self.resize_size_combo.set("A4")

        # 内容缩放功能已合并到"PDF修改比例"中，此处不再重复

        # --- 去除页眉Logo设置卡片 ---
        self.card_logo = tk.Frame(self.scroll_frame, bg=Theme.BG_CARD, highlightthickness=1,
                                  highlightbackground=Theme.BORDER)
        self.card_logo_title = tk.Label(self.card_logo, text="页眉Logo去除设置",
                                         bg=Theme.BG_CARD, fg=Theme.ACCENT,
                                         font=F(12, "bold"))
        # 标识挂在哪一角：角区就是从这一角往里圈出来的搜索范围
        self.logo_anchor_frame = tk.Frame(self.card_logo, bg=Theme.BG_CARD)
        self.logo_anchor_label = tk.Label(self.logo_anchor_frame, text="标识位置：",
                                          bg=Theme.BG_CARD, fg=Theme.FG, font=F(10))
        self.logo_anchor_var = tk.StringVar(value="top_left")
        self.logo_anchor_radios = self._make_choice_group(
            self.logo_anchor_frame,
            [(text, key) for key, text in LogoConfig.ANCHOR_LABELS.items()],
            self.logo_anchor_var,
        )

        # 搜索区大小：角区宽高，比标识本身放宽裕，标识离角多远都能圈进来
        self.logo_zone_frame = tk.Frame(self.card_logo, bg=Theme.BG_CARD)
        self.logo_zone_w_label = tk.Label(self.logo_zone_frame, text="搜索区宽(cm)：",
                                          bg=Theme.BG_CARD, fg=Theme.FG, font=F(10))
        self.logo_zone_w_var = tk.StringVar(value="9")
        self.logo_zone_w_entry = tk.Entry(self.logo_zone_frame, textvariable=self.logo_zone_w_var,
                                          bg=Theme.BG_INPUT, fg=Theme.FG, insertbackground=Theme.FG,
                                          font=F(10), relief="flat", highlightthickness=1,
                                          highlightcolor=Theme.ACCENT, highlightbackground=Theme.BORDER,
                                          width=6, justify=tk.CENTER)
        self.logo_zone_h_label = tk.Label(self.logo_zone_frame, text="高(cm)：",
                                          bg=Theme.BG_CARD, fg=Theme.FG, font=F(10))
        self.logo_zone_h_var = tk.StringVar(value="4")
        self.logo_zone_h_entry = tk.Entry(self.logo_zone_frame, textvariable=self.logo_zone_h_var,
                                          bg=Theme.BG_INPUT, fg=Theme.FG, insertbackground=Theme.FG,
                                          font=F(10), relief="flat", highlightthickness=1,
                                          highlightcolor=Theme.ACCENT, highlightbackground=Theme.BORDER,
                                          width=6, justify=tk.CENTER)
        self.logo_presence_label = tk.Label(self.logo_zone_frame, text="跨页一致比例(%)：",
                                            bg=Theme.BG_CARD, fg=Theme.FG, font=F(10))
        self.logo_presence_var = tk.StringVar(value="50")
        self.logo_presence_entry = tk.Entry(self.logo_zone_frame, textvariable=self.logo_presence_var,
                                            bg=Theme.BG_INPUT, fg=Theme.FG, insertbackground=Theme.FG,
                                            font=F(10), relief="flat", highlightthickness=1,
                                            highlightcolor=Theme.ACCENT, highlightbackground=Theme.BORDER,
                                            width=6, justify=tk.CENTER)

        # 整行删除：认到角标只是入口，把同一行的公司全称和下面那条横线一起抹掉
        self.logo_row_frame = tk.Frame(self.card_logo, bg=Theme.BG_CARD)
        self.logo_whole_row_var = tk.BooleanVar(value=False)
        self.logo_whole_row_check = self._make_check(
            self.logo_row_frame, "整行删除（连页眉文字和横线一起去掉）",
            self.logo_whole_row_var)
        self.logo_row_hint_label = tk.Label(
            self.logo_row_frame,
            text="勾上后按整行处理：这一行右侧的\"文件编号/文件版本\"也会一起去掉",
            bg=Theme.BG_CARD, fg=Theme.FG_DIM, font=F(9), justify=tk.LEFT)

        self.logo_hint_label = tk.Label(
            self.card_logo,
            text="跨页一致比例：这块墨迹至少要在这么多比例的采样页上同时出现才算固定标识。\n"
                 "调高更保险（只认页页都有的角标），调低能容忍有的页没印标识；宁可漏掉，不吃正文",
            bg=Theme.BG_CARD, fg=Theme.FG_DIM, font=F(9), justify=tk.LEFT)

        # --- 按边距缩放控件（属于"内容缩放设置"卡片的一种缩放方式） ---
        self.fit_margin_frame = tk.Frame(self.card_scale, bg=Theme.BG_CARD)
        self.fit_top_label = tk.Label(self.fit_margin_frame, text="顶部距离(cm)：",
                                       bg=Theme.BG_CARD, fg=Theme.FG, font=F(10))
        self.fit_top_var = tk.StringVar(value="2.5")
        self.fit_top_entry = tk.Entry(self.fit_margin_frame, textvariable=self.fit_top_var,
                                       bg=Theme.BG_INPUT, fg=Theme.FG, insertbackground=Theme.FG,
                                       font=F(10), relief="flat", highlightthickness=1,
                                       highlightcolor=Theme.ACCENT, highlightbackground=Theme.BORDER,
                                       width=6, justify=tk.CENTER)
        self.fit_bottom_label = tk.Label(self.fit_margin_frame, text="底部距离(cm)：",
                                          bg=Theme.BG_CARD, fg=Theme.FG, font=F(10))
        self.fit_bottom_var = tk.StringVar(value="2.5")
        self.fit_bottom_entry = tk.Entry(self.fit_margin_frame, textvariable=self.fit_bottom_var,
                                          bg=Theme.BG_INPUT, fg=Theme.FG, insertbackground=Theme.FG,
                                          font=F(10), relief="flat", highlightthickness=1,
                                          highlightcolor=Theme.ACCENT, highlightbackground=Theme.BORDER,
                                          width=6, justify=tk.CENTER)

        # 左右距离判定：勾选后内容两侧越界也等比缩小，不勾选只保证不被页面边缘裁掉
        self.fit_side_frame = tk.Frame(self.card_scale, bg=Theme.BG_CARD)
        self.fit_side_var = tk.BooleanVar(value=False)
        self.cb_fit_side = self._make_check(self.fit_side_frame,
                                            "判定左右距离",
                                            self.fit_side_var,
                                            command=self._on_fit_side_change)
        self.fit_side_cm_var = tk.StringVar(value="2.0")
        self.fit_side_cm_entry = tk.Entry(self.fit_side_frame, textvariable=self.fit_side_cm_var,
                                          bg=Theme.BG_INPUT, fg=Theme.FG, insertbackground=Theme.FG,
                                          font=F(10), relief="flat", highlightthickness=1,
                                          highlightcolor=Theme.ACCENT, highlightbackground=Theme.BORDER,
                                          width=6, justify=tk.CENTER)
        self.fit_side_hint = tk.Label(self.fit_side_frame, text=self.FIT_SIDE_HINT_ON,
                                      bg=Theme.BG_CARD, fg=Theme.FG_DIM, font=F(9))
        self.fit_side_cm_entry.bind("<Return>", lambda e: self._schedule_preview_update())
        self.fit_side_cm_entry.bind("<FocusOut>", lambda e: self._schedule_preview_update())

        # 预缩放比例：先把内容按这个比例缩放，再做边距对齐（只在"顶部对齐"判定下生效）
        self.fit_pre_frame = tk.Frame(self.card_scale, bg=Theme.BG_CARD)
        self.fit_pre_label = tk.Label(self.fit_pre_frame, text="预缩放比例(%)：",
                                       bg=Theme.BG_CARD, fg=Theme.FG, font=F(10))
        self.fit_pre_var = tk.IntVar(value=100)
        self.fit_pre_spinbox = tk.Spinbox(self.fit_pre_frame, from_=50, to=200, increment=5,
                                           textvariable=self.fit_pre_var, width=6,
                                           font=F(10), bg=Theme.BG_INPUT, fg=Theme.FG,
                                           insertbackground=Theme.FG, relief="flat",
                                           buttonbackground=Theme.BORDER,
                                           command=self._schedule_preview_update)
        self.fit_pre_hint = tk.Label(self.fit_pre_frame, text=self.FIT_PRE_HINT_ON,
                                      bg=Theme.BG_CARD, fg=Theme.FG_DIM, font=F(9))
        self.fit_pre_spinbox.bind("<Return>", lambda e: self._schedule_preview_update())
        self.fit_pre_spinbox.bind("<FocusOut>", lambda e: self._schedule_preview_update())

        # 特殊页跳过设置（印发说明页版式固定，不能重新摆放）
        self.fit_skip_frame = tk.Frame(self.card_scale, bg=Theme.BG_CARD)
        self.fit_auto_skip_var = tk.BooleanVar(value=True)
        self.cb_fit_auto_skip = self._make_check(self.fit_skip_frame,
                                                 "自动跳过印发说明页",
                                                 self.fit_auto_skip_var,
                                                 command=self._on_fit_auto_skip_change)
        self.fit_small_label = tk.Label(self.fit_skip_frame, text="印发说明页内容高度阈值(%)：",
                                        bg=Theme.BG_CARD, fg=Theme.FG, font=F(10))
        self.fit_small_var = tk.StringVar(value="15")
        self.fit_small_entry = tk.Entry(self.fit_skip_frame, textvariable=self.fit_small_var,
                                        bg=Theme.BG_INPUT, fg=Theme.FG, insertbackground=Theme.FG,
                                        font=F(10), relief="flat", highlightthickness=1,
                                        highlightcolor=Theme.ACCENT, highlightbackground=Theme.BORDER,
                                        width=5, justify=tk.CENTER)
        self.fit_small_entry.bind("<Return>", lambda e: self._schedule_preview_update())
        self.fit_small_entry.bind("<FocusOut>", lambda e: self._schedule_preview_update())

        self.fit_skip_pages_frame = tk.Frame(self.card_scale, bg=Theme.BG_CARD)
        self.fit_skip_pages_label = tk.Label(self.fit_skip_pages_frame, text="手动跳过页码：",
                                             bg=Theme.BG_CARD, fg=Theme.FG, font=F(10))
        self.fit_skip_pages_var = tk.StringVar(value="")
        self.fit_skip_pages_entry = tk.Entry(self.fit_skip_pages_frame, textvariable=self.fit_skip_pages_var,
                                             bg=Theme.BG_INPUT, fg=Theme.FG, insertbackground=Theme.FG,
                                             font=F(10), relief="flat", highlightthickness=1,
                                             highlightcolor=Theme.ACCENT, highlightbackground=Theme.BORDER,
                                             width=18)
        self.fit_skip_pages_hint = tk.Label(self.fit_skip_pages_frame, text="如 1,12；这些页原样输出",
                                            bg=Theme.BG_CARD, fg=Theme.FG_DIM, font=F(9))
        self.fit_skip_pages_entry.bind("<Return>", lambda e: self._schedule_preview_update())
        self.fit_skip_pages_entry.bind("<FocusOut>", lambda e: self._schedule_preview_update())

        self.fit_mode_frame = tk.Frame(self.card_scale, bg=Theme.BG_CARD)
        self.fit_mode_var = tk.StringVar(value="top")
        (self.rb_fit_top,
         self.rb_fit_bottom,
         self.rb_fit_both) = self._make_choice_group(
            self.fit_mode_frame,
            [("顶部对齐", "top"), ("底部对齐", "bottom"), ("两侧对齐", "both")],
            self.fit_mode_var,
            command=self._on_fit_mode_change,
        )

        # 等比缩放的子选项：让哪个方向的边距成立，或干脆只按比例和偏移走
        self.uniform_axis_frame = tk.Frame(self.card_scale, bg=Theme.BG_CARD)
        self.uniform_axis_var = tk.StringVar(value="v")
        (self.rb_uniform_v,
         self.rb_uniform_h,
         self.rb_uniform_free) = self._make_choice_group(
            self.uniform_axis_frame,
            [("上下符合边距", "v"), ("左右符合边距", "h"), ("只调比例和偏移", "free")],
            self.uniform_axis_var,
            command=self._on_scale_sub_change,
        )

        # 根据边距缩放的子选项：拉伸哪个方向，或两个方向各按自己的比例
        self.stretch_axis_frame = tk.Frame(self.card_scale, bg=Theme.BG_CARD)
        self.stretch_axis_var = tk.StringVar(value="h")
        (self.rb_stretch_h,
         self.rb_stretch_v,
         self.rb_stretch_custom) = self._make_choice_group(
            self.stretch_axis_frame,
            [("左右符合边距（水平拉伸）", "h"), ("上下符合边距（垂直拉伸）", "v"),
             ("自定义左右、上下比例", "custom")],
            self.stretch_axis_var,
            command=self._on_scale_sub_change,
        )

        # 非等比时未被边距接管的那个方向按这里的比例缩放
        self.stretch_ratio_frame = tk.Frame(self.card_scale, bg=Theme.BG_CARD)
        self.stretch_x_label = tk.Label(self.stretch_ratio_frame, text="左右比例(%)：",
                                        bg=Theme.BG_CARD, fg=Theme.FG, font=F(10))
        self.stretch_x_var = tk.IntVar(value=100)
        self.stretch_x_spinbox = tk.Spinbox(self.stretch_ratio_frame, from_=50, to=200,
                                            increment=5, textvariable=self.stretch_x_var,
                                            width=6, font=F(10),
                                            bg=Theme.BG_INPUT, fg=Theme.FG,
                                            insertbackground=Theme.FG, relief="flat",
                                            buttonbackground=Theme.BORDER,
                                            command=self._schedule_preview_update)
        self.stretch_y_label = tk.Label(self.stretch_ratio_frame, text="上下比例(%)：",
                                        bg=Theme.BG_CARD, fg=Theme.FG, font=F(10))
        self.stretch_y_var = tk.IntVar(value=100)
        self.stretch_y_spinbox = tk.Spinbox(self.stretch_ratio_frame, from_=50, to=200,
                                            increment=5, textvariable=self.stretch_y_var,
                                            width=6, font=F(10),
                                            bg=Theme.BG_INPUT, fg=Theme.FG,
                                            insertbackground=Theme.FG, relief="flat",
                                            buttonbackground=Theme.BORDER,
                                            command=self._schedule_preview_update)
        self.stretch_ratio_hint = tk.Label(self.stretch_ratio_frame,
                                           text="没被边距接管的那个方向按这里的比例缩放",
                                           bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                                           font=F(9))
        for spin in (self.stretch_x_spinbox, self.stretch_y_spinbox):
            spin.bind("<Return>", lambda e: self._schedule_preview_update())
            spin.bind("<FocusOut>", lambda e: self._schedule_preview_update())

        self.fit_hint = tk.Label(self.card_scale,
                                  text=self.FIT_HINT_ALIGN,
                                  bg=Theme.BG_CARD, fg=Theme.FG_DIM, font=F(9),
                                  justify=tk.LEFT, anchor=tk.W)
        # 距离输入改变后刷新预览（回车或失去焦点时）
        self.fit_top_entry.bind("<Return>", lambda e: self._schedule_preview_update())
        self.fit_top_entry.bind("<FocusOut>", lambda e: self._schedule_preview_update())
        self.fit_bottom_entry.bind("<Return>", lambda e: self._schedule_preview_update())
        self.fit_bottom_entry.bind("<FocusOut>", lambda e: self._schedule_preview_update())

        # --- 进度日志卡片 ---
        self.card_progress = tk.Frame(self.scroll_frame, bg=Theme.BG_CARD, highlightthickness=1,
                                       highlightbackground=Theme.BORDER)
        self.card_progress_title = tk.Label(self.card_progress, text="处理进度", bg=Theme.BG_CARD,
                                             fg=Theme.ACCENT, font=F(12, "bold"))
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(self.card_progress, variable=self.progress_var,
                                             maximum=100, length=600)
        self.status_icon = tk.Label(self.card_progress, text="◉", bg=Theme.BG_CARD,
                                     fg=Theme.SUCCESS, font=F(12))
        self.status_label = tk.Label(self.card_progress, text="就绪",
                                      bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                                      font=F(10))
        self.log_text = scrolledtext.ScrolledText(self.card_progress, height=6, wrap=tk.WORD,
                                                   bg=Theme.BG_INPUT, fg=Theme.FG,
                                                   insertbackground=Theme.FG,
                                                   font=mono(9),
                                                   relief="flat", state=tk.DISABLED,
                                                   highlightthickness=1,
                                                   highlightcolor=Theme.ACCENT,
                                                   highlightbackground=Theme.BORDER)
        self.log_scrollbar = self.log_text.vbar
        self.log_scrollbar.configure(bg=Theme.BG_CARD, troughcolor=Theme.BG,
                                     activebackground=Theme.BORDER)

        # --- 底部操作栏 ---
        self.footer = tk.Frame(self.root, bg=Theme.BG, height=S(56))
        self.btn_start = tk.Button(self.footer, text="开始处理", bg=Theme.ACCENT,
                                    fg="white", font=F(12, "bold"),
                                    activebackground=Theme.ACCENT_HOVER,
                                    activeforeground="white",
                                    relief="flat", cursor="hand2",
                                    padx=S(30), pady=S(8), command=self._start_process)
        self.btn_stop = tk.Button(self.footer, text="停止", bg="#585b70",
                                   fg="white", font=F(11),
                                   activebackground="#6c7086",
                                   activeforeground="white",
                                   relief="flat", cursor="hand2",
                                   padx=S(20), pady=S(8), command=self._stop_process)
        self.btn_open = tk.Button(self.footer, text="打开输出目录", bg=Theme.BG_CARD,
                                   fg=Theme.FG, font=F(10),
                                   activebackground=Theme.BORDER,
                                   activeforeground=Theme.FG,
                                   relief="flat", cursor="hand2",
                                   padx=S(16), pady=S(8), command=self._open_output_dir)

    # ========== 布局 ==========
    def _layout_widgets(self):
        # 顶部标题栏（不随内容滚动）
        self.header.pack(fill=tk.X, padx=S(20), pady=(S(12), S(0)))
        self.header_label.pack(anchor=tk.W)
        self.header_sub.pack(anchor=tk.W, pady=(S(2), S(6)))
        # 标题区下面一条细分隔线，把"这是什么工具"和"在这儿干活"分开
        tk.Frame(self.root, bg=Theme.BORDER, height=1).pack(fill=tk.X, padx=S(20))

        # 底部按钮栏（不随内容滚动）
        tk.Frame(self.root, bg=Theme.BORDER, height=1).pack(fill=tk.X, side=tk.BOTTOM, padx=S(20))
        self.footer.pack(fill=tk.X, side=tk.BOTTOM, padx=S(20), pady=(S(10), S(14)))
        self.btn_open.pack(side=tk.LEFT, padx=(S(0), S(8)))
        self.btn_stop.pack(side=tk.RIGHT, padx=(S(8), S(0)))
        self.btn_start.pack(side=tk.RIGHT)

        # 可滚动内容区域（填充header和footer之间的空间）
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.scroll_canvas.pack(fill=tk.BOTH, expand=True, padx=(S(16), S(0)), pady=(S(6), S(0)))

        # 卡片内容（pack到scroll_frame内部）
        self._pack_card(self.card_files, self.card_files_title,
                        lambda: self._layout_files_card(), pady=(S(6), S(6)))
        self._pack_card(self.card_output, self.card_output_title,
                        lambda: self._layout_output_card(), pady=S(6))
        self._pack_card(self.card_func_mode, self.card_func_mode_title,
                        lambda: self._layout_func_mode_card(), pady=S(6))
        self._pack_card(self.card_mode, self.card_mode_title,
                        lambda: self._layout_mode_card(), pady=S(6))
        self._pack_card(self.card_settings, None,
                        lambda: self._layout_settings_card(), pady=S(6))
        self._pack_card(self.card_scale, self.card_scale_title,
                        lambda: self._layout_scale_card(), pady=S(6))
        self._pack_card(self.card_page_range, self.card_page_range_title,
                        lambda: self._layout_page_range_card(), pady=S(6))
        self._pack_card(self.card_rotate, self.card_rotate_title,
                        lambda: self._layout_rotate_card(), pady=S(6))
        self._pack_card(self.card_resize, self.card_resize_title,
                        lambda: self._layout_resize_card(), pady=S(6))
        self._pack_card(self.card_logo, self.card_logo_title,
                        lambda: self._layout_logo_card(), pady=S(6))
        self._pack_card(self.card_progress, self.card_progress_title,
                        lambda: self._layout_progress_card(), pady=S(6))

        self._apply_button_hover([self.btn_start, self.btn_stop, self.btn_open,
                                  self.btn_add_files, self.btn_add_folder,
                                  self.btn_remove, self.btn_clear, self.btn_browse,
                                  self.btn_scale_minus, self.btn_scale_plus,
                                  self.btn_offset_x_minus, self.btn_offset_x_plus,
                                  self.btn_offset_y_minus, self.btn_offset_y_plus,
                                  self.btn_offset_reset,
                                  self.btn_preview_prev, self.btn_preview_goto,
                                  self.btn_preview_next])

        # 初始状态：根据功能模式控制页码去除模式卡片可见性
        self._on_func_mode_change()

    def _pack_card(self, card, title_label, layout_fn, pady=S(5)):
        card.pack(fill=tk.X, padx=(S(0), S(16)), pady=pady)
        if title_label:
            title_label.pack(anchor=tk.W, padx=(S(12), S(12)), pady=(S(10), S(0)))
            # 标题底下一小段强调色横线：分节起点一眼能看出来，又不用把标题塞进新容器
            # （Tk 里用 pack -in 换父容器会让按钮画不出来，别再走那条路）
            tk.Frame(card, bg=Theme.ACCENT, width=46, height=2).pack(
                anchor=tk.W, padx=S(12), pady=(S(3), S(6)))
        layout_fn()

    def _on_scroll_canvas_configure(self, event):
        """Canvas尺寸变化时，同步内部frame宽度"""
        self.scroll_canvas.itemconfig(self._scroll_window_id, width=event.width)

    def _bind_mousewheel(self, event):
        """鼠标进入滚动区域时绑定滚轮事件"""
        self.scroll_canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        # Linux支持
        self.scroll_canvas.bind_all("<Button-4>", self._on_mousewheel_linux_up)
        self.scroll_canvas.bind_all("<Button-5>", self._on_mousewheel_linux_down)

    def _unbind_mousewheel(self, event):
        """鼠标离开滚动区域时解绑滚轮事件"""
        self.scroll_canvas.unbind_all("<MouseWheel>")
        self.scroll_canvas.unbind_all("<Button-4>")
        self.scroll_canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event):
        """Windows/Mac鼠标滚轮滚动"""
        self.scroll_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_mousewheel_linux_up(self, event):
        """Linux鼠标滚轮向上"""
        self.scroll_canvas.yview_scroll(-1, "units")

    def _on_mousewheel_linux_down(self, event):
        """Linux鼠标滚轮向下"""
        self.scroll_canvas.yview_scroll(1, "units")

    def _layout_files_card(self):
        self.file_listbox.pack(fill=tk.BOTH, expand=True, padx=(S(12), S(0)), pady=(S(0), S(8)))
        self.file_scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(S(0), S(8)), pady=(S(0), S(8)))
        # 拖拽提示（仅当列表为空时可见）
        dnd_hint = "拖拽PDF文件到此处" if HAS_DND else ""
        self.file_listbox.insert(tk.END, dnd_hint)
        self.file_listbox.itemconfig(0, fg=Theme.FG_DIM)
        self._dnd_hint_index = 0
        f = tk.Frame(self.card_files, bg=Theme.BG_CARD)
        f.pack(fill=tk.X, padx=S(12), pady=(S(0), S(10)))
        self.btn_add_files.pack(side=tk.LEFT, padx=(S(0), S(8)))
        self.btn_add_folder.pack(side=tk.LEFT, padx=(S(0), S(6)))
        self.btn_remove.pack(side=tk.LEFT, padx=(S(0), S(6)))
        self.btn_clear.pack(side=tk.LEFT)
        self.file_count_label.pack(side=tk.RIGHT)

    def _layout_output_card(self):
        f = tk.Frame(self.card_output, bg=Theme.BG_CARD)
        f.pack(fill=tk.X, padx=S(12), pady=(S(0), S(4)))
        tk.Label(f, text="输出目录：", bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                 font=F(10)).pack(side=tk.LEFT)
        self.output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(S(8), S(8)))
        self.btn_browse.pack(side=tk.LEFT)
        self.cb_replace_source.pack(anchor=tk.W, padx=S(12), pady=(S(0), S(10)))

    def _layout_func_mode_card(self):
        # 说明行先占掉最下面一条，六个按钮再从左往右排，长流水线名就不会被卡片右边裁掉
        self.func_mode_desc_label.pack(side=tk.BOTTOM, anchor=tk.W, padx=S(12), pady=(S(4), S(6)))
        for i, cb in enumerate(self.func_mode_checks):
            cb.pack(side=tk.LEFT, padx=(S(12), S(8)) if i == 0 else (0, 8), pady=S(2))

    def _layout_mode_card(self):
        f = tk.Frame(self.card_mode, bg=Theme.BG_CARD)
        f.pack(fill=tk.X, padx=S(12), pady=(S(0), S(10)))
        for rb in self.mode_radios:
            rb.pack(side=tk.LEFT, padx=(S(0), S(8)), pady=S(2))
        self.mode_desc_label.pack(anchor=tk.W, padx=(S(12), S(0)), pady=(S(4), S(6)))

    def _layout_settings_card(self):
        self.btn_toggle_settings.pack(fill=tk.X, padx=(S(8), S(8)), pady=(S(8), S(0)))

    def _layout_settings_content(self):
        self.settings_content.pack(fill=tk.X, padx=S(12), pady=(S(8), S(12)))
        self.lbl_bottom.grid(row=0, column=0, sticky=tk.W, padx=(S(0), S(8)), pady=S(4))
        self.scale_bottom.grid(row=0, column=1, sticky=tk.EW, pady=S(4))
        self.lbl_bottom_val.grid(row=0, column=2, padx=(S(8), S(0)), pady=S(4))
        self.lbl_top.grid(row=1, column=0, sticky=tk.W, padx=(S(0), S(8)), pady=S(4))
        self.scale_top.grid(row=1, column=1, sticky=tk.EW, pady=S(4))
        self.lbl_top_val.grid(row=1, column=2, padx=(S(8), S(0)), pady=S(4))
        self.lbl_dpi.grid(row=2, column=0, sticky=tk.W, padx=(S(0), S(8)), pady=S(4))
        self.spin_dpi.grid(row=2, column=1, sticky=tk.W, pady=S(4))
        self.cb_side.grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=S(4))
        self.cb_seal.grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=S(4))
        self.cb_close_ask.grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=S(4))
        self.settings_content.columnconfigure(1, weight=1)

    def _layout_scale_card(self):
        """布局缩放控制卡片（对齐缩放 / 等比缩放 / 根据边距缩放三种方式）"""
        # 缩放方式选择行
        self.scale_mode_frame.pack(fill=tk.X, padx=S(12), pady=(S(8), S(2)))
        self.rb_scale_align_mode.pack(side=tk.LEFT, padx=(S(0), S(6)), pady=S(2))
        self.rb_scale_uniform_mode.pack(side=tk.LEFT, padx=(S(0), S(6)), pady=S(2))
        self.rb_scale_stretch_mode.pack(side=tk.LEFT, padx=(S(0), S(6)), pady=S(2))
        self.scale_mode_hint.pack(fill=tk.X, anchor=tk.W, padx=S(12), pady=(S(2), S(6)))

        if self._use_fit_spec():
            self._pack_margin_rows()
        else:
            self._pack_ratio_rows()
        self._pack_preview_rows()

        # 初始占位提示（在Canvas内绘制，不使用单独Label）
        self.root.after(50, self._draw_preview_placeholder)

    def _forget_margin_rows(self):
        for frame in (self.uniform_axis_frame, self.stretch_axis_frame,
                      self.stretch_ratio_frame, self.fit_margin_frame,
                      self.fit_side_frame, self.fit_mode_frame, self.fit_pre_frame,
                      self.fit_skip_frame, self.fit_skip_pages_frame, self.fit_hint):
            frame.pack_forget()

    def _forget_ratio_rows(self):
        for frame in (self.scale_control_frame, self.scale_presets_frame,
                      self.offset_frame):
            frame.pack_forget()

    def _pack_ratio_rows(self):
        """显示"只调比例和偏移"的控件行（等比缩放里不看边距的那一档）"""
        self._forget_margin_rows()
        self._forget_ratio_rows()

        # 比例控制行
        self.scale_control_frame.pack(fill=tk.X, padx=S(12), pady=(S(4), S(4)))

        # -按钮
        self.btn_scale_minus.pack(side=tk.LEFT, padx=(S(0), S(4)))
        # 滑块
        self.scale_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=S(4))
        # +按钮
        self.btn_scale_plus.pack(side=tk.LEFT, padx=(S(4), S(8)))
        # 百分比显示
        self.scale_ratio_label.pack(side=tk.LEFT)

        # 预设比例按钮行
        self.scale_presets_frame.pack(fill=tk.X, padx=S(12), pady=(S(2), S(6)))
        for btn in self.scale_preset_buttons:
            btn.pack(side=tk.LEFT, padx=(S(0), S(4)))
        self._update_preset_button_styles()

        # 内容偏移控制区
        self.offset_frame.pack(fill=tk.X, padx=S(12), pady=(S(0), S(10)))
        self.offset_title_label.pack(anchor=tk.W, pady=(S(0), S(2)))

        # 水平偏移行
        self.offset_x_frame.pack(fill=tk.X, pady=(S(0), S(2)))
        self.offset_x_label.pack(side=tk.LEFT, padx=(S(0), S(4)))
        self.btn_offset_x_minus.pack(side=tk.LEFT, padx=(S(0), S(2)))
        self.offset_x_spinbox.pack(side=tk.LEFT, padx=(S(0), S(2)))
        self.btn_offset_x_plus.pack(side=tk.LEFT, padx=(S(0), S(2)))
        self.offset_x_unit.pack(side=tk.LEFT, padx=(S(2), S(0)))

        # 垂直偏移行
        self.offset_y_frame.pack(fill=tk.X, pady=(S(0), S(2)))
        self.offset_y_label.pack(side=tk.LEFT, padx=(S(0), S(4)))
        self.btn_offset_y_minus.pack(side=tk.LEFT, padx=(S(0), S(2)))
        self.offset_y_spinbox.pack(side=tk.LEFT, padx=(S(0), S(2)))
        self.btn_offset_y_plus.pack(side=tk.LEFT, padx=(S(0), S(2)))
        self.offset_y_unit.pack(side=tk.LEFT, padx=(S(2), S(0)))

        # 重置偏移按钮
        self.btn_offset_reset.pack(anchor=tk.E, pady=(S(0), S(2)))

    def _pack_margin_rows(self):
        """显示"按边距摆放"三档共用的控件行：子选项 + 距离 + 对齐 + 比例 + 跳过"""
        self._forget_ratio_rows()
        self._forget_margin_rows()
        kind = self.scale_mode_var.get()

        if kind == "uniform":
            self.uniform_axis_frame.pack(fill=tk.X, padx=S(12), pady=(S(4), S(2)))
            self.rb_uniform_v.pack(side=tk.LEFT, padx=(S(0), S(6)), pady=S(2))
            self.rb_uniform_h.pack(side=tk.LEFT, padx=(S(0), S(6)), pady=S(2))
            self.rb_uniform_free.pack(side=tk.LEFT, padx=(S(0), S(6)), pady=S(2))
        elif kind == "stretch":
            self.stretch_axis_frame.pack(fill=tk.X, padx=S(12), pady=(S(4), S(2)))
            self.rb_stretch_h.pack(side=tk.LEFT, padx=(S(0), S(6)), pady=S(2))
            self.rb_stretch_v.pack(side=tk.LEFT, padx=(S(0), S(6)), pady=S(2))
            self.rb_stretch_custom.pack(side=tk.LEFT, padx=(S(0), S(6)), pady=S(2))

        self.fit_margin_frame.pack(fill=tk.X, padx=S(12), pady=(S(4), S(4)))
        self.fit_top_label.pack(side=tk.LEFT)
        self.fit_top_entry.pack(side=tk.LEFT, padx=(S(0), S(16)))
        self.fit_bottom_label.pack(side=tk.LEFT)
        self.fit_bottom_entry.pack(side=tk.LEFT)

        self.fit_side_frame.pack(fill=tk.X, padx=S(12), pady=(S(2), S(4)))
        self.cb_fit_side.pack(side=tk.LEFT)
        self.fit_side_cm_entry.pack(side=tk.LEFT, padx=(S(8), S(8)))
        self.fit_side_hint.pack(side=tk.LEFT)
        if self._side_locked():
            self.fit_side_var.set(True)
        self._set_side_state(self.fit_side_var.get())

        self.fit_mode_frame.pack(fill=tk.X, padx=S(12), pady=(S(4), S(4)))
        self.rb_fit_top.pack(side=tk.LEFT, padx=(S(0), S(6)), pady=S(2))
        self.rb_fit_bottom.pack(side=tk.LEFT, padx=(S(0), S(6)), pady=S(2))
        self.rb_fit_both.pack(side=tk.LEFT, padx=(S(0), S(6)), pady=S(2))

        if kind == "align":
            self.fit_pre_frame.pack(fill=tk.X, padx=S(12), pady=(S(2), S(4)))
            self.fit_pre_label.pack(side=tk.LEFT)
            self.fit_pre_spinbox.pack(side=tk.LEFT, padx=(S(0), S(8)))
            self.fit_pre_hint.pack(side=tk.LEFT)
            self._set_pre_scale_state(self.fit_mode_var.get() != "both")
        elif kind == "stretch":
            self.stretch_ratio_frame.pack(fill=tk.X, padx=S(12), pady=(S(2), S(4)))
            axis = self.stretch_axis_var.get()
            if axis in ("v", "custom"):
                self.stretch_x_label.pack(side=tk.LEFT)
                self.stretch_x_spinbox.pack(side=tk.LEFT, padx=(S(0), S(12)))
            if axis in ("h", "custom"):
                self.stretch_y_label.pack(side=tk.LEFT)
                self.stretch_y_spinbox.pack(side=tk.LEFT, padx=(S(0), S(12)))
            self.stretch_ratio_hint.pack(side=tk.LEFT)
            self.stretch_ratio_hint.config(
                text=("水平方向的大小由左右边距决定，只有上下比例起作用" if axis == "h" else
                      "垂直方向的大小由上下边距决定，只有左右比例起作用" if axis == "v" else
                      "左右、上下各按填写的比例缩放，页面尺寸不变"))

        self.fit_skip_frame.pack(fill=tk.X, padx=S(12), pady=(S(4), S(2)))
        self.cb_fit_auto_skip.pack(side=tk.LEFT)
        self.fit_small_label.pack(side=tk.LEFT, padx=(S(16), S(2)))
        self.fit_small_entry.pack(side=tk.LEFT)
        self._set_small_ratio_state(self.fit_auto_skip_var.get())

        self.fit_skip_pages_frame.pack(fill=tk.X, padx=S(12), pady=(S(2), S(2)))
        self.fit_skip_pages_label.pack(side=tk.LEFT)
        self.fit_skip_pages_entry.pack(side=tk.LEFT, padx=(S(0), S(8)))
        self.fit_skip_pages_hint.pack(side=tk.LEFT)

        self.fit_hint.pack(fill=tk.X, anchor=tk.W, padx=S(12), pady=(S(0), S(10)))
        self.fit_hint.config(text={
            "align": self.FIT_HINT_ALIGN,
            "uniform": self.FIT_HINT_UNIFORM,
            "stretch": self.FIT_HINT_STRETCH,
        }.get(kind, self.FIT_HINT_ALIGN))

    def _set_small_ratio_state(self, enabled: bool):
        """阈值输入框只在开启自动跳过时可编辑"""
        try:
            self.fit_small_entry.config(state=("normal" if enabled else "disabled"))
            self.fit_small_label.config(fg=(Theme.FG if enabled else Theme.FG_DIM))
        except tk.TclError:
            pass

    def _on_fit_auto_skip_change(self):
        self._set_small_ratio_state(self.fit_auto_skip_var.get())
        self._schedule_preview_update()

    def _pack_preview_rows(self):
        """显示实时预览区（两种方式共用）"""
        # pack 一个已在打包列表里的 widget 不会把它移到末尾，
        # 切换缩放方式时设置行是重新打包的，所以预览必须先 forget 再 pack 才能保持在最下方
        self.preview_frame.pack_forget()
        self.preview_frame.pack(fill=tk.X, padx=S(12), pady=(S(4), S(10)))

        # 预览标题行
        self.preview_title_frame.pack(fill=tk.X, pady=(S(0), S(4)))
        self.preview_label_title.pack(side=tk.LEFT)

        # 预览页面导航
        self.preview_nav_frame.pack(side=tk.RIGHT)
        self.preview_page_label.pack(side=tk.LEFT, padx=(S(0), S(4)))
        self.btn_preview_prev.pack(side=tk.LEFT)
        self.preview_page_entry.pack(side=tk.LEFT, padx=S(2))
        self.btn_preview_goto.pack(side=tk.LEFT, padx=(S(0), S(2)))
        self.btn_preview_next.pack(side=tk.LEFT, padx=(S(0), S(2)))
        self.preview_total_label.pack(side=tk.LEFT, padx=(S(4), S(0)))

        # 预览画布（居中）
        self.preview_canvas_frame.pack(fill=tk.X, pady=(S(0), S(4)))
        self.preview_canvas.pack(anchor=tk.CENTER)

        # 按边距摆放的模式才需要说明红色参考线
        if self._use_fit_spec():
            self.preview_fit_note.pack(anchor=tk.W, pady=(S(0), S(4)))
            self._update_fit_skip_note()
        else:
            self.preview_fit_note.pack_forget()

    SCALE_HINT_ALIGN = ("对齐缩放：逐页测量内容范围，按填写的距离把内容摆到顶部/底部/两侧，"
                        "内容大小以预缩放比例为准；只有对侧越过距离线时才等比缩小。")
    SCALE_HINT_UNIFORM = ("等比缩放：内容整体等比缩放（可放大可缩小），让所选方向的边距精确成立；"
                          "选“只调比例和偏移”时所有页面按同一个百分比缩放，不看边距。")
    SCALE_HINT_STRETCH = ("根据边距缩放（不等比）：所选方向拉伸到刚好符合边距，另一个方向不变、"
                          "只按对齐方式摆放；选“自定义”时左右、上下各按自己的比例缩放。")
    FIT_HINT_ALIGN = ("顶部对齐：内容顶边贴到顶部距离线，底边越过底部距离线时才等比缩小；\n"
                      "底部对齐：内容底边贴到底部距离线，顶边越过顶部距离线时才等比缩小；\n"
                      "两侧对齐：上下边距同时等于填写的值（内容太宽时以不裁切为准，在边距带里居中）。\n"
                      "勾选“判定左右距离”后，内容左右各留填写的距离，任一侧越界时同样等比缩小。")
    FIT_HINT_UNIFORM = ("上下符合边距：等比缩放使顶部、底部边距同时等于填写的值；\n"
                        "左右符合边距：等比缩放使左右边距同时等于填写的值，垂直方向按上面的对齐方式摆放；\n"
                        "两种都需要内容在另一个方向也放得下，放不下时以放不下的那个方向为准。")
    FIT_HINT_STRETCH = ("左右符合边距：水平方向拉伸到左右边距成立，上下大小不变；\n"
                        "上下符合边距：垂直方向拉伸到上下边距成立，左右大小不变；\n"
                        "自定义：左右、上下各按填写的比例缩放，不看边距线定大小。\n"
                        "没被边距接管的那个方向按对齐方式摆放（两侧对齐＝在边距带里居中）。")
    FIT_NOTE_TOP = "红色虚线为上/下边距位置：内容顶边贴上线，底边不超过下线时保持原大小"
    FIT_NOTE_BOTTOM = "红色虚线为上/下边距位置：内容底边贴下线，顶边不超过上线时保持原大小"
    FIT_NOTE_BOTH = "红色虚线为要求的上/下边距位置，内容边缘应正好贴在线上"
    FIT_NOTE_SIDE = "红色虚线为上/下/左/右边距位置：内容按所选对齐方式贴线，任一侧越界时才等比缩小"
    FIT_NOTE_STRETCH_H = "红色虚线为边距位置：水平方向已拉伸到左右贴线，垂直方向大小不变"
    FIT_NOTE_UNIFORM_H = "红色虚线为边距位置：左右已贴线，垂直方向按所选对齐方式摆放"
    FIT_NOTE_STRETCH_V = "红色虚线为边距位置：垂直方向已拉伸到上下贴线，水平方向大小不变"
    FIT_PRE_HINT_ON = "先按此比例缩放内容，再做对齐摆放；100% 表示只对齐不改变大小"
    FIT_PRE_HINT_OFF = "“两侧对齐”由边距值决定内容大小，此项不起作用"
    FIT_SIDE_HINT_ON = "内容两侧各留此距离，越界才等比缩小"
    FIT_SIDE_HINT_OFF = "不勾选时只保证内容不被页面左右边缘裁掉"
    FIT_SIDE_HINT_LOCKED = "此模式要让左右边距成立，左右距离必填"

    def _side_locked(self) -> bool:
        """这个模式必须让左右边距成立，左右距离不能不勾"""
        kind = self.scale_mode_var.get()
        if kind == "uniform":
            return self.uniform_axis_var.get() == "h"
        if kind == "stretch":
            return self.stretch_axis_var.get() == "h"
        return False

    def _set_side_state(self, enabled: bool):
        """左右距离输入框只在勾选"判定左右距离"时可编辑；必须用到它的模式里强制勾选"""
        locked = self._side_locked()
        try:
            self.cb_fit_side.config(state=("disabled" if locked else "normal"))
            self.fit_side_cm_entry.config(state=("normal" if enabled else "disabled"))
            if locked:
                text, fg = self.FIT_SIDE_HINT_LOCKED, Theme.WARNING
            elif enabled:
                text, fg = self.FIT_SIDE_HINT_ON, Theme.FG_DIM
            else:
                text, fg = self.FIT_SIDE_HINT_OFF, Theme.FG_DIM
            self.fit_side_hint.config(text=text, fg=fg)
        except tk.TclError:
            pass

    def _on_fit_side_change(self):
        self._set_side_state(self.fit_side_var.get())
        self._update_scale_mode_hint()
        self._schedule_preview_update()

    def _get_fit_side_cm(self) -> Optional[float]:
        """读取左右距离；未勾选时返回None（不做左右判定）"""
        if not self.fit_side_var.get() and not self._side_locked():
            return None
        raw = self.fit_side_cm_var.get().strip()
        try:
            side_cm = float(raw)
        except ValueError:
            raise ValueError("左右距离请填写数字（单位厘米）")
        if side_cm <= 0:
            raise ValueError("左右距离必须大于0")
        if side_cm > 10:
            raise ValueError("左右距离超过10cm，请检查是否填错了单位")
        return side_cm

    def _set_pre_scale_state(self, enabled: bool):
        """预缩放只在"对齐缩放"里有意义，"两侧对齐"时由边距决定大小"""
        try:
            self.fit_pre_spinbox.config(state=("normal" if enabled else "disabled"))
            self.fit_pre_hint.config(
                text=(self.FIT_PRE_HINT_ON if enabled else self.FIT_PRE_HINT_OFF),
                fg=(Theme.FG_DIM if enabled else Theme.WARNING))
        except tk.TclError:
            pass

    def _get_percent(self, var, what: str) -> float:
        """读取一个 50–200 的百分比输入"""
        raw = str(var.get()).strip()
        try:
            pct = int(float(raw))
        except ValueError:
            raise ValueError(f"{what}请填写50–200之间的整数")
        if not 50 <= pct <= 200:
            raise ValueError(f"{what}只能在50%–200%之间")
        return pct / 100.0

    def _get_fit_pre_scale(self) -> float:
        """读取对齐缩放的预缩放比例；"两侧对齐"下预缩放不起作用，固定返回1.0"""
        if self.fit_mode_var.get() == "both":
            return 1.0
        return self._get_percent(self.fit_pre_var, "预缩放比例")

    def _use_fit_spec(self) -> bool:
        """当前设置是否走"按边距摆放"那套核心（等比缩放的"只调比例和偏移"除外）"""
        return not (self.scale_mode_var.get() == "uniform"
                    and self.uniform_axis_var.get() == "free")

    def _scale_mode_short(self) -> str:
        """给流水线标签、结果汇总用的一句话模式名"""
        kind = self.scale_mode_var.get()
        if kind == "align":
            return "对齐缩放·" + FitSpec.ALIGN_TEXT.get(self.fit_mode_var.get(), "")
        if kind == "stretch":
            axis = self.stretch_axis_var.get()
            return "根据边距缩放·" + FitSpec.AXIS_TEXT.get(
                "none" if axis == "custom" else axis, "")
        axis = self.uniform_axis_var.get()
        if axis == "free":
            return "等比缩放·比例和偏移"
        return "等比缩放·" + FitSpec.AXIS_TEXT.get(axis, "")

    def _build_fit_spec(self) -> FitSpec:
        """把缩放卡片上的选项汇成一个 FitSpec，处理和预览都用它"""
        top_cm, bottom_cm = self._get_fit_margins()
        side_cm = self._get_fit_side_cm()
        kind = self.scale_mode_var.get()
        align = self.fit_mode_var.get()
        common = dict(top_cm=top_cm, bottom_cm=bottom_cm, side_cm=side_cm, align=align)
        if kind == "align":
            ratio = self._get_fit_pre_scale()
            return FitSpec(kind="align", axis="none", ratio_x=ratio, ratio_y=ratio, **common)
        if kind == "uniform":
            return FitSpec(kind="uniform", axis=self.uniform_axis_var.get(),
                           ratio_x=1.0, ratio_y=1.0, **common)
        gui_axis = self.stretch_axis_var.get()
        # 界面上的"自定义"就是两个方向都不交给边距，核心里叫 none
        axis = "none" if gui_axis == "custom" else gui_axis
        rx = self._get_percent(self.stretch_x_var, "左右比例")
        ry = self._get_percent(self.stretch_y_var, "上下比例")
        if axis == "h":
            rx = 1.0     # 水平方向由边距接管，比例不起作用
        elif axis == "v":
            ry = 1.0     # 垂直方向由边距接管
        return FitSpec(kind="stretch", axis=axis, ratio_x=rx, ratio_y=ry, **common)

    def _update_fit_skip_note(self):
        """预览下方的说明文字：本页是否会被跳过，以及红线代表什么"""
        reason = getattr(self, "_fit_skip_reason", None)
        if reason:
            self.preview_fit_note.config(
                text=f"本页保持原样不调整（{reason}）", fg=Theme.WARNING)
            return
        kind = self.scale_mode_var.get()
        align = self.fit_mode_var.get()
        if kind == "stretch":
            axis = self.stretch_axis_var.get()
            note = {
                "h": self.FIT_NOTE_STRETCH_H,
                "v": self.FIT_NOTE_STRETCH_V,
                "custom": "红色虚线为上/下/左/右边距位置：内容按填写的左右、上下比例缩放后摆放",
            }.get(axis, self.FIT_NOTE_BOTH)
        elif kind == "uniform" and self.uniform_axis_var.get() == "h":
            note = self.FIT_NOTE_UNIFORM_H
        else:
            note = {"top": self.FIT_NOTE_TOP,
                    "bottom": self.FIT_NOTE_BOTTOM,
                    "both": self.FIT_NOTE_BOTH}.get(align, self.FIT_NOTE_BOTH)
            if self.fit_side_var.get() or self._side_locked():
                note = self.FIT_NOTE_SIDE
        self.preview_fit_note.config(text=note, fg=Theme.FG_DIM)

    def _layout_page_range_card(self):
        f = tk.Frame(self.card_page_range, bg=Theme.BG_CARD)
        f.pack(fill=tk.X, padx=S(12), pady=(S(0), S(10)))
        tk.Label(f, text="指定页面：", bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                 font=F(10)).pack(side=tk.LEFT)
        self.page_range_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(S(8), S(8)))
        self.page_range_hint.pack(anchor=tk.W, padx=S(12), pady=(S(0), S(8)))

    def _layout_rotate_card(self):
        f = tk.Frame(self.card_rotate, bg=Theme.BG_CARD)
        f.pack(fill=tk.X, padx=S(12), pady=(S(0), S(6)))
        self.rb_rotate_cw.pack(side=tk.LEFT, padx=(S(0), S(6)), pady=S(2))
        self.rb_rotate_ccw.pack(side=tk.LEFT, padx=(S(0), S(6)), pady=S(2))
        self.cb_rotate_auto.pack(side=tk.LEFT, padx=(S(8), S(0)), pady=S(2))

    def _layout_resize_card(self):
        # 目标尺寸行
        self.resize_size_frame.pack(fill=tk.X, padx=S(12), pady=(S(8), S(10)))
        self.resize_size_label.pack(side=tk.LEFT, padx=(S(0), S(8)))
        self.resize_size_combo.pack(side=tk.LEFT)

    def _layout_logo_card(self):
        self.logo_anchor_frame.pack(fill=tk.X, padx=S(12), pady=(S(8), S(4)))
        self.logo_anchor_label.pack(side=tk.LEFT, padx=(S(0), S(8)))
        for rb in self.logo_anchor_radios:
            rb.pack(side=tk.LEFT, padx=(S(0), S(6)), pady=S(2))

        self.logo_zone_frame.pack(fill=tk.X, padx=S(12), pady=S(4))
        for w in (self.logo_zone_w_label, self.logo_zone_w_entry,
                  self.logo_zone_h_label, self.logo_zone_h_entry,
                  self.logo_presence_label, self.logo_presence_entry):
            w.pack(side=tk.LEFT, padx=(S(0), S(8)), pady=S(2))

        self.logo_row_frame.pack(fill=tk.X, padx=S(12), pady=S(4))
        self.logo_whole_row_check.pack(anchor=tk.W)
        self.logo_row_hint_label.pack(anchor=tk.W)

        self.logo_hint_label.pack(anchor=tk.W, padx=S(12), pady=(S(4), S(10)))

    def _layout_progress_card(self):
        self.progress_bar.pack(fill=tk.X, padx=S(12), pady=(S(0), S(6)))
        sf = tk.Frame(self.card_progress, bg=Theme.BG_CARD)
        sf.pack(fill=tk.X, padx=S(12), pady=(S(0), S(6)))
        self.status_icon.pack(side=tk.LEFT)
        self.status_label.pack(side=tk.LEFT, padx=(S(6), S(0)))
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=S(12), pady=(S(0), S(10)))

    # ========== 交互方法 ==========
    def _get_selected_funcs(self) -> list:
        """获取当前选中的功能列表（按处理顺序排列）"""
        selected = []
        # 固定处理顺序：去瑕疵→去Logo→旋转→内容缩放→尺寸调整→去除页码→转Word
        # 去瑕疵排在最前：黑框会同时影响页码检测和按边距缩放的内容边界判定
        # 去Logo紧随其后：标识在页眉里，留着它会被按边距缩放当成内容、也会被页码检测误认
        order = [
            ("clean_scan", self.func_clean_var),
            ("remove_logo", self.func_logo_var),
            ("pdf_rotate", self.func_rotate_var),
            ("pdf_scale", self.func_scale_var),
            ("pdf_resize", self.func_resize_var),
            ("remove_page_numbers", self.func_remove_pages_var),
        ]
        for key, var in order:
            if var.get():
                selected.append(key)

        # 按边距摆放要先知道去掉页码后的内容边界，否则页码会被算进内容里
        if (self._use_fit_spec() and "pdf_scale" in selected
                and "remove_page_numbers" in selected
                and selected.index("pdf_scale") < selected.index("remove_page_numbers")):
            selected.remove("pdf_scale")
            selected.insert(selected.index("remove_page_numbers") + 1, "pdf_scale")
        return selected

    def _on_func_mode_change(self):
        """功能模式切换时，控制各卡片的可见性"""
        selected = self._get_selected_funcs()
        self._apply_choice_group_style(self.func_mode_checks)
        self._update_func_mode_description()

        # 先隐藏所有可选卡片，再按顺序重新显示，确保布局顺序正确
        self.card_page_range.pack_forget()
        self.card_mode.pack_forget()
        self.card_rotate.pack_forget()
        self.card_resize.pack_forget()
        self.card_scale.pack_forget()
        self.card_logo.pack_forget()

        prev_card = self.card_func_mode

        # 页面范围卡片 - 去除页码、旋转、缩放、尺寸调整、去瑕疵、去Logo时显示
        if any(f in selected for f in ("remove_page_numbers", "pdf_scale", "pdf_rotate",
                                       "pdf_resize", "clean_scan", "remove_logo")):
            self.card_page_range.pack(fill=tk.X, padx=(S(0), S(16)), pady=S(6),
                                       after=prev_card, before=self.card_settings)
            prev_card = self.card_page_range

        # 仅在去除页码选中时显示页码去除模式卡片
        if "remove_page_numbers" in selected:
            self.card_mode.pack(fill=tk.X, padx=(S(0), S(16)), pady=S(6),
                                after=prev_card, before=self.card_settings)
            prev_card = self.card_mode

        # 自动旋转卡片
        if "pdf_rotate" in selected:
            self.card_rotate.pack(fill=tk.X, padx=(S(0), S(16)), pady=S(6),
                                   after=prev_card, before=self.card_settings)
            prev_card = self.card_rotate

        # 页面尺寸调整卡片
        if "pdf_resize" in selected:
            self.card_resize.pack(fill=tk.X, padx=(S(0), S(16)), pady=S(6),
                                   after=prev_card, before=self.card_settings)
            prev_card = self.card_resize

        # 缩放控制卡片（内含按比例/按边距两种缩放方式）
        if "pdf_scale" in selected:
            self.card_scale.pack(fill=tk.X, padx=(S(0), S(16)), pady=S(6),
                                  after=prev_card, before=self.card_settings)
            self._update_scale_mode_hint()
            self.root.after(300, self._load_preview_pdf)
            prev_card = self.card_scale

        # 页眉Logo设置卡片
        if "remove_logo" in selected:
            self.card_logo.pack(fill=tk.X, padx=(S(0), S(16)), pady=S(6),
                                after=prev_card, before=self.card_settings)

        # 更新输出目录默认值
        self._update_default_output_dir()

    def _source_output_dir(self) -> str:
        """替换源文件模式下"输出目录"该显示的位置：源文件自己所在的目录

        跨文件夹的批量任务按源文件各归各处，显示上把第一个目录点出来并标注共有几个。
        """
        dirs = []
        for f in self.input_files:
            d = os.path.dirname(f)
            if d not in dirs:
                dirs.append(d)
        if not dirs:
            return ""
        if len(dirs) == 1:
            return dirs[0]
        return f"{dirs[0]}  等 {len(dirs)} 个源文件目录"

    def _render_output_display(self):
        """输出目录输入框只是显示：替换源文件时显示源文件目录，否则显示用户选的目录

        真正用于写文件的值存在 self.output_var 里，替换模式下不会被源文件路径覆盖掉。
        """
        text = (self._source_output_dir() if self.replace_source_var.get()
                else self.output_var.get())
        if not text and self.replace_source_var.get():
            text = "替换源文件：输出到各源文件所在目录"
        old_state = self.output_entry.cget("state")
        self.output_entry.config(state=tk.NORMAL)  # DISABLED 的 Entry 写不进去
        self.output_entry.delete(0, tk.END)
        if text:
            self.output_entry.insert(0, text)
        self.output_entry.config(state=old_state)

    def _default_output_dir(self):
        """统一的默认输出目录：桌面上的“PDF输出”"""
        return os.path.join(os.path.expanduser("~"), "Desktop", DEFAULT_OUTPUT_DIR_NAME)

    def _update_default_output_dir(self):
        """输出目录不再随所选功能变化；只有用户没自己改过时才刷成默认值"""
        if not self.replace_source_var.get():
            current = self.output_var.get()
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            known = [os.path.join(desktop, name) for name
                     in (DEFAULT_OUTPUT_DIR_NAME,) + LEGACY_OUTPUT_DIR_NAMES]
            if not current or current in known:
                self.output_var.set(self._default_output_dir())
        self._render_output_display()

    def _update_func_mode_description(self, *args):
        """这一行只报处理顺序，功能细节在气泡里看，避免长文挤在卡片里"""
        selected = self._get_selected_funcs()
        if not selected:
            self.func_mode_desc_label.config(text="请至少选择一项功能")
            return
        names = []
        for key in selected:
            if key == "pdf_scale":
                short = self._scale_mode_short()
                names.append(short or self.func_mode_labels.get(key, key))
            else:
                names.append(self.func_mode_labels.get(key, key))
        if len(names) == 1:
            self.func_mode_desc_label.config(text=f"将执行：{names[0]}")
        else:
            self.func_mode_desc_label.config(text="组合处理：" + " → ".join(names))

    # ---------- 统一的选项控件工厂 ----------
    # 全应用只用两套选择性控件：
    #   选项组（单选/多选）= 扁平色块，选中=强调色底+白色粗体，悬停=边框色
    #   布尔开关            = 原生方框勾选，勾选底色统一为强调色

    def _make_choice_group(self, parent, options, variable, command=None, multi=False,
                           tooltips=None):
        """
        创建一组视觉统一的选项按钮，返回控件列表（调用方自行 pack）。

        Args:
            options: [(文本, 值), ...]
            variable: multi=False 时为 StringVar；multi=True 时为 {值: BooleanVar}
            multi: True 为多选（Checkbutton），False 为单选（Radiobutton）
            tooltips: {值: 悬停说明文字}，给了就挂气泡
        """
        widgets = []
        for text, value in options:
            if multi:
                w = tk.Checkbutton(parent, text=text, variable=variable[value])
                w._choice_var = variable[value]
            else:
                w = tk.Radiobutton(parent, text=text, value=value, variable=variable)
                w._choice_var = variable
            w._choice_text = text
            w._choice_value = value
            w._choice_multi = multi
            w._choice_command = command
            w._hovered = False
            w._choice_group = widgets
            w.configure(command=lambda b=w: self._on_choice_clicked(b))
            w.bind("<Enter>", lambda e, b=w: self._set_choice_hover(b, True))
            w.bind("<Leave>", lambda e, b=w: self._set_choice_hover(b, False))
            if tooltips and value in tooltips:
                w._tooltip = ToolTip(w, tooltips[value])
            widgets.append(w)
        if not hasattr(self, "_choice_groups"):
            self._choice_groups = []
        self._choice_groups.append(widgets)
        if not multi:
            # 变量被程序化改写（不只是点击）时，高亮也要跟上
            variable.trace_add("write", lambda *a: self._apply_choice_group_style(widgets))
        self._apply_choice_group_style(widgets)
        return widgets

    def _choice_selected(self, w) -> bool:
        try:
            current = w._choice_var.get()
        except tk.TclError:
            return False
        if w._choice_multi:
            return bool(current)
        return current == w._choice_value

    def _apply_choice_widget_style(self, w):
        """选项按钮的唯一样式来源：选中 / 悬停 / 普通三态"""
        if self._choice_selected(w):
            bg, fg, font = Theme.ACCENT, "#ffffff", F(10, "bold")
            abg, afg = Theme.ACCENT_HOVER, "#ffffff"
        elif w._hovered:
            bg, fg, font = Theme.BORDER, Theme.FG, F(10)
            abg, afg = Theme.BORDER, Theme.FG
        else:
            bg, fg, font = Theme.BG_CARD, Theme.FG, F(10)
            abg, afg = Theme.BG_CARD, Theme.FG
        # 文本恒定，选中态不添加 ✓ 前缀，避免按钮宽度在切换时跳动
        # indicatoron=0 时 Tk 用 selectcolor 作为选中态底色，必须和 bg 一起设
        w.config(text=w._choice_text, bg=bg, fg=fg, font=font, selectcolor=bg,
                 activebackground=abg, activeforeground=afg,
                 relief=tk.FLAT, bd=0, highlightthickness=0,
                 indicatoron=0, cursor="hand2", padx=S(14), pady=S(7))

    def _apply_choice_group_style(self, widgets):
        for w in widgets:
            self._apply_choice_widget_style(w)

    def _apply_all_choice_styles(self):
        for widgets in getattr(self, "_choice_groups", []):
            self._apply_choice_group_style(widgets)

    def _apply_button_hover(self, buttons):
        """给普通按钮补一个悬停提亮

        Tk 的 Button 没有 hover 态，一片死灰；鼠标压上去亮一下才知道点得动。
        强调色按钮亮成 hover 色，其余亮成边框色。
        """
        for b in buttons:
            base = b.cget("bg")
            hover = Theme.ACCENT_HOVER if base == Theme.ACCENT else Theme.BORDER
            b.bind("<Enter>", lambda e, w=b, c=hover: w.config(bg=c), add="+")
            b.bind("<Leave>", lambda e, w=b, c=base: w.config(bg=c), add="+")

    def _set_choice_hover(self, w, on: bool):
        w._hovered = on
        self._apply_choice_widget_style(w)

    def _on_choice_clicked(self, w):
        self._apply_choice_group_style(w._choice_group)
        if w._choice_command:
            w._choice_command()

    def _make_check(self, parent, text, variable, command=None, wraplength=0):
        """统一的布尔开关：没勾是空心黑框，勾上是蓝色方框带 √。

        Tk 的原生指示器在深色底上两种状态都画成一小块蓝，看不出勾没勾（用户提过这个）。
        所以关掉指示器，方框和勾用 ☐/☑ 字符画在文字前面，颜色跟着状态走。
        """
        box = tk.Checkbutton(parent, text="☐ " + text, variable=variable, command=command,
                             bg=Theme.BG_CARD, fg=Theme.FG,
                             selectcolor=Theme.BG_CARD,
                             activebackground=Theme.BG_CARD,
                             activeforeground=Theme.ACCENT_HOVER,
                             font=F(10), cursor="hand2",
                             highlightthickness=0, anchor=tk.W, pady=S(2),
                             indicatoron=False, relief="flat", bd=0, padx=0,
                             wraplength=wraplength)

        def repaint(*_a):
            try:
                on = bool(variable.get())
                box.configure(text=("☑ " if on else "☐ ") + text,
                              fg=Theme.ACCENT if on else Theme.FG)
            except tk.TclError:
                pass    # 控件已经销毁，追踪还会响最后一次，忽略

        variable.trace_add("write", repaint)
        repaint()
        return box

    def _update_scale_mode_hint(self):
        """缩放方式下方的说明文字"""
        kind = self.scale_mode_var.get()
        if kind == "uniform" and self.uniform_axis_var.get() == "free":
            self.scale_mode_hint.config(
                text="只调比例和偏移：所有页面统一按同一个百分比缩放内容，页面尺寸不变，不看边距。")
            return
        text = {
            "align": self.SCALE_HINT_ALIGN,
            "uniform": self.SCALE_HINT_UNIFORM,
            "stretch": self.SCALE_HINT_STRETCH,
        }.get(kind, self.SCALE_HINT_ALIGN)
        if getattr(self, "fit_side_var", None) is not None and self.fit_side_var.get():
            text += "已勾选判定左右距离。"
        self.scale_mode_hint.config(text=text)

    def _on_scale_mode_change(self):
        """切换缩放方式时重建卡片控件并刷新预览"""
        self._update_scale_mode_hint()
        self._layout_scale_card()
        if self._preview_doc is None:
            self._load_preview_pdf()
        else:
            self._schedule_preview_update()

    def _on_scale_sub_change(self):
        """切换子选项（等比的方向 / 根据边距缩放的方向）：行要重排，左右距离可能被锁定"""
        self._update_scale_mode_hint()
        self._layout_scale_card()
        if self._preview_doc is None:
            self._load_preview_pdf()
        else:
            self._schedule_preview_update()

    def _on_fit_mode_change(self):
        """切换对齐方式（顶部 / 底部 / 两侧）"""
        self._set_pre_scale_state(self.fit_mode_var.get() != "both")
        self._schedule_preview_update()

    def _toggle_settings(self):
        if self.settings_expanded.get():
            self.settings_content.pack_forget()
            self.btn_toggle_settings.config(text="▶ 高级设置")
            self.settings_expanded.set(False)
        else:
            self._layout_settings_content()
            self.btn_toggle_settings.config(text="▼ 高级设置")
            self.settings_expanded.set(True)

    def _update_mode_description(self, *args):
        self.mode_desc_var.set(self.mode_descriptions.get(self.mode_var.get(), ""))
        self._apply_choice_group_style(self.mode_radios)

    def _update_file_count(self):
        count = len(self.input_files)
        self.file_count_label.config(text=f"{count} 个文件")
        self._render_output_display()

    def _check_dependencies(self):
        """检查依赖库可用性

        页码检测早就改成形态学定位了（OCR 读不出公文的淡墨页码，还慢 60 倍），
        所以这里不再因为 Tesseract / pdf2docx 缺失而报警告——那两条提示只会误导用户。
        """
        if not HAS_DND:
            self._log("[提示] 未安装 tkinterdnd2，拖拽文件进窗口不可用，请用“添加文件”。",
                      color=Theme.WARNING)

    # ========== 记住上次的界面选项 ==========
    # 取值必须是这些之一的枚举项，存了别的值就当没这条记录
    _SAVED_ENUMS = {
        "mode_var": ("auto", "text_only", "scanned_only", "both"),
        "scale_mode_var": ("align", "uniform", "stretch"),
        "fit_mode_var": ("top", "bottom", "both"),
        "uniform_axis_var": ("v", "h", "free"),
        "stretch_axis_var": ("h", "v", "custom"),
        "rotate_direction_var": ("clockwise", "counterclockwise"),
        "close_action_var": ("quit", "tray"),
        "logo_anchor_var": tuple(LogoConfig.ANCHOR_LABELS.keys()),
    }
    # 厘米/百分比这类手打数字框：恢复前先确认还是个数，别让坏数据把预览卡住
    _SAVED_NUMBERS = ("fit_top_var", "fit_bottom_var", "fit_side_cm_var", "fit_small_var",
                      "logo_zone_w_var", "logo_zone_h_var", "logo_presence_var")

    def _collect_settings(self) -> Dict:
        data = {}
        for name in self.PERSISTED_VARS:
            var = getattr(self, name, None)
            if var is None:
                continue
            try:
                data[name] = var.get()
            except tk.TclError:
                continue
        return data

    def _save_settings(self):
        job = self._settings_save_job
        self._settings_save_job = None
        if job:
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
        try:
            write_saved_settings(self._collect_settings())
        except Exception as e:
            logging.debug(f"保存设置异常: {e}")

    def _schedule_settings_save(self, *_args):
        """选项一变就安排落盘（400ms 防抖），进程被杀掉也不会丢设置"""
        if self._settings_save_job:
            try:
                self.root.after_cancel(self._settings_save_job)
            except tk.TclError:
                return
        try:
            self._settings_save_job = self.root.after(400, self._save_settings)
        except tk.TclError:
            pass  # 窗口已销毁

    def _watch_settings_changes(self):
        for name in self.PERSISTED_VARS:
            var = getattr(self, name, None)
            if isinstance(var, tk.Variable):
                var.trace_add("write", self._schedule_settings_save)
        for sequence in ("<KeyRelease>", "<FocusOut>"):
            self.output_entry.bind(sequence, self._on_output_entry_edited)

    def _on_output_entry_edited(self, *_args):
        """用户直接在框里改路径：写回真正的输出目录设置（替换模式下框只读）"""
        if not self.replace_source_var.get():
            text = self.output_entry.get().strip()
            if text and text != self.output_var.get():
                self.output_var.set(text)
            elif not text:
                self._render_output_display()
        self._schedule_settings_save()

    def _apply_saved_value(self, name: str, value) -> bool:
        """把存下来的一条设置写回变量；值不可信就跳过用默认值"""
        var = getattr(self, name, None)
        if not isinstance(var, tk.Variable):
            return False
        allowed = self._SAVED_ENUMS.get(name)
        if allowed is not None and str(value) not in allowed:
            return False
        if name == "resize_size_var" and str(value) not in PAGE_SIZES:
            return False
        if name in self._SAVED_NUMBERS:
            try:
                num = float(str(value).strip())
            except (TypeError, ValueError):
                return False
            if not 0 < num <= 100:
                return False
        try:
            if isinstance(var, tk.BooleanVar):
                var.set(value if isinstance(value, bool)
                        else str(value).lower() in ("1", "true", "yes"))
            elif isinstance(var, tk.IntVar):
                var.set(int(float(value)))
            elif isinstance(var, tk.DoubleVar):
                var.set(float(value))
            else:
                var.set(str(value))
        except (tk.TclError, TypeError, ValueError) as e:
            logging.debug(f"恢复 {name} 失败: {e}")
            return False
        return True

    def _migrate_legacy_settings(self, data: Dict) -> Dict:
        """老版本的缩放选项换成现在这三档：按比例→等比·只调比例偏移，按边距→对齐缩放"""
        old_kind = data.get("scale_mode_var")
        if old_kind == "ratio":
            data["scale_mode_var"] = "uniform"
            data.setdefault("uniform_axis_var", "free")
        elif old_kind == "margin":
            data["scale_mode_var"] = "align"
        return data

    def _restore_settings(self):
        """启动时把上次关闭前的选项填回界面"""
        data = self._migrate_legacy_settings(read_saved_settings())
        if not data:
            return
        restored = [name for name in self.PERSISTED_VARS
                    if name in data and self._apply_saved_value(name, data[name])]
        # 下面几个就是用户手动点选时走的回调，界面（卡片显隐、控件可用性、
        # 说明文字、按钮高亮）才会跟恢复出来的值一致
        self._on_replace_source_changed()
        self._update_mode_description()
        if self.settings_expanded.get():
            # 变量已经是 True，不能再走 _toggle_settings（那会把它折回去）
            self._layout_settings_content()
            self.btn_toggle_settings.config(text="▼ 高级设置")
        self._on_scale_mode_change()
        self._update_scale_mode_hint()
        self._on_scale_change()
        self._on_func_mode_change()
        # 输出目录放最后写：_on_func_mode_change 会按默认值刷一次它
        out_dir = data.get("output_var") or data.get("output_dir")
        if isinstance(out_dir, str) and out_dir.strip():
            self.output_var.set(out_dir.strip())
        self._render_output_display()
        if restored:
            logging.info(f"已恢复上次退出时的 {len(restored)} 项界面设置")

    # ========== 系统托盘 ==========
    def _set_window_icon(self):
        """设置窗口图标（任务栏和标题栏）"""
        try:
            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.ico')
            if os.path.exists(icon_path):
                self.root.iconbitmap(icon_path)
        except Exception:
            pass  # 图标设置失败不影响运行

    def _setup_tray_icon(self):
        """创建系统托盘图标"""
        try:
            import pystray
            from PIL import Image

            # 使用项目图标文件
            icon_png = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.png')
            if os.path.exists(icon_png):
                icon_img = Image.open(icon_png)
            else:
                # 回退：程序化生成简单图标
                icon_img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
                from PIL import ImageDraw
                draw = ImageDraw.Draw(icon_img)
                try:
                    draw.rounded_rectangle([4, 4, 60, 60], radius=12, fill=(41, 98, 210))
                except AttributeError:
                    draw.rectangle([4, 4, 60, 60], fill=(41, 98, 210))
                draw.text((18, 8), "P", fill=(255, 255, 255))

            menu = pystray.Menu(
                pystray.MenuItem('显示窗口', self._show_window, default=True),
                pystray.MenuItem('退出', self._quit_app),
            )
            self._tray_icon = pystray.Icon('pdf_tool', icon_img, 'PDF Tool', menu)
            # 在后台线程启动托盘图标
            self._tray_thread = threading.Thread(target=self._tray_icon.run, daemon=True)
            self._tray_thread.start()
        except ImportError:
            # pystray未安装，不启用托盘功能
            self._tray_icon = None

    def _on_window_close(self):
        """点窗口 X 号时先问一句：退出程序，还是收进托盘。

        不问的话，用户以为关了其实还挂在右下角，下次双击又起一个，
        任务栏里能攒出好几个小图标。
        """
        if not self._tray_icon:
            # 托盘根本没起来（缺 pystray），后台驻留没意义，直接退
            self._save_settings()
            self._quit_app()
            return
        if self.close_ask_var.get():
            action = self._ask_close_action()
            if action is None:
                return
        else:
            action = self.close_action_var.get()
        self._save_settings()
        if action == "tray":
            self.root.withdraw()
        else:
            self._quit_app()

    CLOSE_QUESTION = "窗口关闭后，程序要怎么办？"
    CLOSE_OPT_QUIT = "退出程序 —— 彻底关掉，右下角不留小图标"
    CLOSE_OPT_TRAY = "收进托盘 —— 只关窗口，批量任务继续在后台跑"
    CLOSE_TRAY_HINT = "回来方法：双击右下角小图标，或右键选「显示窗口」"
    CLOSE_REMEMBER = "不再提示，以后按上面选的做（想改：高级设置 → 关闭时先问我）"

    def _ask_close_action(self):
        """关闭方式的询问框；返回 "quit" / "tray"，点「取消」或关掉询问框返回 None"""
        top = tk.Toplevel(self.root)
        top.title("关闭 PDF Tool")
        top.configure(bg=Theme.BG_CARD)
        top.resizable(False, False)
        top.transient(self.root)
        # 主窗口可能被别的程序压在下面，询问框必须压在所有人上面，否则用户只会觉得"点了没反应"
        top.attributes("-topmost", True)

        picked = tk.StringVar(value=self.close_action_var.get() or "quit")
        remember = tk.BooleanVar(value=False)
        answer = {"action": None, "closed": False}

        tk.Label(top, text=self.CLOSE_QUESTION, bg=Theme.BG_CARD, fg=Theme.FG,
                 font=F(12, "bold"), justify=tk.LEFT).pack(anchor=tk.W, padx=S(18),
                                                            pady=(S(16), S(8)))

        # 选项直接用主界面那套选择按钮（选中=蓝底白字），不重造样式
        rb_quit, rb_tray = self._make_choice_group(
            top, [(self.CLOSE_OPT_QUIT, "quit"), (self.CLOSE_OPT_TRAY, "tray")], picked)
        for rb in (rb_quit, rb_tray):
            rb.pack(anchor=tk.W, padx=S(18), pady=S(3))

        tk.Label(top, text=self.CLOSE_TRAY_HINT, bg=Theme.BG_CARD, fg=Theme.FG_DIM,
                 font=F(9), justify=tk.LEFT).pack(anchor=tk.W, padx=S(32), pady=(S(2), S(6)))

        cb_remember = self._make_check(top, self.CLOSE_REMEMBER, remember, wraplength=S(400))
        cb_remember.pack(anchor=tk.W, padx=S(18), pady=(S(6), S(12)))

        btns = tk.Frame(top, bg=Theme.BG_CARD)
        btns.pack(fill=tk.X, padx=S(18), pady=(S(0), S(16)))

        def accept(*_a):
            if answer["closed"]:
                return
            answer["closed"] = True
            action = picked.get()
            if action not in ("quit", "tray"):
                action = "quit"
            self.close_action_var.set(action)
            if remember.get():
                self.close_ask_var.set(False)
            answer["action"] = action
            top.destroy()

        def decline(*_a):
            answer["closed"] = True
            top.destroy()

        for text, style, cmd, side in (
            ("确定", "Accent.TButton", accept, tk.RIGHT),
            ("取消", "Secondary.TButton", decline, tk.LEFT),
        ):
            ttk.Button(btns, text=text, style=style, command=cmd).pack(side=side, padx=S(4))

        top.protocol("WM_DELETE_WINDOW", decline)
        top.bind("<Return>", accept)
        top.bind("<KP_Enter>", accept)
        top.bind("<Escape>", decline)

        top.update_idletasks()
        pw, ph = self.root.winfo_width(), self.root.winfo_height()
        px, py = self.root.winfo_rootx(), self.root.winfo_rooty()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        x = max(0, px + (pw - w) // 2)
        y = max(0, py + (ph - h) // 3)
        top.geometry("+%d+%d" % (x, y))
        try:
            top.grab_set()
        except tk.TclError:
            pass
        # 焦点给询问框本身而不是某个单选钮：Tk 会在获得焦点的控件里画一圈虚线，
        # 一开框就带个虚线框看着像坏了，键盘 Tab 仍然能走到选项上
        top.focus_force()
        self.root.wait_window(top)
        return answer["action"]

    def _show_window(self, icon=None, item=None):
        """从托盘恢复显示窗口"""
        self.root.after(0, self._restore_window)

    def _restore_window(self):
        """在主线程中恢复窗口"""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _notify_tray(self, title: str, message: str):
        """往系统托盘丢一条气泡提醒。

        窗口收在托盘里时，Tk 的弹窗会开在别人窗户后面，用户看不见——签章丢了、
        文件写不进去这类"别把成品交出去"的警告不能只靠弹窗送达。
        """
        icon = getattr(self, "_tray_icon", None)
        if icon is None:
            return
        try:
            icon.notify(message, title)
        except Exception:
            pass

    def _quit_app(self, icon=None, item=None):
        """退出应用（托盘线程里也会调，统一回主线程做，Tk 不能跨线程碰）"""
        self.root.after(0, self._do_quit)

    def _do_quit(self):
        # 正在处理时直接 destroy，可能把源文件写成半截，先请它停下再等一会儿
        thread = self.process_thread
        if thread and thread.is_alive():
            self.is_processing = False
            if not self._quit_waiting:
                self._quit_waiting = True
                self._log("正在退出：等当前文件处理完...", color=Theme.WARNING)
                self.root.after(500, lambda: self._wait_before_quit(20))
            return
        self._finish_quit()

    def _wait_before_quit(self, tries_left: int):
        thread = self.process_thread
        if thread and thread.is_alive() and tries_left > 0:
            self.root.after(500, lambda: self._wait_before_quit(tries_left - 1))
            return
        if thread and thread.is_alive():
            self._log("处理仍未结束，但已等待过久，按当前状态退出", color=Theme.WARNING)
        self._finish_quit()

    def _finish_quit(self):
        self._quit_waiting = False
        self._save_settings()
        # 快照只是这次会话用来重复生成的原本，退出就可以丢（backup/ 里的原件保留）
        if self._originals_dir:
            shutil.rmtree(self._originals_dir, ignore_errors=True)
            self._originals_dir = None
        if self._tray_icon:
            self._tray_icon.stop()
        self.root.destroy()

    # ========== 替换源文件前的"写不写得进去" ==========
    @staticmethod
    def _write_block(path: str) -> Optional[str]:
        """这个文件现在能不能写回（只试探，不改内容）。

        返回 None=能写；'readonly'=文件被打了"只读"属性（从光盘/共享盘/微信另存下来的
        文件常带这个，跟有没有程序占着是两回事，重命名照样成功，所以最容易被误判）；
        'locked'=确实被别的程序占着。
        """
        try:
            with open(path, "r+b"):
                return None
        except PermissionError:
            try:
                ro = not (os.stat(path).st_mode & stat.S_IWRITE)
            except OSError:
                ro = False
            return "readonly" if ro else "locked"
        except OSError:
            return "locked"

    def _clear_readonly(self, path: str) -> bool:
        """去掉"只读"属性；回来再探一次，写不进还是写不进（可能同时又被人占着）"""
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            return False
        return self._write_block(path) is None

    def _warn_files_locked(self, busy: List[tuple]):
        names = "\n".join("  · " + os.path.basename(p) for p, _w in busy[:8])
        if len(busy) > 8:
            names += f"\n  …共 {len(busy)} 个"
        tail = ""
        if any(w == "readonly" for _p, w in busy):
            tail = ("\n其中有的文件被设成了“只读”（从光盘、共享盘或聊天软件另存下来常见）："
                    "右键→属性→把“只读”勾去掉即可，重命名能成功不代表写得进去。")
        messagebox.showwarning(
            "源文件写不进去",
            f"这些文件现在写不进去，先关掉正在用它们的程序再处理：\n{names}{tail}\n\n"
            "在 InDesign / CorelDRAW 里置入、或被 WPS、Acrobat 打开预览时都会占住文件。\n"
            "如果不想动源文件，可以不勾“替换源文件”，改成输出到别的目录。")
        self._notify_tray("源文件写不进去", f"{len(busy)} 个文件没能写回源文件")

    # ========== 缩放控制方法 ==========
    def _on_scale_change(self, value=None):
        """滑块值改变时的回调"""
        ratio = self.scale_ratio_var.get()
        self.scale_ratio_display_var.set(f"{ratio}%")
        # 更新预设按钮高亮
        self._update_preset_button_styles()
        # 防抖更新预览
        self._schedule_preview_update()

    def _scale_decrease(self):
        """减少缩放比例（每次-5%）"""
        current = self.scale_ratio_var.get()
        new_val = max(10, current - 5)
        self.scale_ratio_var.set(new_val)
        self._on_scale_change()

    def _scale_increase(self):
        """增加缩放比例（每次+5%）"""
        current = self.scale_ratio_var.get()
        new_val = min(150, current + 5)
        self.scale_ratio_var.set(new_val)
        self._on_scale_change()

    def _set_scale_preset(self, value):
        """设置预设缩放比例"""
        self.scale_ratio_var.set(value)
        self._on_scale_change()

    def _update_preset_button_styles(self):
        """预设比例按钮：选中项用与选项组一致的强调色"""
        current = self.scale_ratio_var.get()
        for btn in self.scale_preset_buttons:
            btn_val = int(btn.cget("text").replace("%", ""))
            if btn_val == current:
                btn.config(fg="#ffffff", bg=Theme.ACCENT,
                           activebackground=Theme.ACCENT_HOVER,
                           activeforeground="#ffffff",
                           font=F(9, "bold"))
            else:
                btn.config(fg=Theme.FG, bg=Theme.BG_INPUT,
                           activebackground=Theme.BORDER,
                           activeforeground=Theme.ACCENT,
                           font=F(9))

    def _adjust_offset(self, axis: str, delta: int):
        """调整偏移量（axis='x'或'y'，delta为变化值）"""
        if axis == "x":
            new_val = self.offset_x_var.get() + delta
            new_val = max(-500, min(500, new_val))
            self.offset_x_var.set(new_val)
        elif axis == "y":
            new_val = self.offset_y_var.get() + delta
            new_val = max(-500, min(500, new_val))
            self.offset_y_var.set(new_val)
        self._on_offset_change()

    def _on_offset_change(self):
        """偏移值改变时的回调"""
        # 防抖更新预览
        self._schedule_preview_update()

    def _reset_offset(self):
        """重置偏移为0"""
        self.offset_x_var.set(0)
        self.offset_y_var.set(0)
        self._on_offset_change()

    # ========== 预览方法 ==========
    def _draw_preview_placeholder(self, text="请添加PDF文件以预览缩放效果"):
        """在Canvas内绘制占位提示文字"""
        try:
            self.preview_canvas.delete("all")
            canvas_w, canvas_h = self._preview_canvas_w, self._preview_canvas_h
            self.preview_canvas.create_text(
                canvas_w // 2, canvas_h // 2,
                text=text, fill=Theme.FG_DIM, font=F(10)
            )
        except Exception:
            pass

    def _load_preview_pdf(self):
        """加载当前选中的第一个PDF文件用于预览"""
        if "pdf_scale" not in self._get_selected_funcs():
            return

        # 关闭旧文档
        self._close_preview_doc()

        if not self.input_files:
            self._clear_preview()
            return

        pdf_path = self.input_files[0]
        if not os.path.exists(pdf_path):
            self._clear_preview()
            return

        try:
            self._preview_doc = pymupdf.open(pdf_path)
            self._preview_pdf_path = pdf_path
            self._preview_total_pages = len(self._preview_doc)
            self.preview_page_var.set(1)
            hint = "  输入页码后回车跳转" if self._preview_total_pages > 1 else ""
            self.preview_total_label.config(text=f"/ {self._preview_total_pages}{hint}")
            # 根据第一页方向调整Canvas尺寸
            self._adjust_canvas_orientation(0)
            # 延迟渲染预览，确保Canvas已完成布局
            self.root.after(150, self._update_preview)
        except Exception as e:
            self._log(f"[预览] 加载PDF失败: {e}", color=Theme.WARNING)
            self._clear_preview()

    def _adjust_canvas_orientation(self, page_index: int):
        """根据页面横竖方向调整Canvas尺寸"""
        if not self._preview_doc or page_index >= len(self._preview_doc):
            return
        try:
            page = self._preview_doc[page_index]
            is_landscape = page.rect.width > page.rect.height
            if is_landscape != self._preview_page_is_landscape:
                self._preview_page_is_landscape = is_landscape
                # 横向页面用宽Canvas，纵向页面用高Canvas
                self._preview_canvas_w, self._preview_canvas_h = _preview_size(is_landscape)
                self.preview_canvas.config(width=self._preview_canvas_w, height=self._preview_canvas_h)
        except Exception:
            pass

    def _close_preview_doc(self):
        """关闭预览文档"""
        if self._preview_doc:
            try:
                self._preview_doc.close()
            except Exception:
                pass
            self._preview_doc = None
            self._preview_pdf_path = None
            self._preview_total_pages = 0

    def _clear_preview(self):
        """清空预览区域，在Canvas内绘制占位提示"""
        self.preview_canvas.delete("all")
        # 在Canvas中心绘制占位文字
        canvas_w = self._preview_canvas_w
        canvas_h = self._preview_canvas_h
        self.preview_canvas.create_text(
            canvas_w // 2, canvas_h // 2,
            text="请添加PDF文件以预览缩放效果",
            fill=Theme.FG_DIM, font=F(10)
        )
        self.preview_placeholder.pack_forget()  # 确保旧placeholder隐藏
        self.preview_total_label.config(text="")
        self._preview_total_pages = 0

    def _schedule_preview_update(self):
        """防抖：延迟更新预览（避免滑块拖动时频繁渲染）"""
        if self._preview_update_job:
            self.root.after_cancel(self._preview_update_job)
        self._preview_update_job = self.root.after(200, self._update_preview)

    def _update_preview(self):
        """更新预览图"""
        if "pdf_scale" not in self._get_selected_funcs():
            return

        if not self._preview_doc or self._preview_total_pages == 0:
            self._clear_preview()
            return

        try:
            page_index = self.preview_page_var.get() - 1  # 转为0-based
            if page_index < 0 or page_index >= self._preview_total_pages:
                page_index = 0
                self.preview_page_var.set(1)

            # 根据当前页面方向调整Canvas
            self._adjust_canvas_orientation(page_index)

            # 使用scaler模块渲染预览（preview_width根据Canvas宽度动态调整）
            preview_width = self._preview_canvas_w - 10
            scaler = PDFScaler(dpi=150)

            if self._use_fit_spec():
                try:
                    spec = self._build_fit_spec()
                    auto_skip, small_ratio, skip_pages = self._get_fit_skip_params()
                except ValueError:
                    return  # 距离还在编辑中，保留上一张预览
                manual_skip = False
                if skip_pages:
                    try:
                        manual_skip = page_index in parse_page_range(skip_pages,
                                                                    self._preview_total_pages)
                    except Exception:
                        manual_skip = False
                preview_img, skip_reason = scaler.render_fit_preview_from_doc(
                    self._preview_doc,
                    page_index=page_index,
                    spec=spec,
                    preview_width=preview_width,
                    auto_skip_special=auto_skip,
                    small_content_ratio=small_ratio,
                )
                if manual_skip:
                    skip_reason = "手动指定跳过"
                if skip_reason == "未识别到内容边界":
                    skip_reason = None
                self._fit_skip_reason = skip_reason
            else:
                self._fit_skip_reason = None
                scale_ratio = self.scale_ratio_var.get() / 100.0
                offset_x = float(self.offset_x_var.get())
                offset_y = float(self.offset_y_var.get())
                preview_img = scaler.render_page_preview_from_doc(
                    self._preview_doc,
                    page_index=page_index,
                    scale_ratio=scale_ratio,
                    preview_width=preview_width,
                    offset_x=offset_x,
                    offset_y=offset_y,
                )

            if preview_img is None:
                self.preview_canvas.delete("all")
                canvas_w, canvas_h = self._preview_canvas_w, self._preview_canvas_h
                self.preview_canvas.create_text(
                    canvas_w // 2, canvas_h // 2,
                    text="预览渲染失败",
                    fill=Theme.FG_DIM, font=F(10)
                )
                return

            # 隐藏占位文字（确保不再显示）
            self.preview_placeholder.pack_forget()

            # 调整预览图适应Canvas
            canvas_w = self._preview_canvas_w
            canvas_h = self._preview_canvas_h
            img_w, img_h = preview_img.size

            # 等比缩放到Canvas内（留边距）
            margin = 5
            scale_factor = min((canvas_w - margin * 2) / img_w, (canvas_h - margin * 2) / img_h)
            display_w = int(img_w * scale_factor)
            display_h = int(img_h * scale_factor)

            if scale_factor != 1.0:
                preview_img = preview_img.resize(
                    (display_w, display_h), Image.LANCZOS
                )

            # 转为tkinter PhotoImage
            from PIL import ImageTk
            self._preview_photo = ImageTk.PhotoImage(preview_img)

            # 清空Canvas并绘制
            self.preview_canvas.delete("all")
            # 居中显示
            x_offset = (canvas_w - display_w) // 2
            y_offset = (canvas_h - display_h) // 2
            self.preview_canvas.create_image(
                x_offset, y_offset, anchor=tk.NW, image=self._preview_photo
            )

            self._update_fit_skip_note()

        except Exception as e:
            self._log(f"[预览] 渲染失败: {e}", color=Theme.WARNING)
            # 在Canvas中显示错误信息
            try:
                self.preview_canvas.delete("all")
                canvas_w, canvas_h = self._preview_canvas_w, self._preview_canvas_h
                self.preview_canvas.create_text(
                    canvas_w // 2, canvas_h // 2,
                    text=f"预览出错: {e}",
                    fill=Theme.WARNING, font=F(9)
                )
            except Exception:
                pass

    def _sync_preview_page_input(self):
        """把当前预览页码写回输入框（翻页按钮、载入新文件等改动页码后调用）"""
        self.preview_page_input_var.set(str(self.preview_page_var.get()))

    def _preview_jump_to_input(self, event=None):
        """按输入框里的页码跳转，超出范围时靠边取整，非法输入恢复为当前页"""
        text = self.preview_page_input_var.get().strip()
        current = self.preview_page_var.get()
        if not self._preview_total_pages or not text.isdigit():
            self._sync_preview_page_input()
            return "break"

        target = max(1, min(int(text), self._preview_total_pages))
        if target != current:
            self.preview_page_var.set(target)
            self._schedule_preview_update()
        else:
            self._sync_preview_page_input()
        return "break"

    def _preview_prev_page(self):
        """预览上一页"""
        current = self.preview_page_var.get()
        if current > 1:
            self.preview_page_var.set(current - 1)
            self._schedule_preview_update()

    def _preview_next_page(self):
        """预览下一页"""
        current = self.preview_page_var.get()
        if current < self._preview_total_pages:
            self.preview_page_var.set(current + 1)
            self._schedule_preview_update()

    def _on_drop_files(self, event):
        """拖拽文件回调"""
        # tkinterdnd2返回的文件路径用花括号包裹含空格的路径，用空格分隔
        raw = event.data
        # 解析路径：处理{path with spaces}和普通path两种格式
        paths = []
        i = 0
        while i < len(raw):
            if raw[i] == '{':
                j = raw.find('}', i)
                if j == -1:
                    # 拖进来的是网络路径或中文名时，偶尔会拿到没有配对的 "{"
                    # 在这里 index() 会抛 ValueError，整个拖拽当场没反应
                    paths.append(raw[i + 1:])
                    break
                paths.append(raw[i+1:j])
                i = j + 2  # 跳过}和后面的空格
            elif raw[i] == ' ':
                i += 1
            else:
                j = raw.find(' ', i)
                if j == -1:
                    j = len(raw)
                paths.append(raw[i:j])
                i = j + 1

        self._remove_dnd_hint()
        added = 0
        for path in paths:
            path = path.strip()
            if not path:
                continue
            # 支持拖入文件夹：自动扫描其中的PDF
            if os.path.isdir(path):
                for root_dir, _, files in os.walk(path):
                    for f in files:
                        if f.lower().endswith('.pdf'):
                            fp = os.path.join(root_dir, f)
                            if fp not in self.input_files:
                                self.input_files.append(fp)
                                self.file_listbox.insert(tk.END, f)
                                added += 1
            elif path.lower().endswith('.pdf') and path not in self.input_files:
                self.input_files.append(path)
                self.file_listbox.insert(tk.END, os.path.basename(path))
                added += 1

        if added > 0:
            self._update_file_count()
            self._log(f"拖入 {added} 个PDF文件", color=Theme.FG)
            if "pdf_scale" in self._get_selected_funcs():
                self.root.after(200, self._load_preview_pdf)

    def _remove_dnd_hint(self):
        """移除拖拽提示文字（添加文件前调用）"""
        if hasattr(self, '_dnd_hint_index') and self.file_listbox.size() > 0:
            try:
                if self.file_listbox.get(self._dnd_hint_index) and \
                   '拖拽' in self.file_listbox.get(self._dnd_hint_index):
                    self.file_listbox.delete(self._dnd_hint_index)
                    del self._dnd_hint_index
            except (tk.TclError, IndexError):
                pass

    def _restore_dnd_hint(self):
        """恢复拖拽提示文字（文件列表清空时调用）"""
        if HAS_DND and self.file_listbox.size() == 0:
            self.file_listbox.insert(tk.END, "拖拽PDF文件到此处")
            self.file_listbox.itemconfig(0, fg=Theme.FG_DIM)
            self._dnd_hint_index = 0

    def _add_files(self):
        files = filedialog.askopenfilenames(
            title="选择PDF文件",
            filetypes=[("PDF文件", "*.pdf"), ("所有文件", "*.*")]
        )
        if files:
            self._remove_dnd_hint()
            for f in files:
                if f not in self.input_files:
                    self.input_files.append(f)
                    self.file_listbox.insert(tk.END, os.path.basename(f))
            self._update_file_count()
            self._log(f"已添加 {len(files)} 个文件", color=Theme.FG)
            # 缩放模式下延迟加载预览
            if "pdf_scale" in self._get_selected_funcs():
                self.root.after(200, self._load_preview_pdf)

    def _add_folder(self):
        folder = filedialog.askdirectory(title="选择包含PDF文件的文件夹")
        if folder:
            self._remove_dnd_hint()
            count = 0
            for root_dir, _, files in os.walk(folder):
                for f in files:
                    if f.lower().endswith('.pdf'):
                        fp = os.path.join(root_dir, f)
                        if fp not in self.input_files:
                            self.input_files.append(fp)
                            self.file_listbox.insert(tk.END, f)
                            count += 1
            self._update_file_count()
            self._log(f"从文件夹添加了 {count} 个PDF文件", color=Theme.FG)
            # 缩放模式下自动加载预览
            if "pdf_scale" in self._get_selected_funcs():
                self._load_preview_pdf()

    def _remove_selected(self):
        selection = self.file_listbox.curselection()
        if not selection:
            return
        for idx in sorted(selection, reverse=True):
            self.file_listbox.delete(idx)
            self.input_files.pop(idx)
        self._update_file_count()
        self._log(f"已移除 {len(selection)} 个文件", color=Theme.FG)
        if not self.input_files:
            self._restore_dnd_hint()
        # 缩放模式下刷新预览
        if "pdf_scale" in self._get_selected_funcs():
            self._load_preview_pdf()

    def _clear_all(self):
        self.input_files.clear()
        self.file_listbox.delete(0, tk.END)
        self._restore_dnd_hint()
        self._update_file_count()
        self._log("已清空文件列表", color=Theme.FG)
        # 缩放模式下清空预览
        if "pdf_scale" in self._get_selected_funcs():
            self._close_preview_doc()
            self._clear_preview()

    def _on_replace_source_changed(self):
        """替换源文件复选框状态变化时，切换输出目录的显示与可编辑性"""
        replace = self.replace_source_var.get()
        state = tk.DISABLED if replace else tk.NORMAL
        self.output_entry.config(state=state)
        self.btn_browse.config(state=state)
        self._render_output_display()

    def _browse_output(self):
        folder = filedialog.askdirectory(title="选择输出目录")
        if folder:
            self.output_var.set(folder)
            self._render_output_display()
            self._schedule_settings_save()

    def _open_output_dir(self):
        if self.replace_source_var.get():
            # 替换源文件模式：打开源文件所在目录
            if self.input_files:
                src_dir = os.path.dirname(self.input_files[0])
                if os.path.exists(src_dir):
                    os.startfile(src_dir)
            return
        output_dir = self.output_var.get()
        if not output_dir:
            return
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        os.startfile(output_dir)

    def _get_config(self) -> PageNumberConfig:
        return PageNumberConfig(
            bottom_margin_ratio=self.bottom_ratio_var.get(),
            top_margin_ratio=self.top_ratio_var.get(),
            dpi=self.dpi_var.get(),
            check_side_numbers=self.check_side_var.get(),
            protect_seals=self.check_seal_var.get(),
        )

    def _start_process(self):
        if not self.input_files:
            messagebox.showwarning("提示", "请先添加PDF文件！")
            return
        if self.is_processing:
            return
        # 每次开始处理都落盘一次：万一紧接着从托盘退出，防抖还没写完也有这份
        self._save_settings()

        selected = self._get_selected_funcs()
        if not selected:
            messagebox.showwarning("提示", "请至少选择一项功能！")
            return

        # 缩放功能的参数校验（按比例 / 按边距）
        if "pdf_scale" in selected:
            if self._use_fit_spec():
                try:
                    self._build_fit_spec().validate()
                    self._get_fit_skip_params()
                except ValueError as ve:
                    messagebox.showwarning("提示", str(ve))
                    return
            else:
                if (self.scale_ratio_var.get() == 100
                        and self.offset_x_var.get() == 0 and self.offset_y_var.get() == 0):
                    messagebox.showwarning("提示", "缩放比例为100%且无偏移，无需处理！\n请调整缩放比例或偏移量后再试。")
                    return

        # 去Logo参数校验
        if "remove_logo" in selected:
            try:
                self._build_logo_cfg()
            except ValueError as ve:
                messagebox.showwarning("提示", str(ve))
                return

        output_dir = self.output_var.get()
        replace_source = self.replace_source_var.get()
        if replace_source:
            # 替换模式先试探能不能写：文件被 InDesign/Corel 置入或被阅读器预览占住时，
            # 跑完一整批也是白跑，不如一开始就告诉他。"只读"属性单独处理——他要写回就是
            # 想改这个文件，属性是复制过来带的，替他去掉再往下跑。
            if any(f in self.IN_PLACE_STEPS for f in selected):
                busy = []
                for p in self.input_files:
                    why = self._write_block(p)
                    if why == "readonly":
                        if self._clear_readonly(p):
                            self._log(f"  · {os.path.basename(p)[:40]} 原本被设成“只读”，"
                                      f"已去掉只读属性以便写回", color=Theme.WARNING)
                            why = None
                        else:
                            why = self._write_block(p) or "locked"
                    if why:
                        busy.append((p, why))
                if busy:
                    self._warn_files_locked(busy)
                    return
            # 替换源文件模式：使用临时目录，处理完成后覆盖源文件
            output_dir = tempfile.mkdtemp(prefix="pdf_replace_")
        elif not output_dir:
            output_dir = os.path.join(os.path.dirname(self.input_files[0]), DEFAULT_OUTPUT_DIR_NAME)
            self.output_var.set(output_dir)
            self._render_output_display()

        self.is_processing = True
        self.btn_start.config(state=tk.DISABLED, bg="#585b70")
        self.btn_stop.config(state=tk.NORMAL, bg=Theme.ERROR)
        self.progress_var.set(0)
        self.status_icon.config(fg=Theme.WARNING)
        self.status_label.config(text="处理中...", fg=Theme.WARNING)

        remove_mode = self.mode_var.get()

        self.process_thread = threading.Thread(
            target=self._process_thread,
            args=(self.input_files.copy(), output_dir, selected, remove_mode, replace_source),
            daemon=True
        )
        self.process_thread.start()

    def _run_step(self, func_key: str, current_files: List[str], output_dir: str,
                  config, remove_mode: str, progress_callback,
                  origin_of: Optional[Dict[str, str]] = None,
                  staged: bool = False) -> List[dict]:
        """执行一步；需要绕开原地写时，先输出到临时目录再归位

        两种情况必须绕开：一是输出目录里就是这些输入文件本身（处理库是"打开源文件→
        另存到输出路径"，同一个文件在 Windows 上会报 Permission denied）；二是文件名有
        重复而加了序号暂存，否则序号会留在输出文件名里、同名结果还会互相覆盖。
        """
        in_place = (func_key in self.IN_PLACE_STEPS
                    and self._has_in_place_target(current_files, output_dir, origin_of))
        if not staged and not in_place:
            return self._normalize_skipped(self._process_single_step(
                current_files, output_dir, func_key, config, remove_mode, progress_callback))

        self.root.after(0, lambda msg=("输出目录里就是这些文件本身：先输出到临时目录，完成后再覆盖回原文件"
                                       if in_place else
                                       "文件名有重复：先输出到临时目录，再按各自的源文件归位"):
                        self._log(msg, color=Theme.WARNING))
        step_dir = tempfile.mkdtemp(prefix="pdf_step_")
        try:
            results = self._process_single_step(current_files, step_dir, func_key, config,
                                                 remove_mode, progress_callback)
            self._relocate_step_results(results, step_dir, output_dir, origin_of)
        finally:
            shutil.rmtree(step_dir, ignore_errors=True)
        return self._normalize_skipped(results)

    @staticmethod
    def _normalize_skipped(results: List[dict]) -> List[dict]:
        """把"跳过"的结果统一成可交付的样子。

        有的处理库跳过时也把目标路径填进 output_file，可它压根没写文件（比如页面范围
        填错）。这个假路径一旦往下走，签章对账打不开文件就报 0 枚，看起来像把章弄丢了。
        """
        for r in results:
            if r.get("status") == "skipped" and r.get("output_file"):
                if not os.path.exists(r["output_file"]):
                    r["output_file"] = ""
        return results

    def _passthrough_skipped(self, results: List[dict], output_dir: str,
                             origin_of: Optional[Dict[str, str]] = None):
        """最后一步"没什么可做"不等于这本不用交付。

        组合处理 41 本时，去Logo 对没有标识的那本返回 skipped，前几步的产物就留在临时
        目录里被一起删掉了——成品少两本，替换源文件时前几步的成果也一起丢了。这里把
        上一步的产物原样放进输出目录；本来就没有任何改动的（源文件即成品）不用复制。
        """
        for r in results:
            if r.get("status") != "skipped" or r.get("output_file"):
                continue
            src = r.get("input_file") or ""
            if not os.path.exists(src):
                continue
            origin = self._result_origin(r, origin_of) or src
            if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(origin)):
                continue
            dest = self._output_path_for(output_dir, origin, src)
            try:
                os.makedirs(output_dir, exist_ok=True)
                if os.path.exists(dest):
                    dest = self._free_name(dest)
                shutil.copy2(src, dest)
                r["output_file"] = dest
                r["passthrough"] = True
                self.root.after(0, lambda f=os.path.basename(dest): self._log(
                    f"  · {f} 这一步无需处理，按上一步的结果原样交付", color=Theme.FG_DIM))
            except OSError as e:
                r["status"] = "error"
                r["error"] = f"无需处理，但也没能在输出目录留下这本: {e}"

    def _relocate_step_results(self, results: List[dict], step_dir: str, output_dir: str,
                               origin_of: Optional[Dict[str, str]] = None):
        """把临时目录里的结果放回原位：与源文件同路径的覆盖回去，其余移到输出目录"""
        backup_dir = self._backup_dir()
        for r in results:
            out = r.get("output_file") or ""
            if r.get("status") != "success" or not os.path.exists(out):
                continue
            src = self._result_origin(r, origin_of)
            dest = self._output_path_for(output_dir, src, out)
            if self._is_same_file(dest, src):
                # 原地覆盖：先留最初版本，覆盖后同一批文件还能按不同参数重来
                pristine = self._keep_original(src)
                if self._write_over_source(out, src, backup_dir, pristine):
                    r["output_file"] = src
                    r["replaced"] = True
                else:
                    # 结果文件在马上要删的临时目录里，写不回源文件就等于这次没出成品，
                    # 不能留着 success 让汇总报"完成"
                    r["status"] = "error"
                    r["error"] = "没能写回源文件（被占用或没存下可回退的原件）"
                continue
            try:
                os.makedirs(output_dir, exist_ok=True)
                # 不同文件夹的同名文件都要留下，第二个起自动加 (2)
                if os.path.exists(dest):
                    dest = self._free_name(dest)
                shutil.move(out, dest)
                r["output_file"] = dest
            except (OSError, shutil.Error) as e:
                r["status"] = "error"
                r["error"] = str(e)
                self.root.after(0, lambda f=os.path.basename(out), err=str(e):
                    self._log(f"  ✗ 移动到输出目录失败: {f} - {err}", color=Theme.ERROR))

    def _process_single_step(self, input_files: List[str], output_dir: str,
                             func_key: str, config, remove_mode: str,
                             progress_callback) -> List[dict]:
        """执行单个功能步骤，返回结果列表"""
        if func_key == "remove_page_numbers":
            return batch_process(
                input_files=input_files, output_dir=output_dir,
                config=config, progress_callback=progress_callback,
                remove_mode=remove_mode
            )
        elif func_key == "pdf_scale":
            page_range = self.page_range_var.get().strip()
            if self._use_fit_spec():
                spec = self._build_fit_spec()
                auto_skip, small_ratio, skip_pages = self._get_fit_skip_params()
                return batch_fit_margins(
                    input_files=input_files, output_dir=output_dir,
                    spec=spec,
                    page_range=page_range,
                    auto_skip_special=auto_skip,
                    small_content_ratio=small_ratio,
                    skip_pages=skip_pages,
                    dpi=config.dpi, progress_callback=progress_callback,
                )
            scale_ratio = self.scale_ratio_var.get() / 100.0
            offset_x = float(self.offset_x_var.get())
            offset_y = float(self.offset_y_var.get())
            return batch_scale(
                input_files=input_files, output_dir=output_dir,
                scale_ratio=scale_ratio, offset_x=offset_x, offset_y=offset_y,
                page_range=page_range, dpi=config.dpi, progress_callback=progress_callback,
            )
        elif func_key == "pdf_rotate":
            direction = self.rotate_direction_var.get()
            auto_detect = self.rotate_auto_var.get()
            page_range = self.page_range_var.get().strip()
            return batch_rotate(
                input_files=input_files, output_dir=output_dir,
                direction=direction, page_range=page_range,
                auto_detect=auto_detect, progress_callback=progress_callback,
            )
        elif func_key == "pdf_resize":
            target_size = self.resize_size_var.get()
            page_range = self.page_range_var.get().strip()
            return batch_resize(
                input_files=input_files, output_dir=output_dir,
                target_size=target_size,
                page_range=page_range, progress_callback=progress_callback,
            )
        elif func_key == "clean_scan":
            page_range = self.page_range_var.get().strip()
            return batch_clean_scan(
                input_files=input_files, output_dir=output_dir,
                page_range=page_range, progress_callback=progress_callback,
            )
        elif func_key == "remove_logo":
            return batch_remove_logos(
                input_files=input_files, output_dir=output_dir,
                cfg=self._build_logo_cfg(),
                page_range=self.page_range_var.get().strip(),
                progress_callback=progress_callback,
            )
        return []

    def _seal_count(self, path: str) -> int:
        """数文件里的电子签章个数。

        金格（GoldGrid:AddSeal）这类自定义签章，PyMuPDF 的 page.annots() 认不出来、
        返回空，只能直接读页面的 /Annots 数组再按子类型筛。
        """
        from pdf_page_remover.scaler import _page_annot_xrefs
        try:
            doc = pymupdf.open(path)
        except Exception:
            return 0
        try:
            total = 0
            for page in doc:
                for xref in _page_annot_xrefs(doc, page):
                    try:
                        obj = doc.xref_object(xref, compressed=True)
                    except Exception:
                        continue
                    if "Seal" in obj or "签章" in obj or "Sign" in obj:
                        total += 1
            return total
        finally:
            doc.close()

    def _audit_seals(self, baseline: Dict[str, int], results: List[dict],
                     origin_of: Optional[Dict[str, str]] = None):
        """处理前记下签章数，处理后逐个对账；掉了的当场点名，不让用户交付时才发现"""
        lost = []
        for r in results:
            if r.get("status") not in ("success", "skipped") or not r.get("output_file"):
                # skipped 也要对账：最后一步跳过某个文件时，它的成品是上一步的产物，
                # 只看 success 的话这一步之前掉的签章就永远查不出来
                continue
            if not os.path.exists(r["output_file"]):
                # 打不开就数出 0 枚，会把"没产出"报成"把章弄丢了"
                continue
            src = self._result_origin(r, origin_of)
            key = os.path.normcase(os.path.abspath(src)) if src else ""
            was = baseline.get(key, 0)
            if was <= 0:
                continue
            now = self._seal_count(r["output_file"])
            if now < was:
                lost.append((os.path.basename(src), was, now))
        if not lost:
            return
        self.root.after(0, lambda: self._log(
            f"⚠ 本次处理弄丢了电子签章，印章不会被别人看见，交付前必须换回原文件：{len(lost)} 个文件",
            color=Theme.ERROR))
        for name, was, now in lost:
            self.root.after(0, lambda n=name, w=was, c=now: self._log(
                f"    {n}: 处理前 {w} 枚签章 → 处理后 {c} 枚", color=Theme.ERROR))
        # 这条必须送到人眼前：窗口收在托盘里时弹窗会开在别人窗户后面，所以先把窗口唤回来
        self.root.after(0, self._restore_window)
        self.root.after(0, lambda: messagebox.showwarning(
            "电子签章丢失",
            "以下文件在本次处理中丢失了电子签章：\n\n"
            + "\n".join(f"{n}（{w} → {c}）" for n, w, c in lost)
            + "\n\n请勿把这些输出交付出去；请从带签章的原始文件重新处理。"))
        self._notify_tray("电子签章丢失", f"{len(lost)} 个文件的签章在这次处理中丢了，成品不要交付")

    def _seal_baseline(self, input_files: List[str]) -> Dict[str, int]:
        """处理前给整批文件记一次签章数，只有带签章的文件才进这张表。

        顺带发现"手里这份已经没有签章、backup/ 里那份却有"的情况：这通常是把处理过的
        成品当成了原件，签章在上一次就已经没了，再怎么重跑也长不回来。
        """
        baseline: Dict[str, int] = {}
        degraded = []
        backup_dir = self._backup_dir()
        manifest = self._load_backup_manifest(backup_dir) if backup_dir else {}
        for p in input_files:
            key = os.path.normcase(os.path.abspath(p))
            try:
                count = self._seal_count(p)
            except Exception:
                continue
            if count > 0:
                baseline[key] = count
                continue
            if backup_dir:
                name = os.path.basename(p)
                old = os.path.join(backup_dir, name)
                # 汇编里各分类都有同名的一本。backup/ 是平铺的，光看文件名会拿别人家的
                # 原件来比，凭空报"你选的这份早就没章了"。有登记就认登记，没登记才认名字
                owner = manifest.get(name)
                if owner and owner != key:
                    continue
                if os.path.isfile(old) and not self._same_content(old, p):
                    try:
                        old_count = self._seal_count(old)
                    except Exception:
                        old_count = 0
                    if old_count > count:
                        degraded.append((os.path.basename(p), old_count))
        for name, count in degraded:
            self.root.after(0, lambda n=name, c=count: self._log(
                f"⚠ {n}：要处理的这一份已经没有电子签章了，backup/ 里的同名文件却有 {c} 枚。"
                f"签章不是本次处理丢的，是选中的输入本身就是以前处理过的成品，"
                f"请改选原始文件（或先从 backup/ 取回）再处理",
                color=Theme.WARNING))
        return baseline

    def _get_fit_margins(self):
        """读取并校验上下距离，返回 (顶部cm, 底部cm)"""
        try:
            top_cm = float(self.fit_top_var.get().strip())
        except ValueError:
            raise ValueError("顶部距离请填写数字（单位厘米）")
        try:
            bottom_cm = float(self.fit_bottom_var.get().strip())
        except ValueError:
            raise ValueError("底部距离请填写数字（单位厘米）")
        if top_cm <= 0:
            raise ValueError("顶部距离必须大于0")
        if bottom_cm <= 0:
            raise ValueError("底部距离必须大于0")
        if top_cm > 20 or bottom_cm > 20:
            raise ValueError("距离超过20cm，请检查是否填错了单位")
        return top_cm, bottom_cm

    def _get_fit_skip_params(self):
        """读取特殊页跳过设置，返回 (是否自动跳过, 内容高度阈值%, 手动跳过页码串)"""
        auto_skip = bool(self.fit_auto_skip_var.get())
        raw = self.fit_small_var.get().strip()
        try:
            small_ratio = float(raw) if raw else DEFAULT_SMALL_CONTENT_RATIO
        except ValueError:
            raise ValueError("印发说明页内容高度阈值请填写数字（百分比）")
        if not 0 < small_ratio < 100:
            raise ValueError("印发说明页内容高度阈值要在0~100之间（百分比）")
        skip_pages = self.fit_skip_pages_var.get().strip()
        return auto_skip, small_ratio, skip_pages

    def _build_logo_cfg(self) -> LogoConfig:
        """界面上的四个输入 → 标识检测参数；填错当场说清楚，别跑到一半才崩"""
        def num(name, raw, lo, hi, unit):
            try:
                v = float(raw)
            except ValueError:
                raise ValueError(f"{name}请填写数字（{unit}）")
            if not lo <= v <= hi:
                raise ValueError(f"{name}要在 {lo}~{hi} {unit}之间")
            return v

        zone_w = num("搜索区宽", self.logo_zone_w_var.get().strip() or "9", 2.0, 21.0, "厘米")
        zone_h = num("搜索区高", self.logo_zone_h_var.get().strip() or "4", 1.0, 29.7, "厘米")
        presence = num("跨页一致比例", self.logo_presence_var.get().strip() or "50",
                       34.0, 100.0, "%") / 100.0
        return LogoConfig(anchor=self.logo_anchor_var.get(),
                          zone_w_cm=zone_w, zone_h_cm=zone_h, presence_ratio=presence,
                          protect_seals=self.check_seal_var.get(),
                          whole_row=self.logo_whole_row_var.get())

    def _process_thread(self, input_files: List[str], output_dir: str,
                         selected_funcs: List[str], remove_mode: str,
                         replace_source: bool = False):
        replace_temp_dir = output_dir if replace_source else None
        self._replace_summary = None
        stage_dir = None
        try:
            config = self._get_config()
            # 处理前先记下这批文件的签章数，处理完拿来对账（见 _audit_seals）
            seal_baseline = self._seal_baseline(input_files)
            # 上次已经改动过源文件的（替换或原地覆盖），重新生成时用最初的原文件当输入，不叠加
            step_inputs = self._pipeline_inputs(input_files)
            # 不同文件夹的同名文件在库层会写到同一个输出路径上互相覆盖，先按序号暂存
            step_inputs, origin_of, stage_dir = self._stage_unique_names(step_inputs)

            # 单功能模式：直接处理
            if len(selected_funcs) == 1:
                func_key = selected_funcs[0]

                def progress_callback(file_index, total_files, page_index, total_pages, message):
                    if not self.is_processing:
                        return
                    # 空 PDF（0 页）也会走到这里回调，除零会把已经做好的结果标成失败
                    file_progress = (file_index / max(total_files, 1)) * 100
                    page_progress = (page_index / max(total_pages, 1)) * (100 / max(total_files, 1))
                    total_progress = file_progress + page_progress
                    self.root.after(0, lambda: self._update_progress(total_progress, message))

                results = self._run_step(
                    func_key, step_inputs, output_dir, config, remove_mode, progress_callback,
                    origin_of, stage_dir is not None
                )
                self._passthrough_skipped(results, output_dir, origin_of)
                # 替换源文件
                if replace_source and self.is_processing:
                    self._replace_source_files(results, origin_of)
                elif replace_source:
                    self.root.after(0, lambda: self._log(
                        "已停止：本次结果不写回源文件", color=Theme.WARNING))
                # 签章对账：这一趟有没有把电子印章弄丢
                if self.is_processing:
                    self._audit_seals(seal_baseline, results, origin_of)
                # 显示结果
                if func_key == "remove_page_numbers":
                    self.root.after(0, lambda: self._show_results_remove(results))
                elif func_key == "pdf_scale":
                    if self._use_fit_spec():
                        spec = self._build_fit_spec()
                        self.root.after(0, lambda s=spec: self._show_results_fit(results, s))
                    else:
                        scale_ratio = self.scale_ratio_var.get() / 100.0
                        offset_x = float(self.offset_x_var.get())
                        offset_y = float(self.offset_y_var.get())
                        self.root.after(0, lambda: self._show_results_scale(results, scale_ratio, offset_x, offset_y))
                elif func_key == "pdf_rotate":
                    direction = self.rotate_direction_var.get()
                    auto_detect = self.rotate_auto_var.get()
                    self.root.after(0, lambda: self._show_results_rotate(results, direction, auto_detect))
                elif func_key == "pdf_resize":
                    target_size = self.resize_size_var.get()
                    self.root.after(0, lambda: self._show_results_resize(results, target_size))
                elif func_key == "clean_scan":
                    self.root.after(0, lambda: self._show_results_clean(results))
                elif func_key == "remove_logo":
                    cfg = self._build_logo_cfg()
                    self.root.after(0, lambda c=cfg: self._show_results_logo(results, c))
                return

            # 多功能组合模式：流水线处理
            pipeline_results = []  # [{func_key, results}]
            current_files = step_inputs[:]
            temp_dirs = []

            try:
                for step_idx, func_key in enumerate(selected_funcs):
                    if not self.is_processing:
                        break

                    step_label = {
                        "remove_page_numbers": "去除页码",
                        "pdf_scale": self._scale_mode_short(),
                        "pdf_rotate": "自动旋转",
                        "pdf_resize": "尺寸调整",
                        "clean_scan": "扫描件去瑕疵",
                        "remove_logo": "去除页眉Logo",
                    }.get(func_key, func_key)

                    self.root.after(0, lambda lbl=step_label, si=step_idx, total=len(selected_funcs):
                        self._update_progress(
                            (si / total) * 100,
                            f"[{si+1}/{total}] 正在{lbl}..."
                        ))

                    # 最后一步输出到目标目录，中间步骤输出到临时目录
                    is_last_step = (step_idx == len(selected_funcs) - 1)
                    if is_last_step:
                        step_output_dir = output_dir
                    else:
                        temp_dir = tempfile.mkdtemp(prefix="pdf_pipeline_")
                        temp_dirs.append(temp_dir)
                        step_output_dir = temp_dir

                    def progress_callback(file_index, total_files, page_index, total_pages, message,
                                          si=step_idx, total=len(selected_funcs), lbl=step_label):
                        if not self.is_processing:
                            return
                        step_base = (si / total) * 100
                        step_progress = ((file_index + page_index / max(total_pages, 1)) / total_files) * (100 / total)
                        self.root.after(0, lambda: self._update_progress(
                            step_base + step_progress, f"[{si+1}/{total}] {lbl} - {message}"))

                    results = self._run_step(
                        func_key, current_files, step_output_dir, config, remove_mode,
                        progress_callback, origin_of, stage_dir is not None
                    )
                    if is_last_step:
                        self._passthrough_skipped(results, step_output_dir, origin_of)
                    pipeline_results.append({"func_key": func_key, "label": step_label, "results": results})

                    # 检查是否有成功的结果，收集输出文件作为下一步输入
                    if not is_last_step:
                        next_files = []
                        for r in results:
                            if r.get("status") == "success" and r.get("output_file"):
                                # 记下这个临时输出对应哪个最初的源文件，失败的文件会被跳过
                                origin_of[r["output_file"]] = self._result_origin(r, origin_of)
                                next_files.append(r["output_file"])
                            elif r.get("status") == "skipped" and r.get("input_file"):
                                # "这一步没什么可做的"不是失败：这本压根没有页眉标识、
                                # 没有页码，照样得往下走，否则后面选中的功能全落在这本头上
                                # （安全管理类那两本就因为这里被踢出流水线，没缩放没去页码）
                                next_files.append(r["input_file"])
                        if not next_files:
                            self.root.after(0, lambda: self._log(
                                f"步骤 {step_idx+1} ({step_label}) 无成功输出，流水线终止。",
                                color=Theme.WARNING))
                            break
                        current_files = next_files

            finally:
                # 清理流水线临时目录
                for temp_dir in temp_dirs:
                    try:
                        shutil.rmtree(temp_dir, ignore_errors=True)
                    except Exception:
                        pass

            # 替换源文件
            if replace_source and pipeline_results:
                if self.is_processing:
                    self._replace_source_files(pipeline_results[-1].get("results", []), origin_of)
                else:
                    self.root.after(0, lambda: self._log(
                        "已停止：本次结果不写回源文件", color=Theme.WARNING))

            # 显示组合处理结果
            if self.is_processing and pipeline_results:
                self._audit_seals(seal_baseline,
                                  pipeline_results[-1].get("results", []), origin_of)
            self.root.after(0, lambda: self._show_results_pipeline(pipeline_results))

        except Exception as e:
            self.root.after(0, lambda: self._log(f"处理出错: {e}", color=Theme.ERROR))
            self.root.after(0, lambda: messagebox.showerror("错误", f"处理过程中出错:\n{e}"))
        finally:
            # 清理替换模式的临时输出目录
            if replace_temp_dir and os.path.exists(replace_temp_dir):
                try:
                    shutil.rmtree(replace_temp_dir, ignore_errors=True)
                except Exception:
                    pass
            if stage_dir:
                shutil.rmtree(stage_dir, ignore_errors=True)
            self.root.after(0, self._process_finished)

    def _replace_source_files(self, results: List[dict], origin_of: Optional[Dict[str, str]] = None):
        """替换源文件：把处理结果写回它自己的源文件路径

        源文件按结果里的 input_file 回溯（流水线的临时文件会顺藤摸到最初那个），
        不能按结果的下标去对输入列表——某一步有文件失败时两边个数不一样，会张冠李戴。
        backup目录里存的始终是"最初的原文件"，所以同一批文件可以反复重新生成而不会叠加。
        """
        replaced_count = 0
        reasons = {}
        self.root.after(0, lambda: self._log("正在替换源文件...", color=Theme.ACCENT))
        backup_dir = self._backup_dir()

        def deliverable(r: dict) -> bool:
            """这条结果是不是有资格写回源文件

            skipped 一般表示"这本不用动"，覆盖回去纯属找麻烦（源文件可能正被占用）。
            只有 _passthrough_skipped 那种——前面几步确实改了、最后一步没活干——才真要写回，
            否则前面做好的成品会连同临时目录一起被删掉。
            """
            if not r.get("output_file"):
                return False
            return r.get("status") == "success" or bool(r.get("passthrough"))

        # 有几个结果是有资格写回源文件的（转Word 之类产物在下面会单独走"放到旁边"）
        eligible = [r for r in results
                    if deliverable(r)
                    and os.path.splitext(r["output_file"])[1].lower()
                    == os.path.splitext(self._result_origin(r, origin_of) or "")[1].lower()]

        for i, r in enumerate(results):
            if not deliverable(r):
                continue

            output_file = r["output_file"]
            input_file = self._result_origin(r, origin_of)

            if not input_file:
                self.root.after(0, lambda: self._log("  ⚠ 跳过: 无法确定源文件路径", color=Theme.WARNING))
                continue

            if not os.path.exists(output_file):
                self.root.after(0, lambda ef=os.path.basename(output_file):
                    self._log(f"  ⚠ 跳过: 输出文件不存在 ({ef})", color=Theme.WARNING))
                continue

            # 转Word 之类产物和源文件格式不同：覆盖过去等于把 PDF 变成一堆 docx 字节
            src_ext = os.path.splitext(input_file)[1].lower()
            out_ext = os.path.splitext(output_file)[1].lower()
            if src_ext and out_ext and src_ext != out_ext:
                beside = self._free_name(self._output_path_for(
                    os.path.dirname(input_file), input_file, output_file))
                try:
                    shutil.move(output_file, beside)
                    r["output_file"] = beside
                    self.root.after(0, lambda f=os.path.basename(beside): self._log(
                        f"  ↪ {f} 与源文件格式不同，没有覆盖源文件，已放到源文件旁边",
                        color=Theme.WARNING))
                except (OSError, shutil.Error) as e:
                    r["status"] = "error"
                    r["error"] = str(e)
                    self.root.after(0, lambda f=os.path.basename(output_file), err=str(e):
                        self._log(f"  ✗ 保存转换结果失败: {f} - {err}", color=Theme.ERROR))
                continue

            try:
                why = self._write_block(input_file)
                if why == "readonly":
                    if self._clear_readonly(input_file):
                        self.root.after(0, lambda f=os.path.basename(input_file): self._log(
                            f"  · {f} 被设成“只读”，已去掉只读属性再写回", color=Theme.WARNING))
                        why = None
                    else:
                        why = self._write_block(input_file) or "locked"
                if why:
                    raise PermissionError("文件正被其他程序占用" if why == "locked"
                                          else "文件被设成“只读”，且去掉只读属性没成功")
                # 第一次覆盖前，把"当前的内容"（也就是最初的原文件）留一份
                pristine = self._keep_original(input_file)
                if self._write_over_source(output_file, input_file, backup_dir, pristine):
                    r["output_file"] = input_file
                    r["replaced"] = True
                    replaced_count += 1
                else:
                    reasons[os.path.basename(input_file)] = "几种覆盖方式都没成功"
            except Exception as e:
                fn = os.path.basename(input_file)
                reasons[fn] = str(e)
                self.root.after(0, lambda f=fn, err=str(e):
                    self._log(f"  ✗ 替换源文件失败: {f} - {err}", color=Theme.ERROR))

        # 有资格写回、最后却没写成的那些，必须让他看见（窗口在托盘时日志根本看不到）
        missed = [os.path.basename(self._result_origin(r, origin_of) or r.get("output_file", ""))
                  for r in eligible if not r.get("replaced")]
        where = os.path.dirname(self.input_files[0]) if self.input_files else ""
        if not eligible:
            self._replace_summary = "本次产物不覆盖源文件（格式不同或没有成功结果）"
        elif not missed:
            self._replace_summary = f"已替换 {replaced_count}/{len(eligible)} 个源文件（{where}）"
        elif replaced_count:
            self._replace_summary = (f"只写回了 {replaced_count}/{len(eligible)} 个源文件，"
                                     f"{len(missed)} 个没写进去")
        else:
            self._replace_summary = f"源文件没有被改动（{len(missed)} 个都写不进去）"

        if replaced_count > 0:
            self.root.after(0, lambda: self._log(
                f"已替换 {replaced_count}/{len(eligible)} 个源文件", color=Theme.ACCENT))
        else:
            self.root.after(0, lambda: self._log(
                "未能替换任何源文件", color=Theme.WARNING))
        if missed:
            detail = "\n".join("  · " + n + (f"（{reasons[n]}）" if reasons.get(n) else "")
                               for n in missed[:8])
            if len(missed) > 8:
                detail += f"\n  · …共 {len(missed)} 个"
            text = (f"处理结果没有写回这些源文件，你打开的还是会看到旧内容：\n{detail}\n\n"
                    f"文件所在目录：{where}\n"
                    "最常见的原因是它们正被别的程序占用（InDesign / CorelDRAW 置入、"
                    "WPS 或 Acrobat 预览），或是文件被设成了“只读”（右键→属性→去掉只读）。"
                    "关掉那些程序后重新点“开始处理”就行，不会在上次的结果上叠加。")
            self.root.after(0, lambda t=text: messagebox.showwarning("有源文件没被替换", t))

    def _atomic_copy_over(self, src: str, dst: str):
        """把 src 的内容整块换上 dst：先写同目录临时文件，再 os.replace 换上去。

        直接往目标文件名上写，磁盘满、断电、被杀毒软件拦一半时会留下半份 PDF，
        源文件本身就没了；换成临时文件+改名，失败时目标文件保持原样。
        """
        tmp = dst + ".写回中.tmp"
        try:
            shutil.copy2(src, tmp)
            if os.path.getsize(tmp) != os.path.getsize(src):
                raise OSError("写回内容长度对不上，放弃覆盖")
            os.replace(tmp, dst)
        except OSError:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise

    def _write_over_source(self, output_file: str, source_file: str,
                           backup_dir: Optional[str] = None,
                           pristine: Optional[str] = None) -> bool:
        """用处理结果覆盖源文件，成功返回 True

        backup_dir 里写入的是 pristine（最初的原文件），不是上一次替换后的结果，
        这样多次生成后仍能量回第一次的参数重做。
        """
        fn = os.path.basename(source_file)
        # 同名文件在好几个文件夹里各有一份，日志不点出目录就分不清写回的是哪一份
        shown = f"{fn}  ← {os.path.dirname(source_file)}"
        if not (pristine and os.path.exists(pristine)):
            # 回退用的原件快照没存下来时绝不能动源文件：写坏了连还原的机会都没有
            self.root.after(0, lambda f=fn: self._log(
                f"  ✗ 没存下可回退的原件，为安全没有写回: {f}", color=Theme.ERROR))
            return False
        if backup_dir:
            backup_path = os.path.join(backup_dir, fn)
            try:
                if os.path.exists(backup_path) and not self._same_content(backup_path, pristine):
                    # 不同文件夹里的同名文件：备份不能互相顶掉
                    backup_path = self._free_name(backup_path)
                if os.path.exists(backup_path):
                    # backup/ 里已有的那份更早、更接近原始文件，不能被本次结果盖掉
                    self.root.after(0, lambda f=fn: self._log(
                        f"  ↩ backup/ 已有 {f}，保留更早的那份", color=Theme.FG_DIM))
                else:
                    shutil.copy2(pristine, backup_path)
                    self._record_backup_origin(backup_dir, os.path.basename(backup_path),
                                               source_file)
                    self.root.after(0, lambda f=os.path.basename(backup_path):
                        self._log(f"  📦 已备份: {f}", color=Theme.FG))
            except OSError as backup_err:
                self.root.after(0, lambda f=fn, e=str(backup_err):
                    self._log(f"  ⚠ 备份失败({e}): {f}", color=Theme.WARNING))

        try:
            # 方案1：临时文件 + 整块换上（不会留下写了一半的源文件）
            self._atomic_copy_over(output_file, source_file)
            self.root.after(0, lambda f=shown: self._log(f"  ✓ 已替换源文件: {f}", color=Theme.SUCCESS))
            return True
        except (PermissionError, OSError) as copy_err:
            # 方案2：源文件被别的程序占住，换不上，只能先让它让路
            self.root.after(0, lambda f=fn, e=str(copy_err):
                self._log(f"  ⚠ 直接覆盖失败({e})，尝试让路后写回: {f}", color=Theme.WARNING))

        import time
        for attempt in range(3):
            try:
                time.sleep(0.3)
                self._atomic_copy_over(output_file, source_file)
                self.root.after(0, lambda f=shown: self._log(f"  ✓ 已替换源文件(重试{attempt+1}): {f}", color=Theme.SUCCESS))
                return True
            except (PermissionError, OSError) as retry_err:
                if attempt == 2:
                    self.root.after(0, lambda f=fn, err=str(retry_err):
                        self._log(f"  ✗ 替换源文件失败: {f} - {err}", color=Theme.ERROR))
                    break
                # 方案3：把源文件改名让路，复制完再删掉让路文件
                backup = None
                try:
                    if os.path.exists(source_file):
                        # 上次失败留下的 .bak 可能是用户仅剩的那份，不覆盖它，另开一个名字
                        backup = self._free_name(source_file + ".bak")
                        os.rename(source_file, backup)
                    self._atomic_copy_over(output_file, source_file)
                    self.root.after(0, lambda f=shown: self._log(f"  ✓ 已替换源文件(备份方式): {f}", color=Theme.SUCCESS))
                    if backup:
                        try:
                            os.remove(backup)
                        except OSError:
                            self.root.after(0, lambda b=os.path.basename(backup):
                                self._log(f"  ⚠ 让路文件没删掉（可手动清理）: {b}", color=Theme.WARNING))
                    return True
                except OSError:
                    # 复制没成功就把让路文件改回去，绝不能让用户以为文件不见了
                    if backup and not os.path.exists(source_file):
                        try:
                            os.rename(backup, source_file)
                        except OSError:
                            pass
                    continue

        # 上面几招都可能先把源文件删掉/改名，失败时至少把内容还原回去
        if not os.path.exists(source_file) and pristine and os.path.exists(pristine):
            try:
                shutil.copy2(pristine, source_file)
                self.root.after(0, lambda f=fn: self._log(
                    f"  ⤺ 已把 {f} 还原成处理前的样子（本次未改动）", color=Theme.WARNING))
            except OSError:
                self.root.after(0, lambda f=fn: self._log(
                    f"  ✗ 替换失败且原文件已不在，请到 backup/ 取回: {f}", color=Theme.ERROR))
        return False

    # ========== 原地覆盖与"最初的原文件" ==========
    # 这些功能的输出文件名和输入一样，所以输出目录是源文件所在目录时等于覆盖源文件
    IN_PLACE_STEPS = ("remove_page_numbers", "pdf_scale", "pdf_rotate", "pdf_resize",
                      "clean_scan", "remove_logo")

    @staticmethod
    def _is_same_file(a: str, b: str) -> bool:
        if not a or not b:
            return False
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

    @staticmethod
    def _same_content(a: str, b: str) -> bool:
        """是不是同一份内容（备份用 copy2 保留过 mtime，所以比大小+修改时间就够）"""
        try:
            sa, sb = os.stat(a), os.stat(b)
        except OSError:
            return False
        return sa.st_size == sb.st_size and sa.st_mtime_ns == sb.st_mtime_ns

    @staticmethod
    def _free_name(path: str) -> str:
        """path 已被占用时，返回同目录下不冲突的名字（name (2).ext 这种）"""
        if not os.path.exists(path):
            return path
        root_, ext = os.path.splitext(path)
        n = 2
        while os.path.exists(f"{root_} ({n}){ext}"):
            n += 1
        return f"{root_} ({n}){ext}"

    def _source_of(self, path: str) -> str:
        """输入若是'最初的原文件'快照，返回它对应的真实源文件路径

        重复生成时输入被换成了快照，但结果仍然要写回用户列表里那个文件。
        """
        key = os.path.normcase(os.path.abspath(path)) if path else ""
        return self._snapshot_originals.get(key, path)

    def _origin_of_path(self, path: str, origin_of: Optional[Dict[str, str]] = None) -> str:
        """流水线中间输出 → 用户列表里那个真实源文件

        中间步骤的输出在临时目录里，顺着 origin_of 往回找，再把"重复生成时用的快照"
        翻译回真实路径。
        """
        origin_of = origin_of or {}
        seen = set()
        while path in origin_of and path not in seen:
            seen.add(path)
            path = origin_of[path]
        return self._source_of(path)

    def _result_origin(self, r: dict, origin_of: Optional[Dict[str, str]] = None) -> str:
        """这条结果最终该写回哪个源文件"""
        return self._origin_of_path(r.get("input_file", ""), origin_of)

    def _has_in_place_target(self, files: List[str], output_dir: str,
                             origin_of: Optional[Dict[str, str]] = None) -> bool:
        """这一步是否会把自己的输入文件原地覆盖掉"""
        for p in files:
            src = self._origin_of_path(p, origin_of)
            if self._is_same_file(self._output_path_for(output_dir, src, src), src):
                return True
        return False

    @staticmethod
    def _output_path_for(output_dir: str, source: str, produced: str) -> str:
        """结果该落到输出目录的哪个名字：用源文件的名字，扩展名跟实际产物走（转Word 是 .docx）"""
        stem = os.path.splitext(os.path.basename(source))[0]
        ext = os.path.splitext(produced)[1] or os.path.splitext(source)[1]
        return os.path.join(output_dir, f"{stem}{ext}")

    def _backup_dir(self) -> Optional[str]:
        """应用目录下的 backup/：存最初的原文件，供反复生成后回退"""
        backup_dir = os.path.join(_app_home(), "backup")
        try:
            os.makedirs(backup_dir, exist_ok=True)
        except OSError:
            self.root.after(0, lambda: self._log("  ⚠ 无法创建备份目录，跳过备份", color=Theme.WARNING))
            return None
        return backup_dir

    @staticmethod
    def _backup_manifest_path(backup_dir: str) -> str:
        return os.path.join(backup_dir, "_来源.json")

    def _load_backup_manifest(self, backup_dir: str) -> Dict[str, str]:
        """backup/ 里每个文件当初是从哪儿存下来的（老备份没登记，返回里没有它）"""
        try:
            with open(self._backup_manifest_path(backup_dir), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _record_backup_origin(self, backup_dir: str, backup_name: str, source_path: str):
        data = self._load_backup_manifest(backup_dir)
        data[backup_name] = os.path.normcase(os.path.abspath(source_path))
        tmp = self._backup_manifest_path(backup_dir) + ".写回中.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._backup_manifest_path(backup_dir))
        except OSError:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def _snapshot_root(self) -> str:
        if not self._originals_dir or not os.path.isdir(self._originals_dir):
            self._originals_dir = tempfile.mkdtemp(prefix="pdf_tool_originals_")
            self._pristine_snapshots = {}
            self._snapshot_originals = {}
            self._snapshot_seq = 0
            try:
                # 留个主人标记：另开一份时启动清理只删已经没人用的目录
                with open(os.path.join(self._originals_dir, "owner.pid"), "w",
                          encoding="ascii") as f:
                    f.write(str(os.getpid()))
            except OSError:
                pass
        return self._originals_dir

    def _keep_original(self, source_path: str) -> Optional[str]:
        """覆盖源文件前留一份"最初的内容"；已有快照时直接返回，不会被后续结果污染"""
        key = os.path.normcase(os.path.abspath(source_path))
        snap = self._pristine_snapshots.get(key)
        if snap and os.path.exists(snap):
            return snap
        if not os.path.exists(source_path):
            return None
        # 快照放编号子目录，文件名保持原样，日志里才不会显示成改过名的文件
        target_dir = os.path.join(self._snapshot_root(), str(self._snapshot_seq))
        target = os.path.join(target_dir, os.path.basename(source_path))
        try:
            os.makedirs(target_dir, exist_ok=True)
            shutil.copy2(source_path, target)
        except OSError as e:
            self.root.after(0, lambda err=str(e):
                self._log(f"  ⚠ 无法留存原文件（{err}），下次生成将基于当前文件", color=Theme.WARNING))
            return None
        self._snapshot_seq += 1
        self._pristine_snapshots[key] = target
        # 反向表：输入被换成快照后，结果仍要知道该写回哪个真实源文件
        self._snapshot_originals[os.path.normcase(os.path.abspath(target))] = source_path
        return target

    def _stage_unique_names(self, files: List[str]):
        """文件名有重复时先复制到临时目录并加序号，避免库层输出互相覆盖

        处理库的输出名就是"输出目录 + 输入文件名"，两个不同文件夹的同名 PDF 会写到
        同一个路径上，后处理的把先处理的顶掉，替换源文件时两份源文件拿到同一份结果。
        返回 (实际要处理的文件, 暂存路径→真实源文件, 暂存目录或None)。
        """
        names = [os.path.basename(p) for p in files]
        # Windows 认大小写不敏感：X.pdf 和 X.PDF 是同一个文件，必须算同名
        keyed = [os.path.normcase(n) for n in names]
        if len(set(keyed)) == len(keyed):
            return files, {}, None
        stage_dir = tempfile.mkdtemp(prefix="pdf_stage_")
        staged: List[str] = []
        origin: Dict[str, str] = {}
        for i, p in enumerate(files):
            target = os.path.join(stage_dir, f"{i:03d}_{os.path.basename(p)}")
            try:
                shutil.copy2(p, target)
            except OSError as e:
                # 暂存不了就退回原样处理（最多是老行为），别让整批失败
                self.root.after(0, lambda f=os.path.basename(p), err=str(e): self._log(
                    f"  ⚠ 无法暂存同名文件 {f}（{err}）", color=Theme.WARNING))
                staged.append(p)
                continue
            origin[target] = self._origin_of_path(p)
            staged.append(target)
        dup = sorted({names[i] for i, k in enumerate(keyed) if keyed.count(k) > 1})
        self.root.after(0, lambda: self._log(
            f"检测到同名文件（{'、'.join(dup)}）：已加序号暂存，结果各自写回自己的源文件",
            color=Theme.WARNING))
        return staged, origin, stage_dir

    def _pipeline_inputs(self, input_files: List[str]) -> List[str]:
        """源文件已被改动过时，重复生成用首次留下的原文件当输入，不在上次结果上叠加"""
        files = []
        replayed = 0
        for p in input_files:
            snap = self._pristine_snapshots.get(os.path.normcase(os.path.abspath(p)))
            if snap and os.path.exists(snap):
                files.append(snap)
                replayed += 1
            else:
                files.append(p)
        if replayed:
            self.root.after(0, lambda n=replayed:
                self._log(f"本次从最初的原文件重新生成（不叠加上次的改动）：{n} 个文件",
                          color=Theme.ACCENT))
        return files

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """进程还活着没有。Windows 用 OpenProcess 查，不能用 os.kill（那一下真会打死人）。"""
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return True  # 查不了就当还活着：宁可留一个目录，不能删掉别人的退路

    @staticmethod
    def _snapshot_owner_alive(path: str) -> bool:
        """快照目录里写了 owner.pid，主人还在的目录不能动"""
        try:
            with open(os.path.join(path, "owner.pid"), encoding="ascii") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return False  # 没有标记的是老版本留下的，按异常退出处理
        return pid != os.getpid() and PDFPageNumberRemoverGUI._pid_alive(pid)

    @staticmethod
    def _sweep_stale_snapshots():
        """启动时清掉本工具异常退出留下的快照目录。

        只清主人进程已经不在的那些：他又双击开了一份的话，
        把正在跑的那份存的原文件快照删掉，写回时就没有退路了。
        """
        tmp = tempfile.gettempdir()
        try:
            names = os.listdir(tmp)
        except OSError:
            return
        for name in names:
            if name.startswith("pdf_tool_originals_"):
                path = os.path.join(tmp, name)
                if PDFPageNumberRemoverGUI._snapshot_owner_alive(path):
                    continue
                shutil.rmtree(path, ignore_errors=True)


    def _stop_process(self):
        self.is_processing = False
        self._log("正在停止处理...（这一批会跑完，但结果不会写回源文件）", color=Theme.WARNING)
        # 不在这里放开"开始"：工作线程还在跑，等它自己走 _process_finished
        self.btn_stop.config(state=tk.DISABLED, bg="#585b70")
        self.status_icon.config(fg=Theme.WARNING)
        self.status_label.config(text="正在停止...", fg=Theme.WARNING)

    def _process_finished(self):
        self.is_processing = False
        self.btn_start.config(state=tk.NORMAL, bg=Theme.ACCENT)
        self.btn_stop.config(state=tk.DISABLED, bg="#585b70")
        self.status_icon.config(fg=Theme.SUCCESS)

    def _update_progress(self, progress: float, message: str):
        self.progress_var.set(progress)
        self.status_label.config(text=message)

    def _show_results_remove(self, results: List[dict]):
        """显示页码去除结果"""
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("处理结果汇总", color=Theme.ACCENT)
        self._log("━" * 48, color=Theme.FG_DIM)

        total = len(results)
        success = sum(1 for r in results if r.get("status") == "success")
        removed = sum(r.get("removed_count", 0) for r in results if r.get("status") == "success")

        for r in results:
            fn = os.path.basename(r.get("input_file", ""))
            if r.get("status") == "success":
                self._log(f"  {fn}", color=Theme.SUCCESS)
                self._log(f"    去除页码: {r.get('removed_count', 0)}  "
                         f"(文本: {r.get('text_pages', 0)}, 扫描: {r.get('scanned_pages', 0)})",
                         color=Theme.FG_DIM)
            else:
                self._log(f"  {fn}", color=Theme.ERROR)
                self._log(f"    失败: {r.get('error', '未知错误')}", color=Theme.FG_DIM)

        self._log("─" * 48, color=Theme.FG_DIM)
        self._log(f"总计: {total} 文件 | 成功 {success} | 去除 {removed} 页码",
                  color=Theme.ACCENT)
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("")

        self.progress_var.set(100)
        self.status_label.config(text=f"处理完成！共去除 {removed} 个页码", fg=Theme.SUCCESS)
        self.status_icon.config(fg=Theme.SUCCESS)

        messagebox.showinfo(
            "处理完成",
            f"已完成处理！\n\n文件总数: {total}\n成功: {success}\n共去除页码: {removed}\n\n"
            f"输出目录: {self._get_output_location_text()}"
        )

    def _show_results_scale(self, results: List[dict], scale_ratio: float,
                            offset_x: float = 0.0, offset_y: float = 0.0):
        """显示缩放结果"""
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("缩放结果汇总", color=Theme.ACCENT)
        self._log("━" * 48, color=Theme.FG_DIM)

        total = len(results)
        success = sum(1 for r in results if r.get("status") == "success")
        skipped = sum(1 for r in results if r.get("status") == "skipped")
        total_pages = sum(r.get("total_pages", 0) for r in results if r.get("status") == "success")

        # 构建偏移描述
        offset_desc = ""
        if offset_x != 0 or offset_y != 0:
            offset_desc = f" | 偏移: ({offset_x:.0f}, {offset_y:.0f})pt"

        for r in results:
            fn = os.path.basename(r.get("input_file", ""))
            if r.get("status") == "success":
                self._log(f"  {fn}", color=Theme.SUCCESS)
                self._log(f"    页数: {r.get('total_pages', 0)} | 缩放比例: {scale_ratio:.0%}{offset_desc}",
                         color=Theme.FG_DIM)
            elif r.get("status") == "skipped":
                self._log(f"  {fn}", color=Theme.WARNING)
                self._log(f"    跳过: {r.get('message', '无需处理')}", color=Theme.FG_DIM)
            else:
                self._log(f"  {fn}", color=Theme.ERROR)
                self._log(f"    失败: {r.get('error', '未知错误')}", color=Theme.FG_DIM)

        self._log("─" * 48, color=Theme.FG_DIM)
        summary = f"总计: {total} 文件 | 成功 {success}"
        if skipped > 0:
            summary += f" | 跳过 {skipped}"
        summary += f" | 缩放比例 {scale_ratio:.0%}{offset_desc}"
        if total_pages > 0:
            summary += f" | 共 {total_pages} 页"
        self._log(summary, color=Theme.ACCENT)
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("")

        self.progress_var.set(100)
        status_msg = f"缩放完成！比例 {scale_ratio:.0%}{offset_desc}，{success}/{total} 个文件成功"
        self.status_label.config(text=status_msg, fg=Theme.SUCCESS)
        self.status_icon.config(fg=Theme.SUCCESS)

        # 构建结果对话框信息
        offset_info = f"\n偏移: 水平{offset_x:.0f}pt / 垂直{offset_y:.0f}pt" if (offset_x != 0 or offset_y != 0) else ""
        messagebox.showinfo(
            "缩放完成",
            f"已完成缩放！\n\n缩放比例: {scale_ratio:.0%}{offset_info}\n文件总数: {total}\n"
            f"成功: {success}\n"
            + (f"跳过: {skipped}\n" if skipped > 0 else "")
            + f"总页数: {total_pages}\n\n"
            f"输出目录: {self._get_output_location_text()}"
        )

    def _show_results_rotate(self, results: List[dict], direction: str, auto_detect: bool):
        """显示旋转结果"""
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("旋转结果汇总", color=Theme.ACCENT)
        self._log("━" * 48, color=Theme.FG_DIM)

        total = len(results)
        success = sum(1 for r in results if r.get("status") == "success")
        total_rotated = sum(r.get("rotated_count", 0) for r in results if r.get("status") == "success")
        total_landscape = sum(r.get("landscape_count", 0) for r in results if r.get("status") == "success")

        dir_label = "顺时针" if direction == "clockwise" else "逆时针"
        mode_label = "自动检测横向" if auto_detect else "指定页面"

        for r in results:
            fn = os.path.basename(r.get("input_file", ""))
            if r.get("status") == "success":
                self._log(f"  {fn}", color=Theme.SUCCESS)
                self._log(f"    总页数: {r.get('total_pages', 0)} | 横向: {r.get('landscape_count', 0)} | 已旋转: {r.get('rotated_count', 0)}",
                         color=Theme.FG_DIM)
            elif r.get("status") == "skipped":
                self._log(f"  {fn}", color=Theme.WARNING)
                self._log(f"    跳过: {r.get('message', '无需处理')}", color=Theme.FG_DIM)
            else:
                self._log(f"  {fn}", color=Theme.ERROR)
                self._log(f"    失败: {r.get('error', '未知错误')}", color=Theme.FG_DIM)

        self._log("─" * 48, color=Theme.FG_DIM)
        self._log(f"总计: {total} 文件 | 成功 {success} | 旋转 {total_rotated} 页 | {dir_label} | {mode_label}",
                 color=Theme.ACCENT)
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("")

        self.progress_var.set(100)
        self.status_label.config(text=f"旋转完成！{dir_label}旋转 {total_rotated} 页", fg=Theme.SUCCESS)
        self.status_icon.config(fg=Theme.SUCCESS)

        messagebox.showinfo(
            "旋转完成",
            f"已完成旋转！\n\n方向: {dir_label}\n模式: {mode_label}\n文件总数: {total}\n"
            f"成功: {success}\n旋转页数: {total_rotated}\n\n"
            f"输出目录: {self._get_output_location_text()}"
        )

    def _show_results_resize(self, results: List[dict], target_size: str):
        """显示尺寸调整结果"""
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("尺寸调整结果汇总", color=Theme.ACCENT)
        self._log("━" * 48, color=Theme.FG_DIM)

        total = len(results)
        success = sum(1 for r in results if r.get("status") == "success")
        total_resized = sum(r.get("resized_count", 0) for r in results if r.get("status") == "success")

        for r in results:
            fn = os.path.basename(r.get("input_file", ""))
            if r.get("status") == "success":
                self._log(f"  {fn}", color=Theme.SUCCESS)
                self._log(f"    总页数: {r.get('total_pages', 0)} | 已调整: {r.get('resized_count', 0)} | 目标: {target_size}",
                         color=Theme.FG_DIM)
            else:
                self._log(f"  {fn}", color=Theme.ERROR)
                self._log(f"    失败: {r.get('error', '未知错误')}", color=Theme.FG_DIM)

        self._log("─" * 48, color=Theme.FG_DIM)
        self._log(f"总计: {total} 文件 | 成功 {success} | 调整 {total_resized} 页 | 目标尺寸: {target_size}",
                 color=Theme.ACCENT)
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("")

        self.progress_var.set(100)
        self.status_label.config(text=f"尺寸调整完成！{target_size}，{success}/{total} 个文件成功", fg=Theme.SUCCESS)
        self.status_icon.config(fg=Theme.SUCCESS)

        messagebox.showinfo(
            "尺寸调整完成",
            f"已完成尺寸调整！\n\n目标尺寸: {target_size}\n"
            f"文件总数: {total}\n成功: {success}\n调整页数: {total_resized}\n\n"
            f"输出目录: {self._get_output_location_text()}"
        )

    def _show_results_clean(self, results: List[dict]):
        """显示扫描件去瑕疵结果"""
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("扫描件去瑕疵结果汇总", color=Theme.ACCENT)
        self._log("━" * 48, color=Theme.FG_DIM)

        total = len(results)
        success = sum(1 for r in results if r.get("status") == "success")
        total_cleaned = sum(r.get("cleaned_pages", 0) for r in results if r.get("status") == "success")

        for r in results:
            fn = os.path.basename(r.get("input_file", ""))
            if r.get("status") == "success":
                self._log(f"  {fn}", color=Theme.SUCCESS)
                self._log(f"    总页数: {r.get('total_pages', 0)} | 已清理: {r.get('cleaned_pages', 0)}",
                          color=Theme.FG_DIM)
                for rec in r.get("records", [])[:3]:
                    tc = rec.get("trim_cm", {})
                    self._log(
                        f"      第{rec.get('page_index', 0) + 1}页 黑边(厘米) "
                        f"上{tc.get('top', 0)} 下{tc.get('bottom', 0)} "
                        f"左{tc.get('left', 0)} 右{tc.get('right', 0)}",
                        color=Theme.FG_DIM)
            else:
                self._log(f"  {fn}", color=Theme.ERROR)
                self._log(f"    失败: {r.get('error', '未知错误')}", color=Theme.FG_DIM)

        self._log("─" * 48, color=Theme.FG_DIM)
        self._log(f"总计: {total} 文件 | 成功 {success} | 清理 {total_cleaned} 页",
                  color=Theme.ACCENT)
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("")

        self.progress_var.set(100)
        self.status_label.config(text=f"去瑕疵完成！清理了 {total_cleaned} 页", fg=Theme.SUCCESS)
        self.status_icon.config(fg=Theme.SUCCESS)

        messagebox.showinfo(
            "扫描件去瑕疵完成",
            f"已完成扫描边缘清理！\n\n文件总数: {total}\n成功: {success}\n"
            f"清理页数: {total_cleaned}\n\n"
            f"输出目录: {self._get_output_location_text()}"
        )

    def _show_results_logo(self, results: List[dict], cfg):
        """显示页眉Logo去除结果：说清认到的是哪一块、动了哪些页、没动的页为什么"""
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("去除页眉Logo结果汇总", color=Theme.ACCENT)
        self._log("━" * 48, color=Theme.FG_DIM)

        total = len(results)
        success = sum(1 for r in results if r.get("status") == "success")
        skipped = sum(1 for r in results if r.get("status") == "skipped")
        total_removed = sum(r.get("removed_pages", 0) for r in results
                            if r.get("status") == "success")

        for r in results:
            fn = os.path.basename(r.get("input_file", ""))
            det = r.get("detection") or {}
            if r.get("status") == "success":
                self._log(f"  {fn}", color=Theme.SUCCESS)
                cm = det.get("cm") or {}
                where = " ".join(f"{k}{v}cm" for k, v in cm.items())
                self._log(f"    总页数: {r.get('total_pages', 0)} | "
                          f"已去除: {r.get('removed_pages', 0)} | 查了: {r.get('checked_pages', 0)}",
                          color=Theme.FG_DIM)
                row_mode = "｜整行删除：连页眉文字和横线一起去" if cfg.whole_row else ""
                self._log(f"    认到的标识: {det.get('anchor_label', cfg.ANCHOR_LABELS.get(cfg.anchor))}"
                          f"（{where}，由{det.get('mark_parts', 0)}块墨拼成）{row_mode}", color=Theme.FG_DIM)
                for d in r.get("details", []):
                    if not d.get("removed"):
                        self._log(f"    第{d['page_index'] + 1}页: {d.get('reason', '对不上')}，保持原样",
                                  color=Theme.WARNING)
            elif r.get("status") == "skipped":
                self._log(f"  {fn}", color=Theme.WARNING)
                self._log(f"    未处理: {r.get('message', '这一角没有固定标识')}", color=Theme.FG_DIM)
            else:
                self._log(f"  {fn}", color=Theme.ERROR)
                self._log(f"    失败: {r.get('error', '未知错误')}", color=Theme.FG_DIM)

        self._log("─" * 48, color=Theme.FG_DIM)
        self._log(f"总计: {total} 文件 | 成功 {success} | 无标识 {skipped} | 去除 {total_removed} 页",
                  color=Theme.ACCENT)
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("")

        self.progress_var.set(100)
        self.status_label.config(text=f"去Logo完成！去除 {total_removed} 页的标识"
                                      f"，{skipped} 个文件未处理", fg=Theme.SUCCESS)
        self.status_icon.config(fg=Theme.SUCCESS)

        note = (f"\n未处理 {skipped} 个文件：角上没有跨页一致的标识，或标识位置上有印章，"
                f"这些文件原样保留（详见日志）" if skipped else "")
        messagebox.showinfo(
            "去除页眉Logo完成",
            f"已完成页眉标识去除！\n\n位置: {cfg.ANCHOR_LABELS.get(cfg.anchor)}"
            f"{'（整行删除：连页眉文字和横线）' if cfg.whole_row else ''}\n"
            f"文件总数: {total}\n成功: {success}\n去除页数: {total_removed}{note}\n\n"
            f"输出目录: {self._get_output_location_text()}"
        )

    def _show_results_fit(self, results: List[dict], spec):
        """显示按边距缩放结果（spec 是本次的 FitSpec，汇总文字和逐页缩放比都从它来）"""
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("边距缩放结果汇总", color=Theme.ACCENT)
        self._log("━" * 48, color=Theme.FG_DIM)

        total = len(results)
        success = sum(1 for r in results if r.get("status") == "success")
        total_pages = sum(r.get("processed_pages", 0) for r in results if r.get("status") == "success")
        skipped_pages = sum(r.get("skipped_pages", 0) for r in results if r.get("status") == "success")
        spec_text = spec.describe()

        for r in results:
            fn = os.path.basename(r.get("input_file", ""))
            if r.get("status") == "success":
                self._log(f"  {fn}", color=Theme.SUCCESS)
                self._log(f"    总页数: {r.get('total_pages', 0)} | 已调整: {r.get('processed_pages', 0)}"
                          f" | 保持原样: {r.get('skipped_pages', 0)}", color=Theme.FG_DIM)
                self._log(f"    方式: {r.get('spec', spec_text)}", color=Theme.FG_DIM)
                for p in r.get("pages", []):
                    if p.get("skipped"):
                        self._log(f"    第{p['page_index'] + 1}页: {p['reason']}，保持原样", color=Theme.WARNING)
                        continue
                    sx, sy = p.get("scale_x", p.get("scale", 1.0)), p.get("scale_y", p.get("scale", 1.0))
                    scale_text = f"{sx:.0%}" if abs(sx - sy) < 1e-6 else f"横向{sx:.0%} 纵向{sy:.0%}"
                    self._log(
                        f"    第{p['page_index'] + 1}页: 缩放{scale_text}，"
                        f"上{p['actual_top_cm']}cm 下{p['actual_bottom_cm']}cm "
                        f"左{p['actual_left_cm']}cm 右{p['actual_right_cm']}cm",
                        color=Theme.FG_DIM)
            else:
                self._log(f"  {fn}", color=Theme.ERROR)
                self._log(f"    失败: {r.get('error', '未知错误')}", color=Theme.FG_DIM)

        self._log("─" * 48, color=Theme.FG_DIM)
        self._log(f"总计: {total} 文件 | 成功 {success} | 调整 {total_pages} 页 | {spec_text}",
                  color=Theme.ACCENT)
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("")

        self.progress_var.set(100)
        self.status_label.config(text=f"边距缩放完成！{spec.KIND_TEXT.get(spec.kind, spec.kind)}，"
                                      f"{success}/{total} 个文件成功", fg=Theme.SUCCESS)
        self.status_icon.config(fg=Theme.SUCCESS)

        messagebox.showinfo(
            "边距缩放完成",
            f"已完成{spec.KIND_TEXT.get(spec.kind, spec.kind)}！\n\n"
            f"本次设置: {spec_text}\n"
            f"文件总数: {total}\n成功: {success}\n调整页数: {total_pages}\n"
            f"保持原样页数: {skipped_pages}（印发说明页/手动指定）\n\n"
            f"输出目录: {self._get_output_location_text()}"
        )

    def _show_results_pipeline(self, pipeline_results: List[dict]):
        """显示组合处理结果"""
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("组合处理结果汇总", color=Theme.ACCENT)
        self._log("━" * 48, color=Theme.FG_DIM)

        total_files = 0
        all_success = True

        for step_info in pipeline_results:
            func_key = step_info["func_key"]
            label = step_info["label"]
            results = step_info["results"]

            total = len(results)
            success = sum(1 for r in results if r.get("status") == "success")
            total_files = max(total_files, total)

            self._log(f"  ▸ {label}", color=Theme.ACCENT)
            for r in results:
                fn = os.path.basename(r.get("input_file", ""))
                if r.get("status") == "success":
                    self._log(f"    ✓ {fn}", color=Theme.SUCCESS)
                    # 根据功能类型显示不同详情
                    if func_key == "remove_page_numbers":
                        self._log(f"      去除页码: {r.get('removed_count', 0)}", color=Theme.FG_DIM)
                    elif func_key == "pdf_scale":
                        if "processed_pages" in r:
                            self._log(f"      按边距调整: {r.get('processed_pages', 0)} 页", color=Theme.FG_DIM)
                        else:
                            self._log(f"      页数: {r.get('total_pages', 0)}", color=Theme.FG_DIM)
                    elif func_key == "pdf_rotate":
                        self._log(f"      旋转: {r.get('rotated_count', 0)} 页", color=Theme.FG_DIM)
                    elif func_key == "pdf_resize":
                        self._log(f"      调整: {r.get('resized_count', 0)} 页", color=Theme.FG_DIM)
                    elif func_key == "clean_scan":
                        self._log(f"      去瑕疵: {r.get('cleaned_pages', 0)} 页", color=Theme.FG_DIM)
                    elif func_key == "remove_logo":
                        self._log(f"      去Logo: {r.get('removed_pages', 0)} 页"
                                  f"（查了 {r.get('checked_pages', 0)} 页）", color=Theme.FG_DIM)
                elif r.get("status") == "skipped":
                    self._log(f"    ○ {fn}", color=Theme.WARNING)
                    self._log(f"      跳过: {r.get('message', '无需处理')}", color=Theme.FG_DIM)
                else:
                    self._log(f"    ✗ {fn}", color=Theme.ERROR)
                    self._log(f"      失败: {r.get('error', '未知错误')}", color=Theme.FG_DIM)
                    all_success = False

            self._log(f"    小计: {success}/{total} 成功", color=Theme.FG_DIM)
            self._log("─" * 48, color=Theme.FG_DIM)

        # 总汇
        pipeline_labels = " → ".join(s["label"] for s in pipeline_results)
        self._log(f"处理流水线: {pipeline_labels}", color=Theme.ACCENT)
        self._log(f"输出目录: {self._get_output_location_text()}", color=Theme.FG_DIM)
        self._log("━" * 48, color=Theme.FG_DIM)
        self._log("")

        self.progress_var.set(100)
        status_msg = f"组合处理完成！{len(pipeline_results)} 个步骤已执行"
        self.status_label.config(text=status_msg, fg=Theme.SUCCESS)
        self.status_icon.config(fg=Theme.SUCCESS)

        # 构建结果对话框
        steps_summary = "\n".join(
            f"  {s['label']}: {sum(1 for r in s['results'] if r.get('status') == 'success')}/{len(s['results'])} 成功"
            for s in pipeline_results
        )
        messagebox.showinfo(
            "组合处理完成",
            f"已完成组合处理！\n\n处理流水线:\n{pipeline_labels}\n\n{steps_summary}\n\n"
            f"输出目录: {self._get_output_location_text()}"
        )

    def _get_output_location_text(self) -> str:
        """获取输出位置描述文本（替换模式显示源文件所在目录）"""
        if self.replace_source_var.get():
            if self._replace_summary:
                return self._replace_summary
            src = self._source_output_dir()
            return f"准备替换源文件（{src}）" if src else "准备替换源文件"
        output_dir = self.output_var.get()
        return output_dir if output_dir else "源文件目录"

    def _log(self, message: str, color: str = Theme.FG):
        self.log_text.config(state=tk.NORMAL)
        tag = f"color_{id(color)}"
        self.log_text.tag_config(tag, foreground=color)
        self.log_text.insert(tk.END, message + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)


class SplashScreen:
    """启动加载动画窗口"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.splash = tk.Toplevel(root)
        self.splash.overrideredirect(True)  # 无边框
        self.splash.attributes('-topmost', True)
        self.splash.configure(bg=Theme.BG)

        # 窗口尺寸与居中
        width, height = ui_dpi.S(400), ui_dpi.S(220)
        sx = (self.splash.winfo_screenwidth() - width) // 2
        sy = (self.splash.winfo_screenheight() - height) // 2
        self.splash.geometry(f"{width}x{height}+{sx}+{sy}")

        # 内容
        self._create_content(width, height)

        # 动画状态
        self.angle = 0
        self.running = True
        self.splash.after(10, self._animate)

        # 确保窗口显示
        self.splash.update_idletasks()
        self.splash.update()

    def _create_content(self, width, height):
        # 应用图标/Logo区域
        logo_frame = tk.Frame(self.splash, bg=Theme.BG)
        logo_frame.pack(expand=True, fill=tk.BOTH, padx=S(30), pady=(S(30), S(0)))

        # 标题
        tk.Label(logo_frame, text="PDF Tool", bg=Theme.BG,
                 fg=Theme.ACCENT, font=F(22, "bold")).pack(pady=(S(0), S(4)))

        # 副标题
        tk.Label(logo_frame, text="多功能组合 · 去页码 · 内容缩放 · 统一尺寸 · 自动旋转 · 去扫描边瑕 · 去页眉标识", bg=Theme.BG,
                 fg=Theme.FG_DIM, font=F(10)).pack(pady=(S(0), S(16)))

        # Canvas 用于绘制旋转动画
        self.canvas = tk.Canvas(self.splash, width=ui_dpi.S(36), height=ui_dpi.S(36),
                                bg=Theme.BG, highlightthickness=0)
        self.canvas.pack(pady=(S(0), S(8)))

        # 加载文字
        self.loading_label = tk.Label(self.splash, text="正在加载...", bg=Theme.BG,
                                       fg=Theme.FG_DIM, font=F(9))
        self.loading_label.pack(pady=(S(0), S(16)))

        # 底部装饰线
        deco = tk.Frame(self.splash, bg=Theme.ACCENT, height=2)
        deco.pack(fill=tk.X, side=tk.BOTTOM)

    def _animate(self):
        """绘制旋转圆弧动画"""
        if not self.running:
            return
        self.canvas.delete("all")
        cx = cy = ui_dpi.S(18)
        r = ui_dpi.S(14)
        # 绘制背景圆环
        self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                outline=Theme.BORDER, width=ui_dpi.S(3))
        # 绘制旋转弧线
        start = self.angle
        self.canvas.create_arc(cx - r, cy - r, cx + r, cy + r,
                               start=start, extent=90,
                               style=tk.ARC, outline=Theme.ACCENT, width=ui_dpi.S(3))
        # 绘制尾部弧线（渐淡效果）
        self.canvas.create_arc(cx - r, cy - r, cx + r, cy + r,
                               start=start + 90, extent=40,
                               style=tk.ARC, outline=Theme.BORDER, width=ui_dpi.S(2))
        self.angle = (self.angle - 8) % 360
        self.splash.after(40, self._animate)

    def update_text(self, text: str):
        """更新加载提示文字"""
        self.loading_label.config(text=text)

    def destroy(self):
        """关闭加载动画"""
        self.running = False
        try:
            self.splash.destroy()
        except tk.TclError:
            pass


def _create_shortcut(shortcut_path, target, arguments, work_dir, icon_path, description):
    """使用PowerShell创建Windows快捷方式"""
    import subprocess
    # Arguments需要加双引号，防止路径含空格被解析为多个参数
    ps_cmd = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{shortcut_path}'); "
        f"$sc.TargetPath = '{target}'; "
    )
    if arguments:
        ps_cmd += f'$sc.Arguments = \'"\' + \'{arguments}\' + \'"\'; '
    ps_cmd += (
        f"$sc.WorkingDirectory = '{work_dir}'; "
        f"$sc.Description = '{description}'; "
    )
    if icon_path and os.path.exists(icon_path):
        ps_cmd += f"$sc.IconLocation = '{icon_path},0'; "
    ps_cmd += "$sc.Save()"
    result = subprocess.run(
        ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps_cmd],
        capture_output=True, timeout=10,
        startupinfo=subprocess.STARTUPINFO(dwFlags=subprocess.STARTF_USESHOWWINDOW, wShowWindow=0),
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    if result.returncode != 0:
        logging.warning(f"创建快捷方式失败: {result.stderr.decode('gbk', errors='ignore')}")


def _create_start_menu_shortcut():
    """创建Windows开始菜单和桌面快捷方式（每次启动检查，缺失则创建）"""
    if sys.platform != "win32":
        return
    try:
        appdata = os.environ.get('APPDATA', '')
        if not appdata:
            return
        if getattr(sys, 'frozen', False):
            # 打包后：直接指向可执行文件，图标用程序自身内嵌的
            target = sys.executable
            script_path = ''
            icon_path = sys.executable
            work_dir = os.path.dirname(target)
        else:
            # 源码运行：用pythonw.exe起main.py，避免弹出控制台窗口
            pythonw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
            if not os.path.exists(pythonw):
                pythonw = sys.executable
            target = pythonw
            script_path = os.path.normpath(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'main.py'))
            icon_path = os.path.normpath(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.ico'))
            work_dir = os.path.dirname(script_path)
        desc = 'PDF Tool - PDF页面处理工具'
        # 创建/更新开始菜单快捷方式
        start_menu_dir = os.path.join(appdata, 'Microsoft', 'Windows', 'Start Menu', 'Programs')
        start_menu_lnk = os.path.join(start_menu_dir, 'PDF Tool.lnk')
        _create_shortcut(start_menu_lnk, target, script_path, work_dir, icon_path, desc)
        # 创建/更新桌面快捷方式
        desktop_dir = os.path.join(os.path.expanduser('~'), 'Desktop')
        desktop_lnk = os.path.join(desktop_dir, 'PDF Tool.lnk')
        _create_shortcut(desktop_lnk, target, script_path, work_dir, icon_path, desc)
    except Exception as e:
        logging.debug(f"创建快捷方式失败（非致命）: {e}")


def main():
    # 创建开始菜单快捷方式（仅首次运行）
    _create_start_menu_shortcut()

    # 必须在任何窗口创建之前声明，之后 Windows 就不再对本进程做位图拉伸；
    # 125%/150% 缩放屏上的字是不是"真像素"就取决于这一句有没有在 Tk() 之前跑到
    ui_dpi.enable_dpi_awareness()

    # 先创建根窗口（不可见），用于承载SplashScreen
    # 优先使用TkinterDnD以支持拖拽
    if HAS_DND:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    root.withdraw()  # 隐藏主窗口

    # 设置窗口图标（在SplashScreen之前，确保splash也显示图标）
    try:
        _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.ico')
        if os.path.exists(_icon_path):
            root.iconbitmap(_icon_path)
    except Exception:
        pass

    # 显示加载动画（先量屏幕，splash 才跟主窗口一样大）
    ui_dpi.init(root)
    logging.info("界面缩放: %s", ui_dpi.info())
    splash = SplashScreen(root)
    root.update()  # 确保splash立即渲染

    # 初始化主界面（同步执行，splash动画靠after回调维持旋转）
    splash.update_text("正在初始化界面...")
    root.update()

    root.configure(bg=Theme.BG)
    app = PDFPageNumberRemoverGUI(root)

    splash.update_text("正在加载组件...")
    root.update()

    # 居中主窗口
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+"
                  f"{(root.winfo_screenheight() - h) // 2}")

    # 主窗口完全就绪，先显示主窗口，再延迟关闭加载动画
    splash.update_text("加载完成")
    root.deiconify()  # 显示主窗口
    root.after(500, splash.destroy)  # 延迟关闭splash，确保平滑过渡

    root.mainloop()


if __name__ == "__main__":
    main()