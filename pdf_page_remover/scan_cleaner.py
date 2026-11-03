"""
扫描件边缘瑕疵清理模块

扫描仪压盖进光、走纸脏污会在扫描页的四边留下一圈贴着纸边的黑框/黑线（通常只有
1~5 像素宽）。它本身不是内容，但"按边距缩放"是按渲染像素找墨迹外接框的，这圈黑框
会把外接框撑成整页，于是缩放对齐的是纸边而不是文字。

这里直接在页面内嵌的那张扫描图上把黑框涂白：只动最外圈判定为瑕疵的几条像素，
且跳过彩色像素（红印章、红头标题都是彩色的），页面几何、分辨率、注释都不受影响。
涂白要先解码整幅扫描图并重编码，很慢，所以每页先做一次廉价的预检（见 clean_page），
确认边缘带里真有暗瑕疵才走完整流程；纯文本型页面连整页渲染都不做。
"""

import io
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pymupdf

logger = logging.getLogger(__name__)

CM_TO_PT = 28.3465


class ScanCleanConfig:
    """边缘瑕疵的判定参数"""

    def __init__(
        self,
        band_ratio: float = 0.06,
        max_trim_cm: float = 0.6,
        trigger_frac: float = 0.05,
        line_frac: float = 0.02,
        gap_px: int = 2,
        ink_threshold: int = 200,
        color_protect: int = 100,
        paper_tail_gray: int = 248,
        paper_min_mean: int = 225,
        paper_max_ink: float = 0.20,
        min_image_coverage: float = 0.5,
        jpeg_quality: int = 90,
        gate_dpi: int = 110,
        aspect_tol: float = 0.12,
        precheck_min_frac: float = 0.0005,
        precheck_margin: int = 8,
    ):
        """
        Args:
            band_ratio: 从扫描图边往里最多检查其短边这个比例的宽度
            max_trim_cm: 单边最多涂白多少厘米，超过就不当作瑕疵（保护满版内容）
            trigger_frac: 某行/列暗像素占比达到此值才认为这里有一条黑线
            line_frac: 从黑线往里延伸时，占比低于此值就认为已经离开黑线
            gap_px: 允许黑线中间断续的像素数
            ink_threshold: 灰度低于此值算暗
            color_protect: 通道最大-最小差超过此值算彩色（印章/红头），一律保留。
                黑线的 JPEG 色度噪声在 110 以下，红印章像素中位数是 128，取 100 分开两者
            paper_tail_gray: 黑线内侧常带一段渐隐的灰影（实测灰度 209~237，仍低于纸面
                253），高于 ink_threshold 却肉眼可见。核心黑线定好后，凡平均灰度低于此值
                且中性的行/列都并进涂白范围，否则灰影会残留成一条淡黑边
            paper_min_mean: 页面中心区域纸面亮度（中位数）低于此值说明是满版图，不清理
                （实测白纸扫描正文页 253~255，印刷封面远低于此。注意设计满版页不一定低于
                阈值：客户的满版封面整页只有一张铺满的图，中位数 240、墨迹 0.188，正好从这两条
                线里穿过去，会被啃掉顶边 1.5mm）
            paper_max_ink: 全页中性墨迹占比超过此值说明不是白纸文档扫描，不清理
                （实测白纸扫描 0.019~0.082，印刷封面 0.188~0.325）
            min_image_coverage: 图像覆盖页面面积达到此比例才按扫描页处理
            jpeg_quality: 回写 JPEG 的质量
            gate_dpi: 整页渲染所用分辨率，白纸文档判定和瑕疵预检都用这张图
            aspect_tol: 图像长宽比允许和页面差多少仍算整页扫描图。黑框贴着的是
                扫描图的边而不是页面的边（按边距缩放会把整页缩小重摆，图像边离
                页面边一样远），所以只能按长宽比+面积认扫描图，不能按是否铺满页面
            precheck_min_frac: 预检阈值。先在整页渲染图上数扫描图摆放框四边检查带
                里的可疑像素占比，低于此值就认为这页没有瑕疵，直接跳过后面的
                解码/连通块/重编码（实测有瑕疵页 0.0007~0.031，纸边只有残点的页
                ≤0.0003，完全清理过的页 0.0000，取 0.0005）
            precheck_margin: 预检里"可疑"的判据：灰度比本页纸面中位数低这个值以上。
                不能用 ink_threshold——渲染图只有 gate_dpi 分辨率，高分辨率扫描图上的
                细黑线被摊薄成浅灰就抓不到了（见 paper_suspect_mask）
        """
        self.band_ratio = band_ratio
        self.max_trim_cm = max_trim_cm
        self.trigger_frac = trigger_frac
        self.line_frac = line_frac
        self.gap_px = gap_px
        self.ink_threshold = ink_threshold
        self.color_protect = color_protect
        self.paper_tail_gray = paper_tail_gray
        self.paper_min_mean = paper_min_mean
        self.paper_max_ink = paper_max_ink
        self.min_image_coverage = min_image_coverage
        self.jpeg_quality = jpeg_quality
        self.gate_dpi = gate_dpi
        self.aspect_tol = aspect_tol
        self.precheck_min_frac = precheck_min_frac
        self.precheck_margin = precheck_margin


