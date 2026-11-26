"""+similar [@player]: who plays most like this player? One file: matching, drawing and command.

How the match works
  Every player with at least 3 matches is described by nine numbers in three blocks: BATTING (runs per
  match, batting average, strike rate, balls faced per match), BOWLING (wickets per match, economy,
  bowling average, balls bowled per match) and FIELDING (catches per match), plus one overall-impact
  number (fantasy per match, small weight).
  Each number is turned into the player's percentile among all players, so a few stars cannot stretch
  the scale and a difference of 0.1 means the same for runs as for wickets. Numbers from few matches are
  pulled towards the middle (a 3-match average is not trusted like a 12-match one). A player who never
  bats or never bowls scores 0 in that block - "does not bowl" is part of his style, not a missing value.
  The distance is the weighted root-mean-square percentile gap, block by block (batting 40%, bowling 40%,
  fielding 10%, impact 10%), so every block counts by its own weight and cannot be diluted by the others.
  Similarity is the distance relative to how far apart two typical players of this server are:
  100 * exp(-1.2 * distance / typical distance). Identical = 100, a typical pair is about 30.

The card shows the best match in the league, the closest TEAMMATE and the closest RIVAL (other team),
a side-by-side bar chart of the numbers and the playing traits the two share.
The numbers come from the stats index CompareTeams already keeps (no extra database work).
"""
import asyncio
import io
import math
from statistics import quantiles

import discord
from discord.ext import commands
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import xi_engine
from .h2h import fetch_avatar, on_cooldown, render_png, team_role_color
from .teaminfo_image import (
    CORAL, DIM, MINT, MUTED, WHITE, _clip, _fit, _mix, _open, _paste, _pill, _round_image, _text, font,
)
from .versus_image import _Canvas, _footer, _png, _title, pick_sides

MIN_MATCHES = 3          # a player needs this many matches to be compared
MIN_SUBJECT = 2          # ... except the player you ask about

# key, label, text format (the labels and formats are used by the card)
FEATURES = [
    ("runs_pm", "RUNS / MATCH", "{:.1f}"),
    ("bat_avg", "BATTING AVERAGE", "{:.1f}"),
    ("sr", "STRIKE RATE", "{:.1f}"),
    ("wk_pm", "WICKETS / MATCH", "{:.2f}"),
    ("econ", "ECONOMY", "{:.2f}"),
    ("bowl_avg", "BOWLING AVERAGE", "{:.1f}"),
    ("catch_pm", "CATCHES / MATCH", "{:.2f}"),
    ("fant_pm", "FANTASY / MATCH", "{:.1f}"),
]
# what the distance uses: key, block, True when a LOWER number is better, True when the number is about
# what a player does (workload) and not how well, so it is not pulled towards the middle
MODEL = [
    ("runs_pm", "bat", False, False), ("bat_avg", "bat", False, False), ("sr", "bat", False, False),
    ("bf_pm", "bat", False, True),
    ("wk_pm", "bowl", False, False), ("econ", "bowl", True, False), ("bowl_avg", "bowl", True, False),
    ("bb_pm", "bowl", False, True),
    ("catch_pm", "field", False, False), ("fant_pm", "impact", False, False),
]
BLOCK_WEIGHT = {"bat": 0.40, "bowl": 0.40, "field": 0.10, "impact": 0.10}
SHRINK = 2.0             # matches' worth of "no information" a player's numbers are blended with
CURVE = 1.2
SHOWN = ["runs_pm", "bat_avg", "sr", "wk_pm", "catch_pm", "fant_pm"]     # rows of the bar chart
NAMES = {k: (label, fmt) for k, label, fmt in FEATURES}

# trait name, feature, which end of the league counts ("hi" = top quarter, "lo" = best quarter = lowest)
TRAITS = [
    ("POWER HITTER", "sr", "hi"),
    ("RUN MACHINE", "runs_pm", "hi"),
    ("WICKET TAKER", "wk_pm", "hi"),
    ("ECONOMICAL", "econ", "lo"),
    ("SAFE HANDS", "catch_pm", "hi"),
    ("FANTASY STAR", "fant_pm", "hi"),
]


# =========================================================================== matching (no Discord, no images)

