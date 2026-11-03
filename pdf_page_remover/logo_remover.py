"""
页眉标识（Logo）批量去除模块

公文扫描件每一页的角上都挂着同一个单位标识（菱形图形 + LGCI 一类的小字）。它不是页码，
不能靠"页边空白里孤立的一小条墨迹"来认——那样会把正文第一行、页眉文字、表格编号都当成
目标，而这些是不能啃的正文。

可靠的判据是**跨页像素一致**：正文每页都不同，标识却是同一块印版，落在角区里的位置和
墨迹形状页页重合。所以先把采样页的角区二值化叠成一张"出现次数图"，只把至少半数页面都
有的墨迹当模板，再逐页拿模板去对。即便角区里正好有正文，只要那些字各页不同，就进不了
模板，也不会被动过。

模板确定后还压了三道"不许吃正文"的闸：
- 标识必须小（默认 6×3cm 以内），整块正文或表格再怎么一致也不会只有这么点大；
- 标识朝正文那一侧要和下一片墨迹隔开，紧贴着的说明它是正文的头一行（横贯页面的页眉
  横线不算，那是版面线条，标识常常就压在横线上方）；
- 逐页比对要求墨迹量相当、形状重合，对不上的页整页跳过不动。

同一份文件里标识常常不止一种印法（正文页眉大、附件表格页眉小），所以逐页对位走三条路，
一条比一条更靠"这枚标识自己的样子"而不是"页页都有的墨"：先按模板原位对，再按倍率缩放
用模板匹配找位置，最后按彩色团块（标识是彩印的、正文是黑字）定外框、把模板长宽拉伸对
形状。三条路都要再过尺寸、离角距离、与正文的隔离和重合度这四道验收，任何一条不过就
整页不动。

抹除复用去页码那条已经验证过的流水线（core.PageNumberRemover.erase_rect_inplace）：
局部 300dpi 渲染 → 掩码只留标识自己的墨迹（一步都不出标识框，免得把紧贴的页眉横线重绘
出毛边）→ cv2.inpaint → 涂黑删掉底层真墨迹 → 贴回补丁。
电子签章多以注释存在，注释避让和旋转页坐标换算都在那一条里。

整行删除（whole_row）是给"抬头整行都不要"的件准备的：认到角上的标识只是入口，认到之后把
这一页的角区换成铺满整页宽度的横带重新分析，将同在一横带上、间隙够近的字（公司全称）并进来，
再把这一行自己的上下划线并进来，整块一起抹——只去 logo 会剩下一行公司名和一条光秃秃的线。
这时横线不再是受保护对象（它就是要去掉的东西），但护栏照旧：并完的高度仍不得超过标识上限，
整行朝正文那侧仍要隔得到 min_gap，否则这一页保留不动。
"""

import copy
import logging
import math
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np
import pymupdf

from pdf_page_remover.core import (PageNumberConfig, PageNumberRemover, SCAN_ANALYZE_DPI,
                                   detect_seal_regions)

logger = logging.getLogger(__name__)

CM_TO_PT = 28.3465

# 又扁又长的墨迹是版面线条（页眉横线、表格框、下划线），不是标识本身
RULE_THICK_CM = 0.09
RULE_ASPECT = 6.0
RULE_MIN_LEN_CM = 3.0
# 比这还小的连通块是纸屑、标点、压缩噪点
MIN_COMP_PX = 25
# 掩码比标识框四周多让出这一点：框是按 150dpi 的墨迹圈的，量化到格点上就有一行
# 半个点（0.5pt）的笔画头落在框外，没被掩码盖住的笔画底会留在成品上，
# inpaint 还把它往上一行行晕开，看着就是标识底下一圈没擦干净的淡影。
# 上限 1pt 也保证掩码始终不出擦除矩形（外扩 1.42pt）
MASK_SLACK_PT = 1.0
# 整行删除时，横线离这一行文字下沿最远可以有多远还算这一行的下划线
ROW_RULE_GAP_CM = 0.60


class LogoConfig:
    """页眉标识的检测与抹除参数"""

    ANCHORS = ("top_left", "top_right", "bottom_left", "bottom_right", "top_center")
    ANCHOR_LABELS = {
        "top_left": "左上角",
        "top_right": "右上角",
        "bottom_left": "左下角",
        "bottom_right": "右下角",
        "top_center": "顶部居中",
    }

    def __init__(
        self,
        anchor: str = "top_left",
        zone_w_cm: float = 9.0,
        zone_h_cm: float = 4.0,
        presence_ratio: float = 0.5,
        min_sample_pages: int = 3,
        max_sample_pages: int = 60,
        cluster_gap_cm: float = 0.55,
        min_iou: float = 0.45,
        min_iou_scaled: float = 0.50,
        mark_scales: Tuple[float, ...] = (0.90, 0.80, 0.70, 0.62, 1.10, 1.22),
        locate_min_corr: float = 0.30,
        max_move_cm: float = 2.0,
        min_iou_color: float = 0.55,
        color_anchor_px: int = 150,
        ink_min_ratio: float = 0.45,
        ink_max_ratio: float = 2.2,
        max_shift_cm: float = 0.30,
        min_gap_cm: float = 0.20,
        max_mark_w_cm: float = 6.0,
        max_mark_h_cm: float = 3.0,
        pad_cm: float = 0.05,
        mark_ink_delta: int = 45,
        mark_color_delta: int = 40,
        red_dom_delta: int = 30,
        color_pref_px: int = 250,
        color_pref_ratio: float = 0.12,
        mask_sat_delta: int = 22,
        mark_dilate_cm: float = 0.03,
        protect_seals: bool = True,
        whole_row: bool = False,
    ):
        """
        Args:
            anchor: 标识挂在页面哪一角（见 ANCHORS）
            zone_w_cm / zone_h_cm: 从那一角往里最多圈多大一块来找标识
            presence_ratio: 角区里的墨迹至少要这么大比例的采样页同时出现才算固定标识
            min_sample_pages: 可用采样页少于此数就不动手（只有两三页的文件里，正文标题
                也可能页页相同，一致性判据在这里失效）
            max_sample_pages: 最多采样几页来定模板，长文件不必每页都读
            cluster_gap_cm: 隔这么近的墨迹算同一个标识（图形和它下面的"LGCI"是一枚）
            min_iou: 逐页比对时，模板墨迹与该页实际墨迹的重合度下限
            min_iou_scaled: 按尺寸缩放后再比对的重合度下限（比原位比对更严，位置是猜的）
            mark_scales: 原位对不上时，把标识按这些倍率再找一遍——同一份文件的正文页眉
                和附件页眉常有两种大小的同一枚标识
            locate_min_corr: 模板匹配的最低相关度，低于它连验都省了
            max_move_cm: 缩放找到的标识离那一角的距离，比模板远过这些就不认
            min_iou_color: 按颜色团块定位后，标识形状拉伸到该外框上的重合度下限
            color_anchor_px: 角区里最大的彩色团块至少要有这么多像素，才够格当标识的图形
            ink_min_ratio / ink_max_ratio: 该页这块墨的像素数相对模板的允许倍数，
                超出说明混进来的不是标识（或标识本身缺了一半）
            max_shift_cm: 同一份文件里标识逐页允许偏移多少（扫描进纸抖动）
            min_gap_cm: 标识朝正文那侧至少要隔开这么远，否则视为正文的头一行，不动
            max_mark_w_cm / max_mark_h_cm: 标识尺寸上限，超过就不是角标
            pad_cm: 抹除框比检测到的标识框每边再放大这么多，把底层真墨迹和文字层一起删掉
            mark_ink_delta: 标识框内比本页纸面亮度暗过这个值就重画。框是逐页验收过的
                标识范围，里面除标识就是纸面，所以要判得宽——字脚下那圈只剩二三十级，
                判严了就留下一排没擦干净的笔画头
            mark_color_delta: 通道最大-最小差超过这个值算彩色，用来判断"这一角为什么
                没有标识"（是空白的还是被印章盖住了）
            red_dom_delta: 红通道比另外两个通道高出这个值算"偏红"。红头文件的红线红
                标题、盖上去的电子印章都是偏红的，标识的图形是蓝绿的——检测排序时
                "彩色且不偏红"的那一块才敢当标识
            color_pref_px / color_pref_ratio: 跨页一致的墨迹里，彩色像素至少这么多、
                且至少占这块墨这么大比例，才算"这是一枚彩色印刷的标识"优先选中
            mask_sat_delta: 标识框内彩色超过这个值就重画，比上面那个判据宽，
                专门收掉淡绿淡蓝的图形边缘
            mark_dilate_cm: 掩码从认出的墨迹向外的加宽量，盖住抗锯齿的毛边；
                再宽也出不了标识框，框外一律不碰
                （标识常常正压在页眉横线上方，横线离标识往往只有一两毫米）
            protect_seals: 红印章等彩色图案一律保留
            whole_row: 整行删除。认到角上的标识后，把框横向并到页眉那一整行（公司全称），
                纵向把行下面那条横线一起圈进来抹掉——客户要的是"抬头整行不要"，
                只去 logo 会留下一行公司名和一条光秃秃的横线
        """
        self.anchor = anchor if anchor in self.ANCHORS else "top_left"
        self.zone_w_cm = zone_w_cm
        self.zone_h_cm = zone_h_cm
        self.presence_ratio = presence_ratio
        self.min_sample_pages = min_sample_pages
        self.max_sample_pages = max_sample_pages
        self.cluster_gap_cm = cluster_gap_cm
        self.min_iou = min_iou
        self.min_iou_scaled = min_iou_scaled
        self.mark_scales = tuple(mark_scales)
        self.locate_min_corr = locate_min_corr
        self.max_move_cm = max_move_cm
        self.min_iou_color = min_iou_color
        self.color_anchor_px = color_anchor_px
        self.ink_min_ratio = ink_min_ratio
        self.ink_max_ratio = ink_max_ratio
        self.max_shift_cm = max_shift_cm
        self.min_gap_cm = min_gap_cm
        self.max_mark_w_cm = max_mark_w_cm
        self.max_mark_h_cm = max_mark_h_cm
        self.pad_cm = pad_cm
        self.mark_ink_delta = mark_ink_delta
        self.mark_color_delta = mark_color_delta
        self.red_dom_delta = red_dom_delta
        self.color_pref_px = color_pref_px
        self.color_pref_ratio = color_pref_ratio
        self.mask_sat_delta = mask_sat_delta
        self.mark_dilate_cm = mark_dilate_cm
        self.protect_seals = protect_seals
        self.whole_row = bool(whole_row)

    @property
    def is_top(self) -> bool:
        return self.anchor.startswith("top")

    @property
    def x_align(self) -> str:
        if self.anchor in ("top_left", "bottom_left"):
            return "left"
        if self.anchor in ("top_right", "bottom_right"):
            return "right"
        return "center"

    def canvas_shape(self, zoom: float) -> Tuple[int, int]:
        """角区画布的 (高, 宽) 像素数：各页的角区都按这一角对齐叠到这块画布上"""
        return (max(1, int(round(self.zone_h_cm * CM_TO_PT * zoom))),
                max(1, int(round(self.zone_w_cm * CM_TO_PT * zoom))))

    def describe(self) -> str:
        label = self.ANCHOR_LABELS.get(self.anchor, self.anchor)
        return (f"{label}标识 搜索区{self.zone_w_cm:g}×{self.zone_h_cm:g}cm "
                f"跨页一致≥{self.presence_ratio:.0%} "
                f"尺寸≤{self.max_mark_w_cm:g}×{self.max_mark_h_cm:g}cm"
                + (" 整行删除（含页眉文字与横线）" if self.whole_row else ""))

    def seal_config(self) -> PageNumberConfig:
        """印章保护沿用去页码那套阈值，两条流水线的保护判据不会走偏"""
        cfg = PageNumberConfig()
        cfg.protect_seals = self.protect_seals
        return cfg


