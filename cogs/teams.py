import asyncio
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

import discord
from discord.ext import commands

CAPTAIN_EMOJI = "<:captain:1560220118422659123>"
VICE_CAPTAIN_EMOJI = "<:VC:1560220622703824966>"


class ConfirmDeleteAllView(discord.ui.View):
    def __init__(self, cog, guild_id, author_id):
        super().__init__(timeout=30)
        self.cog, self.guild_id, self.author_id = cog, guild_id, author_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the administrator who started this action can confirm it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm deletion", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if not await self.cog._command_allowed(interaction, "deleteallteams"):
            await interaction.response.send_message("You are not allowed to use this command.", ephemeral=True)
            return
        count = self.cog.db.execute(
            "SELECT COUNT(*) FROM registered_teams WHERE guild_id=?", (self.guild_id,)
        ).fetchone()[0]
        self.cog.db.execute("DELETE FROM team_join_requests WHERE guild_id=?", (self.guild_id,))
        self.cog.db.execute(
            "DELETE FROM team_players WHERE team_id IN (SELECT id FROM registered_teams WHERE guild_id=?)",
            (self.guild_id,)
        )
        self.cog.db.execute("DELETE FROM registered_teams WHERE guild_id=?", (self.guild_id,))
        self.cog.db.commit()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Deleted **{count} team(s)** from this server's database. Other servers' teams were not affected.",
            view=self
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Deletion cancelled. No teams were deleted.", view=self)


class TeamApprovalView(discord.ui.View):
    def __init__(self, cog, team_id):
        super().__init__(timeout=None)
        self.cog, self.team_id = cog, team_id

    async def _authorized(self, interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This button can only be used inside the server.", ephemeral=True)
            return False
        if not isinstance(interaction.channel, discord.abc.GuildChannel):
            await interaction.response.send_message("This is not a valid approval channel.", ephemeral=True)
            return False
        perms = interaction.channel.permissions_for(interaction.user)
        if not (perms.view_channel and (perms.send_messages or perms.manage_messages or interaction.user.guild_permissions.administrator)):
            await interaction.response.send_message("You don't have permission to review applications in this channel.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="teamreg:accept")
    async def accept(self, interaction, button):
        if await self._authorized(interaction):
            await self.cog.decide_application(interaction, self.team_id, "approved")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="teamreg:reject")
    async def reject(self, interaction, button):
        if await self._authorized(interaction):
            await self.cog.decide_application(interaction, self.team_id, "rejected")


class TeamListView(discord.ui.View):
    def __init__(self, cog, guild_id, page=0):
        super().__init__(timeout=300)
        self.cog, self.guild_id, self.page = cog, guild_id, page
        self._sync_buttons()

    def _sync_buttons(self):
        self.clear_items()
        prev = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=self.page <= 0)
        nxt = discord.ui.Button(label="Next", style=discord.ButtonStyle.primary)
        prev.callback, nxt.callback = self.previous_page, self.next_page
        self.add_item(prev)
        self.add_item(nxt)

    async def _show(self, interaction, page):
        total = self.cog.db.execute(
            "SELECT COUNT(*) FROM registered_teams WHERE guild_id=? AND status='approved'",
            (self.guild_id,)
        ).fetchone()[0]
        self.page = max(0, min(page, max(0, (total - 1) // 10)))
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=self.cog.make_teamlist_embed(self.guild_id, self.page), view=self
        )

    async def previous_page(self, interaction):
        await self._show(interaction, self.page - 1)

    async def next_page(self, interaction):
        await self._show(interaction, self.page + 1)


class JoinRequestView(discord.ui.View):
    def __init__(self, cog, request_id):
        super().__init__(timeout=None)
        self.cog, self.request_id = cog, request_id
        accept = discord.ui.Button(label="Accept", style=discord.ButtonStyle.success, custom_id=f"teamjoin:accept:{request_id}")
        deny = discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger, custom_id=f"teamjoin:deny:{request_id}")
        accept.callback, deny.callback = self.accept_request, self.deny_request
        self.add_item(accept)
        self.add_item(deny)

    async def _decide(self, interaction, decision):
        row = self.cog.db.execute(
            "SELECT r.*, t.name AS team_name, t.captain_id, t.status AS team_status "
            "FROM team_join_requests r JOIN registered_teams t ON t.id=r.team_id WHERE r.id=?",
            (self.request_id,)
        ).fetchone()
        if not row or row["status"] != "pending":
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content="This request has already been handled or cancelled.", embed=None, view=self)
            return
        if interaction.user.id != row["captain_id"]:
            await interaction.response.send_message("Only this team's captain can respond to the request.", ephemeral=True)
            return
        if row["team_status"] != "approved":
            await interaction.response.send_message("This team is no longer active.", ephemeral=True)
            return
        if decision == "accepted":
            if self.cog._membership(row["guild_id"], row["user_id"]):
                self.cog.db.execute("UPDATE team_join_requests SET status='denied' WHERE id=?", (self.request_id,))
                self.cog.db.commit()
                for child in self.children:
                    child.disabled = True
                await interaction.response.edit_message(content="This player is already in a team. Request closed.", embed=None, view=self)
                return
            maximum = self.cog._max_players(row["guild_id"])
            if self.cog._team_player_count(row["team_id"]) >= maximum:
                await interaction.response.send_message(
                    f"This team has reached its maximum of {maximum} players. The request has not been accepted.",
                    ephemeral=True
                )
                return
            self.cog.db.execute(
                "UPDATE team_join_requests SET status='accepted' WHERE id=? AND status='pending'",
                (self.request_id,)
            )
            try:
                self.cog.db.execute(
                    "INSERT INTO team_players(team_id,user_id,added_by,added_at) VALUES(?,?,?,?)",
                    (row["team_id"], row["user_id"], row["captain_id"], datetime.now(timezone.utc).isoformat())
                )
            except sqlite3.IntegrityError:
                pass
        else:
            self.cog.db.execute(
                "UPDATE team_join_requests SET status='denied' WHERE id=? AND status='pending'",
                (self.request_id,)
            )
        self.cog.db.commit()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"Request **{decision}** by {interaction.user.name}.", embed=None, view=self)
        if decision == "accepted":
            # Join requests are answered in the captain's DMs, so interaction.guild
            # is normally None. Resolve the original server from the saved request.
            guild = self.cog.bot.get_guild(row["guild_id"])
            if guild is not None:
                member = guild.get_member(row["user_id"])
                if member is None:
                    try:
                        member = await guild.fetch_member(row["user_id"])
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        member = None
                if member is not None:
                    await self.cog._assign_team_role(guild, row["team_id"], member)
        try:
            user = self.cog.bot.get_user(row["user_id"]) or await self.cog.bot.fetch_user(row["user_id"])
            await self.cog._dm(user, f"Your request to join **{row['team_name']}** was {'accepted' if decision == 'accepted' else 'denied'} by the captain.")
        except discord.HTTPException:
            pass

    async def accept_request(self, interaction):
        await self._decide(interaction, "accepted")

    async def deny_request(self, interaction):
        await self._decide(interaction, "denied")


class LeaveTeamConfirmView(discord.ui.View):
    def __init__(self, cog, user_id, team_id, guild_id, team_name):
        super().__init__(timeout=60)
        self.cog, self.user_id, self.team_id, self.guild_id, self.team_name = cog, user_id, team_id, guild_id, team_name

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the player who started this action can confirm it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, leave team", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        team = self.cog._membership(self.guild_id, self.user_id)
        if not team or team["id"] != self.team_id:
            await interaction.response.edit_message(content="You're no longer in that team.", view=None)
            return
        if self.user_id in (team["captain_id"], team["vice_captain_id"]):
            await interaction.response.edit_message(content="Captains and vice-captains cannot leave their team.", view=None)
            return
        self.cog.db.execute("DELETE FROM team_players WHERE team_id=? AND user_id=?", (self.team_id, self.user_id))
        self.cog.db.commit()
        # Remove the team's configured role when a player leaves.
        await self.cog._remove_team_role(interaction.guild, self.team_id, interaction.user)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"You have left **{self.team_name}**. You can now join or create another team.", view=self
        )

    @discord.ui.button(label="No, stay", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"Cancelled. You're still in **{self.team_name}**.", view=self)


