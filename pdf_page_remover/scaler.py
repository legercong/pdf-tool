"""
PDF内容比例缩放模块
支持将PDF页面内容按指定比例缩放，保持页面尺寸不变
支持修改页面尺寸（如统一为A4），同时内容按比例适配
"""

import pymupdf
import io
import os
import re
import logging
from dataclasses import dataclass, replace
from typing import List, Dict, Any, Optional, Callable, Tuple, Set

from PIL import Image

logger = logging.getLogger(__name__)

_INDIRECT_REF_RE = re.compile(r'(\d+)\s+\d+\s+R\b')
_RECT_KEY_RE = re.compile(r'/Rect\s*\[\s*([-\d.+\s]+)\s*\]')


def _graft_xref_object(src_doc: "pymupdf.Document", src_xref: int,
                       dst_doc: "pymupdf.Document", cache: Dict[int, int],
                       override_str: Optional[str] = None) -> int:
    """把源文档中的一个对象（连同它引用的全部间接对象）复制到目标文档，返回新 xref。

    override_str 用来传入改过的对象文本（例如先把 /P 的页面引用摘掉，
    免得把整页内容流跟着抄一份）。
    """
    if src_xref in cache:
        return cache[src_xref]

    new_xref = dst_doc.get_new_xref()
    cache[src_xref] = new_xref

    obj_str = src_doc.xref_object(src_xref, compressed=True)
    if override_str is not None:
        obj_str = override_str

    def _rematch(m):
        ref_num = int(m.group(1))
        if ref_num == src_xref:
            return m.group(0)
        return f"{_graft_xref_object(src_doc, ref_num, dst_doc, cache)} 0 R"

    dst_doc.update_object(new_xref, _INDIRECT_REF_RE.sub(_rematch, obj_str))

    if src_doc.xref_is_stream(src_xref):
        # 必须传解码后的内容：xref_stream_raw 拿到的已编码字节再交给 update_stream
        # 会被二次压缩，MuPDF 读取时报 content stream 语法错误、注释渲染为空。
        data = src_doc.xref_stream(src_xref)
        if data:
            dst_doc.update_stream(new_xref, data)

    return new_xref


def _placement_matrix(src_page: "pymupdf.Page", target_rect: "pymupdf.Rect",
                      angle: int) -> Optional["pymupdf.Matrix"]:
    """构造“源页未旋转坐标 → 目标页坐标”的变换矩阵，与 show_pdf_page(rect, ..., rotate=angle) 一致。"""
    mb = src_page.mediabox
    src_rect = pymupdf.Rect(0, 0, mb.width, mb.height)

    mat = pymupdf.Matrix(1, 0, 0, 1, -src_rect.x0, -src_rect.y0)
    # show_pdf_page 的 rotate 是逆时针方向，PyMuPDF 的 Matrix(角度) 在屏幕坐标下是顺时针
    mat = mat * pymupdf.Matrix(-angle)

    rotated = pymupdf.Rect(src_rect) * mat
    mat = mat * pymupdf.Matrix(1, 0, 0, 1, -rotated.x0, -rotated.y0)
    rotated = pymupdf.Rect(src_rect) * mat
    if rotated.width <= 0 or rotated.height <= 0:
        return None

    mat = mat * pymupdf.Matrix(target_rect.width / rotated.width, 0,
                               0, target_rect.height / rotated.height, 0, 0)
    mat = mat * pymupdf.Matrix(1, 0, 0, 1, target_rect.x0, target_rect.y0)
    return mat


def _annot_rect_in_src_page(src_doc, annot_xref: int,
                            src_page: "pymupdf.Page") -> Optional["pymupdf.Rect"]:
    """读取注释的 /Rect，并转换成源页面（未旋转）的 PyMuPDF 坐标。"""
    obj_str = src_doc.xref_object(annot_xref, compressed=True)
    m = _RECT_KEY_RE.search(obj_str)
    if not m:
        return None
    try:
        nums = [float(v) for v in m.group(1).split()]
    except ValueError:
        return None
    if len(nums) != 4:
        return None

    rect = pymupdf.Rect(nums)
    mb = src_page.mediabox
    # PDF 的 y 轴向上，PyMuPDF 向下；/Rect 是未旋转页面上的坐标
    return pymupdf.Rect(rect.x0, mb.y0 + mb.y1 - rect.y1,
                        rect.x1, mb.y0 + mb.y1 - rect.y0)


def _page_annot_xrefs(doc: "pymupdf.Document", page: "pymupdf.Page") -> List[int]:
    """直接解析页面的 /Annots 数组。

    PyMuPDF 的 page.annots()/page_annot_xrefs() 会跳过它不认识的注释子类型，
    而电子签章恰恰用的是自定义类型（如 /GoldGrid:AddSeal），必须自己取。
    """
    try:
        kind, val = doc.xref_get_key(page.xref, "Annots")
    except Exception:
        return []
    if kind == "null" or not val:
        return []

    refs = []
    for num in re.findall(r'(\d+)\s+\d+\s+R', val):
        refs.extend(_resolve_annot_refs(doc, int(num), 2))

    out = []
    for xref in refs:
        try:
            obj = doc.xref_object(xref, compressed=True)
        except Exception:
            continue
        if "/Type/Annot" in obj or "/Subtype" in obj:
            out.append(xref)
    return out


def _resolve_annot_refs(doc, xref: int, depth: int) -> List[int]:
    """/Annots 可能是内联数组，也可能是指向数组对象的间接引用，逐层展开取注释 xref。"""
    try:
        obj = doc.xref_object(xref, compressed=True)
    except Exception:
        return []
    if not obj.startswith("["):
        return [xref]
    if depth <= 0:
        return []
    inner = []
    for num in re.findall(r'(\d+)\s+\d+\s+R', obj):
        inner.extend(_resolve_annot_refs(doc, int(num), depth - 1))
    return inner


def _acroform_xref(dst_doc: "pymupdf.Document") -> int:
    """取（或建）这份文档的 /AcroForm 字典 xref。签章控件必须挂在它下面才算字段。"""
    cat = dst_doc.pdf_catalog()
    kind, val = dst_doc.xref_get_key(cat, "AcroForm")
    if kind == "xref" and val:
        return int(val.split()[0])
    af = dst_doc.get_new_xref()
    dst_doc.update_object(af, "<</Type/AcroForm/Fields[]/SigFlags 3>>")
    dst_doc.xref_set_key(cat, "AcroForm", f"{af} 0 R")
    return af


def _register_form_fields(dst_doc: "pymupdf.Document", dst_page: "pymupdf.Page",
                          field_xrefs: List[int]) -> None:
    """把搬过来的表单控件（电子签章就是 /FT/Sig 的 Widget）挂回字段树。

    金格章是"注释 + 表单字段"两副面孔：只把它写进页面 /Annots 而 /AcroForm 里没有，
    阅读器会当成残缺字段不画；反过来只建 /AcroForm 不写 /Annots，页面上也没有外观。
    所以两边都要登记，并把 /P 指回搬后的那一页。
    """
    if not field_xrefs:
        return
    af = _acroform_xref(dst_doc)
    kind, val = dst_doc.xref_get_key(af, "Fields")
    refs = [int(n) for n in re.findall(r'(\d+)\s+\d+\s+R', val or "")]
    refs.extend(r for r in field_xrefs if r not in refs)
    array = "[ " + " ".join(f"{r} 0 R" for r in refs) + " ]"
    if kind == "xref" and val:
        dst_doc.update_object(int(val.split()[0]), array)
    else:
        dst_doc.xref_set_key(af, "Fields", array)
    for r in field_xrefs:
        dst_doc.xref_set_key(r, "P", f"{dst_page.xref} 0 R")


def carry_doc_extras(src_doc: "pymupdf.Document", new_doc: "pymupdf.Document") -> None:
    """文档级的东西不会跟着页面一起搬过来，保存前手动补上：书签（目录）和文件属性。

    缩放/改尺寸是"另起一个新文档、一页页重画"，新文档除了页面内容是空白的：Word/WPS
    导出的标题书签没了，属性里的作者、创建时间也没了。交付出去是同一份文件却少了目录
    导航，用户一眼就能看出来。
    书签记的是页号，只有每一页都按原顺序在（页数对得上）才能照搬，对不上就不搬，
    免得书签指向错页——那比没有书签更糟。
    """
    try:
        meta = src_doc.metadata
        if meta:
            new_doc.set_metadata(meta)
    except Exception as e:
        logger.debug(f"文档属性没搬过来: {e}")
    try:
        if new_doc.page_count != src_doc.page_count:
            return
        toc = src_doc.get_toc(simple=True)
        if toc:
            new_doc.set_toc(toc)
    except Exception as e:
        logger.debug(f"书签没搬: {e}")


