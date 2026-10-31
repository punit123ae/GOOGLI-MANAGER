"""Custom fielder lines for Googli Manager.

Every player can save one custom line for when they TAKE a catch and one for
when they DROP a catch.  fielding.py calls ``send_line`` right after the Hand
Cricket bot posts "TOOK THE CATCH" / "BOZO DROPPED THE CATCH", so the line
appears immediately after that result message.

Lines are stored in the bot's SQLite database (bot.db), so they survive
restarts.  Storage and lookup are per (server, player), so 100+ players is no
problem: each catch result costs a single indexed lookup.

Player commands
    +settakeline <text>     line sent when YOU take a catch
    +setdropline <text>     line sent when YOU drop a catch
    +mylines                show your saved lines
    +clearlines [take|drop] remove your lines (both if you give no argument)
Admin commands (bot owner or Manage Server)
    +clearlinesfor @user [take|drop]
"""
from __future__ import annotations

import re
import time
from typing import Optional

import discord
from discord.ext import commands

BOT_OWNER_ID = 1014568928682459198
MAX_LINE_LENGTH = 300
NO_MENTIONS = discord.AllowedMentions.none()
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)
_INVITE_RE = re.compile(r"discord\.gg|discord(?:app)?\.com/invite", re.IGNORECASE)
# GIF links are allowed so Discord shows them as a normal GIF preview.
_GIF_HOSTS = (
    "tenor.com", "giphy.com", "imgur.com", "gfycat.com",
    "cdn.discordapp.com", "media.discordapp.net", "klipy.com",
)
_GIF_EXT = (".gif", ".gifv", ".webp", ".mp4")


def _is_gif_url(url: str) -> bool:
    m = re.match(r"(?:https?://|www\.)([^/?#\s]+)([^?#\s]*)", url, re.IGNORECASE)
    if not m:
        return False
    host = m.group(1).lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    path = m.group(2).lower()
    if any(host == h or host.endswith("." + h) for h in _GIF_HOSTS):
        return True
    return path.endswith(_GIF_EXT)
_KINDS = {"take": "take_text", "drop": "drop_text"}


