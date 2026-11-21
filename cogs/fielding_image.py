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