def copy_page_annotations(src_doc: "pymupdf.Document", src_page: "pymupdf.Page",
                          dst_doc: "pymupdf.Document", dst_page: "pymupdf.Page",
                          target_rect: "pymupdf.Rect", angle: int = 0) -> int:
    """
    把源页面的注释补到目标页面。

    show_pdf_page 只搬运页面内容流，不会带上注释，因此电子印章（如 /GoldGrid:AddSeal）、
    签章、批注在缩放/尺寸调整后都会消失。这里按内容所用的同一变换重建注释对象。

    Returns:
        成功搬运的注释数量
    """
    annot_xrefs = _page_annot_xrefs(src_doc, src_page)
    if not annot_xrefs:
        return 0

    mat = _placement_matrix(src_page, target_rect, angle)
    if mat is None:
        return 0

    cache: Dict[int, int] = {}
    copied = []
    fields: List[int] = []
    dst_height = dst_page.mediabox.height

    for annot_xref in annot_xrefs:
        try:
            obj_str = src_doc.xref_object(annot_xref, compressed=True)
            is_widget = "/Widget" in obj_str
            if is_widget:
                # /P 指的是源页面，跟着深拷会把整页内容流再抄一份；
                # 搬运完成后由 _register_form_fields 把它指回目标页
                obj_str = re.sub(r'(/P\s+)(\d+)\s+\d+\s+R', r'\1null', obj_str)

            src_rect = _annot_rect_in_src_page(src_doc, annot_xref, src_page)
            if src_rect is None or src_rect.is_empty:
                logger.debug(f"注释 xref={annot_xref} 无有效 /Rect，跳过")
                continue

            new_rect = pymupdf.Rect(src_rect) * mat
            if new_rect.is_empty:
                continue

            new_xref = _graft_xref_object(src_doc, annot_xref, dst_doc, cache,
                                          override_str=obj_str)
            # 变换后可能宽高互换，需归一化
            if new_rect.x0 > new_rect.x1:
                new_rect.x0, new_rect.x1 = new_rect.x1, new_rect.x0
            if new_rect.y0 > new_rect.y1:
                new_rect.y0, new_rect.y1 = new_rect.y1, new_rect.y0

            pdf_rect = (new_rect.x0, dst_height - new_rect.y1,
                        new_rect.x1, dst_height - new_rect.y0)
            dst_doc.xref_set_key(new_xref, "Rect",
                                 "[ " + " ".join(f"{v:.4f}" for v in pdf_rect) + " ]")
            # /P 指向源页面，留在目标文档里会成为悬空页引用
            if dst_doc.xref_get_key(new_xref, "P")[0] != "null":
                dst_doc.xref_set_key(new_xref, "P", "null")
            copied.append(new_xref)
            if is_widget:
                fields.append(new_xref)
        except Exception as e:
            logger.debug(f"搬运注释 xref={annot_xref} 失败: {e}")

    if not copied:
        return 0

    try:
        existing = dst_doc.xref_get_key(dst_page.xref, "Annots")
        refs = []
        if existing[0] == "xref":
            refs = [int(n) for n in re.findall(r'(\d+)\s+\d+\s+R', existing[1])]
        refs.extend(copied)
        dst_doc.xref_set_key(dst_page.xref, "Annots",
                             "[ " + " ".join(f"{r} 0 R" for r in refs) + " ]")
    except Exception as e:
        logger.debug(f"写入页面注释数组失败: {e}")
        return 0

    _register_form_fields(dst_doc, dst_page, fields)
    return len(copied)


# 1厘米 = 72/2.54 点
CM_TO_PT = 72.0 / 2.54

# 按边距缩放时允许的极端比例，避免小图案被放大到离谱
_FIT_MIN_SCALE = 0.05
_FIT_MAX_SCALE = 5.0


@dataclass
class FitSpec:
    """一次"缩放并摆放"的完整参数，处理和预览共用同一份，免得两边各算一套

    kind:
      "align"   对齐缩放：内容大小以 ratio_y 为准，只有对侧越过边距线时才等比缩小
      "uniform" 等比缩放：等比缩放到 axis 那个方向的边距精确成立（可放大可缩小）
      "stretch" 根据边距缩放：axis 方向贴住边距（非等比拉伸），另一方向按 ratio 不缩放
    align: "top"/"bottom"/"both"，内容贴哪条边；both 是上下都贴住（放不下时在边距带里居中）
    axis:  "v" 上下边距定大小 / "h" 左右边距定大小 / "none" 两个方向都按 ratio
    ratio_x, ratio_y: 用户填的比例，只作用于没被边距接管的那个方向
    side_cm: None 表示不判定左右距离，只保证内容不被页面左右边缘裁掉
    """
    kind: str = "align"
    align: str = "top"
    axis: str = "v"
    top_cm: float = 2.5
    bottom_cm: float = 2.5
    side_cm: Optional[float] = None
    ratio_x: float = 1.0
    ratio_y: float = 1.0

    KIND_TEXT = {"align": "对齐缩放", "uniform": "等比缩放", "stretch": "根据边距缩放"}
    ALIGN_TEXT = {"top": "顶部对齐", "bottom": "底部对齐", "both": "两侧对齐"}
    AXIS_TEXT = {"v": "上下符合边距", "h": "左右符合边距", "none": "自定义比例"}

    def validate(self):
        if self.kind not in self.KIND_TEXT:
            raise ValueError(f"未知的缩放方式: {self.kind}")
        if self.align not in self.ALIGN_TEXT:
            raise ValueError(f"未知的对齐方式: {self.align}")
        if self.axis not in self.AXIS_TEXT:
            raise ValueError(f"未知的边距方向: {self.axis}")
        if self.top_cm <= 0:
            raise ValueError("顶部距离必须大于0")
        if self.bottom_cm <= 0:
            raise ValueError("底部距离必须大于0")
        if self.top_cm > 20 or self.bottom_cm > 20:
            raise ValueError("距离超过20cm，请检查是否填错了单位")
        if self.side_cm is not None:
            if self.side_cm <= 0:
                raise ValueError("左右距离必须大于0")
            if self.side_cm > 10:
                raise ValueError("左右距离超过10cm，请检查是否填错了单位")
        if self.ratio_x <= 0 or self.ratio_y <= 0:
            raise ValueError("缩放比例必须大于0")
        if self.axis == "h" and self.side_cm is None:
            raise ValueError("要让左右符合边距，请先勾选并填写左右距离")

    def describe(self) -> str:
        """一句话说明这次是怎么摆的，日志和结果汇总都用它"""
        parts = [self.KIND_TEXT.get(self.kind, self.kind)]
        if self.kind == "align":
            parts.append(self.ALIGN_TEXT.get(self.align, self.align))
            if self.align != "both" and self.ratio_y != 1.0:
                parts.append(f"先按{self.ratio_y:.0%}缩放")
        elif self.kind == "uniform":
            parts.append(self.AXIS_TEXT.get(self.axis, self.axis))
            parts.append(self.ALIGN_TEXT.get(self.align, self.align))
        else:
            parts.append(self.AXIS_TEXT.get(self.axis, self.axis))
            if self.axis == "v":
                parts.append(f"左右{self.ratio_x:.0%}")
            elif self.axis == "h":
                parts.append(f"上下{self.ratio_y:.0%}")
                parts.append(self.ALIGN_TEXT.get(self.align, self.align))
            else:
                parts.append(f"左右{self.ratio_x:.0%} 上下{self.ratio_y:.0%}")
        parts.append(f"上{self.top_cm}cm 下{self.bottom_cm}cm")
        if self.side_cm is not None:
            parts.append(f"左右各{self.side_cm}cm")
        return "·".join(parts)


