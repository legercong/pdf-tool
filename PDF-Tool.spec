# -*- mode: python ; coding: utf-8 -*-
"""
PDF Tool 打包配置（PyInstaller 6，onedir 文件夹版）

    pyinstaller PDF-Tool.spec --noconfirm

产物在 dist/PDF Tool/，整个文件夹拷到目标电脑解压即用，不需要 Python。
"""

import os
import shutil

from PyInstaller.utils.hooks import collect_dynamic_libs

ICON = os.path.join('pdf_page_remover', 'icon.ico')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=collect_dynamic_libs('pymupdf'),
    datas=[
        # 界面和托盘运行时要读图标文件，位置跟源码里的 dirname(__file__) 一致
        (ICON, 'pdf_page_remover'),
        (os.path.join('pdf_page_remover', 'icon.png'), 'pdf_page_remover'),
    ],
    hiddenimports=[
        'pystray._win32',
        'PIL._tkinter_finder',   # ImageTk 预览用
        'PIL.ImageDraw',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 本工具只做界面 + PDF + 图像处理，这些是被 PyMuPDF 的可选表格导出
        # (pymupdf.table 里 try: import pandas) 顺带拖进来的：
        # pandas 自己又拖 numba/llvmlite/pytz/lxml/sqlalchemy/win32com，
        # 合计约 150MB，去掉后包体小一半，界面与处理功能均不受影响。
        'pandas', 'numba', 'llvmlite', 'pytz', 'tzdata', 'sqlalchemy', 'lxml',
        'yaml', 'zstandard', 'tqdm', 'fsspec', 'cryptography', 'cffi',
        'requests', 'urllib3', 'certifi', 'idna',
        'win32com', 'win32api', 'win32gui', 'win32clipboard', 'Pythonwin',
        'matplotlib', 'scipy', 'IPython', 'pytest', 'setuptools', 'pip',
    ],
    noarchive=False,
)


def _drop(entries, name_parts, dest_parts=()):
    """按文件名或目标目录剔除条目：目标机上不播视频，用不到这些"""
    kept = []
    for entry in entries:
        src, dest = str(entry[0]).lower(), str(entry[1]).lower()
        if any(p in os.path.basename(src) for p in name_parts):
            continue
        if any(p in dest.replace('\\', '/') for p in dest_parts):
            continue
        kept.append(entry)
    return kept


# opencv 的视频解码插件（约 59MB）和人脸级联数据（约 10MB）本工具用不到
a.binaries = _drop(a.binaries, ('opencv_videoio_ffmpeg', 'mupdf-devel'))
a.datas = _drop(a.datas, ('opencv_videoio_ffmpeg',), ('cv2/data', 'mupdf-devel'))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PDF Tool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 无黑窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='PDF Tool',
)

# 说明文件要摆在 exe 旁边才看得见；datas 里的东西一律落在 _internal 下，
# 所以收集完再复制一次。
_out_root = os.path.join(DISTPATH, 'PDF Tool')
os.makedirs(_out_root, exist_ok=True)
shutil.copyfile(os.path.join(SPECPATH, 'packaging', '使用说明.txt'),
                os.path.join(_out_root, '使用说明.txt'))
