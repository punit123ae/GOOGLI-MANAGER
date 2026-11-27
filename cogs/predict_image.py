"""Cards for the prediction game.

render_prediction_png(data)  one user's picks (with points and the real answers once the match is scored)
render_result_png(data)      the official result of a match plus the best predictors
render_board_png(data)       prediction leaderboard

Prediction card data:
  {"guild", "number", "user": {"name", "avatar"}, "a": {"name", "logo", "color"}, "b": {...},
   "winner": 0 | 1 | None, "cats": {"runs": {"a": uid, "b": uid, "m": uid}, "wickets": ..., "fantasy": ...},
   "people": {uid: {"name", "avatar", "color"}}, "ai_picks": {cat: {"a","b","m"}},
   # only after the result is in:
   "result_winner": 0 | 1 | None, "grade": {"winner": pts, "runs:a": pts, ..., "total": n, "max": n},
   "actual": {"runs": {"a","b","m"}, ...}}
"""
from .awards import _glow_text, _tile_glass
from .teaminfo_image import DIM, MUTED, WHITE, _clip, _fit, _mix, _open, _paste, _pill, _round_image, _text, font
from .versus_image import _Canvas, _footer, _png, _title

_INK = (12, 18, 32)
GOLD = (255, 206, 90)
SILVER = (205, 214, 230)
BRONZE = (214, 146, 92)
GOOD = (62, 224, 172)
WARN = (255, 190, 80)
BAD = (255, 100, 112)
CATS = [
    ("runs", "TOP RUN SCORER", (255, 130, 90)),
    ("wickets", "TOP WICKET TAKER", (170, 130, 255)),
    ("fantasy", "TOP FANTASY", (100, 185, 255)),
]
TILES = [(50, 260, 540, 852), (555, 260, 1045, 852), (1060, 260, 1550, 852)]
MEDAL = [GOLD, SILVER, BRONZE]


def _grade_colour(points):
    return GOOD if points >= 3 else WARN if points > 0 else BAD


