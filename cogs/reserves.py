"""
GOOGLI MANAGER - Reserve Match System
------------------------------------
Place this file at: cogs/reserves.py

Requirements:
    pip install -U discord.py

Your bot must use a prefix command setup (for example, "+") and load this cog.
The cog uses its own SQLite database (reserve_matches.db) so it does not alter
your existing team database or schema.

Commands:
    +setreserves <number>       Bot owner only
    +reservematch               Bot owner only; prompts for both team names
    +reservelist                Everyone; 10 reservations per page
    +deletereserve <number>     Bot owner only; owner-only confirmation buttons
    +deleteallreserves          Bot owner only; deletes all reservations and resets numbering
    +creatematchups             Bot owner only; generates/posts schedule
    +reservestatus <team name>  Everyone
    +resetmatchups              Bot owner only; confirmation required
    +reserveaudit               Bot owner only

Rules:
- The configured limit is the maximum total number of reservations involving a team.
- A team can initiate floor(limit / 2) reservations in total across the server.
- Both teams must be below the total involvement limit for a new reservation.
- A team cannot play more than once on a generated day.
- The scheduler tries to find a schedule within the configured limit of days.
- Reservation IDs are AUTOINCREMENT IDs and are not reused after deletion.
"""

import asyncio
import itertools
import sqlite3
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

from .utility import owner_or_whitelisted

DB_PATH = Path(__file__).resolve().parent.parent / "reserve_matches.db"
# Match the path used by cogs/teams.py, which opens team_registration.db
# relative to the bot working directory. Fail closed if this DB is unavailable.
TEAM_DB_PATH = Path("team_registration.db").resolve()
PAGE_SIZE = 10
PROMPT_TIMEOUT = 120
VIEW_TIMEOUT = 120


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _normalize_team_name(name: str) -> str:
    return " ".join(name.casefold().split())


def _resolve_approved_team(guild_id: int, query: str):
    """Return (canonical_name, status, suggestions) from approved teams only.

    status is one of: "ok", "missing", "multiple", "database_missing", "database_error".
    This reads the same registered_teams table as the existing team-registration cog.
    """
    if not TEAM_DB_PATH.is_file():
        return None, "database_missing", []

    key = _normalize_team_name(query)
    if not key:
        return None, "missing", []

    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{TEAM_DB_PATH.as_posix()}?mode=ro", uri=True, timeout=5
        )
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, name_key FROM registered_teams "
            "WHERE guild_id = ? AND status = 'approved' "
            "ORDER BY name COLLATE NOCASE",
            (guild_id,),
        ).fetchall()
    except (sqlite3.Error, OSError):
        return None, "database_error", []
    finally:
        if conn is not None:
            conn.close()

    # Prefer exact official name/name_key matches.
    exact = [
        row["name"] for row in rows
        if _normalize_team_name(row["name_key"] or row["name"]) == key
        or _normalize_team_name(row["name"]) == key
    ]
    if len(exact) == 1:
        return exact[0], "ok", []
    if len(exact) > 1:
        return None, "multiple", exact

    # Allow a unique prefix for convenience, but never guess if ambiguous.
    matches = [
        row["name"] for row in rows
        if _normalize_team_name(row["name"]).startswith(key)
    ]
    if len(matches) == 1:
        return matches[0], "ok", []
    if len(matches) > 1:
        return None, "multiple", matches[:10]
    return None, "missing", []


def _approved_team_error(team_input: str, status: str, suggestions):
    if status == "database_missing":
        return (
            "The team-registration database `team_registration.db` was not found. "
            "I cannot verify approved teams, so no reservation was created."
        )
    if status == "database_error":
        return (
            "I couldn't read the team-registration database. No reservation was created. "
            "Check that the existing team-registration cog and database are available."
        )
    if status == "multiple":
        return (
            f"`{team_input}` matches multiple approved teams: "
            + ", ".join(f"**{name}**" for name in suggestions)
            + ". Please enter the full team name."
        )
    return (
        f"**{team_input}** is not an approved team in this server. "
        "Only teams approved through the existing team-registration system can reserve matches."
    )