def _zone_clip(view: pymupdf.Rect, cfg: LogoConfig) -> pymupdf.Rect:
    """按视图（显示）方向圈出那一角的搜索区"""
    w_pt = cfg.zone_w_cm * CM_TO_PT
    h_pt = cfg.zone_h_cm * CM_TO_PT
    if cfg.x_align == "left":
        x0 = view.x0
    elif cfg.x_align == "right":
        x0 = max(view.x0, view.x1 - w_pt)
    else:
        x0 = max(view.x0, (view.x0 + view.x1) / 2.0 - w_pt / 2.0)
    y0 = view.y0 if cfg.is_top else max(view.y0, view.y1 - h_pt)
    return pymupdf.Rect(x0, y0, min(view.x1, x0 + w_pt), min(view.y1, y0 + h_pt))


def _binarize(gray: np.ndarray, zoom: float) -> np.ndarray:
    """局部自适应二值化，和去页码同一套：全局 Otsu 会丢掉很淡的标识"""
    block = max(11, int(24 * zoom)) | 1
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY_INV, block, 4)


def _boxes(mask: np.ndarray, zoom: float) -> List[List[float]]:
    """墨迹连通块 [[x0, y0, x1, y1, 像素数, 是否版面线条], ...]"""
    count, _, stats, _ = cv2.connectedComponentsWithStats(
        (mask > 0).astype(np.uint8), connectivity=8)
    out = []
    thick_px = RULE_THICK_CM * CM_TO_PT * zoom
    min_len_px = RULE_MIN_LEN_CM * CM_TO_PT * zoom
    for i in range(1, count):
        x, y, w, h, px = (stats[i, 0], stats[i, 1], stats[i, 2], stats[i, 3], stats[i, 4])
        if px < MIN_COMP_PX:
            continue
        short, long_ = (w, h) if w <= h else (h, w)
        rule = short <= thick_px and long_ >= RULE_ASPECT * thick_px and long_ >= min_len_px
        out.append([float(x), float(y), float(x + w), float(y + h), float(px), 1.0 if rule else 0.0])
    return out


