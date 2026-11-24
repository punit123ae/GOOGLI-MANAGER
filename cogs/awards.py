"""+awards (alias +topof): season awards as two image pages. One file: rules, drawing and command.

Page 1: Orange Cap (most runs), Purple Cap (most wickets), MVP (most fantasy points), Golden Gloves (catches).
Page 2: Rising Star, Most Improved, Form Dip (started strong, struggling lately), Most Consistent.

  Rising Star      best overall rating (the +xi rating) among players with 2-5 matches
  Most Improved    biggest rise in fantasy points per match: last 5 matches vs the ones before
  Form Dip         the opposite, but only for players who were above the league median earlier
  Most Consistent  steadiest fantasy points match to match, among players at or above the median

Form awards need 8 matches (3 earlier + the last 5); Most Consistent needs 6. A tile nobody qualifies
for shows NOT ENOUGH DATA.

The numbers come from the stats index CompareTeams already keeps for +xi / +compareteams (read once, kept
2 minutes), so the command adds no database work. Page 2 is only drawn if somebody opens it.
"""
import asyncio
import io
import math
from statistics import mean, median, pstdev

import discord
from discord.ext import commands
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import xi_engine
from .h2h import ImagePager, fetch_avatar, on_cooldown, render_png
from .teaminfo_image import (
    DEFAULT_ACCENT, DIM, MUTED, WHITE, _clip, _fit, _mix, _open, _paste, _pill, _round_image, _text, font,
)
from .versus_image import SIDE_B_FALLBACK, _Canvas, _footer, _png, _title

# (award key, title, subtitle, colour, icon) - four per page
PAGES = [
    [
        ("orange", "ORANGE CAP", "MOST RUNS", (255, 150, 70), "cap"),
        ("purple", "PURPLE CAP", "MOST WICKETS", (165, 115, 255), "cap"),
        ("mvp", "MVP", "MOST FANTASY POINTS", (62, 224, 172), "star"),
        ("gloves", "GOLDEN GLOVES", "MOST CATCHES", (255, 206, 90), "glove"),
    ],
    [
        ("rising", "RISING STAR", "BEST RATED PLAYER WITH 5 MATCHES OR FEWER", (100, 185, 255), "rise"),
        ("improved", "MOST IMPROVED", "BIGGEST JUMP IN RECENT FORM", (255, 110, 150), "up"),
        ("dip", "FORM DIP", "STARTED STRONG, STRUGGLING IN RECENT MATCHES", (240, 95, 95), "down"),
        ("consistent", "MOST CONSISTENT", "STEADIEST FANTASY POINTS, MIN 6 MATCHES", (70, 215, 240), "bars"),
    ],
]


# =========================================================================== rules (no Discord, no images)

RECENT = xi_engine.RECENT                 # the last 5 matches count as "recent"
MIN_FORM_MATCHES = RECENT + 3
MIN_CONSISTENT = 6
RISING_MIN, RISING_MAX = 2, 5


def _matches(n):
    return f"{n} MATCH" + ("" if n == 1 else "ES")


def _best(items, key):
    return max(items, key=key) if items else None