def clean_line(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Return (cleaned_text, error).  Exactly one of them is None."""
    text = " ".join((raw or "").split())  # one line, single spaces
    if not text:
        return None, "The line can't be empty."
    if len(text) > MAX_LINE_LENGTH:
        return None, f"That's {len(text)} characters. The limit is {MAX_LINE_LENGTH}."
    if _INVITE_RE.search(text):
        return None, "Server invites aren't allowed in custom lines."
    for url in _URL_RE.findall(text):
        if not _is_gif_url(url):
            return None, ("Only GIF links are allowed (Tenor, Giphy, Imgur or a direct "
                          "`.gif` link). Other links are blocked.")
    if text[0] in "+/!":
        return None, "A line can't start with `+`, `/` or `!` (it could trigger other bots)."
    return text, None


class FielderLines(commands.Cog):
    """Custom text the bot posts after a player takes or drops a catch."""

    def __init__(self, bot):
        self.bot = bot
        bot.db.execute(
            """CREATE TABLE IF NOT EXISTS fielder_lines (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                take_text TEXT,
                drop_text TEXT,
                updated_at REAL NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )"""
        )
        bot.db.commit()

    # ------------------------------------------------------------------ diagnostics
    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        """Print why one of OUR commands failed or was blocked (console only)."""
        if ctx.command is not None and ctx.command.cog is self:
            print(f"[fielderlines] +{ctx.command.name} failed: {type(error).__name__}: {error}")

    @commands.Cog.listener()
    async def on_command(self, ctx):
        if ctx.command is not None and ctx.command.cog is self:
            print(f"[fielderlines] +{ctx.command.name} used by {ctx.author.id}")

    # ------------------------------------------------------------------ storage
    def _get(self, guild_id: int, user_id: int) -> tuple[Optional[str], Optional[str]]:
        row = self.bot.db.execute(
            "SELECT take_text, drop_text FROM fielder_lines WHERE guild_id=? AND user_id=?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def _set(self, guild_id: int, user_id: int, kind: str, text: Optional[str]):
        column = _KINDS[kind]  # fixed whitelist, never user input
        db = self.bot.db
        db.execute(
            f"INSERT INTO fielder_lines (guild_id, user_id, {column}, updated_at) "
            f"VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            f"{column}=excluded.{column}, updated_at=excluded.updated_at",
            (int(guild_id), int(user_id), text, time.time()),
        )
        # Drop the row entirely once a player has no lines left.
        db.execute(
            "DELETE FROM fielder_lines WHERE guild_id=? AND user_id=? "
            "AND take_text IS NULL AND drop_text IS NULL",
            (int(guild_id), int(user_id)),
        )
        db.commit()

    # ------------------------------------------------------------------ called by fielding.py
    async def send_line(self, channel, fielder_id: int, result: str) -> bool:
        """Send the fielder's custom line for ``result`` ('taken' or 'dropped')."""
        take, drop = self._get(channel.guild.id, fielder_id)
        text = take if result == "taken" else drop
        if not text:
            return False
        await channel.send(text, allowed_mentions=NO_MENTIONS)
        return True

    # ------------------------------------------------------------------ player commands
    async def _save(self, ctx, kind: str, text: str):
        cleaned, error = clean_line(text)
        if error:
            await ctx.reply(f"❌ {error}", mention_author=False, allowed_mentions=NO_MENTIONS)
            return
        self._set(ctx.guild.id, ctx.author.id, kind, cleaned)
        label = "take a catch" if kind == "take" else "drop a catch"
        await ctx.reply(
            f"✅ Saved. When you {label}, I'll send:\n{cleaned}",
            mention_author=False, allowed_mentions=NO_MENTIONS)

    @commands.command(name="settakeline")
    @commands.guild_only()
    async def set_take_line(self, ctx, *, text: str = ""):
        """Set the line sent when you take a catch."""
        if not text.strip():
            await ctx.reply("Usage: `+settakeline <your line>`", mention_author=False)
            return
        await self._save(ctx, "take", text)

    @commands.command(name="setdropline")
    @commands.guild_only()
    async def set_drop_line(self, ctx, *, text: str = ""):
        """Set the line sent when you drop a catch."""
        if not text.strip():
            await ctx.reply("Usage: `+setdropline <your line>`", mention_author=False)
            return
        await self._save(ctx, "drop", text)

    @commands.command(name="mylines")
    @commands.guild_only()
    async def my_lines(self, ctx):
        """Show your saved fielder lines."""
        take, drop = self._get(ctx.guild.id, ctx.author.id)
        await ctx.reply(
            f"**Your lines**\n**Take a catch:** {take or '*not set* (`+settakeline`)'}\n"
            f"**Drop a catch:** {drop or '*not set* (`+setdropline`)'}",
            mention_author=False, allowed_mentions=NO_MENTIONS)

    @commands.command(name="clearlines")
    @commands.guild_only()
    async def clear_lines(self, ctx, which: str = "both"):
        """Remove your take line, drop line, or both."""
        which = which.lower()
        if which not in ("take", "drop", "both"):
            await ctx.reply("Usage: `+clearlines`, `+clearlines take` or `+clearlines drop`",
                            mention_author=False)
            return
        for kind in (("take", "drop") if which == "both" else (which,)):
            self._set(ctx.guild.id, ctx.author.id, kind, None)
        await ctx.reply("🗑️ Removed." if which == "both" else f"🗑️ Removed your {which} line.",
                        mention_author=False)

    # ------------------------------------------------------------------ admin
    @commands.command(name="clearlinesfor")
    @commands.guild_only()
    async def clear_lines_for(self, ctx, member: discord.Member, which: str = "both"):
        """Admin: remove a player's custom lines (e.g. an inappropriate one)."""
        if ctx.author.id != BOT_OWNER_ID and not ctx.author.guild_permissions.manage_guild:
            await ctx.reply("❌ You need the Manage Server permission for this.", mention_author=False)
            return
        which = which.lower()
        if which not in ("take", "drop", "both"):
            await ctx.reply("Usage: `+clearlinesfor @user [take|drop]`", mention_author=False)
            return
        for kind in (("take", "drop") if which == "both" else (which,)):
            self._set(ctx.guild.id, member.id, kind, None)
        await ctx.reply(
            f"🗑️ Removed {which if which != 'both' else 'all'} custom line(s) for "
            f"**{discord.utils.escape_markdown(member.name)}**.",
            mention_author=False, allowed_mentions=NO_MENTIONS)


async def setup(bot):
    await bot.add_cog(FielderLines(bot))
    print("[fielderlines] loaded: +settakeline +setdropline +mylines +clearlines")
