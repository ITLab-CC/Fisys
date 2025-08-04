from brother_ql.raster import BrotherQLRaster
from brother_ql.conversion import convert
from brother_ql.backends.helpers import send
from PIL import Image, ImageDraw, ImageFont

# Drucker- und Band-Einstellungen
printer = '/dev/usb/lp0'  # Kernel-Device, da discover nur das findet
label = '62'  # 62mm Endlosband
img_width = 696  # 62mm * 11.3px/mm (Brother QL-800 Standard)

# Testbild erzeugen
img = Image.new('1', (img_width, 150), 1)  # 1=weiß
draw = ImageDraw.Draw(img)
font = ImageFont.load_default()
text = "Test"
bbox = draw.textbbox((0, 0), text, font=font)
text_width = bbox[2] - bbox[0]
text_height = bbox[3] - bbox[1]
draw.text(
    ((img_width - text_width) // 2, (150 - text_height) // 2),
    text, font=font, fill=0
)

# Druckdaten erzeugen
qlr = BrotherQLRaster('QL-800')
qlr.exception_on_warning = True
instructions = convert(
    qlr=qlr,
    images=[img],
    label=label,
    rotate='auto',
    threshold=70.0,
    dither=False,
    compress=True,
    red=True,
    dpi_600=False,
    hq=True,
    cut=True
)

# Drucken
send(
    instructions=instructions,
    printer_identifier=printer,
    backend_identifier='linux_kernel',
    blocking=True
)
print("✅")