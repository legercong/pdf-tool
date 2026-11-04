# -*- mode: python ; coding: utf-8 -*-
"""
临时冒烟包：与 PDF-Tool.spec 用同一套收集/排除配置，只是入口换成
_frozen_smoke.py 且保留控制台，用来在冻结环境里真跑一遍 PDF 处理。

    pyinstaller _smoke.spec --noconfirm --distpath dist --workpath build
"""

import os

from PyInstaller.utils.hooks import collect_dynamic_libs

a = Analysis(
    ['_frozen_smoke.py'],
    pathex=[],
    binaries=collect_dynamic_libs('pymupdf'),
    datas=[
        (os.path.join('pdf_page_remover', 'icon.ico'), 'pdf_page_remover'),
        (os.path.join('pdf_page_remover', 'icon.png'), 'pdf_page_remover'),
    ],
    hiddenimports=[
        'pystray._win32',
        'PIL._tkinter_finder',
        'PIL.ImageDraw',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'pandas', 'numba', 'llvmlite', 'pytz', 'tzdata', 'sqlalchemy', 'lxml',
        'yaml', 'zstandard', 'tqdm', 'fsspec', 'cryptography', 'cffi',
        'requests', 'urllib3', 'certifi', 'idna',
        'win32com', 'win32api', 'win32gui', 'win32clipboard', 'Pythonwin',
        'matplotlib', 'scipy', 'IPython', 'pytest', 'setuptools', 'pip',
    ],
    noarchive=False,
)


def _drop(entries, name_parts, dest_parts=()):
    kept = []
    for entry in entries:
        src, dest = str(entry[0]).lower(), str(entry[1]).lower()
        if any(p in os.path.basename(src) for p in name_parts):
            continue
        if any(p in dest.replace('\\', '/') for p in dest_parts):
            continue
        kept.append(entry)
    return kept


a.binaries = _drop(a.binaries, ('opencv_videoio_ffmpeg', 'mupdf-devel'))
a.datas = _drop(a.datas, ('opencv_videoio_ffmpeg',), ('cv2/data', 'mupdf-devel'))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='smoke',
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon=None,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='smoke')
