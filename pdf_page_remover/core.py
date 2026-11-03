"""
PDF页码去除核心模块
支持文本型PDF和扫描件PDF的页码检测与去除
"""

import re
import pymupdf  # PyMuPDF (fitz API已弃用)
import cv2
import numpy as np
from PIL import Image
import io
import os
import logging
from typing import List, Tuple, Optional, Dict, Any

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 印章保护只需要判断"页码框里有多少彩色像素"，重叠比例与分辨率基本无关，
# 因此印章检测单独用较低分辨率渲染，比按输出DPI整页渲染快一个数量级。
SEAL_DETECT_DPI = 110

# 扫描页页码定位用的分析分辨率：页码墨迹极淡，但只需找到"孤立的一小行"，150DPI 足够
SCAN_ANALYZE_DPI = 150
# 修补页码那一小块时用高分辨率渲染，补出来的背景才接得上原图细节
SCAN_PATCH_DPI = 300


class PageNumberConfig:
    """页码检测配置"""
    def __init__(
        self,
        # 页码位置区域（相对于页面尺寸的比例）
        bottom_margin_ratio: float = 0.12,  # 底部页码区域高度占比
        top_margin_ratio: float = 0.08,     # 顶部页码区域高度占比
        # 页码文本模式
        page_number_patterns: List[str] = None,
        # 图像处理参数
        dpi: int = 200,
        # 扫描件检测阈值
        scan_detection_threshold: float = 0.9,
        # 页码区域中心范围（水平方向）
        center_range_ratio: float = 0.4,
        # 是否检测侧边页码
        check_side_numbers: bool = True,
        side_margin_ratio: float = 0.15,  # 侧边页码区域宽度占比（需覆盖旋转后从底部移到侧边的页码）
        # 孤立单行文本放宽到"离最近边不超过页面短边此比例"，覆盖横向页码被挪到底角的情况
        corner_margin_ratio: float = 0.20,
        # 印章保护：避免把印章（含印章上的编号/日期）当成页码抹掉
        protect_seals: bool = True,
        seal_saturation_min: int = 60,      # HSV饱和度下限，低于此值视为黑/灰印刷
        seal_value_min: int = 45,           # 过暗像素不算印章墨迹
        seal_min_size_ratio: float = 0.05,  # 印章最小边长占页面短边的比例
        seal_max_area_ratio: float = 0.35,  # 超过页面面积此比例的色块不视为印章
        seal_overlap_ratio: float = 0.25,   # 页码框内印章覆盖占比达到此值即判定与印章重叠
    ):
        self.bottom_margin_ratio = bottom_margin_ratio
        self.top_margin_ratio = top_margin_ratio
        self.page_number_patterns = page_number_patterns or [
            r'^\s*\d+\s*$',                          # 纯数字: 1, 2, 123
            r'^\s*[-—–]\s*\d+\s*[-—–]\s*$',         # 破折号包围: - 1 -, —2—
            r'^\s*\d+\s*/\s*\d+\s*$',               # 格式: 1/10, 2 / 20
            r'^\s*第\s*\d+\s*页?\s*$',               # 中文: 第1页, 第 2 页
            r'^\s*P\s*\.?\s*\d+\s*$',               # P.1, P1, P. 2
            r'^\s*page\s*\d+\s*$',                   # page 1, Page 2
            r'^\s*p\s*\.?\s*\d+\s*$',               # p.1, p1
            r'^\s*\[\s*\d+\s*\]\s*$',               # [1], [ 2 ]
            r'^\s*-\s*\d+\s*-\s*$',                 # -1-, - 2 -
            r'^\s*\d+\s*[.,]\s*\d*\s*$',            # 带小数点(宽松)
        ]
        self.dpi = dpi
        self.scan_detection_threshold = scan_detection_threshold
        self.center_range_ratio = center_range_ratio
        self.check_side_numbers = check_side_numbers
        self.side_margin_ratio = side_margin_ratio
        self.corner_margin_ratio = corner_margin_ratio
        self.protect_seals = protect_seals
        self.seal_saturation_min = seal_saturation_min
        self.seal_value_min = seal_value_min
        self.seal_min_size_ratio = seal_min_size_ratio
        self.seal_max_area_ratio = seal_max_area_ratio
        self.seal_overlap_ratio = seal_overlap_ratio


def detect_seal_regions(img_bgr: np.ndarray, config: PageNumberConfig,
                        rgb: bool = False, page_area_px: float = None,
                        page_min_side_px: float = None) -> Optional[np.ndarray]:
    """
    找出页面上的印章/彩色图案区域，返回与图像等大的掩码（255=受保护像素），无则返回 None。

    印章墨迹是有色（红/蓝）的，而页码几乎都是黑色印刷，因此用 HSV 饱和度即可稳定区分；
    命中区域按外接矩形整块保护，避免把印章内部的编号、日期当成页码抹掉。

    Args:
        rgb: 传入图像是 RGB 顺序（渲染结果）而不是 BGR，省一次整图通道转换
        page_area_px / page_min_side_px: 只渲染了页面局部时，用它把"整页大半是彩色就不保护"
            和"太小的色块不算印章"两条判据换算回整页尺度，避免局部视图下误判
    """
    if img_bgr is None or img_bgr.ndim != 3:
        return None

    height, width = img_bgr.shape[:2]
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_RGB2HSV if rgb else cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    ink = ((sat >= config.seal_saturation_min) & (val >= config.seal_value_min)).astype(np.uint8) * 255

    ref_area = page_area_px if page_area_px else height * width
    ink_area = int(np.count_nonzero(ink))
    # 整页大半都是彩色时（彩印件、照片页）不再保护，否则页码永远去不掉
    if ink_area == 0 or ink_area > ref_area * 0.6:
        return None

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, kernel, iterations=2)
    count, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)

    seal_mask = np.zeros((height, width), dtype=np.uint8)
    min_side = int(page_min_side_px) if page_min_side_px else max(20, int(min(height, width) * config.seal_min_size_ratio))
    min_side = max(20, min_side)
    max_area = ref_area * config.seal_max_area_ratio
    for i in range(1, count):
        x, y, bw, bh = stats[i, 0], stats[i, 1], stats[i, 2], stats[i, 3]
        if bw < min_side or bh < min_side or bw * bh > max_area:
            continue
        seal_mask[y:y + bh, x:x + bw] = 255

    return seal_mask if np.any(seal_mask) else None


