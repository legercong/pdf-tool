"""
PDF页面自动旋转模块
检测横向页面并旋转为纵向，支持顺时针/逆时针旋转
"""

import pymupdf
import os
import logging
from typing import List, Dict, Any, Optional, Callable, Set

logger = logging.getLogger(__name__)


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
            # 范围格式：5-7
            range_parts = part.split("-", 1)
            try:
                start = int(range_parts[0].strip())
                end = int(range_parts[1].strip())
                # 转换为0-based索引，并限制在有效范围内
                for page in range(start, end + 1):
                    idx = page - 1
                    if 0 <= idx < total_pages:
                        result.add(idx)
            except (ValueError, IndexError):
                logger.warning(f"无法解析页面范围: {part}")
                continue
        else:
            # 单页格式：3
            try:
                page = int(part)
                idx = page - 1
                if 0 <= idx < total_pages:
                    result.add(idx)
            except ValueError:
                logger.warning(f"无法解析页码: {part}")
                continue

    return result


class PDFRotator:
    """PDF页面自动旋转器"""

    # 常见纸张尺寸（宽度, 高度），单位：点（1点=1/72英寸）
    # 纵向格式
    PAGE_SIZES = {
        "A4": (595.28, 841.89),
        "A3": (841.89, 1190.55),
        "A5": (419.53, 595.28),
        "Letter": (612, 792),
        "Legal": (612, 1008),
    }

    def __init__(self):
        pass

    @staticmethod
    def is_landscape(page: pymupdf.Page) -> bool:
        """
        判断页面是否为横向

        Args:
            page: PyMuPDF Page对象

        Returns:
            True表示横向，False表示纵向
        """
        rect = page.rect
        return rect.width > rect.height

    @staticmethod
    def get_page_orientation(page: pymupdf.Page) -> str:
        """
        获取页面方向

        Args:
            page: PyMuPDF Page对象

        Returns:
            "landscape" 或 "portrait"
        """
        if PDFRotator.is_landscape(page):
            return "landscape"
        return "portrait"

    def rotate_pdf(
        self,
        input_path: str,
        output_path: str,
        direction: str = "clockwise",
        page_indices: Optional[Set[int]] = None,
        auto_detect: bool = True,
        progress_callback: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        """
        旋转PDF页面

        Args:
            input_path: 输入PDF路径
            output_path: 输出PDF路径
            direction: 旋转方向 "clockwise"（顺时针90°）或 "counterclockwise"（逆时针90°）
            page_indices: 要旋转的页面索引集合（0-based），None表示自动检测横向页面
            auto_detect: True=仅旋转横向页面，False=旋转指定页面
            progress_callback: 进度回调 callback(page_index, total_pages, message)

        Returns:
            处理结果统计
        """
        doc = pymupdf.open(input_path)
        total_pages = len(doc)
        stats = {
            "total_pages": total_pages,
            "rotated_count": 0,
            "landscape_count": 0,
            "portrait_count": 0,
            "status": "success",
        }

        # 确定旋转角度：set_rotation 的正值是顺时针（实测 rot=90 时左上角内容转到右上角），
        # 所以顺时针用 +90，逆时针用 270
        if direction == "clockwise":
            rotation_angle = 90
        else:
            rotation_angle = 270
        dir_label = "顺时针" if rotation_angle == 90 else "逆时针"

        for page_index in range(total_pages):
            page = doc[page_index]

            if progress_callback:
                progress_callback(
                    page_index, total_pages,
                    f"正在检查第 {page_index + 1} 页..."
                )

            is_landscape = self.is_landscape(page)

            if is_landscape:
                stats["landscape_count"] += 1
            else:
                stats["portrait_count"] += 1

            # 判断是否需要旋转此页
            should_rotate = False

            if auto_detect:
                # 自动模式：仅旋转横向页面，使用用户选择的方向
                if is_landscape:
                    should_rotate = True
                    actual_angle = rotation_angle
            else:
                # 指定页面模式：旋转指定页面，使用用户选择的方向
                if page_indices is not None and page_index in page_indices:
                    should_rotate = True
                    actual_angle = rotation_angle

            if should_rotate:
                current_rotation = page.rotation
                new_rotation = (current_rotation + actual_angle) % 360
                page.set_rotation(new_rotation)
                stats["rotated_count"] += 1
                logger.info(f"第{page_index + 1}页已旋转（方向: {dir_label}, {current_rotation}°→{new_rotation}°）")

        # 保存文档
        doc.save(output_path, garbage=4, deflate=True)
        doc.close()

        if progress_callback:
            progress_callback(total_pages, total_pages, "旋转完成！")

        return stats


def batch_rotate(
    input_files: List[str],
    output_dir: str,
    direction: str = "clockwise",
    page_range: str = "",
    auto_detect: bool = True,
    progress_callback: Optional[Callable] = None,
) -> List[Dict]:
    """
    批量旋转PDF文件页面

    Args:
        input_files: 输入PDF文件路径列表
        output_dir: 输出目录
        direction: 旋转方向 "clockwise" 或 "counterclockwise"
        page_range: 页面范围字符串（如"3,5-7"），空字符串表示所有页面
        auto_detect: True=自动检测横向页面旋转，False=旋转指定页面
        progress_callback: 进度回调 callback(file_index, total_files, page_index, total_pages, message)

    Returns:
        所有文件的处理结果统计列表
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    rotator = PDFRotator()
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
        if not auto_detect and page_range:
            # 先打开文件获取总页数来解析页面范围
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
            stats = rotator.rotate_pdf(
                input_path, output_path,
                direction=direction,
                page_indices=page_indices,
                auto_detect=auto_detect,
                progress_callback=page_progress,
            )
            stats["input_file"] = input_path
            stats["output_file"] = output_path
            all_stats.append(stats)
            logger.info(f"旋转完成: {filename} - 旋转了 {stats['rotated_count']} 页")

        except Exception as e:
            logger.error(f"旋转失败: {filename} - {e}")
            all_stats.append({
                "input_file": input_path,
                "output_file": output_path,
                "status": "error",
                "error": str(e),
            })

    return all_stats