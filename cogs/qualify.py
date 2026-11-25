"""+qualify [team]: qualification chances for the team's group, with the "if we win this match" scenarios.

Page 1  one team: qualification chance, what happens for every number of wins left, and the remaining matches
        that swing it most (chance if the team wins that match / loses it).
Page 2  the whole group: chance of every team.

Inputs (all already in the bot): the group table and results of the active stage (points table), the saved
fixture list of the group (+generateschedule), and the number of qualifying places per group
(+qualifyspots <n>, default 2). Without a saved fixture list every pair plays "legs per pair" matches (+setformat).

Chances come from simulating the rest of the group 4000 times. A match is won with the "log5" chance of the two
teams' win rates so far (a smoothed record, so teams with few matches stay close to 50%).

Tie-breakers follow the tournament rule: when teams are level on points at the qualification cut and there are
fewer places than teams (for example 14 points, then four teams on 10 for the last 3 of 4 places), the level teams
play a round-robin among themselves, each team once against each other. Most wins goes through; if teams are still
level at the cut they play another round.
"""
import asyncio
import io
import math
import random
import sqlite3
import sys
import types
from itertools import combinations

import discord
from discord.ext import commands

from .awards import _glow_text, _halo, _icon, _tile_glass
from .h2h import ImagePager, on_cooldown, render_png, team_role_color
from .teaminfo_image import (
    DEFAULT_ACCENT, DIM, MUTED, WHITE, _clip, _fit, _mix, _open, _paste, _pill, _round_image, _text, font,
)
from .utility import owner_or_whitelisted
from .versus_image import SIDE_B_FALLBACK, _Canvas, _footer, _png, _title

GREEN, RED, AMBER, BLUE = (62, 224, 172), (240, 95, 95), (255, 206, 90), (100, 185, 255)
_INK = (12, 18, 32)
DEFAULT_SPOTS = 2
SIMS, SCEN_SIMS = 4000, 1000


# =========================================================================== rules (no Discord, no images)

def win_chance(a, b, prior=1.0):
    """Chance that team a beats team b. a, b = (wins, losses). log5 on smoothed win rates."""
    ra = (a[0] + prior) / (a[0] + a[1] + 2 * prior)
    rb = (b[0] + prior) / (b[0] + b[1] + 2 * prior)
    den = ra * (1 - rb) + rb * (1 - ra)
    return 0.5 if den <= 0 else ra * (1 - rb) / den


def _playoff(tied, need, prob, rng):
    """Tie-breaker: the level teams play each other once (round-robin) for the `need` places left.

    Most wins in that mini-league go through; if teams are still level at the cut they play again."""
    through, tied = set(), list(tied)
    for _ in range(8):
        wins = dict.fromkeys(tied, 0)
        for a, b in combinations(tied, 2):
            wins[a if rng.random() < prob(a, b) else b] += 1
        order = sorted(tied, key=lambda t: (-wins[t], rng.random()))
        cut = wins[order[need - 1]]
        through |= {t for t in tied if wins[t] > cut}
        level = [t for t in tied if wins[t] == cut]
        need -= sum(1 for t in tied if wins[t] > cut)
        if len(level) <= need:
            return through | set(level)
        tied = level
    rng.shuffle(tied)                                      # eight dead heats in a row: draw lots
    return through | set(tied[:need])


def _run(points, fixtures, spots, rng, prob, forced=None):
    """One simulated finish. Returns (teams that qualify, the teams level at the cut or None)."""
    pts = dict(points)
    for i, (a, b, p) in enumerate(fixtures):
        if forced is not None and i in forced:
            win_a = forced[i] == a
        else:
            win_a = rng.random() < p
        pts[a if win_a else b] += 2
    if spots >= len(pts):
        return set(pts), None
    cut = sorted(pts.values(), reverse=True)[spots - 1]
    safe = {t for t in pts if pts[t] > cut}
    level = [t for t in pts if pts[t] == cut]
    room = spots - len(safe)
    if len(level) <= room:
        return safe | set(level), None
    return safe | _playoff(level, room, prob, rng), set(level)


