"""Two-sided comparison images: +compareteams and the image version of +h2h.

The page is split down the middle: the left half glows in the left side's colour and the
right half in the right side's colour. Stat rows grow outwards from the centre line.
"""
import colorsys
import io
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter

from .teaminfo_image import (
    DIM, H, MUTED, W, WHITE, _backdrop_color, _clip, _fit, _lighten, _mix, _open, _paste, _pill,
    _round_image, _text, _visible_accent, font,
)

INK = (12, 18, 32)
FALLBACK_LEFT = (70, 140, 255)
FALLBACK_RIGHT = (255, 112, 92)
CX = W // 2


# --------------------------------------------------------------------------- colours

def _distance(a, b):
    return sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5


def pick_colors(left, right):
    """Role colours for each side; near-identical or missing colours are separated."""
    left = tuple(left) if left else FALLBACK_LEFT
    right = tuple(right) if right else FALLBACK_RIGHT
    if _distance(_visible_accent(left), _visible_accent(right)) < 90:
        h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in right))
        right = tuple(int(c * 255) for c in colorsys.hsv_to_rgb((h + 0.5) % 1, max(s, 0.6), max(v, 0.9)))
    return left, right


@lru_cache(maxsize=3)
def _background(left_bg, right_bg):
    """Dark page, left half tinted left_bg, right half tinted right_bg, soft seam in the middle."""
    base = Image.new("RGB", (W, H), (5, 9, 20)).convert("RGBA")

    def glow(cx, cy, rx, ry, color, alpha, blur):
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse((cx-rx, cy-ry, cx+rx, cy+ry), fill=color + (alpha,))
        return layer.filter(ImageFilter.GaussianBlur(blur))

    for (cx, cy, rx, ry, a, b) in ((260, 200, 620, 420, 150, 150), (120, 800, 520, 300, 90, 130)):
        base = Image.alpha_composite(base, glow(cx, cy, rx, ry, left_bg, a, b))
    for (cx, cy, rx, ry, a, b) in ((1340, 200, 620, 420, 150, 150), (1480, 800, 520, 300, 90, 130)):
        base = Image.alpha_composite(base, glow(cx, cy, rx, ry, right_bg, a, b))

    # half-page tint so each side reads clearly as "its" colour
    tint = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    td = ImageDraw.Draw(tint)
    td.polygon([(0, 0), (CX + 70, 0), (CX - 70, H), (0, H)], fill=left_bg + (34,))
    td.polygon([(CX + 70, 0), (W, 0), (W, H), (CX - 70, H)], fill=right_bg + (34,))
    base = Image.alpha_composite(base, tint)

    # diagonal seam
    seam = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(seam)
    sd.line([(CX + 70, 0), (CX - 70, H)], fill=(255, 255, 255, 70), width=3)
    base = Image.alpha_composite(base, seam.filter(ImageFilter.GaussianBlur(2)))

    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    for gx in range(0, W, 60):
        gd.line((gx, 0, gx, H), fill=(255, 255, 255, 5))
    for gy in range(0, H, 60):
        gd.line((0, gy, W, gy), fill=(255, 255, 255, 5))
    base = Image.alpha_composite(base, grid)

    vignette = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    vignette.putalpha(Image.radial_gradient("L").resize((W, H)).point(lambda v: int(max(0, v - 120) * 1.1)))
    return Image.alpha_composite(base, vignette).convert("RGB")


class _Canvas:
    """Page with queued glass cards; seal() composites them once."""

    def __init__(self, left_bg, right_bg, left, right):
        self.image = _background(left_bg, right_bg).copy()
        self.cards = []
        self.left, self.right = left, right

    def card(self, box, radius=24):
        self.cards.append((box, radius))

    def seal(self):
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for box, radius in self.cards:
            ld.rounded_rectangle(box, radius=radius, fill=(8, 14, 28, 214), outline=(52, 72, 110), width=1)
        self.image = Image.alpha_composite(self.image.convert("RGBA"), layer).convert("RGB")
        self.cards = []
        return ImageDraw.Draw(self.image)

    def png(self):
        out = io.BytesIO()
        self.image.save(out, "PNG", compress_level=3)
        return out.getvalue()


def _setup(left, right):
    left_c, right_c = pick_colors(left.get("color"), right.get("color"))
    canvas = _Canvas(_backdrop_color(left_c), _backdrop_color(right_c), left, right)
    return canvas, _visible_accent(left_c), _visible_accent(right_c)


