"""Draws the Kashmir Titans leaderboard card on cogs/lb_card_template.png (used by +lb)."""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "lb_card_template.png"
FONT_PATHS = [HERE / "Agrandir-TextBold.otf", HERE / "Bangers-Regular.ttf"]

ICE = (232, 243, 255)
GOLD = (255, 194, 26)

# ---- coordinates on the 1024 x 1535 template ----
CAT_X, CAT_Y, CAT_W = 275, 230, 390      # category text in the upper-left blue panel
STAT_X, STAT_Y, STAT_W = 870, 230, 220   # statistic text in the upper-right blue panel

GOLD_RING = (205, 465, 60)               # avatar ring centre and inner radius in the gold leader panel
GOLD_RANK_X = 95
GOLD_NAME_X = 320
GOLD_STAT_RIGHT = 950

ROWS = 9                                 # rows under the gold box (gold + 9 = 10 players per page)
# measured centre y of each avatar ring on the template (the rows are not evenly spaced)
ROW_CY = [663, 722, 781, 839, 899, 958, 1017, 1076, 1135]
ROW_AV_X, ROW_AV_R = 205, 26             # ring centre x, INNER radius
RANK_CX = (28 + 165) // 2
NAME_X = 295
STAT_BOX = (820, 965)

YOU_Y = 1294
YOU_AV_X = 205
YOU_NAME_X = 300
YOU_RANK_CX = (40 + 165) // 2
YOU_STAT_BOX = (835, 985)
YOU_AV_R = 35                            # big ring: fills the whole YOU box

PAGE_RIGHT, PAGE_Y = 970, 230             # page indicator in the upper-right blue panel


@lru_cache(maxsize=None)
def _template_bytes() -> bytes:
    return TEMPLATE.read_bytes()


@lru_cache(maxsize=None)
def _template() -> Image.Image:
    """Decoded once and reused; each card is drawn on a copy."""
    return Image.open(io.BytesIO(_template_bytes())).convert("RGB")


@lru_cache(maxsize=None)
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


def _put(draw, text, size, x, cy, fill, max_w, anchor="l", glow=None):
    """Draws text with a soft dark shadow (and optional coloured glow) so it stays readable on the neon template."""
    text, f = _fit(draw, text, size, max_w)
    a = anchor + "m"
    draw.text((x + 2, cy + 3), text, font=f, fill=(2, 8, 24), anchor=a)       # drop shadow
    draw.text((x + 1, cy + 1), text, font=f, fill=(2, 8, 24), anchor=a)
    draw.text((x, cy), text, font=f, fill=fill, anchor=a, stroke_width=0)


GREEN, RED, GREY = (88, 214, 66), (214, 52, 40), (150, 165, 185)


def _rank_text(rank) -> str:
    return str(rank) if isinstance(rank, int) else str(rank)


def _rank_cell(draw, rank, delta, cx, cy, size, fill, with_marker=True):
    """Zero-padded rank (01, 02...) with a movement marker like the reference:
    delta > 0 green up-arrow + n, delta < 0 red down-arrow + n, delta == 0 grey dot, None = no marker."""
    text = _rank_text(rank)
    # Give the page leader a distinct medal treatment instead of a plain "01".
    if isinstance(rank, int) and rank == 1:
        radius = max(19, size // 2 + 2)
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                     fill=GOLD, outline=(255, 226, 128), width=3)
        _put(draw, "1", size, cx, cy, (18, 30, 54), 48, "m")
        return
    if delta is None or not with_marker:
        _put(draw, text, size, cx, cy, fill, 120, "m")
        return
    _put(draw, text, size, cx - 26, cy, fill, 60, "m")
    mx = cx + 26
    if delta == 0:
        draw.ellipse((mx - 6, cy - 6, mx + 6, cy + 6), fill=GREY)
        return
    up = delta > 0
    col = GREEN if up else RED
    ax, h = mx - 16, 6
    if up:
        pts = [(ax - 10, cy + h), (ax, cy - h), (ax + 10, cy + h)]
    else:
        pts = [(ax - 10, cy - h), (ax, cy + h), (ax + 10, cy - h)]
    draw.line(pts, fill=col, width=5, joint="curve")
    _put(draw, str(abs(delta)), max(18, size - 8), mx + 8, cy, col, 40, "l")


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
                you: dict | None, page_text: str = "", guild_icon: bytes | None = None,
                player_to_watch: str = "—") -> bytes:
    """
    top  : {"rank", "name", "value", "avatar", optional "delta"} for the gold box (overall #1) or None
    rows : up to 9 dicts like top, each with its real rank
    you  : {"rank", "name", "value", "avatar"} for the person who typed +lb, or None
    avatar is PNG/JPEG bytes or None. Returns JPEG bytes.
    """
    img = _template().copy()
    d = ImageDraw.Draw(img)

    # The server's own icon is the only corner logo; the right corner is kept clear.
    _avatar(img, guild_icon, 82, 145, 43)

    _put(d, category, 27, CAT_X, CAT_Y, ICE, CAT_W)
    _put(d, stat_label.split(" (")[0], 27, STAT_X, STAT_Y, ICE, STAT_W)
    if page_text:
        _put(d, page_text, 22, PAGE_RIGHT, PAGE_Y, (159, 208, 255), 200, "r")

    if top:
        cx, cy, r = GOLD_RING
        _avatar(img, top.get("avatar"), cx, cy, r)
        _rank_cell(d, top["rank"], top.get("delta"), GOLD_RANK_X + 20, cy, 50, GOLD)
        _put(d, top["name"], 44, GOLD_NAME_X, cy, ICE, GOLD_STAT_RIGHT - 200 - GOLD_NAME_X)
        _put(d, top["value"], 44, GOLD_STAT_RIGHT, cy, GOLD, 260, "r")

    for i, e in enumerate(rows[:ROWS]):
        cy = ROW_CY[i]
        _rank_cell(d, e["rank"], e.get("delta"), RANK_CX, cy, 33, ICE)
        _avatar(img, e.get("avatar"), ROW_AV_X, cy, ROW_AV_R)
        _put(d, e["name"], 32, NAME_X, cy, ICE, STAT_BOX[0] - 20 - NAME_X)
        _put(d, e["value"], 32, (STAT_BOX[0] + STAT_BOX[1]) // 2, cy, ICE, STAT_BOX[1] - STAT_BOX[0] - 14, "m")

    if you:
        _rank_cell(d, you.get("rank", "-"), you.get("delta"), YOU_RANK_CX, YOU_Y, 30, GOLD)
        _avatar(img, you.get("avatar"), YOU_AV_X, YOU_Y, YOU_AV_R)
        _put(d, you["name"], 29, YOU_NAME_X, YOU_Y, ICE, YOU_STAT_BOX[0] - 20 - YOU_NAME_X)
        _put(d, you["value"], 29, (YOU_STAT_BOX[0] + YOU_STAT_BOX[1]) // 2, YOU_Y, GOLD,
             YOU_STAT_BOX[1] - YOU_STAT_BOX[0] - 14, "m")

    # The template has two small information panels beneath the pinned YOU row.
    # Keep their contents generic so the same card works for every leaderboard.
    panel_y = 1450
    _put(d, "PLAYER TO WATCH", 24, 405, panel_y, (159, 208, 255), 285, "m")
    _put(d, player_to_watch or "—", 25, 770, panel_y, ICE, 300, "m")

    out = io.BytesIO()
    img.save(out, "JPEG", quality=90, subsampling=0)   # ~5x smaller and much faster than PNG
    return out.getvalue()
