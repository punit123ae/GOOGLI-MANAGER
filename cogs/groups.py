"""Group assignment and round-robin schedule commands for GOOGLI MANAGER.

Place this file at cogs/groups.py. Uses team_registration.db (read-only for team
records) and a separate groups_schedule.db for configuration and generated fixtures.
"""

import os
import sqlite3
from datetime import datetime, timezone

import discord
from discord.ext import commands

TEAM_DB = "team_registration.db"
GROUP_DB = "groups_schedule.db"
PROMPT_TIMEOUT = 180


def connect_group_db():
    conn = sqlite3.connect(GROUP_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS group_styles (
            guild_id INTEGER NOT NULL,
            group_key TEXT NOT NULL,
            group_name TEXT NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (guild_id, group_key)
        );
        CREATE TABLE IF NOT EXISTS team_groups (
            guild_id INTEGER NOT NULL,
            team_name_key TEXT NOT NULL,
            team_name TEXT NOT NULL,
            group_key TEXT NOT NULL,
            updated_by INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, team_name_key)
        );
        CREATE TABLE IF NOT EXISTS schedule_formats (
            guild_id INTEGER PRIMARY KEY,
            matches_per_pair INTEGER NOT NULL,
            updated_by INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS generated_fixtures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            group_key TEXT NOT NULL,
            round_no INTEGER NOT NULL,
            day_no INTEGER NOT NULL,
            team_one TEXT NOT NULL,
            team_two TEXT NOT NULL,
            generated_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    return conn


def norm(value):
    return " ".join(str(value).strip().casefold().split())


def get_approved_teams(guild_id):
    if not os.path.exists(TEAM_DB):
        raise FileNotFoundError(
            f"I couldn't find `{TEAM_DB}` in the bot's working folder. "
            "Keep this cog running from the same folder as your team-registration database."
        )
    conn = sqlite3.connect(TEAM_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT name, name_key FROM registered_teams "
            "WHERE guild_id = ? AND LOWER(status) = 'approved' ORDER BY name COLLATE NOCASE",
            (guild_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(row["name"], row["name_key"] or norm(row["name"])) for row in rows]


async def owner_only(ctx):
    if not await ctx.bot.is_owner(ctx.author):
        await ctx.reply("Only the bot owner can use this command.", mention_author=False)
        return False
    if ctx.guild is None:
        await ctx.reply("Use this command in your server, not in DMs.", mention_author=False)
        return False
    return True


async def ask_owner(ctx, prompt, valid_check=None):
    await ctx.send(prompt)

    def check(message):
        return (
            message.author.id == ctx.author.id
            and message.channel.id == ctx.channel.id
            and (valid_check(message.content) if valid_check else True)
        )

    try:
        return await ctx.bot.wait_for("message", check=check, timeout=PROMPT_TIMEOUT)
    except TimeoutError:
        await ctx.send("Timed out. Run the command again when you're ready.")
        return None


def split_chunks(text, limit=1850):
    chunks, current = [], ""
    for line in text.splitlines():
        addition = line if not current else "\n" + line
        if len(current) + len(addition) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current += addition
    if current:
        chunks.append(current)
    return chunks


def make_round_robin(names):
    """Return rounds, each a list of matchday lists. Each team plays max once/day."""
    players = list(names)
    if len(players) < 2:
        return []
    if len(players) % 2:
        players.append(None)  # bye
    size = len(players)
    rounds = []
    rotating = players[:]
    for _ in range(size - 1):
        day_matches = []
        for i in range(size // 2):
            one, two = rotating[i], rotating[size - 1 - i]
            if one is not None and two is not None:
                day_matches.append((one, two))
        rounds.append(day_matches)
        # Keep first player fixed; rotate the rest clockwise.
        rotating = [rotating[0]] + [rotating[-1]] + rotating[1:-1]
    return rounds


class ClearGroupConfirmView(discord.ui.View):
    """Owner-triggered confirmation before clearing a group's assignments."""

    def __init__(self, ctx, group_key, group_name):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.group_key = group_key
        self.group_name = group_name
        self.message = None
        self.completed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the person who ran this command can use these buttons.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Delete all teams", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        conn = connect_group_db()
        try:
            cursor = conn.execute(
                "DELETE FROM team_groups WHERE guild_id = ? AND group_key = ?",
                (self.ctx.guild.id, self.group_key),
            )
            deleted = cursor.rowcount
            conn.execute(
                "DELETE FROM generated_fixtures WHERE guild_id = ? AND group_key = ?",
                (self.ctx.guild.id, self.group_key),
            )
            conn.commit()
        finally:
            conn.close()
        self.completed = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=(
                f"✅ Cleared **{deleted} team assignment(s)** from group **{self.group_name}** "
                "and deleted that group's generated schedule. The group itself is still configured."
            ),
            view=self,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Cancelled. No team assignments were changed.", view=self
        )
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None and not self.completed:
            try:
                await self.message.edit(
                    content="Confirmation expired. No team assignments were changed.", view=self
                )
            except discord.HTTPException:
                pass


class Groups(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        connect_group_db().close()

    @commands.command(name="groupstyle")
    async def groupstyle(self, ctx):
        """Set allowed group names, e.g. A, B, C, D."""
        if not await owner_only(ctx):
            return
        msg = await ask_owner(
            ctx,
            "Send the allowed group names separated by commas (example: `A, B, C, D`).\n"
            "This replaces the allowed group list. Type `cancel` to stop.",
        )
        if msg is None or norm(msg.content) == "cancel":
            if msg is not None:
                await ctx.send("Cancelled. No changes made.")
            return
        groups = [item.strip() for item in msg.content.split(",") if item.strip()]
        keys = [norm(item) for item in groups]
        if not groups or len(keys) != len(set(keys)):
            await ctx.send("Please provide at least one group name, with no duplicates. No changes made.")
            return
        conn = connect_group_db()
        try:
            assigned = conn.execute(
                "SELECT DISTINCT group_key FROM team_groups WHERE guild_id = ?",
                (ctx.guild.id,),
            ).fetchall()
            assigned_keys = {row["group_key"] for row in assigned}
            if assigned_keys - set(keys):
                await ctx.send(
                    "Some teams are assigned to group names that aren't in your new list. "
                    "Run `+setteamgroups` to reassign those teams, then `+audittgroup`."
                )
            conn.execute("DELETE FROM group_styles WHERE guild_id = ?", (ctx.guild.id,))
            conn.executemany(
                "INSERT INTO group_styles (guild_id, group_key, group_name, position) VALUES (?, ?, ?, ?)",
                [(ctx.guild.id, norm(name), name, i) for i, name in enumerate(groups)],
            )
            conn.commit()
        finally:
            conn.close()
        await ctx.send("Allowed groups saved: " + ", ".join(f"`{g}`" for g in groups))

    @commands.command(name="setteamgroups")
    async def setteamgroups(self, ctx):
        """Interactively assign each approved team to one configured group."""
        if not await owner_only(ctx):
            return
        try:
            teams = get_approved_teams(ctx.guild.id)
        except (FileNotFoundError, sqlite3.Error) as exc:
            await ctx.send(f"Database error: {exc}")
            return
        if not teams:
            await ctx.send("There are no approved teams in the team database for this server.")
            return
        conn = connect_group_db()
        try:
            groups = conn.execute(
                "SELECT group_name, group_key FROM group_styles WHERE guild_id = ? ORDER BY position",
                (ctx.guild.id,),
            ).fetchall()
        finally:
            conn.close()
        if not groups:
            await ctx.send("Set allowed groups first with `+groupstyle`.")
            return
        choices = {row["group_key"]: row["group_name"] for row in groups}
        await ctx.send(
            f"Assigning {len(teams)} approved teams. Valid groups: "
            + ", ".join(f"`{row['group_name']}`" for row in groups)
            + ". Reply with a group name for each team, or `cancel` to stop."
        )
        assignments = []
        for index, (team_name, team_key) in enumerate(teams, start=1):
            while True:
                msg = await ask_owner(
                    ctx,
                    f"**Team {index}/{len(teams)}: {team_name}**\nWhich group? ({', '.join(choices.values())})",
                )
                if msg is None:
                    return
                answer = norm(msg.content)
                if answer == "cancel":
                    await ctx.send("Cancelled. No new assignments were saved.")
                    return
                if answer in choices:
                    assignments.append((team_name, team_key, answer))
                    break
                await ctx.send("That group isn't configured. Reply with one of the listed group names.")
        conn = connect_group_db()
        try:
            now = datetime.now(timezone.utc).isoformat()
            conn.executemany(
                "INSERT INTO team_groups (guild_id, team_name_key, team_name, group_key, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id, team_name_key) DO UPDATE SET "
                "team_name=excluded.team_name, group_key=excluded.group_key, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                [(ctx.guild.id, key, name, group_key, ctx.author.id, now) for name, key, group_key in assignments],
            )
            # Remove assignments for teams that are no longer approved in the source database.
            current_keys = {key for _, key in teams}
            existing = conn.execute(
                "SELECT team_name_key FROM team_groups WHERE guild_id = ?", (ctx.guild.id,)
            ).fetchall()
            for row in existing:
                if row["team_name_key"] not in current_keys:
                    conn.execute(
                        "DELETE FROM team_groups WHERE guild_id = ? AND team_name_key = ?",
                        (ctx.guild.id, row["team_name_key"]),
                    )
            conn.commit()
        finally:
            conn.close()
        await ctx.send(f"Saved group assignments for {len(assignments)} approved teams. Run `+audittgroup` to check them.")

    @commands.command(name="audittgroup")
    async def audittgroup(self, ctx):
        """Check approved teams are assigned exactly once to a valid group."""
        if not await owner_only(ctx):
            return
        try:
            teams = get_approved_teams(ctx.guild.id)
        except (FileNotFoundError, sqlite3.Error) as exc:
            await ctx.send(f"Database error: {exc}")
            return
        conn = connect_group_db()
        try:
            groups = conn.execute(
                "SELECT group_key, group_name FROM group_styles WHERE guild_id = ?",
                (ctx.guild.id,),
            ).fetchall()
            assignments = conn.execute(
                "SELECT team_name_key, team_name, group_key FROM team_groups WHERE guild_id = ?",
                (ctx.guild.id,),
            ).fetchall()
        finally:
            conn.close()
        valid_groups = {row["group_key"] for row in groups}
        by_key = {row["team_name_key"]: row for row in assignments}
        missing = [name for name, key in teams if key not in by_key]
        invalid = [row["team_name"] for row in assignments if row["group_key"] not in valid_groups]
        current_keys = {key for _, key in teams}
        stale = [row["team_name"] for row in assignments if row["team_name_key"] not in current_keys]
        lines = ["**Group Assignment Audit**", f"Approved teams: {len(teams)}", f"Configured groups: {len(groups)}"]
        lines.append(f"Missing assignments: {len(missing)}")
        if missing:
            lines.extend(f"• {name}" for name in missing[:40])
        lines.append(f"Assignments using unconfigured groups: {len(invalid)}")
        if invalid:
            lines.extend(f"• {name}" for name in invalid[:40])
        lines.append(f"Teams no longer approved / not in source list: {len(stale)}")
        if stale:
            lines.extend(f"• {name}" for name in stale[:40])
        if not missing and not invalid and not stale and groups:
            lines.append("✅ All approved teams have exactly one valid group assignment.")
        elif not groups:
            lines.append("⚠️ Run `+groupstyle` first.")
        for chunk in split_chunks("\n".join(lines)):
            await ctx.send(chunk)

    @commands.command(name="deletegroupteams")
    async def deletegroupteams(self, ctx, *, group_name: str = None):
        """Remove every team assignment from one group after button confirmation."""
        if not await owner_only(ctx):
            return
        if not group_name:
            await ctx.send("Usage: `+deletegroupteams <group>` (example: `+deletegroupteams A`)")
            return

        conn = connect_group_db()
        try:
            group = conn.execute(
                "SELECT group_name, group_key FROM group_styles WHERE guild_id = ? AND group_key = ?",
                (ctx.guild.id, norm(group_name)),
            ).fetchone()
            if group:
                count = conn.execute(
                    "SELECT COUNT(*) FROM team_groups WHERE guild_id = ? AND group_key = ?",
                    (ctx.guild.id, group["group_key"]),
                ).fetchone()[0]
        finally:
            conn.close()

        if not group:
            await ctx.send("That group isn't configured. Check `+groupstyle`.")
            return
        if count == 0:
            await ctx.send(f"Group **{group['group_name']}** has no assigned teams to delete.")
            return

        view = ClearGroupConfirmView(ctx, group["group_key"], group["group_name"])
        view.message = await ctx.send(
            f"⚠️ **Confirm deletion**\nThis will remove all **{count} team assignment(s)** from "
            f"group **{group['group_name']}** and delete its generated schedule.\n"
            "The approved teams in `team_registration.db` will NOT be deleted, and the group name will remain configured.",
            view=view,
        )

    @commands.command(name="setformat")
    async def setformat(self, ctx):
        """Set how many times each pair of teams plays in each group."""
        if not await owner_only(ctx):
            return
        msg = await ask_owner(
            ctx,
            "How many times should each pair of teams play each other? Send a positive whole number "
            "(for example, `1` or `2`). Type `cancel` to stop.",
        )
        if msg is None or norm(msg.content) == "cancel":
            if msg is not None:
                await ctx.send("Cancelled. Format unchanged.")
            return
        try:
            count = int(msg.content.strip())
            if count < 1 or count > 20:
                raise ValueError
        except ValueError:
            await ctx.send("Please enter a whole number from 1 to 20. Format unchanged.")
            return
        conn = connect_group_db()
        try:
            conn.execute(
                "INSERT INTO schedule_formats (guild_id, matches_per_pair, updated_by, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET matches_per_pair=excluded.matches_per_pair, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (ctx.guild.id, count, ctx.author.id, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        await ctx.send(
            f"Schedule format saved: every pair plays **{count} time(s)**. "
            "Each full round will finish before the next round starts."
        )

    @commands.command(name="generateschedule")
    async def generateschedule(self, ctx, *, group_name: str = None):
        """Generate a saved round-robin schedule for a group: +generateschedule A"""
        if not await owner_only(ctx):
            return
        if not group_name:
            await ctx.send("Usage: `+generateschedule <group>` (example: `+generateschedule A`)")
            return
        conn = connect_group_db()
        try:
            group = conn.execute(
                "SELECT group_name, group_key FROM group_styles WHERE guild_id = ? AND group_key = ?",
                (ctx.guild.id, norm(group_name)),
            ).fetchone()
            fmt = conn.execute(
                "SELECT matches_per_pair FROM schedule_formats WHERE guild_id = ?", (ctx.guild.id,)
            ).fetchone()
            assignments = conn.execute(
                "SELECT team_name FROM team_groups WHERE guild_id = ? AND group_key = ? ORDER BY team_name COLLATE NOCASE",
                (ctx.guild.id, norm(group_name)),
            ).fetchall()
        finally:
            conn.close()
        if not group:
            await ctx.send("That group isn't configured. Check `+groupstyle`.")
            return
        if not fmt:
            await ctx.send("Set the schedule format first with `+setformat`.")
            return
        names = [row["team_name"] for row in assignments]
        if len(names) < 2:
            await ctx.send(f"Group {group['group_name']} needs at least 2 assigned teams to create a schedule.")
            return
        one_round = make_round_robin(names)
        fixtures = []
        day_number = 1
        for round_no in range(1, fmt["matches_per_pair"] + 1):
            for matches in one_round:
                for team_one, team_two in matches:
                    fixtures.append((round_no, day_number, team_one, team_two))
                day_number += 1
        now = datetime.now(timezone.utc).isoformat()
        conn = connect_group_db()
        try:
            conn.execute(
                "DELETE FROM generated_fixtures WHERE guild_id = ? AND group_key = ?",
                (ctx.guild.id, group["group_key"]),
            )
            conn.executemany(
                "INSERT INTO generated_fixtures (guild_id, group_key, round_no, day_no, team_one, team_two, generated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(ctx.guild.id, group["group_key"], rnd, day, a, b, now) for rnd, day, a, b in fixtures],
            )
            conn.commit()
        finally:
            conn.close()
        lines = [f"**{group['group_name']} — Generated Schedule**", f"Teams: {len(names)} | Rounds: {fmt['matches_per_pair']}"]
        for round_no in range(1, fmt["matches_per_pair"] + 1):
            lines.append(f"\n**Round {round_no}**")
            round_days = len(one_round)
            for day in range((round_no - 1) * round_days + 1, round_no * round_days + 1):
                day_fixtures = [f for f in fixtures if f[1] == day]
                lines.append(f"**Day {day}**")
                if day_fixtures:
                    lines.extend(f"• {a} vs {b}" for _, _, a, b in day_fixtures)
                else:
                    lines.append("• Bye day — no matches")
        lines.append("\nSchedule saved. Generating it again for this group will replace its previous schedule.")
        for chunk in split_chunks("\n".join(lines)):
            await ctx.send(chunk)


async def setup(bot):
    await bot.add_cog(Groups(bot))
