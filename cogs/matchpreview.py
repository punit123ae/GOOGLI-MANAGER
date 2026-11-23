"""Automatic match preview: when a match is announced, post a win-probability card.

The three places that announce a match (/match_schedule, the captains' agreed time and
+forceannounce) fire the "match_announced" event after the announcement is sent. This cog
listens for it and replies to the announcement with the card. Nothing here can break an
announcement: the card is drawn afterwards, one at a time, and any failure is only logged.

+winprobability Team A | Team B   (+wp, +mp, +tmp) draw the same card on demand;
                                     with one team name it is your own team against that team
"""
import asyncio
import io
import re
import types

import discord
from discord.ext import commands

from .compareteams import predict
from .h2h import on_cooldown, render_png
from .match_preview_image import render_match_preview


class MatchPreview(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()          # one card at a time (several fixtures can be announced together)

    @property
    def compare(self):
        return self.bot.get_cog("CompareTeams")

    # ------------------------------------------------------------------ building
    def _team_for_role(self, guild_id, role_id):
        core = self.bot.get_cog("TeamRegistration")
        if core is None:
            return None
        try:
            return core.db.execute(
                "SELECT t.* FROM registered_teams t JOIN team_role_settings s ON s.team_id=t.id "
                "WHERE t.guild_id=? AND t.status='approved' AND s.role_id=? LIMIT 1",
                (int(guild_id), int(role_id)),
            ).fetchone()
        except Exception:
            return None

    async def build_png(self, guild, team_a, team_b):
        """The preview image (bytes) for two registered teams."""
        compare = self.compare
        ctx = types.SimpleNamespace(guild=guild)
        a = await compare._side(ctx, team_a, avatars=False)
        b = await compare._side(ctx, team_b, avatars=False)
        prediction = predict(a, b)
        data = {
            "a": {k: a[k] for k in ("name", "logo", "color", "standing", "roster")},
            "b": {k: b[k] for k in ("name", "logo", "color", "standing", "roster")},
            "prediction": prediction, "guild": guild.name,
        }
        return await render_png(render_match_preview, data)

    # ------------------------------------------------------------------ automatic
    @commands.Cog.listener()
    async def on_match_announced(self, guild, channel, role_a, role_b, announcement=None):
        if self.compare is None:
            return
        async with self._lock:
            try:
                team_a = self._team_for_role(guild.id, role_a.id)
                team_b = self._team_for_role(guild.id, role_b.id)
                if team_a is None or team_b is None:
                    return                      # a role that isn't an approved team's role: nothing to compare
                png = await self.build_png(guild, team_a, team_b)
                def card():
                    return discord.File(io.BytesIO(png), filename="match_preview.png")

                none = discord.AllowedMentions.none()
                try:
                    await channel.send(file=card(), reference=announcement, mention_author=False,
                                       allowed_mentions=none)
                except discord.HTTPException:   # e.g. the announcement was deleted meanwhile: post it plain
                    await channel.send(file=card(), allowed_mentions=none)
            except Exception as exc:
                print(f"[matchpreview] skipped: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(1)              # breathing space if many matches are announced together

    # ------------------------------------------------------------------ command
    @commands.command(name="winprobability",
                      aliases=["winprobabilty", "wp", "mp", "tmp", "matchpreview", "testmatchpreview"])
    @commands.guild_only()
    async def win_probability_command(self, ctx, *, teams: str = ""):
        """Win probability card. +wp Team A | Team B, or +wp Team (your own team vs that team)."""
        compare, core = self.compare, self.bot.get_cog("TeamRegistration")
        if compare is None or core is None:
            await ctx.reply("The team system isn't loaded right now.", mention_author=False)
            return
        usage = "Usage: `+wp Team A | Team B`, or `+wp Team` to play your own team against it."
        parts = [p.strip() for p in re.split(r"\s*\|\s*|\s+vs\.?\s+", teams, flags=re.I) if p.strip()]
        if len(parts) == 1:                     # one name: your own team against that one
            mine = core._membership(ctx.guild.id, ctx.author.id)
            if not mine:
                await ctx.reply(usage, mention_author=False)
                return
            parts = [str(mine["name"]), parts[0]]
        if len(parts) != 2:
            await ctx.reply(usage, mention_author=False)
            return
        found = []
        for query in parts:
            team, error = compare._resolve(ctx.guild.id, query)
            if error:
                await ctx.reply(error, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                return
            found.append(team)
        if int(found[0]["id"]) == int(found[1]["id"]):
            await ctx.reply("Pick two different teams.", mention_author=False)
            return
        wait = on_cooldown(ctx.author.id)
        if wait:
            await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
            return
        async with self._lock, ctx.typing():
            try:
                png = await self.build_png(ctx.guild, found[0], found[1])
            except Exception as exc:
                print(f"[matchpreview] command failed: {type(exc).__name__}: {exc}", flush=True)
                await ctx.reply("I couldn't draw the win probability card. Check the bot logs.",
                                mention_author=False)
                return
        await ctx.reply(file=discord.File(io.BytesIO(png), filename="win_probability.png"),
                        mention_author=False)


async def setup(bot):
    await bot.add_cog(MatchPreview(bot))
