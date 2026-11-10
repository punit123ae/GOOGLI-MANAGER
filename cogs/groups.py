"""Group assignment and round-robin schedule commands for GOOGLI MANAGER.

Place this file at cogs/groups.py. Uses team_registration.db (read-only for team
records) and a separate groups_schedule.db for configuration and generated fixtures.
"""

import os
import re
import sqlite3
from datetime import datetime, timezone

import discord
from discord.ext import commands

from .utility import owner_or_whitelisted

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
            guild_id INTEGER NOT NULL, group_key TEXT NOT NULL,
            group_name TEXT NOT NULL, position INTEGER NOT NULL,
            PRIMARY KEY (guild_id, group_key)
        );
        CREATE TABLE IF NOT EXISTS team_groups (
            guild_id INTEGER NOT NULL, team_name_key TEXT NOT NULL,
            team_name TEXT NOT NULL, group_key TEXT NOT NULL,
            updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, team_name_key)
        );
        CREATE TABLE IF NOT EXISTS team_home_stadiums (
            guild_id INTEGER NOT NULL, team_name_key TEXT NOT NULL,
            team_name TEXT NOT NULL, channel_id INTEGER NOT NULL,
            updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, team_name_key)
        );
        CREATE TABLE IF NOT EXISTS schedule_formats (
            guild_id INTEGER PRIMARY KEY, matches_per_pair INTEGER NOT NULL,
            updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stage_formats (
            guild_id INTEGER NOT NULL, stage_key TEXT NOT NULL,
            matches_per_pair INTEGER NOT NULL, updated_by INTEGER NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY (guild_id, stage_key)
        );
        CREATE TABLE IF NOT EXISTS generated_fixtures (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
            group_key TEXT NOT NULL, round_no INTEGER NOT NULL,
            day_no INTEGER NOT NULL, team_one TEXT NOT NULL,
            team_two TEXT NOT NULL, generated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tournament_stages (
            guild_id INTEGER NOT NULL, stage_key TEXT NOT NULL,
            stage_name TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, stage_key)
        );
        """
    )
    # Preserve all legacy group data under a named Stage 1.
    conn.execute(
        "INSERT OR IGNORE INTO tournament_stages VALUES (?, ?, ?, ?)",
        (0, "stage 1", "Stage 1", datetime.now(timezone.utc).isoformat()),
    )
    for row in conn.execute("SELECT guild_id, group_key FROM group_styles").fetchall():
        if "::" not in row["group_key"]:
            conn.execute("UPDATE group_styles SET group_key=? WHERE guild_id=? AND group_key=?",
                         ("stage 1::" + row["group_key"], row["guild_id"], row["group_key"]))
    for row in conn.execute("SELECT guild_id, team_name_key, group_key FROM team_groups").fetchall():
        if "::" not in row["team_name_key"]:
            conn.execute("UPDATE team_groups SET team_name_key=?, group_key=? WHERE guild_id=? AND team_name_key=?",
                         ("stage 1::" + row["team_name_key"], "stage 1::" + row["group_key"],
                          row["guild_id"], row["team_name_key"]))
    for row in conn.execute("SELECT id, group_key FROM generated_fixtures").fetchall():
        if "::" not in row["group_key"]:
            conn.execute("UPDATE generated_fixtures SET group_key=? WHERE id=?",
                         ("stage 1::" + row["group_key"], row["id"]))
    # Create Stage 1 per guild that already has legacy group records or format settings.
    guild_rows = conn.execute("SELECT guild_id FROM group_styles UNION SELECT guild_id FROM team_groups UNION SELECT guild_id FROM generated_fixtures UNION SELECT guild_id FROM schedule_formats").fetchall()
    for row in guild_rows:
        conn.execute("INSERT OR IGNORE INTO tournament_stages VALUES (?, ?, ?, ?)",
                     (row["guild_id"], "stage 1", "Stage 1", datetime.now(timezone.utc).isoformat()))
    for row in conn.execute("SELECT guild_id, matches_per_pair, updated_by, updated_at FROM schedule_formats").fetchall():
        conn.execute("INSERT OR IGNORE INTO stage_formats (guild_id, stage_key, matches_per_pair, updated_by, updated_at) VALUES (?, 'stage 1', ?, ?, ?)",
                     (row["guild_id"], row["matches_per_pair"], row["updated_by"], row["updated_at"]))
    conn.commit()
    return conn

def norm(value):
    return " ".join(str(value).strip().casefold().split())



def stage_key(stage_name):
    return norm(stage_name)


def scoped_key(stage_name, value):
    return f"{stage_key(stage_name)}::{norm(value)}"


def ensure_stage(conn, guild_id, stage_name):
    key = stage_key(stage_name)
    return conn.execute(
        "SELECT stage_key, stage_name FROM tournament_stages WHERE guild_id=? AND stage_key=?",
        (guild_id, key),
    ).fetchone()


def register_stage(conn, guild_id, stage_name):
    key = stage_key(stage_name)
    conn.execute(
        "INSERT OR IGNORE INTO tournament_stages (guild_id, stage_key, stage_name, created_at) VALUES (?, ?, ?, ?)",
        (guild_id, key, stage_name.strip(), datetime.now(timezone.utc).isoformat()),
    )
    return key


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
    if not await owner_or_whitelisted(ctx, app_owner=True):
        await ctx.reply("Only the bot owner or a role whitelisted for this command can use it.", mention_author=False)
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

    @commands.command(name="createnewstage")
    async def createnewstage(self, ctx, *, stage_name: str = None):
        if not await owner_only(ctx):
            return
        if not stage_name or len(stage_name.strip()) > 60:
            await ctx.send("Usage: `+createnewstage <stage name>`")
            return
        conn = connect_group_db()
        try:
            key = stage_key(stage_name)
            if ensure_stage(conn, ctx.guild.id, stage_name):
                await ctx.send(f"Stage **{stage_name}** already exists.")
                return
            register_stage(conn, ctx.guild.id, stage_name)
            conn.commit()
        finally:
            conn.close()
        await ctx.send(f"✅ Created **{stage_name.strip()}**. Existing stages and their data were kept.")

    @commands.command(name="namestage", aliases=["renamestage"])
    async def renamestage(self, ctx, *, names: str = None):
        if not await owner_only(ctx):
            return
        parts = names.split("|", 1) if names else []
        if len(parts) != 2 or not all(part.strip() for part in parts):
            await ctx.send("Usage: `+renamestage <old stage name> | <new stage name>`")
            return
        old_name, new_name = parts[0].strip(), parts[1].strip()
        old_key, new_key = stage_key(old_name), stage_key(new_name)
        if old_key == new_key:
            await ctx.send("The new stage name must be different.")
            return
        conn = connect_group_db()
        try:
            if not ensure_stage(conn, ctx.guild.id, old_name):
                await ctx.send(f"Stage **{old_name}** was not found.")
                return
            if ensure_stage(conn, ctx.guild.id, new_name):
                await ctx.send(f"Stage **{new_name}** already exists.")
                return
            for table, column in (("group_styles", "group_key"), ("team_groups", "team_name_key"),
                                  ("team_groups", "group_key"), ("generated_fixtures", "group_key")):
                rows = conn.execute(f"SELECT rowid, {column} AS value FROM {table} WHERE guild_id=? AND {column} LIKE ?",
                                    (ctx.guild.id, old_key + "::%")).fetchall()
                for row in rows:
                    suffix = row["value"].split("::", 1)[1]
                    conn.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?",
                                 (new_key + "::" + suffix, row["rowid"]))
            conn.execute("UPDATE tournament_stages SET stage_key=?, stage_name=? WHERE guild_id=? AND stage_key=?",
                         (new_key, new_name, ctx.guild.id, old_key))
            conn.execute("UPDATE stage_formats SET stage_key=? WHERE guild_id=? AND stage_key=?",
                         (new_key, ctx.guild.id, old_key))
            conn.commit()
        finally:
            conn.close()
        reserve_db = os.path.join(os.path.dirname(__file__), "..", "reserve_matches.db")
        if os.path.exists(reserve_db):
            reserve_conn = sqlite3.connect(reserve_db)
            try:
                reserve_conn.execute("UPDATE reservations SET stage_name=? WHERE stage_name=? COLLATE NOCASE",
                                     (new_name, old_name))
                reserve_conn.execute("UPDATE reserve_stage_schedule_state SET stage_name=? WHERE stage_name=? COLLATE NOCASE",
                                     (new_name, old_name))
                reserve_conn.commit()
            except sqlite3.Error:
                pass
            finally:
                reserve_conn.close()
        await ctx.send(f"✅ Renamed **{old_name}** to **{new_name}**. Its groups, teams, format, fixtures and reserve matches were retained.")

    @commands.command(name="stages")
    async def stages(self, ctx):
        conn = connect_group_db()
        try:
            rows = conn.execute("SELECT stage_name FROM tournament_stages WHERE guild_id=? ORDER BY created_at",
                                (ctx.guild.id,)).fetchall()
        finally:
            conn.close()
        await ctx.send("**Tournament stages:** " + (", ".join(row["stage_name"] for row in rows) if rows else "None"))


    @commands.command(name="groupstyle")
    async def groupstyle(self, ctx, *, stage_name: str = None):
        """Configure group names for one stage."""
        if not await owner_only(ctx):
            return
        if not stage_name:
            await ctx.send("Usage: `+groupstyle <stage name>`")
            return
        conn = connect_group_db()
        try:
            stage = ensure_stage(conn, ctx.guild.id, stage_name)
        finally:
            conn.close()
        if not stage:
            await ctx.send(f"Stage **{stage_name}** does not exist. Create it with `+createnewstage {stage_name}`.")
            return
        msg = await ask_owner(ctx, "Send the group names for this stage separated by commas (example: A, B, C).")
        if msg is None or norm(msg.content) == "cancel":
            await ctx.send("Cancelled. No changes made.")
            return
        groups = [item.strip() for item in msg.content.split(",") if item.strip()]
        keys = [scoped_key(stage_name, item) for item in groups]
        if not groups or len(keys) != len(set(keys)):
            await ctx.send("Enter at least one group name, without duplicates.")
            return
        conn = connect_group_db()
        try:
            conn.execute("DELETE FROM group_styles WHERE guild_id=? AND group_key LIKE ?",
                         (ctx.guild.id, stage_key(stage_name) + "::%"))
            conn.executemany(
                "INSERT INTO group_styles (guild_id, group_key, group_name, position) VALUES (?, ?, ?, ?)",
                [(ctx.guild.id, scoped_key(stage_name, name), name, i) for i, name in enumerate(groups)],
            )
            conn.commit()
        finally:
            conn.close()
        await ctx.send(f"Groups saved for **{stage_name}**: " + ", ".join(f"`{x}`" for x in groups))


    @commands.command(name="setteamgroups")
    async def setteamgroups(self, ctx, *, stage_name: str = None):
        """Select approved teams and assign them to groups in a named stage."""
        if not await owner_only(ctx):
            return
        if not stage_name:
            await ctx.send("Usage: `+setteamgroups <stage name>`")
            return
        conn = connect_group_db()
        try:
            stage = ensure_stage(conn, ctx.guild.id, stage_name)
            groups = conn.execute(
                "SELECT group_name FROM group_styles WHERE guild_id=? AND group_key LIKE ? ORDER BY position",
                (ctx.guild.id, stage_key(stage_name) + "::%"),
            ).fetchall()
        finally:
            conn.close()
        if not stage:
            await ctx.send(f"Stage **{stage_name}** does not exist. Use `+createnewstage {stage_name}` first.")
            return
        if not groups:
            await ctx.send(f"Set groups first with `+groupstyle {stage_name}`.")
            return
        try:
            teams = get_approved_teams(ctx.guild.id)
        except (FileNotFoundError, sqlite3.Error) as exc:
            await ctx.send(f"Database error: {exc}")
            return
        if not teams:
            await ctx.send("There are no approved teams.")
            return
        await ctx.send("**Approved teams available for " + stage_name + ":**\n" +
                       "\n".join(f"`{i}`. {name}" for i, (name, _) in enumerate(teams, 1)))
        msg = await ask_owner(ctx, "Enter only the team numbers to include, separated by commas (example: 1, 3, 5). Type cancel to stop.")
        if msg is None or norm(msg.content) == "cancel":
            await ctx.send("Cancelled. No assignments changed.")
            return
        try:
            indexes = [int(x.strip()) for x in msg.content.split(",") if x.strip()]
            if not indexes or len(indexes) != len(set(indexes)) or any(i < 1 or i > len(teams) for i in indexes):
                raise ValueError
        except ValueError:
            await ctx.send("Invalid selection. Use unique team numbers separated by commas.")
            return
        selected = [teams[i - 1] for i in indexes]
        assignments = []
        group_names = [row["group_name"] for row in groups]
        await ctx.send("Assigning only your selected teams. Groups: " + ", ".join(group_names))
        for index, (team_name, team_db_key) in enumerate(selected, 1):
            msg = await ask_owner(ctx, f"**{index}/{len(selected)} — {team_name}**\nEnter its group name:")
            if msg is None or norm(msg.content) == "cancel":
                await ctx.send("Cancelled. No assignments saved.")
                return
            group_name = next((name for name in group_names if norm(name) == norm(msg.content)), None)
            if not group_name:
                await ctx.send(f"Invalid group for {team_name}. No assignments saved; run the command again.")
                return
            assignments.append((team_name, team_db_key, scoped_key(stage_name, group_name)))
        conn = connect_group_db()
        try:
            now = datetime.now(timezone.utc).isoformat()
            stage_prefix = stage_key(stage_name) + "::%"
            old_stage_keys = [row["team_name_key"] for row in conn.execute(
                "SELECT team_name_key FROM team_groups WHERE guild_id=? AND team_name_key LIKE ?",
                (ctx.guild.id, stage_prefix),
            ).fetchall()]
            for old_key in old_stage_keys:
                conn.execute("DELETE FROM team_groups WHERE guild_id=? AND team_name_key=?", (ctx.guild.id, old_key))
            conn.execute("DELETE FROM generated_fixtures WHERE guild_id=? AND group_key LIKE ?",
                         (ctx.guild.id, stage_prefix))
            for team_name, team_db_key, group_key in assignments:
                conn.execute(
                    "INSERT INTO team_groups (guild_id, team_name_key, team_name, group_key, updated_by, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id, team_name_key) DO UPDATE SET "
                    "team_name=excluded.team_name, group_key=excluded.group_key, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (ctx.guild.id, scoped_key(stage_name, team_db_key), team_name, group_key, ctx.author.id, now),
                )
            conn.commit()
        finally:
            conn.close()
        await ctx.send(f"Saved **{len(assignments)}** selected team assignments in **{stage_name}**. Other stages were not changed.")


    @commands.command(name="audittgroup")
    async def audittgroup(self, ctx, *, stage_name: str = None):
        if not await owner_only(ctx):
            return
        if not stage_name:
            await ctx.send("Usage: `+audittgroup <stage name>`")
            return
        conn = connect_group_db()
        try:
            stage = ensure_stage(conn, ctx.guild.id, stage_name)
            groups = conn.execute("SELECT group_key, group_name FROM group_styles WHERE guild_id=? AND group_key LIKE ?",
                                  (ctx.guild.id, stage_key(stage_name) + "::%")).fetchall()
            assignments = conn.execute("SELECT team_name, group_key FROM team_groups WHERE guild_id=? AND team_name_key LIKE ?",
                                       (ctx.guild.id, stage_key(stage_name) + "::%")).fetchall()
        finally:
            conn.close()
        if not stage:
            await ctx.send(f"Stage **{stage_name}** does not exist.")
            return
        valid = {row["group_key"] for row in groups}
        invalid = [row["team_name"] for row in assignments if row["group_key"] not in valid]
        lines = [f"**Group Audit — {stage_name}**", f"Teams assigned: {len(assignments)}",
                 f"Groups configured: {len(groups)}", f"Invalid assignments: {len(invalid)}"]
        lines.extend("• " + name for name in invalid)
        if not invalid:
            lines.append("All saved assignments point to valid groups.")
        await ctx.send("\n".join(lines))


    @commands.command(name="deletegroupteams")
    async def deletegroupteams(self, ctx, stage_name: str = None, *, group_name: str = None):
        """Clear one group's assignments inside a named stage."""
        if not await owner_only(ctx):
            return
        if not stage_name or not group_name:
            await ctx.send("Usage: `+deletegroupteams <stage name> <group>`")
            return
        conn = connect_group_db()
        try:
            group = conn.execute(
                "SELECT group_name, group_key FROM group_styles WHERE guild_id=? AND group_key=?",
                (ctx.guild.id, scoped_key(stage_name, group_name)),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) FROM team_groups WHERE guild_id=? AND group_key=?",
                (ctx.guild.id, group["group_key"]),
            ).fetchone()[0] if group else 0
        finally:
            conn.close()
        if not group:
            await ctx.send(f"Group **{group_name}** is not configured in **{stage_name}**.")
            return
        if count == 0:
            await ctx.send(f"Group **{group_name}** in **{stage_name}** has no assigned teams.")
            return
        view = ClearGroupConfirmView(ctx, group["group_key"], f"{stage_name} / {group['group_name']}")
        view.message = await ctx.send(
            f"Confirm removal of **{count} team assignment(s)** from **{stage_name} / {group['group_name']}**? "
            "Only this stage's group assignments and fixtures will be removed.",
            view=view,
        )


    @commands.command(name="setformat")
    async def setformat(self, ctx, *, stage_name: str = None):
        """Set repeat meetings per pair for one stage."""
        if not await owner_only(ctx):
            return
        if not stage_name:
            await ctx.send("Usage: `+setformat <stage name>`")
            return
        conn = connect_group_db()
        try:
            stage = ensure_stage(conn, ctx.guild.id, stage_name)
        finally:
            conn.close()
        if not stage:
            await ctx.send(f"Stage **{stage_name}** does not exist.")
            return
        msg = await ask_owner(ctx, "How many times should each pair play? Enter a whole number from 1 to 20.")
        if msg is None or norm(msg.content) == "cancel":
            await ctx.send("Cancelled. Format unchanged.")
            return
        try:
            count = int(msg.content.strip())
            if not 1 <= count <= 20:
                raise ValueError
        except ValueError:
            await ctx.send("Enter a whole number from 1 to 20.")
            return
        conn = connect_group_db()
        try:
            conn.execute("INSERT INTO stage_formats (guild_id, stage_key, matches_per_pair, updated_by, updated_at) "
                         "VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, stage_key) DO UPDATE SET "
                         "matches_per_pair=excluded.matches_per_pair, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                         (ctx.guild.id, stage_key(stage_name), count, ctx.author.id, datetime.now(timezone.utc).isoformat()))
            conn.commit()
        finally:
            conn.close()
        await ctx.send(f"Format saved for **{stage_name}**: each pair plays **{count}** time(s).")


    @commands.command(name="synchomestadiums")
    async def synchomestadiums(self, ctx):
        """Sync home stadium channels from channel topics."""
        if not await owner_only(ctx):
            return
        try:
            approved = get_approved_teams(ctx.guild.id)
        except (FileNotFoundError, sqlite3.Error) as exc:
            await ctx.send(f"Could not read approved teams: {exc}")
            return
        teams = [{"team_name_key": key, "team_name": name} for name, key in approved]
        if not teams:
            await ctx.send("No approved teams were found.")
            return
        teams_by_name = {norm(row["team_name"]): row for row in teams}
        found, ambiguous = {}, {}
        for channel in ctx.guild.text_channels:
            marker = re.search(r"home\s+stadium\s+of\s+(.+?)\s*$", channel.topic or "", re.IGNORECASE)
            if not marker:
                continue
            described = marker.group(1).strip().strip(" <>\x60*_~.,;:!")
            team = teams_by_name.get(norm(described))
            if not team:
                continue
            key = team["team_name_key"]
            if key in found:
                ambiguous.setdefault(key, [found.pop(key)]).append(channel)
            elif key in ambiguous:
                ambiguous[key].append(channel)
            else:
                found[key] = channel
        now = datetime.now(timezone.utc).isoformat()
        conn = connect_group_db()
        try:
            for key, channel in found.items():
                team = next(row for row in teams if row["team_name_key"] == key)
                conn.execute(
                    "INSERT INTO team_home_stadiums (guild_id, team_name_key, team_name, channel_id, updated_by, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id, team_name_key) DO UPDATE SET "
                    "team_name=excluded.team_name, channel_id=excluded.channel_id, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (ctx.guild.id, key, team["team_name"], channel.id, ctx.author.id, now),
                )
            conn.commit()
        finally:
            conn.close()
        lines = [f"Synced home stadiums for **{len(found)} team(s)** across all stages."]
        lines.extend(f"• **{next(row['team_name'] for row in teams if row['team_name_key']==key)}** → {channel.mention}"
                     for key, channel in found.items())
        if ambiguous:
            lines.append("Multiple matching channels found for: " + ", ".join(
                next(row["team_name"] for row in teams if row["team_name_key"] == key) for key in ambiguous
            ))
        await ctx.send("\n".join(lines))


    @commands.command(name="sethomestadium")
    async def sethomestadium(self, ctx, *, details: str = None):
        """Set a team's home stadium channel: +sethomestadium Team Name #channel."""
        if not await owner_only(ctx):
            return
        if not details:
            await ctx.send("Usage: `+sethomestadium <team name> <#channel>`")
            return
        match = re.fullmatch(r"\s*(.+?)\s+(?:<#(\d+)>|(\d+))\s*", details)
        if not match:
            await ctx.send("Usage: `+sethomestadium <team name> <#channel>`")
            return
        team_name = match.group(1).strip()
        channel = ctx.guild.get_channel(int(match.group(2) or match.group(3)))
        if not isinstance(channel, discord.TextChannel):
            await ctx.send("Please provide a text channel from this server.")
            return
        try:
            approved = get_approved_teams(ctx.guild.id)
        except (FileNotFoundError, sqlite3.Error) as exc:
            await ctx.send(f"Could not read approved teams: {exc}")
            return
        team = next((item for item in approved if norm(item[0]) == norm(team_name) or norm(item[1]) == norm(team_name)), None)
        if not team:
            await ctx.send("That is not an approved team in this server.")
            return
        official_name, official_key = team
        conn = connect_group_db()
        try:
            conn.execute(
                "INSERT INTO team_home_stadiums (guild_id, team_name_key, team_name, channel_id, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id, team_name_key) DO UPDATE SET "
                "team_name=excluded.team_name, channel_id=excluded.channel_id, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (ctx.guild.id, official_key, official_name, channel.id, ctx.author.id, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        await ctx.send(f"Home stadium for **{official_name}** set to {channel.mention}. This applies across all stages.")


    @commands.command(name="schedule")
    async def schedule(self, ctx, stage_name: str = None, group_name: str = None, round_no: int = None):
        """Post one round of a named stage and group."""
        if not await owner_only(ctx):
            return
        if not stage_name or not group_name or round_no is None or round_no < 1:
            await ctx.send("Usage: `+schedule <stage name> <group> <round>`")
            return
        conn = connect_group_db()
        try:
            group = conn.execute("SELECT group_name, group_key FROM group_styles WHERE guild_id=? AND group_key=?",
                                 (ctx.guild.id, scoped_key(stage_name, group_name))).fetchone()
            fixtures = conn.execute("SELECT team_one, team_two, day_no FROM generated_fixtures WHERE guild_id=? AND group_key=? AND day_no=? ORDER BY id",
                                    (ctx.guild.id, group["group_key"], round_no)).fetchall() if group else []
            assigned = conn.execute("SELECT team_name FROM team_groups WHERE guild_id=? AND group_key=? ORDER BY team_name COLLATE NOCASE",
                                    (ctx.guild.id, group["group_key"])).fetchall() if group else []
            home_stadiums = conn.execute("SELECT team_name, channel_id FROM team_home_stadiums WHERE guild_id=?",
                                         (ctx.guild.id,)).fetchall()
        finally:
            conn.close()
        if not group:
            await ctx.send(f"Group **{group_name}** is not configured in **{stage_name}**.")
            return
        if not fixtures:
            await ctx.send(f"No fixtures for **{stage_name} / {group_name} / Round {round_no}**. Generate with `+generateschedule \"{stage_name}\" {group_name}`.")
            return
        if not os.path.exists(TEAM_DB):
            await ctx.send(f"Could not find {TEAM_DB}.")
            return
        team_conn = sqlite3.connect(TEAM_DB)
        try:
            role_rows = team_conn.execute("SELECT t.name, s.role_id FROM registered_teams t JOIN team_role_settings s ON s.team_id=t.id WHERE t.guild_id=? AND LOWER(t.status)='approved'", (ctx.guild.id,)).fetchall()
        finally:
            team_conn.close()
        role_by_team = {norm(name): int(role_id) for name, role_id in role_rows}
        stadium_by_team = {norm(row["team_name"]): int(row["channel_id"]) for row in home_stadiums}
        lines = [f"# KASHMIR TITANS — {stage_name.upper()}", f"## ROUND {round_no}", f"# GROUP {group['group_name']}"]
        ping_roles = []
        playing = set()
        for index, fixture in enumerate(fixtures, 1):
            a, b = fixture["team_one"], fixture["team_two"]
            playing.update((norm(a), norm(b)))
            role_a = ctx.guild.get_role(role_by_team.get(norm(a), 0))
            role_b = ctx.guild.get_role(role_by_team.get(norm(b), 0))
            if role_a is None or role_b is None:
                await ctx.send(f"Missing configured role for {a} or {b}.")
                return
            channel_id = stadium_by_team.get(norm(a))
            if not channel_id or ctx.guild.get_channel(channel_id) is None:
                await ctx.send(f"Set a home stadium for host **{a}** with `+sethomestadium {a} #channel`.")
                return
            ping_roles.extend((role_a, role_b))
            lines.extend(["", f"**Match {index}**", f"- {role_a.mention} vs {role_b.mention} in <#{channel_id}>"])
        rest = []
        for row in assigned:
            if norm(row["team_name"]) not in playing:
                role = ctx.guild.get_role(role_by_team.get(norm(row["team_name"]), 0))
                if role:
                    rest.append(role)
        if rest:
            lines.append("**Rest:** " + ", ".join(role.mention for role in rest))
            ping_roles.extend(rest)
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=ping_roles, replied_user=False))


    @commands.command(name="generateschedule")
    async def generateschedule(self, ctx, stage_name: str = None, *, group_name: str = None):
        """Generate round-robin fixtures for one stage and group."""
        if not await owner_only(ctx):
            return
        if not stage_name or not group_name:
            await ctx.send("Usage: `+generateschedule <stage name> <group>`")
            return
        conn = connect_group_db()
        try:
            group = conn.execute("SELECT group_name, group_key FROM group_styles WHERE guild_id=? AND group_key=?",
                                 (ctx.guild.id, scoped_key(stage_name, group_name))).fetchone()
            fmt = conn.execute("SELECT matches_per_pair FROM stage_formats WHERE guild_id=? AND stage_key=?",
                               (ctx.guild.id, stage_key(stage_name))).fetchone()
            assignments = conn.execute("SELECT team_name FROM team_groups WHERE guild_id=? AND group_key=? ORDER BY team_name COLLATE NOCASE",
                                       (ctx.guild.id, group["group_key"])).fetchall() if group else []
        finally:
            conn.close()
        if not group:
            await ctx.send(f"Group **{group_name}** is not configured in **{stage_name}**.")
            return
        if not fmt:
            await ctx.send(f"Set this stage's format with `+setformat {stage_name}`.")
            return
        names = [row["team_name"] for row in assignments]
        if len(names) < 2:
            await ctx.send("At least two teams must be assigned to this group.")
            return
        one_round = make_round_robin(names)
        positions = {norm(name): i for i, name in enumerate(names)}
        fixtures, day_number = [], 1
        for leg in range(1, fmt["matches_per_pair"] + 1):
            for matches in one_round:
                for a, b in matches:
                    if len(names) % 2:
                        distance = (positions[norm(b)] - positions[norm(a)]) % len(names)
                        if distance > len(names) // 2:
                            a, b = b, a
                    if leg % 2 == 0:
                        a, b = b, a
                    fixtures.append((leg, day_number, a, b))
                day_number += 1
        now = datetime.now(timezone.utc).isoformat()
        conn = connect_group_db()
        try:
            conn.execute("DELETE FROM generated_fixtures WHERE guild_id=? AND group_key=?",
                         (ctx.guild.id, group["group_key"]))
            conn.executemany("INSERT INTO generated_fixtures (guild_id, group_key, round_no, day_no, team_one, team_two, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                             [(ctx.guild.id, group["group_key"], rnd, day, a, b, now) for rnd, day, a, b in fixtures])
            conn.commit()
        finally:
            conn.close()
        lines = [f"**{stage_name} — Group {group['group_name']} Schedule**", f"Teams: {len(names)} | Legs per pair: {fmt['matches_per_pair']}"]
        for day in sorted({item[1] for item in fixtures}):
            lines.append(f"\n**Round {day}**")
            for leg, fixture_day, a, b in fixtures:
                if fixture_day == day:
                    lines.append(f"• {a} vs {b}")
        lines.append("Schedule saved for this stage and group only.")
        for chunk in split_chunks("\n".join(lines)):
            await ctx.send(chunk)



async def setup(bot):
    await bot.add_cog(Groups(bot))
