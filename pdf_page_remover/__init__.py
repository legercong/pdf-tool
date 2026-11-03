"""
公文 PDF 批量处理工具
支持文本型PDF和扫描件PDF的页码检测与去除
支持PDF内容缩放、自动旋转、页面尺寸统一、扫描件四边去瑕疵、页眉标识去除
"""

from .core import PageNumberConfig, PageNumberDetector, PageNumberRemover, batch_process
from .scaler import PDFScaler, batch_scale, PAGE_SIZES, resize_pdf, batch_resize
from .rotator import PDFRotator, batch_rotate, parse_page_range
from .scan_cleaner import ScanCleanConfig, clean_scan_artifacts, batch_clean_scan
from .logo_remover import LogoConfig, batch_remove_logos

__version__ = "4.1.0"
__all__ = [
    'PageNumberConfig',
    'PageNumberDetector',
    'PageNumberRemover',
    'batch_process',
    'PDFScaler',
    'batch_scale',
    'PAGE_SIZES',
    'resize_pdf',
    'batch_resize',
    'PDFRotator',
    'batch_rotate',
    'parse_page_range',
    'ScanCleanConfig',
    'clean_scan_artifacts',
    'batch_clean_scan',
    'LogoConfig',
    'batch_remove_logos',
]