def _team_block(c, cx, side, picked, undecided, graded, correct, won):
    d = c.d
    dim = not picked and not undecided and not won
    ring_col = side["color"] if not dim else _mix(side["color"], _INK, 0.65)
    logo = _round_image(_open(side.get("logo")), 104, ring=ring_col, ring_w=5, initial=side["name"][:1])
    _paste(c.image, logo, (cx - 52, 86))
    name = side["name"].upper()
    size = _fit(d, name, 360, 22, 13, "head")
    _text(d, (cx, 206), _clip(d, name, 360, size, "head"), size, WHITE if not dim else MUTED, "mm", "head")
    if picked:
        if graded:
            label, fill, fg = ("CORRECT  +5", GOOD, _INK) if correct else ("WRONG PICK", BAD, _INK)
        else:
            label, fill, fg = "WINNER PICK", GOLD, _INK
        width = int(d.textlength(label, font=font(13, "bold"))) + 32
        _pill(d, cx - width // 2, 52, label, 13, fill=fill, fg=fg, pad=16, h=28)
    if won:
        _pill(d, cx + 44, 82, "WON", 11, fill=_mix(GOLD, _INK, 0.3), fg=GOLD, outline=GOLD, pad=9, h=22)


def _pick_block(c, y, x1, x2, label, color, uid, people, big, ai, points, actual_uid):
    """One pick: team label, big avatar, name and (when known) the points and the real answer."""
    d = c.d
    _pill(d, x1 + 24, y + 2, label, 12, fill=_mix(color, _INK, 0.72), fg=WHITE, outline=color, pad=12, h=26)
    if points is not None:
        text = f"+{points}" if points else "0"
        tone = _grade_colour(points)
        width = int(d.textlength(text, font=font(15, "head"))) + 28
        _pill(d, x2 - 24 - width, y + 2, text, 15, fill=_mix(tone, _INK, 0.7), fg=tone, outline=tone, pad=14, h=26,
              kind="head")
    if uid is None:
        _text(d, (x1 + 28, y + 76), "NO PICK", 20, DIM, "lm", "bold")
        return
    who = people.get(uid, {})
    ring = _round_image(_open(who.get("avatar")), 78 if not big else 84, ring=who.get("color") or color, ring_w=3,
                        initial=(who.get("name") or "?")[:1])
    _paste(c.image, ring, (x1 + 24, y + 40))
    nx = x1 + 24 + ring.size[0] + 16
    name = (who.get("name") or "UNKNOWN").upper()
    size = _fit(d, name, x2 - 22 - nx, 28 if not big else 30, 14, "head")
    _text(d, (nx, y + 70), _clip(d, name, x2 - 22 - nx, size, "head"), size, WHITE if not big else GOLD, "lm", "head")
    cursor = nx
    if ai:
        cursor += _pill(d, cursor, y + 94, "AI PICK", 10, fill=GOLD, fg=_INK, pad=10, h=24) + 10
    if points is not None and not points and actual_uid is not None and actual_uid != uid:
        real = (people.get(actual_uid, {}).get("name") or "?").upper()
        _text(d, (cursor, y + 106), _clip(d, "WAS " + real, x2 - 22 - cursor, 13), 13, MUTED, "lm", "bold")


def render_prediction(data):
    a, b = data["a"], data["b"]
    c = _Canvas(a["color"], b["color"])
    c.card((50, 22, 1550, 244), 26, 168)
    for box in TILES:
        c.card(box, 26, 150)
    d = c.seal()
    grade = data.get("grade")
    _tile_glass(c, (50, 22, 1550, 244), GOLD)
    _title(d, (76, 66), "MY PREDICTIONS", 44, WHITE, "lm", "head")
    _text(d, (1524, 40), f"MATCH #{data['number']}" if data.get("number") else "MATCH", 16, MUTED, "rm")
    _text(d, (1524, 62), str(data.get("guild", "")).upper(), 13, DIM, "rm")
    if grade:
        _text(d, (1524, 134), str(grade["total"]), 64, GOLD, "rm", "head")
        _text(d, (1524, 188), f"OF {grade['max']} POINTS", 13, MUTED, "rm")
    user = data["user"]
    ring = _round_image(_open(user.get("avatar")), 112, ring=GOLD, ring_w=5, initial=user["name"][:1])
    _paste(c.image, ring, (80, 100))
    _text(d, (212, 118), "PREDICTED BY", 13, MUTED)
    uname = user["name"].upper()
    size = _fit(d, uname, 330, 30, 16, "head")
    _text(d, (212, 140), _clip(d, uname, 330, size, "head"), size, WHITE, "la", "head")
    winner, truth = data.get("winner"), data.get("result_winner")
    undecided = winner is None
    graded = grade is not None
    for cx, side, index in ((800, a, 0), (1220, b, 1)):
        _team_block(c, cx, side, winner == index, undecided, graded, graded and truth == winner, graded and truth == index)
    _text(d, (1010, 138), "VS", 30, MUTED, "mm", "head")
    if undecided:
        _text(d, (1010, 62), "NO WINNER PICKED", 13, DIM, "mm", "bold")
    for box, (key, title, color) in zip(TILES, CATS):
        x1, y1, x2, y2 = box
        _tile_glass(c, box, color)
        d.ellipse((x1 + 28, y1 + 26, x1 + 40, y1 + 38), fill=color)
        _text(d, (x1 + 54, y1 + 18), title, 24, color, "la", "head")
        picks = data["cats"][key]
        rows = [(a["name"].upper(), a["color"], "a", False), (b["name"].upper(), b["color"], "b", False),
                ("BEST OF THE MATCH", GOLD, "m", True)]
        for i, (label, col, side, big) in enumerate(rows):
            y = y1 + 72 + i * 172
            if big:
                d.rounded_rectangle((x1 + 12, y - 8, x2 - 12, y + 150), radius=18, fill=_mix(GOLD, _INK, 0.86),
                                    outline=_mix(GOLD, _INK, 0.5), width=1)
            uid = picks.get(side)
            ai_uid = data.get("ai_picks", {}).get(key, {}).get(side)
            points = grade.get(f"{key}:{side}", 0) if graded and uid is not None else None
            actual = data.get("actual", {}).get(key, {}).get(side)
            _pick_block(c, y, x1, x2, _clip(d, label, 250, 12), col, uid, data["people"], big,
                        uid is not None and uid == ai_uid, points, actual)
    _footer(c, f"GOOGLI MANAGER  •  {a['name']} vs {b['name']}"[:90], data.get("guild"))
    return c.image


def render_prediction_png(data):
    return _png(render_prediction(data))


# =========================================================================== official result

def _rank_badge(d, cx, cy, rank):
    tone = MEDAL[rank] if rank < 3 else _mix(MUTED, _INK, 0.4)
    d.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), fill=_mix(tone, _INK, 0.78), outline=tone, width=2)
    _text(d, (cx, cy), str(rank + 1), 18, tone, "mm", "head")


