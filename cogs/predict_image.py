"""The +predict card: match-winner chance and the top run / wicket / catch / fantasy picks.

data = {"guild", "number", "a": {"name","logo","color","win"}, "b": {...},
        "known": bool (False when there is no history to base the winner on),
        "cats": {"runs": {"team": [pickA, pickB], "match": pick}, ...}
        where a pick = {"uid","chance","exp"} or None,
        "people": {uid: {"name", "avatar", "side": 0|1, "color"}}}
"""
from .awards import _glow_text, _tile_glass
from .teaminfo_image import DIM, MUTED, WHITE, _clip, _fit, _mix, _open, _paste, _pill, _round_image, _text, font
from .versus_image import _Canvas, _footer, _png, _title, pick_sides

_INK = (12, 18, 32)
GOLD = (255, 206, 90)
CATS = [
    ("runs", "TOP RUN SCORER", "runs", (255, 130, 90), "{:.0f}"),
    ("wickets", "TOP WICKET TAKER", "wkts", (170, 130, 255), "{:.1f}"),
    ("catches", "TOP FIELDER", "catches", (62, 224, 172), "{:.1f}"),
    ("fantasy", "TOP FANTASY", "pts", (100, 185, 255), "{:.0f}"),
]


def _bar(d, x1, x2, y, h, share_a, col_a, col_b):
    d.rounded_rectangle((x1, y, x2, y + h), radius=h // 2, fill=col_b)
    w = int((x2 - x1) * max(0.0, min(1.0, share_a)))
    if w > 2:
        d.rounded_rectangle((x1, y, x1 + max(h, w), y + h), radius=h // 2, fill=col_a)


def _header(c, data):
    d = c.d
    a, b = data["a"], data["b"]
    _title(d, (76, 66), "MATCH PREDICTION", 44, WHITE, "lm", "head")
    sub = f"MATCH #{data['number']}" if data.get("number") else "MATCH PREDICTION"
    _text(d, (1524, 40), sub, 16, MUTED, "rm")
    _text(d, (1524, 62), str(data.get("guild", "")).upper(), 13, DIM, "rm")
    for side, left in ((a, True), (b, False)):
        logo = _round_image(_open(side.get("logo")), 120, ring=side["color"], ring_w=5, initial=side["name"][:1])
        x = 80 if left else 1550 - 30 - 120
        _paste(c.image, logo, (x, 100))
        tx = x + 140 if left else x - 20
        anchor = "la" if left else "ra"
        name = side["name"].upper()
        size = _fit(d, name, 420, 38, 20, "head")
        _title(d, (tx, 112), _clip(d, name, 420, size, "head"), size, WHITE, anchor, "head")
        pct = f"{side['win'] * 100:.0f}%" if data["known"] else "—"
        px = tx if left else tx - d.textlength(pct, font=font(54, "head"))
        _glow_text(c, (px, 156), pct, 54, side["color"])
    _text(d, (800, 134), "VS", 34, MUTED, "mm", "head")
    if data["known"]:
        _bar(d, 560, 1040, 178, 16, a["win"], a["color"], b["color"])
        lead = a if a["win"] >= b["win"] else b
        edge = abs(a["win"] - b["win"]) * 100
        text = "TOO CLOSE TO CALL" if edge < 4 else f"{lead['name'].upper()} FAVOURED"
        _text(d, (800, 214), _clip(d, text, 480, 15), 15, GOLD if edge >= 4 else MUTED, "mm", "bold")
    else:
        _text(d, (800, 192), "NOT ENOUGH HISTORY FOR A WINNER", 14, DIM, "mm", "bold")


def _pick_row(c, y, x1, x2, label, color, pick, people, unit, fmt, big=False):
    d = c.d
    _pill(d, x1 + 24, y + 14, label, 11, fill=_mix(color, _INK, 0.72), fg=WHITE, outline=color, pad=11, h=24)
    if pick is None:
        _text(d, (x1 + 190, y + 26), "NO DATA", 15, DIM, "lm", "bold")
        return
    who = people.get(pick["uid"], {})
    ring = _round_image(_open(who.get("avatar")), 46 if not big else 52, ring=who.get("color") or color,
                        ring_w=3, initial=(who.get("name") or "?")[:1])
    _paste(c.image, ring, (x1 + 178, y + 26 - ring.size[1] // 2 + 2))
    nx = x1 + 178 + ring.size[0] + 14
    name = (who.get("name") or "UNKNOWN").upper()
    size = _fit(d, name, 200, 20 if not big else 22, 13, "head")
    _text(d, (nx, y + 16), _clip(d, name, 200, size, "head"), size, WHITE if not big else GOLD, "lm", "head")
    _text(d, (nx, y + 40), f"~{fmt.format(pick['exp'])} {unit.upper()} EXPECTED", 11, MUTED, "lm", "bold")
    _text(d, (x2 - 26, y + 18), f"{pick['chance'] * 100:.0f}%", 26, color, "rm", "head")
    _text(d, (x2 - 26, y + 42), "TO TOP IT", 10, MUTED, "rm", "bold")


def render_predict(data):
    a, b = data["a"], data["b"]
    c = _Canvas(a["color"], b["color"])
    c.card((50, 22, 1550, 244), 26, 168)
    boxes = [(50, 260, 790, 548), (810, 260, 1550, 548), (50, 564, 790, 852), (810, 564, 1550, 852)]
    for box in boxes:
        c.card(box, 26, 150)
    d = c.seal()
    _tile_glass(c, (50, 22, 1550, 244), _mix(a["color"], b["color"], 0.5))
    _header(c, data)
    for box, (key, title, unit, color, fmt) in zip(boxes, CATS):
        x1, y1, x2, y2 = box
        _tile_glass(c, box, color)
        d.ellipse((x1 + 28, y1 + 26, x1 + 40, y1 + 38), fill=color)
        _text(d, (x1 + 54, y1 + 18), title, 24, color, "la", "head")
        cat = data["cats"][key]
        rows = [(a["name"].upper(), a["color"], cat["team"][0], False),
                (b["name"].upper(), b["color"], cat["team"][1], False),
                ("MATCH", GOLD, cat["match"], True)]
        for i, (label, col, pick, big) in enumerate(rows):
            y = y1 + 70 + i * 70
            if big:
                d.rounded_rectangle((x1 + 14, y - 2, x2 - 14, y + 62), radius=16,
                                    fill=_mix(GOLD, _INK, 0.84), outline=_mix(GOLD, _INK, 0.5), width=1)
            _pick_row(c, y, x1, x2, _clip(d, label, 124, 11), col, pick, data["people"], unit, fmt, big)
    _footer(c, f"GOOGLI MANAGER  •  {a['name']} vs {b['name']}"[:90], data.get("guild"))
    return c.image


def render_predict_png(data):
    return _png(render_predict(data))
