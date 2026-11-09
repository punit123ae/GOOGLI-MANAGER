"""Kashmir Titans paginated leaderboard for GOOGLI MANAGER.

Uses the existing stats_imports/player_match_stats tables. +lb now sends the
Kashmir Titans card image (cogs/lb_card.py) for every category and statistic.
"""
from __future__ import annotations

import asyncio
import io
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

# Use the exact fantasy-point formula that +stats uses (batting + bowling).
from .stats import fantasy_raw_points
from .lb_card import render_card

BLUE = discord.Colour.from_rgb(20, 117, 255)

# Batter-impact metrics live in the Batting category but come from batfds' table.
# Value = index into (runs_after_drops, runs_without_drops, catches_dropped).
BATTER_IMPACT_KEYS = {"bi_after": 0, "bi_without": 1, "bi_drops": 2}
PAGE_SIZE = 10  # players per card page: the gold box (first of the page) + 9 rows
EMBED_PAGE_SIZE = 10  # Fielding and Team Fielding stay as the classic embed
EMBED_CATEGORIES = ("Fielding", "Team Fielding")

METRICS = {
    "Batting": [
        ("runs", "Runs"),
        ("bat_avg", "Batting Average"),
        ("fifties", "Most 50s"),
        ("hundreds", "Most 100s"),
        ("ducks", "Ducks"),
        ("highest", "Highest Runs"),
        ("lowest_strike_rate", "Lowest Strike Rate (minimum 50 runs)"),
        ("strike_rate", "Highest Strike Rate (minimum 50 runs)"),
        ("bi_after", "Most Runs After Catch Drops"),
        ("bi_without", "Most Runs Without Catch Drops"),
        ("bi_drops", "Most Catches Dropped (as Batter)"),
    ],
    "Bowling": [
        ("wickets", "Wickets"),
        ("bowl_avg", "Average"),
        ("economy", "Economy"),
        ("best_figures", "Bowling Figures"),
        ("overs", "Most Overs Bowled"),
        ("runs_leaked", "Most Runs Leaked"),
    ],
    "Fielding": [
        ("fld_caught", "Most Catches Caught"),
        ("fld_dropped", "Most Catches Dropped"),
        ("fld_eff", "Highest Catch Efficiency"),
    ],
    "Fantasy Points": [("fantasy", "Total Fantasy Points")],
    "Team Fielding": [
        ("team_best_fielder", "Team's Best Fielder"),
        ("team_eff", "Best Team Catch Efficiency"),
        ("team_caught", "Most Catches as a Team"),
        ("team_dropped", "Most Catch Drops as a Team"),
        ("team_opps", "Most Catch Opportunities as a Team"),
    ],
}


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".") or "0"
    return str(value)


def _totals(rows: list[dict]) -> dict[str, float | int]:
    runs = sum(r["runs"] for r in rows)
    faced = sum(r["balls_faced"] for r in rows)
    conceded = sum(r["runs_conceded"] for r in rows)
    bowled = sum(r["balls_bowled"] for r in rows)
    wickets = sum(r["wickets"] for r in rows)
    dismissals = sum(1 for r in rows if not r["not_out"])
    return {
        "runs": runs,
        "balls_faced": faced,
        "runs_conceded": conceded,
        "balls_bowled": bowled,
        "wickets": wickets,
        "bat_avg": runs / dismissals if dismissals else runs,
        "strike_rate": runs * 100 / faced if faced else 0,
        "bowl_avg": conceded / wickets if wickets else 0,
        "economy": conceded * 6 / bowled if bowled else 0,
        "bowl_sr": bowled / wickets if wickets else 0,
    }


def _fielding_fantasy_points(rec: tuple[int, int] | None) -> float:
    """Fielding fantasy points exactly as +stats: caught * 5 - dropped * 10."""
    if not rec:
        return 0
    caught, dropped = rec
    return caught * 5 - dropped * 10


