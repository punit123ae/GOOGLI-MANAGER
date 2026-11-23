import asyncio
import io
import re
import time
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

import discord
from PIL import Image
from .teaminfo_image import TeamPageRenderer, render_team_pages
from discord.ext import commands

from .teams1 import TeamCore, ConfirmDeleteAllView, TeamApprovalView, TeamListView, JoinRequestView, LeaveTeamConfirmView, CAPTAIN_EMOJI, VICE_CAPTAIN_EMOJI

# Marks the player who ran +teaminfo in the comparison pages (their name is bold too).
YOU_EMOJI = "⭐"


def _bat_entry(rows):
    """(sort key, text) for a player's batting line, or None if they have not batted."""
    from .lb import _totals
    t = _totals(rows)
    if t["runs"] <= 0 and t["balls_faced"] <= 0:
        return None
    return (
        (-t["runs"], -t["strike_rate"]),
        f"`{t['runs']}` runs • avg `{t['bat_avg']:.2f}` • SR `{t['strike_rate']:.1f}` • `{len(rows)}` inn",
    )


def _bowl_entry(rows):
    """(sort key, text) for a player's bowling line, or None if they have not bowled."""
    from .lb import _totals
    t = _totals(rows)
    if t["balls_bowled"] <= 0 and t["wickets"] <= 0:
        return None
    balls = t["balls_bowled"]
    economy = t["economy"] if balls else 0.0
    average = f"{t['bowl_avg']:.2f}" if t["wickets"] else "-"
    return (
        (-t["wickets"], economy if balls else 99999),
        f"`{t['wickets']}` wkts • econ `{economy:.2f}` • avg `{average}` • `{balls // 6}.{balls % 6}` ov",
    )


def _field_entry(rec):
    """(sort key, text) for a player's fielding line, or None if they have not fielded."""
    caught, dropped = rec
    if caught + dropped <= 0:
        return None
    eff = caught / (caught + dropped) * 100
    return (
        (-caught, -eff),
        f"`{caught}` caught • `{dropped}` dropped • eff `{eff:.1f}%`",
    )



_AVATAR_CACHE = {}           # (user id, avatar hash) -> PNG bytes
_TEAMINFO_CACHE = {}          # (guild, team, roster) -> (timestamp, images)
_TEAMINFO_TTL = 120           # seconds a finished set of team images is reused


class TestTeamInfoView(discord.ui.View):
    def __init__(self, ctx, pages):
        super().__init__(timeout=120)
        self.owner_id = ctx.author.id
        self.pages = pages
        self.index = 0
        self.message = None
        self._sync()

    def _sync(self):
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages) - 1
        self.counter.label = f"{self.index + 1}/{len(self.pages)}"

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the person who ran this command can turn the pages.", ephemeral=True)
            return False
        return True

    async def _show(self, interaction, index):
        self.index = max(0, min(index, len(self.pages) - 1))
        self._sync()
        file = discord.File(io.BytesIO(self.pages[self.index]), filename="teaminfo.png")
        await interaction.response.edit_message(attachments=[file], view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        await self._show(interaction, self.index - 1)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.primary, disabled=True)
    async def counter(self, interaction, button):
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        await self._show(interaction, self.index + 1)

    async def on_timeout(self):
        self.clear_items()
        self.pages = []
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass



