#!/usr/bin/env python3
"""사이트 QR 코드 생성 → docs/icons/qr.png (가운데 앱 아이콘), docs/icons/qr_card.png (안내 문구 포함)"""
import glob
from pathlib import Path
import qrcode
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
URL = "https://gnlgus012-pixel.github.io/corp-cash-radar/"
qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=24, border=2)
qr.add_data(URL)
qr.make(fit=True)
img = qr.make_image(fill_color="#0f1b2d", back_color="white").convert("RGBA")
W = img.size[0]
icon = Image.open(ROOT / "docs/icons/icon-512.png").convert("RGBA").resize((W // 4, W // 4))
pad = Image.new("RGBA", (icon.size[0] + 28, icon.size[1] + 28), "white")
pad.paste(icon, (14, 14), icon)
img.paste(pad, ((W - pad.size[0]) // 2, (W - pad.size[1]) // 2), pad)
img.convert("RGB").save(ROOT / "docs/icons/qr.png")

fonts = sorted(glob.glob("/usr/share/fonts/**/NotoSansCJK*Bold*.tt[fc]", recursive=True)) + \
        sorted(glob.glob("/usr/share/fonts/**/NotoSansCJK*.tt[fc]", recursive=True))
def F(size):
    return ImageFont.truetype(fonts[0], size) if fonts else ImageFont.load_default()
card = Image.new("RGB", (W + 160, W + 420), "white")
d = ImageDraw.Draw(card)
d.rectangle([0, 0, card.size[0], 150], fill="#0f1b2d")
t = "법인WM AI레이더"
d.text(((card.size[0] - d.textlength(t, font=F(64))) / 2, 38), t, font=F(64), fill="#5eead4")
card.paste(img.convert("RGB"), (80, 190))
for i, line in enumerate(["휴대폰 카메라로 찍어 접속하세요", "비밀번호 입력 후 📱 버튼으로 앱 설치"]):
    f = F(40 if i == 0 else 32)
    d.text(((card.size[0] - d.textlength(line, font=f)) / 2, W + 230 + i * 70), line, font=f, fill="#0f1b2d" if i == 0 else "#4a5568")
card.save(ROOT / "docs/icons/qr_card.png")
print("ok", W, fonts[:1])
