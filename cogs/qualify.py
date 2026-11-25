"""+qualify [team]: qualification chances for the team's group, in four pages.

Overview      chance split into outright / through a tie-breaker, what the team needs, best and worst case
Matches       the team's remaining matches (chance if it wins / loses each) and the results elsewhere that matter
Tie-breaker   how the round-robin tie-breaker would go: who is level, how many matches, the team's odds
Group         every team: chance, tie-breaker chance, status (and the special benefit, if one is set)

Inputs (all already in the bot): the group table and results of the active stage (points table), the saved
fixture list of the group (+generateschedule), the number of qualifying places per group
(+qualifyspots <n>, default 2).

Tie-breaker rule: when teams are level on points at the cut and there are fewer places than teams, the level
teams play a round-robin among themselves (each team once against each other), most wins goes through, and
a new round is played if teams are still level. The maths is in qualify_engine.py.
"""
import asyncio
import io
import sqlite3
import sys
import types
from itertools import combinations

import discord
from discord.ext import commands

from . import qualify_engine as engine
from .awards import _glow_text, _halo, _icon, _tile_glass
from .h2h import ImagePager, on_cooldown, render_png, team_role_color
from .qualify_engine import win_chance
from .teaminfo_image import (
    DEFAULT_ACCENT, DIM, MUTED, WHITE, _clip, _fit, _mix, _open, _paste, _pill, _round_image, _text, font,
)
from .utility import owner_or_whitelisted
from .versus_image import SIDE_B_FALLBACK, _Canvas, _footer, _png, _title

GREEN, RED, AMBER, BLUE, VIOLET = (62, 224, 172), (240, 95, 95), (255, 206, 90), (100, 185, 255), (190, 140, 255)
ORANGE = (255, 150, 70)
_INK = (12, 18, 32)
DEFAULT_SPOTS = 2
PAGES = ("Overview", "Matches", "Tie-breaker", "Group")


# =========================================================================== wording

def pct_text(v):
    if v is None:
        return "—"
    if v >= 0.995:
        return "100%"
    if v <= 0.005:
        return "0%"
    return f"{v * 100:.0f}%" if 0.01 <= v <= 0.99 else f"{v * 100:.1f}%"


def status(through, outright=None, tied=0.0):
    """(label, colour) for a team from its chance of going through."""
    outright = through if outright is None else outright
    if through >= 0.995:
        return "QUALIFIED", GREEN
    if through + tied <= 0.005 or (through <= 0.005 and tied <= 0.005):
        return "ELIMINATED", RED
    if outright < 0.02 and through >= 0.03:
        return "TIE-BREAKER ROUTE", AMBER
    if through >= 0.7:
        return "STRONG POSITION", GREEN
    if through >= 0.4:
        return "IN THE MIX", AMBER
    if through >= 0.1:
        return "LONG SHOT", ORANGE
    return "NEEDS A MIRACLE", RED


def row_note(row):
    k = row["left"]
    if row["chance"] >= 0.995:
        return "Qualified" if not k else f"Strong position — {k} to play"
    if row["chance"] <= 0.005 and row["tied"] <= 0.005:
        return "Cannot qualify"
    if row["outright"] < 0.02 and row["tied"] >= 0.05:
        return "Likely to end level — the tie-breaker decides"
    if row["tied"] >= 0.15:
        return "Likely to end level — the tie-breaker decides"
    return (f"{'Strong position' if row['chance'] >= 0.7 else 'Needs results to go its way'} — {k} to play" if k
            else "All matches played")


def need_lines(t):
    """Plain-English 'what you need' sentences for the team page."""
    k, scen = t["left"], t["scen"]
    if not k:
        label = t["status"][0]
        return [f"No matches left. Final result: {label.lower()}."] + (
            [f"It finishes level at the cut in {pct_text(t['tied'])} of outcomes and goes to a tie-breaker."]
            if t["tied"] >= 0.005 else [])
    lines = []
    sure = next((w for w, s in sorted(scen, key=lambda x: x[0]) if s["outright"] >= 0.995), None)
    if sure is None:
        best = scen[0][1]
        lines.append(f"Even winning all {k} you can't be sure: {pct_text(best['through'])} (needs other results).")
    elif sure == k:
        lines.append(f"Win all {k} of your {k} matches and you're through for sure." if k > 1
                     else "Win your last match and you're through for sure.")
    else:
        lines.append(f"Win {sure} of your {k} matches and you're through for sure.")
    zero = dict(scen)[0]
    if zero["through"] <= 0.005:
        lines.append("Lose every match and you're out.")
    else:
        lines.append(f"Even losing all {k} you'd still have a {pct_text(zero['through'])} chance (needs help).")
    if t["tied"] >= 0.005:
        win = t["tie_win"]
        lines.append(f"{pct_text(t['tied'])} of outcomes leave you level at the cut: a tie-breaker, "
                     f"which you'd win {pct_text(win)} of the time.")
    return lines


# =========================================================================== drawing

def _gauge(c, cx, cy, r, value, color):
    """Round progress ring drawn 3x and scaled down so it is smooth."""
    from PIL import Image, ImageDraw
    s, w = 3, 22
    size = (r * 2 + 40) * s
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    g = ImageDraw.Draw(layer)
    box = (20 * s, 20 * s, (r * 2 + 20) * s, (r * 2 + 20) * s)
    g.ellipse(box, outline=_mix(color, _INK, 0.8) + (255,), width=w * s)
    if value > 0:
        g.arc(box, -90, -90 + 360 * min(1, max(0.004, value)), fill=color + (255,), width=w * s)
    layer = layer.resize((r * 2 + 40, r * 2 + 40), Image.Resampling.LANCZOS)
    c.image.paste(layer, (cx - r - 20, cy - r - 20), layer)


