"""运行时和打包共用的提醒图标，不需要读取外部图片。"""

from PIL import Image, ImageDraw


def reminder_icon(*, pending: bool = False, size: int = 256) -> Image.Image:
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = "#f59e0b" if pending else "#2563eb"
    draw.rounded_rectangle((4, 4, 252, 252), radius=58, fill=color)
    draw.rounded_rectangle((19, 19, 237, 237), radius=45, outline="#ffffff33", width=2)
    draw.arc((82, 59, 174, 157), 180, 360, fill="white", width=14)
    draw.line([(82, 108), (82, 157), (65, 181), (191, 181), (174, 157), (174, 108)], fill="white", width=14, joint="curve")
    draw.line((128, 52, 128, 65), fill="white", width=12)
    draw.arc((107, 178, 149, 215), 0, 180, fill="white", width=12)
    if pending:
        draw.ellipse((176, 38, 221, 83), fill="#ef4444", outline="white", width=5)
    return image.resize((size, size), Image.Resampling.LANCZOS)
