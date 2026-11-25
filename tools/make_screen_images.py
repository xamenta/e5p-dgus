"""Draws the new/changed DGUS-reloaded page images for e5p-dgus v1.2.

Input:  a DWIN_SET folder built from DGUS-reloaded-Klipper (make_dwin_set.bat)
Output: screen_assets/*.bmp (24-bit, same format as DGUS Tools' input)

Needs Pillow and the Open Sans font. Only needed to regenerate the images;
build_dwin_set.py uses the committed BMPs. Part of e5p-dgus. GPLv3.
"""
import os
import struct
import sys

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/google-fonts/ofl/opensans/OpenSans[wdth,wght].ttf"
TEXT = (170, 170, 170)
TITLE = (190, 190, 190)
BORDER = (21, 21, 21)
BTN_BORDER = (18, 18, 18)
BTN_FILL, BTN_FILL_SEL = (46, 46, 46), (52, 52, 52)
POPUP_FILL = (24, 24, 24)
HEADER_H = 33


def font(size, weight=None):
    f = ImageFont.truetype(FONT, size)
    if weight:
        try:
            f.set_variation_by_axes([100, weight])
        except Exception:
            pass
    return f


def load(src, name):
    return Image.open(os.path.join(src, name)).convert("RGB")


def save_bmp(img, path):
    """24-bit bottom-up BMP with the same 2-byte tail padding DGUS uses."""
    w, h = img.size
    row_pad = (4 - (w * 3) % 4) % 4
    px = img.load()
    rows = []
    for y in range(h - 1, -1, -1):
        row = bytearray()
        for x in range(w):
            r, g, b = px[x, y]
            row += bytes((b, g, r))
        rows.append(bytes(row) + b"\x00" * row_pad)
    data = b"".join(rows) + b"\x00\x00"
    header = struct.pack("<2sIHHI", b"BM", 54 + len(data), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(data) - 2, 2834, 2834, 0, 0)
    with open(path, "wb") as f:
        f.write(header + info + data)


def centered(draw, box, text, fnt, fill):
    x1, y1, x2, y2 = box
    l, t, r, b = draw.textbbox((0, 0), text, font=fnt)
    draw.text(((x1 + x2 + 1 - (r - l)) / 2 - l, (y1 + y2 + 1 - (b - t)) / 2 - t), text, font=fnt, fill=fill)


def button(draw, box, label, fill=BTN_FILL, text=TEXT, size=15):
    draw.rectangle(box, fill=fill, outline=BTN_BORDER)
    centered(draw, box, label, font(size), text)


def tile_background(img, strip_box, target_box):
    """Fill target_box by repeating a clean background patch in both
    directions (keeps the page's noise texture)."""
    strip = img.crop(strip_box)
    sw, sh = strip.size
    x1, y1, x2, y2 = target_box
    for y in range(y1, y2, sh):
        for x in range(x1, x2, sw):
            img.paste(strip.crop((0, 0, min(sw, x2 - x), min(sh, y2 - y))), (x, y))


def fill_gradient(img, box, tl, tr, bl, br):
    """Bilinear grey gradient (tile interiors are lit from the top left)."""
    x1, y1, x2, y2 = box
    px = img.load()
    for y in range(y1, y2 + 1):
        fy = (y - y1) / max(1, y2 - y1)
        for x in range(x1, x2 + 1):
            fx = (x - x1) / max(1, x2 - x1)
            v = round((tl * (1 - fx) + tr * fx) * (1 - fy) + (bl * (1 - fx) + br * fx) * fy)
            px[x, y] = (v, v, v)


def stop_button(img, box=(404, 5, 473, 28)):
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(box, radius=4, fill=(178, 34, 34), outline=(110, 18, 18))
    centered(d, box, "STOP", font(14, 700), (255, 255, 255))


def header(img, title):
    """Replace the header title (keeps the back arrow at 0..39)."""
    d = ImageDraw.Draw(img)
    tile_background(img, (300, 0, 340, HEADER_H), (40, 0, 400, HEADER_H))
    d.text((41, 6), title, font=font(16), fill=TITLE)


def printer_icon(d, cx, cy, color=TEXT, w=3):
    d.rectangle((cx - 15, cy - 26, cx + 15, cy - 10), outline=color, width=w)          # paper in
    d.rounded_rectangle((cx - 30, cy - 10, cx + 30, cy + 16), radius=4, outline=color, width=w)  # body
    d.rectangle((cx - 15, cy + 4, cx + 15, cy + 28), fill=(34, 34, 34))
    d.rectangle((cx - 15, cy + 4, cx + 15, cy + 28), outline=color, width=w)           # paper out
    d.ellipse((cx + 19, cy - 3, cx + 23, cy + 1), fill=color)