def _header(c, data, page):
    d = c.d
    _title(d, (76, 58), "QUALIFICATION", 50, WHITE, "lm", "head")
    for i in range(380):
        d.line((76 + i, 90, 76 + i, 93), fill=_mix(DEFAULT_ACCENT, SIDE_B_FALLBACK, i / 379))
    _text(d, (1524, 44), f"{data.get('group', '')}  •  TOP {data.get('spots', '')} QUALIFY  •  "
          f"{data.get('left_total', 0)} MATCHES LEFT".upper(), 16, MUTED, "rm")
    _text(d, (1524, 64), str(data.get("guild", "")).upper(), 13, DIM, "rm")
    pages = len(PAGES) if data.get("team") else 1
    for n in range(pages):
        cx = 1524 - (pages - 1 - n) * 22
        d.ellipse((cx - 6, 78, cx + 6, 90), fill=(255, 255, 255) if n == page else (70, 86, 120))
    if pages > 1:
        _text(d, (1524 - (pages - 1) * 22 - 18, 84), PAGES[page].upper(), 12, MUTED, "rm", "bold")


def _tile_title(c, box, color, title, subtitle, icon):
    x1, y1, x2, y2 = box
    _tile_glass(c, box, color)
    c.d.ellipse((x1 + 28, y1 + 28, x1 + 40, y1 + 40), fill=color)
    _text(c.d, (x1 + 54, y1 + 20), title, 26, color, "la", "head")
    _text(c.d, (x1 + 28, y1 + 62), _clip(c.d, subtitle, x2 - x1 - 140, 14), 14, MUTED)
    _icon(c, icon, x2 - 52, y1 + 50, color)


def _canvas(color, tiles):
    c = _Canvas(color or DEFAULT_ACCENT, SIDE_B_FALLBACK)
    c.card((50, 22, 1550, 106), 24, 170)
    for b in tiles:
        c.card(b, 26, 150)
    c.seal()
    return c


def _chip(d, x, y, text, color, size=15, h=32, fill=True):
    w = int(d.textlength(text, font=font(size, "head"))) + 28
    _pill(d, x, y, text, size, fill=_mix(color, _INK, 0.7) if fill else None, fg=WHITE, outline=color, pad=14, h=h,
          kind="head")
    return w