def _union(boxes: List[List[float]]) -> List[float]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _hits(a: List[float], b: List[float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _grow(box: List[float], gap_px: float) -> List[float]:
    return [box[0] - gap_px, box[1] - gap_px, box[2] + gap_px, box[3] + gap_px]


def _cluster(boxes: List[List[float]], gap_px: float) -> List[List[List[float]]]:
    """把挨得近的墨迹并成一枚标识：图形和它下面那行小字是一块，不是一个标识加一个字"""
    groups: List[List[List[float]]] = [[b] for b in boxes]
    merged = True
    while merged:
        merged = False
        out: List[List[List[float]]] = []
        for g in groups:
            gb = _union(g)
            hit = None
            for i, other in enumerate(out):
                if _hits(_grow(gb, gap_px), _union(other)):
                    hit = i
                    break
            if hit is None:
                out.append(g)
            else:
                out[hit] = out[hit] + g
                merged = True
        groups = out
    return groups


def _split_chained(marks: List[List[float]], cfg: LogoConfig,
                   ppc: float) -> List[List[List[float]]]:
    """跨页一致的墨块分组；一组宽过标识上限的，按更紧的间隙再切一刀。

    页眉是"角标 + 公司全称"排成一横行的，两者只差半厘米时会被并成一块 7cm 宽的东西，
    按尺寸上限直接拒就等于把整本有页眉的件都判成"没有标识"（应急预案那份 60 页里
    54 页带标识，就是这么认不出来的）。切开以后角上那一块才是标识，页眉文字交给
    整行删除去并。本来就连成一整块（切不动）的，仍按原样交给尺寸护栏如实拒绝。
    """
    groups = _cluster(marks, cfg.cluster_gap_cm * ppc)
    out: List[List[List[float]]] = []
    for g in groups:
        u = _union(g)
        if ((u[2] - u[0]) <= cfg.max_mark_w_cm * ppc
                and (u[3] - u[1]) <= cfg.max_mark_h_cm * ppc):
            out.append(g)
            continue
        tight = _cluster(g, max(0.12 * ppc, cfg.cluster_gap_cm * ppc * 0.5))
        out.extend(tight if len(tight) > 1 else [g])
    return out


class _Zone:
    """一页角区的分析结果；墨迹坐标都在"角区画布"上，不同页面尺寸也能对齐"""

    __slots__ = ("clip", "zoom", "off_x", "off_y", "ink", "boxes", "color", "red")

    def __init__(self, clip: pymupdf.Rect, zoom: float, off_x: int, off_y: int,
                 ink: np.ndarray, boxes: List[List[float]],
                 color: Optional[np.ndarray] = None,
                 red: Optional[np.ndarray] = None):
        self.clip = clip
        self.zoom = zoom
        self.off_x = off_x
        self.off_y = off_y
        self.ink = ink
        self.boxes = boxes
        # 彩色墨迹（印章、彩色印刷），只在解释"这一页为什么没动"时用来分辨是印章还是没标识
        self.color = color
        # 其中偏红的那部分：红头文件的红线红标题、电子印章都落在这里，标识的图形不落在里
        self.red = red

    def to_view(self, box: List[float], pad_pt: float, page_rect: pymupdf.Rect) -> pymupdf.Rect:
        """画布坐标框 → 该页视图坐标矩形（去掉画布对齐偏移，再换算回点）"""
        rect = pymupdf.Rect(
            self.clip.x0 + (box[0] - self.off_x) / self.zoom - pad_pt,
            self.clip.y0 + (box[1] - self.off_y) / self.zoom - pad_pt,
            self.clip.x0 + (box[2] - self.off_x) / self.zoom + pad_pt,
            self.clip.y0 + (box[3] - self.off_y) / self.zoom + pad_pt)
        rect.intersect(page_rect)
        return rect

    def to_canvas(self, view: pymupdf.Rect) -> List[float]:
        """视图坐标矩形 → 画布坐标框（to_view 的反向，换角区时要用）"""
        return [(view.x0 - self.clip.x0) * self.zoom + self.off_x,
                (view.y0 - self.clip.y0) * self.zoom + self.off_y,
                (view.x1 - self.clip.x0) * self.zoom + self.off_x,
                (view.y1 - self.clip.y0) * self.zoom + self.off_y]


def _analyze_page(page: pymupdf.Page, cfg: LogoConfig, zoom: float,
                  canvas: Tuple[int, int]) -> Optional[_Zone]:
    """渲染那一角并二值化，把结果摆到角区画布上；页面太小或渲染失败返回 None"""
    clip = _zone_clip(page.rect, cfg)
    if clip.width < 6 or clip.height < 6:
        return None
    try:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
    except Exception as e:
        logger.debug(f"第{page.number + 1}页角区渲染失败: {e}")
        return None
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)[:, :, :3]
    local = _binarize(arr.mean(axis=2).astype(np.uint8), zoom)
    rgb = arr.astype(np.int16)
    local_color = (rgb.max(axis=2) - rgb.min(axis=2)) > cfg.mark_color_delta
    # 偏红的单独记一份：红头那行红字、盖上来的电子印章都是红的，标识的图形是蓝绿的
    red_ch, grn_ch, blu_ch = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    local_red = ((red_ch - grn_ch > cfg.red_dom_delta)
                 & (red_ch - blu_ch > cfg.red_dom_delta))

    ch, cw = canvas
    pw, ph = min(local.shape[1], cw), min(local.shape[0], ch)
    if cfg.x_align == "right":
        off_x = cw - pw
    elif cfg.x_align == "center":
        off_x = (cw - pw) // 2
    else:
        off_x = 0
    off_y = ch - ph if not cfg.is_top else 0
    ink = np.zeros((ch, cw), np.uint8)
    ink[off_y:off_y + ph, off_x:off_x + pw] = local[:ph, :pw]
    color = np.zeros((ch, cw), bool)
    color[off_y:off_y + ph, off_x:off_x + pw] = local_color[:ph, :pw]
    red = np.zeros((ch, cw), bool)
    red[off_y:off_y + ph, off_x:off_x + pw] = local_red[:ph, :pw]
    return _Zone(clip, zoom, off_x, off_y, ink, _boxes(ink, zoom), color, red)


def _row_zone(page: pymupdf.Page, cfg: LogoConfig, zoom: float) -> Optional[_Zone]:
    """整行删除用的宽角区：横向铺满整页，才看得见页眉那行的公司全称和整条横线"""
    wide = copy.copy(cfg)
    wide.zone_w_cm = (page.rect.width + 4 * CM_TO_PT) / CM_TO_PT
    return _analyze_page(page, wide, zoom, wide.canvas_shape(zoom))


def _row_box(zone: _Zone, box: List[float], cfg: LogoConfig,
             zoom: float) -> Tuple[Optional[List[float]], float, bool]:
    """把标识框并成整行框：横向并同一横带的字，纵向并这一行自己的上下划线。

    返回 (整行框, 整行框朝正文那侧到下一片墨的距离 pt, 下边那条线是不是被吸收的版面线)；
    并完高出标识上限说明越过了一条行距，宁可整页不动。
    """
    gap_px = cfg.cluster_gap_cm * CM_TO_PT * zoom
    rule_px = ROW_RULE_GAP_CM * CM_TO_PT * zoom
    got, sel, added = list(box), set(), True
    lined = [False, False]        # 这一行的上、下划线各只认一条：附件表格的框线常常就压
    # 在页眉横线下面一两毫米，再往上并就把表格并进了页眉
    while added:
        added = False
        for i, b in enumerate(zone.boxes):
            if i in sel:
                continue
            band = (min(b[3], got[3]) - max(b[1], got[1])
                    > 0.35 * max(1e-6, min(b[3] - b[1], got[3] - got[1])))
            h_gap = ((b[0] - got[2]) if b[0] >= got[2]
                     else (got[0] - b[2] if b[2] <= got[0] else -1.0))
            if band and h_gap <= gap_px:
                pass                                    # 同一横带上挨得近的字或图形
            elif (b[5] == 1.0 and b[2] > got[0] and got[2] > b[0]):
                if not lined[0] and -1.0 <= got[1] - b[3] <= rule_px:
                    lined[0] = True
                elif not lined[1] and -1.0 <= b[1] - got[3] <= rule_px:
                    lined[1] = True
                else:
                    continue
            else:
                continue
            got = [min(got[0], b[0]), min(got[1], b[1]),
                   max(got[2], b[2]), max(got[3], b[3])]
            sel.add(i)
            added = True
    if lined[1]:
        # 整行删除的"这一行"是页顶到那条横线为止。模板哪怕只锁到页眉的半截（比如搜索区
        # 设成了顶部居中，认到的是公司全称那一横带），挂在横线上面的标识本体也必须跟着走，
        # 不然成品就是"字去了、章还在"的半行。横线以下的一律不碰。
        for i, b in enumerate(zone.boxes):
            if i in sel or b[3] > got[3] + 1.0 or b[2] <= got[0] or got[2] <= b[0]:
                continue
            got = [got[0], min(got[1], b[1]), got[2], got[3]]
            sel.add(i)
    if (got[3] - got[1]) > cfg.max_mark_h_cm * CM_TO_PT * zoom:
        return None, 0.0, False
    below = float("inf")
    for i, b in enumerate(zone.boxes):
        if i in sel or b[2] <= got[0] or got[2] <= b[0] or b[1] < got[3] - 1.0:
            continue
        below = min(below, (b[1] - got[3]) / zoom)
    return got, below, lined[1]


def _sample_pages(pages: List[int], max_pages: int) -> List[int]:
    """均匀取若干页：只取开头几页会全落在同一份材料上，判不出"整本都有" """
    n = len(pages)
    if n <= max_pages or max_pages < 2:
        return pages[:]
    step = (n - 1) / (max_pages - 1)
    return [pages[i] for i in sorted({int(round(k * step)) for k in range(max_pages)})]


def _gap_cm(boxes: List[List[float]], mark_box: List[float], cfg: LogoConfig,
            zoom: float) -> float:
    """
    标识朝正文那侧到下一片墨迹的距离（厘米），中间什么都没有就是无穷远。

    这是最后一道"别把正文头一行当标识"的闸：正文行下面永远接着下一行，而标识是
    页边空白里独立的一块。横贯页面的版面线条（页眉横线）不算正文——标识常压在它上面。
    """
    tol_px = max(2.0, 0.05 * CM_TO_PT * zoom)
    ppc = CM_TO_PT * zoom
    nearest = float("inf")
    for b in boxes:
        if b[5] == 1.0:
            continue
        if not (b[0] < mark_box[2] and mark_box[0] < b[2]):
            continue
        d = (b[1] - mark_box[3]) if cfg.is_top else (mark_box[1] - b[3])
        if d < -tol_px:
            continue  # 在标识的另一侧（更靠页边）
        nearest = min(nearest, d / ppc)
    return nearest


def _corner_cm(box: List[float], cfg: LogoConfig, canvas: Tuple[int, int],
               zoom: float) -> float:
    """标识框离那一角多远，越近越像角标"""
    ppc = CM_TO_PT * zoom
    ch, cw = canvas
    if cfg.x_align == "left":
        side = box[0]
    elif cfg.x_align == "right":
        side = cw - box[2]
    else:
        # "居中"的角区是按页面对齐的，量到画布中心的距离才和页面宽度无关
        side = abs((box[0] + box[2]) / 2.0 - cw / 2.0)
    cross = box[1] if cfg.is_top else (ch - box[3])
    return math.hypot(side, cross) / ppc


def _cm_of(box: List[float], cfg: LogoConfig, canvas: Tuple[int, int],
           zoom: float) -> Dict[str, float]:
    """画布坐标框 → 相对那一角的距离与尺寸（厘米），用来在结果里说清楚动了什么"""
    ppc = CM_TO_PT * zoom
    ch, cw = canvas
    if cfg.x_align == "left":
        side0 = box[0]
    elif cfg.x_align == "right":
        side0 = cw - box[2]
    else:
        side0 = abs((box[0] + box[2]) / 2.0 - cw / 2.0)
    cross0 = box[1] if cfg.is_top else ch - box[3]
    names = {"left": "距左", "right": "距右", "center": "水平偏"}[cfg.x_align]
    return {names: round(side0 / ppc, 2),
            ("距上" if cfg.is_top else "距下"): round(cross0 / ppc, 2),
            "宽": round((box[2] - box[0]) / ppc, 2),
            "高": round((box[3] - box[1]) / ppc, 2)}


def detect_logo(doc: pymupdf.Document, cfg: LogoConfig = None,
                page_range: str = "") -> Dict[str, Any]:
    """
    找出这份文件固定挂在某一角的标识。

    Returns:
        found=True 时带 template（画布坐标下的标识墨迹）、box、template_px 与尺寸说明；
        found=False 时 reason 说清为什么不动手（宁可漏掉，不能吃正文）。
    """
    cfg = cfg or LogoConfig()
    from pdf_page_remover.rotator import parse_page_range

    total = len(doc)
    pages = sorted(parse_page_range(page_range, total)) if page_range else list(range(total))
    zoom = SCAN_ANALYZE_DPI / 72.0
    canvas = cfg.canvas_shape(zoom)
    ppc = CM_TO_PT * zoom
    info: Dict[str, Any] = {
        "anchor": cfg.anchor,
        "anchor_label": cfg.ANCHOR_LABELS.get(cfg.anchor, cfg.anchor),
        "pages_total": len(pages),
        "zone_cm": [cfg.zone_w_cm, cfg.zone_h_cm],
        "found": False,
    }
    if not pages:
        info["reason"] = "页面范围为空"
        return info

    freq = np.zeros(canvas, np.int32)
    cfreq = np.zeros(canvas, np.int32)
    rfreq = np.zeros(canvas, np.int32)
    usable = 0
    for pi in _sample_pages(pages, cfg.max_sample_pages):
        zone = _analyze_page(doc[pi], cfg, zoom, canvas)
        if zone is None:
            continue
        freq += zone.ink > 0
        cfreq += zone.color
        rfreq += zone.red
        usable += 1
    info["sampled_pages"] = usable
    if usable < cfg.min_sample_pages:
        info["reason"] = (f"能用的采样页只有 {usable} 页（要到 {cfg.min_sample_pages} 页以上），"
                          f"这么几页里正文也可能页页相同，确认不了是固定标识")
        return info

    thr = max(cfg.min_sample_pages, int(math.ceil(cfg.presence_ratio * usable)))
    hot = ((freq >= thr).astype(np.uint8)) * 255
    # 同一门槛下的彩色／偏红墨迹：标识的图形是彩印的，正文是黑白字，红头那行红字和
    # 电子印章是偏红的。跨页一致只说明"固定"，加上颜色这一票才说明"它是标识而不是正文"
    hot_c = cfreq >= thr
    hot_r = rfreq >= thr
    hot_boxes = _boxes(hot, zoom)
    info["threshold_pages"] = thr
    marks = [b for b in hot_boxes if b[5] == 0.0]  # 版面横线不算标识
    if not marks:
        info["reason"] = (f"{cfg.zone_w_cm:g}×{cfg.zone_h_cm:g}cm 的角区里，"
                          f"没有 {thr}/{usable} 页都重合的固定墨迹")
        return info

    groups = _split_chained(marks, cfg, ppc)
    rejected: List[str] = []
    cands = []
    for g in groups:
        box = _union(g)
        w_cm, h_cm = (box[2] - box[0]) / ppc, (box[3] - box[1]) / ppc
        if w_cm > cfg.max_mark_w_cm or h_cm > cfg.max_mark_h_cm:
            rejected.append(f"{w_cm:.1f}×{h_cm:.1f}cm 超过标识尺寸上限"
                            f"（{cfg.max_mark_w_cm:g}×{cfg.max_mark_h_cm:g}cm），更像正文")
            continue
        gap = _gap_cm(hot_boxes, box, cfg, zoom)
        if gap < cfg.min_gap_cm:
            rejected.append(f"这块墨迹朝正文那侧只隔 {gap:.2f}cm 就接着另一片墨迹，"
                            f"更像正文的第一行")
            continue
        x0, y0, x1, y1 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
        ink_n = sum(b[4] for b in g)
        col_n = float(np.count_nonzero(hot_c[y0:y1, x0:x1]))
        red_n = float(np.count_nonzero(hot_r[y0:y1, x0:x1]))
        # 彩色、又不偏红的那一块才是印上去的标识；黑字带（公司全称、文件编号）再怎么
        # 页页相同也是字。偏红的先排掉——红头那行红字和压在页眉上的电子印章都不许当模板。
        colored = col_n >= cfg.color_pref_px and col_n >= cfg.color_pref_ratio * ink_n
        tier = 0 if (colored and red_n < 0.5 * col_n) else 1
        cands.append((tier, _corner_cm(box, cfg, canvas, zoom), -ink_n, box,
                      [b for b in g], gap, col_n, red_n, colored))
    if not cands:
        info["reason"] = ("角区里跨页一致的墨迹里挑不出标识：" +
                          "；".join(list(dict.fromkeys(rejected))[:3]))
        return info

    # 先按"是不是彩印的标识"分层，同层里再挑离那一角最近的：角标就钉在角上，
    # 正文块再怎么重复也在更靠里的位置
    cands.sort(key=lambda c: (c[0], c[1], c[2]))
    _, _, _, box, group, gap, col_n, red_n, is_colored = cands[0]
    x0, y0, x1, y1 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
    template = hot[y0:y1, x0:x1] > 0
    info.update({
        "found": True,
        "box": [float(x0), float(y0), float(x1), float(y1)],
        "template": template,
        # 同一块外框里的彩色那半截：标识的图形是彩印的，横向附件页把它印小一圈，
        # 整块墨迹对不上形状时（字被页眉横线压着并不进框）就拿这一截对颜色
        "color_template": np.ascontiguousarray(hot_c[y0:y1, x0:x1]),
        "template_px": int(template.sum()),
        "mark_parts": len(group),
        "gap_cm": None if gap == float("inf") else round(gap, 2),
        "cm": _cm_of(box, cfg, canvas, zoom),
        "other_candidates": len(cands) - 1,
        "colored_px": int(col_n),
        "red_px": int(red_n),
        # 模板本身是彩印的，逐页兜底匹配才允许"不看位置、只看这枚彩色印痕"来认
        "colored": bool(is_colored),
    })
    return info


def _empty_reason(zone: "_Zone", dilated: List[float], cfg: LogoConfig,
                  zoom: float) -> str:
    """这一页那块墨都没找到，说清是本来就没有标识，还是标识被印章/彩色图案盖住了"""
    color = zone.color
    if color is None:
        return "这一角没有这块标识"
    y0, y1 = int(dilated[1]), int(dilated[3])
    x0, x1 = int(dilated[0]), int(dilated[2])
    area = max(1, (y1 - y0) * (x1 - x0))
    hit = int(np.count_nonzero(color[y0:y1, x0:x1]))
    if hit >= 0.25 * area:
        return "标识的位置上有印章或别的彩色图案，整块都对不上，这一页保留不动"
    ppc = CM_TO_PT * zoom
    for b in _boxes(color.astype(np.uint8) * 255, zoom):
        bw, bh = (b[2] - b[0]) / ppc, (b[3] - b[1]) / ppc
        if (b[4] >= cfg.color_anchor_px and b[5] != 1.0 and min(bw, bh) >= 0.25
                and max(bw, bh) / max(0.01, min(bw, bh)) <= 4.0):
            return ("这一角有一枚彩色标识，可位置和形状都跟别页那枚对不上"
                    "（常被页眉横线压着），这一页保留不动")
    return "这一角没有这块标识"


def _fit_mark(zone: _Zone, box: List[float], tpl: np.ndarray, cfg: LogoConfig,
              zoom: float, min_iou: float) -> Tuple[Optional[List[float]], str]:
    """把一对(标识框, 标识形状)套在这一页上，通过验收就返回该页这块墨的实际外框"""
    shift_px = max(1, int(round(cfg.max_shift_cm * CM_TO_PT * zoom)))
    dilated = _grow(box, shift_px)
    sel = []
    for b in zone.boxes:
        if b[5] == 1.0 or not _hits(b, dilated):
            continue
        ox = min(b[2], dilated[2]) - max(b[0], dilated[0])
        oy = min(b[3], dilated[3]) - max(b[1], dilated[1])
        if ox <= 0 or oy <= 0:
            continue
        # 只要"大半落在标识框里"的墨块。挨着标识的正文行、表格头常常只压到框边一两个点，
        # 把它一起圈进来就会把正文啃掉（第1页的标题块正是这样），所以按面积占比筛掉
        if ox * oy / max(1.0, (b[2] - b[0]) * (b[3] - b[1])) >= 0.5:
            sel.append(b)
    if not sel:
        return None, ""
    got = _union(sel)
    px = sum(b[4] for b in sel)
    ratio = px / max(1.0, float(np.count_nonzero(tpl)))
    if not (cfg.ink_min_ratio <= ratio <= cfg.ink_max_ratio):
        return None, f"这块墨的量是标识的{ratio:.2f}倍，混进了别的东西"

    # 该页的标识相对模板平移了 (dx, dy)，把它移回模板位置再比重合
    th, tw = tpl.shape
    ch, cw = zone.ink.shape
    bx0, by0 = int(box[0]), int(box[1])
    dx, dy = int(got[0] - box[0]), int(got[1] - box[1])
    i0, i1 = max(0, -dx), min(tw, cw - bx0 - dx)
    j0, j1 = max(0, -dy), min(th, ch - by0 - dy)
    if i1 <= i0 or j1 <= j0:
        return None, "这块墨迹和标识完全错开"
    t = tpl[j0:j1, i0:i1]
    cur = zone.ink[by0 + j0 + dy:by0 + j1 + dy, bx0 + i0 + dx:bx0 + i1 + dx] > 0
    union_n = int(np.count_nonzero(t | cur))
    iou = int(np.count_nonzero(t & cur)) / union_n if union_n else 0.0
    if iou < min_iou:
        return None, f"与标识形状只重合{iou:.2f}"
    return got, ""


def _resized(tpl: np.ndarray, s: float, canvas: Tuple[int, int]) -> Optional[np.ndarray]:
    """标识形状按 s 缩放（同一份文件里正文页眉和附件页眉的标识常常大小不同）"""
    th, tw = tpl.shape
    nh, nw = int(round(th * s)), int(round(tw * s))
    ch, cw = canvas
    if min(nh, nw) < 8 or nh + 4 >= ch or nw + 4 >= cw:
        return None
    t = cv2.resize((tpl.astype(np.uint8)) * 255, (nw, nh), interpolation=cv2.INTER_AREA)
    return t > 127


def _locate(zone: _Zone, scaled: np.ndarray, cfg: LogoConfig) -> Optional[Tuple[int, int]]:
    """角区里最像这块缩放标识的位置；只是候选，随后还要逐像素验收"""
    ink = (zone.ink > 0).astype(np.float32)
    res = cv2.matchTemplate(ink, scaled.astype(np.float32), cv2.TM_CCOEFF_NORMED)
    _, maxv, _, loc = cv2.minMaxLoc(res)
    return loc if maxv >= cfg.locate_min_corr else None


def _growth_limit(box: List[float], boxes: List[List[float]],
                  gap_px: float) -> List[float]:
    """并团时允许扩到的范围：往外够一个间隙，但页眉横线压在哪一侧，那一侧就只准扩到线上沿。
    标识常常正压在横线上，横线下面就是正文，越过去会把正文并成标识的一部分。"""
    out = _grow(box, gap_px)
    for r in boxes:
        if r[5] != 1.0 or _hits(r, box):     # 不是版面线，或线本来就在标识里
            continue
        if r[1] >= box[3]:
            out[3] = min(out[3], r[1])
        elif r[3] <= box[1]:
            out[1] = max(out[1], r[3])
        if r[0] >= box[2]:
            out[2] = min(out[2], r[0])
        elif r[2] <= box[0]:
            out[0] = max(out[0], r[2])
    return out


def _merge_mark(zone: _Zone, anchor: List[float], det: Dict[str, Any],
                cfg: LogoConfig, zoom: float) -> Tuple[List[float], List[List[float]]]:
    """从一枚团块出发把挨着的墨块并成标识外框；每并一块都要求外框不超过已知标识的
    1.25 倍，并且不许越过页眉横线（横线下面就是正文）。返回 (外框, 并进来的墨块)。
    """
    ppc = CM_TO_PT * zoom
    gap_px = max(1, int(round(cfg.cluster_gap_cm * ppc)))
    tw, th = det["template"].shape[1] * 1.25, det["template"].shape[0] * 1.25
    got = [anchor[0], anchor[1], anchor[2], anchor[3]]
    sel = [anchor]
    pool = [b for b in zone.boxes if b[5] != 1.0]
    added = True
    while added:
        added = False
        reach = _growth_limit(got, zone.boxes, gap_px)
        for b in pool:
            if b in sel or not _hits(b, reach):
                continue
            cand = [min(got[0], b[0]), min(got[1], b[1]),
                    max(got[2], b[2]), max(got[3], b[3])]
            if cand[2] - cand[0] > tw or cand[3] - cand[1] > th:
                continue
            got = cand
            sel.append(b)
            added = True
    return got, sel


def _match_by_color(zone: _Zone, det: Dict[str, Any], cfg: LogoConfig,
                    zoom: float, ignore_x: bool = False) -> Optional[List[float]]:
    """第三路：按颜色找。标识是彩色印的、正文是黑字，所以角区里最大的彩色团块就是标识
    的图形部分；把挨着它的墨块并成外框，再把已知标识按这个外框长宽拉伸对一次形状。
    同一份文件里附件页眉常把标识印成另一种比例（等比缩放套不上），这一路专门补它。

    ignore_x=True 时连"离那一角多远"都不问了，只认颜色、形状、大小和孤立性——给
    `_match_wide` 在整页宽横带上兜底用（位置选"居中"时，横向附件页和正文页的画布
    对不齐，按位置窗口会把带标识的页全判成"对不上"）。
    """
    if zone.color is None:
        return None
    ppc = CM_TO_PT * zoom
    comps = []
    for b in _boxes(zone.color.astype(np.uint8) * 255, zoom):
        if b[4] < MIN_COMP_PX or b[5] == 1.0:
            continue
        bw, bh = (b[2] - b[0]) / ppc, (b[3] - b[1]) / ppc
        # 红头文件的红线、红标题也是"彩色"，但它们要么是一条细长带子要么扁得没有形状，
        # 拿它当锚点会把整行红字并进来；标识的图形是一坨接近方形的块
        if min(bw, bh) < 0.25 or max(bw, bh) / max(0.01, min(bw, bh)) > 4.0:
            continue
        comps.append(b)
    if not comps:
        return None
    canvas = zone.ink.shape
    base = _corner_cm(det["box"], cfg, canvas, zoom)
    # 锚点只在该出现的位置附近挑：红头页那一行红标题也是一坨坨彩色方块，
    # 只挑"最大的彩色"会挑中 7.8cm 开外的红字（实测第127页）
    near = [b for b in comps
            if ignore_x or
            abs(_corner_cm(b[:4], cfg, canvas, zoom) - base) <= cfg.max_move_cm]
    if not near:
        return None
    anchor = max(near, key=lambda b: b[4])
    if anchor[4] < cfg.color_anchor_px:      # 太小的彩色是噪点/红头文件里的杂色
        return None
    # 从彩色团块出发把挨着的墨块并进同一枚标识：标识是"图形 + 单位名 + 英文缩写"三行，
    # 只取第一行会因为缺了下面那行而对不上形状。但不能像检测阶段那样无限并团——
    # 附件页多是密排表格，一跳跳开去能把整张表并进来（实测 7.9×3.7cm），所以每并一块
    # 都要求外框仍不超过已知标识的 1.25 倍，并且不许越过页眉横线
    got, sel = _merge_mark(zone, anchor, det, cfg, zoom)
    if ((got[2] - got[0]) / ppc > cfg.max_mark_w_cm
            or (got[3] - got[1]) / ppc > cfg.max_mark_h_cm):
        return None
    if not ignore_x and abs(_corner_cm(got, cfg, canvas, zoom) - base) > cfg.max_move_cm:
        return None
    if _gap_cm(zone.boxes, got, cfg, zoom) < cfg.min_gap_cm:
        return None
    x0, y0, x1, y1 = (int(got[0]), int(got[1]), int(got[2]), int(got[3]))
    if zone.red is not None and not _same_color_family(zone, det, cfg, (x0, y0, x1, y1)):
        return None
    t = cv2.resize(det["template"].astype(np.uint8) * 255,
                   (max(4, x1 - x0), max(4, y1 - y0)), interpolation=cv2.INTER_AREA) > 127
    cur = zone.ink[y0:y1, x0:x1] > 0
    if cur.shape != t.shape:
        return None
    union_n = int(np.count_nonzero(t | cur))
    if not union_n or int(np.count_nonzero(t & cur)) / union_n < cfg.min_iou_color:
        return None
    ratio = sum(b[4] for b in sel) / max(1.0, float(np.count_nonzero(t)))
    if not (cfg.ink_min_ratio <= ratio <= cfg.ink_max_ratio):
        return None
    return got


def _same_color_family(zone: _Zone, det: Dict[str, Any], cfg: LogoConfig,
                       box: Tuple[int, int, int, int]) -> bool:
    """落点的颜色构成和模板是不是一路东西：蓝绿印的标识 ≠ 红头那行红字 ≠ 盖上来的红章。

    模板偏红多少，落点也允许偏红多少（差一半以内）。放开位置窗口之后，横带上任何
    一坨彩色都可能被当成标识，这一票是唯一的颜色护栏。
    """
    x0, y0, x1, y1 = box
    col = float(np.count_nonzero(zone.color[y0:y1, x0:x1]))
    if col < cfg.color_anchor_px:
        return False
    red = float(np.count_nonzero(zone.red[y0:y1, x0:x1])) if zone.red is not None else 0.0
    t_col = max(1.0, float(det.get("colored_px") or 0))
    return abs(red / col - float(det.get("red_px") or 0) / t_col) <= 0.5


def _match_colored_shape(zone: _Zone, det: Dict[str, Any], cfg: LogoConfig,
                         zoom: float) -> Optional[List[float]]:
    """拿标识彩色那半截，在整条横带的彩色图里按几档倍率搜一遍。

    整页宽的墨迹图上模板匹配没有信号——横带里全是表格线，归一化相关被打散（实测最高
    -0.11）；彩色图里却只有标识自己，颜色对颜色才有可比性（实测带标识的页 0.81~1.00，
    没标识的页 ≤0.14）。横向附件页的标识印小一圈，落在 0.80 档；正文页是原大。
    落点还要过并团、尺寸、孤立性、颜色家族四道验收才作数。
    """
    ct = det.get("color_template")
    if ct is None or zone.color is None:
        return None
    canvas = zone.ink.shape
    hay = zone.color.astype(np.float32)
    ppc = CM_TO_PT * zoom
    bar = max(cfg.locate_min_corr, cfg.min_iou_color)
    for s in (1.0,) + tuple(cfg.mark_scales):
        t = ct if s == 1.0 else _resized(ct, s, canvas)
        if t is None or t.shape[0] >= canvas[0] or t.shape[1] >= canvas[1]:
            continue
        res = cv2.matchTemplate(hay, t.astype(np.float32), cv2.TM_CCOEFF_NORMED)
        _, maxv, _, loc = cv2.minMaxLoc(res)
        if maxv < bar:
            continue
        box = [float(loc[0]), float(loc[1]),
               float(loc[0] + t.shape[1]), float(loc[1] + t.shape[0])]
        col = float(np.count_nonzero(zone.color[int(box[1]):int(box[3]),
                                                int(box[0]):int(box[2])]))
        if col < cfg.color_anchor_px:
            continue
        got, _sel = _merge_mark(zone, box + [col, 0.0], det, cfg, zoom)
        if ((got[2] - got[0]) / ppc > cfg.max_mark_w_cm
                or (got[3] - got[1]) / ppc > cfg.max_mark_h_cm):
            continue
        if _gap_cm(zone.boxes, got, cfg, zoom) < cfg.min_gap_cm:
            continue
        x0, y0, x1, y1 = (int(got[0]), int(got[1]), int(got[2]), int(got[3]))
        if not _same_color_family(zone, det, cfg, (x0, y0, x1, y1)):
            continue
        logger.debug(f"横带按彩色形状认到标识：{s:.2f} 倍，相关 {maxv:.2f}")
        return got
    return None


def _match_wide(page: Optional[pymupdf.Page], zone: _Zone, det: Dict[str, Any],
                cfg: LogoConfig, zoom: float) -> Optional[List[float]]:
    """最后一招：在整页宽的横带里按"这枚彩色印痕"找，不锁它离角多远。

    位置选"顶部居中"时画布是按页面对齐的，而这份文件正文页 10.1cm 宽、附件页 14.3cm 宽，
    同一枚钉在左角的标识在两块画布上差 4cm 以上——按位置窗口判全都算"对不上"，成品就
    留下二十几页页眉（实测 4 号 25 页，其中 15 页在成品上跑到左下角去了，因为自动旋转
    排在去标识后面）。这一招只在角区里按位置全对不上的页才走，认的是颜色、形状、大小
    和孤立性四样；模板本身不是彩印的（黑白标识）不走，避免拿正文当标识。
    """
    if page is None or not det.get("colored"):
        return None
    wide = _row_zone(page, cfg, zoom)
    if wide is None or wide.color is None:
        return None
    got = _match_by_color(wide, det, cfg, zoom, ignore_x=True)
    if got is None:
        got = _match_colored_shape(wide, det, cfg, zoom)
    if got is None:
        return None
    return zone.to_canvas(wide.to_view(got, 0.0, page.rect))


def _page_mark(zone: _Zone, det: Dict[str, Any], cfg: LogoConfig,
               zoom: float, page: Optional[pymupdf.Page] = None
               ) -> Tuple[Optional[List[float]], str]:
    """在该页把模板对上去，返回画布坐标下该页的实际墨迹框；对不上返回原因"""
    box = det["box"]
    got, why = _fit_mark(zone, box, det["template"], cfg, zoom, cfg.min_iou)
    if got is not None:
        return got, ""
    shift_px = max(1, int(round(cfg.max_shift_cm * CM_TO_PT * zoom)))
    if not why:
        why = _empty_reason(zone, _grow(box, shift_px), cfg, zoom)
    if not cfg.mark_scales:
        return None, why

    # 同一份文件里标识往往有两种尺寸：正文页眉印得大，附件/表格页眉印得小。
    # 这里把已经确认是标识的那枚形状缩小了再找一遍——比对的是这枚标识自己的形状，
    # 不是"随便哪块页页都有的墨"，所以比把跨页一致比例调低安全得多。
    canvas = zone.ink.shape
    base = _corner_cm(box, cfg, canvas, zoom)
    for s in cfg.mark_scales:
        scaled = _resized(det["template"], s, canvas)
        if scaled is None:
            continue
        loc = _locate(zone, scaled, cfg)
        if loc is None:
            continue
        cand = [float(loc[0]), float(loc[1]),
                float(loc[0] + scaled.shape[1]), float(loc[1] + scaled.shape[0])]
        if abs(_corner_cm(cand, cfg, canvas, zoom) - base) > cfg.max_move_cm:
            continue
        got, _why2 = _fit_mark(zone, cand, scaled, cfg, zoom, cfg.min_iou_scaled)
        if got is None:
            continue
        # 缩放匹配的位置是猜的，必须再确认这块墨朝正文那侧是孤立的（标识是页边空白里
        # 独立的一块，正文行下面永远接着下一行）
        if _gap_cm(zone.boxes, got, cfg, zoom) < cfg.min_gap_cm:
            continue
        return got, ""
    got = _match_by_color(zone, det, cfg, zoom)
    if got is not None:
        return got, ""
    got = _match_wide(page, zone, det, cfg, zoom)
    if got is not None:
        return got, ""
    return None, why


class _LogoEraser(PageNumberRemover):
    """去页码那条流水线的标识版：只重画标识自己的墨迹，不整块涂平。

    去页码敢把整个框重画，因为页码框本来就是页边空白里孤立的一小条；标识却常常正压在
    页眉横线上方（实测只隔 0.13cm），整块涂平会把横线啃掉一截、断口两头还留下毛刺。
    """

    def __init__(self, logo_cfg: LogoConfig):
        super().__init__(logo_cfg.seal_config())
        self.logo_cfg = logo_cfg
        self.mark_rect: Optional[pymupdf.Rect] = None
        self.keep_rects: List[pymupdf.Rect] = []
        self.skip_reason = ""

    def target(self, mark_rect: pymupdf.Rect,
               keep_rects: Optional[List[pymupdf.Rect]] = None):
        """这一页要抹的标识本身在哪（视图坐标，不含外扩）"""
        self.mark_rect = mark_rect
        # 穿过标识框的版面线条（页眉横线）：标识压在横线上时，掩码要绕开线那几行，
        # 宁可线上留一点标识的淡影，也不能把横线啃掉一段
        self.keep_rects = list(keep_rects or [])
        self.skip_reason = ""

    @staticmethod
    def _px_box(rect: pymupdf.Rect, clip: pymupdf.Rect, zoom: float,
                shape) -> Tuple[int, int, int, int]:
        x0 = max(0, int((rect.x0 - clip.x0) * zoom))
        y0 = max(0, int((rect.y0 - clip.y0) * zoom))
        x1 = min(shape[1], int(np.ceil((rect.x1 - clip.x0) * zoom)))
        y1 = min(shape[0], int(np.ceil((rect.y1 - clip.y0) * zoom)))
        return x0, y0, x1, y1

    def _patch_ink_mask(self, arr: np.ndarray, rect: pymupdf.Rect, clip: pymupdf.Rect,
                        zoom: float, page_rect: pymupdf.Rect):
        """标识自己的像素掩码；判定"这页不该动"时返回 None 并留下原因"""
        h, w = arr.shape[:2]
        grow = max(1, int(round(self.logo_cfg.mark_dilate_cm * CM_TO_PT * zoom)))
        mx0, my0, mx1, my1 = self._px_box(self.mark_rect, clip, zoom, arr.shape)
        if mx1 <= mx0 or my1 <= my0:
            self.skip_reason = "标识位置出了这一页"
            return None

        rgb = arr[:, :, :3]
        gray = rgb.mean(axis=2)
        sat = rgb.max(axis=2).astype(np.int16) - rgb.min(axis=2).astype(np.int16)
        paper = float(np.median(gray))
        # 标识框内一律按"只要比纸面暗一点就算标识"来判：这个框已经被逐页验收过，
        # 里面只可能有标识、纸面和被单独保护的横线，按 45 级那么严会留下字脚下那圈
        # 只剩二三十级的淡影，放大看就是几个没擦干净的笔画头
        ink = (gray < paper - self.logo_cfg.mark_ink_delta) | (sat > self.logo_cfg.mask_sat_delta)
        mark = np.zeros((h, w), bool)
        mark[my0:my1, mx0:mx1] = True
        mask = np.where(ink & mark, 255, 0).astype(np.uint8)
        if not mask.any():
            self.skip_reason = "这一角已经没有可抹的墨迹"
            return None

        allowed = np.zeros((h, w), np.uint8)
        # 掩码只准落在标识框内：向外放大虽然能盖住毛边，可标识常常正压在页眉横线上方
        # （实测只隔 0.02cm 左右），一放大就把横线最上面一行重画掉，差分成一条断线
        allowed[my0:my1, mx0:mx1] = 255
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1))
        mask = cv2.dilate(mask, kernel) & allowed
        for keep in self.keep_rects:
            kx0, ky0, kx1, ky1 = self._px_box(keep, clip, zoom, (h, w))
            mask[ky0:ky1, kx0:kx1] = 0
        if not mask.any():
            self.skip_reason = "这一角已经没有可抹的墨迹"
            return None

        if self.config.protect_seals:
            seal = detect_seal_regions(
                arr, self.config, rgb=True,
                page_area_px=page_rect.get_area() * zoom * zoom,
                page_min_side_px=min(page_rect.width, page_rect.height) * zoom
                * self.config.seal_min_size_ratio)
            if seal is not None:
                inside = int(np.count_nonzero((seal > 0) & mark))
                outside = int(np.count_nonzero((seal > 0) & ~allowed.astype(bool)))
                # 彩色只出在标识框里 = 那本来就是彩色印的标识；伸出框外的是压在标识上的
                # 印章，整页保留不动——绝不能只把章抹掉一半
                if inside and outside > 0.05 * (inside + outside):
                    self.skip_reason = "标识位置上有印章，保留不动"
                    return None
        return mask


