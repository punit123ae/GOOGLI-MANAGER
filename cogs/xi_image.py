"""Images for +xi (4 pages: playing XI, batting & bowling order, substitutes, bench) and
+cxi (3 pages: the two XIs, key players, rating breakdown).

Pure Pillow. Data comes from cogs/xi.py:
  side = {"name", "logo" (bytes), "color", "guild",
          "xi": [...], "subs": [...], "bench": [...], "strength": {...}, "roster": int, "size": int,
          "people": {uid: {"name": str, "avatar": bytes|None}}}
Players are the dicts built by cogs/xi_engine.py.
"""
from PIL import Image

from .teaminfo_image import (
    DIM, MUTED, W, WHITE, _backdrop_color, _fit, _clip, _lighten, _num, _open, _paste, _pill,
    _Page, _round_image, _tag, _text, _visible_accent, accent_from_logo, font,
)
from .versus_image import (
    BAT_COLOR, BOWL_COLOR, EDGE, FIELD_COLOR, INK, TRACK, _Canvas, _png, _tug, pick_sides,
)

ROLE_COLOR = {"Batsman": BAT_COLOR, "Bowler": BOWL_COLOR, "All-Rounder": FIELD_COLOR}
SHORT = {"Opener": "OPN", "Middle Order": "MID", "All-Rounder": "AR", "Bowler": "BWL", "Batsman": "BAT"}
BOX = (18, 29, 54)


def _theme(side):
    logo = _open(side.get("logo"))
    raw = tuple(side["color"]) if side.get("color") else accent_from_logo(logo)
    accent = _visible_accent(raw)
    return {"accent": accent, "accent2": _lighten(accent, 0.35), "bg": _backdrop_color(raw), "logo": logo}


def _rating_color(value):
    if value >= 82:
        return (255, 205, 70)
    if value >= 66:
        return (62, 224, 140)
    if value >= 52:
        return (110, 170, 255)
    return (170, 180, 200)


def _who(side, player):
    info = side["people"].get(player["uid"], {})
    return info.get("name") or f"Player {player['uid']}", _open(info.get("avatar"))


def _badge(d, cx, cy, r, value, size=None):
    """Round rating badge like a player card: the number inside a ring coloured by tier."""
    color = _rating_color(value)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(10, 16, 30), outline=color, width=3)
    _text(d, (cx, cy + 1), f"{value:.0f}", size or int(r * 1.0), color, "mm", "head")


def _stat_values(p):
    """The four numbers that matter for this player's job: [(label, text)]."""
    def f(v, dec=1):
        return "—" if v is None else f"{v:.{dec}f}"
    if p["role"] == "Bowler":
        return [("WKTS", str(p["wickets"])), ("B.AVG", f(p["bowl_avg"])), ("B.SR", f(p["bowl_sr"])),
                ("MAT", str(p["matches"]))]
    if p["role"] == "All-Rounder":
        return [("AVG", f(p["bat_avg"])), ("RUNS", f"{p['runs']:,}"), ("WKTS", str(p["wickets"])),
                ("B.AVG", f(p["bowl_avg"]))]
    return [("AVG", f(p["bat_avg"])), ("RUNS", f"{p['runs']:,}"), ("MAT", str(p["matches"])),
            ("CATCH", str(p["catches"]))]


def _tag_pill(d, x2, y, tag, accent):
    if tag:
        text = "C" if tag == "C" else "VC"
        _pill(d, x2 - 16 - (34 if tag == "C" else 44), y, text, 13, fill=accent, fg=INK, pad=10, h=26)


def _header(page, side, subtitle, number, total, theme):
    """Logo, team name and page chip. Draws after the glass cards are sealed."""
    accent = theme["accent"]
    page.card((50, 22, 1550, 150), 26)
    d = page.seal()
    badge = _round_image(theme["logo"], 96, ring=accent, ring_w=4, initial=side["name"][:1])
    _paste(page.image, badge, (74, 38))
    _text(d, (190, 44), subtitle, 16, accent, "la", "bold")
    name = side["name"].upper()
    _text(d, (190, 70), _clip(d, name, 630, _fit(d, name, 630, 44, 24, "head"), "head"),
          _fit(d, name, 630, 44, 24, "head"), WHITE, "la", "head")
    label = f"PAGE {number} / {total}"
    chip_w = int(d.textlength(label, font=font(16))) + 28
    _pill(d, 1550 - 24 - chip_w, 34, label, 16, fill=accent, fg=INK)
    st = side["strength"]
    tiles = [("OVERALL", st["overall"]), ("BATTING", st["batting"]), ("BOWLING", st["bowling"]),
             ("FIELDING", st["fielding"]), ("FORM", st.get("form", 0))]
    x = 836
    for label, value in tiles:
        d.rounded_rectangle((x, 74, x + 130, 136), radius=14, fill=BOX, outline=(40, 58, 92), width=1)
        _text(d, (x + 12, 82), label, 12, MUTED, "la")
        _text(d, (x + 12, 98), f"{value:.0f}", 30, _rating_color(value), "la", "head")
        d.rounded_rectangle((x + 58, 114, x + 118, 122), radius=4, fill=TRACK)
        d.rounded_rectangle((x + 58, 114, x + 58 + int(60 * min(100, value) / 100), 122), radius=4,
                            fill=_rating_color(value))
        x += 138
    return d


