"""Image pages for +h2h (player vs player) and +compareteams (team vs team).

Pure Pillow, no Discord objects: the commands gather the data, this module draws it.
Fonts and drawing helpers come from teaminfo_image.py, so the pages match the look of
+teaminfo and +fdstats (1600x900, glass cards, Agrandir headings).

The background is split down the middle on a diagonal seam: the left side takes the
first side's colour and the right side takes the second side's colour. For teams that
is each team's Discord role colour (logo colour if there is no role colour); for
players it is the colour of their team (a default blue / coral when they have none).

Entry points (each returns a list of PNG bytes, one per page):
  render_h2h_pages(data)      page 1 stat comparison, page 2 matches against each other
  render_compare_pages(data)  page 1 team comparison, page 2 top performers + meetings

Row helper for the commands:
  stat_row(label, a, b, kind, better, a_text, b_text)
"""
import colorsys
import io
import time
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .teaminfo_image import (
    CORAL, DEFAULT_ACCENT, DIM, GOLD, H, LOSS, MINT, MUTED, VIOLET, W, WHITE, WIN,
    _backdrop_color, _clip, _fit, _open, _paste, _pill, _round_image, _tag, _text,
    _visible_accent, font,
)

INK = (12, 18, 32)
EDGE = (62, 86, 130)
SIDE_B_FALLBACK = (255, 128, 94)
TRACK = (26, 38, 64)

# Category colours, same as the squad / team-stats pages of +teaminfo.
BAT_COLOR, BOWL_COLOR, FIELD_COLOR, RECORD_COLOR = CORAL, VIOLET, MINT, GOLD


# --------------------------------------------------------------------------- stat rows

def fmt_value(value, kind="int"):
    """Display text for a number. kind: int, num1, num2 (or num), pct, text."""
    if value is None:
        return "—"
    if kind == "int":
        return f"{int(round(value)):,}"
    if kind == "num1":
        return f"{value:.1f}"
    if kind in ("num", "num2"):
        return f"{value:.2f}"
    if kind == "pct":
        return f"{value:.1f}%"
    return str(value)


def stat_row(label, a, b, kind="int", better="high", a_text=None, b_text=None):
    """One comparison row. better: 'high', 'low' or None (information only).

    a / b are numbers, or None when there is no data (shown as a dash, never "wins").
    a_text / b_text override the displayed text (for example "3/12" or "14.2 ov").
    """
    return {
        "label": label, "a": a, "b": b, "better": better,
        "a_text": a_text if a_text is not None else fmt_value(a, kind),
        "b_text": b_text if b_text is not None else fmt_value(b, kind),
    }


def _verdict(row):
    """1 if side A wins the row, -1 if side B wins it, 0 for a draw / info / no data."""
    a, b, better = row["a"], row["b"], row["better"]
    if better is None or a is None or b is None or a == b:
        return 0
    if better == "high":
        return 1 if a > b else -1
    return 1 if a < b else -1


def _share(row):
    """Share of the bar that belongs to side A (0..1), or None when there is no data."""
    a, b = row["a"], row["b"]
    if a is None or b is None:
        return None
    if row["better"] is None:
        return 0.5
    a, b = float(a), float(b)
    if row["better"] == "high":
        low = min(a, b)
        if low < 0:                      # e.g. negative fielding points: measure from the lower one
            a, b = a - low, b - low
        total = a + b
        share = 0.5 if total <= 0 else a / total
    elif a <= 0 and b <= 0:
        share = 0.5
    elif a <= 0:
        share = 1.0
    elif b <= 0:
        share = 0.0
    else:
        share = b / (a + b)          # lower is better: the smaller value gets the bigger share
    return min(0.93, max(0.07, share))


def _tally(groups):
    """(a_total, b_total, [(a, b) per group]) of rows won."""
    a_total = b_total = 0
    per = []
    for group in groups:
        a = sum(1 for r in group["rows"] if _verdict(r) > 0)
        b = sum(1 for r in group["rows"] if _verdict(r) < 0)
        per.append((a, b))
        a_total += a
        b_total += b
    return a_total, b_total, per