def _room_pt(zone: _Zone, box: List[float], zoom: float) -> float:
    """标识框外扩多少点才会碰上角区里的另一块墨迹（页眉横线最典型）"""
    room = float("inf")
    for b in zone.boxes:
        dx = ((b[0] - box[2]) if b[0] >= box[2]
              else (box[0] - b[2] if b[2] <= box[0] else -1.0))
        dy = ((b[1] - box[3]) if b[1] >= box[3]
              else (box[1] - b[3] if b[3] <= box[1] else -1.0))
        need = max(dx, dy)          # 两个方向都被外扩框盖住才算相碰
        if need < 0:                # 本来就压在标识框里：是标识自己的字，或是被保护的线
            continue
        room = min(room, need)
    return room / zoom


def _mask_slack_pt(zone: _Zone, box: List[float], zoom: float) -> float:
    """掩码比标识框多让出多少点：够得着框外那半行笔画底，又绝不碰角上的邻居"""
    return max(0.0, min(MASK_SLACK_PT, _room_pt(zone, box, zoom) - 1.0))


def _clip_pad_pt(zone: _Zone, box: List[float], zoom: float, outer_pt: float) -> float:
    """重绘补丁还能往外多取多少点。
    补丁是按 300dpi 重新渲染再贴回去的，贴回去的那一片会比别处淡一点、也可能错开半像素；
    标识下缘常常离页眉横线只有几个点，多取这几正好把横线重采了一遍，成品上就是一条
    一厘米多长的淡线。所以邻居在哪个方向多近，补丁就只准扩到它跟前——再留出 1pt，
    那是横线自己带的一圈抗锯齿边，压上去就会让线在最上面那一行变淡。"""
    return max(0.0, min(4.0, _room_pt(zone, box, zoom) - outer_pt - 1.0))