def compute_fit_placement(spec: FitSpec, display_w: float, display_h: float,
                          bbox: "pymupdf.Rect"
                          ) -> Tuple[float, float, float, float, List[str]]:
    """算出这一页内容缩放到多大、整页矩形落到哪里

    返回 (sx, sy, x0, y0, notes)：x0/y0 是"整页（含空白边）矩形"的左上角，
    直接喂给 show_pdf_page；notes 是要写进日志的解释（被哪条边距限制住了之类）。
    单位跟着传进来的 display_w/display_h 走（点是点、像素是像素），预览可以按像素调它。
    """
    top = spec.top_cm * CM_TO_PT
    bottom = spec.bottom_cm * CM_TO_PT
    side = spec.side_cm * CM_TO_PT if spec.side_cm is not None else 0.0
    avail_h = display_h - top - bottom
    avail_w = display_w - 2 * side
    if avail_h <= 0:
        raise ValueError(
            f"上下距离之和({spec.top_cm + spec.bottom_cm:.2f}cm)不小于页面高度"
            f"({display_h / CM_TO_PT:.2f}cm)")
    if avail_w <= 0:
        raise ValueError(
            f"左右距离之和({2 * (spec.side_cm or 0.0):.2f}cm)不小于页面宽度"
            f"({display_w / CM_TO_PT:.2f}cm)")

    content_w, content_h = bbox.width, bbox.height
    h_fit = avail_h / content_h   # 上下边距同时成立需要的比例
    w_fit = avail_w / content_w   # 左右边距同时成立需要的比例
    notes: List[str] = []

    def clamp(v: float, what: str) -> float:
        c = max(_FIT_MIN_SCALE, min(_FIT_MAX_SCALE, v))
        if c != v:
            notes.append(f"{what}比例{v:.2f}超出合理范围，按{c:.2f}处理")
        return c

    if spec.kind == "align":
        if spec.align == "both":
            s = min(h_fit, w_fit)
        else:
            s = min(spec.ratio_y, h_fit, w_fit)
            if s < spec.ratio_y:
                guard = "底部" if spec.align == "top" else "顶部"
                if h_fit <= w_fit:
                    notes.append(f"按{spec.ratio_y:.0%}摆放会越过{guard}"
                                 f"{spec.bottom_cm if spec.align == 'top' else spec.top_cm:.2f}cm"
                                 f"距离线，等比缩小到{s:.0%}")
                elif spec.side_cm is not None:
                    notes.append(f"按{spec.ratio_y:.0%}摆放内容宽度会越过左右"
                                 f"{spec.side_cm:.2f}cm距离，等比缩小到{s:.0%}")
                else:
                    notes.append(f"按{spec.ratio_y:.0%}摆放内容宽度会超出页面，等比缩小到{s:.0%}")
        sx = sy = clamp(s, "缩放")
    elif spec.kind == "uniform":
        driven = h_fit if spec.axis == "v" else w_fit
        s = min(driven, h_fit, w_fit)
        if s < driven:
            if spec.axis == "v":
                limit = "页面宽度" if spec.side_cm is None else f"左右{spec.side_cm:.2f}cm距离"
                notes.append(f"受{limit}限制只能缩放至{s:.0%}，"
                             f"下边距将大于要求的{spec.bottom_cm:.2f}cm")
            else:
                notes.append(f"受上{spec.top_cm:.2f}cm/下{spec.bottom_cm:.2f}cm距离限制"
                             f"只能缩放至{s:.0%}，"
                             f"左右边距将大于要求的{spec.side_cm:.2f}cm")
        sx = sy = clamp(s, "缩放")
    elif spec.axis == "v":
        sy = clamp(h_fit, "上下")
        sx = clamp(min(spec.ratio_x, display_w / content_w), "左右")
    elif spec.axis == "h":
        sx = clamp(w_fit, "左右")
        sy = clamp(min(spec.ratio_y, display_h / content_h), "上下")
    else:
        sx = clamp(min(spec.ratio_x, display_w / content_w), "左右")
        sy = clamp(min(spec.ratio_y, display_h / content_h), "上下")

    cw, ch = content_w * sx, content_h * sy
    if spec.align == "top":
        cy = top
    elif spec.align == "bottom":
        cy = display_h - bottom - ch
    else:
        cy = top + (avail_h - ch) / 2
    if spec.side_cm is not None:
        cx = side + (avail_w - cw) / 2
    else:
        cx = (display_w - cw) / 2
    return sx, sy, cx - bbox.x0 * sx, cy - bbox.y0 * sy, notes


def flatten_page_for_placement(src_doc: "pymupdf.Document", page_index: int
                               ) -> Tuple["pymupdf.Document", int, bool]:
    """
    得到一张 rotation=0、但视觉与源页完全一致的页面，供 show_pdf_page 按显示坐标摆放。

    源页本身没有 /Rotate 时直接返回源文档（不产生临时文档）。
    show_pdf_page 不读取源页的 /Rotate，旧代码靠手工拼 rotate 参数补救，
    在 90°/270° 页面上会把内容转成上下颠倒的方向，因此统一改用 remove_rotation 展平。

    Returns:
        (文档, 页索引, 是否为需要调用方关闭的临时文档)
    """
    page = src_doc[page_index]
    if page.rotation % 360 == 0:
        return src_doc, page_index, False

    tmp = pymupdf.open()
    tmp.insert_pdf(src_doc, from_page=page_index, to_page=page_index)
    tmp[0].remove_rotation()
    buf = io.BytesIO()
    tmp.save(buf)
    tmp.close()
    buf.seek(0)
    return pymupdf.open(stream=buf.getvalue(), filetype="pdf"), 0, True


# 印发说明页（只有页脚几行字）版式固定，不能重新摆放
FIT_ANALYZE_DPI = 110
# 内容高度占页面高度低于这个百分比、且重心落在下半页时判为印发说明页
DEFAULT_SMALL_CONTENT_RATIO = 15.0
SMALL_CONTENT_CENTER = 0.5


def _drop_dust(ink):
    """
    去掉纸边残点和灰尘，只留下真正的笔画/图形。

    扫描时压在纸边上的残点常常只是一条 15~16 像素的 1px 细条，而一个汉字有
    几百到几千像素。它不过滤的话会把内容外接框撑到纸边，"按边距缩放"就对着
    纸边对齐，整页文字被顶下去几厘米（实测红头页顶部 2.8cm 变成 9.28cm）。
    """
    import numpy as np
    import cv2

    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        ink.astype(np.uint8), 8)
    if count <= 1:
        return ink
    min_area = max(30, int(ink.size * 0.00002))
    keep = np.zeros(count, dtype=bool)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= min_area
    return keep[labels]


def analyze_page_for_fit(page: "pymupdf.Page",
                         small_content_ratio: float = DEFAULT_SMALL_CONTENT_RATIO,
                         dpi: int = FIT_ANALYZE_DPI):
    """
    一次渲染同时得到内容外接框和"印发说明页"判定。

    用渲染而不是取文本/图形列表，是因为扫描件、图片以及电子印章注释
    （PyMuPDF 的 get_text/get_drawings 看不见它们）都要算进内容范围。

    Args:
        page: 待分析的页面
        small_content_ratio: 内容高度占页面高度的百分比阈值，低于它判为印发说明页
        dpi: 分析渲染分辨率

    Returns:
        (bbox, reason, metrics)
        bbox: 内容外接框（页面显示坐标，单位点），无法判定为 None
        reason: "印发说明页"，不需要跳过时为 None
        metrics: 判定用到的量化值，供日志和预览提示显示
    """
    import numpy as np

    zoom = dpi / 72.0
    try:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8)
        arr = arr.reshape(pix.height, pix.width, pix.n)[:, :, :3]
    except Exception as e:
        logger.debug(f"页面分析渲染失败: {e}")
        return None, None, {}

    gray = arr.mean(axis=2)
    ink = _drop_dust(gray < 200)

    def bbox_of(mask):
        rows = np.nonzero(mask.sum(axis=1) >= 2)[0]
        cols = np.nonzero(mask.sum(axis=0) >= 2)[0]
        if len(rows) and len(cols):
            return pymupdf.Rect(cols[0] / zoom, rows[0] / zoom,
                                (cols[-1] + 1) / zoom, (rows[-1] + 1) / zoom)
        return None

    page_w = float(page.rect.width)
    page_h = float(page.rect.height)

    bbox = None
    if ink.any() and ink.mean() <= 0.9:
        bbox = bbox_of(ink)
        # 扫描时贴在纸边的黑框不是内容，会把外接框撑成整页，
        # 于是"按边距缩放"对齐的是纸边而不是文字，量之前先按瑕疵掩码剔掉
        if (bbox is not None
                and (bbox.height >= 0.9 * page_h or bbox.width >= 0.9 * page_w)):
            from pdf_page_remover.scan_cleaner import scan_artifact_zone

            zone, _ = scan_artifact_zone(arr, dpi / 2.54)
            if zone is not None and zone.any():
                cleaned = bbox_of(ink & ~zone)
                if cleaned is not None:
                    bbox = cleaned
    elif ink.mean() > 0.9:
        # 整页偏暗（扫描件底色），无法区分内容边界
        bbox = pymupdf.Rect(0, 0, pix.width / zoom, pix.height / zoom)

    metrics = {}
    if bbox is not None and page_h > 0:
        metrics["content_h_pct"] = bbox.height / page_h * 100.0
        metrics["content_center_pct"] = ((bbox.y0 + bbox.y1) / 2) / page_h * 100.0

    reason = None
    if ("content_h_pct" in metrics
            and metrics["content_h_pct"] < small_content_ratio
            and metrics["content_center_pct"] > SMALL_CONTENT_CENTER * 100.0):
        reason = "印发说明页"
    return bbox, reason, metrics


# 常见纸张尺寸（宽度, 高度），单位：点（1点=1/72英寸）
# 纵向格式
PAGE_SIZES = {
    "A4": (595.28, 841.89),
    "A3": (841.89, 1190.55),
    "A5": (419.53, 595.28),
    "Letter": (612, 792),
    "Legal": (612, 1008),
    "B5": (498.90, 708.66),
    "B4": (708.66, 1000.63),
}