def _footer(page, side, number, total, what):
    d = page.d
    _text(d, (60, 874), f"{side.get('guild', '').upper()}  •  {what}", 13, DIM)
    _text(d, (1540, 874), f"{_tag(side['name'])}  •  {number}/{total}", 13, DIM, "ra")


# --------------------------------------------------------------------------- page 1: the XI

HOT, COLD = (62, 224, 140), (255, 96, 120)


def _form_spark(d, right, bottom, p):
    """Fantasy points of the last matches as small bars (oldest left), plus HOT / COLD / STEADY."""
    trend = p.get("trend") or []
    top = max([abs(v) for v in trend] + [1])
    delta = p.get("form_delta", 0)
    color = HOT if delta >= 12 else COLD if delta <= -12 else (150, 165, 195)
    bar_w, gap, height = 7, 4, 18
    x = right - (bar_w * 5 + gap * 4)
    for i in range(5):
        bx = x + i * (bar_w + gap)
        d.rounded_rectangle((bx, bottom - 3, bx + bar_w, bottom), radius=1, fill=(36, 52, 84))       # empty slot
        if i < len(trend):
            h = max(3, int(height * max(0, trend[i]) / top))
            d.rounded_rectangle((bx, bottom - h, bx + bar_w, bottom), radius=2, fill=color)
    word = "HOT" if delta >= 12 else "COLD" if delta <= -12 else "STEADY"
    mark = "▲" if delta >= 12 else "▼" if delta <= -12 else "●"
    _text(d, (right, bottom - height - 14), f"{mark} {word}", 11, color, "ra", "bold")


def _xi_card(page, side, box, p, accent):
    x1, y1, x2, y2 = box
    d = page.d
    color = ROLE_COLOR[p["role"]]
    name, avatar = _who(side, p)
    d.rounded_rectangle((x1, y1 + 20, x1 + 6, y2 - 20), radius=3, fill=color)
    _text(d, (x1 + 22, y1 + 14), f"#{p['order']}", 14, DIM, "la", "head")
    ring = _round_image(avatar, 92, ring=color, ring_w=4, initial=name[:1])
    _paste(page.image, ring, (x1 + 22, y1 + 36))
    _badge(d, x1 + 22 + 92 - 8, y1 + 36 + 92 - 8, 22, p["value"], 22)
    room = x2 - (x1 + 134) - 16
    shown = _clip(d, name, room, _fit(d, name, room, 25, 17, "head"), "head")
    _text(d, (x1 + 134, y1 + 48), shown, _fit(d, shown, room, 25, 17, "head"), WHITE, "la", "head")
    _pill(d, x1 + 134, y1 + 86, p["label"].upper(), 13, fill=None, fg=color, outline=color, pad=12, h=28)
    style = p["style"] if p["role"] != "Bowler" else (p.get("bstyle") or "Balanced")
    _text(d, (x1 + 134, y1 + 126), style.upper(), 12, MUTED, "la", "bold")
    _form_spark(d, x2 - 18, y1 + 138, p)
    _tag_pill(d, x2, y1 + 12, p.get("tag"), accent)
    stats = _stat_values(p)
    gap, left = 8, x1 + 16
    width = (x2 - x1 - 32 - gap * 3) // 4
    for i, (label, value) in enumerate(stats):
        bx = left + i * (width + gap)
        d.rounded_rectangle((bx, y1 + 150, bx + width, y2 - 14), radius=12, fill=BOX)
        _text(d, (bx + width / 2, y1 + 160), label, 11, MUTED, "ma")
        _text(d, (bx + width / 2, y1 + 178), value, _fit(d, value, width - 10, 24, 14, "head"), WHITE, "ma", "head")


def _balance_card(page, side, box, accent):
    x1, y1, x2, y2 = box
    d = page.d
    xi = side["xi"]
    _text(d, (x1 + 22, y1 + 18), "XI BALANCE", 16, accent, "la", "bold")
    counts = [("BATTERS", sum(1 for p in xi if p["role"] == "Batsman"), BAT_COLOR),
              ("ALL-ROUNDERS", sum(1 for p in xi if p["role"] == "All-Rounder"), FIELD_COLOR),
              ("BOWLERS", sum(1 for p in xi if p["role"] == "Bowler"), BOWL_COLOR)]
    total = max(1, len(xi))
    for i, (label, count, color) in enumerate(counts):
        y = y1 + 56 + i * 40
        _text(d, (x1 + 22, y), label, 13, MUTED, "la")
        _text(d, (x2 - 22, y - 4), str(count), 24, color, "ra", "head")
        d.rounded_rectangle((x1 + 22, y + 20, x2 - 22, y + 28), radius=4, fill=TRACK)
        if count:
            d.rounded_rectangle((x1 + 22, y + 20, x1 + 22 + int((x2 - x1 - 44) * count / total), y + 28),
                                radius=4, fill=color)
    note = f"ROSTER HAS ONLY {side['roster']} PLAYERS" if side["roster"] < 11 else \
        f"{len(side['subs'])} SUBS  •  {len(side['bench'])} ON THE BENCH"
    _text(d, ((x1 + x2) / 2, y2 - 22), note, 12, DIM, "mm")


