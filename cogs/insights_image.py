"""Images for +compareplayers, +form, +awards, +similar, +strengths, +whatif and +streaks.

All 1600x900, pure Pillow, built from the same pieces as +cxi / +wp. Data dicts come from
cogs/insights.py; players are the rated profiles of cogs/xi_engine.py.
"""
from .teaminfo_image import (
    DIM, LOSS, MUTED, W, WHITE, WIN, _clip, _fit, _open, _paste, _pill, _round_image, _tag, _text, font,
)
from .versus_image import (
    BAT_COLOR, BOWL_COLOR, EDGE, FIELD_COLOR, INK, RECORD_COLOR, TRACK, _Canvas, _draw_row, _footer, _png,
    _title, _verdict, fmt_value, pick_sides, stat_row,
)
from .xi_image import BOX, _badge, _rating_color

HOT, COLD = (62, 224, 140), (255, 96, 120)
GOLD = (255, 205, 70)


def _canvas(color_a, color_b=None):
    a, b = pick_sides(color_a, color_b) if color_b is not None else pick_sides(color_a, None)
    return _Canvas(a, b)


def _avatar(c, who, size, xy, accent):
    ring = _round_image(_open(who.get("avatar")), size, ring=accent, ring_w=5, initial=who["name"][:1])
    _paste(c.image, ring, xy)


def _name(d, text, x, y, room, size, anchor="la"):
    text = text.upper()
    s = _fit(d, text, room, size, 20, "head")
    _title(d, (x, y), _clip(d, text, room, s, "head"), s, WHITE, anchor, "head")


def _team_pill(d, x, y, team, accent, anchor="la", size=16):
    text = _clip(d, (team or "FREE AGENT").upper(), 300, size)
    width = int(d.textlength(text, font=font(size))) + 32
    _pill(d, x if anchor == "la" else x - width, y, text, size, fg=WHITE, outline=accent, pad=16, h=34)


def _f(v, dec=1):
    return None if v is None else round(v, dec)


def _role_text(p):
    return p["role"].upper()


# =========================================================================== +compareplayers

