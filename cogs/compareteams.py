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
from datetime import datetime

import discord
from discord.ext import commands

from .h2h import (
    ImagePager, contrasting_overall_color, fetch_avatar, on_cooldown, overall_color_for, render_png,
    team_role_color,
)
from .versus_image import (
    BAT_COLOR, BOWL_COLOR, FIELD_COLOR, RECORD_COLOR, render_compare_page, render_compare_pages, stat_row,
)

USAGE = (
    "Usage: `+compareteams Team A | Team B`  (or `Team A vs Team B`)\n"
    "`+ctall` (or `+ct overall`) compares your team with the average of all other teams.\n"
    "Or `+compareteams Team B` to compare your own team with Team B.\n"
    "Team names work like `+teaminfo`: at least the first 4 letters."
)


_LOGO_CACHE = {}


def _shrink_logo(raw, size=420):
    """A small copy of a team logo. Saved logos can be several MB; every page decodes them again."""
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        image = image.convert("RGBA")
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="PNG", compress_level=1)
        return out.getvalue()


def _innings_best(rows):
    """Highest team total in one innings: the runs of the team's players added up per match."""
    per_match = {}
    for row in rows:
        mid = row.get("mid")
        if mid is not None:
            per_match[mid] = per_match.get(mid, 0) + int(row["runs"])
    return max(per_match.values(), default=0)


def _smooth(wins, played):
    """Win rate pulled towards 50% so one or two matches can't mean 0% or 100%."""
    return (wins + 2) / (played + 4) if played else 0.5


def _log5(x, y):
    """Chance that a side with strength x beats a side with strength y (both 0..1)."""
    top = x * (1 - y)
    bottom = top + y * (1 - x)
    return top / bottom if bottom else 0.5


def _edge(diff, scale):
    """Chance for side A from a difference in some stat (positive = A better); 50% when equal."""
    import math
    return 1 / (1 + math.exp(-diff / scale))


def _rel(a, b, higher=True, scale=0.3):
    """Chance for A from one stat, judged by the RELATIVE gap (30% better ~ 73%). None without data."""
    if a is None or b is None:
        return None
    mean = (abs(a) + abs(b)) / 2
    if mean == 0 or a == b:
        return 0.5
    diff = (a - b) / mean
    return _edge(diff if higher else -diff, scale)


