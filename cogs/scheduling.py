import asyncio
import re
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

IST = ZoneInfo("Asia/Kolkata")
MATCH_EMOJI = "<a:arrow_arrow:1565966619291811984>"
VS_EMOJI = "<:VS128:1559620966709993472>"
HEADER_EMOJI = "<:emoji_60:1562481891037290637>"
TIME_EMOJI = "<:time:1565970550067691710>"
PIN_EMOJI = "<:pin:1565975934882943066>"


def parse_start_time(date_text, time_text):
    """Parse 'YYYY-MM-DD' plus a 12-hour time such as '8:30 PM', '8pm' or '08:30 p.m.'.

    Returns a timezone-aware datetime in India time. Raises ValueError if invalid.
    """
    cleaned = re.sub(r"\s+", "", time_text or "").upper().replace(".", "")
    for time_format in ("%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(
                f"{date_text.strip()} {cleaned}", f"%Y-%m-%d {time_format}"
            ).replace(tzinfo=IST)
        except ValueError:
            continue
    raise ValueError("Invalid date or 12-hour time")


async def command_allowed(interaction, command_name):
    """Delegate command access to the shared utility/whitelist system."""
    utility = interaction.client.get_cog("Utility")
    if utility is None:
        # Fail closed until the shared permission system is loaded.
        return False
    return await utility.command_allowed(interaction, command_name)