# --------------------------------------------------------------------------- colours / background

def _far(x, y, limit=110):
    return sum((p - q) ** 2 for p, q in zip(x, y)) ** 0.5 >= limit


def _rotate_hue(color, turn=0.5):
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in color))
    return tuple(int(round(c * 255)) for c in colorsys.hsv_to_rgb((h + turn) % 1.0, max(s, 0.55), max(v, 0.85)))


def pick_sides(color_a, color_b):
    """Two clearly different side colours; a missing colour gets a default, a clash gets recoloured."""
    a = tuple(int(c) for c in color_a) if color_a else None
    b = tuple(int(c) for c in color_b) if color_b else None
    if a is None and b is None:
        return DEFAULT_ACCENT, SIDE_B_FALLBACK
    if a is None:
        return (DEFAULT_ACCENT if _far(DEFAULT_ACCENT, b) else SIDE_B_FALLBACK), b
    if b is None:
        return a, (SIDE_B_FALLBACK if _far(SIDE_B_FALLBACK, a) else DEFAULT_ACCENT)
    if not _far(a, b, 90):
        b = _rotate_hue(b)
    return a, b


SEAM_TOP, SEAM_BOTTOM = 880, 720      # the diagonal that separates the two colours


@lru_cache(maxsize=24)
def _split_background(bg_a, bg_b):
    """Dark page, left half in colour A, right half in colour B, with glows, beams, grid, vignette."""
    base = Image.composite(
        Image.new("RGB", (W, H), (12, 20, 42)),
        Image.new("RGB", (W, H), (4, 8, 18)),
        Image.linear_gradient("L").resize((W, H)),
    ).convert("RGBA")

    def poly(points, color, alpha, blur):
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(layer).polygon(points, fill=color + (alpha,))
        return layer.filter(ImageFilter.GaussianBlur(blur))

    def glow(cx, cy, rx, ry, color, alpha, blur):
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=color + (alpha,))
        return layer.filter(ImageFilter.GaussianBlur(blur))

    base = Image.alpha_composite(base, poly([(-300, -300), (SEAM_TOP, -300), (SEAM_BOTTOM, H + 300), (-300, H + 300)], bg_a, 120, 80))
    base = Image.alpha_composite(base, poly([(SEAM_TOP, -300), (W + 300, -300), (W + 300, H + 300), (SEAM_BOTTOM, H + 300)], bg_b, 120, 80))
    base = Image.alpha_composite(base, glow(240, 170, 560, 360, bg_a, 150, 130))
    base = Image.alpha_composite(base, glow(1360, 170, 560, 360, bg_b, 150, 130))
    base = Image.alpha_composite(base, glow(120, 880, 520, 260, bg_a, 70, 120))
    base = Image.alpha_composite(base, glow(1480, 880, 520, 260, bg_b, 70, 120))

    beams = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(beams)
    bd.polygon([(0, 0), (190, 0), (50, H), (0, H)], fill=bg_a + (34,))
    bd.polygon([(W, 0), (W - 190, 0), (W - 50, H), (W, H)], fill=bg_b + (34,))
    bd.polygon([(SEAM_TOP - 4, 0), (SEAM_TOP + 4, 0), (SEAM_BOTTOM + 4, H), (SEAM_BOTTOM - 4, H)], fill=(255, 255, 255, 40))
    base = Image.alpha_composite(base, beams.filter(ImageFilter.GaussianBlur(7)))

    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    for gx in range(0, W, 60):
        gd.line((gx, 0, gx, H), fill=(255, 255, 255, 6))
    for gy in range(0, H, 60):
        gd.line((0, gy, W, gy), fill=(255, 255, 255, 6))
    base = Image.alpha_composite(base, grid)

    vignette = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    vignette.putalpha(Image.radial_gradient("L").resize((W, H)).point(lambda v: int(max(0, v - 120) * 1.1)))
    return Image.alpha_composite(base, vignette).convert("RGB")


