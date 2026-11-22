"""Team vs team comparison for Googli Manager (image version).

+compareteams Team A | Team B     compare two teams (separate them with | or vs)
+compareteams Team B              your own team against Team B

Team names work like +teaminfo: the full name, or at least the first 4 letters.

Page 1 compares batting, bowling, fielding and the tournament record. Page 2 shows each
team's top run scorer / wicket taker / fielder and the recent matches between the two
teams. The background is split in the two teams' Discord role colours (the logo colour
is used for a team that has no role colour).

Numbers use the same rules as +teaminfo: the whole team history, with every match
counted for the team a player was on when it was played (transfers respected).
"""

import asyncio
import io
import re
import sqlite3
import time

import discord
from discord.ext import commands

from .h2h import ImagePager, fetch_avatar, team_role_color
from .versus_image import (
    BAT_COLOR, BOWL_COLOR, FIELD_COLOR, RECORD_COLOR, render_compare_pages, stat_row,
)

COOLDOWN_SECONDS = 8
USAGE = (
    "Usage: `+compareteams Team A | Team B`  (or `Team A vs Team B`)\n"
    "Or `+compareteams Team B` to compare your own team with Team B.\n"
    "Team names work like `+teaminfo`: at least the first 4 letters."
)


class CompareTeams(commands.Cog):
    """+compareteams: two teams side by side."""

    def __init__(self, bot):
        self.bot = bot
        self._last_use = {}

    @property
    def core(self):
        """The team cog (teams1.py), which owns the team database and the stat helpers."""
        return self.bot.get_cog("TeamRegistration")

    # ------------------------------------------------------------------ lookup
    def _resolve(self, guild_id, query):
        team, error = self.core._resolve_team(guild_id, query)
        shown = discord.utils.escape_markdown(discord.utils.escape_mentions(query))
        if error == "short":
            return None, f"Enter at least the first **4 letters** of each team name (`{shown}` is too short)."
        if error == "multiple":
            names = "\n".join(f"• {row['name']}" for row in team[:15])
            return None, f"More than one team matches `{shown}`. Type more letters:\n{names}"
        if not team:
            return None, f"I couldn't find an approved team matching `{shown}`."
        return team, None

    # ------------------------------------------------------------------ data
    def _meetings(self, guild_id, name_a, name_b, limit=4):
        """Recent matches where both teams played, from the team names saved at import time.

        Each team's score is the sum of its players' runs. The team with more runs is shown as
        the winner, so a match decided some other way (for example a super over) can look tied.
        """
        try:
            rows = self.bot.db.execute(
                """SELECT mt.source_message_id AS mid, mt.team_name AS team,
                          SUM(p.runs) AS runs, i.imported_at AS ts
                   FROM match_teams mt
                   JOIN player_match_stats p
                     ON p.source_message_id = mt.source_message_id AND p.user_id = mt.user_id
                   JOIN stats_imports i ON i.source_message_id = mt.source_message_id
                   WHERE i.guild_id = ?
                     AND COALESCE(i.competition, 'MAIN TOURNAMENT') = 'MAIN TOURNAMENT'
                     AND (mt.team_name = ? COLLATE NOCASE OR mt.team_name = ? COLLATE NOCASE)
                   GROUP BY mt.source_message_id, mt.team_name""",
                (int(guild_id), str(name_a), str(name_b)),
            ).fetchall()
        except sqlite3.Error:
            return []
        by_match = {}
        for row in rows:
            entry = by_match.setdefault(int(row["mid"]), {"ts": float(row["ts"])})
            entry[str(row["team"]).casefold()] = int(row["runs"] or 0)
        key_a, key_b = str(name_a).casefold(), str(name_b).casefold()
        meetings = [
            {"ts": entry["ts"], "a": entry[key_a], "b": entry[key_b]}
            for entry in by_match.values() if key_a in entry and key_b in entry
        ]
        meetings.sort(key=lambda m: -m["ts"])
        return meetings[:limit]

    def _display_name(self, guild, uid):
        member = guild.get_member(int(uid))
        if member is not None:
            return member.display_name
        name = self.core._username(guild, int(uid))
        return f"Player {str(uid)[-4:]}" if name == str(uid) else name

    async def _side(self, ctx, team):
        """Everything one side of the comparison needs."""
        from .lb import _totals
        from .stats import fantasy_raw_points
        from .teaminfo_image import accent_from_logo
        from .teaminfo_image import _open

        core, guild = self.core, ctx.guild
        player_rows = core.db.execute(
            "SELECT user_id FROM team_players WHERE team_id=? ORDER BY added_at", (team["id"],)
        ).fetchall()
        ordered = list(dict.fromkeys(
            [int(team["captain_id"])]
            + ([int(team["vice_captain_id"])] if team["vice_captain_id"] else [])
            + [int(row["user_id"]) for row in player_rows]
        ))
        try:   # players who used to play for this team still count towards its totals
            historical = core.db.execute(
                "SELECT DISTINCT user_id FROM player_team_transfers "
                "WHERE guild_id=? AND (old_team_id=? OR new_team_id=?)",
                (int(guild.id), int(team["id"]), int(team["id"])),
            ).fetchall()
            total_ids = list(dict.fromkeys(ordered + [int(row[0]) for row in historical]))
        except sqlite3.Error:
            total_ids = ordered

        stat_rows = core._team_stat_rows(guild.id, team)
        fielding = core._team_fielding_totals(guild.id, team)

        team_rows = [r for uid in total_ids for r in stat_rows.get(uid, [])]
        totals = _totals(team_rows)
        catches = sum(fielding.get(uid, (0, 0))[0] for uid in total_ids)
        drops = sum(fielding.get(uid, (0, 0))[1] for uid in total_ids)

        players = []
        for uid in total_ids:
            rows = stat_rows.get(uid, [])
            caught, dropped = fielding.get(uid, (0, 0))
            if not rows and not (caught or dropped):
                continue
            t = _totals(rows)
            spells = [r for r in rows if r["balls_bowled"] > 0]
            best = max(spells, key=lambda r: (r["wickets"], -r["runs_conceded"]), default=None)
            players.append({
                "uid": uid, "runs": t["runs"], "wickets": t["wickets"],
                "bat_avg": t["bat_avg"], "strike_rate": t["strike_rate"],
                "economy": t["economy"] if t["balls_bowled"] else None,
                "best": f"{best['wickets']}/{best['runs_conceded']}" if best else "—",
                "catches": int(caught), "dropped": int(dropped),
                "fantasy": fantasy_raw_points(rows)[2] + caught * 5 - dropped * 10,
            })

        def top(key):
            pool = [p for p in players if p[key] > 0]
            pool.sort(key=lambda p: (-p[key], -p["fantasy"], p["uid"]))
            return pool[0] if pool else None

        picks = {"runs": top("runs"), "wickets": top("wickets"), "catches": top("catches")}

        logo_task = core._team_logo_bytes(ctx, team)

        async def avatar_of(player):
            if player is None:
                return None
            member = guild.get_member(player["uid"]) or self.bot.get_user(player["uid"])
            if member is None:
                try:
                    member = await self.bot.fetch_user(player["uid"])
                except discord.HTTPException:
                    member = None
            return await fetch_avatar(self.bot, member)

        logo, *avatars = await asyncio.gather(
            logo_task, *(avatar_of(picks[key]) for key in ("runs", "wickets", "catches"))
        )
        avatar_by_key = dict(zip(("runs", "wickets", "catches"), avatars))

        color = team_role_color(core, guild, team)
        if color is None:
            color = accent_from_logo(_open(logo))

        standing = None
        points_cog = self.bot.get_cog("PointsTable")
        if points_cog is not None and hasattr(points_cog, "team_standing"):
            try:
                standing = points_cog.team_standing(guild.id, team["name"])
            except Exception:
                standing = None

        max_players = core._max_players(guild.id)
        return {
            "team": team, "name": str(team["name"]), "logo": logo, "color": color,
            "standing": standing, "roster": f"ROSTER {len(ordered)}/{max_players}",
            "has_rows": bool(team_rows), "totals": totals,
            "highest": max((r["runs"] for r in team_rows), default=0),
            "catches": catches, "drops": drops,
            "fantasy": sum(p["fantasy"] for p in players),
            "picks": picks, "avatars": avatar_by_key,
        }

    # ------------------------------------------------------------------ comparison rows
    @staticmethod
    def _record(side):
        """(played, wins, win rate %, points) from the points table, or Nones without a standing."""
        standing = side["standing"]
        if not standing:
            return None, None, None, None
        played = int(standing["won"]) + int(standing["lost"])
        wins = int(standing["won"])
        return played, wins, (wins / played * 100 if played else None), int(standing["points"])

    def _groups(self, a, b):
        ta, tb = a["totals"], b["totals"]

        def pick(side, value, condition=True):
            return value if (side["has_rows"] and condition) else None

        def efficiency(side):
            chances = side["catches"] + side["drops"]
            return side["catches"] / chances * 100 if chances else None

        batting = [
            stat_row("Runs", ta["runs"], tb["runs"], "int", "high"),
            stat_row("Average", pick(a, ta["bat_avg"]), pick(b, tb["bat_avg"]), "num1", "high"),
            stat_row("Strike rate", pick(a, ta["strike_rate"], ta["balls_faced"] > 0),
                     pick(b, tb["strike_rate"], tb["balls_faced"] > 0), "num1", "high"),
            stat_row("Top score", pick(a, a["highest"]), pick(b, b["highest"]), "int", "high"),
        ]
        bowling = [
            stat_row("Wickets", ta["wickets"], tb["wickets"], "int", "high"),
            stat_row("Economy", pick(a, ta["economy"], ta["balls_bowled"] > 0),
                     pick(b, tb["economy"], tb["balls_bowled"] > 0), "num2", "low"),
            stat_row("Average", pick(a, ta["bowl_avg"], ta["wickets"] > 0),
                     pick(b, tb["bowl_avg"], tb["wickets"] > 0), "num1", "low"),
            stat_row("Strike rate", pick(a, ta["bowl_sr"], ta["wickets"] > 0),
                     pick(b, tb["bowl_sr"], tb["wickets"] > 0), "num1", "low"),
        ]
        fielding = [
            stat_row("Catches", a["catches"], b["catches"], "int", "high"),
            stat_row("Drops", a["drops"], b["drops"], "int", "low"),
            stat_row("Efficiency", efficiency(a), efficiency(b), "pct", "high"),
            stat_row("Fantasy pts", a["fantasy"], b["fantasy"], "int", "high"),
        ]
        pa, wa, ra, pta = self._record(a)
        pb, wb, rb, ptb = self._record(b)
        record = [
            stat_row("Played", pa, pb, "int", None),
            stat_row("Wins", wa, wb, "int", "high"),
            stat_row("Win rate", ra, rb, "pct", "high"),
            stat_row("Points", pta, ptb, "int", "high"),
        ]
        return [
            {"title": "BATTING", "color": BAT_COLOR, "rows": batting},
            {"title": "BOWLING", "color": BOWL_COLOR, "rows": bowling},
            {"title": "FIELDING", "color": FIELD_COLOR, "rows": fielding},
            {"title": "RECORD", "color": RECORD_COLOR, "rows": record},
        ]

    def _leader(self, guild, side, key, avatar_key):
        player = side["picks"][avatar_key]
        if player is None:
            return None
        name = self._display_name(guild, player["uid"])
        if key == "runs":
            sub = f"AVG {player['bat_avg']:.1f}  •  SR {player['strike_rate']:.1f}"
            value, unit = f"{player['runs']:,}", "RUNS"
        elif key == "wickets":
            econ = f"{player['economy']:.1f}" if player["economy"] is not None else "—"
            sub = f"ECON {econ}  •  BEST {player['best']}"
            value, unit = str(player["wickets"]), "WICKETS"
        else:
            sub = f"DROPPED {player['dropped']}"
            value, unit = str(player["catches"]), "CATCHES"
        return {
            "name": name, "avatar": side["avatars"][avatar_key], "value": value, "unit": unit,
            "sub": sub, "number": player[key],
        }

    def _leaders(self, guild, a, b):
        leaders = []
        for label, color, key in (
            ("TOP RUN SCORER", BAT_COLOR, "runs"),
            ("TOP WICKET TAKER", BOWL_COLOR, "wickets"),
            ("TOP FIELDER", FIELD_COLOR, "catches"),
        ):
            leaders.append({
                "label": label, "color": color,
                "a": self._leader(guild, a, key, key), "b": self._leader(guild, b, key, key),
            })
        return leaders

    # ------------------------------------------------------------------ command
    @commands.command(name="compareteams", aliases=["teamcompare", "cmpteams"])
    @commands.guild_only()
    async def compare_teams(self, ctx, *, teams: str = ""):
        """Compare two teams: +compareteams Team A | Team B"""
        core = self.core
        if core is None:
            await ctx.reply("The team system isn't loaded right now.", mention_author=False)
            return

        parts = [p.strip() for p in re.split(r"\s*\|\s*|\s+vs\.?\s+", teams.strip(), flags=re.IGNORECASE) if p.strip()]
        if len(parts) == 1:
            mine = core._membership(ctx.guild.id, ctx.author.id)
            if not mine:
                await ctx.reply(USAGE, mention_author=False)
                return
            parts = [str(mine["name"]), parts[0]]
        if len(parts) != 2:
            await ctx.reply(USAGE, mention_author=False)
            return

        first, second = None, None
        for index, query in enumerate(parts):
            team, error = self._resolve(ctx.guild.id, query)
            if error:
                await ctx.reply(error, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                return
            if index == 0:
                first = team
            else:
                second = team
        if int(first["id"]) == int(second["id"]):
            await ctx.reply("Pick two different teams to compare.", mention_author=False)
            return

        wait = COOLDOWN_SECONDS - (time.monotonic() - self._last_use.get(ctx.author.id, -1e9))
        if wait > 0:
            await ctx.reply(f"Please wait {wait:.0f}s before comparing again.", mention_author=False)
            return
        self._last_use[ctx.author.id] = time.monotonic()

        loading = await ctx.reply("⏳ Building the team comparison...", mention_author=False)
        try:
            a, b = await self._side(ctx, first), await self._side(ctx, second)
            data = {
                "a": {k: a[k] for k in ("name", "logo", "color", "standing", "roster")},
                "b": {k: b[k] for k in ("name", "logo", "color", "standing", "roster")},
                "groups": self._groups(a, b),
                "leaders": self._leaders(ctx.guild, a, b),
                "meetings": self._meetings(ctx.guild.id, a["name"], b["name"]),
                "guild": ctx.guild.name,
            }
            pngs = await asyncio.to_thread(render_compare_pages, data)
            view = ImagePager(ctx, pngs, ("Comparison", "Top performers"), "compareteams.png")
            view.message = await loading.edit(
                content=None,
                attachments=[discord.File(io.BytesIO(pngs[0]), filename="compareteams.png")],
                view=view,
            )
        except Exception as exc:
            print(f"[compareteams] image generation failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                await loading.delete()
            except Exception:
                pass
            await ctx.send("I couldn't generate the team comparison. Check the bot logs.")


async def setup(bot):
    await bot.add_cog(CompareTeams(bot))