def render_result(data):
    a, b = data["a"], data["b"]
    c = _Canvas(a["color"], b["color"])
    c.card((50, 22, 1550, 244), 26, 168)
    for box in [(50, 260, 540, 632), (555, 260, 1045, 632), (1060, 260, 1550, 632)]:
        c.card(box, 26, 150)
    c.card((50, 648, 1550, 852), 26, 150)
    d = c.seal()
    _tile_glass(c, (50, 22, 1550, 244), GOLD)
    _title(d, (76, 66), "OFFICIAL RESULT", 44, WHITE, "lm", "head")
    _text(d, (76, 118), f"MATCH #{data['number']}", 20, GOLD, "lm", "head")
    _text(d, (1524, 40), str(data.get("guild", "")).upper(), 13, DIM, "rm")
    winner = data.get("winner")
    for cx, side, index in ((800, a, 0), (1220, b, 1)):
        _team_block(c, cx, side, False, False, False, False, winner == index)
    _text(d, (1010, 138), "VS", 30, MUTED, "mm", "head")
    if winner is None:
        _text(d, (1010, 62), "NO WINNER  •  TIE / NO RESULT", 13, MUTED, "mm", "bold")
    people = data["people"]
    for box, (key, title, color) in zip([(50, 260, 540, 632), (555, 260, 1045, 632), (1060, 260, 1550, 632)], CATS):
        x1, y1, x2, y2 = box
        _tile_glass(c, box, color)
        d.ellipse((x1 + 28, y1 + 26, x1 + 40, y1 + 38), fill=color)
        _text(d, (x1 + 54, y1 + 18), title, 24, color, "la", "head")
        top = data["tops"].get(key, [])
        if not top:
            _text(d, ((x1 + x2) // 2, (y1 + y2) // 2 + 20), "NOT ENTERED", 18, DIM, "mm", "bold")
        for i, uid in enumerate(top[:5]):
            y = y1 + 70 + i * 58
            who = people.get(uid, {})
            if i == 0:
                d.rounded_rectangle((x1 + 12, y - 4, x2 - 12, y + 50), radius=16, fill=_mix(GOLD, _INK, 0.86),
                                    outline=_mix(GOLD, _INK, 0.5), width=1)
            _rank_badge(d, x1 + 40, y + 23, i)
            ring = _round_image(_open(who.get("avatar")), 40, ring=who.get("color") or color, ring_w=2,
                                initial=(who.get("name") or "?")[:1])
            _paste(c.image, ring, (x1 + 68, y + 23 - ring.size[1] // 2))
            nx = x1 + 68 + ring.size[0] + 14
            name = (who.get("name") or "UNKNOWN").upper()
            size = _fit(d, name, x2 - 22 - nx, 24, 14, "head")
            _text(d, (nx, y + 23), _clip(d, name, x2 - 22 - nx, size, "head"), size,
                  GOLD if i == 0 else WHITE, "lm", "head")
    x1, y1, x2, y2 = 50, 648, 1550, 852
    _tile_glass(c, (x1, y1, x2, y2), GOLD)
    _text(d, (x1 + 36, y1 + 24), "TOP PREDICTORS", 24, GOLD, "la", "head")
    board = data.get("board", [])
    _text(d, (x2 - 36, y1 + 30), f"{data.get('players', len(board))} PLAYED  •  MAX {data.get('max', 0)} PTS", 14, MUTED,
          "ra", "bold")
    if not board:
        _text(d, ((x1 + x2) // 2, y1 + 130), "NOBODY PREDICTED THIS MATCH", 20, DIM, "mm", "bold")
    for i, row in enumerate(board[:5]):
        cx = x1 + 30 + i * 288
        d.rounded_rectangle((cx, y1 + 70, cx + 270, y1 + 184), radius=18,
                            fill=_mix(MEDAL[i] if i < 3 else MUTED, _INK, 0.86),
                            outline=_mix(MEDAL[i] if i < 3 else MUTED, _INK, 0.5), width=1)
        _rank_badge(d, cx + 30, y1 + 127, i)
        ring = _round_image(_open(row.get("avatar")), 56, ring=MEDAL[i] if i < 3 else MUTED, ring_w=3,
                            initial=(row.get("name") or "?")[:1])
        _paste(c.image, ring, (cx + 58, y1 + 127 - ring.size[1] // 2))
        tx = cx + 58 + ring.size[0] + 12
        name = (row.get("name") or "?").upper()
        size = _fit(d, name, cx + 258 - tx, 20, 12, "head")
        _text(d, (tx, y1 + 108), _clip(d, name, cx + 258 - tx, size, "head"), size, WHITE, "lm", "head")
        _text(d, (tx, y1 + 146), str(row["points"]), 38, MEDAL[i] if i < 3 else WHITE, "lm", "head")
        _text(d, (tx + 12 + int(d.textlength(str(row["points"]), font=font(38, "head"))), y1 + 154), "PTS", 13, MUTED,
              "lm", "bold")
    _footer(c, f"GOOGLI MANAGER  •  {a['name']} vs {b['name']}"[:90], data.get("guild"))
    return c.image


def render_result_png(data):
    return _png(render_result(data))


# =========================================================================== leaderboard

def render_board(data):
    c = _Canvas(GOLD, (100, 185, 255))
    c.card((50, 22, 1550, 170), 26, 168)
    c.card((50, 186, 1550, 852), 26, 150)
    d = c.seal()
    _tile_glass(c, (50, 22, 1550, 170), GOLD)
    _title(d, (76, 78), "PREDICTION LEADERBOARD", 44, WHITE, "lm", "head")
    _text(d, (76, 128), str(data.get("subtitle", "")).upper(), 18, GOLD, "lm", "head")
    _text(d, (1524, 60), str(data.get("guild", "")).upper(), 13, DIM, "rm")
    rows = data.get("rows", [])
    _tile_glass(c, (50, 186, 1550, 852), (100, 185, 255))
    if not rows:
        _text(d, (800, 520), "NO POINTS YET", 28, DIM, "mm", "head")
    top = max([r["points"] for r in rows] + [1])
    for i, row in enumerate(rows[:10]):
        y = 212 + i * 64
        tone = MEDAL[i] if i < 3 else MUTED
        if i < 3:
            d.rounded_rectangle((70, y - 4, 1530, y + 56), radius=18, fill=_mix(tone, _INK, 0.88),
                                outline=_mix(tone, _INK, 0.55), width=1)
        _text(d, (112, y + 26), str(i + 1), 28, tone if i < 3 else MUTED, "mm", "head")
        ring = _round_image(_open(row.get("avatar")), 44, ring=tone if i < 3 else row.get("color") or MUTED, ring_w=3,
                            initial=(row.get("name") or "?")[:1])
        _paste(c.image, ring, (150, y + 26 - ring.size[1] // 2))
        name = (row.get("name") or "?").upper()
        size = _fit(d, name, 380, 26, 14, "head")
        _text(d, (222, y + 26), _clip(d, name, 380, size, "head"), size, WHITE if i else GOLD, "lm", "head")
        _text(d, (626, y + 26), f"{row.get('matches', 0)} MATCH{'ES' if row.get('matches', 0) != 1 else ''}", 14, MUTED,
              "lm", "bold")
        bar_x1, bar_x2 = 770, 1330
        d.rounded_rectangle((bar_x1, y + 18, bar_x2, y + 34), radius=8, fill=_mix(MUTED, _INK, 0.8))
        fill = max(14, int((bar_x2 - bar_x1) * row["points"] / top))
        d.rounded_rectangle((bar_x1, y + 18, bar_x1 + fill, y + 34), radius=8, fill=tone if i < 3 else (100, 185, 255))
        _text(d, (1440, y + 26), str(row["points"]), 34, tone if i < 3 else WHITE, "rm", "head")
        _text(d, (1500, y + 32), "PTS", 13, MUTED,
              "rm", "bold")
    _footer(c, "GOOGLI MANAGER  •  PREDICTIONS", data.get("guild"))
    return c.image


def render_board_png(data):
    return _png(render_board(data))
