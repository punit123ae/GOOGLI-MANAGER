"""Draws the Kashmir Titans leaderboard card on cogs/lb_card_template.png (used by +lb).

Layout (1024 x 1535):
  top-left ring     server icon (falls back to the Kashmir Titans badge when the server has none)
  gold box          first player of the page: crown for overall #1, rank, movement marker
  9 rows            rank + movement marker, avatar, name, stat
  YOU panel         the person who typed +lb
  bottom bar        PLAYER TO WATCH + username
"""
from __future__ import annotations

import io
import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "lb_card_template.png"
BADGE = HERE / "lb_card_badge.png"
FONT_PATHS = [HERE / "Agrandir-TextBold.otf", HERE / "Bangers-Regular.ttf"]

ICE = (232, 243, 255)
LABEL = (150, 205, 245)
CYAN = (95, 215, 235)
GOLD = (255, 200, 50)
GOLD_LIGHT = (255, 232, 140)
# Movement colours, brightened from the reference so they read on the dark card.
GREEN, RED, GREY = (96, 214, 70), (240, 78, 66), (150, 162, 182)

# ---- coordinates on the 1024 x 1535 template ----
LOGO = (96, 98, 57)                       # server icon: centre x, centre y, radius (inside the ring)
CAT_X, CAT_Y, CAT_W = 62, 231, 420        # category text in the upper-left chip
PAGE_CX, PAGE_Y = 848, 231                # page indicator in the upper-right chip
TITLE_CX, TITLE_CY, TITLE_W = 512, 318, 640

AV_X = 205                                # every avatar ring shares this centre x
GOLD_CY, GOLD_AV_R = 467, 57
GOLD_STACK_X = 98                         # rank / crown / marker column inside the gold box
GOLD_NAME_X, GOLD_STAT_RIGHT = 292, 960

HEADER_STAT = (900, 609)                  # "STATS" heading
ROWS = 9                                  # rows under the gold box (gold + 9 = 10 players per page)
ROW_CY = [663, 722, 781, 840, 899, 958, 1017, 1076, 1135]
ROW_AV_R = 21
ROW_RANK_LEFT = 50
NAME_X = 250
STAT_CX = 900

YOU_CY, YOU_AV_R = 1294, 46
YOU_STACK_X = 98
YOU_NAME_X = 285

BAR_CY = 1410                             # PLAYER TO WATCH bar
BAR_LEFT_CX, BAR_RIGHT_CX = 318, 815


@lru_cache(maxsize=None)
def _template() -> Image.Image:
    """Decoded once and reused; each card is drawn on a copy."""
    return Image.open(TEMPLATE).convert("RGB")


@lru_cache(maxsize=None)
def _badge() -> Image.Image | None:
    try:
        return Image.open(BADGE).convert("RGBA")
    except OSError:
        return None


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


def _put(draw, text, size, x, cy, fill, max_w, anchor="l"):
    """Text with a soft dark shadow so it stays readable on the neon template."""
    text, f = _fit(draw, text, size, max_w)
    a = anchor + "m"
    draw.text((x + 2, cy + 3), text, font=f, fill=(2, 8, 24), anchor=a)
    draw.text((x, cy), text, font=f, fill=fill, anchor=a)


def _shape(img: Image.Image, ox: int, oy: int, w: int, h: int, color, paint, ss: int = 4) -> None:
    """Anti-aliased vector shape: paint(draw, s) draws on a w*h box scaled by s; pasted at (ox, oy)."""
    layer = Image.new("RGBA", (w * ss, h * ss), tuple(color) + (0,))
    paint(ImageDraw.Draw(layer), ss)
    layer = layer.resize((w, h), Image.LANCZOS)
    img.paste(layer, (ox, oy), layer)


def _avatar(img: Image.Image, data: bytes | None, cx: int, cy: int, r: int) -> bool:
    if not data:
        return False
    try:
        av = Image.open(io.BytesIO(data)).convert("RGB").resize((2 * r, 2 * r), Image.LANCZOS)
    except Exception:
        return False
    big = 8 * r
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    img.paste(av, (cx - r, cy - r), mask.resize((2 * r, 2 * r), Image.LANCZOS))
    return True


# ---------------------------------------------------------------- rank + movement
def _rank_text(rank) -> str:
    """01, 02 ... 99, then plain numbers."""
    if isinstance(rank, int) and 0 < rank < 100:
        return f"{rank:02d}"
    return str(rank)


def _marker_width(draw, delta, size: int) -> int:
    if delta is None:
        return 0
    if delta == 0:
        return 14
    return 26 + int(draw.textlength(str(abs(delta)), font=_font(max(18, size - 6))))


