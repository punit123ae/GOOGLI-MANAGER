"""Match preview card: who is more likely to win, posted when a match is announced.

One 1600x900 image. The win probability is the hero (two huge percentages and a wide bar);
under it four small "factor" tiles show what the prediction is built from (record, batting,
bowling, fielding) and which team each one favours.

Pure Pillow, no Discord objects. data = {
    "a"/"b": {name, logo (bytes), color, standing, roster},
    "prediction": the dict from compareteams.predict(),
    "guild": server name,
}
"""
from .teaminfo_image import (
    DIM, LOSS, MUTED, W, WHITE, WIN, _clip, _fit, _open, _paste, _pill, _round_image, _tag, _text,
)
from .versus_image import (
    BAT_COLOR, BOWL_COLOR, EDGE, FIELD_COLOR, INK, RECORD_COLOR, TRACK, _Canvas, _draw_row, _footer, _png,
    _title, _tug, pick_sides,
)

CLOSE = 0.04          # within 4 points of 50%: "too close to call"


def _team_column(c, team, accent, cx):
    """Big logo ring, name and record chip, centred on cx."""
    d = c.d
    ring = _round_image(_open(team.get("logo")), 230, ring=accent, ring_w=7, initial=team["name"][:1])
    _paste(c.image, ring, (cx - ring.size[0] // 2, 70))
    name = team["name"].upper()
    size = _fit(d, name, 320, 38, 18, "head")
    _title(d, (cx, 356), _clip(d, name, 320, size, "head"), size, WHITE, "mm", "head")
    standing = team.get("standing")
    if standing:
        text = f"RANK #{standing['rank']} OF {standing['of']}  •  {standing['group']}"
    else:
        text = team.get("roster") or ""
    if text:
        text = _clip(d, text.upper(), 300, 15)
        width = int(d.textlength(text, font=_font(15))) + 32
        _pill(d, cx - width // 2, 392, text, 15, fg=WHITE, outline=accent, pad=16, h=32)
    _last_five(d, cx, 462, (standing or {}).get("history", [])[-5:])


def _last_five(d, cx, y, history):
    """LAST 5 results as green W / red L circles, oldest to newest (graphics only, not part of the maths)."""
    _text(d, (cx, y - 26), "LAST 5 MATCHES", 13, MUTED, "mm")
    if not history:
        _text(d, (cx, y), "NO RESULTS YET", 14, DIM, "mm")
        return
    step = 42
    start = cx - step * (len(history) - 1) / 2
    for i, result in enumerate(history):
        x = start + i * step
        win = result == "W"
        d.ellipse((x - 16, y - 16, x + 16, y + 16), fill=WIN if win else LOSS)
        _text(d, (x, y), result, 16, INK, "mm", "head")


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
        basis = "BASED ON RECORD, BATTING, BOWLING, FIELDING AND SQUAD BALANCE"
    else:
        basis = "NOT ENOUGH MATCHES PLAYED YET  •  SHOWN AS EVEN"
    _text(d, (cx, 500), basis, 13, DIM, "mm")


TILE_COLORS = {"RECORD": RECORD_COLOR, "BATTING": BAT_COLOR, "BOWLING": BOWL_COLOR, "FIELDING": FIELD_COLOR}


def _tile(c, box, factor, a_tag, b_tag):
    """One category: title, three stat rows (winner lit up, tug bar) and who has the edge."""
    d = c.d
    x1, y1, x2, y2 = box
    label, chance = factor["label"], factor["chance"]
    color = TILE_COLORS.get(label, WHITE)
    d.ellipse((x1 + 24, y1 + 25, x1 + 36, y1 + 37), fill=color)
    _text(d, (x1 + 48, y1 + 20), label, 22, color, "la", "head")
    for i, row in enumerate(factor["rows"]):
        _draw_row(d, x1, x2, y1 + 58 + i * 62, row, c.a, c.b, pad=24)
    if chance is None:
        text, fill, fg, outline = "NO DATA", None, DIM, EDGE
    elif abs(chance - 0.5) < 0.03:
        text, fill, fg, outline = "EVEN", None, WHITE, EDGE
    else:
        win_a = chance > 0.5
        share = round((chance if win_a else 1 - chance) * 100)
        text = f"EDGE  {a_tag if win_a else b_tag}  {share}%"
        fill, fg, outline = (c.a if win_a else c.b), INK, None
    width = int(d.textlength(text, font=_font(16, "bold"))) + 36
    _pill(d, (x1 + x2) // 2 - width // 2, y2 - 52, text, 16, fill=fill, fg=fg, outline=outline, pad=18, h=34)


def render_match_preview(data):
    a, b = data["a"], data["b"]
    col_a, col_b = pick_sides(a.get("color"), b.get("color"))
    c = _Canvas(col_a, col_b)
    prediction = data["prediction"]
    factors = prediction["factors"]
    gap, left, right = 20, 50, 1550
    tile_w = (right - left - gap * 3) // 4
    boxes = [(left + i * (tile_w + gap), 560, left + i * (tile_w + gap) + tile_w, 848) for i in range(4)]
    for box in boxes:
        c.card(box, 24)
    c.seal()

    _team_column(c, a, c.a, 190)
    _team_column(c, b, c.b, W - 190)
    _hero(c, prediction, a, b)

    a_tag, b_tag = _tag(a["name"]), _tag(b["name"])
    for box, factor in zip(boxes, factors):
        _tile(c, box, factor, a_tag, b_tag)
    _footer(c, f"GOOGLI MANAGER  •  MATCH PREVIEW  •  {a['name']} vs {b['name']}"[:90], data.get("guild"))
    return _png(c.image)