# --------------------------------------------------------------------------- pieces

def _side_header(canvas, d, side, accent, left_side, top=52):
    """Logo/avatar ring, name and chips for one side of the hero card."""
    picture = _open(side.get("image"))
    size = 190
    px = 84 if left_side else W - 84 - size - 12
    _paste(canvas.image, _round_image(picture, size, ring=accent, ring_w=6, initial=side["name"][:1]), (px, top))
    name = side["name"].upper()
    text_w = 360
    anchor_x = 312 if left_side else W - 312
    anchor = "la" if left_side else "ra"
    size = _fit(d, name, text_w, 60, 40, "head")
    if d.textlength(name, font=font(size, "head")) <= text_w:
        _text(d, (anchor_x, top + 16), name, size, WHITE, anchor, "head")
    else:   # long names: split on the most even space and draw two lines
        words = name.split()
        best = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))),
                   default=None)
        lines = [" ".join(words[:best]), " ".join(words[best:])] if best else [name]
        small = min(_fit(d, line, text_w, 44, 20, "head") for line in lines)
        for i, line in enumerate(lines):
            _text(d, (anchor_x, top + 6 + i * (small + 4)), _clip(d, line, text_w, small, "head"), small, WHITE, anchor, "head")
    chips, used = [], 0
    for chip in side.get("chips", []):
        if not chip:
            continue
        w = int(d.textlength(chip, font=font(18))) + 32
        if used + w > text_w + 8:
            continue
        chips.append((chip, w))
        used += w + 10
    x = anchor_x if left_side else anchor_x - (used - 10)
    for i, (chip, w) in enumerate(chips):
        _pill(d, x, top + 112, chip, 18, fill=accent if i == 0 else None, fg=INK if i == 0 else WHITE,
              outline=None if i == 0 else accent, pad=16, h=36)
        x += w + 10


def _hero(canvas, d, title, left, right, la, ra, score):
    _side_header(canvas, d, left, la, True)
    _side_header(canvas, d, right, ra, False)
    _text(d, (CX, 46), title, 15, MUTED, "mm")
    _text(d, (CX, 118), "VS", 88, WHITE, "mm", "head")
    a, b = score
    _text(d, (CX - 56, 206), a, 48, la, "mm", "head")
    _text(d, (CX, 206), "—", 30, MUTED, "mm")
    _text(d, (CX + 56, 206), b, 48, ra, "mm", "head")
    _text(d, (CX, 250), "STATS WON", 13, DIM, "mm")


def _footer(d, text):
    _text(d, (CX, 884), text, 13, DIM, "mm")


# --------------------------------------------------------------------------- stat rows

def _winner(row):
    a, b, direction = row["a"], row["b"], row.get("better")
    if direction is None or a is None or b is None or a == b:
        return 0
    if direction == "high":
        return 1 if a > b else 2
    return 1 if a < b else 2


def tally(rows):
    """(left wins, right wins) over all rows that have a direction."""
    left = right = 0
    for row in rows:
        w = _winner(row)
        left += w == 1
        right += w == 2
    return left, right