class TeamInfoMixedView(discord.ui.View):
    """Original roster embed followed by the image-based profile pages (drawn on demand)."""
    def __init__(self, ctx, roster_embed, renderer):
        super().__init__(timeout=120)
        self.owner_id = ctx.author.id
        self.roster_embed = roster_embed
        self.renderer = renderer
        self.index = 0
        self.message = None
        self._sync()

    @property
    def page_count(self):
        return 1 + self.renderer.page_count

    def _sync(self):
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index >= self.page_count - 1
        self.counter.label = f"{self.index + 1}/{self.page_count}"
        active = 0 if self.index == 0 else (1 if self.index == 1 else (2 if self.index == 2 else 3))
        for position, button in enumerate((self.roster_btn, self.overview_btn, self.stats_btn, self.squad_btn)):
            button.style = discord.ButtonStyle.success if position == active else discord.ButtonStyle.secondary
        self.squad_btn.disabled = self.page_count < 4

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who ran this command can turn the pages.", ephemeral=True
            )
            return False
        return True

    async def _show(self, interaction, index):
        self.index = max(0, min(index, self.page_count - 1))
        self._sync()
        if self.index == 0:
            await interaction.response.edit_message(
                embed=self.roster_embed, attachments=[], view=self
            )
            return
        await interaction.response.defer()   # drawing can take a few seconds on a small host
        try:
            png = await asyncio.to_thread(self.renderer.render, self.index)
        except Exception as exc:
            print(f"[teaminfo] page render failed: {type(exc).__name__}: {exc}", flush=True)
            await interaction.followup.send("I couldn't draw that page. Check the bot logs.", ephemeral=True)
            return
        file = discord.File(io.BytesIO(png), filename="teaminfo.png")
        await interaction.edit_original_response(embed=None, attachments=[file], view=self)

    @discord.ui.button(label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary, row=0)
    async def previous(self, interaction, button):
        await self._show(interaction, self.index - 1)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.primary, disabled=True, row=0)
    async def counter(self, interaction, button):
        await interaction.response.defer()

    @discord.ui.button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction, button):
        await self._show(interaction, self.index + 1)

    @discord.ui.button(label="Roster", emoji="📋", style=discord.ButtonStyle.success, row=1)
    async def roster_btn(self, interaction, button):
        await self._show(interaction, 0)

    @discord.ui.button(label="Overview", emoji="🏏", style=discord.ButtonStyle.secondary, row=1)
    async def overview_btn(self, interaction, button):
        await self._show(interaction, 1)

    @discord.ui.button(label="Team Stats", emoji="📊", style=discord.ButtonStyle.secondary, row=1)
    async def stats_btn(self, interaction, button):
        await self._show(interaction, 2)

    @discord.ui.button(label="Squad", emoji="👥", style=discord.ButtonStyle.secondary, row=1)
    async def squad_btn(self, interaction, button):
        await self._show(interaction, 3)

    async def on_timeout(self):
        self.clear_items()
        self.roster_embed = None
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class TeamCommandsB:
    """Command mixin; methods are registered by TeamRegistration in teams1.py."""

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
        affected_ids = self._team_player_ids(team_id)
        self.db.execute("DELETE FROM team_join_requests WHERE team_id=?", (team_id,))
        self.db.execute("DELETE FROM team_players WHERE team_id=?", (team_id,))
        self.db.execute("DELETE FROM registered_teams WHERE id=?", (team_id,))
        self.db.commit()
        for user_id in affected_ids:
            member = ctx.guild.get_member(int(user_id))
            if member is None:
                try:
                    member = await ctx.guild.fetch_member(int(user_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None
            if member is not None:
                await self._sync_base_role_member(ctx.guild, member)
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
        await self._sync_base_role_member(ctx.guild, member)
        await ctx.reply(f"{member.name} is now vice-captain of **{team['name']}**.", mention_author=False)
        await self._dm(member, f"You have been appointed vice-captain of **{team['name']}**.")

    @commands.command(name="stealplayer")
    @commands.guild_only()
    async def steal_player(self, ctx, target: str, *, teams: str = None):
        """Move a rostered player while retaining both Discord team roles and transfer history.

        Syntax: +stealplayer @player Old Team | New Team
        """
        if not await self._command_allowed(ctx, "stealplayer"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return
        if not teams or "|" not in teams:
            await ctx.reply(
                "Usage: `+stealplayer @player Old Team | New Team`",
                mention_author=False,
            )
            return
        old_name, new_name = (part.strip() for part in teams.split("|", 1))
        if not old_name or not new_name:
            await ctx.reply("Provide both the old team and the new team, separated by `|`.", mention_author=False)
            return

        member = await self._get_member(ctx.guild, target)
        if member is None:
            await ctx.reply("I couldn't find that server member. Use a mention or numeric user ID.", mention_author=False)
            return
        old_team = self._team(ctx.guild.id, old_name)
        new_team = self._team(ctx.guild.id, new_name)
        if not old_team or old_team["status"] != "approved":
            await ctx.reply("I couldn't find an approved old team.", mention_author=False)
            return
        if not new_team or new_team["status"] != "approved":
            await ctx.reply("I couldn't find an approved new team.", mention_author=False)
            return
        if int(old_team["id"]) == int(new_team["id"]):
            await ctx.reply("The old team and new team cannot be the same.", mention_author=False)
            return
        if member.id in (old_team["captain_id"], old_team["vice_captain_id"]):
            await ctx.reply("A team captain or vice-captain cannot be stolen with this command.", mention_author=False)
            return
        # Older admin workflows often used +kickplayer on the old team and
        # +addplayer on the new team before +stealplayer existed. In that case
        # the old roster row is already gone and the new row may already exist.
        # The command's explicit Old Team | New Team arguments are the transfer
        # declaration; do not reject it just because the old row was removed.
        memberships = self.db.execute(
            """SELECT p.team_id FROM team_players p
               JOIN registered_teams t ON t.id=p.team_id
               WHERE t.guild_id=? AND t.status='approved' AND p.user_id=?""",
            (ctx.guild.id, member.id),
        ).fetchall()
        membership_ids = {int(row[0]) for row in memberships}
        unexpected = membership_ids - {int(old_team["id"]), int(new_team["id"])}
        if unexpected:
            other = self.db.execute(
                "SELECT name FROM registered_teams WHERE id=?",
                (next(iter(unexpected)),),
            ).fetchone()
            other_name = str(other[0]) if other else "another team"
            await ctx.reply(
                f"{member.mention} is currently listed on **{other_name}**. "
                "Remove that roster entry first, or use that team as the old team.",
                mention_author=False,
            )
            return
        already_new = int(new_team["id"]) in membership_ids
        if not already_new and self._team_player_count(new_team["id"]) >= self._max_players(ctx.guild.id):
            await ctx.reply(f"**{new_team['name']}** has reached its maximum roster size.", mention_author=False)
            return

        now = datetime.now(timezone.utc).isoformat()
        try:
            with self.db:
                self.db.execute(
                    "DELETE FROM team_players WHERE team_id=? AND user_id=?",
                    (old_team["id"], member.id),
                )
                if not already_new:
                    self.db.execute(
                        "INSERT INTO team_players(team_id,user_id,added_by,added_at) VALUES(?,?,?,?)",
                        (new_team["id"], member.id, ctx.author.id, now),
                    )
                self.db.execute(
                    "INSERT INTO player_team_transfers "
                    "(guild_id,user_id,old_team_id,old_team_name,new_team_id,new_team_name,transferred_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (ctx.guild.id, member.id, old_team["id"], old_team["name"],
                     new_team["id"], new_team["name"], now),
                )
        except sqlite3.Error as exc:
            await ctx.reply(f"Transfer failed; the roster was not changed. Error: `{type(exc).__name__}`.", mention_author=False)
            return

        # Keep the old role and also assign the new team's role.
        await self._assign_team_role(ctx.guild, old_team["id"], member)
        await self._assign_team_role(ctx.guild, new_team["id"], member)
        await self._sync_base_role_member(ctx.guild, member)
        await ctx.reply(
            f"✅ **{member.display_name}** has been transferred: **{old_team['name']} → {new_team['name']}**.\n"
            f"Previous +addplayer/+kickplayer roster changes were handled automatically. "
            f"Both team roles have been retained/assigned, and previous match records remain unchanged.",
            mention_author=False,
        )
        await self._dm(member, f"You have been transferred from **{old_team['name']}** to **{new_team['name']}**. Both team roles are retained.")

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
        await self._sync_base_role_member(ctx.guild, member)
        await ctx.reply(f"Added {member.name} to **{team['name']}**.", mention_author=False)
        await self._dm(member, f"You have been added by an administrator to **{team['name']}**.")
        await self._dm_captain(ctx.guild, team, f"An administrator added **{member.name}** to your team **{team['name']}**.")

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
            # Prefer an explicit Discord user ID in parentheses, e.g. "name (123456789012345678)".
            # Converting it to a mention lets _get_member resolve the exact member rather than
            # relying on a username that may have changed or may not be unique.
            id_match = re.search(r"\((?:<@!?(\d+)>|@(\d+)|(\d{15,22}))\)\s*$", line)
            if id_match:
                user_id = next(group for group in id_match.groups() if group)
                usernames.append(f"<@{user_id}>")
                continue
            # Also accept username (DiscordUserID) when the ID is a plain number in parentheses.
            id_match = re.search(r"\((\d{15,22})\)\s*$", line)
            if id_match:
                usernames.append(f"<@{id_match.group(1)}>")
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
                await self._sync_base_role_member(ctx.guild, member)
                await self._dm(member, f"You have been added by an administrator to **{team['name']}**.")
                await self._dm_captain(ctx.guild, team, f"An administrator added **{member.name}** to your team **{team['name']}**.")
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
                await self._sync_base_role_member(ctx.guild, member)
            except sqlite3.IntegrityError:
                skipped_names.append(f"{member.name} (already on this team)")
                continue
            if not await self._dm(member, f"You have been added to **{team['name']}** by a server administrator."):
                dm_failed_names.append(member.name)
            await self._dm_captain(ctx.guild, team, f"An administrator added **{member.name}** to your team **{team['name']}**.")

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

    @commands.command(name="teamchat")
    @commands.guild_only()
    async def team_chat(self, ctx, *, message: str = ""):
        """DM a message and any attached images/files to this captain's own team."""
        team = self._membership(ctx.guild.id, ctx.author.id)
        if not team or int(team["captain_id"]) != ctx.author.id:
            await ctx.reply("Only a team captain can use `+teamchat` for their own team.", mention_author=False)
            return
        attachments = list(ctx.message.attachments)
        if not message.strip() and not attachments:
            await ctx.reply("Usage: `+teamchat <message>` and optionally attach an image/file.", mention_author=False)
            return

        player_rows = self.db.execute(
            "SELECT user_id FROM team_players WHERE team_id=? ORDER BY added_at", (team["id"],)
        ).fetchall()
        recipient_ids = list(dict.fromkeys([int(team["captain_id"])] + [int(row["user_id"]) for row in player_rows]))
        sent = 0
        failed = 0
        for user_id in recipient_ids:
            try:
                recipient = ctx.guild.get_member(user_id) or self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                files = [await attachment.to_file() for attachment in attachments]
                await recipient.send(
                    content=f"**Team chat • {team['name']}**\n{message}".strip() if message.strip() else f"**Team chat • {team['name']}**",
                    files=files
                )
                sent += 1
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                failed += 1
        await ctx.reply(f"Team chat sent to **{sent}** team member(s)." + (f" Could not DM **{failed}** member(s)." if failed else ""), mention_author=False)

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

    @staticmethod
    async def _fetch_url_bytes(url):
        """Fetch and validate an image URL, including signed Discord CDN URLs."""
        clean_url = str(url or "").strip()
        if not clean_url.startswith(("http://", "https://")):
            return None
        try:
            import aiohttp
            from PIL import Image
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; TeamProfileBot/1.0)",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            }
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=18, connect=8),
                headers=headers,
            ) as session:
                async with session.get(clean_url, allow_redirects=True) as response:
                    print(
                        f"[teaminfo][logo] HTTP {response.status} "
                        f"host={urlparse(str(response.url)).netloc!r}",
                        flush=True,
                    )
                    if response.status != 200:
                        return None
                    # Read the entire response body. StreamReader.read(n) may return
                    # fewer than n bytes as soon as some data is available, which can
                    # leave Discord CDN images truncated even when the response is 200.
                    max_bytes = 8 * 1024 * 1024
                    if response.content_length is not None and response.content_length > max_bytes:
                        print("[teaminfo][logo] Image response exceeds 8 MB limit", flush=True)
                        return None
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            print("[teaminfo][logo] Image response exceeded 8 MB while downloading", flush=True)
                            return None
                    payload = bytes(body)
                    if not payload:
                        return None
                    try:
                        # Fully decode and re-encode every downloaded logo as PNG.
                        # Discord's media proxy can return formats/modes that pass
                        # verify() but fail later when the profile renderer converts them.
                        with Image.open(io.BytesIO(payload)) as image:
                            image.load()
                            normalized = image.convert("RGBA")
                            output = io.BytesIO()
                            normalized.save(output, format="PNG", optimize=True)
                            payload = output.getvalue()
                        if not payload:
                            return None
                    except Exception as exc:
                        print(
                            f"[teaminfo][logo] Image decode/normalization failed: "
                            f"{type(exc).__name__}: {exc}",
                            flush=True,
                        )
                        return None
                    return payload
        except Exception as exc:
            print(f"[teaminfo][logo] download failed: {type(exc).__name__}: {exc}", flush=True)
            return None

    async def _team_logo_bytes(self, ctx, team):
        """Use a saved SQLite copy first; otherwise try original and Discord's cached proxy URL."""
        try:
            saved = team["logo_data"]
        except (KeyError, IndexError):
            saved = None
        if saved:
            try:
                from PIL import Image
                raw_saved = bytes(saved)
                with Image.open(io.BytesIO(raw_saved)) as image:
                    image.load()
                    if image.format == "PNG" and image.mode == "RGBA":
                        payload = raw_saved   # already normalised: skip the slow re-encode
                    else:
                        output = io.BytesIO()
                        image.convert("RGBA").save(output, format="PNG", compress_level=3)
                        payload = output.getvalue()
                # Repair old/non-normalized backups in place.
                if payload != raw_saved:
                    self.db.execute(
                        "UPDATE registered_teams SET logo_data=? WHERE id=?",
                        (sqlite3.Binary(payload), int(team["id"])),
                    )
                    self.db.commit()
                print(
                    f"[teaminfo][logo] Using valid saved copy for {team['name']!r} "
                    f"({len(payload)} bytes, PNG)",
                    flush=True,
                )
                return payload
            except Exception as exc:
                print(
                    f"[teaminfo][logo] Saved copy for {team['name']!r} is invalid; "
                    f"will recover again: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                self.db.execute(
                    "UPDATE registered_teams SET logo_data=NULL WHERE id=?",
                    (int(team["id"]),),
                )
                self.db.commit()

        candidates = [team["logo_url"]]
        # Discord's message embed may expose a proxy URL that still works even when
        # the original attachment URL stored in the DB has expired.
        channel_id = team["approval_channel_id"]
        message_id = team["approval_message_id"]
        if channel_id and message_id:
            channel = ctx.guild.get_channel(int(channel_id))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(channel_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    channel = None
            if channel is not None:
                try:
                    message = await channel.fetch_message(int(message_id))
                    for embed in message.embeds:
                        thumbnail = embed.thumbnail
                        proxy_url = getattr(thumbnail, "proxy_url", None)
                        image_url = getattr(thumbnail, "url", None)
                        if proxy_url:
                            candidates.insert(0, proxy_url)
                        if image_url:
                            candidates.append(image_url)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        seen = set()
        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            payload = await self._fetch_url_bytes(candidate)
            if payload:
                try:
                    self.db.execute(
                        "UPDATE registered_teams SET logo_data=? WHERE id=?",
                        (sqlite3.Binary(payload), int(team["id"])),
                    )
                    self.db.commit()
                    print(f"[teaminfo][logo] Saved permanent DB copy for {team['name']!r}", flush=True)
                except sqlite3.Error as exc:
                    print(f"[teaminfo][logo] Could not save DB copy: {exc}", flush=True)
                return payload
        print(f"[teaminfo][logo] No recoverable image URL for {team['name']!r}", flush=True)
        return None

    @staticmethod
    async def _fetch_avatar(member):
        """Avatar PNG bytes; retries, then falls back to a larger size and the default avatar."""
        if member is None:
            return None
        cache_key = (int(member.id), str(getattr(member.display_avatar, "key", "")))
        cached = _AVATAR_CACHE.get(cache_key)
        if cached:
            return cached
        attempts = (
            lambda: member.display_avatar.replace(size=128, static_format="png"),
            lambda: member.display_avatar.replace(size=256, static_format="png"),
            lambda: member.display_avatar.replace(size=128, format="png"),
            lambda: member.default_avatar.replace(size=128, format="png"),
        )
        for index, make in enumerate(attempts):
            try:
                payload = await asyncio.wait_for(make().read(), timeout=8)
                if payload and Image.open(io.BytesIO(payload)).verify() is None:
                    if len(_AVATAR_CACHE) >= 120:
                        _AVATAR_CACHE.pop(next(iter(_AVATAR_CACHE)))
                    _AVATAR_CACHE[cache_key] = payload
                    return payload
            except Exception as exc:
                print(f"[teaminfo][avatar] attempt {index+1} failed for {getattr(member, 'id', '?')}: "
                      f"{type(exc).__name__}: {exc}", flush=True)
                await asyncio.sleep(0.25 * (index + 1))
        return None

    async def _team_profile_data(self, ctx, team, ordered_ids, fetch_avatars=True):
        """Everything the premium team-profile images need, gathered from the bot's own data."""
        from .lb import _totals
        from .stats import fantasy_raw_points

        guild = ctx.guild
        member_ids = list(dict.fromkeys(
            [int(team["captain_id"])]
            + ([int(team["vice_captain_id"])] if team["vice_captain_id"] else [])
            + [int(uid) for uid in ordered_ids]
        ))
        try:   # players who used to play for this team still count towards its totals
            historical = self.db.execute(
                "SELECT DISTINCT user_id FROM player_team_transfers "
                "WHERE guild_id=? AND (old_team_id=? OR new_team_id=?)",
                (int(guild.id), int(team["id"]), int(team["id"])),
            ).fetchall()
            total_ids = list(dict.fromkeys(member_ids + [int(row[0]) for row in historical]))
        except sqlite3.Error:
            total_ids = member_ids
        stat_rows = self._team_stat_rows(guild.id, team)
        fielding = self._team_fielding_totals(guild.id, team)
        captain_id = int(team["captain_id"])
        vice_id = int(team["vice_captain_id"]) if team["vice_captain_id"] else None

        semaphore = asyncio.Semaphore(4)

        async def resolve_member(uid):
            member = guild.get_member(uid)
            if member is not None:
                return member
            try:
                async with semaphore:
                    return await asyncio.wait_for(guild.fetch_member(uid), timeout=10)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, asyncio.TimeoutError):
                pass
            try:   # left the server: the global user still has an avatar
                user = self.bot.get_user(uid)
                if user is None:
                    async with semaphore:
                        user = await asyncio.wait_for(self.bot.fetch_user(uid), timeout=10)
                return user
            except (discord.NotFound, discord.HTTPException, asyncio.TimeoutError):
                return None

        resolved_members = await asyncio.gather(*(resolve_member(uid) for uid in member_ids))
        members = dict(zip(member_ids, resolved_members))

        avatar_semaphore = asyncio.Semaphore(10)

        async def avatar_of(uid):
            async with avatar_semaphore:
                return await self._fetch_avatar(members[uid])
        print(
            f"[teaminfo][logo:DATA] Team={team['name']!r} "
            f"logo_url_present={bool(team['logo_url'])} url_host={urlparse(str(team['logo_url'] or '')).netloc!r}",
            flush=True,
        )
        if fetch_avatars:
            avatar_list, logo_bytes = await asyncio.gather(
                asyncio.gather(*(avatar_of(uid) for uid in member_ids)),
                self._team_logo_bytes(ctx, team),
            )
        else:   # callers that only need a few avatars fetch those themselves
            logo_bytes = await self._team_logo_bytes(ctx, team)
            avatar_list = [None] * len(member_ids)
        print(
            f"[teaminfo][logo:DATA] Downloader returned "
            f"{len(logo_bytes) if logo_bytes else 0} bytes",
            flush=True,
        )
        avatars = dict(zip(member_ids, avatar_list))

        players = []
        for uid in member_ids:
            rows = stat_rows.get(uid, [])
            t = _totals(rows) if rows else None
            caught, dropped = fielding.get(uid, (0, 0))
            member = members[uid]
            name = member.name if member else self._username(guild, uid)
            batted = bool(t and (t["runs"] > 0 or t["balls_faced"] > 0))
            bowled = bool(t and (t["balls_bowled"] > 0 or t["wickets"] > 0))
            best = "—"
            if bowled:
                bw = max((r for r in rows if r["balls_bowled"] > 0 or r["wickets"] > 0),
                         key=lambda r: (r["wickets"], -r["runs_conceded"]))
                best = f"{bw['wickets']}/{bw['runs_conceded']}"
            players.append({
                "uid": uid, "name": str(name), "handle": member.name if member else str(name),
                "avatar_bytes": avatars.get(uid),
                "role": "C" if uid == captain_id else ("VC" if uid == vice_id else ""),
                "matches": len(rows),
                "runs": t["runs"] if t else 0, "batted": batted,
                "bat_avg": t["bat_avg"] if batted else 0.0, "strike_rate": t["strike_rate"] if batted else 0.0,
                "hs": max((r["runs"] for r in rows), default=0),
                "wickets": t["wickets"] if t else 0, "bowled": bowled,
                "economy": t["economy"] if bowled else 0.0, "best": best,
                "catches": int(caught), "dropped": int(dropped),
                "fantasy": (fantasy_raw_points(rows)[2] if rows else 0.0) + caught * 5 - dropped * 10,
            })
        by_uid = {p["uid"]: p for p in players}

        # team totals (whole team history, same rule as the comparison pages)
        team_rows = [r for uid in total_ids for r in stat_rows.get(uid, [])]
        tt = _totals(team_rows) if team_rows else None
        catches = sum(fielding.get(uid, (0, 0))[0] for uid in total_ids)
        drops = sum(fielding.get(uid, (0, 0))[1] for uid in total_ids)
        efficiency = catches / (catches + drops) * 100 if catches + drops else 0.0
        balls = tt["balls_bowled"] if tt else 0

        def top(key, limit=5, predicate=None):
            pool = [p for p in players if (p[key] > 0 if predicate is None else predicate(p))]
            return sorted(pool, key=lambda p: (-p[key], p["name"].casefold()))[:limit]
        sections = [
            {"title": "BATTING", "color": (255, 128, 94),
             "tiles": [("RUNS", f"{tt['runs']:,}" if tt else "0"), ("AVERAGE", f"{tt['bat_avg']:.1f}" if tt else "—"),
                       ("STRIKE RATE", f"{tt['strike_rate']:.1f}" if tt else "—")],
             "list_title": "TOP RUN SCORERS",
             "top": [{"player": p, "value": p["runs"], "label": str(p["runs"])} for p in top("runs")]},
            {"title": "BOWLING", "color": (156, 128, 255),
             "tiles": [("WICKETS", str(tt["wickets"]) if tt else "0"), ("ECONOMY", f"{tt['economy']:.2f}" if balls else "—"),
                       ("OVERS", f"{balls // 6}.{balls % 6}" if balls else "0")],
             "list_title": "TOP WICKET TAKERS",
             "top": [{"player": p, "value": p["wickets"], "label": str(p["wickets"])} for p in top("wickets")]},
            {"title": "FIELDING", "color": (62, 224, 172),
             "tiles": [("CATCHES", str(catches)), ("DROPPED", str(drops)),
                       ("EFFICIENCY", f"{efficiency:.0f}%" if catches + drops else "—")],
             "list_title": "TOP FIELDERS",
             "top": [{"player": p, "value": p["catches"], "label": str(p["catches"])} for p in top("catches")]},
        ]
        leaders = []
        for label, color, key, unit, builder in [
            ("TOP RUN SCORER", (255, 128, 94), "runs", "RUNS",
             lambda p: f"AVG {p['bat_avg']:.1f}  •  SR {p['strike_rate']:.1f}"),
            ("TOP WICKET TAKER", (156, 128, 255), "wickets", "WICKETS",
             lambda p: f"ECON {p['economy']:.1f}  •  BEST {p['best']}"),
            ("TOP FIELDER", (62, 224, 172), "catches", "CATCHES",
             lambda p: f"DROPPED {p['dropped']}"),
        ]:
            best_player = (top(key, 1) or [None])[0]
            leaders.append({
                "label": label, "color": color, "player": best_player, "unit": unit,
                "value": best_player[key] if best_player else 0,
                "sub": builder(best_player) if best_player else "",
            })

        standing = None
        points_cog = self.bot.get_cog("PointsTable")
        if points_cog is not None and hasattr(points_cog, "team_standing"):
            standing = points_cog.team_standing(guild.id, team["name"])

        team_color = None
        role_setting = self.db.execute(
            "SELECT role_id FROM team_role_settings WHERE team_id=? AND enabled=1",
            (int(team["id"]),),
        ).fetchone()
        if role_setting:
            team_role = guild.get_role(int(role_setting["role_id"]))
            if team_role is not None and team_role.color.value:
                team_color = team_role.color.to_rgb()

        return {
            "name": str(team["name"]), "guild": guild.name, "logo": logo_bytes,
            "team_color": team_color,
            "players": players, "captain": by_uid.get(captain_id), "vice": by_uid.get(vice_id) if vice_id else None,
            "roster_size": len(ordered_ids), "roster_max": self._max_players(guild.id),
            "standing": standing, "sections": sections, "leaders": leaders,
            "totals": {
                "runs": tt["runs"] if tt else None, "bat_avg": tt["bat_avg"] if tt else None,
                "strike_rate": tt["strike_rate"] if tt else None, "wickets": tt["wickets"] if tt else None,
                "economy": tt["economy"] if balls else None,
                "catches": catches, "drops": drops, "eff": efficiency if catches + drops else None,
            },
        }

    @commands.command(name="testteaminfo")
    async def test_team_info(self, ctx, *, team_name: str):
        """Private bot-owner preview of team profiles across every connected server."""
        # This experimental command is deliberately unavailable in every server.
        if ctx.guild is not None or not isinstance(ctx.channel, discord.DMChannel):
            return
        if not await self.bot.is_owner(ctx.author):
            await ctx.send("This command is restricted to the bot owner.")
            return

        query = self._key(team_name)
        if len(query) < 4:
            await ctx.send("Enter at least the first **4 letters** of the team name.")
            return

        rows = self.db.execute(
            "SELECT * FROM registered_teams WHERE status='approved' ORDER BY name COLLATE NOCASE"
        ).fetchall()
        exact = [row for row in rows if self._key(row["name"]) == query]
        matches = exact or [row for row in rows if self._key(row["name"]).startswith(query)]
        # Only show teams belonging to servers the bot is currently connected to.
        matches = [row for row in matches if self.bot.get_guild(int(row["guild_id"])) is not None]

        if len(matches) > 1:
            labels = []
            for row in matches[:20]:
                guild = self.bot.get_guild(int(row["guild_id"]))
                labels.append(f"• **{row['name']}** — {guild.name}")
            await ctx.send(
                "More than one team matches. Please provide a longer team name:\n" + "\n".join(labels)
            )
            return
        if not matches:
            await ctx.send("I couldn't find an approved team with that name or prefix in any connected server.")
            return

        team = matches[0]
        guild = self.bot.get_guild(int(team["guild_id"]))
        loading = await ctx.send("⏳ Building the private team profile...")
        try:
            player_rows = self.db.execute(
                "SELECT user_id FROM team_players WHERE team_id=? ORDER BY added_at",
                (team["id"],),
            ).fetchall()
            ordered_ids = list(dict.fromkeys(
                [int(team["captain_id"])] + [int(row["user_id"]) for row in player_rows]
            ))
            # The image builder expects a context-like object with the target guild
            # and requesting user; the command itself remains in the owner's DM.
            profile_ctx = type("TeamProfileContext", (), {
                "guild": guild, "author": ctx.author
            })()
            data = await self._team_profile_data(profile_ctx, team, ordered_ids)
            images = await asyncio.to_thread(render_team_pages, data)
            view = TestTeamInfoView(ctx, images)
            view.message = await loading.edit(
                content=None, attachments=[discord.File(io.BytesIO(images[0]), filename="teaminfo.png")], view=view
            )
        except Exception as exc:
            print(f"[testteaminfo] image generation failed: {type(exc).__name__}: {exc}")
            try:
                await loading.delete()
            except Exception:
                pass
            await ctx.send("I couldn't generate the team profile images. Check the bot logs.")

    @commands.command(name="backupteamlogos")
    @commands.guild_only()
    async def backup_team_logos(self, ctx):
        """Recover and permanently save every approved team's logo where Discord still has a cached copy."""
        if not (ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.manage_guild):
            await ctx.reply("Only server administrators or members with Manage Server can use this command.", mention_author=False)
            return
        teams = self.db.execute(
            "SELECT * FROM registered_teams WHERE guild_id=? AND status='approved' ORDER BY team_number, id",
            (ctx.guild.id,),
        ).fetchall()
        if not teams:
            await ctx.reply("There are no approved teams to back up.", mention_author=False)
            return

        status = await ctx.reply(
            f"⏳ Checking and backing up logos for **{len(teams)}** approved teams...",
            mention_author=False,
        )
        saved = []
        unavailable = []
        for team in teams:
            payload = await self._team_logo_bytes(ctx, team)
            if payload:
                saved.append(team["name"])
            else:
                unavailable.append(team["name"])
            await asyncio.sleep(0.15)

        lines = [
            f"**Logo backup finished:** {len(saved)}/{len(teams)} recovered and saved.",
        ]
        if unavailable:
            lines.append("\n**Could not recover yet:**\n" + "\n".join(f"• {name}" for name in unavailable[:30]))
            if len(unavailable) > 30:
                lines.append(f"\n…and {len(unavailable) - 30} more.")
        await status.edit(content="\n".join(lines))

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

        other_rows = self.db.execute(
            "SELECT user_id FROM team_players WHERE team_id=? ORDER BY added_at",
            (team["id"],),
        ).fetchall()
        ordered_ids = list(dict.fromkeys(
            [int(team["captain_id"])] + [int(row["user_id"]) for row in other_rows]
        ))

        # Keep the original +teaminfo roster embed as page one.
        roster_embed = discord.Embed(
            title=f"🏏 {team['name']} • Roster",
            color=discord.Color.from_rgb(55, 150, 255),
        )
        roster_embed.add_field(
            name="Captain",
            value=f"{self._username(ctx.guild, team['captain_id'])} {CAPTAIN_EMOJI}",
            inline=True,
        )
        vice = (
            f"{self._username(ctx.guild, team['vice_captain_id'])} {VICE_CAPTAIN_EMOJI}"
            if team["vice_captain_id"] else "Not assigned"
        )
        roster_embed.add_field(name="Vice-Captain", value=vice, inline=True)
        roster = []
        for index, uid in enumerate(ordered_ids, start=1):
            emoji = (
                f" {CAPTAIN_EMOJI}" if uid == team["captain_id"]
                else (f" {VICE_CAPTAIN_EMOJI}" if uid == team["vice_captain_id"] else "")
            )
            roster.append(f"{index}. {self._username(ctx.guild, uid)}{emoji}")
        roster_embed.add_field(
            name=f"Players ({len(ordered_ids)}/{self._max_players(ctx.guild.id)})",
            value="\n".join(roster) if roster else "No players yet.",
            inline=False,
        )
        if team["logo_url"]:
            roster_embed.set_thumbnail(url=team["logo_url"])

        loading = await ctx.send("⏳ Building the team profile...")
        try:
            cache_key = (int(ctx.guild.id), int(team["id"]), tuple(int(u) for u in ordered_ids))
            hit = _TEAMINFO_CACHE.get(cache_key)
            if hit and time.time() - hit[0] < _TEAMINFO_TTL:
                renderer = hit[1]
            else:
                started = time.perf_counter()
                data = await self._team_profile_data(ctx, team, ordered_ids)
                renderer = await asyncio.to_thread(TeamPageRenderer, data)
                print(f"[teaminfo] data ready in {time.perf_counter()-started:.1f}s (pages draw on demand)", flush=True)
                if len(_TEAMINFO_CACHE) >= 2:
                    _TEAMINFO_CACHE.pop(next(iter(_TEAMINFO_CACHE)))
                _TEAMINFO_CACHE[cache_key] = (time.time(), renderer)
            view = TeamInfoMixedView(ctx, roster_embed, renderer)
            view.message = await loading.edit(
                content=None, embed=roster_embed, attachments=[], view=view
            )
        except Exception as exc:
            print(f"[teaminfo] image generation failed: {type(exc).__name__}: {exc}")
            try:
                await loading.delete()
            except Exception:
                pass
            await ctx.send("I couldn't generate the team profile images. Check the bot logs.")

    # ------------------------------------------------------------------ team comparison
    def _team_match_time(self, source_message_id, imported_at=0):
        """Use the raw-stat message timestamp, not its later import timestamp."""
        try:
            return discord.utils.snowflake_time(int(source_message_id)).timestamp()
        except (TypeError, ValueError, OverflowError):
            return float(imported_at or 0)

    def _team_transfer_history(self, guild_id, user_id):
        try:
            return self.db.execute(
                """SELECT old_team_id, old_team_name, new_team_id, new_team_name, transferred_at
                   FROM player_team_transfers
                   WHERE guild_id=? AND user_id=? ORDER BY transferred_at ASC""",
                (int(guild_id), int(user_id)),
            ).fetchall()
        except sqlite3.Error:
            return []

    def _team_at_match(self, guild_id, user_id, source_message_id, imported_at, saved_team):
        transfers = self._team_transfer_history(guild_id, user_id)
        if not transfers:
            return str(saved_team) if saved_team else None
        match_at = self._team_match_time(source_message_id, imported_at)
        team_name = str(transfers[0]["old_team_name"])
        for transfer in transfers:
            try:
                transferred_at = datetime.fromisoformat(
                    str(transfer["transferred_at"]).replace("Z", "+00:00")
                ).timestamp()
            except (TypeError, ValueError, OverflowError):
                continue
            if match_at >= transferred_at:
                team_name = str(transfer["new_team_name"])
            else:
                break
        return team_name

    def _team_stat_rows(self, guild_id, team):
        """Main-tournament batting/bowling rows assigned to the team at match time."""
        db = getattr(self.bot, "db", None)
        if db is None:
            return {}
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
            return {}
        result = {}
        target = str(team["name"]).casefold()
        current_ids = {int(team["captain_id"])}
        if team["vice_captain_id"]:
            current_ids.add(int(team["vice_captain_id"]))
        try:
            current_ids.update(
                int(item[0]) for item in self.db.execute(
                    "SELECT user_id FROM team_players WHERE team_id=?", (int(team["id"]),)
                ).fetchall()
            )
        except sqlite3.Error:
            pass
        for row in rows:
            uid = int(row["user_id"])
            team_at_match = self._team_at_match(
                guild_id, uid, row["source_message_id"], row["imported_at"], row["team_name"]
            )
            if not team_at_match and uid in current_ids:
                team_at_match = str(team["name"])
            if not team_at_match or team_at_match.casefold() != target:
                continue
            result.setdefault(uid, []).append({
                "runs": int(row["runs"]), "balls_faced": int(row["balls_faced"]),
                "runs_conceded": int(row["runs_conceded"]), "balls_bowled": int(row["balls_bowled"]),
                "wickets": int(row["wickets"]), "not_out": int(row["not_out"]),
            })
        return result

    def _team_fielding_totals(self, guild_id, team):
        """Imported main-tournament fielding totals attributed to the team at match time."""
        db = getattr(self.bot, "db", None)
        if db is None:
            return {}
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
            return {}
        result = {}
        target = str(team["name"]).casefold()
        current_ids = {int(team["captain_id"])}
        if team["vice_captain_id"]:
            current_ids.add(int(team["vice_captain_id"]))
        try:
            current_ids.update(
                int(item[0]) for item in self.db.execute(
                    "SELECT user_id FROM team_players WHERE team_id=?", (int(team["id"]),)
                ).fetchall()
            )
        except sqlite3.Error:
            pass
        for row in rows:
            uid = int(row["user_id"])
            team_at_match = self._team_at_match(
                guild_id, uid, row["source_message_id"], row["imported_at"], row["team_name"]
            )
            if not team_at_match and uid in current_ids:
                team_at_match = str(team["name"])
            if not team_at_match or team_at_match.casefold() != target:
                continue
            caught, dropped = result.get(uid, (0, 0))
            result[uid] = (caught + int(row["catches_taken"]), dropped + int(row["catches_dropped"]))

        # Keep existing aggregate/live fielding totals for current players who have
        # never been transferred. Transferred players use match-level rows so their
        # catches are not all reassigned to their latest team.
        try:
            aggregate_rows = db.execute(
                "SELECT user_id, catches_caught, catches_dropped FROM fielding_stats WHERE guild_id=?",
                (int(guild_id),),
            ).fetchall()
            for aggregate in aggregate_rows:
                uid = int(aggregate["user_id"])
                if uid in current_ids and not self._team_transfer_history(guild_id, uid):
                    result[uid] = (int(aggregate["catches_caught"]), int(aggregate["catches_dropped"]))
        except Exception:
            pass
        return result

    def _comparison_page(self, ctx, team, title, entry_fn, data_by_user, member_ids, summary, empty_text):
        """One ranked comparison page. The person who ran the command gets ⭐ and a bold name."""
        guild = ctx.guild
        you = ctx.author.id

        def line(rank, uid, detail):
            name = discord.utils.escape_markdown(self._username(guild, uid))
            if uid == you:
                return f"`{rank}.` {YOU_EMOJI} **{name}** → {detail}"
            return f"`{rank}.` {name} → {detail}"

        ranked = []
        for uid in member_ids:
            data = data_by_user.get(uid)
            entry = entry_fn(data) if data else None
            if entry:
                ranked.append((entry[0], uid, entry[1]))
        ranked.sort(key=lambda r: (r[0], r[1]))

        lines = [line(i, uid, detail) for i, (_, uid, detail) in enumerate(ranked, start=1)]
        body = "\n".join(lines) if lines else empty_text
        embed = discord.Embed(
            title=f"{title} • {team['name']}",
            description=f"{summary}\n\n{body}",
            color=discord.Color.from_rgb(55, 150, 255),
        )
        if team["logo_url"]:
            embed.set_thumbnail(url=team["logo_url"])

        if you not in member_ids:
            data = data_by_user.get(you)
            entry = entry_fn(data) if data else None
            your_name = discord.utils.escape_markdown(ctx.author.name)
            mine = f"{YOU_EMOJI} **{your_name}** → {entry[1] if entry else 'no records yet'}"
            embed.add_field(name="YOU (not on this team)", value=mine, inline=False)

        silent = len(member_ids) - len(ranked)
        note = f" • {silent} player(s) have no records" if silent > 0 else ""
        embed.set_footer(text=f"{YOU_EMOJI} = you{note}")
        return embed

    def _team_comparison_pages(self, ctx, team, ordered_ids):
        from .lb import _totals

        # Same membership as +lb/+stats: captain, vice-captain, then the other players.
        member_ids = list(dict.fromkeys(
            [int(team["captain_id"])]
            + ([int(team["vice_captain_id"])] if team["vice_captain_id"] else [])
            + [int(uid) for uid in ordered_ids]
        ))
        # Include players who used to represent this team, even after a steal.
        try:
            historical = self.db.execute(
                "SELECT DISTINCT user_id FROM player_team_transfers "
                "WHERE guild_id=? AND (old_team_id=? OR new_team_id=?)",
                (int(ctx.guild.id), int(team["id"]), int(team["id"])),
            ).fetchall()
            member_ids = list(dict.fromkeys(member_ids + [int(row[0]) for row in historical]))
        except sqlite3.Error:
            pass
        stat_rows = self._team_stat_rows(ctx.guild.id, team)
        fielding = self._team_fielding_totals(ctx.guild.id, team)

        team_rows = [r for uid in member_ids for r in stat_rows.get(uid, [])]
        t = _totals(team_rows)
        bat_summary = (
            f"**Team total:** `{t['runs']}` runs • avg `{t['bat_avg']:.2f}` • SR `{t['strike_rate']:.1f}`"
        )
        bowl_summary = (
            f"**Team total:** `{t['wickets']}` wkts • econ `{t['economy']:.2f}` • "
            f"`{t['balls_bowled'] // 6}.{t['balls_bowled'] % 6}` ov"
        )
        caught = sum(fielding.get(uid, (0, 0))[0] for uid in member_ids)
        dropped = sum(fielding.get(uid, (0, 0))[1] for uid in member_ids)
        eff = caught / (caught + dropped) * 100 if caught + dropped else 0.0
        field_summary = (
            f"**Team total:** `{caught}` caught • `{dropped}` dropped • eff `{eff:.1f}%`"
        )

        return [
            self._comparison_page(ctx, team, "🏏 Batting comparison", _bat_entry, stat_rows,
                                  member_ids, bat_summary, "No batting records for this team yet."),
            self._comparison_page(ctx, team, "🎯 Bowling comparison", _bowl_entry, stat_rows,
                                  member_ids, bowl_summary, "No bowling records for this team yet."),
            self._comparison_page(ctx, team, "🧤 Fielding comparison", _field_entry, fielding,
                                  member_ids, field_summary, "No fielding records for this team yet."),
        ]

    @commands.command(name="kickplayer")
    @commands.guild_only()
    async def kick_player(self, ctx, target: str, *, team_name: str):
        if not await self._command_allowed(ctx, "kickplayer"):
            await ctx.reply("You are not allowed to use this command.", mention_author=False)
            return

        # A departed member cannot be resolved as a Guild Member. Accept a
        # mention/ID directly, then verify that ID against this team's roster.
        id_match = re.fullmatch(r"<@!?(\d+)>|@(\d+)|(\d+)", target.strip())
        member = None
        user_id = None
        if id_match:
            user_id = int(next(group for group in id_match.groups() if group))
            member = ctx.guild.get_member(user_id)
            if member is None:
                try:
                    member = await ctx.guild.fetch_member(user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    # NotFound means they left; Forbidden/HTTPException should
                    # not block an ID-based roster removal either.
                    member = None
        else:
            member = await self._get_member(ctx.guild, target)
            if member is None:
                await ctx.reply(
                    "I couldn't find that server member. For someone who has left, use their numeric Discord user ID.",
                    mention_author=False
                )
                return
            user_id = member.id

        team = self._team(ctx.guild.id, team_name)
        if not team or team["status"] != "approved":
            await ctx.reply("I couldn't find an approved team.", mention_author=False)
            return
        if user_id == int(team["captain_id"]):
            await ctx.reply("You can't kick the team captain. Change the captain first.", mention_author=False)
            return

        cur = self.db.execute(
            "DELETE FROM team_players WHERE team_id=? AND user_id=?",
            (team["id"], user_id)
        )
        if cur.rowcount == 0:
            await ctx.reply(f"User ID `{user_id}` isn't listed in **{team['name']}**.", mention_author=False)
            return

        if team["vice_captain_id"] is not None and int(team["vice_captain_id"]) == user_id:
            self.db.execute("UPDATE registered_teams SET vice_captain_id=NULL WHERE id=?", (team["id"],))
        self.db.commit()

        # Discord role operations require a current Guild Member. Database
        # removal still succeeds for departed users.
        if member is not None:
            await self._remove_team_role(ctx.guild, team["id"], member)
            await self._sync_base_role_member(ctx.guild, member)

        user = member or self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                user = None
        player_name = user.name if user is not None else str(user_id)

        await ctx.reply(
            f"Removed **{discord.utils.escape_markdown(player_name)}** (`{user_id}`) from **{team['name']}**.",
            mention_author=False
        )
        if user is not None:
            await self._dm(user, f"An administrator has removed you from **{team['name']}**.")
        await self._dm_captain(
            ctx.guild, team,
            f"An administrator removed **{discord.utils.escape_markdown(player_name)}** from your team **{team['name']}**."
        )