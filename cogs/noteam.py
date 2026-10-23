"""Standalone +noteam list updater for the KTS1 PARTICIPANTS role.

Load this extension from bot.py with: await bot.load_extension("cogs.noteam")
(or the appropriate module path for your project).
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

BOT_OWNER_ID = 1014568928682459198
PARTICIPANTS_ROLE_ID = 1560979656096743474
MESSAGE_CHAR_LIMIT = 1750  # safely below Discord's 2,000-character limit
UPDATE_DEBOUNCE_SECONDS = 2.0

# State is stored beside the bot's working directory so active lists survive restarts.
STATE_FILE = Path(os.getenv("NOTEAM_STATE_FILE", "noteam_state.json"))


class NoTeam(commands.Cog):
    """Maintains one live list per guild of human members lacking the participant role."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_lists: dict[str, dict] = {}
        self._update_tasks: dict[int, asyncio.Task] = {}
        self._state_lock = asyncio.Lock()
        self._load_state()

    def _load_state(self):
        try:
            if STATE_FILE.exists():
                raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.active_lists = raw
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not load noteam state (%s); starting with no active lists.", exc)
            self.active_lists = {}

    async def _save_state(self):
        async with self._state_lock:
            try:
                STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                temp_path = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
                temp_path.write_text(json.dumps(self.active_lists, indent=2), encoding="utf-8")
                temp_path.replace(STATE_FILE)
            except OSError:
                log.exception("Could not save noteam state. Automatic updates may not survive a restart.")

    def _is_bot_owner(self, ctx: commands.Context) -> bool:
        return ctx.author.id == BOT_OWNER_ID

    async def _members_without_role(self, guild: discord.Guild) -> list[discord.Member]:
        # Fetch/chunk when possible so the list is not limited to a partial member cache.
        try:
            await guild.chunk(cache=True)
        except (discord.HTTPException, discord.Forbidden, RuntimeError):
            log.warning("Could not fully chunk members for guild %s; using available member cache.", guild.id)

        role = guild.get_role(PARTICIPANTS_ROLE_ID)
        if role is None:
            raise LookupError(
                f"Could not find role ID {PARTICIPANTS_ROLE_ID} in server {guild.name}. "
                "Check the role ID and ensure the bot can view the server's roles."
            )

        members = list(guild.members)
        # If member intent/cache is incomplete, fetch the full member list where supported.
        if not guild.chunked:
            try:
                members = [member async for member in guild.fetch_members(limit=None)]
            except (discord.HTTPException, discord.Forbidden, discord.ClientException):
                log.warning("Full member fetch failed for guild %s; using available cache.", guild.id)

        humans = [m for m in members if not m.bot and role not in m.roles]
        return sorted(humans, key=lambda m: (m.display_name.casefold(), m.name.casefold(), m.id))

    @staticmethod
    def _make_chunks(members: list[discord.Member]) -> list[str]:
        if not members:
            return ["Everyone currently has the **KTS1 PARTICIPANTS** role.\n\n*No members to list.*"]

        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for index, member in enumerate(members, start=1):
            line = f"{index}. {discord.utils.escape_markdown(member.name)} ({member.id})"
            # Avoid accidental @mentions from names in the list.
            line = discord.utils.escape_mentions(line)
            if current and current_len + len(line) + 1 > MESSAGE_CHAR_LIMIT:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        return chunks

    async def _write_list(self, guild: discord.Guild, channel: discord.abc.Messageable, state: dict):
        members = await self._members_without_role(guild)
        chunks = self._make_chunks(members)
        message_ids = list(state.get("message_ids", []))
        saved_channel_id = int(state.get("channel_id", 0))

        # Edit the current list in place whenever possible.
        if saved_channel_id == getattr(channel, "id", None):
            old_messages: list[discord.Message | None] = []
            for message_id in message_ids:
                try:
                    old_messages.append(await channel.fetch_message(int(message_id)))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
                    old_messages.append(None)

            new_ids: list[int] = []
            for index, content in enumerate(chunks):
                if index < len(old_messages) and old_messages[index] is not None:
                    try:
                        await old_messages[index].edit(content=content, allowed_mentions=discord.AllowedMentions.none())
                        new_ids.append(old_messages[index].id)
                        continue
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                try:
                    message = await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
                    new_ids.append(message.id)
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Could not send a noteam list message in guild %s.", guild.id)
                    return

            # Remove surplus old messages if the list shrank.
            for old_message in old_messages[len(chunks):]:
                if old_message is not None:
                    try:
                        await old_message.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
            state["message_ids"] = new_ids
        else:
            # First run in a new channel: send a fresh list there and delete old tracked messages if possible.
            new_ids = []
            for content in chunks:
                try:
                    message = await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
                    new_ids.append(message.id)
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Could not send a noteam list message in guild %s.", guild.id)
                    return
            old_channel = guild.get_channel(saved_channel_id)
            if old_channel is not None:
                for message_id in message_ids:
                    try:
                        old_message = await old_channel.fetch_message(int(message_id))
                        await old_message.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
                        pass
            state["channel_id"] = channel.id
            state["message_ids"] = new_ids

        state["updated_count"] = len(members)
        state["updated_at"] = discord.utils.utcnow().isoformat()
        self.active_lists[str(guild.id)] = state
        await self._save_state()

    async def _refresh_guild(self, guild_id: int):
        state = self.active_lists.get(str(guild_id))
        if not state or not state.get("active", False):
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(int(state.get("channel_id", 0)))
        if channel is None:
            # Do not crash or spin if the tracked channel was deleted or is inaccessible.
            log.warning("The tracked noteam channel for guild %s is missing; stopping this list.", guild_id)
            state["active"] = False
            await self._save_state()
            return
        try:
            await self._write_list(guild, channel, state)
        except LookupError as exc:
            log.error("noteam refresh skipped: %s", exc)
        except Exception:
            log.exception("Unexpected error refreshing noteam list for guild %s.", guild_id)

    def _schedule_refresh(self, guild_id: int):
        state = self.active_lists.get(str(guild_id))
        if not state or not state.get("active", False):
            return
        task = self._update_tasks.get(guild_id)
        if task and not task.done():
            task.cancel()

        async def delayed_refresh():
            try:
                await asyncio.sleep(UPDATE_DEBOUNCE_SECONDS)
                await self._refresh_guild(guild_id)
            except asyncio.CancelledError:
                pass
            finally:
                if self._update_tasks.get(guild_id) is asyncio.current_task():
                    self._update_tasks.pop(guild_id, None)

        self._update_tasks[guild_id] = asyncio.create_task(delayed_refresh())

    @commands.command(name="noteam")
    @commands.guild_only()
    async def noteam(self, ctx: commands.Context):
        """Create the list, or refresh the existing list for this server."""
        if not self._is_bot_owner(ctx):
            await ctx.reply("Only the bot owner can use this command.", mention_author=False)
            return
        role = ctx.guild.get_role(PARTICIPANTS_ROLE_ID)
        if role is None:
            await ctx.reply(
                f"I couldn't find the configured participants role (`{PARTICIPANTS_ROLE_ID}`) in this server.",
                mention_author=False,
            )
            return
        state = self.active_lists.get(str(ctx.guild.id), {})
        state["active"] = True
        state["channel_id"] = ctx.channel.id if not state.get("message_ids") else state.get("channel_id", ctx.channel.id)
        self.active_lists[str(ctx.guild.id)] = state
        try:
            channel = ctx.guild.get_channel(int(state["channel_id"]))
            if channel is None:
                channel = ctx.channel
                state["channel_id"] = channel.id
            await self._write_list(ctx.guild, channel, state)
        except LookupError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        except (discord.Forbidden, discord.HTTPException):
            await ctx.reply("I couldn't update the list. Check that I can view the channel and send/edit messages.", mention_author=False)
            return
        await ctx.reply(
            f"Updated the live **No Team** list: **{state.get('updated_count', 0)}** human member(s) currently lack the participants role.\n"
            "It will refresh automatically when the server membership or participants role changes. Run `+stopnoteam` to stop automatic updates.",
            mention_author=False,
        )

    @commands.command(name="stopnoteam")
    @commands.guild_only()
    async def stop_noteam(self, ctx: commands.Context):
        """Stop future automatic edits while leaving the current list messages in place."""
        if not self._is_bot_owner(ctx):
            await ctx.reply("Only the bot owner can use this command.", mention_author=False)
            return
        state = self.active_lists.get(str(ctx.guild.id))
        if not state or not state.get("active", False):
            await ctx.reply("There is no active No Team list to stop in this server.", mention_author=False)
            return
        state["active"] = False
        task = self._update_tasks.pop(ctx.guild.id, None)
        if task and not task.done():
            task.cancel()
        await self._save_state()
        await ctx.reply("Automatic updates are now **stopped**. The list messages are left in place so you can delete them manually.", mention_author=False)

    @commands.Cog.listener()
    async def on_ready(self):
        # Resume tracked active lists after reconnect/restart.
        for guild_key, state in list(self.active_lists.items()):
            if state.get("active", False):
                try:
                    self._schedule_refresh(int(guild_key))
                except (TypeError, ValueError):
                    log.warning("Ignoring invalid guild ID in noteam state: %r", guild_key)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        self._schedule_refresh(member.guild.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        self._schedule_refresh(member.guild.id)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        role_id = PARTICIPANTS_ROLE_ID
        before_has = any(role.id == role_id for role in before.roles)
        after_has = any(role.id == role_id for role in after.roles)
        if before_has != after_has:
            self._schedule_refresh(after.guild.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(NoTeam(bot))
