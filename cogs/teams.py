import asyncio
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

import discord
from discord.ext import commands


class TeamApprovalView(discord.ui.View):
    def __init__(self, cog, team_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.team_id = team_id

    async def _authorized(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This button can only be used inside the server.", ephemeral=True)
            return False
        channel = interaction.channel
        if not isinstance(channel, discord.abc.GuildChannel):
            await interaction.response.send_message("This is not a valid approval channel.", ephemeral=True)
            return False
        perms = channel.permissions_for(interaction.user)
        if not (perms.view_channel and (perms.send_messages or perms.manage_messages or interaction.user.guild_permissions.administrator)):
            await interaction.response.send_message("You don't have permission to review applications in this channel.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="teamreg:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._authorized(interaction):
            return
        await self.cog.decide_application(interaction, self.team_id, "approved")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="teamreg:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._authorized(interaction):
            return
        await self.cog.decide_application(interaction, self.team_id, "rejected")


class TeamListView(discord.ui.View):
    def __init__(self, cog, guild_id: int, page: int = 0):
        super().__init__(timeout=300)
        self.cog, self.guild_id, self.page = cog, guild_id, page
        self._sync_buttons()

    def _sync_buttons(self):
        self.clear_items()
        prev = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=self.page <= 0)
        nxt = discord.ui.Button(label="Next", style=discord.ButtonStyle.primary)
        prev.callback = self.previous_page
        nxt.callback = self.next_page
        self.add_item(prev)
        self.add_item(nxt)

    async def _show(self, interaction):
        total = self.cog.db.execute("SELECT COUNT(*) FROM registered_teams WHERE guild_id=? AND status='approved'", (self.guild_id,)).fetchone()[0]
        max_page = max(0, (total - 1) // 10)
        self.page = max(0, min(self.page, max_page))
        embed = self.cog.make_teamlist_embed(self.guild_id, self.page)
        self._sync_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    async def previous_page(self, interaction):
        await self._show_page(interaction, self.page - 1)

    async def next_page(self, interaction):
        await self._show_page(interaction, self.page + 1)

    async def _show_page(self, interaction, page):
        self.page = page
        await self._show(interaction)


class JoinRequestView(discord.ui.View):
    def __init__(self, cog, request_id: int):
        super().__init__(timeout=None)
        self.cog, self.request_id = cog, request_id
        accept = discord.ui.Button(label="Accept", style=discord.ButtonStyle.success, custom_id=f"teamjoin:accept:{request_id}")
        deny = discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger, custom_id=f"teamjoin:deny:{request_id}")
        accept.callback = self.accept_request
        deny.callback = self.deny_request
        self.add_item(accept)
        self.add_item(deny)

    async def _decide(self, interaction, decision):
        row = self.cog.db.execute("SELECT r.*, t.name AS team_name, t.captain_id, t.status AS team_status FROM team_join_requests r JOIN registered_teams t ON t.id=r.team_id WHERE r.id=?", (self.request_id,)).fetchone()
        if not row or row['status'] != 'pending':
            for item in self.children: item.disabled = True
            if interaction.response.is_done():
                await interaction.followup.send("This request was cancelled or already handled; these buttons no longer work.", ephemeral=True)
            else:
                await interaction.response.edit_message(view=self)
                await interaction.followup.send("This request was cancelled or already handled; these buttons no longer work.", ephemeral=True)
            return
        if interaction.user.id != row['captain_id']:
            await interaction.response.send_message("Only this team's captain can respond to the request.", ephemeral=True)
            return
        if row['team_status'] != 'approved':
            await interaction.response.send_message("This team is no longer active.", ephemeral=True)
            return
        self.cog.db.execute("UPDATE team_join_requests SET status=? WHERE id=? AND status='pending'", (decision, self.request_id))
        if decision == 'accepted':
            current = self.cog._membership(row['guild_id'], row['user_id'])
            if current and current['id'] != row['team_id']:
                self.cog.db.execute("UPDATE team_join_requests SET status='denied' WHERE id=?", (self.request_id,))
                self.cog.db.commit()
                for item in self.children: item.disabled = True
                await interaction.response.edit_message(content="This player has already joined another team. Request closed.", embed=None, view=self)
                try:
                    user = self.cog.bot.get_user(row['user_id']) or await self.cog.bot.fetch_user(row['user_id'])
                    await self.cog._dm(user, f"Your request to join **{row['team_name']}** was denied because you're already in **{current['name']}**.")
                except discord.HTTPException:
                    pass
                return
            try:
                self.cog.db.execute("INSERT INTO team_players(team_id,user_id,added_by,added_at) VALUES(?,?,?,?)", (row['team_id'], row['user_id'], row['captain_id'], datetime.now(timezone.utc).isoformat()))
            except sqlite3.IntegrityError:
                pass
        self.cog.db.commit()
        for item in self.children: item.disabled = True
        await interaction.response.edit_message(content=f"Request **{decision}** by {interaction.user.mention}.", embed=None, view=self)
        try:
            user = self.cog.bot.get_user(row['user_id']) or await self.cog.bot.fetch_user(row['user_id'])
            await self.cog._dm(user, f"Your request to join **{row['team_name']}** was {decision} by the captain.")
        except discord.HTTPException:
            pass

    async def accept_request(self, interaction):
        await self._decide(interaction, 'accepted')

    async def deny_request(self, interaction):
        await self._decide(interaction, 'denied')


class LeaveTeamConfirmView(discord.ui.View):
    def __init__(self, cog, user_id: int, team_id: int, guild_id: int, team_name: str):
        super().__init__(timeout=60)
        self.cog, self.user_id, self.team_id, self.guild_id, self.team_name = cog, user_id, team_id, guild_id, team_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the player who started this action can confirm it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, leave team", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        team = self.cog._membership(self.guild_id, self.user_id)
        if not team or team["id"] != self.team_id:
            await interaction.response.edit_message(content="You're no longer in that team.", view=None)
            return
        if self.user_id in (team["captain_id"], team["vice_captain_id"]):
            await interaction.response.edit_message(content="Captains and vice-captains cannot leave their team.", view=None)
            return
        self.cog.db.execute("DELETE FROM team_players WHERE team_id=? AND user_id=?", (self.team_id, self.user_id))
        self.cog.db.commit()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"You have left **{self.team_name}**. You can now join or create another team.", view=self)

    @discord.ui.button(label="No, stay", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"Cancelled. You're still in **{self.team_name}**.", view=self)


class TeamRegistration(commands.Cog):
    """Team registration and roster management, stored separately from match/stat tables."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = sqlite3.connect("team_registration.db")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self._init_db()

    def cog_unload(self):
        self.db.close()

    def _init_db(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS team_settings (
            guild_id INTEGER PRIMARY KEY,
            approval_channel_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS registered_teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            name_key TEXT NOT NULL,
            captain_id INTEGER NOT NULL,
            vice_captain_id INTEGER,
            logo_url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            applicant_id INTEGER NOT NULL,
            approval_channel_id INTEGER,
            approval_message_id INTEGER,
            created_at TEXT NOT NULL,
            decided_by INTEGER,
            UNIQUE(guild_id, name_key)
        );
        CREATE TABLE IF NOT EXISTS team_players (
            team_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            added_by INTEGER NOT NULL,
            added_at TEXT NOT NULL,
            PRIMARY KEY(team_id, user_id),
            FOREIGN KEY(team_id) REFERENCES registered_teams(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS team_join_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            message_id INTEGER,
            channel_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(team_id) REFERENCES registered_teams(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_pending_join_per_user
        ON team_join_requests(guild_id, user_id) WHERE status='pending';
        """)
        self.db.commit()
        # Restore pending join-request buttons after a restart.
        for row in self.db.execute("SELECT id FROM team_join_requests WHERE status='pending'").fetchall():
            self.bot.add_view(JoinRequestView(self, row['id']))

    @staticmethod
    def _key(name: str) -> str:
        return " ".join(name.casefold().split())

    def _team(self, guild_id: int, name: str):
        return self.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND name_key=?",
            (guild_id, self._key(name)),
        ).fetchone()

    def _membership(self, guild_id: int, user_id: int):
        return self.db.execute(
            "SELECT t.* FROM team_players p JOIN registered_teams t ON t.id=p.team_id "
            "WHERE t.guild_id=? AND t.status='approved' AND p.user_id=? LIMIT 1",
            (guild_id, user_id),
        ).fetchone()

    def _is_admin(self, ctx: commands.Context) -> bool:
        return bool(ctx.guild and isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator)

    async def _get_member(self, guild: discord.Guild, value: str):
        value = value.strip()
        match = re.fullmatch(r"<@!?(\d+)>|@(\d+)|(\d+)", value)
        if not match:
            return None
        user_id = int(next(group for group in match.groups() if group))
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.HTTPException):
                return None
        return member

    async def _dm(self, user: discord.abc.User, message: str):
        try:
            await user.send(message)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    def make_teamlist_embed(self, guild_id: int, page: int):
        rows = self.db.execute("SELECT * FROM registered_teams WHERE guild_id=? AND status='approved' ORDER BY name COLLATE NOCASE LIMIT 10 OFFSET ?", (guild_id, page * 10)).fetchall()
        total = self.db.execute("SELECT COUNT(*) FROM registered_teams WHERE guild_id=? AND status='approved'", (guild_id,)).fetchone()[0]
        embed = discord.Embed(title="🏏 Teams • KASHMIR TITANS", description=f"Page {page + 1} of {max(1, (total + 9) // 10)}", color=discord.Color.from_rgb(55, 150, 255))
        if not rows:
            embed.description += "\n\nNo approved teams yet."
        for team in rows:
            roster_ids = {r[0] for r in self.db.execute("SELECT user_id FROM team_players WHERE team_id=?", (team['id'],)).fetchall()}
            roster_ids.add(team['captain_id'])
            players = len(roster_ids)
            captain = self.bot.get_user(team['captain_id'])
            captain_text = captain.name if captain else f"<@{team['captain_id']}>"
            embed.add_field(name=f"🏏 {team['name']}", value=f"**Captain:** {captain_text}\n**Players:** {players}", inline=False)
        guild = self.bot.get_guild(guild_id)
        if guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        return embed

    @commands.command(name="teamlist")
    @commands.guild_only()
    async def team_list(self, ctx):
        total = self.db.execute("SELECT COUNT(*) FROM registered_teams WHERE guild_id=? AND status='approved'", (ctx.guild.id,)).fetchone()[0]
        await ctx.send(embed=self.make_teamlist_embed(ctx.guild.id, 0), view=TeamListView(self, ctx.guild.id, 0) if total > 10 else None)

    @commands.command(name="jointeam")
    @commands.guild_only()
    async def join_team(self, ctx, *, team_name: str):
        team = self._team(ctx.guild.id, team_name)
        if not team or team['status'] != 'approved':
            await ctx.reply("I couldn't find an approved team with that name.", mention_author=False)
            return
        if team['captain_id'] == ctx.author.id:
            await ctx.reply("You're already the captain of this team.", mention_author=False)
            return
        current_team = self._membership(ctx.guild.id, ctx.author.id)
        if current_team:
            await ctx.reply(f"You're already in **{current_team['name']}**. Use `+leaveteam` before joining another team.", mention_author=False)
            return
        existing_player = self.db.execute("SELECT 1 FROM team_players WHERE team_id=? AND user_id=?", (team['id'], ctx.author.id)).fetchone()
        if existing_player:
            await ctx.reply("You're already in this team.", mention_author=False)
            return
        pending = self.db.execute("SELECT * FROM team_join_requests WHERE guild_id=? AND user_id=? AND status='pending'", (ctx.guild.id, ctx.author.id)).fetchone()
        if pending:
            pending_team = self.db.execute("SELECT name FROM registered_teams WHERE id=?", (pending['team_id'],)).fetchone()
            await ctx.reply(f"You already have a pending request for **{pending_team['name']}**. Wait for the captain or an administrator to cancel it.", mention_author=False)
            return
        cur = self.db.execute("INSERT INTO team_join_requests(guild_id,team_id,user_id,status,created_at) VALUES(?,?,?,'pending',?)", (ctx.guild.id, team['id'], ctx.author.id, datetime.now(timezone.utc).isoformat()))
        request_id = cur.lastrowid
        self.db.commit()
        captain = ctx.guild.get_member(team['captain_id'])
        if captain is None:
            try: captain = await ctx.guild.fetch_member(team['captain_id'])
            except discord.HTTPException: captain = None
        if captain is None:
            self.db.execute("UPDATE team_join_requests SET status='cancelled' WHERE id=?", (request_id,)); self.db.commit()
            await ctx.reply("I couldn't contact the captain. Your request was cancelled; try again later.", mention_author=False)
            return
        view = JoinRequestView(self, request_id)
        try:
            message = await captain.send(f"**{ctx.author}** (`{ctx.author.id}`) wants to join **{team['name']}**.", view=view)
            self.db.execute("UPDATE team_join_requests SET message_id=?,channel_id=? WHERE id=?", (message.id, message.channel.id, request_id)); self.db.commit()
        except discord.Forbidden:
            self.db.execute("UPDATE team_join_requests SET status='cancelled' WHERE id=?", (request_id,)); self.db.commit()
            await ctx.reply("The captain's DMs are closed. Your request wasn't sent; you can try again later.", mention_author=False)
            return
        await ctx.reply(f"Your request to join **{team['name']}** has been sent to the captain.", mention_author=False)

    @commands.command(name="cancelrequests")
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def cancel_requests(self, ctx, user_id: str):
        match = re.fullmatch(r"<@!?(\d+)>|(\d+)", user_id.strip())
        if not match:
            await ctx.reply("Usage: `+cancelrequests USER_ID`", mention_author=False); return
        uid = int(match.group(1) or match.group(2))
        rows = self.db.execute("SELECT * FROM team_join_requests WHERE guild_id=? AND user_id=? AND status='pending'", (ctx.guild.id, uid)).fetchall()
        if not rows:
            await ctx.reply("That user has no pending team-join request.", mention_author=False); return
        self.db.execute("UPDATE team_join_requests SET status='cancelled' WHERE guild_id=? AND user_id=? AND status='pending'", (ctx.guild.id, uid)); self.db.commit()
        for row in rows:
            channel = self.bot.get_channel(row['channel_id']) if row['channel_id'] else None
            if channel and row['message_id']:
                try:
                    msg = await channel.fetch_message(row['message_id'])
                    for item in msg.components: pass
                    await msg.edit(content="This request was cancelled by an administrator. The buttons no longer work.", view=None)
                except (discord.HTTPException, discord.NotFound): pass
        try:
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            await self._dm(user, "Your pending team-join request was cancelled by an administrator. You can now request to join another team.")
        except discord.HTTPException: pass
        await ctx.reply(f"Cancelled **{len(rows)}** pending request(s) for `{uid}`.", mention_author=False)

    @commands.command(name="disbandteam")
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def disband_team(self, ctx, *, team_name: str):
        team = self._team(ctx.guild.id, team_name)
        if not team:
            await ctx.reply("I couldn't find a team with that name.", mention_author=False); return
        captain_id, name, team_id = team['captain_id'], team['name'], team['id']
        self.db.execute("DELETE FROM team_join_requests WHERE team_id=?", (team_id,))
        self.db.execute("DELETE FROM team_players WHERE team_id=?", (team_id,))
        self.db.execute("DELETE FROM registered_teams WHERE id=?", (team_id,))
        self.db.commit()
        try:
            captain = self.bot.get_user(captain_id) or await self.bot.fetch_user(captain_id)
            await self._dm(captain, f"Your team **{name}** has been disbanded by a server administrator. All players have been removed from the team.")
        except discord.HTTPException: pass
        await ctx.reply(f"Team **{name}** has been disbanded and removed from the database.", mention_author=False)

    @commands.command(name="setteamapproval")
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def set_team_approval(self, ctx: commands.Context, channel: discord.TextChannel):
        self.db.execute(
            "INSERT INTO team_settings(guild_id, approval_channel_id) VALUES(?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET approval_channel_id=excluded.approval_channel_id",
            (ctx.guild.id, channel.id),
        )
        self.db.commit()
        await ctx.reply(f"Team approval channel set to {channel.mention}.", mention_author=False)

    @commands.command(name="createteam")
    @commands.guild_only()
    async def create_team(self, ctx: commands.Context):
        current_team = self._membership(ctx.guild.id, ctx.author.id)
        if current_team:
            await ctx.reply(f"You're already in **{current_team['name']}**. Use `+leaveteam` before creating another team.", mention_author=False)
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
                await logo_msg.channel.send("I couldn't recognize that as an image. Upload an image or send a direct URL ending in .png, .jpg, .jpeg, .gif, or .webp, then run `+createteam` again.")
                return
            try:
                cur = self.db.execute(
                    "INSERT INTO registered_teams(guild_id,name,name_key,captain_id,logo_url,status,applicant_id,approval_channel_id,created_at) VALUES(?,?,?,?,?,'pending',?,?,?)",
                    (ctx.guild.id, name, self._key(name), ctx.author.id, logo_url, ctx.author.id, approval_channel.id, datetime.now(timezone.utc).isoformat()),
                )
                team_id = cur.lastrowid
                self.db.commit()
            except sqlite3.IntegrityError:
                await logo_msg.channel.send("A team with that name already exists or is pending approval.")
                return
            embed = discord.Embed(title="Team Application", color=discord.Color.from_rgb(40, 150, 255), timestamp=discord.utils.utcnow())
            embed.add_field(name="Team name", value=name, inline=False)
            embed.add_field(name="Applicant / proposed captain", value=f"{ctx.author.mention} (`{ctx.author.id}`)", inline=False)
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
                await logo_msg.channel.send("I couldn't post the application. Please ask an administrator to check my channel permissions.")
                return
            await logo_msg.channel.send(f"Your application for **{name}** has been submitted. You're the proposed captain; captain status becomes active after approval.")
        except asyncio.TimeoutError:
            await self._dm(ctx.author, "Team registration timed out. Run `+createteam` to start again.")

    async def decide_application(self, interaction: discord.Interaction, team_id: int, decision: str):
        row = self.db.execute("SELECT * FROM registered_teams WHERE id=? AND guild_id=?", (team_id, interaction.guild_id)).fetchone()
        if not row:
            await interaction.response.send_message("This application could not be found.", ephemeral=True)
            return
        if row["status"] != "pending":
            await interaction.response.send_message(f"This application was already {row['status']}.", ephemeral=True)
            return
        self.db.execute("UPDATE registered_teams SET status=?, decided_by=? WHERE id=? AND status='pending'", (decision, interaction.user.id, team_id))
        self.db.commit()
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else discord.Embed(title="Team Application")
        embed.color = discord.Color.green() if decision == "approved" else discord.Color.red()
        embed.add_field(name="Decision", value=f"{decision.title()} by {interaction.user.mention}", inline=False)
        for child in self.children if hasattr(self, "children") else []:
            pass
        await interaction.response.edit_message(embed=embed, view=None)
        applicant = self.bot.get_user(row["applicant_id"])
        if applicant is None:
            try:
                applicant = await self.bot.fetch_user(row["applicant_id"])
            except discord.HTTPException:
                applicant = None
        if applicant:
            if decision == "approved":
                await self._dm(applicant, f"Congratulations! Your team **{row['name']}** has been approved. You are now its captain.")
            else:
                await self._dm(applicant, f"Your application for **{row['name']}** was rejected by the team approval staff.")

    @commands.command(name="vc")
    @commands.guild_only()
    async def set_vice_captain(self, ctx: commands.Context, target: str, *, team_name: str):
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team with that name.", mention_author=False)
            return
        if ctx.author.id != team["captain_id"]:
            await ctx.reply("Only the team captain can use this command.", mention_author=False)
            return
        member = await self._get_member(ctx.guild, target)
        if member is None:
            await ctx.reply("I couldn't find that server member. Use a mention or user ID.", mention_author=False)
            return
        if member.id == team["captain_id"]:
            await ctx.reply("The captain cannot also be the vice-captain.", mention_author=False)
            return
        self.db.execute("UPDATE registered_teams SET vice_captain_id=? WHERE id=?", (member.id, team["id"]))
        self.db.execute("INSERT OR IGNORE INTO team_players(team_id,user_id,added_by,added_at) VALUES(?,?,?,?)", (team["id"], member.id, ctx.author.id, datetime.now(timezone.utc).isoformat()))
        self.db.commit()
        await ctx.reply(f"{member.mention} is now vice-captain of **{team['name']}**.", mention_author=False)
        await self._dm(member, f"You have been appointed vice-captain of **{team['name']}**.")

    @commands.command(name="addplayer")
    @commands.guild_only()
    async def add_player(self, ctx: commands.Context, target: str, *, team_name: str):
        if not self._is_admin(ctx):
            await ctx.reply("Only server administrators can use this command.", mention_author=False)
            return
        member = await self._get_member(ctx.guild, target)
        if member is None:
            await ctx.reply("I couldn't find that server member. Use a mention or user ID.", mention_author=False)
            return
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team with that name.", mention_author=False)
            return
        current_team = self._membership(ctx.guild.id, member.id)
        if current_team:
            await ctx.reply(f"{member.mention} is already in **{current_team['name']}**. They must leave first.", mention_author=False)
            return
        try:
            self.db.execute("INSERT INTO team_players(team_id,user_id,added_by,added_at) VALUES(?,?,?,?)", (team["id"], member.id, ctx.author.id, datetime.now(timezone.utc).isoformat()))
            self.db.commit()
        except sqlite3.IntegrityError:
            await ctx.reply(f"{member.mention} is already in **{team['name']}**.", mention_author=False)
            return
        await ctx.reply(f"Added {member.mention} to **{team['name']}**.", mention_author=False)
        await self._dm(member, f"You have been added by an administrator to **{team['name']}**.")

    @commands.command(name="bulkadd")
    @commands.guild_only()
    async def bulk_add(self, ctx: commands.Context, *, arguments: str):
        if not self._is_admin(ctx):
            await ctx.reply("Only server administrators can use this command.", mention_author=False)
            return
        parts = arguments.rsplit(maxsplit=1)
        if len(parts) != 2 or not parts[1].isdigit():
            await ctx.reply("Usage: `+bulkadd Team Name ROLE_ID`", mention_author=False)
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
        added = skipped = failed_dm = 0
        members = list(role.members)
        if not members:
            try:
                async for member in ctx.guild.fetch_members(limit=None):
                    if role in member.roles:
                        members.append(member)
            except (discord.Forbidden, discord.HTTPException):
                await ctx.reply("I couldn't fetch the server member list. Check the Members intent and bot permissions.", mention_author=False)
                return
        for member in members:
            current_team = self._membership(ctx.guild.id, member.id)
            if current_team:
                skipped += 1
                continue
            try:
                self.db.execute("INSERT INTO team_players(team_id,user_id,added_by,added_at) VALUES(?,?,?,?)", (team["id"], member.id, ctx.author.id, datetime.now(timezone.utc).isoformat()))
                self.db.commit()
                added += 1
                if not await self._dm(member, f"You have been added to **{team['name']}** by a server administrator."):
                    failed_dm += 1
            except sqlite3.IntegrityError:
                skipped += 1
        await ctx.reply(f"Bulk-add complete for **{team['name']}**.\nAdded: **{added}**\nAlready on team: **{skipped}**\nDMs unavailable: **{failed_dm}**", mention_author=False)


    @commands.command(name="leaveteam")
    @commands.guild_only()
    async def leave_team(self, ctx: commands.Context):
        team = self._membership(ctx.guild.id, ctx.author.id)
        if not team:
            await ctx.reply("You're not currently in an approved team.", mention_author=False)
            return
        if ctx.author.id in (team["captain_id"], team["vice_captain_id"]):
            await ctx.reply("Captains and vice-captains cannot leave their team. Ask an administrator to disband the team or change the leadership first.", mention_author=False)
            return
        view = LeaveTeamConfirmView(self, ctx.author.id, team["id"], ctx.guild.id, team["name"])
        await ctx.reply(f"Are you sure you want to leave **{team['name']}**?", view=view, mention_author=False)

    @commands.command(name="teaminfo")
    @commands.guild_only()
    async def team_info(self, ctx: commands.Context, *, team_name: str):
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team with that name.", mention_author=False)
            return
        rows = self.db.execute("SELECT user_id FROM team_players WHERE team_id=? ORDER BY added_at", (team["id"],)).fetchall()
        ids = list(dict.fromkeys([team["captain_id"]] + [r["user_id"] for r in rows]))
        embed = discord.Embed(title=f"🏏 {team['name']} • Roster", color=discord.Color.from_rgb(55, 150, 255))
        captain = f"<@{team['captain_id']}>"
        vice = f"<@{team['vice_captain_id']}>" if team["vice_captain_id"] else "Not assigned"
        embed.add_field(name="Captain", value=captain, inline=True)
        embed.add_field(name="Vice-Captain", value=vice, inline=True)
        roster = []
        for index, uid in enumerate(ids, start=1):
            role = " (Captain)" if uid == team["captain_id"] else " (Vice-Captain)" if uid == team["vice_captain_id"] else ""
            roster.append(f"{index}. <@{uid}>{role}")
        embed.add_field(name=f"Players ({len(set(ids))})", value="\n".join(roster) if roster else "No players yet.", inline=False)
        if team["logo_url"]:
            embed.set_thumbnail(url=team["logo_url"])
        await ctx.send(embed=embed)

    @commands.command(name="kickplayer")
    @commands.guild_only()
    async def kick_player(self, ctx: commands.Context, target: str, *, team_name: str):
        if not self._is_admin(ctx):
            await ctx.reply("Only server administrators can use this command.", mention_author=False)
            return
        member = await self._get_member(ctx.guild, target)
        if member is None:
            await ctx.reply("I couldn't find that server member. Use a mention or user ID.", mention_author=False)
            return
        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team with that name.", mention_author=False)
            return
        if member.id == team["captain_id"]:
            await ctx.reply("You can't kick the team captain. Disband the team or change its captain first.", mention_author=False)
            return
        cur = self.db.execute("DELETE FROM team_players WHERE team_id=? AND user_id=?", (team["id"], member.id))
        if cur.rowcount == 0:
            await ctx.reply(f"{member.mention} isn't listed in **{team['name']}**.", mention_author=False)
            return
        if team["vice_captain_id"] == member.id:
            self.db.execute("UPDATE registered_teams SET vice_captain_id=NULL WHERE id=?", (team["id"],))
        self.db.commit()
        await ctx.reply(f"Removed {member.mention} from **{team['name']}**.", mention_author=False)
        await self._dm(member, f"An administrator has removed you from **{team['name']}**.")

async def setup(bot: commands.Bot):
    await bot.add_cog(TeamRegistration(bot))