def _raw(p):
    """The per-match numbers of a profile; economy / bowling average are None if he never bowls."""
    m = max(1, p["matches"])
    bowls = p["balls_bowled"] > 0
    return {
        "runs_pm": p["runs"] / m, "bat_avg": p["bat_avg"] or 0.0, "sr": p["strike_rate"] or 0.0,
        "wk_pm": p["wickets"] / m,
        "econ": p["economy"] if (bowls and p["economy"] is not None) else None,
        "bowl_avg": p["bowl_avg"] if (bowls and p["bowl_avg"] is not None) else None,
        "catch_pm": p["catches"] / m, "fant_pm": p["fantasy"] / m,
        "bf_pm": p["balls_faced"] / m, "bb_pm": p["balls_bowled"] / m,
    }


def _rank(sorted_values, v):
    """Percentile (0..1) of v among the sorted values; ties share the middle."""
    from bisect import bisect_left, bisect_right
    n = len(sorted_values)
    if not n:
        return 0.5
    return (bisect_left(sorted_values, v) + bisect_right(sorted_values, v)) / (2.0 * n)


def _vector(p, r, tables):
    """Percentile vector of one player (same order as MODEL)."""
    m = max(1, p["matches"])
    bats, bowls = p["balls_faced"] > 0, p["balls_bowled"] > 0
    out = []
    for key, block, lower_better, fact in MODEL:
        v = r[key]
        if block == "bat" and not bats:
            u = 0.0
        elif block == "bowl" and not bowls:
            u = 0.0
        elif v is None:
            u = 0.0                                   # bowls but never took a wicket: no bowling average
        else:
            u = _rank(tables[key], v)
            if lower_better:
                u = 1.0 - u
            if not fact:                              # few matches -> less trust: pull towards the middle
                n = p["bat_inn"] if block == "bat" else m
                u = 0.5 + (u - 0.5) * n / (n + SHRINK)
        out.append(u)
    return out


def _quartile(values, upper):
    values = sorted(values)
    if len(values) < 4:
        return values[-1] if upper else values[0]
    q = quantiles(values, n=4)
    return q[2] if upper else q[0]


def build_model(index, totals_fn, fantasy_fn):
    profiles, raw = {}, {}
    for uid, rows in index["players"].items():
        caught, dropped = index["fielding_players"].get(uid, (0, 0))
        profiles[uid] = xi_engine.profile(uid, rows, caught, dropped, totals_fn, fantasy_fn)
        raw[uid] = _raw(profiles[uid])
    eligible = [u for u, p in profiles.items() if p["matches"] >= MIN_MATCHES]
    tables = {}
    for key, block, _lb, _fact in MODEL:
        vals = []
        for u in eligible:
            p = profiles[u]
            if (block == "bat" and p["balls_faced"] <= 0) or (block == "bowl" and p["balls_bowled"] <= 0):
                continue
            if raw[u][key] is not None:
                vals.append(raw[u][key])
        tables[key] = sorted(vals)
    vec = {u: _vector(profiles[u], raw[u], tables) for u in raw}
    limits = {}
    for name, key, side in TRAITS:
        col = [raw[u][key] for u in eligible if raw[u][key] is not None and (key != "wk_pm" or raw[u][key] > 0)]
        limits[name] = (_quartile(col, side == "hi") if col else None, key, side)
    model = {"profiles": profiles, "raw": raw, "vec": vec, "eligible": eligible, "limits": limits,
             "pool": xi_engine.Pool(list(profiles.values())), "typical": 1.0}
    model["typical"] = _typical_distance(model)
    return model