def _fantasy_points(rows: list[dict]) -> float:
    """Batting + bowling fantasy points using the +stats formula itself.

    Fielding points are added separately in LeaderboardView.make_payload().
    """
    return fantasy_raw_points(rows)[2]


def _value(key: str, rows: list[dict]) -> float | int | tuple[int, int]:
    t = _totals(rows)
    if key in ("runs", "bat_avg", "strike_rate", "bowl_avg", "economy", "bowl_sr", "wickets"):
        return t[key]
    if key == "highest":
        return max((r["runs"] for r in rows), default=0)
    if key == "lowest_strike_rate":
        faced = sum(r["balls_faced"] for r in rows)
        runs = t["runs"]
        return runs * 100 / faced if faced else 0
    if key == "fifties":
        return sum(1 for r in rows if 50 <= r["runs"] < 100)
    if key == "hundreds":
        return sum(1 for r in rows if r["runs"] >= 100)
    if key == "ducks":
        return sum(1 for r in rows if r["runs"] == 0 and not r["not_out"])
    if key == "overs":
        return t["balls_bowled"] / 6
    if key == "runs_leaked":
        return t["runs_conceded"]
    if key == "best_figures":
        best = max(((r["wickets"], -r["runs_conceded"]) for r in rows), default=(0, 0))
        return best[0], -best[1]
    if key == "fantasy":
        return _fantasy_points(rows)
    return 0


def _eligible(key: str, rows: list[dict]) -> bool:
    t = _totals(rows)
    if key in ("runs", "bat_avg", "highest", "strike_rate", "lowest_strike_rate", "fifties", "hundreds", "ducks"):
        if key in ("strike_rate", "lowest_strike_rate"):
            return t["runs"] >= 50 and t["balls_faced"] > 0
        if key == "ducks":
            return any(r["runs"] == 0 and not r["not_out"] for r in rows)
        return t["runs"] > 0
    if key in ("wickets", "bowl_avg", "economy", "best_figures", "bowl_sr", "overs", "runs_leaked"):
        return t["balls_bowled"] > 0 or t["wickets"] > 0
    return bool(rows)


def _fielding_value(key: str, rec: tuple[int, int]) -> int | tuple[float, int, int]:
    """rec = (catches_caught, catches_dropped) from the fielding_stats table."""
    caught, dropped = rec
    if key == "fld_caught":
        return caught
    if key == "fld_dropped":
        return dropped
    attempts = caught + dropped
    pct = caught * 100 / attempts if attempts else 0.0  # no division by zero
    return pct, caught, attempts


def _fielding_eligible(key: str, rec: tuple[int, int]) -> bool:
    caught, dropped = rec
    if key == "fld_caught":
        return caught > 0
    if key == "fld_dropped":
        return dropped > 0
    return caught + dropped > 0


def _team_db_path() -> Path | None:
    """Locate team_registration.db the same way the +stats team lookup does."""
    path = Path(__file__).resolve().parent.parent / "team_registration.db"
    if path.is_file():
        return path
    fallback = Path("team_registration.db")
    return fallback if fallback.is_file() else None


def _team_rosters(guild_id: int) -> dict[int, dict]:
    """Approved teams of this server: {team_id: {"name": str, "members": [user_id, ...]}}.

    A player belongs to one team. Same membership rule as +stats: captain,
    vice-captain, or a team_players entry (captains are assigned first).
    """
    path = _team_db_path()
    if path is None:
        return {}
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
        teams = conn.execute(
            "SELECT id, name, captain_id, vice_captain_id FROM registered_teams "
            "WHERE guild_id = ? AND status = 'approved' ORDER BY name COLLATE NOCASE",
            (int(guild_id),),
        ).fetchall()
        players = conn.execute(
            "SELECT p.team_id, p.user_id FROM team_players p "
            "JOIN registered_teams t ON t.id = p.team_id "
            "WHERE t.guild_id = ? AND t.status = 'approved' ORDER BY p.team_id, p.user_id",
            (int(guild_id),),
        ).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return {}
    finally:
        if conn is not None:
            conn.close()

    result = {int(t[0]): {"name": str(t[1]), "members": []} for t in teams}
    assigned: set[int] = set()

    def add(team_id: int, user_id) -> None:
        if user_id is None:
            return
        user_id = int(user_id)
        if user_id in assigned or team_id not in result:
            return
        assigned.add(user_id)
        result[team_id]["members"].append(user_id)

    for t in teams:
        add(int(t[0]), t[2])
    for t in teams:
        add(int(t[0]), t[3])
    for team_id, user_id in players:
        add(int(team_id), user_id)
    return result


