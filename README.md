# PDF Tool

[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-green)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)

批量处理公文体 PDF 的桌面小工具（Tkinter + PyMuPDF）。一次拖进几十本，勾上功能就整批跑完，
**电子签章（金格 GoldGrid 这类 /FT/Sig 表单控件）原样保留** —— 这是它存在的理由。

界面长这样：顶部一排功能勾选（鼠标停上去看详细说明），下面是各自的参数卡片，左边处理、右边实时预览。

## 能做什么

| 功能 | 说明 |
| --- | --- |
| 去页码 | 识别并涂掉页脚页码（`- 3 -`、`3/12` 这类），文本型和扫描件都能处理；红头页、印发说明页原样保留 |
| 改比例 | 三种模式：等比缩放（比例+偏移）、按边距对齐摆放（上/下/左右距离自动算比例）、非等比拉伸 |
| 旋转 | 自动识别扫描页的文字朝向并转正，也可手动指定方向和页面范围 |
| 改尺寸 | 统一到 A4/A5/A3 等目标纸张，内容按比例摆放 |
| 去扫描瑕疵 | 只处理扫描图**四边**的黑框、黑线、贴边脏块，不动正文 |
| 去除页眉Logo | 按跨页像素一致性认出角上的固定标识并抹掉；可连整行页眉（标识+公司全称+文件编号+横线）一起删 |

多选即组合处理，固定顺序：去瑕疵 → 去Logo → 旋转 → 改比例 → 改尺寸 → 去页码。

## 关于"保住签章"

金格的章在 PDF 里是一个 `/Subtype /Widget`、`/FT /Sig` 的表单控件，`page.annots()` 根本看不见它。
以前"改页面尺寸""按边距缩放"逐页重画时会把控件连同 `/AcroForm` 一起丢掉——文件打开着一切正常，
唯独章没了。现在：

- 缩放/改尺寸重建页面时，注释和表单控件都会搬过去，并重新登记进 `/AcroForm/Fields`；
- 涂黑、局部重绘前先取页面上所有注释与控件的矩形（`core.annot_protect_rects`），绕开它们；
- 每批处理前先数一遍签章，处理完逐个对账，掉了就当场弹窗点名，不让你交付时才发现。

同样会保住的还有：文档书签（目录）与文件属性（作者、创建时间）——重建文档时这些不会自己跟过来，
由 `scaler.carry_doc_extras()` 搬。

## 关于"替换源文件"

勾了它就直接把成品写回原文件路径，写法是**同目录临时文件 + 原子换上**，不会出现写了一半的文件。
写不进去（被 InDesign / CorelDRAW / WPS / Acrobat 占用，或文件带"只读"属性）会逐个点名并弹窗，
而不是悄悄"处理完成"。第一次覆盖前会把最初的原文件存进 `backup/`，之后同一批文件可以换参数反复重做，
不会在上次的结果上叠加。

## 安装与运行

拿代码：仓库页绿色 `Code` → `Download ZIP`，或者下某个版本标签的源码包
（例如 [v4.2.0](https://github.com/legercong/pdf-tool/archive/refs/tags/v4.2.0.zip)）；也可以用 git：

```bash
git clone https://github.com/legercong/pdf-tool.git
```

需要 Python 3.10+（带 Tkinter），依赖全部列在 `requirements.txt`：

```bash
pip install -r requirements.txt
python main.py
```

Windows 上想不开控制台窗口就用 `pythonw main.py`。拖拽进窗口需要 `tkinterdnd2`；没装会退回"添加文件"按钮。

界面选项存在 `%APPDATA%/PDF Tool/settings.json`，下次启动接着用（文件列表和页面范围故意不记住，避免误处理上一批）。

## 打包发布（目标机不装 Python）

不想折腾环境的，直接下 Release 里附带的 `PDF Tool 便携版 v4.2.0.zip`，解压双击 `PDF Tool.exe` 就能用
（Windows 10/11 64 位，第一次打开要在 SmartScreen 上点「更多信息 → 仍要运行」）。下面是怎么自己从源码打一份。

仓库带了 PyInstaller 配置，一条命令出免安装的文件夹版：

```bash
pip install pyinstaller
pyinstaller PDF-Tool.spec --noconfirm --distpath dist --workpath build
```

产物是 `dist/PDF Tool/`（约 190MB，压成 zip 约 80MB）：里面 `PDF Tool.exe` 加 `_internal/` 是完整的程序，
**分发必须整个文件夹一起给**，只拷 exe 打不开。目标机要求 Windows 10/11 64 位。
`使用说明.txt` 由 `packaging/使用说明.txt` 在构建末尾复制到 exe 同级，改说明改那个文件。

冻结后有两处路径跟着变，都在 `gui._app_home()` 里：`backup/` 落在 exe 同级而不是源码目录，
开始菜单/桌面快捷方式直接指向 exe（不再绕 `pythonw main.py`）。

`PDF-Tool.spec` 里排掉了两类东西，包体从 354MB 降到 191MB，功能不受影响：

- `pandas`/`numba`/`llvmlite` 及其连带（约 150MB）：`pymupdf.table` 的 `to_pandas()` 里是
  `try: import pandas`，本工具不做表格导出，用不到；
- OpenCV 的视频解码插件和人脸级联数据（约 70MB）：只用图像算子，不读视频。

打完先自检再发人：

```bash
pyinstaller _smoke.spec --noconfirm --distpath dist --workpath build
dist\smoke\smoke.exe "某个真件.pdf"
```

冒烟包和主包用同一套收集/排除配置，只是入口换成 `_frozen_smoke.py` 且留着控制台。
它会在冻结环境里真跑一遍去页码、改尺寸、内容缩放、去Logo，并检查页数不变、图片（含章）数量不变、
文字层还在、拖拽组件起得来。全绿才算打包成功。


## 回归检查

这个工具的失败方式很讨厌：不报错，只是成品"少了东西"（章没了、书签没了、少交了本）。
所以每次改动处理链路都要用真文件跑一遍差分检查。本机配了一组 `_probe_*.py` 探针，重点覆盖：

- 金格签章走过整条流水线仍在，且"旧行为"复现时必须报警；
- 四条重建路径保住书签与文件属性；
- 组合处理里"这一步没活干"的书照样交付；
- 界面排版在 920×1050 窗口里不裁切。

打包相关的两个是通用的，可以跟着改一起跑：`_verify_frozen_paths.py`（拦截 PowerShell，验冻结/源码两个分支的
快捷方式目标和 `backup/` 落点）、`_frozen_smoke.py`（配合 `_smoke.spec`，见上一节）。

这些探针里写死了本机样本路径和客户文件名，没有随仓库发布；要收进来得先把路径抽成配置。


## 隐私

仓库里只有代码：客户 PDF、`backup/` 原件快照、诊断输出和截图全部在 `.gitignore` 里，不会进版本库。

## 许可

Apache License 2.0（见 [LICENSE](LICENSE)）。可以自用、改、商用、再分发，但要保留版权声明和这份许可文本，
改动过的文件最好注明；工具本身不提供任何担保。**公文体处理涉及具体单位的签章流程，拿去用之前自己拿真件跑一遍验证。**