def compute_awards(index, totals_fn, fantasy_fn):
    """index is CompareTeams._guild_rows(); returns {award key: {"uid", "value", "sub"} or None}."""
    profiles, series = [], {}
    for uid, rows in index["players"].items():
        caught, dropped = index["fielding_players"].get(uid, (0, 0))
        profiles.append(xi_engine.profile(uid, rows, caught, dropped, totals_fn, fantasy_fn))
        if len(rows) >= MIN_CONSISTENT:     # fantasy points of every match, oldest first (match ids grow with time)
            series[uid] = [float(fantasy_fn([r])) for r in sorted(rows, key=lambda r: r["mid"])]
    pool = xi_engine.Pool(profiles)
    out = {}

    p = _best([x for x in profiles if x["bat_inn"] and x["runs"] > 0],
              lambda x: (x["runs"], x["bat_avg"] or 0, -x["uid"]))
    if p:
        bits = []
        if p["bat_avg"] is not None:
            bits.append(f"AVG {p['bat_avg']:.1f}")
        if p["strike_rate"] is not None:
            bits.append(f"SR {p['strike_rate']:.1f}")
        bits.append(_matches(p["matches"]))
        out["orange"] = {"uid": p["uid"], "value": f"{p['runs']:,}", "sub": " • ".join(bits)}

    p = _best([x for x in profiles if x["wickets"] > 0],
              lambda x: (x["wickets"], -(x["economy"] if x["economy"] is not None else 99), -x["uid"]))
    if p:
        bits = []
        if p["economy"] is not None:
            bits.append(f"ECON {p['economy']:.1f}")
        if p["bowl_avg"] is not None:
            bits.append(f"AVG {p['bowl_avg']:.1f}")
        bits.append(_matches(p["matches"]))
        out["purple"] = {"uid": p["uid"], "value": f"{p['wickets']:,}", "sub": " • ".join(bits)}

    p = _best([x for x in profiles if x["matches"] > 0], lambda x: (x["fantasy"], x["matches"], -x["uid"]))
    if p:
        role = xi_engine.rate(dict(p), pool)["role"]
        out["mvp"] = {"uid": p["uid"], "value": f"{p['fantasy']:,.0f}",
                      "sub": f"{role.upper()} • {_matches(p['matches'])}"}

    p = _best([x for x in profiles if x["catches"] > 0], lambda x: (x["catches"], x["catch_eff"] or 0, -x["uid"]))
    if p:
        bits = []
        if p["catch_eff"] is not None:
            bits.append(f"CATCH RATE {p['catch_eff']:.0f}%")
        bits.append(_matches(p["matches"]))
        out["gloves"] = {"uid": p["uid"], "value": f"{p['catches']:,}", "sub": " • ".join(bits)}

    rated = [xi_engine.rate(dict(x), pool) for x in profiles if RISING_MIN <= x["matches"] <= RISING_MAX]
    p = _best(rated, lambda x: (x["value"], x["matches"], -x["uid"]))
    out["rising"] = ({"uid": p["uid"], "value": f"{p['value']:.0f}",
                      "sub": f"OVERALL RATING • {_matches(p['matches'])}"} if p else None)

    league = median(mean(s) for s in series.values()) if series else 0.0
    form = {}
    for uid, s in series.items():
        if len(s) >= MIN_FORM_MATCHES:
            early, recent = mean(s[:-RECENT]), mean(s[-RECENT:])
            form[uid] = (early, recent, recent - early, len(s))

    def form_pick(uid):
        early, recent, delta, n = form[uid]
        return {"uid": uid, "value": f"{delta:+.1f}", "sub": f"{early:.0f} → {recent:.0f} PTS/MATCH • {_matches(n)}"}

    up = _best([u for u, f in form.items() if f[2] > 0], lambda u: (form[u][2], -u))
    out["improved"] = form_pick(up) if up is not None else None
    down = _best([u for u, f in form.items() if f[2] < 0 and f[0] >= league], lambda u: (-form[u][2], -u))
    out["dip"] = form_pick(down) if down is not None else None

    steady = [(pstdev(s) / mean(s), uid, mean(s), len(s)) for uid, s in series.items() if mean(s) > 0 and mean(s) >= league]
    if steady:
        cv, uid, avg, n = min(steady)
        out["consistent"] = {"uid": uid, "value": f"{100 / (1 + cv):.0f}",
                             "sub": f"AVG {avg:.0f} PTS/MATCH • {_matches(n)}"}
    else:
        out["consistent"] = None
    return out


# =========================================================================== drawing

BOXES = [(50, 126, 790, 484), (810, 126, 1550, 484), (50, 498, 790, 856), (810, 498, 1550, 856)]
_INK = (12, 18, 32)