def _draw_marker(img, draw, delta, left: int, cy: int, size: int) -> None:
    """Movement marker like the reference: grey dot = unchanged, green chevron + n = up n places,
    red chevron + n = down n places. `left` is the marker's left edge."""
    if delta is None:
        return
    if delta == 0:
        _shape(img, left - 2, cy - 9, 18, 18, GREY,
               lambda d, s: d.ellipse((2 * s, 2 * s, 16 * s - 1, 16 * s - 1), fill=GREY + (255,)))
        return
    up = delta > 0
    col = GREEN if up else RED

    def chevron(d, s):
        pts = [(3, 15), (10, 7), (17, 15)] if up else [(3, 7), (10, 15), (17, 7)]
        pts = [(x * s, y * s) for x, y in pts]
        d.line(pts, fill=col + (255,), width=int(4.6 * s), joint="curve")
        for x, y in (pts[0], pts[-1]):
            d.ellipse((x - 2.3 * s, y - 2.3 * s, x + 2.3 * s, y + 2.3 * s), fill=col + (255,))

    _shape(img, left, cy - 11, 20, 22, col, chevron)
    _put(draw, str(abs(delta)), max(18, size - 6), left + 26, cy, col, 40, "l")


def _rank_inline(img, draw, rank, delta, left, cy, size, color) -> None:
    """Row layout:  02  ^1"""
    text, f = _fit(draw, _rank_text(rank), size, 70)
    _put(draw, text, size, left, cy, color, 70, "l")
    if isinstance(rank, int):
        _draw_marker(img, draw, delta, left + int(draw.textlength(text, font=f)) + 16, cy, size)


