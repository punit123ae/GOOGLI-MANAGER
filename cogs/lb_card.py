"""Draws the Kashmir Titans leaderboard card on cogs/lb_card_template.png (used by +lb)."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "lb_card_template.png"
FONT_PATHS = [HERE / "Agrandir-TextBold.otf", HERE / "Bangers-Regular.ttf"]

ICE = (232, 243, 255)
GOLD = (255, 194, 26)

# ---- coordinates on the 1024 x 1535 template ----
CAT_X, CAT_Y, CAT_W = 208, 310, 150      # text after "CATEGORY:"
STAT_X, STAT_Y, STAT_W = 750, 310, 205   # text after "STAT:"

GOLD_RING = (315, 425, 72)               # centre x, centre y, radius
GOLD_RANK_X = 85
GOLD_NAME_X = 425
GOLD_STAT_RIGHT = 955

ROWS = 11                                # rows under the gold box
ROW_Y0, ROW_STEP = 608, 56.2             # centre y of first row, spacing
ROW_AV_X, ROW_AV_R = 237, 20
RANK_CX = (28 + 165) // 2
NAME_X = 285
STAT_BOX = (838, 992)

YOU_Y = 1330
YOU_RANK_CX = (40 + 165) // 2
YOU_STAT_BOX = (835, 985)
YOU_AV_R = 22

PAGE_RIGHT, PAGE_Y = 985, 262            # small "PAGE 1/3" text under the title


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for p in FONT_PATHS:
        try:
            return ImageFont.truetype(str(p), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit(draw: ImageDraw.ImageDraw, text, size: int, max_w: int, min_size: int = 15):
    """Largest font (size down to min_size) where the text fits max_w; ellipsis as last resort."""
    text = str(text)
    for s in range(size, min_size - 1, -1):
        f = _font(s)
        if draw.textlength(text, font=f) <= max_w:
            return text, f
    f = _font(min_size)
    while text and draw.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return text + "…", f


def _put(draw, text, size, x, cy, fill, max_w, anchor="l"):
    text, f = _fit(draw, text, size, max_w)
    draw.text((x, cy), text, font=f, fill=fill, anchor=anchor + "m")


def _avatar(img: Image.Image, data: bytes | None, cx: int, cy: int, r: int) -> None:
    if not data:
        return
    try:
        av = Image.open(io.BytesIO(data)).convert("RGB").resize((2 * r, 2 * r), Image.LANCZOS)
    except Exception:
        return
    big = 8 * r
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    img.paste(av, (cx - r, cy - r), mask.resize((2 * r, 2 * r), Image.LANCZOS))


def render_card(category: str, stat_label: str, top: dict | None, rows: list[dict],
                you: dict | None, page_text: str = "") -> bytes:
    """
    top  : {"rank", "name", "value", "avatar"} for the gold box (overall #1) or None
    rows : up to 11 dicts like top, each with its real rank
    you  : {"rank", "name", "value", "avatar"} for the person who typed +lb, or None
    avatar is PNG/JPEG bytes or None. Returns PNG bytes.
    """
    img = Image.open(TEMPLATE).convert("RGB")
    d = ImageDraw.Draw(img)

    _put(d, category, 27, CAT_X, CAT_Y, ICE, CAT_W)
    _put(d, stat_label.split(" (")[0], 27, STAT_X, STAT_Y, ICE, STAT_W)
    if page_text:
        _put(d, page_text, 22, PAGE_RIGHT, PAGE_Y, (159, 208, 255), 200, "r")

    if top:
        cx, cy, r = GOLD_RING
        _avatar(img, top.get("avatar"), cx, cy, r - 4)
        _put(d, top["rank"], 54, GOLD_RANK_X, cy, GOLD, 70, "m")
        _put(d, top["name"], 44, GOLD_NAME_X, cy, ICE, GOLD_STAT_RIGHT - 200 - GOLD_NAME_X)
        _put(d, top["value"], 44, GOLD_STAT_RIGHT, cy, GOLD, 260, "r")

    for i, e in enumerate(rows[:ROWS]):
        cy = int(round(ROW_Y0 + i * ROW_STEP))
        _put(d, e["rank"], 30, RANK_CX, cy, ICE, 120, "m")
        _avatar(img, e.get("avatar"), ROW_AV_X, cy, ROW_AV_R)
        _put(d, e["name"], 29, NAME_X, cy, ICE, STAT_BOX[0] - 20 - NAME_X)
        _put(d, e["value"], 29, (STAT_BOX[0] + STAT_BOX[1]) // 2, cy, ICE, STAT_BOX[1] - STAT_BOX[0] - 14, "m")

    if you:
        _put(d, you.get("rank", "-"), 30, YOU_RANK_CX, YOU_Y, GOLD, 115, "m")
        _avatar(img, you.get("avatar"), ROW_AV_X, YOU_Y, YOU_AV_R)
        _put(d, you["name"], 29, NAME_X, YOU_Y, ICE, YOU_STAT_BOX[0] - 20 - NAME_X)
        _put(d, you["value"], 29, (YOU_STAT_BOX[0] + YOU_STAT_BOX[1]) // 2, YOU_Y, GOLD,
             YOU_STAT_BOX[1] - YOU_STAT_BOX[0] - 14, "m")

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
