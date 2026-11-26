"""Images for +dream: the best XI of the whole server, picked from every team's players.

Page 1  the XI on a pitch (top order at the front, all-rounders in the middle, bowlers at the back)
Page 2  the numbers behind every pick, the next players in line, and which teams supplied them

data = {"guild", "xi": [player dicts from xi_engine], "subs": [...], "strength": {...},
        "people": {uid: {"name", "avatar"}}, "teams": {uid: {"name", "logo", "color"}},
        "team_counts": [(team name, logo, how many)], "players": int, "teams_total": int,
        "captain": uid, "vice": uid}
"""
from PIL import Image, ImageDraw

from .awards import _glow_text, _tile_glass
from .teaminfo_image import DIM, GOLD, MUTED, VIOLET, WHITE, _clip, _fit, _mix, _open, _paste, _pill, _round_image, _text, font
from .versus_image import _Canvas, _footer, _png, _title
from .xi_image import ROLE_COLOR, _badge, _rating_color, _stat_values

_INK = (12, 18, 32)
GRASS = (22, 78, 58)
DREAM_A, DREAM_B = (255, 190, 70), (150, 110, 255)
PAGES = ("Dream XI", "The numbers")


def _rows(xi):
    """Group the XI into 2-3 pitch rows: front = batters, middle = all-rounders, back = bowlers."""
    groups = [[p for p in xi if p["role"] == r] for r in ("Batsman", "All-Rounder", "Bowler")]
    rows = []
    for g in groups:
        g = sorted(g, key=lambda p: p["order"])
        while len(g) > 5:
            rows.append(g[:len(g) // 2 + len(g) % 2])
            g = g[len(g) // 2 + len(g) % 2:]
        if g:
            rows.append(g)
    while len(rows) > 4:                       # never more than 4 rows on the pitch
        last = rows.pop()
        rows[-1] += last
    return rows


def _pitch(c):
    """The ground, drawn on the background before the glass cards go over it."""
    box = (70, 126, 1110, 858)
    d = c.d
    d.ellipse(box, fill=_mix(GRASS, _INK, 0.45), outline=_mix(GRASS, WHITE, 0.35), width=3)
    inner = (box[0] + 70, box[1] + 56, box[2] - 70, box[3] - 56)
    d.ellipse(inner, outline=_mix(GRASS, WHITE, 0.22), width=2)
    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    d.rounded_rectangle((cx - 34, cy - 170, cx + 34, cy + 170), radius=8, fill=_mix(GRASS, (150, 130, 80), 0.35))
    for y in (cy - 150, cy + 150):
        d.line((cx - 34, y, cx + 34, y), fill=_mix(GRASS, WHITE, 0.55), width=2)


def _node(c, x, y, p, data, big=False):
    d = c.d
    people, teams = data["people"], data["teams"]
    name = people.get(p["uid"], {}).get("name") or f"Player {p['uid']}"
    color = ROLE_COLOR[p["role"]]
    size = 96 if big else 84
    ring = _round_image(_open(people.get(p["uid"], {}).get("avatar")), size, ring=color, ring_w=4, initial=name[:1])
    _paste(c.image, ring, (x - size // 2, y - size // 2))
    _badge(d, x + size // 2 - 6, y + size // 2 - 6, 21, p["value"], 20)
    if p["uid"] == data.get("captain"):
        _pill(d, x - size // 2 - 10, y - size // 2 - 6, "C", 14, fill=GOLD, fg=_INK, pad=9, h=26)
    elif p["uid"] == data.get("vice"):
        _pill(d, x - size // 2 - 18, y - size // 2 - 6, "VC", 13, fill=GOLD, fg=_INK, pad=8, h=24)
    ny = y + size // 2 + 22
    shown = _clip(d, name.upper(), 170, _fit(d, name.upper(), 170, 19, 13, "head"), "head")
    _text(d, (x, ny), shown, _fit(d, shown, 170, 19, 13, "head"), WHITE, "ma", "head")
    team = teams.get(p["uid"], {})
    _text(d, (x, ny + 25), _clip(d, (team.get("name") or "").upper(), 170, 12), 12, _mix(team.get("color") or MUTED, WHITE, 0.4), "ma", "bold")


def _page_xi(data):
    c = _Canvas(DREAM_A, DREAM_B)
    _pitch(c)
    c.card((50, 22, 1550, 106), 24, 170)
    c.card((1130, 126, 1550, 858), 26, 160)
    d = c.seal()
    _title(d, (76, 58), "DREAM XI", 50, WHITE, "lm", "head")
    for i in range(300):
        d.line((76 + i, 90, 76 + i, 93), fill=_mix(DREAM_A, DREAM_B, i / 299))
    _text(d, (1524, 44), f"BEST OF {data['players']} PLAYERS FROM {data['teams_total']} TEAMS", 16, MUTED, "rm")
    _text(d, (1524, 66), str(data.get("guild", "")).upper(), 13, DIM, "rm")
    xi = data["xi"]
    rows = _rows(xi)
    if not rows:
        _text(d, (590, 480), "NOT ENOUGH PLAYER DATA YET", 28, DIM, "mm", "head")
    n = len(rows)
    ys = [int(150 + (i + 0.5) * (640 / n)) for i in range(n)] if n else []
    for ry, row in zip(ys, rows):
        span = 940 if n else 0
        for i, p in enumerate(row):
            x = int(590 - span / 2 + (i + 0.5) * span / len(row))
            _node(c, x, ry, p, data)
    # right panel
    x1, y1, x2, y2 = 1130, 126, 1550, 858
    _tile_glass(c, (x1, y1, x2, y2), DREAM_A)
    st = data["strength"]
    _text(d, (x1 + 28, y1 + 24), "TEAM RATING", 14, MUTED)
    _glow_text(c, (x1 + 28, y1 + 44), f"{st['overall']:.0f}", 84, _rating_color(st["overall"]))
    for i, (label, key) in enumerate((("BATTING", "batting"), ("BOWLING", "bowling"), ("FIELDING", "fielding"))):
        y = y1 + 168 + i * 40
        _text(d, (x1 + 28, y), label, 13, MUTED, "la", "bold")
        d.rounded_rectangle((x1 + 130, y + 4, x2 - 90, y + 14), radius=5, fill=(26, 38, 64))
        d.rounded_rectangle((x1 + 130, y + 4, x1 + 130 + int((x2 - 90 - x1 - 130) * min(100, st[key]) / 100), y + 14),
                            radius=5, fill=_rating_color(st[key]))
        _text(d, (x2 - 28, y), f"{st[key]:.0f}", 18, _rating_color(st[key]), "ra", "head")
    _text(d, (x1 + 28, y1 + 310), "WHERE THEY COME FROM", 14, MUTED)
    for i, (name, logo, count) in enumerate(data["team_counts"][:7]):
        y = y1 + 342 + i * 50
        ring = _round_image(_open(logo), 34, ring=DREAM_B, ring_w=2, initial=name[:1])
        _paste(c.image, ring, (x1 + 28, y))
        _text(d, (x1 + 78, y + 17), _clip(d, name.upper(), 220, 16, "head"), 16, WHITE, "lm", "head")
        _text(d, (x2 - 28, y + 17), f"{count} PLAYER{'S' if count != 1 else ''}", 14, GOLD, "rm", "bold")
    _footer(c, "GOOGLI MANAGER  •  DREAM XI", data.get("guild"))
    return c.image


def _page_numbers(data):
    c = _Canvas(DREAM_A, DREAM_B)
    c.card((50, 22, 1550, 106), 24, 170)
    c.card((50, 126, 1550, 858), 26, 160)
    d = c.seal()
    _title(d, (76, 58), "THE NUMBERS", 50, WHITE, "lm", "head")
    _text(d, (1524, 52), "WHY EACH PLAYER MADE THE DREAM XI", 16, MUTED, "rm")
    x1, y1, x2, y2 = 50, 126, 1550, 858
    _tile_glass(c, (x1, y1, x2, y2), DREAM_A)
    for label, x in (("#", 84), ("PLAYER", 130), ("TEAM", 520), ("ROLE", 760), ("KEY NUMBERS", 930), ("RATING", 1480)):
        _text(d, (x1 + x - 50, y1 + 24), label, 13, MUTED, "la" if label != "RATING" else "ra", "bold")
    xi = data["xi"]
    step = min(52, (y2 - y1 - 120 - 130) // max(1, len(xi)))
    for i, p in enumerate(xi):
        y = y1 + 60 + i * step
        if i:
            d.line((x1 + 28, y - 2, x2 - 28, y - 2), fill=_mix(DREAM_A, _INK, 0.85), width=1)
        info = data["people"].get(p["uid"], {})
        name = info.get("name") or f"Player {p['uid']}"
        team = data["teams"].get(p["uid"], {})
        color = ROLE_COLOR[p["role"]]
        mid = y + step // 2
        _text(d, (x1 + 34, mid), str(p["order"]), 18, GOLD, "lm", "head")
        ring = _round_image(_open(info.get("avatar")), 34, ring=color, ring_w=2, initial=name[:1])
        _paste(c.image, ring, (x1 + 80, mid - 17))
        tag = "  (C)" if p["uid"] == data.get("captain") else "  (VC)" if p["uid"] == data.get("vice") else ""
        _text(d, (x1 + 128, mid), _clip(d, name.upper() + tag, 330, 17, "head"), 17, WHITE, "lm", "head")
        _text(d, (x1 + 470, mid), _clip(d, (team.get("name") or "").upper(), 230, 15), 15, _mix(team.get("color") or MUTED, WHITE, 0.4), "lm", "bold")
        _pill(d, x1 + 710, mid - 14, p["label"].upper(), 12, fill=None, fg=color, outline=color, pad=10, h=28)
        text = "   ".join(f"{k} {v}" for k, v in _stat_values(p))
        _text(d, (x1 + 880, mid), text, 15, WHITE, "lm", "bold")
        _text(d, (x2 - 30, mid), f"{p['value']:.0f}", 22, _rating_color(p["value"]), "rm", "head")
    sy = y1 + 60 + len(xi) * step + 18
    _text(d, (x1 + 34, sy), "NEXT IN LINE", 14, MUTED, "la", "bold")
    for i, p in enumerate(data["subs"][:4]):
        info = data["people"].get(p["uid"], {})
        name = info.get("name") or f"Player {p['uid']}"
        team = data["teams"].get(p["uid"], {})
        bx = x1 + 34 + i * 360
        d.rounded_rectangle((bx, sy + 28, bx + 340, sy + 90), radius=14, fill=(18, 29, 54))
        ring = _round_image(_open(info.get("avatar")), 40, ring=ROLE_COLOR[p["role"]], ring_w=2, initial=name[:1])
        _paste(c.image, ring, (bx + 12, sy + 39))
        _text(d, (bx + 64, sy + 46), _clip(d, name.upper(), 190, 15, "head"), 15, WHITE, "la", "head")
        _text(d, (bx + 64, sy + 68), _clip(d, (team.get("name") or "").upper(), 190, 11), 11, MUTED)
        _text(d, (bx + 326, sy + 59), f"{p['value']:.0f}", 22, _rating_color(p["value"]), "rm", "head")
    _footer(c, "GOOGLI MANAGER  •  DREAM XI", data.get("guild"))
    return c.image


def render_dream_page(data, index):
    return _png((_page_xi, _page_numbers)[max(0, min(1, index))](data))