def chances(points, fixtures, spots, rng, prob, sims=SIMS):
    """{team: chance of qualifying}, {team: chance of being level at the cut}, {team: chance of winning the tie-breaker}."""
    count = dict.fromkeys(points, 0)
    tied = dict.fromkeys(points, 0)
    won = dict.fromkeys(points, 0)
    for _ in range(sims):
        through, level = _run(points, fixtures, spots, rng, prob)
        for t in through:
            count[t] += 1
        for t in level or ():
            tied[t] += 1
            won[t] += t in through
    return ({t: count[t] / sims for t in points}, {t: tied[t] / sims for t in points},
            {t: (won[t] / tied[t] if tied[t] else None) for t in points})


def team_chance(team, points, fixtures, spots, rng, prob, sims, own_wins=None, forced=None):
    """P(team qualifies) when exactly own_wins of its remaining matches are won (random ones) or forced results."""
    own = [i for i, f in enumerate(fixtures) if team in (f[0], f[1])]
    hit = 0
    for _ in range(sims):
        f = dict(forced or {})
        if own_wins is not None:
            winners = set(rng.sample(own, own_wins))
            for i in own:
                f[i] = team if i in winners else (fixtures[i][1] if fixtures[i][0] == team else fixtures[i][0])
        if team in _run(points, fixtures, spots, rng, prob, f)[0]:
            hit += 1
    return hit / sims


def analyse(team, points, fixtures, spots, seed, prob):
    """Everything +qualify shows for one team."""
    rng = random.Random(seed)
    base, tied, tie_win = chances(points, fixtures, spots, rng, prob)
    own = [i for i, f in enumerate(fixtures) if team in (f[0], f[1])]
    k = len(own)
    scen = [(w, team_chance(team, points, fixtures, spots, rng, prob, SCEN_SIMS, own_wins=w)) for w in range(k, -1, -1)]
    swings = []
    for i in own:
        a, b, p = fixtures[i]
        opp = b if a == team else a
        pw = p if a == team else 1 - p
        win = team_chance(team, points, fixtures, spots, rng, prob, SCEN_SIMS, forced={i: team})
        lose = team_chance(team, points, fixtures, spots, rng, prob, SCEN_SIMS, forced={i: opp})
        swings.append({"opp": opp, "p": pw, "win": win, "lose": lose, "idx": i})
    need = next((w for w, c in sorted(scen, key=lambda s: s[0]) if c >= 0.5), None)
    return {"base": base, "tied": tied, "tie_win": tie_win, "scen": scen, "swings": swings, "need": need, "left": k}


def status(chance):
    if chance >= 0.995:
        return "QUALIFIED", GREEN
    if chance <= 0.005:
        return "ELIMINATED", RED
    if chance >= 0.7:
        return "STRONG POSITION", GREEN
    if chance >= 0.4:
        return "IN THE MIX", AMBER
    if chance >= 0.1:
        return "LONG SHOT", (255, 150, 70)
    return "NEEDS A MIRACLE", RED


def pct_text(v):
    if v >= 0.995:
        return "100%"
    if v <= 0.005:
        return "0%"
    return f"{v * 100:.0f}%" if 0.01 <= v <= 0.99 else (f"{v * 100:.1f}%")


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


def _header(c, title, data, page, pages):
    d = c.d
    _title(d, (76, 58), title, 50, WHITE, "lm", "head")
    for i in range(380):
        d.line((76 + i, 90, 76 + i, 93), fill=_mix(DEFAULT_ACCENT, SIDE_B_FALLBACK, i / 379))
    _text(d, (1524, 52), f"{data.get('guild', '')}  •  {data.get('group', '')}".upper(), 16, MUTED, "rm")
    for n in range(pages):
        cx = 1524 - (pages - 1 - n) * 22
        d.ellipse((cx - 6, 78, cx + 6, 90), fill=(255, 255, 255) if n == page else (70, 86, 120))


