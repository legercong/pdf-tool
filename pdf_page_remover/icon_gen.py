"""
PDF Tool 图标生成器
生成美观的应用图标，用于窗口、任务栏、快捷方式
"""
import os
from PIL import Image, ImageDraw, ImageFont


def create_icon(size=256):
    """创建指定尺寸的PDF Tool图标
    
    设计理念：
    - 蓝色圆角矩形背景（匹配应用主题色）
    - 白色文档轮廓 + 折角
    - 红色PDF标识条
    - 白色"P"字母
    """
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # 尺寸比例
    margin = size * 0.08
    radius = size * 0.18
    
    # === 1. 蓝色圆角矩形背景 ===
    bg_rect = [margin, margin, size - margin, size - margin]
    draw.rounded_rectangle(bg_rect, radius=radius, fill=(41, 98, 210))
    
    # === 2. 白色文档形状 ===
    doc_margin_x = size * 0.22
    doc_margin_top = size * 0.18
    doc_margin_bottom = size * 0.28
    doc_width = size - 2 * doc_margin_x
    doc_height = size - doc_margin_top - doc_margin_bottom
    fold_size = size * 0.13
    
    doc_left = doc_margin_x
    doc_top = doc_margin_top
    doc_right = doc_left + doc_width
    doc_bottom = doc_top + doc_height
    
    # 文档主体（白色，略带圆角）
    doc_radius = size * 0.04
    draw.rounded_rectangle(
        [doc_left, doc_top, doc_right, doc_bottom],
        radius=doc_radius, fill=(255, 255, 255)
    )
    
    # 折角（右上角三角形 - 先画一个稍暗的三角形作为折角阴影）
    fold_points = [
        (doc_right - fold_size, doc_top),
        (doc_right, doc_top + fold_size),
        (doc_right - fold_size, doc_top + fold_size),
    ]
    draw.polygon(fold_points, fill=(220, 225, 235))
    
    # 折角翻面（更亮的三角形）
    fold_front = [
        (doc_right - fold_size, doc_top),
        (doc_right, doc_top + fold_size),
    ]
    draw.line(fold_front, fill=(190, 200, 215), width=max(1, int(size * 0.012)))
    
    # === 3. 文档上的横线（模拟文字行）===
    line_left = doc_left + doc_width * 0.15
    line_right = doc_right - doc_width * 0.15
    line_color = (200, 210, 225)
    line_h = max(1, int(size * 0.018))
    line_gap = size * 0.055
    lines_start_y = doc_top + fold_size + size * 0.06
    
    for i in range(4):
        y = lines_start_y + i * line_gap
        if y + line_h < doc_bottom - size * 0.08:
            # 最后一行短一点
            r = line_right if i < 3 else line_right - doc_width * 0.2
            draw.rounded_rectangle(
                [line_left, y, r, y + line_h],
                radius=line_h // 2, fill=line_color
            )
    
    # === 4. 红色PDF标识条 ===
    bar_height = size * 0.16
    bar_top = doc_bottom - bar_height - size * 0.04
    bar_left = doc_left + doc_width * 0.08
    bar_right = doc_right - doc_width * 0.08
    bar_radius = size * 0.03
    
    draw.rounded_rectangle(
        [bar_left, bar_top, bar_right, bar_top + bar_height],
        radius=bar_radius, fill=(220, 50, 47)
    )
    
    # === 5. "PDF" 文字 ===
    try:
        font_size = int(bar_height * 0.65)
        font = ImageFont.truetype("arial.ttf", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", int(bar_height * 0.65))
        except (OSError, IOError):
            font = ImageFont.load_default()
    
    text = "PDF"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_x = (bar_left + bar_right - text_w) / 2
    text_y = bar_top + (bar_height - text_h) / 2 - bbox[1]
    draw.text((text_x, text_y), text, fill=(255, 255, 255), font=font)
    
    return img


def generate_ico(output_path=None):
    """生成ICO图标文件（包含多尺寸）"""
    if output_path is None:
        output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.ico')
    
    # 先生成256x256的高质量图标
    base_icon = create_icon(256)
    
    # ICO需要包含多个尺寸，使用resize从大图缩放
    sizes = [16, 24, 32, 48, 64, 128, 256]
    icon_images = []
    for s in sizes:
        if s == 256:
            icon_images.append(base_icon)
        else:
            icon_images.append(base_icon.resize((s, s), Image.LANCZOS))
    
    # 保存为ICO
    base_icon.save(
        output_path,
        format='ICO',
        sizes=[(s, s) for s in sizes],
        append_images=icon_images[1:]
    )
    print(f"图标已保存到: {output_path} ({os.path.getsize(output_path)} bytes)")
    return output_path


def generate_png(output_path=None, size=256):
    """生成PNG图标文件（用于托盘等）"""
    if output_path is None:
        output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.png')
    
    icon = create_icon(size)
    icon.save(output_path, format='PNG')
    print(f"PNG图标已保存到: {output_path}")
    return output_path


if __name__ == '__main__':
    ico_path = generate_ico()
    png_path = generate_png()
    print("完成！")