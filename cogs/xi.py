"""+xi and +cxi: the best playing XI of a team, and two XIs side by side.

+xi                      your own team's best XI
+xi <team>               another team's best XI
+cxi <team>              your team's XI against that team's XI
+cxi <team> | <team>     any two teams

The XI is picked from real stats (see xi_engine.py): batting average, runs, matches played,
wickets, bowling average and strike rate, catches and fantasy points. Strike rate hardly counts.
+dream                   the best XI of the whole server, from every team's players
Pages: 1 playing XI, 2 batting & bowling order, 3 substitutes, 4 bench (+cxi: 1 the two XIs, 2 key players, 3 rating breakdown).
"""
import asyncio
import io
import re

import discord
from discord.ext import commands

from . import xi_engine
from .h2h import ImagePager, fetch_avatar, on_cooldown, render_png, team_role_color
from .dream_image import render_dream_page
from .xi_image import render_cxi_page, render_xi_page

USAGE_XI = "Usage: `+xi` (your team) or `+xi <team name>`."
USAGE_CXI = "Usage: `+cxi <team>` (your team against it) or `+cxi <team> | <team>`."


class PlayingXI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._pool_cache = {}          # guild id -> (index object, Pool)
        self._side_cache = {}          # (guild, team id) -> (time, side)

    @property
    def compare(self):
        return self.bot.get_cog("CompareTeams")

    # ------------------------------------------------------------------ data
    def _pool(self, guild_id, index):
        """Percentile tables over every player in the server, rebuilt when the stats index is."""
        hit = self._pool_cache.get(int(guild_id))
        if hit and hit[0] is index:
            return hit[1]
        from .lb import _totals
        from .stats import fantasy_raw_points
        everyone = [
            xi_engine.profile(uid, rows, *index["fielding_players"].get(uid, (0, 0)),
                              _totals, lambda r: fantasy_raw_points(r)[2])
            for uid, rows in index["players"].items()
        ]
        pool = xi_engine.Pool(everyone)
        self._pool_cache[int(guild_id)] = (index, pool)
        return pool

    async def build_side(self, ctx, team):
        """Everything the images need for one team (kept 2 minutes)."""
        import time
        key = (ctx.guild.id, int(team["id"]))
        hit = self._side_cache.get(key)
        if hit and time.monotonic() - hit[0] < 120:
            return hit[1]
        compare, guild = self.compare, ctx.guild
        core = compare.core
        index = compare._guild_rows(guild.id)
        pool = self._pool(guild.id, index)

        rows = core.db.execute("SELECT user_id FROM team_players WHERE team_id=? ORDER BY added_at",
                               (team["id"],)).fetchall()
        roster = list(dict.fromkeys(
            [int(team["captain_id"])] + ([int(team["vice_captain_id"])] if team["vice_captain_id"] else [])
            + [int(r["user_id"]) for r in rows]))

        from .lb import _totals
        from .stats import fantasy_raw_points
        profiles = [
            xi_engine.profile(uid, index["players"].get(uid, []), *index["fielding_players"].get(uid, (0, 0)),
                              _totals, lambda r: fantasy_raw_points(r)[2])
            for uid in roster
        ]
        typical = index.get("typical") or 11
        size = 11 if typical >= 9 else max(5, typical)
        result = xi_engine.build(profiles, pool, size=size, captain=int(team["captain_id"]),
                                 vice=int(team["vice_captain_id"]) if team["vice_captain_id"] else None)

        logo = await compare._logo_for(ctx, team)
        people = {}
        shown = result["xi"] + result["subs"] + result["bench"][:9]
        for p in shown:
            people[p["uid"]] = {"name": compare._display_name(guild, p["uid"]), "avatar": None}
        for p in result["xi"] + result["subs"] + result["bench"]:
            people.setdefault(p["uid"], {"name": compare._display_name(guild, p["uid"]), "avatar": None})

        sem = asyncio.Semaphore(6)

        async def avatar(uid):
            async with sem:
                member = guild.get_member(uid) or self.bot.get_user(uid)
                if member is None:
                    try:
                        member = await self.bot.fetch_user(uid)
                    except discord.HTTPException:
                        member = None
                people[uid]["avatar"] = await fetch_avatar(self.bot, member)

        await asyncio.gather(*(avatar(uid) for uid in people), return_exceptions=True)

        color = team_role_color(core, guild, team)
        side = {"name": str(team["name"]), "logo": logo, "color": color, "guild": guild.name,
                "xi": result["xi"], "subs": result["subs"], "bench": result["bench"],
                "strength": result["strength"], "roster": len(roster), "size": result["size"],
                "bowl_order": result["bowl_order"],
                "people": people}
        if len(self._side_cache) >= 4:
            self._side_cache.pop(next(iter(self._side_cache)))
        self._side_cache[key] = (time.monotonic(), side)
        return side

    # ------------------------------------------------------------------ helpers
    async def _teams(self, ctx, query, usage, mode):
        """Resolve the names given to a command into a list of team rows; replies on a problem.

        mode "one" (+xi):  no name = your team, one name = that team.
        mode "two" (+cxi): one name = your team against it, two names = those two teams.
        """
        compare, core = self.compare, self.bot.get_cog("TeamRegistration")
        if compare is None or core is None:
            await ctx.reply("The team system isn't loaded right now.", mention_author=False)
            return None
        parts = [p.strip() for p in re.split(r"\s*\|\s*|\s+vs\.?\s+", query, flags=re.I) if p.strip()]
        if mode == "one":
            if len(parts) > 1:
                await ctx.reply(usage, mention_author=False)
                return None
            if not parts:
                mine = core._membership(ctx.guild.id, ctx.author.id)
                if not mine:
                    await ctx.reply("You aren't on a team, so name one. " + usage, mention_author=False)
                    return None
                return [mine]
        else:
            if len(parts) == 1:
                mine = core._membership(ctx.guild.id, ctx.author.id)
                if not mine:
                    await ctx.reply(usage, mention_author=False)
                    return None
                parts = [str(mine["name"]), parts[0]]
            if len(parts) != 2:
                await ctx.reply(usage, mention_author=False)
                return None
        found = []
        for part in parts:
            team, error = compare._resolve(ctx.guild.id, part)
            if error:
                await ctx.reply(error, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                return None
            found.append(team)
        return found

    # ------------------------------------------------------------------ commands
    # ------------------------------------------------------------------ +dream
    async def build_dream(self, ctx):
        """Best XI across every approved team of the server (kept 2 minutes)."""
        import time
        key = ("dream", ctx.guild.id)
        hit = self._side_cache.get(key)
        if hit and time.monotonic() - hit[0] < 120:
            return hit[1]
        compare, guild = self.compare, ctx.guild
        core = compare.core
        index = compare._guild_rows(guild.id)
        pool = self._pool(guild.id, index)
        teams = core.db.execute("SELECT * FROM registered_teams WHERE guild_id=? AND status='approved'",
                                (guild.id,)).fetchall()
        owner = {}                                   # uid -> team row
        for team in teams:
            ids = [int(team["captain_id"])] + ([int(team["vice_captain_id"])] if team["vice_captain_id"] else [])
            ids += [int(r["user_id"]) for r in core.db.execute(
                "SELECT user_id FROM team_players WHERE team_id=?", (team["id"],)).fetchall()]
            for uid in ids:
                owner.setdefault(uid, team)
        from .lb import _totals
        from .stats import fantasy_raw_points
        profiles = [
            xi_engine.profile(uid, index["players"].get(uid, []), *index["fielding_players"].get(uid, (0, 0)),
                              _totals, lambda r: fantasy_raw_points(r)[2])
            for uid in owner if index["players"].get(uid)
        ]
        if not profiles:
            return None
        typical = index.get("typical") or 11
        size = 11 if typical >= 9 else max(5, typical)
        result = xi_engine.build(profiles, pool, size=size)
        xi, subs = result["xi"], result["subs"]
        by_value = sorted(xi, key=lambda p: -p["value"])
        captain = by_value[0]["uid"] if by_value else None
        vice = by_value[1]["uid"] if len(by_value) > 1 else None

        shown = xi + subs
        logos, colors = {}, {}
        for team in {int(t["id"]): t for t in owner.values()}.values():
            logos[int(team["id"])] = await compare._logo_for(ctx, team)
            colors[int(team["id"])] = team_role_color(core, guild, team)
        people = {p["uid"]: {"name": compare._display_name(guild, p["uid"]), "avatar": None} for p in shown}
        sem = asyncio.Semaphore(6)

        async def avatar(uid):
            async with sem:
                member = guild.get_member(uid) or self.bot.get_user(uid)
                if member is None:
                    try:
                        member = await self.bot.fetch_user(uid)
                    except discord.HTTPException:
                        member = None
                people[uid]["avatar"] = await fetch_avatar(self.bot, member)

        await asyncio.gather(*(avatar(uid) for uid in people), return_exceptions=True)
        team_of = {p["uid"]: {"name": str(owner[p["uid"]]["name"]), "color": colors.get(int(owner[p["uid"]]["id"]))}
                   for p in shown}
        counts = {}
        for p in xi:
            t = owner[p["uid"]]
            counts[int(t["id"])] = counts.get(int(t["id"]), 0) + 1
        by_id = {int(t["id"]): t for t in owner.values()}
        team_counts = sorted(((str(by_id[i]["name"]), logos.get(i), n) for i, n in counts.items()),
                             key=lambda x: (-x[2], x[0]))
        data = {"guild": guild.name, "xi": xi, "subs": subs, "strength": result["strength"], "people": people,
                "teams": team_of, "team_counts": team_counts, "players": len(profiles), "teams_total": len(teams),
                "captain": captain, "vice": vice}
        if len(self._side_cache) >= 4:
            self._side_cache.pop(next(iter(self._side_cache)))
        self._side_cache[key] = (time.monotonic(), data)
        return data

    @commands.command(name="dream", aliases=["dreamxi", "serverxi"])
    @commands.guild_only()
    async def dream_command(self, ctx):
        """The best XI of the whole server, picked from every team's players."""
        if self.compare is None:
            return await ctx.reply("The team system isn't loaded right now.", mention_author=False)
        wait = on_cooldown(ctx.author.id)
        if wait:
            return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
        loading = await ctx.reply("⏳ Picking the dream XI...", mention_author=False)
        try:
            data = await self.build_dream(ctx)
            if data is None:
                return await loading.edit(content="No player has recorded matches yet, so there is no dream XI.")
            png = await render_png(render_dream_page, data, 0)
            view = ImagePager(ctx, png, 2, lambda i: render_dream_page(data, i), ("Dream XI", "The numbers"), "dream.png")
            view.message = await loading.edit(content=None, attachments=[discord.File(io.BytesIO(png), filename="dream.png")],
                                              view=view)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"[dream] failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                await loading.delete()
            except Exception:
                pass
            await ctx.send("I couldn't build the dream XI. Check the bot logs.")


    @commands.command(name="xi", aliases=["bestxi", "playingxi"])
    @commands.guild_only()
    async def xi_command(self, ctx, *, team: str = ""):
        """Best playing XI: +xi (your team) or +xi <team>."""
        found = await self._teams(ctx, team, USAGE_XI, "one")
        if not found:
            return
        wait = on_cooldown(ctx.author.id)
        if wait:
            await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
            return
        loading = await ctx.reply("⏳ Picking the best XI...", mention_author=False)
        try:
            side = await self.build_side(ctx, found[0])
            png = await render_png(render_xi_page, side, 0)
            view = ImagePager(ctx, png, 4, lambda index: render_xi_page(side, index),
                              ("Playing XI", "Batting & Bowling", "Substitutes", "Bench"), "xi.png")
            view.message = await loading.edit(content=None,
                                              attachments=[discord.File(io.BytesIO(png), filename="xi.png")],
                                              view=view)
        except Exception as exc:
            print(f"[xi] failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                await loading.delete()
            except Exception:
                pass
            await ctx.send("I couldn't build the XI. Check the bot logs.")

    @commands.command(name="cxi", aliases=["comparexi", "xivs"])
    @commands.guild_only()
    async def cxi_command(self, ctx, *, teams: str = ""):
        """Compare two best XIs: +cxi <team> or +cxi <team> | <team>."""
        found = await self._teams(ctx, teams, USAGE_CXI, "two")
        if not found:
            return
        if int(found[0]["id"]) == int(found[1]["id"]):
            await ctx.reply("Pick two different teams.", mention_author=False)
            return
        wait = on_cooldown(ctx.author.id)
        if wait:
            await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
            return
        loading = await ctx.reply("⏳ Comparing the two XIs...", mention_author=False)
        try:
            a = await self.build_side(ctx, found[0])
            b = await self.build_side(ctx, found[1])
            png = await render_png(render_cxi_page, a, b, 0)
            view = ImagePager(ctx, png, 3, lambda index: render_cxi_page(a, b, index),
                              ("Both XIs", "Key players", "Ratings"), "cxi.png")
            view.message = await loading.edit(content=None,
                                              attachments=[discord.File(io.BytesIO(png), filename="cxi.png")],
                                              view=view)
        except Exception as exc:
            print(f"[cxi] failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                await loading.delete()
            except Exception:
                pass
            await ctx.send("I couldn't compare the XIs. Check the bot logs.")


async def setup(bot):
    await bot.add_cog(PlayingXI(bot))