def chevron(d, cx, cy, up, color=TEXT, w=3, size=12):
    s = -1 if up else 1
    d.line((cx - size, cy - s * size / 2, cx, cy + s * size / 2, cx + size, cy - s * size / 2),
           fill=color, width=w, joint="curve")


# ---------------------------------------------------------------------------
def make_home(src):
    img = load(src, "001_home.bmp")
    # Old tiles: 108x108 incl. 1px border at x 51/186/321, y 76..183
    tiles = [img.crop((x, 76, x + 108, 184)) for x in (51, 186, 321)]
    px = tiles[1].load()
    corners = [px[1, 1][0], px[106, 1][0], px[1, 106][0], px[106, 106][0]]
    tile_background(img, (2, 60, 42, 100), (0, 60, 480, 200))
    # blank tile for "Print": the Move tile's frame with a fresh interior
    blank = tiles[1].copy()
    fill_gradient(blank, (1, 1, 106, 106), *corners)
    d = ImageDraw.Draw(blank)
    printer_icon(d, 54, 46)
    centered(d, (1, 82, 106, 100), "Print", font(15), TEXT)
    for x, t in zip((12, 128, 244, 360), tiles + [blank]):
        img.paste(t, (x, 76))
    stop_button(img)
    return img


def make_presets(src, name, fill):
    img = load(src, name)
    d = ImageDraw.Draw(img)
    title = img.crop((160, 100, 320, 140))
    xmark = img.crop((313, 99, 351, 136))
    box = (95, 99, 384, 202)
    d.rectangle(box, fill=POPUP_FILL, outline=BORDER)
    img.paste(title, (160, 100))
    img.paste(xmark, (347, 99))
    for x, label in zip((110, 178, 246, 314), ("PLA", "ABS", "PETG", "TPU")):
        button(d, (x, 158, x + 55, 187), label, fill=fill)
    return img


def make_kill(src):
    img = load(src, "250_kill.bmp")
    d = ImageDraw.Draw(img)
    button(d, (175, 220, 304, 252), "Restart", size=16)
    return img


def make_stop_page(src, name):
    img = load(src, name)
    stop_button(img)
    return img


def blank_page(src, title):
    img = load(src, "018_settings_menu2.bmp")
    tile_background(img, (2, 40, 42, 272), (0, HEADER_H + 1, 480, 272))
    header(img, title)
    return img


def make_files(src):
    img = blank_page(src, "Print")
    d = ImageDraw.Draw(img)
    for i in range(5):
        y = 42 + i * 45
        d.rectangle((8, y, 404, y + 40), fill=(36, 36, 36), outline=BORDER)
    for box, up in (((412, 42, 472, 138), True), ((412, 166, 472, 262), False)):
        d.rectangle(box, fill=(36, 36, 36), outline=BORDER)
        chevron(d, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, up)
    return img


def make_confirm(src):
    img = blank_page(src, "Start print")
    d = ImageDraw.Draw(img)
    d.rectangle((14, 44, 466, 108), fill=(36, 36, 36), outline=BORDER)
    button(d, (122, 216, 232, 252), "Cancel", fill=(36, 36, 36), size=16)
    button(d, (248, 216, 358, 252), "Print", fill=(36, 36, 36), text=(220, 220, 220), size=16)
    return img


def main(src, out):
    os.makedirs(out, exist_ok=True)
    jobs = {
        "001_home.bmp": make_home(src),
        "002_print_status.bmp": make_stop_page(src, "002_print_status.bmp"),
        "003_print_adjust.bmp": make_stop_page(src, "003_print_adjust.bmp"),
        "008_move.bmp": make_stop_page(src, "008_move.bmp"),
        "021_files.bmp": make_files(src),
        "022_print_confirm.bmp": make_confirm(src),
        "210_popup_presets.bmp": make_presets(src, "210_popup_presets.bmp", BTN_FILL),
        "211_popup_presets_sel.bmp": make_presets(src, "211_popup_presets_sel.bmp", BTN_FILL_SEL),
        "250_kill.bmp": make_kill(src),
    }
    for name, img in jobs.items():
        save_bmp(img, os.path.join(out, name))
        print("wrote", name)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "screen_assets")
