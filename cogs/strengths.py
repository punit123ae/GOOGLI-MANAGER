"""+strengths [team]: a scouting report on a team. One file: analysis, drawing and command.

  +strengths Team Name     (first 4+ letters work, like +teaminfo)
  +strengths               your own team          aliases: +scout  +scouting

What it shows (one image)
  STRENGTHS / WEAKNESSES   Every team is ranked on 12 numbers (runs per match, batting average, strike
                           rate, wickets per match, economy, bowling average, bowling strike rate,
                           catches per match, catch rate, drops per match, win rate, squad depth).
                           A number is measured in standard deviations from the league average; the
                           three best (above average) are strengths, the three worst are weaknesses,
                           at most two from the same area (batting / bowling / fielding) per list.
  GRADES                   Batting, Bowling, Fielding and Depth as 0-100 league percentiles with a letter.
  WHAT THE TEAM DEPENDS ON Share of the team's runs, wickets and catches that come from its top player
                           in each, and how much of the team's fantasy points its top 3 players supply.
  KEY PLAYER WARNING       The player with the largest share of the team's fantasy points, how much of the
                           team's runs / wickets / fantasy go with him, and a risk level
                           (HIGH from 30% of the fantasy points, MEDIUM from 20%).

Numbers follow +teaminfo / +compareteams: the whole history, each match counted for the team a player
was on when it was played. They come from the stats index CompareTeams already keeps for 2 minutes, so
the command adds no database work of its own.
"""
import asyncio
import io
import math
import re
from statistics import mean, pstdev

import discord
from discord.ext import commands
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .h2h import ImagePager, fetch_avatar, on_cooldown, render_png, team_role_color
from .teaminfo_image import (
    CORAL, DIM, GOLD, MINT, MUTED, VIOLET, WHITE, _clip, _fit, _mix, _open, _paste, _pill, _round_image, _text,
    accent_from_logo, font,
)
from .versus_image import EDGE, _Canvas, _footer, _png, _title, pick_sides

STRENGTH_COLOR, WEAKNESS_COLOR = (62, 224, 140), (255, 96, 120)
DEPEND_COLOR, WARN_COLOR = GOLD, (255, 140, 70)
RISK_COLORS = {"HIGH RISK": (255, 96, 120), "MEDIUM RISK": (255, 180, 70), "LOW RISK": (62, 224, 140)}
Z_MIN = 0.25                    # how far from the league average a number must be to count
PER_GROUP = 2                   # at most this many strengths / weaknesses from the same area
HIGH_SHARE, MEDIUM_SHARE = 0.30, 0.20

# key, label, higher is better, area, text format, strength phrase, weakness phrase
METRICS = [
    ("runs_pm", "RUNS / MATCH", True, "bat", "{:.1f}", "Piles up big totals", "Struggles to post big totals"),
    ("bat_avg", "BATTING AVERAGE", True, "bat", "{:.1f}", "Batters make their starts count", "Wickets fall too cheaply"),
    ("sr", "STRIKE RATE", True, "bat", "{:.1f}", "Attacks from ball one", "Scores too slowly"),
    ("wk_pm", "WICKETS / MATCH", True, "bowl", "{:.1f}", "Takes wickets regularly", "Struggles to take wickets"),
    ("econ", "ECONOMY", False, "bowl", "{:.2f}", "Squeezes the run rate", "Leaks runs"),
    ("bowl_avg", "BOWLING AVERAGE", False, "bowl", "{:.1f}", "Wickets come cheap", "Wickets cost too many runs"),
    ("bowl_sr", "BOWLING STRIKE RATE", False, "bowl", "{:.1f}", "Breakthroughs come quickly", "Breakthroughs are slow"),
    ("catch_pm", "CATCHES / MATCH", True, "field", "{:.1f}", "Safe hands in the field", "Takes too few catches"),
    ("catch_eff", "CATCH RATE", True, "field", "{:.0f}%", "Rarely drops a chance", "Drops too many chances"),
    ("drops_pm", "DROPS / MATCH", False, "field", "{:.1f}", "Hardly ever drops a catch", "Drops are costing the team"),
    ("win_rate", "WIN RATE", True, "record", "{:.0f}%", "Wins most of its matches", "Struggles to close out matches"),
    ("depth", "SQUAD DEPTH", True, "depth", "{:.0f}", "Everyone chips in", "Leans on a few players"),
]
BY_KEY = {m[0]: m for m in METRICS}
AREAS = [("BATTING", "bat", CORAL), ("BOWLING", "bowl", VIOLET), ("FIELDING", "field", MINT), ("DEPTH", "depth", GOLD)]


# =========================================================================== analysis (no Discord, no images)

def _gini(values):
    """0 = everybody contributes the same, towards 1 = one player does everything (None under 3 players)."""
    ordered = sorted(max(0.0, float(v)) for v in values)
    n, total = len(ordered), sum(ordered)
    if n < 3 or total <= 0:
        return None
    return sum((2 * (i + 1) - n - 1) * x for i, x in enumerate(ordered)) / (n * total)


def team_numbers(rows_by_uid, field_by_uid, standing, totals_fn, fantasy_fn):
    """Everything about one team: the 12 numbers plus its players (for the dependency part)."""
    rows = [r for rs in rows_by_uid.values() for r in rs]
    matches = len({r["mid"] for r in rows})
    if not matches:
        return None
    t = totals_fn(rows)
    catches = sum(c for c, _d in field_by_uid.values())
    drops = sum(d for _c, d in field_by_uid.values())
    players = []
    for uid in set(rows_by_uid) | set(field_by_uid):
        prs = rows_by_uid.get(uid, [])
        caught, dropped = field_by_uid.get(uid, (0, 0))
        pt = totals_fn(prs) if prs else None
        players.append({
            "uid": uid, "runs": pt["runs"] if pt else 0, "wickets": pt["wickets"] if pt else 0,
            "catches": caught, "fantasy": (fantasy_fn(prs) if prs else 0.0) + caught * 5 - dropped * 10,
            "matches": len({r["mid"] for r in prs}), "drops": dropped,
            "balls_faced": pt["balls_faced"] if pt else 0, "balls_bowled": pt["balls_bowled"] if pt else 0,
            "sr": pt["strike_rate"] if pt and pt["balls_faced"] else None,
            "bat_avg": pt["bat_avg"] if pt and pt["balls_faced"] else None,
            "econ": pt["economy"] if pt and pt["balls_bowled"] else None,
            "bowl_sr": pt["bowl_sr"] if pt and pt["wickets"] else None,
        })
    chances = catches + drops
    played = won = None
    if standing:
        won = int(standing["won"])
        played = won + int(standing["lost"])
    gini = _gini([p["fantasy"] for p in players])
    m = {
        "runs_pm": t["runs"] / matches,
        "bat_avg": t["bat_avg"] if t["balls_faced"] > 0 else None,
        "sr": t["strike_rate"] if t["balls_faced"] > 0 else None,
        "wk_pm": t["wickets"] / matches if t["balls_bowled"] > 0 else None,
        "econ": t["economy"] if t["balls_bowled"] > 0 else None,
        "bowl_avg": t["bowl_avg"] if t["wickets"] > 0 else None,
        "bowl_sr": t["bowl_sr"] if t["wickets"] > 0 else None,
        "catch_pm": catches / matches,
        "catch_eff": catches / chances * 100 if chances else None,
        "drops_pm": drops / matches,
        "win_rate": won / played * 100 if played else None,
        "depth": (1 - gini) * 100 if gini is not None else None,
    }
    return {"metrics": m, "players": players, "matches": matches, "totals": t, "standing": standing,
            "won": won, "played": played, "catches": catches}