def render_overview(title, left, right, rows, edges, footer="GOOGLI MANAGER"):
    """rows: dicts {label, a, b, fa, fb, better('high'|'low'|None), group}; edges: [(label, winner 0/1/2, detail)]."""
    canvas, la, ra = _setup(left, right)
    score = tally(rows)
    canvas.card((50, 22, 1550, 296), 28)
    row_h = min(42, 420 // max(1, len(rows)))
    table_top = 314
    table_bottom = table_top + 24 + row_h * len(rows) + 14
    canvas.card((50, table_top, 1550, table_bottom), 28)
    canvas.card((50, table_bottom + 16, 1550, 868), 28)
    d = canvas.seal()
    _hero(canvas, d, title, left, right, la, ra, score)

    dim_l, dim_r = _mix(la, (20, 28, 48), 0.62), _mix(ra, (20, 28, 48), 0.62)
    bar_max = 470
    y = table_top + 18
    for index, row in enumerate(rows):
        win = _winner(row)
        if index % 2 == 0:
            d.rounded_rectangle((70, y, 1530, y + row_h - 2), radius=12, fill=(14, 22, 42))
        mid = y + row_h // 2 - 1
        top = max((v for v in (row["a"], row["b"]) if v), default=0)
        for side, value, accent, dim, sign in ((1, row["a"], la, dim_l, -1), (2, row["b"], ra, dim_r, 1)):
            ratio = (value / top) if (value and top) else 0
            length = max(8 if value else 0, int(bar_max * ratio))
            x0 = CX + sign * 108
            x1 = x0 + sign * length
            fill = accent if win in (0, side) else dim
            if length:
                d.rounded_rectangle((min(x0, x1), mid - 8, max(x0, x1), mid + 8), radius=8, fill=fill)
            text = row["fa"] if side == 1 else row["fb"]
            big = win == side
            col = WHITE if win in (0, side) else MUTED
            if side == 1:
                _text(d, (176, mid), text, 25 if big else 22, col if not big else _lighten(la, 0.55), "rm", "head")
            else:
                _text(d, (W - 176, mid), text, 25 if big else 22, col if not big else _lighten(ra, 0.55), "lm", "head")
        _text(d, (CX, mid), row["label"], 15, MUTED, "mm")
        y += row_h

    # edges strip
    strip_top = table_bottom + 16
    _text(d, (80, strip_top + 24), "THE EDGE", 16, WHITE, "lm", "bold")
    slot = (1520 - 280 + 16) // max(1, len(edges))
    for i, (label, winner, detail) in enumerate(edges):
        x = 280 + i * slot
        color = la if winner == 1 else ra if winner == 2 else MUTED
        d.rounded_rectangle((x, strip_top + 12, x + slot - 16, 868 - 14), radius=18, fill=(16, 26, 48), outline=color, width=2)
        _text(d, (x + 22, strip_top + 26), label, 14, MUTED)
        name_part, _, score_part = detail.partition("||")
        tone = color if winner else WHITE
        if score_part:
            _text(d, (x + slot - 38, strip_top + 56), score_part, 26, tone, "rm", "head")
            room = slot - 60 - int(d.textlength(score_part, font=font(26, "head"))) - 12
            _text(d, (x + 22, strip_top + 56), _clip(d, name_part, room, 22, "head"), 22, tone, "lm", "head")
        else:
            _text(d, (x + 22, strip_top + 46), _clip(d, detail, slot - 60, 22), _fit(d, detail, slot - 60, 22, 14), tone, kind="head")
    _footer(d, footer)
    return canvas.png()


def render_leaders(title, left, right, leaders, footer="GOOGLI MANAGER"):
    """leaders: [{label, color, left: {name,value,unit,sub,image}|None, right: ...}] (3 rows)."""
    canvas, la, ra = _setup(left, right)
    canvas.card((50, 22, 1550, 112), 28)
    rows = leaders[:3]
    tops = [132 + i * 250 for i in range(len(rows))]
    for top in tops:
        canvas.card((50, top, 1550, top + 232), 28)
    d = canvas.seal()
    for img, name, accent, x, anchor in ((left.get("image"), left["name"], la, 80, "la"), (right.get("image"), right["name"], ra, W - 80, "ra")):
        pic = _open(img)
        ring = _round_image(pic, 56, ring=accent, ring_w=3, initial=name[:1])
        _paste(canvas.image, ring, (x if anchor == "la" else x - ring.width, 38))
        tx = x + 78 if anchor == "la" else x - 78
        _text(d, (tx, 44), _clip(d, name.upper(), 520, 34, "head"), 34, WHITE, anchor, "head")
    _text(d, (CX, 66), title, 16, MUTED, "mm")
    _text(d, (CX, 34), "KEY PLAYERS", 13, DIM, "mm")

    for top, item in zip(tops, rows):
        tone = tuple(item["color"])
        label = item["label"]
        w = int(d.textlength(label, font=font(18))) + 44
        _pill(d, CX - w // 2, top + 22, label, 18, fill=tone, fg=INK, pad=22, h=40)
        lv = (item.get("left") or {}).get("number")
        rv = (item.get("right") or {}).get("number")
        for side, data, accent in ((1, item.get("left"), la), (2, item.get("right"), ra)):
            sign = -1 if side == 1 else 1
            if not data:
                _text(d, (CX + sign * 360, top + 116), "NO DATA", 26, DIM, "mm", "head")
                continue
            pic = _open(data.get("image"))
            size = 150
            ring = _round_image(pic, size, ring=accent, ring_w=5, initial=data["name"][:1])
            leader_side = (lv is not None and rv is not None and lv != rv and ((lv > rv) == (side == 1)))
            ax = 96 if side == 1 else W - 96 - ring.width
            _paste(canvas.image, ring, (ax, top + 40))
            tx = 96 + ring.width + 30 if side == 1 else W - 96 - ring.width - 30
            anchor = "la" if side == 1 else "ra"
            _text(d, (tx, top + 26), _clip(d, data["name"], 380, 30), _fit(d, data["name"], 380, 30, 18), WHITE, anchor, "bold")
            value = str(data["value"])
            vcol = _lighten(accent, 0.25) if leader_side or lv == rv else MUTED
            vsize = 86
            _text(d, (tx, top + 78), value, vsize, vcol, anchor, "head")
            vw = int(d.textlength(value, font=font(vsize, "head")))
            ux = tx + vw + 14 if side == 1 else tx - vw - 14
            _text(d, (ux, top + 136), data["unit"], 22, MUTED, anchor, "bold")
            _text(d, (tx, top + 182), _clip(d, data.get("sub", ""), 420, 20), 20, MUTED, anchor)
        _text(d, (CX, top + 116), "VS", 44, DIM, "mm", "head")
        if lv is not None and rv is not None and lv != rv:
            gap = abs(lv - rv)
            gap = int(gap) if float(gap).is_integer() else round(gap, 1)
            who = la if lv > rv else ra
            tag = f"{'◀' if lv > rv else ''} LEADS BY {gap} {'▶' if rv > lv else ''}".strip()
            w = int(d.textlength(tag, font=font(16))) + 32
            _pill(d, CX - w // 2, top + 152, tag, 16, fill=who, fg=INK, pad=16, h=32)
    _footer(d, footer)
    return canvas.png()


def render_matches(title, left, right, matches, summary, footer="GOOGLI MANAGER"):
    """matches: [{date, teams, a_text, b_text, winner(0/1/2)}] (up to 6)."""
    canvas, la, ra = _setup(left, right)
    canvas.card((50, 22, 1550, 112), 28)
    shown = max(1, min(len(matches), 6))
    canvas.card((50, 132, 1550, 190 + shown * 116 + 24 if matches else 560), 28)
    d = canvas.seal()
    for img, name, accent, x, anchor in ((left.get("image"), left["name"], la, 80, "la"), (right.get("image"), right["name"], ra, W - 80, "ra")):
        ring = _round_image(_open(img), 56, ring=accent, ring_w=3, initial=name[:1])
        _paste(canvas.image, ring, (x if anchor == "la" else x - ring.width, 38))
        tx = x + 78 if anchor == "la" else x - 78
        _text(d, (tx, 44), _clip(d, name.upper(), 520, 34, "head"), 34, WHITE, anchor, "head")
    _text(d, (CX, 34), "FACE TO FACE", 13, DIM, "mm")
    _text(d, (CX, 66), title, 16, MUTED, "mm")
    _text(d, (80, 158), summary, 20, WHITE, "lm")
    if not matches:
        _text(d, (CX, 480), "No recorded matches where they were on opposing teams.", 26, MUTED, "mm")
        _text(d, (CX, 524), "Only matches imported after team tracking was added can be matched up.", 18, DIM, "mm")
    row_h = 110
    for i, m in enumerate(matches[:6]):
        y = 190 + i * (row_h + 6)
        d.rounded_rectangle((70, y, 1530, y + row_h), radius=18, fill=(14, 22, 42))
        w = m["winner"]
        _text(d, (CX, y + 22), m["date"], 16, MUTED, "mm")
        _text(d, (CX, y + 52), _clip(d, m["teams"], 520, 22), _fit(d, m["teams"], 520, 22, 14), WHITE, "mm", "bold")
        for side, accent, x, anchor, text in ((1, la, 100, "la", m["a_text"]), (2, ra, W - 100, "ra", m["b_text"])):
            win = w in (0, side)
            _text(d, (x, y + 20), text[0], 30, _lighten(accent, 0.3) if w == side else WHITE if win else MUTED, anchor, "head")
            _text(d, (x, y + 66), text[1], 20, MUTED, anchor)
        if w:
            d.rounded_rectangle((70, y, 78, y + row_h) if w == 1 else (1522, y, 1530, y + row_h),
                                radius=4, fill=la if w == 1 else ra)
    _footer(d, footer)
    return canvas.png()


class Renderer:
    """Lazy page renderer shared by the compare/h2h commands: pages are drawn on demand."""

    def __init__(self, builders):
        import threading
        self._builders = builders        # {page number: zero-arg function -> PNG bytes}
        self._cache = {}
        self._lock = threading.Lock()
        self.page_count = len(builders)

    def render(self, number):
        with self._lock:
            if number not in self._cache:
                self._cache[number] = self._builders[number]()
            return self._cache[number]


# --------------------------------------------------------------------------- team inputs

def _num(value, digits=1, suffix=""):
    if value is None:
        return "—"
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer() and digits == 0):
        return f"{int(value):,}{suffix}"
    return f"{value:.{digits}f}{suffix}"


def _team_side(data):
    standing = data.get("standing")
    chips = []
    if standing:
        chips.append(f"{str(standing['group']).upper()} • #{standing['rank']}")
        chips.append(f"W{standing['won']} L{standing['lost']}")
    chips.append(f"ROSTER {data['roster_size']}")
    return {"name": data["name"], "image": data.get("logo"), "color": data.get("team_color"), "chips": chips}


def team_inputs(a, b):
    """(left, right, rows, edges) for the team overview page from two _team_profile_data dicts."""
    def figures(data):
        totals = data.get("totals") or {}
        standing = data.get("standing")
        won = standing["won"] if standing else None
        lost = standing["lost"] if standing else None
        played = (won + lost) if standing else 0
        return {
            "won": won, "points": standing["points"] if standing else None,
            "win_rate": (won / played * 100) if played else None,
            "runs": totals.get("runs"), "bat_avg": totals.get("bat_avg"), "strike_rate": totals.get("strike_rate"),
            "wickets": totals.get("wickets"), "economy": totals.get("economy"),
            "catches": totals.get("catches"), "drops": totals.get("drops"), "eff": totals.get("eff"),
        }

    fa, fb = figures(a), figures(b)

    def row(label, key, group, better="high", digits=1, suffix=""):
        x, y = fa[key], fb[key]
        return {"label": label, "a": x, "b": y, "fa": _num(x, digits, suffix), "fb": _num(y, digits, suffix),
                "better": better, "group": group}

    rows = [
        row("MATCHES WON", "won", "RECORD", digits=0),
        row("WIN RATE", "win_rate", "RECORD", digits=0, suffix="%"),
        row("POINTS", "points", "RECORD", digits=0),
        row("RUNS", "runs", "BATTING", digits=0),
        row("BATTING AVERAGE", "bat_avg", "BATTING"),
        row("STRIKE RATE", "strike_rate", "BATTING"),
        row("WICKETS", "wickets", "BOWLING", digits=0),
        row("ECONOMY", "economy", "BOWLING", better="low", digits=2),
        row("CATCHES", "catches", "FIELDING", digits=0),
        row("DROPPED", "drops", "FIELDING", better="low", digits=0),
        row("FIELDING EFFICIENCY", "eff", "FIELDING", digits=0, suffix="%"),
    ]
    edges = []
    for group in ("RECORD", "BATTING", "BOWLING", "FIELDING"):
        left_w, right_w = tally([r for r in rows if r["group"] == group])
        winner = 1 if left_w > right_w else 2 if right_w > left_w else 0
        name = a["name"] if winner == 1 else b["name"] if winner == 2 else "LEVEL"
        edges.append((f"{group} EDGE", winner, f"{name.upper()}||{max(left_w, right_w)}–{min(left_w, right_w)}"
                      if winner else f"LEVEL||{left_w}–{right_w}"))
    return _team_side(a), _team_side(b), rows, edges


def team_leader_inputs(a, b):
    leaders = []
    for la_, lb_ in zip(a.get("leaders", []), b.get("leaders", [])):
        def side(item):
            player = item.get("player")
            if not player:
                return None
            return {"name": player["name"], "value": item["value"], "number": item["value"], "unit": item["unit"],
                    "sub": item.get("sub", ""), "image": player.get("avatar_bytes")}
        leaders.append({"label": la_["label"], "color": la_["color"], "left": side(la_), "right": side(lb_)})
    return leaders