def _typical_distance(model, sample=60):
    """Median distance between two eligible players: the yardstick that turns a distance into a percentage."""
    people = model["eligible"]
    if len(people) > sample:
        step = len(people) / sample
        people = [people[int(i * step)] for i in range(sample)]
    ds = sorted(distance(model, a, b) for i, a in enumerate(people) for b in people[i + 1:])
    return max(0.05, ds[len(ds) // 2]) if ds else 1.0


def distance(model, a, b):
    """Weighted root-mean-square percentile gap, block by block (each block counts by its own weight)."""
    va, vb = model["vec"][a], model["vec"][b]
    totals, counts = {}, {}
    for (key, block, _lb, _fact), x, y in zip(MODEL, va, vb):
        totals[block] = totals.get(block, 0.0) + (x - y) ** 2
        counts[block] = counts.get(block, 0) + 1
    return math.sqrt(sum(BLOCK_WEIGHT[b] * totals[b] / counts[b] for b in totals))


def similarity(model, dist):
    return max(0, min(100, round(100 * math.exp(-CURVE * dist / model["typical"]))))


def neighbours(model, uid):
    """[(similarity %, uid)] for every other eligible player, most similar first."""
    ranked = sorted((distance(model, uid, o), o) for o in model["eligible"] if o != uid)
    return [(similarity(model, d), o) for d, o in ranked]


def traits(model, uid):
    out = []
    for name, (limit, key, side) in model["limits"].items():
        value = model["raw"][uid][key]
        if limit is None or value is None:
            continue
        if (side == "hi" and value >= limit and value > 0) or (side == "lo" and value <= limit):
            out.append(name)
    return out


def verdict(pct):
    return ("NEAR-IDENTICAL" if pct >= 85 else "VERY SIMILAR" if pct >= 70
            else "SIMILAR" if pct >= 50 else "LOOSELY SIMILAR")


# =========================================================================== drawing

_INK = (12, 18, 32)
TEAMMATE_COLOR, RIVAL_COLOR, GOLD_ACCENT = MINT, CORAL, (255, 206, 90)
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
        _blur_ellipse((w, h), (-w * 0.25, -h * 0.65, w * 0.65, h * 0.55), color, 100, 70),
        _blur_ellipse((w, h), (w * 0.55, h * 0.55, w * 1.3, h * 1.4), color, 50, 80))
    glow.putalpha(ImageChops.multiply(glow.getchannel("A"), _rounded_mask((w, h), 26)))
    c.image.paste(glow, (x1, y1), glow)
    d = c.d
    d.rounded_rectangle(box, radius=26, outline=_mix(color, _INK, 0.35), width=2)
    d.rounded_rectangle((x1 + 3, y1 + 3, x2 - 3, y2 - 3), radius=23, outline=_mix(color, _INK, 0.8), width=1)
    d.line((x1 + 30, y1 + 2, x2 - 30, y1 + 2), fill=_mix(color, WHITE, 0.55), width=1)
    d.rounded_rectangle((x1 + 28, y1 - 1, x1 + 150, y1 + 4), radius=2, fill=color)


def _halo(c, cx, cy, radius, color):
    size = int(radius * 3.4)
    glow = _blur_ellipse((size, size), (size * 0.2, size * 0.2, size * 0.8, size * 0.8), color, 140, 36)
    c.image.paste(glow, (int(cx - size / 2), int(cy - size / 2)), glow)


def _glow_text(c, xy, value, size, color, anchor="la"):
    """Big number with a coloured glow behind it ("la" = x is the left edge, "ra" = the right edge)."""
    pad = 24
    f = font(size, "head")
    width = int(c.d.textlength(value, font=f))
    left = xy[0] if anchor == "la" else xy[0] - width
    layer = Image.new("RGBA", (width + pad * 2, size + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((pad, pad), value, font=f, fill=color + (230,))
    layer = layer.filter(ImageFilter.GaussianBlur(9))
    c.image.paste(layer, (int(left - pad), int(xy[1] - pad)), layer)
    _title(c.d, xy, value, size, WHITE, anchor, "head")


def _gauge(c, cx, cy, r, pct, col_a, col_b):
    """Ring that fills to pct %, blending from colour A to colour B, with a soft glow."""
    s, size = 3, (r + 24) * 2
    layer = Image.new("RGBA", (size * s, size * s), (0, 0, 0, 0))
    g = ImageDraw.Draw(layer)
    m, big, wdt = size * s // 2, r * s, 18 * s
    box = (m - big, m - big, m + big, m + big)
    g.ellipse(box, outline=TRACK + (255,), width=wdt)
    degrees = max(1, int(3.6 * pct))
    for deg in range(degrees):
        col = _mix(col_a, col_b, deg / max(1, degrees - 1))
        g.arc(box, -90 + deg, -90 + deg + 1.8, fill=col + (255,), width=wdt)
    layer = layer.resize((size, size), Image.Resampling.LANCZOS)
    glow = _blur_ellipse((size + 80, size + 80), (60, 60, size + 20, size + 20), _mix(col_a, col_b, 0.5), 90, 40)
    c.image.paste(glow, (int(cx - size / 2 - 40), int(cy - size / 2 - 40)), glow)
    c.image.paste(layer, (int(cx - size / 2), int(cy - size / 2)), layer)


def _chips(d, x, y, labels, accent, left, room):
    """Small trait chips from the inner edge outwards (up to 3, as many as fit)."""
    for label in labels[:3]:
        label = _clip(d, label, room, 13)
        width = int(d.textlength(label, font=font(13))) + 26
        if width > room:
            break
        _pill(d, x if left else x - width, y, label, 13, fill=_mix(accent, _INK, 0.75), fg=WHITE,
              outline=_mix(accent, _INK, 0.2), pad=13, h=28)
        x = x + width + 8 if left else x - width - 8
        room -= width + 8


def _player_block(c, who, accent, left):
    """Avatar, name, team and trait chips of one player on the hero."""
    d = c.d
    ring = _round_image(_open(who.get("avatar")), 170, ring=accent, ring_w=6, initial=who["name"][:1])
    ax = 70 if left else 1600 - 70 - ring.size[0]
    ay = 140
    _halo(c, ax + ring.size[0] / 2, ay + ring.size[1] / 2, ring.size[0] / 2, accent)
    _paste(c.image, ring, (ax, ay))
    tx = ax + ring.size[0] + 28 if left else ax - 28
    anchor = "la" if left else "ra"
    room = 330
    name = who["name"].upper()
    size = _fit(d, name, room, 46, 22, "head")
    _title(d, (tx, ay + 6), _clip(d, name, room, size, "head"), size, WHITE, anchor, "head")
    team = _clip(d, (who.get("team") or "FREE AGENT").upper(), room - 36, 16)
    pw = int(d.textlength(team, font=font(16))) + 36
    _pill(d, tx if left else tx - pw, ay + 62, team, 16, fg=WHITE, outline=accent, pad=18, h=34)
    role = (who.get("role") or "").upper()
    _chips(d, tx, ay + 108, ([role] if role else []) + list(who.get("traits", [])), accent, left, room)


def _butterfly(c, box, data, col_a, col_b):
    """Mirrored bars: the asked player grows to the left, the match to the right, scaled to the larger one."""
    d = c.d
    x1, y1, x2, y2 = box
    _tile_glass(c, box, _mix(col_a, col_b, 0.5))
    d.ellipse((x1 + 28, y1 + 28, x1 + 40, y1 + 40), fill=GOLD_ACCENT)
    _text(d, (x1 + 54, y1 + 20), "STYLE COMPARISON", 26, GOLD_ACCENT, "la", "head")
    cx = (x1 + x2) // 2
    reach, top, row_h = 330, y1 + 76, 46
    for i, key in enumerate(SHOWN):
        y = top + i * row_h
        a, b = data["a"]["raw"][key], data["b"]["raw"][key]
        label, fmt = NAMES[key]
        _text(d, (cx, y + 8), label, 13, MUTED, "mm")
        biggest = max(a, b) or 1.0
        d.rounded_rectangle((cx - 8 - reach, y + 22, cx - 8, y + 32), radius=5, fill=TRACK)
        d.rounded_rectangle((cx + 8, y + 22, cx + 8 + reach, y + 32), radius=5, fill=TRACK)
        if a > 0:
            d.rounded_rectangle((cx - 8 - max(10, int(reach * a / biggest)), y + 22, cx - 8, y + 32), radius=5, fill=col_a)
        if b > 0:
            d.rounded_rectangle((cx + 8, y + 22, cx + 8 + max(10, int(reach * b / biggest)), y + 32), radius=5, fill=col_b)
        close = abs(a - b) <= 0.1 * biggest
        _text(d, (x1 + 30, y + 27), fmt.format(a), 22, WHITE if close else _mix(col_a, WHITE, 0.45), "lm", "head")
        _text(d, (x2 - 30, y + 27), fmt.format(b), 22, WHITE if close else _mix(col_b, WHITE, 0.45), "rm", "head")
    sy = top + len(SHOWN) * row_h + 10
    d.line((x1 + 30, sy, x2 - 30, sy), fill=_mix(GOLD_ACCENT, _INK, 0.7), width=1)
    _text(d, (x1 + 30, sy + 24), "SHARED TRAITS", 13, MUTED, "lm")
    shared = data.get("shared") or []
    if shared:
        x = x1 + 170
        for name in shared[:4]:
            x += _pill(d, x, sy + 8, name, 14, fill=_mix(GOLD_ACCENT, _INK, 0.75), fg=WHITE,
                       outline=GOLD_ACCENT, pad=15, h=32) + 10
    else:
        _text(d, (x1 + 170, sy + 24), "SIMILAR OVERALL OUTPUT, NO STANDOUT TRAIT IN COMMON", 14, DIM, "lm")


def _mini(c, box, title, color, entry):
    """Closest teammate / closest rival card."""
    d = c.d
    x1, y1, x2, y2 = box
    _tile_glass(c, box, color)
    d.ellipse((x1 + 28, y1 + 28, x1 + 40, y1 + 40), fill=color)
    _text(d, (x1 + 54, y1 + 20), title, 22, color, "la", "head")
    if entry is None:
        _text(d, ((x1 + x2) // 2, (y1 + y2) // 2 + 20), "NOBODY WITH ENOUGH MATCHES", 18, DIM, "mm", "head")
        return
    ring = _round_image(_open(entry.get("avatar")), 96, ring=color, ring_w=4, initial=entry["name"][:1])
    _halo(c, x1 + 30 + ring.size[0] / 2, y1 + 66 + ring.size[1] / 2, ring.size[0] / 2, color)
    _paste(c.image, ring, (x1 + 30, y1 + 66))
    tx = x1 + 30 + ring.size[0] + 22
    room = x2 - 30 - tx - 120
    name = entry["name"].upper()
    size = _fit(d, name, room, 30, 18, "head")
    _title(d, (tx, y1 + 72), _clip(d, name, room, size, "head"), size, WHITE, "la", "head")
    team = _clip(d, (entry.get("team") or "FREE AGENT").upper(), room, 14)
    _pill(d, tx, y1 + 116, team, 14, fill=_mix(color, _INK, 0.7), fg=WHITE, outline=color, pad=14, h=30)
    _glow_text(c, (x2 - 30, y1 + 62), f"{entry['pct']}%", 52, color, "ra")
    bar_y = y2 - 22
    d.rounded_rectangle((x1 + 30, bar_y, x2 - 30, bar_y + 10), radius=5, fill=TRACK)
    d.rounded_rectangle((x1 + 30, bar_y, x1 + 30 + max(10, int((x2 - x1 - 60) * entry["pct"] / 100)), bar_y + 10),
                        radius=5, fill=color)
    if entry.get("best"):
        _text(d, (x2 - 30, y1 + 28), "★ BEST OVERALL MATCH", 12, WHITE, "rm")


def _render(data):
    col_a, col_b = pick_sides(data["a"].get("color"), data["b"].get("color"))
    c = _Canvas(col_a, col_b)
    c.card((50, 22, 1550, 100), 24, 170)
    c.card((50, 440, 1000, 856), 26, 150)
    c.card((1020, 440, 1550, 640), 26, 150)
    c.card((1020, 656, 1550, 856), 26, 150)
    d = c.seal()
    _title(d, (76, 50), "SIMILAR PLAYERS", 44, WHITE, "lm", "head")
    for i in range(380):                                   # underline blending the two side colours
        d.line((76 + i, 80, 76 + i, 83), fill=_mix(c.a, c.b, i / 379))
    _text(d, (1524, 48), _clip(d, f"CLOSEST PLAYING STYLE TO {data['a']['name']}".upper(), 560, 16), 16, MUTED, "rm")
    _text(d, (1524, 74), f"COMPARED WITH {data['pool']} PLAYERS  •  MIN {MIN_MATCHES} MATCHES", 13, DIM, "rm")

    _player_block(c, data["a"], c.a, True)
    _player_block(c, data["b"], c.b, False)
    _gauge(c, 800, 250, 96, data["pct"], c.a, c.b)
    _text(d, (800, 250), f"{data['pct']}%", 56, WHITE, "mm", "head")
    _text(d, (800, 378), "STYLE MATCH", 14, MUTED, "mm")
    label = verdict(data["pct"])
    pw = int(d.textlength(label, font=font(16))) + 40
    _pill(d, 800 - pw // 2, 392, label, 16, fill=_mix(c.a, c.b, 0.5), fg=_INK, pad=20, h=34)

    _butterfly(c, (50, 440, 1000, 856), data, c.a, c.b)
    _mini(c, (1020, 440, 1550, 640), "CLOSEST TEAMMATE", TEAMMATE_COLOR, data.get("teammate"))
    _mini(c, (1020, 656, 1550, 856), "CLOSEST RIVAL", RIVAL_COLOR, data.get("rival"))
    _footer(c, f"GOOGLI MANAGER  •  SIMILAR PLAYERS  •  {data['a']['name']}", data.get("guild"))
    return c.image


def render_similar(data):
    """data -> PNG bytes."""
    return _png(_render(data))


# =========================================================================== command

class Similar(commands.Cog):
    """+similar."""

    def __init__(self, bot):
        self.bot = bot
        self._models = {}          # guild id -> (stats index object, model)

    @property
    def compare(self):
        return self.bot.get_cog("CompareTeams")

    def _model(self, guild_id, index):
        hit = self._models.get(int(guild_id))
        if hit and hit[0] is index:           # same index object = same data, nothing to redo
            return hit[1]
        from .lb import _totals
        from .stats import fantasy_raw_points
        model = build_model(index, _totals, lambda rows: fantasy_raw_points(rows)[2])
        self._models[int(guild_id)] = (index, model)
        return model

    async def _avatar(self, guild, uid):
        member = guild.get_member(uid) or self.bot.get_user(uid)
        if member is None:
            try:
                member = await self.bot.fetch_user(uid)
            except discord.HTTPException:
                member = None
        return uid, await fetch_avatar(self.bot, member)

    def _team(self, guild, uid):
        """(team row or None, role colour or None)."""
        core = self.compare.core
        try:
            team = core._membership(guild.id, uid)
        except Exception:
            return None, None
        return (team, team_role_color(core, guild, team)) if team else (None, None)

    @commands.command(name="similar", aliases=["twin", "lookalike"])
    @commands.guild_only()
    async def similar(self, ctx, member: discord.Member = None):
        """Who plays most like you (or @player)? Closest teammate and rival too."""
        compare = self.compare
        if compare is None:
            await ctx.reply("The team system isn't loaded right now.", mention_author=False)
            return
        member = member or ctx.author
        wait = on_cooldown(ctx.author.id)
        if wait:
            await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
            return
        loading = await ctx.reply("⏳ Looking for look-alikes...", mention_author=False)
        try:
            guild = ctx.guild
            model = self._model(guild.id, compare._guild_rows(guild.id))
            me = model["profiles"].get(member.id)
            if me is None or me["matches"] < MIN_SUBJECT:
                await loading.edit(content=f"**{discord.utils.escape_markdown(member.display_name)}** needs at "
                                           f"least {MIN_SUBJECT} recorded matches to be compared.")
                return
            ranked = neighbours(model, member.id)
            if not ranked:
                await loading.edit(content="Not enough players with recorded matches to compare yet.")
                return

            my_team, my_color = self._team(guild, member.id)
            best_pct, best_uid = ranked[0]
            teammate = rival = None
            for pct, uid in ranked[:80]:                 # stop as soon as both are found
                team, _color = self._team(guild, uid)
                same = bool(my_team and team and int(team["id"]) == int(my_team["id"]))
                if same and teammate is None:
                    teammate = (pct, uid, team)
                elif not same and rival is None and team is not None:
                    rival = (pct, uid, team)
                if teammate and rival:
                    break
            best_team, best_color = self._team(guild, best_uid)

            wanted = {member.id, best_uid} | {e[1] for e in (teammate, rival) if e}
            avatars = dict(await asyncio.gather(*(self._avatar(guild, u) for u in wanted)))

            def person(uid, team, color):
                return {
                    "name": compare._display_name(guild, uid), "avatar": avatars.get(uid),
                    "team": str(team["name"]) if team else None, "color": color,
                    "role": xi_engine.rate(dict(model["profiles"][uid]), model["pool"])["role"],
                    "traits": traits(model, uid), "raw": model["raw"][uid],
                }

            a, b = person(member.id, my_team, my_color), person(best_uid, best_team, best_color)
            data = {"a": a, "b": b, "pct": best_pct, "guild": guild.name, "pool": len(model["eligible"]),
                    "shared": [t for t in a["traits"] if t in b["traits"]]}
            for key, entry in (("teammate", teammate), ("rival", rival)):
                if entry:
                    pct, uid, team = entry
                    data[key] = {"name": compare._display_name(guild, uid), "avatar": avatars.get(uid),
                                 "team": str(team["name"]), "pct": pct, "best": uid == best_uid}
            png = await render_png(render_similar, data)
            await loading.edit(content=None, attachments=[discord.File(io.BytesIO(png), filename="similar.png")])
        except Exception as exc:
            print(f"[similar] failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                await loading.edit(content="I couldn't build the comparison. Check the bot logs.")
            except Exception:
                pass


async def setup(bot):
    await bot.add_cog(Similar(bot))
