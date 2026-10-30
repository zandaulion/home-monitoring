"""Render the app's simple code-native house mark at install-icon sizes."""
from pathlib import Path
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1] / "home_monitor/static/icons"
root.mkdir(exist_ok=True)
scale = 3
image = Image.new("RGB", (512*scale, 512*scale), "#1c3029")
draw = ImageDraw.Draw(image)
points = [(136, 248), (256, 140), (376, 248), (376, 380), (136, 380), (136, 248)]
draw.line([(x*scale, y*scale) for x, y in points], fill="#f2f5e9", width=25*scale, joint="curve")
draw.ellipse((230*scale, 240*scale, 282*scale, 292*scale), fill="#bbcfaa")
draw.rounded_rectangle((215*scale, 297*scale, 297*scale, 369*scale), radius=41*scale, fill="#bbcfaa")
draw.rectangle((210*scale, 352*scale, 302*scale, 375*scale), fill="#1c3029")
for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("icon-maskable.png", 512), ("apple-touch-icon.png", 180)):
    image.resize((size, size), Image.Resampling.LANCZOS).save(root / name)
