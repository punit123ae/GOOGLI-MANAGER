"""Match preview card: who is more likely to win, posted when a match is announced.

One 1600x900 image. The win probability is the hero (two huge percentages and a wide bar);
under it four small "factor" tiles show what the prediction is built from (record, last 5,
head to head, run strength) and which team each one favours.

Pure Pillow, no Discord objects. data = {
    "a"/"b": {name, logo (bytes), color, standing, roster},
    "prediction": the dict from compareteams.predict(),
    "guild": server name,
}
"""
from .teaminfo_image import (
    DIM, H, MUTED, W, WHITE, _clip, _fit, _open, _paste, _pill, _round_image, _text,
)
from .versus_image import (
    EDGE, INK, TRACK, _Canvas, _footer, _png, _title, _tug, pick_sides,
)

CLOSE = 0.04          # within 4 points of 50%: "too close to call"


def _team_column(c, team, accent, cx):
    """Big logo ring, name and record chip, centred on cx."""
    d = c.d
    ring = _round_image(_open(team.get("logo")), 230, ring=accent, ring_w=7, initial=team["name"][:1])
    _paste(c.image, ring, (cx - ring.size[0] // 2, 70))
    name = team["name"].upper()
    size = _fit(d, name, 320, 38, 18, "head")
    _title(d, (cx, 342), _clip(d, name, 320, size, "head"), size, WHITE, "ma", "head")
    standing = team.get("standing")
    if standing:
        text = f"RANK #{standing['rank']} OF {standing['of']}  •  {standing['group']}"
    else:
        text = team.get("roster") or ""
    if text:
        text = _clip(d, text.upper(), 300, 15)
        width = int(d.textlength(text, font=_font(15))) + 32
        _pill(d, cx - width // 2, 392, text, 15, fg=WHITE, outline=accent, pad=16, h=32)


def _font(size, kind="reg"):
    from .teaminfo_image import font
    return font(size, kind)


def _hero(c, prediction, a, b):
    """Percentages, the bar and the verdict between the two teams."""
    d = c.d
    cx = W // 2
    pa, pb = prediction["a"], prediction["b"]
    la = int(round(pa * 100))
    lb = 100 - la
    cap = "MATCH PREVIEW  •  WIN PROBABILITY"
    width = int(d.textlength(cap, font=_font(16))) + 40
    _pill(d, cx - width // 2, 52, cap, 16, fill=(10, 16, 32), fg=MUTED, outline=EDGE, pad=20, h=34)

    big = 168
    _title(d, (cx - 26, 188), f"{la}%", big, c.a if pa >= pb else MUTED, "rm", "head")
    _title(d, (cx + 26, 188), f"{lb}%", big, c.b if pb > pa else MUTED, "lm", "head")
    _text(d, (cx, 190), "VS", 28, DIM, "mm", "head")

    # wide bar with a marker where the split is
    x1, x2, y, h = 330, 1270, 392, 26
    d.rounded_rectangle((x1, y, x2, y + h), radius=h // 2, fill=TRACK)
    cut = x1 + int((x2 - x1) * pa)
    d.rounded_rectangle((x1, y, max(x1 + h, cut - 4), y + h), radius=h // 2, fill=c.a)
    d.rounded_rectangle((min(x2 - h, cut + 4), y, x2, y + h), radius=h // 2, fill=c.b)
    d.polygon([(cut - 9, y - 12), (cut + 9, y - 12), (cut, y - 1)], fill=WHITE)

    if abs(pa - 0.5) < CLOSE:
        label, fill, fg, outline = "TOO CLOSE TO CALL", None, WHITE, EDGE
    else:
        fav, color = (a["name"], c.a) if pa > pb else (b["name"], c.b)
        label, fill, fg, outline = _clip(d, f"{fav.upper()} FAVOURED", 520, 20), color, INK, None
    pw = int(d.textlength(label, font=_font(20))) + 44
    _pill(d, cx - pw // 2, 440, label, 20, fill=fill, fg=fg, outline=outline, pad=22, h=40)
    if prediction.get("known", True):
        met = prediction.get("met", 0)
        basis = f"BASED ON RECORD, FORM AND {met} PAST MEETING(S)" if met else "BASED ON RECORD AND FORM  •  FIRST MEETING"
    else:
        basis = "NOT ENOUGH MATCHES PLAYED YET  •  SHOWN AS EVEN"
    _text(d, (cx, 500), basis, 13, DIM, "mm")


def _tile(c, box, factor, a_tag, b_tag):
    d = c.d
    x1, y1, x2, y2 = box
    label, chance, a_text, b_text = factor
    cx = (x1 + x2) // 2
    _text(d, (cx, y1 + 28), label, 15, MUTED, "mm")
    missing = chance is None
    for text, side_x, anchor, color in ((a_text, x1 + 26, "lm", c.a), (b_text, x2 - 26, "rm", c.b)):
        size = _fit(d, text, 130, 38, 18, "head")
        _text(d, (side_x, y1 + 78), text, size, DIM if text == "—" else color, anchor, "head")
    _tug(d, x1 + 26, x2 - 26, y1 + 118, 9, None if missing else min(0.93, max(0.07, chance)), c.a, c.b)
    if missing:
        note, color = "NO DATA", DIM
    elif abs(chance - 0.5) < 0.03:
        note, color = "EVEN", MUTED
    else:
        note, color = (f"EDGE  {a_tag}", c.a) if chance > 0.5 else (f"EDGE  {b_tag}", c.b)
    _text(d, (cx, y1 + 152), note, 15, color, "mm", "head")


def render_match_preview(data):
    a, b = data["a"], data["b"]
    col_a, col_b = pick_sides(a.get("color"), b.get("color"))
    c = _Canvas(col_a, col_b)
    prediction = data["prediction"]
    factors = prediction["factors"]
    gap, left, right = 20, 50, 1550
    tile_w = (right - left - gap * 3) // 4
    boxes = [(left + i * (tile_w + gap), 640, left + i * (tile_w + gap) + tile_w, 836) for i in range(4)]
    for box in boxes:
        c.card(box, 24)
    c.card((left, 560, right, 618), 20)
    c.seal()

    _team_column(c, a, c.a, 190)
    _team_column(c, b, c.b, W - 190)
    _hero(c, prediction, a, b)

    d = c.d
    _text(d, (W // 2, 589), "WHAT THE PREDICTION IS BASED ON", 16, MUTED, "mm", "head")
    from .teaminfo_image import _tag
    a_tag, b_tag = _tag(a["name"]), _tag(b["name"])
    for box, factor in zip(boxes, factors):
        _tile(c, box, factor, a_tag, b_tag)
    _footer(c, f"GOOGLI MANAGER  •  MATCH PREVIEW  •  {a['name']} vs {b['name']}"[:90], data.get("guild"))
    return _png(c.image)
