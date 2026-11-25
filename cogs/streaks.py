"""+streaks: win / loss streaks of every team and the players who are on a run. One file: data, drawing, command.

Teams     current streak, best win streak, worst losing streak and the last 5 results, from the same
          match history the points table keeps (every stage, oldest to newest).
Players   consecutive most recent matches with 20+ runs or at least one wicket (2 or more in a row).
"""
import asyncio
import io
import sys
import types

import discord
from discord.ext import commands

from .awards import _glow_text, _halo, _icon, _tile_glass
from .h2h import fetch_avatar, on_cooldown, render_png
from .teaminfo_image import (
    DEFAULT_ACCENT, DIM, LOSS, MUTED, WIN, WHITE, _clip, _fit, _mix, _open, _paste, _pill, _round_image, _text, font,
)
from .versus_image import SIDE_B_FALLBACK, _Canvas, _footer, _png, _title

GREEN, RED, ORANGE = (62, 224, 172), (240, 95, 95), (255, 150, 70)
_INK = (12, 18, 32)
RUN_RUNS, RUN_WICKETS = 20, 1
MIN_RUN = 2
MAX_TEAMS, MAX_PLAYERS = 10, 5


# =========================================================================== rules (no Discord, no images)

def streak_stats(results):
    """results oldest -> newest ("W"/"L"). Returns now=(kind, n), best_w, worst_l."""
    now = ("", 0)
    best_w = worst_l = run = 0
    prev = None
    for r in results:
        run = run + 1 if r == prev else 1
        prev = r
        if r == "W":
            best_w = max(best_w, run)
        elif r == "L":
            worst_l = max(worst_l, run)
    if results:
        now = (results[-1], run)
    return now, best_w, worst_l


def player_run(rows):
    """Consecutive newest matches with 20+ runs or a wicket."""
    n = 0
    for r in sorted(rows, key=lambda r: r["mid"], reverse=True):
        if r["runs"] >= RUN_RUNS or r["wickets"] >= RUN_WICKETS:
            n += 1
        else:
            break
    return n


# =========================================================================== drawing

def _chip(d, x, y, now):
    kind, n = now
    if not n:
        _text(d, (x, y), "—", 20, DIM, "lm", "head")
        return
    color = GREEN if kind == "W" else RED
    _pill(d, x, y - 17, f"{kind}{n}", 18, fill=_mix(color, _INK, 0.6), fg=WHITE, outline=color, pad=14, h=34, kind="head")