def _tile_title(c, box, color, title, subtitle, icon):
    x1, y1, x2, y2 = box
    _tile_glass(c, box, color)
    c.d.ellipse((x1 + 28, y1 + 28, x1 + 40, y1 + 40), fill=color)
    _text(c.d, (x1 + 54, y1 + 20), title, 26, color, "la", "head")
    _text(c.d, (x1 + 28, y1 + 62), _clip(c.d, subtitle, x2 - x1 - 140, 14), 14, MUTED)
    _icon(c, icon, x2 - 52, y1 + 50, color)


def _page_team(data):
    t = data["team"]
    c = _Canvas(t.get("color") or DEFAULT_ACCENT, SIDE_B_FALLBACK)
    boxes = [(50, 126, 790, 484), (810, 126, 1550, 484), (50, 498, 1550, 856)]
    c.card((50, 22, 1550, 106), 24, 170)
    for b in boxes:
        c.card(b, 26, 150)
    d = c.seal()
    _header(c, "QUALIFICATION", data, 0, 2)
    state, color = t["status"]

    # ---- chance
    x1, y1, x2, y2 = boxes[0]
    _tile_title(c, boxes[0], color, "QUALIFICATION CHANCE",
                f"TOP {data['spots']} OF {data['group'].upper()} QUALIFY  •  LEVEL AT THE CUT = TIE-BREAKER", "star")
    gx, gy, r = x1 + 190, y1 + 232, 100
    _halo(c, gx, gy, r, color)
    _gauge(c, gx, gy, r, t["chance"], color)
    value = pct_text(t["chance"])
    _glow_text(c, (gx - d.textlength(value, font=font(52, "head")) / 2, gy - 34), value, 52, color)
    tx = x1 + 340
    logo = _round_image(_open(t.get("logo")), 56, ring=color, ring_w=3, initial=t["name"][:1])
    _paste(c.image, logo, (tx, y1 + 112))
    name = t["name"].upper()
    size = _fit(d, name, x2 - tx - 100, 28, 16, "head")
    _title(d, (tx + 76, y1 + 124), _clip(d, name, x2 - tx - 100, size, "head"), size, WHITE, "la", "head")
    _pill(d, tx, y1 + 190, state, 17, fill=_mix(color, _INK, 0.7), fg=WHITE, outline=color, pad=18, h=38, kind="head")
    lines = [f"RANK #{t['rank']} OF {t['of']}", f"{t['w']}W – {t['l']}L  •  {t['pts']} PTS",
             f"{t['left']} MATCH{'ES' if t['left'] != 1 else ''} LEFT"]
    for i, line in enumerate(lines):
        _text(d, (tx, y1 + 244 + i * 28), line, 18, WHITE if i == 0 else MUTED, "la", "bold")
    if t.get("tied", 0) >= 0.005:
        win = t.get("tie_win")
        _text(d, (tx, y1 + 244 + 3 * 28), f"IN A TIE-BREAKER {pct_text(t['tied'])}"
              + (f"  •  WINS IT {pct_text(win)}" if win is not None else ""), 15, AMBER, "la", "bold")

    # ---- scenarios
    x1, y1, x2, y2 = boxes[1]
    need = t["need"]
    sub = ("NOTHING LEFT TO PLAY" if not t["left"] else
           "ALREADY SAFE ON ANY RESULT" if t["scen"] and t["scen"][-1][1] >= 0.995 else
           f"NEEDS {need} OF {t['left']} WINS FOR A BETTER-THAN-EVEN CHANCE" if need else
           "BELOW 50% EVEN IF EVERY MATCH IS WON")
    _tile_title(c, boxes[1], BLUE, "IF THEY WIN…", sub, "bars")
    rows = t["scen"]
    if len(rows) > 5:
        rows = rows[:4] + [rows[-1]]
    for i, (w, ch) in enumerate(rows):
        y = y1 + 112 + i * 49
        k = t["left"]
        label = "NO MATCHES LEFT" if k == 0 else (f"ALL {k}" if w == k and k > 1 else
                                                  "LOSE ALL" if w == 0 else f"{w} OF {k}")
        _text(d, (x1 + 30, y + 13), label, 17, WHITE, "lm", "bold")
        col = status(ch)[1]
        d.rounded_rectangle((x1 + 230, y + 3, x2 - 150, y + 23), radius=10, fill=_mix(col, _INK, 0.85))
        wbar = int((x2 - 150 - x1 - 230) * ch)
        if wbar > 0:
            d.rounded_rectangle((x1 + 230, y + 3, x1 + 230 + max(20, wbar), y + 23), radius=10, fill=col)
        _text(d, (x2 - 30, y + 13), pct_text(ch), 24, col, "rm", "head")

    # ---- matches that matter
    x1, y1, x2, y2 = boxes[2]
    _tile_title(c, boxes[2], AMBER, "MATCHES THAT MATTER",
                "CHANCE OF QUALIFYING IF THEY WIN OR LOSE EACH REMAINING MATCH  •  BIGGEST SWING FIRST", "up")
    swings = sorted(t["swings"], key=lambda s: -(s["win"] - s["lose"]))[:6]
    if not swings:
        _text(d, ((x1 + x2) // 2, y1 + 220), "NO REMAINING MATCHES", 24, DIM, "mm", "head")
    cw = 240
    start = x1 + (x2 - x1 - (len(swings) * cw + (len(swings) - 1) * 15)) // 2
    for i, s in enumerate(swings):
        cx1 = start + i * (cw + 15)
        cy1, cy2 = y1 + 100, y2 - 24
        d.rounded_rectangle((cx1, cy1, cx1 + cw, cy2), radius=18, fill=_mix(AMBER, _INK, 0.9),
                            outline=_mix(AMBER, _INK, 0.55), width=1)
        mid = cx1 + cw // 2
        _text(d, (mid, cy1 + 18), "VS", 13, MUTED, "mm", "bold")
        nm = s["opp"].upper()
        size = _fit(d, nm, cw - 24, 20, 12, "head")
        _text(d, (mid, cy1 + 42), _clip(d, nm, cw - 24, size, "head"), size, WHITE, "mm", "head")
        _text(d, (mid, cy1 + 68), f"WIN CHANCE {s['p'] * 100:.0f}%", 12, MUTED, "mm", "bold")
        for j, (lab, v, col) in enumerate((("IF WIN", s["win"], GREEN), ("IF LOSE", s["lose"], RED))):
            yy = cy1 + 100 + j * 60
            _text(d, (mid, yy), lab, 12, col, "mm", "bold")
            _text(d, (mid, yy + 24), pct_text(v), 34, WHITE, "mm", "head")
        swing = (s["win"] - s["lose"]) * 100
        _text(d, (mid, cy2 - 14), f"SWING {swing:.0f}%", 12, AMBER, "mm", "bold")
    _footer(c, f"GOOGLI MANAGER  •  QUALIFY  •  {t['name']}"[:90], data.get("guild"))
    return c.image


def _page_group(data):
    table = data["table"]
    c = _Canvas(DEFAULT_ACCENT, SIDE_B_FALLBACK)
    box = (50, 126, 1550, 856)
    c.card((50, 22, 1550, 106), 24, 170)
    c.card(box, 26, 150)
    d = c.seal()
    pages = 2 if data.get("team") else 1
    _header(c, "QUALIFICATION", data, pages - 1, pages)
    x1, y1, x2, y2 = box
    _tile_title(c, box, BLUE, f"{data['group'].upper()} — WHO QUALIFIES",
                f"TOP {data['spots']} QUALIFY  •  LEVEL AT THE CUT = ROUND-ROBIN TIE-BREAKER  •  4,000 SIMULATIONS", "bars")
    for label, x in (("#", x1 + 34), ("TEAM", x1 + 90), ("W-L", x1 + 520), ("PTS", x1 + 620), ("LEFT", x1 + 720),
                     ("CHANCE", x1 + 840), ("TIE-BREAK", x1 + 1190)):
        _text(d, (x, y1 + 104), label, 13, MUTED)
    n = max(1, len(table))
    step = min(78, (y2 - y1 - 160) // n)
    for i, row in enumerate(table):
        y = y1 + 150 + i * step
        state, col = status(row["chance"])
        if i:
            d.line((x1 + 28, y - step // 2, x2 - 28, y - step // 2), fill=_mix(BLUE, _INK, 0.85), width=1)
        mine = row["name"] == data.get("highlight")
        _text(d, (x1 + 34, y), str(row["rank"]), 22, col if i < data["spots"] else MUTED, "lm", "head")
        logo = _round_image(_open(row.get("logo")), 34, ring=row["color"], ring_w=2, initial=row["name"][:1])
        _paste(c.image, logo, (x1 + 90, y - logo.size[1] // 2))
        _text(d, (x1 + 138, y), _clip(d, row["name"].upper(), 360, 19, "head"), 19, WHITE if not mine else AMBER, "lm", "head")
        _text(d, (x1 + 520, y), f"{row['w']}-{row['l']}", 20, WHITE, "lm", "bold")
        _text(d, (x1 + 620, y), str(row["pts"]), 22, WHITE, "lm", "head")
        _text(d, (x1 + 720, y), str(row["left"]), 20, MUTED, "lm", "bold")
        bx1, bx2 = x1 + 840, x1 + 1130
        d.rounded_rectangle((bx1, y - 9, bx2, y + 9), radius=9, fill=_mix(col, _INK, 0.85))
        w = int((bx2 - bx1) * row["chance"])
        if w > 0:
            d.rounded_rectangle((bx1, y - 9, bx1 + max(18, w), y + 9), radius=9, fill=col)
        _text(d, (bx2 + 20, y), pct_text(row["chance"]), 24, col, "lm", "head")
        _text(d, (x1 + 1190, y), pct_text(row["tied"]) if row.get("tied", 0) >= 0.005 else "—", 18,
              AMBER if row.get("tied", 0) >= 0.005 else DIM, "lm", "bold")
        _pill(d, x2 - 30 - 190, y - 15, state, 12, fill=_mix(col, _INK, 0.7), fg=WHITE, outline=col, pad=10, h=30, kind="head")
    if not table:
        _text(d, ((x1 + x2) // 2, (y1 + y2) // 2 + 20), "NO GROUP DATA", 26, DIM, "mm", "head")
    d.line((x1 + 28, y1 + 150 + (min(data["spots"], n)) * step - step // 2, x2 - 28,
            y1 + 150 + min(data["spots"], n) * step - step // 2), fill=GREEN, width=2)
    _footer(c, f"GOOGLI MANAGER  •  QUALIFY  •  {data['group']}"[:90], data.get("guild"))
    return c.image


def render_qualify_page(data, index):
    if data.get("team") and index == 0:
        return _png(_page_team(data))
    return _png(_page_group(data))


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

        # results already recorded between pairs of this group
        pconn = mod.connect_points_db()
        try:
            results = pconn.execute("SELECT winner_team_key, loser_team_key FROM imported_match_results WHERE guild_id=?",
                                    (guild_id,)).fetchall()
        finally:
            pconn.close()
        first = (mod.first_stage_key(guild_id) == stage_key)
        played = {}
        for r in results:
            ends = []
            for key in (r["winner_team_key"], r["loser_team_key"]):
                prefix, _, rest = str(key).rpartition("::")
                if (prefix == stage_key) or (not prefix and first):
                    ends.append(norm(rest))
            if len(ends) == 2 and ends[0] in names and ends[1] in names:
                pair = frozenset(ends)
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
        fixtures = [(a, b, win_chance(record[a], record[b])) for a, b, _ in remaining]
        seed = hash((guild.id, team["name"], tuple(sorted(points.items())), tuple(f[:2] for f in fixtures))) & 0xFFFFFFFF
        key = (guild.id, team["name"], seed, spots)
        analysis = self._cache.get(key)
        if analysis is None:
            analysis = await asyncio.to_thread(analyse, team["name"], points, fixtures, spots, seed,
                                               lambda a, b: win_chance(record[a], record[b]))
            self._cache = {key: analysis}
        shim = types.SimpleNamespace(guild=guild)
        by_name = {str(t["name"]).casefold(): t for t in core.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved'", (guild.id,)).fetchall()}
        table = []
        for position, r in enumerate(rows, start=1):
            reg = by_name.get(str(r["team_name"]).casefold())
            left = sum(1 for f in fixtures if r["team_name"] in (f[0], f[1]))
            table.append({
                "name": r["team_name"], "rank": position, "w": int(r["matches_won"]), "l": int(r["matches_lost"]),
                "pts": int(r["points"]), "left": left, "chance": analysis["base"][r["team_name"]],
                "tied": analysis["tied"][r["team_name"]], "tie_win": analysis["tie_win"][r["team_name"]],
                "logo": await compare._logo_for(shim, reg) if reg is not None else None,
                "color": (team_role_color(core, guild, reg) if reg is not None else None) or DEFAULT_ACCENT,
            })
        table.sort(key=lambda x: (-x["chance"], x["rank"]))
        mine = next(x for x in table if x["name"] == team["name"])
        data = {"guild": guild.name, "group": f"Group {group_name}" if group_name else "Group", "spots": spots,
                "table": sorted(table, key=lambda x: x["rank"]),
                "highlight": team["name"], "team": None}
        if want_team:
            data["team"] = {
                "name": team["name"], "logo": mine["logo"], "color": mine["color"], "rank": mine["rank"], "of": len(rows),
                "w": mine["w"], "l": mine["l"], "pts": mine["pts"], "left": analysis["left"],
                "chance": mine["chance"], "status": status(mine["chance"]), "tied": mine["tied"], "tie_win": mine["tie_win"], "scen": analysis["scen"],
                "swings": analysis["swings"], "need": analysis["need"],
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
            view = ImagePager(ctx, png, 2, lambda i: render_qualify_page(data, i), ("This team", "Whole group"),
                              "qualify.png")
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
        """Temporary diagnostic for qualification fixtures and imported results."""
        from .groups import norm
        pts = self.bot.get_cog("PointsTable")
        if pts is None:
            return await ctx.reply("Points table is not loaded.", mention_author=False)
        mod = sys.modules[type(pts).__module__]
        stage = mod.get_active_stage(ctx.guild.id)
        gconn = sqlite3.connect(mod.GROUP_DB)
        gconn.row_factory = sqlite3.Row
        try:
            rows = gconn.execute(
                "SELECT team_name, group_key FROM team_groups WHERE guild_id=? AND team_name=? COLLATE NOCASE",
                (ctx.guild.id, team.strip())).fetchall()
            if not rows:
                return await ctx.reply(f"Could not find **{team}** in team groups.", mention_author=False)
            group_key = next((r["group_key"] for r in rows
                              if stage and r["group_key"].split("::", 1)[0].casefold() == stage[0].casefold()),
                             rows[-1]["group_key"])
            fixtures = gconn.execute(
                "SELECT team_one, team_two, day_no FROM generated_fixtures WHERE guild_id=? AND group_key=? ORDER BY day_no, id",
                (ctx.guild.id, group_key)).fetchall()
        finally:
            gconn.close()
        pconn = mod.connect_points_db()
        try:
            results = pconn.execute(
                "SELECT match_no, winner_team_key, loser_team_key FROM imported_match_results WHERE guild_id=? ORDER BY match_no",
                (ctx.guild.id,)).fetchall()
        finally:
            pconn.close()
        target = norm(team)
        lines = [f"QUALIFY DEBUG — {team}", f"Active stage: {stage}", f"Group key: {group_key}", "FIXTURES:"]
        for f in fixtures:
            lines.append(f"• {f['team_one']} vs {f['team_two']} (day {f['day_no']})")
        lines.append("IMPORTED RESULTS INVOLVING THIS TEAM:")
        found = 0
        for r in results:
            w, l = str(r["winner_team_key"]), str(r["loser_team_key"])
            if target in (norm(w.rpartition("::")[2]), norm(l.rpartition("::")[2])):
                found += 1
                lines.append(f"• Match #{r['match_no']}: WINNER=[{w}] LOSER=[{l}]")
        if not found:
            lines.append("• No imported result matched this team name.")
        message = "\n".join(lines)
        for start in range(0, len(message), 1800):
            await ctx.send(message[start:start+1800], allowed_mentions=discord.AllowedMentions.none())

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