def remove_logos_pdf(input_path: str, output_path: str, cfg: LogoConfig = None,
                     page_range: str = "",
                     progress_callback: Optional[Callable] = None) -> Dict[str, Any]:
    """
    去掉一个 PDF 每一页角上的固定标识。

    没找到标识、或者一页都没抹成时不产出文件（status=skipped），免得用户在输出目录里
    看到一份和原件一模一样的副本，误以为处理过了。

    Args:
        input_path / output_path: 输入输出路径
        cfg: 检测参数
        page_range: 页面范围字符串（如"3,5-7"），空表示全部
        progress_callback: 进度回调 callback(page_index, total_pages, message)

    Returns:
        status（success/skipped）、removed_pages、每页的 details、检测结论 detection
    """
    cfg = cfg or LogoConfig()
    from pdf_page_remover.rotator import parse_page_range

    doc = pymupdf.open(input_path)
    total_pages = len(doc)
    pages = (sorted(parse_page_range(page_range, total_pages)) if page_range
             else list(range(total_pages)))
    try:
        det = detect_logo(doc, cfg, page_range)
        if not det["found"]:
            return _skipped(total_pages, det, det.get("reason", "没有找到跨页一致的角上标识"))

        zoom = SCAN_ANALYZE_DPI / 72.0
        canvas = cfg.canvas_shape(zoom)
        pad_pt = cfg.pad_cm * CM_TO_PT
        eraser = _LogoEraser(cfg)
        details: List[Dict[str, Any]] = []
        for page_index in pages:
            if progress_callback:
                progress_callback(page_index, total_pages,
                                  f"正在去除第 {page_index + 1} 页标识...")
            page = doc[page_index]
            zone = _analyze_page(page, cfg, zoom, canvas)
            if zone is None:
                continue
            got, why = _page_mark(zone, det, cfg, zoom, page)
            if got is None:
                details.append({"page_index": page_index, "removed": False, "reason": why})
                continue
            work, box, keep = zone, got, None
            lead_pad_pt = None
            if cfg.whole_row:
                wide = _row_zone(page, cfg, zoom)
                row, below, on_rule = ((None, 0.0, False) if wide is None else
                                       _row_box(wide,
                                                wide.to_canvas(zone.to_view(got, 0.0, page.rect)),
                                                cfg, zoom))
                if row is None:
                    details.append({"page_index": page_index, "removed": False,
                                    "reason": "整行页眉并出来太高（越过了行距），这一页保留不动"})
                    continue
                if below < pad_pt + 1.0 and not on_rule:
                    details.append({"page_index": page_index, "removed": False,
                                    "reason": "整行页眉下方就压着表格线，这一页保留不动"})
                    continue
                if below < pad_pt + 1.0:
                    # 整行的下边就是被吸收进来的那条页眉横线，线下面紧接着表格线。
                    # 这种页要的不是"整页放弃"——客户看到的是一整行还在。擦除框朝正文
                    # 那一侧只让到离下一片墨还剩 1pt，宁可擦窄一毫米也不啃表格线。
                    lead_pad_pt = max(0.0, (below - 1.0) / 2.0)
                work, box, keep = wide, row, []
            slack_px = _mask_slack_pt(work, box, zoom) * zoom
            mbox = _grow(box, slack_px)
            mark_view = work.to_view(mbox, 0.0, page.rect)
            rect = work.to_view(box, pad_pt, page.rect)
            if lead_pad_pt is not None:
                edge = work.clip.y0 + (box[3] - work.off_y) / work.zoom
                rect.y1 = min(rect.y1, edge + lead_pad_pt)
            if rect.width < 2 or rect.height < 2 or mark_view.width < 2:
                details.append({"page_index": page_index, "removed": False,
                                "reason": "位置出了页面范围"})
                continue
            eraser.target(mark_view, keep if keep is not None else
                          [work.to_view(b[:4], 0.0, page.rect) for b in work.boxes
                           if b[5] == 1.0 and _hits(b, mbox)])
            done = eraser.erase_rect_inplace(doc, page, rect, page_index,
                                             pad_pt=_clip_pad_pt(work, box, zoom, pad_pt))
            details.append({
                "page_index": page_index, "removed": bool(done),
                "reason": "" if done else (eraser.skip_reason
                                           or "标识位置上有注释（电子签章），保留不动"),
                "rect": [round(v, 1) for v in (rect.x0, rect.y0, rect.x1, rect.y1)],
            })

        removed = [d for d in details if d.get("removed")]
        if not removed:
            return _skipped(total_pages, det, "标识和正文对不上，一页都没动")

        doc.save(output_path, garbage=4, deflate=True)
        if progress_callback:
            progress_callback(total_pages, total_pages, "标识去除完成！")
        return {
            "status": "success",
            "total_pages": total_pages,
            "removed_count": len(removed),
            "removed_pages": len({d["page_index"] for d in removed}),
            "checked_pages": len(pages),
            "details": details,
            "detection": _plain(det),
            "message": "",
        }
    finally:
        doc.close()


