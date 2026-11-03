import re
import asyncio

import discord
from discord.ext import commands

from .utility import owner_or_whitelisted

HAND_CRICKET_BOT_ID = 753191385296928808
RAW_STATS_MARKER = "Raw statistics of the match:"


class AutoLock(commands.Cog):
    """Automatically lock configured channels when the trusted hand-cricket bot posts raw stats."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS autolock_channels (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                original_send_messages INTEGER,
                locked_by_autolock INTEGER NOT NULL DEFAULT 0,
                delay_seconds INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, channel_id)
            )
        """)
        # Add the delay column to databases created by the older cog version.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(autolock_channels)").fetchall()}
        if "delay_seconds" not in columns:
            self.db.execute(
                "ALTER TABLE autolock_channels ADD COLUMN delay_seconds INTEGER NOT NULL DEFAULT 0"
            )
        self.db.commit()

    async def _permitted(self, ctx):
        if await owner_or_whitelisted(ctx, ctx.command.name if ctx.command else ""):
            return True
        await ctx.reply(
            "Only the bot owner or a role whitelisted for this command can use it.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    @staticmethod
    def _resolve_text_channel(ctx, target):
        if not target or target.lower() == "channel":
            return ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None
        match = re.fullmatch(r"<#(\d+)>", target.strip())
        raw_id = match.group(1) if match else target.strip()
        if raw_id.isdigit():
            channel = ctx.guild.get_channel(int(raw_id))
            return channel if isinstance(channel, discord.TextChannel) else None
        name = target.strip().lstrip("#")
        return discord.utils.get(ctx.guild.text_channels, name=name)

    async def _set_channel_permission(self, channel, value, reason):
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.send_messages = value
        await channel.set_permissions(
            channel.guild.default_role,
            overwrite=overwrite,
            reason=reason,
        )

    @staticmethod
    def _parse_duration(value):
        """Parse durations such as 30s, 5m, 2h, or 1d into seconds."""
        match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", value.lower())
        if not match:
            return None
        seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
        return seconds if seconds <= 7 * 86400 else None

    async def _lock_after_delay(self, guild_id, channel_id, delay_seconds):
        """Lock after the configured delay; every raw-stats post can trigger this."""
        if delay_seconds:
            await asyncio.sleep(delay_seconds)

        # Recheck the setting so a pending task cannot lock a channel after
        # auto-lock has been disabled during the delay.
        row = self.db.execute(
            "SELECT * FROM autolock_channels WHERE guild_id=? AND channel_id=? AND enabled=1",
            (guild_id, channel_id),
        ).fetchone()
        if row is None:
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if not isinstance(channel, discord.TextChannel):
            return

        already_managed = bool(row["locked_by_autolock"])
        try:
            overwrite = channel.overwrites_for(channel.guild.default_role)

            # Save the original permission only once. Keep it even if a user
            # manually unlocks the channel between two raw-stats messages.
            if not already_managed:
                original = overwrite.send_messages
                self.db.execute(
                    """UPDATE autolock_channels
                       SET original_send_messages=?, locked_by_autolock=1
                       WHERE guild_id=? AND channel_id=? AND enabled=1""",
                    (None if original is None else int(original), guild_id, channel_id),
                )
                self.db.commit()

            overwrite.send_messages = False
            await channel.set_permissions(
                channel.guild.default_role,
                overwrite=overwrite,
                reason=f"Auto-lock: raw match statistics posted by HandCricket ({HAND_CRICKET_BOT_ID})",
            )
            await channel.send(
                "🔒 Channel locked automatically after HandCricket posted the raw match statistics.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            # Don't erase a previously saved original permission on a later retry.
            if not already_managed:
                self.db.execute(
                    """UPDATE autolock_channels
                       SET original_send_messages=NULL, locked_by_autolock=0
                       WHERE guild_id=? AND channel_id=?""",
                    (guild_id, channel_id),
                )
                self.db.commit()

    @commands.command(name="autolock")
    async def autolock(self, ctx, channel: discord.TextChannel = None):
        """Enable raw-statistics auto-locking in a channel (defaults to this channel)."""
        if not await self._permitted(ctx):
            return
        if ctx.guild is None:
            return await ctx.reply("This command can only be used in a server.", mention_author=False)
        channel = channel or ctx.channel
        if not isinstance(channel, discord.TextChannel):
            return await ctx.reply("Please select a text channel.", mention_author=False)
        self.db.execute("""
            INSERT INTO autolock_channels (guild_id, channel_id, enabled, original_send_messages, locked_by_autolock)
            VALUES (?, ?, 1, NULL, 0)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET enabled=1
        """, (ctx.guild.id, channel.id))
        self.db.commit()
        await ctx.reply(
            f"🔒 Auto-lock enabled for {channel.mention}. It will lock when HandCricket posts `{RAW_STATS_MARKER}`.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="autolockduration", aliases=["setautolockduration", "setautolockdelay"])
    async def autolockduration(self, ctx, duration: str, channel: discord.TextChannel = None):
        """Set the delay between raw stats being posted and the channel being locked."""
        if not await self._permitted(ctx):
            return
        if ctx.guild is None:
            return await ctx.reply("This command can only be used in a server.", mention_author=False)

        seconds = self._parse_duration(duration)
        if seconds is None:
            return await ctx.reply(
                "Invalid duration. Use `0s`, `30s`, `5m`, `2h`, or `1d` (maximum 7 days).",
                mention_author=False,
            )

        channel = channel or ctx.channel
        if not isinstance(channel, discord.TextChannel):
            return await ctx.reply("Please select a text channel.", mention_author=False)

        row = self.db.execute(
            "SELECT 1 FROM autolock_channels WHERE guild_id=? AND channel_id=?",
            (ctx.guild.id, channel.id),
        ).fetchone()
        if row is None:
            return await ctx.reply(
                f"Auto-lock is not configured for {channel.mention}. Run `+autolock` first.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        self.db.execute(
            "UPDATE autolock_channels SET delay_seconds=? WHERE guild_id=? AND channel_id=?",
            (seconds, ctx.guild.id, channel.id),
        )
        self.db.commit()
        await ctx.reply(
            f"⏱️ Auto-lock delay for {channel.mention} set to **{seconds} seconds**.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="stopautolock")
    async def stopautolock(self, ctx, channel: discord.TextChannel = None):
        """Disable auto-locking in a channel and restore a lock made by this feature."""
        if not await self._permitted(ctx):
            return
        if ctx.guild is None:
            return await ctx.reply("This command can only be used in a server.", mention_author=False)
        channel = channel or ctx.channel
        if not isinstance(channel, discord.TextChannel):
            return await ctx.reply("Please select a text channel.", mention_author=False)
        row = self.db.execute(
            "SELECT * FROM autolock_channels WHERE guild_id=? AND channel_id=?",
            (ctx.guild.id, channel.id),
        ).fetchone()
        if row is None or not row["enabled"]:
            return await ctx.reply(f"Auto-lock is already disabled for {channel.mention}.", mention_author=False,
                                    allowed_mentions=discord.AllowedMentions.none())
        try:
            if row["locked_by_autolock"]:
                original = row["original_send_messages"]
                await self._set_channel_permission(
                    channel,
                    None if original is None else bool(original),
                    "Stopping auto-lock and restoring its channel lock",
                )
        except discord.Forbidden:
            return await ctx.reply("I couldn't restore the channel permissions. Check my Manage Channels permission.", mention_author=False)
        except discord.HTTPException as exc:
            return await ctx.reply(f"Couldn't restore the channel permissions: {exc}", mention_author=False)
        self.db.execute("""
            UPDATE autolock_channels
            SET enabled=0, original_send_messages=NULL, locked_by_autolock=0
            WHERE guild_id=? AND channel_id=?
        """, (ctx.guild.id, channel.id))
        self.db.commit()
        await ctx.reply(f"⏹️ Auto-lock disabled for {channel.mention}.", mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="statusautolock", aliases=["statusautlock"])
    async def statusautolock(self, ctx, target: str = "channel"):
        """Show status for this/selected channel, or all configured channels with 'server'."""
        if not await self._permitted(ctx):
            return
        if ctx.guild is None:
            return await ctx.reply("This command can only be used in a server.", mention_author=False)
        if target.strip().lower() == "server":
            rows = self.db.execute(
                "SELECT channel_id, enabled, locked_by_autolock, delay_seconds FROM autolock_channels WHERE guild_id=? ORDER BY channel_id",
                (ctx.guild.id,),
            ).fetchall()
            active = [r for r in rows if r["enabled"]]
            if not active:
                return await ctx.reply("Auto-lock is disabled in every channel in this server.", mention_author=False)
            lines = []
            for row in active:
                channel = ctx.guild.get_channel(row["channel_id"])
                channel_label = channel.mention if channel else f"`{row['channel_id']}` (channel not found)"
                if row["locked_by_autolock"] and channel:
                    is_locked = channel.overwrites_for(ctx.guild.default_role).send_messages is False
                    lock_state = "currently locked" if is_locked else "unlocked; will re-lock on next raw stats"
                else:
                    lock_state = "waiting for raw stats"
                lines.append(f"• {channel_label} — {lock_state} — delay: `{row['delay_seconds'] or 0}s`")
            return await ctx.reply("**Auto-lock status for this server:**\n" + "\n".join(lines),
                                   mention_author=False, allowed_mentions=discord.AllowedMentions.none())

        channel = self._resolve_text_channel(ctx, target)
        if channel is None:
            return await ctx.reply("Use `+statusautolock channel`, `+statusautolock #channel`, or `+statusautolock server`.", mention_author=False)
        row = self.db.execute(
            "SELECT enabled, locked_by_autolock, delay_seconds FROM autolock_channels WHERE guild_id=? AND channel_id=?",
            (ctx.guild.id, channel.id),
        ).fetchone()
        if row and row["enabled"]:
            if row["locked_by_autolock"]:
                is_locked = channel.overwrites_for(ctx.guild.default_role).send_messages is False
                lock_state = (
                    "The channel is currently locked by auto-lock."
                    if is_locked else "The channel is unlocked; it will lock again after the next raw-stats post."
                )
            else:
                lock_state = "Enabled; waiting for raw statistics."
            message = f"🔒 Auto-lock is **ON** for {channel.mention}. {lock_state} Delay: **{row['delay_seconds'] or 0}s**."
        else:
            message = f"🔓 Auto-lock is **OFF** for {channel.mention}."
        await ctx.reply(message, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot is False or message.author.id != HAND_CRICKET_BOT_ID:
            return
        if message.guild is None or not isinstance(message.channel, discord.TextChannel):
            return
        if RAW_STATS_MARKER not in (message.content or ""):
            return
        row = self.db.execute(
            "SELECT delay_seconds FROM autolock_channels WHERE guild_id=? AND channel_id=? AND enabled=1",
            (message.guild.id, message.channel.id),
        ).fetchone()
        if row is None:
            return
        asyncio.create_task(
            self._lock_after_delay(
                message.guild.id,
                message.channel.id,
                max(0, int(row["delay_seconds"] or 0)),
            )
        )


async def setup(bot):
    await bot.add_cog(AutoLock(bot))