class PDFScaler:
    """PDF内容比例缩放器"""

    def __init__(self, dpi: int = 150):
        """
        初始化缩放器

        Args:
            dpi: 渲染预览/处理时使用的DPI
        """
        self.dpi = dpi

    def render_page_preview(
        self,
        pdf_path: str,
        page_index: int = 0,
        scale_ratio: float = 1.0,
        preview_width: int = 400,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
    ) -> Optional[Image.Image]:
        """
        渲染PDF页面预览图（带缩放和偏移效果）

        Args:
            pdf_path: PDF文件路径
            page_index: 页面索引（从0开始）
            scale_ratio: 缩放比例（0.1~1.0），1.0表示100%原大小
            preview_width: 预览图宽度（像素）
            offset_x: 水平偏移（点数，正=右移，负=左移）
            offset_y: 垂直偏移（点数，正=下移，负=上移）

        Returns:
            PIL Image对象，失败返回None
        """
        try:
            doc = pymupdf.open(pdf_path)
            if page_index >= len(doc):
                doc.close()
                return None

            page = doc[page_index]
            page_rect = page.rect

            # 计算渲染缩放因子，使预览图宽度接近 preview_width
            render_zoom = preview_width / page_rect.width
            mat = pymupdf.Matrix(render_zoom, render_zoom)
            pix = page.get_pixmap(matrix=mat)

            img_data = pix.tobytes("png")
            img = Image.open(io.BytesIO(img_data))

            doc.close()

            # 应用缩放效果到预览图
            if scale_ratio < 1.0 or offset_x != 0 or offset_y != 0:
                # 计算缩放后的内容尺寸
                content_w = int(img.width * scale_ratio)
                content_h = int(img.height * scale_ratio)

                # 创建与原始预览同尺寸的白色画布
                canvas = Image.new("RGB", (img.width, img.height), (255, 255, 255))

                # 将缩放后的内容居中放置，再叠加偏移
                # 偏移量按渲染缩放比例转换到像素
                pixel_offset_x = int(offset_x * render_zoom)
                pixel_offset_y = int(offset_y * render_zoom)
                pos_x = (img.width - content_w) // 2 + pixel_offset_x
                pos_y = (img.height - content_h) // 2 + pixel_offset_y

                # 缩放内容图像（使用高质量LANCZOS重采样）
                scaled_content = img.resize(
                    (content_w, content_h), Image.LANCZOS
                )
                canvas.paste(scaled_content, (pos_x, pos_y))

                return canvas
            else:
                return img

        except Exception as e:
            logger.error(f"渲染预览失败: {e}")
            return None

    def render_page_preview_from_doc(
        self,
        doc: pymupdf.Document,
        page_index: int = 0,
        scale_ratio: float = 1.0,
        preview_width: int = 400,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
    ) -> Optional[Image.Image]:
        """
        从已打开的Document渲染预览图（避免重复打开文件）

        Args:
            doc: 已打开的PyMuPDF Document
            page_index: 页面索引
            scale_ratio: 缩放比例
            preview_width: 预览图宽度
            offset_x: 水平偏移（点数，正=右移，负=左移）
            offset_y: 垂直偏移（点数，正=下移，负=上移）

        Returns:
            PIL Image对象
        """
        try:
            if page_index >= len(doc):
                return None

            page = doc[page_index]
            page_rect = page.rect

            render_zoom = preview_width / page_rect.width
            mat = pymupdf.Matrix(render_zoom, render_zoom)
            pix = page.get_pixmap(matrix=mat)

            img_data = pix.tobytes("png")
            img = Image.open(io.BytesIO(img_data))

            if scale_ratio < 1.0 or offset_x != 0 or offset_y != 0:
                content_w = int(img.width * scale_ratio)
                content_h = int(img.height * scale_ratio)

                canvas = Image.new("RGB", (img.width, img.height), (255, 255, 255))
                pixel_offset_x = int(offset_x * render_zoom)
                pixel_offset_y = int(offset_y * render_zoom)
                pos_x = (img.width - content_w) // 2 + pixel_offset_x
                pos_y = (img.height - content_h) // 2 + pixel_offset_y

                scaled_content = img.resize(
                    (content_w, content_h), Image.LANCZOS
                )
                canvas.paste(scaled_content, (pos_x, pos_y))
                return canvas
            else:
                return img

        except Exception as e:
            logger.error(f"渲染预览失败: {e}")
            return None

    def render_fit_preview_from_doc(
        self,
        doc: pymupdf.Document,
        page_index: int = 0,
        spec: Optional[FitSpec] = None,
        preview_width: int = 400,
        auto_skip_special: bool = True,
        small_content_ratio: float = DEFAULT_SMALL_CONTENT_RATIO,
    ) -> Tuple[Optional[Image.Image], Optional[str]]:
        """
        渲染"缩放并摆放"的预览图：用处理时同一份 compute_fit_placement 算几何，
        再画出要求的边距参考线，所以预览看到的就是输出会得到的样子。

        Returns:
            (PIL Image对象或None, 该页被跳过的原因或None)
        """
        try:
            from PIL import ImageDraw

            spec = spec or FitSpec()
            if page_index >= len(doc):
                return None, None
            page = doc[page_index]
            render_zoom = preview_width / page.rect.width
            pix = page.get_pixmap(matrix=pymupdf.Matrix(render_zoom, render_zoom), alpha=False)
            img = Image.open(io.BytesIO(pix.tobytes("png")))

            bbox, reason, _ = analyze_page_for_fit(page, small_content_ratio)
            if reason and not auto_skip_special:
                reason = None
            if bbox is None or bbox.width <= 1 or bbox.height <= 1:
                return img, reason or "未识别到内容边界"  # 处理时也是原样保留
            if reason:
                return img, reason

            # 几何全在像素空间算：边距按 zoom 换算过去，比例和判定与点数空间完全一致
            px_spec = replace(spec,
                              top_cm=spec.top_cm * render_zoom,
                              bottom_cm=spec.bottom_cm * render_zoom,
                              side_cm=(spec.side_cm * render_zoom
                                       if spec.side_cm is not None else None))
            try:
                sx, sy, x0, y0, _ = compute_fit_placement(
                    px_spec, float(img.width), float(img.height), bbox * render_zoom)
            except ValueError:
                return img, None  # 边距之和超过页面，处理时会报错，这里就显示原图

            scaled = img.resize((max(1, int(img.width * sx)), max(1, int(img.height * sy))),
                                Image.LANCZOS)
            canvas = Image.new("RGB", (img.width, img.height), (255, 255, 255))
            canvas.paste(scaled, (int(x0), int(y0)))

            draw = ImageDraw.Draw(canvas)
            line_color = (200, 60, 60)
            top_px = spec.top_cm * CM_TO_PT * render_zoom
            bottom_px = spec.bottom_cm * CM_TO_PT * render_zoom
            self._draw_dashed_h_line(draw, int(top_px), canvas.width, canvas.height, line_color)
            self._draw_dashed_h_line(draw, int(canvas.height - bottom_px),
                                     canvas.width, canvas.height, line_color)
            if spec.side_cm is not None:
                side_px = spec.side_cm * CM_TO_PT * render_zoom
                self._draw_dashed_v_line(draw, int(side_px), canvas.width, canvas.height, line_color)
                self._draw_dashed_v_line(draw, int(canvas.width - side_px),
                                         canvas.width, canvas.height, line_color)
            return canvas, None

        except Exception as e:
            logger.error(f"渲染边距预览失败: {e}")
            return None, None

    @staticmethod
    def _draw_dashed_h_line(draw, y: int, width: int, height: int, color,
                            dash=6, gap=4):
        if y < 0 or y >= height:
            return
        x = 0
        while x < width:
            draw.line([(x, y), (min(x + dash, width), y)], fill=color)
            x += dash + gap

    @staticmethod
    def _draw_dashed_v_line(draw, x: int, width: int, height: int, color,
                            dash=6, gap=4):
        if x < 0 or x >= width:
            return
        y = 0
        while y < height:
            draw.line([(x, y), (x, min(y + dash, height))], fill=color)
            y += dash + gap

    def scale_pdf(
        self,
        input_path: str,
        output_path: str,
        scale_ratio: float = 1.0,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        page_range: str = "",
        progress_callback: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        """
        缩放PDF内容比例并支持偏移

        将每页内容按scale_ratio缩放，保持页面尺寸不变，内容居中放置后叠加偏移。
        使用矢量方式（show_pdf_page）保留原始矢量质量。

        Args:
            input_path: 输入PDF路径
            output_path: 输出PDF路径
            scale_ratio: 缩放比例（0.1~1.0）
            offset_x: 水平偏移（点数，正=右移，负=左移）
            offset_y: 垂直偏移（点数，正=下移，负=上移）
            page_range: 页面范围字符串（如"3,5-7"），空字符串表示所有页面
            progress_callback: 进度回调 callback(page_index, total_pages, message)

        Returns:
            处理结果统计
        """
        from pdf_page_remover.rotator import parse_page_range

        if scale_ratio == 1.0 and offset_x == 0 and offset_y == 0:
            # 无需缩放和偏移，直接复制
            import shutil
            shutil.copy2(input_path, output_path)
            return {
                "total_pages": 0,
                "status": "skipped",
                "message": "缩放比例为100%且无偏移，无需处理",
            }

        src_doc = pymupdf.open(input_path)
        total_pages = len(src_doc)
        stats = {
            "total_pages": total_pages,
            "scale_ratio": scale_ratio,
            "offset_x": offset_x,
            "offset_y": offset_y,
            "status": "success",
        }

        # 解析页面范围
        page_indices = parse_page_range(page_range, total_pages) if page_range else set(range(total_pages))

        # 创建新文档，使用矢量方式保留原始质量
        new_doc = pymupdf.open()

        for page_index in range(total_pages):
            if progress_callback:
                progress_callback(
                    page_index, total_pages,
                    f"正在缩放第 {page_index + 1} 页..."
                )

            src_page = src_doc[page_index]
            page_rect = src_page.rect

            # 不在范围内的页面：直接复制原页面
            if page_index not in page_indices:
                new_doc.insert_pdf(src_doc, from_page=page_index, to_page=page_index)
                continue

            # 处理页面rotation属性
            # show_pdf_page 不读取源页的 /Rotate，先把源页展平成 rotation=0
            # 且视觉一致的页面，之后所有坐标都是显示坐标，无需再传 rotate 参数。
            flat_doc, src_page_index, is_temp_flat = flatten_page_for_placement(
                src_doc, page_index)
            src_doc_for_insert = flat_doc
            display_w = flat_doc[src_page_index].rect.width
            display_h = flat_doc[src_page_index].rect.height
            insert_rotate = 0

            # 创建同尺寸的新页面（使用原来的显示尺寸，保持视觉方向）
            new_page = new_doc.new_page(width=display_w, height=display_h)

            # 计算缩放后的内容尺寸（基于显示尺寸）
            scaled_width = display_w * scale_ratio
            scaled_height = display_h * scale_ratio

            # 计算居中偏移 + 用户偏移
            center_offset_x = (display_w - scaled_width) / 2
            center_offset_y = (display_h - scaled_height) / 2
            final_offset_x = center_offset_x + offset_x
            final_offset_y = center_offset_y + offset_y

            # 目标矩形
            target_rect = pymupdf.Rect(
                final_offset_x, final_offset_y,
                final_offset_x + scaled_width, final_offset_y + scaled_height
            )

            # 使用矢量方式插入原页面内容（保留矢量质量，无重影）
            new_page.show_pdf_page(target_rect, src_doc_for_insert, src_page_index)

            # show_pdf_page 不带注释，电子印章/签章需按同一变换补回
            copied = copy_page_annotations(
                src_doc_for_insert, src_doc_for_insert[src_page_index],
                new_doc, new_page, target_rect, insert_rotate
            )
            if copied:
                stats["copied_annot_count"] = stats.get("copied_annot_count", 0) + copied

            if is_temp_flat:
                flat_doc.close()

        # 保存
        carry_doc_extras(src_doc, new_doc)
        new_doc.save(output_path, garbage=4, deflate=True)
        new_doc.close()
        src_doc.close()

        if progress_callback:
            progress_callback(total_pages, total_pages, "缩放完成！")

        return stats

    def fit_margins_pdf(
        self,
        input_path: str,
        output_path: str,
        spec: FitSpec,
        page_range: str = "",
        auto_skip_special: bool = True,
        small_content_ratio: float = DEFAULT_SMALL_CONTENT_RATIO,
        skip_pages: str = "",
        progress_callback: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        """
        按 spec 把每页内容缩放并摆放到指定边距上，页面尺寸不变。

        三种缩放方式（spec.kind）都走这一份代码，几何全在 compute_fit_placement 里，
        预览用的是同一个函数，所以预览和输出不会各说各话：
          align   对齐缩放：内容大小以 spec.ratio_y 为准，贴住 spec.align 那条边，
                  只有对侧越过边距线时才继续等比缩小（两侧对齐时大小由边距决定）。
          uniform 等比缩放：等比缩放到 spec.axis 那个方向的边距精确成立，可放大可缩小。
          stretch 根据边距缩放：spec.axis 方向贴住边距（非等比拉伸，表格类文件用），
                  另一方向按 spec.ratio 不缩放，只按对齐方式摆放。

        使用矢量方式（show_pdf_page）不重排文字；电子印章等注释按同一变换补回。

        只剩几行字的印发说明页版式是固定的，摆放它们会破坏格式，
        因此默认自动识别并原样保留（auto_skip_special），也可用 skip_pages 手动指定。

        Args:
            input_path: 输入PDF路径
            output_path: 输出PDF路径
            spec: 缩放与摆放参数（见 FitSpec）
            page_range: 页面范围字符串（如"3,5-7"），空字符串表示所有页面
            auto_skip_special: 是否自动跳过印发说明页
            small_content_ratio: 判为印发说明页的内容高度占比阈值（百分比）
            skip_pages: 手动指定跳过的页码（如"1,12"），空字符串表示不指定
            progress_callback: 进度回调 callback(page_index, total_pages, message)

        Returns:
            处理结果统计，pages 里带每页实际边距
        """
        from pdf_page_remover.rotator import parse_page_range

        spec.validate()

        src_doc = pymupdf.open(input_path)
        total_pages = len(src_doc)
        page_indices = parse_page_range(page_range, total_pages) if page_range else set(range(total_pages))
        manual_skip = (parse_page_range(skip_pages, total_pages)
                       if skip_pages and skip_pages.strip() else set())

        new_doc = pymupdf.open()
        stats = {
            "total_pages": total_pages,
            "spec": spec.describe(),
            "kind": spec.kind,
            "align": spec.align,
            "axis": spec.axis,
            "top_cm": spec.top_cm,
            "bottom_cm": spec.bottom_cm,
            "auto_skip_special": auto_skip_special,
            "small_content_ratio": small_content_ratio,
            "ratio_x": spec.ratio_x,
            "ratio_y": spec.ratio_y,
            "side_cm": spec.side_cm,
            "status": "success",
            "pages": [],
        }

        for page_index in range(total_pages):
            if progress_callback:
                progress_callback(page_index, total_pages, f"正在缩放摆放第 {page_index + 1} 页...")

            # 不在范围内的页面：原样复制（insert_pdf 会保留注释）
            if page_index not in page_indices:
                new_doc.insert_pdf(src_doc, from_page=page_index, to_page=page_index)
                continue

            if page_index in manual_skip:
                new_doc.insert_pdf(src_doc, from_page=page_index, to_page=page_index)
                stats["pages"].append({
                    "page_index": page_index,
                    "skipped": True,
                    "reason": "手动指定跳过",
                })
                continue

            flat_doc, src_page_index, is_temp_flat = flatten_page_for_placement(src_doc, page_index)
            src_page = flat_doc[src_page_index]
            display_w = src_page.rect.width
            display_h = src_page.rect.height

            bbox, skip_reason, metrics = analyze_page_for_fit(src_page, small_content_ratio)
            if not auto_skip_special:
                skip_reason = None

            if skip_reason or bbox is None or bbox.is_empty or bbox.width <= 1 or bbox.height <= 1:
                # 特殊页保持原样；空白页无法判断内容边界，也保持原样
                new_doc.insert_pdf(src_doc, from_page=page_index, to_page=page_index)
                if is_temp_flat:
                    flat_doc.close()
                reason = skip_reason or "未识别到内容边界"
                page_stat = {"page_index": page_index, "skipped": True, "reason": reason}
                if "content_h_pct" in metrics:
                    page_stat["content_h_pct"] = round(metrics["content_h_pct"], 1)
                stats["pages"].append(page_stat)
                logger.info(f"第{page_index + 1}页: {reason}，保持原样")
                continue

            sx, sy, target_x0, target_y0, notes = compute_fit_placement(
                spec, display_w, display_h, bbox)
            for note in notes:
                logger.info(f"第{page_index + 1}页: {note}")

            target_rect = pymupdf.Rect(
                target_x0, target_y0,
                target_x0 + display_w * sx, target_y0 + display_h * sy
            )

            new_page = new_doc.new_page(width=display_w, height=display_h)
            # keep_proportion 默认 True 会把非等比的目标矩形拉回等比，"水平拉伸"直接失效
            new_page.show_pdf_page(target_rect, flat_doc, src_page_index,
                                   keep_proportion=False)

            # show_pdf_page 不带注释，电子印章/签章需按同一变换补回
            copied = copy_page_annotations(
                flat_doc, src_page, new_doc, new_page, target_rect, 0
            )
            if copied:
                stats["copied_annot_count"] = stats.get("copied_annot_count", 0) + copied

            if is_temp_flat:
                flat_doc.close()

            actual_top = (target_y0 + bbox.y0 * sy) / CM_TO_PT
            actual_bottom = (display_h - (target_y0 + bbox.y1 * sy)) / CM_TO_PT
            actual_left = (target_x0 + bbox.x0 * sx) / CM_TO_PT
            actual_right = (display_w - (target_x0 + bbox.x1 * sx)) / CM_TO_PT
            page_stat = {
                "page_index": page_index,
                "scale": round(sx, 4),
                "scale_x": round(sx, 4),
                "scale_y": round(sy, 4),
                "actual_top_cm": round(actual_top, 2),
                "actual_bottom_cm": round(actual_bottom, 2),
                "actual_left_cm": round(actual_left, 2),
                "actual_right_cm": round(actual_right, 2),
            }
            stats["pages"].append(page_stat)
            scale_text = (f"{sx:.0%}" if abs(sx - sy) < 1e-6 else f"横向{sx:.0%} 纵向{sy:.0%}")
            logger.info(
                f"第{page_index + 1}页: 内容缩放 {scale_text}，"
                f"上边距 {actual_top:.2f}cm 下边距 {actual_bottom:.2f}cm "
                f"左边距 {actual_left:.2f}cm 右边距 {actual_right:.2f}cm"
            )

        carry_doc_extras(src_doc, new_doc)
        new_doc.save(output_path, garbage=4, deflate=True)
        new_doc.close()
        src_doc.close()

        stats["processed_pages"] = sum(1 for p in stats["pages"] if not p.get("skipped"))
        stats["skipped_pages"] = sum(1 for p in stats["pages"] if p.get("skipped"))

        if progress_callback:
            progress_callback(total_pages, total_pages, "边距调整完成！")

        return stats


def batch_fit_margins(
    input_files: List[str],
    output_dir: str,
    spec: FitSpec,
    page_range: str = "",
    auto_skip_special: bool = True,
    small_content_ratio: float = DEFAULT_SMALL_CONTENT_RATIO,
    skip_pages: str = "",
    dpi: int = 200,
    progress_callback: Optional[Callable] = None,
) -> List[Dict]:
    """
    批量按 spec 缩放并摆放内容（对齐缩放 / 等比缩放 / 根据边距缩放都走这里）

    Args:
        input_files: 输入PDF文件路径列表
        output_dir: 输出目录
        spec: 缩放与摆放参数（见 FitSpec）
        page_range: 页面范围字符串（如"3,5-7"）
        auto_skip_special: 自动跳过印发说明页
        small_content_ratio: 印发说明页判定的内容高度占比阈值（百分比）
        skip_pages: 手动指定跳过的页码（如"1,12"）
        dpi: 渲染DPI
        progress_callback: 进度回调 callback(file_index, total_files, page_index, total_pages, message)

    Returns:
        所有文件的处理结果统计列表
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    scaler = PDFScaler(dpi=dpi)
    all_stats = []
    total_files = len(input_files)

    for file_index, input_path in enumerate(input_files):
        if not os.path.exists(input_path):
            logger.warning(f"文件不存在: {input_path}")
            continue

        filename = os.path.basename(input_path)
        output_path = os.path.join(output_dir, filename)

        def page_progress(page_index, total_pages, message):
            if progress_callback:
                progress_callback(file_index, total_files, page_index, total_pages, message)

        try:
            stats = scaler.fit_margins_pdf(
                input_path, output_path,
                spec=spec,
                page_range=page_range,
                auto_skip_special=auto_skip_special,
                small_content_ratio=small_content_ratio,
                skip_pages=skip_pages,
                progress_callback=page_progress,
            )
            stats["input_file"] = input_path
            stats["output_file"] = output_path
            all_stats.append(stats)
            logger.info(f"缩放摆放完成: {filename} - {spec.describe()}")

        except Exception as e:
            logger.error(f"边距缩放失败: {filename} - {e}")
            all_stats.append({
                "input_file": input_path,
                "output_file": output_path,
                "status": "error",
                "error": str(e),
            })

    return all_stats


def batch_scale(
    input_files: List[str],
    output_dir: str,
    scale_ratio: float = 1.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    page_range: str = "",
    dpi: int = 200,
    progress_callback: Optional[Callable] = None,
) -> List[Dict]:
    """
    批量缩放PDF文件内容比例

    Args:
        input_files: 输入PDF文件路径列表
        output_dir: 输出目录
        scale_ratio: 缩放比例
        offset_x: 水平偏移（点数，正=右移，负=左移）
        offset_y: 垂直偏移（点数，正=下移，负=上移）
        page_range: 页面范围字符串（如"3,5-7"），空字符串表示所有页面
        dpi: 渲染DPI
        progress_callback: 进度回调 callback(file_index, total_files, page_index, total_pages, message)

    Returns:
        所有文件的处理结果统计列表
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    scaler = PDFScaler(dpi=dpi)
    all_stats = []
    total_files = len(input_files)

    for file_index, input_path in enumerate(input_files):
        if not os.path.exists(input_path):
            logger.warning(f"文件不存在: {input_path}")
            continue

        filename = os.path.basename(input_path)
        output_path = os.path.join(output_dir, filename)

        def page_progress(page_index, total_pages, message):
            if progress_callback:
                progress_callback(file_index, total_files, page_index, total_pages, message)

        try:
            stats = scaler.scale_pdf(
                input_path, output_path,
                scale_ratio=scale_ratio,
                offset_x=offset_x,
                offset_y=offset_y,
                page_range=page_range,
                progress_callback=page_progress,
            )
            stats["input_file"] = input_path
            stats["output_file"] = output_path
            all_stats.append(stats)
            logger.info(f"缩放完成: {filename} - 比例 {scale_ratio:.0%} 偏移({offset_x}, {offset_y})")

        except Exception as e:
            logger.error(f"缩放失败: {filename} - {e}")
            all_stats.append({
                "input_file": input_path,
                "output_file": output_path,
                "status": "error",
                "error": str(e),
            })

    return all_stats


def resize_pdf(
    input_path: str,
    output_path: str,
    target_size: str = "A4",
    content_scale: float = 1.0,
    page_range: str = "",
    progress_callback: Optional[Callable] = None,
) -> Dict[str, Any]:
    """
    修改PDF页面尺寸，同时内容按比例适配

    将每页调整为指定纸张尺寸，原内容等比缩放后居中放置在新页面上。
    当页面方向与目标方向不一致时，自动旋转内容以最佳适配。
    使用矢量方式（show_pdf_page）保留原始矢量质量。

    Args:
        input_path: 输入PDF路径
        output_path: 输出PDF路径
        target_size: 目标纸张尺寸名称（如"A4", "Letter"）或"widthxheight"格式（点数）
        content_scale: 内容额外缩放系数（1.0=按原比例适配，0.9=缩小到90%）
        page_range: 页面范围字符串（如"3,5-7"），空字符串表示所有页面
        progress_callback: 进度回调 callback(page_index, total_pages, message)

    Returns:
        处理结果统计
    """
    from pdf_page_remover.rotator import parse_page_range

    # 解析目标尺寸
    if target_size in PAGE_SIZES:
        target_w, target_h = PAGE_SIZES[target_size]
    else:
        # 尝试解析 "widthxheight" 格式（点数）
        try:
            parts = target_size.lower().split("x")
            target_w = float(parts[0])
            target_h = float(parts[1])
        except (ValueError, IndexError):
            raise ValueError(f"无法解析纸张尺寸: {target_size}。请使用预设名称(如A4)或widthxheight格式(如595x842)")

    src_doc = pymupdf.open(input_path)
    total_pages = len(src_doc)
    stats = {
        "total_pages": total_pages,
        "resized_count": 0,
        "target_size": target_size,
        "status": "success",
    }

    # 解析页面范围
    page_indices = parse_page_range(page_range, total_pages) if page_range else set(range(total_pages))

    # 判断目标方向
    target_is_portrait = target_h >= target_w

    # 创建新文档，使用矢量方式保留原始质量
    new_doc = pymupdf.open()

    for page_index in range(total_pages):
        if progress_callback:
            progress_callback(page_index, total_pages, f"正在调整第 {page_index + 1} 页尺寸...")

        src_page = src_doc[page_index]

        if page_index not in page_indices:
            # 不在范围内，原样复制
            new_doc.insert_pdf(src_doc, from_page=page_index, to_page=page_index)
            continue

        # show_pdf_page 不读取源页的 /Rotate，先把源页展平成 rotation=0 且视觉一致的页面。
        # 之后 old_w/old_h 是显示尺寸，方向判断与摆放都在显示坐标下进行，
        # 避免出现 /Rotate 90/270 的页面处理后方向颠倒。
        flat_doc, src_page_index, is_temp_flat = flatten_page_for_placement(src_doc, page_index)
        src_doc_for_insert = flat_doc
        frect = flat_doc[src_page_index].rect
        old_w, old_h = frect.width, frect.height
        insert_rotate = 0

        # 检测页面方向，决定是否需要旋转内容
        # 基于MediaBox（实际内容尺寸）判断
        src_is_portrait = old_h >= old_w
        need_rotate = (src_is_portrait != target_is_portrait)

        if need_rotate:
            # 方向不匹配：使用show_pdf_page的rotate参数直接旋转
            # rotate=90 表示逆时针旋转90°，横向变纵向
            # 旋转后有效尺寸交换：宽=原高，高=原宽
            eff_w, eff_h = old_h, old_w
            rotate_angle = 90
        else:
            # 方向匹配：无需旋转
            eff_w, eff_h = old_w, old_h
            rotate_angle = 0

        # 计算内容适配比例
        scale_x = target_w / eff_w if eff_w > 0 else 1.0
        scale_y = target_h / eff_h if eff_h > 0 else 1.0
        fit_scale = min(scale_x, scale_y) * content_scale

        new_content_w = eff_w * fit_scale
        new_content_h = eff_h * fit_scale

        offset_x = (target_w - new_content_w) / 2
        offset_y = (target_h - new_content_h) / 2

        # 创建目标尺寸的新页面
        new_page = new_doc.new_page(width=target_w, height=target_h)
        target_rect = pymupdf.Rect(
            offset_x, offset_y,
            offset_x + new_content_w, offset_y + new_content_h
        )
        # 计算总旋转角度：源页面旋转 + 方向匹配旋转
        total_rotate = (insert_rotate + rotate_angle) % 360
        if total_rotate != 0:
            new_page.show_pdf_page(target_rect, src_doc_for_insert, src_page_index, rotate=total_rotate)
        else:
            new_page.show_pdf_page(target_rect, src_doc_for_insert, src_page_index)

        # show_pdf_page 不带注释，电子印章/签章需按同一变换补回
        copied = copy_page_annotations(
            src_doc_for_insert, src_doc_for_insert[src_page_index],
            new_doc, new_page, target_rect, total_rotate
        )
        if copied:
            stats["copied_annot_count"] = stats.get("copied_annot_count", 0) + copied

        # 关闭展平文档（如果有）
        if is_temp_flat:
            flat_doc.close()

        stats["resized_count"] += 1

    # 保存
    carry_doc_extras(src_doc, new_doc)
    new_doc.save(output_path, garbage=4, deflate=True)
    new_doc.close()
    src_doc.close()

    if progress_callback:
        progress_callback(total_pages, total_pages, "尺寸调整完成！")

    return stats


def batch_resize(
    input_files: List[str],
    output_dir: str,
    target_size: str = "A4",
    content_scale: float = 1.0,
    page_range: str = "",
    progress_callback: Optional[Callable] = None,
) -> List[Dict]:
    """
    批量修改PDF页面尺寸

    Args:
        input_files: 输入PDF文件路径列表
        output_dir: 输出目录
        target_size: 目标纸张尺寸名称
        content_scale: 内容额外缩放系数
        page_range: 页面范围字符串
        progress_callback: 进度回调 callback(file_index, total_files, page_index, total_pages, message)

    Returns:
        所有文件的处理结果统计列表
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    all_stats = []
    total_files = len(input_files)

    for file_index, input_path in enumerate(input_files):
        if not os.path.exists(input_path):
            logger.warning(f"文件不存在: {input_path}")
            continue

        filename = os.path.basename(input_path)
        output_path = os.path.join(output_dir, filename)

        def page_progress(page_index, total_pages, message):
            if progress_callback:
                progress_callback(file_index, total_files, page_index, total_pages, message)

        try:
            stats = resize_pdf(
                input_path, output_path,
                target_size=target_size,
                content_scale=content_scale,
                page_range=page_range,
                progress_callback=page_progress,
            )
            stats["input_file"] = input_path
            stats["output_file"] = output_path
            all_stats.append(stats)
            logger.info(f"尺寸调整完成: {filename} - 目标尺寸 {target_size}")

        except Exception as e:
            logger.error(f"尺寸调整失败: {filename} - {e}")
            all_stats.append({
                "input_file": input_path,
                "output_file": output_path,
                "status": "error",
                "error": str(e),
            })

    return all_stats


def parse_page_range(page_range_str: str, total_pages: int) -> Set[int]:
    """
    解析页面范围字符串为页码集合（0-based索引）

    支持格式：
    - "3,5-7" → 第3页和第5-7页 → {2, 4, 5, 6}
    - "1-10" → 第1-10页
    - "all" 或空字符串 → 所有页面
    - "3,5,7" → 第3、5、7页

    Args:
        page_range_str: 页面范围字符串（1-based输入）
        total_pages: 总页数

    Returns:
        0-based页码索引集合
    """
    if not page_range_str or page_range_str.strip().lower() == "all":
        return set(range(total_pages))

    result = set()
    parts = page_range_str.split(",")

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if "-" in part:
            range_parts = part.split("-", 1)
            try:
                start = int(range_parts[0].strip())
                end = int(range_parts[1].strip())
                for page in range(start, end + 1):
                    idx = page - 1
                    if 0 <= idx < total_pages:
                        result.add(idx)
            except (ValueError, IndexError):
                logger.warning(f"无法解析页面范围: {part}")
                continue
        else:
            try:
                page = int(part)
                idx = page - 1
                if 0 <= idx < total_pages:
                    result.add(idx)
            except ValueError:
                logger.warning(f"无法解析页码: {part}")
                continue

    return result


class PDFResizer:
    """PDF页面尺寸修改器"""

    def __init__(self, dpi: int = 200):
        """
        初始化页面尺寸修改器

        Args:
            dpi: 渲染时使用的DPI
        """
        self.dpi = dpi

    def resize_pdf(
        self,
        input_path: str,
        output_path: str,
        target_size: str = "A4",
        page_indices: Optional[Set[int]] = None,
        progress_callback: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        """
        修改PDF页面尺寸，内容按比例适配到新页面

        将指定页面调整为标准纸张尺寸，内容等比缩放并居中放置。
        当页面方向与目标方向不一致时，自动旋转内容以最佳适配。
        使用矢量方式（show_pdf_page）保留原始矢量质量。
        未指定的页面保持原样。

        Args:
            input_path: 输入PDF路径
            output_path: 输出PDF路径
            target_size: 目标纸张尺寸名称（如"A4", "Letter"等）
            page_indices: 要修改的页面索引集合（0-based），None表示所有页面
            progress_callback: 进度回调 callback(page_index, total_pages, message)

        Returns:
            处理结果统计
        """
        if target_size not in PAGE_SIZES:
            return {
                "status": "error",
                "error": f"不支持的纸张尺寸: {target_size}",
            }

        target_w, target_h = PAGE_SIZES[target_size]

        src_doc = pymupdf.open(input_path)
        total_pages = len(src_doc)
        stats = {
            "total_pages": total_pages,
            "resized_count": 0,
            "target_size": target_size,
            "status": "success",
        }

        # 判断目标方向
        target_is_portrait = target_h >= target_w

        # 创建新文档，使用矢量方式保留原始质量
        new_doc = pymupdf.open()

        for page_index in range(total_pages):
            if progress_callback:
                progress_callback(
                    page_index, total_pages,
                    f"正在处理第 {page_index + 1} 页..."
                )

            src_page = src_doc[page_index]

            # 判断是否需要处理此页
            if page_indices is not None and page_index not in page_indices:
                # 不需要处理的页面，原样复制
                new_doc.insert_pdf(src_doc, from_page=page_index, to_page=page_index)
                continue

            # 检查页面是否已经是目标尺寸（允许小误差）
            # 使用显示尺寸（rect）判断，因为用户看到的是旋转后的效果
            disp_w, disp_h = src_page.rect.width, src_page.rect.height
            if (abs(disp_w - target_w) < 1 and abs(disp_h - target_h) < 1) or \
               (abs(disp_w - target_h) < 1 and abs(disp_h - target_w) < 1):
                # 已经是目标尺寸，原样复制
                new_doc.insert_pdf(src_doc, from_page=page_index, to_page=page_index)
                continue

            # show_pdf_page 不读取源页的 /Rotate，先把源页展平成 rotation=0 且视觉一致的页面。
            # 之后 old_w/old_h 是显示尺寸，方向判断与摆放都在显示坐标下进行，
            # 避免出现 /Rotate 90/270 的页面处理后方向颠倒。
            flat_doc, src_page_index, is_temp_flat = flatten_page_for_placement(src_doc, page_index)
            src_doc_for_insert = flat_doc
            frect = flat_doc[src_page_index].rect
            old_w, old_h = frect.width, frect.height
            insert_rotate = 0

            # 检测页面方向，决定是否需要旋转内容
            # 基于MediaBox（实际内容尺寸）判断
            src_is_portrait = old_h >= old_w
            need_rotate = (src_is_portrait != target_is_portrait)

            if need_rotate:
                # 方向不匹配：使用show_pdf_page的rotate参数直接旋转
                # rotate=90 表示逆时针旋转90°，横向变纵向
                # 旋转后有效尺寸交换：宽=原高，高=原宽
                eff_w, eff_h = old_h, old_w
                rotate_angle = 90
            else:
                # 方向匹配：无需旋转
                eff_w, eff_h = old_w, old_h
                rotate_angle = 0

            # 计算内容适配比例
            scale_x = target_w / eff_w if eff_w > 0 else 1.0
            scale_y = target_h / eff_h if eff_h > 0 else 1.0
            scale = min(scale_x, scale_y)

            scaled_w = eff_w * scale
            scaled_h = eff_h * scale
            offset_x = (target_w - scaled_w) / 2
            offset_y = (target_h - scaled_h) / 2

            new_page = new_doc.new_page(width=target_w, height=target_h)
            target_rect = pymupdf.Rect(
                offset_x, offset_y,
                offset_x + scaled_w, offset_y + scaled_h
            )
            # 计算总旋转角度：源页面旋转 + 方向匹配旋转
            total_rotate = (insert_rotate + rotate_angle) % 360
            if total_rotate != 0:
                new_page.show_pdf_page(target_rect, src_doc_for_insert, src_page_index, rotate=total_rotate)
            else:
                new_page.show_pdf_page(target_rect, src_doc_for_insert, src_page_index)

            # 关闭展平文档（如果有）
            if is_temp_flat:
                flat_doc.close()

            stats["resized_count"] += 1

        # 保存文档
        carry_doc_extras(src_doc, new_doc)
        new_doc.save(output_path, garbage=4, deflate=True)
        new_doc.close()
        src_doc.close()

        if progress_callback:
            progress_callback(total_pages, total_pages, "页面尺寸修改完成！")

        return stats


def batch_resize(
    input_files: List[str],
    output_dir: str,
    target_size: str = "A4",
    page_range: str = "",
    progress_callback: Optional[Callable] = None,
) -> List[Dict]:
    """
    批量修改PDF页面尺寸

    Args:
        input_files: 输入PDF文件路径列表
        output_dir: 输出目录
        target_size: 目标纸张尺寸名称
        page_range: 页面范围字符串（如"3,5-7"），空字符串表示所有页面
        progress_callback: 进度回调 callback(file_index, total_files, page_index, total_pages, message)

    Returns:
        所有文件的处理结果统计列表
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    resizer = PDFResizer()
    all_stats = []
    total_files = len(input_files)

    for file_index, input_path in enumerate(input_files):
        if not os.path.exists(input_path):
            logger.warning(f"文件不存在: {input_path}")
            continue

        filename = os.path.basename(input_path)
        output_path = os.path.join(output_dir, filename)

        # 解析页面范围
        page_indices = None
        if page_range:
            try:
                doc = pymupdf.open(input_path)
                total_pages = len(doc)
                doc.close()
                page_indices = parse_page_range(page_range, total_pages)
                if not page_indices:
                    logger.warning(f"页面范围'{page_range}'无效，跳过文件: {filename}")
                    all_stats.append({
                        "input_file": input_path,
                        "output_file": output_path,
                        "status": "skipped",
                        "message": f"页面范围'{page_range}'无效",
                    })
                    continue
            except Exception as e:
                logger.error(f"打开文件失败: {filename} - {e}")
                all_stats.append({
                    "input_file": input_path,
                    "output_file": output_path,
                    "status": "error",
                    "error": str(e),
                })
                continue

        def page_progress(page_index, total_pages, message):
            if progress_callback:
                progress_callback(file_index, total_files, page_index, total_pages, message)

        try:
            stats = resizer.resize_pdf(
                input_path, output_path,
                target_size=target_size,
                page_indices=page_indices,
                progress_callback=page_progress,
            )
            stats["input_file"] = input_path
            stats["output_file"] = output_path
            all_stats.append(stats)
            logger.info(f"页面尺寸修改完成: {filename} - 目标尺寸 {target_size}")

        except Exception as e:
            logger.error(f"页面尺寸修改失败: {filename} - {e}")
            all_stats.append({
                "input_file": input_path,
                "output_file": output_path,
                "status": "error",
                "error": str(e),
            })

    return all_stats


def resize_pdf(
    input_path: str,
    output_path: str,
    target_size: str = "A4",
    content_scale: float = 1.0,
    page_range: str = "",
    progress_callback: Optional[Callable] = None,
) -> Dict[str, Any]:
    """
    修改PDF页面尺寸，同时内容按比例适配

    将每页调整为指定纸张尺寸，原内容等比缩放后居中放置在新页面上。
    当页面方向与目标方向不一致时，自动旋转内容以最佳适配。
    使用矢量方式（show_pdf_page）保留原始矢量质量。

    Args:
        input_path: 输入PDF路径
        output_path: 输出PDF路径
        target_size: 目标纸张尺寸名称（如"A4", "Letter"）或"widthxheight"格式（点数）
        content_scale: 内容额外缩放系数（1.0=按原比例适配，0.9=缩小到90%）
        page_range: 页面范围字符串（如"3,5-7"），空字符串表示所有页面
        progress_callback: 进度回调 callback(page_index, total_pages, message)

    Returns:
        处理结果统计
    """
    from pdf_page_remover.rotator import parse_page_range

    # 解析目标尺寸
    if target_size in PAGE_SIZES:
        target_w, target_h = PAGE_SIZES[target_size]
    else:
        # 尝试解析 "widthxheight" 格式（点数）
        try:
            parts = target_size.lower().split("x")
            target_w = float(parts[0])
            target_h = float(parts[1])
        except (ValueError, IndexError):
            raise ValueError(f"无法解析纸张尺寸: {target_size}。请使用预设名称(如A4)或widthxheight格式(如595x842)")

    src_doc = pymupdf.open(input_path)
    total_pages = len(src_doc)
    stats = {
        "total_pages": total_pages,
        "resized_count": 0,
        "target_size": target_size,
        "status": "success",
    }

    # 解析页面范围
    page_indices = parse_page_range(page_range, total_pages) if page_range else set(range(total_pages))

    # 判断目标方向
    target_is_portrait = target_h >= target_w

    # 创建新文档，使用矢量方式保留原始质量
    new_doc = pymupdf.open()

    for page_index in range(total_pages):
        if progress_callback:
            progress_callback(page_index, total_pages, f"正在调整第 {page_index + 1} 页尺寸...")

        src_page = src_doc[page_index]

        if page_index not in page_indices:
            # 不在范围内，原样复制
            new_doc.insert_pdf(src_doc, from_page=page_index, to_page=page_index)
            continue

        # show_pdf_page 不读取源页的 /Rotate，先把源页展平成 rotation=0 且视觉一致的页面。
        # 之后 old_w/old_h 是显示尺寸，方向判断与摆放都在显示坐标下进行，
        # 避免出现 /Rotate 90/270 的页面处理后方向颠倒。
        flat_doc, src_page_index, is_temp_flat = flatten_page_for_placement(src_doc, page_index)
        src_doc_for_insert = flat_doc
        frect = flat_doc[src_page_index].rect
        old_w, old_h = frect.width, frect.height
        insert_rotate = 0

        # 检测页面方向，决定是否需要旋转内容
        # 基于MediaBox（实际内容尺寸）判断
        src_is_portrait = old_h >= old_w
        need_rotate = (src_is_portrait != target_is_portrait)

        if need_rotate:
            # 方向不匹配：使用show_pdf_page的rotate参数直接旋转
            # rotate=90 表示逆时针旋转90°，横向变纵向
            # 旋转后有效尺寸交换：宽=原高，高=原宽
            eff_w, eff_h = old_h, old_w
            rotate_angle = 90
        else:
            # 方向匹配：无需旋转
            eff_w, eff_h = old_w, old_h
            rotate_angle = 0

        # 计算内容适配比例
        scale_x = target_w / eff_w if eff_w > 0 else 1.0
        scale_y = target_h / eff_h if eff_h > 0 else 1.0
        fit_scale = min(scale_x, scale_y) * content_scale

        new_content_w = eff_w * fit_scale
        new_content_h = eff_h * fit_scale

        offset_x = (target_w - new_content_w) / 2
        offset_y = (target_h - new_content_h) / 2

        # 创建目标尺寸的新页面
        new_page = new_doc.new_page(width=target_w, height=target_h)
        target_rect = pymupdf.Rect(
            offset_x, offset_y,
            offset_x + new_content_w, offset_y + new_content_h
        )
        # 计算总旋转角度：源页面旋转 + 方向匹配旋转
        total_rotate = (insert_rotate + rotate_angle) % 360
        if total_rotate != 0:
            new_page.show_pdf_page(target_rect, src_doc_for_insert, src_page_index, rotate=total_rotate)
        else:
            new_page.show_pdf_page(target_rect, src_doc_for_insert, src_page_index)

        # show_pdf_page 不带注释，电子印章/签章需按同一变换补回
        copied = copy_page_annotations(
            src_doc_for_insert, src_doc_for_insert[src_page_index],
            new_doc, new_page, target_rect, total_rotate
        )
        if copied:
            stats["copied_annot_count"] = stats.get("copied_annot_count", 0) + copied

        # 关闭展平文档（如果有）
        if is_temp_flat:
            flat_doc.close()

        stats["resized_count"] += 1

    # 保存
    carry_doc_extras(src_doc, new_doc)
    new_doc.save(output_path, garbage=4, deflate=True)
    new_doc.close()
    src_doc.close()

    if progress_callback:
        progress_callback(total_pages, total_pages, "尺寸调整完成！")

    return stats


def batch_resize(
    input_files: List[str],
    output_dir: str,
    target_size: str = "A4",
    content_scale: float = 1.0,
    page_range: str = "",
    progress_callback: Optional[Callable] = None,
) -> List[Dict]:
    """
    批量修改PDF页面尺寸

    Args:
        input_files: 输入PDF文件路径列表
        output_dir: 输出目录
        target_size: 目标纸张尺寸名称
        content_scale: 内容额外缩放系数
        page_range: 页面范围字符串
        progress_callback: 进度回调 callback(file_index, total_files, page_index, total_pages, message)

    Returns:
        所有文件的处理结果统计列表
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    all_stats = []
    total_files = len(input_files)

    for file_index, input_path in enumerate(input_files):
        if not os.path.exists(input_path):
            logger.warning(f"文件不存在: {input_path}")
            continue

        filename = os.path.basename(input_path)
        output_path = os.path.join(output_dir, filename)

        def page_progress(page_index, total_pages, message):
            if progress_callback:
                progress_callback(file_index, total_files, page_index, total_pages, message)

        try:
            stats = resize_pdf(
                input_path, output_path,
                target_size=target_size,
                content_scale=content_scale,
                page_range=page_range,
                progress_callback=page_progress,
            )
            stats["input_file"] = input_path
            stats["output_file"] = output_path
            all_stats.append(stats)
            logger.info(f"尺寸调整完成: {filename} - 目标尺寸 {target_size}")

        except Exception as e:
            logger.error(f"尺寸调整失败: {filename} - {e}")
            all_stats.append({
                "input_file": input_path,
                "output_file": output_path,
                "status": "error",
                "error": str(e),
            })

    return all_stats