def _plain(det: Dict[str, Any]) -> Dict[str, Any]:
    """检测结论去掉掩码，能进日志和结果弹窗"""
    return {k: v for k, v in det.items() if not k.endswith("template")}


def _skipped(total_pages: int, det: Dict[str, Any], message: str) -> Dict[str, Any]:
    return {"status": "skipped", "total_pages": total_pages, "removed_count": 0,
            "removed_pages": 0, "checked_pages": det.get("pages_total", 0),
            "details": [], "detection": _plain(det), "message": message}


def batch_remove_logos(input_files: List[str], output_dir: str,
                       cfg: LogoConfig = None, page_range: str = "",
                       progress_callback: Optional[Callable] = None) -> List[Dict]:
    """
    批量去除多个 PDF 角上的固定标识

    Args:
        input_files: 输入PDF路径列表
        output_dir: 输出目录
        cfg: 检测参数
        page_range: 页面范围字符串
        progress_callback: 进度回调 callback(file_index, total_files, page_index, total_pages, message)

    Returns:
        每个文件的结果；success 才有 output_file
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    all_stats: List[Dict] = []
    total_files = len(input_files)
    for file_index, input_path in enumerate(input_files):
        if not os.path.exists(input_path):
            logger.warning(f"文件不存在: {input_path}")
            continue
        filename = os.path.basename(input_path)

        def page_progress(page_index, total_pages, message, fi=file_index):
            if progress_callback:
                progress_callback(fi, total_files, page_index, total_pages, message)

        output_path = os.path.join(output_dir, filename)
        try:
            stats = remove_logos_pdf(input_path, output_path, cfg=cfg,
                                     page_range=page_range, progress_callback=page_progress)
        except Exception as e:
            logger.error(f"标识去除失败: {filename} - {e}")
            stats = {"status": "error", "error": str(e), "total_pages": 0,
                     "removed_count": 0, "removed_pages": 0, "checked_pages": 0,
                     "details": [], "message": ""}
        stats["input_file"] = input_path
        stats["output_file"] = output_path if stats["status"] == "success" else ""
        if stats["status"] == "success":
            logger.info(f"标识去除完成: {filename} - {stats['removed_pages']} 页")
        else:
            logger.info(f"标识去除: {filename} - {stats.get('message', '未处理')}")
        all_stats.append(stats)
    return all_stats