def _blend(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _gini(values):
    """0 = every player contributes the same, towards 1 = one player does everything.
    None with fewer than 3 players or no points at all."""
    ordered = sorted(max(0.0, float(v)) for v in values)
    n, total = len(ordered), sum(ordered)
    if n < 3 or total <= 0:
        return None
    return sum((2 * (i + 1) - n - 1) * x for i, x in enumerate(ordered)) / (n * total)


def predict(a, b):
    """Win chance when side a faces side b, from what each team has actually done.

    Each category is the average of several stats, every stat judged by how far apart the two
    teams are in relative terms, so nothing rests on a single number:
      record    win rate (Log5, smoothed) and points per match played                       30%
      batting   runs per match, average, strike rate                                        25%
      bowling   wickets per match, economy, bowling average, bowling strike rate            25%
      fielding  catches per match, catch efficiency, (fewer) drops per match                10%
      balance   how evenly the team's fantasy points are spread over its players            10%
    "Balance" is not drawn on the card: a team that leans on one or two players (a big gap
    between its best and its other players) is rated lower than an even squad.
    A category without data drops out and the others share its weight. 5%-95%.
    Returns {"a", "b", "known", "factors": [{label, chance, rows}], "balance": chance_a or None}.
    """
    pa, wa, ra, pta = CompareTeams._record(a)
    pb, wb, rb, ptb = CompareTeams._record(b)

    record = _blend([
        _log5(_smooth(wa or 0, pa or 0), _smooth(wb or 0, pb or 0)) if (pa and pb) else None,
        _rel(pta / pa, ptb / pb) if (pa and pb and pta is not None and ptb is not None) else None,
    ])

    def per_match(side, value):
        m = side.get("matches") or 0
        return value / m if m else None

    ta, tb = a["totals"], b["totals"]
    bat_a, bat_b = a.get("has_rows") and ta["balls_faced"] > 0, b.get("has_rows") and tb["balls_faced"] > 0
    batting = _blend([
        _rel(per_match(a, ta["runs"]), per_match(b, tb["runs"])),
        _rel(ta["bat_avg"], tb["bat_avg"]),
        _rel(ta["strike_rate"], tb["strike_rate"]),
    ]) if (bat_a and bat_b) else None

    bowl_a, bowl_b = a.get("has_rows") and ta["balls_bowled"] > 0, b.get("has_rows") and tb["balls_bowled"] > 0
    both_wk = ta["wickets"] > 0 and tb["wickets"] > 0
    bowling = _blend([
        _rel(per_match(a, ta["wickets"]), per_match(b, tb["wickets"])),
        _rel(ta["economy"], tb["economy"], higher=False),
        _rel(ta["bowl_avg"], tb["bowl_avg"], higher=False) if both_wk else None,
        _rel(ta["bowl_sr"], tb["bowl_sr"], higher=False) if both_wk else None,
    ]) if (bowl_a and bowl_b) else None

    def eff(side):
        chances = side["catches"] + side["drops"]
        return side["catches"] / chances * 100 if chances else None

    ea, eb = eff(a), eff(b)
    fielding = _blend([
        _rel(per_match(a, a["catches"]), per_match(b, b["catches"])),
        _rel(ea, eb),
        _rel(per_match(a, a["drops"]), per_match(b, b["drops"]), higher=False),
    ]) if (ea is not None and eb is not None) else None

    ga, gb = a.get("gini"), b.get("gini")
    balance = _edge(gb - ga, 0.25) if (ga is not None and gb is not None) else None

    parts = [(0.30, record), (0.25, batting), (0.25, bowling), (0.10, fielding), (0.10, balance)]
    live = [(w, v) for w, v in parts if v is not None]
    base = sum(w * v for w, v in live) / sum(w for w, _ in live) if live else 0.5
    base = min(0.95, max(0.05, base))

    row = stat_row
    factors = [
        {"label": "RECORD", "chance": record, "rows": [
            row("Played", pa, pb, "int", None),
            row("Wins", wa, wb, "int", "high"),
            row("Win rate", ra, rb, "pct", "high"),
        ]},
        {"label": "BATTING", "chance": batting, "rows": [
            row("Runs", ta["runs"] if bat_a else None, tb["runs"] if bat_b else None, "int", "high"),
            row("Average", ta["bat_avg"] if bat_a else None, tb["bat_avg"] if bat_b else None, "num1", "high"),
            row("Strike rate", ta["strike_rate"] if bat_a else None, tb["strike_rate"] if bat_b else None,
                "num1", "high"),
        ]},
        {"label": "BOWLING", "chance": bowling, "rows": [
            row("Wickets", ta["wickets"] if bowl_a else None, tb["wickets"] if bowl_b else None, "int", "high"),
            row("Economy", ta["economy"] if bowl_a else None, tb["economy"] if bowl_b else None, "num2", "low"),
            row("Average", ta["bowl_avg"] if ta["wickets"] else None, tb["bowl_avg"] if tb["wickets"] else None,
                "num1", "low"),
        ]},
        {"label": "FIELDING", "chance": fielding, "rows": [
            row("Catches", a["catches"], b["catches"], "int", "high"),
            row("Drops", a["drops"], b["drops"], "int", "low"),
            row("Catch rate", ea, eb, "pct", "high",
                f"{ea:.0f}%" if ea is not None else None, f"{eb:.0f}%" if eb is not None else None),
        ]},
    ]
    return {"a": base, "b": 1 - base, "known": bool(live), "factors": factors, "balance": balance}


class CompareTeams(commands.Cog):
    """+compareteams: two teams side by side."""

    def __init__(self, bot):
        self.bot = bot
        self._numbers_cache = {}     # team id -> (time, numbers) for the +ctall average
        self._index_cache = {}       # guild id -> (time, rows of every team) read once

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
    def _meetings(self, guild_id, name_a, name_b, limit=None):
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
        return meetings[:limit] if limit else meetings

    def _guild_rows(self, guild_id):
        """Batting/bowling rows and fielding totals of EVERY team, read from the database once (kept 2 minutes).

        Same rule as +teaminfo (the team a player was on when the match was played), but one
        pass over the stats for the whole server instead of one full pass per team, which is
        what used to peg the CPU, above all for +ct overall.
        Returns {"stats": {team name (casefolded): {user id: [rows]}}, "fielding": {team: {user id: (caught, dropped)}}}.
        """
        hit = self._index_cache.get(int(guild_id))
        if hit and time.monotonic() - hit[0] < 120:
            return hit[1]
        core, db = self.core, self.bot.db
        transfers = {}
        try:
            for r in core.db.execute(
                "SELECT user_id, old_team_name, new_team_name, transferred_at FROM player_team_transfers "
                "WHERE guild_id=? ORDER BY transferred_at ASC", (int(guild_id),)
            ).fetchall():
                try:
                    when = datetime.fromisoformat(str(r["transferred_at"]).replace("Z", "+00:00")).timestamp()
                except (TypeError, ValueError, OverflowError):
                    when = None
                transfers.setdefault(int(r["user_id"]), []).append(
                    (str(r["old_team_name"]), str(r["new_team_name"]), when))
        except sqlite3.Error:
            pass
        current = {}
        try:
            for t in core.db.execute(
                "SELECT id, name, captain_id, vice_captain_id FROM registered_teams "
                "WHERE guild_id=? AND status='approved'", (int(guild_id),)
            ).fetchall():
                for uid in (t["captain_id"], t["vice_captain_id"]):
                    if uid:
                        current.setdefault(int(uid), str(t["name"]))
            for r in core.db.execute(
                "SELECT tp.user_id, t.name FROM team_players tp JOIN registered_teams t ON t.id=tp.team_id "
                "WHERE t.guild_id=? AND t.status='approved'", (int(guild_id),)
            ).fetchall():
                current.setdefault(int(r["user_id"]), str(r["name"]))
        except sqlite3.Error:
            pass
        def team_of(uid, mid, imported_at, saved):
            """Name of the team the player was on when the match was played (None if unknown)."""
            history = transfers.get(uid)
            if not history:
                name = str(saved) if saved else None
            else:
                match_at = core._team_match_time(mid, imported_at)
                name = history[0][0]
                for _old, new, when in history:
                    if when is None:
                        continue
                    if match_at >= when:
                        name = new
                    else:
                        break
            return name or current.get(uid)

        stats, fielding = {}, {}
        try:
            rows = db.execute(
                """SELECT p.source_message_id, p.user_id, p.runs, p.balls_faced,
                          p.runs_conceded, p.balls_bowled, p.wickets, p.not_out,
                          i.imported_at, mt.team_name
                   FROM player_match_stats p
                   JOIN stats_imports i ON i.source_message_id=p.source_message_id
                   LEFT JOIN match_teams mt
                     ON mt.source_message_id=p.source_message_id AND mt.user_id=p.user_id
                   WHERE i.guild_id=?
                     AND COALESCE(i.competition, 'MAIN TOURNAMENT')='MAIN TOURNAMENT'""",
                (int(guild_id),),
            ).fetchall()
        except Exception:
            rows = []
        for row in rows:
            uid = int(row["user_id"])
            name = team_of(uid, row["source_message_id"], row["imported_at"], row["team_name"])
            if not name:
                continue
            stats.setdefault(name.casefold(), {}).setdefault(uid, []).append({
                "mid": int(row["source_message_id"]),
                "runs": int(row["runs"]), "balls_faced": int(row["balls_faced"]),
                "runs_conceded": int(row["runs_conceded"]), "balls_bowled": int(row["balls_bowled"]),
                "wickets": int(row["wickets"]), "not_out": int(row["not_out"]),
            })

        # Fielding: the same rule as +teaminfo (match rows by team at the time; players who were
        # never transferred use their live totals).
        try:
            rows = db.execute(
                """SELECT f.source_message_id, f.user_id, f.catches_taken, f.catches_dropped,
                          i.imported_at, mt.team_name
                   FROM player_match_fielding f
                   JOIN stats_imports i ON i.source_message_id=f.source_message_id
                   LEFT JOIN match_teams mt
                     ON mt.source_message_id=f.source_message_id AND mt.user_id=f.user_id
                   WHERE i.guild_id=?
                     AND COALESCE(i.competition, 'MAIN TOURNAMENT')='MAIN TOURNAMENT'
                     AND (f.catches_taken>0 OR f.catches_dropped>0)""",
                (int(guild_id),),
            ).fetchall()
        except Exception:
            rows = []
        for row in rows:
            uid = int(row["user_id"])
            name = team_of(uid, row["source_message_id"], row["imported_at"], row["team_name"])
            if not name:
                continue
            per_team = fielding.setdefault(name.casefold(), {})
            caught, dropped = per_team.get(uid, (0, 0))
            per_team[uid] = (caught + int(row["catches_taken"]), dropped + int(row["catches_dropped"]))
        try:
            for r in db.execute(
                "SELECT user_id, catches_caught, catches_dropped FROM fielding_stats WHERE guild_id=?",
                (int(guild_id),),
            ).fetchall():
                uid = int(r["user_id"])
                if uid in current and uid not in transfers:
                    fielding.setdefault(current[uid].casefold(), {})[uid] = (
                        int(r["catches_caught"]), int(r["catches_dropped"]))
        except Exception:
            pass

        # The same rows regrouped per player (all teams he has played for), for +xi / +cxi.
        players, field_players = {}, {}
        for per_team in stats.values():
            for uid, rows in per_team.items():
                players.setdefault(uid, []).extend(rows)
        for per_team in fielding.values():
            for uid, (caught, dropped) in per_team.items():
                c0, d0 = field_players.get(uid, (0, 0))
                field_players[uid] = (c0 + caught, d0 + dropped)
        sizes = []
        try:
            sizes = sorted(int(r[0]) for r in db.execute(
                """SELECT COUNT(*) FROM match_teams mt
                   JOIN stats_imports i ON i.source_message_id=mt.source_message_id
                   WHERE i.guild_id=? AND COALESCE(i.competition, 'MAIN TOURNAMENT')='MAIN TOURNAMENT'
                   GROUP BY mt.source_message_id, mt.team_name""", (int(guild_id),)).fetchall())
        except Exception:
            pass
        typical = sizes[len(sizes) // 2] if sizes else 0      # usual number of players in a team per match

        result = {"stats": stats, "fielding": fielding, "players": players,
                  "fielding_players": field_players, "typical": typical}
        self._index_cache[int(guild_id)] = (time.monotonic(), result)
        return result

    async def _logo_for(self, ctx, team):
        """The team logo as a small PNG (the big saved copy is decoded once, then remembered)."""
        try:
            saved = team["logo_data"]
        except (KeyError, IndexError):
            saved = None
        try:
            url = team["logo_url"]
        except (KeyError, IndexError):
            url = None
        key = (int(team["id"]), len(saved) if saved else -1, url)
        if key in _LOGO_CACHE:
            return _LOGO_CACHE[key]
        raw = await self.core._team_logo_bytes(ctx, team)
        small = None
        if raw:
            try:
                small = await asyncio.to_thread(_shrink_logo, raw)
            except Exception:
                small = raw
        if len(_LOGO_CACHE) >= 8:
            _LOGO_CACHE.pop(next(iter(_LOGO_CACHE)))
        _LOGO_CACHE[key] = small
        return small

    def _display_name(self, guild, uid):
        member = guild.get_member(int(uid))
        if member is not None:
            return member.name
        name = self.core._username(guild, int(uid))
        return f"Player {str(uid)[-4:]}" if name == str(uid) else name

    async def _side(self, ctx, team, avatars=True):
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

        index = self._guild_rows(guild.id)
        key = str(team["name"]).casefold()
        stat_rows, fielding = index["stats"].get(key, {}), index["fielding"].get(key, {})

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
        gini = _gini([p["fantasy"] for p in players])

        logo_task = self._logo_for(ctx, team)

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
            logo_task, *(avatar_of(picks[key]) if avatars else asyncio.sleep(0, None)
                         for key in ("runs", "wickets", "catches"))
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
            "highest": _innings_best(team_rows),
            "catches": catches, "drops": drops,
            "fantasy": sum(p["fantasy"] for p in players),
            "picks": picks, "avatars": avatar_by_key,
            "matches": len({r["mid"] for r in team_rows}), "gini": gini,
        }

    # ------------------------------------------------------------------ comparison rows
    @staticmethod
    def _record(side):
        """(played, wins, win rate %, points) from the points table, or Nones without a standing."""
        if side.get("record") is not None:      # the "all teams" average side
            return side["record"]
        standing = side["standing"]
        if not standing:
            return None, None, None, None
        played = int(standing["won"]) + int(standing["lost"])
        wins = int(standing["won"])
        return played, wins, (wins / played * 100 if played else None), int(standing["points"])

    def _groups(self, a, b, avg_b=False):
        """The four comparison cards. avg_b: side B is a per-team average, so its counts show a decimal."""
        ta, tb = a["totals"], b["totals"]

        def pick(side, value, condition=True):
            return value if (side["has_rows"] and condition) else None

        def count(label, av, bv, better="high"):
            if avg_b and bv is not None:
                return stat_row(label, av, bv, "int", better, None, f"{bv:,.1f}")
            return stat_row(label, av, bv, "int", better)

        def efficiency(side):
            chances = side["catches"] + side["drops"]
            return side["catches"] / chances * 100 if chances else None

        batting = [
            count("Runs", ta["runs"], tb["runs"]),
            stat_row("Average", pick(a, ta["bat_avg"]), pick(b, tb["bat_avg"]), "num1", "high"),
            stat_row("Strike rate", pick(a, ta["strike_rate"], ta["balls_faced"] > 0),
                     pick(b, tb["strike_rate"], tb["balls_faced"] > 0), "num1", "high"),
            count("Highest total", pick(a, a["highest"]), pick(b, b["highest"])),
        ]
        bowling = [
            count("Wickets", ta["wickets"], tb["wickets"]),
            stat_row("Economy", pick(a, ta["economy"], ta["balls_bowled"] > 0),
                     pick(b, tb["economy"], tb["balls_bowled"] > 0), "num2", "low"),
            stat_row("Average", pick(a, ta["bowl_avg"], ta["wickets"] > 0),
                     pick(b, tb["bowl_avg"], tb["wickets"] > 0), "num1", "low"),
            stat_row("Strike rate", pick(a, ta["bowl_sr"], ta["wickets"] > 0),
                     pick(b, tb["bowl_sr"], tb["wickets"] > 0), "num1", "low"),
        ]
        fielding = [
            count("Catches", a["catches"], b["catches"]),
            count("Drops", a["drops"], b["drops"], "low"),
            stat_row("Efficiency", efficiency(a), efficiency(b), "pct", "high"),
            count("Fantasy pts", a["fantasy"], b["fantasy"]),
        ]
        pa, wa, ra, pta = self._record(a)
        pb, wb, rb, ptb = self._record(b)
        record = [
            count("Played", pa, pb, None),
            count("Wins", wa, wb),
            stat_row("Win rate", ra, rb, "pct", "high"),
            count("Points", pta, ptb),
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

    # ------------------------------------------------------------------ overall (vs all other teams)
    def _team_numbers(self, guild, team):
        """Rows, fielding, fantasy and standing of one team (same rules as _side, no images)."""
        from .stats import fantasy_raw_points

        core = self.core
        player_rows = core.db.execute(
            "SELECT user_id FROM team_players WHERE team_id=? ORDER BY added_at", (team["id"],)
        ).fetchall()
        ids = [int(team["captain_id"])] + ([int(team["vice_captain_id"])] if team["vice_captain_id"] else []) \
            + [int(row["user_id"]) for row in player_rows]
        try:
            ids += [int(row[0]) for row in core.db.execute(
                "SELECT DISTINCT user_id FROM player_team_transfers "
                "WHERE guild_id=? AND (old_team_id=? OR new_team_id=?)",
                (int(guild.id), int(team["id"]), int(team["id"])),
            ).fetchall()]
        except sqlite3.Error:
            pass
        ids = list(dict.fromkeys(ids))
        index = self._guild_rows(guild.id)
        key = str(team["name"]).casefold()
        stat_rows, fielding = index["stats"].get(key, {}), index["fielding"].get(key, {})
        rows = [r for uid in ids for r in stat_rows.get(uid, [])]
        fantasy = sum(
            (fantasy_raw_points(stat_rows[uid])[2] if stat_rows.get(uid) else 0)
            + fielding.get(uid, (0, 0))[0] * 5 - fielding.get(uid, (0, 0))[1] * 10
            for uid in ids
        )
        standing = None
        points_cog = self.bot.get_cog("PointsTable")
        if points_cog is not None and hasattr(points_cog, "team_standing"):
            try:
                standing = points_cog.team_standing(guild.id, team["name"])
            except Exception:
                standing = None
        return {
            "rows": rows, "fantasy": fantasy, "standing": standing,
            "catches": sum(fielding.get(uid, (0, 0))[0] for uid in ids),
            "drops": sum(fielding.get(uid, (0, 0))[1] for uid in ids),
            "highest": _innings_best(rows),
        }

    def _numbers_for(self, guild, team):
        """_team_numbers, remembered for 2 minutes so repeated +ctall calls don't redo the work."""
        hit = self._numbers_cache.get(int(team["id"]))
        if hit and time.monotonic() - hit[0] < 120:
            return hit[1]
        numbers = self._team_numbers(guild, team)
        self._numbers_cache[int(team["id"])] = (time.monotonic(), numbers)
        return numbers

    async def _overall_side(self, guild, own_team):
        """Average of every OTHER approved team: counts are per team, rates are pooled over all of them."""
        from .lb import _totals

        teams = self.core.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved'", (int(guild.id),)
        ).fetchall()
        numbers = []
        for t in teams:
            if int(t["id"]) == int(own_team["id"]):
                continue
            numbers.append(self._numbers_for(guild, t))
            await asyncio.sleep(0)            # let the bot answer other things between teams
        numbers = [x for x in numbers if x["rows"]]
        n = len(numbers)
        if n == 0:
            return None
        pooled = _totals([r for x in numbers for r in x["rows"]])
        totals = dict(pooled)
        for key in ("runs", "balls_faced", "runs_conceded", "balls_bowled", "wickets"):
            totals[key] = pooled[key] / n

        def mean(key):
            return sum(x[key] for x in numbers) / n

        record = None
        standings = [x["standing"] for x in numbers if x["standing"]]
        if standings:
            played = sum(int(s["won"]) + int(s["lost"]) for s in standings)
            wins = sum(int(s["won"]) for s in standings)
            m = len(standings)
            record = (played / m, wins / m, wins / played * 100 if played else None,
                      sum(int(s["points"]) for s in standings) / m)
        return {
            "name": "All teams", "logo": None, "color": None, "standing": None,
            "roster": f"AVERAGE OF {n} TEAMS", "has_rows": True, "totals": totals,
            "highest": mean("highest"), "catches": mean("catches"), "drops": mean("drops"),
            "fantasy": mean("fantasy"), "record": record,
        }

    async def _run_overall(self, ctx, query=None):
        core = self.core
        if core is None:
            await ctx.reply("The team system isn't loaded right now.", mention_author=False)
            return
        if query:
            team, error = self._resolve(ctx.guild.id, query)
            if error:
                await ctx.reply(error, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                return
        else:
            team = core._membership(ctx.guild.id, ctx.author.id)
            if not team:
                await ctx.reply(
                    "You're not in a team. Use `+ctall Team Name` to compare a specific team with all others.",
                    mention_author=False,
                )
                return
        wait = on_cooldown(ctx.author.id)
        if wait:
            await ctx.reply(f"Please wait {wait:.0f}s before comparing again.", mention_author=False)
            return

        loading = await ctx.reply("⏳ Building the overall comparison...", mention_author=False)
        try:
            a = await self._side(ctx, team)
            b = await self._overall_side(ctx.guild, team)
            if b is None:
                await loading.edit(content="No other team has recorded matches yet.")
                return
            b["color"] = contrasting_overall_color(await overall_color_for(self.bot, ctx), a["color"])
            data = {
                "a": {k: a[k] for k in ("name", "logo", "color", "standing", "roster")},
                "b": {k: b[k] for k in ("name", "logo", "color", "standing", "roster")},
                "groups": self._groups(a, b, avg_b=True),
                "leaders": [], "meetings": [], "guild": ctx.guild.name, "caption": "Vs all teams",
            }
            png = (await render_png(render_compare_pages, data, True))[0]
            await loading.edit(content=None, attachments=[discord.File(io.BytesIO(png), filename="ctall.png")])
        except Exception as exc:
            print(f"[ctall] failed: {type(exc).__name__}: {exc}", flush=True)
            try:
                await loading.edit(content="I couldn't generate the overall comparison. Check the bot logs.")
            except Exception:
                pass

    @commands.command(name="ctall", aliases=["ctoverall"])
    @commands.guild_only()
    async def ct_all(self, ctx, *, team: str = ""):
        """Your team (or the named team) vs the average of all other teams."""
        await self._run_overall(ctx, team.strip() or None)

    # ------------------------------------------------------------------ command
    @commands.command(name="compareteams", aliases=["ct", "teamcompare", "cmpteams"])
    @commands.guild_only()
    async def compare_teams(self, ctx, *, teams: str = ""):
        """Compare two teams: +compareteams Team A | Team B"""
        core = self.core
        if core is None:
            await ctx.reply("The team system isn't loaded right now.", mention_author=False)
            return

        words = teams.strip().split()
        if words and words[0].lower() in ("overall", "all"):       # +ct overall [Team]
            await self._run_overall(ctx, " ".join(words[1:]) or None)
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

        wait = on_cooldown(ctx.author.id)
        if wait:
            await ctx.reply(f"Please wait {wait:.0f}s before comparing again.", mention_author=False)
            return

        loading = await ctx.reply("⏳ Building the team comparison...", mention_author=False)
        try:
            a, b = await self._side(ctx, first), await self._side(ctx, second)
            meetings = self._meetings(ctx.guild.id, a["name"], b["name"])
            prediction = predict(a, b)
            data = {
                "a": {k: a[k] for k in ("name", "logo", "color", "standing", "roster")},
                "b": {k: b[k] for k in ("name", "logo", "color", "standing", "roster")},
                "groups": self._groups(a, b),
                "leaders": self._leaders(ctx.guild, a, b),
                "meetings": meetings[:4],
                "win_prob": prediction,
                "guild": ctx.guild.name,
            }
            png = await render_png(render_compare_page, data, 0)
            view = ImagePager(ctx, png, 2, lambda index: render_compare_page(data, index),
                              ("Comparison", "Top performers"), "compareteams.png")
            view.message = await loading.edit(
                content=None,
                attachments=[discord.File(io.BytesIO(png), filename="compareteams.png")],
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