def render_compare_players(data):
    A, B = data["a"], data["b"]
    pa, pb = A["p"], B["p"]
    c = _canvas(A.get("color"), B.get("color"))
    rows = {
        "BATTING": (BAT_COLOR, [
            stat_row("Runs", pa["runs"], pb["runs"]),
            stat_row("Average", _f(pa["bat_avg"]), _f(pb["bat_avg"]), "num1"),
            stat_row("Strike rate", _f(pa["strike_rate"]), _f(pb["strike_rate"]), "num1", better=None),
            stat_row("Matches", pa["matches"], pb["matches"], better=None),
            stat_row("Runs / match (L5)", _f(pa["r_rpm"]), _f(pb["r_rpm"]), "num1"),
        ]),
        "BOWLING": (BOWL_COLOR, [
            stat_row("Wickets", pa["wickets"], pb["wickets"]),
            stat_row("Bowling avg", _f(pa["bowl_avg"]), _f(pb["bowl_avg"]), "num1", better="low"),
            stat_row("Economy", _f(pa["economy"]), _f(pb["economy"]), "num2", better="low"),
            stat_row("Bowling SR", _f(pa["bowl_sr"]), _f(pb["bowl_sr"]), "num1", better="low"),
            stat_row("Wkts / match (L5)", _f(pa["r_wpm"]), _f(pb["r_wpm"]), "num2"),
        ]),
        "FIELDING & FORM": (FIELD_COLOR, [
            stat_row("Catches", pa["catches"], pb["catches"]),
            stat_row("Catch rate", _f(pa["catch_eff"], 0), _f(pb["catch_eff"], 0), "pct"),
            stat_row("Drops", pa["drops"], pb["drops"], better="low"),
            stat_row("Fantasy pts", pa["fantasy"], pb["fantasy"]),
            stat_row("Form rating", pa["form"], pb["form"], "int"),
        ]),
    }
    gap, left, right = 20, 50, 1550
    tw = (right - left - gap * 2) // 3
    boxes = [(left + i * (tw + gap), 392, left + i * (tw + gap) + tw, 800) for i in range(3)]
    c.card((50, 262, 1550, 372), 24)
    for box in boxes:
        c.card(box, 24)
    c.seal()
    d = c.d

    for who, left_side, accent in ((A, True, c.a), (B, False, c.b)):
        ax = 70 if left_side else W - 70 - 190
        _avatar(c, who, 190, (ax, 40), accent)
        tx = ax + 190 + 26 if left_side else ax - 26
        anchor = "la" if left_side else "ra"
        _name(d, who["name"], tx, 62, 400, 52, anchor)
        _team_pill(d, tx if left_side else tx, 136, who.get("team"), accent, "la" if left_side else "ra")
        _text(d, (tx, 190), f"{_role_text(who['p'])}  •  {who['p']['style'].upper()}", 15, MUTED, anchor)
        _text(d, (tx, 214), f"{who['p']['matches']} MATCHES", 15, DIM, anchor)
    _title(d, (W // 2, 120), "VS", 44, MUTED, "mm", "head")
    big_a, big_b = pa["value"], pb["value"]
    _title(d, (W // 2 - 70, 188), f"{big_a:.0f}", 70, c.a if big_a >= big_b else MUTED, "rm", "head")
    _title(d, (W // 2 + 70, 188), f"{big_b:.0f}", 70, c.b if big_b > big_a else MUTED, "lm", "head")
    _text(d, (W // 2, 244), "OVERALL RATING", 13, DIM, "mm")

    # rating strip: five columns, both values, winner lit
    cols = [("BATTING", "bat"), ("BOWLING", "bowl"), ("FIELDING", "field"), ("FORM", "form"), ("FANTASY", "fantasy_rating")]
    step = (1500 - 40) / 5
    for i, (label, key) in enumerate(cols):
        cx = 70 + step * i + step / 2
        va, vb = pa.get(key), pb.get(key)
        _text(d, (cx, 290), label, 14, MUTED, "mm")
        for val, other, dx, col in ((va, vb, -62, c.a), (vb, va, 62, c.b)):
            if val is None:
                _text(d, (cx + dx, 328), "—", 34, DIM, "mm", "head")
            else:
                win = other is None or val > other
                _text(d, (cx + dx, 328), f"{val:.0f}", 38, col if win else MUTED, "mm", "head")
        _text(d, (cx, 328), "|", 24, EDGE, "mm")

    wins_a = wins_b = 0
    for (label, (color, rws)), box in zip(rows.items(), boxes):
        x1, y1, x2, y2 = box
        d.ellipse((x1 + 24, y1 + 25, x1 + 36, y1 + 37), fill=color)
        _text(d, (x1 + 48, y1 + 20), label, 22, color, "la", "head")
        for i, row in enumerate(rws):
            _draw_row(d, x1, x2, y1 + 62 + i * 70, row, c.a, c.b, pad=24)
            v = _verdict(row)
            wins_a += v > 0
            wins_b += v < 0
    if wins_a == wins_b:
        text = f"EVEN  {wins_a} – {wins_b}"
    else:
        lead = A if wins_a > wins_b else B
        text = f"{lead['name'].upper()} LEADS  {max(wins_a, wins_b)} – {min(wins_a, wins_b)}"
    text = _clip(d, text, 700, 20)
    pw = int(d.textlength(text, font=font(20))) + 44
    _pill(d, W // 2 - pw // 2, 812, text, 20, fill=(c.a if wins_a > wins_b else c.b) if wins_a != wins_b else None,
          fg=INK if wins_a != wins_b else WHITE, outline=None if wins_a != wins_b else EDGE, pad=22, h=44)
    _footer(c, f"GOOGLI MANAGER  •  PLAYER COMPARISON  •  {A['name']} vs {B['name']}"[:90], data.get("guild"))
    return _png(c.image)


# =========================================================================== +form

def render_form(data):
    who, p = data["who"], data["who"]["p"]
    c = _canvas(who.get("color"), None)
    matches = data["matches"]                      # oldest to newest, each {label, bat, bowl, fantasy}
    boxes = [(50 + i * 296, 360, 50 + i * 296 + 276, 640) for i in range(5)]
    c.card((50, 30, 1550, 330), 26)
    for b in boxes:
        c.card(b, 22)
    c.card((50, 660, 1550, 848), 24)
    c.seal()
    d = c.d
    _avatar(c, who, 200, (80, 80), c.a)
    _name(d, who["name"], 310, 70, 620, 56)
    _team_pill(d, 310, 146, who.get("team"), c.a)
    _text(d, (310, 196), f"{_role_text(p)}  •  {p['style'].upper()}", 16, MUTED)
    _text(d, (310, 222), f"{p['matches']} MATCHES  •  LAST {p['r_matches']} COUNTED", 15, DIM)
    delta = p.get("form_delta", 0)
    color = HOT if delta >= 12 else COLD if delta <= -12 else (150, 165, 195)
    word = "HOT" if delta >= 12 else "COLD" if delta <= -12 else "STEADY"
    mark = "▲" if delta >= 12 else "▼" if delta <= -12 else "●"
    _text(d, (1470, 64), "FORM RATING", 15, MUTED, "ra")
    _title(d, (1470, 80), f"{p['form']:.0f}", 130, _rating_color(p["form"]), "ra", "head")
    _pill(d, 1470 - 190, 232, f"{mark} {word}", 22, fill=color, fg=INK, pad=22, h=44)
    _text(d, (1470, 292), f"{delta:+.0f} vs season fantasy rating", 14, DIM, "ra")

    top = max([abs(m["fantasy"]) for m in matches] + [1])
    for i, box in enumerate(boxes):
        x1, y1, x2, y2 = box
        if i >= len(matches):
            _text(d, ((x1 + x2) // 2, (y1 + y2) // 2), "NO MATCH", 18, DIM, "mm")
            continue
        m = matches[i]
        _text(d, (x1 + 20, y1 + 18), m["label"], 15, color if i == len(matches) - 1 else MUTED, "la", "bold")
        _text(d, (x1 + 20, y1 + 56), "BATTING", 12, MUTED)
        _text(d, (x1 + 20, y1 + 74), m["bat"], 28, BAT_COLOR, "la", "head")
        _text(d, (x1 + 20, y1 + 124), "BOWLING", 12, MUTED)
        _text(d, (x1 + 20, y1 + 142), m["bowl"], 28, BOWL_COLOR, "la", "head")
        _text(d, (x1 + 20, y1 + 192), "FANTASY", 12, MUTED)
        _text(d, (x1 + 20, y1 + 208), f"{m['fantasy']:.0f}", 26, WHITE, "la", "head")
        d.rounded_rectangle((x1 + 20, y2 - 22, x2 - 20, y2 - 12), radius=5, fill=TRACK)
        w = int((x2 - x1 - 40) * max(0, m["fantasy"]) / top)
        if w > 0:
            d.rounded_rectangle((x1 + 20, y2 - 22, x1 + 20 + max(10, w), y2 - 12), radius=5, fill=color)

    tiles = [
        ("RUNS / MATCH", p["runs"] / max(1, p["matches"]) if p["bat_inn"] else None, p["r_rpm"], 1, True),
        ("BAT AVERAGE", p["bat_avg"], p["r_avg"], 1, True),
        ("WKTS / MATCH", p["wickets"] / max(1, p["matches"]) if p["balls_bowled"] >= 6 else None, p["r_wpm"], 2, True),
        ("ECONOMY", p["economy"], p["r_econ"], 2, False),
    ]
    _text(d, (80, 680), "SEASON  →  LAST 5", 15, MUTED)
    for i, (label, season, recent, dec, high) in enumerate(tiles):
        cx = 80 + i * 360
        _text(d, (cx, 716), label, 14, MUTED)
        s = "—" if season is None else f"{season:.{dec}f}"
        r = "—" if recent is None else f"{recent:.{dec}f}"
        _text(d, (cx, 748), s, 34, MUTED, "la", "head")
        sw = int(d.textlength(s, font=font(34, "head")))
        _text(d, (cx + sw + 14, 756), "→", 26, DIM, "la", "head")
        better = None if season is None or recent is None else (recent > season if high else recent < season)
        col = WHITE if better is None else (HOT if better else COLD)
        _text(d, (cx + sw + 52, 748), r, 40, col, "la", "head")
        if better is not None and recent != season:
            _text(d, (cx, 800), ("▲ IMPROVING" if better else "▼ DROPPING"), 13, col, "la", "bold")
    _footer(c, f"GOOGLI MANAGER  •  PLAYER FORM  •  {who['name']}"[:90], data.get("guild"))
    return _png(c.image)


# =========================================================================== +awards

def render_awards(data):
    c = _canvas((46, 134, 222), (255, 150, 80))
    awards = data["awards"][:6]
    cw, ch, gap = 480, 366, 30
    boxes = []
    for i in range(6):
        x1 = 50 + (i % 3) * (cw + gap + 10)
        y1 = 130 + (i // 3) * (ch + 20)
        boxes.append((x1, y1, x1 + cw + 10, y1 + ch))
    c.card((50, 24, 1550, 106), 24)
    for b in boxes[:len(awards)]:
        c.card(b, 24)
    c.seal()
    d = c.d
    _title(d, (80, 38), "SEASON AWARDS", 46, WHITE, "la", "head")
    _text(d, (1520, 56), f"{data['guild'].upper()}  •  MAIN TOURNAMENT", 15, MUTED, "ra")
    for a, box in zip(awards, boxes):
        x1, y1, x2, y2 = box
        col = a["color"]
        d.ellipse((x1 + 24, y1 + 25, x1 + 36, y1 + 37), fill=col)
        _text(d, (x1 + 48, y1 + 20), a["title"], 22, col, "la", "head")
        _text(d, (x1 + 24, y1 + 58), a["subtitle"].upper(), 13, MUTED)
        if a.get("who") is None:
            _text(d, ((x1 + x2) // 2, (y1 + y2) // 2 + 20), "NOT ENOUGH DATA", 18, DIM, "mm")
            continue
        who = a["who"]
        _avatar(c, who, 120, (x1 + 24, y1 + 96), col)
        _name(d, who["name"], x1 + 164, y1 + 108, x2 - x1 - 188, 34)
        _team_pill(d, x1 + 164, y1 + 154, who.get("team"), col, size=14)
        _title(d, (x1 + 24, y1 + 236), a["value"], 66, WHITE, "la", "head")
        _text(d, (x1 + 24, y1 + 316), a["extra"].upper(), 14, MUTED)
    _footer(c, "GOOGLI MANAGER  •  SEASON AWARDS", data.get("guild"))
    return _png(c.image)


# =========================================================================== +similar

def render_similar(data):
    who, p = data["who"], data["who"]["p"]
    c = _canvas(who.get("color"), None)
    c.card((50, 40, 560, 848), 26)
    for i in range(5):
        c.card((590, 40 + i * 162, 1550, 40 + i * 162 + 148), 22)
    c.seal()
    d = c.d
    _text(d, (80, 66), "PLAYERS LIKE", 16, MUTED)
    _avatar(c, who, 220, (205, 100), c.a)
    _name(d, who["name"], 305, 350, 440, 44, "ma")
    _team_pill(d, 305 - int(d.textlength(_clip(d, (who.get("team") or "FREE AGENT").upper(), 300, 16), font=font(16))) // 2 - 16,
               402, who.get("team"), c.a)
    _text(d, (305, 462), f"{_role_text(p)}  •  {p['style'].upper()}", 15, MUTED, "mm")
    for i, (label, key) in enumerate((("BAT", "bat"), ("BOWL", "bowl"), ("FIELD", "field"))):
        cx = 120 + i * 185
        v = p.get(key)
        if v is None:
            d.ellipse((cx - 36, 536, cx + 36, 608), fill=(10, 16, 30), outline=EDGE, width=3)
            _text(d, (cx, 572), "—", 24, DIM, "mm", "head")
        else:
            _badge(d, cx, 572, 36, v, 30)
        _text(d, (cx, 630), label, 13, MUTED, "mm")
    _text(d, (305, 700), "MATCHED ON", 13, DIM, "mm")
    _text(d, (305, 724), "batting, bowling, fielding", 15, MUTED, "mm")
    _text(d, (305, 746), "form and playing style", 15, MUTED, "mm")
    _text(d, (305, 800), f"{p['matches']} MATCHES PLAYED", 14, DIM, "mm")
    for i, other in enumerate(data["others"][:5]):
        y = 40 + i * 162
        o = other["who"]
        op = o["p"]
        _text(d, (622, y + 16), f"#{i + 1}", 22, c.a, "la", "head")
        _avatar(c, o, 100, (680, y + 24), c.b)
        _name(d, o["name"], 806, y + 22, 400, 34)
        _team_pill(d, 806, y + 70, o.get("team"), c.b, size=14)
        _text(d, (806, y + 112), f"{_role_text(op)}  •  {op['style'].upper()}  •  {op['matches']} MATCHES", 13, MUTED)
        for j, (label, key) in enumerate((("BAT", "bat"), ("BOWL", "bowl"), ("FIELD", "field"))):
            cx = 1280 + j * 66
            v = op.get(key)
            if v is None:
                d.ellipse((cx - 24, y + 32, cx + 24, y + 80), fill=(10, 16, 30), outline=EDGE, width=2)
                _text(d, (cx, y + 56), "—", 18, DIM, "mm", "head")
            else:
                _badge(d, cx, y + 56, 24, v, 20)
            _text(d, (cx, y + 94), label, 11, MUTED, "mm")
        _text(d, (1524, y + 16), "MATCH", 11, MUTED, "ra")
        _text(d, (1524, y + 30), f"{other['sim']:.0f}%", 28, GOLD if other["sim"] >= 90 else WHITE, "ra", "head")
        if other.get("same_team"):
            _pill(d, 1524 - 120, y + 112, "TEAMMATE", 11, fill=c.b, fg=INK, pad=10, h=22)
    _footer(c, f"GOOGLI MANAGER  •  SIMILAR PLAYERS  •  {who['name']}"[:90], data.get("guild"))
    return _png(c.image)


# =========================================================================== +strengths

def render_strengths(data):
    t = data["team"]
    c = _canvas(t.get("color"), None)
    c.card((50, 24, 1550, 170), 26)
    c.card((50, 190, 940, 848), 24)
    c.card((960, 190, 1550, 500), 24)
    c.card((960, 520, 1550, 848), 24)
    c.seal()
    d = c.d
    logo = _round_image(_open(t.get("logo")), 110, ring=c.a, ring_w=4, initial=t["name"][:1])
    _paste(c.image, logo, (76, 42))
    _name(d, t["name"], 212, 48, 900, 52)
    _text(d, (212, 118), f"SCOUTING REPORT  •  {data['roster']} PLAYERS  •  XI RATING {t['overall']:.0f}", 16, MUTED)
    _pill(d, 1550 - 24 - 220, 52, data["verdict"], 18, fill=c.a, fg=INK, pad=20, h=40)

    _text(d, (84, 214), "CATEGORY STRENGTH (XI RATING, RANK IN SERVER)", 15, MUTED)
    for i, cat in enumerate(data["cats"]):
        y = 256 + i * 116
        _text(d, (84, y), cat["label"], 22, cat["color"], "la", "head")
        _text(d, (84, y + 34), cat["note"].upper(), 12, MUTED)
        d.rounded_rectangle((300, y + 10, 760, y + 32), radius=11, fill=TRACK)
        w = int(460 * min(100, cat["value"]) / 100)
        d.rounded_rectangle((300, y + 10, 300 + max(24, w), y + 32), radius=11, fill=_rating_color(cat["value"]))
        _text(d, (780, y + 8), f"{cat['value']:.0f}", 34, _rating_color(cat["value"]), "la", "head")
        tag, fill, fg = cat["tag"]
        _pill(d, 840, y + 8, tag, 14, fill=fill, fg=fg, outline=None if fill else EDGE, pad=12, h=32)
        _text(d, (780, y + 56), f"#{cat['rank']} OF {cat['of']}", 13, DIM)

    _text(d, (990, 214), "KEY-PLAYER DEPENDENCE", 15, MUTED)
    dep = data["dependence"]
    for i, (label, share, who) in enumerate(dep):
        y = 250 + i * 80
        _text(d, (990, y), label, 14, MUTED)
        col = COLD if share >= 60 else (GOLD if share >= 45 else HOT)
        _text(d, (990, y + 20), f"{share:.0f}%", 36, col, "la", "head")
        _text(d, (1100, y + 30), _clip(d, f"from {who}", 420, 15), 15, WHITE)
    _pill(d, 990, 456, data["risk"][0], 15, fill=data["risk"][1], fg=INK, pad=16, h=32)

    _text(d, (990, 540), "KEY PLAYERS", 15, MUTED)
    for i, k in enumerate(data["key"][:3]):
        y = 574 + i * 90
        _avatar(c, k["who"], 70, (990, y), c.a)
        _name(d, k["who"]["name"], 1076, y + 4, 330, 26)
        _text(d, (1076, y + 42), k["line"].upper(), 12, MUTED)
        _badge(d, 1500, y + 35, 30, k["p"]["value"], 24)
    _footer(c, f"GOOGLI MANAGER  •  SCOUTING REPORT  •  {t['name']}"[:90], data.get("guild"))
    return _png(c.image)


# =========================================================================== +whatif

def render_whatif(data):
    A, B = data["a"], data["b"]
    c = _canvas(A.get("color"), B.get("color"))
    c.card((50, 30, 1550, 290), 26)
    c.card((50, 310, 790, 700), 24)
    c.card((810, 310, 1550, 700), 24)
    c.card((50, 720, 1550, 848), 24)
    c.seal()
    d = c.d
    _text(d, (W // 2, 60), "WHAT IF", 16, MUTED, "mm")
    _title(d, (W // 2, 112), _clip(d, data["title"].upper(), 1300, 40, "head"), 40, WHITE, "mm", "head")
    _text(d, (W // 2, 160), data["subtitle"].upper(), 16, MUTED, "mm")
    for who, x in ((A, 400), (B, 1200)):
        pass
    pa0, pa1, pb0, pb1 = data["chance"]
    _text(d, (W // 2, 214), "WIN CHANCE IF THESE TWO MEET", 13, DIM, "mm")
    _title(d, (W // 2 - 40, 262), f"{pa0:.0f}% → {pa1:.0f}%", 36, c.a, "rm", "head")
    _title(d, (W // 2 + 40, 262), f"{pb0:.0f}% → {pb1:.0f}%", 36, c.b, "lm", "head")
    for team, box, accent, side in ((A, (50, 310, 790, 700), c.a, "a"), (B, (810, 310, 1550, 700), c.b, "b")):
        x1, y1, x2, y2 = box
        logo = _round_image(_open(team.get("logo")), 84, ring=accent, ring_w=4, initial=team["name"][:1])
        _paste(c.image, logo, (x1 + 24, y1 + 20))
        _name(d, team["name"], x1 + 126, y1 + 28, 400, 34)
        _text(d, (x1 + 126, y1 + 76), team["change"].upper(), 14, accent, "la", "bold")
        for i, (label, before, after) in enumerate(team["rows"]):
            y = y1 + 130 + i * 56
            _text(d, (x1 + 28, y), label, 16, MUTED, "la", "bold")
            diff = after - before
            col = HOT if diff > 0.4 else COLD if diff < -0.4 else WHITE
            _text(d, (x1 + 250, y - 6), f"{before:.0f}", 30, MUTED, "la", "head")
            _text(d, (x1 + 318, y - 2), "→", 24, DIM, "la", "head")
            _text(d, (x1 + 370, y - 6), f"{after:.0f}", 30, col, "la", "head")
            _text(d, (x1 + 460, y), f"{diff:+.1f}", 18, col, "la", "bold")
            d.rounded_rectangle((x1 + 540, y + 4, x2 - 28, y + 20), radius=8, fill=TRACK)
            d.rounded_rectangle((x1 + 540, y + 4, x1 + 540 + max(12, int((x2 - x1 - 568) * min(100, after) / 100)), y + 20),
                                radius=8, fill=col if col != WHITE else accent)
    _text(d, (80, 744), "WHAT IT MEANS", 14, MUTED)
    for i, line in enumerate(data["notes"][:3]):
        _text(d, (80, 772 + i * 26), "• " + _clip(d, line, 1380, 17), 17, WHITE)
    _footer(c, "GOOGLI MANAGER  •  WHAT IF  •  XI RATINGS, NOT A GUARANTEE", data.get("guild"))
    return _png(c.image)


# =========================================================================== +streaks

def _chip(d, x, y, streak, accent):
    kind, n = streak
    if not n:
        _text(d, (x, y), "—", 18, DIM, "lm")
        return
    col = HOT if kind == "W" else COLD
    _pill(d, x, y - 15, f"{kind}{n}", 16, fill=col, fg=INK, pad=14, h=30)


def render_streaks(data):
    c = _canvas((46, 134, 222), (255, 140, 90))
    c.card((50, 24, 1550, 100), 24)
    c.card((50, 116, 1000, 848), 24)
    c.card((1020, 116, 1550, 848), 24)
    c.seal()
    d = c.d
    _title(d, (80, 36), "STREAKS", 44, WHITE, "la", "head")
    _text(d, (1520, 56), data["guild"].upper(), 15, MUTED, "ra")
    heads = (("TEAM", 80), ("NOW", 400), ("BEST W", 520), ("WORST L", 640), ("LAST 5", 790))
    for label, x in heads:
        _text(d, (x, 138), label, 13, MUTED)
    teams = data["teams"][:12]
    step = min(52, (848 - 170 - 20) // max(1, len(teams)))
    for i, t in enumerate(teams):
        y = 186 + i * step
        if i % 2 == 0:
            d.rounded_rectangle((60, y - step // 2 + 2, 990, y + step // 2 - 2), radius=10, fill=(255, 255, 255, 14)
                                if False else BOX)
        _text(d, (80, y), _clip(d, t["name"].upper(), 290, 17), 17, WHITE, "lm", "bold")
        _chip(d, 400, y, t["now"], c.a)
        _text(d, (520, y), str(t["best_w"]) if t["best_w"] else "—", 20, HOT if t["best_w"] else DIM, "lm", "head")
        _text(d, (640, y), str(t["worst_l"]) if t["worst_l"] else "—", 20, COLD if t["worst_l"] else DIM, "lm", "head")
        for j, res in enumerate(t["last"][-5:]):
            cx = 800 + j * 38
            d.ellipse((cx - 14, y - 14, cx + 14, y + 14), fill=WIN if res == "W" else LOSS)
            _text(d, (cx, y), res, 14, INK, "mm", "head")
    if not teams:
        _text(d, (525, 400), "NO RESULTS YET", 22, DIM, "mm")
    _text(d, (1050, 138), "PLAYERS ON A RUN", 13, MUTED)
    _text(d, (1050, 158), "CONSECUTIVE MATCHES WITH 20+ RUNS OR A WICKET", 11, DIM)
    for i, row in enumerate(data["players"][:6]):
        y = 190 + i * 104
        _avatar(c, row["who"], 70, (1050, y), c.a)
        _name(d, row["who"]["name"], 1136, y + 4, 280, 24)
        _text(d, (1136, y + 40), _clip(d, (row["who"].get("team") or "").upper(), 280, 12), 12, MUTED)
        _title(d, (1510, y + 18), str(row["n"]), 50, HOT, "rm", "head")
        _text(d, (1510, y + 52), "IN A ROW", 11, MUTED, "ra")
    if not data["players"]:
        _text(d, (1285, 400), "NO ACTIVE RUNS", 20, DIM, "mm")
    _footer(c, "GOOGLI MANAGER  •  STREAKS", data.get("guild"))
    return _png(c.image)