DEFAULT_SCAN_CLEAN_CONFIG = ScanCleanConfig()


def _trim_along(frac: np.ndarray, band_n: int, max_trim_n: int,
                trigger: float, line: float, gap: int) -> int:
    """沿一条边找核心黑线的像素条数，返回 0 表示这条边没有黑线"""
    n = min(band_n, max_trim_n, len(frac))
    start = None
    for i in range(n):
        if frac[i] >= trigger:
            start = i
            break
    if start is None:
        return 0

    last = start
    for i in range(start, n):
        if frac[i] >= line:
            last = i
        elif i - last > gap:
            break
    trim = last + 1
    if trim >= n:
        # 暗色一路延续到检查上限之外，说明那是版面边框/满版设计而不是扫描黑线，整条放弃
        return 0
    return trim


def _extend_tail(gray_series: np.ndarray, sat_series: np.ndarray,
                 trim: int, band_n: int, max_trim_n: int, cfg: ScanCleanConfig) -> int:
    """
    把核心黑线内侧那段"高于 ink_threshold 却仍发灰"的渐隐阴影并入涂白范围。

    扫描黑线往往不是干脆的一条，而是外侧深、内侧一路渐隐到纸面（实测灰度 209→237）。
    这些行没进 ink 掩码，核心 trim 会在它们之前就停下，留下一条肉眼可见的淡黑边。
    """
    if trim <= 0:
        return trim
    n = min(band_n, max_trim_n, len(gray_series))
    i = trim
    while i < n and gray_series[i] < cfg.paper_tail_gray and sat_series[i] < cfg.color_protect:
        i += 1
    # 灰影一路伸到上限之外：可能是版面设计，保守只涂核心黑线
    return trim if i >= n else i


def detect_edge_trim(ink: np.ndarray, band_px: int, max_trim_px: int,
                     cfg: ScanCleanConfig, gray: Optional[np.ndarray] = None,
                     sat: Optional[np.ndarray] = None) -> Tuple[int, int, int, int]:
    """
    在墨迹掩码上找四边贴边的黑框。

    Args:
        ink: HxW 的布尔数组，True 表示中性暗像素
        band_px: 从边往里检查多少像素
        max_trim_px: 单边最多涂白多少像素
        gray, sat: 与 ink 同尺寸的灰度/彩色度；给了就把黑线内侧的渐隐灰影一起涂白

    Returns:
        (上, 下, 左, 右) 各边要涂白的像素条数
    """
    h, w = ink.shape

    def series(arr, edge):
        if edge == "top":
            return arr[:band_px, :].mean(axis=1)
        if edge == "bottom":
            return arr[-band_px:, :].mean(axis=1)[::-1]
        if edge == "left":
            return arr[:, :band_px].mean(axis=0)
        return arr[:, -band_px:].mean(axis=0)[::-1]

    trims = []
    for edge in ("top", "bottom", "left", "right"):
        t = _trim_along(series(ink, edge), band_px, max_trim_px,
                        cfg.trigger_frac, cfg.line_frac, cfg.gap_px)
        if t and gray is not None and sat is not None:
            t = _extend_tail(series(gray, edge), series(sat, edge), t,
                             band_px, max_trim_px, cfg)
        trims.append(t)
    return tuple(trims)