class _Canvas:
    """One 1600x900 page. Queue the glass cards, seal them, then draw the content on top."""

    def __init__(self, col_a, col_b):
        self.a = _visible_accent(col_a)
        self.b = _visible_accent(col_b)
        self.image = _split_background(_backdrop_color(col_a), _backdrop_color(col_b)).copy()
        self.cards = []
        self.d = ImageDraw.Draw(self.image)

    def card(self, box, radius=24, alpha=186):
        self.cards.append((box, radius, alpha))

    def seal(self):
        glass = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glass)
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        for box, radius, alpha in self.cards:
            gd.rounded_rectangle(box, radius=radius, fill=(8, 14, 30, alpha))
            md.rounded_rectangle(box, radius=radius, fill=255)
        # a faint left-to-right wash of the two side colours inside every card
        sweep = Image.linear_gradient("L").rotate(90, expand=True).resize((W, H))     # black left, white right
        wash = Image.composite(
            Image.new("RGBA", (W, H), self.b + (44,)),
            Image.new("RGBA", (W, H), self.a + (44,)),
            sweep,
        )
        wash.putalpha(ImageChops.multiply(wash.getchannel("A"), mask))
        glass = Image.alpha_composite(glass, wash)
        gd = ImageDraw.Draw(glass)
        for box, radius, _alpha in self.cards:
            gd.rounded_rectangle(box, radius=radius, outline=EDGE + (190,), width=1)
        self.image = Image.alpha_composite(self.image.convert("RGBA"), glass).convert("RGB")
        self.d = ImageDraw.Draw(self.image)
        self.cards = []
        return self.d


# --------------------------------------------------------------------------- drawing helpers

def _title(d, xy, value, size, fill=WHITE, anchor="la", kind="head"):
    """Heading text with a soft dark shadow so it stays readable on the coloured background."""
    x, y = xy
    d.text((x + 2, y + 3), str(value), font=font(size, kind), fill=(0, 0, 0), anchor=anchor)
    d.text(xy, str(value), font=font(size, kind), fill=fill, anchor=anchor)


def _png(image):
    out = io.BytesIO()
    image.save(out, format="PNG", compress_level=3)
    return out.getvalue()


