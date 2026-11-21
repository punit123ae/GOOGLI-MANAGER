"""Image version of +fdstats (fielding profile), rendered with the team-profile look."""
import io

from PIL import Image, ImageDraw

from .teaminfo_image import (
    DEFAULT_ACCENT, H, LOSS, MUTED, DIM, W, WHITE, WIN, _Page, _backdrop_color, _clip, _donut,
    _fit, _lighten, _open, _paste, _pill, _round_image, _text, _visible_accent, font,
)

INK = (12, 18, 32)


def _tile(page, d, box, label, value, color, sub=None):
    x1, y1, x2, y2 = box
    d.rounded_rectangle((x1, y1+16, x1+6, y2-16), radius=3, fill=color)
    _text(d, (x1+22, y1+14), label, 15, MUTED)
    _text(d, (x1+22, y1+38), value, _fit(d, value, x2-x1-40, 58, 30, "head"), color, kind="head")
    if sub:
        _text(d, (x1+22, y2-26), sub, 14, DIM)


def render_fielding_image(data):
    color = tuple(data.get("team_color") or DEFAULT_ACCENT)
    accent = _visible_accent(color)
    theme = {"accent": accent, "accent2": _lighten(accent, 0.35), "bg": _backdrop_color(color), "logo": None}
    page = _Page(theme)

    page.card((50, 22, 1550, 106), 24)
    page.card((50, 126, 520, 570), 24)
    page.card((540, 126, 1010, 570), 24)
    page.card((1030, 126, 1550, 570), 24)
    page.card((50, 590, 790, 868), 24)
    page.card((810, 590, 1550, 868), 24)
    d = page.seal()

    avatar = _open(data.get("avatar_bytes"))
    _paste(page.image, _round_image(avatar, 58, ring=accent, ring_w=3, initial=data["name"][:1]), (72, 34))
    _text(d, (150, 29), "FIELDING PROFILE", 15, accent)
    _text(d, (150, 53), data["name"].upper(), _fit(d, data["name"].upper(), 900, 36, 22, "head"), WHITE, kind="head")
    if data.get("team_name"):
        label = _clip(d, data["team_name"].upper(), 380, 20)
        _pill(d, 1550 - 24 - int(d.textlength(label, font=font(20))) - 32, 40, label, 20, fill=accent, fg=INK)

    # --- left: portrait + rank
    _paste(page.image, _round_image(avatar, 190, ring=accent, ring_w=6, initial=data["name"][:1]), (190, 150))
    _text(d, (285, 372), _clip(d, data["name"], 410, 30), 30, WHITE, "ma", "head")
    title = data.get("title")
    if title:
        _text(d, (285, 416), title, _fit(d, title, 420, 20, 13), accent, "ma")
    rank = data.get("rank")
    if rank:
        _pill(d, 285 - 110, 470, f"#{rank[0]} OF {rank[1]} FIELDERS", 20, fill=accent, fg=INK, pad=22, h=44)
    else:
        _text(d, (285, 480), f"Needs {data['min_chances']}+ catch chances to be ranked", 16, DIM, "ma")
    if data.get("share") is not None:
        _text(d, (285, 530), f"{data['share']:.1f}% of team catches", 17, MUTED, "ma")

    # --- centre: success-rate donut
    eff = data["eff"]
    ring_color = WIN if eff >= 50 or not data["chances"] else LOSS
    _paste(page.image, _donut(250, eff/100, ring_color, (30, 44, 72), 22), (650, 150))
    _text(d, (775, 255), f"{eff:.1f}%", 46, WHITE, "mm", "head")
    _text(d, (775, 292), "SUCCESS RATE", 14, MUTED, "mm")
    _text(d, (775, 430), f"{data['caught']} caught of {data['chances']} chances", 20, WHITE, "mm")
    _text(d, (775, 470), f"Team average {data['team_eff']:.1f}%" if data.get("team_eff") is not None else "",
          17, MUTED, "mm")
    if data.get("drop_runs"):
        drops, cost = data["drop_runs"]
        _text(d, (775, 520), f"{drops} tracked drop(s) cost {cost} runs", 16, LOSS, "mm")

    # --- right: 2x2 stat tiles
    fantasy = data["fantasy"]
    tiles = [
        ("CATCHES TAKEN", data["caught"], WIN, "+5 fantasy each"),
        ("DROPPED", data["dropped"], LOSS, "-10 fantasy each"),
        ("TOTAL CHANCES", data["chances"], WHITE, None),
        ("FANTASY PTS", f"{fantasy:+d}" if fantasy else "0", accent, "from fielding"),
    ]
    for i, (label, value, tone, sub) in enumerate(tiles):
        x = 1050 + (i % 2)*250
        y = 146 + (i // 2)*206
        _tile(page, d, (x, y, x+240, y+190), label, str(value), tone, sub)

    # --- bottom-left: recent matches (two bars per match)
    _text(d, (76, 606), "RECENT MATCHES", 17, accent)
    recent = data.get("recent") or []
    if not recent:
        _text(d, (420, 740), "No match records yet", 20, DIM, "mm")
    else:
        slot = 680 // max(5, len(recent))
        top = max([max(r["taken"], r["dropped"]) for r in recent] + [1])
        base_y = 800
        for i, r in enumerate(recent):
            cx = 90 + slot*i + slot//2
            for j, (val, tone) in enumerate(((r["taken"], WIN), (r["dropped"], LOSS))):
                bx = cx - 34 + j*38
                height = int(120 * val / top)
                d.rounded_rectangle((bx, base_y-height, bx+30, base_y), radius=6, fill=tone if val else (36, 52, 84))
                _text(d, (bx+15, base_y-height-6), val, 16, WHITE, "ms")
            _text(d, (cx, base_y+12), _clip(d, r["label"], slot-8, 14), 14, MUTED, "ma")

    # --- bottom-right: team fielders
    _text(d, (836, 606), _clip(d, f"TOP FIELDERS  •  {data.get('team_name') or 'SERVER'}".upper(), 680, 17), 17, accent)
    top_rows = data.get("top") or []
    if not top_rows:
        _text(d, (1180, 740), "No ranked fielders yet", 20, DIM, "mm")
    for i, row in enumerate(top_rows[:5]):
        y = 640 + i*44
        me = row["me"]
        if me:
            d.rounded_rectangle((830, y, 1530, y+38), radius=12, fill=(28, 42, 72), outline=accent, width=1)
        _text(d, (850, y+19), f"{i+1:02d}", 15, accent, "lm")
        _text(d, (896, y+19), _clip(d, row["name"], 330, 19), 19, WHITE, "lm")
        bar_x, bar_w = 1240, 150
        d.rounded_rectangle((bar_x, y+14, bar_x+bar_w, y+24), radius=5, fill=(30, 44, 72))
        d.rounded_rectangle((bar_x, y+14, bar_x+max(6, int(bar_w*row["eff"]/100)), y+24), radius=5,
                            fill=WIN if row["eff"] >= 50 else LOSS)
        _text(d, (1520, y+19), f"{row['caught']} CT", 17, WHITE, "rm")
    _text(d, (800, 884), "Fielding tracker", 14, DIM, "mm")

    out = io.BytesIO()
    page.image.save(out, "PNG", compress_level=3)
    return out.getvalue()


# --------------------------------------------------------------------------- design 2

def _gauge(size, pct, color, track, width):
    """Half-circle gauge (open at the bottom), supersampled for clean edges."""
    ss = 3
    layer = Image.new("RGBA", (size*ss, size*ss//2 + width*ss), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    box = (width*ss//2, width*ss//2, size*ss - width*ss//2, size*ss - width*ss//2)
    ld.arc(box, start=180, end=360, fill=track + (255,), width=width*ss)
    if pct > 0:
        ld.arc(box, start=180, end=180 + 180*min(1.0, pct), fill=color + (255,), width=width*ss)
    return layer.resize((size, (size//2) + width), Image.Resampling.LANCZOS)


def _tier(eff, chances, min_chances):
    if chances < min_chances:
        return "NEW FIELDER", (150, 170, 205)
    if eff >= 85:
        return "ELITE HANDS", (62, 224, 140)
    if eff >= 70:
        return "SAFE HANDS", (90, 200, 255)
    if eff >= 50:
        return "WORK IN PROGRESS", (255, 196, 80)
    return "BUTTERFINGERS", (255, 96, 120)


def _versus(d, x, y, w, label, mine, theirs, unit, tone):
    """My value vs the team average as two stacked bars on a shared scale."""
    top = max(mine, theirs, 1e-9)
    _text(d, (x, y), label, 15, MUTED)
    for i, (value, fill, tag) in enumerate(((mine, tone, "YOU"), (theirs, (96, 116, 160), "TEAM"))):
        by = y + 28 + i*30
        d.rounded_rectangle((x+58, by, x+w-110, by+18), radius=9, fill=(26, 38, 64))
        d.rounded_rectangle((x+58, by, x+58+max(8, int((w-168)*value/top)), by+18), radius=9, fill=fill)
        _text(d, (x, by+9), tag, 13, DIM, "lm")
        _text(d, (x+w, by+9), f"{value:.1f}{unit}", 17, WHITE, "rm")


def render_fielding_image_v2(data):
    color = tuple(data.get("team_color") or DEFAULT_ACCENT)
    accent = _visible_accent(color)
    theme = {"accent": accent, "accent2": _lighten(accent, 0.35), "bg": _backdrop_color(color), "logo": None}
    page = _Page(theme)
    tier, tier_color = _tier(data["eff"], data["chances"], data["min_chances"])

    page.card((50, 22, 1550, 118), 26)
    page.card((50, 138, 600, 600), 26)
    page.card((620, 138, 1090, 600), 26)
    page.card((1110, 138, 1550, 600), 26)
    page.card((50, 620, 1550, 868), 26)
    d = page.seal()

    avatar = _open(data.get("avatar_bytes"))
    _paste(page.image, _round_image(avatar, 70, ring=tier_color, ring_w=4, initial=data["name"][:1]), (74, 32))
    _text(d, (170, 36), f"FIELDING REPORT  •  {(data.get('team_name') or 'FREE AGENT').upper()}", 15, accent)
    _text(d, (170, 62), data["name"].upper(), _fit(d, data["name"].upper(), 820, 38, 22, "head"), WHITE, kind="head")
    width = int(d.textlength(tier, font=font(22))) + 44
    _pill(d, 1550 - 28 - width, 44, tier, 22, fill=tier_color, fg=INK, pad=22, h=44)
    if data.get("rank"):
        rank = f"#{data['rank'][0]} OF {data['rank'][1]}"
        _pill(d, 1550 - 28 - width - 16 - int(d.textlength(rank, font=font(22))) - 44, 44, rank, 22,
              outline=accent, fg=WHITE, pad=22, h=44)

    # --- hero gauge
    eff = data["eff"]
    gauge_color = WIN if eff >= 50 or not data["chances"] else LOSS
    _paste(page.image, _gauge(440, eff/100, gauge_color, (30, 44, 72), 36), (105, 190))
    _text(d, (325, 380), f"{eff:.0f}%", 104, WHITE, "mm", "head")
    _text(d, (325, 450), "CATCH SUCCESS RATE", 17, MUTED, "mm")
    d.line((100, 492, 550, 492), fill=(40, 58, 94), width=2)
    for i, (value, label, tone) in enumerate((
        (data["caught"], "CAUGHT", WIN), (data["dropped"], "DROPPED", LOSS), (data["chances"], "CHANCES", WHITE),
    )):
        cx = 175 + i*150
        _text(d, (cx, 538), value, 46, tone, "mm", "head")
        _text(d, (cx, 578), label, 14, MUTED, "mm")

    # --- fantasy breakdown + versus team
    _text(d, (650, 158), "FANTASY IMPACT", 17, accent)
    plus, minus = data["caught"]*5, data["dropped"]*10
    net = plus - minus
    _text(d, (650, 190), f"{net:+d}", 76, WIN if net >= 0 else LOSS, kind="head")
    _text(d, (650, 280), "net points from fielding", 16, MUTED)
    span = max(plus + minus, 1)
    d.rounded_rectangle((650, 312, 1060, 340), radius=14, fill=(26, 38, 64))
    plus_w = int(410*plus/span)
    if plus:
        d.rounded_rectangle((650, 312, 650+max(14, plus_w), 340), radius=14, fill=WIN)
    if minus:
        d.rounded_rectangle((1060-max(14, 410-plus_w), 312, 1060, 340), radius=14, fill=LOSS)
    _text(d, (650, 352), f"+{plus}  catches (x5)", 15, WIN)
    _text(d, (1060, 352), f"-{minus}  drops (x10)", 15, LOSS, "ra")
    if data.get("team_eff") is not None:
        _versus(d, 650, 388, 410, "SUCCESS RATE vs TEAM AVERAGE", eff, data["team_eff"], "%", gauge_color)
        _versus(d, 650, 474, 410, "CATCHES vs TEAM PER FIELDER", data["caught"], data.get("team_avg") or 0, "", accent)
    else:
        _text(d, (855, 470), "Not on a team yet", 20, DIM, "mm")
    if data.get("drop_runs"):
        drops, cost = data["drop_runs"]
        _text(d, (650, 572), f"{drops} tracked drop(s) cost {cost} runs", 15, LOSS)

    # --- recent form: pips per match
    _text(d, (1136, 158), "RECENT FORM", 17, accent)
    recent = data.get("recent") or []
    if not recent:
        _text(d, (1330, 380), "No match records yet", 20, DIM, "mm")
    for i, r in enumerate(reversed(recent[-5:])):
        y = 200 + i*78
        d.rounded_rectangle((1132, y, 1528, y+66), radius=16, fill=(18, 28, 50), outline=(40, 58, 94))
        _text(d, (1152, y+33), r["label"], 18, WHITE, "lm")
        px = 1262
        for _ in range(min(r["taken"], 6)):
            d.ellipse((px, y+21, px+24, y+45), fill=WIN)
            px += 28
        for _ in range(min(r["dropped"], 4)):
            d.ellipse((px, y+21, px+24, y+45), fill=LOSS)
            px += 28
        if not r["taken"] and not r["dropped"]:
            _text(d, (1262, y+33), "no chances", 15, DIM, "lm")

    # --- leaderboard strip
    _text(d, (76, 640), _clip(d, f"TEAM FIELDING LEADERS  •  {data.get('team_name') or 'SERVER'}".upper(), 900, 17), 17, accent)
    rows = (data.get("top") or [])[:5]
    if not rows:
        _text(d, (800, 770), "No ranked fielders yet", 20, DIM, "mm")
    cell = 1440 // 5
    for i, row in enumerate(rows):
        x = 70 + i*(cell+0)
        box = (x, 676, x+cell-14, 848)
        fill = (28, 42, 72) if row["me"] else (16, 25, 46)
        d.rounded_rectangle(box, radius=20, fill=fill, outline=accent if row["me"] else (40, 58, 94), width=2 if row["me"] else 1)
        _text(d, (x+22, 696), f"#{i+1}", 30, accent, kind="head")
        _text(d, (x+22, 744), _clip(d, row["name"], cell-60, 22), 22, WHITE)
        _text(d, (x+22, 784), f"{row['caught']} catches", 18, MUTED)
        d.rounded_rectangle((x+22, 820, x+cell-36, 828), radius=4, fill=(30, 44, 72))
        d.rounded_rectangle((x+22, 820, x+22+max(6, int((cell-58)*row["eff"]/100)), 828), radius=4,
                            fill=WIN if row["eff"] >= 50 else LOSS)
    out = io.BytesIO()
    page.image.save(out, "PNG", compress_level=3)
    return out.getvalue()


# --------------------------------------------------------------------------- design 3 (hero style)

def _tile3(d, x, y, label, value, color):
    _text(d, (x, y), label, 14, MUTED)
    _text(d, (x, y+18), value, 52, color, kind="head")


def render_fielding_image_v3(data):
    color = tuple(data.get("team_color") or DEFAULT_ACCENT)
    accent = _visible_accent(color)
    accent2 = _lighten(accent, 0.35)
    theme = {"accent": accent, "accent2": accent2, "bg": _backdrop_color(color), "logo": None}
    page = _Page(theme)
    tier, tier_color = _tier(data["eff"], data["chances"], data["min_chances"])

    for box in ((50, 26, 1550, 336), (50, 356, 790, 596), (810, 356, 1550, 596),
                (50, 616, 530, 852), (560, 616, 1040, 852), (1070, 616, 1550, 852)):
        page.card(box, 26)
    d = page.seal()

    avatar = _open(data.get("avatar_bytes"))
    recent = data.get("recent") or []
    eff = data["eff"]
    tone = WIN if eff >= 50 or not data["chances"] else LOSS

    # ---- hero
    d.rounded_rectangle((50, 26, 60, 336), radius=5, fill=accent)
    _paste(page.image, _round_image(avatar, 226, ring=tier_color, ring_w=7, initial=data["name"][:1]), (92, 66))
    _text(d, (362, 50), "FIELDING PROFILE", 17, accent)
    name = data["name"].upper()
    _text(d, (362, 74), _clip(d, name, 640, 84, "head"), _fit(d, name, 640, 84, 40, "head"), WHITE, kind="head")
    x = 362
    for label, fill, fg, outline in (
        ((data.get("team_name") or "FREE AGENT").upper(), None, WHITE, accent),
        (tier, tier_color, INK, None),
        *([(f"RANK #{data['rank'][0]} OF {data['rank'][1]}", accent, INK, None)] if data.get("rank") else []),
    ):
        x += _pill(d, x, 196, _clip(d, label, 300, 20), 20, fill=fill, fg=fg, outline=outline, pad=18, h=40) + 12
    if data.get("share") is not None:
        _text(d, (364, 262), f"{data['share']:.1f}% of all team catches", 20, MUTED)
    elif data["chances"] < data["min_chances"]:
        _text(d, (364, 262), f"Needs {data['min_chances']}+ chances to be ranked", 20, MUTED)

    _paste(page.image, _donut(196, eff/100, tone, (30, 44, 72), 16), (1018, 60))
    _text(d, (1116, 150), f"{eff:.0f}%", 44, WHITE, "mm", "head")
    _text(d, (1116, 188), "SUCCESS", 14, MUTED, "mm")
    net = data["caught"]*5 - data["dropped"]*10
    for i, (label, value, tint) in enumerate((
        ("CAUGHT", data["caught"], WIN), ("DROPPED", data["dropped"], LOSS),
        ("CHANCES", data["chances"], WHITE), ("FANTASY", f"{net:+d}", accent2),
    )):
        _tile3(d, 1250 + (i % 2)*145, 70 + (i // 2)*98, label, str(value), tint)
    _text(d, (1010, 282), "FORM", 15, MUTED, "lm")
    for i, r in enumerate(recent[-5:]):
        cx = 1100 + i*44
        clean = r["dropped"] == 0
        d.ellipse((cx-17, 265, cx+17, 299), fill=WIN if clean else LOSS)
        _text(d, (cx, 282), r["taken"], 17, INK, "mm")

    # ---- fantasy impact
    plus, minus = data["caught"]*5, data["dropped"]*10
    d.rounded_rectangle((50, 376, 58, 576), radius=4, fill=WIN if net >= 0 else LOSS)
    _text(d, (82, 376), "FANTASY IMPACT", 17, accent, kind="bold")
    _text(d, (82, 410), f"{net:+d}", 110, WIN if net >= 0 else LOSS, kind="head")
    _text(d, (82, 536), "net points from fielding", 18, MUTED)
    span = max(plus + minus, 1)
    bx, bw = 400, 360
    for i, (label, value, fill, per) in enumerate((("CATCHES x5", plus, WIN, "+"), ("DROPS x10", minus, LOSS, "-"))):
        y = 420 + i*84
        _text(d, (bx, y), label, 15, MUTED)
        _text(d, (bx+bw, y), f"{per}{value}", 24, fill, "ra", "head")
        d.rounded_rectangle((bx, y+36, bx+bw, y+52), radius=8, fill=(26, 38, 64))
        if value:
            d.rounded_rectangle((bx, y+36, bx+max(14, int(bw*value/span)), y+52), radius=8, fill=fill)

    # ---- versus team
    _text(d, (842, 376), "VS TEAM AVERAGE", 17, accent)
    if data.get("team_eff") is not None:
        _versus(d, 842, 404, 660, "SUCCESS RATE", eff, data["team_eff"], "%", tone)
        _versus(d, 842, 480, 660, "CATCHES PER FIELDER", data["caught"], data.get("team_avg") or 0, "", accent)
    else:
        _text(d, (1180, 480), "Not on a team yet", 22, DIM, "mm")
    if data.get("drop_runs"):
        _text(d, (842, 574), f"{data['drop_runs'][0]} tracked drop(s) cost {data['drop_runs'][1]} runs", 16, LOSS)

    # ---- recent matches (grouped bars)
    d.ellipse((76, 640, 88, 652), fill=WIN)
    _text(d, (100, 636), "RECENT MATCHES", 17, WIN)
    if not recent:
        _text(d, (290, 740), "No match records yet", 20, DIM, "mm")
    else:
        slot = 420 // max(5, len(recent))
        top = max([max(r["taken"], r["dropped"]) for r in recent] + [1])
        for i, r in enumerate(recent[-5:]):
            cx = 80 + slot*i + slot//2
            for j, (val, tint) in enumerate(((r["taken"], WIN), (r["dropped"], LOSS))):
                bx2 = cx - 28 + j*30
                h = int(90*val/top)
                d.rounded_rectangle((bx2, 800-h, bx2+24, 800), radius=6, fill=tint if val else (36, 52, 84))
                _text(d, (bx2+12, 794-h), val, 15, WHITE, "ms")
            _text(d, (cx, 812), r["label"], 13, MUTED, "ma")

    # ---- team leaders
    d.ellipse((586, 640, 598, 652), fill=(156, 128, 255))
    _text(d, (610, 636), "TEAM LEADERS", 17, (156, 128, 255))
    rows = (data.get("top") or [])[:3]
    if not rows:
        _text(d, (800, 740), "No ranked fielders yet", 20, DIM, "mm")
    for i, row in enumerate(rows):
        y = 676 + i*56
        if row["me"]:
            d.rounded_rectangle((576, y-4, 1024, y+48), radius=14, fill=(28, 42, 72), outline=accent, width=1)
        _paste(page.image, _round_image(None, 40, ring=None, initial=row["name"][:1]), (592, y))
        _text(d, (646, y+20), _clip(d, row["name"], 220, 21), 21, WHITE, "lm")
        _text(d, (1006, y+20), f"{row['caught']} CT", 20, accent2, "rm", "head")
        d.rounded_rectangle((870, y+34, 1000, y+40), radius=3, fill=(30, 44, 72))
        d.rounded_rectangle((870, y+34, 870+max(5, int(130*row['eff']/100)), y+40), radius=3,
                            fill=WIN if row["eff"] >= 50 else LOSS)

    # ---- highlights
    d.ellipse((1096, 640, 1108, 652), fill=(255, 196, 80))
    _text(d, (1120, 636), "HIGHLIGHTS", 17, (255, 196, 80))
    best = max(recent, key=lambda r: (r["taken"], -r["dropped"]), default=None)
    clean = sum(1 for r in recent if r["dropped"] == 0 and r["taken"] > 0)
    for i, (label, value, sub, tint) in enumerate((
        ("BEST MATCH", f"{best['taken']} catches" if best else "—", best["label"] if best else "", WIN),
        ("CLEAN MATCHES", f"{clean} of {len(recent)}" if recent else "—", "no drops", accent2),
        ("RUNS COST BY DROPS", str(data["drop_runs"][1]) if data.get("drop_runs") else "0", "from tracked drops", LOSS),
    )):
        y = 676 + i*56
        _text(d, (1096, y), label, 13, MUTED)
        _text(d, (1096, y+16), value, 26, tint, kind="head")
        _text(d, (1520, y+30), sub, 14, DIM, "rm")
    _text(d, (60, 880), f"GOOGLI MANAGER  •  FIELDING PROFILE  •  {data['name'].upper()}"[:80], 13, DIM, "lm")

    out = io.BytesIO()
    page.image.save(out, "PNG", compress_level=3)
    return out.getvalue()