def league_stats(all_numbers):
    """{metric: (mean, std)} over the teams that have the number."""
    out = {}
    for key, *_rest in METRICS:
        vals = [n["metrics"][key] for n in all_numbers.values() if n["metrics"][key] is not None]
        out[key] = (mean(vals), pstdev(vals)) if len(vals) >= 2 else None
    return out


def _z(value, key, stats):
    """Standard deviations better (+) or worse (-) than the league average; None without data."""
    if value is None or stats.get(key) is None:
        return None
    mu, sd = stats[key]
    if sd == 0:
        return 0.0
    z = (value - mu) / sd
    return z if BY_KEY[key][2] else -z


def _rank(key, name, all_numbers):
    """(rank, teams that have the number); 1 = best in the league."""
    higher = BY_KEY[key][2]
    vals = {n: x["metrics"][key] for n, x in all_numbers.items() if x["metrics"][key] is not None}
    ordered = sorted(vals, key=lambda n: -vals[n] if higher else vals[n])
    return (ordered.index(name) + 1 if name in ordered else None), len(ordered)


def _percentile(z):
    return 100 * 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _grade(score):
    for limit, letter in ((88, "A+"), (75, "A"), (60, "B"), (45, "C"), (30, "D")):
        if score >= limit:
            return letter
    return "E"


def scout(name, all_numbers):
    """The report for one team (name is the team's key in all_numbers)."""
    me = all_numbers[name]
    stats = league_stats(all_numbers)
    zs = {key: _z(me["metrics"][key], key, stats) for key, *_r in METRICS}

    def build(sign):
        picked, used = [], {}
        order = sorted((k for k, z in zs.items() if z is not None and sign * z >= Z_MIN), key=lambda k: -sign * zs[k])
        for key in order:
            area = BY_KEY[key][3]
            if used.get(area, 0) >= PER_GROUP:
                continue
            used[area] = used.get(area, 0) + 1
            _k, label, _hb, _area, fmt, good, bad = BY_KEY[key]
            rank, of = _rank(key, name, all_numbers)
            picked.append({"label": label, "value": fmt.format(me["metrics"][key]), "rank": rank, "of": of,
                           "note": good if sign > 0 else bad, "pct": _percentile(zs[key]), "key": key})
            if len(picked) == 3:
                break
        return picked

    grades = []
    for label, area, color in AREAS:
        vals = [zs[k] for k, *_r in METRICS if BY_KEY[k][3] == area and zs[k] is not None]
        score = _percentile(mean(vals)) if vals else None
        grades.append({"label": label, "score": score, "letter": _grade(score) if score is not None else "—",
                       "color": color})
    bat = [g for g in grades if g["label"] == "BATTING"][0]["score"]
    bowl = [g for g in grades if g["label"] == "BOWLING"][0]["score"]
    if bat is None or bowl is None:
        style = "NOT ENOUGH DATA"
    elif bat >= 60 and bowl >= 60:
        style = "WELL-ROUNDED SIDE"
    elif bat - bowl >= 15:
        style = "BATTING-LED SIDE"
    elif bowl - bat >= 15:
        style = "BOWLING-LED SIDE"
    elif bat < 40 and bowl < 40:
        style = "STRUGGLING ALL-ROUND"
    else:
        style = "BALANCED SIDE"
    return {"strengths": build(+1), "weaknesses": build(-1), "grades": grades, "style": style,
            "teams": len(all_numbers)}


# key, label, higher is better, text format, enough data to count the number?
PLAYER_METRICS = [
    ("runs_pm", "RUNS / MATCH", True, "{:.1f}", lambda p: p["matches"] >= 2 and p["balls_faced"] >= 10),
    ("sr", "STRIKE RATE", True, "{:.0f}", lambda p: p["balls_faced"] >= 30),
    ("bat_avg", "BATTING AVERAGE", True, "{:.1f}", lambda p: p["balls_faced"] >= 30),
    ("wk_pm", "WICKETS / MATCH", True, "{:.1f}", lambda p: p["matches"] >= 2 and p["balls_bowled"] >= 12),
    ("econ", "ECONOMY", False, "{:.2f}", lambda p: p["balls_bowled"] >= 24),
    ("bowl_sr", "BOWLING STRIKE RATE", False, "{:.1f}", lambda p: p["balls_bowled"] >= 24 and p["bowl_sr"]),
    ("catch_pm", "CATCHES / MATCH", True, "{:.1f}", lambda p: p["matches"] >= 2 and p["catches"] > 0),
    ("drops_pm", "DROPS / MATCH", False, "{:.1f}", lambda p: p["matches"] >= 2 and p["drops"] > 0),
]
PLAYER_STRONG, PLAYER_WEAK = 70, 30


def _player_value(p, key):
    m = max(1, p["matches"])
    return {"runs_pm": p["runs"] / m, "wk_pm": p["wickets"] / m, "catch_pm": p["catches"] / m,
            "drops_pm": p["drops"] / m}.get(key) if key in ("runs_pm", "wk_pm", "catch_pm", "drops_pm") else p[key]


def player_report(all_numbers, name, limit=6):
    """Per player of one team: role, standout numbers (against every player in the league) and a weak spot."""
    pools = {}
    for team in all_numbers.values():
        for p in team["players"]:
            for key, _label, _hb, _fmt, ok in PLAYER_METRICS:
                if ok(p):
                    pools.setdefault(key, []).append(_player_value(p, key))

    def pct(key, value, higher):
        pool = pools.get(key, [])
        if len(pool) < 6:
            return None
        worse = sum(1 for v in pool if (v < value if higher else v > value))
        same = sum(1 for v in pool if v == value)
        return 100.0 * (worse + same / 2) / len(pool)

    me = all_numbers[name]
    out = []
    for p in sorted(me["players"], key=lambda q: -q["fantasy"]):
        scores = []
        for key, label, higher, fmt, ok in PLAYER_METRICS:
            if not ok(p):
                continue
            value = _player_value(p, key)
            score = pct(key, value, higher)
            if score is not None:
                scores.append({"key": key, "label": label, "value": fmt.format(value), "pct": score})
        if not scores and p["fantasy"] <= 0:
            continue
        by = {s_["key"]: s_["pct"] for s_ in scores}
        bat, bowl = by.get("runs_pm"), by.get("wk_pm")
        if bat is not None and bowl is not None and bat >= 55 and bowl >= 55:
            role = "ALL-ROUNDER"
        elif bat is not None and (bowl is None or bat >= bowl):
            role = "BATTER"
        elif bowl is not None:
            role = "BOWLER"
        elif p["catches"]:
            role = "FIELDER"
        else:
            role = "SQUAD PLAYER"
        strong = sorted((s_ for s_ in scores if s_["pct"] >= PLAYER_STRONG and s_["key"] != "drops_pm"), key=lambda x: -x["pct"])[:2]
        weak = sorted((s_ for s_ in scores if s_["pct"] <= PLAYER_WEAK), key=lambda x: x["pct"])[:1]
        out.append({"uid": p["uid"], "role": role, "fantasy": p["fantasy"], "matches": p["matches"],
                    "strong": strong, "weak": weak})
        if len(out) == limit:
            break
    return out