def is_paper_scan(gray: np.ndarray, ink: np.ndarray, cfg: ScanCleanConfig) -> bool:
    """
    判断这是不是一页"白纸黑字"的文档扫描，用来排除满版封面/图纸。

    纸面亮度用中位数而不是平均数：白纸文档哪怕排得很密，纸面仍是像素的多数
    （实测 8 页正文扫描中位数 253~255），而平均数会被墨迹拖下来——同一批页面
    平均数低到 224.4，用 225 做阈值会把最密的那页误判成满版图而完全不清理。
    """
    h, w = gray.shape
    core = gray[int(h * 0.2):int(h * 0.8) or 1, int(w * 0.2):int(w * 0.8) or 1]
    if core.size == 0:
        return False
    return float(np.median(core)) >= cfg.paper_min_mean and float(ink.mean()) <= cfg.paper_max_ink


def split_ink(rgb: np.ndarray, cfg: ScanCleanConfig):
    """返回 (灰度, 中性暗墨迹, 彩色度)；彩色像素（印章/红头）不计进墨迹"""
    rgb = rgb[:, :, :3]
    gray = rgb.mean(axis=2)
    sat = (rgb.max(axis=2).astype(np.int16) - rgb.min(axis=2).astype(np.int16))
    ink = (gray < cfg.ink_threshold) & (sat < cfg.color_protect)
    return gray, ink, sat


def border_blob_zone(ink: np.ndarray, band_px: int) -> np.ndarray:
    """
    找出"贴着纸边、且整块都落在边缘带内"的墨迹连通块。

    扫描瑕疵除了整条黑线，还有压在角上的一小段黑块，它占不到一行的 5%，
    按行占比找不到，只能靠连通块判定。从纸边一直伸进正文的线条（表格框、
    装订线）整块会被排除在带外，因此保留不动。

    Returns:
        与 ink 同尺寸的布尔数组，True 表示该像素属于贴边瑕疵块
    """
    import cv2

    h, w = ink.shape
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        ink.astype(np.uint8), connectivity=8)
    if count <= 1:
        return np.zeros((h, w), dtype=bool)

    keep = []
    for pid in range(1, count):
        x, y, bw, bh, area = stats[pid]
        if area <= 0:
            continue
        # 必须贴着图像四边之一
        if not (x == 0 or y == 0 or x + bw >= w or y + bh >= h):
            continue
        # 外接框的四个角都要落在边缘带里，否则说明这块伸进了正文
        deep = False
        for cx in (x, x + bw - 1):
            for cy in (y, y + bh - 1):
                if min(cx, w - 1 - cx, cy, h - 1 - cy) >= band_px:
                    deep = True
        if deep:
            continue
        keep.append(pid)

    if not keep:
        return np.zeros((h, w), dtype=bool)
    return np.isin(labels, keep)


def _write_image(doc: "pymupdf.Document", xref: int, data: bytes,
                 width: int, height: int) -> None:
    """
    原地换掉图像对象里的 JPEG 数据。

    不用 page.replace_image：它会新插一个图像对象，旧的那份仍被内容流引用，
    结果是同一页存了两张图，文件体积翻倍（实测 37MB 的扫描件清完变 65MB）。
    """
    doc.update_stream(xref, data, compress=False)
    doc.xref_set_key(xref, "Filter", "/DCTDecode")
    doc.xref_set_key(xref, "ColorSpace", "/DeviceRGB")
    doc.xref_set_key(xref, "BitsPerComponent", "8")
    doc.xref_set_key(xref, "Width", str(width))
    doc.xref_set_key(xref, "Height", str(height))
    # Decode / SMask 保持原样：尺寸没变，透明度掩码仍然对得上


def scan_artifact_zone(rgb: np.ndarray, px_per_cm: float,
                       cfg: ScanCleanConfig = DEFAULT_SCAN_CLEAN_CONFIG
                       ) -> Tuple[Optional[np.ndarray], Tuple[int, int, int, int]]:
    """
    在一张位图（原始扫描图或页面渲染图）上圈出贴边的扫描瑕疵。

    Args:
        rgb: HxWx3 的 uint8 位图
        px_per_cm: 该位图每厘米多少像素，用于换算 max_trim_cm
        cfg: 判定参数

    Returns:
        (瑕疵掩码, (上, 下, 左, 右) 黑线条数)。掩码为 None 表示这页不是
        白纸文档扫描（满版封面、图纸），不应该做任何清理。
    """
    gray, ink, sat = split_ink(rgb, cfg)
    if not is_paper_scan(gray, ink, cfg):
        return None, (0, 0, 0, 0)

    h, w = gray.shape
    band_px = max(1, int(cfg.band_ratio * min(h, w)))
    max_trim_px = max(1, min(int(cfg.max_trim_cm * px_per_cm), band_px))
    trims = detect_edge_trim(ink, band_px, max_trim_px, cfg, gray, sat)

    zone = np.zeros((h, w), dtype=bool)
    top, bottom, left, right = trims
    if top:
        zone[:top, :] = True
    if bottom:
        zone[h - bottom:, :] = True
    if left:
        zone[:, :left] = True
    if right:
        zone[:, w - right:] = True
    zone |= border_blob_zone(ink, band_px)
    # 彩色像素（印章、红头）即使在瑕疵带里也原样保留
    zone &= sat < cfg.color_protect
    return zone, trims