class TeamRegistration(commands.Cog):
    """Team registration and roster management."""

    def __init__(self, bot):
        self.bot = bot
        self.db = sqlite3.connect("team_registration.db")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self._init_db()

    def cog_unload(self):
        self.db.close()

    def _init_db(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS team_settings (guild_id INTEGER PRIMARY KEY, approval_channel_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS registered_teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, name TEXT NOT NULL,
            name_key TEXT NOT NULL, captain_id INTEGER NOT NULL, vice_captain_id INTEGER,
            logo_url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', applicant_id INTEGER NOT NULL,
            approval_channel_id INTEGER, approval_message_id INTEGER, created_at TEXT NOT NULL,
            decided_by INTEGER, UNIQUE(guild_id, name_key)
        );
        CREATE TABLE IF NOT EXISTS team_players (
            team_id INTEGER NOT NULL, user_id INTEGER NOT NULL, added_by INTEGER NOT NULL, added_at TEXT NOT NULL,
            PRIMARY KEY(team_id, user_id),
            FOREIGN KEY(team_id) REFERENCES registered_teams(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS team_join_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, team_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', message_id INTEGER,
            channel_id INTEGER, created_at TEXT NOT NULL,
            FOREIGN KEY(team_id) REFERENCES registered_teams(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_pending_join_per_user
        ON team_join_requests(guild_id, user_id) WHERE status='pending';
        CREATE TABLE IF NOT EXISTS team_limits (guild_id INTEGER PRIMARY KEY, max_players INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS team_role_settings (
            team_id INTEGER PRIMARY KEY, role_id INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(team_id) REFERENCES registered_teams(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS server_team_role_settings (guild_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS team_feature_toggles (
            guild_id INTEGER NOT NULL, feature TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(guild_id, feature)
        );
        CREATE TABLE IF NOT EXISTS approved_team_post_settings (
            guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL
        );
        """)
        # Assign each team a permanent sequence number based on its creation time.
        # Existing teams are numbered oldest-first once; future teams receive the next number.
        team_columns = [r[1] for r in self.db.execute("PRAGMA table_info(registered_teams)").fetchall()]
        if "team_number" not in team_columns:
            self.db.execute("ALTER TABLE registered_teams ADD COLUMN team_number INTEGER")
        guild_ids = [r[0] for r in self.db.execute("SELECT DISTINCT guild_id FROM registered_teams").fetchall()]
        for guild_id in guild_ids:
            existing = self.db.execute(
                "SELECT id FROM registered_teams WHERE guild_id=? AND team_number IS NOT NULL",
                (guild_id,),
            ).fetchall()
            next_number = max((int(r[0]) for r in existing), default=0) + 1
            missing = self.db.execute(
                "SELECT id FROM registered_teams WHERE guild_id=? AND team_number IS NULL ORDER BY created_at ASC, id ASC",
                (guild_id,),
            ).fetchall()
            for team_row in missing:
                self.db.execute(
                    "UPDATE registered_teams SET team_number=? WHERE id=? AND team_number IS NULL",
                    (next_number, int(team_row["id"])),
                )
                next_number += 1

        # Migrate announcement tracking to be channel-specific. This allows a
        # previous test-channel post to be repeated in the real announcement channel.
        posted_columns = [r[1] for r in self.db.execute("PRAGMA table_info(approved_team_posted)").fetchall()]
        if posted_columns and "channel_id" not in posted_columns:
            self.db.execute("ALTER TABLE approved_team_posted RENAME TO approved_team_posted_legacy")
        self.db.execute("""CREATE TABLE IF NOT EXISTS approved_team_posted (
            guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, team_id INTEGER NOT NULL,
            message_id INTEGER, PRIMARY KEY(guild_id, channel_id, team_id)
        )""")
        self.db.execute("DROP TABLE IF EXISTS approved_team_posted_legacy")
        self.db.commit()
        for row in self.db.execute("SELECT id FROM team_join_requests WHERE status='pending'").fetchall():
            self.bot.add_view(JoinRequestView(self, row["id"]))

    @staticmethod
    def _key(name):
        return " ".join(name.casefold().split())

    def _team(self, guild_id, name):
        return self.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND name_key=?",
            (guild_id, self._key(name))
        ).fetchone()

    def _resolve_team(self, guild_id, query):
        key = self._key(query)
        if len(key) < 4:
            return None, "short"
        exact = self._team(guild_id, key)
        if exact and exact["status"] == "approved":
            return exact, None
        rows = self.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved' ORDER BY name COLLATE NOCASE",
            (guild_id,)
        ).fetchall()
        matches = [r for r in rows if self._key(r["name"]).startswith(key)]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return matches, "multiple"
        return None, "missing"

    def _membership(self, guild_id, user_id):
        return self.db.execute(
            "SELECT t.* FROM team_players p JOIN registered_teams t ON t.id=p.team_id "
            "WHERE t.guild_id=? AND t.status='approved' AND p.user_id=? "
            "UNION SELECT t.* FROM registered_teams t WHERE t.guild_id=? AND t.status='approved' "
            "AND (t.captain_id=? OR t.vice_captain_id=?) LIMIT 1",
            (guild_id, user_id, guild_id, user_id, user_id)
        ).fetchone()

    def _max_players(self, guild_id):
        row = self.db.execute("SELECT max_players FROM team_limits WHERE guild_id=?", (guild_id,)).fetchone()
        return row["max_players"] if row else 18

    def _team_player_ids(self, team_id):
        team = self.db.execute("SELECT captain_id,vice_captain_id FROM registered_teams WHERE id=?", (team_id,)).fetchone()
        if not team:
            return set()
        ids = {team["captain_id"]}
        if team["vice_captain_id"] is not None:
            ids.add(team["vice_captain_id"])
        ids.update(r["user_id"] for r in self.db.execute("SELECT user_id FROM team_players WHERE team_id=?", (team_id,)).fetchall())
        return ids

    def _team_player_count(self, team_id):
        return len(self._team_player_ids(team_id))

    async def _command_allowed(self, ctx, command_name):
        """Delegate command permissions to the shared utility cog."""
        utility = self.bot.get_cog("Utility")
        if utility is None:
            # Fail closed until the shared permission system is loaded.
            return False
        return await utility.command_allowed(ctx, command_name)

    async def _get_member(self, guild, value):
        value = value.strip()
        match = re.fullmatch(r"<@!?(\d+)>|@(\d+)|(\d+)", value)
        if match:
            uid = int(next(g for g in match.groups() if g))
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except (discord.NotFound, discord.HTTPException):
                    return None
            return member
        lowered = value.casefold()
        for member in guild.members:
            if member.name.casefold() == lowered or member.display_name.casefold() == lowered:
                return member
        try:
            async for member in guild.fetch_members(limit=None):
                if member.name.casefold() == lowered or member.display_name.casefold() == lowered:
                    return member
        except (discord.Forbidden, discord.HTTPException):
            pass
        return None

    async def _dm(self, user, message):
        try:
            await user.send(message)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    def _username(self, guild, user_id):
        member = guild.get_member(user_id) if guild else None
        return member.name if member else str(user_id)

    def _feature_enabled(self, guild_id, feature):
        row = self.db.execute(
            "SELECT enabled FROM team_feature_toggles WHERE guild_id=? AND feature=?", (guild_id, feature)
        ).fetchone()
        return bool(row["enabled"]) if row else True

    async def _feature_blocked(self, ctx, feature):
        if self._feature_enabled(ctx.guild.id, feature):
            return False
        await ctx.reply("This feature has been toggled off by an administrator.", mention_author=False)
        return True

    async def _assign_team_role(self, guild, team_id, member):
        global_setting = self.db.execute(
            "SELECT enabled FROM server_team_role_settings WHERE guild_id=?", (guild.id,)
        ).fetchone()
        if global_setting and not global_setting["enabled"]:
            return
        setting = self.db.execute(
            "SELECT role_id,enabled FROM team_role_settings WHERE team_id=?", (team_id,)
        ).fetchone()
        if not setting or not setting["enabled"]:
            return
        role = guild.get_role(setting["role_id"])
        if role is None or role in member.roles:
            return
        me = guild.me
        if not me or not me.guild_permissions.manage_roles or role >= me.top_role:
            return
        try:
            await member.add_roles(role, reason="Automatic team role assignment")
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _remove_team_role(self, guild, team_id, member):
        if guild is None:
            return
        setting = self.db.execute("SELECT role_id FROM team_role_settings WHERE team_id=?", (team_id,)).fetchone()
        if not setting:
            return
        role = guild.get_role(setting["role_id"])
        me = guild.me
        if role and role in member.roles and me and me.guild_permissions.manage_roles and role < me.top_role:
            try:
                await member.remove_roles(role, reason="Player left team")
            except (discord.Forbidden, discord.HTTPException):
                pass

    def make_teamlist_embed(self, guild_id, page):
        rows = self.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved' "
            "ORDER BY team_number ASC, created_at ASC, id ASC LIMIT 10 OFFSET ?",
            (guild_id, page * 10)
        ).fetchall()
        total = self.db.execute(
            "SELECT COUNT(*) FROM registered_teams WHERE guild_id=? AND status='approved'", (guild_id,)
        ).fetchone()[0]
        embed = discord.Embed(
            title="🏏 Teams • KASHMIR TITANS",
            description=f"Page {page + 1} of {max(1, (total + 9) // 10)}",
            color=discord.Color.from_rgb(55, 150, 255)
        )
        if not rows:
            embed.description += "\n\nNo approved teams yet."
        guild = self.bot.get_guild(guild_id)
        for index, team in enumerate(rows, start=page * 10 + 1):
            count = self._team_player_count(team["id"])
            captain = self._username(guild, team["captain_id"])
            # Requested format: numbered teams in inline code; no cricket-bat emoji per team.
            embed.add_field(
                name=f"`{index}` {team['name']}",
                value=f"**Captain:** {captain}\n**Players:** {count}/{self._max_players(guild_id)}",
                inline=False
            )
        if guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        return embed

    @commands.command(name="teamlist")
    @commands.guild_only()
    async def team_list(self, ctx):
        total = self.db.execute(
            "SELECT COUNT(*) FROM registered_teams WHERE guild_id=? AND status='approved'", (ctx.guild.id,)
        ).fetchone()[0]
        await ctx.send(embed=self.make_teamlist_embed(ctx.guild.id, 0),
                       view=TeamListView(self, ctx.guild.id) if total > 10 else None)

    @commands.command(name="setmaxplayers")
    @commands.guild_only()
    async def set_max_players(self, ctx, maximum: int):
        if not await self._command_allowed(ctx, "setmaxplayers"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        if not 1 <= maximum <= 100:
            await ctx.reply("Choose a maximum between 1 and 100 players per team.", mention_author=False)
            return
        self.db.execute(
            "INSERT INTO team_limits(guild_id,max_players) VALUES(?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET max_players=excluded.max_players",
            (ctx.guild.id, maximum)
        )
        self.db.commit()
        await ctx.reply(
            f"Maximum roster size set to **{maximum} players per team**. Existing oversized rosters won't be deleted, but no further players can be added to a full team.",
            mention_author=False
        )

    @commands.command(name="setcaptain")
    @commands.guild_only()
    async def set_captain(self, ctx, target: str, *, team_name: str):
        if not await self._command_allowed(ctx, "setcaptain"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team with that name.", mention_author=False)
            return
        member = await self._get_member(ctx.guild, target)
        if member is None:
            await ctx.reply("I couldn't find that server member. Use a mention, username, or ID.", mention_author=False)
            return
        if member.id == team["captain_id"]:
            await ctx.reply(f"{member.name} is already the captain of **{team['name']}**.", mention_author=False)
            return
        current = self._membership(ctx.guild.id, member.id)
        if current and current["id"] != team["id"]:
            await ctx.reply(f"{member.name} is already in **{current['name']}**.", mention_author=False)
            return
        if member.id not in self._team_player_ids(team["id"]) and self._team_player_count(team["id"]) >= self._max_players(ctx.guild.id):
            await ctx.reply(f"**{team['name']}** has reached its roster limit. Remove a player before appointing someone new.", mention_author=False)
            return
        old_captain = team["captain_id"]
        self.db.execute(
            "INSERT OR IGNORE INTO team_players(team_id,user_id,added_by,added_at) VALUES(?,?,?,?)",
            (team["id"], old_captain, ctx.author.id, datetime.now(timezone.utc).isoformat())
        )
        self.db.execute("DELETE FROM team_players WHERE team_id=? AND user_id=?", (team["id"], member.id))
        self.db.execute("UPDATE registered_teams SET captain_id=? WHERE id=?", (member.id, team["id"]))
        self.db.commit()
        await ctx.reply(f"**{member.name}** is now captain of **{team['name']}**. The previous captain remains on the roster.", mention_author=False)
        await self._dm(member, f"You have been appointed captain of **{team['name']}** by a server administrator.")

    @commands.command(name="setteamrole", aliases=["setteamroles"])
    @commands.guild_only()
    async def set_team_role(self, ctx, *, arguments: str):
        if not await self._command_allowed(ctx, "setteamrole"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        parts = arguments.rsplit(maxsplit=1)
        if len(parts) != 2 or not parts[1].isdigit():
            await ctx.reply("Usage: `+setteamrole Team Name ROLE_ID`", mention_author=False)
            return
        team_name, role_id_text = parts
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team with that name.", mention_author=False)
            return
        role = ctx.guild.get_role(int(role_id_text))
        if role is None:
            await ctx.reply("That role ID doesn't exist in this server.", mention_author=False)
            return
        self.db.execute(
            "INSERT INTO team_role_settings(team_id,role_id,enabled) VALUES(?,?,1) "
            "ON CONFLICT(team_id) DO UPDATE SET role_id=excluded.role_id,enabled=1",
            (team["id"], role.id)
        )
        self.db.commit()
        # Never use role.mention here; suppress all mentions for this confirmation.
        safe_role_name = discord.utils.escape_markdown(role.name)
        await ctx.reply(
            f"Automatic role assignment for **{team['name']}** is configured to use **{safe_role_name}** (ID: `{role.id}`). "
            "It applies to approved `+jointeam` requests and administrator `+addplayer` additions, not `+bulkadd`.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none()
        )

    @commands.command(name="teamroles", aliases=["approvedteamroles", "listteamroles"])
    @commands.guild_only()
    async def list_approved_team_roles(self, ctx):
        """List configured roles for all approved teams, showing role IDs without pinging."""
        if not await self._command_allowed(ctx, "teamroles"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        teams = self.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved' ORDER BY name COLLATE NOCASE",
            (ctx.guild.id,),
        ).fetchall()
        if not teams:
            await ctx.reply("There are no approved teams in this server.", mention_author=False)
            return

        lines = []
        for team in teams:
            setting = self.db.execute(
                "SELECT role_id, enabled FROM team_role_settings WHERE team_id=?", (team["id"],)
            ).fetchone()
            if not setting:
                role_text = "Not configured"
            else:
                role = ctx.guild.get_role(setting["role_id"])
                if role is None:
                    role_text = f"Role not found (`{setting['role_id']}`)"
                else:
                    safe_name = discord.utils.escape_markdown(role.name).replace("@", "＠")
                    state = "enabled" if setting["enabled"] else "disabled"
                    role_text = f"{safe_name} (`{role.id}`) — {state}"
            lines.append(f"**{discord.utils.escape_markdown(team['name'])}** — {role_text}")

        # Split across multiple embeds/messages to stay within Discord's limits.
        chunks, current = [], []
        length = 0
        for line in lines:
            if length + len(line) + 1 > 3800:
                chunks.append("\n".join(current))
                current, length = [], 0
            current.append(line)
            length += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        for index, chunk in enumerate(chunks, start=1):
            embed = discord.Embed(
                title="Approved Team Roles" if len(chunks) == 1 else f"Approved Team Roles ({index}/{len(chunks)})",
                description=chunk,
                color=discord.Color.from_rgb(55, 150, 255),
            )
            await ctx.reply(embed=embed, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="stopteamroles")
    @commands.guild_only()
    async def stop_team_roles(self, ctx, *, team_name: str):
        if not await self._command_allowed(ctx, "stopteamroles"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team.", mention_author=False)
            return
        row = self.db.execute("SELECT role_id FROM team_role_settings WHERE team_id=?", (team["id"],)).fetchone()
        if not row:
            await ctx.reply("No team role has been configured. Use `+setteamrole` first.", mention_author=False)
            return
        self.db.execute("UPDATE team_role_settings SET enabled=0 WHERE team_id=?", (team["id"],))
        self.db.commit()
        await ctx.reply(f"Automatic role assignment is disabled for **{team['name']}**.", mention_author=False)

    @commands.command(name="startteamroles")
    @commands.guild_only()
    async def start_team_roles(self, ctx, *, team_name: str):
        if not await self._command_allowed(ctx, "startteamroles"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team.", mention_author=False)
            return
        row = self.db.execute("SELECT role_id FROM team_role_settings WHERE team_id=?", (team["id"],)).fetchone()
        if not row:
            await ctx.reply("No team role has been configured. Use `+setteamrole` first.", mention_author=False)
            return
        self.db.execute("UPDATE team_role_settings SET enabled=1 WHERE team_id=?", (team["id"],))
        self.db.commit()
        await ctx.reply(f"Automatic role assignment is enabled for **{team['name']}**.", mention_author=False)

    @commands.command(name="stopallteamroles")
    @commands.guild_only()
    async def stop_all_team_roles(self, ctx):
        if not await self._command_allowed(ctx, "stopallteamroles"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        self.db.execute(
            "INSERT INTO server_team_role_settings(guild_id,enabled) VALUES(?,0) "
            "ON CONFLICT(guild_id) DO UPDATE SET enabled=0", (ctx.guild.id,)
        )
        self.db.commit()
        await ctx.reply("Automatic team-role assignment is now disabled server-wide.", mention_author=False)

    @commands.command(name="startallteamroles")
    @commands.guild_only()
    async def start_all_team_roles(self, ctx):
        if not await self._command_allowed(ctx, "startallteamroles"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        self.db.execute(
            "INSERT INTO server_team_role_settings(guild_id,enabled) VALUES(?,1) "
            "ON CONFLICT(guild_id) DO UPDATE SET enabled=1", (ctx.guild.id,)
        )
        self.db.commit()
        await ctx.reply("Server-wide automatic team-role assignment is enabled. Team-specific settings still apply.", mention_author=False)

    @commands.command(name="syncteamroles", aliases=["syncteamrole", "syncroles"])
    @commands.guild_only()
    async def sync_team_roles(self, ctx):
        """Give existing team members their configured team roles when missing."""
        if not await self._command_allowed(ctx, "syncteamroles"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        guild = ctx.guild
        global_setting = self.db.execute(
            "SELECT enabled FROM server_team_role_settings WHERE guild_id=?", (guild.id,)
        ).fetchone()
        if global_setting and not global_setting["enabled"]:
            await ctx.reply(
                "Server-wide automatic team roles are still disabled. Run `+startallteamroles` first, then retry `+syncteamroles`.",
                mention_author=False,
            )
            return

        teams = self.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved' ORDER BY name COLLATE NOCASE",
            (guild.id,),
        ).fetchall()
        if not teams:
            await ctx.reply("There are no approved teams to synchronise.", mention_author=False)
            return

        added, already, failed, skipped = [], [], [], []
        for team in teams:
            setting = self.db.execute(
                "SELECT role_id, enabled FROM team_role_settings WHERE team_id=?", (team["id"],)
            ).fetchone()
            if not setting:
                skipped.append(f"{team['name']} — no team role configured")
                continue
            if not setting["enabled"]:
                skipped.append(f"{team['name']} — team role assignment is disabled")
                continue
            role = guild.get_role(setting["role_id"])
            if role is None:
                skipped.append(f"{team['name']} — configured role no longer exists")
                continue

            me = guild.me
            if not me or not me.guild_permissions.manage_roles or role >= me.top_role:
                failed.append(f"{team['name']} — bot cannot assign role **{discord.utils.escape_markdown(role.name)}** (check Manage Roles and role hierarchy)")
                continue

            user_ids = self._team_player_ids(team["id"])
            for user_id in sorted(user_ids):
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.NotFound:
                        skipped.append(f"{user_id} — member not found (team: {team['name']})")
                        continue
                    except discord.Forbidden:
                        failed.append(f"{user_id} — bot lacks permission to fetch member (team: {team['name']})")
                        continue
                    except discord.HTTPException as exc:
                        failed.append(f"{user_id} — fetch error {type(exc).__name__} (team: {team['name']})")
                        continue

                if role in member.roles:
                    already.append(f"{member.name} — {team['name']}")
                    continue
                try:
                    await member.add_roles(role, reason=f"Team-role sync requested by {ctx.author}")
                    added.append(f"{member.name} — {team['name']}")
                except discord.Forbidden:
                    failed.append(f"{member.name} — permission denied assigning {role.name} (team: {team['name']})")
                except discord.HTTPException as exc:
                    failed.append(f"{member.name} — role assignment error {type(exc).__name__} (team: {team['name']})")

        # Remove configured team roles from members who are not on the corresponding team.
        # This also cleans up stale roles left behind by older/manual role assignments.
        removed, remove_failed = [], []
        for team in teams:
            setting = self.db.execute(
                "SELECT role_id FROM team_role_settings WHERE team_id=?", (team["id"],)
            ).fetchone()
            if not setting:
                continue
            role = guild.get_role(setting["role_id"])
            if role is None:
                continue
            me = guild.me
            if not me or not me.guild_permissions.manage_roles or role >= me.top_role:
                remove_failed.append(f"{team['name']} — bot cannot remove role **{discord.utils.escape_markdown(role.name)}** (check Manage Roles and role hierarchy)")
                continue

            current_team_ids = self._team_player_ids(team["id"])
            for member in list(guild.members):
                if role not in member.roles or member.id in current_team_ids:
                    continue
                try:
                    await member.remove_roles(role, reason=f"Team-role sync: member is not on {team['name']}")
                    removed.append(f"{member.name} — removed stale role for {team['name']}")
                except discord.Forbidden:
                    remove_failed.append(f"{member.name} — permission denied removing role for {team['name']}")
                except discord.HTTPException as exc:
                    remove_failed.append(f"{member.name} — removal error {type(exc).__name__} (team: {team['name']})")

        failed.extend(remove_failed)

        def format_sync_list(items):
            if not items:
                return "None"
            # Keep each embed field under Discord's 1024-character field-value limit.
            lines, length = [], 0
            for item in items:
                line = f"• {discord.utils.escape_markdown(str(item))}"
                if length + len(line) + 1 > 1000:
                    lines.append(f"…and {len(items) - len(lines)} more")
                    break
                lines.append(line)
                length += len(line) + 1
            return "\n".join(lines) or "None"

        embed = discord.Embed(
            title="Team role synchronisation complete",
            description="Team roles were synchronised: missing roles were added to team members, and configured team roles were removed from members who are not on the corresponding team. Team membership was not changed.",
            color=discord.Color.from_rgb(55, 150, 255),
        )
        embed.add_field(name=f"Roles added ({len(added)})", value=format_sync_list(added), inline=False)
        embed.add_field(name=f"Stale roles removed ({len(removed)})", value=format_sync_list(removed), inline=False)
        embed.add_field(name=f"Already synced ({len(already)})", value=format_sync_list(already), inline=False)
        embed.add_field(name=f"Failed ({len(failed)})", value=format_sync_list(failed), inline=False)
        embed.add_field(name=f"Skipped ({len(skipped)})", value=format_sync_list(skipped), inline=False)
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="toggle")
    @commands.guild_only()
    async def toggle_feature(self, ctx, feature: str):
        if not await self._command_allowed(ctx, "toggle"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        feature = feature.casefold()
        if feature not in ("jointeam", "leaveteam", "createteam"):
            await ctx.reply("Usage: `+toggle jointeam`, `+toggle leaveteam`, or `+toggle createteam`.", mention_author=False)
            return
        value = 0 if self._feature_enabled(ctx.guild.id, feature) else 1
        self.db.execute(
            "INSERT INTO team_feature_toggles(guild_id,feature,enabled) VALUES(?,?,?) "
            "ON CONFLICT(guild_id,feature) DO UPDATE SET enabled=excluded.enabled",
            (ctx.guild.id, feature, value)
        )
        self.db.commit()
        await ctx.reply(f"`{feature}` has been **{'enabled' if value else 'disabled'}** by an administrator.", mention_author=False)

    @commands.command(name="deleteallteams")
    @commands.guild_only()
    async def delete_all_teams(self, ctx):
        if not await self._command_allowed(ctx, "deleteallteams"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        count = self.db.execute("SELECT COUNT(*) FROM registered_teams WHERE guild_id=?", (ctx.guild.id,)).fetchone()[0]
        if not count:
            await ctx.reply("There are no teams to delete.", mention_author=False)
            return
        await ctx.reply(
            f"**Warning:** This will permanently delete all **{count} team(s)** and their rosters and join requests from this server's database. Other servers will not be affected. Confirm below:",
            view=ConfirmDeleteAllView(self, ctx.guild.id, ctx.author.id), mention_author=False
        )

    @commands.command(name="jointeam")
    @commands.guild_only()
    async def join_team(self, ctx, *, team_name: str):
        if await self._feature_blocked(ctx, "jointeam"):
            return
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team with that name.", mention_author=False)
            return
        if team["captain_id"] == ctx.author.id:
            await ctx.reply("You're already the captain of this team.", mention_author=False)
            return
        current = self._membership(ctx.guild.id, ctx.author.id)
        if current:
            await ctx.reply(f"You're already in **{current['name']}**. Use `+leaveteam` before joining another team.", mention_author=False)
            return
        if self._team_player_count(team["id"]) >= self._max_players(ctx.guild.id):
            await ctx.reply(f"**{team['name']}** has reached its maximum roster size.", mention_author=False)
            return
        pending = self.db.execute(
            "SELECT * FROM team_join_requests WHERE guild_id=? AND user_id=? AND status='pending'",
            (ctx.guild.id, ctx.author.id)
        ).fetchone()
        if pending:
            pending_team = self.db.execute("SELECT name FROM registered_teams WHERE id=?", (pending["team_id"],)).fetchone()
            await ctx.reply(f"You already have a pending request for **{pending_team['name']}**.", mention_author=False)
            return
        cur = self.db.execute(
            "INSERT INTO team_join_requests(guild_id,team_id,user_id,status,created_at) VALUES(?,?,?,'pending',?)",
            (ctx.guild.id, team["id"], ctx.author.id, datetime.now(timezone.utc).isoformat())
        )
        request_id = cur.lastrowid
        self.db.commit()
        captain = ctx.guild.get_member(team["captain_id"])
        if captain is None:
            try:
                captain = await ctx.guild.fetch_member(team["captain_id"])
            except discord.HTTPException:
                captain = None
        if captain is None:
            self.db.execute("UPDATE team_join_requests SET status='cancelled' WHERE id=?", (request_id,))
            self.db.commit()
            await ctx.reply("I couldn't contact the captain. Your request was cancelled.", mention_author=False)
            return
        try:
            message = await captain.send(f"**{ctx.author.name}** wants to join **{team['name']}**.", view=JoinRequestView(self, request_id))
            self.db.execute("UPDATE team_join_requests SET message_id=?,channel_id=? WHERE id=?", (message.id, message.channel.id, request_id))
            self.db.commit()
        except discord.Forbidden:
            self.db.execute("UPDATE team_join_requests SET status='cancelled' WHERE id=?", (request_id,))
            self.db.commit()
            await ctx.reply("The captain's DMs are closed. Your request wasn't sent.", mention_author=False)
            return
        await ctx.reply(f"Your request to join **{team['name']}** has been sent to the captain.", mention_author=False)

    @commands.command(name="cancelrequests")
    @commands.guild_only()
    async def cancel_requests(self, ctx, user_id: str):
        if not await self._command_allowed(ctx, "cancelrequests"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        match = re.fullmatch(r"<@!?(\d+)>|(\d+)", user_id.strip())
        if not match:
            await ctx.reply("Usage: `+cancelrequests USER_ID`", mention_author=False)
            return
        uid = int(match.group(1) or match.group(2))
        rows = self.db.execute(
            "SELECT * FROM team_join_requests WHERE guild_id=? AND user_id=? AND status='pending'",
            (ctx.guild.id, uid)
        ).fetchall()
        if not rows:
            await ctx.reply("That user has no pending team-join request.", mention_author=False)
            return
        self.db.execute(
            "UPDATE team_join_requests SET status='cancelled' WHERE guild_id=? AND user_id=? AND status='pending'",
            (ctx.guild.id, uid)
        )
        self.db.commit()
        for row in rows:
            channel = self.bot.get_channel(row["channel_id"]) if row["channel_id"] else None
            if channel and row["message_id"]:
                try:
                    msg = await channel.fetch_message(row["message_id"])
                    await msg.edit(content="This request was cancelled by an administrator.", view=None)
                except (discord.HTTPException, discord.NotFound):
                    pass
        user = self.bot.get_user(uid)
        if user is None:
            try:
                user = await self.bot.fetch_user(uid)
            except discord.HTTPException:
                user = None
        if user:
            await self._dm(user, "Your pending team-join request was cancelled by an administrator.")
        await ctx.reply(f"Cancelled **{len(rows)}** pending request(s) for `{uid}`.", mention_author=False)

    @commands.command(name="disbandteam")
    @commands.guild_only()
    async def disband_team(self, ctx, *, team_name: str):
        if not await self._command_allowed(ctx, "disbandteam"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        team = self._team(ctx.guild.id, team_name)
        if not team:
            await ctx.reply("I couldn't find a team with that name.", mention_author=False)
            return
        name, captain_id, team_id = team["name"], team["captain_id"], team["id"]
        self.db.execute("DELETE FROM team_join_requests WHERE team_id=?", (team_id,))
        self.db.execute("DELETE FROM team_players WHERE team_id=?", (team_id,))
        self.db.execute("DELETE FROM registered_teams WHERE id=?", (team_id,))
        self.db.commit()
        try:
            captain = self.bot.get_user(captain_id) or await self.bot.fetch_user(captain_id)
            await self._dm(captain, f"Your team **{name}** has been disbanded by a server administrator.")
        except discord.HTTPException:
            pass
        await ctx.reply(f"Team **{name}** has been disbanded and removed from the database.", mention_author=False)

    @commands.command(name="setteamapproval")
    @commands.guild_only()
    async def set_team_approval(self, ctx, channel: discord.TextChannel):
        if not await self._command_allowed(ctx, "setteamapproval"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        self.db.execute(
            "INSERT INTO team_settings(guild_id,approval_channel_id) VALUES(?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET approval_channel_id=excluded.approval_channel_id",
            (ctx.guild.id, channel.id)
        )
        self.db.commit()
        await ctx.reply(f"Team approval channel set to {channel.mention}.", mention_author=False)

    @commands.command(name="createteam")
    @commands.guild_only()
    async def create_team(self, ctx):
        if await self._feature_blocked(ctx, "createteam"):
            return
        if self._membership(ctx.guild.id, ctx.author.id):
            current = self._membership(ctx.guild.id, ctx.author.id)
            await ctx.reply(f"You're already in **{current['name']}**. Use `+leaveteam` before creating another team.", mention_author=False)
            return
        setting = self.db.execute("SELECT approval_channel_id FROM team_settings WHERE guild_id=?", (ctx.guild.id,)).fetchone()
        if not setting:
            await ctx.reply("Team applications aren't configured yet. An administrator must use `+setteamapproval #channel` first.", mention_author=False)
            return
        approval_channel = ctx.guild.get_channel(setting["approval_channel_id"])
        if not isinstance(approval_channel, discord.TextChannel):
            await ctx.reply("The configured approval channel no longer exists. Ask an administrator to set it again.", mention_author=False)
            return
        if not await self._dm(ctx.author, "Let's register your team! Reply with the team name. Type `cancel` to stop."):
            await ctx.reply("I couldn't DM you. Enable server DMs and run `+createteam` again.", mention_author=False)
            return
        await ctx.reply("I've sent you a DM to complete team registration.", mention_author=False)

        def check(message):
            return message.author.id == ctx.author.id and isinstance(message.channel, discord.DMChannel)

        try:
            name_msg = await self.bot.wait_for("message", timeout=300, check=check)
            if name_msg.content.strip().casefold() == "cancel":
                await name_msg.channel.send("Team registration cancelled.")
                return
            name = " ".join(name_msg.content.strip().split())
            if not name or len(name) > 50:
                await name_msg.channel.send("Team names must be between 1 and 50 characters. Run `+createteam` to try again.")
                return
            if self._team(ctx.guild.id, name):
                await name_msg.channel.send("A team with that name already exists or is pending approval.")
                return
            await name_msg.channel.send("Now send your team logo as an image attachment or a direct image URL. Type `cancel` to stop.")
            logo_msg = await self.bot.wait_for("message", timeout=300, check=check)
            if logo_msg.content.strip().casefold() == "cancel":
                await logo_msg.channel.send("Team registration cancelled.")
                return
            logo_url = None
            if logo_msg.attachments:
                attachment = next((a for a in logo_msg.attachments if (a.content_type or "").startswith("image/")), None)
                if attachment:
                    logo_url = attachment.url
            if not logo_url:
                candidate = logo_msg.content.strip().strip("<>")
                parsed = urlparse(candidate)
                if parsed.scheme in ("http", "https") and parsed.netloc and re.search(r"\.(png|jpe?g|gif|webp)(?:$|\?)", candidate, re.I):
                    logo_url = candidate
            if not logo_url:
                await logo_msg.channel.send("I couldn't recognize that as an image. Upload an image or send a direct image URL, then run `+createteam` again.")
                return
            try:
                next_team_number = self.db.execute(
                    "SELECT COALESCE(MAX(team_number), 0) + 1 FROM registered_teams WHERE guild_id=?",
                    (ctx.guild.id,),
                ).fetchone()[0]
                cur = self.db.execute(
                    "INSERT INTO registered_teams(guild_id,name,name_key,captain_id,logo_url,status,applicant_id,approval_channel_id,created_at,team_number) "
                    "VALUES(?,?,?,?,?,'pending',?,?,?,?)",
                    (ctx.guild.id, name, self._key(name), ctx.author.id, logo_url, ctx.author.id, approval_channel.id, datetime.now(timezone.utc).isoformat(), next_team_number)
                )
                team_id = cur.lastrowid
                self.db.commit()
            except sqlite3.IntegrityError:
                await logo_msg.channel.send("A team with that name already exists or is pending approval.")
                return
            embed = discord.Embed(title="Team Application", color=discord.Color.from_rgb(40, 150, 255), timestamp=discord.utils.utcnow())
            embed.add_field(name="Team name", value=name, inline=False)
            embed.add_field(name="Applicant / proposed captain", value=f"{ctx.author.name} (`{ctx.author.id}`)", inline=False)
            embed.add_field(name="Status", value="Pending approval", inline=False)
            embed.set_thumbnail(url=logo_url)
            embed.set_footer(text=f"Team application ID: {team_id}")
            try:
                post = await approval_channel.send(embed=embed, view=TeamApprovalView(self, team_id))
                self.db.execute("UPDATE registered_teams SET approval_message_id=? WHERE id=?", (post.id, team_id))
                self.db.commit()
            except discord.HTTPException:
                self.db.execute("DELETE FROM registered_teams WHERE id=?", (team_id,))
                self.db.commit()
                await logo_msg.channel.send("I couldn't post the application. Check the bot's channel permissions.")
                return
            await logo_msg.channel.send(f"Your application for **{name}** has been submitted. Captain status becomes active after approval.")
        except asyncio.TimeoutError:
            await self._dm(ctx.author, "Team registration timed out. Run `+createteam` to start again.")

    async def decide_application(self, interaction, team_id, decision):
        row = self.db.execute(
            "SELECT * FROM registered_teams WHERE id=? AND guild_id=?", (team_id, interaction.guild_id)
        ).fetchone()
        if not row:
            await interaction.response.send_message("This application could not be found.", ephemeral=True)
            return
        if row["status"] != "pending":
            await interaction.response.send_message(f"This application was already {row['status']}.", ephemeral=True)
            return
        self.db.execute(
            "UPDATE registered_teams SET status=?,decided_by=? WHERE id=? AND status='pending'",
            (decision, interaction.user.id, team_id)
        )
        if decision == "approved":
            self.db.execute(
                "INSERT OR IGNORE INTO team_players(team_id,user_id,added_by,added_at) VALUES(?,?,?,?)",
                (team_id, row["captain_id"], interaction.user.id, datetime.now(timezone.utc).isoformat())
            )
        self.db.commit()
        # Announce newly approved teams in the owner-selected channel. Keep this
        # isolated so a Discord posting failure never blocks the approval itself.
        if decision == "approved":
            try:
                settings = self.db.execute(
                    "SELECT channel_id FROM approved_team_post_settings WHERE guild_id=?",
                    (int(interaction.guild_id),),
                ).fetchone()
                already_posted = self.db.execute(
                    "SELECT 1 FROM approved_team_posted WHERE guild_id=? AND channel_id=? AND team_id=?",
                    (int(interaction.guild_id), int(settings["channel_id"]), int(team_id)),
                ).fetchone()
                if settings and not already_posted:
                    channel = self.bot.get_channel(int(settings["channel_id"]))
                    if channel is None:
                        channel = await self.bot.fetch_channel(int(settings["channel_id"]))
                    if isinstance(channel, discord.TextChannel) and channel.guild.id == int(interaction.guild_id):
                        approved = self.db.execute(
                            "SELECT id, name, captain_id, logo_url, team_number FROM registered_teams "
                            "WHERE guild_id=? AND status='approved' ORDER BY team_number ASC",
                            (int(interaction.guild_id),),
                        ).fetchall()
                        number_row = self.db.execute(
                            "SELECT team_number FROM registered_teams WHERE id=? AND guild_id=?",
                            (int(team_id), int(interaction.guild_id)),
                        ).fetchone()
                        number = int(number_row["team_number"]) if number_row and number_row["team_number"] is not None else len(approved)
                        safe_name = discord.utils.escape_markdown(str(row["name"] or "Unknown Team"))
                        captain_id = int(row["captain_id"])
                        logo_url = str(row["logo_url"] or "").strip()
                        team_embed = discord.Embed(
                            title="Kashmir Titans S1",
                            description=(f"{number}. **__Team name:__** **`{safe_name}`**\n"
                                         f"**__Team captain:__** <@{captain_id}>"),
                            color=discord.Color.from_rgb(55, 150, 255),
                        )
                        if logo_url.startswith(("https://", "http://")):
                            team_embed.set_thumbnail(url=logo_url)
                            team_embed.set_image(url=logo_url)
                        # Mention is shown in the embed only. Discord won't notify for embed mentions.
                        posted_message = await channel.send(
                            embed=team_embed,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        self.db.execute(
                            "INSERT OR IGNORE INTO approved_team_posted(guild_id, channel_id, team_id, message_id) VALUES(?,?,?,?)",
                            (int(interaction.guild_id), int(settings["channel_id"]), int(team_id), int(posted_message.id)),
                        )
                        self.db.commit()
            except (sqlite3.Error, discord.HTTPException, KeyError, TypeError, ValueError):
                pass
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else discord.Embed(title="Team Application")
        embed.color = discord.Color.green() if decision == "approved" else discord.Color.red()
        embed.add_field(name="Decision", value=f"{decision.title()} by {interaction.user.name}", inline=False)
        await interaction.response.edit_message(embed=embed, view=None)
        applicant = self.bot.get_user(row["applicant_id"])
        if applicant is None:
            try:
                applicant = await self.bot.fetch_user(row["applicant_id"])
            except discord.HTTPException:
                applicant = None
        if applicant:
            msg = f"Congratulations! Your team **{row['name']}** has been approved. You are now its captain." if decision == "approved" else f"Your application for **{row['name']}** was rejected."
            await self._dm(applicant, msg)

    @commands.command(name="vc")
    @commands.guild_only()
    async def set_vice_captain(self, ctx, target: str, *, team_name: str):
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team.", mention_author=False)
            return
        if ctx.author.id != team["captain_id"]:
            await ctx.reply("Only the team captain can use this command.", mention_author=False)
            return
        member = await self._get_member(ctx.guild, target)
        if member is None:
            await ctx.reply("I couldn't find that server member. Use a mention, username, or ID.", mention_author=False)
            return
        if member.id == team["captain_id"]:
            await ctx.reply("The captain cannot also be the vice-captain.", mention_author=False)
            return
        current = self._membership(ctx.guild.id, member.id)
        if current and current["id"] != team["id"]:
            await ctx.reply(f"{member.name} is already in **{current['name']}**.", mention_author=False)
            return
        if member.id not in self._team_player_ids(team["id"]) and self._team_player_count(team["id"]) >= self._max_players(ctx.guild.id):
            await ctx.reply(f"**{team['name']}** has reached its maximum roster size.", mention_author=False)
            return
        self.db.execute("UPDATE registered_teams SET vice_captain_id=? WHERE id=?", (member.id, team["id"]))
        self.db.execute(
            "INSERT OR IGNORE INTO team_players(team_id,user_id,added_by,added_at) VALUES(?,?,?,?)",
            (team["id"], member.id, ctx.author.id, datetime.now(timezone.utc).isoformat())
        )
        self.db.commit()
        await ctx.reply(f"{member.name} is now vice-captain of **{team['name']}**.", mention_author=False)
        await self._dm(member, f"You have been appointed vice-captain of **{team['name']}**.")

    @commands.command(name="addplayer")
    @commands.guild_only()
    async def add_player(self, ctx, target: str, *, team_name: str):
        if not await self._command_allowed(ctx, "addplayer"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        member = await self._get_member(ctx.guild, target)
        if member is None:
            await ctx.reply("I couldn't find that server member. Use a mention, username, or ID.", mention_author=False)
            return
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team.", mention_author=False)
            return
        current = self._membership(ctx.guild.id, member.id)
        if current:
            await ctx.reply(f"{member.name} is already in **{current['name']}**.", mention_author=False)
            return
        if self._team_player_count(team["id"]) >= self._max_players(ctx.guild.id):
            await ctx.reply(f"**{team['name']}** has reached its maximum roster size.", mention_author=False)
            return
        try:
            self.db.execute(
                "INSERT INTO team_players(team_id,user_id,added_by,added_at) VALUES(?,?,?,?)",
                (team["id"], member.id, ctx.author.id, datetime.now(timezone.utc).isoformat())
            )
            self.db.commit()
        except sqlite3.IntegrityError:
            await ctx.reply(f"{member.name} is already on that team.", mention_author=False)
            return
        await self._assign_team_role(ctx.guild, team["id"], member)
        await ctx.reply(f"Added {member.name} to **{team['name']}**.", mention_author=False)
        await self._dm(member, f"You have been added by an administrator to **{team['name']}**.")

    @commands.command(name="addallplayers")
    @commands.guild_only()
    async def add_all_players(self, ctx, *, team_name: str):
        """Add every username in the message this command replies to, with the team's configured role."""
        if not await self._command_allowed(ctx, "addallplayers"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        source = ctx.message.reference
        if source is None or source.message_id is None:
            await ctx.reply(
                "Reply directly to the squad-list message and use `+addallplayers Team Name`.",
                mention_author=False
            )
            return

        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team with that name. No players were added.", mention_author=False)
            return

        role_setting = self.db.execute(
            "SELECT role_id FROM team_role_settings WHERE team_id=?", (team["id"],)
        ).fetchone()
        if not role_setting:
            await ctx.reply(
                f"No team role is configured for **{team['name']}**. Use `+setteamrole` first. No players were added.",
                mention_author=False
            )
            return
        role = ctx.guild.get_role(role_setting["role_id"])
        if role is None:
            await ctx.reply(
                f"The configured role for **{team['name']}** no longer exists. Fix it with `+setteamrole`. No players were added.",
                mention_author=False
            )
            return
        me = ctx.guild.me
        if not me or not me.guild_permissions.manage_roles or role >= me.top_role:
            await ctx.reply(
                f"I can't assign **{discord.utils.escape_markdown(role.name)}**. Give me Manage Roles and move my highest role above the team role. No players were added.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none()
            )
            return

        try:
            squad_message = await ctx.channel.fetch_message(source.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await ctx.reply("I couldn't fetch the replied-to squad list. No players were added.", mention_author=False)
            return

        # Accept numbered lists such as `1. username (C)` and strip captain/vice-captain labels.
        usernames = []
        for line in squad_message.content.splitlines():
            line = line.strip()
            if not line:
                continue
            line = re.sub(r"^\s*(?:\d+|[-*•])\s*[.)-]?\s*", "", line)
            line = re.sub(r"\s*\((?:C|VC|CAPTAIN|VICE[- ]CAPTAIN)\)\s*$", "", line, flags=re.IGNORECASE)
            line = line.strip().strip("`* ")
            if not line or line.startswith(("http://", "https://")):
                continue
            # Ignore headings and prose; keep likely Discord usernames, mentions, or IDs.
            if re.fullmatch(r"(?:<@!?\d+>|@?\d+|[\w.]{2,32})", line):
                usernames.append(line)

        if not usernames:
            await ctx.reply("I couldn't find any usernames in the replied-to message. No players were added.", mention_author=False)
            return

        added_names, already_names, failed_names, full_names = [], [], [], []
        seen = set()
        for username in usernames:
            lookup_key = username.casefold()
            if lookup_key in seen:
                continue
            seen.add(lookup_key)
            member = await self._get_member(ctx.guild, username)
            if member is None:
                failed_names.append(f"{username} — couldn't find/fetch member")
                continue

            current = self._membership(ctx.guild.id, member.id)
            if current:
                already_names.append(f"{member.name} — {current['name']}")
                continue

            if self._team_player_count(team["id"]) >= self._max_players(ctx.guild.id):
                full_names.append(member.name)
                continue

            try:
                # Assign the configured role first. If Discord rejects it, do not add the player to the database.
                if role not in member.roles:
                    await member.add_roles(role, reason=f"Bulk squad import for {team['name']} by {ctx.author}")
                self.db.execute(
                    "INSERT INTO team_players(team_id,user_id,added_by,added_at) VALUES(?,?,?,?)",
                    (team["id"], member.id, ctx.author.id, datetime.now(timezone.utc).isoformat())
                )
                self.db.commit()
                added_names.append(member.name)
            except sqlite3.IntegrityError:
                already_names.append(f"{member.name} — {team['name']}")
            except (discord.Forbidden, discord.HTTPException) as exc:
                failed_names.append(f"{member.name} — role assignment failed ({type(exc).__name__})")
            except Exception as exc:
                failed_names.append(f"{member.name} — error ({type(exc).__name__})")

        def format_names(names):
            if not names:
                return "None"
            return "\n".join(f"• {discord.utils.escape_markdown(name)}" for name in names[:25]) + (f"\n…and {len(names) - 25} more." if len(names) > 25 else "")

        embed = discord.Embed(
            title=f"Squad import complete • {team['name']}",
            description=f"Team: **{discord.utils.escape_markdown(team['name'])}**\nRole assigned: **{discord.utils.escape_markdown(role.name)}**",
            color=discord.Color.from_rgb(55, 150, 255)
        )
        embed.add_field(name=f"Added successfully ({len(added_names)})", value=format_names(added_names), inline=False)
        embed.add_field(name=f"Already in a team ({len(already_names)})", value=format_names(already_names), inline=False)
        embed.add_field(name=f"Couldn't find/fetch or error ({len(failed_names)})", value=format_names(failed_names), inline=False)
        if full_names:
            embed.add_field(name=f"Not added — team full ({len(full_names)})", value=format_names(full_names), inline=False)
        embed.set_footer(text=f"Roster: {self._team_player_count(team['id'])}/{self._max_players(ctx.guild.id)} players")
        await ctx.reply(embed=embed, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    # bulkadd intentionally kept functionally unchanged from the user's supplied code.
    @commands.command(name="bulkadd")
    @commands.guild_only()
    async def bulk_add(self, ctx, *, arguments: str):
        if not await self._command_allowed(ctx, "bulkadd"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        parts = arguments.rsplit(maxsplit=1)
        if len(parts) != 2 or not parts[1].isdigit():
            await ctx.reply("Usage: `+bulkadd Team Name ROLE_ID`", mention_author=False)
            return
        team_name, role_id_text = parts
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team.", mention_author=False)
            return
        role = ctx.guild.get_role(int(role_id_text))
        if role is None:
            await ctx.reply("That role ID doesn't exist in this server.", mention_author=False)
            return
        members = list(role.members)
        if not members:
            try:
                async for member in ctx.guild.fetch_members(limit=None):
                    if role in member.roles:
                        members.append(member)
            except (discord.Forbidden, discord.HTTPException):
                await ctx.reply("I couldn't fetch the server member list. Check the Members intent and bot permissions.", mention_author=False)
                return
        added_names, skipped_names, dm_failed_names, full_names = [], [], [], []
        seen_ids = set()
        members = [m for m in members if not (m.id in seen_ids or seen_ids.add(m.id))]
        for member in members:
            current = self._membership(ctx.guild.id, member.id)
            if current:
                skipped_names.append(f"{member.name} ({current['name']})")
                continue
            if self._team_player_count(team["id"]) >= self._max_players(ctx.guild.id):
                full_names.append(member.name)
                continue
            try:
                self.db.execute(
                    "INSERT INTO team_players(team_id,user_id,added_by,added_at) VALUES(?,?,?,?)",
                    (team["id"], member.id, ctx.author.id, datetime.now(timezone.utc).isoformat())
                )
                self.db.commit()
                added_names.append(member.name)
            except sqlite3.IntegrityError:
                skipped_names.append(f"{member.name} (already on this team)")
                continue
            if not await self._dm(member, f"You have been added to **{team['name']}** by a server administrator."):
                dm_failed_names.append(member.name)

        def format_names(names):
            if not names:
                return "None"
            return "\n".join(f"• {name}" for name in names[:25]) + (f"\n…and {len(names) - 25} more." if len(names) > 25 else "")
        embed = discord.Embed(title=f"Bulk-add complete • {team['name']}", color=discord.Color.from_rgb(55, 150, 255))
        embed.add_field(name=f"Added ({len(added_names)})", value=format_names(added_names), inline=False)
        embed.add_field(name=f"Already in a team ({len(skipped_names)})", value=format_names(skipped_names), inline=False)
        embed.add_field(name=f"DM unavailable ({len(dm_failed_names)})", value=format_names(dm_failed_names), inline=False)
        if full_names:
            embed.add_field(name=f"Not added — team full ({len(full_names)})", value=format_names(full_names), inline=False)
        embed.set_footer(text=f"Roster: {self._team_player_count(team['id'])}/{self._max_players(ctx.guild.id)} players")
        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="leaveteam")
    @commands.guild_only()
    async def leave_team(self, ctx):
        if await self._feature_blocked(ctx, "leaveteam"):
            return
        team = self._membership(ctx.guild.id, ctx.author.id)
        if not team:
            await ctx.reply("You're not currently in an approved team.", mention_author=False)
            return
        if ctx.author.id in (team["captain_id"], team["vice_captain_id"]):
            await ctx.reply("Captains and vice-captains cannot leave their team. Ask an administrator to change the leadership first.", mention_author=False)
            return
        await ctx.reply(
            f"Are you sure you want to leave **{team['name']}**?",
            view=LeaveTeamConfirmView(self, ctx.author.id, team["id"], ctx.guild.id, team["name"]),
            mention_author=False
        )

    @commands.command(name="teaminfo")
    @commands.guild_only()
    async def team_info(self, ctx, *, team_name: str):
        team, error = self._resolve_team(ctx.guild.id, team_name)
        if error == "short":
            await ctx.reply("Enter at least the first **4 letters** of the team name.", mention_author=False)
            return
        if error == "multiple":
            await ctx.reply("More than one team matches that prefix. Type more letters:\n" + "\n".join(f"• {r['name']}" for r in team[:20]), mention_author=False)
            return
        if not team:
            await ctx.reply("I couldn't find an approved team matching that name or prefix.", mention_author=False)
            return
        other_rows = self.db.execute("SELECT user_id FROM team_players WHERE team_id=? ORDER BY added_at", (team["id"],)).fetchall()
        ordered_ids = list(dict.fromkeys([team["captain_id"]] + [r["user_id"] for r in other_rows]))
        embed = discord.Embed(title=f"🏏 {team['name']} • Roster", color=discord.Color.from_rgb(55, 150, 255))
        embed.add_field(name="Captain", value=f"{self._username(ctx.guild, team['captain_id'])} {CAPTAIN_EMOJI}", inline=True)
        vice = f"{self._username(ctx.guild, team['vice_captain_id'])} {VICE_CAPTAIN_EMOJI}" if team["vice_captain_id"] else "Not assigned"
        embed.add_field(name="Vice-Captain", value=vice, inline=True)
        roster = []
        for index, uid in enumerate(ordered_ids, start=1):
            emoji = f" {CAPTAIN_EMOJI}" if uid == team["captain_id"] else (f" {VICE_CAPTAIN_EMOJI}" if uid == team["vice_captain_id"] else "")
            roster.append(f"{index}. {self._username(ctx.guild, uid)}{emoji}")
        embed.add_field(name=f"Players ({len(ordered_ids)}/{self._max_players(ctx.guild.id)})", value="\n".join(roster) if roster else "No players yet.", inline=False)
        if team["logo_url"]:
            embed.set_thumbnail(url=team["logo_url"])
        await ctx.send(embed=embed)

    @commands.command(name="kickplayer")
    @commands.guild_only()
    async def kick_player(self, ctx, target: str, *, team_name: str):
        if not await self._command_allowed(ctx, "kickplayer"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        member = await self._get_member(ctx.guild, target)
        if member is None:
            await ctx.reply("I couldn't find that server member. Use a mention, username, or ID.", mention_author=False)
            return
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team.", mention_author=False)
            return
        if member.id == team["captain_id"]:
            await ctx.reply("You can't kick the team captain. Change the captain first.", mention_author=False)
            return
        cur = self.db.execute("DELETE FROM team_players WHERE team_id=? AND user_id=?", (team["id"], member.id))
        if cur.rowcount == 0:
            await ctx.reply(f"{member.name} isn't listed in **{team['name']}**.", mention_author=False)
            return
        if team["vice_captain_id"] == member.id:
            self.db.execute("UPDATE registered_teams SET vice_captain_id=NULL WHERE id=?", (team["id"],))
        self.db.commit()
        await self._remove_team_role(ctx.guild, team["id"], member)
        await ctx.reply(f"Removed {member.name} from **{team['name']}**.", mention_author=False)
        await self._dm(member, f"An administrator has removed you from **{team['name']}**.")


async def setup(bot):
    await bot.add_cog(TeamRegistration(bot))