def _stack_bar(d, x1, x2, y, h, parts):
    """parts [(share, colour)] left to right, rest dark."""
    d.rounded_rectangle((x1, y, x2, y + h), radius=h // 2, fill=_mix(BLUE, _INK, 0.88))
    x = x1
    total = x2 - x1
    for share, color in parts:
        w = int(total * share)
        if w >= 3:
            d.rounded_rectangle((x, y, min(x2, x + max(h, w)), y + h), radius=h // 2, fill=color) if x == x1 else \
                d.rectangle((x, y, min(x2, x + w), y + h), fill=color)
        x += w


def _page_overview(data):
    t = data["team"]
    c = _canvas(t.get("color"), [(50, 126, 790, 484), (810, 126, 1550, 484), (50, 498, 790, 856), (810, 498, 1550, 856)])
    d = c.d
    _header(c, data, 0)
    state, color = t["status"]

    x1, y1, x2, y2 = 50, 126, 790, 484
    _tile_title(c, (x1, y1, x2, y2), color, "QUALIFICATION CHANCE",
                f"TOP {data['spots']} OF {data['group'].upper()} QUALIFY  •  LEVEL AT THE CUT = TIE-BREAKER", "star")
    gx, gy, r = x1 + 170, y1 + 196, 88
    _halo(c, gx, gy, r, color)
    _gauge(c, gx, gy, r, t["chance"], color)
    value = pct_text(t["chance"])
    _glow_text(c, (gx - d.textlength(value, font=font(46, "head")) / 2, gy - 30), value, 46, color)
    tx = x1 + 300
    logo = _round_image(_open(t.get("logo")), 54, ring=color, ring_w=3, initial=t["name"][:1])
    _paste(c.image, logo, (tx, y1 + 104))
    name = t["name"].upper()
    size = _fit(d, name, x2 - tx - 96, 26, 16, "head")
    _title(d, (tx + 72, y1 + 116), _clip(d, name, x2 - tx - 96, size, "head"), size, WHITE, "la", "head")
    _chip(d, tx, y1 + 176, state, color, 16, 36)
    for i, line in enumerate((f"RANK #{t['rank']} OF {t['of']}", f"{t['w']}W – {t['l']}L  •  {t['pts']} PTS",
                              f"{t['left']} MATCH{'ES' if t['left'] != 1 else ''} LEFT")):
        _text(d, (tx, y1 + 230 + i * 27), line, 17, WHITE if i == 0 else MUTED, "la", "bold")
    via = max(0.0, t["chance"] - t["outright"])
    _stack_bar(d, x1 + 30, x2 - 30, y1 + 306, 14, [(t["outright"], GREEN), (via, AMBER)])
    _text(d, (x1 + 30, y1 + 326), f"OUTRIGHT {pct_text(t['outright'])}", 13, GREEN, "la", "bold")
    _text(d, ((x1 + x2) // 2, y1 + 326), f"VIA TIE-BREAKER {pct_text(via)}", 13, AMBER, "ma", "bold")
    _text(d, (x2 - 30, y1 + 326), f"OUT {pct_text(max(0.0, 1 - t['chance']))}", 13, RED, "ra", "bold")

    # ---- what you need
    x1, y1, x2, y2 = 810, 126, 1550, 484
    _tile_title(c, (x1, y1, x2, y2), BLUE, "WHAT YOU NEED", "PLAIN ENGLISH, THEN EVERY NUMBER OF WINS", "bars")
    for i, line in enumerate(need_lines(t)[:3]):
        _text(d, (x1 + 30, y1 + 92 + i * 27), _clip(d, line, x2 - x1 - 60, 16), 16, WHITE if i == 0 else MUTED, "la",
              "bold" if i == 0 else "reg")
    rows = t["scen"]
    k = t["left"]
    if len(rows) > 4:
        rows = rows[:3] + [rows[-1]]
    for i, (w, s) in enumerate(rows):
        y = y1 + 200 + i * 40
        label = "NO MATCHES LEFT" if k == 0 else (f"Win all {k}" if w == k and k > 1 else
                                                  f"Lose all {k}" if w == 0 else f"Win {w} of {k}")
        _text(d, (x1 + 30, y + 16), label, 17, WHITE, "lm", "bold")
        ch = s["through"]
        sure = s["outright"] >= 0.995
        text, col = (("THROUGH FOR SURE", GREEN) if sure else ("OUT", RED) if ch <= 0.005
                     else (f"{pct_text(ch)} TO QUALIFY", status(ch, s["outright"], s["tied"])[1]))
        _chip(d, x1 + 190, y, text, col, 14, 32)
        if not sure and ch > 0.005:
            extra = f"{pct_text(s['outright'])} outright"
            if s["tied"] >= 0.005:
                extra += f" · +{pct_text(max(0.0, ch - s['outright']))} via tie-breaker"
            _text(d, (x2 - 30, y + 16), _clip(d, extra, 260, 13), 13, MUTED, "rm")

    # ---- outlook
    x1, y1, x2, y2 = 50, 498, 790, 856
    _tile_title(c, (x1, y1, x2, y2), GREEN, "OUTLOOK", "WHAT THE REMAINING MATCHES CAN DO TO THE POINTS", "up")
    cells = [("BEST CASE", f"{t['best']} PTS", GREEN), ("WORST CASE", f"{t['worst']} PTS", RED),
             ("MATCHES LEFT", str(t["left"]), WHITE),
             ("IN YOUR HANDS", "YES" if t["scen"][0][1]["outright"] >= 0.995 else "NO",
              GREEN if t["scen"][0][1]["outright"] >= 0.995 else AMBER)]
    for i, (label, val, col) in enumerate(cells):
        bx = x1 + 30 + (i % 2) * 340
        by = y1 + 100 + (i // 2) * 120
        d.rounded_rectangle((bx, by, bx + 320, by + 104), radius=18, fill=_mix(col, _INK, 0.9),
                            outline=_mix(col, _INK, 0.55), width=1)
        _text(d, (bx + 20, by + 18), label, 13, MUTED)
        _text(d, (bx + 20, by + 44), val, 36, col, "la", "head")

    # ---- special benefit or tie-breaker pointer
    x1, y1, x2, y2 = 810, 498, 1550, 856
    if True:
        _tile_title(c, (x1, y1, x2, y2), AMBER, "TIE-BREAKER RISK",
                    "SEE THE TIE-BREAKER PAGE FOR THE LINE-UPS", "bars")
        _glow_text(c, (x1 + 30, y1 + 108), pct_text(t["tied"]), 74, AMBER)
        _text(d, (x1 + 30, y1 + 200), "OF OUTCOMES LEAVE YOU LEVEL ON POINTS AT THE CUT", 14, MUTED)
        win = t["tie_win"]
        _text(d, (x1 + 30, y1 + 252), "YOU WOULD WIN IT" if win is not None else "", 14, MUTED)
        _title(d, (x1 + 30, y1 + 274), pct_text(win) if win is not None else "", 40, WHITE, "la", "head")
    _footer(c, f"GOOGLI MANAGER  •  QUALIFY  •  {t['name']}"[:90], data.get("guild"))
    return c.image


def _page_matches(data):
    t = data["team"]
    c = _canvas(t.get("color"), [(50, 126, 790, 856), (810, 126, 1550, 856)])
    d = c.d
    _header(c, data, 1)
    x1, y1, x2, y2 = 50, 126, 790, 856
    _tile_title(c, (x1, y1, x2, y2), AMBER, "YOUR REMAINING MATCHES",
                "CHANCE OF QUALIFYING AFTER EACH RESULT (OTHER MATCHES STILL COUNT)", "up")
    swings = sorted(t["swings"], key=lambda s: -(s["win"]["through"] - s["lose"]["through"]))[:5]
    if not swings:
        _text(d, ((x1 + x2) // 2, (y1 + y2) // 2 + 20), "NO REMAINING MATCHES", 24, DIM, "mm", "head")
    for i, s in enumerate(swings):
        y = y1 + 104 + i * 124
        d.rounded_rectangle((x1 + 24, y, x2 - 24, y + 112), radius=18, fill=_mix(AMBER, _INK, 0.9),
                            outline=_mix(AMBER, _INK, 0.6), width=1)
        _text(d, (x1 + 46, y + 20), f"MATCH {i + 1}", 12, MUTED)
        _text(d, (x1 + 46, y + 40), _clip(d, "VS " + s["opp"].upper(), 330, 24, "head"), 24, WHITE, "la", "head")
        _text(d, (x1 + 46, y + 78), f"YOUR WIN CHANCE {s['p'] * 100:.0f}%", 13, MUTED)
        for j, (lab, v, col) in enumerate((("IF YOU WIN", s["win"], GREEN), ("IF YOU LOSE", s["lose"], RED))):
            bx = x1 + 400 + j * 160
            _text(d, (bx, y + 24), lab, 12, col, "la", "bold")
            _text(d, (bx, y + 44), pct_text(v["through"]), 34, WHITE, "la", "head")
            if v["tied"] >= 0.005:
                _text(d, (bx, y + 88), f"{pct_text(v['tied'])} tie-break", 11, MUTED)
    x1, y1, x2, y2 = 810, 126, 1550, 856
    _tile_title(c, (x1, y1, x2, y2), BLUE, "RESULTS ELSEWHERE THAT MATTER",
                "HOW MUCH EACH OTHER MATCH CHANGES YOUR CHANCE", "bars")
    rows = [e for e in t["elsewhere"] if e["swing"] >= 0.005][:7]
    if not rows:
        _text(d, ((x1 + x2) // 2, (y1 + y2) // 2 + 20), "NOTHING ELSEWHERE CHANGES YOUR CHANCE", 20, DIM, "mm", "head")
    for i, e in enumerate(rows):
        y = y1 + 104 + i * 88
        _text(d, (x1 + 30, y + 6), _clip(d, f"{e['winner']} beating {e['loser']}".upper(), 460, 18, "head"), 18, WHITE,
              "la", "head")
        _text(d, (x1 + 30, y + 36), f"YOUR CHANCE {pct_text(e['if_win'])} IF IT HAPPENS  •  {pct_text(e['if_lose'])} IF NOT", 12, MUTED)
        _chip(d, x2 - 30 - 150, y + 8, f"+{e['swing'] * 100:.0f}% FOR YOU", GREEN, 14, 34)
        d.line((x1 + 30, y + 74, x2 - 30, y + 74), fill=_mix(BLUE, _INK, 0.85), width=1)
    _footer(c, f"GOOGLI MANAGER  •  QUALIFY  •  {t['name']}"[:90], data.get("guild"))
    return c.image


def _page_tiebreak(data):
    t = data["team"]
    c = _canvas(t.get("color"), [(50, 126, 790, 856), (810, 126, 1550, 440), (810, 454, 1550, 856)])
    d = c.d
    _header(c, data, 2)
    scenes = t["scenes"] or data.get("all_scenes") or []
    main = scenes[0] if scenes else None

    x1, y1, x2, y2 = 50, 126, 790, 856
    _tile_title(c, (x1, y1, x2, y2), AMBER, "TIE-BREAKER CHANCES — ALL TEAMS",
                "LEVEL AT THE CUT: EVERYONE PLAYS EVERYONE ONCE, MOST WINS GOES THROUGH", "bars")
    table = data["table"]
    for label, x in (("TEAM", x1 + 84), ("LEVEL", x1 + 440), ("WIN IT", x1 + 540), ("THROUGH", x1 + 640)):
        _text(d, (x, y1 + 104), label, 12, MUTED)
    n = max(1, len(table))
    step = min(66, (y2 - y1 - 190) // n)
    for i, row in enumerate(table):
        y = y1 + 140 + i * step + step // 2
        if i:
            d.line((x1 + 28, y - step // 2, x2 - 28, y - step // 2), fill=_mix(AMBER, _INK, 0.85), width=1)
        mine = row["name"] == t["name"]
        _text(d, (x1 + 34, y), str(row["rank"]), 20, AMBER if mine else MUTED, "lm", "head")
        logo = _round_image(_open(row.get("logo")), 30, ring=row["color"], ring_w=2, initial=row["name"][:1])
        _paste(c.image, logo, (x1 + 84, y - logo.size[1] // 2))
        _text(d, (x1 + 126, y - 8), _clip(d, row["name"].upper(), 300, 17, "head"), 17, AMBER if mine else WHITE, "lm", "head")
        _text(d, (x1 + 126, y + 14), f"{row['pts']} PTS  •  {row['left']} LEFT", 11, MUTED, "lm")
        lv = row["tied"] >= 0.005
        _text(d, (x1 + 440, y), pct_text(row["tied"]) if lv else "—", 18, AMBER if lv else DIM, "lm", "bold")
        _text(d, (x1 + 540, y), pct_text(row["tie_win"]) if lv and row["tie_win"] is not None else "—", 18,
              WHITE if lv else DIM, "lm", "bold")
        _text(d, (x1 + 640, y), pct_text(row["tied"] * (row["tie_win"] or 0.0)) if lv else "—", 18,
              GREEN if lv else DIM, "lm", "bold")
    _text(d, (x1 + 30, y2 - 34), "LEVEL = ENDS LEVEL AT THE CUT   •   WIN IT = WINS THE TIE-BREAKER   •   THROUGH = THROUGH THIS WAY",
          11, DIM, "lm")

    x1, y1, x2, y2 = 810, 126, 1550, 440
    _tile_title(c, (x1, y1, x2, y2), color := (status(t["chance"], t["outright"], t["tied"])[1]), "YOUR TIE-BREAKER ODDS",
                f"{t['name'].upper()}", "star")
    cells = [("LEVEL AT THE CUT", t["tied"], AMBER), ("YOU WIN IT", t["tie_win"], GREEN),
             ("THROUGH THIS WAY", (t["tied"] * (t["tie_win"] or 0.0)), WHITE)]
    for i, (label, v, col) in enumerate(cells):
        bx = x1 + 30 + i * 230
        _text(d, (bx, y1 + 100), label, 12, MUTED)
        _glow_text(c, (bx, y1 + 124), pct_text(v), 52, col)
    via = max(0.0, t["chance"] - t["outright"])
    _stack_bar(d, x1 + 30, x2 - 30, y1 + 232, 16, [(t["outright"], GREEN), (via, AMBER)])
    _text(d, (x1 + 30, y1 + 262), f"OUTRIGHT {pct_text(t['outright'])}   •   VIA TIE-BREAKER {pct_text(via)}   •   "
          f"OUT {pct_text(max(0.0, 1 - t['chance']))}", 13, MUTED)

    x1, y1, x2, y2 = 810, 454, 1550, 856
    _tile_title(c, (x1, y1, x2, y2), BLUE, "LIKELY TIE-BREAKERS", "THE LINE-UPS THAT COME UP MOST OFTEN IN THE SIMULATIONS", "bars")
    shown = t["scenes"] or data.get("all_scenes") or []
    if not shown:
        _text(d, ((x1 + x2) // 2, y1 + 220), "NONE LIKELY", 22, DIM, "mm", "head")
    for i, s in enumerate(shown[:4]):
        y = y1 + 104 + i * 72
        n = len(s["teams"])
        _text(d, (x1 + 30, y), f"{n} TEAMS  •  {s['room']} PLACE{'S' if s['room'] != 1 else ''}  •  "
              f"{n * (n - 1) // 2} MATCHES", 15, WHITE, "la", "head")
        _text(d, (x1 + 30, y + 24), _clip(d, ", ".join(s["teams"]), 480, 12), 12, MUTED)
        _text(d, (x2 - 30, y), f"HAPPENS {pct_text(s['p'])}", 14, AMBER, "ra", "bold")
        mine = s["win"].get(t["name"])
        if mine is not None:
            _text(d, (x2 - 30, y + 24), f"YOU GO THROUGH {pct_text(mine)}", 13, GREEN, "ra", "bold")
    _footer(c, f"GOOGLI MANAGER  •  TIE-BREAKER  •  {t['name']}"[:90], data.get("guild"))
    return c.image


def _page_group(data):
    table = data["table"]
    c = _canvas(DEFAULT_ACCENT, [(50, 126, 1550, 856)])
    d = c.d
    pages = len(PAGES) if data.get("team") else 1
    _header(c, data, pages - 1)
    x1, y1, x2, y2 = 50, 126, 1550, 856
    _tile_title(c, (x1, y1, x2, y2), BLUE, f"{data['group'].upper()} — WHO QUALIFIES",
                f"TOP {data['spots']} QUALIFY  •  LEVEL AT THE CUT = ROUND-ROBIN TIE-BREAKER", "bars")
    cols = [("#", x1 + 34), ("TEAM", x1 + 84), ("W-L", x1 + 470), ("PTS", x1 + 550), ("LEFT", x1 + 620),
            ("CHANCE", x1 + 710), ("TIE-BREAK", x1 + 1060)]
    for label, x in cols:
        _text(d, (x, y1 + 104), label, 13, MUTED)
    n = max(1, len(table))
    step = min(72, (y2 - y1 - 150) // n)
    for i, row in enumerate(table):
        y = y1 + 150 + i * step + step // 2 - 10
        state, col = row["status"]
        if i:
            d.line((x1 + 28, y - step // 2, x2 - 28, y - step // 2), fill=_mix(BLUE, _INK, 0.85), width=1)
        mine = row["name"] == data.get("highlight")
        _text(d, (x1 + 34, y), str(row["rank"]), 22, col if i < data["spots"] else MUTED, "lm", "head")
        logo = _round_image(_open(row.get("logo")), 32, ring=row["color"], ring_w=2, initial=row["name"][:1])
        _paste(c.image, logo, (x1 + 84, y - logo.size[1] // 2))
        _text(d, (x1 + 130, y), _clip(d, row["name"].upper(), 330, 18, "head"), 18, WHITE if not mine else AMBER, "lm", "head")
        _text(d, (x1 + 470, y), f"{row['w']}-{row['l']}", 18, WHITE, "lm", "bold")
        _text(d, (x1 + 550, y), str(row["pts"]), 20, WHITE, "lm", "head")
        _text(d, (x1 + 620, y), str(row["left"]), 18, MUTED, "lm", "bold")
        bx1, bx2 = x1 + 710, x1 + 1000
        _stack_bar(d, bx1, bx2, y - 8, 16, [(row["outright"], col), (max(0.0, row["chance"] - row["outright"]), _mix(col, WHITE, 0.45))])
        _text(d, (bx1, y - 20), f"through in {pct_text(row['chance'])} · up to {pct_text(min(1.0, row['outright'] + row['tied']))}", 11, MUTED, "lm")
        _text(d, (bx1, y + 22), row_note(row), 12, WHITE, "lm")
        _text(d, (x1 + 1060, y), pct_text(row["tied"]) if row["tied"] >= 0.005 else "—", 18,
              AMBER if row["tied"] >= 0.005 else DIM, "lm", "bold")
        _chip(d, x2 - 30 - 190, y - 15, state, col, 12, 30)
    if not table:
        _text(d, ((x1 + x2) // 2, (y1 + y2) // 2 + 20), "NO GROUP DATA", 26, DIM, "mm", "head")
    cutline = y1 + 140 + min(data["spots"], n) * step
    d.line((x1 + 28, cutline, x2 - 28, cutline), fill=GREEN, width=2)
    _text(d, (x1 + 30, y2 - 24), "SOLID BAR = THROUGH ON POINTS ALONE   •   LIGHT BAR = THROUGH ONLY BY WINNING THE TIE-BREAKER "
          "(LEVEL TEAMS PLAY A ROUND-ROBIN)", 12, MUTED, "lm", "bold")
    _footer(c, f"GOOGLI MANAGER  •  QUALIFY  •  {data['group']}"[:90], data.get("guild"))
    return c.image


def render_qualify_page(data, index):
    if data.get("team"):
        page = (_page_overview, _page_matches, _page_tiebreak, _page_group)[max(0, min(3, index))]
    else:
        page = _page_group
    return _png(page(data))


# =========================================================================== command

class Qualify(commands.Cog):
    """+qualify and +qualifyspots."""

    def __init__(self, bot):
        self.bot = bot
        bot.db.execute("CREATE TABLE IF NOT EXISTS qualify_settings (guild_id INTEGER PRIMARY KEY, spots INTEGER NOT NULL)")
        bot.db.commit()
        self._cache = {}

    @property
    def compare(self):
        return self.bot.get_cog("CompareTeams")

    def spots(self, guild_id):
        row = self.bot.db.execute("SELECT spots FROM qualify_settings WHERE guild_id=?", (guild_id,)).fetchone()
        return int(row[0]) if row else DEFAULT_SPOTS

    # ---- reading the group (blocking, run in a thread)
    def _group(self, guild_id, team_name):
        """(group name, rows, fixtures) of the team's group in the active stage, or None.

        rows = points-table rows; fixtures = remaining (team a, team b, day) with played ones removed."""
        from .groups import norm
        pts = self.bot.get_cog("PointsTable")
        if pts is None:
            return None
        mod = sys.modules[type(pts).__module__]
        stage = mod.get_active_stage(guild_id)
        gconn = sqlite3.connect(mod.GROUP_DB)
        gconn.row_factory = sqlite3.Row
        try:
            keys = [r["group_key"] for r in gconn.execute(
                "SELECT group_key FROM team_groups WHERE guild_id=? AND team_name=? COLLATE NOCASE",
                (guild_id, team_name))]
            if not keys:
                return None
            group_key = next((k for k in keys if stage and k.split("::", 1)[0] == stage[0]), keys[-1])
            stage_key = group_key.split("::", 1)[0]
            fixtures = [(r["team_one"], r["team_two"], r["day_no"]) for r in gconn.execute(
                "SELECT team_one, team_two, day_no FROM generated_fixtures WHERE guild_id=? AND group_key=? "
                "ORDER BY day_no, id", (guild_id, group_key))]
            fmt = gconn.execute("SELECT matches_per_pair FROM stage_formats WHERE guild_id=? AND stage_key=?",
                                (guild_id, stage_key)).fetchone()
        finally:
            gconn.close()
        group_name, rows = pts._group_data(guild_id, group_key)
        if not rows:
            return None
        names = {norm(r["team_name"]): r["team_name"] for r in rows}
        if not fixtures:                                   # no saved schedule: everyone plays everyone "legs" times
            legs = int(fmt["matches_per_pair"]) if fmt else 1
            fixtures = [(a, b, None) for _ in range(legs) for a, b in combinations([r["team_name"] for r in rows], 2)]
        fixtures = [(names[norm(a)], names[norm(b)], day) for a, b, day in fixtures if norm(a) in names and norm(b) in names]

        # Count only results whose stored team keys belong to this exact
        # stage/group. Team names alone are not enough: the same two teams can
        # meet again in a later stage, and an older-stage result must not mark
        # the new-stage fixture as played.
        pconn = mod.connect_points_db()
        try:
            first = (mod.first_stage_key(guild_id) == stage_key)
            storage_to_name = {}
            for row in rows:
                storage_key = mod.points_storage_key(
                    pconn, guild_id, row["team_name_key"], legacy_ok=first
                )
                storage_to_name[str(storage_key)] = norm(row["team_name"])
            results = pconn.execute(
                "SELECT winner_team_key, loser_team_key FROM imported_match_results WHERE guild_id=?",
                (guild_id,),
            ).fetchall()
        finally:
            pconn.close()

        played = {}
        for result in results:
            winner_key = str(result["winner_team_key"])
            loser_key = str(result["loser_team_key"])
            winner_name = storage_to_name.get(winner_key)
            loser_name = storage_to_name.get(loser_key)
            if winner_name is None or loser_name is None:
                continue
            pair = frozenset((winner_name, loser_name))
            played[pair] = played.get(pair, 0) + 1
        remaining, seen = [], {}
        for a, b, day in fixtures:
            pair = frozenset((norm(a), norm(b)))
            seen[pair] = seen.get(pair, 0) + 1
            if seen[pair] > played.get(pair, 0):
                remaining.append((a, b, day))
        # Remaining games are determined only by saved fixtures minus imported results.
        # Do not cap this using the points-table W/L totals: those totals can include
        # manually entered adjustments or results outside this fixture list, which
        # would incorrectly hide a genuinely scheduled remaining game.
        return group_name, rows, remaining

    async def _data(self, ctx, team, want_team=True):
        compare, guild = self.compare, ctx.guild
        core = compare.core
        got = await asyncio.to_thread(self._group, guild.id, team["name"])
        if got is None:
            return None
        group_name, rows, remaining = got
        canon = next((r["team_name"] for r in rows if str(r["team_name"]).casefold() == str(team["name"]).casefold()), None)
        if canon is None:
            return None
        team = {"name": canon}
        spots = max(1, min(self.spots(guild.id), len(rows)))
        record = {r["team_name"]: (int(r["matches_won"]), int(r["matches_lost"])) for r in rows}
        points = {r["team_name"]: int(r["points"]) for r in rows}
        prob = lambda a, b: win_chance(record[a], record[b])
        fixtures = [(a, b, prob(a, b)) for a, b, _ in remaining]
        seed = hash((guild.id, team["name"], tuple(sorted(points.items())), tuple(f[:2] for f in fixtures),
                     spots)) & 0xFFFFFFFF
        key = (guild.id, team["name"], seed)
        analysis = self._cache.get(key)
        if analysis is None:
            analysis = await asyncio.to_thread(engine.analyse, points, fixtures, prob, team["name"], spots,
                                               0, seed)
            self._cache = {key: analysis}
        shim = types.SimpleNamespace(guild=guild)
        by_name = {str(t["name"]).casefold(): t for t in core.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved'", (guild.id,)).fetchall()}
        table, names = [], {}
        for position, r in enumerate(rows, start=1):
            reg = by_name.get(str(r["team_name"]).casefold())
            nm = r["team_name"]
            stats = analysis["teams"][nm]
            logo = await compare._logo_for(shim, reg) if reg is not None else None
            color = (team_role_color(core, guild, reg) if reg is not None else None) or DEFAULT_ACCENT
            names[nm] = {"logo": logo, "color": color}
            table.append({
                "name": nm, "rank": position, "w": int(r["matches_won"]), "l": int(r["matches_lost"]),
                "pts": int(r["points"]), "left": sum(1 for f in fixtures if nm in (f[0], f[1])),
                "chance": stats["through"], "outright": stats["outright"], "tied": stats["tied"],
                "tie_win": stats["tie_win"], "logo": logo, "color": color,
                "status": status(stats["through"], stats["outright"], stats["tied"]),
            })
        mine = next(x for x in table if x["name"] == team["name"])
        data = {"guild": guild.name, "group": f"Group {group_name}" if group_name else "Group", "spots": spots,
                "table": table, "highlight": team["name"], "team": None, "bonus": None, "names": names, "left_total": len(fixtures),
                "pair": analysis["pair"], "all_scenes": analysis["all_scenes"]}
        if want_team:
            stats = analysis["teams"][team["name"]]
            data["team"] = {
                "name": team["name"], "logo": mine["logo"], "color": mine["color"], "rank": mine["rank"], "of": len(rows),
                "w": mine["w"], "l": mine["l"], "pts": mine["pts"], "left": analysis["left"],
                "chance": stats["through"], "outright": stats["outright"], "tied": stats["tied"],
                "tie_win": stats["tie_win"],
                "status": mine["status"], "scen": analysis["scen"], "swings": analysis["swings"],
                "elsewhere": analysis["elsewhere"], "scenes": analysis["scenes"],
                "best": analysis["best"], "worst": analysis["worst"],
            }
        return data

    @commands.command(name="qualify", aliases=["qualification", "qual", "playoffs"])
    @commands.guild_only()
    async def qualify(self, ctx, *, team: str = ""):
        """Qualification chances: +qualify (your team) or +qualify <team>."""
        if self.compare is None or self.compare.core is None:
            return await ctx.reply("The team system isn't loaded right now.", mention_author=False)
        core = self.compare.core
        if team.strip():
            row, error = self.compare._resolve(ctx.guild.id, team.strip())
            if error:
                return await ctx.reply(error, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        else:
            row = core._membership(ctx.guild.id, ctx.author.id)
            if not row:
                return await ctx.reply("You're not on a team. Use `+qualify <team name>`.", mention_author=False)
        wait = on_cooldown(ctx.author.id)
        if wait:
            return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
        loading = await ctx.reply("⏳ Simulating the rest of the group...", mention_author=False)
        try:
            data = await self._data(ctx, {"name": str(row["name"])})
            if data is None:
                await loading.edit(content=f"**{row['name']}** isn't in a group of the active stage yet "
                                           "(set groups with the group commands first).")
                return
            png = await render_png(render_qualify_page, data, 0)
            view = ImagePager(ctx, png, len(PAGES), lambda i: render_qualify_page(data, i), PAGES, "qualify.png")
            view.message = await loading.edit(
                content=None, attachments=[discord.File(io.BytesIO(png), filename="qualify.png")], view=view)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"[qualify] failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                await loading.delete()
            except Exception:
                pass
            await ctx.send(f"I couldn't work out the qualification chances: `{type(exc).__name__}: {exc}`")

    @commands.command(name="qualifydebug")
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def qualifydebug(self, ctx, *, team: str):
        """Temporary read-only diagnostic for stage-specific qualification records."""
        from .groups import norm
        pts = self.bot.get_cog("PointsTable")
        if pts is None:
            return await ctx.reply("Points table is not loaded.", mention_author=False)
        mod = sys.modules[type(pts).__module__]
        active = mod.get_active_stage(ctx.guild.id)
        gconn = sqlite3.connect(mod.GROUP_DB)
        gconn.row_factory = sqlite3.Row
        try:
            assignments = gconn.execute(
                "SELECT team_name, team_name_key, group_key FROM team_groups "
                "WHERE guild_id=? AND team_name=? COLLATE NOCASE ORDER BY group_key",
                (ctx.guild.id, team.strip()),
            ).fetchall()
            if not assignments:
                return await ctx.reply(f"Could not find **{team}** in team groups.", mention_author=False)
            active_assignment = next(
                (r for r in assignments if active and
                 str(r["group_key"]).split("::", 1)[0].casefold() == active[0].casefold()),
                assignments[-1],
            )
            group_key = str(active_assignment["group_key"])
            group_teams = gconn.execute(
                "SELECT team_name, team_name_key FROM team_groups WHERE guild_id=? AND group_key=?",
                (ctx.guild.id, group_key),
            ).fetchall()
            fixtures = gconn.execute(
                "SELECT team_one, team_two, day_no FROM generated_fixtures "
                "WHERE guild_id=? AND group_key=? ORDER BY day_no, id",
                (ctx.guild.id, group_key),
            ).fetchall()
        finally:
            gconn.close()

        pconn = mod.connect_points_db()
        try:
            legacy_ok = bool(active and mod.first_stage_key(ctx.guild.id) == active[0])
            key_map = {}
            for row in group_teams:
                storage = mod.points_storage_key(
                    pconn, ctx.guild.id, row["team_name_key"], legacy_ok=legacy_ok
                )
                key_map[str(storage)] = str(row["team_name"])
            results = pconn.execute(
                "SELECT match_no, winner_team_key, loser_team_key FROM imported_match_results "
                "WHERE guild_id=? ORDER BY match_no",
                (ctx.guild.id,),
            ).fetchall()
            target_key = next(
                (key for key, name in key_map.items() if norm(name) == norm(team)),
                None,
            )
            history = pconn.execute(
                "SELECT id, result, match_no, created_at FROM team_match_history "
                "WHERE guild_id=? AND team_name_key=? ORDER BY id",
                (ctx.guild.id, target_key),
            ).fetchall() if target_key else []
            point_row = pconn.execute(
                "SELECT matches_won, matches_lost FROM team_points "
                "WHERE guild_id=? AND team_name_key=?",
                (ctx.guild.id, target_key),
            ).fetchone() if target_key else None
        finally:
            pconn.close()

        lines = [
            f"QUALIFY DEBUG — {team}",
            f"Active points stage: {active}",
            f"Selected group: {group_key}",
            "CURRENT GROUP STORAGE KEYS:",
        ]
        for key, name in key_map.items():
            lines.append(f"• {name}: [{key}]")
        lines.append(
            f"POINTS TOTAL: W={point_row['matches_won']} L={point_row['matches_lost']}"
            if point_row else "POINTS TOTAL: no row found"
        )
        lines.append("HIGH VOLTAGES MATCH HISTORY (match_no blank = manual/unlinked entry):")
        for item in history:
            lines.append(
                f"• row {item['id']}: {item['result']} | match_no={item['match_no'] or 'BLANK'} "
                f"| created={item['created_at']}"
            )
        lines.append("SAVED FIXTURES:")
        for fixture in fixtures:
            lines.append(f"• {fixture['team_one']} vs {fixture['team_two']} (day {fixture['day_no']})")
        lines.append("IMPORTED RESULTS MATCHING THIS GROUP'S STORAGE KEYS:")
        found = 0
        for result in results:
            winner_key, loser_key = str(result["winner_team_key"]), str(result["loser_team_key"])
            if winner_key in key_map or loser_key in key_map:
                found += 1
                lines.append(
                    f"• Match #{result['match_no']}: "
                    f"[{winner_key}] vs [{loser_key}] "
                    f"=> {key_map.get(winner_key, 'NOT IN CURRENT GROUP')} vs "
                    f"{key_map.get(loser_key, 'NOT IN CURRENT GROUP')}"
                )
        if not found:
            lines.append("• No imported results use the current group's storage keys.")
        message = "\n".join(lines)
        for start in range(0, len(message), 1700):
            await ctx.send(message[start:start + 1700], allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="qualifyspots", aliases=["setqualifyspots"])
    @commands.guild_only()
    async def qualifyspots(self, ctx, spots: int = None):
        """Set how many teams qualify from each group (owner / whitelisted). No number shows the current value."""
        if spots is None:
            return await ctx.reply(f"**{self.spots(ctx.guild.id)}** team(s) qualify from each group. "
                                   "Change it with `+qualifyspots <number>`.", mention_author=False)
        if not await owner_or_whitelisted(ctx, command_name="qualifyspots", app_owner=True):
            return await ctx.reply("Only the bot owner or a role whitelisted for this command can use it.",
                                   mention_author=False)
        if not 1 <= spots <= 16:
            return await ctx.reply("Choose a number from 1 to 16.", mention_author=False)
        self.bot.db.execute("INSERT INTO qualify_settings (guild_id, spots) VALUES (?, ?) "
                            "ON CONFLICT(guild_id) DO UPDATE SET spots=excluded.spots", (ctx.guild.id, spots))
        self.bot.db.commit()
        await ctx.reply(f"✅ **{spots}** team(s) now qualify from each group.", mention_author=False)


async def setup(bot):
    await bot.add_cog(Qualify(bot))
