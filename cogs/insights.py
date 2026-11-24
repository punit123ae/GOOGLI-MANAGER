"""Player / team insight commands built on the XI rating engine.

    +compareplayers   two players side by side (alias +cp)
"""
import io
import re

import discord
from discord.ext import commands

from . import xi_engine
from .h2h import fetch_avatar, on_cooldown, render_png, team_role_color
from .insights_image import render_compare_players

MENTION = re.compile(r"<@!?(\d+)>")
USAGE_CP = "Use `+compareplayers @player` (you vs them) or `+compareplayers @player | @player`."


class Insights(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._rated = {}              # guild id -> (stats index, {uid: rated profile})

    @property
    def compare(self):
        return self.bot.get_cog("CompareTeams")

    @property
    def core(self):
        return self.bot.get_cog("TeamRegistration")

    # ------------------------------------------------------------------ shared data
    def rated_all(self, guild_id):
        """Rated profile of every player with stats (same maths as +xi), rebuilt with the stats index."""
        compare, xi = self.compare, self.bot.get_cog("PlayingXI")
        index = compare._guild_rows(guild_id)
        hit = self._rated.get(int(guild_id))
        if hit and hit[0] is index:
            return hit[1]
        from .lb import _totals
        from .stats import fantasy_raw_points
        pool = xi._pool(guild_id, index)
        out = {}
        for uid in set(index["players"]) | set(index["fielding_players"]):
            raw = xi_engine.profile(uid, index["players"].get(uid, []), *index["fielding_players"].get(uid, (0, 0)),
                                    _totals, lambda r: fantasy_raw_points(r)[2])
            out[uid] = xi_engine.rate(raw, pool)
        self._rated = {int(guild_id): (index, out)}
        return out

    async def person(self, guild, uid, rated):
        """Name, avatar, current team, team colour and rating of one player."""
        compare, core = self.compare, self.core
        p = rated.get(uid)
        if p is None:
            from .lb import _totals
            from .stats import fantasy_raw_points
            index = compare._guild_rows(guild.id)
            pool = self.bot.get_cog("PlayingXI")._pool(guild.id, index)
            p = xi_engine.rate(xi_engine.profile(uid, [], 0, 0, _totals, lambda r: fantasy_raw_points(r)[2]), pool)
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await self.bot.fetch_user(uid)
            except discord.HTTPException:
                member = None
        team = core._membership(guild.id, uid)
        return {
            "uid": uid, "name": compare._display_name(guild, uid), "avatar": await fetch_avatar(self.bot, member),
            "team": str(team["name"]) if team else None,
            "color": team_role_color(core, guild, team) if team else None, "p": p,
        }

    async def members_from(self, ctx, text, usage, need=1, max_n=2):
        """Players named by mention or by name in the command text. Returns a list of user ids or None."""
        ids = [int(m) for m in MENTION.findall(text)]
        rest = MENTION.sub("|", text)
        for part in re.split(r"\s*\|\s*|\s+vs\.?\s+", rest, flags=re.I):
            part = part.strip()
            if not part:
                continue
            try:
                ids.append((await commands.MemberConverter().convert(ctx, part)).id)
            except commands.BadArgument:
                await ctx.reply(f"I couldn't find a member called `{discord.utils.escape_markdown(part)}`. {usage}",
                                mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                return None
        ids = list(dict.fromkeys(ids))
        if len(ids) < need or len(ids) > max_n:
            await ctx.reply(usage, mention_author=False)
            return None
        return ids

    async def _busy(self, ctx):
        wait = on_cooldown(ctx.author.id)
        if wait:
            await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
            return True
        return False

    async def _send(self, ctx, loading, png, name):
        await loading.edit(content=None, attachments=[discord.File(io.BytesIO(png), filename=name)])

    # ------------------------------------------------------------------ +compareplayers
    @commands.command(name="compareplayers", aliases=["cp", "cmpp", "playervs"])
    @commands.guild_only()
    async def compareplayers(self, ctx, *, players: str = ""):
        """Compare two players: +cp @player (you vs them) or +cp @a | @b."""
        if self.compare is None or self.core is None or self.bot.get_cog("PlayingXI") is None:
            return await ctx.reply("The team system isn't loaded right now.", mention_author=False)
        ids = await self.members_from(ctx, players, USAGE_CP, need=1, max_n=2)
        if not ids:
            return
        if len(ids) == 1:
            ids = [ctx.author.id, ids[0]]
        if ids[0] == ids[1]:
            return await ctx.reply("Pick two different players.", mention_author=False)
        if await self._busy(ctx):
            return
        loading = await ctx.reply("⏳ Comparing the players...", mention_author=False)
        try:
            rated = self.rated_all(ctx.guild.id)
            a = await self.person(ctx.guild, ids[0], rated)
            b = await self.person(ctx.guild, ids[1], rated)
            png = await render_png(render_compare_players, {"a": a, "b": b, "guild": ctx.guild.name})
            await self._send(ctx, loading, png, "compare_players.png")
        except Exception as exc:
            print(f"[compareplayers] failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                await loading.delete()
            except Exception:
                pass
            await ctx.send("I couldn't compare those players. Check the bot logs.")


async def setup(bot):
    await bot.add_cog(Insights(bot))