def _team_fielding(guild_id: int, fielding_by_user: dict[int, tuple[int, int]]) -> dict[int, dict]:
    """Team totals built from each current member's saved fielding totals."""
    teams: dict[int, dict] = {}
    for team_id, info in _team_rosters(guild_id).items():
        caught = dropped = 0
        best = None  # (user_id, caught, dropped)
        for uid in info["members"]:
            c, d = fielding_by_user.get(uid, (0, 0))
            caught += c
            dropped += d
            if (c or d) and (best is None or (c, -d) > (best[1], -best[2])):
                best = (uid, c, d)
        teams[team_id] = {"name": info["name"], "caught": caught, "dropped": dropped, "best": best}
    return teams


def _team_value(key: str, team: dict, guild) -> Any:
    """Return the leaderboard value for one team, or None if it doesn't qualify."""
    caught, dropped = team["caught"], team["dropped"]
    attempts = caught + dropped
    if key == "team_caught":
        return caught if caught > 0 else None
    if key == "team_dropped":
        return dropped if dropped > 0 else None
    if key == "team_opps":
        return attempts if attempts > 0 else None
    if key == "team_eff":
        if attempts <= 0:
            return None
        return caught * 100 / attempts, caught, attempts
    if key == "team_best_fielder":
        best = team["best"]
        if best is None or best[1] <= 0:
            return None
        member = guild.get_member(best[0]) if guild else None
        name = member.name if member else f"Unknown player ({str(best[0])[-4:]})"
        return best[1], name
    return None


def _sort_key(key: str, value: Any):
    if key == "team_best_fielder":
        return (-value[0], value[1].casefold())
    if key in ("fld_eff", "team_eff"):
        # Highest efficiency first; ties go to the player with more attempts.
        return (-value[0], -value[2])
    if key == "lowest_strike_rate":
        return (value,)
    if key in ("bowl_avg", "economy"):
        return (value == 0, value)
    if key == "best_figures":
        return (-value[0], value[1])
    return (-value,)


def _display(key: str, value: Any) -> str:
    if key == "team_best_fielder":
        return f"{discord.utils.escape_markdown(value[1])} ({value[0]} catches)"
    if key in ("fld_eff", "team_eff"):
        return f"{value[0]:.2f}% ({value[1]}/{value[2]})"
    if key == "best_figures":
        return f"{value[0]}/{value[1]}"
    return _fmt(value)


def _display_plain(key: str, value: Any) -> str:
    """Same text as _display but without Discord markdown escapes (for the image)."""
    if key == "team_best_fielder":
        return f"{value[1]} ({value[0]} catches)"
    return _display(key, value)