def _to_rgb_array(image_bytes: bytes) -> Optional[np.ndarray]:
    pix = pymupdf.Pixmap(image_bytes)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    if not pix.colorspace or pix.colorspace.n != 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)


def _encode(arr: np.ndarray, quality: int) -> bytes:
    """统一回写 JPEG：图像对象里的 Filter 是按 JPEG 改的，写成 PNG 会让整页渲染失败"""
    from PIL import Image

    buf = io.BytesIO()
    # 彩色像素（印章）用 4:4:4，避免 4:2:0 在红边上窜色
    Image.fromarray(arr).save(buf, format="JPEG", quality=quality, optimize=True,
                              subsampling=0)
    return buf.getvalue()


def render_page_rgb(page: "pymupdf.Page", cfg: ScanCleanConfig) -> Optional[np.ndarray]:
    """
    按 gate_dpi 把整页渲染成 HxWx3 的视图位图，失败返回 None。

    渲染视图（带 /Rotate）而不是未旋转的原始图，是因为一次渲染要同时服务两件事：
    判断整页是不是白纸文档，以及在扫描图摆放框的四边预检瑕疵。
    """
    zoom = cfg.gate_dpi / 72.0
    try:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8)
        return arr.reshape(pix.height, pix.width, pix.n)[:, :, :3]
    except Exception as e:
        logger.debug(f"第{page.number + 1}页整页渲染失败: {e}")
        return None


def rect_in_view(rect: "pymupdf.Rect", page: "pymupdf.Page",
                 shape) -> Tuple[int, int, int, int]:
    """把未旋转坐标的矩形换算成渲染视图上的像素框 (x0, y0, x1, y1)"""
    view = pymupdf.Rect(rect) * page.rotation_matrix
    view.normalize()
    view.intersect(page.rect)
    ppp = shape[0] / page.rect.height if page.rect.height else 0.0
    if ppp <= 0:
        return 0, 0, 0, 0
    x0 = max(0, int((view.x0 - page.rect.x0) * ppp))
    y0 = max(0, int((view.y0 - page.rect.y0) * ppp))
    x1 = min(shape[1], int(round((view.x1 - page.rect.x0) * ppp)))
    y1 = min(shape[0], int(round((view.y1 - page.rect.y0) * ppp)))
    return x0, y0, x1, y1


def band_artifact_frac(suspect: np.ndarray, rect: "pymupdf.Rect",
                       page: "pymupdf.Page", cfg: ScanCleanConfig) -> float:
    """
    数扫描图摆放框四边检查带里的可疑像素占比，作为"这页有没有瑕疵"的预检。

    黑框贴着的是扫描图自己的边，所以检查带要沿着摆放框而不是页面边框：页面被
    按边距缩放重摆过以后，图边离纸边还有一截，沿纸边量就什么都量不到。

    Args:
        suspect: 整页渲染图上的可疑像素掩码（见 paper_suspect_mask）
        rect: 扫描图的摆放框（未旋转坐标）
        page: 该页，用来把摆放框换算到渲染视图
        cfg: 判定参数，用 band_ratio 决定检查带宽度

    Returns:
        检查带内可疑像素占比，0 表示这条带干净
    """
    h, w = suspect.shape
    x0, y0, x1, y1 = rect_in_view(rect, page, suspect.shape)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return 0.0

    band = max(1, int(cfg.band_ratio * min(x1 - x0, y1 - y0)))
    m = np.zeros((h, w), dtype=bool)
    m[y0:y0 + band, x0:x1] = True
    m[max(0, y1 - band):y1, x0:x1] = True
    m[y0:y1, x0:x0 + band] = True
    m[y0:y1, max(0, x1 - band):x1] = True
    return float((suspect & m).sum()) / float(m.sum())