def seal_cover_ratio(seal_mask: np.ndarray, rect: "pymupdf.Rect", zoom: float) -> float:
    """页码框内被印章覆盖的面积占比（0~1）。"""
    if seal_mask is None:
        return 0.0
    height, width = seal_mask.shape
    x0 = max(0, int(rect.x0 * zoom))
    y0 = max(0, int(rect.y0 * zoom))
    x1 = min(width, int(rect.x1 * zoom))
    y1 = min(height, int(rect.y1 * zoom))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    sub = seal_mask[y0:y1, x0:x1]
    return float(np.count_nonzero(sub)) / float(sub.size)


def annot_protect_rects(doc: "pymupdf.Document", page: "pymupdf.Page") -> List["pymupdf.Rect"]:
    """页面上所有注释与表单控件的矩形（未旋转坐标），涂黑要绕开它们。

    apply_redactions 会顺手删掉与涂黑框相交的注释，而金格这类电子签章是 /FT/Sig
    签名控件，page.annots() 根本不返回它——只按 annots() 避让等于没避让。
    """
    rects: List[pymupdf.Rect] = []
    for getter in (page.annots, page.widgets):
        try:
            rects.extend(a.rect for a in getter())
        except Exception:
            logger.debug(f"第{page.number + 1}页: {getter.__name__} 枚举失败")
    from .scaler import _page_annot_xrefs  # 自己解析 /Annots，PyMuPDF 不认的自定义类型也在这里
    for xref in _page_annot_xrefs(doc, page):
        try:
            kind, val = doc.xref_get_key(xref, "Rect")
        except Exception:
            continue
        nums = re.findall(r'-?\d+(?:\.\d+)?', val or "")
        if len(nums) >= 4:
            try:
                rects.append(pymupdf.Rect(*(float(n) for n in nums[:4])))
            except ValueError:
                continue
    return rects


