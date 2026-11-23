"""+potd @player  -> Player Of The Day card, drawn by the bot (no template image).

Everything is filled from the player's latest imported match (team, opponent, batting, bowling,
fielding). Attach a picture to use it as the photo, otherwise the Discord avatar is used.
Any field can be overridden in the same message:

    +potd @Ahmad vs=OrDer stage="Intermediate Stage • Round 1" batting="74 (19)" bowling="2/18 (4.0)"
    fielding="2 catches" team="Kashmir Titans" title="Kashmir Titans" match=<result message id>
"""
import re
import types

import discord
from discord.ext import commands

from .h2h import fetch_avatar, render_png, team_role_color
from .potd_image import render_potd
from .utility import owner_or_whitelisted

KEYS = ("vs", "stage", "batting", "bowling", "fielding", "team", "title", "match")
_PAIR = re.compile(r'(\w+)=(?:"([^"]*)"|“([^”]*)”|(\S+))')


def _overs(balls):
    return f"{balls // 6}.{balls % 6}"


class PlayerOfTheDay(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def core(self):
        return self.bot.get_cog("TeamRegistration")

    def _team_row(self, guild_id, name):
        if not name or self.core is None:
            return None
        try:
            row = self.core.db.execute(
                "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved' AND lower(name)=lower(?)",
                (guild_id, name)).fetchone()
            if row:
                return row
            team, error = self.core._resolve_team(guild_id, name)
            return team if not error else None
        except Exception:
            return None

    def _latest(self, guild_id, user_id, match_id=None):
        db = self.core.db
        sql = ("SELECT p.*, i.imported_at FROM player_match_stats p JOIN stats_imports i "
               "ON i.source_message_id=p.source_message_id WHERE i.guild_id=? AND p.user_id=? ")
        args = [guild_id, user_id]
        if match_id:
            sql += "AND p.source_message_id=? "
            args.append(match_id)
        sql += "ORDER BY i.imported_at DESC, p.source_message_id DESC LIMIT 1"
        row = db.execute(sql, args).fetchone()
        if not row:
            return None
        info = dict(row)
        sid = info["source_message_id"]
        mine = db.execute("SELECT team_name FROM match_teams WHERE source_message_id=? AND user_id=?",
                          (sid, user_id)).fetchone()
        info["team"] = mine["team_name"] if mine else None
        others = db.execute("SELECT DISTINCT team_name FROM match_teams WHERE source_message_id=?", (sid,)).fetchall()
        info["opponent"] = next((o["team_name"] for o in others if o["team_name"] != info["team"]), None)
        f = db.execute("SELECT catches_taken FROM player_match_fielding WHERE source_message_id=? AND user_id=?",
                       (sid, user_id)).fetchone()
        info["catches"] = int(f["catches_taken"]) if f else 0
        return info

    @commands.command(name="potd", aliases=["playeroftheday"])
    @commands.guild_only()
    async def potd(self, ctx, *, text: str = ""):
        if not await owner_or_whitelisted(ctx, "potd", app_owner=True):
            return await ctx.send("You are not allowed to use this command.")
        if self.core is None:
            return await ctx.send("The team system is not loaded.")
        opts = {}
        for m in _PAIR.finditer(text):
            key = m.group(1).lower()
            if key in KEYS:
                opts[key] = next(g for g in m.groups()[1:] if g is not None)
        member = ctx.message.mentions[0] if ctx.message.mentions else None
        if member is None:
            first = text.split()[0] if text.split() else None
            if first and "=" not in first:
                try:
                    member = await commands.MemberConverter().convert(ctx, first)
                except commands.BadArgument:
                    member = None
        if member is None:
            return await ctx.send("Usage: `+potd @player` (optional: attach a photo, `vs=Team stage=\"Round 1\" "
                                  "batting=\"74 (19)\" bowling=\"2/18 (4.0)\" fielding=\"2 catches\"`)")
        match_id = int(opts["match"]) if opts.get("match", "").isdigit() else None
        info = self._latest(ctx.guild.id, member.id, match_id)
        if info is None and not (opts.get("batting") or opts.get("bowling")):
            return await ctx.send(f"No imported match found for **{member.display_name}**. "
                                  "Give the stats yourself with `batting=` / `bowling=`.")
        info = info or {}
        team_name = opts.get("team") or info.get("team")
        if not team_name:
            row = self.core._membership(ctx.guild.id, member.id)
            team_name = row["name"] if row else ""
        opp_name = opts.get("vs") or info.get("opponent") or ""
        async with ctx.typing():
            team = self._team_row(ctx.guild.id, team_name)
            opp = self._team_row(ctx.guild.id, opp_name)
            compare = self.bot.get_cog("CompareTeams")
            shim = types.SimpleNamespace(guild=ctx.guild)
            logo = opp_logo = None
            if compare is not None:
                if team is not None:
                    logo = await compare._logo_for(shim, team)
                if opp is not None:
                    opp_logo = await compare._logo_for(shim, opp)
            color = team_role_color(self.core, ctx.guild, team) if team is not None else None
            photo = None
            for att in ctx.message.attachments:
                if (att.content_type or "").startswith("image/"):
                    try:
                        photo = await att.read()
                    except discord.HTTPException:
                        photo = None
                    break
            if photo is None:
                photo = await fetch_avatar(self.bot, member)
            batting = opts.get("batting")
            if batting is None:
                batting = "DNB"
                if info and (info["balls_faced"] or info["runs"]):
                    batting = f"{info['runs']}{'*' if info['not_out'] else ''} ({info['balls_faced']})"
            bowling = opts.get("bowling")
            if bowling is None:
                bowling = "DNB"
                if info and info["balls_bowled"]:
                    bowling = f"{info['wickets']}/{info['runs_conceded']} ({_overs(info['balls_bowled'])})"
            fielding = opts.get("fielding")
            if fielding is None:
                n = info.get("catches", 0) if info else 0
                fielding = f"{n} catch{'es' if n != 1 else ''}" if n else "DNF"
            title = opts.get("title") or ctx.guild.name
            words = [w for w in re.findall(r"[A-Za-z0-9]+", title)]
            data = {
                "title": title, "initials": "".join(w[0] for w in words[:3]).upper() or title[:2],
                "team": (team["name"] if team is not None else team_name),
                "team_logo": logo, "team_color": color,
                "opponent": (opp["name"] if opp is not None else opp_name),
                "opponent_logo": opp_logo, "photo": photo,
                "player": member.display_name, "stage": opts.get("stage", ""),
                "batting": batting, "bowling": bowling, "fielding": fielding,
            }
            png = await render_png(render_potd, data)
        import io
        await ctx.send(file=discord.File(io.BytesIO(png), filename="player_of_the_day.png"))


async def setup(bot):
    await bot.add_cog(PlayerOfTheDay(bot))