def _init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reserve_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                total_limit INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO reserve_settings (id, total_limit)
            VALUES (1, 0)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reserving_team TEXT NOT NULL COLLATE NOCASE,
                opposing_team TEXT NOT NULL COLLATE NOCASE,
                created_by_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS generated_schedule (
                reservation_id INTEGER PRIMARY KEY,
                day_number INTEGER NOT NULL,
                FOREIGN KEY (reservation_id) REFERENCES reservations(id)
                    ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schedule_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                is_generated INTEGER NOT NULL DEFAULT 0,
                channel_id INTEGER,
                message_ids TEXT
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO schedule_state
                (id, is_generated, channel_id, message_ids)
            VALUES (1, 0, NULL, NULL)
        """)


def _limit():
    with _connect() as conn:
        row = conn.execute(
            "SELECT total_limit FROM reserve_settings WHERE id = 1"
        ).fetchone()
        return int(row["total_limit"]) if row else 0


def _team_counts(team_name: str):
    with _connect() as conn:
        total = conn.execute("""
            SELECT COUNT(*) AS n FROM reservations
            WHERE reserving_team = ? COLLATE NOCASE
               OR opposing_team = ? COLLATE NOCASE
        """, (team_name, team_name)).fetchone()["n"]
        initiated = conn.execute("""
            SELECT COUNT(*) AS n FROM reservations
            WHERE reserving_team = ? COLLATE NOCASE
        """, (team_name,)).fetchone()["n"]
    return int(total), int(initiated)


def _all_reservations():
    with _connect() as conn:
        return conn.execute("""
            SELECT id, reserving_team, opposing_team, created_by_id, created_at
            FROM reservations ORDER BY id
        """).fetchall()


def _schedule_exists():
    with _connect() as conn:
        row = conn.execute(
            "SELECT is_generated FROM schedule_state WHERE id = 1"
        ).fetchone()
        return bool(row["is_generated"])


def _clear_schedule_state():
    with _connect() as conn:
        conn.execute("DELETE FROM generated_schedule")
        conn.execute("""
            UPDATE schedule_state
            SET is_generated = 0, channel_id = NULL, message_ids = NULL
            WHERE id = 1
        """)


def _schedule_days(max_days: int):
    """
    Backtracking edge-colouring scheduler. Each reservation is an edge between
    two teams; edges assigned to the same day cannot share a team. It searches
    from the theoretical minimum number of days (maximum team degree) upwards,
    stopping at max_days. This avoids wasting days when matches can be grouped.
    """
    rows = _all_reservations()
    if not rows:
        return []

    edges = []
    degree = {}
    for row in rows:
        a = row["reserving_team"].strip()
        b = row["opposing_team"].strip()
        if a.casefold() == b.casefold():
            raise ValueError(f"Reserve ID {row['id']} has the same team on both sides.")
        edges.append({
            "id": int(row["id"]),
            "a": a,
            "b": b,
            "ak": a.casefold(),
            "bk": b.casefold(),
        })
        degree[a.casefold()] = degree.get(a.casefold(), 0) + 1
        degree[b.casefold()] = degree.get(b.casefold(), 0) + 1

    lower_bound = max(degree.values(), default=1)
    if lower_bound > max_days:
        raise ValueError(
            f"At least {lower_bound} days are required because one team has "
            f"{lower_bound} reserved matches, but the configured limit is {max_days}."
        )

    # Place the most constrained edges first: high-degree teams first.
    edges.sort(
        key=lambda e: (
            -(degree[e["ak"]] + degree[e["bk"]]),
            -max(degree[e["ak"]], degree[e["bk"]]),
            e["id"],
        )
    )

    for day_count in range(max(1, lower_bound), max_days + 1):
        day_teams = [set() for _ in range(day_count)]
        assigned = [-1] * len(edges)
        nodes = 0
        node_cap = 250_000

        # For identical conflicts, prefer the currently least-populated day.
        day_load = [0] * day_count

        def search(index):
            nonlocal nodes
            nodes += 1
            if nodes > node_cap:
                return False
            if index == len(edges):
                return True

            edge = edges[index]
            candidates = sorted(range(day_count), key=lambda d: (day_load[d], d))
            seen_day_states = set()
            for day in candidates:
                if edge["ak"] in day_teams[day] or edge["bk"] in day_teams[day]:
                    continue
                # Symmetry pruning: don't try equivalent empty day slots.
                state = tuple(sorted(day_teams[day]))
                if not state and state in seen_day_states:
                    continue
                if not state:
                    seen_day_states.add(state)

                day_teams[day].add(edge["ak"])
                day_teams[day].add(edge["bk"])
                day_load[day] += 1
                assigned[index] = day

                if search(index + 1):
                    return True

                assigned[index] = -1
                day_load[day] -= 1
                day_teams[day].remove(edge["ak"])
                day_teams[day].remove(edge["bk"])

            return False

        if search(0):
            result = {}
            for edge, day in zip(edges, assigned):
                result.setdefault(day + 1, []).append(edge)
            return [
                (day, sorted(matches, key=lambda e: e["id"]))
                for day, matches in sorted(result.items())
            ]

    raise ValueError(
        f"Could not fit every reservation into {max_days} days without a team "
        "playing twice in one day. Try increasing the limit or reviewing the reservations."
    )


class OwnerConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, timeout: float = VIEW_TIMEOUT):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.choice: Optional[bool] = None
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the user who ran this command can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = False
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class ReserveListView(discord.ui.View):
    def __init__(self, rows, owner_id: Optional[int] = None):
        super().__init__(timeout=180)
        self.rows = list(rows)
        self.page = 0
        self.pages = max(1, (len(self.rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Anyone may browse this public list.
        return True

    def make_embed(self):
        # Kept as a plain-text message to make the list short and easy to scan.
        start = self.page * PAGE_SIZE
        lines = [f"**Reserved Matches ({len(self.rows)})**"]
        for index, row in enumerate(self.rows[start:start + PAGE_SIZE], start=start + 1):
            lines.append(
                f"**{index}.** {row['reserving_team']} vs {row['opposing_team']}"
            )
        lines.append(f"Page {self.page + 1}/{self.pages}")
        return "\n".join(lines)

    async def update(self, interaction: discord.Interaction):
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.pages - 1
        await interaction.response.edit_message(content=self.make_embed(), embed=None, view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self.update(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.pages - 1, self.page + 1)
        await self.update(interaction)


class Reserves(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        _init_db()

    async def owner_only(self, ctx):
        if not await owner_or_whitelisted(ctx, app_owner=True):
            await ctx.reply("Only the bot owner or a role whitelisted for this command can use it.", mention_author=False)
            return False
        return True

    async def ask_team(self, ctx, prompt: str):
        await ctx.send(prompt)

        def check(message):
            return (
                message.author.id == ctx.author.id
                and message.channel.id == ctx.channel.id
            )

        try:
            message = await self.bot.wait_for("message", check=check, timeout=PROMPT_TIMEOUT)
        except asyncio.TimeoutError:
            await ctx.send("Timed out. Please run the command again.")
            return None

        answer = message.content.strip()
        if not answer:
            await ctx.send("Team name cannot be empty.")
            return None
        if len(answer) > 100:
            await ctx.send("Team name must be 100 characters or fewer.")
            return None
        return answer

    @commands.command(name="setreserves")
    async def setreserves(self, ctx, total: int):
        if not await self.owner_only(ctx):
            return
        if total < 1 or total > 100:
            await ctx.reply("Choose a reserve limit from 1 to 100.", mention_author=False)
            return

        with _connect() as conn:
            current_counts = conn.execute("""
                SELECT team_name, COUNT(*) AS n
                FROM (
                    SELECT reserving_team AS team_name FROM reservations
                    UNION ALL
                    SELECT opposing_team AS team_name FROM reservations
                )
                GROUP BY team_name COLLATE NOCASE
            """).fetchall()
            over = [r["team_name"] for r in current_counts if int(r["n"]) > total]
            if over:
                await ctx.reply(
                    f"Cannot set the limit to {total}: these teams already exceed it: "
                    + ", ".join(over[:20]),
                    mention_author=False,
                )
                return
            conn.execute(
                "UPDATE reserve_settings SET total_limit = ? WHERE id = 1", (total,)
            )

        await ctx.send(
            f"Reserve limit set to **{total} matches per team**.\n"
            f"Each team may initiate **{total // 2}** reservations total across the server.\n"
            "Each team's total involvement (as reserving or opposing team) cannot exceed "
            f"**{total}**."
        )

    @commands.command(name="reservematch")
    async def reservematch(self, ctx):
        if not await self.owner_only(ctx):
            return
        limit = _limit()
        if limit <= 0:
            await ctx.send("Set the limit first with `+setreserves <number>`.")
            return

        reserving = await self.ask_team(ctx, "Enter the **reserving team name**:")
        if not reserving:
            return
        opposing = await self.ask_team(ctx, "Enter the **opposing team name**:")
        if not opposing:
            return

        if ctx.guild is None:
            await ctx.send("This command can only be used inside the server where teams are approved.")
            return

        official_reserving, status_a, suggestions_a = _resolve_approved_team(
            ctx.guild.id, reserving
        )
        if status_a != "ok":
            await ctx.send(_approved_team_error(reserving, status_a, suggestions_a))
            return

        official_opposing, status_b, suggestions_b = _resolve_approved_team(
            ctx.guild.id, opposing
        )
        if status_b != "ok":
            await ctx.send(_approved_team_error(opposing, status_b, suggestions_b))
            return

        # Store the exact approved names from the registration database.
        reserving = official_reserving
        opposing = official_opposing

        if reserving.casefold() == opposing.casefold():
            await ctx.send("A team cannot reserve a match against itself.")
            return

        existing = _all_reservations()
        total_a = sum(
            1 for r in existing
            if reserving.casefold() in (
                r["reserving_team"].casefold(), r["opposing_team"].casefold()
            )
        )
        total_b = sum(
            1 for r in existing
            if opposing.casefold() in (
                r["reserving_team"].casefold(), r["opposing_team"].casefold()
            )
        )
        initiated_a = sum(
            1 for r in existing
            if r["reserving_team"].casefold() == reserving.casefold()
        )
        initiation_quota = limit // 2

        problems = []
        if total_a >= limit:
            problems.append(f"**{reserving}** has reached its total limit ({total_a}/{limit}).")
        if total_b >= limit:
            problems.append(f"**{opposing}** has reached its total limit ({total_b}/{limit}).")
        if initiated_a >= initiation_quota:
            problems.append(
                f"**{reserving}** has initiated {initiated_a}/{initiation_quota} reservations "
                "and cannot initiate another. Only the opposing team can initiate a reservation "
                "involving it, if both teams still have capacity."
            )
        if problems:
            await ctx.send("❌ Reservation rejected.\n" + "\n".join(problems))
            return

        with _connect() as conn:
            cur = conn.execute("""
                INSERT INTO reservations (reserving_team, opposing_team, created_by_id)
                VALUES (?, ?, ?)
            """, (reserving, opposing, ctx.author.id))
            reserve_id = cur.lastrowid

        total_a += 1
        total_b += 1
        initiated_a += 1
        embed = discord.Embed(
            title="Reservation Added",
            description=(
                f"**ID:** `{int(reserve_id)}`\n"
                f"**Match:** {reserving} vs {opposing}\n"
                f"**Team limits:** {total_a}/{limit} · {total_b}/{limit}\n"
                f"**Initiated by {reserving}:** {initiated_a}/{initiation_quota}"
            ),
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

        # A new reservation invalidates the stored schedule.
        _clear_schedule_state()

    @commands.command(name="reservelist")
    async def reservelist(self, ctx):
        rows = _all_reservations()
        if not rows:
            await ctx.send("There are no reserved matches yet.")
            return
        view = ReserveListView(rows)
        view.previous.disabled = True
        view.next.disabled = view.pages <= 1
        view.message = await ctx.send(view.make_embed(), view=view)

    @commands.command(name="deletereserve")
    async def deletereserve(self, ctx, match_number: int):
        if not await self.owner_only(ctx):
            return
        # Match numbers are the current 1-based positions shown by +reservelist.
        rows = _all_reservations()
        if match_number < 1 or match_number > len(rows):
            await ctx.reply(
                f"Match number {match_number} was not found. Check `+reservelist`.",
                mention_author=False,
            )
            return
        row = rows[match_number - 1]
        reserve_id = int(row["id"])

        embed = discord.Embed(
            title="Delete this reservation?",
            description=(
                f"**ID:** `{int(reserve_id)}`\n"
                f"**Match:** {row['reserving_team']} vs {row['opposing_team']}\n\n"
                "Confirm to delete it."
            ),
            color=discord.Color.red(),
        )
        view = OwnerConfirmView(ctx.author.id)
        view.message = await ctx.send(embed=embed, view=view)
        await view.wait()

        if view.choice is True:
            with _connect() as conn:
                conn.execute("DELETE FROM reservations WHERE id = ?", (reserve_id,))
                conn.execute(
                    "DELETE FROM generated_schedule WHERE reservation_id = ?", (reserve_id,)
                )
            _clear_schedule_state()
            await ctx.send(
                f"✅ Reserve ID **{reserve_id}** deleted. Team counts have been recalculated. "
                "The generated schedule has been invalidated; run `+creatematchups` again."
            )
        elif view.choice is False:
            await ctx.send(f"Deletion cancelled. Reserve ID **{reserve_id}** was not changed.")
        else:
            await ctx.send(
                f"Confirmation timed out. Reserve ID **{reserve_id}** was not deleted."
            )

    @commands.command(name="reservestatus")
    async def reservestatus(self, ctx, *, team_name: str):
        limit = _limit()
        total, initiated = _team_counts(team_name)
        rows = [
            r for r in _all_reservations()
            if team_name.casefold() in (
                r["reserving_team"].casefold(), r["opposing_team"].casefold()
            )
        ]
        if not rows:
            await ctx.send(f"No reservations found for **{team_name}**.")
            return

        lines = [
            f"**{team_name} — Reserve Status**",
            f"Total matches: **{total}/{limit}**",
            f"Initiated: **{initiated}/{limit // 2}**",
            f"Remaining: **{max(0, limit - total)}**",
            "**Reservations:**",
        ]
        lines.extend(
            f"`{r['id']}` {r['reserving_team']} vs {r['opposing_team']}"
            for r in rows[:25]
        )
        await ctx.send("\n".join(lines))

    @commands.command(name="creatematchups")
    async def creatematchups(self, ctx):
        if not await self.owner_only(ctx):
            return
        if _schedule_exists():
            await ctx.send(
                "A schedule is already marked as generated. Use `+resetmatchups` first, "
                "then run `+creatematchups` to create a new one."
            )
            return

        limit = _limit()
        if limit <= 0:
            await ctx.send("Set the limit first with `+setreserves <number>`.")
            return
        rows = _all_reservations()
        if not rows:
            await ctx.send("There are no reserved matches to schedule.")
            return

        try:
            days = await asyncio.to_thread(_schedule_days, limit)
        except ValueError as exc:
            await ctx.send(f"❌ Could not create the schedule: {exc}")
            return

        # Save schedule before posting so it can be inspected after a restart.
        with _connect() as conn:
            conn.execute("DELETE FROM generated_schedule")
            for day, matches in days:
                for match in matches:
                    conn.execute(
                        "INSERT INTO generated_schedule (reservation_id, day_number) VALUES (?, ?)",
                        (match["id"], day),
                    )
            conn.execute("""
                UPDATE schedule_state
                SET is_generated = 1, channel_id = ?, message_ids = ''
                WHERE id = 1
            """, (ctx.channel.id,))

        intro = discord.Embed(
            title="✦ OFFICIAL RESERVE MATCH SCHEDULE",
            color=discord.Color.from_rgb(35, 63, 110),
            description=(
                f"**Total reserved matches:** {len(rows)}\n"
                f"**Days required:** {len(days)}\n"
                "**Rule:** Each team plays at most once per day."
            ),
        )
        sent = []
        sent.append(await ctx.send(embed=intro))
        for day, matches in days:
            lines = [
                f"`ID {m['id']}` **{m['a']} vs {m['b']}**"
                for m in matches
            ]
            embed = discord.Embed(
                title=f"◆ MATCHDAY {day:02d}",
                color=discord.Color.from_rgb(35, 63, 110),
                description="\n".join(lines) if lines else "No matches",
            )
            sent.append(await ctx.send(embed=embed))

        ids = ",".join(str(m.id) for m in sent)
        with _connect() as conn:
            conn.execute(
                "UPDATE schedule_state SET message_ids = ? WHERE id = 1", (ids,)
            )
        await ctx.send("✅ Complete schedule posted successfully.")

    @commands.command(name="deleteallreserves")
    async def deleteallreserves(self, ctx):
        """Delete every reservation and reset reservation numbering to 1."""
        if not await self.owner_only(ctx):
            return

        rows = _all_reservations()
        if not rows:
            # Also clean stale schedule data and reset the sequence if the table is empty.
            with _connect() as conn:
                conn.execute("DELETE FROM generated_schedule")
                conn.execute("DELETE FROM sqlite_sequence WHERE name = 'reservations'")
                conn.execute("""
                    UPDATE schedule_state
                    SET is_generated = 0, channel_id = NULL, message_ids = NULL
                    WHERE id = 1
                """)
            await ctx.send("There are no reserved matches. Match numbering is reset and will start at **1**.")
            return

        view = OwnerConfirmView(ctx.author.id)
        embed = discord.Embed(
            title="Delete All Reserved Matches?",
            description=(
                f"This will permanently delete **all {len(rows)} reserved matches**, "
                "clear the saved generated schedule, and reset match numbering so the next match starts at **1**.\n\n"
                "Old schedule messages already posted in Discord will not be deleted. Continue?"
            ),
            color=discord.Color.red(),
        )
        view.message = await ctx.send(embed=embed, view=view)
        await view.wait()

        if view.choice is True:
            try:
                with _connect() as conn:
                    conn.execute("DELETE FROM generated_schedule")
                    conn.execute("DELETE FROM reservations")
                    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'reservations'")
                    conn.execute("""
                        UPDATE schedule_state
                        SET is_generated = 0, channel_id = NULL, message_ids = NULL
                        WHERE id = 1
                    """)
            except sqlite3.Error as exc:
                await ctx.send(f"❌ Could not delete all reservations: `{type(exc).__name__}`. No success was reported; check the bot console/database.")
                return
            await ctx.send(
                f"✅ Deleted all **{len(rows)} reserved matches**. Match numbering has been reset; the next reservation will be **Match 1**."
            )
        elif view.choice is False:
            await ctx.send("Deletion cancelled. All reservations are unchanged.")
        else:
            await ctx.send("Confirmation timed out. Nothing was deleted.")

    @commands.command(name="resetmatchups")
    async def resetmatchups(self, ctx):
        if not await self.owner_only(ctx):
            return
        if not _schedule_exists():
            await ctx.send("There is no generated schedule to reset.")
            return
        view = OwnerConfirmView(ctx.author.id)
        embed = discord.Embed(
            title="Reset Generated Schedule?",
            description=(
                "This clears the saved schedule data but does not delete reservations "
                "or remove old schedule messages from Discord. Continue?"
            ),
            color=discord.Color.orange(),
        )
        view.message = await ctx.send(embed=embed, view=view)
        await view.wait()
        if view.choice is True:
            _clear_schedule_state()
            await ctx.send("✅ Saved schedule cleared. Reservations are unchanged.")
        elif view.choice is False:
            await ctx.send("Reset cancelled. The saved schedule is unchanged.")
        else:
            await ctx.send("Confirmation timed out. Nothing was reset.")

    @commands.command(name="reserveaudit")
    async def reserveaudit(self, ctx):
        if not await self.owner_only(ctx):
            return
        limit = _limit()
        rows = _all_reservations()
        counts = {}
        initiated = {}
        issues = []
        for r in rows:
            a = r["reserving_team"].strip()
            b = r["opposing_team"].strip()
            if a.casefold() == b.casefold():
                issues.append(f"ID {r['id']}: a team is matched against itself.")
            for team in (a, b):
                counts[team.casefold()] = counts.get(team.casefold(), 0) + 1
            initiated[a.casefold()] = initiated.get(a.casefold(), 0) + 1

        for team_key, count in counts.items():
            if count > limit:
                issues.append(f"{team_key}: total involvement {count}/{limit}.")
        quota = limit // 2
        for team_key, count in initiated.items():
            if count > quota:
                issues.append(f"{team_key}: initiated {count}/{quota}.")

        embed = discord.Embed(title="Reserve Audit", color=discord.Color.green() if not issues else discord.Color.red())
        embed.add_field(name="Total reservations", value=str(len(rows)), inline=True)
        embed.add_field(name="Per-team limit", value=str(limit), inline=True)
        embed.add_field(name="Initiation quota", value=str(quota), inline=True)
        embed.add_field(
            name="Result",
            value="\n".join(f"• {issue}" for issue in issues[:20]) if issues else "No count or self-match issues found.",
            inline=False,
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Reserves(bot))