def dependencies(me):
    """Top player's share of runs / wickets / catches, fantasy concentration and the key-player warning."""
    players = me["players"]
    t = me["totals"]
    out = {"rows": [], "top3": None, "key": None}
    for label, field, total in (("TOP RUN SCORER", "runs", t["runs"]), ("TOP WICKET TAKER", "wickets", t["wickets"]),
                                ("TOP FIELDER", "catches", me["catches"])):
        pool = [p for p in players if p[field] > 0]
        if pool and total > 0:
            best = max(pool, key=lambda p: (p[field], p["fantasy"], -p["uid"]))
            out["rows"].append({"label": label, "uid": best["uid"], "value": best[field], "share": best[field] / total,
                                "field": field})
    positive = sorted((p for p in players if p["fantasy"] > 0), key=lambda p: -p["fantasy"])
    total_fantasy = sum(p["fantasy"] for p in positive)
    if total_fantasy > 0:
        out["top3"] = sum(p["fantasy"] for p in positive[:3]) / total_fantasy
        key = positive[0]
        share = key["fantasy"] / total_fantasy
        risk = "HIGH RISK" if share >= HIGH_SHARE else "MEDIUM RISK" if share >= MEDIUM_SHARE else "LOW RISK"
        out["key"] = {
            "uid": key["uid"], "fantasy_share": share, "risk": risk,
            "runs_share": key["runs"] / t["runs"] if t["runs"] > 0 else 0.0,
            "wickets_share": key["wickets"] / t["wickets"] if t["wickets"] > 0 else 0.0,
            "catch_share": key["catches"] / me["catches"] if me["catches"] > 0 else 0.0,
        }
    return out


def verdict_line(report):
    s, w = report["strengths"], report["weaknesses"]
    if s and w:
        return f"Best at {s[0]['label'].lower()}, weakest at {w[0]['label'].lower()}."
    if s:
        return f"Best at {s[0]['label'].lower()}, with no clear weakness."
    if w:
        return f"No clear strength; weakest at {w[0]['label'].lower()}."
    return "Close to the league average everywhere."


# =========================================================================== one player / two players

PLAYER_NOTES = {
    "runs_pm": ("Scores heavily every match", "Rarely makes big scores"),
    "sr": ("Attacks from the first ball", "Scores too slowly"),
    "bat_avg": ("Makes his starts count", "Gets out too cheaply"),
    "wk_pm": ("Takes wickets regularly", "Rarely takes wickets"),
    "econ": ("Keeps the runs tight", "Leaks too many runs"),
    "bowl_sr": ("Breaks through quickly", "Breakthroughs are slow"),
    "catch_pm": ("Safe hands in the field", "Takes few catches"),
    "drops_pm": ("Hardly ever drops a catch", "Drops are costing the team"),
}
PLAYER_AREAS = {"BATTING": ("runs_pm", "sr", "bat_avg"), "BOWLING": ("wk_pm", "econ", "bowl_sr"),
                "FIELDING": ("catch_pm", "drops_pm")}
PLAYER_META = {k: (label, higher, fmt, ok) for k, label, higher, fmt, ok in PLAYER_METRICS}


def player_numbers(p):
    """A rated xi_engine profile in the shape the player metrics expect."""
    return {"uid": p["uid"], "matches": p["matches"], "runs": p["runs"], "wickets": p["wickets"],
            "catches": p["catches"], "drops": p["drops"], "balls_faced": p["balls_faced"],
            "balls_bowled": p["balls_bowled"], "sr": p.get("strike_rate"), "bat_avg": p.get("bat_avg"),
            "econ": p.get("economy"), "bowl_sr": p.get("bowl_sr"), "fantasy": p["fantasy"]}


def player_scout(everyone, uid):
    """Strengths, weak spots and grades of one player against every player in the league.

    everyone: {uid: player_numbers(...)}. Returns None when the player has no numbers."""
    me = everyone.get(uid)
    if me is None:
        return None
    pools = {}
    for q in everyone.values():
        for key, _l, _h, _f, ok in PLAYER_METRICS:
            if ok(q):
                pools.setdefault(key, []).append(_player_value(q, key))
    scores = {}
    for key, label, higher, fmt, ok in PLAYER_METRICS:
        pool = pools.get(key, [])
        if len(pool) < 6 or not (ok(me) or (key == "drops_pm" and me["matches"] >= 2 and me["drops"] == 0
                                            and me["catches"] > 0)):
            continue
        value = _player_value(me, key)
        better = sum(1 for v in pool if (v > value if higher else v < value))
        same = sum(1 for v in pool if v == value) - (1 if ok(me) else 0)
        worse = len(pool) - better - same - (1 if ok(me) else 0)
        n = len(pool) + (0 if ok(me) else 1)
        pct = 100.0 * (worse + max(0, same) / 2) / max(1, n - 1)
        scores[key] = {"key": key, "label": label, "value": fmt.format(value), "raw": value, "pct": pct,
                       "rank": better + 1, "of": n, "higher": higher}
    if not scores:
        return None
    strong = sorted((v for v in scores.values() if v["pct"] >= 60 and v["key"] != "drops_pm"
                     or v["key"] == "drops_pm" and v["pct"] >= 85), key=lambda v: -v["pct"])[:3]
    weak = sorted((v for v in scores.values() if v["pct"] <= 40), key=lambda v: v["pct"])[:3]
    for v in strong:
        v["note"] = PLAYER_NOTES[v["key"]][0]
    for v in weak:
        v["note"] = PLAYER_NOTES[v["key"]][1]
    grades = []
    for label, keys in PLAYER_AREAS.items():
        vals = [scores[k]["pct"] for k in keys if k in scores]
        score = mean(vals) if vals else None
        color = {"BATTING": CORAL, "BOWLING": VIOLET, "FIELDING": MINT}[label]
        grades.append({"label": label, "score": score, "letter": _grade(score) if score is not None else "—",
                       "color": color})
    bat, bowl = grades[0]["score"], grades[1]["score"]
    if bat is not None and bowl is not None and bat >= 55 and bowl >= 55:
        role = "ALL-ROUNDER"
    elif bat is not None and (bowl is None or bat >= bowl):
        role = "BATTER"
    elif bowl is not None:
        role = "BOWLER"
    else:
        role = "FIELDER"
    if strong and weak:
        verdict = f"Best at {strong[0]['label'].lower()}, weakest at {weak[0]['label'].lower()}."
    elif strong:
        verdict = f"Best at {strong[0]['label'].lower()}, with no clear weakness."
    elif weak:
        verdict = f"No clear strength; weakest at {weak[0]['label'].lower()}."
    else:
        verdict = "Close to the league average everywhere."
    return {"scores": scores, "strong": strong, "weak": weak, "grades": grades, "role": role, "verdict": verdict,
            "fantasy": me["fantasy"], "matches": me["matches"], "players": len(everyone)}


