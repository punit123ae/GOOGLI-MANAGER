from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import discord
from discord.ext import commands

OWNER_ID = 1014568928682459198
PARTICIPANT_ROLE_ID = 1560979656096743474
DB_PATH = Path(__file__).resolve().parent.parent / "team_registration.db"


class Unteamed(commands.Cog):
    """Owner-configured unteamed list, automatically refreshed on membership changes."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._refreshing_guilds: set[int] = set()

    def _ensure_tracking_table(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS unteamed_message_settings (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL
                )"""
            )
            db.commit()

    def _get_tracking(self, guild_id: int):
        self._ensure_tracking_table()
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            return db.execute(
                "SELECT channel_id, message_id FROM unteamed_message_settings WHERE guild_id=?",
                (guild_id,),
            ).fetchone()

    def _save_tracking(self, guild_id: int, channel_id: int, message_id: int):
        self._ensure_tracking_table()
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            db.execute(
                """INSERT INTO unteamed_message_settings (guild_id, channel_id, message_id)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                     channel_id=excluded.channel_id,
                     message_id=excluded.message_id""",
                (guild_id, channel_id, message_id),
            )
            db.commit()

    def _make_content(self, guild: discord.Guild) -> str:
        role = guild.get_role(PARTICIPANT_ROLE_ID)
        if role is None:
            return (
                "KASHMIR TITANS S1\n\nTEAMLESS USERS;\n\n"
                f"ERROR: I couldn't find the participant role (ID: {PARTICIPANT_ROLE_ID})."
            )

        teamless = sorted(
            (
                member for member in guild.members
                if not member.bot and role not in member.roles
            ),
            key=lambda member: (member.name.casefold(), member.id),
        )

        lines = ["KASHMIR TITANS S1", "", "TEAMLESS USERS;", ""]
        if teamless:
            lines.extend(
                f"{index}. {member.name} ({member.id})"
                for index, member in enumerate(teamless, start=1)
            )
        else:
            lines.append("No teamless users.")

        content = "\n".join(lines)
        if len(content) > 1990:
            content = content[:1970].rsplit("\n", 1)[0] + "\n... (list too long for one Discord message)"
        return content

    async def _refresh_saved_message(self, guild: discord.Guild) -> bool:
        """Edit the configured target message; return whether it succeeded."""
        if guild.id in self._refreshing_guilds:
            return False

        tracking = self._get_tracking(guild.id)
        if not tracking:
            return False

        self._refreshing_guilds.add(guild.id)
        try:
            await guild.chunk(cache=True)
            channel_id, message_id = map(int, tracking)
            channel = guild.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
            await message.edit(
                content=self._make_content(guild),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError) as exc:
            print(f"[unteamed] Could not refresh message for guild {guild.id}: {exc}")
            return False
        finally:
            self._refreshing_guilds.discard(guild.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self._refresh_saved_message(member.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self._refresh_saved_message(member.guild)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        role_id = PARTICIPANT_ROLE_ID
        had_role = any(role.id == role_id for role in before.roles)
        has_role = any(role.id == role_id for role in after.roles)
        if had_role != has_role:
            await self._refresh_saved_message(after.guild)

    @commands.Cog.listener()
    async def on_ready(self):
        # Rebuild the saved list after a bot restart, without needing +unteamed.
        for guild in self.bot.guilds:
            if self._get_tracking(guild.id):
                await self._refresh_saved_message(guild)

    async def _fetch_message_from_reference(self, ctx, reference: str):
        """Accept a Discord message link or a raw message ID in the current server."""
        reference = reference.strip()
        match = re.search(
            r"(?:https?://)?(?:www\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)",
            reference,
        )

        if match:
            guild_id, channel_id, message_id = map(int, match.groups())
            if guild_id != ctx.guild.id:
                raise ValueError("Use a message link from this server.")
        elif reference.isdigit():
            message_id = int(reference)
            channel_id = ctx.channel.id
        else:
            raise ValueError("Provide a Discord message link or message ID.")

        channel = ctx.guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                raise ValueError("I couldn't access that channel.")

        if not hasattr(channel, "fetch_message"):
            raise ValueError("That link doesn't point to a text channel message.")

        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            raise ValueError("I couldn't find or access that message.")

        if message.guild.id != ctx.guild.id:
            raise ValueError("Use a message from this server.")
        return message

    @commands.command(name="setunteamed")
    @commands.guild_only()
    async def set_unteamed_message(self, ctx: commands.Context, *, reference: str = ""):
        if ctx.author.id != OWNER_ID:
            await ctx.reply("Only the bot owner can use this command.", mention_author=False)
            return
        if not reference.strip():
            await ctx.reply(
                "Usage: `+setunteamed <message link or message ID>`",
                mention_author=False,
            )
            return

        try:
            message = await self._fetch_message_from_reference(ctx, reference)
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return

        self._save_tracking(ctx.guild.id, message.channel.id, message.id)
        await ctx.reply(
            f"Target message saved: {message.jump_url}\n"
            "Automatic updates are enabled. Run `+unteamed` once to fill the message now.",
            mention_author=False,
        )

    @commands.command(name="unteamed")
    @commands.guild_only()
    async def unteamed(self, ctx: commands.Context):
        if ctx.author.id != OWNER_ID:
            await ctx.reply("Only the bot owner can use this command.", mention_author=False)
            return

        if not self._get_tracking(ctx.guild.id):
            await ctx.reply(
                "Choose the message first with `+setunteamed <message link>`.",
                mention_author=False,
            )
            return

        success = await self._refresh_saved_message(ctx.guild)
        if success:
            tracking = self._get_tracking(ctx.guild.id)
            channel = ctx.guild.get_channel(int(tracking[0]))
            if channel is None:
                channel = await self.bot.fetch_channel(int(tracking[0]))
            message = await channel.fetch_message(int(tracking[1]))
            await ctx.reply(f"Updated: {message.jump_url}", mention_author=False)
        else:
            await ctx.reply(
                "I couldn't update the saved message. Check the bot's channel permissions "
                "and make sure the target message still exists.",
                mention_author=False,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Unteamed(bot))
