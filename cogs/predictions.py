"""+addmatch and +predict: predictions for upcoming matches.

+addmatch Team A | Team B     (admins) save an upcoming match
+addmatch                     list the saved matches
+addmatch remove <number>     delete a saved match
+predict [number]             the prediction card for a saved match (the newest one by default)
+predict Team A | Team B      the same card for any two teams, without saving anything

The card shows who is favoured to win and, for runs, wickets, catches and fantasy points, the most likely
top performer of each team and of the whole match (see predict_engine.py for how).
"""
import asyncio
import io
import re
import sqlite3
import types

import discord
from discord.ext import commands

from .compareteams import predict as win_prediction
from .h2h import fetch_avatar, on_cooldown, render_png
from .predict_engine import CATEGORIES, expectation, league_means, player_input, simulate
from .predict_image import render_predict_png
from .utility import owner_or_whitelisted

SIDE_A, SIDE_B = (70, 140, 255), (255, 120, 60)


class Predictions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()           # one card at a time: the simulation and the drawing use the CPU
        bot.db.execute(
            "CREATE TABLE IF NOT EXISTS predict_matches (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, "
            "number INTEGER NOT NULL, team_a_id INTEGER NOT NULL, team_b_id INTEGER NOT NULL, added_by INTEGER, "
            "added_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(guild_id, number))")
        bot.db.commit()

    @property
    def compare(self):
        return self.bot.get_cog("CompareTeams")

    # ------------------------------------------------------------------ helpers
    def _teams_from(self, guild_id, text):
        """(team A row, team B row, error text)."""
        compare = self.compare
        parts = [p.strip() for p in re.split(r"\s*\|\s*|\s+vs\.?\s+", text, flags=re.I) if p.strip()]
        if len(parts) != 2:
            return None, None, "Give two teams: `Team A | Team B`."
        found = []
        for query in parts:
            team, error = compare._resolve(guild_id, query)
            if error:
                return None, None, error
            found.append(team)
        if int(found[0]["id"]) == int(found[1]["id"]):
            return None, None, "Pick two different teams."
        return found[0], found[1], None

    def _team_by_id(self, team_id):
        return self.compare.core.db.execute("SELECT * FROM registered_teams WHERE id=?", (int(team_id),)).fetchone()

    def _roster(self, team):
        core = self.compare.core
        rows = core.db.execute("SELECT user_id FROM team_players WHERE team_id=? ORDER BY added_at",
                               (team["id"],)).fetchall()
        return list(dict.fromkeys([int(team["captain_id"])]
                                  + ([int(team["vice_captain_id"])] if team["vice_captain_id"] else [])
                                  + [int(r["user_id"]) for r in rows]))

    def _inputs(self, index, team_name, uids, fantasy_rows):
        """player_input() for each given player of a team (rows are the ones recorded under that team)."""
        key = str(team_name).casefold()
        stats, fielding = index["stats"].get(key, {}), index["fielding"].get(key, {})
        team_matches = len({r["mid"] for rows in stats.values() for r in rows})
        out = []
        for uid in uids:
            rows = stats.get(uid, [])
            caught = fielding.get(uid, (0, 0))[0]
            if not rows and not caught:
                continue
            out.append(player_input(uid, rows, [fantasy_rows(r) for r in rows], caught, team_matches))
        return out

    def _compute(self, index, team_a, team_b, win):
        from .stats import fantasy_raw_points
        fant = lambda r: float(fantasy_raw_points([r])[2])
        rosters = [self._roster(team_a), self._roster(team_b)]
        league = league_means([
            pi for key, players in index["stats"].items() for pi in self._inputs(index, key, list(players), fant)])
        squads = []
        for team, uids in zip((team_a, team_b), rosters):
            squads.append([expectation(pi, league) for pi in self._inputs(index, team["name"], uids, fant)])
        total_rows = sum(len(rows) for players in index["stats"].values() for rows in players.values())
        seed = hash((int(team_a["id"]), int(team_b["id"]), total_rows)) & 0xFFFFFFFF
        return simulate(squads, 4000, seed), squads

    async def _card(self, ctx, team_a, team_b, number):
        compare, guild = self.compare, ctx.guild
        shim = types.SimpleNamespace(guild=guild)
        side_a = await compare._side(shim, team_a, avatars=False)
        side_b = await compare._side(shim, team_b, avatars=False)
        win = win_prediction(side_a, side_b)
        index = compare._guild_rows(guild.id)
        result, squads = await asyncio.to_thread(self._compute, index, team_a, team_b, win)
        side_of = {e["uid"]: t for t, squad in enumerate(squads) for e in squad}
        colors = (side_a.get("color") or SIDE_A, side_b.get("color") or SIDE_B)
        wanted = set()
        cats = {}
        for cat in CATEGORIES:
            r = result[cat]
            cats[cat] = {"team": [(r["team"][t][0] if r["team"][t] else None) for t in (0, 1)],
                         "match": r["match"][0] if r["match"] else None}
            for pick in (*cats[cat]["team"], cats[cat]["match"]):
                if pick:
                    wanted.add(pick["uid"])
        sem = asyncio.Semaphore(6)
        people = {}

        async def one(uid):
            async with sem:
                member = guild.get_member(uid) or self.bot.get_user(uid)
                if member is None:
                    try:
                        member = await self.bot.fetch_user(uid)
                    except discord.HTTPException:
                        member = None
                people[uid] = {"name": compare._display_name(guild, uid), "avatar": await fetch_avatar(self.bot, member),
                               "color": colors[side_of.get(uid, 0)]}

        await asyncio.gather(*(one(u) for u in wanted), return_exceptions=True)
        data = {
            "guild": guild.name, "number": number, "known": bool(win["known"]),
            "a": {"name": side_a["name"], "logo": side_a["logo"], "color": colors[0], "win": win["a"]},
            "b": {"name": side_b["name"], "logo": side_b["logo"], "color": colors[1], "win": win["b"]},
            "cats": cats, "people": {u: v for u, v in people.items()},
        }
        return await render_png(render_predict_png, data)

    # ------------------------------------------------------------------ +addmatch
    @commands.command(name="addmatch", aliases=["addpredictmatch"])
    @commands.guild_only()
    async def addmatch(self, ctx, *, text: str = ""):
        """+addmatch Team A | Team B saves an upcoming match for +predict. No text lists them; `remove <n>` deletes one."""
        if self.compare is None:
            return await ctx.reply("The team system isn't loaded right now.", mention_author=False)
        db, none = self.bot.db, discord.AllowedMentions.none()
        text = text.strip()
        if not text:
            rows = db.execute("SELECT * FROM predict_matches WHERE guild_id=? ORDER BY number DESC LIMIT 15",
                              (ctx.guild.id,)).fetchall()
            if not rows:
                return await ctx.reply("No matches saved yet. Add one with `+addmatch Team A | Team B`.",
                                       mention_author=False)
            lines = []
            for r in rows:
                a, b = self._team_by_id(r["team_a_id"]), self._team_by_id(r["team_b_id"])
                lines.append(f"**#{r['number']}**  {a['name'] if a else '?'} vs {b['name'] if b else '?'}")
            return await ctx.reply("**Saved matches** (newest first; `+predict <number>` shows one)\n" + "\n".join(lines),
                                   mention_author=False, allowed_mentions=none)
        if not await owner_or_whitelisted(ctx, command_name="addmatch", app_owner=True):
            return await ctx.reply("Only the bot owner or a role whitelisted for this command can add matches.",
                                   mention_author=False)
        m = re.match(r"(?i)^(remove|delete|del)\s+#?(\d+)$", text)
        if m:
            cur = db.execute("DELETE FROM predict_matches WHERE guild_id=? AND number=?", (ctx.guild.id, int(m.group(2))))
            db.commit()
            return await ctx.reply(f"✅ Match #{m.group(2)} removed." if cur.rowcount else
                                   f"There is no saved match #{m.group(2)}.", mention_author=False)
        team_a, team_b, error = self._teams_from(ctx.guild.id, text)
        if error:
            return await ctx.reply(error, mention_author=False, allowed_mentions=none)
        number = (db.execute("SELECT COALESCE(MAX(number), 0) FROM predict_matches WHERE guild_id=?",
                             (ctx.guild.id,)).fetchone()[0]) + 1
        db.execute("INSERT INTO predict_matches (guild_id, number, team_a_id, team_b_id, added_by) VALUES (?,?,?,?,?)",
                   (ctx.guild.id, number, int(team_a["id"]), int(team_b["id"]), ctx.author.id))
        db.commit()
        await ctx.reply(f"✅ Match **#{number}** saved: **{team_a['name']}** vs **{team_b['name']}**. "
                        "Use `+predict` to see the prediction.", mention_author=False, allowed_mentions=none)

    # ------------------------------------------------------------------ +predict
    @commands.command(name="predict", aliases=["prediction", "predictmatch"])
    @commands.guild_only()
    async def predict(self, ctx, *, text: str = ""):
        """The prediction card for a saved match: +predict (newest), +predict <number>, or +predict Team A | Team B."""
        if self.compare is None:
            return await ctx.reply("The team system isn't loaded right now.", mention_author=False)
        db, none = self.bot.db, discord.AllowedMentions.none()
        text = text.strip().lstrip("#")
        number = None
        if text and re.search(r"\||\s+vs\.?\s+", text, flags=re.I):
            team_a, team_b, error = self._teams_from(ctx.guild.id, text)
            if error:
                return await ctx.reply(error, mention_author=False, allowed_mentions=none)
        else:
            if text.isdigit():
                row = db.execute("SELECT * FROM predict_matches WHERE guild_id=? AND number=?",
                                 (ctx.guild.id, int(text))).fetchone()
            elif not text:
                row = db.execute("SELECT * FROM predict_matches WHERE guild_id=? ORDER BY number DESC LIMIT 1",
                                 (ctx.guild.id,)).fetchone()
            else:
                return await ctx.reply("Use `+predict`, `+predict <match number>` or `+predict Team A | Team B`.",
                                       mention_author=False)
            if row is None:
                return await ctx.reply("No saved match found. An admin can add one with `+addmatch Team A | Team B`.",
                                       mention_author=False)
            team_a, team_b = self._team_by_id(row["team_a_id"]), self._team_by_id(row["team_b_id"])
            if team_a is None or team_b is None:
                return await ctx.reply("One of the teams in that match no longer exists.", mention_author=False)
            number = int(row["number"])
        wait = on_cooldown(ctx.author.id)
        if wait:
            return await ctx.reply(f"Please wait {wait:.0f}s before drawing another image.", mention_author=False)
        async with self._lock, ctx.typing():
            try:
                png = await self._card(ctx, team_a, team_b, number)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[predict] failed: {type(exc).__name__}: {exc}", flush=True)
                return await ctx.reply("I couldn't build the prediction. Check the bot logs.", mention_author=False)
        await ctx.send(file=discord.File(io.BytesIO(png), filename="prediction.png"))


async def setup(bot):
    await bot.add_cog(Predictions(bot))