def paper_suspect_mask(gray: np.ndarray, sat: np.ndarray,
                       cfg: ScanCleanConfig) -> np.ndarray:
    """
    预检用的"可疑像素"掩码：比纸面暗 precheck_margin 以上、且不是彩色的像素。

    这里刻意不用 ink_threshold(灰度<200) 那条正式判据：渲染图只有 gate_dpi 分辨率，
    300dpi 扫描图上 5~6 像素的黑线降到 110dpi 以后会被抗锯齿摊薄成灰度 210~240，
    用 200 去卡就会漏掉真黑线（实测同一条 6px 黑线，灰度<200 的占比只有 0.00004，
    而纸边残点是 0.00000，根本没有区分度）。改成"比本页纸面中位数暗一截"以后，
    脏页 0.0007~0.031、干净页 ≤0.0003，而且黑线内侧那段渐隐的灰影也算可疑像素。
    判据放宽只会多跑几页，不会漏页，正式涂白仍按 ink_threshold 精确判定。
    """
    paper = float(np.median(gray))
    return (gray < paper - cfg.precheck_margin) & (sat < cfg.color_protect)


def _scan_candidates(page: "pymupdf.Page", cfg: ScanCleanConfig) -> List[Tuple[int, "pymupdf.Rect"]]:
    """
    找出这页里"可能是整页扫描图"的图像，返回 (xref, 摆放框)。

    只处理占页面过半、长宽比和页面一致的扫描图：文本型页面没有这种图像，
    拼版页的分块图像既不够大也不同比例。这一步不渲染、不解码，纯文本页
    在这里就返回空，一次整页渲染都不用做。
    """
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return []
    page_aspect = (max(page.rect.width, page.rect.height)
                   / min(page.rect.width, page.rect.height))
    out = []
    seen = set()
    for img_info in page.get_images(full=True):
        xref = img_info[0]
        if xref in seen:
            continue
        seen.add(xref)
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            continue
        if len(rects) != 1:
            continue
        rect = rects[0]
        if max(rect.width, 0) * max(rect.height, 0) / page_area < cfg.min_image_coverage:
            continue
        # 黑框贴着的是扫描图自己的边，而扫描图不一定铺满页面（被按边距缩放重摆过
        # 就会整体内缩）。用长宽比认整页扫描图：页内插图/拼版分块极少既是页面比例
        # 又占过半面积
        img_aspect = max(rect.width, rect.height) / min(rect.width, rect.height)
        if abs(img_aspect - page_aspect) > cfg.aspect_tol:
            continue
        out.append((xref, rect))
    return out


def clean_page(page: pymupdf.Page, cfg: ScanCleanConfig = DEFAULT_SCAN_CLEAN_CONFIG
               ) -> List[Dict[str, Any]]:
    """
    清理一页扫描瑕疵，返回每条被处理图像的记录（未处理返回空列表）。

    完整的涂白要对原始扫描图解码、做整幅连通块分析、再 JPEG 重编码，一页几百毫秒，
    整份文件跑下来很慢，而绝大多数页面本来就没有瑕疵。所以按成本从低到高逐层筛：
    先按面积和长宽比认出整页扫描图（纯文本页到此为止），再渲染一次整页，用它判
    白纸文档 + 预检四边检查带的可疑像素占比，两项都通过才真的动手改图。满版封面/
    图纸被白纸判定挡在外面。
    """
    doc = page.parent
    candidates = _scan_candidates(page, cfg)
    if not candidates:
        return []

    arr_page = render_page_rgb(page, cfg)
    if arr_page is None:
        return []
    gray_view, ink_view, sat_view = split_ink(arr_page, cfg)
    if not is_paper_scan(gray_view, ink_view, cfg):
        return []

    suspect = paper_suspect_mask(gray_view, sat_view, cfg)
    dirty = [c for c in candidates
             if band_artifact_frac(suspect, c[1], page, cfg) >= cfg.precheck_min_frac]
    if not dirty:
        return []

    records = []
    for xref, rect in dirty:
        try:
            info = doc.extract_image(xref)
            arr = _to_rgb_array(info["image"])
        except Exception as e:
            logger.debug(f"第{page.number + 1}页 xref{xref} 图像解码失败: {e}")
            continue
        if arr is None:
            continue

        cm_h = rect.height / CM_TO_PT
        cm_w = rect.width / CM_TO_PT
        if cm_h <= 0 or cm_w <= 0:
            continue
        # 取两个方向里较小的像素密度，保证涂白宽度不会超过 cfg.max_trim_cm
        px_per_cm = min(arr.shape[0] / cm_h, arr.shape[1] / cm_w)
        zone, trims = scan_artifact_zone(arr, px_per_cm, cfg)
        top, bottom, left, right = trims
        if zone is None or not zone.any():
            continue

        new_arr = arr.copy()
        new_arr[zone] = 255
        data = _encode(new_arr, cfg.jpeg_quality)
        try:
            _write_image(doc, xref, data, arr.shape[1], arr.shape[0])
        except Exception as e:
            logger.warning(f"第{page.number + 1}页 xref{xref} 回写失败: {e}")
            continue

        rec = {
            "page_index": page.number,
            "xref": xref,
            "trim_px": {"top": top, "bottom": bottom, "left": left, "right": right},
            "trim_cm": {
                "top": round(top / px_per_cm, 2), "bottom": round(bottom / px_per_cm, 2),
                "left": round(left / px_per_cm, 2), "right": round(right / px_per_cm, 2),
            },
            "blob_px": int(zone.sum()),
            "bytes_before": len(info["image"]),
            "bytes_after": len(data),
        }
        records.append(rec)
        logger.info(
            f"第{page.number + 1}页: 涂白边缘瑕疵 上{top}px 下{bottom}px "
            f"左{left}px 右{right}px 贴边脏块{int(zone.sum())}px")
    return records