# =========================================================================== drawing

_INK = (12, 18, 32)
TRACK = (26, 38, 64)


def _rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def _blur_ellipse(size, box, color, alpha, blur):
    """Soft coloured glow, drawn at half size (blurs are the expensive part) and scaled up."""
    w, h = size
    small = Image.new("RGBA", (w // 2, h // 2), (0, 0, 0, 0))
    ImageDraw.Draw(small).ellipse([v / 2 for v in box], fill=color + (alpha,))
    small = small.filter(ImageFilter.GaussianBlur(blur / 2))
    return small.resize((w, h), Image.Resampling.BILINEAR)


def _tile_glass(c, box, color):
    """Glow in the tile colour, coloured edge, glossy top line and an accent tab."""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    glow = Image.alpha_composite(
        _blur_ellipse((w, h), (-w * 0.25, -h * 0.45, w * 0.65, h * 0.35), color, 95, 70),
        _blur_ellipse((w, h), (w * 0.45, h * 0.65, w * 1.3, h * 1.4), color, 45, 80))
    glow.putalpha(ImageChops.multiply(glow.getchannel("A"), _rounded_mask((w, h), 26)))
    c.image.paste(glow, (x1, y1), glow)
    d = c.d
    d.rounded_rectangle(box, radius=26, outline=_mix(color, _INK, 0.35), width=2)
    d.rounded_rectangle((x1 + 3, y1 + 3, x2 - 3, y2 - 3), radius=23, outline=_mix(color, _INK, 0.8), width=1)
    d.line((x1 + 30, y1 + 2, x2 - 30, y1 + 2), fill=_mix(color, WHITE, 0.55), width=1)
    d.rounded_rectangle((x1 + 28, y1 - 1, x1 + 150, y1 + 4), radius=2, fill=color)


def _halo(c, cx, cy, radius, color):
    size = int(radius * 3.4)
    glow = _blur_ellipse((size, size), (size * 0.2, size * 0.2, size * 0.8, size * 0.8), color, 130, 36)
    c.image.paste(glow, (int(cx - size / 2), int(cy - size / 2)), glow)


def _glow_text(c, xy, value, size, color, anchor="la"):
    """Big number with a coloured glow behind it ("la" = x is the left edge, "ra" = the right edge)."""
    pad = 22
    f = font(size, "head")
    width = int(c.d.textlength(value, font=f))
    left = xy[0] if anchor == "la" else xy[0] - width
    layer = Image.new("RGBA", (width + pad * 2, size + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((pad, pad), value, font=f, fill=color + (220,))
    layer = layer.filter(ImageFilter.GaussianBlur(8))
    c.image.paste(layer, (int(left - pad), int(xy[1] - pad)), layer)
    _title(c.d, xy, value, size, WHITE, anchor, "head")


def _ring(c, cx, cy, r, pct, color, width=11):
    """Small round meter filled to pct %."""
    s, size = 3, (r + 14) * 2
    layer = Image.new("RGBA", (size * s, size * s), (0, 0, 0, 0))
    g = ImageDraw.Draw(layer)
    m, big, wdt = size * s // 2, r * s, width * s
    box = (m - big, m - big, m + big, m + big)
    g.ellipse(box, outline=TRACK + (255,), width=wdt)
    if pct and pct > 0:
        for deg in range(max(1, int(3.6 * pct))):
            g.arc(box, -90 + deg, -90 + deg + 1.8, fill=color + (255,), width=wdt)
    layer = layer.resize((size, size), Image.Resampling.LANCZOS)
    c.image.paste(layer, (int(cx - size / 2), int(cy - size / 2)), layer)


def _bar(d, x1, x2, y, h, pct, color, marker=None):
    d.rounded_rectangle((x1, y, x2, y + h), radius=h // 2, fill=TRACK)
    if pct and pct > 0:
        d.rounded_rectangle((x1, y, x1 + max(h, int((x2 - x1) * min(100, pct) / 100)), y + h), radius=h // 2, fill=color)
    if marker is not None:
        mx = x1 + int((x2 - x1) * marker / 100)
        d.line((mx, y - 3, mx, y + h + 3), fill=_mix(color, WHITE, 0.6), width=2)


def _item(c, box, y, item, color, kind):
    """One strength / weakness: label, rank chip, big value, note and a league-percentile bar."""
    d = c.d
    x1, _y1, x2, _y2 = box
    d.rounded_rectangle((x1 + 28, y + 2, x1 + 33, y + 140), radius=3, fill=color)
    _text(d, (x1 + 50, y), item["label"], 18, _mix(color, WHITE, 0.35), "la", "head")
    chip = f"#{item['rank']} OF {item['of']}" if item["rank"] else "—"
    _pill(d, x2 - 28 - int(d.textlength(chip, font=font(14))) - 28, y - 2, chip, 14, fill=_mix(color, _INK, 0.7),
          fg=WHITE, outline=color, pad=14, h=28)
    _glow_text(c, (x1 + 50, y + 30), item["value"], 54, color)
    _text(d, (x1 + 50, y + 98), _clip(d, item["note"].upper(), x2 - x1 - 90, 14), 14, MUTED)
    _bar(d, x1 + 50, x2 - 28, y + 124, 10, item["pct"], color, marker=50)


def _empty(c, box, text):
    x1, y1, x2, y2 = box
    _text(c.d, ((x1 + x2) // 2, (y1 + y2) // 2 + 30), text, 20, DIM, "mm", "head")


def _column(c, box, title, subtitle, color, items, kind, empty_text):
    d = c.d
    x1, y1, x2, y2 = box
    _tile_glass(c, box, color)
    d.ellipse((x1 + 28, y1 + 28, x1 + 40, y1 + 40), fill=color)
    _text(d, (x1 + 54, y1 + 20), title, 26, color, "la", "head")
    _text(d, (x1 + 28, y1 + 62), subtitle, 13, MUTED)
    if not items:
        _empty(c, box, empty_text)
        return
    for i, item in enumerate(items):
        _item(c, box, y1 + 96 + i * 158, item, color, kind)


def _depends(c, box, data):
    d = c.d
    x1, y1, x2, y2 = box
    _tile_glass(c, box, DEPEND_COLOR)
    d.ellipse((x1 + 28, y1 + 28, x1 + 40, y1 + 40), fill=DEPEND_COLOR)
    _text(d, (x1 + 54, y1 + 20), "WHAT THE TEAM DEPENDS ON", 22, DEPEND_COLOR, "la", "head")
    rows = data["dep"]["rows"]
    if not rows:
        _empty(c, box, "NOT ENOUGH DATA")
        return
    for i, row in enumerate(rows):
        y = y1 + 64 + i * 78
        who = data["people"].get(row["uid"], {})
        ring = _round_image(_open(who.get("avatar")), 48, ring=DEPEND_COLOR, ring_w=3, initial=who.get("name", "?")[:1])
        _paste(c.image, ring, (x1 + 28, y + 4))
        tx = x1 + 28 + ring.size[0] + 18
        name = _clip(d, who.get("name", "?").upper(), 190, 18, "head")
        _text(d, (tx, y + 2), name, 18, WHITE, "la", "head")
        _text(d, (tx, y + 26), row["label"], 12, MUTED)
        share = row["share"] * 100
        hot = row["share"] >= 0.35
        _text(d, (x2 - 28, y + 6), f"{share:.0f}%", 36, WARN_COLOR if hot else WHITE, "ra", "head")
        _bar(d, tx, x2 - 28, y + 54, 9, share, WARN_COLOR if hot else DEPEND_COLOR)
    top3 = data["dep"]["top3"]
    if top3 is not None:
        tag = "TOP-HEAVY" if top3 >= 0.55 else "SPREAD OUT"
        _text(d, (x1 + 28, y2 - 24), f"TOP 3 PLAYERS = {top3 * 100:.0f}% OF FANTASY POINTS  •  {tag}", 13,
              WARN_COLOR if top3 >= 0.55 else MUTED, "lm")


def _warning(c, box, data):
    d = c.d
    x1, y1, x2, y2 = box
    key = data["dep"]["key"]
    color = RISK_COLORS[key["risk"]] if key else WARN_COLOR
    _tile_glass(c, box, color)
    tri = [(x1 + 34, y1 + 42), (x1 + 52, y1 + 42), (x1 + 43, y1 + 24)]
    d.polygon(tri, fill=color)
    _text(d, (x1 + 43, y1 + 38), "!", 12, _INK, "mm", "head")
    _text(d, (x1 + 64, y1 + 20), "KEY PLAYER WARNING", 22, color, "la", "head")
    if not key:
        _empty(c, box, "NOT ENOUGH DATA")
        return
    chip_w = int(d.textlength(key["risk"], font=font(14))) + 28
    _pill(d, x2 - 28 - chip_w, y1 + 20, key["risk"], 14, fill=color, fg=_INK, pad=14, h=30)
    who = data["people"].get(key["uid"], {})
    ring = _round_image(_open(who.get("avatar")), 96, ring=color, ring_w=5, initial=who.get("name", "?")[:1])
    _halo(c, x1 + 28 + ring.size[0] / 2, y1 + 76 + ring.size[1] / 2, ring.size[0] / 2, color)
    _paste(c.image, ring, (x1 + 28, y1 + 76))
    tx = x1 + 28 + ring.size[0] + 22
    room = x2 - 28 - tx
    name = who.get("name", "?").upper()
    size = _fit(d, name, room, 30, 18, "head")
    _title(d, (tx, y1 + 78), _clip(d, name, room, size, "head"), size, WHITE, "la", "head")
    team = _clip(d, f"{key['fantasy_share'] * 100:.0f}% OF THE TEAM'S FANTASY POINTS", room, 14)
    _text(d, (tx, y1 + 120), team, 14, MUTED)
    chips = [(f"{key['runs_share'] * 100:.0f}% OF RUNS", key["runs_share"]),
             (f"{key['wickets_share'] * 100:.0f}% OF WICKETS", key["wickets_share"]),
             (f"{key['catch_share'] * 100:.0f}% OF CATCHES", key["catch_share"])]
    x = tx
    for label, share in sorted(chips, key=lambda ch: -ch[1])[:2]:
        if share <= 0:
            continue
        x += _pill(d, x, y1 + 146, label, 13, fill=_mix(color, _INK, 0.72), fg=WHITE, outline=color, pad=12, h=28) + 8
    if key["risk"] == "LOW RISK":
        line = "No single player carries this team."
    else:
        line = f"If {who.get('name', 'he')} is out, a big part of the team's output goes with him."
    _text(d, (x1 + 28, y2 - 30), _clip(d, line, x2 - x1 - 56, 15, "reg"), 15, _mix(MUTED, WHITE, 0.3), "lm", "reg")


def _render(data):
    team = data["team"]
    col_a, col_b = pick_sides(team.get("color"), None)
    c = _Canvas(col_a, col_b)
    for box in ((50, 22, 1550, 250), (50, 270, 540, 856), (560, 270, 1050, 856), (1070, 270, 1550, 600),
                (1070, 616, 1550, 856)):
        c.card(box, 26, 150 if box[1] > 250 else 165)
    d = c.seal()
    accent = c.a

    ring = _round_image(_open(team.get("logo")), 150, ring=accent, ring_w=6, initial=team["name"][:1])
    _halo(c, 80 + ring.size[0] / 2, 54 + ring.size[1] / 2, ring.size[0] / 2, accent)
    _paste(c.image, ring, (80, 54))
    tx = 80 + ring.size[0] + 30
    name = team["name"].upper()
    size = _fit(d, name, 560, 54, 26, "head")
    _title(d, (tx, 62), _clip(d, name, 560, size, "head"), size, WHITE, "la", "head")
    x = tx
    report = data["report"]
    x += _pill(d, x, 132, report["style"], 15, fill=accent, fg=_INK, pad=16, h=34) + 10
    standing = team.get("standing")
    if standing:
        x += _pill(d, x, 132, f"RANK #{standing['rank']} OF {standing['of']}", 15, fg=WHITE, outline=accent,
                   pad=16, h=34) + 10
        _pill(d, x, 132, f"{standing['won']}W - {standing['lost']}L", 15, fg=WHITE, outline=accent, pad=16, h=34)
    _text(d, (tx, 188), _clip(d, verdict_line(report), 640, 17), 17, _mix(MUTED, WHITE, 0.35))
    _text(d, (tx, 216), f"BENCHMARKED AGAINST {report['teams']} TEAMS  •  {data['matches']} MATCHES", 12, DIM)

    # grades: four rings on the right of the hero
    for i, g in enumerate(report["grades"]):
        cx = 1000 + i * 128
        _ring(c, cx, 118, 40, g["score"], g["color"])
        _text(d, (cx, 118), g["letter"], 30, WHITE, "mm", "head")
        _text(d, (cx, 184), g["label"], 13, _mix(g["color"], WHITE, 0.35), "mm", "head")
        _text(d, (cx, 206), f"{g['score']:.0f}/100" if g["score"] is not None else "NO DATA", 12, MUTED, "mm")

    _column(c, (50, 270, 540, 856), "STRENGTHS", "BEST AGAINST THE LEAGUE AVERAGE", STRENGTH_COLOR,
            report["strengths"], "s", "NO CLEAR STRENGTH YET")
    _column(c, (560, 270, 1050, 856), "WEAKNESSES", "WORST AGAINST THE LEAGUE AVERAGE", WEAKNESS_COLOR,
            report["weaknesses"], "w", "NO CLEAR WEAKNESS")
    _depends(c, (1070, 270, 1550, 600), data)
    _warning(c, (1070, 616, 1550, 856), data)
    _footer(c, f"GOOGLI MANAGER  •  SCOUTING REPORT  •  {team['name']}", data.get("guild"))
    return c.image


def _render_players(data):
    team = data["team"]
    col_a, col_b = pick_sides(team.get("color"), None)
    c = _Canvas(col_a, col_b)
    players = data["players"][:6]
    rows = max(1, (len(players) + 1) // 2)
    boxes = []
    for i in range(len(players)):
        col, row = i % 2, i // 2
        gap = 16
        h = (856 - 130 - gap * (rows - 1)) // rows
        boxes.append((50 + col * 760, 130 + row * (h + gap), 790 + col * 760, 130 + row * (h + gap) + h))
    c.card((50, 22, 1550, 112), 24, 170)
    for b in boxes:
        c.card(b, 24, 150)
    d = c.seal()
    accent = c.a
    _title(d, (76, 66), "PLAYER STRENGTHS", 46, WHITE, "lm", "head")
    _text(d, (1524, 52), _clip(d, team["name"].upper(), 600, 24, "head"), 24, WHITE, "rm", "head")
    _text(d, (1524, 82), "BEST AND WEAKEST NUMBERS AGAINST EVERY PLAYER IN THE LEAGUE", 12, MUTED, "rm")
    if not players:
        _text(d, (800, 480), "NOT ENOUGH PLAYER DATA YET", 26, DIM, "mm", "head")
    role_color = {"BATTER": CORAL, "BOWLER": VIOLET, "ALL-ROUNDER": GOLD, "FIELDER": MINT}
    for box, pl in zip(boxes, players):
        x1, y1, x2, y2 = box
        color = role_color.get(pl["role"], accent)
        _tile_glass(c, box, color)
        person = data["people"].get(pl["uid"], {})
        ring = _round_image(_open(person.get("avatar")), 64, ring=color, ring_w=4,
                            initial=(person.get("name") or "?")[:1])
        _paste(c.image, ring, (x1 + 24, y1 + 22))
        _text(d, (x1 + 106, y1 + 24), _clip(d, (person.get("name") or "UNKNOWN").upper(), 330, 22, "head"), 22, WHITE, "la", "head")
        pw = _pill(d, x1 + 106, y1 + 58, pl["role"], 12, fill=_mix(color, _INK, 0.7), fg=WHITE, outline=color, pad=12, h=26)
        _text(d, (x1 + 106 + pw + 12, y1 + 71), f"{pl['matches']} MATCHES", 12, MUTED, "lm")
        _text(d, (x2 - 28, y1 + 28), f"{pl['fantasy']:.0f}", 30, color, "ra", "head")
        _text(d, (x2 - 28, y1 + 64), "FANTASY PTS", 11, MUTED, "ra")
        y = y1 + 100
        items = [(s_, STRENGTH_COLOR) for s_ in pl["strong"]] + [(w_, WEAKNESS_COLOR) for w_ in pl["weak"]]
        if not items:
            _text(d, (x1 + 28, y + 8), "NOTHING STANDS OUT EITHER WAY", 13, DIM, "la", "head")
        for item, col in items:
            top = max(1, round(100 - item["pct"])) if col == STRENGTH_COLOR else None
            _text(d, (x1 + 28, y), item["label"], 13, _mix(col, WHITE, 0.35), "la", "head")
            _text(d, (x1 + 250, y - 1), item["value"], 17, WHITE, "la", "head")
            tag = f"TOP {top}%" if top is not None else f"BOTTOM {max(1, round(item['pct']))}%"
            _text(d, (x2 - 28, y), tag, 12, col, "ra", "bold")
            _bar(d, x1 + 28, x2 - 28, y + 24, 7, item["pct"], col, marker=50)
            y += 40
    _footer(c, f"GOOGLI MANAGER  •  PLAYER STRENGTHS  •  {team['name']}", data.get("guild"))
    return c.image


def render_players(data):
    return _png(_render_players(data))


def _person_hero(c, who, report, x_left=80, accent=None):
    d = c.d
    accent = accent or c.a
    ring = _round_image(_open(who.get("avatar")), 150, ring=accent, ring_w=6, initial=who["name"][:1])
    _halo(c, x_left + 75, 54 + 75, 75, accent)
    _paste(c.image, ring, (x_left, 54))
    tx = x_left + 180
    name = who["name"].upper()
    size = _fit(d, name, 560, 54, 26, "head")
    _title(d, (tx, 62), _clip(d, name, 560, size, "head"), size, WHITE, "la", "head")
    x = tx
    x += _pill(d, x, 132, report["role"], 15, fill=accent, fg=_INK, pad=16, h=34) + 10
    if who.get("team"):
        x += _pill(d, x, 132, _clip(d, who["team"].upper(), 240, 15), 15, fg=WHITE, outline=accent, pad=16, h=34) + 10
    _pill(d, x, 132, f"{report['matches']} MATCHES", 15, fg=WHITE, outline=accent, pad=16, h=34)
    _text(d, (tx, 188), _clip(d, report["verdict"], 640, 17), 17, _mix(MUTED, WHITE, 0.35))
    _text(d, (tx, 216), f"BENCHMARKED AGAINST {report['players']} PLAYERS", 12, DIM)


def _numbers_tile(c, box, report):
    d = c.d
    x1, y1, x2, y2 = box
    _tile_glass(c, box, DEPEND_COLOR)
    d.ellipse((x1 + 28, y1 + 28, x1 + 40, y1 + 40), fill=DEPEND_COLOR)
    _text(d, (x1 + 54, y1 + 20), "ALL NUMBERS", 26, DEPEND_COLOR, "la", "head")
    _text(d, (x1 + 28, y1 + 62), "WHERE HE SITS AMONG EVERY PLAYER", 13, MUTED)
    keys = [k for k, *_r in PLAYER_METRICS]
    step = (y2 - y1 - 110) // len(keys)
    for i, key in enumerate(keys):
        y = y1 + 96 + i * step
        v = report["scores"].get(key)
        label = PLAYER_META[key][0]
        if v is None:
            _text(d, (x1 + 28, y), label, 14, DIM, "la", "head")
            _text(d, (x2 - 28, y), "NO DATA", 13, DIM, "ra", "bold")
            continue
        col = STRENGTH_COLOR if v["pct"] >= 60 else WEAKNESS_COLOR if v["pct"] <= 40 else (150, 170, 210)
        _text(d, (x1 + 28, y), label, 14, _mix(col, WHITE, 0.35), "la", "head")
        _text(d, (x2 - 28, y - 2), v["value"], 20, WHITE, "ra", "head")
        _bar(d, x1 + 28, x2 - 28, y + 30, 8, v["pct"], col, marker=50)
        _text(d, (x1 + 28 + int(d.textlength(label, font=font(14, "head"))) + 12, y + 4),
              f"#{v['rank']} OF {v['of']}", 11, MUTED)


def _render_player(data):
    who, report = data["who"], data["report"]
    col_a, col_b = pick_sides(who.get("color"), None)
    c = _Canvas(col_a, col_b)
    for box in ((50, 22, 1550, 250), (50, 270, 540, 856), (560, 270, 1050, 856), (1070, 270, 1550, 856)):
        c.card(box, 26, 150 if box[1] > 250 else 165)
    c.seal()
    d = c.d
    _person_hero(c, who, report)
    for i, g in enumerate(report["grades"]):
        cx = 1000 + i * 128
        _ring(c, cx, 118, 40, g["score"], g["color"])
        _text(d, (cx, 118), g["letter"], 30, WHITE, "mm", "head")
        _text(d, (cx, 184), g["label"], 13, _mix(g["color"], WHITE, 0.35), "mm", "head")
        _text(d, (cx, 206), f"{g['score']:.0f}/100" if g["score"] is not None else "NO DATA", 12, MUTED, "mm")
    _column(c, (50, 270, 540, 856), "STRENGTHS", "BEST AGAINST ALL PLAYERS", STRENGTH_COLOR, report["strong"], "s",
            "NO CLEAR STRENGTH YET")
    _column(c, (560, 270, 1050, 856), "WEAKNESSES", "WEAKEST AGAINST ALL PLAYERS", WEAKNESS_COLOR, report["weak"], "w",
            "NO CLEAR WEAKNESS")
    _numbers_tile(c, (1070, 270, 1550, 856), report)
    _footer(c, f"GOOGLI MANAGER  •  PLAYER SCOUTING  •  {who['name']}", data.get("guild"))
    return c.image


def _render_player_compare(data):
    A, B = data["a"], data["b"]
    ra, rb = A["report"], B["report"]
    col_a, col_b = pick_sides(A.get("color"), B.get("color"))
    c = _Canvas(col_a, col_b)
    c.card((50, 22, 1550, 262), 26, 165)
    c.card((50, 282, 1550, 800), 26, 150)
    c.seal()
    d = c.d
    for who, rep, left, accent in ((A, ra, True, c.a), (B, rb, False, c.b)):
        ax = 80 if left else 1550 - 30 - 150
        ring = _round_image(_open(who.get("avatar")), 130, ring=accent, ring_w=5, initial=who["name"][:1])
        _paste(c.image, ring, (ax, 40))
        tx = ax + 150 if left else ax - 20
        anchor = "la" if left else "ra"
        name = who["name"].upper()
        size = _fit(d, name, 430, 44, 22, "head")
        _title(d, (tx, 48), _clip(d, name, 430, size, "head"), size, WHITE, anchor, "head")
        _text(d, (tx, 108), f"{rep['role']}  •  {(who.get('team') or 'FREE AGENT').upper()}", 14, MUTED, anchor)
        _text(d, (tx, 134), _clip(d, rep["verdict"], 430, 14), 14, _mix(MUTED, WHITE, 0.35), anchor)
        best = rep["strong"][0]["label"] if rep["strong"] else "—"
        _text(d, (tx, 168), f"BEST AT  {best}", 13, STRENGTH_COLOR, anchor, "bold")
        worst = rep["weak"][0]["label"] if rep["weak"] else "—"
        _text(d, (tx, 192), f"WEAKEST AT  {worst}", 13, WEAKNESS_COLOR, anchor, "bold")
    _title(d, (800, 100), "VS", 46, MUTED, "mm", "head")
    # grade comparison between the two heroes
    for i, label in enumerate(("BATTING", "BOWLING", "FIELDING")):
        cx = 800 + (i - 1) * 150
        ga, gb = ra["grades"][i]["score"], rb["grades"][i]["score"]
        _text(d, (cx, 164), label, 12, MUTED, "mm", "bold")
        for val, other, dx, col in ((ga, gb, -34, c.a), (gb, ga, 34, c.b)):
            if val is None:
                _text(d, (cx + dx, 196), "—", 28, DIM, "mm", "head")
            else:
                _text(d, (cx + dx, 196), f"{val:.0f}", 30, col if other is None or val >= other else MUTED, "mm", "head")
        _text(d, (cx, 196), "|", 22, EDGE, "mm")
    x1, y1, x2, y2 = 50, 282, 1550, 800
    _text(d, (x1 + 34, y1 + 20), "WHO IS STRONGER AT WHAT  (LEAGUE PERCENTILE, BIGGER BAR = BETTER)", 14, MUTED, "la", "bold")
    keys = [k for k, *_r in PLAYER_METRICS]
    step = (y2 - y1 - 120) // len(keys)
    wins_a = wins_b = 0
    mid = (x1 + x2) // 2
    for i, key in enumerate(keys):
        y = y1 + 62 + i * step
        label = PLAYER_META[key][0]
        va, vb = ra["scores"].get(key), rb["scores"].get(key)
        _text(d, (mid, y + 12), label, 15, MUTED, "mm", "head")
        lead = 0
        if va and vb and abs(va["pct"] - vb["pct"]) >= 1:
            lead = 1 if va["pct"] > vb["pct"] else -1
        wins_a += lead > 0
        wins_b += lead < 0
        for side, v, col, left in ((0, va, c.a, True), (1, vb, c.b, False)):
            win = (lead > 0) == left and lead != 0
            if v is None:
                _text(d, (x1 + 34 if left else x2 - 34, y + 12), "NO DATA", 14, DIM, "lm" if left else "rm", "bold")
                continue
            bx1, bx2 = (x1 + 34, mid - 150) if left else (mid + 150, x2 - 34)
            d.rounded_rectangle((bx1, y + 6, bx2, y + 20), radius=7, fill=TRACK)
            w = int((bx2 - bx1) * max(2, min(100, v["pct"])) / 100)
            fill = col if win or lead == 0 else _mix(col, _INK, 0.55)
            if left:
                d.rounded_rectangle((bx2 - w, y + 6, bx2, y + 20), radius=7, fill=fill)
            else:
                d.rounded_rectangle((bx1, y + 6, bx1 + w, y + 20), radius=7, fill=fill)
            _text(d, (bx1 if left else bx2, y + 26), v["value"], 15, WHITE if win else MUTED,
                  "la" if left else "ra", "head")
            tag = f"TOP {max(1, round(100 - v['pct']))}%" if v["pct"] >= 50 else f"BOTTOM {max(1, round(v['pct']))}%"
            _text(d, (bx2 if left else bx1, y + 26), tag, 12,
                  col if win else MUTED, "ra" if left else "la", "bold")
    text = (f"EVEN  {wins_a} – {wins_b}" if wins_a == wins_b else
            f"{(A if wins_a > wins_b else B)['name'].upper()} IS STRONGER  {max(wins_a, wins_b)} – {min(wins_a, wins_b)}")
    text = _clip(d, text, 700, 20)
    pw = int(d.textlength(text, font=font(20))) + 44
    _pill(d, 800 - pw // 2, 810, text, 20, fill=(c.a if wins_a > wins_b else c.b) if wins_a != wins_b else None,
          fg=_INK if wins_a != wins_b else WHITE, outline=None if wins_a != wins_b else (60, 80, 120), pad=22, h=40)
    _footer(c, f"GOOGLI MANAGER  •  STRENGTHS  •  {A['name']} vs {B['name']}"[:90], data.get("guild"))
    return c.image


def render_player_strengths(data):
    return _png(_render_player(data))


def render_player_compare(data):
    return _png(_render_player_compare(data))


def render_strengths(data):
    """data -> PNG bytes."""
    return _png(_render(data))


# =========================================================================== command

class Strengths(commands.Cog):
    """+strengths / +scout."""

    def __init__(self, bot):
        self.bot = bot
        self._league = {}          # guild id -> (stats index object, {team key: numbers})

    @property
    def compare(self):
        return self.bot.get_cog("CompareTeams")

    def _league_numbers(self, guild, index):
        hit = self._league.get(int(guild.id))
        if hit and hit[0] is index:           # same index object = same data, nothing to redo
            return hit[1]
        from .lb import _totals
        from .stats import fantasy_raw_points
        core = self.compare.core
        points = self.bot.get_cog("PointsTable")
        out = {}
        for team in core.db.execute(
            "SELECT id, name FROM registered_teams WHERE guild_id=? AND status='approved'", (int(guild.id),)
        ).fetchall():
            key = str(team["name"]).casefold()
            standing = None
            if points is not None and hasattr(points, "team_standing"):
                try:
                    standing = points.team_standing(guild.id, team["name"])
                except Exception:
                    standing = None
            numbers = team_numbers(index["stats"].get(key, {}), index["fielding"].get(key, {}), standing,
                                   _totals, lambda rows: fantasy_raw_points(rows)[2])
            if numbers:
                out[key] = numbers
        self._league[int(guild.id)] = (index, out)
        return out

    async def _avatar(self, guild, uid):
        member = guild.get_member(uid) or self.bot.get_user(uid)
        if member is None:
            try:
                member = await self.bot.fetch_user(uid)
            except discord.HTTPException:
                member = None
        return uid, await fetch_avatar(self.bot, member)

    async def _player_mode(self, ctx, text):
        """+strengths @player (one player's strengths) or +strengths @a @b (two players compared).
        Returns True when it handled the command."""
        ins = self.bot.get_cog("Insights")
        words = text.strip().lower()
        if ins is None or not (re.search(r"<@!?\d+>", text) or words in ("me", "my", "myself")):
            return False
        usage = "Use `+strengths @player` or `+strengths @player | @player`."
        if words in ("me", "my", "myself"):
            ids = [ctx.author.id]
        else:
            ids = await ins.members_from(ctx, text, usage, need=1, max_n=2)
            if not ids:
                return True
        if len(ids) == 2 and ids[0] == ids[1]:
            await ctx.reply("Pick two different players.", mention_author=False)
            return True
        wait = on_cooldown(ctx.author.id)
        if wait:
            await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
            return True
        loading = await ctx.reply("⏳ Scouting the player...", mention_author=False)
        try:
            rated = ins.rated_all(ctx.guild.id)
            everyone = {uid: player_numbers(p) for uid, p in rated.items() if p["matches"] or p["catches"]}
            people = []
            for uid in ids:
                report = player_scout(everyone, uid)
                who = await ins.person(ctx.guild, uid, rated)
                if report is None:
                    await loading.edit(content=f"**{discord.utils.escape_markdown(who['name'])}** has too few recorded "
                                               "matches to scout yet.")
                    return True
                who["report"] = report
                people.append(who)
            if len(people) == 1:
                data = {"who": people[0], "report": people[0]["report"], "guild": ctx.guild.name}
                png = await render_png(render_player_strengths, data)
            else:
                data = {"a": people[0], "b": people[1], "guild": ctx.guild.name}
                png = await render_png(render_player_compare, data)
            await loading.edit(content=None, attachments=[discord.File(io.BytesIO(png), filename="strengths.png")])
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"[strengths] player mode failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                await loading.edit(content="I couldn't build that scouting report. Check the bot logs.")
            except Exception:
                pass
        return True


    @commands.command(name="strengths", aliases=["scout", "scouting"])
    @commands.guild_only()
    async def strengths(self, ctx, *, team: str = ""):
        """Scouting report: +strengths (your team), +strengths <team>, +strengths @player, +strengths @a @b."""
        compare = self.compare
        if compare is None:
            await ctx.reply("The team system isn't loaded right now.", mention_author=False)
            return
        if await self._player_mode(ctx, team):
            return
        core = compare.core
        if team.strip():
            row, error = compare._resolve(ctx.guild.id, team.strip())
            if error:
                await ctx.reply(error, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                return
        else:
            row = core._membership(ctx.guild.id, ctx.author.id)
            if not row:
                await ctx.reply("You're not in a team. Use `+strengths Team Name`.", mention_author=False)
                return
        wait = on_cooldown(ctx.author.id)
        if wait:
            await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
            return
        loading = await ctx.reply("⏳ Writing the scouting report...", mention_author=False)
        try:
            guild = ctx.guild
            league = self._league_numbers(guild, compare._guild_rows(guild.id))
            key = str(row["name"]).casefold()
            if key not in league:
                await loading.edit(content=f"**{discord.utils.escape_markdown(str(row['name']))}** has no recorded "
                                           "matches yet, so there is nothing to scout.")
                return
            me = league[key]
            report, dep = scout(key, league), dependencies(me)

            plist = player_report(league, key)
            uids = ({r["uid"] for r in dep["rows"]} | ({dep["key"]["uid"]} if dep["key"] else set())
                    | {p["uid"] for p in plist})
            logo_task = compare._logo_for(ctx, row)
            logo, *pairs = await asyncio.gather(logo_task, *(self._avatar(guild, u) for u in uids))
            avatars = dict(pairs)
            color = team_role_color(core, guild, row)
            if color is None:
                color = accent_from_logo(_open(logo))
            people = {u: {"name": compare._display_name(guild, u), "avatar": avatars.get(u)} for u in uids}
            data = {
                "team": {"name": str(row["name"]), "logo": logo, "color": color, "standing": me["standing"]},
                "report": report, "dep": dep, "people": people, "players": plist, "matches": me["matches"], "guild": guild.name,
            }
            png = await render_png(render_strengths, data)
            view = ImagePager(ctx, png, 2, lambda i: render_strengths(data) if i == 0 else render_players(data),
                              ("Team report", "Players"), "strengths.png")
            view.message = await loading.edit(
                content=None, attachments=[discord.File(io.BytesIO(png), filename="strengths.png")], view=view)
        except Exception as exc:
            print(f"[strengths] failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                await loading.edit(content="I couldn't build the scouting report. Check the bot logs.")
            except Exception:
                pass


async def setup(bot):
    await bot.add_cog(Strengths(bot))