def render_streaks(data):
    c = _Canvas(DEFAULT_ACCENT, SIDE_B_FALLBACK)
    left, right = (50, 126, 1000, 856), (1020, 126, 1550, 856)
    c.card((50, 22, 1550, 106), 24, 170)
    c.card(left, 26, 150)
    c.card(right, 26, 150)
    d = c.seal()
    _title(d, (76, 58), "STREAKS", 50, WHITE, "lm", "head")
    for i in range(300):
        d.line((76 + i, 90, 76 + i, 93), fill=_mix(DEFAULT_ACCENT, SIDE_B_FALLBACK, i / 299))
    _text(d, (1524, 56), f"{data.get('guild', '')}  •  MAIN TOURNAMENT".upper(), 16, MUTED, "rm")

    # ---- teams
    x1, y1, x2, y2 = left
    _tile_glass(c, left, GREEN)
    d.ellipse((x1 + 28, y1 + 28, x1 + 40, y1 + 40), fill=GREEN)
    _text(d, (x1 + 54, y1 + 20), "TEAM STREAKS", 26, GREEN, "la", "head")
    _text(d, (x1 + 28, y1 + 62), "CURRENT RUN, BEST WINNING RUN, WORST LOSING RUN", 14, MUTED)
    _icon(c, "up", x2 - 52, y1 + 50, GREEN)
    heads = (("TEAM", x1 + 30), ("NOW", x1 + 400), ("BEST W", x1 + 510), ("WORST L", x1 + 620), ("LAST 5", x1 + 750))
    for label, x in heads:
        _text(d, (x, y1 + 100), label, 13, MUTED)
    teams = data["teams"][:MAX_TEAMS]
    step = 54
    for i, t in enumerate(teams):
        y = y1 + 154 + i * step
        if i:
            d.line((x1 + 28, y - 27, x2 - 28, y - 27), fill=_mix(GREEN, _INK, 0.82), width=1)
        logo = _round_image(_open(t.get("logo")), 34, ring=t["color"], ring_w=2, initial=t["name"][:1])
        _paste(c.image, logo, (x1 + 28, y - logo.size[1] // 2))
        _text(d, (x1 + 78, y), _clip(d, t["name"].upper(), 300, 18, "head"), 18, WHITE, "lm", "head")
        _chip(d, x1 + 400, y, t["now"])
        _text(d, (x1 + 510, y), str(t["best_w"]) if t["best_w"] else "—", 24, GREEN if t["best_w"] else DIM, "lm", "head")
        _text(d, (x1 + 620, y), str(t["worst_l"]) if t["worst_l"] else "—", 24, RED if t["worst_l"] else DIM, "lm", "head")
        for j, res in enumerate(t["last"][-5:]):
            cx = x1 + 765 + j * 36
            d.ellipse((cx - 14, y - 14, cx + 14, y + 14), fill=WIN if res == "W" else LOSS)
            _text(d, (cx, y), res, 14, _INK, "mm", "head")
    if not teams:
        _text(d, ((x1 + x2) // 2, (y1 + y2) // 2 + 20), "NO RESULTS YET", 26, DIM, "mm", "head")

    # ---- players
    x1, y1, x2, y2 = right
    _tile_glass(c, right, ORANGE)
    d.ellipse((x1 + 28, y1 + 28, x1 + 40, y1 + 40), fill=ORANGE)
    _text(d, (x1 + 54, y1 + 20), "ON A RUN", 26, ORANGE, "la", "head")
    _text(d, (x1 + 28, y1 + 62), f"{RUN_RUNS}+ RUNS OR A WICKET, MATCH AFTER MATCH", 14, MUTED)
    _icon(c, "star", x2 - 52, y1 + 50, ORANGE)
    players = data["players"][:MAX_PLAYERS]
    for i, p in enumerate(players):
        y = y1 + 108 + i * 122
        ring = _round_image(_open(p.get("avatar")), 84, ring=ORANGE, ring_w=4, initial=p["name"][:1])
        _halo(c, x1 + 30 + ring.size[0] / 2, y + ring.size[1] / 2, ring.size[0] / 2, ORANGE)
        _paste(c.image, ring, (x1 + 30, y))
        tx = x1 + 140
        name = p["name"].upper()
        size = _fit(d, name, 240, 26, 16, "head")
        _title(d, (tx, y + 12), _clip(d, name, 240, size, "head"), size, WHITE, "la", "head")
        _text(d, (tx, y + 50), _clip(d, (p.get("team") or "FREE AGENT").upper(), 240, 13), 13, MUTED)
        _glow_text(c, (x2 - 112, y + 4), str(p["n"]), 64, ORANGE)
        _text(d, (x2 - 30, y + 62), "IN A ROW", 12, MUTED, "ra")
    if not players:
        _text(d, ((x1 + x2) // 2, (y1 + y2) // 2 + 20), "NO ACTIVE RUNS", 24, DIM, "mm", "head")
    _footer(c, "GOOGLI MANAGER  •  STREAKS", data.get("guild"))
    return _png(c.image)


# =========================================================================== command

class Streaks(commands.Cog):
    """+streaks."""

    def __init__(self, bot):
        self.bot = bot

    @property
    def compare(self):
        return self.bot.get_cog("CompareTeams")

    def _team_results(self, guild_id):
        """{normalised team name: ["W", "L", ...] oldest first} from the points history of every stage."""
        pts = self.bot.get_cog("PointsTable")
        if pts is None:
            return {}
        mod = sys.modules[type(pts).__module__]
        conn = mod.connect_points_db()
        try:
            rows = conn.execute("SELECT id, team_name_key, result, match_no FROM team_match_history WHERE guild_id=?",
                                (guild_id,)).fetchall()
        finally:
            conn.close()

        def order(item):                       # same order the points table uses for form
            number = (item["match_no"] or "").strip()
            if not number:
                return (-1, 0, item["id"])
            if number.isdigit():
                return (0, int(number), item["id"])
            return (1, 0, item["id"])

        out = {}
        for item in sorted(rows, key=order):
            out.setdefault(str(item["team_name_key"]).split("::")[-1], []).append(item["result"])
        return out

    async def _data(self, ctx):
        from .groups import norm
        from .h2h import team_role_color
        compare, guild = self.compare, ctx.guild
        core = compare.core
        history = await asyncio.to_thread(self._team_results, guild.id)
        shim = types.SimpleNamespace(guild=guild)
        teams = []
        for team in core.db.execute(
                "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved'", (guild.id,)).fetchall():
            results = history.get(norm(team["name"]), [])
            if not results:
                continue
            now, best_w, worst_l = streak_stats(results)
            teams.append({
                "name": str(team["name"]), "now": now, "best_w": best_w, "worst_l": worst_l, "last": results[-5:],
                "logo": await compare._logo_for(shim, team),
                "color": team_role_color(core, guild, team) or DEFAULT_ACCENT,
            })
        teams.sort(key=lambda t: (-(t["now"][1] if t["now"][0] == "W" else 0), -t["best_w"],
                                  (t["now"][1] if t["now"][0] == "L" else 0), t["name"].casefold()))
        index = compare._guild_rows(guild.id)
        runs = sorted(((player_run(rows), uid) for uid, rows in index["players"].items()), reverse=True)
        runs = [(n, uid) for n, uid in runs if n >= MIN_RUN][:MAX_PLAYERS]
        sem = asyncio.Semaphore(6)

        async def person(n, uid):
            async with sem:
                member = guild.get_member(uid) or self.bot.get_user(uid)
                if member is None:
                    try:
                        member = await self.bot.fetch_user(uid)
                    except discord.HTTPException:
                        member = None
                membership = core._membership(guild.id, uid)
                return {"n": n, "name": compare._display_name(guild, uid), "avatar": await fetch_avatar(self.bot, member),
                        "team": str(membership["name"]) if membership else None}

        players = await asyncio.gather(*(person(n, uid) for n, uid in runs))
        return {"guild": guild.name, "teams": teams, "players": list(players)}

    @commands.command(name="streaks", aliases=["streak", "runs"])
    @commands.guild_only()
    async def streaks(self, ctx):
        """Team win / loss streaks and the players who are on a run."""
        if self.compare is None:
            return await ctx.reply("The team system isn't loaded right now.", mention_author=False)
        wait = on_cooldown(ctx.author.id)
        if wait:
            return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
        loading = await ctx.reply("⏳ Checking the streaks...", mention_author=False)
        try:
            data = await self._data(ctx)
            png = await render_png(render_streaks, data)
            await loading.edit(content=None, attachments=[discord.File(io.BytesIO(png), filename="streaks.png")])
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"[streaks] failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                await loading.delete()
            except Exception:
                pass
            await ctx.send(f"I couldn't build the streaks: `{type(exc).__name__}: {exc}`")


async def setup(bot):
    await bot.add_cog(Streaks(bot))