def _page_xi(side, theme):
    page = _Page(theme)
    accent = theme["accent"]
    xi = side["xi"]
    slots = []
    cols, gap = 4, 14
    card_w = (1500 - gap * (cols - 1)) // cols
    card_h = (852 - 166 - gap * 2) // 3
    for i in range(12):
        row, col = divmod(i, cols)
        x1 = 50 + col * (card_w + gap)
        y1 = 166 + row * (card_h + gap)
        slots.append((x1, y1, x1 + card_w, y1 + card_h))
    for box in slots:
        page.card(box, 24)
    d = _header(page, side, "BEST PLAYING XI  •  CAREER STATS + RECENT FORM", 1, 4, theme)
    for box, p in zip(slots, xi):
        _xi_card(page, side, box, p, accent)
    if len(xi) < 12:
        _balance_card(page, side, slots[min(11, max(len(xi), 11))], accent)
    if not xi:
        _text(d, (800, 500), "NO PLAYERS IN THIS TEAM YET", 28, DIM, "mm")
    _footer(page, side, 1, 4, "BEST PLAYING XI")
    return page.image


# --------------------------------------------------------------------------- page 2: substitutes

def _sub_card(page, side, box, p, accent):
    x1, y1, x2, y2 = box
    d = page.d
    color = ROLE_COLOR[p["role"]]
    name, avatar = _who(side, p)
    cx = (x1 + x2) // 2
    d.rounded_rectangle((x1 + 24, y1, x2 - 24, y1 + 6), radius=3, fill=color)
    ring = _round_image(avatar, 150, ring=color, ring_w=5, initial=name[:1])
    _paste(page.image, ring, (cx - 75, y1 + 28))
    _badge(d, cx + 62, y1 + 28 + 140, 30, p["value"], 28)
    shown = _clip(d, name, x2 - x1 - 40, _fit(d, name, x2 - x1 - 40, 30, 18, "head"), "head")
    _text(d, (cx, y1 + 212), shown, _fit(d, shown, x2 - x1 - 40, 30, 18, "head"), WHITE, "ma", "head")
    label = p["label"].upper()
    width = int(d.textlength(label, font=font(14))) + 28
    _pill(d, cx - width // 2, y1 + 254, label, 14, fill=None, fg=color, outline=color, pad=14, h=30)
    _tag_pill(d, x2, y1 + 18, p.get("tag"), accent)

    def f(v, dec=1):
        return "—" if v is None else f"{v:.{dec}f}"
    rows = [("MATCHES", str(p["matches"])), ("BAT AVG", f(p["bat_avg"])), ("RUNS", f"{p['runs']:,}"),
            ("WICKETS", str(p["wickets"])), ("BOWL AVG", f(p["bowl_avg"])), ("BOWL SR", f(p["bowl_sr"])),
            ("CATCHES", str(p["catches"])), ("FANTASY", f"{p['fantasy']:.0f}")]
    for i, (label, value) in enumerate(rows):
        col, row = i % 2, i // 2
        bx = x1 + 22 + col * ((x2 - x1 - 44) // 2 + 6)
        bw = (x2 - x1 - 44) // 2 - 6
        by = y1 + 304 + row * 62
        d.rounded_rectangle((bx, by, bx + bw, by + 54), radius=12, fill=BOX)
        _text(d, (bx + 12, by + 8), label, 11, MUTED, "la")
        _text(d, (bx + 12, by + 24), value, 22, WHITE, "la", "head")
    cover = side["people"].get(p.get("cover"), {}).get("name")
    d.rounded_rectangle((x1 + 22, y2 - 62, x2 - 22, y2 - 18), radius=14, fill=(14, 24, 46), outline=color, width=2)
    _text(d, (x1 + 38, y2 - 40), "COVERS", 11, MUTED, "lm")
    _text(d, (x2 - 36, y2 - 40), _clip(d, (cover or "—").upper(), x2 - x1 - 150, 16), 16, WHITE, "rm", "bold")


def _page_subs(side, theme):
    page = _Page(theme)
    accent = theme["accent"]
    subs = side["subs"]
    width, gap = 360, 20
    count = max(1, len(subs))
    start = 50 + (1500 - (count * width + (count - 1) * gap)) // 2
    boxes = [(start + i * (width + gap), 170, start + i * (width + gap) + width, 852) for i in range(count)]
    for box in boxes:
        page.card(box, 26)
    d = _header(page, side, "SUBSTITUTES  •  NEXT IN LINE", 3, 4, theme)
    if subs:
        for box, p in zip(boxes, subs):
            _sub_card(page, side, box, p, accent)
    else:
        _text(d, (800, 480), "NO SUBSTITUTES", 34, DIM, "mm", "head")
        _text(d, (800, 530), "EVERY PLAYER ON THE ROSTER IS IN THE XI", 16, DIM, "mm")
    _footer(page, side, 3, 4, "SUBSTITUTES")
    return page.image


# --------------------------------------------------------------------------- page 3: bench

BENCH_COLUMNS = [("MAT", 860, lambda p: str(p["matches"])),
                 ("AVG", 940, lambda p: "—" if p["bat_avg"] is None else f"{p['bat_avg']:.1f}"),
                 ("RUNS", 1030, lambda p: f"{p['runs']:,}"),
                 ("WKTS", 1120, lambda p: str(p["wickets"])),
                 ("B.AVG", 1210, lambda p: "—" if p["bowl_avg"] is None else f"{p['bowl_avg']:.1f}"),
                 ("CATCH", 1300, lambda p: str(p["catches"])),
                 ("FPTS", 1390, lambda p: f"{p['fantasy']:.0f}")]


def _page_bench(side, theme):
    page = _Page(theme)
    accent = theme["accent"]
    bench = side["bench"][:9]
    page.card((50, 170, 1550, 852), 26)
    d = _header(page, side, "BENCH  •  OUTSIDE THE XI AND SUBS", 4, 4, theme)
    if not bench:
        _text(d, (800, 480), "NOBODY ON THE BENCH", 34, DIM, "mm", "head")
        _text(d, (800, 530), "THE XI AND THE SUBSTITUTES USE THE WHOLE ROSTER", 16, DIM, "mm")
    else:
        _text(d, (110, 204), "PLAYER", 13, MUTED, "la")
        _text(d, (700, 204), "ROLE", 13, MUTED, "la")
        for label, x, _fn in BENCH_COLUMNS:
            _text(d, (x, 204), label, 13, MUTED, "ma")
        _text(d, (1500, 204), "RATING", 13, MUTED, "ra")
        for i, p in enumerate(bench):
            y = 232 + i * 68
            color = ROLE_COLOR[p["role"]]
            d.rounded_rectangle((70, y, 1530, y + 60), radius=16, fill=BOX if i % 2 == 0 else (14, 24, 46))
            d.rounded_rectangle((70, y + 12, 76, y + 48), radius=3, fill=color)
            name, avatar = _who(side, p)
            ring = _round_image(avatar, 44, ring=color, ring_w=2, initial=name[:1])
            _paste(page.image, ring, (92, y + 8))
            _text(d, (152, y + 30), _clip(d, name, 460, 22, "head"), 22, WHITE, "lm", "head")
            _tag_pill(d, 640, y + 17, p.get("tag"), accent)
            _pill(d, 700, y + 15, p["label"].upper(), 12, fill=None, fg=color, outline=color, pad=10, h=30)
            for _label, x, fn in BENCH_COLUMNS:
                _text(d, (x, y + 30), fn(p), 20, WHITE, "mm", "head")
            _badge(d, 1472, y + 30, 22, p["value"], 20)
    _footer(page, side, 4, 4, "BENCH")
    return page.image


def _slot(p):
    return {"Opener": "OPENER", "Middle Order": "MIDDLE ORDER"}.get(p["label"], p["label"].upper())


def _page_order(side, theme):
    page = _Page(theme)
    accent = theme["accent"]
    left, right = (50, 170, 790, 852), (810, 170, 1550, 852)
    page.card(left, 26)
    page.card(right, 26)
    d = _header(page, side, "BATTING ORDER  •  BOWLING ORDER", 2, 4, theme)
    xi, attack = side["xi"], side.get("bowl_order", [])

    # ---- batting order
    x1, y1, x2, y2 = left
    _text(d, (x1 + 26, y1 + 18), "BATTING ORDER", 18, BAT_COLOR, "la", "head")
    if len(xi) >= 2:
        a, b = xi[0], xi[1]
        na, nb = _who(side, a)[0], _who(side, b)[0]
        note = (f"OPENING PAIR  {na.upper()} ({a['style'].upper()})  +  {nb.upper()} ({b['style'].upper()})")
        _text(d, (x1 + 26, y1 + 46), _clip(d, note, x2 - x1 - 52, 12, "bold"), 12, MUTED, "la", "bold")
    _text(d, (x2 - 112, y1 + 62), "AVG", 11, MUTED, "ma")
    _text(d, (x2 - 48, y1 + 62), "SR", 11, MUTED, "ma")
    pitch = 50
    for i, p in enumerate(xi[:11]):
        y = y1 + 80 + i * pitch
        color = ROLE_COLOR[p["role"]]
        name, avatar = _who(side, p)
        d.rounded_rectangle((x1 + 16, y, x2 - 16, y + pitch - 6), radius=14,
                            fill=BOX if i % 2 == 0 else (14, 24, 46), outline=accent if i < 2 else None, width=2)
        _text(d, (x1 + 40, y + (pitch - 6) / 2), str(p["order"]), 20, accent if i < 2 else DIM, "mm", "head")
        ring = _round_image(avatar, 34, ring=color, ring_w=2, initial=name[:1])
        _paste(page.image, ring, (x1 + 60, y + 5))
        tag = f" ({p['tag']})" if p.get("tag") else ""
        _text(d, (x1 + 104, y + (pitch - 6) / 2), _clip(d, name + tag, 190, 17, "head"), 17, WHITE, "lm", "head")
        label = _slot(p)
        pw = int(d.textlength(label, font=font(11))) + 18
        _pill(d, x1 + 306, y + 8, label, 11, fill=None, fg=color, outline=color, pad=9, h=28)
        _text(d, (x1 + 306 + pw + 10, y + (pitch - 6) / 2), p["style"].upper(), 11,
              HOT if p["style"] == "Aggressor" else (110, 170, 255) if p["style"] == "Anchor" else MUTED,
              "lm", "bold")
        avg = "—" if p["bat_avg"] is None else f"{p['bat_avg']:.1f}"
        sr = "—" if p["strike_rate"] is None else f"{p['strike_rate']:.0f}"
        _text(d, (x2 - 112, y + (pitch - 6) / 2), avg, 17, WHITE, "mm", "head")
        _text(d, (x2 - 48, y + (pitch - 6) / 2), sr, 17, WHITE, "mm", "head")

    # ---- bowling order
    x1, y1, x2, y2 = right
    _text(d, (x1 + 26, y1 + 18), "BOWLING ORDER", 18, BOWL_COLOR, "la", "head")
    if not attack:
        _text(d, ((x1 + x2) / 2, 480), "NO BOWLERS IN THE XI", 26, DIM, "mm", "head")
    else:
        opening = [p for p in attack if p["spell"] == "Opening"]
        if len(opening) == 2:
            note = (f"NEW BALL  {_who(side, opening[0])[0].upper()}  +  {_who(side, opening[1])[0].upper()}"
                    f"  ({opening[0]['bstyle'].upper()} / {opening[1]['bstyle'].upper()})")
            _text(d, (x1 + 26, y1 + 46), _clip(d, note, x2 - x1 - 52, 12, "bold"), 12, MUTED, "la", "bold")
        spells = [("OPENING", "Opening", "NEW BALL  •  WICKETS AND CONTROL", BOWL_COLOR),
                  ("MIDDLE OVERS", "Middle", "KEEP IT TIGHT, TAKE WICKETS", (110, 170, 255)),
                  ("DEATH", "Death", "LOWEST ECONOMY CLOSES IT", COLD)]
        y = y1 + 76
        for title, key, blurb, color in spells:
            group = [p for p in attack if p["spell"] == key]
            if not group:
                continue
            d.rounded_rectangle((x1 + 16, y, x2 - 16, y + 28), radius=10, fill=(20, 32, 58))
            d.rounded_rectangle((x1 + 16, y + 4, x1 + 22, y + 24), radius=3, fill=color)
            _text(d, (x1 + 36, y + 14), title, 13, color, "lm", "head")
            _text(d, (x2 - 30, y + 14), blurb, 11, MUTED, "rm", "bold")
            y += 36
            for p in group:
                name, avatar = _who(side, p)
                d.rounded_rectangle((x1 + 16, y, x2 - 16, y + 70), radius=14, fill=BOX)
                _text(d, (x1 + 40, y + 35), str(p["bowl_pos"]), 20, color, "mm", "head")
                ring = _round_image(avatar, 46, ring=color, ring_w=2, initial=name[:1])
                _paste(page.image, ring, (x1 + 62, y + 12))
                tag = f" ({p['tag']})" if p.get("tag") else ""
                _text(d, (x1 + 122, y + 24), _clip(d, name + tag, 190, 18, "head"), 18, WHITE, "lm", "head")
                _text(d, (x1 + 122, y + 50), p["bstyle"].upper(), 11, MUTED, "lm", "bold")
                cols = [("WKTS", str(p["wickets"])),
                        ("ECO", "—" if p["economy"] is None else f"{p['economy']:.1f}"),
                        ("AVG", "—" if p["bowl_avg"] is None else f"{p['bowl_avg']:.1f}"),
                        ("SR", "—" if p["bowl_sr"] is None else f"{p['bowl_sr']:.1f}")]
                for j, (label, value) in enumerate(cols):
                    cx = x1 + 372 + j * 82
                    _text(d, (cx, y + 18), label, 11, MUTED, "mm")
                    _text(d, (cx, y + 44), value, 20, WHITE, "mm", "head")
                y += 78
            y += 6
    _footer(page, side, 2, 4, "BATTING & BOWLING ORDER")
    return page.image


def render_xi_page(side, index):
    theme = side.setdefault("_theme", _theme(side))
    page = (_page_xi, _page_order, _page_subs, _page_bench)[index](side, theme)
    return _png(page)


# =========================================================================== +cxi

def _centre_bars(c, box, a, b):
    x1, y1, x2, y2 = box
    d = c.d
    _text(d, ((x1 + x2) / 2, y1 + 26), "XI STRENGTH", 16, MUTED, "mm", "bold")
    rows = [("OVERALL", "overall"), ("BATTING", "batting"), ("BOWLING", "bowling"),
            ("ALL-ROUND", "allround"), ("FIELDING", "fielding"), ("DEPTH", "depth")]
    pitch = (y2 - y1 - 130) / len(rows)
    for i, (label, key) in enumerate(rows):
        y = y1 + 66 + i * pitch
        va, vb = a["strength"][key], b["strength"][key]
        _text(d, ((x1 + x2) / 2, y), label, 14, MUTED, "mm", "bold")
        _text(d, (x1 + 24, y + 30), f"{va:.0f}", 30, c.a if va >= vb else MUTED, "lm", "head")
        _text(d, (x2 - 24, y + 30), f"{vb:.0f}", 30, c.b if vb > va else MUTED, "rm", "head")
        total = va + vb
        share = 0.5 if total <= 0 else min(0.93, max(0.07, va / total))
        _tug(d, x1 + 84, x2 - 84, y + 24, 10, share, c.a, c.b)
    diff = a["strength"]["overall"] - b["strength"]["overall"]
    if abs(diff) < 1.0:
        text, fill, fg = "EVENLY MATCHED", None, WHITE
    else:
        side, color = (a, c.a) if diff > 0 else (b, c.b)
        text, fill, fg = f"{_tag(side['name'])} AHEAD BY {abs(diff):.1f}", color, INK
    width = int(d.textlength(text, font=font(16))) + 36
    _pill(d, (x1 + x2) // 2 - width // 2, y2 - 54, text, 16, fill=fill, fg=fg, outline=None if fill else EDGE,
          pad=18, h=36)


def _xi_list(c, box, side, color, mirror):
    x1, y1, x2, y2 = box
    d = c.d
    xi = side["xi"]
    _text(d, (x2 - 22 if mirror else x1 + 22, y1 + 20), "BATTING ORDER", 14, color, "ra" if mirror else "la", "bold")
    pitch = (y2 - y1 - 52) / 11
    for i in range(11):
        y = y1 + 46 + i * pitch
        if i >= len(xi):
            d.rounded_rectangle((x1 + 14, y, x2 - 14, y + pitch - 6), radius=12, fill=(12, 20, 38))
            continue
        p = xi[i]
        role_color = ROLE_COLOR[p["role"]]
        name, avatar = _who(side, p)
        d.rounded_rectangle((x1 + 14, y, x2 - 14, y + pitch - 6), radius=12, fill=BOX if i % 2 == 0 else (14, 24, 46))
        ring = _round_image(avatar, 38, ring=role_color, ring_w=2, initial=name[:1])
        pill = SHORT[p["label"]]
        pw = int(d.textlength(pill, font=font(12))) + 20
        num = f"{p['order']:02d}"
        if not mirror:
            _text(d, (x1 + 30, y + (pitch - 6) / 2), num, 13, DIM, "lm", "head")
            _paste(c.image, ring, (x1 + 56, int(y + (pitch - 6 - 38) / 2)))
            _text(d, (x1 + 106, y + (pitch - 6) / 2),
                  _clip(d, name + (" (C)" if p.get("tag") == "C" else " (VC)" if p.get("tag") == "VC" else ""),
                        x2 - x1 - 106 - 150, 18, "head"), 18, WHITE, "lm", "head")
            _pill(d, x2 - 130 - pw, int(y + (pitch - 6) / 2 - 13), pill, 12, fill=None, fg=role_color,
                  outline=role_color, pad=10, h=26)
            _text(d, (x2 - 40, y + (pitch - 6) / 2), f"{p['value']:.0f}", 22, _rating_color(p["value"]), "rm", "head")
        else:
            _text(d, (x2 - 30, y + (pitch - 6) / 2), num, 13, DIM, "rm", "head")
            _paste(c.image, ring, (x2 - 56 - 38, int(y + (pitch - 6 - 38) / 2)))
            _text(d, (x2 - 106, y + (pitch - 6) / 2),
                  _clip(d, name + (" (C)" if p.get("tag") == "C" else " (VC)" if p.get("tag") == "VC" else ""),
                        x2 - x1 - 106 - 150, 18, "head"), 18, WHITE, "rm", "head")
            _pill(d, x1 + 130, int(y + (pitch - 6) / 2 - 13), pill, 12, fill=None, fg=role_color,
                  outline=role_color, pad=10, h=26)
            _text(d, (x1 + 40, y + (pitch - 6) / 2), f"{p['value']:.0f}", 22, _rating_color(p["value"]), "lm", "head")


def _page_cxi_main(a, b):
    col_a, col_b = pick_sides(a["color"], b["color"])
    c = _Canvas(col_a, col_b)
    left, right, mid = (50, 146, 610, 852), (990, 146, 1550, 852), (630, 146, 970, 852)
    c.card((50, 22, 1550, 126), 26)
    for box in (left, right, mid):
        c.card(box, 26)
    d = c.seal()
    for side, accent, x, anchor in ((a, c.a, 74, "la"), (b, c.b, 1526, "ra")):
        logo = _round_image(_open(side.get("logo")), 78, ring=accent, ring_w=4, initial=side["name"][:1])
        _paste(c.image, logo, (x if anchor == "la" else x - 78, 34))
        name = side["name"].upper()
        size = _fit(d, name, 380, 34, 18, "head")
        tx = x + 98 if anchor == "la" else x - 98
        _text(d, (tx, 50), _clip(d, name, 380, size, "head"), size, WHITE, anchor, "head")
        _text(d, (tx, 92), f"BEST XI  •  RATING {side['strength']['overall']:.0f}", 14, accent, anchor, "bold")
    _text(d, (800, 60), "XI  vs  XI", 34, WHITE, "mm", "head")
    _text(d, (800, 98), "BEST PLAYING XI COMPARISON", 13, MUTED, "mm", "bold")
    _xi_list(c, left, a, c.a, False)
    _xi_list(c, right, b, c.b, True)
    _centre_bars(c, mid, a, b)
    _text(d, (60, 874), f"{a.get('guild', '').upper()}  •  XI COMPARISON  •  1/3", 13, DIM)
    return c.image


def _best(side, key_fn, roles=None):
    pool = [p for p in side["xi"] if roles is None or p["role"] in roles]
    return max(pool, key=key_fn, default=None)


def _page_cxi_key(a, b):
    col_a, col_b = pick_sides(a["color"], b["color"])
    c = _Canvas(col_a, col_b)
    c.card((50, 22, 1550, 118), 26)
    rows = []
    row_h, gap, y0 = 150, 14, 138
    for i in range(4):
        rows.append((50, y0 + i * (row_h + gap), 1550, y0 + i * (row_h + gap) + row_h))
    for box in rows:
        c.card(box, 26)
    c.card((50, y0 + 4 * (row_h + gap), 1550, 852), 26)
    d = c.seal()
    _text(d, (800, 56), "KEY PLAYERS", 36, WHITE, "mm", "head")
    _text(d, (800, 96), f"{a['name'].upper()}  vs  {b['name'].upper()}", 14, MUTED, "mm", "bold")

    def line_bat(p):
        return f"AVG {'—' if p['bat_avg'] is None else format(p['bat_avg'], '.1f')}  •  RUNS {p['runs']:,}  •  MAT {p['matches']}"

    def line_bowl(p):
        return (f"WKTS {p['wickets']}  •  AVG {'—' if p['bowl_avg'] is None else format(p['bowl_avg'], '.1f')}"
                f"  •  SR {'—' if p['bowl_sr'] is None else format(p['bowl_sr'], '.1f')}")

    def line_ar(p):
        return (f"AVG {'—' if p['bat_avg'] is None else format(p['bat_avg'], '.1f')}  •  WKTS {p['wickets']}"
                f"  •  RUNS {p['runs']:,}")

    def line_field(p):
        return f"CATCHES {p['catches']}  •  DROPS {p['drops']}  •  MAT {p['matches']}"

    categories = [
        ("BEST BATTER", lambda s: _best(s, lambda p: p["bat"]), line_bat, lambda p: p["bat"], BAT_COLOR),
        ("BEST BOWLER", lambda s: _best(s, lambda p: p["bowl"] or 0, ("Bowler", "All-Rounder")), line_bowl,
         lambda p: p["bowl"] or 0, BOWL_COLOR),
        ("BEST ALL-ROUNDER", lambda s: _best(s, lambda p: min(p["bat"], p["bowl"] or 0), ("All-Rounder",)),
         line_ar, lambda p: min(p["bat"], p["bowl"] or 0), FIELD_COLOR),
        ("BEST FIELDER", lambda s: _best(s, lambda p: p["field"]), line_field, lambda p: p["field"], (255, 205, 70)),
    ]
    for box, (title, pick, line, score, color) in zip(rows, categories):
        x1, y1, x2, y2 = box
        pa, pb = pick(a), pick(b)
        sa = score(pa) if pa else None
        sb = score(pb) if pb else None
        _text(d, (800, y1 + 24), title, 16, color, "mm", "bold")
        for side, p, score_v, other, accent, sx, anchor in (
                (a, pa, sa, sb, c.a, x1 + 28, "la"), (b, pb, sb, sa, c.b, x2 - 28, "ra")):
            if p is None:
                _text(d, (sx, y1 + row_h / 2), "NO PLAYER", 20, DIM, anchor.replace("a", "m"))
                continue
            name, avatar = _who(side, p)
            ring = _round_image(avatar, 96, ring=accent, ring_w=4, initial=name[:1])
            px = sx if anchor == "la" else sx - 96
            _paste(c.image, ring, (px, y1 + 36))
            tx = sx + 118 if anchor == "la" else sx - 118
            _text(d, (tx, y1 + 46), _clip(d, name, 400, _fit(d, name, 400, 30, 18, "head"), "head"),
                  _fit(d, name, 400, 30, 18, "head"), WHITE, anchor, "head")
            _pill(d, tx if anchor == "la" else tx - (int(d.textlength(p["label"].upper(), font=font(12))) + 20),
                  y1 + 84, p["label"].upper(), 12, fill=None, fg=ROLE_COLOR[p["role"]],
                  outline=ROLE_COLOR[p["role"]], pad=10, h=26)
            _text(d, (tx, y1 + 120), line(p), 14, MUTED, anchor, "bold")
            lead = other is None or score_v > other
            bx = 690 if anchor == "la" else 910
            _text(d, (bx, y1 + row_h / 2 + 12), f"{score_v:.0f}", 52, _rating_color(score_v) if lead else MUTED,
                  "mm", "head")
    # verdict
    vx1, vy1, vx2, vy2 = 50, y0 + 4 * (row_h + gap), 1550, 852
    sa, sb = a["strength"], b["strength"]
    areas = (("batting", "BATTING"), ("bowling", "BOWLING"), ("allround", "ALL-ROUND"),
             ("fielding", "FIELDING"), ("depth", "DEPTH"))
    if abs(sa["overall"] - sb["overall"]) < 1.0:
        verdict = f"EVENLY MATCHED  •  OVERALL {sa['overall']:.0f} vs {sb['overall']:.0f}"
    else:
        lead, mine, theirs = (a, sa, sb) if sa["overall"] > sb["overall"] else (b, sb, sa)
        better = [label for key, label in areas if mine[key] > theirs[key]]
        verdict = (f"{lead['name'].upper()} RATED HIGHER  •  OVERALL {sa['overall']:.0f} vs {sb['overall']:.0f}"
                   f"  •  STRONGER IN {', '.join(better) if better else 'NOTHING ELSE'}")
    _text(d, (800, (vy1 + vy2) / 2), _clip(d, verdict, 1400, 20, "head"), 20, WHITE, "mm", "head")
    _text(d, (60, 874), f"{a.get('guild', '').upper()}  •  XI COMPARISON  •  2/3", 13, DIM)
    return c.image


def _rating_table(c, box, side, color, mirror):
    x1, y1, x2, y2 = box
    d = c.d
    name = side["name"].upper()
    _text(d, (x1 + 26, y1 + 18), _clip(d, name, 420, 20, "head"), 20, color, "la", "head")
    cols = [("BAT", 330), ("BOWL", 400), ("FIELD", 470), ("FORM", 540), ("RATING", 640)]
    for label, off in cols:
        _text(d, (x1 + off, y1 + 62), label, 11, MUTED, "mm")
    pitch = 44
    for i, p in enumerate(side["xi"][:11]):
        y = y1 + 82 + i * pitch
        who, avatar = _who(side, p)
        d.rounded_rectangle((x1 + 14, y, x2 - 14, y + pitch - 5), radius=12, fill=BOX if i % 2 == 0 else (14, 24, 46))
        ring = _round_image(avatar, 30, ring=ROLE_COLOR[p["role"]], ring_w=2, initial=who[:1])
        _paste(c.image, ring, (x1 + 24, y + 4))
        _text(d, (x1 + 64, y + (pitch - 5) / 2), _clip(d, who, 190, 16, "head"), 16, WHITE, "lm", "head")
        values = [p["bat"], p["bowl"], p["field"], p["form"]]
        for (label, off), value in zip(cols, values):
            if value is None:
                _text(d, (x1 + off, y + (pitch - 5) / 2), "—", 16, DIM, "mm", "head")
            else:
                _text(d, (x1 + off, y + (pitch - 5) / 2), f"{value:.0f}", 17, _rating_color(value), "mm", "head")
        _badge(d, x1 + 640, y + (pitch - 5) / 2, 18, p["value"], 16)


def _page_cxi_ratings(a, b):
    col_a, col_b = pick_sides(a["color"], b["color"])
    c = _Canvas(col_a, col_b)
    left, right = (50, 140, 790, 700), (810, 140, 1550, 700)
    c.card((50, 22, 1550, 120), 26)
    c.card(left, 26)
    c.card(right, 26)
    c.card((50, 716, 1550, 852), 26)
    d = c.seal()
    _text(d, (800, 56), "HOW EACH PLAYER IS RATED", 34, WHITE, "mm", "head")
    _text(d, (800, 94), "EVERY NUMBER IS 0-100, RANKED AGAINST EVERY PLAYER IN THE SERVER", 13, MUTED, "mm", "bold")
    _rating_table(c, left, a, c.a, False)
    _rating_table(c, right, b, c.b, True)
    lines = [
        ("BAT", "average, total runs and runs per match (strike rate only 5%), plus a little for matches played"),
        ("BOWL", "wickets, wickets per match, bowling average, bowling strike rate and economy"),
        ("FIELD", "catches, catches per match and catch rate (catches vs drops)"),
        ("FORM", "his last 5 matches against everyone else's last 5; it also moves BAT and BOWL a little"),
        ("RATING", "40% his best skill + 15% his other skill + 15% fantasy + 10% fielding + 12% form + 8% experience"),
    ]
    for i, (label, text) in enumerate(lines):
        y = 736 + i * 22
        _text(d, (80, y), label, 12, _rating_color(75), "la", "head")
        _text(d, (160, y), text, 12, MUTED, "la", "bold")
    _text(d, (60, 874), f"{a.get('guild', '').upper()}  •  XI COMPARISON  •  3/3", 13, DIM)
    return c.image


def render_cxi_page(a, b, index):
    page = (_page_cxi_main, _page_cxi_key, _page_cxi_ratings)[index](a, b)
    return _png(page)