def _rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def _blur_ellipse(size, box, color, alpha, blur):
    """A soft coloured glow, drawn at half size (blurs are the expensive part) and scaled up."""
    w, h = size
    small = Image.new("RGBA", (w // 2, h // 2), (0, 0, 0, 0))
    ImageDraw.Draw(small).ellipse([v / 2 for v in box], fill=color + (alpha,))
    small = small.filter(ImageFilter.GaussianBlur(blur / 2))
    return small.resize((w, h), Image.Resampling.BILINEAR)


def _tile_glass(c, box, color):
    """Tinted glow in the award colour, a coloured edge, a glossy top line and an accent tab."""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    mask = _rounded_mask((w, h), 26)
    glow = _blur_ellipse((w, h), (-w * 0.25, -h * 0.65, w * 0.65, h * 0.55), color, 110, 70)
    glow2 = _blur_ellipse((w, h), (w * 0.55, h * 0.55, w * 1.3, h * 1.4), color, 55, 80)
    glow = Image.alpha_composite(glow, glow2)
    glow.putalpha(ImageChops.multiply(glow.getchannel("A"), mask))
    c.image.paste(glow, (x1, y1), glow)
    d = c.d
    d.rounded_rectangle(box, radius=26, outline=_mix(color, _INK, 0.35), width=2)
    d.rounded_rectangle((x1 + 3, y1 + 3, x2 - 3, y2 - 3), radius=23, outline=_mix(color, _INK, 0.8), width=1)
    d.line((x1 + 30, y1 + 2, x2 - 30, y1 + 2), fill=_mix(color, WHITE, 0.55), width=1)      # gloss
    d.rounded_rectangle((x1 + 28, y1 - 1, x1 + 150, y1 + 4), radius=2, fill=color)           # accent tab


def _star(cx, cy, outer, inner, points=5):
    pts = []
    for i in range(points * 2):
        r = outer if i % 2 == 0 else inner
        a = -math.pi / 2 + i * math.pi / points
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _icon(c, kind, cx, cy, color):
    """A small vector icon in a round badge (drawn 4x and scaled down so the edges are smooth)."""
    s, box = 4, 64
    layer = Image.new("RGBA", (box * s, box * s), (0, 0, 0, 0))
    g = ImageDraw.Draw(layer)
    m = box * s // 2

    def p(x, y):
        return (m + x * s, m + y * s)

    g.ellipse((p(-30, -30), p(30, 30)), fill=_mix(color, _INK, 0.78) + (235,), outline=color + (255,), width=3 * s)
    if kind == "cap":
        g.pieslice((p(-15, -13), p(15, 17)), 180, 360, fill=color)
        g.rounded_rectangle((p(-15, 2), p(22, 7)), radius=2 * s, fill=color)
        g.ellipse((p(-2.5, -15), p(2.5, -10)), fill=_mix(color, WHITE, 0.5))
    elif kind == "star":
        g.polygon([p(x, y) for x, y in _star(0, 1, 17, 7.5)], fill=color)
    elif kind == "rise":
        g.polygon([p(x, y) for x, y in _star(0, 1, 17, 7.5)], outline=color, width=3 * s)
        g.polygon([p(x, y) for x, y in _star(0, 1, 8, 3.5)], fill=color)
    elif kind == "glove":
        g.rounded_rectangle((p(-12, -1), p(12, 17)), radius=4 * s, fill=color)
        for i, top in enumerate((-14, -17, -16, -12)):
            x = -12 + i * 6.5
            g.rounded_rectangle((p(x, top), p(x + 5, 3)), radius=2 * s, fill=color)
        g.rounded_rectangle((p(-17, 2), p(-10, 10)), radius=3 * s, fill=color)
    elif kind in ("up", "down"):
        sign = -1 if kind == "up" else 1
        g.polygon([p(0, sign * 16), p(-15, -sign * 3), p(-6, -sign * 3), p(-6, -sign * 16),
                   p(6, -sign * 16), p(6, -sign * 3), p(15, -sign * 3)], fill=color)
    elif kind == "bars":
        for i in range(3):
            x = -14 + i * 10
            g.rounded_rectangle((p(x, -13 + abs(i - 1) * 3), p(x + 7, 14)), radius=2 * s, fill=color)
    layer = layer.resize((box, box), Image.Resampling.LANCZOS)
    c.image.paste(layer, (int(cx - box / 2), int(cy - box / 2)), layer)


def _halo(c, cx, cy, radius, color):
    """Soft glow behind an avatar."""
    size = int(radius * 3.4)
    glow = _blur_ellipse((size, size), (size * 0.2, size * 0.2, size * 0.8, size * 0.8), color, 140, 36)
    c.image.paste(glow, (int(cx - size / 2), int(cy - size / 2)), glow)


def _glow_text(c, xy, value, size, color):
    """Big number: coloured glow, dark shadow, white text."""
    pad = 24
    f = font(size, "head")
    width = int(c.d.textlength(value, font=f)) + pad * 2
    layer = Image.new("RGBA", (width, size + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((pad, pad), value, font=f, fill=color + (230,))
    layer = layer.filter(ImageFilter.GaussianBlur(9))
    c.image.paste(layer, (int(xy[0] - pad), int(xy[1] - pad)), layer)
    _title(c.d, xy, value, size, WHITE, "la", "head")


def _tile(c, box, tile):
    d = c.d
    x1, y1, x2, y2 = box
    color = tuple(tile["color"])
    _tile_glass(c, box, color)
    d.ellipse((x1 + 28, y1 + 28, x1 + 40, y1 + 40), fill=color)
    _text(d, (x1 + 54, y1 + 20), tile["title"], 26, color, "la", "head")
    _text(d, (x1 + 28, y1 + 62), _clip(d, tile["subtitle"], x2 - x1 - 140, 14), 14, MUTED)
    _icon(c, tile.get("icon", "star"), x2 - 52, y1 + 50, color)
    winner = tile.get("winner")
    if not winner:
        _text(d, ((x1 + x2) // 2, (y1 + y2) // 2 + 24), "NOT ENOUGH DATA", 26, DIM, "mm", "head")
        return
    ring = _round_image(_open(winner.get("avatar")), 170, ring=color, ring_w=5, initial=winner["name"][:1])
    ax, ay = x1 + 30, y1 + 96
    _halo(c, ax + ring.size[0] / 2, ay + ring.size[1] / 2, ring.size[0] / 2, color)
    _paste(c.image, ring, (ax, ay))
    tx = ax + ring.size[0] + 30
    room = x2 - 28 - tx
    name = winner["name"].upper()
    size = _fit(d, name, room, 46, 22, "head")
    _title(d, (tx, y1 + 108), _clip(d, name, room, size, "head"), size, WHITE, "la", "head")
    team = _clip(d, (winner.get("team") or "FREE AGENT").upper(), room - 36, 16)
    _pill(d, tx, y1 + 168, team, 16, fill=_mix(color, _INK, 0.7), fg=WHITE, outline=color, pad=18, h=36)
    value = str(winner["value"])
    _glow_text(c, (tx, y1 + 206), value, _fit(d, value, room, 80, 40, "head"), color)
    d.line((x1 + 30, y1 + 312, x2 - 30, y1 + 312), fill=_mix(color, _INK, 0.65), width=1)
    _text(d, (x1 + 30, y1 + 322), _clip(d, winner["sub"], x2 - x1 - 60, 15), 15, _mix(MUTED, WHITE, 0.25))


def _page(data, index):
    c = _Canvas(DEFAULT_ACCENT, SIDE_B_FALLBACK)          # blue on the left, orange on the right
    tiles = data["pages"][index]
    c.card((50, 22, 1550, 106), 24, 170)
    for box in BOXES[:len(tiles)]:
        c.card(box, 26, 150)
    d = c.seal()
    _title(d, (76, 58), "SEASON AWARDS", 50, WHITE, "lm", "head")
    for i in range(380):                                   # blue -> orange underline
        d.line((76 + i, 90, 76 + i, 93), fill=_mix(DEFAULT_ACCENT, SIDE_B_FALLBACK, i / 379))
    _text(d, (1524, 52), f"{data.get('guild', '')}  •  MAIN TOURNAMENT".upper(), 16, MUTED, "rm")
    for n in range(len(data["pages"])):                    # page dots
        cx = 1524 - (len(data["pages"]) - 1 - n) * 22
        d.ellipse((cx - 6, 78, cx + 6, 90), fill=(255, 255, 255) if n == index else (70, 86, 120))
    for box, tile in zip(BOXES, tiles):
        _tile(c, box, tile)
    _footer(c, f"GOOGLI MANAGER  •  SEASON AWARDS  •  PAGE {index + 1} OF {len(data['pages'])}", data.get("guild"))
    return c.image


def render_awards_page(data, index):
    """Draw ONE page (0 or 1) as PNG bytes."""
    return _png(_page(data, index))


# =========================================================================== command

class Awards(commands.Cog):
    """+awards / +topof."""

    def __init__(self, bot):
        self.bot = bot
        self._picks = {}          # guild id -> (stats index object, picks)

    @property
    def compare(self):
        return self.bot.get_cog("CompareTeams")

    def _compute(self, guild_id, index):
        hit = self._picks.get(int(guild_id))
        if hit and hit[0] is index:           # same index object = same data, nothing to redo
            return hit[1]
        from .lb import _totals
        from .stats import fantasy_raw_points
        picks = compute_awards(index, _totals, lambda rows: fantasy_raw_points(rows)[2])
        self._picks[int(guild_id)] = (index, picks)
        return picks

    async def _avatar(self, guild, uid):
        member = guild.get_member(uid) or self.bot.get_user(uid)
        if member is None:
            try:
                member = await self.bot.fetch_user(uid)
            except discord.HTTPException:
                member = None
        return await fetch_avatar(self.bot, member)

    async def _data(self, ctx):
        compare = self.compare
        core, guild = compare.core, ctx.guild
        picks = self._compute(guild.id, compare._guild_rows(guild.id))
        sem = asyncio.Semaphore(6)

        async def avatar(uid):
            async with sem:
                return uid, await self._avatar(guild, uid)

        avatars = dict(await asyncio.gather(*(avatar(uid) for uid in {p["uid"] for p in picks.values() if p})))
        pages = []
        for page in PAGES:
            tiles = []
            for key, title, subtitle, color, icon in page:
                pick = picks.get(key)
                winner = None
                if pick:
                    try:
                        team = core._membership(guild.id, pick["uid"])
                    except Exception:
                        team = None
                    winner = {
                        "name": compare._display_name(guild, pick["uid"]), "avatar": avatars.get(pick["uid"]),
                        "team": str(team["name"]) if team else None, "value": pick["value"], "sub": pick["sub"],
                    }
                tiles.append({"title": title, "subtitle": subtitle, "color": color, "icon": icon, "winner": winner})
            pages.append(tiles)
        return {"guild": guild.name, "pages": pages}

    @commands.command(name="awards", aliases=["topof"])
    @commands.guild_only()
    async def awards(self, ctx):
        """Season awards: Orange Cap, Purple Cap, MVP, Golden Gloves, Rising Star, Most Improved..."""
        if self.compare is None:
            await ctx.reply("The team system isn't loaded right now.", mention_author=False)
            return
        wait = on_cooldown(ctx.author.id)
        if wait:
            await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
            return
        loading = await ctx.reply("⏳ Working out the season awards...", mention_author=False)
        try:
            data = await self._data(ctx)
            png = await render_png(render_awards_page, data, 0)
            view = ImagePager(ctx, png, 2, lambda index: render_awards_page(data, index),
                              ("Caps & MVP", "Rising & Form"), "awards.png")
            view.message = await loading.edit(
                content=None, attachments=[discord.File(io.BytesIO(png), filename="awards.png")], view=view)
        except Exception as exc:
            print(f"[awards] failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                await loading.delete()
            except Exception:
                pass
            await ctx.send("I couldn't build the awards. Check the bot logs.")


async def setup(bot):
    await bot.add_cog(Awards(bot))