def _tug(d, x1, x2, y, h, share, col_a, col_b):
    """Tug-of-war bar: side A fills from the left, side B from the right."""
    d.rounded_rectangle((x1, y, x2, y + h), radius=h // 2, fill=TRACK)
    if share is None:
        return
    cut = x1 + int((x2 - x1) * share)
    d.rounded_rectangle((x1, y, max(x1 + h, cut - 3), y + h), radius=h // 2, fill=col_a)
    d.rounded_rectangle((min(x2 - h, cut + 3), y, x2, y + h), radius=h // 2, fill=col_b)


def _draw_row(d, x1, x2, y, row, col_a, col_b, pad=28):
    """label in the middle, both values at the sides (winner lit up with a marker), tug bar below."""
    verdict = _verdict(row)
    cx = (x1 + x2) // 2
    mid = y + 17
    label = row["label"].upper()
    _text(d, (cx, mid), label, 14, MUTED, "mm")
    room = max(60, (x2 - x1) / 2 - pad - d.textlength(label, font=font(14)) / 2 - 16)
    for side in ("a", "b"):
        text = row[side + "_text"]
        missing = row[side] is None
        wins = verdict > 0 if side == "a" else verdict < 0
        loses = verdict < 0 if side == "a" else verdict > 0
        accent = col_a if side == "a" else col_b
        if missing:
            color = DIM
        elif wins:
            color = accent
        elif loses:
            color = MUTED
        else:
            color = WHITE
        kind = "bold" if missing else "head"
        size = _fit(d, text, room, 30, 16, kind)
        width = d.textlength(text, font=font(size, kind))
        if side == "a":
            _text(d, (x1 + pad, mid), text, size, color, "lm", kind)
            if wins:
                tx = x1 + pad + width + 10
                d.polygon([(tx, mid + 6), (tx + 11, mid + 6), (tx + 5.5, mid - 6)], fill=accent)
        else:
            _text(d, (x2 - pad, mid), text, size, color, "rm", kind)
            if wins:
                tx = x2 - pad - width - 10
                d.polygon([(tx - 11, mid + 6), (tx, mid + 6), (tx - 5.5, mid - 6)], fill=accent)
    _tug(d, x1 + pad, x2 - pad, y + 40, 8, _share(row), col_a, col_b)


def _draw_tally(d, right, y, a, b, col_a, col_b, size=26):
    f = font(size, "head")
    b_w = int(d.textlength(str(b), font=f))
    a_w = int(d.textlength(str(a), font=f))
    dash_w = int(d.textlength("-", font=f))
    _text(d, (right, y), b, size, col_b, "ra", "head")
    _text(d, (right - b_w - 10, y), "-", size, DIM, "ra", "head")
    _text(d, (right - b_w - 10 - dash_w - 10, y), a, size, col_a, "ra", "head")
    return a_w


def _draw_group(d, box, group, col_a, col_b, won, row_h, first_row=64):
    x1, y1, x2, y2 = box
    color = group["color"]
    d.ellipse((x1 + 28, y1 + 27, x1 + 40, y1 + 39), fill=color)
    _text(d, (x1 + 54, y1 + 21), group["title"], 24, color, "la", "head")
    _draw_tally(d, x2 - 28, y1 + 21, won[0], won[1], col_a, col_b)
    for i, row in enumerate(group["rows"]):
        _draw_row(d, x1, x2, y1 + first_row + i * row_h, row, col_a, col_b)


def _scoreboard(c, a_total, b_total, a_name, b_name, caption):
    """Centre of the hero: caption chip, big 'A : B' score, who leads."""
    d = c.d
    cx = W // 2
    cap = caption.upper()
    width = int(d.textlength(cap, font=font(16))) + 40
    _pill(d, cx - width // 2, 34, cap, 16, fill=(10, 16, 32), fg=MUTED, outline=EDGE, pad=20, h=34)
    big = 96
    _title(d, (cx - 28, 134), a_total, big, c.a, "rm", "head")
    _text(d, (cx, 126), ":", 72, DIM, "mm", "bold")
    _title(d, (cx + 28, 134), b_total, big, c.b, "lm", "head")
    _text(d, (cx, 198), "STATS WON", 14, MUTED, "mm")
    if a_total == b_total:
        label, fill, outline = "LEVEL", None, EDGE
        fg = WHITE
    else:
        leader_name, leader_color = (a_name, c.a) if a_total > b_total else (b_name, c.b)
        label = _clip(d, f"{leader_name.upper()} LEADS", 300, 18)
        fill, outline, fg = leader_color, None, INK
    pw = int(d.textlength(label, font=font(18))) + 40
    _pill(d, cx - pw // 2, 214, label, 18, fill=fill, fg=fg, outline=outline, pad=20, h=34)


def _footer(c, left_text, guild):
    d = c.d
    _text(d, (60, 872), left_text.upper(), 13, DIM)
    if guild:
        _text(d, (1540, 872), str(guild).upper(), 13, DIM, "ra")


# =========================================================================== +h2h

def _player_block(c, who, accent, left):
    """Avatar, name, team pill and two mini stats for one player on the hero."""
    d = c.d
    ring = _round_image(_open(who.get("avatar")), 200, ring=accent, ring_w=6, initial=who["name"][:1])
    ax = 70 if left else W - 70 - ring.size[0]
    _paste(c.image, ring, (ax, 52))
    tx = ax + ring.size[0] + 28 if left else ax - 28
    anchor = "la" if left else "ra"
    room = 330
    name = who["name"].upper()
    size = _fit(d, name, room, 56, 24, "head")
    _title(d, (tx, 74), _clip(d, name, room, size, "head"), size, WHITE, anchor, "head")
    team = _clip(d, (who.get("team") or "FREE AGENT").upper(), 300, 18)
    pw = int(d.textlength(team, font=font(18))) + 36
    _pill(d, tx if left else tx - pw, 152, team, 18, fg=WHITE, outline=accent, pad=18, h=38)
    for i, (label, value) in enumerate((who.get("mini") or [])[:2]):
        sx = tx + i * 150 if left else tx - i * 150
        _text(d, (sx, 206), label, 13, MUTED, anchor)
        _text(d, (sx, 224), value, 30, accent, anchor, "head")


def _h2h_stats_page(data, col_a, col_b):
    c = _Canvas(col_a, col_b)
    groups = data["groups"]
    boxes = [(50, 302, 530, 852), (560, 302, 1040, 852), (1070, 302, 1550, 852)]
    for box in boxes:
        c.card(box, 26)
    c.seal()
    a, b = data["a"], data["b"]
    _player_block(c, a, c.a, True)
    _player_block(c, b, c.b, False)
    a_total, b_total, per = _tally(groups)
    _scoreboard(c, a_total, b_total, a["name"], b["name"], data.get("caption", "Head to head"))
    for box, group, won in zip(boxes, groups, per):
        _draw_group(c.d, box, group, c.a, c.b, won, row_h=62, first_row=70)
    _footer(c, f"GOOGLI MANAGER  •  HEAD TO HEAD  •  {a['name']} vs {b['name']}"[:90], data.get("guild"))
    return c.image


def _fmt_date(ts):
    try:
        return time.strftime("%d %b %Y", time.gmtime(float(ts))).upper()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _h2h_matches_page(data, col_a, col_b):
    c = _Canvas(col_a, col_b)
    a, b = data["a"], data["b"]
    shown = list(data.get("matches") or [])[:7]
    extra = int(data.get("match_total", len(shown))) - len(shown)
    c.card((50, 22, 1550, 106), 24)
    boxes = [(50, 126 + i * 100, 1550, 126 + i * 100 + 92) for i in range(len(shown))]
    for box in boxes:
        c.card(box, 20)
    if not shown:
        c.card((50, 126, 1550, 852), 26)
    d = c.seal()

    # header: both players, title and the tally in the middle
    _paste(c.image, _round_image(_open(a.get("avatar")), 58, ring=c.a, ring_w=3, initial=a["name"][:1]), (72, 34))
    _paste(c.image, _round_image(_open(b.get("avatar")), 58, ring=c.b, ring_w=3, initial=b["name"][:1]), (W - 72 - 64, 34))
    _title(d, (150, 38), _clip(d, a["name"].upper(), 360, 30, "head"), _fit(d, a["name"].upper(), 360, 30, 18, "head"), WHITE, "la")
    _title(d, (W - 150, 38), _clip(d, b["name"].upper(), 360, 30, "head"), _fit(d, b["name"].upper(), 360, 30, 18, "head"), WHITE, "ra")
    _text(d, (W // 2, 40), "MATCHES AGAINST EACH OTHER", 20, WHITE, "mm")
    if shown:
        summary = (f"{data.get('match_total', len(shown))} MATCH(ES)  •  {a['name']} SCORED MORE IN "
                   f"{data.get('a_more', 0)}, {b['name']} IN {data.get('b_more', 0)}").upper()
    else:
        summary = "NO RECORDED MATCHES ON OPPOSING TEAMS"
    _text(d, (W // 2, 72), _clip(d, summary, 560, 15), 15, MUTED, "mm")

    if not shown:
        _text(d, (W // 2, 470), "THEY HAVE NOT FACED EACH OTHER YET", 30, DIM, "mm", "head")
        _text(d, (W // 2, 520), "Only matches imported after team tracking was added can be matched up.", 18, DIM, "mm", "reg")

    for i, (box, m) in enumerate(zip(boxes, shown)):
        x1, y1, x2, y2 = box
        a_runs, b_runs = int(m["a_runs"]), int(m["b_runs"])
        winner = 1 if a_runs > b_runs else (-1 if b_runs > a_runs else 0)
        if winner > 0:
            d.rounded_rectangle((x1, y1 + 14, x1 + 7, y2 - 14), radius=3, fill=c.a)
        elif winner < 0:
            d.rounded_rectangle((x2 - 7, y1 + 14, x2, y2 - 14), radius=3, fill=c.b)
        # centre: number, date and a runs tug bar
        _text(d, (W // 2, y1 + 18), f"#{i + 1}  •  {_fmt_date(m['ts'])}", 15, MUTED, "mm")
        _tug(d, W // 2 - 110, W // 2 + 110, y1 + 40, 8,
             None if a_runs + b_runs == 0 else min(0.93, max(0.07, a_runs / (a_runs + b_runs))), c.a, c.b)
        _text(d, (W // 2 - 120, y1 + 44), a_runs, 18, c.a if winner > 0 else MUTED, "rm", "head")
        _text(d, (W // 2 + 120, y1 + 44), b_runs, 18, c.b if winner < 0 else MUTED, "lm", "head")
        _text(d, (W // 2, y1 + 72), "RUNS", 12, DIM, "mm")
        for side, left, accent in (("a", True, c.a), ("b", False, c.b)):
            team = _clip(d, str(m[side + "_team"]).upper(), 330, 16)
            edge = x1 + 36 if left else x2 - 36
            anchor = "la" if left else "ra"
            _text(d, (edge, y1 + 12), team, 16, accent, anchor)
            for j, (label, value) in enumerate((("BAT", m[side + "_bat"]), ("BOWL", m[side + "_bowl"]))):
                off = j * 210
                px = edge + off if left else edge - off
                _text(d, (px, y1 + 40), label, 12, MUTED, anchor)
                size = _fit(d, value, 190, 30, 16, "head" if value != "DNB" else "bold")
                _text(d, (px, y1 + 54), value, size, WHITE if value != "DNB" else DIM, anchor,
                      "head" if value != "DNB" else "bold")
    if extra > 0:
        _text(d, (W // 2, 846), f"…AND {extra} OLDER MATCH(ES)", 14, MUTED, "mm")
    _footer(c, f"GOOGLI MANAGER  •  HEAD TO HEAD  •  MATCHES  •  {a['name']} vs {b['name']}"[:90], data.get("guild"))
    return c.image


def render_h2h_pages(data, only_first=False):
    """data: a/b {name, avatar, team, color, mini}, groups, matches, match_total, a_more, b_more, guild.

    Optional data["caption"] replaces the "Head to head" chip; only_first draws just page 1.
    """
    col_a, col_b = pick_sides(data["a"].get("color"), data["b"].get("color"))
    if only_first:
        return [_png(_h2h_stats_page(data, col_a, col_b))]
    return [_png(_h2h_stats_page(data, col_a, col_b)), _png(_h2h_matches_page(data, col_a, col_b))]


# =========================================================================== +compareteams

def _chips(d, x, y, chips, accent, left, room):
    """Small outlined / filled chips laid out from the inner edge outwards."""
    for label, filled in chips:
        label = _clip(d, label.upper(), room, 17)
        width = int(d.textlength(label, font=font(17))) + 32
        px = x if left else x - width
        if filled:
            _pill(d, px, y, label, 17, fill=accent, fg=INK, pad=16, h=36)
        else:
            _pill(d, px, y, label, 17, fg=WHITE, outline=accent, pad=16, h=36)
        x = x + width + 10 if left else x - width - 10
        room = max(40, room - width - 10)


def _team_block(c, team, accent, left):
    d = c.d
    ring = _round_image(_open(team.get("logo")), 196, ring=accent, ring_w=6, initial=team["name"][:1])
    ax = 70 if left else W - 70 - ring.size[0]
    _paste(c.image, ring, (ax, 40))
    tx = ax + ring.size[0] + 26 if left else ax - 26
    anchor = "la" if left else "ra"
    room = 330
    name = team["name"].upper()
    size = _fit(d, name, room, 58, 26, "head")
    _title(d, (tx, 62), _clip(d, name, room, size, "head"), size, WHITE, anchor, "head")
    standing = team.get("standing")
    chips = []
    if standing:
        chips.append((str(standing["group"]), False))
        chips.append((f"RANK #{standing['rank']} OF {standing['of']}", True))
    elif team.get("roster"):
        chips.append((team["roster"], False))
    _chips(d, tx, 146, chips, accent, left, room)
    history = (standing or {}).get("history", [])[-5:]
    _text(d, (tx, 214), "FORM", 14, MUTED, "lm" if left else "rm")
    if history:
        for i, result in enumerate(history):
            win = result == "W"
            cx = (tx + 62 + i * 40) if left else (tx - 62 - i * 40)
            d.ellipse((cx - 15, 199, cx + 15, 229), fill=WIN if win else LOSS)
            _text(d, (cx, 214), result, 15, INK, "mm")
    else:
        _text(d, (tx + 62 if left else tx - 62, 214), "NO RESULTS YET", 14, DIM, "lm" if left else "rm")


def _compare_main_page(data, col_a, col_b):
    c = _Canvas(col_a, col_b)
    groups = data["groups"]
    boxes = [(50, 268, 790, 552), (810, 268, 1550, 552), (50, 568, 790, 852), (810, 568, 1550, 852)]
    for box in boxes:
        c.card(box, 26)
    c.seal()
    a, b = data["a"], data["b"]
    _team_block(c, a, c.a, True)
    _team_block(c, b, c.b, False)
    a_total, b_total, per = _tally(groups)
    _scoreboard(c, a_total, b_total, a["name"], b["name"], data.get("caption", "Team comparison"))
    for box, group, won in zip(boxes, groups, per):
        _draw_group(c.d, box, group, c.a, c.b, won, row_h=54, first_row=64)
    _footer(c, f"GOOGLI MANAGER  •  TEAM COMPARISON  •  {a['name']} vs {b['name']}"[:90], data.get("guild"))
    return c.image


def _leader_side(c, box, who, accent, left, label_color):
    """One side of a top-performer row: avatar, name, big number, small line."""
    d = c.d
    x1, y1, x2, y2 = box
    if who is None:
        _text(d, (x1 + 160 if left else x2 - 160, (y1 + y2) // 2), "NO DATA YET", 22, DIM, "mm", "bold")
        return
    ring = _round_image(_open(who.get("avatar")), 104, ring=accent, ring_w=4, initial=who["name"][:1])
    ax = x1 + 30 if left else x2 - 30 - ring.size[0]
    _paste(c.image, ring, (ax, y1 + (y2 - y1 - ring.size[1]) // 2))
    tx = ax + ring.size[0] + 22 if left else ax - 22
    anchor = "la" if left else "ra"
    room = 270
    name = who["name"].upper()
    size = _fit(d, name, room, 30, 18, "head")
    _text(d, (tx, y1 + 24), _clip(d, name, room, size, "head"), size, WHITE, anchor, "head")
    _text(d, (tx, y1 + 56), who["value"], 58, accent, anchor, "head")
    width = d.textlength(str(who["value"]), font=font(58, "head"))
    ux = tx + width + 10 if left else tx - width - 10
    _text(d, (ux, y1 + 96), who["unit"], 16, MUTED, "la" if left else "ra")
    _text(d, (tx, y1 + 122), _clip(d, who["sub"], room + 60, 16, "reg"), 16, MUTED, anchor, "reg")


def _compare_people_page(data, col_a, col_b):
    c = _Canvas(col_a, col_b)
    a, b = data["a"], data["b"]
    leaders = data.get("leaders") or []
    c.card((50, 22, 1550, 106), 24)
    row_boxes = [(50, 126 + i * 164, 1550, 126 + i * 164 + 152) for i in range(len(leaders))]
    for box in row_boxes:
        c.card(box, 24)
    meet_box = (50, 126 + len(leaders) * 164, 1550, 852)
    c.card(meet_box, 26)
    d = c.seal()

    # header
    _paste(c.image, _round_image(_open(a.get("logo")), 58, ring=c.a, ring_w=3, initial=a["name"][:1]), (72, 34))
    _paste(c.image, _round_image(_open(b.get("logo")), 58, ring=c.b, ring_w=3, initial=b["name"][:1]), (W - 72 - 64, 34))
    an, bn = a["name"].upper(), b["name"].upper()
    _title(d, (150, 38), _clip(d, an, 380, 30, "head"), _fit(d, an, 380, 30, 18, "head"), WHITE, "la")
    _title(d, (W - 150, 38), _clip(d, bn, 380, 30, "head"), _fit(d, bn, 380, 30, 18, "head"), WHITE, "ra")
    _text(d, (W // 2, 40), "TOP PERFORMERS & MEETINGS", 20, WHITE, "mm")
    _text(d, (W // 2, 72), "MAIN TOURNAMENT  •  WHOLE TEAM HISTORY", 15, MUTED, "mm")

    for box, leader in zip(row_boxes, leaders):
        x1, y1, x2, y2 = box
        color = leader["color"]
        _leader_side(c, (x1, y1, 790, y2), leader.get("a"), c.a, True, color)
        _leader_side(c, (810, y1, x2, y2), leader.get("b"), c.b, False, color)
        label = leader["label"]
        pw = int(d.textlength(label, font=font(17))) + 36
        _pill(d, W // 2 - pw // 2, y1 + 26, label, 17, fill=color, fg=INK, pad=18, h=38)
        av = (leader.get("a") or {}).get("number")
        bv = (leader.get("b") or {}).get("number")
        share = None
        if av is not None and bv is not None:
            share = 0.5 if av + bv <= 0 else min(0.93, max(0.07, av / (av + bv)))
        _tug(d, W // 2 - 130, W // 2 + 130, y1 + 92, 10, share, c.a, c.b)

    # recent meetings
    x1, y1, x2, y2 = meet_box
    meetings = list(data.get("meetings") or [])[:4]
    a_wins = sum(1 for m in meetings if m["a"] > m["b"])
    b_wins = sum(1 for m in meetings if m["b"] > m["a"])
    _text(d, (x1 + 30, y1 + 22), "RECENT MEETINGS", 20, GOLD, "la", "head")
    _text(d, (x1 + 30, y1 + 52), "TEAM TOTAL RUNS IN MATCHES BETWEEN THESE TWO TEAMS", 13, MUTED)
    if meetings:
        _draw_tally(d, x2 - 30, y1 + 22, a_wins, b_wins, c.a, c.b)
    if not meetings:
        _text(d, ((x1 + x2) // 2, (y1 + y2) // 2 + 20), "NO RECORDED MEETINGS YET", 26, DIM, "mm", "head")
    slot = min(380, (x2 - x1 - 60) // max(1, len(meetings)))
    start = (x1 + x2) // 2 - slot * len(meetings) // 2
    ta, tb = _tag(a["name"]), _tag(b["name"])
    for i, m in enumerate(meetings):
        sx = start + i * slot
        if i:
            d.line((sx - 8, y1 + 96, sx - 8, y2 - 26), fill=EDGE, width=1)
        cx = sx + slot // 2 - 8
        _text(d, (cx, y1 + 100), f"MATCH {i + 1}  •  {_fmt_date(m['ts'])}", 14, MUTED, "mm")
        winner = 1 if m["a"] > m["b"] else (-1 if m["b"] > m["a"] else 0)
        _text(d, (cx - 22, y1 + 148), m["a"], 46, c.a if winner > 0 else WHITE, "rm", "head")
        _text(d, (cx, y1 + 144), "-", 36, DIM, "mm", "bold")
        _text(d, (cx + 22, y1 + 148), m["b"], 46, c.b if winner < 0 else WHITE, "lm", "head")
        if winner == 0:
            _text(d, (cx, y1 + 194), "TIED", 16, MUTED, "mm")
        else:
            who, color, margin = (ta, c.a, m["a"] - m["b"]) if winner > 0 else (tb, c.b, m["b"] - m["a"])
            _text(d, (cx, y1 + 194), f"{who} WON BY {margin} RUNS", 16, color, "mm")
    _footer(c, f"GOOGLI MANAGER  •  TEAM COMPARISON  •  PERFORMERS  •  {a['name']} vs {b['name']}"[:90], data.get("guild"))
    return c.image


def render_compare_pages(data, only_first=False):
    """data: a/b {name, logo, color, standing, roster}, groups (4), leaders (3), meetings, guild.

    Optional data["caption"] replaces the "Team comparison" chip; only_first draws just page 1.
    """
    col_a, col_b = pick_sides(data["a"].get("color"), data["b"].get("color"))
    if only_first:
        return [_png(_compare_main_page(data, col_a, col_b))]
    return [_png(_compare_main_page(data, col_a, col_b)), _png(_compare_people_page(data, col_a, col_b))]
