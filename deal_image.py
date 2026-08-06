"""
Deal-accepted branded image generator (P.A.G.A.L Escrow Bot).
Draws a dark card with a green "P.A.G.A.L" badge, big "ESCROW BOT" title,
and the buyer/seller usernames - matching the bot's branding template.

NOTE: fonts/Poppins-Bold.ttf must sit next to this file (bundled in repo)
so this works on any server, not just machines with Google Fonts installed.
"""
import io
import os
import random
from PIL import Image, ImageDraw, ImageFont

W, H = 640, 640

BG = (8, 14, 10)
DOLLAR_COLOR = (20, 42, 30)
GREEN = (34, 197, 94)
GREEN_DARK = (22, 163, 74)
WHITE = (245, 245, 245)

_HERE = os.path.dirname(os.path.abspath(__file__))
FONT_BOLD = os.path.join(_HERE, "Poppins-Bold.ttf")


def _fit_font(draw: ImageDraw.ImageDraw, text: str, start_size: int, max_width: int, min_size: int = 16) -> ImageFont.FreeTypeFont:
    """Lambi username ho to font size ghata do taaki image ke bahar na nikle."""
    size = start_size
    while size > min_size:
        font = ImageFont.truetype(FONT_BOLD, size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            return font
        size -= 2
    return ImageFont.truetype(FONT_BOLD, min_size)


def _draw_money_bag(size: int = 40) -> Image.Image:
    """Simple hand-drawn money-bag icon (no emoji font needed - fully portable)."""
    s = size * 4  # supersample for smoother edges, downscale later
    icon = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(icon)

    # bag body (rounded)
    body_top = int(s * 0.32)
    d.ellipse([int(s * 0.08), body_top, int(s * 0.92), s - 4], fill=GREEN)
    # tie/neck at top
    d.rectangle([int(s * 0.36), int(s * 0.14), int(s * 0.64), body_top + 6], fill=GREEN_DARK)
    d.ellipse([int(s * 0.30), int(s * 0.06), int(s * 0.70), int(s * 0.28)], fill=GREEN_DARK)
    # "$" on the bag
    dollar_font = ImageFont.truetype(FONT_BOLD, int(s * 0.34))
    bbox = d.textbbox((0, 0), "$", font=dollar_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(
        ((s - tw) // 2 - bbox[0], int(s * 0.55) - th // 2 - bbox[1]),
        "$",
        font=dollar_font,
        fill=(255, 255, 255),
    )

    return icon.resize((size, size), Image.LANCZOS)


def generate_deal_image(buyer_mention: str, seller_mention: str) -> io.BytesIO:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # subtle background $ texture
    random.seed(7)
    for _ in range(22):
        size = random.choice([30, 42, 54, 70])
        f = ImageFont.truetype(FONT_BOLD, size)
        txt_img = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
        td = ImageDraw.Draw(txt_img)
        td.text((size // 2, size // 2), "$", font=f, fill=DOLLAR_COLOR + (255,))
        angle = random.randint(-20, 20)
        rotated = txt_img.rotate(angle, expand=True)
        x = random.randint(-20, W - 20)
        y = random.randint(-20, H - 20)
        img.paste(rotated, (x, y), rotated)

    # badge "P.A.G.A.L"
    badge_text = "P.A.G.A.L"
    badge_font = ImageFont.truetype(FONT_BOLD, 30)
    bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 26, 14
    badge_w, badge_h = tw + pad_x * 2, th + pad_y * 2
    badge_x = (W - badge_w) // 2
    badge_y = 110
    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h], radius=6, fill=GREEN
    )
    draw.text((badge_x + pad_x - bbox[0], badge_y + pad_y - bbox[1]), badge_text, font=badge_font, fill=WHITE)

    # ESCROW BOT (big)
    big_font = ImageFont.truetype(FONT_BOLD, 70)
    txt = "ESCROW BOT"
    bbox2 = draw.textbbox((0, 0), txt, font=big_font)
    tw2 = bbox2[2] - bbox2[0]
    escrow_y = badge_y + badge_h + 30
    draw.text(((W - tw2) // 2 - bbox2[0], escrow_y), txt, font=big_font, fill=WHITE)
    bbox2_bottom = escrow_y + (bbox2[3] - bbox2[1])

    # Buyer / Seller rows
    label_font = ImageFont.truetype(FONT_BOLD, 26)
    money_bag = _draw_money_bag(38)

    rows = [("BUYER :", buyer_mention), ("SELLER :", seller_mention)]
    start_y = bbox2_bottom + 60
    row_gap = 66
    left_margin = 95

    for i, (label, value) in enumerate(rows):
        y = start_y + i * row_gap
        img.paste(money_bag, (34, y - 3), money_bag)
        draw.text((left_margin, y), label, font=label_font, fill=WHITE)
        lb = draw.textbbox((left_margin, y), label, font=label_font)
        vx = lb[2] + 14
        max_w = W - 20 - vx
        value_font = _fit_font(draw, value, 34, max_w)
        draw.text((vx, y - 4), value, font=value_font, fill=WHITE)

    buf = io.BytesIO()
    buf.name = "deal.png"
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