class Scheduling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.scheduler.start()

    def cog_unload(self):
        self.scheduler.cancel()

    def get_match(self, match_id):
        return self.db.execute(
            "SELECT * FROM matches WHERE id = ?",
            (match_id.strip().upper(),)
        ).fetchone()

    def get_logs_channel_id(self, guild_id):
        row = self.db.execute(
            "SELECT value FROM settings WHERE key = ?",
            (f"logs_{guild_id}",)
        ).fetchone()
        return int(row["value"]) if row else None

    def role_mentions(self, team_a, team_b):
        return discord.AllowedMentions(
            everyone=False, users=False, roles=[team_a, team_b],
            replied_user=False
        )

    async def send_log(self, guild, message):
        channel_id = self.get_logs_channel_id(guild.id)
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        try:
            await channel.send(
                message,
                allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            pass

    async def restore_channel_permission(self, guild, row):
        channel = guild.get_channel(row["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return False
        overwrite = channel.overwrites_for(guild.default_role)
        original = row["original_send_messages"]
        overwrite.send_messages = None if original is None else bool(original)
        try:
            await channel.set_permissions(
                guild.default_role,
                overwrite=overwrite,
                reason=f"Restore permissions for match {row['id']}"
            )
            return True
        except discord.HTTPException:
            return False

    async def dm_team_members(self, guild, row):
        team_a = guild.get_role(row["team_a_id"])
        team_b = guild.get_role(row["team_b_id"])
        channel = guild.get_channel(row["channel_id"])
        if not team_a or not team_b or not channel:
            return 0, 0

        # Backticks inside a role name would break the inline-code formatting.
        name_a = team_a.name.replace("`", "'")
        name_b = team_b.name.replace("`", "'")
        message = (
            f"# {HEADER_EMOJI} `{name_a}` {VS_EMOJI} `{name_b}` {HEADER_EMOJI}\n"
            f"{TIME_EMOJI}  **Match Time:** <t:{int(row['start_time'])}:F>\n"
            f"{PIN_EMOJI} **Match Channel:** [Join Match]({channel.jump_url})"
        )
        member_ids = set()
        sent = failed = 0
        try:
            async for member in guild.fetch_members(limit=None):
                if not member.bot and (team_a in member.roles or team_b in member.roles):
                    member_ids.add(member.id)
        except discord.HTTPException as exc:
            await self.send_log(guild, f"⚠️ Couldn't retrieve all members for `{row['id']}`: {exc}")
            return 0, len(member_ids)

        for member_id in member_ids:
            member = guild.get_member(member_id)
            if member is None:
                try:
                    member = await guild.fetch_member(member_id)
                except discord.HTTPException:
                    failed += 1
                    continue
            try:
                await member.send(
                    message,
                    allowed_mentions=discord.AllowedMentions.none()
                )
                sent += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
            await asyncio.sleep(1)
        return sent, failed

    async def start_match(self, guild, row):
        result = self.db.execute(
            "UPDATE matches SET status = 'started' "
            "WHERE id = ? AND status = 'scheduled'",
            (row["id"],)
        )
        self.db.commit()
        if result.rowcount == 0:
            return

        team_a = guild.get_role(row["team_a_id"])
        team_b = guild.get_role(row["team_b_id"])
        channel = guild.get_channel(row["channel_id"])
        restored = await self.restore_channel_permission(guild, row)

        if channel and team_a and team_b:
            try:
                await channel.send(
                    f"# {HEADER_EMOJI} `MATCH TIME!` {HEADER_EMOJI}\n"
                    f"{team_a.mention} {VS_EMOJI} {team_b.mention}\n"
                    f"{TIME_EMOJI}  The scheduled match starts now!",
                    allowed_mentions=self.role_mentions(team_a, team_b)
                )
            except discord.HTTPException as exc:
                await self.send_log(guild, f"⚠️ Start announcement failed for `{row['id']}`: {exc}")

        await self.send_log(
            guild,
            f"🏁 **MATCH STARTED**\n**Match ID:** `{row['id']}`\n"
            f"**Channel permissions restored:** {'Yes' if restored else 'No'}"
        )

    @tasks.loop(seconds=15)
    async def scheduler(self):
        now = datetime.now(timezone.utc).timestamp()
        rows = self.db.execute(
            "SELECT * FROM matches WHERE status = 'scheduled' AND start_time <= ?",
            (now,)
        ).fetchall()
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if guild:
                await self.start_match(guild, row)

    @scheduler.before_loop
    async def before_scheduler(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="set_match_logs", description="Set the channel for private match logs.")
    @app_commands.describe(channel="Channel where match IDs and admin details are logged")
    async def set_match_logs(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await command_allowed(interaction, "set_match_logs"):
            await interaction.response.send_message("You are not allowed to use this command.", ephemeral=True)
            return
        self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (f"logs_{interaction.guild_id}", str(channel.id))
        )
        self.db.commit()
        await interaction.response.send_message(f"✅ Match logs channel set to {channel.mention}.", ephemeral=True)
        await self.send_log(interaction.guild, f"🛠️ Match logs channel updated by <@{interaction.user.id}>.")

    class ScheduleView(discord.ui.View):
        def __init__(self, cog, owner_id, guild, team_a, team_b, channel, start_dt):
            super().__init__(timeout=120)
            self.cog = cog
            self.owner_id = owner_id
            self.guild = guild
            self.team_a = team_a
            self.team_b = team_b
            self.channel = channel
            self.start_dt = start_dt
            self.used = False

        async def interaction_check(self, interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("Only the admin who created this preview can confirm it.", ephemeral=True)
                return False
            if not await command_allowed(interaction, "match_schedule"):
                await interaction.response.send_message("You are not allowed to use this command.", ephemeral=True)
                return False
            return True

        @discord.ui.button(label="Confirm Match", style=discord.ButtonStyle.success)
        async def confirm(self, interaction, button):
            if self.used:
                await interaction.response.send_message("This preview has already been used.", ephemeral=True)
                return
            if not self.cog.get_logs_channel_id(self.guild.id):
                self.used = True
                for item in self.children:
                    item.disabled = True
                await interaction.response.edit_message(content="❌ Run `/set_match_logs` first.", view=self)
                return
            if self.start_dt.timestamp() <= datetime.now(timezone.utc).timestamp():
                self.used = True
                for item in self.children:
                    item.disabled = True
                await interaction.response.edit_message(content="❌ That match time has already passed.", view=self)
                return

            self.used = True
            for item in self.children:
                item.disabled = True
            await interaction.response.defer()

            match_id = uuid.uuid4().hex[:8].upper()
            overwrite = self.channel.overwrites_for(self.guild.default_role)
            original = overwrite.send_messages
            original_value = None if original is None else int(original)
            overwrite.send_messages = False
            try:
                await self.channel.set_permissions(
                    self.guild.default_role,
                    overwrite=overwrite,
                    reason=f"Locking channel for match {match_id}"
                )
            except discord.HTTPException as exc:
                await interaction.edit_original_response(content=f"❌ Couldn't lock the channel: {exc}", view=self)
                return

            self.cog.db.execute("""
                INSERT INTO matches (
                    id, guild_id, team_a_id, team_b_id, channel_id,
                    start_time, status, created_by, created_at,
                    original_send_messages, announcement_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, NULL)
            """, (
                match_id, self.guild.id, self.team_a.id, self.team_b.id,
                self.channel.id, self.start_dt.timestamp(), self.owner_id,
                datetime.now(timezone.utc).timestamp(), original_value
            ))
            self.cog.db.commit()

            unix_time = int(self.start_dt.timestamp())
            announcement = (
                f"{self.team_a.mention} {VS_EMOJI} {self.team_b.mention}\n\n"
                f"{MATCH_EMOJI} **Match at** <t:{unix_time}:F>"
            )
            announcement_message_id = None
            try:
                message = await self.channel.send(
                    announcement,
                    allowed_mentions=self.cog.role_mentions(self.team_a, self.team_b)
                )
                announcement_message_id = message.id
                self.cog.db.execute(
                    "UPDATE matches SET announcement_message_id = ? WHERE id = ?",
                    (message.id, match_id)
                )
                self.cog.db.commit()
            except discord.HTTPException as exc:
                self.cog.db.execute("UPDATE matches SET error = ? WHERE id = ?", (f"Announcement failed: {exc}", match_id))
                self.cog.db.commit()

            await self.cog.send_log(
                self.guild,
                f"📋 **MATCH SCHEDULED**\n**Match ID:** `{match_id}`\n"
                f"**Team A:** {self.team_a.name}\n**Team B:** {self.team_b.name}\n"
                f"**Channel:** <#{self.channel.id}>\n**Match at:** <t:{unix_time}:F>\n"
                f"**Scheduled by:** <@{self.owner_id}>\n"
                f"**Announcement message ID:** `{announcement_message_id}`"
            )
            sent, failed = await self.cog.dm_team_members(self.guild, self.cog.get_match(match_id))
            await self.cog.send_log(self.guild, f"✉️ **MATCH DMs — `{match_id}`**\n📨 Sent: **{sent}**\n⚠️ Failed/unavailable: **{failed}**")
            await interaction.edit_original_response(
                content=f"✅ **Match scheduled successfully!**\n🆔 Match ID: `{match_id}`\n"
                        f"🏏 {self.team_a.name} vs {self.team_b.name}\n🕒 <t:{unix_time}:F>\n"
                        f"📨 DMs sent: **{sent}** · Failed/unavailable: **{failed}**",
                view=self
            )

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
        async def cancel(self, interaction, button):
            self.used = True
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(content="Match scheduling cancelled. No match was created.", view=self)

    def get_match(self, match_id):
        return self.db.execute("SELECT * FROM matches WHERE id = ?", (match_id.strip().upper(),)).fetchone()

    @app_commands.command(name="match_schedule", description="Schedule a match between two team roles.")
    @app_commands.describe(
        team_a="First team's role", team_b="Second team's role",
        channel="Match announcement channel", date="Date in YYYY-MM-DD format, India time",
        time="Time in 12-hour format with AM/PM, India time (example: 8:30 PM)"
    )
    async def match_schedule(self, interaction: discord.Interaction, team_a: discord.Role, team_b: discord.Role, channel: discord.TextChannel, date: str, time: str):
        if not await command_allowed(interaction, "match_schedule"):
            await interaction.response.send_message("You are not allowed to use this command.", ephemeral=True)
            return
        if team_a.id == team_b.id:
            await interaction.response.send_message("Choose two different team roles.", ephemeral=True)
            return
        try:
            start_dt = parse_start_time(date, time)
        except ValueError:
            await interaction.response.send_message("Use date `YYYY-MM-DD` and a 12-hour time with AM/PM (India time). Example: `2026-11-01` and `8:30 PM`.", ephemeral=True)
            return
        if start_dt.timestamp() <= datetime.now(timezone.utc).timestamp():
            await interaction.response.send_message("The match time must be in the future.", ephemeral=True)
            return
        unix_time = int(start_dt.timestamp())
        preview = (
            "🏏 **MATCH PREVIEW**\nNo announcement has been sent yet.\n\n"
            f"⚔️ **Teams:** {team_a.name} {VS_EMOJI} {team_b.name}\n"
            f"📍 **Channel:** {channel.mention}\n🕒 **Match at:** <t:{unix_time}:F>\n\n"
            "Confirm to announce the match, ping both team roles, lock the channel, and DM team members."
        )
        await interaction.response.send_message(
            preview,
            view=self.ScheduleView(self, interaction.user.id, interaction.guild, team_a, team_b, channel, start_dt),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="match_list", description="List scheduled matches.")
    async def match_list(self, interaction: discord.Interaction):
        if not await command_allowed(interaction, "match_list"):
            await interaction.response.send_message("You are not allowed to use this command.", ephemeral=True)
            return
        rows = self.db.execute(
            "SELECT * FROM matches WHERE guild_id = ? AND status = 'scheduled' ORDER BY start_time ASC LIMIT 20",
            (interaction.guild_id,)
        ).fetchall()
        if not rows:
            await interaction.response.send_message("There are no scheduled matches.", ephemeral=True)
            return
        lines = []
        for row in rows:
            team_a = interaction.guild.get_role(row["team_a_id"])
            team_b = interaction.guild.get_role(row["team_b_id"])
            channel = interaction.guild.get_channel(row["channel_id"])
            lines.append(
                f"🆔 `{row['id']}` — **{team_a.name if team_a else 'Unknown'} vs {team_b.name if team_b else 'Unknown'}**\n"
                f"📍 {channel.mention if channel else 'Channel unavailable'}\n🕒 <t:{int(row['start_time'])}:F>"
            )
        await interaction.response.send_message("\n\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="match_info", description="View a match by its ID.")
    @app_commands.describe(match_id="Match ID from the logs channel")
    async def match_info(self, interaction: discord.Interaction, match_id: str):
        if not await command_allowed(interaction, "match_info"):
            await interaction.response.send_message("You are not allowed to use this command.", ephemeral=True)
            return
        row = self.get_match(match_id)
        if not row or row["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Match ID not found.", ephemeral=True)
            return
        team_a = interaction.guild.get_role(row["team_a_id"])
        team_b = interaction.guild.get_role(row["team_b_id"])
        channel = interaction.guild.get_channel(row["channel_id"])
        await interaction.response.send_message(
            f"**Match ID:** `{row['id']}`\n**Teams:** {team_a.name if team_a else 'Unknown'} vs {team_b.name if team_b else 'Unknown'}\n"
            f"**Channel:** {channel.mention if channel else 'Unavailable'}\n**Match at:** <t:{int(row['start_time'])}:F>\n"
            f"**Status:** {row['status']}\n**Scheduled by:** <@{row['created_by']}>\n**Error:** {row['error'] or 'None'}",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="match_delete", description="Cancel a scheduled match by its ID.")
    @app_commands.describe(match_id="Match ID from the logs channel")
    async def match_delete(self, interaction: discord.Interaction, match_id: str):
        if not await command_allowed(interaction, "match_delete"):
            await interaction.response.send_message("You are not allowed to use this command.", ephemeral=True)
            return
        row = self.get_match(match_id)
        if not row or row["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("Match ID not found.", ephemeral=True)
            return
        if row["status"] != "scheduled":
            await interaction.response.send_message(f"This match is already `{row['status']}` and cannot be cancelled.", ephemeral=True)
            return
        self.db.execute("UPDATE matches SET status = 'cancelled' WHERE id = ?", (row["id"],))
        self.db.commit()
        deleted = False
        channel = interaction.guild.get_channel(row["channel_id"])
        if channel and row["announcement_message_id"]:
            try:
                announcement = await channel.fetch_message(row["announcement_message_id"])
                await announcement.delete()
                deleted = True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        restored = await self.restore_channel_permission(interaction.guild, row)
        await self.send_log(
            interaction.guild,
            f"🗑️ **MATCH CANCELLED**\n**ID:** `{row['id']}`\n**Cancelled by:** <@{interaction.user.id}>\n"
            f"**Announcement deleted:** {'Yes' if deleted else 'No / not found'}\n"
            f"**Channel permission restored:** {'Yes' if restored else 'No'}"
        )
        await interaction.response.send_message(
            f"✅ Match `{row['id']}` cancelled.\nAnnouncement deleted: **{'Yes' if deleted else 'No / not found'}**\n"
            f"Channel permission restored: **{'Yes' if restored else 'No'}**",
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Scheduling(bot))