def clean_scan_artifacts(
    input_path: str,
    output_path: str,
    page_range: str = "",
    cfg: ScanCleanConfig = DEFAULT_SCAN_CLEAN_CONFIG,
    progress_callback: Optional[Callable] = None,
) -> Dict[str, Any]:
    """
    清理一个 PDF 全部扫描页的四边黑框/黑线。

    Args:
        input_path: 输入PDF路径
        output_path: 输出PDF路径
        page_range: 页面范围字符串（如"3,5-7"），空字符串表示所有页面
        cfg: 判定参数
        progress_callback: 进度回调 callback(page_index, total_pages, message)

    Returns:
        处理统计，records 里是每页被涂白的边宽
    """
    from pdf_page_remover.rotator import parse_page_range

    doc = pymupdf.open(input_path)
    total_pages = len(doc)
    page_indices = (parse_page_range(page_range, total_pages) if page_range
                    else set(range(total_pages)))

    records: List[Dict[str, Any]] = []
    for page_index in range(total_pages):
        if progress_callback:
            progress_callback(page_index, total_pages,
                              f"正在清理第 {page_index + 1} 页边缘...")
        if page_index not in page_indices:
            continue
        try:
            records.extend(clean_page(doc[page_index], cfg))
        except Exception as e:
            logger.warning(f"第{page_index + 1}页清理失败: {e}")

    doc.save(output_path, garbage=4, deflate=True)
    doc.close()

    if progress_callback:
        progress_callback(total_pages, total_pages, "边缘清理完成！")

    cleaned_pages = len({r["page_index"] for r in records})
    return {
        "status": "success",
        "total_pages": total_pages,
        "cleaned_pages": cleaned_pages,
        "records": records,
    }


def batch_clean_scan(
    input_files: List[str],
    output_dir: str,
    page_range: str = "",
    cfg: ScanCleanConfig = DEFAULT_SCAN_CLEAN_CONFIG,
    progress_callback: Optional[Callable] = None,
) -> List[Dict]:
    """
    批量清理扫描件边缘瑕疵

    Args:
        input_files: 输入PDF文件路径列表
        output_dir: 输出目录
        page_range: 页面范围字符串（如"3,5-7"）
        cfg: 判定参数
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

        def page_progress(page_index, total_pages, message, fi=file_index):
            if progress_callback:
                progress_callback(fi, total_files, page_index, total_pages, message)

        try:
            stats = clean_scan_artifacts(
                input_path, output_path, page_range=page_range,
                cfg=cfg, progress_callback=page_progress,
            )
            stats["input_file"] = input_path
            stats["output_file"] = output_path
            all_stats.append(stats)
            logger.info(f"边缘清理完成: {filename} - 处理 {stats['cleaned_pages']} 页")
        except Exception as e:
            logger.error(f"边缘清理失败: {filename} - {e}")
            all_stats.append({
                "status": "error",
                "error": str(e),
                "input_file": input_path,
                "output_file": "",
                "total_pages": 0,
                "cleaned_pages": 0,
                "records": [],
            })

    return all_stats
