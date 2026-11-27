"""The card a user posts with +predict: their own picks for a match.

data = {"guild", "number", "user": {"name", "avatar"},
        "a": {"name", "logo", "color"}, "b": {...}, "winner": 0 | 1 | None,
        "cats": {"runs": {"a": uid|None, "b": uid|None, "m": uid|None}, "wickets": ..., "catches": ..., "fantasy": ...},
        "people": {uid: {"name", "avatar", "color"}}}
"""
from .awards import _glow_text, _tile_glass
from .teaminfo_image import DIM, MUTED, WHITE, _clip, _fit, _mix, _open, _paste, _pill, _round_image, _text, font
from .versus_image import _Canvas, _footer, _png, _title

_INK = (12, 18, 32)
GOLD = (255, 206, 90)
CATS = [
    ("runs", "TOP RUN SCORER", (255, 130, 90)),
    ("wickets", "TOP WICKET TAKER", (170, 130, 255)),
    ("catches", "TOP FIELDER", (62, 224, 172)),
    ("fantasy", "TOP FANTASY", (100, 185, 255)),
]


def _team_block(c, cx, side, picked, undecided):
    d = c.d
    dim = not picked and not undecided
    ring_col = side["color"] if not dim else _mix(side["color"], _INK, 0.65)
    logo = _round_image(_open(side.get("logo")), 104, ring=ring_col, ring_w=5, initial=side["name"][:1])
    _paste(c.image, logo, (cx - 52, 86))
    name = side["name"].upper()
    size = _fit(d, name, 360, 22, 13, "head")
    _text(d, (cx, 206), _clip(d, name, 360, size, "head"), size, WHITE if not dim else MUTED, "mm", "head")
    if picked:
        _pill(d, cx - 70, 52, "WINNER PICK", 13, fill=GOLD, fg=_INK, pad=16, h=28)


def _pick_row(c, y, x1, x2, label, color, uid, people, big=False):
    d = c.d
    _pill(d, x1 + 24, y + 14, label, 11, fill=_mix(color, _INK, 0.72), fg=WHITE, outline=color, pad=11, h=24)
    if uid is None:
        _text(d, (x1 + 190, y + 26), "NO PICK", 16, DIM, "lm", "bold")
        return
    who = people.get(uid, {})
    ring = _round_image(_open(who.get("avatar")), 50 if not big else 54, ring=who.get("color") or color, ring_w=3,
                        initial=(who.get("name") or "?")[:1])
    _paste(c.image, ring, (x1 + 178, y + 26 - ring.size[1] // 2 + 2))
    nx = x1 + 178 + ring.size[0] + 16
    name = (who.get("name") or "UNKNOWN").upper()
    size = _fit(d, name, 360, 24 if not big else 26, 14, "head")
    _text(d, (nx, y + 26), _clip(d, name, 360, size, "head"), size, WHITE if not big else GOLD, "lm", "head")


def render_prediction(data):
    a, b = data["a"], data["b"]
    c = _Canvas(a["color"], b["color"])
    c.card((50, 22, 1550, 244), 26, 168)
    boxes = [(50, 260, 790, 548), (810, 260, 1550, 548), (50, 564, 790, 852), (810, 564, 1550, 852)]
    for box in boxes:
        c.card(box, 26, 150)
    d = c.seal()
    _tile_glass(c, (50, 22, 1550, 244), GOLD)
    _title(d, (76, 66), "MY PREDICTIONS", 44, WHITE, "lm", "head")
    _text(d, (1524, 40), f"MATCH #{data['number']}" if data.get("number") else "MATCH", 16, MUTED, "rm")
    _text(d, (1524, 62), str(data.get("guild", "")).upper(), 13, DIM, "rm")
    user = data["user"]
    ring = _round_image(_open(user.get("avatar")), 112, ring=GOLD, ring_w=5, initial=user["name"][:1])
    _paste(c.image, ring, (80, 100))
    _text(d, (212, 118), "PREDICTED BY", 13, MUTED)
    uname = user["name"].upper()
    size = _fit(d, uname, 330, 30, 16, "head")
    _text(d, (212, 140), _clip(d, uname, 330, size, "head"), size, WHITE, "la", "head")
    undecided = data.get("winner") is None
    _team_block(c, 800, a, data.get("winner") == 0, undecided)
    _text(d, (1010, 138), "VS", 30, MUTED, "mm", "head")
    _team_block(c, 1220, b, data.get("winner") == 1, undecided)
    if undecided:
        _text(d, (1010, 62), "NO WINNER PICKED", 13, DIM, "mm", "bold")
    for box, (key, title, color) in zip(boxes, CATS):
        x1, y1, x2, y2 = box
        _tile_glass(c, box, color)
        d.ellipse((x1 + 28, y1 + 26, x1 + 40, y1 + 38), fill=color)
        _text(d, (x1 + 54, y1 + 18), title, 24, color, "la", "head")
        picks = data["cats"][key]
        rows = [(a["name"].upper(), a["color"], picks.get("a"), False), (b["name"].upper(), b["color"], picks.get("b"), False),
                ("MATCH", GOLD, picks.get("m"), True)]
        for i, (label, col, uid, big) in enumerate(rows):
            y = y1 + 70 + i * 70
            if big:
                d.rounded_rectangle((x1 + 14, y - 2, x2 - 14, y + 62), radius=16, fill=_mix(GOLD, _INK, 0.84),
                                    outline=_mix(GOLD, _INK, 0.5), width=1)
            _pick_row(c, y, x1, x2, _clip(d, label, 124, 11), col, uid, data["people"], big)
    _footer(c, f"GOOGLI MANAGER  •  {a['name']} vs {b['name']}"[:90], data.get("guild"))
    return c.image


def render_prediction_png(data):
    return _png(render_prediction(data))