def _rank_stack(img, draw, rank, delta, cx, cy, size, color, crown=False) -> None:
    """Big-box layout: [crown] / rank / marker, centred on cx."""
    text = _rank_text(rank)
    has_marker = isinstance(rank, int) and delta is not None
    if crown:
        _draw_crown(img, cx, cy - 40)
        num_cy, mk_cy = cy + 2, cy + 40
    else:
        num_cy, mk_cy = cy - (10 if has_marker else 0), cy + 30
    _put(draw, text, size, cx, num_cy, color, 90, "m")
    if has_marker:
        mw = _marker_width(draw, delta, size)
        _draw_marker(img, draw, delta, cx - mw // 2, mk_cy, size)


def _draw_crown(img, cx: int, cy: int) -> None:
    """Gold crown for the overall #1."""
    w, h = 58, 38  # drawn at 58x38, pasted as-is

    def crown(d, s):
        pts = [(2, 34), (2, 10), (16, 22), (29, 3), (42, 22), (56, 10), (56, 34)]
        d.polygon([(x * s, y * s) for x, y in pts], fill=GOLD + (255,), outline=GOLD_LIGHT + (255,))
        d.rectangle((2 * s, 30 * s, 56 * s, 36 * s), fill=GOLD + (255,), outline=GOLD_LIGHT + (255,))
        for x, y in ((2, 10), (29, 3), (56, 10)):
            d.ellipse(((x - 3.2) * s, (y - 3.2) * s, (x + 3.2) * s, (y + 3.2) * s), fill=GOLD_LIGHT + (255,))

    _shape(img, cx - w // 2, cy - h // 2, w, h, GOLD, crown)


def _draw_star(img, cx: int, cy: int, r: int, color) -> None:
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.45
        pts.append((r + 2 + rad * math.cos(ang), r + 2 + rad * math.sin(ang)))
    size = 2 * r + 4
    _shape(img, cx - r - 2, cy - r - 2, size, size, color,
           lambda d, s: d.polygon([(x * s, y * s) for x, y in pts], fill=tuple(color) + (255,)))


# ---------------------------------------------------------------- title text
TITLE_MAP = {
    "Runs": "TOP RUN SCORERS", "Wickets": "TOP WICKET TAKERS",
    "Batting Average": "TOP BATTER AVERAGES", "Most 50s": "MOST FIFTIES",
    "Most 100s": "MOST CENTURIES", "Ducks": "MOST DUCKS",
    "Highest Runs": "HIGHEST INDIVIDUAL SCORES",
    "Highest Strike Rate (minimum 50 runs)": "TOP STRIKE RATES",
    "Lowest Strike Rate (minimum 50 runs)": "LOWEST STRIKE RATES",
    "Average": "TOP BOWLING AVERAGES", "Economy": "BEST ECONOMY RATES",
    "Bowling Figures": "BEST BOWLING FIGURES", "Most Overs Bowled": "MOST OVERS BOWLED",
    "Most Runs Leaked": "MOST RUNS CONCEDED", "Total Fantasy Points": "TOP FANTASY PLAYERS",
    "Most Catches Caught": "TOP CATCH TAKERS", "Most Catches Dropped": "MOST CATCH DROPS",
    "Highest Catch Efficiency": "TOP FIELDING EFFICIENCY",
    "Most Runs After Catch Drops": "RUNS AFTER CATCH DROPS",
    "Most Runs Without Catch Drops": "RUNS WITHOUT CATCH DROPS",
    "Most Catches Dropped (as Batter)": "MOST BATTER CATCH DROPS",
    "Team's Best Fielder": "TOP TEAM FIELDER",
    "Best Team Catch Efficiency": "BEST TEAM FIELDING EFFICIENCY",
    "Most Catches as a Team": "MOST TEAM CATCHES",
    "Most Catch Drops as a Team": "MOST TEAM CATCH DROPS",
    "Most Catch Opportunities as a Team": "MOST TEAM CATCH OPPORTUNITIES",
}


def _draw_title(d: ImageDraw.ImageDraw, stat_label: str) -> None:
    title = TITLE_MAP.get(stat_label, f"TOP {stat_label.upper()}")
    title, f = _fit(d, title, 56, TITLE_W, min_size=26)
    words = title.split(" ")
    if len(words) > 1:                                   # last word in cyan, like the reference
        head, tail = " ".join(words[:-1]) + " ", words[-1]
        wh, wt = d.textlength(head, font=f), d.textlength(tail, font=f)
        x = TITLE_CX - (wh + wt) / 2
        d.text((x, TITLE_CY), head, font=f, fill=ICE, anchor="lm")
        d.text((x + wh, TITLE_CY), tail, font=f, fill=CYAN, anchor="lm")
    else:
        d.text((TITLE_CX, TITLE_CY), title, font=f, fill=CYAN, anchor="mm")


# ---------------------------------------------------------------- card
def render_card(category: str, stat_label: str, top: dict | None, rows: list[dict],
                you: dict | None, page_text: str = "", guild_icon: bytes | None = None,
                player_to_watch: str = "—") -> bytes:
    """
    top  : {"rank", "name", "value", "avatar", "delta"} for the gold box (first player of the page)
    rows : up to 9 dicts like top, each with its real rank
    you  : {"rank", "name", "value", "avatar", "delta"} for the person who typed +lb, or None
    delta: places moved since the last stats update (+ up, - down, 0 unchanged, None no marker)
    avatar / guild_icon are PNG/JPEG bytes or None. Returns JPEG bytes.
    """
    img = _template().copy()
    d = ImageDraw.Draw(img)

    # server icon in the top-left ring (the right corner stays clear)
    cx, cy, r = LOGO
    if not _avatar(img, guild_icon, cx, cy, r):
        badge = _badge()
        if badge is not None:
            img.paste(badge, (cx - badge.width // 2, cy - badge.height // 2), badge)

    _put(d, category.upper(), 27, CAT_X, CAT_Y, ICE, CAT_W)
    if page_text:
        _put(d, page_text, 24, PAGE_CX, PAGE_Y, CYAN, 140, "m")
    _draw_title(d, stat_label)
    _put(d, "STATS", 25, HEADER_STAT[0], HEADER_STAT[1], LABEL, 150, "m")

    if top:
        _avatar(img, top.get("avatar"), AV_X, GOLD_CY, GOLD_AV_R)
        _rank_stack(img, d, top["rank"], top.get("delta"), GOLD_STACK_X, GOLD_CY, 46, GOLD,
                    crown=(top["rank"] == 1))
        _put(d, top["name"], 48, GOLD_NAME_X, GOLD_CY, ICE, GOLD_STAT_RIGHT - 230 - GOLD_NAME_X)
        _put(d, top["value"], 48, GOLD_STAT_RIGHT, GOLD_CY, GOLD, 230, "r")

    for i, e in enumerate(rows[:ROWS]):
        cy = ROW_CY[i]
        _rank_inline(img, d, e["rank"], e.get("delta"), ROW_RANK_LEFT, cy, 33, ICE)
        _avatar(img, e.get("avatar"), AV_X, cy, ROW_AV_R)
        _put(d, e["name"], 32, NAME_X, cy, ICE, STAT_CX - 80 - NAME_X)
        _put(d, e["value"], 32, STAT_CX, cy, ICE, 150, "m")

    if you:
        _avatar(img, you.get("avatar"), AV_X, YOU_CY, YOU_AV_R)
        _rank_stack(img, d, you.get("rank", "-"), you.get("delta"), YOU_STACK_X, YOU_CY, 36, GOLD)
        _put(d, you["name"], 36, YOU_NAME_X, YOU_CY, ICE, STAT_CX - 80 - YOU_NAME_X)
        _put(d, you["value"], 36, STAT_CX, YOU_CY, GOLD, 150, "m")

    # bottom bar: PLAYER TO WATCH | username
    label = "PLAYER TO WATCH"
    lw = d.textlength(label, font=_font(27))
    start = BAR_LEFT_CX - (lw + 36) / 2
    _draw_star(img, int(start + 11), BAR_CY, 11, GOLD)
    _put(d, label, 27, int(start + 36), BAR_CY, LABEL, 330, "l")
    _put(d, player_to_watch or "—", 30, BAR_RIGHT_CX, BAR_CY, ICE, 320, "m")

    out = io.BytesIO()
    img.save(out, "JPEG", quality=90, subsampling=0)   # ~5x smaller and much faster than PNG
    return out.getvalue()
