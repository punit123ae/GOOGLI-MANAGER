from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import discord
from discord.ext import commands

OWNER_ID = 1014568928682459198
PARTICIPANT_ROLE_ID = 1560979656096743474
DB_PATH = Path(__file__).resolve().parent.parent / "team_registration.db"
MAX_CONTENT = 1900  # Safely below Discord's 2000-character limit.


class Unteamed(commands.Cog):
    """Automatically refresh a role-based teamless list across managed messages."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._refreshing_guilds: set[int] = set()
        self._last_refresh_error = "No detailed error recorded."

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
            db.execute(
                """CREATE TABLE IF NOT EXISTS unteamed_extra_messages (
                    guild_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, position)
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
            # Changing the target means old continuation messages are no longer managed.
            db.execute("DELETE FROM unteamed_extra_messages WHERE guild_id=?", (guild_id,))
            db.commit()

    def _get_extra_ids(self, guild_id: int) -> list[int]:
        self._ensure_tracking_table()
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            rows = db.execute(
                "SELECT message_id FROM unteamed_extra_messages WHERE guild_id=? ORDER BY position",
                (guild_id,),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def _save_extra_ids(self, guild_id: int, message_ids: list[int]):
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            db.execute("DELETE FROM unteamed_extra_messages WHERE guild_id=?", (guild_id,))
            db.executemany(
                "INSERT INTO unteamed_extra_messages (guild_id, position, message_id) VALUES (?, ?, ?)",
                [(guild_id, i, mid) for i, mid in enumerate(message_ids, start=1)],
            )
            db.commit()

    def _make_chunks(self, guild: discord.Guild) -> list[str]:
        role = guild.get_role(PARTICIPANT_ROLE_ID)
        if role is None:
            return [
                "KASHMIR TITANS S1\n\nTEAMLESS USERS\n\n"
                f"ERROR: Participant role not found (ID: {PARTICIPANT_ROLE_ID})."
            ]

        teamless = sorted(
            (m for m in guild.members if not m.bot and role not in m.roles),
            key=lambda m: (m.name.casefold(), m.id),
        )
        rows = [f"{i}. {m.display_name} (@{m.name}) — {m.id}" for i, m in enumerate(teamless, start=1)]
        if not rows:
            rows = ["No teamless users."]

        # Split rows into Discord-safe messages, preserving every user.
        chunks: list[str] = []
        current = ""
        for row in rows:
            if len(row) > MAX_CONTENT - 80:
                row = row[: MAX_CONTENT - 83] + "..."
            candidate = f"{current}\n{row}" if current else row
            if len(candidate) > MAX_CONTENT - 100 and current:
                chunks.append(current)
                current = row
            else:
                current = candidate
        if current:
            chunks.append(current)

        total = len(chunks)
        return [
            f"KASHMIR TITANS S1\nTEAMLESS USERS — Part {i + 1}/{total}\n\n{chunk}"
            for i, chunk in enumerate(chunks)
        ]

    
    async def _refresh_saved_message(self, guild: discord.Guild) -> bool:
        if guild.id in self._refreshing_guilds:
            self._last_refresh_error = (
                "A refresh is already running. Wait a few seconds and try again."
            )
            print(f"[unteamed] Guild {guild.id}: refresh already in progress.")
            return False

        self._refreshing_guilds.add(guild.id)

        try:
            self._last_refresh_error = "No detailed error recorded."

            tracking = self._get_tracking(guild.id)
            if not tracking:
                raise ValueError(
                    "No saved target message. Run +setunteamed first."
                )

            # Populate the member cache before building the list.
            await guild.chunk(cache=True)

            channel_id, message_id = map(int, tracking)

            channel = guild.get_channel_or_thread(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)

            if not hasattr(channel, "fetch_message"):
                raise TypeError(
                    f"Channel {channel_id} cannot fetch messages."
                )

            primary = await channel.fetch_message(message_id)

            if self.bot.user is None:
                raise RuntimeError("The bot user is not ready.")

            if primary.author.id != self.bot.user.id:
                raise PermissionError(
                    f"Target message belongs to user ID {primary.author.id}, "
                    f"not this bot (ID {self.bot.user.id})."
                )

            chunks = self._make_chunks(guild)
            extra_ids = self._get_extra_ids(guild.id)
            new_extra_ids = []

            # Update the original saved message.
            await primary.edit(
                content=chunks[0],
                allowed_mentions=discord.AllowedMentions.none()
            )

            # Update existing continuation messages or create new ones.
            for i, content in enumerate(chunks[1:]):
                extra = None

                if i < len(extra_ids):
                    try:
                        extra = await channel.fetch_message(extra_ids[i])

                        if extra.author.id != self.bot.user.id:
                            raise PermissionError(
                                f"Continuation message {extra.id} "
                                "was not sent by this bot."
                            )

                    except discord.NotFound:
                        extra = None

                if extra is not None:
                    await extra.edit(
                        content=content,
                        allowed_mentions=discord.AllowedMentions.none()
                    )
                    new_extra_ids.append(extra.id)
                else:
                    extra = await channel.send(
                        content,
                        allowed_mentions=discord.AllowedMentions.none()
                    )
                    new_extra_ids.append(extra.id)

            # Delete obsolete continuation messages.
            for stale_id in extra_ids[len(chunks) - 1:]:
                try:
                    stale = await channel.fetch_message(stale_id)

                    if stale.author.id == self.bot.user.id:
                        await stale.delete()

                except (discord.NotFound, discord.Forbidden):
                    pass

            self._save_extra_ids(guild.id, new_extra_ids)
            return True

        except Exception as exc:
            self._last_refresh_error = f"{type(exc).__name__}: {exc}"

            print(
                f"[unteamed] Refresh failed | "
                f"Guild: {guild.id} | "
                f"Error: {self._last_refresh_error}",
                flush=True
            )

            return False

        finally:
            self._refreshing_guilds.discard(guild.id)


            # Delete stale continuation messages if the list has shrunk.
            for stale_id in extra_ids[len(chunks) - 1:]:
                try:
                    stale = await channel.fetch_message(stale_id)
                    if self.bot.user is not None and stale.author.id == self.bot.user.id:
                        await stale.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
            self._save_extra_ids(guild.id, new_extra_ids)
            return True
        except Exception as exc:
            print(f"[unteamed] Refresh failed for guild {guild.id}: {type(exc).__name__}: {exc}")
            self._last_refresh_error = f"{type(exc).__name__}: {exc}"
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
        before_has = any(r.id == PARTICIPANT_ROLE_ID for r in before.roles)
        after_has = any(r.id == PARTICIPANT_ROLE_ID for r in after.roles)
        if before_has != after_has:
            await self._refresh_saved_message(after.guild)

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            if self._get_tracking(guild.id):
                await self._refresh_saved_message(guild)

    async def _fetch_message_from_reference(self, ctx, reference: str):
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
            message_id, channel_id = int(reference), ctx.channel.id
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
        if self.bot.user is not None and message.author.id != self.bot.user.id:
            raise ValueError("The target message must have been sent by Googli Manager.")
        return message

    @commands.command(name="setunteamed")
    @commands.guild_only()
    async def set_unteamed_message(self, ctx: commands.Context, *, reference: str = ""):
        if ctx.author.id != OWNER_ID:
            await ctx.reply("Only the bot owner can use this command.", mention_author=False)
            return
        if not reference.strip():
            await ctx.reply("Usage: `+setunteamed <message link or message ID>`", mention_author=False)
            return
        try:
            message = await self._fetch_message_from_reference(ctx, reference)
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        self._save_tracking(ctx.guild.id, message.channel.id, message.id)
        await ctx.reply(
            f"Target message saved: {message.jump_url}\nAutomatic updates are enabled. Run `+unteamed` once to fill it now.",
            mention_author=False,
        )

    @commands.command(name="unteamed")
    @commands.guild_only()
    async def unteamed(self, ctx: commands.Context):
        if ctx.author.id != OWNER_ID:
            await ctx.reply("Only the bot owner can use this command.", mention_author=False)
            return
        if not self._get_tracking(ctx.guild.id):
            await ctx.reply("Choose the message first with `+setunteamed <message link>`.", mention_author=False)
            return
        if await self._refresh_saved_message(ctx.guild):
            await ctx.reply("Updated the saved teamless list.", mention_author=False)
        else:
            await ctx.reply(
                "I couldn't update the saved message. **Exact error:** "
                f"`{self._last_refresh_error[:1200]}`",
                mention_author=False,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Unteamed(bot))