class CategorySelect(discord.ui.Select):
    def __init__(self, view: "LeaderboardView"):
        self.parent_view = view
        category_emojis = {
            "Batting": "<:cricketbat:1561294474951393360>",
            "Bowling": "<:cricketball:1561294577392951338>",
            "Fantasy Points": "<:Crown:1561296003104243803>",
            "Fielding": "🧤",
            "Team Fielding": "🛡️",
        }
        options = [discord.SelectOption(label=name, emoji=category_emojis[name], default=name == view.category)
                   for name in METRICS]
        super().__init__(placeholder="Choose category", min_values=1, max_values=1,
                         options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.category = self.values[0]
        self.parent_view.metric_key, self.parent_view.metric_label = METRICS[self.values[0]][0]
        await self.parent_view.refresh(interaction)


class MetricSelect(discord.ui.Select):
    def __init__(self, view: "LeaderboardView"):
        self.parent_view = view
        options = [discord.SelectOption(label=label, value=key, default=key == view.metric_key)
                   for key, label in METRICS[view.category]]
        super().__init__(placeholder="Choose statistic", min_values=1, max_values=1,
                         options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.metric_key = self.values[0]
        self.parent_view.metric_label = next(
            label for key, label in METRICS[self.parent_view.category]
            if key == self.parent_view.metric_key
        )
        self.parent_view.page = 0
        await self.parent_view.refresh(interaction)


class PageButton(discord.ui.Button):
    LABELS = {"first": "⏮ First", "prev": "◀ Previous", "next": "Next ▶", "last": "Last ⏭"}

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(label=self.LABELS[kind], style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction):
        view: LeaderboardView = self.view  # type: ignore[assignment]
        if self.kind == "first":
            view.page = 0
        elif self.kind == "prev":
            view.page -= 1
        elif self.kind == "next":
            view.page += 1
        else:
            view.page = 10 ** 9  # clamped to the last page when the card is built
        await view.refresh(interaction)


class LeaderboardView(discord.ui.View):
    def __init__(self, cog: "LeaderboardCog", ctx: commands.Context):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.owner_id = ctx.author.id
        self.category = "Batting"
        self.metric_key, self.metric_label = METRICS[self.category][0]
        self.page = 0
        self.team_names: dict[int, str] = {}
        self._rank_delta_cache: dict[tuple[str, str], dict[int, int | None]] = {}
        self._rebuild_items()

    def _rebuild_items(self):
        self.clear_items()
        self.add_item(CategorySelect(self))
        self.add_item(MetricSelect(self))
        for kind in ("first", "prev", "next", "last"):
            self.add_item(PageButton(kind))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who ran `+lb` can use these menus.", ephemeral=True)
            return False
        return True

    def _classic_embed(self, computed: list) -> discord.Embed:
        """The original embed leaderboard, still used for Fielding and Team Fielding."""
        guild = self.ctx.guild
        total = len(computed)
        pages = max(1, (total + EMBED_PAGE_SIZE - 1) // EMBED_PAGE_SIZE)
        self.page = max(0, min(self.page, pages - 1))
        current = computed[self.page * EMBED_PAGE_SIZE:(self.page + 1) * EMBED_PAGE_SIZE]

        noun = "teams" if self.category == "Team Fielding" else "players"
        embed = discord.Embed(
            title="# **`KASHMIR TITANS S1`**",
            description=f"**{self.category.upper()} LEADERBOARD — {self.metric_label.upper()}**\n"
                        f"**{total} {noun} in this category**\n\n",
            colour=BLUE,
        )
        lines = []
        for index, (uid, value) in enumerate(current, start=self.page * EMBED_PAGE_SIZE + 1):
            if self.category == "Team Fielding":
                username = self.team_names.get(uid, f"Team {uid}")
            else:
                member = guild.get_member(uid) if guild else None
                username = member.name if member else f"Unknown player ({str(uid)[-4:]})"
            lines.append(f"`{index}.` **{discord.utils.escape_markdown(username)}** → `{_display(self.metric_key, value)}`")
        embed.description += "\n".join(lines) if lines else "No players qualify for this statistic yet."
        embed.set_footer(text=f"Kashmir Titans S1 • Page {self.page + 1}/{pages} • {EMBED_PAGE_SIZE} {noun} per page")
        for child in self.children:
            if isinstance(child, PageButton):
                child.disabled = (self.page == 0) if child.kind in ("first", "prev") else (self.page >= pages - 1)
        return embed

    def _rank_deltas(self, computed: list[tuple[int, Any]]) -> dict[int, int | None]:
        """Compare this ranking with the last saved snapshot for this exact stat."""
        cache_key = (self.category, self.metric_key)
        if cache_key in self._rank_delta_cache:
            return self._rank_delta_cache[cache_key]

        db = getattr(self.cog.bot, "db", None)
        if db is None:
            result = {int(uid): None for uid, _ in computed}
            self._rank_delta_cache[cache_key] = result
            return result

        guild_id = int(self.ctx.guild.id)
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS lb_rank_history ("
                "guild_id INTEGER NOT NULL, category TEXT NOT NULL, metric_key TEXT NOT NULL, "
                "user_id INTEGER NOT NULL, rank INTEGER NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (guild_id, category, metric_key, user_id))"
            )
            previous_rows = db.execute(
                "SELECT user_id, rank FROM lb_rank_history "
                "WHERE guild_id = ? AND category = ? AND metric_key = ?",
                (guild_id, self.category, self.metric_key),
            ).fetchall()
            previous = {int(row[0]): int(row[1]) for row in previous_rows}
            current = {int(uid): rank for rank, (uid, _) in enumerate(computed, start=1)}
            # Missing baseline means unchanged/neutral. +rs clears the baseline
            # intentionally so restored rankings don't show reverse arrows.
            result = {
                uid: (previous[uid] - rank if uid in previous else 0)
                for uid, rank in current.items()
            }
            db.execute(
                "DELETE FROM lb_rank_history WHERE guild_id = ? AND category = ? AND metric_key = ?",
                (guild_id, self.category, self.metric_key),
            )
            db.executemany(
                "INSERT INTO lb_rank_history (guild_id, category, metric_key, user_id, rank) "
                "VALUES (?, ?, ?, ?, ?)",
                [(guild_id, self.category, self.metric_key, uid, rank)
                 for uid, rank in current.items()],
            )
            db.commit()
        except Exception as exc:
            print(f"[lb] Could not update rank history: {type(exc).__name__}: {exc}")
            result = {int(uid): None for uid, _ in computed}

        self._rank_delta_cache[cache_key] = result
        return result

    async def make_payload(self) -> tuple[discord.Embed | None, discord.File | None]:
        """(embed, None) for Fielding / Team Fielding, otherwise (None, card image file)."""
        guild = self.ctx.guild
        rows_by_user = self.cog.rows_by_user
        computed = []
        if self.category == "Fielding":
            # Fielding data comes from its own table, so fielding-only players count.
            for uid, rec in self.cog.fielding_by_user.items():
                if not _fielding_eligible(self.metric_key, rec):
                    continue
                computed.append((uid, _fielding_value(self.metric_key, rec)))
        elif self.category == "Team Fielding":
            # Re-read both databases on every render so new catches and roster changes show up.
            self.cog.fielding_by_user = self.cog._fielding_rows(guild.id)
            teams = _team_fielding(guild.id, self.cog.fielding_by_user)
            self.team_names = {team_id: t["name"] for team_id, t in teams.items()}
            for team_id, t in teams.items():
                value = _team_value(self.metric_key, t, guild)
                if value is not None:
                    computed.append((team_id, value))
        elif self.metric_key in BATTER_IMPACT_KEYS:
            # Re-read the database on every render so owner edits/resets appear
            # when the leaderboard is refreshed, not only after a bot restart.
            self.cog.batter_impact_by_user = self.cog._batter_impact_rows(guild.id)
            index = BATTER_IMPACT_KEYS[self.metric_key]
            for uid, rec in self.cog.batter_impact_by_user.items():
                value = int(rec[index])
                if value > 0:
                    computed.append((uid, value))
        else:
            if self.metric_key == "fantasy":
                # Re-read so catch totals edited elsewhere show up, like +stats does.
                self.cog.fielding_by_user = self.cog._fielding_rows(guild.id)
            for uid, rows in rows_by_user.items():
                if not _eligible(self.metric_key, rows):
                    continue
                value = _value(self.metric_key, rows)
                if self.metric_key == "fantasy":
                    # Total = batting + bowling + fielding, same as +stats.
                    value += _fielding_fantasy_points(self.cog.fielding_by_user.get(uid))
                computed.append((uid, value))
        computed.sort(key=lambda item: (_sort_key(self.metric_key, item[1]), item[0]))
        if self.category in EMBED_CATEGORIES:
            return self._classic_embed(computed), None

        rank_deltas = self._rank_deltas(computed)
        total = len(computed)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = max(0, min(self.page, pages - 1))
        first = self.page * PAGE_SIZE              # index of the first player on this page (shown in the gold box)
        current = computed[first + 1:first + PAGE_SIZE]
        is_team = self.category == "Team Fielding"
        key = self.metric_key

        async def entry(rank: int, uid: int, value: Any) -> dict:
            if is_team:
                return {"rank": rank, "name": self.team_names.get(uid, f"Team {uid}"),
                        "value": _display_plain(key, value), "avatar": None,
                        "delta": rank_deltas.get(uid)}
            member = guild.get_member(uid) if guild else None
            name = member.name if member else f"Unknown player ({str(uid)[-4:]})"
            return {"rank": rank, "name": name, "value": _display_plain(key, value),
                    "avatar": await self.cog.avatar_bytes(member), "delta": rank_deltas.get(uid)}

        author = self.ctx.author
        top, *rows, you_avatar = await asyncio.gather(
            entry(first + 1, *computed[first]) if computed else asyncio.sleep(0),
            *[entry(first + i + 2, uid, value) for i, (uid, value) in enumerate(current)],
            self.cog.avatar_bytes(author),
        )
        rows = list(rows)

        # The bottom YOU row: the person who ran +lb, wherever they rank in this statistic.
        you_uid = author.id
        if is_team:
            you_uid = next((tid for tid, info in _team_rosters(guild.id).items()
                            if author.id in info["members"]), None)
        you = {"rank": "-", "name": author.name, "value": "-",
               "avatar": you_avatar, "delta": None}
        for index, (uid, value) in enumerate(computed, start=1):
            if uid == you_uid:
                you.update(rank=index, value=_display_plain(key, value),
                           delta=rank_deltas.get(uid))
                if is_team:
                    you["name"] = self.team_names.get(uid, you["name"])
                break

        # Recent form: last five imported match performances, weighted toward wickets.
        watch_uid = None
        watch_score = None
        for uid, player_rows in self.cog.rows_by_user.items():
            recent = player_rows[-5:]
            if not recent:
                continue
            score = sum(int(r["runs"]) + 20 * int(r["wickets"]) for r in recent)
            runs = sum(int(r["runs"]) for r in recent)
            wickets = sum(int(r["wickets"]) for r in recent)
            candidate = (score, runs, wickets, -int(uid))
            if score > 0 and (watch_score is None or candidate > watch_score):
                watch_score, watch_uid = candidate, uid
        watch_member = guild.get_member(watch_uid) if guild and watch_uid is not None else None
        watch_name = watch_member.name if watch_member else (
            f"Player {str(watch_uid)[-4:]}" if watch_uid is not None else "—"
        )
        guild_icon = await self.cog.guild_icon_bytes(guild)
        page_text = f"PAGE {self.page + 1}/{pages}"
        loop = asyncio.get_running_loop()
        png = await loop.run_in_executor(
            None, lambda: render_card(self.category, self.metric_label, top, rows, you, page_text,
                                      guild_icon, watch_name))
        for child in self.children:
            if isinstance(child, PageButton):
                child.disabled = (self.page == 0) if child.kind in ("first", "prev") else (self.page >= pages - 1)
        return None, discord.File(io.BytesIO(png), filename="leaderboard.jpg")

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self._rebuild_items()
        embed, card = await self.make_payload()
        await interaction.message.edit(embed=embed, attachments=[card] if card else [], view=self)


class LeaderboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rows_by_user: dict[int, list[dict]] = {}
        self.fielding_by_user: dict[int, tuple[int, int]] = {}
        self.batter_impact_by_user: dict[int, tuple[int, int, int]] = {}
        self._avatar_cache: dict[str, bytes | None] = {}

    async def _asset_bytes(self, asset) -> bytes | None:
        if asset is None:
            return None
        if asset.key in self._avatar_cache:
            return self._avatar_cache[asset.key]
        try:
            data = await asset.replace(size=128, format="png", static_format="png").read()
        except Exception:
            data = None
        if len(self._avatar_cache) > 300:
            self._avatar_cache.clear()
        self._avatar_cache[asset.key] = data
        return data

    async def avatar_bytes(self, member) -> bytes | None:
        """Small PNG of a member's avatar, cached so flipping pages stays fast."""
        return await self._asset_bytes(member.display_avatar if member is not None else None)

    async def guild_icon_bytes(self, guild) -> bytes | None:
        """Fetch the server icon for the card's top-left corner."""
        return await self._asset_bytes(guild.icon if guild is not None else None)

    def _batter_impact_rows(self, guild_id: int) -> dict[int, tuple[int, int, int]]:
        """Return {user_id: (runs_affected, runs_without_drops, drops)} for this server."""
        db = getattr(self.bot, "db", None)
        if db is None:
            return {}
        try:
            rows = db.execute(
                "SELECT user_id, runs_affected, runs_without_drops, drops "
                "FROM batter_drop_lb WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
        except Exception:
            # batfds may not have been loaded yet; keep the rest of +lb usable.
            return {}
        return {
            int(row["user_id"]): (int(row["runs_affected"]), int(row["runs_without_drops"]), int(row["drops"]))
            for row in rows
        }

    def _fielding_rows(self, guild_id: int) -> dict[int, tuple[int, int]]:
        """Persistent fielding totals for this server only: {user_id: (caught, dropped)}."""
        db = getattr(self.bot, "db", None)
        if db is None:
            return {}
        try:
            rows = db.execute(
                "SELECT user_id, catches_caught, catches_dropped "
                "FROM fielding_stats WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
        except Exception:
            # Table not created yet (fielding cog not loaded): no fielding data.
            return {}
        return {int(r["user_id"]): (int(r["catches_caught"]), int(r["catches_dropped"])) for r in rows}

    def _rows(self, guild_id: int) -> dict[int, list[dict]]:
        db = getattr(self.bot, "db", None)
        if db is None:
            raise RuntimeError("The bot database connection is not available.")
        result: dict[int, list[dict]] = defaultdict(list)
        query = """SELECT p.user_id, p.runs, p.balls_faced, p.runs_conceded,
                          p.balls_bowled, p.wickets, p.not_out
                   FROM player_match_stats p
                   JOIN stats_imports i ON i.source_message_id = p.source_message_id
                   WHERE i.guild_id = ?
                   ORDER BY i.imported_at ASC, p.source_message_id ASC"""
        for row in db.execute(query, (guild_id,)).fetchall():
            result[int(row["user_id"])].append({
                "runs": int(row["runs"]),
                "balls_faced": int(row["balls_faced"]),
                "runs_conceded": int(row["runs_conceded"]),
                "balls_bowled": int(row["balls_bowled"]),
                "wickets": int(row["wickets"]),
                "not_out": int(row["not_out"]),
            })
        return result

    @commands.command(name="lb", aliases=["leaderboard", "leaderboards"])
    @commands.guild_only()
    async def leaderboard(self, ctx: commands.Context):
        try:
            self.rows_by_user = self._rows(ctx.guild.id)
            self.fielding_by_user = self._fielding_rows(ctx.guild.id)
            self.batter_impact_by_user = self._batter_impact_rows(ctx.guild.id)
            view = LeaderboardView(self, ctx)
            async with ctx.typing():
                embed, card = await view.make_payload()
            message = await ctx.send(embed=embed, file=card, view=view)
            view.message = message
        except Exception as exc:
            print(f"[lb] Failed to generate leaderboard: {type(exc).__name__}: {exc}")
            await ctx.send("I couldn't generate the leaderboard. Check the bot console for the error.")


async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))
