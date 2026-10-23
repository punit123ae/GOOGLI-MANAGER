from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import discord
from discord.ext import commands

OWNER_ID = 1014568928682459198
DB_PATH = Path(__file__).resolve().parent.parent / "team_registration.db"


class Unteamed(commands.Cog):
    """Owner-only command that edits one configured teamless-users message."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

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

    def _team_member_ids(self, guild_id: int) -> set[int]:
        """Read approved roster members plus captains and vice-captains."""
        if not DB_PATH.is_file():
            return set()

        with sqlite3.connect(
            f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=5
        ) as db:
            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not {"registered_teams", "team_players"}.issubset(tables):
                return set()

            rows = db.execute(
                """SELECT p.user_id
                   FROM team_players p
                   JOIN registered_teams t ON t.id=p.team_id
                   WHERE t.guild_id=? AND t.status='approved'
                   UNION
                   SELECT captain_id FROM registered_teams
                   WHERE guild_id=? AND status='approved'
                   UNION
                   SELECT vice_captain_id FROM registered_teams
                   WHERE guild_id=? AND status='approved'
                     AND vice_captain_id IS NOT NULL""",
                (guild_id, guild_id, guild_id),
            ).fetchall()

        return {int(row[0]) for row in rows if row[0] is not None}

    def _make_content(self, guild: discord.Guild) -> str:
        team_ids = self._team_member_ids(guild.id)
        teamless = sorted(
            (
                member for member in guild.members
                if not member.bot and member.id not in team_ids
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
            content = (
                content[:1970].rsplit("\n", 1)[0]
                + "\n... (list too long for one Discord message)"
            )
        return content

    async def _fetch_message_from_reference(self, ctx, reference: str):
        """Accept a Discord message link or a raw message ID in the current server."""
        reference = reference.strip()
        match = re.search(
            r"(?:https?://)?(?:www\.)?discord(?:app)?\.com/channels/"
            r"(\d+)/(\d+)/(\d+)",
            reference,
        )

        if match:
            guild_id, channel_id, message_id = map(int, match.groups())
            if guild_id != ctx.guild.id:
                raise ValueError("Use a message link from this server.")
        elif reference.isdigit():
            message_id = int(reference)
            # A raw ID needs to be in the channel where the command was used.
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
        """Set the one message that +unteamed is allowed to edit."""
        if ctx.author.id != OWNER_ID:
            await ctx.reply(
                "Only the bot owner can use this command.", mention_author=False
            )
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
            f"Teamless-users message set to: {message.jump_url}",
            mention_author=False,
        )

    @commands.command(name="unteamed")
    @commands.guild_only()
    async def unteamed(self, ctx: commands.Context):
        """Update the configured message only when this command is run."""
        if ctx.author.id != OWNER_ID:
            await ctx.reply(
                "Only the bot owner can use this command.", mention_author=False
            )
            return

        if ctx.guild is None:
            return

        await ctx.guild.chunk(cache=True)
        tracking = self._get_tracking(ctx.guild.id)
        if not tracking:
            # First run: send the list here and save this exact message as the target.
            try:
                message = await ctx.send(
                    self._make_content(ctx.guild),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                await ctx.reply(
                    "I couldn't send the teamless-users list in this channel.",
                    mention_author=False,
                )
                return

            self._save_tracking(ctx.guild.id, message.channel.id, message.id)
            await ctx.reply(
                f"Teamless-users list sent and saved as the target message: {message.jump_url}",
                mention_author=False,
            )
            return

        channel_id, message_id = map(int, tracking)
        channel = ctx.guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await ctx.reply(
                    "I can't access the configured message channel. Set it again with `+setunteamed`.",
                    mention_author=False,
                )
                return

        try:
            message = await channel.fetch_message(message_id)
            await message.edit(
                content=self._make_content(ctx.guild),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            await ctx.reply(
                "I couldn't edit the configured message. Check the message/channel permissions or set another message.",
                mention_author=False,
            )
            return

        await ctx.reply(
            f"Updated the configured teamless-users message: {message.jump_url}",
            mention_author=False,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Unteamed(bot))