class PageNumberDetector:
    """页码检测器"""
    def __init__(self, config: PageNumberConfig = None):
        self.config = config or PageNumberConfig()
        # 检测阶段算好的印章掩码，修复阶段直接复用，避免同一页再做一次整页分析
        self._seal_cache = None

    def cache_seal_mask(self, page_index: int, zoom: float, mask):
        self._seal_cache = (page_index, zoom, mask)

    def get_cached_seal_mask(self, page_index: int, zoom: float):
        """返回 (是否命中, 掩码)"""
        cache = self._seal_cache
        if cache is not None and cache[0] == page_index and abs(cache[1] - zoom) < 1e-9:
            return True, cache[2]
        return False, None

    def is_scanned_pdf(self, page: pymupdf.Page) -> bool:
        """
        判断页面是否为扫描件（图片型PDF）
        通过检查页面中的图像占比来判断
        """
        # 获取页面中的图像列表
        image_list = page.get_images(full=True)
        if not image_list:
            return False

        # 获取页面尺寸
        page_rect = page.rect
        page_area = page_rect.width * page_rect.height

        # 计算图像覆盖面积
        total_image_area = 0
        for img_info in image_list:
            xref = img_info[0]
            try:
                # 获取图像在页面上的位置
                img_rects = page.get_image_rects(xref)
                for rect in img_rects:
                    img_area = rect.width * rect.height
                    total_image_area += img_area
            except Exception:
                continue

        # 如果图像覆盖面积占页面面积的90%以上，则认为是扫描件
        coverage_ratio = total_image_area / page_area if page_area > 0 else 0
        if coverage_ratio >= self.config.scan_detection_threshold:
            return True
        # 缩过边距、补过瑕疵的扫描页，整页底图会被挪位或缩小到阈值以下（实测 0.87~0.89），
        # 这种页一个文字对象都提不出来，仍然是扫描件。以前只看覆盖率就把它们当成文本页，
        # 结果"自动"模式下走文本分支，扫描页码一个都去不掉（用户报的 15号 那份正是这样）。
        return not page.get_text().strip()

    @staticmethod
    def _upright_rect(rect, page_width, page_height, dir_y):
        """把"竖排于视图"的矩形换算到文字正立的那个空间（页面宽高随之互换）

        dir_y<0 表示文字自下而上，读者要把页面顺时针转 90° 才正立；dir_y>0 反之。
        """
        if dir_y < 0:
            out = pymupdf.Rect(page_height - rect.y1, rect.x0,
                               page_height - rect.y0, rect.x1)
        else:
            out = pymupdf.Rect(rect.y0, page_width - rect.x1,
                               rect.y1, page_width - rect.x0)
        out.normalize()
        return out

    def detect_text_page_numbers(self, page: pymupdf.Page, page_index: int) -> List[Dict]:
        """
        检测文本型PDF页面中的页码
        返回页码文本块列表，每个元素包含文本内容和位置信息
        """
        results = []
        page_rect = page.rect
        page_width = page_rect.width
        page_height = page_rect.height
        # get_text 给的是未旋转坐标，而页面宽高和页边带看的是显示方向：
        # 旋转页必须换算到视图坐标才能判对页码在哪个边（实测涂黑仍要用未旋转坐标）
        rotated = page.rotation % 360 != 0
        if rotated:
            unrot_rect = pymupdf.Rect(page_rect * page.derotation_matrix)
            unrot_rect.normalize()
            unrot_w, unrot_h = unrot_rect.width, unrot_rect.height
        else:
            unrot_w, unrot_h = page_width, page_height

        # 获取页面所有文本块
        text_dicts = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)

        for block in text_dicts.get("blocks", []):
            if block.get("type") != 0:  # 只处理文本块
                continue

            lines = block.get("lines", [])
            if not lines:
                continue

            block_rect = pymupdf.Rect(block["bbox"])
            if rotated:
                block_view = pymupdf.Rect(block_rect * page.rotation_matrix)
                block_view.normalize()
            else:
                block_view = block_rect

            # 页码要相对"它自己正立的方向"判边带：横排文字用未旋转坐标；竖排于视图的文字
            # （整页被转置存放、靠 /Rotate 补偿的横向页）用视图坐标；而 /Rotate 已经被烧进
            # 内容流的页（rotation=0 但文字全是竖的，缩放/旋转步骤会产出这种页）要把视图矩形
            # 再转 90° 换回文字正立的逻辑空间、宽高互换。否则页码落在视图角落里，边带全够不着
            dx, dy = lines[0].get("dir", (1.0, 0.0))
            if abs(dy) > abs(dx):
                if rotated:
                    check_rect = block_view
                    space_w, space_h = page_width, page_height
                    space_mode = "view"
                else:
                    check_rect = self._upright_rect(block_view, page_width, page_height, dy)
                    space_w, space_h = page_height, page_width
                    space_mode = "upright"
            else:
                check_rect = block_rect
                space_w, space_h = unrot_w, unrot_h
                space_mode = "unrot"

            # 检查是否在页码区域（底部、顶部或侧边）；用整块判定，
            # 单行判定会把正文左栏的序号、表格行号当成侧边页码
            is_in_page_number_area = False

            # 检查底部区域
            bottom_boundary = space_h * (1 - self.config.bottom_margin_ratio)
            if check_rect.y0 >= bottom_boundary:
                is_in_page_number_area = True

            # 检查顶部区域
            top_boundary = space_h * self.config.top_margin_ratio
            if check_rect.y1 <= top_boundary:
                is_in_page_number_area = True

            # 检查侧边区域（包括旋转后从底部移到侧边的页码）
            if self.config.check_side_numbers:
                left_boundary = space_w * self.config.side_margin_ratio
                right_boundary = space_w * (1 - self.config.side_margin_ratio)
                if check_rect.x1 <= left_boundary or check_rect.x0 >= right_boundary:
                    # 侧边页码：放宽垂直范围以覆盖旋转后的页码
                    mid_height = space_h / 2
                    if abs((check_rect.y0 + check_rect.y1) / 2 - mid_height) < space_h * 0.4:
                        is_in_page_number_area = True

            if not is_in_page_number_area:
                # 横向公文页的"— 13 —"印在底边外角，被按边距缩放挪过位后严格的边带会差几磅够不着。
                # 这里只给"整块就一行"的孤立文本放宽到离最近一条边不超过页面短边的 20%；
                # 正文左栏序号、表格行号那种多行大块不受影响
                if len(lines) != 1:
                    continue
                nearest = min(check_rect.x0, space_w - check_rect.x1,
                              check_rect.y0, space_h - check_rect.y1)
                if nearest > min(space_w, space_h) * self.config.corner_margin_ratio:
                    continue
                corner_relax = True
            else:
                corner_relax = False

            for line in lines:
                line_text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                if not line_text:
                    continue

                line_rect = pymupdf.Rect(line["bbox"])
                if rotated:
                    view_rect = pymupdf.Rect(line_rect * page.rotation_matrix)
                    view_rect.normalize()
                else:
                    view_rect = line_rect
                if space_mode == "view":
                    line_check = view_rect
                elif space_mode == "upright":
                    line_check = self._upright_rect(view_rect, page_width, page_height, dy)
                else:
                    line_check = line_rect

                # 检查文本是否匹配页码模式
                for pattern in self.config.page_number_patterns:
                    if not re.match(pattern, line_text, re.IGNORECASE):
                        continue
                    # 角落放宽只认带装饰的页码（— 13 —、第3页、3/12）；纯数字条目编号
                    # （正文里的 "4"、"1.1"、"10"…）只能靠严格边带，放宽了就会误删
                    if corner_relax and not re.search(r"[^\d\s.]", line_text):
                        break
                    # 额外验证：检查是否在页面中心水平区域
                    line_center_x = (line_check.x0 + line_check.x1) / 2
                    page_center_x = space_w / 2
                    center_range = space_w * self.config.center_range_ratio

                    # 页码通常在页面中心附近或左右两侧
                    in_center = abs(line_center_x - page_center_x) <= center_range
                    in_side = (line_check.x1 <= space_w * 0.20 or
                               line_check.x0 >= space_w * 0.80)

                    if in_center or in_side:
                        results.append({
                            "text": line_text,
                            "rect": line_rect,
                            "view_rect": view_rect,
                            "page_index": page_index,
                            "type": "text",
                            "confidence": 0.9 if in_center else 0.7
                        })
                    break

        return results

    # 同一行内允许的字间距："— 3 —" 这种公文页码里破折号与数字之间约 8~15pt
    SCAN_LINE_MERGE_PT = 22.0
    SCAN_MIN_GLYPH_AREA = 6
    SCAN_MAX_GLYPH_W_PT = 60.0
    SCAN_MAX_GLYPH_H_PT = 24.0
    SCAN_MIN_LINE_PT = 3.0
    SCAN_MAX_LINE_PT = 20.0
    # 页码是一"行"字，横向总要有这么宽；封面只印一个"1"时宽不到 2 点
    SCAN_MIN_LINE_W_PT = 1.5
    SCAN_MAX_W_RATIO = 0.30
    # 页码要"突出"：在页边方向上比同一截范围内的其他墨迹块至少还外伸这么多。
    # 表格格子里的字、被印章截断的落款都紧贴正文，突出量只有几个点；
    # 真页码一般能外伸 20pt 以上（实测分离度很好，12pt 落在中间空档）
    SCAN_PROTRUDE_PT = 12.0
    # 算突出量时只看候选沿边方向前后这么长一段里的墨迹块，整页比会漏掉远处的正文
    SCAN_PROTRUDE_WINDOW_PT = 40.0
    # 候选所在的整行/整列朝正文方向还延伸这么长，说明候选只是这一行的头一个字或尾一个字
    SCAN_LINE_TAIL_PT = 60.0
    # 离边同样远（相差不到这么多）的候选算"同一列/同一行"
    SCAN_PEER_TOL_PT = 4.0
    # 同一列/同一行里凑够这么多块、且沿边排开页面这么长，就是正文而不是页码
    SCAN_PEER_MIN = 4
    SCAN_PEER_SPAN_RATIO = 0.30
    # 页码在正文块之外的空白里：朝页边方向至少还要比正文最外沿外伸这么多
    SCAN_OUTSIDE_BODY_PT = 4.0
    # 一条墨迹要有这么长才算"一行正文"（页码只有几毫米，撑不开这个长度）
    SCAN_BODY_LINE_PT = 60.0
    # 边带里比这还小的墨迹是纸屑和标点，不可能是页码
    SCAN_MIN_LINE_MAX_PT = 5.0
    # 这么薄、长宽比又超过这个数的墨迹是扫描黑框/表格线/下划线，不是字也不是页码
    # （旋转着扫的页码数字又小又方，长宽比不到 2，不会被误当成线）
    SCAN_RULE_THICK_PT = 5.0
    SCAN_RULE_ASPECT = 3.0
    # 比这还细的墨迹才可能是"根本不是字"的线。页码里那一横（"— 22 —" 淡到只剩破折号）
    # 有 4~7 点高，跟正文下划线一样扁，只能靠这个更松的门槛把它当页码留下来。
    SCAN_RULE_CAND_THICK_PT = 2.5
    # 短到这个数以下的细条不是线，是一笔字：页码"1"就是 1.9x6.2 的一竖，
    # 细长比和横线一样，只有"够长才谈得上是一条线"才判成线（真正的扫描黑框、
    # 表格线都是几十点上长）
    SCAN_RULE_CAND_MIN_LEN_PT = 12.0
    # 离纸边不足这么远的墨迹是扫描仪吃进去的纸边阴影/脏点：既不是页码（页码是印上去的，
    # 实测这一批公文离边 48~95 点），也不该被当成撑开正文块的一行
    SCAN_EDGE_DEBRIS_PT = 12.0
    # 正文横躺还是正立，由下面三条各自投票，取多数（见 _is_sideways）
    # 1) 按列拼比按行拼多连出这么倍，这一票算"横躺"
    SCAN_SIDEWAYS_RATIO = 1.1
    # 2) 哪个方向拼出的正文行总长更长（超过这个倍数），这一票算"横躺"
    SCAN_SIDEWAYS_LEN_RATIO = 1.0
    # 3) 汉字部件正立时高比宽大得多，横躺过来就反过来：高/宽低于这个比就这一票算"横躺"
    SCAN_SIDEWAYS_ASPECT = 0.7
    # 字太少时方向判断不可靠，这种页两种边带都放行
    SCAN_MIN_ORIENT_GLYPHS = 20

    def detect_scanned_page_numbers(self, page: pymupdf.Page, page_index: int) -> List[Dict]:
        """
        检测扫描件页面中的页码：在页边空白里找"孤立的一小行墨迹"。

        这里刻意不用 OCR。公文页码（— 3 —）的墨色常常淡到只剩十几个灰度级，
        裁成小图后 OCR 基本读不出来（实测 RapidOCR 与 Tesseract 都大面积返回空），
        而形态学定位又快又准：每页分析约 170ms（旧的 OCR 路线要 6~12 秒），
        比对 48 页真值召回 41 处、零误检。
        """
        zoom = SCAN_ANALYZE_DPI / 72
        try:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        except Exception as e:
            logger.debug(f"扫描页渲染失败: {e}")
            return []
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        gray = arr[:, :, :3].mean(axis=2).astype(np.uint8)

        page_rect = page.rect
        results = []
        for band, rect in self._scan_candidates(gray, zoom, page_rect):
            # 印章保护：印章里的编号/日期也是页边的一小行墨迹，但它不是页码
            if self.config.protect_seals and \
                    self.seal_cover_for_rect(page, rect) >= self.config.seal_overlap_ratio:
                logger.info(f"第{page_index + 1}页: 页码候选与印章重叠，保留不动")
                continue
            results.append({
                "text": "页码(图像)",
                "rect": rect,
                "page_index": page_index,
                "type": "scanned",
                "region": band,
                "confidence": 0.8,
            })
            # 一页只会有一个页码，最靠近该边的那一小行就是它
            break
        return results

    def _scan_binarize(self, gray: np.ndarray, zoom: float) -> np.ndarray:
        """局部自适应二值化。全局 Otsu 在正文密集的页面上会把很淡的页码数字整条丢掉。"""
        block = max(11, int(24 * zoom)) | 1
        ad = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, block, 4)
        return ad

    def _scan_candidates(self, gray: np.ndarray, zoom: float,
                         page_rect: "pymupdf.Rect") -> List[Tuple[str, "pymupdf.Rect"]]:
        """把墨迹连通域按"同一行、字距不太远"拼成行块，返回边带里明显外伸的候选（最贴边的在前）"""
        img_h, img_w = gray.shape
        binary = self._scan_binarize(gray, zoom)
        count, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)

        max_glyph_w = self.SCAN_MAX_GLYPH_W_PT * zoom
        max_glyph_h = self.SCAN_MAX_GLYPH_H_PT * zoom
        glyphs = []
        for i in range(1, count):
            x, y, w, h, area = (stats[i, 0], stats[i, 1], stats[i, 2], stats[i, 3], stats[i, 4])
            if area < self.SCAN_MIN_GLYPH_AREA or h < 2:
                continue
            # 正文/表格线会被连成巨大的块，它们不可能是页码里的一个字符
            if w > max_glyph_w or h > max_glyph_h:
                continue
            glyphs.append([x, y, x + w, y + h])

        merge_gap = self.SCAN_LINE_MERGE_PT * zoom
        long_line = self.SCAN_BODY_LINE_PT * zoom
        # 全页行块：判断"贴不贴着正文"要看到正文，不能只看边带里那一点
        # 正文横躺的页（用户主动旋转过、或整页旋转着扫）先把字块转置成"字行横排"
        # 再判：页码在这种页里是竖着的一列字，按横排拼只会碎成一个个单字，
        # 边带也会因为"离顶更近"被判到正文那一侧去（2026-10-15 用户报的 10号 正是这样）
        sideways = self._is_sideways(glyphs, merge_gap, long_line)
        if sideways:
            glyphs = [[g[1], g[0], g[3], g[2]] for g in glyphs]
            img_w, img_h = img_h, img_w
        members = self._cluster_lines(glyphs, merge_gap)
        boxes = [self._box_of(glyphs, g) for g in members]
        owner = [0] * len(glyphs)
        for ci, group in enumerate(members):
            for gi in group:
                owner[gi] = ci
        # 候选行块只在边带里的字之间拼：整页拼法会让页码和旁边的表格字连成一块，
        # 涂白时就把表格内容一起啃掉了（旋转页的页码离正文只有十几个点）
        band_of = self._band_of_maker(img_w, img_h)
        peer_tol = self.SCAN_PEER_TOL_PT * zoom
        rule = (self.SCAN_RULE_THICK_PT * zoom, self.SCAN_RULE_ASPECT)
        long_line = self.SCAN_BODY_LINE_PT * zoom
        debris = self.SCAN_EDGE_DEBRIS_PT * zoom

        def at_edge(b):
            """贴着纸边这么近的东西是扫描仪吃进去的纸边阴影和脏点，不是印在字行里的东西"""
            return min(b[0], b[1], img_w - b[2], img_h - b[3]) < debris

        # 归一化之后正立和横躺是同一套坐标：正文永远铺到视图的左右两侧，
        # 页码永远只可能出现在上下边带
        cand_rule = (self.SCAN_RULE_CAND_THICK_PT * zoom, self.SCAN_RULE_ASPECT,
                     self.SCAN_RULE_CAND_MIN_LEN_PT * zoom)
        cand_groups = self._cluster_lines(
            glyphs, merge_gap, [i for i, g in enumerate(glyphs) if band_of(g)])
        cands = []
        for group in cand_groups:
            box = self._box_of(glyphs, group)
            band_dist = band_of(box)
            if not band_dist or self._is_rule(box, *cand_rule) or at_edge(box):
                continue
            if band_dist[0] in ("left", "right"):
                continue
            cands.append((band_dist[0], band_dist[1], box, boxes[owner[group[0]]]))
        # 边带里"离边同样远、又沿边排开一大片"的墨迹是正文的一列/一行，不是页码
        occupied = self._occupied_bands(cands, img_w, img_h, peer_tol)

        min_w = self.SCAN_MIN_LINE_W_PT * zoom
        max_w = img_w * self.SCAN_MAX_W_RATIO
        min_h = self.SCAN_MIN_LINE_PT * zoom
        max_h = self.SCAN_MAX_LINE_PT * zoom
        min_max = self.SCAN_MIN_LINE_MAX_PT * zoom
        # 扫描黑框、表格线这类"长条细墨迹"不是字，也不能拿来跟页码比谁更靠边；
        # 纸边上的灰尘点、脏印同理，既算不上"正文比页码更贴边"，也不该把正文块撑大
        ink = [b for b in boxes if not self._is_rule(b, *rule) and
               max(b[2] - b[0], b[3] - b[1]) >= min_max and not at_edge(b)]
        window = self.SCAN_PROTRUDE_WINDOW_PT * zoom
        need = self.SCAN_PROTRUDE_PT * zoom
        tail = self.SCAN_LINE_TAIL_PT * zoom
        pad = self.SCAN_OUTSIDE_BODY_PT * zoom
        body = self._body_block(ink, long_line)

        found = []
        for band, dist, box, run in cands:
            x0, y0, x1, y1 = box
            if max(x1 - x0, y1 - y0) < min_max:
                continue  # 比这还小的只是纸屑、标点
            if body and not self._outside_body(box, band, body, pad):
                continue  # 没有真正出到正文块之外，那是行首/行尾的字
            if any(lo - peer_tol <= dist <= hi + peer_tol for lo, hi in occupied.get(band, ())):
                continue
            w, h = x1 - x0, y1 - y0
            if w < min_w or w > max_w or h < min_h or h > max_h:
                continue
            # 这个边带小块在全页拼法里属于哪一条正文：整行/整列的其余部分都在里面
            if self._line_tail(box, run, band, tail):
                continue
            if self._protrusion(box, ink, band, window) < need:
                continue
            if sideways:
                # 映射回视图坐标：转置空间里的"上下边带"就是视图的左右边带
                x0, y0, x1, y1 = y0, x0, y1, x1
                band = {"top": "left", "bottom": "right"}.get(band, band)
            rect = pymupdf.Rect(page_rect.x0 + x0 / zoom, page_rect.y0 + y0 / zoom,
                                page_rect.x0 + x1 / zoom, page_rect.y0 + y1 / zoom)
            found.append((dist, band, rect))
        found.sort(key=lambda t: t[0])
        return [(band, rect) for _, band, rect in found]

    def _band_of_maker(self, img_w: int, img_h: int):
        """返回"这块墨在哪个页边带、离那条边多远"的判定函数（未命中返回 None）

        距离一律取"到最近那条页边的距离"，四个边带之间才可比。旋转着扫的页正文横躺，
        页码落在视图的左右边带里，若让底部边带靠绝对坐标赢就会漏掉真页码。
        """
        # 上下边带的深浅按页面长边量。横躺的页在这套坐标里短边成了"高"，
        # 按短边算 12% 只有 71 点，而这类页的页码实测离边 70 点，正好卡在带子外面
        ref = max(img_w, img_h)
        bottom_start = img_h - ref * self.config.bottom_margin_ratio
        top_end = ref * self.config.top_margin_ratio
        left_end = img_w * self.config.side_margin_ratio
        right_start = img_w * (1 - self.config.side_margin_ratio)
        check_side = self.config.check_side_numbers

        def band_of(box):
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            if cy >= bottom_start:
                return "bottom", img_h - box[3]
            if cy <= top_end:
                return "top", box[1]
            if check_side and cx <= left_end:
                return "left", box[0]
            if check_side and cx >= right_start:
                return "right", img_w - box[2]
            return None

        return band_of

    @staticmethod
    def _occupied_bands(cands, img_w: int, img_h: int,
                        tol: float) -> Dict[str, List[Tuple[float, float]]]:
        """每个边带里"正文的一列/一行"所占的离边距离区间

        正文每行的行首字正好排在一条竖线上，它们到左边的距离几乎相等；页码在这一列里
        是孤零零的一个。所以离边距离凑成一堆、又沿边铺满一大片的，整堆都是正文。
        """
        occupied = {}
        by_band = {}
        for band, dist, box, _run in cands:
            along = (box[1], box[3]) if band in ("left", "right") else (box[0], box[2])
            by_band.setdefault(band, []).append((dist, along))
        for band, items in by_band.items():
            length = img_h if band in ("left", "right") else img_w
            items.sort()
            groups, cur = [], [items[0]]
            for it in items[1:]:
                if it[0] - cur[-1][0] > tol:
                    groups.append(cur)
                    cur = [it]
                else:
                    cur.append(it)
            groups.append(cur)
            spans = [(g[0][0], g[-1][0]) for g in groups
                     if len(g) >= PageNumberDetector.SCAN_PEER_MIN and
                     max(a[1][1] for a in g) - min(a[1][0] for a in g) >=
                     length * PageNumberDetector.SCAN_PEER_SPAN_RATIO]
            if spans:
                occupied[band] = spans
        return occupied

    @staticmethod
    def _cluster_lines(glyphs: List[List[int]], merge_gap: float,
                       subset: List[int] = None) -> List[List[int]]:
        """把字块按"同一行、横向间距不太远"并起来，返回每组里的字块下标

        subset 只让参与拼组的字块限定在给定下标内（边带里的字），组与组的判定不变。
        """
        ids = list(range(len(glyphs))) if subset is None else sorted(subset)
        parent = {i: i for i in ids}

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        order = sorted(ids, key=lambda k: glyphs[k][1])
        for pos, i in enumerate(order):
            x0, y0, x1, y1 = glyphs[i]
            for j in order[pos + 1:pos + 20]:
                gx0, gy0, gx1, gy1 = glyphs[j]
                if gy0 >= y1:
                    break
                overlap = min(y1, gy1) - max(y0, gy0)
                if overlap <= 0.5 * min(y1 - y0, gy1 - gy0):
                    continue
                if max(gx0 - x1, x0 - gx1) < merge_gap:
                    root_i, root_j = find(i), find(j)
                    if root_i != root_j:
                        parent[root_j] = root_i

        clusters = {}
        for i in ids:
            clusters.setdefault(find(i), []).append(i)
        return list(clusters.values())

    @staticmethod
    def _box_of(glyphs: List[List[int]], group: List[int]) -> List[float]:
        return [min(glyphs[i][0] for i in group),
                min(glyphs[i][1] for i in group),
                max(glyphs[i][2] for i in group),
                max(glyphs[i][3] for i in group)]

    @classmethod
    def _is_sideways(cls, glyphs: List[List[int]], merge_gap: float,
                     long_line: float) -> bool:
        """正文是横躺着的（整页旋转着扫，或被用户主动旋转过）吗

        旋转过的表格里横着竖着都连成一大片，任何一条判据都会判反；判反以后边带换了边，
        正文里的一列字就会被当成页码涂白（2026-10-15 那份 48 页《廉政风险防控清单》
        第 15 页就是这样丢了"分管责任人"一列的几个字）。所以三条各投一票取多数：
          1 哪个方向连起来的字数多
          2 哪个方向拼出的正文行总长长
          3 汉字的部件正立时高比宽大，横躺过来就反过来
        三条互相独立：1、2 看拼法，3 只看单个字的形状，表格骗不了它。
        """
        if len(glyphs) < cls.SCAN_MIN_ORIENT_GLYPHS:
            return False
        trans = [[g[1], g[0], g[3], g[2]] for g in glyphs]
        row_groups = cls._cluster_lines(glyphs, merge_gap)
        col_groups = cls._cluster_lines(trans, merge_gap)
        votes = (
            sum(len(g) - 1 for g in col_groups) >=
            sum(len(g) - 1 for g in row_groups) * cls.SCAN_SIDEWAYS_RATIO,
            cls._body_line_length(col_groups, trans, long_line) >
            cls._body_line_length(row_groups, glyphs, long_line) * cls.SCAN_SIDEWAYS_LEN_RATIO,
            sum(1 for g in glyphs if g[3] - g[1] > 1.4 * (g[2] - g[0])) <
            sum(1 for g in glyphs if g[2] - g[0] > 1.4 * (g[3] - g[1])) * cls.SCAN_SIDEWAYS_ASPECT,
        )
        return sum(votes) >= 2

    @staticmethod
    def _body_line_length(members: List[List[int]], glyphs: List[List[int]],
                          long_line: float) -> int:
        """这个方向拼出的正文行总长（只算够得上一行字长度的，零碎的字不算）"""
        return sum(max(b[2] - b[0], b[3] - b[1])
                   for b in (PageNumberDetector._box_of(glyphs, g) for g in members)
                   if max(b[2] - b[0], b[3] - b[1]) >= long_line)

    @staticmethod
    def _is_rule(box, thick: float, aspect: float, min_len: float = 0.0) -> bool:
        """又扁又长的一条墨迹：扫描黑框、表格框线、下划线，不是字也不是页码

        min_len 用来把"一笔竖着的字"排除在外：页码"1"就是 1.9 宽 6.2 高的一竖，
        细长比和横线一样，只有"够长"才谈得上是一条线。
        """
        w, h = box[2] - box[0], box[3] - box[1]
        return min(w, h) <= thick and max(w, h) >= aspect * min(w, h) and max(w, h) >= min_len

    @staticmethod
    def _body_block(boxes, min_line: float):
        """正文块 = 所有"够长的一条墨迹"（横排的行、竖排的列）的外接框，没有则 None

        页码、标点和扫描黑框都短或细，进不了这个集合，所以正文块的边界就是行首字/行尾字
        最外沿的位置——真页码必须在这条边界之外，行头行尾那几个字则正好压在边界上。
        """
        lines = [b for b in boxes if b[2] - b[0] >= min_line or b[3] - b[1] >= min_line]
        if len(lines) < 2:
            return None
        return [min(b[0] for b in lines), min(b[1] for b in lines),
                max(b[2] for b in lines), max(b[3] for b in lines)]

    @staticmethod
    def _outside_body(cand, band: str, body, pad: float) -> bool:
        """候选朝页边方向是否真的超出了正文块（没超出就说明它只是正文的边缘字）"""
        return {"left": cand[0] <= body[0] - pad,
                "right": cand[2] >= body[2] + pad,
                "top": cand[1] <= body[1] - pad,
                "bottom": cand[3] >= body[3] + pad}[band]

    @staticmethod
    def _line_tail(cand, run, band: str, min_len: float) -> bool:
        """候选朝正文那一侧还连着长长一条墨迹（整行/整列的其余部分）：它是行头/行尾的字"""
        side = band in ("left", "right")
        a0, a1 = (0, 2) if side else (1, 3)          # 朝边方向的那根轴
        inward_is_less = band in ("right", "bottom")
        if inward_is_less:
            return run[a0] <= cand[a0] - min_len
        return run[a1] >= cand[a1] + min_len

    @staticmethod
    def _protrusion(cand, boxes, band: str, window: float) -> float:
        """候选朝页边方向还比周围墨迹块外伸多少；边带里空无一物时视为无限突出"""
        side = band in ("left", "right")
        along = (cand[1] - window, cand[3] + window) if side \
            else (cand[0] - window, cand[2] + window)
        outward = {"left": 0, "right": 2, "top": 1, "bottom": 3}[band]
        innermost = band in ("left", "top")
        best = None
        for box in boxes:
            if (box[0] < cand[2] and cand[0] < box[2] and
                    box[1] < cand[3] and cand[1] < box[3]):
                continue  # 和候选重叠的就是它自己那一行
            lo, hi = (box[1], box[3]) if side else (box[0], box[2])
            if hi < along[0] or lo > along[1]:
                continue
            v = box[outward]
            if best is None or (v < best if innermost else v > best):
                best = v
        if best is None:
            return float("inf")
        return {"left": best - cand[0], "right": cand[2] - best,
                "top": cand[1] - best, "bottom": cand[3] - best}[band]

    def seal_cover_for_rect(self, page: pymupdf.Page, rect: "pymupdf.Rect") -> float:
        """页码框内被印章覆盖的面积占比。只渲染页码附近的小块，不做整页彩色分析。"""
        mask, origin_x, origin_y, zoom = self.probe_seal_mask(page, [rect])
        probe = pymupdf.Rect(rect.x0 - origin_x, rect.y0 - origin_y,
                             rect.x1 - origin_x, rect.y1 - origin_y)
        return seal_cover_ratio(mask, probe, zoom)

    # 页码框向外放大这么多点做印章探测：足够罩住一枚常见印章，又远小于整页
    SEAL_PROBE_PAD_PT = 120.0

    def _seal_probe_rect(self, page: pymupdf.Page, rects: List["pymupdf.Rect"]) -> "pymupdf.Rect":
        """页码候选框的并集向外放大，得到需要渲染检查的小块范围"""
        page_rect = page.rect
        x0 = min(r.x0 for r in rects)
        y0 = min(r.y0 for r in rects)
        x1 = max(r.x1 for r in rects)
        y1 = max(r.y1 for r in rects)
        pad = self.SEAL_PROBE_PAD_PT
        clip = pymupdf.Rect(max(page_rect.x0, x0 - pad), max(page_rect.y0, y0 - pad),
                            min(page_rect.x1, x1 + pad), min(page_rect.y1, y1 + pad))
        return clip if clip.width > 1 and clip.height > 1 else page_rect

    def probe_seal_mask(self, page: pymupdf.Page, rects: List["pymupdf.Rect"]):
        """返回 (印章掩码, 掩码原点在PDF坐标的x, y, 渲染zoom)

        只渲染页码附近的小块：整页200DPI渲染+彩色分析要几十毫秒，
        而页码只会压在页边的小图案上，局部探测快约7倍且判断结果一致。
        """
        zoom = min(self.config.dpi, SEAL_DETECT_DPI) / 72
        mat = pymupdf.Matrix(zoom, zoom)
        clip = self._seal_probe_rect(page, rects)
        try:
            pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
        except Exception as e:
            logger.debug(f"局部印章探测失败({e})，改为整页探测")
            clip = page.rect
            pix = page.get_pixmap(matrix=mat, alpha=False)

        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)[:, :, :3]
        mask = detect_seal_regions(
            arr, self.config, rgb=True,
            page_area_px=page.rect.get_area() * zoom * zoom,
            page_min_side_px=min(page.rect.width, page.rect.height) * zoom * self.config.seal_min_size_ratio,
        )
        return mask, clip.x0, clip.y0, zoom

class PageNumberRemover:
    """页码去除器"""

    # 涂白掩码比检测到的页码框每边再放宽这么多点：见 _patch_ink_mask
    PATCH_MASK_LOOSE_PT = 3.0

    def __init__(self, config: PageNumberConfig = None):
        self.config = config or PageNumberConfig()
        self.detector = PageNumberDetector(self.config)

    def process_pdf(
        self,
        input_path: str,
        output_path: str,
        progress_callback=None,
        remove_mode: str = "auto"
    ) -> Dict[str, Any]:
        """
        处理单个PDF文件，去除页码

        Args:
            input_path: 输入PDF文件路径
            output_path: 输出PDF文件路径
            progress_callback: 进度回调函数 callback(page_index, total_pages, message)
            remove_mode: 去除模式
                - "auto": 自动检测（默认）
                - "text_only": 仅处理文本型PDF
                - "scanned_only": 仅处理扫描件PDF
                - "both": 同时处理两种类型

        Returns:
            处理结果统计
        """
        doc = pymupdf.open(input_path)
        total_pages = len(doc)
        stats = {
            "total_pages": total_pages,
            "removed_count": 0,
            "scanned_pages": 0,
            "text_pages": 0,
            "details": []
        }

        for page_index in range(total_pages):
            page = doc[page_index]

            if progress_callback:
                progress_callback(page_index, total_pages, f"正在处理第 {page_index + 1} 页...")

            # 判断页面类型
            is_scanned = self.detector.is_scanned_pdf(page)

            if is_scanned:
                stats["scanned_pages"] += 1
            else:
                stats["text_pages"] += 1

            # 根据模式决定处理方式
            page_numbers = []

            if remove_mode == "auto":
                if is_scanned:
                    page_numbers = self.detector.detect_scanned_page_numbers(page, page_index)
                else:
                    page_numbers = self.detector.detect_text_page_numbers(page, page_index)
            elif remove_mode == "text_only" and not is_scanned:
                page_numbers = self.detector.detect_text_page_numbers(page, page_index)
            elif remove_mode == "scanned_only" and is_scanned:
                page_numbers = self.detector.detect_scanned_page_numbers(page, page_index)
            elif remove_mode == "both":
                page_numbers = self.detector.detect_text_page_numbers(page, page_index)
                if is_scanned:
                    page_numbers.extend(
                        self.detector.detect_scanned_page_numbers(page, page_index)
                    )

            # 去除页码
            if page_numbers:
                if is_scanned:
                    # 扫描件：将页面转为图像，用图像修复方法去除页码
                    removed_here = self._remove_scanned_page_numbers_inplace(
                        doc, page, page_numbers, page_index)
                else:
                    # 文本型：使用涂黑/遮盖方式真正移除页码文本
                    self._remove_text_page_numbers_inplace(page, page_numbers)
                    removed_here = list(page_numbers)

                # 计数只能按真正动过的报：扫描件里被印章挡住的那几个没动，
                # 报成"去除 3 个"就混着没去的，页码还原样在页上
                stats["removed_count"] += len(removed_here)
                stats["details"].extend(removed_here)

        # 保存文档
        doc.save(output_path, garbage=4, deflate=True)
        doc.close()

        if progress_callback:
            progress_callback(total_pages, total_pages, "处理完成！")

        return stats

    def _remove_text_page_numbers_inplace(
        self,
        page: pymupdf.Page,
        page_numbers: List[Dict]
    ):
        """使用涂黑方式真正移除文本型PDF的页码"""
        seal_mask = None
        zoom = min(self.config.dpi, SEAL_DETECT_DPI) / 72
        origin_x = origin_y = 0.0
        if self.config.protect_seals and page_numbers:
            try:
                # 印章探测要渲染页面小块，必须用视图坐标（get_pixmap 的 clip 是显示方向）
                seal_mask, origin_x, origin_y, zoom = self.detector.probe_seal_mask(
                    page, [pn.get("view_rect", pn["rect"]) for pn in page_numbers])
            except Exception as e:
                logger.debug(f"印章保护渲染失败，按无印章处理: {e}")

        for pn in page_numbers:
            rect = pn["rect"]
            view_rect = pn.get("view_rect", rect)
            probe_rect = pymupdf.Rect(view_rect.x0 - origin_x, view_rect.y0 - origin_y,
                                      view_rect.x1 - origin_x, view_rect.y1 - origin_y)
            cover = seal_cover_ratio(seal_mask, probe_rect, zoom)
            if cover >= self.config.seal_overlap_ratio:
                # 铺白底会把压在该区域的印章一起涂白，这里只删文字对象
                page.add_redact_annot(rect, text="")
            else:
                # 添加涂黑标注，用白色填充覆盖页码区域
                page.add_redact_annot(
                    rect,
                    fill=(1, 1, 1),
                    text=""
                )
        # 应用所有涂黑操作，真正移除文本内容；
        # graphics=NONE 避免矢量印章（线条/圆环）被连带删除
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE
        )

    def erase_rect_inplace(self, doc: pymupdf.Document, page: pymupdf.Page,
                           rect: "pymupdf.Rect", page_index: int = 0,
                           pad_pt: float = 4.0) -> bool:
        """抹掉视图坐标里的一小块（去页码用的同一条流水线）

        给"去Logo"这类外部功能复用：注释避让、印章掩码、局部300dpi重绘、
        底层墨迹真删除、旋转页坐标换算这些坑都在这一个函数里踩过一遍了。
        pad_pt 是往外多取多少再重绘：重绘那一片是按 300dpi 重新采样过的，
        会比别处淡一点，所以紧贴着的邻居（页眉横线）越不该卷进来就把它调小。
        返回是否真的动了这一块。
        """
        return len(self._remove_scanned_page_numbers_inplace(
            doc, page, [{"rect": rect}], page_index, pad=pad_pt)) > 0

    def _remove_scanned_page_numbers_inplace(
        self,
        doc: pymupdf.Document,
        page: pymupdf.Page,
        page_numbers: List[Dict],
        page_index: int = 0,
        pad: float = 4.0
    ) -> List[Dict]:
        """就地抹掉扫描件页码：只重绘页码所在的那一小块。

        以前的做法是整页渲染→修复→JPEG→整页涂黑再贴回，每页要好几秒，
        还把已经压缩过的扫描图又压了一遍，越处理越糊。
        返回真正动过的那几个页码（位置上有注释的会保留，不算去掉）。
        """
        zoom = SCAN_PATCH_DPI / 72
        page_rect = page.rect
        rotation = page.rotation % 360
        # 注释和涂黑用的都是未旋转坐标，而检测到的页码框是视图（显示）坐标
        derot = page.derotation_matrix if rotation else None
        annot_rects = annot_protect_rects(doc, page)
        removed: List[Dict] = []
        for pn in page_numbers:
            rect = pymupdf.Rect(pn["rect"])
            if derot is not None:
                pdf_rect = pymupdf.Rect(rect * derot)
                pdf_rect.normalize()
            else:
                pdf_rect = rect
            if any(r.intersects(pdf_rect) for r in annot_rects):
                # 涂黑会把与框相交的注释一并删掉，电子签章往往就是以注释存在的
                logger.info(f"第{page_index + 1}页: 页码位置有注释，保留不动")
                continue
            clip = pymupdf.Rect(
                max(page_rect.x0, rect.x0 - pad), max(page_rect.y0, rect.y0 - pad),
                min(page_rect.x1, rect.x1 + pad), min(page_rect.y1, rect.y1 + pad))
            try:
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
            except Exception as e:
                logger.debug(f"第{page_index + 1}页: 局部渲染失败({e})")
                continue
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n)[:, :, :3].copy()

            mask = self._patch_ink_mask(arr, rect, clip, zoom, page_rect)
            if mask is None:
                continue
            fixed = cv2.inpaint(arr, mask, inpaintRadius=max(5, int(2 * zoom)),
                                flags=cv2.INPAINT_TELEA)

            # 涂黑只针对页码那一小块，把底层图像里的原始墨迹真正去掉（不只是盖住）
            page.add_redact_annot(pdf_rect, text="")
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)

            # insert_image 用的是未旋转的页面坐标系，而补丁是按视图裁出来的，
            # 所以要把补丁转回未旋转朝向；实测 90/180/270 度都是 rot90(rotation//90)
            if rotation:
                fixed = np.ascontiguousarray(np.rot90(fixed, rotation // 90))
                target = pymupdf.Rect(clip * page.derotation_matrix)
                target.normalize()
            else:
                target = clip
            self._insert_patch(page, target, fixed)
            removed.append(pn)
        return removed

    def _patch_ink_mask(self, arr: np.ndarray, rect: "pymupdf.Rect", clip: "pymupdf.Rect",
                        zoom: float, page_rect: "pymupdf.Rect"):
        """补丁内需要修复的像素掩码；整块都没得修时返回 None

        页码框本来就是从"页边空白里孤立的一小条墨迹"圈出来的，框内除了页码就是纸，
        所以整块都要重画：只圈二值化认定的墨迹会漏掉淡到只剩十几个灰度级的数字，
        一道破折号被抹掉、旁边的"12"还留在那儿。
        """
        h, w = arr.shape[:2]
        # 页码框是按 150dpi 的墨迹圈出来的，淡到只剩十几个灰度级的数字在 300dpi 下
        # 会超出这个框一两个点，照框修就留两团灰印子，所以掩码比框再松一点
        loose = int(self.PATCH_MASK_LOOSE_PT * zoom)
        x0 = max(0, int((rect.x0 - clip.x0) * zoom) - loose)
        y0 = max(0, int((rect.y0 - clip.y0) * zoom) - loose)
        x1 = min(w, int(np.ceil((rect.x1 - clip.x0) * zoom)) + loose)
        y1 = min(h, int(np.ceil((rect.y1 - clip.y0) * zoom)) + loose)
        if x1 <= x0 or y1 <= y0:
            return None
        mask = np.zeros((h, w), np.uint8)
        mask[y0:y1, x0:x1] = 255
        if self.config.protect_seals:
            seal = detect_seal_regions(
                arr, self.config, rgb=True,
                page_area_px=page_rect.get_area() * zoom * zoom,
                page_min_side_px=min(page_rect.width, page_rect.height) * zoom * self.config.seal_min_size_ratio)
            if seal is not None:
                mask[seal > 0] = 0
                if not np.any(mask):
                    return None
        return mask

    @staticmethod
    def _insert_patch(page: pymupdf.Page, target: "pymupdf.Rect", arr: np.ndarray):
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, format="PNG")
        page.insert_image(target, stream=buf.getvalue())


def batch_process(
    input_files: List[str],
    output_dir: str,
    config: PageNumberConfig = None,
    progress_callback=None,
    remove_mode: str = "auto"
) -> List[Dict]:
    """
    批量处理PDF文件

    Args:
        input_files: 输入PDF文件路径列表
        output_dir: 输出目录
        config: 页码检测配置
        progress_callback: 进度回调 callback(file_index, total_files, page_index, total_pages, message)
        remove_mode: 去除模式

    Returns:
        所有文件的处理结果统计列表
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    remover = PageNumberRemover(config)
    all_stats = []

    total_files = len(input_files)

    for file_index, input_path in enumerate(input_files):
        if not os.path.exists(input_path):
            logger.warning(f"文件不存在: {input_path}")
            continue

        # 生成输出文件名（保持原文件名）
        filename = os.path.basename(input_path)
        output_path = os.path.join(output_dir, filename)

        def page_progress(page_index, total_pages, message):
            if progress_callback:
                progress_callback(file_index, total_files, page_index, total_pages, message)

        try:
            stats = remover.process_pdf(
                input_path,
                output_path,
                progress_callback=page_progress,
                remove_mode=remove_mode
            )
            stats["input_file"] = input_path
            stats["output_file"] = output_path
            stats["status"] = "success"
            all_stats.append(stats)
            logger.info(f"处理完成: {filename} - 去除了 {stats['removed_count']} 个页码")

        except Exception as e:
            logger.error(f"处理失败: {filename} - {e}")
            all_stats.append({
                "input_file": input_path,
                "output_file": output_path,
                "status": "error",
                "error": str(e)
            })

    return all_stats